"""A golden corpus for the vendored extractors, driven through the port.

`infrastructure/codeanalysis/` is roughly 27,600 lines of vendored Graphify,
byte-identical by contract and forbidden to edit. `test_vendoring.py` pins that
provenance exhaustively — 43 sha256s, the two declaration lists, the packaged
attribution. What it does not do, and what nothing else did either, is check
that any of that code *extracts the right thing*: roughly forty languages
shipped with zero behavioural tests between them.

That pairing is the problem. Un-editable code with no behavioural coverage
means a per-language bug is simultaneously undetectable (nothing would notice)
and unfixable in place (the fix would have to live outside the tree). It also
makes the one operation the NOTICE is designed around — re-vendoring, "a copy
rather than a merge" — a leap of faith: the sha pin proves the bytes changed,
and then nothing says whether the graph they produce still means the same
thing. This corpus is what turns that into a decision someone can read.

What is asserted
----------------

For each `tests/fixtures/<language>/`: extract `source/` and compare the result
against the committed `expected.json`. Nodes *and* edges, in full — labels,
relations, line numbers, metadata — because the cheap version of this test
(count the nodes, check a name is present) passes through exactly the drift it
exists to catch.

Extraction goes through `CodeExtractorPort` — `extract` and `survey`, nothing
else. Binding to the port rather than to `graphify.extract` is deliberate:
that seam is the one brainskit owns and the one production actually calls, so
a re-vendor that moved a vendored internal cannot quietly leave these tests
exercising something no caller reaches. It is also what makes the per-language
skip honest: `survey()` reports which grammars a scan needs and whether they
are installed, so a machine with `code` but not `code-all` skips the languages
it genuinely cannot parse and runs the rest, instead of the whole file
vanishing behind one import guard.

Adding a language is one directory
----------------------------------

    tests/fixtures/<language>/
        source/…            the files to extract (the scan root)
        expected.json       the golden, deliberately *outside* the scan root

Nothing here holds a list of languages: `discover()` walks `tests/fixtures/`.
Write the sources, run the regenerator, declare the paradigm, commit.

`expected.json` carries a `paradigm`, because the point of the corpus is the
*spread* rather than the count — the vendored extractors come in three shapes
and a corpus of twelve config-driven languages would test one of them twelve
times. A new fixture is written with `paradigm: "UNDECLARED"` and
`FixtureShapeTest` fails until a human classifies it.

Regenerating, and why it cannot rubber-stamp a regression
---------------------------------------------------------

    python tests/test_language_extraction.py --regenerate [language …]

A golden that any failure can refresh is not a test. So regeneration reads the
sha256 of every fixture source out of the golden it is about to replace, and
classifies before writing:

  sources changed    → the input moved. Rewriting is the whole point. Written.
  nothing changed    → no-op.
  sources unchanged,
  graph changed      → the *extractor* moved, which is the regression case
                       exactly. REFUSED, with the diff printed.

Unlocking that last case takes a second, differently-named flag
(`--accept-extractor-change`), so "regenerate the goldens" — the thing someone
types when a test is red and they are in a hurry — can never be the thing that
erases the finding. `RegenerationGuardTest` exercises all four verdicts,
including the control that the flag is what unlocks the refusal rather than
the refusal being unconditional.

Normalisation
-------------

Compared as a normalised projection, not as raw output:

- **sorted** — nodes by id, edges by (source, relation, target, file, line),
  each with the record's own canonical JSON as the tie-break. Emission order is
  an implementation detail of the walk (and of whether the extractor took its
  parallel path), never a claim about the code being parsed.
- **path-relative** — the scan root is the fixture's own `source/`, so every
  `source_file` and every derived node id is relative to it and identical on
  every machine. `test_the_golden_names_no_absolute_path` asserts that rather
  than assuming it.
- **posix separators** — so a Windows checkout compares equal.
- **JSON-native** — every value coerced to a type that survives a round trip,
  so "equal to the golden" means equal to what was committed rather than equal
  to a repr.

Nothing is dropped. Line numbers and metadata are kept even though they make
the golden noisier, because a re-vendor that shifts a symbol's line or renames
a metadata key *is* a change worth being shown; the corpus exists to make that
visible, not comfortable. Determinism was verified before choosing this:
across three separate interpreters and with the parallel path both enabled and
forced off (`BK_NO_PARALLEL`), the twelve fixtures produced byte-identical
output.
"""

from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import sys
import unittest
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, ClassVar

from brainskit.application.ports import CodeExtractorPort
from brainskit.infrastructure.extractor import GraphifyExtractor

FIXTURES = Path(__file__).resolve().parent / "fixtures"

#: The name of the subdirectory that is handed to the extractor. The golden
#: sits beside it rather than inside it so that `expected.json` is never itself
#: a file the scan collects -- which it would be, `.json` being an extension
#: `_DISPATCH` dispatches on.
SOURCE_DIR = "source"

GOLDEN = "expected.json"

#: The three shapes the vendored extractors come in, which is what the corpus
#: is chosen to span. Read off the dispatch table rather than invented:
#:
#: - `config-engine` -- `extract_python` and friends are three-line functions
#:   delegating to `_extract_generic(path, _X_CONFIG)`; the behaviour under
#:   test is the shared engine plus one declarative `LanguageConfig`.
#: - `standalone-tree-sitter` -- 100-400 lines of hand-rolled extractor with
#:   its own grammar and its own node vocabulary (`extractors/go.py`,
#:   `extractors/sql.py`, …). Every one is a separate implementation.
#: - `no-grammar` -- a reader that never touches tree-sitter at all
#:   (`extractors/markdown.py`), so it stays exercised on a machine with no
#:   compiled grammars installed whatsoever.
PARADIGMS = frozenset({"config-engine", "standalone-tree-sitter", "no-grammar"})

UNDECLARED = "UNDECLARED"


@dataclass(frozen=True, slots=True)
class Fixture:
    language: str
    directory: Path

    @property
    def source_root(self) -> Path:
        return self.directory / SOURCE_DIR

    @property
    def golden_path(self) -> Path:
        return self.directory / GOLDEN

    def sources(self) -> list[Path]:
        return sorted(p for p in self.source_root.rglob("*") if p.is_file())

    def digests(self) -> dict[str, str]:
        """sha256 of every fixture source, keyed by its path under `source/`.

        This is what ties a golden to the bytes it was generated from, and so
        what lets the regenerator tell "the fixture changed" from "the
        extractor changed" without being told which happened.
        """

        return {
            path.relative_to(self.source_root).as_posix(): hashlib.sha256(
                path.read_bytes()
            ).hexdigest()
            for path in self.sources()
        }

    def golden(self) -> dict[str, Any]:
        return json.loads(self.golden_path.read_text(encoding="utf-8"))


def discover() -> list[Fixture]:
    """Every fixture directory, found rather than listed.

    A directory qualifies by having a `source/`; that is the whole registration
    mechanism. `FixtureShapeTest` then insists the rest of the contract is met,
    so a half-built fixture fails loudly instead of being skipped silently.
    """

    if not FIXTURES.is_dir():
        return []
    return [
        Fixture(language=child.name, directory=child)
        for child in sorted(FIXTURES.iterdir())
        if (child / SOURCE_DIR).is_dir()
    ]


def _jsonable(value: Any) -> Any:
    """Coerce to something a JSON round trip preserves exactly.

    The corpus is JSON-native today -- verified across all twelve fixtures --
    but "today" is doing real work in that sentence: the comparison is against
    bytes on disk, so a value that only survives as its `repr` would make a
    golden that can never match again. Coercing here means a re-vendor that
    starts emitting a `Path` or a tuple produces a diff about the graph rather
    than a `TypeError` about serialisation.
    """

    if isinstance(value, Path | PurePosixPath):
        return PurePosixPath(value).as_posix()
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_jsonable(item) for item in value]
    if isinstance(value, str | bool | int | float) or value is None:
        return value
    return str(value)


def _canonical(record: dict[str, Any]) -> dict[str, Any]:
    canonical = {str(key): _jsonable(value) for key, value in record.items()}
    path = canonical.get("source_file")
    if isinstance(path, str):
        canonical["source_file"] = PurePosixPath(path.replace("\\", "/")).as_posix()
    return canonical


def _key(record: dict[str, Any], *fields: str) -> tuple[str, ...]:
    return (
        *(str(record.get(field, "")) for field in fields),
        json.dumps(record, sort_keys=True),
    )


def normalise(payload: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    """The projection the goldens are written in. See the module docstring."""

    nodes = [_canonical(node) for node in payload.get("nodes", [])]
    edges = [_canonical(edge) for edge in payload.get("edges", [])]
    nodes.sort(key=lambda node: _key(node, "id", "source_file", "source_location"))
    edges.sort(
        key=lambda edge: _key(
            edge, "source", "relation", "target", "source_file", "source_location"
        )
    )
    return {"nodes": nodes, "edges": edges}


def port() -> CodeExtractorPort:
    """The seam under test, named as the seam rather than as the adapter."""

    return GraphifyExtractor()


def extract(fixture: Fixture) -> dict[str, list[dict[str, Any]]]:
    return normalise(port().extract(fixture.source_root.resolve()))


def missing_grammars(fixture: Fixture) -> list[str]:
    """Which distributions this fixture needs and this machine lacks.

    Asked of `survey()` -- a port method -- rather than of an import table, so
    the answer comes from the same code that decides what a real scan would
    reach for.
    """

    survey = port().survey(fixture.source_root.resolve())
    return sorted(need.distribution for need in survey.grammars if not need.installed)


def render(golden: dict[str, Any]) -> str:
    return json.dumps(golden, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def diff(before: str, after: str, label: str) -> str:
    return "".join(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile=f"{label} (committed)",
            tofile=f"{label} (extracted now)",
        )
    )


# --------------------------------------------------------------------------
# regeneration
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Verdict:
    action: str  # "written" | "unchanged" | "refused" | "new"
    reason: str
    payload: dict[str, Any]
    diff: str = ""


def plan_regeneration(
    *,
    existing: dict[str, Any] | None,
    language: str,
    digests: dict[str, str],
    graph: dict[str, list[dict[str, Any]]],
    accept_extractor_change: bool = False,
) -> Verdict:
    """Decide what regenerating this fixture would mean, before doing it.

    Pure, and separate from the writing, so the refusal can be tested without
    a real extractor or a real fixture on disk -- see `RegenerationGuardTest`.

    The whole design rests on one distinction. A golden is a claim about
    *inputs -> outputs*, so:

    - if the inputs moved, the claim is simply out of date and rewriting it is
      the intended workflow;
    - if the inputs did not move and the output did, then the code under test
      changed behaviour. That is either a regression or an improvement, and
      either way it is a finding. Overwriting the golden there deletes the only
      evidence, which is exactly how a corpus stops being a test.

    So the second case is refused by default and needs its own flag, named for
    what it actually asserts rather than for what the operator wants.
    """

    fresh = {
        "language": language,
        "paradigm": (existing or {}).get("paradigm", UNDECLARED),
        "cross_file": (existing or {}).get("cross_file", len(digests) > 1),
        "sources": digests,
        **graph,
    }
    if existing is None:
        return Verdict("new", "no golden yet", fresh)

    rendered_before = render(existing)
    rendered_after = render(fresh)
    if rendered_before == rendered_after:
        return Verdict("unchanged", "nothing moved", fresh)

    sources_moved = existing.get("sources") != digests
    graph_moved = {
        "nodes": existing.get("nodes"),
        "edges": existing.get("edges"),
    } != {"nodes": graph["nodes"], "edges": graph["edges"]}
    delta = diff(rendered_before, rendered_after, f"{language}/{GOLDEN}")

    if graph_moved and not sources_moved and not accept_extractor_change:
        return Verdict(
            "refused",
            (
                "the fixture sources are byte-identical and the extracted graph "
                "is not: the extractor changed behaviour. That is the finding "
                "this corpus exists to surface, so --regenerate will not "
                "overwrite it. Read the diff; if the new output is correct, "
                "re-run with --accept-extractor-change."
            ),
            fresh,
            delta,
        )
    if sources_moved:
        reason = "the fixture sources changed"
    elif graph_moved:
        reason = "extractor change accepted explicitly"
    else:
        reason = "metadata only"
    return Verdict("written", reason, fresh, delta)


def regenerate(languages: list[str], *, accept_extractor_change: bool) -> int:
    selected = [f for f in discover() if not languages or f.language in languages]
    unknown = sorted(set(languages) - {f.language for f in discover()})
    if unknown:
        print(f"no such fixture: {', '.join(unknown)}", file=sys.stderr)
        return 2
    refused = 0
    for fixture in selected:
        absent = missing_grammars(fixture)
        if absent:
            print(f"{fixture.language}: skipped, needs {', '.join(absent)}")
            continue
        existing = fixture.golden() if fixture.golden_path.is_file() else None
        verdict = plan_regeneration(
            existing=existing,
            language=fixture.language,
            digests=fixture.digests(),
            graph=extract(fixture),
            accept_extractor_change=accept_extractor_change,
        )
        if verdict.diff:
            print(verdict.diff, end="")
        if verdict.action == "refused":
            refused += 1
            print(f"{fixture.language}: REFUSED -- {verdict.reason}\n", file=sys.stderr)
            continue
        if verdict.action == "unchanged":
            print(f"{fixture.language}: unchanged")
            continue
        fixture.golden_path.write_text(render(verdict.payload), encoding="utf-8")
        print(f"{fixture.language}: {verdict.action} ({verdict.reason})")
        if verdict.payload["paradigm"] == UNDECLARED:
            print(
                f"  declare {fixture.language}'s paradigm in {GOLDEN}: "
                f"one of {', '.join(sorted(PARADIGMS))}",
                file=sys.stderr,
            )
    return 1 if refused else 0


# --------------------------------------------------------------------------
# tests
# --------------------------------------------------------------------------


class CorpusShapeTest(unittest.TestCase):
    """Control: the corpus must not be able to pass by being empty.

    Every per-language test below is generated from `discover()`. If discovery
    returned nothing -- a moved directory, a renamed `source/` -- the file would
    report a clean run having asserted nothing at all, which is the failure mode
    a generated suite is most prone to and least likely to notice.
    """

    def test_the_corpus_is_not_empty(self) -> None:
        self.assertGreaterEqual(
            len(discover()),
            8,
            "the language corpus is missing or was not discovered; every "
            "per-language test below is generated from it, so a suite that "
            "finds no fixtures passes without asserting anything",
        )

    def test_every_extractor_paradigm_is_represented(self) -> None:
        """The corpus is chosen for spread, so the spread is what is asserted.

        Twelve config-driven languages would exercise one code path twelve
        times and leave `extractors/sql.py` and `extractors/markdown.py` --
        which share nothing with it -- as untested as they were before.
        """

        covered = {
            fixture.golden().get("paradigm")
            for fixture in discover()
            if fixture.golden_path.is_file()
        }
        self.assertEqual(
            PARADIGMS - covered,
            set(),
            f"no fixture covers {sorted(PARADIGMS - covered)}",
        )

    def test_at_least_one_fixture_resolves_a_reference_across_files(self) -> None:
        """Single-file fixtures cannot exercise the resolution layer at all.

        `resolver_registry` plus `ruby_resolution`/`pascal_resolution`/
        `symbol_resolution` exist to connect a call in one file to a definition
        in another, and a corpus of one-file fixtures would leave every one of
        them unexercised while looking thorough.
        """

        declared = [
            fixture
            for fixture in discover()
            if fixture.golden_path.is_file() and fixture.golden().get("cross_file")
        ]
        self.assertTrue(declared, "no fixture claims to cross a file boundary")
        for fixture in declared:
            with self.subTest(language=fixture.language):
                golden = fixture.golden()
                home = {node["id"]: node.get("source_file") for node in golden["nodes"]}
                spanning = [
                    edge
                    for edge in golden["edges"]
                    if edge["source"] in home
                    and edge["target"] in home
                    and home[edge["source"]] != home[edge["target"]]
                    and home[edge["source"]]
                    and home[edge["target"]]
                ]
                self.assertTrue(
                    spanning,
                    f"{fixture.language} is declared cross-file but every edge "
                    "in its golden stays inside one file",
                )


class FixtureShapeTest(unittest.TestCase):
    """Each directory is a complete fixture, or it fails rather than skips."""

    def test_every_fixture_has_sources(self) -> None:
        for fixture in discover():
            with self.subTest(language=fixture.language):
                self.assertTrue(fixture.sources(), "source/ is empty")

    def test_every_fixture_has_a_committed_golden(self) -> None:
        for fixture in discover():
            with self.subTest(language=fixture.language):
                self.assertTrue(
                    fixture.golden_path.is_file(),
                    f"run: python {Path(__file__).name} --regenerate "
                    f"{fixture.language}",
                )

    def test_every_golden_declares_a_known_paradigm(self) -> None:
        for fixture in discover():
            if not fixture.golden_path.is_file():
                continue
            with self.subTest(language=fixture.language):
                self.assertIn(
                    fixture.golden().get("paradigm"),
                    PARADIGMS,
                    "a new fixture is written with paradigm UNDECLARED and has "
                    "to be classified by hand: the corpus is chosen for the "
                    "spread of extractor shapes it covers, and nothing can "
                    "derive that from the file extension",
                )

    def test_the_golden_is_committed_in_its_canonical_rendering(self) -> None:
        """Otherwise a hand-edit produces a diff the regenerator silently undoes."""

        for fixture in discover():
            if not fixture.golden_path.is_file():
                continue
            with self.subTest(language=fixture.language):
                self.assertEqual(
                    fixture.golden_path.read_text(encoding="utf-8"),
                    render(fixture.golden()),
                )

    def test_the_golden_names_no_absolute_path(self) -> None:
        """The normalisation claim, asserted instead of assumed.

        Every path in a golden is relative to that fixture's own `source/`. An
        absolute one would make the corpus pass on the machine that generated
        it and nowhere else.
        """

        for fixture in discover():
            if not fixture.golden_path.is_file():
                continue
            with self.subTest(language=fixture.language):
                offenders = [
                    record.get("source_file")
                    for key in ("nodes", "edges")
                    for record in fixture.golden()[key]
                    if isinstance(record.get("source_file"), str)
                    and (
                        record["source_file"].startswith("/")
                        or record["source_file"].startswith("..")
                        or ":" in record["source_file"]
                    )
                ]
                self.assertEqual(offenders, [], f"absolute paths: {offenders}")

    def test_the_golden_lives_outside_the_scan_root(self) -> None:
        """`expected.json` inside `source/` would be extracted as a fixture file."""

        for fixture in discover():
            with self.subTest(language=fixture.language):
                self.assertFalse((fixture.source_root / GOLDEN).exists())


class LanguageCorpusTest(unittest.TestCase):
    """One generated test per language; see `_attach` below."""

    def _check(self, fixture: Fixture) -> None:
        """Compare first, explain second.

        The graph diff is checked ahead of the source digests on purpose. Both
        conditions fire together whenever someone edits a fixture -- and if the
        digest assertion ran first it would short-circuit with "regenerate
        this", which is the right instruction attached to none of the evidence.
        The diff is the part a reader can act on; whether the sources moved
        only decides which sentence goes above it.
        """

        absent = missing_grammars(fixture)
        if absent:
            self.skipTest(f"needs {', '.join(absent)} (`code-all` extra)")

        golden = fixture.golden()
        digests = fixture.digests()
        stale = golden.get("sources") != digests
        edited = (
            f"{fixture.language}'s fixture sources were edited and its golden "
            f"was not regenerated."
            if stale
            else f"{fixture.language}'s fixture sources are byte-identical, so "
            f"the extractor changed behaviour."
        )
        rerun = (
            f"Run: python {Path(__file__).name} --regenerate {fixture.language}"
            if stale
            else "Read the diff before regenerating: --regenerate refuses this "
            "case without --accept-extractor-change, and that refusal is the "
            "point."
        )

        live = extract(fixture)
        expected = {"nodes": golden["nodes"], "edges": golden["edges"]}
        if live != expected:
            self.fail(
                f"{fixture.language} extraction no longer matches its golden.\n"
                f"{edited}\n{rerun}\n\n"
                + diff(
                    render({**golden, **expected}),
                    render({**golden, **live}),
                    f"{fixture.language}/{GOLDEN}",
                )
            )
        self.assertFalse(
            stale,
            f"{edited} The graph is unchanged, but the golden's recorded "
            f"digests no longer describe the files it was generated from. "
            f"{rerun}",
        )

    def test_the_generated_tests_exist(self) -> None:
        """Control: `_attach` must actually have attached something.

        A generation loop that silently produced nothing would leave this class
        holding one passing test and no coverage.
        """

        generated = [name for name in dir(self) if name.startswith("test_extracts_")]
        self.assertGreaterEqual(len(generated), 8, generated)


def _attach() -> None:
    for fixture in discover():

        def check(self: LanguageCorpusTest, fixture: Fixture = fixture) -> None:
            self._check(fixture)

        check.__name__ = f"test_extracts_{fixture.language}"
        check.__doc__ = f"{fixture.language}: nodes and edges match the golden."
        setattr(LanguageCorpusTest, check.__name__, check)


_attach()


class RegenerationGuardTest(unittest.TestCase):
    """`--regenerate` must not be able to erase a real behavioural finding.

    Exercised against `plan_regeneration` directly -- it is pure, so the four
    verdicts can be driven with synthetic inputs rather than by mutating the
    committed corpus.
    """

    GRAPH: ClassVar[dict[str, list[dict[str, Any]]]] = {
        "nodes": [{"id": "a", "source_file": "a.py"}],
        "edges": [{"source": "a", "target": "a", "relation": "contains"}],
    }
    MOVED: ClassVar[dict[str, list[dict[str, Any]]]] = {
        "nodes": [{"id": "renamed", "source_file": "a.py"}],
        "edges": [{"source": "renamed", "target": "renamed", "relation": "contains"}],
    }

    def golden(self, **overrides: Any) -> dict[str, Any]:
        base = {
            "language": "probe",
            "paradigm": "config-engine",
            "cross_file": False,
            "sources": {"a.py": "digest-one"},
            **self.GRAPH,
        }
        base.update(overrides)
        return base

    def plan(self, **kwargs: Any) -> Verdict:
        defaults: dict[str, Any] = {
            "existing": self.golden(),
            "language": "probe",
            "digests": {"a.py": "digest-one"},
            "graph": self.GRAPH,
        }
        defaults.update(kwargs)
        return plan_regeneration(**defaults)

    def test_nothing_moved_is_a_no_op(self) -> None:
        self.assertEqual(self.plan().action, "unchanged")

    def test_a_changed_fixture_source_is_rewritten(self) -> None:
        """The intended workflow: new inputs, so the recorded outputs are stale."""

        verdict = self.plan(digests={"a.py": "digest-two"}, graph=self.MOVED)
        self.assertEqual(verdict.action, "written")
        self.assertIn("sources changed", verdict.reason)

    def test_a_changed_extractor_with_unchanged_sources_is_refused(self) -> None:
        """The regression case, which is the only reason this guard exists."""

        verdict = self.plan(graph=self.MOVED)
        self.assertEqual(verdict.action, "refused", verdict.reason)
        self.assertIn("extractor changed behaviour", verdict.reason)
        self.assertIn("--accept-extractor-change", verdict.reason)

    def test_the_refusal_carries_the_diff_that_justifies_it(self) -> None:
        """A refusal nobody can read is a refusal that gets worked around."""

        verdict = self.plan(graph=self.MOVED)
        self.assertIn("renamed", verdict.diff)
        self.assertIn("-", verdict.diff)

    def test_the_explicit_flag_is_what_unlocks_it(self) -> None:
        """Control: the refusal must be the flag's doing, not unconditional.

        Without this, a `plan_regeneration` that refused every graph change
        would pass the test above while making the regenerator useless.
        """

        verdict = self.plan(graph=self.MOVED, accept_extractor_change=True)
        self.assertEqual(verdict.action, "written")
        self.assertIn("accepted explicitly", verdict.reason)

    def test_a_brand_new_fixture_is_written_undeclared(self) -> None:
        verdict = self.plan(existing=None)
        self.assertEqual(verdict.action, "new")
        self.assertEqual(verdict.payload["paradigm"], UNDECLARED)

    def test_regeneration_preserves_the_hand_declared_paradigm(self) -> None:
        """It is the one field a human owns; a rewrite must not reset it."""

        verdict = self.plan(
            existing=self.golden(paradigm="standalone-tree-sitter"),
            digests={"a.py": "digest-two"},
        )
        self.assertEqual(verdict.payload["paradigm"], "standalone-tree-sitter")


def _main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description="Regenerate the language corpus goldens.",
        epilog=(
            "Without --regenerate this file is an ordinary unittest module and "
            "every other argument is passed straight to unittest."
        ),
    )
    parser.add_argument("--regenerate", action="store_true")
    parser.add_argument(
        "--accept-extractor-change",
        action="store_true",
        help=(
            "allow a rewrite when the fixture sources are unchanged and the "
            "extracted graph is not -- i.e. when the extractor's behaviour "
            "moved. Read the printed diff first: this is the case the guard "
            "exists for."
        ),
    )
    parser.add_argument("language", nargs="*")
    known, rest = parser.parse_known_args(argv[1:])
    if not known.regenerate:
        unittest.main(argv=[argv[0], *argv[1:]])
        return 0
    if rest:
        parser.error(f"unrecognised arguments: {' '.join(rest)}")
    return regenerate(
        known.language, accept_extractor_change=known.accept_extractor_change
    )


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_main(sys.argv))
