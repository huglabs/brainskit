"""The extractor's AST cache, given somewhere durable to live.

`infrastructure/codeanalysis/cache.py` (vendored, unedited) already knows how
to skip reparsing a file whose content it has already seen. Handing it a
`tempfile.TemporaryDirectory()` on every call — the prior behaviour — threw
that away: the cache started cold every time, which is not a caching bug
inside the vendored code but a wiring choice one layer up, in
`infrastructure/extractor.py`. This file is about that wiring:

- the cache survives across calls when given a durable `cache_root`;
- a changed file gets reparsed, an unchanged one does not;
- a broken cache directory degrades to a full, correct re-extraction rather
  than a wrong or partial graph;
- the cache lands under the vault's own `.brain/`, never in the scanned
  repository or the process cwd; and
- a brainkit vault's own `.brain/*.json` state files are never handed to the
  extractor at all, so the upstream "please report the file(s) (#1666)"
  warning about files brainkit excluded from the graph on purpose never
  fires.

`GraphifyExtractor.extract` is exercised directly (not through `CodeGraph` or
`bk code build`) — the same layer `test_code_graph.py`'s
`test_build_and_import_agree_on_the_same_input` already calls into.
"""

from __future__ import annotations

import contextlib
import io
import tempfile
import unittest
import unittest.mock
from pathlib import Path
from typing import Any

from test_projections import policy

from brainkit.infrastructure.extractor import GraphifyExtractor, _cache_format_marker
from brainkit.infrastructure.vault import FileVault


def _code_extra_installed() -> bool:
    try:
        import tree_sitter  # noqa: F401
        import tree_sitter_python  # noqa: F401
    except ImportError:
        return False
    return True


#: Same rationale as `test_code_graph.py`'s guard of the same name: real
#: extraction needs the `code` extra's compiled grammars, which the dev
#: environment may or may not have installed.
_HAS_CODE_EXTRA = _code_extra_installed()


def _node_paths(payload: dict[str, Any]) -> set[str]:
    return {str(node["source_file"]) for node in payload["nodes"]}


def _edge_identity(payload: dict[str, Any]) -> set[tuple[str, str, str]]:
    return {
        (str(edge.get("source")), str(edge.get("target")), str(edge.get("relation") or edge.get("type")))
        for edge in payload["edges"]
    }


@unittest.skipUnless(_HAS_CODE_EXTRA, "requires the `code` extra (tree-sitter + grammars)")
class CodeCacheFixture(unittest.TestCase):
    """A small real repository plus a vault nested inside it, extracted
    in-process — the layout `infrastructure/vault.py::code_root` documents as
    the common one, and the one `test_code_graph.py`'s own fixtures use.
    """

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.repo = Path(self.temporary.name) / "repo"
        (self.repo / "src").mkdir(parents=True)
        (self.repo / ".git").mkdir()
        (self.repo / "src" / "a.py").write_text(
            "class A:\n    def m(self):\n        return 1\n", encoding="utf-8"
        )
        (self.repo / "src" / "b.py").write_text(
            "from src.a import A\n\n\ndef use():\n    return A().m()\n", encoding="utf-8"
        )
        (self.repo / "src" / "c.py").write_text(
            "def helper(x):\n    return x * 2\n", encoding="utf-8"
        )
        self.vault = FileVault.initialize(self.repo / "docs" / "brain", policy())
        self.extractor = GraphifyExtractor()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def build(self, **kwargs: Any) -> dict[str, Any]:
        return self.extractor.extract(self.vault.code_root(), [self.vault.code_root()], **kwargs)

    def ast_entries(self) -> list[Path]:
        return sorted(self.vault.code_cache_dir.rglob("*.json"))


class PersistenceTest(CodeCacheFixture):
    def test_cache_persists_across_two_builds(self) -> None:
        first = self.build(cache_root=self.vault.code_cache_dir)
        entries = self.ast_entries()
        self.assertGreaterEqual(len(entries), 3)  # a.py, b.py, c.py

        second = self.build(cache_root=self.vault.code_cache_dir)
        self.assertEqual(_node_paths(first), _node_paths(second))
        self.assertEqual(_edge_identity(first), _edge_identity(second))
        # Rebuilding with nothing changed must not rewrite a single entry —
        # every one of them should be served from the cache, not reparsed
        # and resaved.
        after = {p: p.stat().st_mtime_ns for p in self.ast_entries()}
        before = {p: p.stat().st_mtime_ns for p in entries}
        self.assertEqual(before, after)

    def test_a_changed_file_is_reparsed_while_unchanged_ones_are_not(self) -> None:
        self.build(cache_root=self.vault.code_cache_dir)
        before = {p: p.stat().st_mtime_ns for p in self.ast_entries()}

        (self.repo / "src" / "c.py").write_text(
            "def helper(x):\n    return x * 3  # changed\n", encoding="utf-8"
        )
        self.build(cache_root=self.vault.code_cache_dir)
        after = {p: p.stat().st_mtime_ns for p in self.ast_entries()}

        # c.py's old content-hash-keyed entry is untouched (a stale orphan,
        # never rewritten); every entry that existed before the edit still
        # has the exact same mtime it had — none of them were reparsed.
        for path, mtime in before.items():
            self.assertEqual(after.get(path), mtime, f"{path} was rewritten unnecessarily")
        # And a new entry appeared for c.py's new content hash — proof it WAS
        # reparsed rather than silently reusing a.py/b.py's cache freshness.
        self.assertGreater(len(after), len(before))


class CorruptionTest(CodeCacheFixture):
    def test_cache_directory_blocked_by_a_stray_file_falls_back_to_full_extract(self) -> None:
        # The exact path `_prepare_cache_dir` would try to create for this
        # vendored revision, pre-empted by a plain file.
        blocker = self.vault.code_cache_dir / f"v{_cache_format_marker()}"
        blocker.parent.mkdir(parents=True, exist_ok=True)
        blocker.write_text("not a directory", encoding="utf-8")

        baseline = self.build()  # cache_root=None: known-good, uncached extraction
        result = self.build(cache_root=self.vault.code_cache_dir)

        self.assertEqual(_node_paths(baseline), _node_paths(result))
        self.assertEqual(_edge_identity(baseline), _edge_identity(result))
        # The blocker must survive untouched — falling back means "use a
        # throwaway directory instead", never "clear the caller's path".
        self.assertTrue(blocker.is_file())

    def test_a_cache_subdirectory_replaced_by_a_file_falls_back_to_full_extract(self) -> None:
        baseline = self.build(cache_root=self.vault.code_cache_dir)
        graphify_out_dirs = list(self.vault.code_cache_dir.rglob("graphify-out"))
        self.assertEqual(len(graphify_out_dirs), 1)
        ast_dir = graphify_out_dirs[0] / "cache" / "ast"
        self.assertTrue(ast_dir.is_dir())

        # Corrupt below the layer `_prepare_cache_dir`'s write-probe checks:
        # the marker directory itself stays a perfectly usable directory, but
        # a vendored subdirectory one level down is now a plain file.
        import shutil

        shutil.rmtree(ast_dir)
        ast_dir.write_text("not a directory either", encoding="utf-8")

        result = self.build(cache_root=self.vault.code_cache_dir)
        self.assertEqual(_node_paths(baseline), _node_paths(result))
        self.assertEqual(_edge_identity(baseline), _edge_identity(result))

    def test_a_corrupted_cache_entry_self_heals_rather_than_producing_a_wrong_graph(self) -> None:
        baseline = self.build(cache_root=self.vault.code_cache_dir)
        entries = self.ast_entries()
        self.assertTrue(entries)
        entries[0].write_text("{not valid json", encoding="utf-8")

        result = self.build(cache_root=self.vault.code_cache_dir)
        self.assertEqual(_node_paths(baseline), _node_paths(result))
        self.assertEqual(_edge_identity(baseline), _edge_identity(result))


class ContainmentTest(CodeCacheFixture):
    def test_cache_lands_under_the_vault_and_nowhere_else(self) -> None:
        import os

        cwd_before = Path.cwd()
        os.chdir(self.temporary.name)  # somewhere that is neither the repo nor brainkit's own tree
        try:
            self.build(cache_root=self.vault.code_cache_dir)
        finally:
            os.chdir(cwd_before)

        self.assertTrue(self.vault.code_cache_dir.is_dir())
        self.assertTrue(any(self.vault.code_cache_dir.rglob("*.json")))
        # The vendored cache is free to write its own `graphify-out/` *inside*
        # the directory brainkit handed it; what must never happen is one
        # appearing anywhere else — beside the user's source, at the repo
        # root, or in whatever the cwd happened to be.
        cache_dir = self.vault.code_cache_dir.resolve()
        stray = [
            path
            for path in self.repo.resolve().rglob("graphify-out")
            if cache_dir not in path.parents
        ]
        self.assertEqual(stray, [])
        self.assertEqual(list(Path(self.temporary.name).glob("graphify-out")), [])
        # The vault's own `.brain/` holds only the code cache under the name
        # this module chose for it — not a bare `graphify-out` sitting beside
        # the other vault state files.
        self.assertFalse((self.vault.root / ".brain" / "graphify-out").exists())

    def test_falling_back_to_a_throwaway_directory_still_avoids_the_repo_and_cwd(self) -> None:
        import os

        cwd_before = Path.cwd()
        os.chdir(self.temporary.name)
        try:
            self.build()  # cache_root=None
        finally:
            os.chdir(cwd_before)

        self.assertEqual(list(self.repo.rglob("graphify-out")), [])
        self.assertEqual(list(Path(self.temporary.name).glob("graphify-out")), [])


class IdentityTest(CodeCacheFixture):
    def test_a_warm_graph_equals_a_cold_graph(self) -> None:
        cold = self.build()  # cache_root=None
        warm_first = self.build(cache_root=self.vault.code_cache_dir)
        warm_second = self.build(cache_root=self.vault.code_cache_dir)

        self.assertEqual(_node_paths(cold), _node_paths(warm_first))
        self.assertEqual(_node_paths(warm_first), _node_paths(warm_second))
        self.assertEqual(_edge_identity(cold), _edge_identity(warm_first))
        self.assertEqual(_edge_identity(warm_first), _edge_identity(warm_second))
        self.assertEqual(len(cold["nodes"]), len(warm_first["nodes"]))
        self.assertEqual(len(cold["edges"]), len(warm_first["edges"]))


class VaultStateFilterTest(CodeCacheFixture):
    def test_the_vaults_own_state_files_never_reach_the_extractor(self) -> None:
        # `code_root()` is the repo, which contains the vault
        # (`docs/brain/.brain/*.json`) — the layout that used to trip
        # upstream's "please report the file(s) (#1666)" warning about files
        # brainkit already excludes from the graph on purpose.
        buffer = io.StringIO()
        with contextlib.redirect_stderr(buffer):
            result = self.build()
        self.assertNotIn("1666", buffer.getvalue())
        self.assertFalse(
            any(".brain" in Path(p).parts for p in _node_paths(result)),
            "a vault state file reached the graph",
        )


class StdoutIsolationTest(unittest.TestCase):
    """The vendored extractor's own progress lines must never land on stdout.

    `codeanalysis/extract.py` prints cold-cache "AST extraction: N/M uncached
    files" progress with no `file=sys.stderr` once a scan crosses its internal
    batch threshold. `bk code build --json` also writes to stdout — its JSON
    result — so an unredirected call interleaves plain text into that payload
    and breaks every parser reading it. Stubs the vendored call rather than
    needing a 100+ file real scan to cross that threshold; this exercises the
    same `GraphifyExtractor._run` call site regardless of what the vendored
    code happens to print or when.
    """

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        (self.root / "a.py").write_text("x = 1\n", encoding="utf-8")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_a_noisy_vendored_call_never_reaches_real_stdout(self) -> None:
        def noisy_extract(
            files: list[Path], cache_root: Path | None = None, *, root: Path | None = None
        ) -> dict[str, Any]:
            print("  AST extraction: 100/580 uncached files (17%) [12 workers]")
            return {"nodes": [], "edges": []}

        def collect_one(target: Path, *, root: Path | None = None) -> list[Path]:
            return [self.root / "a.py"]

        stdout_buffer, stderr_buffer = io.StringIO(), io.StringIO()
        with unittest.mock.patch(
            "brainkit.infrastructure.extractor._load",
            return_value=(noisy_extract, collect_one),
        ):
            with contextlib.redirect_stdout(stdout_buffer), contextlib.redirect_stderr(
                stderr_buffer
            ):
                GraphifyExtractor().extract(self.root, [self.root])

        self.assertEqual(stdout_buffer.getvalue(), "")
        self.assertIn("AST extraction", stderr_buffer.getvalue())
