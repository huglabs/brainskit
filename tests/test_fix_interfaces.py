from __future__ import annotations

import io
import json
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from http import HTTPStatus
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from brainkit.application.services import BrainkitService
from brainkit.domain.model import ValidationError
from brainkit.infrastructure.graph import MarkdownGraph
from brainkit.infrastructure.index import SqliteFtsIndex
from brainkit.infrastructure.vault import FileVault
from brainkit.interfaces import cli
from brainkit.interfaces.mcp import (
    MCP_MAX_REQUEST_BYTES,
    MCP_PROTOCOL_VERSION,
    BrainkitMcpHttpHandler,
    BrainkitMcpHttpServer,
    _safe_reason,
    _tool_definitions,
    run_stdio,
)


def policy() -> dict:
    return {
        "version": 3,
        "wiki_language": "Portuguese (Brazil)",
        "inbox_policy": {"privacy": "local-only", "filing": "approve-each"},
        "branches": {
            "10-work": {"privacy": "never-ingest", "filing": "approve-each"},
            "20-research": {"privacy": "local-only", "filing": "auto+digest-review"},
        },
        "providers": {"ollama": {"base_url": "http://127.0.0.1:11434"}},
        "job_models": {
            job: {"provider": "ollama", "model": "test"}
            for job in (
                "ingest",
                "query",
                "digest",
                "lint-semantic",
                "file-proposal",
                "resurface",
            )
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
            name: {"enabled": False, "managed": False, "options": {}}
            for name in ("obsidian", "neo4j", "postgres", "web")
        },
    }


class RecordingService:
    """Stands in for BrainkitService so interface tests stay hermetic."""

    def __init__(self, failures: set[str] | None = None):
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []
        self.failures = failures or set()

    def __getattr__(self, name: str):
        def operation(*args: Any, **kwargs: Any) -> dict[str, Any]:
            self.calls.append((name, args, kwargs))
            if name in self.failures:
                raise RuntimeError("boom")
            return {"called": name}

        return operation


class CliExportConsumerTest(unittest.TestCase):
    """export must carry a privacy boundary, defaulting to local."""

    def setUp(self) -> None:
        self.service = RecordingService()
        self.original = cli.create_service
        cli.create_service = lambda vault: self.service  # type: ignore[assignment]

    def tearDown(self) -> None:
        cli.create_service = self.original  # type: ignore[assignment]

    def _run(self, argv: list[str]) -> tuple[int, dict[str, Any]]:
        stream = io.StringIO()
        with redirect_stdout(stream):
            code = cli.main(argv)
        return code, json.loads(stream.getvalue())

    def test_export_defaults_to_local_consumer(self) -> None:
        code, payload = self._run(["--json", "export", "--target", "json"])
        self.assertEqual(code, 0)
        self.assertTrue(payload["ok"])
        name, args, kwargs = self.service.calls[-1]
        self.assertEqual(name, "export")
        self.assertEqual(args, ("json",))
        self.assertEqual(kwargs, {"consumer": "local"})

    def test_export_forwards_explicit_consumer(self) -> None:
        for consumer in ("human", "local", "cloud"):
            with self.subTest(consumer=consumer):
                self._run(
                    ["--json", "export", "--target", "graphml", "--consumer", consumer]
                )
                self.assertEqual(self.service.calls[-1][2], {"consumer": consumer})

    def test_export_rejects_an_unknown_consumer(self) -> None:
        with redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as exit_code:
                cli.main(["export", "--target", "json", "--consumer", "nope"])
        self.assertEqual(exit_code.exception.code, 2)

    def test_export_consumer_never_raises_unlike_search_and_context(self) -> None:
        """search/context still demand an explicit --consumer in --json mode."""

        self._run(["--json", "export", "--target", "cypher"])
        with self.assertRaises(ValidationError):
            cli._consumer_for_args(
                cli.build_parser().parse_args(["--json", "search", "q"])
            )


class CliSafetyNetTest(unittest.TestCase):
    """A foreign adapter exception must not replace the CLI contract."""

    def setUp(self) -> None:
        self.original = cli.create_service
        service = RecordingService(failures={"status"})
        cli.create_service = lambda vault: service  # type: ignore[assignment]

    def tearDown(self) -> None:
        cli.create_service = self.original  # type: ignore[assignment]

    def test_json_mode_emits_a_single_line_envelope_without_a_traceback(self) -> None:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(["--json", "status"])
        self.assertNotEqual(code, 0)
        stdout = out.getvalue()
        self.assertEqual(len(stdout.strip().splitlines()), 1)
        self.assertNotIn("Traceback", stdout)
        payload = json.loads(stdout)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"]["code"], "internal_error")
        self.assertIn("RuntimeError", payload["error"]["message"])
        self.assertIn("Traceback", err.getvalue())

    def test_non_json_mode_prints_a_readable_message_on_stderr(self) -> None:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(["status"])
        self.assertNotEqual(code, 0)
        self.assertEqual(out.getvalue(), "")
        self.assertIn("bk: RuntimeError: boom", err.getvalue())

    def test_safety_net_does_not_swallow_system_exit(self) -> None:
        original = cli._dispatch

        def exiting(args):
            raise SystemExit(7)

        cli._dispatch = exiting  # type: ignore[assignment]
        try:
            with self.assertRaises(SystemExit) as exit_code:
                cli.main(["--json", "status"])
        finally:
            cli._dispatch = original  # type: ignore[assignment]
        self.assertEqual(exit_code.exception.code, 7)

    def test_safety_net_keeps_the_interrupt_exit_code(self) -> None:
        original = cli._dispatch

        def interrupted(args):
            raise KeyboardInterrupt

        cli._dispatch = interrupted  # type: ignore[assignment]
        try:
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                code = cli.main(["--json", "status"])
        finally:
            cli._dispatch = original  # type: ignore[assignment]
        self.assertEqual(code, 130)


class McpStdioSafetyNetTest(unittest.TestCase):
    def test_stdio_loop_survives_an_unexpected_exception(self) -> None:
        service = RecordingService(failures={"search"})
        requests = [
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": MCP_PROTOCOL_VERSION},
            },
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {
                    "name": "search",
                    "arguments": {"query": "q", "consumer": "local"},
                },
            },
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "status", "arguments": {}},
            },
        ]
        stdin = io.StringIO("".join(json.dumps(item) + "\n" for item in requests))
        out, err = io.StringIO(), io.StringIO()
        original_stdin = sys.stdin
        sys.stdin = stdin
        try:
            with redirect_stdout(out), redirect_stderr(err):
                run_stdio(service)  # type: ignore[arg-type]
        finally:
            sys.stdin = original_stdin
        responses = {
            item["id"]: item
            for item in (json.loads(line) for line in out.getvalue().splitlines())
        }
        self.assertEqual(sorted(responses), [1, 2, 3])
        self.assertEqual(responses[2]["error"]["code"], -32603)
        self.assertIn("RuntimeError", responses[2]["error"]["data"]["reason"])
        self.assertIn("structuredContent", responses[3]["result"])
        self.assertNotIn("Traceback", out.getvalue())
        self.assertIn("Traceback", err.getvalue())


class McpHttpContractTest(unittest.TestCase):
    """The Streamable HTTP status codes a conforming client relies on."""

    def setUp(self) -> None:
        self.service = RecordingService(failures={"search"})
        self.server = BrainkitMcpHttpServer(
            ("127.0.0.1", 0), BrainkitMcpHttpHandler
        )
        self.server.service = self.service  # type: ignore[assignment]
        self.server.token = "test-secret"
        self.base_url = f"http://127.0.0.1:{self.server.server_port}"
        self.server.allowed_origins = {self.base_url}
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def _post(
        self,
        payload: dict[str, Any] | str,
        *,
        authenticated: bool = True,
        origin: str | None = None,
        version: str | None = None,
    ) -> tuple[int, dict[str, Any], dict[str, str]]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if authenticated:
            headers["Authorization"] = "Bearer test-secret"
        if origin:
            headers["Origin"] = origin
        if version:
            headers["MCP-Protocol-Version"] = version
        body = payload if isinstance(payload, str) else json.dumps(payload)
        request = Request(
            f"{self.base_url}/mcp",
            data=body.encode(),
            headers=headers,
            method="POST",
        )
        try:
            with urlopen(request, timeout=5) as response:
                raw = response.read()
                return (
                    response.status,
                    json.loads(raw) if raw else {},
                    dict(response.headers),
                )
        except HTTPError as error:
            with error:
                raw = error.read()
                return error.code, json.loads(raw) if raw else {}, dict(error.headers)

    def _initialize(self) -> dict[str, Any]:
        return {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": MCP_PROTOCOL_VERSION},
        }

    def test_missing_protocol_version_is_a_bad_request(self) -> None:
        status, body, _ = self._post({"jsonrpc": "2.0", "id": 2, "method": "ping"})
        self.assertEqual(status, HTTPStatus.BAD_REQUEST)
        self.assertEqual(body["id"], 2)
        self.assertEqual(body["error"]["code"], -32600)
        self.assertIn(
            "MCP-Protocol-Version", body["error"]["data"]["reason"]
        )
        self.assertEqual(
            body["error"]["data"]["supported"], [MCP_PROTOCOL_VERSION]
        )

    def test_unsupported_protocol_version_is_a_bad_request(self) -> None:
        status, body, _ = self._post(
            {"jsonrpc": "2.0", "id": 2, "method": "ping"}, version="1999-01-01"
        )
        self.assertEqual(status, HTTPStatus.BAD_REQUEST)
        self.assertEqual(body["error"]["code"], -32600)
        self.assertIn("unsupported", body["error"]["data"]["reason"])

    def test_initialize_still_negotiates_without_the_version_header(self) -> None:
        status, body, _ = self._post(self._initialize())
        self.assertEqual(status, HTTPStatus.OK)
        self.assertEqual(body["result"]["protocolVersion"], MCP_PROTOCOL_VERSION)

    def test_supported_protocol_version_still_succeeds(self) -> None:
        status, body, _ = self._post(
            {"jsonrpc": "2.0", "id": 3, "method": "ping"},
            version=MCP_PROTOCOL_VERSION,
        )
        self.assertEqual(status, HTTPStatus.OK)
        self.assertEqual(body["result"], {})

    def test_authentication_and_origin_guards_are_preserved(self) -> None:
        unauthorized, _, _ = self._post(self._initialize(), authenticated=False)
        self.assertEqual(unauthorized, HTTPStatus.UNAUTHORIZED)
        forbidden, _, _ = self._post(
            self._initialize(), origin="https://attacker.invalid"
        )
        self.assertEqual(forbidden, HTTPStatus.FORBIDDEN)
        allowed, _, _ = self._post(self._initialize(), origin=self.base_url)
        self.assertEqual(allowed, HTTPStatus.OK)

    def test_wrong_bearer_token_is_unauthorized(self) -> None:
        request = Request(
            f"{self.base_url}/mcp",
            data=json.dumps(self._initialize()).encode(),
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
                "Authorization": "Bearer wrong-secret",
            },
            method="POST",
        )
        with self.assertRaises(HTTPError) as error:
            urlopen(request, timeout=5)
        error.exception.close()
        self.assertEqual(error.exception.code, HTTPStatus.UNAUTHORIZED)

    def test_standalone_sse_get_is_not_allowed(self) -> None:
        request = Request(
            f"{self.base_url}/mcp",
            headers={"Authorization": "Bearer test-secret"},
            method="GET",
        )
        with self.assertRaises(HTTPError) as error:
            urlopen(request, timeout=5)
        error.exception.close()
        self.assertEqual(error.exception.code, HTTPStatus.METHOD_NOT_ALLOWED)

    def test_notifications_are_accepted_without_a_body(self) -> None:
        status, body, _ = self._post(
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            version=MCP_PROTOCOL_VERSION,
        )
        self.assertEqual(status, HTTPStatus.ACCEPTED)
        self.assertEqual(body, {})

    def test_oversized_body_is_rejected_and_malformed_body_is_not(self) -> None:
        payload = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "ping",
                "params": {"pad": "x" * (MCP_MAX_REQUEST_BYTES + 1024)},
            }
        )
        self.assertGreater(len(payload), MCP_MAX_REQUEST_BYTES)
        status, body, _ = self._post(payload, version=MCP_PROTOCOL_VERSION)
        self.assertEqual(status, HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
        self.assertEqual(body["error"]["code"], "body_too_large")
        empty, empty_body, _ = self._post("", version=MCP_PROTOCOL_VERSION)
        self.assertEqual(empty, HTTPStatus.BAD_REQUEST)
        self.assertEqual(empty_body["error"]["code"], "body_size_invalid")
        broken, broken_body, _ = self._post(
            '{"jsonrpc":"2.0","id":7,', version=MCP_PROTOCOL_VERSION
        )
        self.assertEqual(broken, HTTPStatus.BAD_REQUEST)
        self.assertEqual(broken_body["error"]["code"], -32600)

    def test_tool_level_validation_stays_a_jsonrpc_error_at_200(self) -> None:
        """Only transport failures become HTTP errors; tool errors do not."""

        status, body, _ = self._post(
            {
                "jsonrpc": "2.0",
                "id": 8,
                "method": "tools/call",
                "params": {"name": "does-not-exist", "arguments": {}},
            },
            version=MCP_PROTOCOL_VERSION,
        )
        self.assertEqual(status, HTTPStatus.OK)
        self.assertEqual(body["error"]["code"], -32600)
        unknown, unknown_body, _ = self._post(
            {"jsonrpc": "2.0", "id": 9, "method": "no/such/method"},
            version=MCP_PROTOCOL_VERSION,
        )
        self.assertEqual(unknown, HTTPStatus.OK)
        self.assertEqual(unknown_body["error"]["code"], -32601)

    def test_security_headers_are_present_on_success_and_on_the_new_400(self) -> None:
        _, _, ok_headers = self._post(
            {"jsonrpc": "2.0", "id": 5, "method": "ping"},
            version=MCP_PROTOCOL_VERSION,
        )
        _, _, bad_headers = self._post({"jsonrpc": "2.0", "id": 6, "method": "ping"})
        for headers in (ok_headers, bad_headers):
            self.assertEqual(headers["Cache-Control"], "no-store")
            self.assertEqual(headers["X-Content-Type-Options"], "nosniff")

    def test_handler_survives_an_unexpected_exception_and_keeps_serving(self) -> None:
        with redirect_stderr(io.StringIO()):
            failed, body, _ = self._post(
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {
                        "name": "search",
                        "arguments": {"query": "q", "consumer": "local"},
                    },
                },
                version=MCP_PROTOCOL_VERSION,
            )
            self.assertEqual(failed, HTTPStatus.INTERNAL_SERVER_ERROR)
            self.assertEqual(body["id"], 2)
            self.assertEqual(body["error"]["code"], -32603)
            self.assertIn("RuntimeError", body["error"]["data"]["reason"])
            self.assertNotIn("Traceback", json.dumps(body))
            survived, survivor, _ = self._post(
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "tools/call",
                    "params": {"name": "status", "arguments": {}},
                },
                version=MCP_PROTOCOL_VERSION,
            )
        self.assertEqual(survived, HTTPStatus.OK)
        self.assertEqual(survivor["id"], 3)


class McpErrorRedactionTest(unittest.TestCase):
    def test_absolute_paths_are_replaced_in_remote_reasons(self) -> None:
        reason = _safe_reason(
            FileNotFoundError(2, "No such file or directory: '/Users/x/vault/a.md'")
        )
        self.assertIn("FileNotFoundError", reason)
        self.assertNotIn("/Users/x/vault", reason)
        self.assertIn("<path>", reason)

    def test_long_messages_are_bounded(self) -> None:
        self.assertLessEqual(len(_safe_reason(RuntimeError("x" * 5000))), 260)


class McpConsumerSchemaTest(unittest.TestCase):
    """An agent reading the schema must not mistake human for a safe default."""

    def _consumer(self, tool_name: str) -> dict[str, Any]:
        tool = next(
            item for item in _tool_definitions() if item["name"] == tool_name
        )
        return dict(tool["inputSchema"]["properties"]["consumer"])

    def test_consumer_values_are_documented_for_search_and_context(self) -> None:
        for tool_name in ("search", "context"):
            with self.subTest(tool=tool_name):
                schema = self._consumer(tool_name)
                self.assertEqual(schema["enum"], ["human", "local", "cloud"])
                description = schema["description"]
                self.assertIn("no default", description.lower())
                self.assertIn("NO restriction", description)
                self.assertIn("never-ingest", description)
                self.assertIn("cloud", description)

    def test_consumer_stays_required_on_search_and_context(self) -> None:
        for tool_name in ("search", "context"):
            tool = next(
                item for item in _tool_definitions() if item["name"] == tool_name
            )
            self.assertIn("consumer", tool["inputSchema"]["required"])


class AgentInstallTest(unittest.TestCase):
    """`hooks install` seeds the skill and the graph instructions."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.vault = FileVault.initialize(self.root, policy())
        self.service = BrainkitService(
            self.vault,
            SqliteFtsIndex(self.vault.index_path),
            graph=MarkdownGraph(),
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def install(self, agent: str = "claude", *, force: bool = False) -> dict[str, Any]:
        return cli._install_hooks(self.service, agent, force=force)

    def instructions(self, agent: str = "claude") -> str:
        return (self.root / cli.INSTRUCTION_FILES[agent]).read_text(encoding="utf-8")

    def skill_path(self) -> Path:
        return self.root / ".claude" / "skills" / "brainkit" / "SKILL.md"

    def test_a_vault_without_git_still_installs(self) -> None:
        result = self.install()
        self.assertEqual(result["pre_commit"]["state"], "skipped")
        self.assertEqual(result["skill"]["state"], "created")
        self.assertTrue(self.skill_path().is_file())

    def test_the_skill_is_rendered_with_the_vault_path(self) -> None:
        self.install()
        content = self.skill_path().read_text(encoding="utf-8")
        self.assertNotIn("{{vault}}", content)
        self.assertIn(str(self.root), content)
        self.assertTrue(content.startswith("---\nname: brainkit\n"))

    def test_the_instructions_describe_how_the_graph_is_formed(self) -> None:
        self.install()
        content = self.instructions()
        self.assertNotIn("{{vault}}", content)
        for expected in ("sourced_from", "links_to", "raw:<sha256>", "page:<path>"):
            self.assertIn(expected, content)

    def test_reinstalling_never_duplicates_the_block(self) -> None:
        for _ in range(3):
            self.install()
        content = self.instructions()
        self.assertEqual(content.count(cli.INSTRUCTION_START), 1)
        self.assertEqual(content.count(cli.INSTRUCTION_END), 1)

    def test_reinstalling_is_byte_identical(self) -> None:
        self.install()
        first = self.instructions()
        result = self.install()
        self.assertEqual(result["instructions"]["state"], "current")
        self.assertEqual(self.instructions(), first)

    def test_surrounding_instructions_keep_their_position(self) -> None:
        target = self.root / "CLAUDE.md"
        target.write_text("# Topo\n\nAntes.\n", encoding="utf-8")
        self.install()
        target.write_text(
            target.read_text(encoding="utf-8") + "\n## Rodape\n\nDepois.\n",
            encoding="utf-8",
        )
        self.install()
        content = self.instructions()
        self.assertLess(content.index("# Topo"), content.index(cli.INSTRUCTION_START))
        self.assertGreater(
            content.index("## Rodape"), content.index(cli.INSTRUCTION_END)
        )

    def test_an_existing_skill_is_not_replaced_without_force(self) -> None:
        self.skill_path().parent.mkdir(parents=True, exist_ok=True)
        self.skill_path().write_text("mine", encoding="utf-8")
        with self.assertRaises(ValidationError):
            self.install()
        self.assertEqual(self.skill_path().read_text(encoding="utf-8"), "mine")
        self.assertEqual(self.install(force=True)["skill"]["state"], "updated")

    def test_only_claude_receives_a_skill(self) -> None:
        for agent, filename in (
            ("codex", "AGENTS.md"),
            ("gemini", "GEMINI.md"),
            ("opencode", "AGENTS.md"),
        ):
            with self.subTest(agent=agent):
                result = self.install(agent)
                self.assertNotIn("skill", result)
                self.assertTrue((self.root / filename).is_file())

    def test_templates_are_packaged_with_the_distribution(self) -> None:
        for name in ("claude-skill", "instructions"):
            with self.subTest(template=name):
                self.assertTrue(cli._agent_template(name, self.root))

    def test_an_unknown_template_is_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            cli._agent_template("does-not-exist", self.root)


if __name__ == "__main__":
    unittest.main()
