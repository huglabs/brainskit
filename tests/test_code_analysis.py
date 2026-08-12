"""Community detection, import cycles and structural diff over the code graph.

`bk code communities`/`cycles`/`diff` are the three questions brainskit's own
plain traversal (`affected`/`path`/`hubs`) has no way to answer — none of
them are about one symbol's neighbourhood, they are about the shape of the
whole graph — so they are delegated whole to the vendored
`graphify.cluster`/`graphify.analyze` rather than approximated with a second,
simpler implementation brainskit would then have to keep matching.

What this module has to prove is narrower than Graphify's own test suite:
not that Leiden/Louvain or cycle-detection are correct — that is upstream's
job — but that brainskit's boundary holds around them: the same privacy check
every other reader in `CodeGraph` uses, a clear brainskit error (never a bare
`ImportError` traceback) when the optional `networkx` dependency is absent,
and a faithful translation between brainskit's stored node/edge shape and the
attribute names the vendored analysis was written to read.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

from test_projections import policy

from brainskit.application.codegraph import CODE_PROJECTION
from brainskit.application.services import BrainskitService
from brainskit.domain.model import ValidationError
from brainskit.infrastructure.graph import MarkdownGraph
from brainskit.infrastructure.index import SqliteFtsIndex
from brainskit.infrastructure.vault import FileVault


def _networkx_installed() -> bool:
    return importlib.util.find_spec("networkx") is not None


#: `communities`/`cycles`/`diff` all need the vendored analysis, which needs
#: `networkx` — the other half of the `code` extra alongside tree-sitter.
#: Every fixture that exercises real analysis skips rather than fails when
#: it is absent, same as `test_code_graph.py`'s own `_HAS_CODE_EXTRA`.
_HAS_NETWORKX = _networkx_installed()


def node(node_id: str, label: str, path: str, line: int, kind: str = "code") -> dict[str, Any]:
    return {
        "id": node_id,
        "label": label,
        "source_file": path,
        "source_location": f"L{line}",
        "file_type": kind,
    }


def edge(source: str, target: str, relation: str, path: str) -> dict[str, Any]:
    return {
        "source": source,
        "target": target,
        "relation": relation,
        "source_file": path,
        "source_location": "L1",
    }


#: Two files, each with two functions calling each other, and nothing at all
#: connecting the two files — the plainest structure Leiden/Louvain can be
#: asked to recover. A test failure here means the boundary broke, not that
#: the partitioner disagreed about a borderline case.
def clustered_payload() -> dict[str, Any]:
    return {
        "nodes": [
            node("src_a", "a.ts", "src/a.ts", 1),
            node("src_a_one", "one()", "src/a.ts", 2),
            node("src_a_two", "two()", "src/a.ts", 3),
            node("src_b", "b.ts", "src/b.ts", 1),
            node("src_b_one", "uno()", "src/b.ts", 2),
            node("src_b_two", "dos()", "src/b.ts", 3),
        ],
        "links": [
            edge("src_a", "src_a_one", "contains", "src/a.ts"),
            edge("src_a", "src_a_two", "contains", "src/a.ts"),
            edge("src_a_one", "src_a_two", "calls", "src/a.ts"),
            edge("src_b", "src_b_one", "contains", "src/b.ts"),
            edge("src_b", "src_b_two", "contains", "src/b.ts"),
            edge("src_b_one", "src_b_two", "calls", "src/b.ts"),
        ],
    }


#: Three files importing in a ring: a -> b -> c -> a.
def cyclic_payload() -> dict[str, Any]:
    return {
        "nodes": [
            node("src_a", "a.ts", "src/a.ts", 1),
            node("src_b", "b.ts", "src/b.ts", 1),
            node("src_c", "c.ts", "src/c.ts", 1),
        ],
        "links": [
            edge("src_a", "src_b", "imports_from", "src/a.ts"),
            edge("src_b", "src_c", "imports_from", "src/b.ts"),
            edge("src_c", "src_a", "imports_from", "src/c.ts"),
        ],
    }


def acyclic_payload() -> dict[str, Any]:
    """The same three files, missing the edge that closes the ring."""
    payload = cyclic_payload()
    payload["links"] = payload["links"][:2]
    return payload


class AnalysisFixture(unittest.TestCase):
    """A repository with a vault inside it — the layout `test_code_graph`'s
    own fixtures use. No real extraction: every payload here is hand-built
    the same way `code import` accepts one, so these tests do not need the
    tree-sitter grammars, only `networkx`.
    """

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.repo = Path(self.temporary.name) / "repo"
        (self.repo / "src").mkdir(parents=True)
        (self.repo / ".git").mkdir()
        for name in ("a.ts", "b.ts", "c.ts"):
            (self.repo / "src" / name).write_text("export {}\n", encoding="utf-8")

        vault = FileVault.initialize(self.repo / "docs" / "brain", policy())
        self.vault = vault
        self.service = BrainskitService(
            vault, SqliteFtsIndex(vault.index_path), graph=MarkdownGraph()
        )
        self.service.reindex()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def graph_file(self) -> dict[str, Any]:
        return json.loads((self.vault.root / CODE_PROJECTION).read_text("utf-8"))


@unittest.skipUnless(_HAS_NETWORKX, "requires the `code` extra (networkx)")
class CommunitiesTest(AnalysisFixture):
    def test_finds_the_two_obvious_clusters(self) -> None:
        self.service.code_import(clustered_payload())
        result = self.service.code_communities()
        self.assertEqual(result["count"], 2)
        self.assertEqual(sorted(c["size"] for c in result["communities"]), [3, 3])

    def test_members_are_grouped_by_file(self) -> None:
        # The two clusters are exactly the two files; a member list mixing
        # paths would mean the partitioner (or the translation into it) is
        # not seeing the graph brainskit thinks it built.
        self.service.code_import(clustered_payload())
        result = self.service.code_communities()
        for community in result["communities"]:
            paths = {member["path"] for member in community["members"]}
            self.assertEqual(len(paths), 1)

    def test_reports_a_cohesion_score_in_range(self) -> None:
        self.service.code_import(clustered_payload())
        result = self.service.code_communities()
        for community in result["communities"]:
            self.assertIn("cohesion", community)
            self.assertGreaterEqual(community["cohesion"], 0.0)
            self.assertLessEqual(community["cohesion"], 1.0)

    def test_labels_each_community_after_its_hub(self) -> None:
        self.service.code_import(clustered_payload())
        result = self.service.code_communities()
        labels = {c["label"] for c in result["communities"]}
        self.assertEqual(labels, {"a.ts", "b.ts"})

    def test_a_higher_resolution_never_yields_fewer_communities(self) -> None:
        self.service.code_import(clustered_payload())
        coarse = self.service.code_communities(resolution=0.5)["count"]
        fine = self.service.code_communities(resolution=4.0)["count"]
        self.assertGreaterEqual(fine, coarse)

    def test_refuses_the_cloud_consumer(self) -> None:
        self.service.code_import(clustered_payload())
        with self.assertRaises(ValidationError):
            self.service.code_communities(consumer="cloud")

    def test_querying_without_a_graph_says_how_to_build_one(self) -> None:
        from brainskit.domain.model import NotFoundError

        with self.assertRaises(NotFoundError) as caught:
            self.service.code_communities()
        self.assertIn("bk code import", caught.exception.details["hint"])


@unittest.skipUnless(_HAS_NETWORKX, "requires the `code` extra (networkx)")
class CyclesTest(AnalysisFixture):
    def test_detects_a_real_cycle(self) -> None:
        self.service.code_import(cyclic_payload())
        result = self.service.code_cycles()
        self.assertEqual(result["count"], 1)
        self.assertEqual(
            set(result["cycles"][0]["cycle"]), {"src/a.ts", "src/b.ts", "src/c.ts"}
        )

    def test_reports_nothing_when_there_is_no_cycle(self) -> None:
        self.service.code_import(acyclic_payload())
        result = self.service.code_cycles()
        self.assertEqual(result["count"], 0)
        self.assertEqual(result["cycles"], [])

    def test_respects_the_max_length_bound(self) -> None:
        self.service.code_import(cyclic_payload())
        result = self.service.code_cycles(max_length=2)
        self.assertEqual(result["cycles"], [])

    def test_refuses_the_cloud_consumer(self) -> None:
        self.service.code_import(cyclic_payload())
        with self.assertRaises(ValidationError):
            self.service.code_cycles(consumer="cloud")


@unittest.skipUnless(_HAS_NETWORKX, "requires the `code` extra (networkx)")
class DiffTest(AnalysisFixture):
    def test_reports_added_nodes_and_edges(self) -> None:
        self.service.code_import(cyclic_payload())
        grown = cyclic_payload()
        grown["nodes"].append(node("src_d", "d.ts", "src/d.ts", 1))
        grown["links"].append(edge("src_a", "src_d", "imports_from", "src/a.ts"))

        result = self.service.code_diff(grown)

        self.assertEqual({n["id"] for n in result["new_nodes"]}, {"src_d"})
        self.assertEqual(len(result["new_edges"]), 1)
        self.assertEqual(result["removed_nodes"], [])
        self.assertEqual(result["removed_edges"], [])

    def test_reports_removed_nodes_and_edges(self) -> None:
        self.service.code_import(cyclic_payload())

        result = self.service.code_diff(acyclic_payload())  # drops c -> a

        self.assertEqual(result["new_nodes"], [])
        self.assertEqual(result["removed_nodes"], [])
        self.assertEqual(len(result["removed_edges"]), 1)
        self.assertEqual(result["removed_edges"][0]["source"], "src_c")

    def test_no_changes_is_reported_as_no_changes(self) -> None:
        self.service.code_import(cyclic_payload())

        result = self.service.code_diff(cyclic_payload())

        self.assertEqual(result["new_nodes"], [])
        self.assertEqual(result["removed_nodes"], [])
        self.assertEqual(result["new_edges"], [])
        self.assertEqual(result["removed_edges"], [])
        self.assertEqual(result["summary"], "no changes")

    def test_without_an_extractor_or_an_external_graph_says_so(self) -> None:
        self.service.code_import(cyclic_payload())
        with self.assertRaises(ValidationError) as caught:
            self.service.code_diff()
        self.assertIn("configure", caught.exception.details["hint"])

    def test_refuses_the_cloud_consumer(self) -> None:
        self.service.code_import(cyclic_payload())
        with self.assertRaises(ValidationError):
            self.service.code_diff(cyclic_payload(), consumer="cloud")

    def test_the_refusal_is_checked_before_the_second_graph_is_built(self) -> None:
        # Same property `test_code_graph.py`'s `BoundaryTest` asserts for the
        # existing readers: the boundary does not depend on the rest of the
        # call succeeding, so `against=None` (which would otherwise demand a
        # configured extractor) must not surface that error instead.
        self.service.code_import(cyclic_payload())
        with self.assertRaises(ValidationError) as caught:
            self.service.code_diff(consumer="cloud")
        self.assertEqual(caught.exception.details.get("consumer"), "cloud")


class MissingNetworkxTest(AnalysisFixture):
    """`networkx` gates all three commands; each must fail with a brainskit
    error naming the fix, not a bare `ImportError` — the same property
    `test_code_graph.py`'s `MissingDependencyTest` establishes for
    tree-sitter and `build()`.
    """

    def setUp(self) -> None:
        super().setUp()
        self.service.code_import(clustered_payload())

    def _blocked(self) -> Any:
        import unittest.mock as mock

        return mock.patch.dict(sys.modules, {"networkx": None})

    def _reset_analysis_cache(self) -> None:
        import brainskit.application.codegraph as codegraph_module

        codegraph_module._ANALYSIS_MODULES = None

    def test_communities_without_networkx_is_a_clear_error(self) -> None:
        self._reset_analysis_cache()
        with self._blocked():
            with self.assertRaises(ValidationError) as caught:
                self.service.code_communities()
        self._reset_analysis_cache()
        self.assertIn("brainskit[code]", caught.exception.details["needs"])
        self.assertNotIsInstance(caught.exception, ImportError)

    def test_cycles_without_networkx_is_a_clear_error(self) -> None:
        self._reset_analysis_cache()
        with self._blocked():
            with self.assertRaises(ValidationError) as caught:
                self.service.code_cycles()
        self._reset_analysis_cache()
        self.assertIn("brainskit[code]", caught.exception.details["needs"])
        self.assertNotIsInstance(caught.exception, ImportError)

    def test_diff_without_networkx_is_a_clear_error(self) -> None:
        self._reset_analysis_cache()
        with self._blocked():
            with self.assertRaises(ValidationError) as caught:
                self.service.code_diff(clustered_payload())
        self._reset_analysis_cache()
        self.assertIn("brainskit[code]", caught.exception.details["needs"])
        self.assertNotIsInstance(caught.exception, ImportError)

    def test_the_refusal_is_checked_before_the_privacy_boundary(self) -> None:
        # Either order would be defensible; what matters is that a caller
        # sees one clear reason rather than whichever guard happened to run
        # first looking arbitrary. Missing-dependency is checked first here.
        self._reset_analysis_cache()
        with self._blocked():
            with self.assertRaises(ValidationError) as caught:
                self.service.code_communities(consumer="cloud")
        self._reset_analysis_cache()
        self.assertIn("brainskit[code]", caught.exception.details["needs"])


class VendoredAnalysisImportTest(unittest.TestCase):
    """The alias `infrastructure/codeanalysis/__init__.py` installs, exercised
    for the two analysis modules this file's commands depend on — the
    counterpart to `test_code_graph.py`'s `VendoredImportTest`, which covers
    the extraction closure.
    """

    @unittest.skipUnless(_HAS_NETWORKX, "requires the `code` extra (networkx)")
    def test_cluster_and_analyze_import_without_tree_sitter(self) -> None:
        # Community detection and cycle/diff analysis touch no parser at
        # all; blocking tree-sitter has to leave them unaffected. Run in a
        # fresh interpreter for the same reason `test_code_graph.py` does:
        # once anything has imported the vendored closure in this process,
        # `sys.modules` keeps serving it regardless of what gets blocked
        # afterwards.
        script = (
            "import sys\n"
            "sys.modules['tree_sitter'] = None\n"
            "from brainskit.infrastructure import codeanalysis\n"
            "import graphify.cluster\n"
            "import graphify.analyze\n"
            "print('ok')\n"
        )
        completed = subprocess.run(
            [sys.executable, "-c", script], capture_output=True, text=True, timeout=30
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("ok", completed.stdout)

    def test_codegraph_module_imports_without_networkx(self) -> None:
        # `application/codegraph.py` is on brainskit's normal startup path
        # (`services.py` imports it unconditionally); it must not cost every
        # caller an import of networkx just because three of its methods
        # eventually need one.
        script = (
            "import sys\n"
            "sys.modules['networkx'] = None\n"
            "import brainskit.application.codegraph\n"
            "print('ok')\n"
        )
        completed = subprocess.run(
            [sys.executable, "-c", script], capture_output=True, text=True, timeout=30
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("ok", completed.stdout)
