"""A scoped rebuild must not keep nodes for files that are gone.

`_merge_scoped` kept every stored node whose path fell outside the current
scope, so a deleted file's nodes survived indefinitely unless someone ran a
whole-root build. Combined with a `status` that truthfully reported `stale`, the
failure read: rebuild one file, then every query answers with phantoms --
symbols at line numbers in files that no longer exist.

The fixture puts its files inside `code_root()`, which is where `code_hash`
looks. A first attempt at this test placed them beside the vault instead, so
*every* file read as deleted and the control -- "a file that is merely out of
scope is kept" -- failed. That control is the reason this is a test of the fix
rather than a test of the fixture.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from brainskit.application.codegraph import CodeGraph
from brainskit.application.services import BrainskitService
from brainskit.infrastructure.index import SqliteFtsIndex
from brainskit.infrastructure.vault import FileVault

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_engine import policy


def _node(node_id: str, label: str, path: str, line: int = 1) -> dict:
    return {
        "id": node_id,
        "label": label,
        "source_file": path,
        "source_location": f"L{line}",
        "file_type": "code",
    }


class ScopedBuildPrunesTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name).resolve()
        self.vault = FileVault.initialize(root / "vault", policy())
        self.service = BrainskitService(
            self.vault, SqliteFtsIndex(self.vault.index_path)
        )
        # Inside code_root(), which is what `code_hash` resolves against and
        # what `staleness()` re-reads. Anywhere else and every file reads as
        # deleted, which is a fixture bug wearing a fix's clothes.
        self.code_root = self.vault.code_root()
        for name in ("kept.py", "doomed.py"):
            (self.code_root / name).write_text("x = 1\n", encoding="utf-8")
        self.service.code_import(
            {
                "nodes": [
                    _node("keep", "Kept", "kept.py"),
                    _node("gone", "Doomed", "doomed.py"),
                ],
                "links": [
                    {
                        "source": "keep",
                        "target": "gone",
                        "relation": "imports_from",
                        "source_file": "kept.py",
                        "source_location": "L2",
                    }
                ],
            }
        )

    def merge_scope(self, scope: str) -> tuple[dict, list]:
        """Drive `_merge_scoped` directly.

        `code_import` replaces the stored graph wholesale and never reaches the
        merge, so a test written against it passes without exercising anything.
        `build(paths=...)` is its only caller and needs a configured extractor,
        which this fixture deliberately does not have.
        """

        fresh = {
            "keep": {
                "id": "keep",
                "label": "Kept",
                "kind": "code",
                "path": scope,
                "line": 1,
            }
        }
        return CodeGraph(self.vault)._merge_scoped(fresh, [], [Path(scope)])

    def paths(self, nodes: dict) -> set[str]:
        return {str(node.get("path", "")) for node in nodes.values()}

    def test_the_fixture_files_are_where_code_hash_looks(self) -> None:
        """Control: if this fails, every assertion below is about nothing."""

        self.assertIsNotNone(self.vault.code_hash("kept.py"))
        self.assertIsNotNone(self.vault.code_hash("doomed.py"))
        self.assertEqual(self.service.code_status()["state"], "fresh")

    def test_an_out_of_scope_file_that_still_exists_is_kept(self) -> None:
        """Control: preserving out-of-scope nodes is the merge's whole purpose."""

        nodes, _ = self.merge_scope("kept.py")
        self.assertIn("doomed.py", self.paths(nodes))

    def test_an_out_of_scope_file_that_was_deleted_is_pruned(self) -> None:
        (self.code_root / "doomed.py").unlink()
        nodes, _ = self.merge_scope("kept.py")
        self.assertNotIn("doomed.py", self.paths(nodes))

    def test_the_scoped_file_survives_the_prune(self) -> None:
        (self.code_root / "doomed.py").unlink()
        nodes, _ = self.merge_scope("kept.py")
        self.assertIn("kept.py", self.paths(nodes))

    def test_no_edge_dangles_after_a_prune(self) -> None:
        """An edge to a pruned node would break every traversal."""

        (self.code_root / "doomed.py").unlink()
        nodes, edges = self.merge_scope("kept.py")
        for edge in edges:
            self.assertIn(str(edge["source"]), nodes)
            self.assertIn(str(edge["target"]), nodes)

    def test_the_prune_agrees_with_what_staleness_calls_removed(self) -> None:
        """The two must not hold different opinions about one file.

        `staleness()` decides "gone" with the same `code_hash is None` test, so
        a graph reporting a file removed and a merge keeping its nodes would be
        the codebase disagreeing with itself.
        """

        (self.code_root / "doomed.py").unlink()
        removed = set(self.service.code_status().get("removed", []))
        nodes, _ = self.merge_scope("kept.py")
        self.assertIn("doomed.py", removed)
        self.assertEqual(removed & self.paths(nodes), set())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
