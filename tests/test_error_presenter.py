"""One error, one presentation, whichever surface answers.

`BrainskitError.code` exists so a caller can branch without parsing English,
and ADR 0002's taxonomy work sharpened those codes into five remedies. What no
surface had was a shared reading of them. `interfaces/mcp.py` carried two
`except` ladders that disagreed with each other -- one `ValidationError` was
JSON-RPC `-32000` on stdio and `-32600` over HTTP, and the HTTP one dropped
`code` and `details` from the payload entirely. `interfaces/web.py` carried the
same fifteen lines in its GET and POST handlers, both answering
`HTTPStatus.BAD_REQUEST` to every error there is, so a missing page and a
privacy refusal both arrived as bad requests. Only `interfaces/cli.py` turned a
`needs` list into a runnable install command.

These tests pin the table in `interfaces/errors.py` row by row, pin the three
surfaces to it, and fail when a new code is added without a row. See ADR 0006.
"""

from __future__ import annotations

import ast
import io
import json
import sys
import tempfile
import threading
import unittest
from collections.abc import Iterator
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from http import HTTPStatus
from pathlib import Path
from typing import Any, ClassVar
from unittest import mock
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from test_fix_interfaces import RecordingService, policy

from brainskit.application.services import BrainskitService
from brainskit.domain.model import (
    BrainskitError,
    ConflictError,
    ModelResponseError,
    NotConfiguredError,
    NotFoundError,
    PolicyError,
    RefusalError,
    ValidationError,
)
from brainskit.domain.privacy import Consumer
from brainskit.infrastructure.graph import MarkdownGraph
from brainskit.infrastructure.index import SqliteFtsIndex
from brainskit.infrastructure.vault import FileVault
from brainskit.interfaces import cli, errors, mcp, web
from brainskit.interfaces.errors import PRESENTATIONS, Presentation
from brainskit.interfaces.mcp import (
    MCP_PROTOCOL_VERSION,
    BrainskitMcpHttpHandler,
    BrainskitMcpHttpServer,
    JsonRpcRequestError,
    ProtocolVersionError,
    run_stdio,
)
from brainskit.interfaces.web import build_server

#: The whole contract, restated here rather than imported, so that editing the
#: table is a decision taken twice. Read as (exit status, HTTP status,
#: JSON-RPC code) -- the three columns, and the three surfaces.
EXPECTED: dict[str, tuple[int, int, int]] = {
    "brainskit_error": (2, 500, -32000),
    "validation_error": (2, 400, -32000),
    "conflict": (2, 409, -32000),
    "not_configured": (2, 501, -32000),
    "refused": (2, 403, -32000),
    "model_response_invalid": (2, 502, -32000),
    "not_found": (2, 404, -32000),
    "policy_denied": (3, 403, -32000),
    "internal_error": (2, 500, -32603),
    "jsonrpc_request_invalid": (2, 400, -32600),
    "protocol_version_invalid": (2, 400, -32600),
    "invalid_request": (2, 400, -32000),
}

#: Every error class a surface can catch, with the code it must present as.
CLASSES: dict[type[BrainskitError], str] = {
    BrainskitError: "brainskit_error",
    ValidationError: "validation_error",
    ConflictError: "conflict",
    NotConfiguredError: "not_configured",
    RefusalError: "refused",
    ModelResponseError: "model_response_invalid",
    NotFoundError: "not_found",
    PolicyError: "policy_denied",
    cli.InternalError: "internal_error",
    JsonRpcRequestError: "jsonrpc_request_invalid",
    ProtocolVersionError: "protocol_version_invalid",
}


class FailingService(RecordingService):
    """A service whose every call raises one chosen error."""

    def __init__(self, error: BaseException):
        super().__init__()
        self.error = error

    def __getattr__(self, name: str):
        def operation(*args: Any, **kwargs: Any) -> dict[str, Any]:
            self.calls.append((name, args, kwargs))
            raise self.error

        return operation


class ErrorTableTest(unittest.TestCase):
    """The table itself, row by row."""

    def test_every_row_reads_as_expected(self) -> None:
        for code, (exit_status, http, jsonrpc) in EXPECTED.items():
            with self.subTest(code=code):
                self.assertEqual(
                    PRESENTATIONS[code],
                    Presentation(exit_status, HTTPStatus(http), jsonrpc),
                )

    def test_the_table_has_no_rows_beyond_the_contract(self) -> None:
        self.assertEqual(sorted(PRESENTATIONS), sorted(EXPECTED))

    def test_every_class_presents_as_its_own_code(self) -> None:
        for error, code in CLASSES.items():
            with self.subTest(error=error.__name__):
                self.assertEqual(errors.error_code(error("boom")), code)
                self.assertEqual(errors.present(error("boom")), PRESENTATIONS[code])

    def test_an_exception_with_no_code_is_a_bad_request(self) -> None:
        """`interfaces/web.py` catches bare `ValueError` on purpose."""

        self.assertEqual(errors.error_code(ValueError("nope")), "invalid_request")
        self.assertEqual(
            errors.present(ValueError("nope")).http_status, HTTPStatus.BAD_REQUEST
        )

    def test_an_unknown_code_does_not_raise_inside_an_error_path(self) -> None:
        class Rogue(BrainskitError):
            code = "no_such_row"

        self.assertEqual(errors.present(Rogue("x")), PRESENTATIONS["brainskit_error"])


class ErrorTableHasOneOwnerTest(unittest.TestCase):
    """A new code without a row is the divergence this table exists to end.

    Same discipline as `ConstantsHaveOneOwnerTest` in `test_layering.py`: the
    guard is not that the two agree today, it is that they cannot silently
    stop agreeing. Every `code = "..."` class attribute in the tree is read out
    of the source, so adding a `BrainskitError` subclass and forgetting the
    presentation fails here rather than at whichever surface meets it first.
    """

    def codes_defined_in_the_tree(self) -> set[str]:
        root = Path(__file__).resolve().parents[1] / "src" / "brainskit"
        found: set[str] = set()
        for path in root.rglob("*.py"):
            if "codeanalysis" in path.parts:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.ClassDef):
                    continue
                for statement in node.body:
                    if (
                        isinstance(statement, ast.Assign)
                        and len(statement.targets) == 1
                        and isinstance(statement.targets[0], ast.Name)
                        and statement.targets[0].id == "code"
                        and isinstance(statement.value, ast.Constant)
                        and isinstance(statement.value.value, str)
                    ):
                        found.add(statement.value.value)
        return found

    def test_the_tree_defines_the_codes_the_table_expects(self) -> None:
        # `invalid_request` is the table's own row for an exception that
        # carries no code at all, so it is the one entry with no class.
        self.assertEqual(
            self.codes_defined_in_the_tree(),
            set(EXPECTED) - {"invalid_request"},
        )

    def test_every_code_in_the_tree_has_a_row(self) -> None:
        missing = sorted(self.codes_defined_in_the_tree() - set(PRESENTATIONS))
        self.assertEqual(missing, [], msg=f"no presentation for {missing}")


class CliExitStatusTest(unittest.TestCase):
    """`bk`'s exit statuses are a shipped contract and did not move.

    `ErrorExitCodeTest` in `test_fix_domain.py` pins the same claim from the
    domain side. This one drives every class the table names, including the
    two the CLI never raises itself, so the column cannot drift on a code that
    happens to have no CLI path today.
    """

    def status_for(self, error: BrainskitError) -> int:
        buffer = io.StringIO()
        with (
            mock.patch.object(cli, "_dispatch", side_effect=error),
            redirect_stderr(buffer),
        ):
            return cli.main(["status"])

    def test_only_a_policy_denial_exits_three(self) -> None:
        for error, code in CLASSES.items():
            with self.subTest(error=error.__name__):
                self.assertEqual(self.status_for(error("boom")), EXPECTED[code][0])

    def test_a_wrapped_os_error_still_exits_two(self) -> None:
        self.assertEqual(self.status_for(OSError("disk gone")), 2)  # type: ignore[arg-type]


@contextmanager
def serving(service: Any, *, consumer: str = "human") -> Iterator[int]:
    """A viewer bound to an ephemeral port, yielding that port."""

    server = build_server(service, host="127.0.0.1", port=0, consumer=consumer)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_port
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def fetch(
    port: int, path: str, body: dict[str, Any] | None = None, **headers: str
) -> tuple[int, dict[str, Any]]:
    request = Request(
        f"http://127.0.0.1:{port}{path}",
        data=None if body is None else json.dumps(body).encode(),
        headers={"Content-Type": "application/json", **headers},
        method="GET" if body is None else "POST",
    )
    try:
        with urlopen(request, timeout=3) as response:
            return response.status, json.loads(response.read())
    except HTTPError as error:
        with error:
            return error.code, json.loads(error.read())


class WebErrorStatusTest(unittest.TestCase):
    """The viewer's HTTP status is the answer, so it has to be the right one."""

    def test_every_code_answers_with_its_own_status_on_get(self) -> None:
        for error, code in CLASSES.items():
            with (
                self.subTest(error=error.__name__),
                serving(FailingService(error("boom"))) as port,
            ):
                status, body = fetch(port, "/api/status")
                self.assertEqual(body["error"]["code"], code)
                self.assertEqual(status, EXPECTED[code][1])

    def test_the_post_handler_answers_the_same_way(self) -> None:
        """It used to be a second copy of the block, and copies drift."""

        for error, code in CLASSES.items():
            with (
                self.subTest(error=error.__name__),
                serving(FailingService(error("boom"))) as port,
            ):
                status, body = fetch(port, "/api/ask", {"question": "why"})
                self.assertEqual(body["error"]["code"], code)
                self.assertEqual(status, EXPECTED[code][1])

    def test_an_unparsable_query_parameter_is_still_a_bad_request(self) -> None:
        with serving(RecordingService()) as port:
            status, body = fetch(port, "/api/graph?limit=many")
        self.assertEqual(status, HTTPStatus.BAD_REQUEST)
        self.assertEqual(body["error"]["code"], "invalid_request")

    def test_the_guards_keep_their_own_statuses_and_shape(self) -> None:
        """A refusal names a rule, not a code -- and carries nothing else."""

        with serving(RecordingService()) as port:
            missing = fetch(port, "/api/nothing-here")
            denied = fetch(port, "/api/status", Host="attacker.example")
        self.assertEqual(missing[0], HTTPStatus.NOT_FOUND)
        self.assertEqual(missing[1], {"ok": False, "error": {"code": "not_found"}})
        self.assertEqual(denied[0], HTTPStatus.FORBIDDEN)
        self.assertEqual(denied[1], {"ok": False, "error": {"code": "host_denied"}})

    def test_a_machine_viewer_still_refuses_writes_with_its_own_message(self) -> None:
        with serving(RecordingService(), consumer="local") as port:
            status, body = fetch(port, "/api/ask", {"question": "why"})
        self.assertEqual(status, HTTPStatus.FORBIDDEN)
        self.assertEqual(body["error"]["code"], "writes_refused")
        self.assertEqual(body["error"]["details"], {"consumer": "local"})
        self.assertIn("--consumer human", body["error"]["message"])


class WebRealNotFoundTest(unittest.TestCase):
    """The defect, end to end, against a real vault rather than a stub."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        vault = FileVault.initialize(Path(self.temporary.name), policy())
        service = BrainskitService(
            vault, SqliteFtsIndex(vault.index_path), graph=MarkdownGraph()
        )
        service.reindex()
        self.server = build_server(service, host="127.0.0.1", port=0, consumer="human")
        self.port = self.server.server_port
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temporary.cleanup()

    def test_a_missing_resource_is_not_found(self) -> None:
        request = Request(
            f"http://127.0.0.1:{self.port}/api/resource?id=page:wiki/nope.md"
        )
        with self.assertRaises(HTTPError) as missing:
            urlopen(request, timeout=3)
        with missing.exception as error:
            self.assertEqual(error.code, HTTPStatus.NOT_FOUND)
            self.assertEqual(json.loads(error.read())["error"]["code"], "not_found")


class McpTransportParityTest(unittest.TestCase):
    """One error must not read differently depending on the transport."""

    #: A well-formed JSON-RPC request whose *tool name* does not exist, so
    #: `_call_tool` raises a plain `ValidationError` on both transports.
    REQUEST: ClassVar[dict[str, Any]] = {
        "jsonrpc": "2.0",
        "id": 11,
        "method": "tools/call",
        "params": {"name": "does-not-exist", "arguments": {}},
    }

    def setUp(self) -> None:
        self.service = RecordingService()
        self.server = BrainskitMcpHttpServer(("127.0.0.1", 0), BrainskitMcpHttpHandler)
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

    def over_stdio(self, service: Any, request: dict[str, Any]) -> dict[str, Any]:
        stdin = io.StringIO(json.dumps(request) + "\n")
        out, err = io.StringIO(), io.StringIO()
        original = sys.stdin
        sys.stdin = stdin
        try:
            with redirect_stdout(out), redirect_stderr(err):
                run_stdio(service)
        finally:
            sys.stdin = original
        return json.loads(out.getvalue().splitlines()[0])

    def over_http(self, request: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        message = Request(
            f"{self.base_url}/mcp",
            data=json.dumps(request).encode(),
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
                "Authorization": "Bearer test-secret",
                "MCP-Protocol-Version": MCP_PROTOCOL_VERSION,
            },
            method="POST",
        )
        try:
            with urlopen(message, timeout=5) as response:
                return response.status, json.loads(response.read())
        except HTTPError as error:
            with error:
                return error.code, json.loads(error.read())

    def test_both_transports_report_the_same_jsonrpc_code(self) -> None:
        stdio = self.over_stdio(self.service, self.REQUEST)
        _, http = self.over_http(self.REQUEST)
        self.assertEqual(stdio["error"]["code"], -32000)
        self.assertEqual(http["error"]["code"], -32000)

    def test_both_transports_carry_the_machine_readable_code(self) -> None:
        stdio = self.over_stdio(self.service, self.REQUEST)
        _, http = self.over_http(self.REQUEST)
        self.assertEqual(stdio["error"]["data"]["code"], "validation_error")
        self.assertEqual(http["error"]["data"]["code"], "validation_error")

    def test_every_error_class_agrees_across_the_two_transports(self) -> None:
        ping = {
            "jsonrpc": "2.0",
            "id": 12,
            "method": "tools/call",
            "params": {"name": "status", "arguments": {}},
        }
        for error, code in CLASSES.items():
            if code in {"jsonrpc_request_invalid", "protocol_version_invalid"}:
                # The two classes the transport raises about itself, never
                # about a call. No service can produce one, so there is no
                # cross-transport claim here to make.
                continue
            with self.subTest(error=error.__name__):
                service = FailingService(error("boom"))
                self.server.service = service  # type: ignore[assignment]
                stdio = self.over_stdio(service, ping)
                status, http = self.over_http(ping)
                self.assertEqual(stdio["error"]["code"], EXPECTED[code][2])
                self.assertEqual(http["error"]["code"], EXPECTED[code][2])
                self.assertEqual(stdio["error"]["data"]["code"], code)
                self.assertEqual(http["error"]["data"]["code"], code)
                # Anything that reached the dispatcher is a successful HTTP
                # exchange carrying a JSON-RPC error, per the Streamable HTTP
                # spec -- the status column is not this transport's to use.
                self.assertEqual(status, HTTPStatus.OK)

    def test_a_transport_contract_failure_is_the_one_hard_http_error(self) -> None:
        message = Request(
            f"{self.base_url}/mcp",
            data=json.dumps({"jsonrpc": "1.0", "id": 13, "method": "ping"}).encode(),
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
                "Authorization": "Bearer test-secret",
                "MCP-Protocol-Version": MCP_PROTOCOL_VERSION,
            },
            method="POST",
        )
        with self.assertRaises(HTTPError) as refused:
            urlopen(message, timeout=5)
        with refused.exception as error:
            self.assertEqual(error.code, HTTPStatus.BAD_REQUEST)
            body = json.loads(error.read())
        self.assertEqual(body["error"]["code"], -32600)
        self.assertEqual(body["error"]["data"]["code"], "jsonrpc_request_invalid")


class InstallHintReachesEverySurfaceTest(unittest.TestCase):
    """`needs` becomes a runnable command on all three surfaces, not just one.

    The application layer names what is missing and stops there, because which
    command installs it is a fact about the running interpreter. While the
    enrichment lived in `interfaces/cli.py`, an MCP or web caller received the
    bare `{"needs": ["brainskit[code]"]}` and no command at all.
    """

    ERROR = NotConfiguredError(
        "The code graph needs the code extra",
        details={"needs": ["brainskit[code]"]},
    )

    def test_the_cli_still_enriches(self) -> None:
        out, err = io.StringIO(), io.StringIO()
        with (
            mock.patch.object(cli, "_dispatch", side_effect=self.ERROR),
            redirect_stdout(out),
            redirect_stderr(err),
        ):
            cli.main(["--json", "status"])
        payload = json.loads(out.getvalue() or err.getvalue())
        self.assertIn("brainskit[code]", payload["error"]["details"]["hint"])

    def test_the_web_viewer_enriches_too(self) -> None:
        with serving(FailingService(self.ERROR)) as port:
            status, body = fetch(port, "/api/status")
        self.assertEqual(status, HTTPStatus.NOT_IMPLEMENTED)
        self.assertIn("brainskit[code]", body["error"]["details"]["hint"])

    def test_mcp_enriches_too(self) -> None:
        service = FailingService(self.ERROR)
        request = {
            "jsonrpc": "2.0",
            "id": 14,
            "method": "tools/call",
            "params": {"name": "status", "arguments": {}},
        }
        stdin = io.StringIO(json.dumps(request) + "\n")
        out, err = io.StringIO(), io.StringIO()
        original = sys.stdin
        sys.stdin = stdin
        try:
            with redirect_stdout(out), redirect_stderr(err):
                run_stdio(service)
        finally:
            sys.stdin = original
        payload = json.loads(out.getvalue().splitlines()[0])
        self.assertIn("brainskit[code]", payload["error"]["data"]["hint"])


class ConsumerDefaultTest(unittest.TestCase):
    """The interface layer's half of the `--consumer` default, in one function.

    `Consumer.parse` is the domain-side answer (ADR 0001) and is untouched.
    What was scattered is which boundary an *unnamed* read runs under, decided
    at six sites: `_consumer_for_args`, two `args.consumer or "local"` (one of
    them next to an `argparse` `default="local"` that already said it), MCP's
    three hardcoded `"local"`, and the viewer's own list of the three names.
    """

    def args(self, **overrides: Any) -> Any:
        import argparse

        namespace = argparse.Namespace(consumer=None, json=False)
        for key, value in overrides.items():
            setattr(namespace, key, value)
        return namespace

    def test_an_interactive_read_defaults_to_human(self) -> None:
        self.assertEqual(cli._consumer_for_args(self.args()), "human")

    def test_a_machine_caller_must_declare_a_boundary(self) -> None:
        with self.assertRaises(ValidationError) as refused:
            cli._consumer_for_args(self.args(json=True))
        self.assertEqual(
            refused.exception.details["choices"], ["human", "local", "cloud"]
        )

    def test_a_file_target_defaults_to_local_and_never_refuses(self) -> None:
        """An export runs unattended; `human` would withhold nothing."""

        for namespace in (self.args(), self.args(json=True)):
            self.assertEqual(
                cli._consumer_for_args(namespace, default=Consumer.LOCAL), "local"
            )

    def test_an_explicit_consumer_wins_under_either_default(self) -> None:
        for default in (Consumer.HUMAN, Consumer.LOCAL):
            with self.subTest(default=default):
                self.assertEqual(
                    cli._consumer_for_args(
                        self.args(consumer="cloud", json=True), default=default
                    ),
                    "cloud",
                )

    def test_the_three_names_are_spelled_once_per_surface(self) -> None:
        """Derived from the enum, not restated beside it."""

        values = [consumer.value for consumer in Consumer]
        self.assertEqual(cli.CONSUMER_CHOICES, values)
        self.assertEqual(mcp._consumer_schema()["enum"], values)
        self.assertEqual(mcp.MCP_CONSUMER, Consumer.LOCAL.value)
        source = Path(cli.__file__).read_text(encoding="utf-8")
        self.assertNotIn('choices=["human", "local", "cloud"]', source)
        for module in (cli, mcp, web):
            with self.subTest(module=module.__name__):
                self.assertNotIn(
                    '"human", "local", "cloud"',
                    Path(module.__file__).read_text(encoding="utf-8"),
                )

    def test_the_viewer_still_refuses_an_unknown_consumer(self) -> None:
        with self.assertRaises(ValidationError) as refused:
            build_server(RecordingService(), host="127.0.0.1", port=0, consumer="nope")
        self.assertEqual(refused.exception.details["consumer"], "nope")


if __name__ == "__main__":
    unittest.main()
