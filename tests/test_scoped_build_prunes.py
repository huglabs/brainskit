"""A scoped rebuild must prune what was deleted, and nothing else.

Two failures, in opposite directions, and the second was shipped as the fix for
the first.

`_merge_scoped` originally kept every stored node whose path fell outside the
current scope, so a deleted file's nodes survived indefinitely unless someone
ran a whole-root build: rebuild one file, and every query answers with phantoms
-- symbols at line numbers in files that no longer exist.

The fix asked `vault.code_hash(path) is not None` and pruned whatever came back
`None`, on the reasoning that `_write` records the `files` map with that same
call, so the path base is shared by construction. It is -- **within one build**.
`code_root()` re-resolves on every call, from a config key an operator may edit
and an upward walk for `.git`, and the artefact recorded no root at all. Change
`code_root`, or import a graph an external extractor emitted relative to its own
working directory, and every stored path resolves to a missing file: the merge
read a whole out-of-scope graph as deleted and destroyed it, then `_write`
recomputed `files` from the survivors so the truncated graph fingerprinted as
consistent and reported `fresh`.

So pruning now needs positive evidence out of the artefact -- the root it was
written under, and a recorded digest for the path -- and this file tests both
directions: that a genuine deletion is still pruned, and that nothing else ever
is. The tests drive `build()` through a fake extractor rather than calling
`_merge_scoped` directly, because the defect lives in the relationship between
two builds and only a real second build has one.

The earlier fixture ran entirely under `code_root() == vault root`, a layout
where the two bases cannot diverge -- so it could not have caught this. The
repository here is a real one (`.git`, sources beside a `docs/brain` vault),
which is the layout the defect needs.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from brainskit.application.codegraph import CODE_PROJECTION
from brainskit.application.services import BrainskitService
from brainskit.domain.model import RefusalError
from brainskit.infrastructure.index import SqliteFtsIndex
from brainskit.infrastructure.vault import FileVault

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_engine import policy


class _FakeExtractor:
    """Reports the `.py` files under whatever root it is handed.

    Stands in for `GraphifyExtractor` so these tests run without the `code`
    extra. What matters for the defect is only the property every real
    extractor has: **`source_file` is relative to the root it was given**, so
    the same file yields a different path under a different root.

    It emits a node per file and a node per `def`, because a symbol that
    disappears from a file that still exists is the only signal that separates
    "this scope was replaced" from "this scope was merged into". A `# uses:`
    comment declares an edge, which keeps the fixture's graph legible.
    """

    def available(self) -> bool:
        return True

    def extract(
        self,
        root: Path,
        paths: list[Path] | None = None,
        *,
        cache_root: Path | None = None,
    ) -> dict[str, Any]:
        targets = [
            candidate
            for candidate in sorted(root.rglob("*.py"))
            if paths is None or self._in_scope(candidate, root, paths)
        ]
        nodes = []
        links = []
        for source in targets:
            relative = source.relative_to(root).as_posix()
            nodes.append(
                {
                    "id": relative,
                    "label": source.stem,
                    "source_file": relative,
                    "source_location": "L1",
                    "file_type": "code",
                }
            )
            for line in source.read_text(encoding="utf-8").splitlines():
                if line.startswith("# uses:"):
                    links.append(
                        {
                            "source": relative,
                            "target": line.split(":", 1)[1].strip(),
                            "relation": "imports_from",
                            "source_file": relative,
                            "source_location": "L1",
                        }
                    )
                elif line.startswith("def "):
                    symbol = line[4:].split("(", 1)[0]
                    nodes.append(
                        {
                            "id": f"{relative}::{symbol}",
                            "label": symbol,
                            "source_file": relative,
                            "source_location": "L1",
                            "file_type": "code",
                        }
                    )
        return {"nodes": nodes, "links": links}

    @staticmethod
    def _in_scope(candidate: Path, root: Path, paths: list[Path]) -> bool:
        for target in paths:
            resolved = (target if target.is_absolute() else root / target).resolve()
            if candidate == resolved or resolved in candidate.parents:
                return True
        return False


class ScopedBuildFixture(unittest.TestCase):
    """A repository with a vault inside it — the layout the defect needs.

    `code_root()` discovers the repository by walking up for `.git`, so node
    paths are recorded as `src/a.py`. Pointing `code_root` at `src/` later is
    what makes the recorded base and the resolving base disagree.
    """

    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.repo = Path(self.temporary.name).resolve() / "proj"
        (self.repo / "src").mkdir(parents=True)
        (self.repo / ".git").mkdir()
        (self.repo / "src" / "a.py").write_text("def alpha():\n    pass\n", "utf-8")
        (self.repo / "src" / "b.py").write_text(
            "# uses: src/c.py\ndef beta():\n    pass\n", "utf-8"
        )
        (self.repo / "src" / "c.py").write_text("def gamma():\n    pass\n", "utf-8")

        self.vault = FileVault.initialize(self.repo / "docs" / "brain", policy())
        self.service = BrainskitService(
            self.vault,
            SqliteFtsIndex(self.vault.index_path),
            extractor=_FakeExtractor(),
        )

    # -------------------------------------------------------------- utilities

    def graph(self) -> dict[str, Any]:
        return json.loads((self.vault.root / CODE_PROJECTION).read_text("utf-8"))

    def node_paths(self) -> set[str]:
        return {str(node["path"]) for node in self.graph()["nodes"]}

    def point_code_root_at(self, relative: str) -> None:
        """Rewrite `.brain/config.json` the way an operator would.

        Documented in `docs/code-graph.md`, and `config()` re-reads on every
        call, so this takes effect on the very next command.
        """

        path = self.vault.root / ".brain" / "config.json"
        config = json.loads(path.read_text(encoding="utf-8"))
        config["code_root"] = relative
        path.write_text(json.dumps(config, indent=2), encoding="utf-8")

    def store_graph(self, graph: dict[str, Any]) -> None:
        """Put an artefact on disk directly, for shapes a build cannot produce."""

        target = self.vault.root / CODE_PROJECTION
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(graph, indent=2) + "\n", encoding="utf-8")


class DeletedFilesArePrunedTest(ScopedBuildFixture):
    """The behaviour the guard must not cost: a real deletion still prunes."""

    def test_the_whole_root_build_sees_all_three_files(self) -> None:
        """Control: if this fails, every assertion below is about nothing."""

        result = self.service.code_build()
        self.assertEqual(self.node_paths(), {"src/a.py", "src/b.py", "src/c.py"})
        self.assertEqual(result["files"], 3)

    def test_an_out_of_scope_file_that_still_exists_is_kept(self) -> None:
        """Control: preserving out-of-scope nodes is the merge's whole purpose."""

        self.service.code_build()
        self.service.code_build(["src/a.py"])
        self.assertIn("src/c.py", self.node_paths())

    def test_an_out_of_scope_file_that_was_deleted_is_pruned(self) -> None:
        self.service.code_build()
        (self.repo / "src" / "c.py").unlink()

        self.service.code_build(["src/a.py"])

        self.assertNotIn("src/c.py", self.node_paths())

    def test_the_scoped_file_survives_the_prune(self) -> None:
        self.service.code_build()
        (self.repo / "src" / "c.py").unlink()

        self.service.code_build(["src/a.py"])

        self.assertIn("src/a.py", self.node_paths())

    def test_no_edge_dangles_after_a_prune(self) -> None:
        """An edge to a pruned node would break every traversal.

        The fixture's `src/b.py` declares an edge to `src/c.py`, so this loop
        has a body: the previous version of this test ran over an empty edge
        list and would have passed with the dangling guard deleted.
        """

        self.service.code_build()
        before = self.graph()
        doomed = {
            str(node["id"])
            for node in before["nodes"]
            if str(node["path"]) == "src/c.py"
        }
        self.assertTrue(
            [edge for edge in before["edges"] if str(edge["target"]) in doomed],
            "the fixture must have an edge into the file the prune removes",
        )
        (self.repo / "src" / "c.py").unlink()

        self.service.code_build(["src/a.py"])

        graph = self.graph()
        ids = {str(node["id"]) for node in graph["nodes"]}
        for edge in graph["edges"]:
            self.assertIn(str(edge["source"]), ids)
            self.assertIn(str(edge["target"]), ids)

    def test_the_prune_agrees_with_what_status_calls_removed(self) -> None:
        """The two must not hold different opinions about one file."""

        self.service.code_build()
        (self.repo / "src" / "c.py").unlink()

        removed = set(self.service.code_status().get("removed", []))
        self.service.code_build(["src/a.py"])

        self.assertIn("src/c.py", removed)
        self.assertEqual(removed & self.node_paths(), set())


class DivergingBaseKeepsEverythingTest(ScopedBuildFixture):
    """The regression: a base the artefact never recorded destroyed the graph."""

    def test_the_artefact_records_the_root_its_paths_are_relative_to(self) -> None:
        self.service.code_build()
        self.assertEqual(
            self.graph()["code_root"], self.vault.code_root().resolve().as_posix()
        )

    def test_pointing_code_root_elsewhere_prunes_nothing(self) -> None:
        self.service.code_build()
        self.point_code_root_at("../../src")

        self.service.code_build(["a.py"])

        # The three original nodes are all unresolvable under the new root, and
        # every one of them survives; `a.py` is what this build contributed.
        self.assertEqual(
            self.node_paths(), {"src/a.py", "src/b.py", "src/c.py", "a.py"}
        )

    def test_the_build_says_why_it_pruned_nothing(self) -> None:
        self.service.code_build()
        self.point_code_root_at("../../src")

        result = self.service.code_build(["a.py"])

        self.assertIn("different code root", result["prune_skipped"])
        self.assertEqual(
            result["stored_code_root"], (self.repo).resolve().as_posix()
        )
        self.assertEqual(result["code_root"], (self.repo / "src").resolve().as_posix())

    def test_the_kept_nodes_are_still_disclosed_as_stale(self) -> None:
        """Keeping them is only safe because nothing calls them current."""

        self.service.code_build()
        self.point_code_root_at("../../src")
        self.service.code_build(["a.py"])

        status = self.service.code_status()
        self.assertTrue(status["stale"])
        self.assertEqual(
            set(status["removed"]), {"src/a.py", "src/b.py", "src/c.py"}
        )

    def test_a_graph_imported_from_a_foreign_base_survives_a_scoped_build(self) -> None:
        """The second trigger, and it needs no config edit at all.

        An external extractor run from another working directory emits
        `source_file` relative to *its* base, so `_write` records `""` for every
        digest. The stored root then matches the one resolving -- only the
        recorded digests say those paths were never resolvable here.
        """

        self.service.code_import(
            {
                "nodes": [
                    {
                        "id": "elsewhere/x.py",
                        "label": "x",
                        "source_file": "elsewhere/x.py",
                        "source_location": "L1",
                        "file_type": "code",
                    }
                ],
                "links": [],
            }
        )
        self.assertEqual(self.graph()["files"], {"elsewhere/x.py": ""})

        self.service.code_build(["src/a.py"])

        self.assertIn("elsewhere/x.py", self.node_paths())

    def test_a_graph_that_records_no_root_prunes_nothing(self) -> None:
        """Migration: every artefact written before this field existed.

        An unknown base cannot be confirmed, so the safe reading is that
        nothing is known to be deleted. The next whole-root build restates the
        root and pruning resumes.
        """

        self.service.code_build()
        stored = self.graph()
        del stored["code_root"]
        self.store_graph(stored)
        (self.repo / "src" / "c.py").unlink()

        result = self.service.code_build(["src/a.py"])

        self.assertIn("src/c.py", self.node_paths())
        self.assertIn("records no code root", result["prune_skipped"])

    def test_restating_the_root_lets_pruning_resume(self) -> None:
        """Control: the migration rule is a pause, not a permanent disable."""

        self.service.code_build()
        stored = self.graph()
        del stored["code_root"]
        self.store_graph(stored)

        self.service.code_build()  # whole root: restates code_root
        (self.repo / "src" / "c.py").unlink()
        self.service.code_build(["src/a.py"])

        self.assertNotIn("src/c.py", self.node_paths())

    def test_a_matching_root_still_reports_no_reason_to_skip(self) -> None:
        """Control: the note fires on divergence, not on every scoped build."""

        self.service.code_build()
        result = self.service.code_build(["src/a.py"])
        self.assertNotIn("prune_skipped", result)


class ScopingTheRootItselfTest(ScopedBuildFixture):
    """`bk code build .` must mean what `bk code build` means.

    The signal is a symbol that disappeared from a file that still exists. A
    *deleted file* cannot tell these apart, because the deletion test prunes its
    nodes either way -- which is why this defect survived the scoped-build tests
    that already existed.
    """

    def drop_gamma(self) -> None:
        (self.repo / "src" / "c.py").write_text("def epsilon():\n    pass\n", "utf-8")

    def labels(self) -> set[str]:
        return {str(node["label"]) for node in self.graph()["nodes"]}

    def test_a_bare_build_drops_a_symbol_that_is_gone(self) -> None:
        """Control: this is the behaviour `.` has to match."""

        self.service.code_build()
        self.assertIn("gamma", self.labels())
        self.drop_gamma()

        self.service.code_build()

        self.assertNotIn("gamma", self.labels())

    def test_scoping_to_the_root_replaces_rather_than_merges(self) -> None:
        self.service.code_build()
        self.drop_gamma()

        self.service.code_build(["."])

        # `.` relativises to `""`, which contains every path. Before that it
        # became `"."` -- a prefix of nothing -- so every stored node counted as
        # out of scope, and a whole-root rebuild spelled with a path kept every
        # stale symbol a bare `bk code build` would have replaced.
        self.assertNotIn("gamma", self.labels())

    def test_scoping_to_the_root_with_a_trailing_slash_behaves_the_same(self) -> None:
        self.service.code_build()
        self.drop_gamma()

        self.service.code_build(["./"])

        self.assertNotIn("gamma", self.labels())

    def test_scoping_to_the_root_by_absolute_path_behaves_the_same(self) -> None:
        self.service.code_build()
        self.drop_gamma()

        self.service.code_build([str(self.vault.code_root())])

        self.assertNotIn("gamma", self.labels())

    def test_a_subdirectory_scope_still_keeps_what_is_outside_it(self) -> None:
        """Control: the root case must not make every scope the whole root."""

        (self.repo / "other").mkdir()
        (self.repo / "other" / "d.py").write_text("def delta():\n    pass\n", "utf-8")
        self.service.code_build()

        self.service.code_build(["src"])

        self.assertIn("other/d.py", self.node_paths())
        self.assertIn("delta", self.labels())


class MalformedStoredGraphTest(ScopedBuildFixture):
    """The stored graph is a disposable projection; it may be anything."""

    def test_an_edge_with_no_source_file_is_kept(self) -> None:
        """`_edges` never requires one, so the merge must not punish its absence.

        Node paths are filtered for emptiness on the way in (`_nodes` drops a
        path-less node); edge paths are not. Reading `""` as a deleted file
        pruned an edge whose endpoints both survived.
        """

        self.service.code_build()
        stored = self.graph()
        for edge in stored["edges"]:
            edge["path"] = ""
        self.store_graph(stored)

        self.service.code_build(["src/a.py"])

        self.assertTrue(
            self.graph()["edges"], "an edge with no recorded path must survive"
        )

    def test_an_edge_with_no_source_file_is_kept_even_beside_an_empty_digest(
        self,
    ) -> None:
        """The narrow case the recorded-digest rule alone does not cover.

        `_write` never puts an empty key in `files`, so `files.get("")` is
        normally `None` and an edge with no path is kept for that reason. A
        hand-edited artefact can put one there, and then only the emptiness
        check stands between a perfectly good edge and a prune.
        """

        self.service.code_build()
        stored = self.graph()
        stored["files"][""] = "0" * 64
        for edge in stored["edges"]:
            edge["path"] = ""
        self.store_graph(stored)

        self.service.code_build(["src/a.py"])

        self.assertTrue(self.graph()["edges"], "an edge with no path is not a deletion")

    def _store_a_malformed_edge(self) -> None:
        self.service.code_build()
        stored = self.graph()
        self.assertTrue(stored["edges"], "fixture must produce an edge to strip")
        for edge in stored["edges"]:
            del edge["type"]
        self.store_graph(stored)

    def test_a_scoped_build_refuses_a_malformed_stored_graph(self) -> None:
        """Not crashing was only half of it — it was still laundering the fault.

        The first fix stopped the `KeyError` by reading the stored edge with
        `.get`, which made `build` succeed and write the malformed edge back
        out unchanged. So the corruption survived every rebuild while `build`
        reported success, and `affected` and `path` -- which do subscript
        `type` -- kept failing on an artefact the build had just blessed.
        """

        self._store_a_malformed_edge()

        with self.assertRaises(RefusalError) as caught:
            self.service.code_build(["src/a.py"])

        self.assertIn("bk code build", caught.exception.details["hint"])

    def test_the_refused_merge_leaves_the_artefact_exactly_as_it_found_it(self) -> None:
        """Refusing must not be a third way to damage the graph."""

        self._store_a_malformed_edge()
        before = (self.vault.root / CODE_PROJECTION).read_text(encoding="utf-8")

        with self.assertRaises(RefusalError):
            self.service.code_build(["src/a.py"])

        self.assertEqual(
            (self.vault.root / CODE_PROJECTION).read_text(encoding="utf-8"), before
        )

    def test_a_whole_root_build_is_the_remedy_and_stays_reachable(self) -> None:
        """The refusal is only defensible because recovery costs one command.

        A whole-root build never merges, so it cannot inherit the fault however
        corrupt the artefact is. If this ever fails, the refusal above has
        stranded the operator instead of redirecting them.
        """

        self._store_a_malformed_edge()

        result = self.service.code_build()

        self.assertGreater(result["nodes"], 0)
        self.assertTrue(all("type" in edge for edge in self.graph()["edges"]))
        self.assertTrue(self.service.code_hubs()["hubs"])

    def test_a_sound_stored_graph_still_merges(self) -> None:
        """Control: the check refuses malformed graphs, not scoped builds."""

        self.service.code_build()

        result = self.service.code_build(["src/a.py"])

        self.assertGreater(result["nodes"], 0)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
