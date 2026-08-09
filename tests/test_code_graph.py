"""The repository graph as a projection brainkit owns but does not build.

Brainkit has no parser and should not grow one. What it has, and what no code
graph tool in the companion analysis had, is a way to say *when the artefact
stopped being true* and *who may read it*. So the split is: an extractor
produces the graph, and this module owns the contract, the freshness and the
boundary.

Three properties carry it:

- **The import is a boundary, not a copy.** Prose nodes are dropped, dangling
  edges are dropped, and the vault is excluded from its own graph. Trusting the
  caller to have passed `--code-only` would make that a property of a command
  line instead of a property of the vault.
- **Freshness is content, not time.** The graph records the hash of every file
  it indexed, and `status` re-reads them. A stored git revision would miss
  uncommitted edits; mtime would break on `git checkout`.
- **The graph is local-only.** It carries repository paths, so the boundary is
  checked before the file is opened rather than filtered afterwards.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

from test_projections import policy

from brainkit.application.codegraph import CODE_PROJECTION
from brainkit.application.services import BrainkitService
from brainkit.domain.model import NotFoundError, ValidationError
from brainkit.infrastructure.extractor import GraphifyExtractor
from brainkit.infrastructure.graph import MarkdownGraph
from brainkit.infrastructure.index import SqliteFtsIndex
from brainkit.infrastructure.vault import FileVault


def _code_extra_installed() -> bool:
    try:
        import tree_sitter  # noqa: F401
        import tree_sitter_python  # noqa: F401
    except ImportError:
        return False
    return True


#: Real extraction (`bk code build`) needs the `code` extra's compiled
#: grammars, which the dev environment may or may not have installed. Every
#: fixture that calls it skips rather than fails when they are absent, so the
#: suite is green either way — the same property `pyproject.toml` documents
#: for the extra itself.
_HAS_CODE_EXTRA = _code_extra_installed()


def node(node_id: str, label: str, path: str, line: int, kind: str = "code") -> dict:
    return {
        "id": node_id,
        "label": label,
        "source_file": path,
        "source_location": f"L{line}",
        "file_type": kind,
    }


def edge(source: str, target: str, relation: str, path: str = "src/db.ts") -> dict:
    return {
        "source": source,
        "target": target,
        "relation": relation,
        "source_file": path,
        "source_location": "L2",
    }


class CodeGraphFixture(unittest.TestCase):
    """A repository with a vault inside it, which is the layout in the wild."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.repo = Path(self.temporary.name) / "repo"
        (self.repo / "src").mkdir(parents=True)
        (self.repo / ".git").mkdir()
        (self.repo / "src" / "db.ts").write_text("class Db {}\n", encoding="utf-8")
        (self.repo / "src" / "app.ts").write_text("import './db'\n", encoding="utf-8")

        vault = FileVault.initialize(self.repo / "docs" / "brain", policy())
        self.vault = vault
        self.service = BrainkitService(
            vault, SqliteFtsIndex(vault.index_path), graph=MarkdownGraph()
        )
        self.service.reindex()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def payload(self, **overrides: Any) -> dict[str, Any]:
        base: dict[str, Any] = {
            "nodes": [
                node("src_db", "db.ts", "src/db.ts", 1),
                node("src_db_class", "Db", "src/db.ts", 1),
                node("src_app", "app.ts", "src/app.ts", 1),
            ],
            "links": [
                edge("src_db", "src_db_class", "contains"),
                edge("src_app", "src_db", "imports_from", "src/app.ts"),
            ],
        }
        base.update(overrides)
        return base

    def graph_file(self) -> dict[str, Any]:
        return json.loads((self.vault.root / CODE_PROJECTION).read_text("utf-8"))


class ImportBoundaryTest(CodeGraphFixture):
    def test_imports_code_nodes_and_their_edges(self) -> None:
        result = self.service.code_import(self.payload())
        self.assertEqual(result["nodes"], 3)
        self.assertEqual(result["edges"], 2)
        self.assertEqual(result["files"], 2)

    def test_drops_prose_nodes_rather_than_trusting_the_caller(self) -> None:
        payload = self.payload()
        payload["nodes"].append(node("doc_1", "Readme", "README.md", 1, "document"))
        payload["nodes"].append(node("con_1", "Idea", "README.md", 1, "concept"))
        result = self.service.code_import(payload)
        self.assertEqual(result["dropped_non_code_nodes"], 2)
        self.assertNotIn("doc_1", [n["id"] for n in self.graph_file()["nodes"]])

    def test_drops_an_edge_whose_endpoint_was_dropped(self) -> None:
        # A dangling endpoint would make every traversal guard against nodes
        # that are not there.
        payload = self.payload()
        payload["nodes"].append(node("doc_1", "Readme", "README.md", 1, "document"))
        payload["links"].append(edge("src_db", "doc_1", "references"))
        self.service.code_import(payload)
        self.assertEqual(len(self.graph_file()["edges"]), 2)

    def test_excludes_the_vault_from_its_own_graph(self) -> None:
        # Measured against a real extraction: an extractor pointed at the repo
        # indexed `.brain/schema.json` and it came out as the top hub.
        payload = self.payload()
        payload["nodes"].append(
            node("vault_schema", "schema.json", "docs/brain/.brain/schema.json", 1)
        )
        result = self.service.code_import(payload)
        paths = {n["path"] for n in self.graph_file()["nodes"]}
        self.assertNotIn("docs/brain/.brain/schema.json", paths)
        self.assertEqual(result["dropped_non_code_nodes"], 1)

    def test_refuses_a_graph_with_no_code_in_it(self) -> None:
        with self.assertRaises(ValidationError) as caught:
            self.service.code_import(
                {"nodes": [node("d", "R", "README.md", 1, "document")], "links": []}
            )
        self.assertIn("code-only", caught.exception.details["hint"])

    def test_deduplicates_repeated_edges(self) -> None:
        payload = self.payload()
        payload["links"].append(edge("src_db", "src_db_class", "contains"))
        self.service.code_import(payload)
        self.assertEqual(len(self.graph_file()["edges"]), 2)

    def test_records_the_privacy_it_was_written_under(self) -> None:
        self.service.code_import(self.payload())
        self.assertEqual(self.graph_file()["privacy"], "local-only")

    def test_reports_a_file_it_could_not_read(self) -> None:
        payload = self.payload()
        payload["nodes"].append(node("gone", "gone.ts", "src/gone.ts", 1))
        result = self.service.code_import(payload)
        self.assertEqual(result["unreadable_files"], ["src/gone.ts"])

    def test_parses_the_line_number_and_tolerates_a_missing_one(self) -> None:
        payload = self.payload()
        payload["nodes"][0]["source_location"] = "not-a-line"
        self.service.code_import(payload)
        lines = {n["id"]: n["line"] for n in self.graph_file()["nodes"]}
        self.assertIsNone(lines["src_db"])
        self.assertEqual(lines["src_db_class"], 1)

    def test_normalizes_ids_to_the_extractor_s_own_recipe(self) -> None:
        # A payload need not spell an id exactly as the extractor's own
        # `_make_id` would; the edge below names its endpoint differently
        # than the node that defines it (extra punctuation, different
        # casing), and both have to collapse to the same canonical id or the
        # edge reads as dangling and gets dropped.
        payload = {
            "nodes": [
                node("Src--DB", "db.ts", "src/db.ts", 1),
                node("SRC_DB__CLASS", "Db", "src/db.ts", 1),
            ],
            "links": [edge("Src.DB!!", "SRC_DB__CLASS", "contains")],
        }
        result = self.service.code_import(payload)
        self.assertEqual(result["nodes"], 2)
        self.assertEqual(result["edges"], 1)
        ids = {n["id"] for n in self.graph_file()["nodes"]}
        self.assertEqual(ids, {"src_db", "src_db_class"})


class FreshnessTest(CodeGraphFixture):
    def test_a_vault_with_no_graph_is_missing_not_stale(self) -> None:
        report = self.service.code_status()
        self.assertEqual(report["state"], "missing")
        self.assertFalse(report["stale"])

    def test_a_freshly_imported_graph_is_fresh(self) -> None:
        self.service.code_import(self.payload())
        report = self.service.code_status()
        self.assertEqual(report["state"], "fresh")
        self.assertFalse(report["stale"])
        self.assertEqual(report["files"], 2)

    def test_editing_an_indexed_file_makes_it_stale(self) -> None:
        self.service.code_import(self.payload())
        (self.repo / "src" / "db.ts").write_text("class Db { x() {} }\n", encoding="utf-8")
        report = self.service.code_status()
        self.assertEqual(report["state"], "stale")
        self.assertTrue(report["stale"])
        self.assertEqual(report["changed"], ["src/db.ts"])
        self.assertIn("bk code import", report["command"])

    def test_deleting_an_indexed_file_is_reported_separately(self) -> None:
        self.service.code_import(self.payload())
        (self.repo / "src" / "db.ts").unlink()
        report = self.service.code_status()
        self.assertEqual(report["removed"], ["src/db.ts"])
        self.assertEqual(report["changed"], [])

    def test_touching_an_unindexed_file_changes_nothing(self) -> None:
        # The fingerprint covers what the artefact reads, and only that.
        self.service.code_import(self.payload())
        (self.repo / "src" / "unrelated.ts").write_text("x\n", encoding="utf-8")
        self.assertEqual(self.service.code_status()["state"], "fresh")

    def test_status_reports_it_beside_the_other_projections(self) -> None:
        self.service.code_import(self.payload())
        report = self.service.status()["projections"][CODE_PROJECTION]
        self.assertEqual(report["state"], "fresh")
        # The block's shared contract, so a caller walking it need not branch.
        self.assertIn("stale", report)
        self.assertIn("generated_at", report)


class TraversalTest(CodeGraphFixture):
    def setUp(self) -> None:
        super().setUp()
        self.service.code_import(
            self.payload(
                nodes=[
                    node("src_db", "db.ts", "src/db.ts", 1),
                    node("src_db_connect", "connect()", "src/db.ts", 2),
                    node("src_db_getdb", "getDb()", "src/db.ts", 5),
                    node("src_app", "app.ts", "src/app.ts", 1),
                    node("src_app_main", "main()", "src/app.ts", 2),
                ],
                links=[
                    edge("src_db", "src_db_connect", "contains"),
                    edge("src_db", "src_db_getdb", "contains"),
                    edge("src_db_connect", "src_db_getdb", "calls"),
                    edge("src_app", "src_db", "imports_from", "src/app.ts"),
                    edge("src_app", "src_app_main", "contains", "src/app.ts"),
                ],
            )
        )

    def test_affected_follows_edges_backwards(self) -> None:
        # The question a rename asks: forward edges say what this uses, only the
        # reverse says what breaks.
        result = self.service.code_affected("getDb()", depth=1)
        self.assertEqual(
            {item["id"] for item in result["affected"]},
            {"src_db", "src_db_connect"},
        )

    def test_affected_respects_the_depth_bound(self) -> None:
        near = self.service.code_affected("getDb()", depth=1)["count"]
        far = self.service.code_affected("getDb()", depth=3)["count"]
        self.assertGreater(far, near)

    def test_affected_records_which_relation_it_arrived_by(self) -> None:
        result = self.service.code_affected("getDb()", depth=1)
        relations = {item["id"]: item["via"] for item in result["affected"]}
        self.assertEqual(relations["src_db_connect"], "calls")

    def test_a_symbol_reached_two_ways_is_reported_once(self) -> None:
        result = self.service.code_affected("getDb()", depth=3)
        ids = [item["id"] for item in result["affected"]]
        self.assertEqual(len(ids), len(set(ids)))

    def test_path_is_undirected(self) -> None:
        # "How are these two related" should not depend on argument order.
        forward = self.service.code_path("main()", "getDb()")
        backward = self.service.code_path("getDb()", "main()")
        self.assertTrue(forward["found"])
        self.assertTrue(backward["found"])
        self.assertEqual(forward["hops"], backward["hops"])

    def test_path_reports_no_route_rather_than_failing(self) -> None:
        self.service.code_import(
            self.payload(
                nodes=[
                    node("a", "alpha", "src/db.ts", 1),
                    node("b", "beta", "src/app.ts", 1),
                ],
                links=[],
            )
        )
        self.assertFalse(self.service.code_path("alpha", "beta")["found"])

    def test_hubs_rank_by_connection_count(self) -> None:
        hubs = self.service.code_hubs(top=2)["hubs"]
        self.assertEqual(hubs[0]["id"], "src_db")
        self.assertGreaterEqual(hubs[0]["edges"], hubs[1]["edges"])

    def test_resolves_a_symbol_by_id_or_by_label(self) -> None:
        by_id = self.service.code_affected("src_db_getdb", depth=1)
        by_label = self.service.code_affected("getDb()", depth=1)
        self.assertEqual(by_id["id"], by_label["id"])

    def test_resolves_a_label_case_insensitively(self) -> None:
        self.assertEqual(self.service.code_affected("GETDB()", depth=1)["id"], "src_db_getdb")

    def test_an_unknown_symbol_is_not_found(self) -> None:
        with self.assertRaises(NotFoundError):
            self.service.code_affected("nothingHere", depth=1)

    def test_an_ambiguous_label_asks_for_an_id_rather_than_guessing(self) -> None:
        self.service.code_import(
            self.payload(
                nodes=[
                    node("one", "connect()", "src/db.ts", 1),
                    node("two", "connect()", "src/app.ts", 1),
                ],
                links=[],
            )
        )
        with self.assertRaises(ValidationError) as caught:
            self.service.code_affected("connect()", depth=1)
        self.assertEqual(len(caught.exception.details["candidates"]), 2)


class BoundaryTest(CodeGraphFixture):
    def test_the_cloud_consumer_is_refused(self) -> None:
        self.service.code_import(self.payload())
        for call in (
            lambda: self.service.code_hubs(consumer="cloud"),
            lambda: self.service.code_affected("db.ts", consumer="cloud"),
            lambda: self.service.code_path("db.ts", "Db", consumer="cloud"),
        ):
            with self.assertRaises(ValidationError):
                call()

    def test_the_refusal_does_not_depend_on_the_graph_existing(self) -> None:
        # Checked before the file is opened, so an empty vault cannot leak the
        # boundary by answering "not found" to a consumer that may not ask.
        with self.assertRaises(ValidationError):
            self.service.code_hubs(consumer="cloud")

    def test_querying_without_a_graph_says_how_to_build_one(self) -> None:
        with self.assertRaises(NotFoundError) as caught:
            self.service.code_hubs()
        self.assertIn("bk code import", caught.exception.details["hint"])


class VendoredImportTest(unittest.TestCase):
    """The alias `infrastructure/codeanalysis/__init__.py` installs, and the
    one function `application/codegraph.py` imports through it.
    """

    def test_the_full_vendored_closure_imports_and_registers_by_name(self) -> None:
        # `graphify.extract` alone transitively imports every top-level module
        # except `detect`/`google_workspace` (those two are lazy, reached only
        # from inside `extract()`/`collect_files()`) and all 28 extractor
        # submodules, so the two explicit imports below are what it takes to
        # touch the entire vendored closure. Asserted by name, not just "no
        # exception", so a module silently missing from `sys.modules` (e.g. an
        # alias bug that shadowed one) would still fail this.
        import importlib

        from brainkit.infrastructure import codeanalysis  # noqa: F401

        importlib.import_module("graphify.extract")
        importlib.import_module("graphify.detect")

        top_level = (
            "cache", "extract", "ids", "manifest_ingest", "mcp_ingest",
            "pascal_resolution", "paths", "resolver_registry",
            "ruby_resolution", "security", "symbol_resolution", "detect",
            "google_workspace",
        )
        extractors = (
            "__init__", "apex", "base", "bash", "blade", "csharp", "dart",
            "dm", "elixir", "engine", "fortran", "go", "json_config",
            "julia", "markdown", "models", "objc", "pascal", "pascal_forms",
            "powershell", "razor", "resolution", "rust", "sln", "sql",
            "terraform", "verilog", "zig",
        )
        for name in top_level:
            with self.subTest(module=name):
                self.assertIn(f"graphify.{name}", sys.modules)
        for name in extractors:
            module_name = "graphify.extractors" if name == "__init__" else f"graphify.extractors.{name}"
            with self.subTest(module=module_name):
                self.assertIn(module_name, sys.modules)

    def test_the_closure_imports_without_tree_sitter_in_a_fresh_process(self) -> None:
        # Has to be a fresh interpreter: once anything in this suite has
        # imported `graphify.extract`, `sys.modules` keeps serving the cached
        # module to every later test regardless of what gets blocked
        # afterwards, so this process cannot honestly re-test "importable
        # without tree-sitter" once any other test here has run first.
        script = (
            "import sys\n"
            "sys.modules['tree_sitter'] = None\n"
            "from brainkit.infrastructure import codeanalysis\n"
            "import graphify.extract\n"
            "import graphify.detect\n"
            "print('ok')\n"
        )
        completed = subprocess.run(
            [sys.executable, "-c", script], capture_output=True, text=True, timeout=30
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("ok", completed.stdout)

    def test_normalize_id_is_idempotent(self) -> None:
        from brainkit.infrastructure.codeanalysis import normalize_id

        value = normalize_id("Hello, World!!  -- foo_bar")
        self.assertEqual(normalize_id(value), value)

    def test_normalize_id_collapses_non_word_runs(self) -> None:
        from brainkit.infrastructure.codeanalysis import normalize_id

        self.assertEqual(normalize_id("foo--bar..baz"), "foo_bar_baz")

    def test_normalize_id_casefolds(self) -> None:
        from brainkit.infrastructure.codeanalysis import normalize_id

        self.assertEqual(normalize_id("FooBar"), "foobar")


class _UnavailableExtractor:
    """A `CodeExtractorPort` that reports no working installation.

    Stands in for `GraphifyExtractor` on a machine without the `code` extra,
    without actually needing one absent — the assertion is about
    `CodeGraph.build`'s response to `available() is False`, not about this
    environment's installed packages.
    """

    def available(self) -> bool:
        return False

    def extract(self, root: Path, paths: list[Path] | None = None) -> dict[str, Any]:
        raise AssertionError("build() must not call extract() when unavailable")


class MissingDependencyTest(CodeGraphFixture):
    def test_build_without_an_extractor_configured_says_so(self) -> None:
        # `self.service` (CodeGraphFixture) was built without one, the same as
        # any caller that only ever imports externally-produced graphs.
        with self.assertRaises(ValidationError) as caught:
            self.service.code_build()
        self.assertIn("bk code import", caught.exception.details["hint"])

    def test_build_refuses_clearly_when_the_extractor_is_unavailable(self) -> None:
        service = BrainkitService(
            self.vault,
            SqliteFtsIndex(self.vault.index_path),
            graph=MarkdownGraph(),
            extractor=_UnavailableExtractor(),
        )
        with self.assertRaises(ValidationError) as caught:
            service.code_build()
        self.assertIn("brainkit[code]", caught.exception.details["needs"])

    def test_extract_translates_a_missing_grammar_into_a_brainkit_error(self) -> None:
        # `graphify.extract` re-checks tree-sitter on every call (not just at
        # import time), so blocking it here reaches the same failure a
        # genuinely bare environment would hit, regardless of import order
        # against the rest of this suite.
        missing = object()
        previous = sys.modules.get("tree_sitter", missing)
        sys.modules["tree_sitter"] = None  # type: ignore[assignment]
        try:
            with self.assertRaises(ValidationError) as caught:
                GraphifyExtractor().extract(self.vault.code_root())
        finally:
            if previous is missing:
                del sys.modules["tree_sitter"]
            else:
                sys.modules["tree_sitter"] = previous
        # The extractor is infrastructure, so it may resolve the install
        # command itself and ships a ready `hint`. The application layer
        # cannot -- it names `needs` and the CLI resolves it (`_install_hint_for`).
        self.assertIn("brainkit[code]", caught.exception.details["hint"])
        self.assertNotIsInstance(caught.exception, ImportError)


@unittest.skipUnless(_HAS_CODE_EXTRA, "requires the `code` extra (tree-sitter + grammars)")
class CodeBuildFixture(unittest.TestCase):
    """A real repository, extracted in-process rather than via a hand-built
    payload — `build()`'s counterpart to `CodeGraphFixture`.
    """

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.repo = Path(self.temporary.name) / "repo"
        (self.repo / "src").mkdir(parents=True)
        (self.repo / ".git").mkdir()
        (self.repo / "src" / "db.py").write_text(
            "class Db:\n"
            "    def connect(self):\n"
            "        return self.get_db()\n"
            "\n"
            "    def get_db(self):\n"
            "        return None\n",
            encoding="utf-8",
        )
        (self.repo / "src" / "app.py").write_text(
            "from src.db import Db\n"
            "\n"
            "def main():\n"
            "    Db().connect()\n",
            encoding="utf-8",
        )
        # Prose the scan will also reach: real coverage that the boundary
        # rule dropping non-code nodes holds on the build path too, not just
        # for a hand-built payload (`extract_markdown` tags these `document`).
        (self.repo / "README.md").write_text(
            "# Notes\n\nProse, not code.\n", encoding="utf-8"
        )

        vault = FileVault.initialize(self.repo / "docs" / "brain", policy())
        self.vault = vault
        self.service = BrainkitService(
            vault,
            SqliteFtsIndex(vault.index_path),
            graph=MarkdownGraph(),
            extractor=GraphifyExtractor(),
        )
        self.service.reindex()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def graph_file(self) -> dict[str, Any]:
        return json.loads((self.vault.root / CODE_PROJECTION).read_text("utf-8"))


class BuildTest(CodeBuildFixture):
    def test_builds_nodes_and_edges_from_real_source(self) -> None:
        result = self.service.code_build()
        self.assertGreater(result["nodes"], 0)
        self.assertGreater(result["edges"], 0)
        paths = {node["path"] for node in self.graph_file()["nodes"]}
        self.assertIn("src/db.py", paths)
        self.assertIn("src/app.py", paths)

    def test_can_be_scoped_to_a_subset_of_paths(self) -> None:
        result = self.service.code_build(["src/db.py"])
        paths = {node["path"] for node in self.graph_file()["nodes"]}
        self.assertEqual(paths, {"src/db.py"})
        self.assertGreater(result["nodes"], 0)

    def test_a_scoped_build_after_a_full_one_merges_rather_than_replaces(self) -> None:
        full = self.service.code_build()

        scoped = self.service.code_build(["src/db.py"])

        paths = {node["path"] for node in self.graph_file()["nodes"]}
        self.assertEqual(paths, {"src/db.py", "src/app.py"})
        self.assertEqual(scoped["files"], full["files"])
        self.assertEqual(scoped["nodes"], full["nodes"])
        self.assertEqual(scoped["edges"], full["edges"])
        status = self.service.code_status()
        self.assertEqual(status["state"], "fresh")
        self.assertEqual(status["files"], full["files"])

    def test_a_scoped_rebuild_drops_the_in_scope_file_s_stale_nodes(self) -> None:
        self.service.code_build()
        (self.repo / "src" / "db.py").write_text("class Db:\n    pass\n", encoding="utf-8")

        self.service.code_build(["src/db.py"])

        graph = self.graph_file()
        paths = {node["path"] for node in graph["nodes"]}
        self.assertEqual(paths, {"src/db.py", "src/app.py"})  # out-of-scope file kept
        # `Db` survived the edit; its two now-deleted methods did not, and
        # nothing stale stands in for them.
        db_labels = {
            node["label"] for node in graph["nodes"] if node["path"] == "src/db.py"
        }
        self.assertIn("Db", db_labels)
        self.assertNotIn(".connect()", db_labels)
        self.assertNotIn(".get_db()", db_labels)

    def test_drops_prose_nodes_on_the_build_path_too(self) -> None:
        self.service.code_build()
        paths = {node["path"] for node in self.graph_file()["nodes"]}
        self.assertNotIn("README.md", paths)

    def test_excludes_the_vault_from_its_own_graph_on_the_build_path(self) -> None:
        # Real extraction, not a simulated payload: an extractor pointed at
        # the repo indexes the vault's own `.brain/schema.json` (tree-sitter
        # parses JSON too) unless brainkit excludes it.
        self.service.code_build()
        paths = {node["path"] for node in self.graph_file()["nodes"]}
        self.assertFalse(any(path.startswith("docs/brain/") for path in paths))

    def test_build_and_import_agree_on_the_same_input(self) -> None:
        payload = GraphifyExtractor().extract(self.vault.code_root())

        imported_summary = self.service.code_import(payload)
        imported_graph = self.graph_file()

        built_summary = self.service.code_build()
        built_graph = self.graph_file()

        self.assertEqual(imported_summary["nodes"], built_summary["nodes"])
        self.assertEqual(imported_summary["edges"], built_summary["edges"])
        self.assertEqual(
            {node["id"] for node in imported_graph["nodes"]},
            {node["id"] for node in built_graph["nodes"]},
        )
        self.assertEqual(
            {(e["source"], e["target"], e["type"]) for e in imported_graph["edges"]},
            {(e["source"], e["target"], e["type"]) for e in built_graph["edges"]},
        )
