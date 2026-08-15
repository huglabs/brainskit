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
from unittest import mock
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from brainskit.application import installer
from brainskit.application.gate import INSTRUCTION_END, INSTRUCTION_START
from brainskit.application.install import agent_install
from brainskit.application.services import BrainskitService
from brainskit.domain.model import NotFoundError, ValidationError
from brainskit.infrastructure.graph import MarkdownGraph
from brainskit.infrastructure.index import SqliteFtsIndex
from brainskit.infrastructure.integrations import NativeIntegrations
from brainskit.infrastructure.vault import FileVault
from brainskit.interfaces import cli, console, onboarding, prompt
from brainskit.interfaces.mcp import (
    MCP_MAX_REQUEST_BYTES,
    MCP_PROTOCOL_VERSION,
    BrainskitMcpHttpHandler,
    BrainskitMcpHttpServer,
    _call_tool,
    _safe_reason,
    _tool_definitions,
    run_stdio,
)
from brainskit.interfaces.web import build_server


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
    """Stands in for BrainskitService so interface tests stay hermetic."""

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
        # By key rather than by whole dict: this test is about the boundary,
        # and pinning every kwarg makes any unrelated flag look like a defect.
        self.assertEqual(kwargs["consumer"], "local")

    def test_export_omits_enrichment_unless_it_is_asked_for(self) -> None:
        # Model-inferred edges are a weaker claim than derived ones, so the
        # default has to stay off wherever the graph leaves the vault.
        self._run(["--json", "export", "--target", "json"])
        self.assertFalse(self.service.calls[-1][2]["enrichment"])
        self._run(["--json", "export", "--target", "json", "--enrichment"])
        self.assertTrue(self.service.calls[-1][2]["enrichment"])

    def test_export_forwards_explicit_consumer(self) -> None:
        for consumer in ("human", "local", "cloud"):
            with self.subTest(consumer=consumer):
                self._run(
                    ["--json", "export", "--target", "graphml", "--consumer", consumer]
                )
                self.assertEqual(self.service.calls[-1][2]["consumer"], consumer)

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


class CliWebNoBrowserFlagTest(unittest.TestCase):
    """`--no-browser` must exist on both `web` and its `web serve` alias."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        FileVault.initialize(self.root, policy())

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_the_flag_parses_on_both_the_alias_and_the_command_it_stands_in_for(
        self,
    ) -> None:
        parser = cli.build_parser()
        self.assertFalse(parser.parse_args(["web"]).no_browser)
        self.assertTrue(parser.parse_args(["web", "--no-browser"]).no_browser)
        self.assertTrue(
            parser.parse_args(["web", "serve", "--no-browser"]).no_browser
        )

    def test_the_flag_reaches_run_web_as_open_browser_false(self) -> None:
        with mock.patch("brainskit.interfaces.web.run_web") as run_web_mock:
            code = cli.main(["--vault", str(self.root), "web", "--no-browser"])
        self.assertEqual(code, 0)
        run_web_mock.assert_called_once()
        self.assertFalse(run_web_mock.call_args.kwargs["open_browser"])

    def test_without_the_flag_open_browser_defaults_to_true(self) -> None:
        with mock.patch("brainskit.interfaces.web.run_web") as run_web_mock:
            cli.main(["--vault", str(self.root), "web"])
        self.assertTrue(run_web_mock.call_args.kwargs["open_browser"])


class CliWebNoVaultPromptTest(unittest.TestCase):
    """`bk web` with no vault offers to run `bk init` instead of just erring.

    A person clicking their way to `bk web` has no other way to discover
    `bk init` short of reading the error; a script piping into it must never
    block on a question nobody is there to answer.
    """

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.empty = Path(self.temporary.name) / "no-vault-here"
        self.empty.mkdir()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_non_interactive_keeps_the_plain_refusal(self) -> None:
        with (
            mock.patch.object(prompt, "supports_interactive", return_value=False),
            mock.patch.object(prompt, "confirm") as confirm_mock,
        ):
            code = cli.main(["--vault", str(self.empty), "web"])
        self.assertEqual(code, 2)
        confirm_mock.assert_not_called()
        self.assertFalse((self.empty / ".brain" / "config.json").exists())

    def test_json_mode_keeps_the_plain_refusal_even_if_a_tty_is_present(
        self,
    ) -> None:
        with (
            mock.patch.object(prompt, "supports_interactive", return_value=True),
            mock.patch.object(prompt, "confirm") as confirm_mock,
        ):
            code = cli.main(["--json", "--vault", str(self.empty), "web"])
        self.assertEqual(code, 2)
        confirm_mock.assert_not_called()

    def test_declining_the_prompt_falls_through_to_the_same_refusal(self) -> None:
        with (
            mock.patch.object(prompt, "supports_interactive", return_value=True),
            mock.patch.object(prompt, "confirm", return_value=False) as confirm_mock,
        ):
            code = cli.main(["--vault", str(self.empty), "web"])
        self.assertEqual(code, 2)
        confirm_mock.assert_called_once()
        self.assertFalse((self.empty / ".brain" / "config.json").exists())

    def test_accepting_the_prompt_initializes_and_then_serves_the_vault(self) -> None:
        outcome = onboarding.Outcome(policy=policy(), wire_agent=False, vault=self.empty)
        with (
            mock.patch.object(prompt, "supports_interactive", return_value=True),
            mock.patch.object(prompt, "confirm", return_value=True),
            mock.patch.object(cli, "_guided_init", return_value=outcome) as init_mock,
            mock.patch("brainskit.interfaces.web.run_web") as run_web_mock,
        ):
            code = cli.main(["--vault", str(self.empty), "web", "--no-browser"])
        self.assertEqual(code, 0)
        init_mock.assert_called_once_with(self.empty.expanduser().resolve())
        self.assertTrue((self.empty / ".brain" / "config.json").exists())
        run_web_mock.assert_called_once()


class CliEmitErrorHumanRenderingTest(unittest.TestCase):
    """The non-JSON error path must read like prose, never like a JSON dump.

    `json_mode=True` is the machine contract and must stay byte-for-byte the
    same regardless of `details` shape -- that half of each test here is the
    regression guard for `--json`, MCP, and every script that parses it.
    """

    def test_json_mode_output_is_unchanged_for_a_hint_bearing_error(self) -> None:
        error = NotFoundError(
            "No brainskit vault found",
            details={"start": "/private/tmp/no-vault-test", "hint": "Pass --vault or run bk init"},
        )
        out = io.StringIO()
        with redirect_stdout(out):
            cli._emit_error(error, json_mode=True)
        self.assertEqual(
            out.getvalue(),
            json.dumps(
                {
                    "ok": False,
                    "error": {
                        "code": "not_found",
                        "message": "No brainskit vault found",
                        "details": {
                            "start": "/private/tmp/no-vault-test",
                            "hint": "Pass --vault or run bk init",
                        },
                    },
                },
                ensure_ascii=False,
            )
            + "\n",
        )

    def test_human_mode_hint_line_has_no_json_punctuation(self) -> None:
        error = NotFoundError(
            "No brainskit vault found",
            details={"start": "/private/tmp/no-vault-test", "hint": "Pass --vault or run bk init"},
        )
        err = io.StringIO()
        with redirect_stderr(err):
            cli._emit_error(error, json_mode=False)
        rendered = err.getvalue()
        self.assertIn("bk: No brainskit vault found", rendered)
        self.assertIn("Pass --vault or run bk init", rendered)
        # The hint line specifically -- not merely "somewhere in the output" --
        # must carry none of json.dumps's punctuation.
        hint_line = next(
            line for line in rendered.splitlines() if "Pass --vault" in line
        )
        for char in ("{", "}", '"'):
            self.assertNotIn(char, hint_line)

    def test_human_mode_renders_a_list_of_conflicts_without_json_dumps(self) -> None:
        error = ValidationError(
            "Apply proposal rejected; no files were written",
            details={
                "failures": [
                    {"path": "wiki/concepts/a.md", "code": "stale_page"},
                    {"path": "wiki/concepts/b.md", "code": "missing_base_hash"},
                ]
            },
        )
        err = io.StringIO()
        with redirect_stderr(err):
            cli._emit_error(error, json_mode=False)
        rendered = err.getvalue()
        self.assertNotIn("{", rendered)
        self.assertNotIn("}", rendered)
        self.assertNotIn('"', rendered)
        self.assertIn("wiki/concepts/a.md", rendered)
        self.assertIn("stale_page", rendered)
        self.assertIn("wiki/concepts/b.md", rendered)
        self.assertIn("missing_base_hash", rendered)

    def test_human_mode_prints_nothing_extra_when_details_is_empty(self) -> None:
        error = ValidationError("Unknown command")
        err = io.StringIO()
        with redirect_stderr(err):
            cli._emit_error(error, json_mode=False)
        self.assertEqual(err.getvalue().strip().splitlines(), ["bk: Unknown command"])


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
        self.server = BrainskitMcpHttpServer(
            ("127.0.0.1", 0), BrainskitMcpHttpHandler
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


class McpIntegrationStatusConsumerTest(unittest.TestCase):
    """integration_status over MCP names the machine boundary explicitly.

    The tool schema declares no consumer, so the call inherits the transport's
    own scope: `local`, the same boundary resources/list and resources/read
    already hardcode. Before the fix the call was bare, which handed a machine
    caller the unfiltered human payload.
    """

    def test_the_tool_passes_the_local_consumer(self) -> None:
        service = RecordingService()
        _call_tool(service, "integration_status", {})  # type: ignore[arg-type]
        name, args, kwargs = service.calls[-1]
        self.assertEqual(name, "integration_status")
        self.assertEqual(args, (None,))
        self.assertEqual(kwargs.get("consumer"), "local")

    def test_a_named_integration_keeps_the_local_consumer(self) -> None:
        service = RecordingService()
        _call_tool(service, "integration_status", {"name": "web"})  # type: ignore[arg-type]
        name, args, kwargs = service.calls[-1]
        self.assertEqual(name, "integration_status")
        self.assertEqual(args, ("web",))
        self.assertEqual(kwargs.get("consumer"), "local")


class AgentInstallTest(unittest.TestCase):
    """`hooks install` seeds the skill and the graph instructions."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.vault = FileVault.initialize(self.root, policy())
        self.service = BrainskitService(
            self.vault,
            SqliteFtsIndex(self.vault.index_path),
            graph=MarkdownGraph(),
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def install(self, agent: str = "claude", *, force: bool = False) -> dict[str, Any]:
        return cli._install_hooks(self.service, agent, force=force)

    def instructions(self, agent: str = "claude") -> str:
        target = self.root / agent_install(agent).instructions
        return target.read_text(encoding="utf-8")

    def skill_path(self) -> Path:
        return self.root / ".claude" / "skills" / "brainskit" / "SKILL.md"

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
        self.assertTrue(content.startswith("---\nname: brainskit\n"))

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
        self.assertEqual(content.count(INSTRUCTION_START), 1)
        self.assertEqual(content.count(INSTRUCTION_END), 1)

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
        self.assertLess(content.index("# Topo"), content.index(INSTRUCTION_START))
        self.assertGreater(content.index("## Rodape"), content.index(INSTRUCTION_END))

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
                self.assertTrue(installer._agent_template(name, self.root))

    def test_an_unknown_template_is_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            installer._agent_template("does-not-exist", self.root)


class WebViewerBoundaryTest(unittest.TestCase):
    """The viewer answers only to the names and origins it was bound for.

    A loopback viewer needs no token — that is the documented default — so the
    only thing keeping a web page out of the vault is that it cannot reach
    127.0.0.1. DNS rebinding removes that, and at `--consumer human` the API
    withholds nothing, `never-ingest` bodies included. The MCP endpoint has
    checked Origin since it shipped; this covers the same ground for the
    viewer, plus the Host check that rebinding actually turns on.
    """

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.vault = FileVault.initialize(self.root, policy())
        self.service = BrainskitService(
            self.vault, SqliteFtsIndex(self.vault.index_path), graph=MarkdownGraph()
        )
        self.service.reindex()
        self.server = build_server(
            self.service, host="127.0.0.1", port=0, consumer="human"
        )
        self.port = self.server.server_port
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temporary.cleanup()

    def get(self, path: str, **headers: str) -> tuple[int, str]:
        request = Request(f"http://127.0.0.1:{self.port}{path}", headers=headers)
        try:
            with urlopen(request, timeout=3) as response:
                return response.status, response.read().decode("utf-8")
        except HTTPError as error:
            return error.code, error.read().decode("utf-8")

    def test_the_configured_host_is_served(self) -> None:
        for path in ("/", "/api/health", "/api/status"):
            with self.subTest(path=path):
                status, _ = self.get(path)
                self.assertEqual(status, HTTPStatus.OK)

    def test_the_loopback_alias_is_served(self) -> None:
        status, _ = self.get("/api/status", Host=f"localhost:{self.port}")
        self.assertEqual(status, HTTPStatus.OK)

    def test_a_rebound_name_reaches_nothing(self) -> None:
        # Health included: a rebound page must not even learn a viewer is here.
        for path in ("/", "/api/health", "/api/status", "/api/resource?id=x"):
            with self.subTest(path=path):
                status, body = self.get(path, Host="attacker.example")
                self.assertEqual(status, HTTPStatus.FORBIDDEN)
                self.assertEqual(json.loads(body)["error"]["code"], "host_denied")

    def test_a_foreign_origin_is_refused(self) -> None:
        status, body = self.get("/api/status", Origin="http://attacker.example")
        self.assertEqual(status, HTTPStatus.FORBIDDEN)
        self.assertEqual(json.loads(body)["error"]["code"], "origin_denied")

    def test_the_viewers_own_origin_is_allowed(self) -> None:
        status, _ = self.get("/api/status", Origin=f"http://127.0.0.1:{self.port}")
        self.assertEqual(status, HTTPStatus.OK)

    def test_a_named_origin_whitelists_its_host_too(self) -> None:
        server = build_server(
            self.service,
            host="127.0.0.1",
            port=0,
            consumer="local",
            allowed_origins=["https://brain.example"],
        )
        try:
            self.assertIn("brain.example", server.allowed_hosts)
            self.assertIn("127.0.0.1", server.allowed_hosts)
            self.assertEqual(server.allowed_origins, {"https://brain.example"})
        finally:
            server.server_close()

    def test_a_remote_bind_still_requires_a_token(self) -> None:
        with self.assertRaises(ValidationError):
            build_server(
                self.service, host="0.0.0.0", port=0, consumer="local"  # noqa: S104
            )


class WebIntegrationsConsumerTest(unittest.TestCase):
    """/api/integrations answers under the viewer's bound consumer.

    Before the fix the handler called `integration_status()` bare, so a viewer
    bound at a machine consumer received the human payload over HTTP —
    filesystem paths and secret-bearing env-var names included. ADR 0001 names
    this as one of the two behavioral gaps the seam closes: "machine consumers
    no longer receive filesystem paths, container names, DSN env names".
    """

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.obsidian_target = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        vault = FileVault.initialize(root, policy())
        self.service = BrainskitService(
            vault,
            SqliteFtsIndex(vault.index_path),
            graph=MarkdownGraph(),
            integrations=NativeIntegrations(vault),
        )
        self.service.integration_configure(
            "obsidian",
            enabled=True,
            managed=False,
            options={"path": self.obsidian_target.name, "subdirectory": "brainskit"},
        )
        self.service.integration_configure(
            "postgres",
            enabled=True,
            managed=False,
            options={"dsn_env": "BRAINSKIT_TEST_PG_DSN", "consumer": "local"},
        )

    def tearDown(self) -> None:
        self.obsidian_target.cleanup()
        self.temporary.cleanup()

    def _fetch(self, consumer: str) -> tuple[int, str]:
        server = build_server(self.service, host="127.0.0.1", port=0, consumer=consumer)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            request = Request(f"http://127.0.0.1:{server.server_port}/api/integrations")
            try:
                with urlopen(request, timeout=3) as response:
                    return response.status, response.read().decode("utf-8")
            except HTTPError as error:
                return error.code, error.read().decode("utf-8")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_the_handler_forwards_the_bound_consumer(self) -> None:
        recording = RecordingService()
        server = build_server(
            recording,  # type: ignore[arg-type]
            host="127.0.0.1",
            port=0,
            consumer="local",
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with urlopen(
                f"http://127.0.0.1:{server.server_port}/api/integrations", timeout=3
            ) as response:
                self.assertEqual(response.status, HTTPStatus.OK)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
        self.assertEqual(
            recording.calls[-1],
            ("integration_status", (), {"consumer": "local"}),
        )

    def test_a_machine_viewer_receives_no_machine_layout_disclosure(self) -> None:
        leak_path = self.obsidian_target.name
        status, human_body = self._fetch("human")
        self.assertEqual(status, HTTPStatus.OK)
        # Control: the human payload really carries the layout the machine
        # assertions below are about, so absence there is filtering at work,
        # not a vacuously green test.
        self.assertIn(leak_path, human_body)
        self.assertIn("BRAINSKIT_TEST_PG_DSN", human_body)

        status, machine_body = self._fetch("local")
        self.assertEqual(status, HTTPStatus.OK)
        payload = json.loads(machine_body)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["result"].get("consumer"), "local")
        self.assertNotIn(leak_path, machine_body)
        self.assertNotIn("BRAINSKIT_TEST_PG_DSN", machine_body)


class RendererUnitTests(unittest.TestCase):
    """Each per-command renderer against the exact shape its service method
    returns -- confirmed by reading health.py, retrieval.py, filing.py,
    jobs.py and codegraph.py directly, not guessed. These pin content, not
    exact strings, per this repo's own "assert the outcome" philosophy.

    Every call runs under `redirect_stdout(io.StringIO())` even though a
    renderer only returns a string: `console.style` reads
    `sys.stdout.isatty()` at call time, and a StringIO is the same
    "definitely not a terminal" signal the rest of the suite relies on --
    it keeps these tests deterministic regardless of whether the run
    happens to have a real TTY attached.
    """

    def render(self, fn: Any, value: dict[str, Any]) -> str:
        with redirect_stdout(io.StringIO()):
            return fn(value)

    def test_status_reports_health_counts_and_branches(self) -> None:
        text = self.render(
            cli._render_status,
            {
                "vault": "/tmp/vault",
                "sources": 3,
                "pending": 1,
                "wiki_pages": 5,
                "by_branch": {"10-work": 2, "20-research": 1},
                "index": {"documents": 5, "updated_at": "2026-01-01T00:00:00+00:00"},
                "freshness": {"fresh": 3, "review": 1, "stale": 0, "unknown": 1},
                "projections": {
                    "views": {
                        "state": "fresh",
                        "stale": False,
                        "generated_at": "2026-01-01T00:00:00+00:00",
                    }
                },
                "enforcement": {
                    "layers": [
                        {
                            "layer": "write_gate",
                            "mechanism": "x",
                            "active": True,
                            "detail": "active",
                        }
                    ],
                    "inactive": [],
                    "gated": True,
                },
                "healthy": True,
                "lint_errors": 0,
            },
        )
        self.assertIn("/tmp/vault", text)
        self.assertIn("10-work", text)
        self.assertIn("write_gate", text)
        self.assertIn("vault healthy", text)

    def test_status_reports_unhealthy_with_a_lint_error_count(self) -> None:
        text = self.render(
            cli._render_status,
            {
                "vault": "/tmp/vault",
                "sources": 0,
                "pending": 0,
                "wiki_pages": 0,
                "by_branch": {},
                "index": {"documents": 0, "updated_at": None},
                "freshness": {},
                "projections": {},
                "enforcement": {"layers": [], "inactive": [], "gated": False},
                "healthy": False,
                "lint_errors": 2,
            },
        )
        self.assertIn("2 lint error(s)", text)

    def test_search_lists_hits_and_the_redacted_count(self) -> None:
        text = self.render(
            cli._render_search,
            {
                "query": "governed wiki",
                "consumer": "human",
                "redacted": 1,
                "count": 1,
                "hits": [
                    {
                        "path": "raw/10-work/note.md",
                        "kind": "raw",
                        "title": "note.md",
                        "excerpt": "a governed wiki",
                        "score": 1.246,
                        "content_hash": None,
                        "privacy": "local-only",
                    }
                ],
            },
        )
        self.assertIn("note.md", text)
        self.assertIn("1 redacted", text)

    def test_context_shows_citation_and_content_per_evidence_item(self) -> None:
        text = self.render(
            cli._render_context,
            {
                "contract_version": 1,
                "query": "q",
                "consumer": "human",
                "wiki_language": "English",
                "evidence": [
                    {
                        "citation": "source:abc123",
                        "path": "raw/10-work/note.md",
                        "kind": "raw",
                        "branches": ["10-work"],
                        "privacy": "local-only",
                        "content": "the actual evidence text",
                    }
                ],
                "redacted": 0,
                "apply_contract": {},
            },
        )
        self.assertIn("source:abc123", text)
        self.assertIn("the actual evidence text", text)

    def test_lint_reports_no_findings(self) -> None:
        text = self.render(cli._render_lint, {"ok": True, "findings": [], "semantic_report": None})
        self.assertIn("no lint findings", text)

    def test_lint_separates_errors_from_warnings(self) -> None:
        text = self.render(
            cli._render_lint,
            {
                "ok": False,
                "findings": [
                    {"code": "raw.content_modified", "severity": "error", "message": "boom", "path": "x"},
                    {"code": "views.stale", "severity": "warning", "message": "stale", "path": None},
                ],
                "semantic_report": None,
            },
        )
        self.assertIn("1 error(s)", text)
        self.assertIn("1 warning(s)", text)
        self.assertIn("raw.content_modified", text)

    def test_proposals_lists_id_branch_and_status(self) -> None:
        text = self.render(
            cli._render_proposals,
            {
                "count": 1,
                "proposals": [
                    {
                        "proposal_id": "p1",
                        "source_hash": "abc",
                        "destination_branch": "10-work",
                        "filing_mode": "approve-each",
                        "apply_proposal": {},
                        "reason": "why",
                        "status": "pending",
                        "created_at": "2026-01-01T00:00:00+00:00",
                        "decided_at": None,
                        "result": None,
                    }
                ],
            },
        )
        self.assertIn("p1", text)
        self.assertIn("10-work", text)
        self.assertIn("pending", text)

    def test_ingest_reports_queued_vs_applied(self) -> None:
        text = self.render(
            cli._render_ingest,
            {
                "ingested": 2,
                "results": [
                    {
                        "source": "abcdef1234567890",
                        "proposal": {"destination_branch": "10-work"},
                        "queued": True,
                    },
                    {
                        "source": "0987654321fedcba",
                        "proposal": {"destination_branch": "20-research"},
                        "apply": {},
                        "queued": False,
                    },
                ],
            },
        )
        self.assertIn("queued for approval", text)
        self.assertIn("applied", text)

    def test_approve_names_the_proposal(self) -> None:
        text = self.render(
            cli._render_approve,
            {"proposal": {"proposal_id": "p1"}, "apply": {}, "idempotent": False},
        )
        self.assertIn("p1", text)
        self.assertIn("applied", text)

    def test_reject_names_the_proposal(self) -> None:
        text = self.render(cli._render_reject, {"proposal": {"proposal_id": "p1"}})
        self.assertIn("p1", text)
        self.assertIn("rejected", text)

    def test_forget_warns_about_pages_still_citing_it(self) -> None:
        text = self.render(
            cli._render_forget,
            {
                "forgotten": {"path": "raw/10-work/note.md", "content_hash": "abc"},
                "still_cited_by": ["wiki/concepts/foo.md"],
            },
        )
        self.assertIn("raw/10-work/note.md", text)
        self.assertIn("still cited by 1 page(s)", text)
        self.assertIn("wiki/concepts/foo.md", text)

    def test_file_names_source_and_destination(self) -> None:
        text = self.render(
            cli._render_file,
            {"source": {"original_name": "note.md", "path": "raw/20-research/note.md"}},
        )
        self.assertIn("note.md", text)
        self.assertIn("raw/20-research/note.md", text)

    def test_ask_shows_answer_citations_uncertainty_and_saved_path(self) -> None:
        text = self.render(
            cli._render_ask,
            {
                "question": "q",
                "answer": "the answer",
                "citations": ["source:abc"],
                "uncertainty": "low confidence",
                "saved_to": "output/answers/q.md",
            },
        )
        self.assertIn("the answer", text)
        self.assertIn("source:abc", text)
        self.assertIn("low confidence", text)
        self.assertIn("output/answers/q.md", text)

    def test_digest_shows_markdown_actions_and_path(self) -> None:
        text = self.render(
            cli._render_digest,
            {
                "digest": "## Today",
                "actions": ["review the inbox"],
                "resurfaced": "concept:x",
                "path": "output/digests/2026-01-01.md",
            },
        )
        self.assertIn("## Today", text)
        self.assertIn("review the inbox", text)
        self.assertIn("output/digests/2026-01-01.md", text)

    def test_resurface_shows_markdown_and_page(self) -> None:
        text = self.render(
            cli._render_resurface,
            {
                "markdown": "## Revisit this",
                "page": "concept:durable-insight",
                "question": "why now",
                "path": "output/resurface/2026-01-01.md",
            },
        )
        self.assertIn("## Revisit this", text)
        self.assertIn("concept:durable-insight", text)
        self.assertIn("output/resurface/2026-01-01.md", text)

    def test_code_affected_lists_symbols_and_depth(self) -> None:
        text = self.render(
            cli._render_code_affected,
            {
                "symbol": "foo",
                "id": "mod:foo",
                "depth": 2,
                "consumer": "local",
                "count": 1,
                "affected": [
                    {"id": "mod:bar", "label": "bar", "path": "bar.py", "line": 10, "via": "calls", "depth": 1}
                ],
            },
        )
        self.assertIn("foo", text)
        self.assertIn("bar", text)
        self.assertIn("calls", text)

    def test_code_path_reports_hops_when_found(self) -> None:
        text = self.render(
            cli._render_code_path,
            {
                "found": True,
                "consumer": "local",
                "hops": 1,
                "path": [
                    {"id": "mod:a", "label": "a", "path": "a.py", "line": 1},
                    {"id": "mod:b", "label": "b", "path": "b.py", "line": 2, "via": "imports"},
                ],
            },
        )
        self.assertIn("1 hop(s)", text)
        self.assertIn("a", text)
        self.assertIn("imports", text)
        self.assertIn("b", text)

    def test_code_path_reports_not_found(self) -> None:
        text = self.render(
            cli._render_code_path,
            {"found": False, "from": "mod:a", "to": "mod:z", "consumer": "local"},
        )
        self.assertIn("no path", text)
        self.assertIn("mod:a", text)
        self.assertIn("mod:z", text)

    def test_code_hubs_lists_symbols_by_edge_count(self) -> None:
        text = self.render(
            cli._render_code_hubs,
            {
                "consumer": "local",
                "hubs": [{"id": "mod:a", "label": "a", "path": "a.py", "line": 1, "edges": 12}],
            },
        )
        self.assertIn("a", text)
        self.assertIn("12", text)

    def test_code_communities_lists_clusters(self) -> None:
        text = self.render(
            cli._render_code_communities,
            {
                "consumer": "local",
                "count": 1,
                "communities": [
                    {"id": 0, "label": "Community 0", "size": 4, "cohesion": 0.8123, "members": []}
                ],
            },
        )
        self.assertIn("Community 0", text)
        self.assertIn("0.812", text)

    def test_code_cycles_reports_none_found(self) -> None:
        text = self.render(cli._render_code_cycles, {"consumer": "local", "count": 0, "cycles": []})
        self.assertIn("0 import cycle(s)", text)

    def test_code_cycles_lists_the_cycle_chain(self) -> None:
        text = self.render(
            cli._render_code_cycles,
            {
                "consumer": "local",
                "count": 1,
                "cycles": [{"cycle": ["a.ts", "b.ts"], "length": 2, "why": "circular dependency"}],
            },
        )
        self.assertIn("a.ts", text)
        self.assertIn("b.ts", text)
        self.assertIn("circular dependency", text)

    def test_code_diff_reports_new_and_removed_nodes_and_edges(self) -> None:
        text = self.render(
            cli._render_code_diff,
            {
                "consumer": "local",
                "new_nodes": [{"id": "mod:c", "label": "c"}],
                "removed_nodes": [],
                "new_edges": [{"source": "mod:a", "target": "mod:c", "relation": "calls", "confidence": 0.9}],
                "removed_edges": [],
                "summary": "1 new node, 1 new edge",
            },
        )
        self.assertIn("1 new node, 1 new edge", text)
        self.assertIn("c", text)
        self.assertIn("calls", text)

    def test_init_shows_the_confirmation_and_next_steps(self) -> None:
        text = self.render(
            cli._render_init,
            {
                "vault": "/tmp/vault",
                "config": {"wiki_language": "English", "branches": {"10-work": {}}},
                "indexed_documents": 2,
                "views": ["views/home.md"],
            },
        )
        self.assertIn("/tmp/vault", text)
        self.assertIn("10-work", text)
        self.assertIn("bk status", text)
        self.assertIn("bk capture", text)

    def test_auto_render_flat_dict_as_a_panel(self) -> None:
        text = self.render(cli._render_auto, {"indexed_documents": 5})
        self.assertIn("indexed_documents", text)
        self.assertIn("5", text)

    def test_auto_render_scalars_plus_one_list_of_dicts_as_a_table(self) -> None:
        text = self.render(
            cli._render_auto,
            {
                "created": 1,
                "duplicates": 0,
                "ignored": 0,
                "failures": [{"path": "x.md", "error": "boom"}],
            },
        )
        self.assertIn("created", text)
        self.assertIn("x.md", text)
        self.assertIn("boom", text)

    def test_auto_render_scalars_plus_one_list_of_strings_as_bullets(self) -> None:
        text = self.render(cli._render_auto, {"written": ["views/home.md", "views/map/_inbox.md"]})
        self.assertIn(f"{console.BULLET} views/home.md", text)

    def test_auto_render_falls_back_to_json_for_two_lists(self) -> None:
        value = {"a": [1, 2], "b": [3, 4]}
        text = self.render(cli._render_auto, value)
        self.assertEqual(json.loads(text), value)

    def test_auto_render_falls_back_to_json_for_a_nested_dict(self) -> None:
        value = {"outer": {"inner": 1}}
        text = self.render(cli._render_auto, value)
        self.assertEqual(json.loads(text), value)


class GroupedHelpTests(unittest.TestCase):
    """Every command `build_parser()` registers has to land in exactly one
    `HELP_CATEGORIES` section -- not zero (silently unlisted) and not two
    (double-counted), so a future command can't go missing from `bk --help`
    without a test noticing.
    """

    def test_every_command_is_grouped_exactly_once(self) -> None:
        registered = set(cli._command_help_index(cli.build_parser()))
        counts: dict[str, int] = {}
        for _title, names in cli.HELP_CATEGORIES:
            for name in names:
                counts[name] = counts.get(name, 0) + 1
        self.assertEqual(registered, set(counts), "registered vs. grouped commands differ")
        self.assertEqual(
            {name for name, count in counts.items() if count != 1},
            set(),
            "a command appears in more than one HELP_CATEGORIES section",
        )

    def test_top_level_help_is_branded_and_grouped(self) -> None:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(["--help"])
        self.assertEqual(0, code)
        self.assertIn("brainskit", out.getvalue())
        self.assertIn("HugLabs", out.getvalue())
        self.assertIn("Vault & capture", out.getvalue())
        self.assertIn("Code graph", out.getvalue())
        self.assertEqual("", err.getvalue())

    def test_subcommand_help_is_left_to_argparse(self) -> None:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            with self.assertRaises(SystemExit) as exit_code:
                cli.main(["code", "--help"])
        self.assertEqual(0, exit_code.exception.code)
        self.assertIn("{build,import,status", out.getvalue())
        self.assertNotIn("HugLabs", out.getvalue())


if __name__ == "__main__":
    unittest.main()
