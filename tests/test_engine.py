from __future__ import annotations

import gzip
import json
import re
import shutil
import socket
import tempfile
import threading
import unittest
import webbrowser
from contextlib import redirect_stderr
from datetime import UTC, datetime, timedelta
from io import StringIO
from pathlib import Path
from unittest import mock
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from brainskit.application.jobs import MAX_HISTORY_CHARS, MAX_HISTORY_EXCHANGES
from brainskit.application.ports import ApplyPlan
from brainskit.application.schema import validate_schema
from brainskit.application.services import BrainskitService
from brainskit.domain.model import (
    ConflictError,
    PolicyError,
    ValidationError,
    VaultConfig,
    resolve_source_path,
)
from brainskit.infrastructure.graph import MarkdownGraph
from brainskit.infrastructure.index import SqliteFtsIndex
from brainskit.infrastructure.integrations import NativeIntegrations
from brainskit.infrastructure.llm import JobSpecs, PolicyJudgmentRouter
from brainskit.infrastructure.vault import FileVault
from brainskit.interfaces.mcp import (
    MCP_PROTOCOL_VERSION,
    BrainskitMcpHttpHandler,
    BrainskitMcpHttpServer,
    _handle,
)
from brainskit.interfaces.web import WEB_VIEWER_HTML, build_server, run_web


def policy() -> dict:
    return {
        "version": 3,
        "wiki_language": "Portuguese (Brazil)",
        "inbox_policy": {
            "privacy": "local-only",
            "filing": "approve-each",
        },
        "branches": {
            "10-work": {
                "privacy": "never-ingest",
                "filing": "approve-each",
            },
            "20-research": {
                "privacy": "local-only",
                "filing": "auto+digest-review",
            },
        },
        "providers": {
            "ollama": {"base_url": "http://127.0.0.1:11434"},
        },
        "job_models": {
            "ingest": {"provider": "ollama", "model": "test"},
            "query": {"provider": "ollama", "model": "test"},
            "digest": {"provider": "ollama", "model": "test"},
            "lint-semantic": {"provider": "ollama", "model": "test"},
            "file-proposal": {"provider": "ollama", "model": "test"},
            "resurface": {"provider": "ollama", "model": "test"},
        },
        "sources": [],
        "schedule": {"digest": "0 8 * * *"},
        "taxonomy_seed": ["work", "research"],
        "novelty": {
            "duplicate_similarity_threshold": 0.9,
            "min_new_token_ratio": 0.15,
            "stale_after_days": 30,
        },
        "integrations": {
            "obsidian": {"enabled": False, "managed": False, "options": {}},
            "neo4j": {"enabled": False, "managed": False, "options": {}},
            "postgres": {"enabled": False, "managed": False, "options": {}},
            "web": {"enabled": False, "managed": True, "options": {}},
        },
    }


class FakeJudgment:
    def __init__(self, responses: dict[str, list[dict | str]]):
        self.responses = responses
        self.calls: list[str] = []
        self.variables: list[dict] = []

    def run(self, *, job, branches, variables, output_schema=None) -> str:
        self.calls.append(job)
        self.variables.append(variables)
        values = self.responses[job]
        value = values.pop(0) if len(values) > 1 else values[0]
        return value if isinstance(value, str) else json.dumps(value)


class EngineTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.vault = FileVault.initialize(self.root, policy())
        self.service = BrainskitService(
            self.vault,
            SqliteFtsIndex(self.vault.index_path),
            graph=MarkdownGraph(),
            integrations=NativeIntegrations(self.vault),
        )
        self.service.reindex()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_mechanical_loop_from_capture_to_graph(self) -> None:
        captured = self.service.capture(
            None,
            text="Memória compilada reduz trabalho no momento da consulta.",
            title="Memória compilada",
        )
        duplicate = self.service.capture(
            None,
            text="Memória compilada reduz trabalho no momento da consulta.",
            title="Outro título",
        )
        self.assertTrue(captured["created"])
        self.assertFalse(duplicate["created"])
        self.assertIn("memoria-compilada", captured["source"]["path"])

        content_hash = captured["source"]["content_hash"]
        proposal = {
            "operations": [
                {
                    "action": "upsert",
                    "kind": "concept",
                    "slug": "memoria-compilada",
                    "title": "Memória compilada",
                    "aliases": ["compiled memory"],
                    "source_hashes": [content_hash],
                    "body": (
                        "Conhecimento organizado antes da consulta."
                        f"[^source:{content_hash}]"
                    ),
                    "links": [],
                }
            ]
        }
        applied = self.service.apply(proposal)
        self.assertEqual(applied["applied"], 1)
        self.assertEqual(self.service.status()["pending"], 0)

        results = self.service.search("memória compilada")
        self.assertGreaterEqual(results["count"], 2)
        context = self.service.context("memória compilada")
        self.assertTrue(context["evidence"])
        self.assertTrue(self.service.lint()["ok"])

        graph = self.service.graph(html=True)
        self.assertEqual(graph["edges"], 1)
        self.assertTrue((self.root / "graph" / "graph.html").is_file())

        filed = self.service.file(content_hash[:12], "20-research")
        self.assertTrue(filed["source"]["path"].startswith("raw/20-research/"))
        self.assertTrue(self.service.lint()["ok"])

    def test_apply_rejects_whole_batch_before_write(self) -> None:
        captured = self.service.capture(None, text="Evidence", title="Evidence")
        content_hash = captured["source"]["content_hash"]
        proposal = {
            "operations": [
                {
                    "action": "upsert",
                    "kind": "concept",
                    "slug": "valid-page",
                    "title": "Valid page",
                    "aliases": [],
                    "source_hashes": [content_hash],
                    "body": f"Supported.[^source:{content_hash}]",
                    "links": [],
                },
                {
                    "action": "upsert",
                    "kind": "concept",
                    "slug": "invalid-page",
                    "title": "Invalid page",
                    "aliases": [],
                    "source_hashes": ["f" * 64],
                    "body": f"Unsupported.[^source:{'f' * 64}]",
                    "links": [],
                },
            ]
        }
        with self.assertRaises(ValidationError):
            self.service.apply(proposal)
        self.assertFalse((self.root / "wiki/concepts/valid-page.md").exists())
        self.assertFalse((self.root / "wiki/concepts/invalid-page.md").exists())

    def test_reconcile_heals_manual_move_by_hash(self) -> None:
        captured = self.service.capture(None, text="Move me", title="Move me")
        old_path = self.root / captured["source"]["path"]
        new_path = self.root / "raw/20-research/manual-name.md"
        old_path.rename(new_path)
        result = self.service.reconcile()
        self.assertEqual(result["moved"], 1)
        record = self.vault.registry()[captured["source"]["content_hash"]]
        self.assertEqual(record.path, "raw/20-research/manual-name.md")

    def test_forget_drops_missing_registry_entry(self) -> None:
        captured = self.service.capture(None, text="Gone soon", title="Gone soon")
        content_hash = captured["source"]["content_hash"]
        (self.root / captured["source"]["path"]).unlink()
        self.assertFalse(self.service.lint()["ok"])

        result = self.service.forget(content_hash[:12])

        self.assertEqual(result["forgotten"]["content_hash"], content_hash)
        self.assertNotIn(content_hash, self.vault.registry())
        findings = {finding["code"] for finding in self.service.lint()["findings"]}
        self.assertNotIn("registry.missing_file", findings)

    def test_forget_refuses_present_file_without_force(self) -> None:
        captured = self.service.capture(None, text="Still here", title="Still here")
        content_hash = captured["source"]["content_hash"]

        with self.assertRaises(ValidationError):
            self.service.forget(content_hash)
        self.assertIn(content_hash, self.vault.registry())

        self.service.forget(content_hash, force=True)
        self.assertNotIn(content_hash, self.vault.registry())

    def test_forget_reports_pages_still_citing_it(self) -> None:
        captured = self.service.capture(None, text="Cited", title="Cited")
        content_hash = captured["source"]["content_hash"]
        proposal = {
            "operations": [
                {
                    "action": "upsert",
                    "kind": "concept",
                    "slug": "cited-page",
                    "title": "Cited page",
                    "aliases": [],
                    "source_hashes": [content_hash],
                    "body": f"Supported.[^source:{content_hash}]",
                    "links": [],
                }
            ]
        }
        self.service.apply(proposal)
        (self.root / captured["source"]["path"]).unlink()

        result = self.service.forget(content_hash)

        self.assertIn("wiki/concepts/cited-page.md", result["still_cited_by"])
        findings = {finding["code"] for finding in self.service.lint()["findings"]}
        self.assertIn("wiki.unknown_source", findings)

    def test_lint_detects_raw_content_mutation(self) -> None:
        captured = self.service.capture(None, text="Immutable", title="Immutable")
        raw_path = self.root / captured["source"]["path"]
        raw_path.write_text("Mutated", encoding="utf-8")
        result = self.service.lint()
        self.assertFalse(result["ok"])
        self.assertIn(
            "raw.content_modified",
            {finding["code"] for finding in result["findings"]},
        )

    def test_apply_honors_human_owned_schema(self) -> None:
        schema_path = self.root / ".brain/schema.json"
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        schema["properties"]["confidence"] = {
            "type": "string",
            "enum": ["low", "medium", "high"],
        }
        schema_path.write_text(json.dumps(schema), encoding="utf-8")
        captured = self.service.capture(None, text="Evidence", title="Evidence")
        content_hash = captured["source"]["content_hash"]
        operation = {
            "action": "upsert",
            "kind": "concept",
            "slug": "schema-gate",
            "title": "Schema gate",
            "aliases": [],
            "source_hashes": [content_hash],
            "body": f"Supported.[^source:{content_hash}]",
            "links": [],
            "metadata": {"confidence": "impossible"},
        }
        with self.assertRaises(ValidationError):
            self.service.apply({"operations": [operation]})
        operation["metadata"]["confidence"] = "high"
        result = self.service.apply({"operations": [operation]})
        self.assertEqual(result["applied"], 1)

    def test_html_capture_is_converted_and_indexed_incrementally(self) -> None:
        html_path = self.root / "article.html"
        html_path.write_text(
            "<html><style>hidden style</style><article>"
            "<h1>Graph memory</h1><p>Durable evidence.</p>"
            "<script>hidden script</script></article></html>",
            encoding="utf-8",
        )
        try:
            captured = self.service.capture(str(html_path))
            results = self.service.search("durable evidence")
        finally:
            html_path.unlink(missing_ok=True)
        self.assertTrue(captured["created"])
        self.assertEqual(results["count"], 1)
        self.assertNotIn("hidden script", results["hits"][0]["excerpt"])

    def test_mcp_exposes_same_status_use_case(self) -> None:
        response = _handle(
            self.service,
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "status", "arguments": {}},
            },
        )
        self.assertEqual(response["id"], 1)
        self.assertEqual(response["result"]["structuredContent"]["sources"], 0)

    def test_mcp_exposes_persistent_integration_status(self) -> None:
        response = _handle(
            self.service,
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {
                    "name": "integration_status",
                    "arguments": {"name": "web"},
                },
            },
        )
        result = response["result"]["structuredContent"]
        self.assertEqual(result["integrations"][0]["name"], "web")
        self.assertEqual(result["integrations"][0]["state"], "disabled")

    def test_integration_status_hides_machine_layout_from_machine_consumers(
        self,
    ) -> None:
        # One canary per disclosure class the sanctioned fix strips: a
        # filesystem path, a directory name, a DSN, env-var names, and the
        # runtime file listing / paths / container name a sync leaves behind.
        self.service.integration_configure(
            "obsidian",
            options={
                "path": str(self.root / "mirror-canary"),
                "subdirectory": "layout-canary-subdir",
            },
        )
        self.service.integration_configure(
            "neo4j",
            enabled=True,
            managed=False,
            options={
                "uri": "bolt://dsn-canary-host:7687",
                "user": "neo4j",
                "password_env": "BRAINSKIT_CANARY_NEO4J_PASSWORD",
                "database": "graphdb",
                "consumer": "local",
            },
        )
        self.service.integration_configure(
            "postgres",
            enabled=True,
            managed=False,
            options={
                "dsn_env": "BRAINSKIT_CANARY_PG_DSN",
                "consumer": "local",
            },
        )

        def seed(state: dict) -> dict:
            rows = state.setdefault("integrations", {})
            rows["obsidian"] = {
                "last_sync_at": "2026-08-14T00:00:00+00:00",
                "path": "/tmp/canary-managed-root",
                "managed_files": ["wiki/canary-page.md"],
                "managed_file_count": 1,
            }
            rows["postgres"] = {"container": "canary-container"}
            rows["web"] = {"log": "/tmp/canary-web.log", "pid": 4242}
            return state

        self.vault.mutate_state("integration-state", seed)

        canaries = (
            "mirror-canary",
            "layout-canary-subdir",
            "bolt://",
            "BRAINSKIT_CANARY",
            "canary-managed-root",
            "canary-page.md",
            "canary-container",
            "canary-web.log",
        )
        human = self.service.integration_status()
        serialized_human = json.dumps(human)
        self.assertNotIn("consumer", human)
        for canary in canaries:
            self.assertIn(canary, serialized_human)
        for consumer in ("local", "cloud"):
            with self.subTest(consumer=consumer):
                machine = self.service.integration_status(consumer=consumer)
                serialized = json.dumps(machine)
                self.assertEqual(consumer, machine["consumer"])
                for canary in canaries:
                    self.assertNotIn(canary, serialized)
                by_name = {row["name"]: row for row in machine["integrations"]}
                # The question the row exists to answer still is answered.
                self.assertIn("state", by_name["neo4j"])
                self.assertTrue(by_name["neo4j"]["enabled"])
                self.assertEqual("local", by_name["neo4j"]["options"]["consumer"])
                self.assertEqual("graphdb", by_name["neo4j"]["options"]["database"])
                self.assertEqual(4242, by_name["web"]["runtime"]["pid"])
                self.assertEqual(
                    1, by_name["obsidian"]["runtime"]["managed_file_count"]
                )

    def test_apply_is_idempotent_and_rejects_stale_updates(self) -> None:
        captured = self.service.capture(None, text="Evidence", title="Evidence")
        content_hash = captured["source"]["content_hash"]
        proposal = {
            "proposal_id": "stable-proposal",
            "operations": [
                {
                    "action": "upsert",
                    "kind": "concept",
                    "slug": "stable-page",
                    "title": "Stable page",
                    "aliases": [],
                    "source_hashes": [content_hash],
                    "body": f"Version one.[^source:{content_hash}]",
                    "links": [],
                }
            ],
        }
        first = self.service.apply(proposal)
        second = self.service.apply(proposal)
        self.assertFalse(first["idempotent"])
        self.assertTrue(second["idempotent"])
        collision = json.loads(json.dumps(proposal))
        collision["operations"][0]["body"] = (
            f"Different payload.[^source:{content_hash}]"
        )
        with self.assertRaises(ValidationError):
            self.service.apply(collision)
        page_path = "wiki/concepts/stable-page.md"
        current_hash = self.vault.wiki_version(page_path)
        update = {
            "proposal_id": "stale-update",
            "operations": [
                {
                    **proposal["operations"][0],
                    "body": f"Version two.[^source:{content_hash}]",
                    "base_hash": "f" * 64,
                }
            ],
        }
        with self.assertRaises(ValidationError):
            self.service.apply(update)
        update["proposal_id"] = "fresh-update"
        update["operations"][0]["base_hash"] = current_hash
        applied = self.service.apply(update)
        self.assertEqual(applied["applied"], 1)

    def test_incomplete_apply_journal_rolls_back_on_open(self) -> None:
        captured = self.service.capture(None, text="Evidence", title="Evidence")
        content_hash = captured["source"]["content_hash"]
        self.service.apply(
            {
                "proposal_id": "before-crash",
                "operations": [
                    {
                        "action": "upsert",
                        "kind": "concept",
                        "slug": "recoverable",
                        "title": "Recoverable",
                        "aliases": [],
                        "source_hashes": [content_hash],
                        "body": f"Original.[^source:{content_hash}]",
                        "links": [],
                    }
                ],
            }
        )
        relative = "wiki/concepts/recoverable.md"
        target = self.root / relative
        original = target.read_text(encoding="utf-8")
        transaction_id = "a" * 32
        backup_root = self.root / ".brain/transactions" / transaction_id / "backups"
        backup_root.mkdir(parents=True)
        page_backup = backup_root / "0.md"
        registry_backup = backup_root / "registry.json"
        applied_backup = backup_root / "applied.json"
        shutil.copy2(target, page_backup)
        shutil.copy2(self.root / ".brain/registry.json", registry_backup)
        shutil.copy2(self.root / ".brain/applied.json", applied_backup)
        target.write_text("partial replacement", encoding="utf-8")
        journal = {
            "version": 1,
            "transaction_id": transaction_id,
            "state": "committing",
            "entries": [
                {
                    "path": relative,
                    "backup": page_backup.relative_to(self.root).as_posix(),
                    "existed": True,
                }
            ],
            "replaced": [relative],
            "inflight": None,
            "registry_backup": registry_backup.relative_to(self.root).as_posix(),
            "applied_backup": applied_backup.relative_to(self.root).as_posix(),
        }
        (self.root / ".brain/apply-journal.json").write_text(
            json.dumps(journal), encoding="utf-8"
        )
        FileVault(self.root)
        self.assertEqual(target.read_text(encoding="utf-8"), original)
        self.assertFalse((self.root / ".brain/apply-journal.json").exists())

    def test_machine_consumers_receive_privacy_filtered_results(self) -> None:
        captured = self.service.capture(
            None, text="Private local evidence", title="Private evidence"
        )
        cloud = self.service.search("private evidence", consumer="cloud")
        human = self.service.search("private evidence", consumer="human")
        self.assertEqual(cloud["count"], 0)
        self.assertGreaterEqual(cloud["redacted"], 1)
        self.assertEqual(human["count"], 1)
        self.service.file(captured["source"]["content_hash"], "10-work")
        local_context = self.service.context(
            captured["source"]["content_hash"], consumer="local"
        )
        self.assertFalse(local_context["evidence"])
        # The bundle counts the redaction without describing it: naming the
        # path would disclose the document and the branch it lives in.
        self.assertEqual(local_context["redacted"], 1)
        serialized = json.dumps(local_context, ensure_ascii=False)
        for disclosure in ("10-work", "never-ingest", "Private evidence", "raw/"):
            self.assertNotIn(disclosure, serialized)

    def test_graph_expansion_cannot_bypass_privacy_filter(self) -> None:
        public = self.service.capture(None, text="Evidence A", title="Evidence A")
        private = self.service.capture(None, text="Evidence B", title="Evidence B")
        public_hash = public["source"]["content_hash"]
        private_hash = private["source"]["content_hash"]
        self.service.file(private_hash, "10-work")
        self.service.apply(
            {
                "operations": [
                    {
                        "action": "upsert",
                        "kind": "concept",
                        "slug": "public-beacon",
                        "title": "Public beacon",
                        "aliases": [],
                        "source_hashes": [public_hash],
                        "body": (
                            "Public beacon points to [[private-neighbor]]."
                            f"[^source:{public_hash}]"
                        ),
                        "links": ["private-neighbor"],
                    },
                    {
                        "action": "upsert",
                        "kind": "concept",
                        "slug": "private-neighbor",
                        "title": "Private neighbor",
                        "aliases": [],
                        "source_hashes": [private_hash],
                        "body": f"Restricted details.[^source:{private_hash}]",
                        "links": [],
                    },
                ]
            }
        )
        result = self.service.search("public beacon", limit=4, consumer="local")
        paths = {hit["path"] for hit in result["hits"]}
        self.assertIn("wiki/concepts/public-beacon.md", paths)
        self.assertNotIn("wiki/concepts/private-neighbor.md", paths)
        self.assertGreaterEqual(result["redacted"], 1)

    def test_graph_export_applies_the_consumer_privacy_boundary(self) -> None:
        captured = self.service.capture(
            None, text="Local graph evidence", title="Local graph evidence"
        )
        node_id = f'raw:{captured["source"]["content_hash"]}'
        human = self.service.graph_data(consumer="human")
        cloud = self.service.graph_data(consumer="cloud")
        cloud_status = self.service.reader_status(consumer="cloud")
        self.assertIn(node_id, {node["id"] for node in human["nodes"]})
        self.assertNotIn(node_id, {node["id"] for node in cloud["nodes"]})
        self.assertGreaterEqual(cloud["redacted_nodes"], 1)
        self.assertEqual(cloud_status["sources"], 0)
        self.assertEqual(cloud_status["by_branch"], {})
        self.assertGreaterEqual(cloud_status["redacted_sources"], 1)

    def test_graph_data_bounds_by_degree_when_a_limit_is_given(self) -> None:
        # The bug report: a 34k-node vault's `/api/graph` shipped every node
        # unbounded (8 MB, ~18s) for a client that only ever renders its own
        # top ~1,100 by degree anyway. `limit` reproduces that same
        # rank-then-slice server-side, so the hub survives a limit of 1 and a
        # leaf does not.
        captured = self.service.capture(None, text="Evidence", title="Evidence")
        content_hash = captured["source"]["content_hash"]
        self.service.apply(
            {
                "operations": [
                    {
                        "action": "upsert",
                        "kind": "concept",
                        "slug": "hub",
                        "title": "Hub",
                        "aliases": [],
                        "source_hashes": [content_hash],
                        "body": f"The center.[^source:{content_hash}]",
                        "links": [],
                    },
                    {
                        "action": "upsert",
                        "kind": "concept",
                        "slug": "leaf-one",
                        "title": "Leaf one",
                        "aliases": [],
                        "source_hashes": [content_hash],
                        "body": f"Points at [[hub]].[^source:{content_hash}]",
                        "links": ["hub"],
                    },
                    {
                        "action": "upsert",
                        "kind": "concept",
                        "slug": "leaf-two",
                        "title": "Leaf two",
                        "aliases": [],
                        "source_hashes": [content_hash],
                        "body": f"Also points at [[hub]].[^source:{content_hash}]",
                        "links": ["hub"],
                    },
                ]
            }
        )

        unbounded = self.service.graph_data(consumer="human")
        self.assertNotIn("total_nodes", unbounded)
        self.assertNotIn("hidden_nodes", unbounded)

        bounded = self.service.graph_data(consumer="human", limit=1)
        self.assertEqual(len(bounded["nodes"]), 1)
        self.assertEqual(bounded["nodes"][0]["id"], "page:wiki/concepts/hub.md")
        self.assertEqual(bounded["total_nodes"], len(unbounded["nodes"]))
        self.assertEqual(bounded["hidden_nodes"], len(unbounded["nodes"]) - 1)
        self.assertEqual(bounded["edges"], [])

        # export() and integration_sync() must never see a truncated graph --
        # data loss on a full export is not the trade `limit` exists to make.
        exported = self.service.export("json")
        exported_payload = json.loads(
            Path(self.root, exported["path"]).read_text(encoding="utf-8")
        )
        self.assertEqual(len(exported_payload["nodes"]), len(unbounded["nodes"]))

    def test_obsidian_sync_is_manifest_based_and_persistent(self) -> None:
        with tempfile.TemporaryDirectory() as destination:
            configured = self.service.integration_configure(
                "obsidian",
                enabled=True,
                managed=False,
                options={"path": destination, "subdirectory": "brainskit"},
            )
            self.assertTrue(configured["policy"]["enabled"])
            synced = self.service.integration_sync("obsidian")
            target = Path(destination) / "brainskit"
            self.assertTrue((target / "wiki/index.md").is_file())
            self.assertTrue((target / "views/home.md").is_file())
            self.assertTrue((target / "graph/graph.json").is_file())
            self.assertGreater(synced["managed_file_count"], 2)
            reloaded = FileVault(self.root).config()
            self.assertTrue(reloaded.integrations["obsidian"].enabled)
            runtime = self.vault.read_state("integration-state")
            self.assertEqual(
                len(runtime["integrations"]["obsidian"]["managed_files"]),
                synced["managed_file_count"],
            )

    def test_database_integration_requires_an_explicit_privacy_consumer(self) -> None:
        with self.assertRaises(ValidationError):
            self.service.integration_configure(
                "neo4j",
                enabled=True,
                managed=False,
                options={
                    "uri": "bolt://127.0.0.1:7687",
                    "user": "neo4j",
                    "password_env": "BRAINSKIT_TEST_NEO4J_PASSWORD",
                },
            )

    def test_web_viewer_serves_health_graph_and_complete_html(self) -> None:
        server = build_server(
            self.service,
            host="127.0.0.1",
            port=0,
            consumer="human",
            instance_id="test-instance",
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base_url = f"http://127.0.0.1:{server.server_port}"
        try:
            with urlopen(f"{base_url}/api/health", timeout=2) as response:
                health = json.loads(response.read())
            with urlopen(f"{base_url}/api/graph", timeout=2) as response:
                graph = json.loads(response.read())
            with urlopen(f"{base_url}/api/sources", timeout=2) as response:
                sources = json.loads(response.read())
            with urlopen(f"{base_url}/api/pages", timeout=2) as response:
                pages = json.loads(response.read())
            with urlopen(f"{base_url}/api/timeline", timeout=2) as response:
                timeline = json.loads(response.read())
            with urlopen(f"{base_url}/api/integrations", timeout=2) as response:
                integrations = json.loads(response.read())
            with urlopen(base_url, timeout=2) as response:
                html = response.read().decode("utf-8")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
        self.assertTrue(health["ok"])
        self.assertTrue(graph["ok"])
        self.assertTrue(sources["ok"])
        self.assertTrue(pages["ok"])
        self.assertTrue(timeline["ok"])
        self.assertTrue(integrations["ok"])
        self.assertIn('<canvas id="graph">', html)
        self.assertIn("/api/search", html)
        self.assertIn("/api/proposals", html)
        self.assertIn("/api/resource", html)
        for view in ("graph", "sources", "pages", "timeline", "integrations"):
            self.assertIn(f'data-view="{view}"', html)

    def test_handle_error_silences_a_vanished_client_but_not_a_real_bug(self) -> None:
        # The bug report: a page load fired several requests, a browser tab
        # closed or reloaded mid-request, and the stdlib's default
        # `handle_error` printed a full traceback for a client that was
        # never coming back. A genuine bug in a handler must still surface.
        server = build_server(
            self.service,
            host="127.0.0.1",
            port=0,
            consumer="human",
            instance_id="test-instance",
        )
        try:
            for vanished in (
                ConnectionResetError,
                BrokenPipeError,
                ConnectionAbortedError,
            ):
                stderr = StringIO()
                with redirect_stderr(stderr):
                    try:
                        raise vanished("simulated client disconnect")
                    except vanished:
                        server.handle_error(None, ("127.0.0.1", 0))
                self.assertEqual(stderr.getvalue(), "")

            stderr = StringIO()
            with redirect_stderr(stderr):
                try:
                    raise ValueError("a real bug in a handler")
                except ValueError:
                    server.handle_error(None, ("127.0.0.1", 0))
            self.assertIn("ValueError", stderr.getvalue())
        finally:
            server.server_close()

    def test_web_viewer_fly_never_writes_a_nan_theta(self) -> None:
        # `flyTo()` only ever sets sx/sy/sz/ex/ey/ez/sd/ed/t on G.fly — never
        # `.theta` — so `G.cam.theta=f.theta+0` was always `NaN`, permanently
        # corrupting the orbit angle on every fly (every graph load, every
        # node click) and making the whole WebGL scene project to NaN NDC
        # coordinates. The fix removes the statement outright; theta/phi stay
        # under the user's orbit controls exclusively.
        self.assertNotIn("f.theta+0", WEB_VIEWER_HTML)
        self.assertNotIn("G.cam.theta=f.theta", WEB_VIEWER_HTML)

    def test_web_viewer_graph_meta_captions_parenthesize_their_fallback(self) -> None:
        # `X||Y+'literal'` binds as `X||(Y+'literal')` in JS — `+` is tighter
        # than `||` — so a truthy `X` short-circuits away everything appended
        # after it. Two call sites had this shape: buildGraph()'s node/edge
        # count caption, and showCodeNode()'s kind/path/line caption (the
        # twin this fix's TWINS check found). buildGraph now assembles the
        # caption from segments (zero-count segments are omitted), but each
        # fallback must still be parenthesized before anything concatenates
        # onto it.
        self.assertIn(
            "(data.total_nodes||data.nodes.length)+' nodes'", WEB_VIEWER_HTML
        )
        self.assertIn(
            "(data.total_edges||data.edges.length)+' edges'", WEB_VIEWER_HTML
        )
        self.assertIn(
            "(nd.kind||'code symbol')+' · '+(nd.path||'')", WEB_VIEWER_HTML
        )
        # And the broken shapes must not have come back.
        self.assertNotIn("data.total_nodes||data.nodes.length+' nodes", WEB_VIEWER_HTML)
        self.assertNotIn("nd.kind||'code symbol'+' · '", WEB_VIEWER_HTML)

    def test_web_viewer_ring_material_parenthesizes_each_channel_truncation(
        self,
    ) -> None:
        # `'rgba('+rgb[0]*255|0+','+...` is the same precedence-bug family as
        # the `||` traps above, but with `|` (bitwise OR): `|` binds looser
        # than `+`, so the whole chain collapses to a single `|` reduction
        # over ToInt32-coerced operands instead of building an rgba string —
        # the entire expression evaluates to the number 0, and
        # CanvasGradient.addColorStop throws a SyntaxError on that value.
        # Each `channel*255|0` truncation must be parenthesized before the
        # string concatenation joins them.
        self.assertIn("(rgb[0]*255|0)", WEB_VIEWER_HTML)
        self.assertIn("(rgb[1]*255|0)", WEB_VIEWER_HTML)
        self.assertIn("(rgb[2]*255|0)", WEB_VIEWER_HTML)
        # And the broken unparenthesized shape must not have come back.
        self.assertNotIn("rgb[0]*255|0+','+rgb[1]", WEB_VIEWER_HTML)

        # Stronger check: every arithmetic sub-expression between the
        # addColorStop(0.74,'rgba(' call and its closing ",0.9)')" is wrapped
        # in its own parens, so no bare `X|0` can leak into the concatenation.
        call = re.search(
            r"addColorStop\(0\.74,'rgba\('(.*?),0\.9\)'\)", WEB_VIEWER_HTML
        )
        self.assertIsNotNone(call)
        bare_pipe_zero = re.compile(r"(?<!\()[\w.\[\]]+\*255\|0(?!\))")
        self.assertEqual(bare_pipe_zero.findall(call.group(1)), [])

    def test_web_viewer_html_has_no_remaining_precedence_traps(self) -> None:
        # TWINS check, kept as a standing regression test rather than a
        # one-off grep: any `ident||'literal'+` shape anywhere in the shipped
        # JS is the same operator-precedence trap as the two fixed above.
        script = re.search(r"<script>\n(.*)\n</script>", WEB_VIEWER_HTML, re.S)
        self.assertIsNotNone(script)
        trap = re.compile(r"[\w.\[\]]+\|\|'[^']*'\+")
        self.assertEqual(trap.findall(script.group(1)), [])

    def test_web_viewer_fog_range_contains_every_node_at_every_zoom(self) -> None:
        # This test used to check the fog range at one distance -- the one a
        # fly-to happens to end at -- and passed the whole time scrolling out
        # rendered a black screen: the wheel takes the camera to 1600 while a
        # fog fitted around a ~300-unit graph ends at ~1670, so every node sat
        # at or past the far plane and was drawn in the fog colour. The range
        # has to hold across the whole zoom range, so the fog is refit from
        # the camera's *current* distance on every frame.
        self.assertIn("const FOG_NEAR_K=1.5,FOG_FAR_K=2.5,FOG_REFIT_MS=200;", WEB_VIEWER_HTML)
        self.assertIn(
            "function fitFog(dist){if(!G.scene||!G.scene.fog)return;let R=fogRadius();"
            "G.scene.fog.near=Math.max(1,dist-R*FOG_NEAR_K);"
            "G.scene.fog.far=dist+R*FOG_FAR_K}",
            WEB_VIEWER_HTML,
        )
        self.assertIn("fitFog(G.cam.dist);setCamera();", WEB_VIEWER_HTML)
        # Nothing else may pin the fog to one distance behind the loop's back.
        self.assertNotIn("G.scene.fog.near=Math.max(20,", WEB_VIEWER_HTML)
        self.assertIn("fog:false", WEB_VIEWER_HTML)  # star field ignores graph zoom

        def fog_bounds(radius: float, dist: float) -> tuple[float, float]:
            return max(1.0, dist - radius * 1.5), dist + radius * 2.5

        # 40 = buildGraph's clamp floor; 600 = the real ~81-node knowledge
        # graph this bug was root-caused against; 3000 = a synthetic
        # large-graph radius, well past a ~1000+ node code graph. The
        # distances are the wheel clamp's own ends plus the fitted distance
        # each radius settles at.
        for radius in (40, 600, 3000):
            for dist in (40, max(150.0, radius * 2.7), 1600):
                near, far = fog_bounds(radius, dist)
                self.assertGreaterEqual(far, dist + radius, (radius, dist))
                if dist - radius >= 1:
                    self.assertLessEqual(near, dist - radius, (radius, dist))

    def test_web_viewer_hover_overlays_scale_with_camera_distance(self) -> None:
        # The bug report: a near-empty (2-node) vault has a graph radius
        # small enough that flyToNode's Math.max(40,R*0.55) lands the camera
        # well under the ~150 units the ring/label sizes were tuned against,
        # and the label -- unlike a ring, which is just a glow -- is legible
        # text: it filled most of the viewport, clipped by the canvas edge.
        # `dscale` (camera distance over that same 150-unit reference) must
        # multiply every hover-overlay size, so the fixed, un-scaled shapes
        # must not remain.
        self.assertIn("let dscale=G.cam.dist/150;", WEB_VIEWER_HTML)
        self.assertIn("ring.scale.set(26*dscale,26*dscale,1)", WEB_VIEWER_HTML)
        self.assertIn("r2.scale.set(18*dscale,18*dscale,1)", WEB_VIEWER_HTML)
        self.assertIn("label.position.set(p.x,p.y+22*dscale,p.z)", WEB_VIEWER_HTML)
        self.assertIn("label.scale.set(ar*8*dscale,8*dscale,1)", WEB_VIEWER_HTML)
        self.assertNotIn("ring.scale.set(26,26,1)", WEB_VIEWER_HTML)
        self.assertNotIn("r2.scale.set(18,18,1)", WEB_VIEWER_HTML)
        self.assertNotIn("label.scale.set(ar*8,8,1)", WEB_VIEWER_HTML)
        # The creation-time size is only the first frame: flyToNode changes
        # G.cam.dist ~10x over the fly, so each overlay stores its base size
        # in world-units-per-150-dist and animate() recomputes the scale from
        # the CURRENT camera distance every frame — otherwise rings and label
        # balloon relative to the graph mid-fly.
        self.assertIn("G.rings.push({sprite:ring,base:26})", WEB_VIEWER_HTML)
        self.assertIn("G.rings.push({sprite:r2,base:18})", WEB_VIEWER_HTML)
        self.assertIn("label.userData={ar,baseY:p.y};", WEB_VIEWER_HTML)
        self.assertIn(
            "G.label.scale.set(u.ar*8*dscale,8*dscale,1);"
            "G.label.position.y=u.baseY+22*dscale",
            WEB_VIEWER_HTML,
        )

    def test_web_viewer_graph_empty_state_markup_and_wiring(self) -> None:
        # A fresh vault's graph is two system pages (index.md, log.md) and
        # nothing else -- the exact shape behind the screenshot this design
        # pass answers: a near-empty 3D scene with no explanation and no next
        # step. `showGraphEmpty` and its DOM hooks must all ship together.
        for element_id in (
            'id="graphEmpty"',
            'id="graphEmptyTitle"',
            'id="graphEmptyBody"',
            'id="graphEmptyAction"',
            'id="graphEmptyCli"',
        ):
            self.assertIn(element_id, WEB_VIEWER_HTML)
        self.assertIn(".graph-empty{position:absolute", WEB_VIEWER_HTML)
        self.assertIn("function showGraphEmpty(show,opts){", WEB_VIEWER_HTML)

        # buildGraph: cleared on every load, shown only when nothing but the
        # system pages exist, never for the code graph (its own empty case
        # is the "not built yet" branch below, not "vault has no content").
        self.assertIn("function buildGraph(data,source){showGraphEmpty(false);", WEB_VIEWER_HTML)
        self.assertIn(
            "if(source!=='code'&&!data.nodes.some(nd=>nd.kind!=='system')){"
            "showGraphEmpty(true,{title:'Nothing captured yet',",
            WEB_VIEWER_HTML,
        )
        self.assertIn("action:{label:'Capture something',onClick:()=>openModal('captureModal')}", WEB_VIEWER_HTML)
        self.assertIn("cli:'bk capture'", WEB_VIEWER_HTML)

        # loadGraph: cleared before every fetch (no stale message flashes
        # while switching tabs), and the code graph's "nothing built yet"
        # case goes through the same overlay instead of a plain text line.
        self.assertIn("showGraphLoading(true);showGraphEmpty(false);", WEB_VIEWER_HTML)
        self.assertIn(
            "showGraphEmpty(true,{title:'No code graph yet',", WEB_VIEWER_HTML
        )
        self.assertIn("cli:'bk code build'", WEB_VIEWER_HTML)

        # A successful capture must invalidate the cached graph and reload
        # the active tab -- otherwise the empty state (or a stale graph)
        # outlives the capture that was supposed to fill it in. Invalidation
        # goes through one helper that also drops the collection and
        # resource caches, so no fetch layer serves pre-capture data.
        self.assertIn(
            "function invalidateData(){state.cache={};state.graph_cache={};"
            "state.resourceCache={}}",
            WEB_VIEWER_HTML,
        )
        self.assertIn(
            "invalidateData();if(state.source!=='code')loadGraph(state.source);load()",
            WEB_VIEWER_HTML,
        )

    def test_web_viewer_static_assets_cache_while_dynamic_responses_do_not(self) -> None:
        server = build_server(
            self.service,
            host="127.0.0.1",
            port=0,
            consumer="human",
            instance_id="test-instance",
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base_url = f"http://127.0.0.1:{server.server_port}"
        try:
            with urlopen(f"{base_url}/static/three.min.js", timeout=2) as response:
                static_cache_control = response.headers.get("Cache-Control")
            with urlopen(f"{base_url}/api/health", timeout=2) as response:
                json_cache_control = response.headers.get("Cache-Control")
            with urlopen(base_url, timeout=2) as response:
                html_cache_control = response.headers.get("Cache-Control")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
        self.assertEqual(json_cache_control, "no-store")
        self.assertEqual(html_cache_control, "no-store")
        self.assertNotEqual(static_cache_control, "no-store")
        self.assertIn("immutable", static_cache_control)

    def test_web_viewer_uses_http11_and_gzips_compressible_bodies(self) -> None:
        server = build_server(
            self.service,
            host="127.0.0.1",
            port=0,
            consumer="human",
            instance_id="test-instance",
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base_url = f"http://127.0.0.1:{server.server_port}"
        try:
            gzip_request = Request(base_url, headers={"Accept-Encoding": "gzip"})
            with urlopen(gzip_request, timeout=2) as response:
                self.assertEqual(response.version, 11)
                self.assertEqual(response.headers.get("Content-Encoding"), "gzip")
                compressed = response.read()
            uncompressed_length = len(WEB_VIEWER_HTML.encode("utf-8"))

            # Below the compression floor, gzip is skipped even when asked for.
            small_request = Request(
                f"{base_url}/api/health", headers={"Accept-Encoding": "gzip"}
            )
            with urlopen(small_request, timeout=2) as response:
                self.assertIsNone(response.headers.get("Content-Encoding"))
                small_body = response.read()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
        self.assertEqual(gzip.decompress(compressed).decode("utf-8"), WEB_VIEWER_HTML)
        self.assertLess(len(compressed), uncompressed_length)
        self.assertTrue(json.loads(small_body)["ok"])

    def _run_web_without_blocking(self, **kwargs) -> None:
        """Drive `run_web` synchronously by stubbing out `serve_forever`.

        `run_web` blocks forever on the real server loop, which is right for
        the CLI and wrong for a test: patching `serve_forever` to a no-op
        lets every statement before and after it (bind, print, auto-open,
        close) run exactly once and return, so the browser-open behavior can
        be asserted without a background thread.
        """

        with mock.patch(
            "brainskit.interfaces.web.BrainskitWebServer.serve_forever",
            return_value=None,
        ):
            run_web(self.service, port=0, **kwargs)

    def test_run_web_opens_a_browser_only_for_the_human_consumer(self) -> None:
        for consumer in ("local", "cloud"):
            with self.subTest(consumer=consumer):
                with mock.patch("webbrowser.open") as opener:
                    self._run_web_without_blocking(
                        host="127.0.0.1", consumer=consumer
                    )
                opener.assert_not_called()
        with mock.patch("webbrowser.open", return_value=True) as opener:
            self._run_web_without_blocking(host="127.0.0.1", consumer="human")
        opener.assert_called_once()
        (url,), _kwargs = opener.call_args
        self.assertRegex(url, r"^http://127\.0\.0\.1:\d+/$")

    def test_run_web_no_browser_flag_suppresses_auto_open(self) -> None:
        with mock.patch("webbrowser.open") as opener:
            self._run_web_without_blocking(
                host="127.0.0.1", consumer="human", open_browser=False
            )
        opener.assert_not_called()

    def test_run_web_survives_a_browser_open_failure(self) -> None:
        # Two distinct failure shapes `webbrowser.open` can take depending on
        # platform: an exception, or a plain `False` return -- neither may
        # crash the server or propagate out of `run_web`.
        with mock.patch("webbrowser.open", side_effect=webbrowser.Error("no display")):
            with redirect_stderr(StringIO()) as err:
                self._run_web_without_blocking(host="127.0.0.1", consumer="human")
        self.assertIn("brainskit web viewer ready at", err.getvalue())
        with mock.patch("webbrowser.open", return_value=False):
            with redirect_stderr(StringIO()) as err:
                self._run_web_without_blocking(host="127.0.0.1", consumer="human")
        self.assertIn("brainskit web viewer ready at", err.getvalue())

    def test_build_server_falls_back_to_the_next_port_when_taken(self) -> None:
        holder = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        holder.bind(("127.0.0.1", 0))
        holder.listen(1)
        held_port = holder.getsockname()[1]
        try:
            server = build_server(
                self.service, host="127.0.0.1", port=held_port, consumer="human"
            )
            self.assertNotEqual(server.server_port, held_port)
            self.assertLess(server.server_port - held_port, 10)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                with urlopen(
                    f"http://127.0.0.1:{server.server_port}/api/health", timeout=2
                ) as response:
                    self.assertTrue(json.loads(response.read())["ok"])
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)
        finally:
            holder.close()

    def test_run_web_reports_the_port_actually_used_after_a_fallback(self) -> None:
        holder = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        holder.bind(("127.0.0.1", 0))
        holder.listen(1)
        held_port = holder.getsockname()[1]
        try:
            with mock.patch(
                "brainskit.interfaces.web.BrainskitWebServer.serve_forever",
                return_value=None,
            ):
                with redirect_stderr(StringIO()) as err:
                    run_web(
                        self.service,
                        host="127.0.0.1",
                        port=held_port,
                        consumer="local",
                        open_browser=False,
                    )
        finally:
            holder.close()
        message = err.getvalue()
        self.assertIn(f"{held_port} was already in use", message)
        self.assertNotIn(f":{held_port}/", message)

    def test_build_server_raises_a_friendly_error_when_every_fallback_port_is_taken(
        self,
    ) -> None:
        holder = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        holder.bind(("127.0.0.1", 0))
        holder.listen(1)
        base_port = holder.getsockname()[1]
        holders = [holder]
        try:
            for offset in range(1, 10):
                extra = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                extra.bind(("127.0.0.1", base_port + offset))
                extra.listen(1)
                holders.append(extra)
            with self.assertRaises(ValidationError) as caught:
                build_server(
                    self.service, host="127.0.0.1", port=base_port, consumer="human"
                )
            self.assertIn("ports_tried", caught.exception.details)
        finally:
            for held in holders:
                held.close()

    def test_web_viewer_graph_source_switches_are_cached_client_side(self) -> None:
        # loadGraph() must consult state.graph_cache before hitting the
        # network, the same pattern the other collection views already use —
        # otherwise switching Knowledge/+Inferred/Code always refetches.
        self.assertIn("graph_cache:{}", WEB_VIEWER_HTML)
        self.assertIn("let data=state.graph_cache[source];if(!data){", WEB_VIEWER_HTML)
        self.assertIn("state.graph_cache[source]=data}buildGraph(data,source)", WEB_VIEWER_HTML)

    def test_web_viewer_animate_skips_work_while_the_graph_view_is_hidden(self) -> None:
        # The DOM lookup is cached once (not re-queried every frame), and the
        # hidden-view check runs before stepSim/ring-pulsation/setCamera, not
        # only before the final render() call.
        self.assertIn("const graphViewEl=document.getElementById('graphView');", WEB_VIEWER_HTML)
        self.assertIn(
            "function animate(){requestAnimationFrame(animate);"
            "if(graphViewEl.classList.contains('hidden'))return;",
            WEB_VIEWER_HTML,
        )

    def test_web_viewer_inspector_has_a_fullscreen_expand_toggle(self) -> None:
        self.assertIn('id="expandDetail"', WEB_VIEWER_HTML)
        self.assertIn(".detail.expanded{position:fixed;inset:0;", WEB_VIEWER_HTML)
        self.assertIn(
            "document.getElementById('expandDetail').onclick=()=>{"
            "document.getElementById('detail').classList.toggle('expanded');",
            WEB_VIEWER_HTML,
        )
        # Escape collapses the expanded inspector alongside the existing
        # closeModals() behaviour, without replacing it.
        self.assertIn(
            "if(e.key==='Escape'){closeModals();"
            "document.getElementById('detail').classList.remove('expanded');",
            WEB_VIEWER_HTML,
        )
        # The pre-existing mobile drawer (`.detail.open`) must still be intact.
        self.assertIn(".detail.open{transform:none}", WEB_VIEWER_HTML)

    def test_web_viewer_markdown_renderer_escapes_before_transforming(self) -> None:
        # `escapeHtml` must be defined, and it must run before any markdown
        # block/inline transform touches the text — this is what makes the
        # renderer safe for untrusted captured content once it is fed to
        # innerHTML. `mdToHtml` is the only entry point and must compose
        # `mdBlocks(escapeHtml(raw))` in that order, not the reverse.
        self.assertIn(
            "function escapeHtml(raw){return String(raw==null?'':raw)"
            ".replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')"
            ".replace(/\"/g,'&quot;').replace(/'/g,'&#39;')}",
            WEB_VIEWER_HTML,
        )
        self.assertIn(
            "function mdToHtml(raw){return mdBlocks(escapeHtml("
            "stripFrontmatter(raw)))}",
            WEB_VIEWER_HTML,
        )
        # `stripFrontmatter` only removes a leading `---`...`---` YAML block
        # (this vault's wiki pages all carry one) — a substring deletion, not
        # a transform that touches innerHTML, so it may run before the
        # escaping step without weakening it.
        self.assertIn(
            "function stripFrontmatter(raw){return String(raw==null?'':raw)"
            ".replace(/^---\\r?\\n[\\s\\S]*?\\r?\\n---\\r?\\n?/,'')}",
            WEB_VIEWER_HTML,
        )
        escape_pos = WEB_VIEWER_HTML.index("function escapeHtml")
        blocks_pos = WEB_VIEWER_HTML.index("function mdBlocks")
        to_html_pos = WEB_VIEWER_HTML.index("function mdToHtml")
        self.assertLess(escape_pos, blocks_pos)
        self.assertLess(blocks_pos, to_html_pos)

    def test_web_viewer_markdown_only_applies_to_markdown_looking_resources(
        self,
    ) -> None:
        # `.md` path/id, or a wiki page kind, gets rendered; everything else
        # (code symbols, non-.md raw sources) must keep the raw <pre> path.
        self.assertIn(
            "function looksLikeMarkdown(item){let path=String((item&&"
            "(item.path||item.id))||'');if(/\\.md$/i.test(path))return true;"
            "return ['source','entity','concept','synthesis']"
            ".indexOf(item&&item.kind)!==-1}",
            WEB_VIEWER_HTML,
        )
        # showCodeNode is untouched: still a raw <pre>, never fed through the
        # markdown renderer.
        code_node = re.search(
            r"function showCodeNode\(nd\)\{.*?\}\n", WEB_VIEWER_HTML
        )
        self.assertIsNotNone(code_node)
        self.assertIn("pre.className='content'", code_node.group(0))
        self.assertNotIn("mdToHtml", code_node.group(0))
        self.assertNotIn("looksLikeMarkdown", code_node.group(0))

    def test_web_viewer_resource_inspector_has_raw_rendered_toggle(self) -> None:
        self.assertIn("rendered.className='md-content'", WEB_VIEWER_HTML)
        self.assertIn("rendered.innerHTML=mdToHtml(item.content)", WEB_VIEWER_HTML)
        self.assertIn("toggle.className='view-toggle'", WEB_VIEWER_HTML)
        self.assertIn("rBtn.textContent='Rendered'", WEB_VIEWER_HTML)
        self.assertIn("wBtn.textContent='Raw'", WEB_VIEWER_HTML)
        self.assertIn(".md-content{", WEB_VIEWER_HTML)
        self.assertIn(".toggle-btn{", WEB_VIEWER_HTML)

    def test_web_viewer_ask_answer_renders_as_markdown(self) -> None:
        # TWINS follow-up: the Ask answer box is the other place raw resource
        # content was dumped via escText into a <pre>. LLM answers are
        # synthesized prose (not a citable byte-exact source), so the chat
        # thread's answer card renders unconditionally as markdown through
        # the same mdToHtml pipeline, without a Raw/Rendered toggle.
        self.assertIn("box.className='md-content chat-answer'", WEB_VIEWER_HTML)
        self.assertIn("box.innerHTML=mdToHtml(r.answer)", WEB_VIEWER_HTML)
        self.assertNotIn("escText(pre,r.answer)", WEB_VIEWER_HTML)
        # The query job emits `uncertainty` as free prose as often as a level
        # word: a short value rides inside the badge, long prose renders as a
        # muted note beside a compact badge instead of a paragraph-sized pill.
        self.assertIn(
            "let short=unc.length<=40;"
            "escText(b,short?'uncertainty '+unc:'uncertainty')",
            WEB_VIEWER_HTML,
        )

    def test_web_viewer_collection_filters_are_derived_from_loaded_data(self) -> None:
        # The field list per view, and proof the chip label/count comes from
        # counting the actually-loaded items rather than a hardcoded list.
        self.assertIn(
            "const COLLECTION_FILTER_FIELDS={sources:['branch','privacy'],"
            "pages:['kind','freshness'],timeline:['type'],"
            "integrations:['state']};",
            WEB_VIEWER_HTML,
        )
        self.assertIn("counts[v]=(counts[v]||0)+1", WEB_VIEWER_HTML)
        self.assertIn("escText(chip,value+' ('+counts[value]+')')", WEB_VIEWER_HTML)
        # OR within a field, AND across fields.
        self.assertIn(
            "return items.filter(it=>Object.keys(selected).every(field=>{"
            "let set=selected[field];return !set||!set.size||"
            "set.has(it[field])}))",
            WEB_VIEWER_HTML,
        )

    def test_web_viewer_timeline_has_its_own_chronological_renderer(self) -> None:
        # Dispatch: the timeline view must not fall through to the generic
        # card-grid renderer used by sources/pages/integrations.
        self.assertIn("function renderTimeline(events){", WEB_VIEWER_HTML)
        self.assertIn(
            "if(name==='timeline'){document.getElementById('collectionGrid')"
            ".classList.add('hidden');", WEB_VIEWER_HTML
        )
        self.assertIn("renderTimeline(filtered)", WEB_VIEWER_HTML)
        # Grouped by local calendar day, sticky day headers, and the two
        # event types reuse the app's existing cyan/blue tokens.
        self.assertIn("function timelineDayKey(d){", WEB_VIEWER_HTML)
        self.assertIn(".timeline-day-head{position:sticky;top:0;", WEB_VIEWER_HTML)
        self.assertIn(
            "dot.className='dot timeline-dot'+(ev.type==='captured'?'':' blue')",
            WEB_VIEWER_HTML,
        )
        self.assertIn(".dot.blue{background:var(--blue);", WEB_VIEWER_HTML)

    def test_web_viewer_md_content_wraps_long_unbreakable_tokens(self) -> None:
        # A pipe table with a hash in one cell forces `width:100%` past the
        # docked Inspector panel's ~350px: `overflow-wrap` cannot help a
        # table shrink below its cells' minimum content width, so the wrap
        # needs `overflow-wrap:anywhere` on the flowing containers *and* a
        # scrolling wrapper around the table as a second line of defense.
        self.assertIn(
            "min-height:180px;overflow-wrap:anywhere}", WEB_VIEWER_HTML
        )
        self.assertIn(
            "background:var(--panel2);border-radius:4px;padding:1px 5px;"
            "overflow-wrap:anywhere}",
            WEB_VIEWER_HTML,
        )
        self.assertIn(
            "text-align:left;overflow-wrap:anywhere}.md-content th{",
            WEB_VIEWER_HTML,
        )
        self.assertIn(
            ".md-content a{color:var(--blue);overflow-wrap:anywhere}",
            WEB_VIEWER_HTML,
        )
        self.assertIn(".md-content .table-wrap{overflow-x:auto;", WEB_VIEWER_HTML)
        # mdBlocks() must actually emit that wrapper around every table.
        self.assertIn(
            "out.push('<div class=\"table-wrap\">'+table+'</table></div>');",
            WEB_VIEWER_HTML,
        )

    def test_web_viewer_render_cover_is_deterministic_and_type_colored(self) -> None:
        # Same seed -> same cached SVG; different seed -> a different one.
        # Built from the app's own hash() (never a second hash function or
        # Math.random), colored from the two tokens already used for the
        # timeline dots — never a new hex literal.
        self.assertIn("function renderCover(seed,type){", WEB_VIEWER_HTML)
        cover = re.search(
            r"function renderCover\(seed,type\)\{(.*?)\n", WEB_VIEWER_HTML
        )
        self.assertIsNotNone(cover)
        body = cover.group(1)
        self.assertIn("state.coverCache[key]", body)
        self.assertIn("hash(seed+", body)
        self.assertNotIn("Math.random()", body)
        self.assertIn("type==='captured'?'var(--cyan)':'var(--blue)'", body)
        # Cache lookup must return before doing any recomputation.
        self.assertIn(
            "if(state.coverCache[key])return state.coverCache[key];", body
        )

    def test_web_viewer_timeline_cards_lazy_fetch_only_from_a_click_handler(
        self,
    ) -> None:
        # Level 1 (cover+time+title+badge) must come from data already on
        # the page; the per-event GET only happens once a card is opened.
        renderer = re.search(
            r"function renderTimeline\(events\)\{.*?root\.replaceChildren\(frag\)\}",
            WEB_VIEWER_HTML,
        )
        self.assertIsNotNone(renderer)
        body = renderer.group(0)
        self.assertIn("card.className='feed-card'", body)
        self.assertIn("cover.innerHTML=renderCover(seed,ev.type)", body)
        # The fetch must be textually inside the onclick callback, not in
        # the forEach body that builds the card.
        pre_click, _, after_click = body.partition("headBtn.onclick=()=>{")
        self.assertNotIn("await api(", pre_click)
        self.assertIn("await api('/api/resource?id='+encodeURIComponent(ev.id))", after_click)
        # A card already open collapses back to level 1 without refetching;
        # a second open reuses the cache instead of hitting the network again.
        self.assertIn("body.classList.toggle('hidden')", body)
        self.assertIn("if(!opening||loaded||!ev.id)return;", body)
        self.assertIn("let item=state.resourceCache[ev.id];if(!item){", body)
        # Level 3: a distinct control opens the same Inspector every other
        # click-to-inspect entry point in the app uses.
        self.assertIn("openBtn.textContent='Open in Inspector'", body)
        self.assertIn("openBtn.onclick=()=>showResource(ev.id)", body)
        # Excerpt reuses the existing markdown pipeline instead of a new one.
        self.assertIn(
            "excerpt.innerHTML=looksLikeMarkdown(item)?mdToHtml(item.content):",
            body,
        )
        self.assertIn(".feed-excerpt{overflow:hidden;", WEB_VIEWER_HTML)
        # The truncation shape (max-height + fade mask, never line-clamp) is
        # pinned by test_web_viewer_feed_excerpt_stacks_blocks_and_fades.
        self.assertIn("mask-image:linear-gradient(", WEB_VIEWER_HTML)

    def test_web_viewer_ring_pulse_amplitude_is_a_named_reduced_constant(
        self,
    ) -> None:
        # The user disliked the hover/selection ring's breathing glow.
        # Reduced from 0.12 to a named constant (0.04) rather than deleting
        # the mechanism, so it reads as a steady highlight, and so a human
        # can retune the single named constant later.
        self.assertIn("const RING_PULSE_AMPLITUDE=0.04;", WEB_VIEWER_HTML)
        # The pulse survives, but behind the reduced-motion gate: an OS-level
        # "reduce motion" preference gets a steady ring instead.
        self.assertIn(
            "p=REDUCED_MOTION?1:1+RING_PULSE_AMPLITUDE*Math.sin(tGlobal*2.2)",
            WEB_VIEWER_HTML,
        )
        self.assertNotIn("1+0.12*Math.sin(tGlobal*2.2)", WEB_VIEWER_HTML)
        # The pulse must multiply a STORED base size, never the sprite's
        # current scale: `s=r.sprite.scale.x; scale.set(s*p,...)` compounds
        # frame over frame (a random walk, measured x1.349 drift in 4s)
        # instead of oscillating within +/-RING_PULSE_AMPLITUDE.
        self.assertIn(
            "for(let r of G.rings){let s=r.base*dscale*p;r.sprite.scale.set(s,s,1)}",
            WEB_VIEWER_HTML,
        )
        self.assertNotIn("r.sprite.scale.x", WEB_VIEWER_HTML)
        # Untouched: fly-to easing, force-sim damping/gravity, star field.
        self.assertIn("let k=Math.min(1,f.t),e=1-Math.pow(1-k,3);", WEB_VIEWER_HTML)
        self.assertIn("damp:0.84", WEB_VIEWER_HTML)
        self.assertIn("opacity:0.65,map:dotTexture()", WEB_VIEWER_HTML)

    def test_web_viewer_nodes_are_shaded_instanced_atoms_with_degree_scaling(
        self,
    ) -> None:
        # The molecule look: nodes are lit spheres (InstancedMesh + Phong
        # under a hemisphere + key directional light), not additive dot
        # sprites. Per-instance radius encodes degree so hubs read as heavy
        # atoms, and colors flow through setColorAt so the semantic palettes
        # and mono mode keep working under the lighting.
        self.assertIn("new THREE.SphereGeometry(1,16,12)", WEB_VIEWER_HTML)
        self.assertIn(
            "new THREE.MeshPhongMaterial({color:0xffffff,shininess:40",
            WEB_VIEWER_HTML,
        )
        self.assertIn("new THREE.InstancedMesh(ageom,amat,n)", WEB_VIEWER_HTML)
        self.assertIn("new THREE.DirectionalLight(0xffffff,0.9)", WEB_VIEWER_HTML)
        self.assertIn(
            "clamp(1+ATOM_SCALE_K*Math.log(1+deg),1,ATOM_SCALE_MAX)",
            WEB_VIEWER_HTML,
        )
        self.assertIn("G.atoms.setColorAt(i,", WEB_VIEWER_HTML)
        # Picking uses the InstancedMesh instanceId everywhere the fuzzy
        # Points index used to be read; the Points threshold hack is gone.
        self.assertIn("hit.instanceId", WEB_VIEWER_HTML)
        self.assertNotIn("hit.index", WEB_VIEWER_HTML)
        self.assertNotIn("G.raycaster.params.Points.threshold", WEB_VIEWER_HTML)
        # The star backdrop is the only remaining Points/dot-sprite user;
        # the node sprite material (alphaTest) and G.points are fully gone.
        self.assertNotIn("alphaTest:0.01", WEB_VIEWER_HTML)
        self.assertNotIn("G.points", WEB_VIEWER_HTML)

    def test_web_viewer_bonds_are_split_color_half_cylinders_with_line_fallback(
        self,
    ) -> None:
        # The classic molecular bond: two thin open-ended half-cylinders per
        # edge, each colored by its endpoint atom — a sourced_from bond reads
        # evidence-cyan on the raw half flowing into the page's kind color.
        # Above the instancing budget the same endpoint coloring degrades to
        # gradient-colored line segments instead of vanishing. 6000 covers
        # the 1100-node/5051-edge code graph, measured at 120fps (idle and
        # mid-sim, 10,102 instance matrices rewritten per frame).
        self.assertIn("BOND_SPLIT_MAX_EDGES=6000", WEB_VIEWER_HTML)
        self.assertIn("new THREE.CylinderGeometry(1,1,1,8,1,true)", WEB_VIEWER_HTML)
        self.assertIn("edgeVerts.length*2", WEB_VIEWER_HTML)
        self.assertIn(
            "function bondRadius(){return atomBaseRadius()*0.18}", WEB_VIEWER_HTML
        )
        self.assertIn(
            "setBondHalf(i*2,ax,ay,az,mx,my,mz,r,s);"
            "setBondHalf(i*2+1,bx,by,bz,mx,my,mz,r,s)",
            WEB_VIEWER_HTML,
        )
        # Each half is oriented from the midpoint to its endpoint with a
        # quaternion from the unit Y axis, through reused scratch objects
        # (no per-frame allocations across ~10k matrix writes).
        self.assertIn("s.q.setFromUnitVectors(s.up,s.v2)", WEB_VIEWER_HTML)
        # Both halves brighten when adjacent to the focus, dim otherwise.
        self.assertIn("f=set?(lit?1.6:0.22):1", WEB_VIEWER_HTML)
        # The legend explains the split-color bond with a two-color capsule.
        self.assertIn("cap.className='bond'", WEB_VIEWER_HTML)
        self.assertIn("'evidence → page'", WEB_VIEWER_HTML)

    def test_web_viewer_click_inspects_and_only_double_click_flies(self) -> None:
        # The single biggest "awful mouse" complaint: clicking a node yanked
        # the camera across the scene. Single click = select + highlight +
        # inspector only; double click (two clicks on the same node within
        # DBLCLICK_MS) = fly to the node.
        select_node = re.search(r"function selectNode\(i\)\{.*?\}\n", WEB_VIEWER_HTML)
        self.assertIsNotNone(select_node)
        self.assertNotIn("flyToNode", select_node.group(0))
        self.assertNotIn("flyTo", select_node.group(0))
        self.assertIn("DBLCLICK_MS=300", WEB_VIEWER_HTML)
        self.assertIn(
            "function handleClick(i){let now=performance.now();"
            "if(G.lastClick&&G.lastClick.i===i&&now-G.lastClick.t<DBLCLICK_MS){"
            "G.lastClick=null;selectNode(i);flyToNode(i);return}",
            WEB_VIEWER_HTML,
        )
        # The footer hint teaches the new gestures.
        self.assertIn("double-click to fly · 0 to reset", WEB_VIEWER_HTML)

    def test_web_viewer_navigation_is_inertial_eased_and_reduced_motion_aware(
        self,
    ) -> None:
        # Inertial orbit: drag stays 1:1, release glides with exponential
        # damping, pointerdown hard-stops the glide.
        self.assertIn("const ORBIT_DAMPING=0.92", WEB_VIEWER_HTML)
        self.assertIn(
            "G.spin.t*=ORBIT_DAMPING;G.spin.p*=ORBIT_DAMPING", WEB_VIEWER_HTML
        )
        self.assertIn(
            "G.lastInteract=performance.now();G.spin.t=0;G.spin.p=0",
            WEB_VIEWER_HTML,
        )
        # Smooth zoom: the wheel writes a target distance; animate() eases
        # the actual distance toward it instead of snapping.
        self.assertIn("ZOOM_EASE=0.18", WEB_VIEWER_HTML)
        self.assertIn(
            "G.cam.distTarget=clamp(G.cam.distTarget*Math.exp(e.deltaY*0.0012),40,1600)",
            WEB_VIEWER_HTML,
        )
        self.assertNotIn("G.cam.dist=clamp(G.cam.dist*Math.exp", WEB_VIEWER_HTML)
        # Reset view: keyboard 0 (unless typing) and the toolbar button.
        self.assertIn('id="resetViewBtn"', WEB_VIEWER_HTML)
        self.assertIn(
            "document.getElementById('resetViewBtn').onclick=resetView",
            WEB_VIEWER_HTML,
        )
        self.assertIn("if(e.key==='0'&&!e.metaKey&&!e.ctrlKey&&!e.altKey)", WEB_VIEWER_HTML)
        self.assertIn(
            "function resetView(){if(!state.nodes.length)return;"
            "state.userMoved=false;flyToOrigin(Math.max(graphRadius(),40))}",
            WEB_VIEWER_HTML,
        )
        # The continuous sim has no "end": the one-time settle refit fires at
        # the FIRST sleep after a build, still suppressed once the user has
        # orbited, zoomed or panned — no camera yank mid-exploration. The
        # fitted/userMoved flags reset with every rebuilt graph. While the
        # load reveal is still assembling the graph, the sim refuses to
        # sleep at all, so the settle refit always waits for reveal
        # completion.
        self.assertIn(
            "function simSleep(s){if(state.reveal&&!state.reveal.done)return;"
            "s.sleeping=true;if(!s.fitted){s.fitted=true;"
            "if(!state.userMoved)flyToOrigin(graphRadius())}}",
            WEB_VIEWER_HTML,
        )
        self.assertIn("state.dragging=null;state.userMoved=false", WEB_VIEWER_HTML)
        # Idle ambience drift and the ring pulse both honor the OS-level
        # prefers-reduced-motion setting.
        self.assertIn(
            "matchMedia('(prefers-reduced-motion: reduce)')", WEB_VIEWER_HTML
        )
        self.assertIn(
            "if(!REDUCED_MOTION&&downMode===null&&now-G.lastInteract>IDLE_DELAY_MS)"
            "G.cam.theta+=IDLE_DRIFT",
            WEB_VIEWER_HTML,
        )

    def test_web_viewer_hover_dimming_eases_instead_of_strobing(self) -> None:
        # Sweeping the mouse used to snap every node's color per hover
        # change (a strobe). Targets are computed once per change;
        # animate() lerps displayed colors toward them and skips the loop
        # entirely once settled.
        self.assertIn("COLOR_EASE=0.3", WEB_VIEWER_HTML)
        self.assertIn("function refreshHighlight(instant){", WEB_VIEWER_HTML)
        self.assertIn("G.colorsAnimating=true", WEB_VIEWER_HTML)
        self.assertIn("if(G.colorsAnimating&&G.atoms){", WEB_VIEWER_HTML)
        self.assertIn("if(settled)G.colorsAnimating=false", WEB_VIEWER_HTML)
        self.assertIn("if(maxd<0.005){arr.set(target);return true}", WEB_VIEWER_HTML)

    def test_web_viewer_sim_is_continuous_with_alpha_temperature_and_sleep(
        self,
    ) -> None:
        # The physics brief: a living force simulation where every node's
        # motion propagates through the network. Forces are scaled by a
        # decaying alpha (d3-style temperature); the sim never "ends", it
        # sleeps, and interactions reheat it. All tuning constants are named.
        self.assertIn(
            "const ALPHA_DECAY=0.995,ALPHA_MIN=0.003,WAKE_ALPHA=0.3,"
            "KE_SLEEP=0.0001,FLING_GAIN=0.5,FLING_MAX=6,DRAG_VEL_STALE_MS=90;",
            WEB_VIEWER_HTML,
        )
        # Rebuild = full reheat; the repulsion sample set is refreshed here
        # and only here.
        self.assertIn("state.sim={vel,sample,alpha:1,sleeping:false,fitted:false", WEB_VIEWER_HTML)
        self.assertIn(
            "function wakeSim(a){let s=state.sim;if(!s)return;"
            "if(a>s.alpha)s.alpha=a;s.sleeping=false}",
            WEB_VIEWER_HTML,
        )
        # Forces are alpha-scaled (all three: gravity, springs, repulsion).
        self.assertIn("*grav*a", WEB_VIEWER_HTML)
        self.assertIn("f=k*(d-rest)/d*a", WEB_VIEWER_HTML)
        self.assertIn("f=rep/d2*a", WEB_VIEWER_HTML)
        self.assertIn("s.alpha*=ALPHA_DECAY", WEB_VIEWER_HTML)
        # Kinetic sleep: alpha-cold OR kinetically still, and a sleeping sim
        # costs nothing — animate() gates stepping, and matrices are only
        # rewritten on frames that actually stepped.
        self.assertIn("if(s.alpha<ALPHA_MIN){simSleep(s);break}", WEB_VIEWER_HTML)
        self.assertIn(
            "if(drag==null&&(!n||ke/n<KE_SLEEP)){simSleep(s);break}",
            WEB_VIEWER_HTML,
        )
        self.assertIn(
            "if(state.sim&&!state.sim.sleeping)stepSim(state.sim);",
            WEB_VIEWER_HTML,
        )
        self.assertIn(
            "if(stepped){updateAtomMatrices();updateBondTransforms()}",
            WEB_VIEWER_HTML,
        )
        # Drag is a kinematic constraint, not a sim kill: nothing writes
        # state.sim=null anymore; the dragged node is pinned (velocity
        # zeroed, position owned by the pointer) while springs pull its
        # neighborhood, and alpha is sustained for the whole drag.
        self.assertNotIn("state.sim=null", WEB_VIEWER_HTML)
        self.assertNotIn("s.iter", WEB_VIEWER_HTML)
        self.assertNotIn("total:160", WEB_VIEWER_HTML)
        self.assertIn(
            "if(drag!=null&&!REDUCED_MOTION&&s.alpha<WAKE_ALPHA)s.alpha=WAKE_ALPHA",
            WEB_VIEWER_HTML,
        )
        self.assertIn(
            "if(i===drag){vel[i*3]=0;vel[i*3+1]=0;vel[i*3+2]=0;continue}",
            WEB_VIEWER_HTML,
        )
        # Release gives the node a fling from smoothed pointer velocity —
        # unless the pointer paused before letting go — then the network
        # resettles from WAKE_ALPHA.
        self.assertIn(
            "if(performance.now()-s.dragT<=DRAG_VEL_STALE_MS){"
            "s.vel[idx*3]=clamp(s.dragVX*FLING_GAIN,-FLING_MAX,FLING_MAX);",
            WEB_VIEWER_HTML,
        )
        # Reduced motion: the initial layout still settles (alpha:1 at
        # build), but drags never reheat — propagation is frozen and the
        # drag falls back to moving only the grabbed node, with matrices
        # updated directly since the sim stays asleep.
        self.assertIn("if(!REDUCED_MOTION)wakeSim(WAKE_ALPHA);", WEB_VIEWER_HTML)
        self.assertIn(
            "if(!state.sim||state.sim.sleeping){updateAtomMatrices();"
            "updateBondTransforms()}",
            WEB_VIEWER_HTML,
        )
        self.assertIn("if(s&&wasMoved>=5&&!REDUCED_MOTION){", WEB_VIEWER_HTML)

    def test_web_viewer_display_controls_size_opacity_color_mode(self) -> None:
        # A "Display" toolbar lives beside, not inside, #graphTools — it
        # must not join the querySelectorAll('#graphTools button') wiring
        # that switches graph sources by data-source.
        self.assertIn(
            '<div class="graph-tools" id="displayTools">'
            '<button type="button" id="resetViewBtn" title="Reset view (0)">⌂</button>'
            '<button type="button" id="displayBtn">Display</button></div>',
            WEB_VIEWER_HTML,
        )
        self.assertIn("#displayTools{left:auto;right:16px;transform:none}", WEB_VIEWER_HTML)
        self.assertIn('id="nodeSizeRange"', WEB_VIEWER_HTML)
        self.assertIn('id="edgeOpacityRange"', WEB_VIEWER_HTML)
        self.assertIn('id="colorModeMulti"', WEB_VIEWER_HTML)
        self.assertIn('id="colorModeMono"', WEB_VIEWER_HTML)
        self.assertIn('<input type="color" id="monoColorPicker"', WEB_VIEWER_HTML)
        # Node size and edge opacity apply live (no reload) to the graph
        # already on screen: the size slider is now the base atom radius, so
        # it rebuilds the instance matrices (atoms AND bonds — bond radius
        # derives from it); opacity drives the bond material directly.
        self.assertIn(
            "display.nodeSize=Number(nodeSizeRange.value);"
            "if(G.atoms){updateAtomMatrices();updateBondTransforms()}",
            WEB_VIEWER_HTML,
        )
        self.assertIn(
            "if(G.bonds)G.bonds.material.opacity=display.edgeOpacity;",
            WEB_VIEWER_HTML,
        )
        # The materials must read the persisted/live prefs, never the old
        # hardcoded 9 / 0.6.
        self.assertIn(
            "function atomBaseRadius(){return display.nodeSize*0.38}",
            WEB_VIEWER_HTML,
        )
        self.assertIn("base=atomBaseRadius()", WEB_VIEWER_HTML)
        self.assertIn(
            "new THREE.MeshPhongMaterial({shininess:30,transparent:true,"
            "opacity:display.edgeOpacity})",
            WEB_VIEWER_HTML,
        )
        self.assertIn(
            "new THREE.LineBasicMaterial({vertexColors:true,transparent:true,"
            "opacity:display.edgeOpacity})",
            WEB_VIEWER_HTML,
        )
        self.assertNotIn("PointsMaterial({size:9,", WEB_VIEWER_HTML)
        self.assertNotIn(
            "LineBasicMaterial({vertexColors:true,transparent:true,opacity:0.6})",
            WEB_VIEWER_HTML,
        )
        # Color mode is wired into colorFor()/refreshHighlight()'s buffer,
        # so switching modes recolors the graph already on screen.
        self.assertIn(
            "if(display.colorMode==='mono')return hexToRgb01(display.monoColor);",
            WEB_VIEWER_HTML,
        )
        self.assertIn("if(state.nodes.length)refreshHighlight()", WEB_VIEWER_HTML)
        # linewidth is a documented no-op on the vendored core three.js UMD
        # build (no Line2/LineMaterial/LineSegments2 addon) — opacity is the
        # only real knob for edge prominence, so no linewidth slider exists.
        self.assertNotIn("linewidth", WEB_VIEWER_HTML)
        # Persisted the same way the access token already is.
        self.assertIn(
            "localStorage.getItem('brainskit-display')", WEB_VIEWER_HTML
        )
        self.assertIn(
            "localStorage.setItem('brainskit-display',JSON.stringify(display))",
            WEB_VIEWER_HTML,
        )
        self.assertIn("let display=loadDisplayPrefs();", WEB_VIEWER_HTML)

    def test_web_viewer_paste_listener_checks_active_element_before_acting(
        self,
    ) -> None:
        # Pasting into the search box, the capture modal's own fields, or the
        # ask textarea must be left completely alone.
        self.assertIn(
            "addEventListener('paste',e=>{let active=document.activeElement,"
            "tag=active&&active.tagName;if(tag==='INPUT'||tag==='TEXTAREA'||"
            "(active&&active.isContentEditable))return;",
            WEB_VIEWER_HTML,
        )
        self.assertIn(
            "document.getElementById('captureText').value=text;"
            "openModal('captureModal');",
            WEB_VIEWER_HTML,
        )
        # Existing drag-and-drop handler must still be present, unmodified in
        # spirit — paste is additive, not a replacement.
        self.assertIn("addEventListener('drop',async e=>", WEB_VIEWER_HTML)

    def test_web_viewer_feed_excerpt_stacks_blocks_and_fades(self) -> None:
        # The bug report (screenshot): `.feed-excerpt` combined the
        # -webkit-box/line-clamp pair with `.md-content`, whose 180px
        # min-height and block children (h1/p with margins) it inherits —
        # multi-paragraph markdown painted its lines on top of each other.
        # Blocks must stack normally, truncated by max-height with a bottom
        # fade, and the md-content min-height floor must not apply inside
        # the card.
        self.assertIn(
            ".feed-excerpt{overflow:hidden;max-height:120px;min-height:0;"
            "-webkit-mask-image:linear-gradient(#000 60%,transparent);"
            "mask-image:linear-gradient(#000 60%,transparent)}",
            WEB_VIEWER_HTML,
        )
        # TWINS: no line-clamp box layout survives anywhere in the page.
        self.assertNotIn("-webkit-line-clamp", WEB_VIEWER_HTML)
        self.assertNotIn("display:-webkit-box", WEB_VIEWER_HTML)

    def test_web_viewer_ask_is_a_full_chat_view_not_a_modal(self) -> None:
        # Ask is a main-area view — a sibling of #graphView/#collection using
        # the same hidden-toggle pattern switchView uses — not a modal whose
        # one-shot answer lands in the narrow Inspector.
        for element_id in (
            'id="chatView"',
            'id="chatThread"',
            'id="chatInput"',
            'id="chatSend"',
            'id="chatClear"',
            'id="askSave"',
        ):
            self.assertIn(element_id, WEB_VIEWER_HTML)
        self.assertNotIn("askModal", WEB_VIEWER_HTML)
        self.assertNotIn("askGo", WEB_VIEWER_HTML)
        self.assertNotIn("askQuestion", WEB_VIEWER_HTML)
        # `.chat{display:flex}` is declared AFTER the shared `.hidden` rule,
        # so at equal specificity it would win and paint the chat over every
        # other view; the two-class rule restores the toggle.
        self.assertIn(".chat.hidden{display:none}", WEB_VIEWER_HTML)
        # The header Ask button opens the chat view and carries an active
        # state; the nav view buttons switch away from it normally.
        self.assertIn(
            "document.getElementById('askBtn').onclick=openChat", WEB_VIEWER_HTML
        )
        self.assertIn(
            "document.getElementById('chatView').classList.remove('hidden');"
            "document.getElementById('askBtn').classList.add('active')",
            WEB_VIEWER_HTML,
        )
        self.assertIn(
            "document.getElementById('chatView').classList.add('hidden');"
            "document.getElementById('askBtn').classList.remove('active')",
            WEB_VIEWER_HTML,
        )
        # Enter sends; Shift+Enter stays a newline.
        self.assertIn(
            "if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();sendChat()}",
            WEB_VIEWER_HTML,
        )
        # Honest signpost for the new contract: answers cite vault evidence,
        # and the conversation rides along as follow-up context. The request
        # body carries the structured `history` field — never a question with
        # history concatenated into it, which would pollute BM25 retrieval.
        self.assertIn(
            "Answers cite evidence from the vault; the conversation is "
            "context for follow-ups.",
            WEB_VIEWER_HTML,
        )
        self.assertIn(
            "body:JSON.stringify({question:q,save,history})", WEB_VIEWER_HTML
        )
        # Errors join the thread as a quiet bubble, not a toast.
        self.assertIn(
            "pushChatTurn({role:'error',text:error.message})", WEB_VIEWER_HTML
        )
        self.assertIn(".chat-error{", WEB_VIEWER_HTML)

    def test_web_viewer_chat_thread_persists_capped_and_restores(self) -> None:
        self.assertIn(
            "const CHAT_STORAGE_KEY='brainskit-ask-thread',CHAT_TURN_CAP=50",
            WEB_VIEWER_HTML,
        )
        self.assertIn("localStorage.getItem(CHAT_STORAGE_KEY)", WEB_VIEWER_HTML)
        self.assertIn(
            "localStorage.setItem(CHAT_STORAGE_KEY,JSON.stringify("
            "state.chatThread.slice(-CHAT_TURN_CAP)))",
            WEB_VIEWER_HTML,
        )
        # Restored on boot (script top level), not only on first open.
        self.assertIn("state.chatThread=loadChatThread()", WEB_VIEWER_HTML)
        # Clear drops both the in-memory thread and the stored copy.
        self.assertIn(
            "document.getElementById('chatClear').onclick=()=>{"
            "state.chatThread=[];localStorage.removeItem(CHAT_STORAGE_KEY);"
            "renderChat()}",
            WEB_VIEWER_HTML,
        )
        # Empty state: an invitation plus static example chips that fill the
        # composer without sending.
        self.assertIn(
            "Ask the compiled brain — answers cite the evidence they came from.",
            WEB_VIEWER_HTML,
        )
        self.assertIn(
            "chip.onclick=()=>{let input=document.getElementById('chatInput');"
            "input.value=example;growComposer();input.focus()}",
            WEB_VIEWER_HTML,
        )
        self.assertNotIn("input.value=example;sendChat", WEB_VIEWER_HTML)
        # The in-flight indicator's animation sits behind the reduced-motion
        # media gate; a reduce-motion OS preference gets steady dots.
        self.assertIn(
            "@media(prefers-reduced-motion:no-preference){.chat-thinking i{"
            "animation:",
            WEB_VIEWER_HTML,
        )

    def test_web_viewer_chat_sends_bounded_history_of_completed_exchanges(
        self,
    ) -> None:
        # The client-side bound mirrors the server's MAX_HISTORY_EXCHANGES so
        # payloads stay small instead of leaning on server-side truncation.
        self.assertIn(
            f"CHAT_TURN_CAP=50,CHAT_HISTORY_MAX={MAX_HISTORY_EXCHANGES};",
            WEB_VIEWER_HTML,
        )
        # Only completed exchanges become history: a user turn pairs with the
        # answer that followed it, and an error clears the pending question —
        # a question that failed has no answer to quote.
        self.assertIn(
            "function chatHistory(){let out=[],question=null;"
            "for(let turn of state.chatThread){"
            "if(turn.role==='user'){question=turn.text}"
            "else if(turn.role==='answer'&&question!=null){"
            "out.push({question,answer:turn.answer});question=null}"
            "else if(turn.role==='error'){question=null}}"
            "return out.slice(-CHAT_HISTORY_MAX)}",
            WEB_VIEWER_HTML,
        )
        # History is snapshotted BEFORE the new user turn joins the thread,
        # so the question being asked can never pair with a stale answer.
        send = WEB_VIEWER_HTML[
            WEB_VIEWER_HTML.index("async function sendChat()"):
        ]
        send = send[: send.index("}}") + 2]
        self.assertLess(
            send.index("let history=chatHistory()"),
            send.index("pushChatTurn({role:'user',text:q})"),
        )

    def test_web_viewer_chat_answer_names_the_answering_model(self) -> None:
        # The stored turn persists provider/model with the answer, so a
        # restored thread keeps its chips.
        self.assertIn(
            "provider:r.provider?String(r.provider):'',"
            "model:r.model?String(r.model):''",
            WEB_VIEWER_HTML,
        )
        # The chip renders `provider · model` in the muted meta row — and only
        # when both are present, so turns stored before the field existed
        # simply omit it instead of rendering a half-empty chip.
        self.assertIn(
            "if(r.provider&&r.model){let mc=document.createElement('span');"
            "mc.className='chat-model';escText(mc,r.provider+' · '+r.model);"
            "meta.append(mc)}",
            WEB_VIEWER_HTML,
        )

    def test_web_viewer_graph_reveals_connections_on_load(self) -> None:
        # Load-in choreography: atoms pop in BFS order from the biggest hub,
        # bonds draw on from their midpoints once both endpoints have begun —
        # the graph visibly assembles through its connections. All timing
        # constants are named.
        self.assertIn(
            "const REVEAL_SPAN_MIN=1200,REVEAL_SPAN_MAX=2500,"
            "REVEAL_ATOM_MS=300,REVEAL_BOND_MS=250;",
            WEB_VIEWER_HTML,
        )
        # BFS from index 0 outward; nodes are degree-sorted at build, so index
        # order IS hub order and every disconnected component joins hubs-first.
        self.assertIn(
            "function computeRevealDelays(){let n=state.nodes.length,"
            "order=new Int32Array(n).fill(-1)",
            WEB_VIEWER_HTML,
        )
        self.assertIn("delays[i]=order[i]/Math.max(1,n)", WEB_VIEWER_HTML)
        # Re-armed on EVERY build (it is the load animation, not an intro) and
        # skipped outright under reduced motion — instant final state.
        self.assertIn(
            "state.reveal=(REDUCED_MOTION||!n)?null:{start:performance.now(),"
            "span:clamp(REVEAL_SPAN_MIN+n/NODES_LIMIT*"
            "(REVEAL_SPAN_MAX-REVEAL_SPAN_MIN),"
            "REVEAL_SPAN_MIN,REVEAL_SPAN_MAX),done:false};",
            WEB_VIEWER_HTML,
        )
        # Cubic ease-out scale-pop, applied inside the same matrix rebuild the
        # sim already owns — no second writer, no per-frame allocations.
        self.assertIn(
            "function revealEase(t){t=clamp(t,0,1);let u=1-t;return 1-u*u*u}",
            WEB_VIEWER_HTML,
        )
        self.assertIn(
            "if(rv)r*=Math.max(revealAtomScale(i,rv),0.001);", WEB_VIEWER_HTML
        )
        # Bonds gate on BOTH endpoints having begun, then grow from the
        # midpoint outward along the cylinder axis; the LineSegments fallback
        # fades edge color in instead.
        self.assertIn(
            "Math.max(state.revealDelay[a],state.revealDelay[b])",
            WEB_VIEWER_HTML,
        )
        self.assertIn(
            "ax=mx+(ax-mx)*g;ay=my+(ay-my)*g;az=mz+(az-mz)*g", WEB_VIEWER_HTML
        )
        self.assertIn(
            "if(G.bondMode==='line'&&G.bonds){let arr=bondColorArray()",
            WEB_VIEWER_HTML,
        )
        self.assertIn("arr[i*6+c]=bt[i*6+c]*g", WEB_VIEWER_HTML)
        # The reveal rides the existing animate() loop off a timestamp —
        # never a timer.
        self.assertIn(
            "if(state.reveal&&!state.reveal.done)stepReveal(now);",
            WEB_VIEWER_HTML,
        )
        self.assertNotIn("setInterval", WEB_VIEWER_HTML)
        # Any pointer interaction on the canvas snaps the reveal complete —
        # user intent beats choreography. The snap runs before pointer capture
        # (so a capture failure cannot swallow it) and before pick() (so the
        # press lands on full-size atoms).
        self.assertIn(
            "if(!G.renderer)return;finishReveal();"
            "canvas3d.setPointerCapture(e.pointerId);",
            WEB_VIEWER_HTML,
        )
        self.assertIn("e.preventDefault();finishReveal();", WEB_VIEWER_HTML)
        self.assertIn(
            "function finishReveal(){let rv=state.reveal;if(!rv||rv.done)return;"
            "rv.done=true;updateAtomMatrices();updateBondTransforms();"
            "if(G.bondMode==='line')refreshHighlight(true)}",
            WEB_VIEWER_HTML,
        )

    def test_web_viewer_atoms_have_rim_light_and_hub_halos(self) -> None:
        # Fresnel rim: injected into the Phong fragment via onBeforeCompile
        # (the vendored three has no postprocessing), scaled by the lit
        # color's luminance so hover-dimmed atoms stay quiet, and placed
        # before tonemapping/fog so distance still attenuates it.
        self.assertIn("const RIM_INTENSITY=0.35,RIM_POWER=2.5", WEB_VIEWER_HTML)
        self.assertIn("amat.onBeforeCompile=shader=>", WEB_VIEWER_HTML)
        self.assertIn("'#include <output_fragment>'", WEB_VIEWER_HTML)
        self.assertIn(
            "float rimLum=dot(gl_FragColor.rgb,vec3(0.299,0.587,0.114));",
            WEB_VIEWER_HTML,
        )
        self.assertIn(
            "dot(normalize(vViewPosition),normalize(normal))", WEB_VIEWER_HTML
        )
        # Hub halos: one additive glow sprite per top-degree atom, positions
        # synced where bond matrices sync, kept under reduced motion (they
        # are static, not motion).
        self.assertIn(
            "HALO_COUNT=8,HALO_SCALE=2.2,HALO_OPACITY=0.35", WEB_VIEWER_HTML
        )
        self.assertIn(
            "for(let i=0;i<Math.min(HALO_COUNT,n);i++)", WEB_VIEWER_HTML
        )
        self.assertIn(
            "opacity:HALO_OPACITY,blending:THREE.AdditiveBlending,"
            "depthWrite:false",
            WEB_VIEWER_HTML,
        )
        # Both bond paths (split cylinders AND the line fallback's early
        # return) sync halo positions.
        self.assertIn(
            "G.bonds.geometry.attributes.position.needsUpdate=true;"
            "syncHalos();return}",
            WEB_VIEWER_HTML,
        )
        self.assertIn(
            "G.bonds.instanceMatrix.needsUpdate=true;syncHalos()}",
            WEB_VIEWER_HTML,
        )

    def test_web_viewer_services_cards_are_truthful_and_filters_partition(
        self,
    ) -> None:
        # The `web` integration card used to read "disabled" while serving
        # the very tab displaying it. It now reads live, and the other cards
        # stop duplicating state between the badge and the detail line.
        self.assertIn(
            "if(name==='integrations'&&item.name==='web'){"
            "badge.className='badge good';badgeText='live';"
            "details='serving this tab · persistent service '+item.state"
            "+' · '+(item.managed?'managed':'external')}",
            WEB_VIEWER_HTML,
        )
        # Default integrations detail line: managed/external only — state
        # lives in the badge alone.
        self.assertIn(":(item.managed?'managed':'external');", WEB_VIEWER_HTML)
        self.assertNotIn(":item.state+' · '", WEB_VIEWER_HTML)
        self.assertIn("escText(badge,badgeText)", WEB_VIEWER_HTML)
        # A filter group with fewer than 2 distinct values cannot partition
        # the collection — it is noise and is omitted, for every collection.
        self.assertIn(
            "let values=Object.keys(counts).sort();if(values.length<2)return;",
            WEB_VIEWER_HTML,
        )
        self.assertNotIn("if(!values.length)return;", WEB_VIEWER_HTML)

    def test_network_mcp_requires_auth_origin_and_protocol_version(self) -> None:
        server = BrainskitMcpHttpServer(("127.0.0.1", 0), BrainskitMcpHttpHandler)
        server.service = self.service
        server.token = "test-secret"
        base_url = f"http://127.0.0.1:{server.server_port}"
        server.allowed_origins = {base_url}
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        def request(
            payload: dict,
            *,
            authenticated: bool = True,
            origin: str | None = None,
            protocol: bool = False,
        ):
            headers = {
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
            }
            if authenticated:
                headers["Authorization"] = "Bearer test-secret"
            if origin:
                headers["Origin"] = origin
            if protocol:
                headers["MCP-Protocol-Version"] = MCP_PROTOCOL_VERSION
            return urlopen(
                Request(
                    f"{base_url}/mcp",
                    data=json.dumps(payload).encode(),
                    headers=headers,
                    method="POST",
                ),
                timeout=2,
            )

        initialize = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": MCP_PROTOCOL_VERSION},
        }
        try:
            with self.assertRaises(HTTPError) as unauthorized:
                request(initialize, authenticated=False)
            self.assertEqual(unauthorized.exception.code, 401)
            with self.assertRaises(HTTPError) as unauthenticated_delete:
                urlopen(
                    Request(f"{base_url}/mcp", method="DELETE"),
                    timeout=2,
                )
            self.assertEqual(unauthenticated_delete.exception.code, 401)
            with self.assertRaises(HTTPError) as forbidden:
                request(initialize, origin="https://attacker.invalid")
            self.assertEqual(forbidden.exception.code, 403)
            with request(initialize, origin=base_url) as response:
                initialized = json.loads(response.read())
            self.assertEqual(
                initialized["result"]["protocolVersion"], MCP_PROTOCOL_VERSION
            )
            with self.assertRaises(HTTPError) as missing_version:
                request({"jsonrpc": "2.0", "id": 2, "method": "ping"})
            self.assertEqual(missing_version.exception.code, 400)
            with missing_version.exception as unsupported_version:
                self.assertEqual(
                    json.loads(unsupported_version.read())["error"]["code"], -32600
                )
            with request(
                {"jsonrpc": "2.0", "id": 3, "method": "ping"},
                protocol=True,
            ) as response:
                pong = json.loads(response.read())
            self.assertEqual(pong["result"], {})
            with request(
                {
                    "jsonrpc": "2.0",
                    "method": "notifications/initialized",
                },
                protocol=True,
            ) as response:
                self.assertEqual(response.status, 202)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_apply_unit_of_work_rolls_back_and_commits_all_surfaces(self) -> None:
        captured = self.service.capture(
            None,
            text="Atomic source evidence for the durable transaction.",
            title="Atomic source",
        )
        content_hash = captured["source"]["content_hash"]
        original_path = captured["source"]["path"]
        page_path = "wiki/concepts/durable-transaction.md"
        proposal = {
            "proposal_id": "uow-rollback-test",
            "operations": [
                {
                    "action": "upsert",
                    "kind": "concept",
                    "slug": "durable-transaction",
                    "title": "Durable transaction",
                    "aliases": [],
                    "source_hashes": [content_hash],
                    "body": (
                        "Wiki, freshness, raw and index commit together."
                        f"[^source:{content_hash}]"
                    ),
                    "links": [],
                }
            ],
        }
        apply_snapshot = self.service.index.apply_snapshot

        def fail_index(*args, **kwargs):
            raise RuntimeError("injected index failure")

        self.service.index.apply_snapshot = fail_index  # type: ignore[method-assign]
        try:
            with self.assertRaisesRegex(RuntimeError, "injected index failure"):
                self.service.gate.commit(
                    proposal, raw_move=(content_hash, "20-research")
                )
        finally:
            self.service.index.apply_snapshot = apply_snapshot  # type: ignore[method-assign]

        self.assertFalse((self.root / page_path).exists())
        self.assertEqual(self.vault.registry()[content_hash].path, original_path)
        self.assertTrue((self.root / original_path).is_file())
        self.assertNotIn(page_path, self.vault.read_state("freshness")["pages"])
        self.assertFalse(
            any(hit.kind == "wiki" for hit in self.service.index.search("durable"))
        )

        result = self.service.gate.commit(
            proposal, raw_move=(content_hash, "20-research")
        )
        record = self.vault.registry()[content_hash]
        self.assertTrue((self.root / page_path).is_file())
        self.assertTrue(record.path.startswith("raw/20-research/"))
        self.assertFalse((self.root / original_path).exists())
        self.assertEqual(record.status, "ingested")
        self.assertEqual(
            self.vault.read_state("freshness")["pages"][page_path]["status"],
            "fresh",
        )
        self.assertIn(
            page_path,
            {hit.path for hit in self.service.index.search("durable")},
        )
        self.assertEqual(result["raw_move"]["to"], record.path)

    def test_lint_detects_wiki_edit_outside_apply_gate(self) -> None:
        captured = self.service.capture(None, text="Evidence", title="Evidence")
        content_hash = captured["source"]["content_hash"]
        self.service.apply(
            {
                "operations": [
                    {
                        "action": "upsert",
                        "kind": "concept",
                        "slug": "gate-owned",
                        "title": "Gate owned",
                        "aliases": [],
                        "source_hashes": [content_hash],
                        "body": f"Original.[^source:{content_hash}]",
                        "links": [],
                    }
                ]
            }
        )
        page = self.root / "wiki/concepts/gate-owned.md"
        page.write_text(
            page.read_text(encoding="utf-8").replace("Original.", "Manual edit."),
            encoding="utf-8",
        )
        result = self.service.lint()
        self.assertFalse(result["ok"])
        self.assertIn(
            "wiki.outside_apply",
            {finding["code"] for finding in result["findings"]},
        )

    def test_alias_dedup_and_freshness_state(self) -> None:
        first = self.service.capture(None, text="First evidence", title="First")
        first_hash = first["source"]["content_hash"]
        self.service.apply(
            {
                "operations": [
                    {
                        "action": "upsert",
                        "kind": "concept",
                        "slug": "compiled-memory",
                        "title": "Compiled memory",
                        "aliases": ["Memória compilada"],
                        "source_hashes": [first_hash],
                        "body": f"First durable claim.[^source:{first_hash}]",
                        "links": [],
                    }
                ]
            }
        )
        page_path = "wiki/concepts/compiled-memory.md"
        freshness = self.vault.read_state("freshness")
        self.assertEqual(freshness["pages"][page_path]["status"], "fresh")
        second = self.service.capture(None, text="Second evidence", title="Second")
        second_hash = second["source"]["content_hash"]
        with self.assertRaises(ValidationError):
            self.service.apply(
                {
                    "operations": [
                        {
                            "action": "upsert",
                            "kind": "concept",
                            "slug": "memoria-compilada",
                            "title": "Memória compilada",
                            "aliases": ["compiled memory"],
                            "source_hashes": [second_hash],
                            "body": f"Different claim.[^source:{second_hash}]",
                            "links": [],
                        }
                    ]
                }
            )

        def age_state(state):
            state["pages"][page_path]["updated_at"] = (
                datetime.now(UTC) - timedelta(days=45)
            ).isoformat()
            return state

        self.vault.mutate_state("freshness", age_state)
        status = self.service.status()
        self.assertEqual(status["freshness"]["stale"], 1)

    def test_approve_each_filing_and_structured_repair(self) -> None:
        captured = self.service.capture(
            None, text="Approval evidence", title="Approval"
        )
        content_hash = captured["source"]["content_hash"]
        valid_ingest = {
            "operations": [
                {
                    "action": "upsert",
                    "kind": "concept",
                    "slug": "approval-flow",
                    "title": "Approval flow",
                    "aliases": [],
                    "source_hashes": [content_hash],
                    "body": f"Approval is explicit.[^source:{content_hash}]",
                    "links": [],
                    "base_hash": None,
                }
            ]
        }
        invalid_domain = json.loads(json.dumps(valid_ingest))
        invalid_domain["operations"][0]["body"] = "Missing its citation."
        fake = FakeJudgment(
            {
                "ingest": ["not-json", invalid_domain, valid_ingest],
            }
        )
        service = BrainskitService(
            self.vault,
            SqliteFtsIndex(self.vault.index_path),
            judgment=fake,
            jobs=JobSpecs(),
            graph=MarkdownGraph(),
        )
        result = service.ingest(content_hash, target_branch="10-work")
        proposal = result["results"][0]["proposal"]
        self.assertTrue(result["results"][0]["queued"])
        self.assertEqual(fake.calls, ["ingest", "ingest", "ingest"])
        self.assertFalse((self.root / "wiki/concepts/approval-flow.md").exists())
        approved = service.approve(proposal["proposal_id"])
        self.assertEqual(approved["proposal"]["status"], "applied")
        self.assertTrue((self.root / "wiki/concepts/approval-flow.md").exists())
        record = self.vault.registry()[content_hash]
        self.assertTrue(record.path.startswith("raw/10-work/"))
        self.assertEqual(record.status, "ingested")


class RetrievalContextApplyContractScopeTest(unittest.TestCase):
    """`apply_contract` belongs to a caller about to write a proposal, not to
    a job that only ever reads. See `Retrieval.context`'s
    `include_apply_contract`.

    The bug this guards: `ask`, `resurface` and semantic `lint` all built
    their evidence with `Retrieval.context()`'s *default* bundle, which
    always carried the `[^source:<sha256>]` / `upsert` / `source_hashes`
    write-proposal schema alongside the real evidence -- confirmed live
    against a 20k-source vault, where a question its actual evidence did
    not obviously answer got answered from that schema instead: a
    description of brainskit's own `raw/`/`wiki/`/`bk apply` mechanics, not
    of the vault's content.
    """

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.vault = FileVault.initialize(self.root, policy())
        self.index = SqliteFtsIndex(self.vault.index_path)
        seed = BrainskitService(self.vault, self.index, graph=MarkdownGraph())
        seed.capture(None, text="Evidence about the vault.", title="Evidence")
        seed.reindex()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _service(self, fake: FakeJudgment) -> BrainskitService:
        return BrainskitService(
            self.vault,
            self.index,
            judgment=fake,
            jobs=JobSpecs(),
            graph=MarkdownGraph(),
        )

    def test_bk_context_keeps_the_apply_contract_by_default(self) -> None:
        # The agent-facing tool (`bk context` / MCP `context`) that composes
        # a `bk apply` proposal from this bundle must not lose it.
        service = BrainskitService(self.vault, self.index, graph=MarkdownGraph())
        bundle = service.context("evidence")
        self.assertIn("apply_contract", bundle)

    def test_ask_never_sees_the_apply_contract(self) -> None:
        fake = FakeJudgment(
            {"query": [{"answer": "x", "citations": [], "uncertainty": ""}]}
        )
        service = self._service(fake)
        service.ask("what is this vault about")
        context = json.loads(fake.variables[0]["context"])
        self.assertNotIn("apply_contract", context)
        self.assertIn("evidence", context)

    def test_resurface_never_sees_the_apply_contract(self) -> None:
        fake = FakeJudgment(
            {
                "resurface": [
                    {"markdown": "x", "page": "wiki/index.md", "question": "q"}
                ]
            }
        )
        service = self._service(fake)
        service.jobs_runner.resurface()
        context = json.loads(fake.variables[0]["context"])
        self.assertNotIn("apply_contract", context)

    def test_semantic_lint_never_sees_the_apply_contract(self) -> None:
        fake = FakeJudgment({"lint-semantic": [{"findings": []}]})
        service = self._service(fake)
        service.lint(semantic=True)
        context = json.loads(fake.variables[0]["context"])
        self.assertNotIn("apply_contract", context)


class _SpyRetrieval:
    """Records every retrieval query while delegating to the real retrieval."""

    def __init__(self, inner) -> None:
        self.inner = inner
        self.queries: list[str] = []

    def context(self, query: str, **kwargs) -> dict:
        self.queries.append(query)
        return self.inner.context(query, **kwargs)


class AskConversationHistoryTest(unittest.TestCase):
    """`ask` carries the conversation to the model, never to retrieval.

    History exists so a follow-up like "e sobre o code graph?" can resolve its
    pronouns; it must not leak into the BM25 query (which would bury the
    current question's terms under every word already discussed) and it must
    ride the query prompt bounded, oldest-trimmed-first.
    """

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.vault = FileVault.initialize(self.root, policy())
        self.index = SqliteFtsIndex(self.vault.index_path)
        seed = BrainskitService(self.vault, self.index, graph=MarkdownGraph())
        seed.capture(None, text="Evidence about the code graph.", title="Evidence")
        seed.reindex()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _service(self, fake: FakeJudgment) -> BrainskitService:
        return BrainskitService(
            self.vault,
            self.index,
            judgment=fake,
            jobs=JobSpecs(),
            graph=MarkdownGraph(),
        )

    def _answer(self) -> dict:
        return {"answer": "x", "citations": [], "uncertainty": ""}

    def test_history_keeps_only_the_last_six_exchanges(self) -> None:
        fake = FakeJudgment({"query": [self._answer()]})
        service = self._service(fake)
        service.ask(
            "e sobre o code graph?",
            history=[
                {"question": f"q{n}", "answer": f"a{n}"} for n in range(1, 8)
            ],
        )
        serialized = fake.variables[0]["history"]
        self.assertNotIn("q1", serialized)
        self.assertIn("Q: q2", serialized)
        self.assertIn("Q: q7", serialized)
        self.assertEqual(serialized.count("Q: "), MAX_HISTORY_EXCHANGES)
        # Oldest first: the transcript reads in conversation order.
        self.assertLess(serialized.index("q2"), serialized.index("q7"))

    def test_history_char_budget_trims_oldest_first(self) -> None:
        fake = FakeJudgment({"query": [self._answer()]})
        service = self._service(fake)
        service.ask(
            "e sobre o code graph?",
            history=[
                {"question": f"q{n}", "answer": "a" * 2_500} for n in range(1, 4)
            ],
        )
        serialized = fake.variables[0]["history"]
        self.assertLessEqual(len(serialized), MAX_HISTORY_CHARS)
        self.assertNotIn("q1", serialized)
        self.assertNotIn("q2", serialized)
        self.assertIn("Q: q3", serialized)

    def test_a_single_oversized_exchange_is_truncated_not_dropped(self) -> None:
        fake = FakeJudgment({"query": [self._answer()]})
        service = self._service(fake)
        service.ask(
            "e sobre o code graph?",
            history=[{"question": "q1", "answer": "a" * (MAX_HISTORY_CHARS + 2_000)}],
        )
        serialized = fake.variables[0]["history"]
        self.assertLessEqual(len(serialized), MAX_HISTORY_CHARS)
        self.assertIn("Q: q1", serialized)

    def test_retrieval_is_keyed_on_the_bare_current_question(self) -> None:
        fake = FakeJudgment({"query": [self._answer()]})
        service = self._service(fake)
        spy = _SpyRetrieval(service.jobs_runner.retrieval)
        service.jobs_runner.retrieval = spy
        service.ask(
            "e sobre o code graph?",
            history=[{"question": "o que sabemos?", "answer": "um resumo."}],
        )
        self.assertEqual(spy.queries, ["e sobre o code graph?"])

    def test_prompt_variables_render_through_the_real_template(self) -> None:
        fake = FakeJudgment({"query": [self._answer()]})
        service = self._service(fake)
        service.ask(
            "e sobre o code graph?",
            history=[{"question": "o que sabemos?", "answer": "um resumo."}],
        )
        variables = fake.variables[0]
        self.assertIn("Q: o que sabemos?\nA: um resumo.", variables["history"])
        # The real template must consume every variable `ask` supplies --
        # `wiki_language` is added by the router the fake replaces.
        prompt = JobSpecs().prompt("query", {**variables, "wiki_language": "English"})
        self.assertNotIn("{{", prompt)
        self.assertIn("Conversation so far", prompt)
        self.assertIn("Q: o que sabemos?", prompt)

    def test_ask_without_history_still_renders_the_template(self) -> None:
        fake = FakeJudgment({"query": [self._answer()]})
        service = self._service(fake)
        service.ask("e sobre o code graph?")
        variables = fake.variables[0]
        self.assertEqual(variables["history"], "(none)")
        prompt = JobSpecs().prompt("query", {**variables, "wiki_language": "English"})
        self.assertNotIn("{{", prompt)

    def test_ask_result_names_the_answering_provider_and_model(self) -> None:
        # Read from the same `job_models.query` mapping the router resolves,
        # not hardcoded: the engine policy routes query to ollama/test.
        fake = FakeJudgment({"query": [self._answer()]})
        service = self._service(fake)
        result = service.ask("e sobre o code graph?")
        self.assertEqual(result["provider"], "ollama")
        self.assertEqual(result["model"], "test")

    def test_a_privacy_keyed_mapping_reports_the_routed_model(self) -> None:
        # The router honours `job_models.query` keyed by effective privacy;
        # the reported model must follow the same route it took.
        raw = policy()
        raw["job_models"]["query"] = {
            "local-only": {"provider": "ollama", "model": "local-model"},
            "cloud": {"provider": "ollama", "model": "cloud-model"},
        }
        with tempfile.TemporaryDirectory() as scratch:
            vault = FileVault.initialize(Path(scratch), raw)
            index = SqliteFtsIndex(vault.index_path)
            fake = FakeJudgment({"query": [self._answer()]})
            service = BrainskitService(
                vault, index, judgment=fake, jobs=JobSpecs(), graph=MarkdownGraph()
            )
            service.reindex()
            # No evidence captured: branches fall back to `_inbox`, whose
            # policy in the engine config is local-only.
            result = service.ask("anything")
        self.assertEqual(result["provider"], "ollama")
        self.assertEqual(result["model"], "local-model")


class WebAskHistoryTest(unittest.TestCase):
    """`/api/ask` validates conversation history strictly at the boundary."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.vault = FileVault.initialize(self.root, policy())
        self.fake = FakeJudgment(
            {"query": [{"answer": "resposta", "citations": [], "uncertainty": ""}]}
        )
        self.service = BrainskitService(
            self.vault,
            SqliteFtsIndex(self.vault.index_path),
            judgment=self.fake,
            jobs=JobSpecs(),
            graph=MarkdownGraph(),
        )
        self.service.reindex()
        self.server = build_server(
            self.service,
            host="127.0.0.1",
            port=0,
            consumer="human",
            instance_id="test-instance",
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temporary.cleanup()

    def _spy_ask(self) -> list[dict]:
        """Replace `service.ask` with a recorder, returning the record list."""

        asked: list[dict] = []

        def recorder(question, *, save=False, history=None):
            asked.append({"question": question, "save": save, "history": history})
            return {"question": question, "answer": "ok", "citations": [],
                    "uncertainty": "", "saved_to": None,
                    "provider": "ollama", "model": "test"}

        self.service.ask = recorder
        return asked

    def _post(self, payload: dict) -> dict:
        with urlopen(
            Request(
                f"{self.base_url}/api/ask",
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            ),
            timeout=5,
        ) as response:
            return json.loads(response.read())

    def _reject(self, payload: dict) -> dict:
        with self.assertRaises(HTTPError) as caught:
            self._post(payload)
        self.assertEqual(caught.exception.code, 400)
        with caught.exception as error:
            return json.loads(error.read())["error"]

    def test_valid_history_reaches_the_service(self) -> None:
        asked = self._spy_ask()
        history = [
            {"question": "o que sabemos?", "answer": "um resumo."},
            {"question": "e os hubs?", "answer": "outro resumo."},
        ]
        value = self._post({"question": "e sobre o code graph?", "history": history})
        self.assertTrue(value["ok"])
        self.assertEqual(asked[0]["history"], history)
        self.assertEqual(asked[0]["question"], "e sobre o code graph?")

    def test_absent_history_stays_none(self) -> None:
        asked = self._spy_ask()
        value = self._post({"question": "e sobre o code graph?"})
        self.assertTrue(value["ok"])
        self.assertIsNone(asked[0]["history"])

    def test_history_must_be_a_list(self) -> None:
        error = self._reject({"question": "q", "history": "not-a-list"})
        self.assertEqual(error["code"], "validation_error")
        self.assertEqual(error["details"]["field"], "history")

    def test_history_items_must_be_objects(self) -> None:
        error = self._reject({"question": "q", "history": ["bare string"]})
        self.assertEqual(error["details"]["field"], "history[0]")

    def test_history_question_must_be_a_string(self) -> None:
        error = self._reject(
            {"question": "q", "history": [{"question": 7, "answer": "a"}]}
        )
        self.assertEqual(error["details"]["field"], "history[0].question")

    def test_history_answer_must_be_a_string(self) -> None:
        error = self._reject(
            {"question": "q", "history": [{"question": "x", "answer": None}]}
        )
        self.assertEqual(error["details"]["field"], "history[0].answer")

    def test_excess_history_is_truncated_silently_before_validation(self) -> None:
        # The first two items are malformed AND beyond the bound: truncation
        # drops them before validation, so the request succeeds -- a dropped
        # item never reaches the model, so rejecting over it would fail
        # requests the model would answer identically.
        asked = self._spy_ask()
        history = ["bad", "also bad"] + [
            {"question": f"q{n}", "answer": f"a{n}"}
            for n in range(MAX_HISTORY_EXCHANGES)
        ]
        value = self._post({"question": "q", "history": history})
        self.assertTrue(value["ok"])
        self.assertEqual(len(asked[0]["history"]), MAX_HISTORY_EXCHANGES)
        self.assertEqual(asked[0]["history"][0]["question"], "q0")

    def test_rejected_items_are_named_by_their_index_as_sent(self) -> None:
        # Seven items: the first is truncated away, and the malformed item
        # must be reported at its position in the list the client sent.
        history: list = [{"question": "q0", "answer": "a0"}, "malformed"] + [
            {"question": f"q{n}", "answer": f"a{n}"} for n in range(2, 7)
        ]
        error = self._reject({"question": "q", "history": history})
        self.assertEqual(error["details"]["field"], "history[1]")

    def test_ask_end_to_end_carries_history_provider_and_model(self) -> None:
        value = self._post(
            {
                "question": "e sobre o code graph?",
                "history": [{"question": "o que sabemos?", "answer": "um resumo."}],
            }
        )
        self.assertTrue(value["ok"])
        self.assertEqual(value["result"]["answer"], "resposta")
        self.assertEqual(value["result"]["provider"], "ollama")
        self.assertEqual(value["result"]["model"], "test")
        self.assertIn("Q: o que sabemos?", self.fake.variables[0]["history"])


class PolicyTest(unittest.TestCase):
    def test_full_json_schema_draft_formats_and_local_references(self) -> None:
        schema = {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "type": "object",
            "$defs": {
                "email": {"type": "string", "format": "email"},
            },
            "properties": {
                "kind": {"enum": ["adult", "child"]},
                "age": {"type": "integer", "exclusiveMinimum": 0},
                "email": {"$ref": "#/$defs/email"},
                "guardian": {"type": "string"},
            },
            "required": ["kind", "age", "email"],
            "if": {
                "properties": {"kind": {"const": "child"}},
                "required": ["kind"],
            },
            "then": {"required": ["guardian"]},
            "dependentRequired": {"email": ["age"]},
            "unevaluatedProperties": False,
        }
        self.assertEqual(
            validate_schema(
                {"kind": "adult", "age": 42, "email": "not-an-email"},
                schema,
            )[0]["code"],
            "schema.format",
        )
        self.assertEqual(
            validate_schema(
                {"kind": "adult", "age": 42, "email": "raul@example.com"},
                schema,
            ),
            [],
        )
        failures = validate_schema(
            {
                "kind": "child",
                "age": 0,
                "email": "not-an-email",
                "unexpected": True,
            },
            schema,
        )
        self.assertTrue(
            {
                "schema.exclusiveMinimum",
                "schema.format",
                "schema.required",
                "schema.unevaluatedProperties",
            }.issubset({failure["code"] for failure in failures})
        )

    def test_config_requires_every_policy_area(self) -> None:
        incomplete = policy()
        del incomplete["inbox_policy"]
        with self.assertRaises(ValidationError):
            VaultConfig.from_dict(incomplete)

    def test_a_source_must_be_a_string_path(self) -> None:
        # `{"type": "folder", "path": ...}` is a plausible guess at the schema.
        # `str()` coercion accepted it as the literal path "{'type': ...}",
        # which resolved to a directory that cannot exist and was skipped in
        # silence -- so the policy is refused where every other malformed
        # policy value is.
        raw = policy()
        raw["sources"] = [{"type": "folder", "path": "../inbox"}]
        with self.assertRaises(ValidationError) as caught:
            VaultConfig.from_dict(raw)
        self.assertEqual(caught.exception.details["index"], 0)

    def test_a_source_cannot_be_blank(self) -> None:
        raw = policy()
        raw["sources"] = ["   "]
        with self.assertRaises(ValidationError):
            VaultConfig.from_dict(raw)

    def test_a_relative_source_resolves_against_the_vault(self) -> None:
        # Not against the process's current directory: `bk schedule` emits
        # cron lines, and cron runs from `$HOME`.
        vault = Path("/vaults/brain")
        self.assertEqual(
            resolve_source_path(vault, "../inbox"), Path("/vaults/inbox")
        )
        self.assertEqual(
            resolve_source_path(vault, "./inbox"), Path("/vaults/brain/inbox")
        )

    def test_an_absolute_source_is_returned_unchanged(self) -> None:
        # Unlike `code_root`, which refuses one: watching `~/Downloads` is a
        # documented use, so this path must keep working exactly as it did.
        self.assertEqual(
            resolve_source_path(Path("/vaults/brain"), "/srv/dropbox"),
            Path("/srv/dropbox"),
        )
        self.assertEqual(
            resolve_source_path(Path("/vaults/brain"), "~/Downloads"),
            Path.home().resolve() / "Downloads",
        )

    def test_never_ingest_fails_before_provider_call(self) -> None:
        config = VaultConfig.from_dict(policy())
        router = PolicyJudgmentRouter(config, JobSpecs())
        with self.assertRaises(PolicyError):
            router.run(
                job="query",
                branches=["10-work"],
                variables={"question": "Q", "context": "{}"},
            )

    def test_local_only_evidence_cannot_use_cloud_route(self) -> None:
        raw = policy()
        raw["providers"]["openai"] = {
            "base_url": "https://api.openai.invalid/v1",
            "api_key_env": "UNSET_TEST_KEY",
        }
        raw["job_models"]["query"] = {
            "provider": "openai",
            "model": "test",
        }
        router = PolicyJudgmentRouter(VaultConfig.from_dict(raw), JobSpecs())
        with self.assertRaises(PolicyError):
            router.run(
                job="query",
                branches=["_inbox", "20-research"],
                variables={"question": "Q", "context": "{}"},
            )


class ProposalIdReuseNamesATerminatingRemedyTest(unittest.TestCase):
    """A reused `proposal_id` is not a conflict, and the difference is a loop.

    The shipped contract tells an agent to hold `proposal_id` stable across
    retries. Repairing a rejected body is a retry, and it changes the payload --
    so the reuse check fires, and the code it reports decides whether the agent
    can ever finish. `conflict` means "re-read the vault and send the same
    intent again", which is exactly what the agent already did.

    So the assertions here are about the *remedy*, not the code. Asserting
    `code == "validation_error"` alone would have passed before this bug existed
    and after it was introduced; what discriminates is driving each code's
    stated remedy and seeing which one terminates.
    """

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.vault = FileVault.initialize(self.root, policy())
        self.service = BrainskitService(
            self.vault, SqliteFtsIndex(self.vault.index_path), graph=MarkdownGraph()
        )
        self.hash = self.service.capture(
            None, text="Evidence about widgets.", title="Evidence"
        )["source"]["content_hash"]
        self.page = "wiki/concepts/widget.md"
        self.service.apply(
            {
                "proposal_id": "agent-turn-1",
                "operations": [self.operation(f"A first claim.[^source:{self.hash}]")],
            }
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def operation(self, body: str, base_hash: str | None = None) -> dict:
        operation = {
            "action": "upsert",
            "kind": "concept",
            "slug": "widget",
            "title": "Widget",
            "aliases": [],
            "source_hashes": [self.hash],
            "body": body,
            "links": [],
        }
        if base_hash is not None:
            operation["base_hash"] = base_hash
        return operation

    def revised(self, note: str = "revised") -> str:
        return f"A first claim, {note}.[^source:{self.hash}]"

    def refusal(self, payload: dict) -> ValidationError:
        with self.assertRaises(ValidationError) as refused:
            self.service.apply(payload)
        return refused.exception

    def test_the_conflict_remedy_can_never_clear_this_refusal(self) -> None:
        """The loop itself. This is the test the old classification failed.

        Five cycles of exactly what `conflict` instructs -- re-read the page's
        current version, rebuild the request against it, send it again under the
        stable id the contract asks for. The binding is in `applied.json`, so
        every cycle reads the same answer back.
        """

        base_hash = None
        for attempt in range(5):
            error = self.refusal(
                {
                    "proposal_id": "agent-turn-1",
                    "operations": [self.operation(self.revised(str(attempt)), base_hash)],
                }
            )
            self.assertEqual(
                error.details.get("proposal_id"), "agent-turn-1", f"cycle {attempt}"
            )
            base_hash = self.vault.wiki_version(self.page)

    def test_the_remedy_it_states_actually_succeeds(self) -> None:
        """Read the hint, do what it says, and the turn finishes."""

        error = self.refusal(
            {
                "proposal_id": "agent-turn-1",
                "operations": [self.operation(self.revised())],
            }
        )
        self.assertIn("new proposal_id", error.details["hint"])
        applied = self.service.apply(
            {
                "proposal_id": "agent-turn-2",
                "operations": [
                    self.operation(self.revised(), self.vault.wiki_version(self.page))
                ],
            }
        )
        self.assertEqual(applied["applied"], 1)
        self.assertIn(self.revised(), (self.root / self.page).read_text())

    def test_omitting_the_id_is_the_other_stated_remedy(self) -> None:
        """The hint offers two moves; both have to work, or it misleads."""

        error = self.refusal(
            {
                "proposal_id": "agent-turn-1",
                "operations": [self.operation(self.revised())],
            }
        )
        self.assertIn("omit it", error.details["hint"])
        applied = self.service.apply(
            {
                "operations": [
                    self.operation(self.revised(), self.vault.wiki_version(self.page))
                ]
            }
        )
        self.assertEqual(applied["applied"], 1)

    def test_it_is_reported_as_change_the_request_not_as_a_conflict(self) -> None:
        error = self.refusal(
            {
                "proposal_id": "agent-turn-1",
                "operations": [self.operation(self.revised())],
            }
        )
        self.assertEqual(error.code, "validation_error")
        self.assertNotIsInstance(error, ConflictError)

    def test_the_refusal_shows_the_payload_really_differs(self) -> None:
        """Without both hashes the caller cannot tell a real reuse from a bug."""

        error = self.refusal(
            {
                "proposal_id": "agent-turn-1",
                "operations": [self.operation(self.revised())],
            }
        )
        self.assertNotEqual(
            error.details["applied_request_hash"], error.details["request_hash"]
        )

    def test_replaying_the_identical_payload_is_still_a_no_op(self) -> None:
        """Control: idempotent replay is the whole reason the binding exists."""

        replay = self.service.apply(
            {
                "proposal_id": "agent-turn-1",
                "operations": [self.operation(f"A first claim.[^source:{self.hash}]")],
            }
        )
        self.assertTrue(replay["idempotent"])

    def test_a_genuinely_stale_page_is_still_a_conflict(self) -> None:
        """Control: the narrowing must not swallow the case that *is* one."""

        error = self.refusal(
            {
                "proposal_id": "agent-turn-2",
                "operations": [self.operation(self.revised(), "f" * 64)],
            }
        )
        self.assertEqual(error.code, "conflict")

    def test_the_locked_check_answers_exactly_the_same(self) -> None:
        """The twin under the write lock, driven directly.

        The gate checks this before taking the lock and the vault checks it
        again after; a caller must not learn a different remedy depending on
        which of them won the race.
        """

        with self.assertRaises(ValidationError) as refused:
            self.vault.commit_wiki_batch(
                ApplyPlan(
                    pages={},
                    expected_versions={},
                    source_statuses={},
                    proposal_id="agent-turn-1",
                    request_hash="0" * 64,
                    freshness_updates={},
                    raw_move=None,
                    index_rebuild=lambda records: 0,
                )
            )
        locked = refused.exception
        gate = self.refusal(
            {
                "proposal_id": "agent-turn-1",
                "operations": [self.operation(self.revised())],
            }
        )
        self.assertEqual(locked.code, gate.code)
        self.assertEqual(str(locked), str(gate))
        self.assertEqual(locked.details["hint"], gate.details["hint"])


class WikiCatalogHasNoSelfDeclaredExemptionTest(unittest.TestCase):
    """A page could exempt itself from the apply gate's duplicate check.

    `ApplyGate._wiki_catalog` skipped any page whose *own* frontmatter said
    `type: "system"`, and that catalog is what `_validate_novelty` compares a
    proposal against. So four words in a page's own header bought it permanent
    invisibility to `duplicate_identity` -- the same shape as the
    `wiki.outside_apply` exemption, where the file being checked decided whether
    it would be checked.

    The seeded pages are deliberately *not* exempted here, unlike in
    `SEEDED_SYSTEM_PAGES`. That constant answers a provenance question ("which
    pages may exist with no ledger entry"); this asks which identities are
    already taken, and `wiki/index.md` does take one. Sharing a constant would
    couple two questions that only agree by coincidence today.
    """

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.vault = FileVault.initialize(self.root, policy())
        self.service = BrainskitService(
            self.vault, SqliteFtsIndex(self.vault.index_path), graph=MarkdownGraph()
        )
        self.hash = self.service.capture(
            None, text="Evidence about compiled memory.", title="Evidence"
        )["source"]["content_hash"]

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def write_page(self, name: str, title: str, page_type: str) -> str:
        path = f"wiki/{name}.md"
        (self.root / path).write_text(
            "---\n"
            f'id: "concept:{name}"\n'
            f'type: "{page_type}"\n'
            f"title: {json.dumps(title)}\n"
            "aliases:\nsources:\n"
            'updated_at: "2026-01-01T00:00:00Z"\n'
            "---\n\n"
            f"# {title}\n\nCompiled memory moves work out of query time.\n",
            encoding="utf-8",
        )
        return path

    def proposal(self, slug: str, title: str) -> dict:
        return {
            "operations": [
                {
                    "action": "upsert",
                    "kind": "concept",
                    "slug": slug,
                    "title": title,
                    "aliases": [],
                    "source_hashes": [self.hash],
                    "body": f"Compiled memory moves work.[^source:{self.hash}]",
                    "links": [],
                }
            ]
        }

    def test_a_page_declaring_type_system_is_still_catalogued(self) -> None:
        path = self.write_page("impostor", "Compiled memory", "system")
        catalog = {entry["path"] for entry in self.service.gate._wiki_catalog()}
        self.assertIn(path, catalog)

    def test_it_cannot_buy_silence_from_the_duplicate_check(self) -> None:
        """The defect end to end: the refusal a stolen title must produce."""

        self.write_page("impostor", "Compiled memory", "system")
        with self.assertRaises(ValidationError) as refused:
            self.service.apply(self.proposal("compiled-memory", "Compiled memory"))
        failures = refused.exception.details["failures"]
        self.assertEqual(
            [(item["code"], item["existing_path"]) for item in failures],
            [("duplicate_identity", "wiki/impostor.md")],
        )

    def test_the_declared_type_changes_nothing_about_being_seen(self) -> None:
        """`system` was the exempt spelling; every other one already collided.

        Asserted as one behaviour across spellings, because the bug was that a
        single value behaved differently from the rest.
        """

        for page_type in ("system", "concept", "entity", "synthesis", "source"):
            with self.subTest(page_type=page_type):
                path = self.write_page(f"impostor-{page_type}", "Shared name", page_type)
                catalog = {entry["path"] for entry in self.service.gate._wiki_catalog()}
                self.assertIn(path, catalog)
                with self.assertRaises(ValidationError) as refused:
                    self.service.apply(self.proposal(f"page-{page_type}", "Shared name"))
                codes = {
                    item["code"] for item in refused.exception.details["failures"]
                }
                self.assertIn("duplicate_identity", codes)
                (self.root / path).unlink()

    def test_the_pages_bk_init_seeds_are_catalogued_too(self) -> None:
        """They hold real titles and real slugs, so they are real occupants."""

        catalog = {entry["path"] for entry in self.service.gate._wiki_catalog()}
        self.assertLessEqual({"wiki/index.md", "wiki/log.md"}, catalog)
        with self.assertRaises(ValidationError) as refused:
            self.service.apply(self.proposal("brainskit-index", "Brainskit index"))
        failures = refused.exception.details["failures"]
        self.assertEqual(
            [(item["code"], item["existing_path"]) for item in failures],
            [("duplicate_identity", "wiki/index.md")],
        )

    def test_cataloguing_them_does_not_block_an_ordinary_apply(self) -> None:
        """The control: the seeded pages must not collide with normal work.

        Only an identity clash is a clash. A proposal with its own title still
        applies, and the `type` partition keeps the seeded bodies out of the
        similarity comparison.
        """

        result = self.service.apply(self.proposal("memoria-compilada", "Memória compilada"))
        self.assertEqual(result["applied"], 1)


if __name__ == "__main__":
    unittest.main()
