#!/usr/bin/env python3
"""Measure what `bk code build` actually covers, and whether that is slipping.

The metric is **coverage**: files that produced at least one node, over files
whose extension has an extractor. Not node count, which is the number that
looked fine while three of four source files were being silently dropped —
a graph can grow while a whole language falls out of it, and only coverage
notices.

Two tiers, because they answer different questions:

- **`--fixtures`** (default, hermetic, no network). A generated polyglot tree,
  one minimal file per extension the shipped extractors claim. It answers "can
  this installation parse what it says it can", runs in seconds, and is the
  regression gate: the four-file repository that built a two-node graph would
  have failed here.
- **`--corpus`** (opt-in, clones real repositories at pinned commits). It
  answers "does this hold on real code, and what does it cost" — wall time,
  peak RSS, and the size of `graph/code.json`, which is how the 683 MB
  accident first became visible.

`--check` compares against `baseline.json` and exits non-zero on a regression
outside tolerance. Coverage may not fall at all; time and size have slack,
because they are machine-dependent in a way coverage is not.

    python benchmarks/run.py                     # fixtures
    python benchmarks/run.py --check             # fixtures vs baseline
    python benchmarks/run.py --corpus --record   # refresh the baseline
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent
BASELINE = ROOT / "baseline.json"
CORPUS = ROOT / "corpus.json"
# NOT `.cache`. The extractor prunes conventionally-ignored directories --
# `.cache` among them -- so clones stored there collect zero files and every
# repository in the corpus measured as "No code nodes". A benchmark whose
# fixtures live somewhere the tool refuses to look measures nothing, and it
# fails in the most misleading way available: as a total coverage collapse that
# reads like a defect in the thing under test.
CACHE = ROOT / "corpus-repos"

sys.path.insert(0, str(ROOT.parent / "src"))

# Imported at module scope, and that placement is load-bearing. The vendored
# extractor parallelises with a process pool, and on macOS those children are
# *spawned*: a fresh interpreter that re-imports `__main__` and inherits none of
# the parent's `sys.modules`. The `graphify` alias this import registers is
# synthetic -- no such distribution exists -- so a child that has not run this
# import cannot resolve the extractor function it was handed, and the pool
# reports "a process in the process pool was terminated abruptly".
#
# The symptom is not a crash but a *quiet* one: those files simply produce no
# nodes, so the first run of this benchmark measured 7.7% coverage on a fixture
# whose every file parses correctly in isolation. Anything embedding the
# extractor in-process needs this import at module scope for the same reason.
import brainskit.infrastructure.codeanalysis  # noqa: E402,F401

#: Coverage must never fall. Time and memory are machine-dependent, so they
#: only fail on a change large enough that no machine explains it.
TOLERANCE = {"coverage": 0.0, "seconds": 2.0, "graph_bytes": 1.35}

#: One minimal, syntactically valid sample per language. Deliberately tiny:
#: this measures whether a grammar is *reachable*, not how well it parses.
SAMPLES: dict[str, str] = {
    ".py": "def greet(name):\n    return f'hi {name}'\n",
    ".js": "export function greet(name) { return `hi ${name}`; }\n",
    ".ts": "export function greet(name: string): string { return `hi ${name}`; }\n",
    ".tsx": "export const App = (): JSX.Element => <div>hi</div>;\n",
    ".go": "package main\n\nfunc Greet(name string) string { return name }\n",
    ".rs": "pub fn greet(name: &str) -> String { name.to_string() }\n",
    ".java": "public class Greeter { public String greet() { return \"hi\"; } }\n",
    ".c": "#include <stdio.h>\nint greet(void) { return 0; }\n",
    ".cpp": "#include <string>\nstd::string greet() { return \"hi\"; }\n",
    ".cs": "public class Greeter { public string Greet() => \"hi\"; }\n",
    ".rb": "def greet(name)\n  \"hi #{name}\"\nend\n",
    ".php": "<?php\nfunction greet(string $name): string { return $name; }\n",
    ".sh": "#!/bin/sh\ngreet() { echo \"hi $1\"; }\n",
    ".sql": "CREATE TABLE users (id INT PRIMARY KEY, name TEXT);\n",
    ".swift": "struct User { let id: Int }\nfunc greet(u: User) -> String { \"hi\" }\n",
    ".tf": 'resource "aws_s3_bucket" "b" {\n  bucket = "example"\n}\n',
    ".kt": "fun greet(name: String): String = \"hi $name\"\n",
    ".scala": "object Greeter { def greet(name: String): String = name }\n",
    ".lua": "local function greet(name) return 'hi ' .. name end\nreturn greet\n",
    ".ex": "defmodule Greeter do\n  def greet(name), do: name\nend\n",
    ".jl": "function greet(name)\n    return name\nend\n",
    ".zig": "pub fn greet() u8 {\n    return 0;\n}\n",
    ".ps1": "function Get-Greeting { param($Name) return $Name }\n",
    ".m": "#import <Foundation/Foundation.h>\n@implementation Greeter\n@end\n",
    ".groovy": "class Greeter { String greet(String n) { return n } }\n",
    ".f90": "function greet(n)\n  integer :: greet, n\n  greet = n\nend function\n",
}


@dataclass
class Result:
    name: str
    files_seen: int = 0
    files_supported: int = 0
    #: Files whose extractor deliberately declined to index them. Reported
    #: rather than hidden, so the difference between "not indexed" and "failed
    #: to index" stays visible.
    files_skipped: int = 0
    files_covered: int = 0
    nodes: int = 0
    edges: int = 0
    seconds: float = 0.0
    graph_bytes: int = 0
    missing_grammars: list[str] = field(default_factory=list)
    error: str = ""

    @property
    def coverage(self) -> float:
        if not self.files_supported:
            return 1.0
        return round(self.files_covered / self.files_supported, 4)

    def row(self) -> str:
        if self.error:
            return f"  {self.name:<22} ERROR  {self.error[:60]}"
        gap = f"  missing: {', '.join(self.missing_grammars)}" if self.missing_grammars else ""
        return (
            f"  {self.name:<22} coverage {self.coverage:>6.1%}"
            f"  {self.files_covered:>4}/{self.files_supported:<4} files"
            f"  {self.nodes:>6} nodes  {self.seconds:>6.2f}s"
            f"  {self.graph_bytes / 1e6:>6.1f} MB{gap}"
        )


def build_fixture(target: Path) -> None:
    """Write one sample per language, plus a file with no extractor at all.

    The last one matters: a file nothing claims must not count against
    coverage, or the metric would punish a repository for containing a README.
    """

    target.mkdir(parents=True, exist_ok=True)
    for extension, body in SAMPLES.items():
        (target / f"sample{extension}").write_text(body, encoding="utf-8")
    (target / "README.md").write_text("# fixture\n", encoding="utf-8")
    (target / "notes.txt").write_text("not code\n", encoding="utf-8")


def measure(name: str, repo: Path) -> Result:
    """Build a graph over `repo` in a throwaway vault and measure the result."""

    from brainskit.application.codegraph import CodeGraph
    from brainskit.domain.model import BrainskitError
    from brainskit.infrastructure.extractor import (
        GraphifyExtractor,
        _extension_grammars,
    )
    from brainskit.infrastructure.vault import FileVault

    result = Result(name=name)
    with tempfile.TemporaryDirectory() as scratch:
        vault_root = Path(scratch) / "vault"
        try:
            # `code_root` is vault-relative by contract, and the vault lives in
            # a scratch directory so the benchmark never writes into the
            # repository it is measuring.
            relative = os.path.relpath(repo.resolve(), vault_root.resolve())
            vault = FileVault.initialize(vault_root, _policy(relative))
        except BrainskitError as exc:
            result.error = str(exc)
            return result

        graph = CodeGraph(vault, GraphifyExtractor())
        survey = graph.survey()
        if survey is not None:
            result.files_seen = survey.files
            result.missing_grammars = [n.distribution for n in survey.missing]

        # Supported = the extractor claims this extension AND intends to index
        # it. A missing grammar still counts, which is the point: that is a gap
        # in what this installation delivers. A *deliberate* skip does not --
        # `extract_json` refuses data-shaped JSON on purpose (upstream #1224,
        # after datasets swamped graphs with orphan key-nodes) and says so by
        # returning a `skipped` marker.
        #
        # Counting those as gaps is not a rounding error. It made Alamofire
        # read as 82.4% and look like a Swift extraction problem; all 98 Swift
        # files were covered, and the entire shortfall was 23 fixture JSON
        # files the extractor was never going to index. A coverage metric that
        # penalises correct behaviour points investigation at the wrong place.
        known = _extension_grammars()
        candidates = [
            path
            for path in repo.rglob("*")
            if path.is_file() and path.suffix.lower() in known
        ]
        skipped = _deliberately_skipped(candidates)
        result.files_skipped = len(skipped)
        result.files_supported = len(candidates) - len(skipped)

        started = time.monotonic()
        try:
            built = graph.build()
        except BrainskitError as exc:
            result.error = str(exc)
            return result
        result.seconds = round(time.monotonic() - started, 3)

        result.nodes = built["nodes"]
        result.edges = built["edges"]
        result.files_covered = built["files"]
        artefact = vault.root / "graph" / "code.json"
        result.graph_bytes = artefact.stat().st_size if artefact.is_file() else 0
    return result



def _deliberately_skipped(paths: list[Path]) -> list[Path]:
    """Files whose extractor returned a `skipped` marker rather than nodes.

    Three outcomes, not two: nodes, an error, or a deliberate decision not to
    index. Only the first two say anything about whether extraction works.
    """

    from graphify.extract import _get_extractor  # type: ignore[import-not-found]

    skipped = []
    for path in paths:
        extractor = _get_extractor(path)
        if extractor is None:
            continue
        try:
            result = extractor(path)
        except Exception:  # noqa: S112 -- an extractor that raises is not a
            # deliberate skip; it belongs in the uncovered count, which is what
            # continuing here leaves it as.
            continue
        if result.get("skipped") and not (result.get("nodes") or []):
            skipped.append(path)
    return skipped


def _policy(code_root: str) -> dict:
    from brainskit.domain.model import DEFAULT_IGNORE_PATTERNS, INTEGRATION_NAMES

    jobs = ("digest", "file-proposal", "ingest", "lint-semantic", "query", "resurface")
    return {
        "version": 3,
        "wiki_language": "English",
        "inbox_policy": {"privacy": "local-only", "filing": "approve-each"},
        "branches": {"10-work": {"privacy": "local-only", "filing": "approve-each"}},
        "providers": {"ollama": {"base_url": "http://127.0.0.1:11434"}},
        "job_models": {j: {"provider": "ollama", "model": "qwen2.5:3b"} for j in jobs},
        "sources": [],
        "ignore": list(DEFAULT_IGNORE_PATTERNS),
        "schedule": {"digest": "0 8 * * *"},
        "taxonomy_seed": ["10-work"],
        "novelty": {
            "duplicate_similarity_threshold": 0.9,
            "min_new_token_ratio": 0.15,
            "stale_after_days": 30,
        },
        "code_root": code_root,
        # The corpus contains repositories far larger than a default scan
        # allows; the ceiling is a safety rail for operators, not for a
        # benchmark that named its root deliberately.
        "code_scan_limit": 500_000,
        "integrations": {
            name: {"enabled": False, "managed": False, "options": {}}
            for name in sorted(INTEGRATION_NAMES)
        },
    }


def clone(entry: dict) -> Path | None:
    """Fetch one corpus repository at its pinned commit, cached across runs."""

    destination = CACHE / entry["name"]
    if (destination / ".git").is_dir():
        return destination
    CACHE.mkdir(parents=True, exist_ok=True)
    commands = [
        ["git", "init", "-q", str(destination)],
        ["git", "-C", str(destination), "remote", "add", "origin", entry["url"]],
        ["git", "-C", str(destination), "fetch", "-q", "--depth", "1", "origin", entry["commit"]],
        ["git", "-C", str(destination), "checkout", "-q", "FETCH_HEAD"],
    ]
    for command in commands:
        # Fixed argv, no shell; every value comes from the committed
        # corpus manifest, not from a caller.
        if subprocess.run(command, capture_output=True).returncode != 0:  # noqa: S603
            shutil.rmtree(destination, ignore_errors=True)
            print(f"  {entry['name']:<22} SKIP   could not fetch", file=sys.stderr)
            return None
    return destination


def compare(results: list[Result], baseline: dict) -> list[str]:
    """Regressions worth failing over, stated as sentences."""

    failures = []
    for result in results:
        previous = baseline.get(result.name)
        if previous is None:
            continue
        if result.coverage < previous["coverage"] - TOLERANCE["coverage"]:
            failures.append(
                f"{result.name}: coverage {previous['coverage']:.1%} -> "
                f"{result.coverage:.1%}"
            )
        if previous["seconds"] and result.seconds > previous["seconds"] * TOLERANCE["seconds"]:
            failures.append(
                f"{result.name}: {previous['seconds']:.2f}s -> {result.seconds:.2f}s"
            )
        if (
            previous["graph_bytes"]
            and result.graph_bytes > previous["graph_bytes"] * TOLERANCE["graph_bytes"]
        ):
            failures.append(
                f"{result.name}: graph {previous['graph_bytes'] / 1e6:.1f} MB -> "
                f"{result.graph_bytes / 1e6:.1f} MB"
            )
    return failures


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", action="store_true", help="Also clone and measure real repositories")
    parser.add_argument("--fixtures", action="store_true", help="Only the hermetic fixture (default)")
    parser.add_argument("--check", action="store_true", help="Fail on regression against baseline.json")
    parser.add_argument("--record", action="store_true", help="Overwrite baseline.json with this run")
    parser.add_argument("--json", action="store_true", help="Machine-readable output")
    args = parser.parse_args()

    results: list[Result] = []
    with tempfile.TemporaryDirectory() as scratch:
        fixture = Path(scratch) / "polyglot"
        build_fixture(fixture)
        results.append(measure("fixture:polyglot", fixture))

    if args.corpus:
        entries = json.loads(CORPUS.read_text())["repositories"] if CORPUS.is_file() else []
        for entry in entries:
            path = clone(entry)
            if path is not None:
                results.append(measure(entry["name"], path))

    if args.json:
        print(json.dumps([asdict(r) | {"coverage": r.coverage} for r in results], indent=2))
    else:
        print()
        for result in results:
            print(result.row())
        print()

    if args.record:
        BASELINE.write_text(
            json.dumps(
                {r.name: asdict(r) | {"coverage": r.coverage} for r in results},
                indent=2,
            )
            + "\n"
        )
        print(f"baseline written to {BASELINE}")

    if args.check:
        baseline = json.loads(BASELINE.read_text()) if BASELINE.is_file() else {}
        if not baseline:
            print("no baseline recorded; run with --record first", file=sys.stderr)
            return 1
        failures = compare(results, baseline)
        for line in failures:
            print(f"REGRESSION  {line}", file=sys.stderr)
        return 1 if failures else 0

    return 1 if any(r.error for r in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
