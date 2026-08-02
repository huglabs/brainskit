from __future__ import annotations

import json
import shutil
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from brainkit.application.services import BrainkitService
from brainkit.domain.model import (
    PolicyError,
    ValidationError,
    VaultConfig,
    validate_schema,
)
from brainkit.infrastructure.graph import MarkdownGraph
from brainkit.infrastructure.index import SqliteFtsIndex
from brainkit.infrastructure.integrations import NativeIntegrations
from brainkit.infrastructure.llm import JobSpecs, PolicyJudgmentRouter
from brainkit.infrastructure.vault import FileVault
from brainkit.interfaces.mcp import (
    MCP_PROTOCOL_VERSION,
    BrainkitMcpHttpHandler,
    BrainkitMcpHttpServer,
    _handle,
)
from brainkit.interfaces.web import BrainkitWebHandler, BrainkitWebServer


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

    def run(self, *, job, branches, variables, output_schema=None) -> str:
        self.calls.append(job)
        values = self.responses[job]
        value = values.pop(0) if len(values) > 1 else values[0]
        return value if isinstance(value, str) else json.dumps(value)


class EngineTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.vault = FileVault.initialize(self.root, policy())
        self.service = BrainkitService(
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

    def test_obsidian_sync_is_manifest_based_and_persistent(self) -> None:
        with tempfile.TemporaryDirectory() as destination:
            configured = self.service.integration_configure(
                "obsidian",
                enabled=True,
                managed=False,
                options={"path": destination, "subdirectory": "brainkit"},
            )
            self.assertTrue(configured["policy"]["enabled"])
            synced = self.service.integration_sync("obsidian")
            target = Path(destination) / "brainkit"
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
                    "password_env": "BRAINKIT_TEST_NEO4J_PASSWORD",
                },
            )

    def test_web_viewer_serves_health_graph_and_complete_html(self) -> None:
        server = BrainkitWebServer(("127.0.0.1", 0), BrainkitWebHandler)
        server.service = self.service
        server.consumer = "human"
        server.token = None
        server.instance_id = "test-instance"
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

    def test_network_mcp_requires_auth_origin_and_protocol_version(self) -> None:
        server = BrainkitMcpHttpServer(("127.0.0.1", 0), BrainkitMcpHttpHandler)
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
                self.service._apply_transaction(
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

        result = self.service._apply_transaction(
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
                datetime.now(timezone.utc) - timedelta(days=45)
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
        service = BrainkitService(
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


if __name__ == "__main__":
    unittest.main()
