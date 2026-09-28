from __future__ import annotations

import hmac
import json
import os
import re
import ssl
import sys
import traceback
from collections.abc import Callable
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlparse

from brainskit import __version__
from brainskit.application.services import BrainskitService
from brainskit.domain.model import (
    BrainskitError,
    NotConfiguredError,
    PolicyError,
    PrivacyMode,
    ValidationError,
)
from brainskit.domain.privacy import Consumer
from brainskit.interfaces.errors import (
    JSONRPC_INTERNAL_ERROR,
    JSONRPC_INVALID_REQUEST,
    jsonrpc_error_data,
    present,
    refusal_envelope,
    succeeded,
)

#: The server's declared consumer when the operator names none (ADR 0010). MCP
#: hands its answers to a model, and nothing on this side of the pipe can see
#: where that model runs, so the default is the one boundary that is safe to
#: forward anywhere. `--consumer local` is the operator saying otherwise.
MCP_DEFAULT_CONSUMER = Consumer.CLOUD


def _narrows(requested: Consumer, ceiling: Consumer) -> bool:
    """Whether `requested` sees nothing `ceiling` does not.

    Derived from `Consumer.allows` rather than a rank table beside it, so the
    lattice is stated once, in the domain.
    """

    return all(
        ceiling.allows(mode) for mode in PrivacyMode if requested.allows(mode)
    ) and (ceiling.sees_installation() or not requested.sees_installation())


#: The consumers an MCP server may be declared as: everything within `local`.
#: `human` is not among them -- it withholds nothing, never-ingest included,
#: and a model-facing transport is never the reader it was meant for.
MCP_CONSUMERS = tuple(
    consumer for consumer in Consumer if _narrows(consumer, Consumer.LOCAL)
)


def server_consumer(value: str | Consumer | None) -> Consumer:
    """The ceiling an MCP server answers under, or the refusal to serve at all."""

    if value is None:
        return MCP_DEFAULT_CONSUMER
    parsed = Consumer.parse(value)
    if parsed not in MCP_CONSUMERS:
        raise PolicyError(
            "MCP serves a model, and a model is never given the unrestricted "
            "human scope",
            details={
                "consumer": parsed.value,
                "allowed": [consumer.value for consumer in MCP_CONSUMERS],
                "hint": (
                    "Serve with --consumer local for an agent running on this "
                    "machine, or --consumer cloud (the default) for anything "
                    "that may forward results off it"
                ),
            },
        )
    return parsed

MCP_PROTOCOL_VERSION = "2025-06-18"
MCP_SUPPORTED_VERSIONS = {MCP_PROTOCOL_VERSION}
MCP_MAX_REQUEST_BYTES = 1_048_576
MCP_INTERNAL_ERROR = JSONRPC_INTERNAL_ERROR
_MAX_DISCARDED_BYTES = 4 * MCP_MAX_REQUEST_BYTES
_MAX_REASON_CHARS = 200
_FILESYSTEM_PATH = re.compile(r"(?:/[^\s'\"]+){2,}")


class JsonRpcRequestError(ValidationError):
    """The body parsed, and it is not a JSON-RPC request this server accepts.

    Distinct from every other `ValidationError` because it is a statement about
    the *envelope*, not about the call inside it -- which is precisely what
    JSON-RPC's `-32600` means, and precisely what a tool failure is not. The
    HTTP transport already answers `400` to a body it cannot parse ("a
    malformed HTTP request, not a successful call that happens to carry an
    error"); a body that parses but is not a request is the same claim.
    """

    code = "jsonrpc_request_invalid"


class ProtocolVersionError(JsonRpcRequestError):
    """MCP-Protocol-Version failure that must surface as HTTP 400.

    A `JsonRpcRequestError` so the transport needs one branch rather than two:
    the Streamable HTTP spec requires the hard 400 here, and the table in
    `interfaces/errors.py` gives both codes the same row for the same reason.
    """

    code = "protocol_version_invalid"


def run_stdio(
    service: BrainskitService, *, consumer: str | Consumer | None = None
) -> None:
    """Minimal MCP JSON-RPC stdio transport; stdout is protocol-only."""

    ceiling = server_consumer(consumer)
    for line in sys.stdin:
        if not line.strip():
            continue
        request: dict[str, Any] | None = None
        try:
            request = json.loads(line)
            response = _handle(service, request, ceiling)
        except (json.JSONDecodeError, TypeError) as exc:
            response = _error(
                request.get("id") if isinstance(request, dict) else None,
                -32700,
                "Parse error",
                {"reason": str(exc)},
            )
        except BrainskitError as exc:
            response = _error(
                request.get("id") if isinstance(request, dict) else None,
                present(exc).jsonrpc_code,
                str(exc),
                jsonrpc_error_data(exc),
            )
        except Exception as exc:
            _report_internal_error("stdio request", exc)
            response = _error(
                request.get("id") if isinstance(request, dict) else None,
                MCP_INTERNAL_ERROR,
                "Internal error",
                {"reason": _safe_reason(exc)},
            )
        if response is not None:
            sys.stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
            sys.stdout.flush()


def run_http(
    service: BrainskitService,
    *,
    host: str,
    port: int,
    token_env: str,
    allowed_origins: list[str] | None = None,
    tls_cert: str | None = None,
    tls_key: str | None = None,
    consumer: str | Consumer | None = None,
) -> None:
    """Serve stateless MCP Streamable HTTP with pre-shared Bearer auth."""

    ceiling = server_consumer(consumer)
    if not 1 <= port <= 65535:
        raise ValidationError("MCP HTTP port must be between 1 and 65535")
    token = os.environ.get(token_env) if token_env else None
    if not token:
        raise NotConfiguredError(
            "MCP HTTP requires a populated token environment variable",
            details={"environment": token_env},
        )
    loopback = host in {"127.0.0.1", "localhost", "::1"}
    if not loopback and not (tls_cert and tls_key):
        raise ValidationError(
            "Non-loopback MCP HTTP requires --tls-cert and --tls-key"
        )
    if bool(tls_cert) != bool(tls_key):
        raise ValidationError("MCP TLS certificate and key must be provided together")
    scheme = "https" if tls_cert else "http"
    defaults = {
        f"{scheme}://127.0.0.1:{port}",
        f"{scheme}://localhost:{port}",
    }
    server = BrainskitMcpHttpServer((host, port), BrainskitMcpHttpHandler)
    server.service = service
    server.consumer = ceiling
    server.token = token
    server.allowed_origins = set(allowed_origins or defaults)
    if tls_cert and tls_key:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(tls_cert, tls_key)
        server.socket = context.wrap_socket(server.socket, server_side=True)
    try:
        server.serve_forever(poll_interval=0.25)
    finally:
        server.server_close()


class BrainskitMcpHttpServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    service: BrainskitService
    consumer: Consumer = MCP_DEFAULT_CONSUMER
    token: str
    allowed_origins: set[str]


class BrainskitMcpHttpHandler(BaseHTTPRequestHandler):
    server: BrainskitMcpHttpServer

    def do_POST(self) -> None:
        if urlparse(self.path).path != "/mcp":
            self._send_http_error(HTTPStatus.NOT_FOUND, "not_found")
            return
        if not self._authorized():
            self._send_http_error(
                HTTPStatus.UNAUTHORIZED,
                "unauthorized",
                authenticate='Bearer realm="brainskit-mcp"',
            )
            return
        if not self._origin_allowed():
            self._send_http_error(HTTPStatus.FORBIDDEN, "origin_denied")
            return
        content_type = self.headers.get("Content-Type", "").split(";", 1)[0]
        if content_type != "application/json":
            self._send_http_error(
                HTTPStatus.UNSUPPORTED_MEDIA_TYPE, "content_type_invalid"
            )
            return
        accept = self.headers.get("Accept", "")
        if "application/json" not in accept or "text/event-stream" not in accept:
            self._send_http_error(HTTPStatus.NOT_ACCEPTABLE, "accept_invalid")
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if length > MCP_MAX_REQUEST_BYTES:
            self._discard_body(length)
            self._send_http_error(
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "body_too_large"
            )
            return
        if length < 1:
            self._send_http_error(HTTPStatus.BAD_REQUEST, "body_size_invalid")
            return
        raw = self.rfile.read(length)
        try:
            parsed = json.loads(raw)
            if not isinstance(parsed, dict):
                raise TypeError("MCP request must be a JSON object")
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError) as exc:
            # A body brainskit cannot even parse is a malformed HTTP request,
            # not a successful call that happens to carry an error.
            self._send_json(
                _error(
                    None,
                    JSONRPC_INVALID_REQUEST,
                    "Invalid Request",
                    {"reason": str(exc)},
                ),
                status=HTTPStatus.BAD_REQUEST,
            )
            return
        request: dict[str, Any] = parsed
        status = HTTPStatus.OK
        try:
            self._validate_http_contract(request)
            response = _handle(self.server.service, request, self.server.consumer)
        except JsonRpcRequestError as exc:
            # The one family that is an HTTP failure as well as a JSON-RPC one:
            # the Streamable HTTP transport requires a hard 400 so a conforming
            # client cannot read a version mismatch as success.
            presentation = present(exc)
            status = presentation.http_status
            response = _error(
                request.get("id"),
                presentation.jsonrpc_code,
                str(exc),
                jsonrpc_error_data(exc),
            )
        except (json.JSONDecodeError, TypeError) as exc:
            response = _error(
                request.get("id"),
                JSONRPC_INVALID_REQUEST,
                "Invalid Request",
                {"reason": str(exc)},
            )
        except BrainskitError as exc:
            # Everything that reached the dispatcher answers 200 with a
            # JSON-RPC error inside, per the Streamable HTTP spec -- and now
            # with the same code and the same `data` the stdio transport sends.
            response = _error(
                request.get("id"),
                present(exc).jsonrpc_code,
                str(exc),
                jsonrpc_error_data(exc),
            )
        except Exception as exc:
            _report_internal_error("http request", exc)
            status = HTTPStatus.INTERNAL_SERVER_ERROR
            response = _error(
                request.get("id"),
                MCP_INTERNAL_ERROR,
                "Internal error",
                {"reason": _safe_reason(exc)},
            )
        if response is None:
            self.send_response(HTTPStatus.ACCEPTED.value)
            self._security_headers()
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self._send_json(response, status=status)

    def do_GET(self) -> None:
        if urlparse(self.path).path != "/mcp":
            self._send_http_error(HTTPStatus.NOT_FOUND, "not_found")
            return
        if not self._authorized():
            self._send_http_error(
                HTTPStatus.UNAUTHORIZED,
                "unauthorized",
                authenticate='Bearer realm="brainskit-mcp"',
            )
            return
        if not self._origin_allowed():
            self._send_http_error(HTTPStatus.FORBIDDEN, "origin_denied")
            return
        self._send_http_error(
            HTTPStatus.METHOD_NOT_ALLOWED,
            "sse_not_supported",
            allow="POST",
        )

    def do_DELETE(self) -> None:
        if urlparse(self.path).path != "/mcp":
            self._send_http_error(HTTPStatus.NOT_FOUND, "not_found")
            return
        if not self._authorized():
            self._send_http_error(
                HTTPStatus.UNAUTHORIZED,
                "unauthorized",
                authenticate='Bearer realm="brainskit-mcp"',
            )
            return
        if not self._origin_allowed():
            self._send_http_error(HTTPStatus.FORBIDDEN, "origin_denied")
            return
        self._send_http_error(
            HTTPStatus.METHOD_NOT_ALLOWED,
            "sessions_not_enabled",
            allow="POST",
        )

    # `format` is BaseHTTPRequestHandler's parameter name, not ours.
    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        return

    def _authorized(self) -> bool:
        supplied = self.headers.get("Authorization", "")
        expected = f"Bearer {self.server.token}"
        return hmac.compare_digest(supplied, expected)

    def _origin_allowed(self) -> bool:
        origin = self.headers.get("Origin")
        return origin is None or origin in self.server.allowed_origins

    def _validate_http_contract(self, request: dict[str, Any]) -> None:
        if request.get("jsonrpc") != "2.0" or not isinstance(
            request.get("method"), str
        ):
            raise JsonRpcRequestError("MCP requires JSON-RPC 2.0 and a method")
        method = str(request["method"])
        if method != "initialize":
            version = self.headers.get("MCP-Protocol-Version")
            if version not in MCP_SUPPORTED_VERSIONS:
                raise ProtocolVersionError(
                    "MCP-Protocol-Version is missing or unsupported",
                    details={"supported": sorted(MCP_SUPPORTED_VERSIONS)},
                )
        mirrored_method = self.headers.get("Mcp-Method")
        if mirrored_method and mirrored_method != method:
            raise JsonRpcRequestError(
                "Mcp-Method does not match the JSON-RPC method"
            )
        mirrored_name = self.headers.get("Mcp-Name")
        params = request.get("params")
        if mirrored_name and isinstance(params, dict):
            expected_name = params.get("name", params.get("uri"))
            if expected_name is not None and mirrored_name != str(expected_name):
                raise JsonRpcRequestError(
                    "Mcp-Name does not match the JSON-RPC parameters"
                )

    def _discard_body(self, length: int) -> None:
        """Drain a bounded prefix so the rejection reaches the client."""

        self.close_connection = True
        remaining = min(length, _MAX_DISCARDED_BYTES)
        while remaining > 0:
            chunk = self.rfile.read(min(remaining, 65_536))
            if not chunk:
                return
            remaining -= len(chunk)

    def _send_json(
        self, value: dict[str, Any], *, status: HTTPStatus = HTTPStatus.OK
    ) -> None:
        body = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status.value)
        self._security_headers()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_http_error(
        self,
        status: HTTPStatus,
        code: str,
        *,
        authenticate: str | None = None,
        allow: str | None = None,
    ) -> None:
        body = json.dumps(refusal_envelope(code)).encode()
        self.send_response(status.value)
        self._security_headers()
        if authenticate:
            self.send_header("WWW-Authenticate", authenticate)
        if allow:
            self.send_header("Allow", allow)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _security_headers(self) -> None:
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")


def _safe_reason(exc: BaseException) -> str:
    """Short, path-free description safe to hand back to a remote caller."""

    message = _FILESYSTEM_PATH.sub("<path>", str(exc)).strip()
    if len(message) > _MAX_REASON_CHARS:
        message = message[:_MAX_REASON_CHARS].rstrip() + "…"
    name = type(exc).__name__
    return f"{name}: {message}" if message else name


def _report_internal_error(context: str, exc: BaseException) -> None:
    """Keep the verbose diagnosis on stderr where an operator can read it."""

    print(f"brainskit mcp: unhandled error during {context}", file=sys.stderr)
    traceback.print_exception(exc, file=sys.stderr)
    sys.stderr.flush()


def _handle(
    service: BrainskitService,
    request: dict[str, Any],
    consumer: str | Consumer | None = None,
) -> dict[str, Any] | None:
    ceiling = server_consumer(consumer)
    method = request.get("method")
    request_id = request.get("id")
    params = request.get("params") or {}
    if method and method.startswith("notifications/"):
        return None
    if method == "initialize":
        requested_version = str(params.get("protocolVersion", MCP_PROTOCOL_VERSION))
        negotiated_version = (
            requested_version
            if requested_version in MCP_SUPPORTED_VERSIONS
            else MCP_PROTOCOL_VERSION
        )
        return _result(
            request_id,
            {
                "protocolVersion": negotiated_version,
                "capabilities": {"tools": {}, "resources": {}},
                "serverInfo": {"name": "brainskit", "version": __version__},
            },
        )
    if method == "ping":
        return _result(request_id, {})
    if method == "tools/list":
        return _result(request_id, {"tools": _tool_definitions(ceiling)})
    if method == "tools/call":
        name = params.get("name")
        arguments = params.get("arguments") or {}
        if not isinstance(name, str):
            raise ValidationError("MCP tool name must be a string")
        if not isinstance(arguments, dict):
            raise ValidationError(
                "MCP tool arguments must be a JSON object",
                details={"tool": name, "received": _json_type(arguments)},
            )
        value = _call_tool(service, name, arguments, ceiling)
        return _result(
            request_id,
            {
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps(value, ensure_ascii=False),
                    }
                ],
                "structuredContent": value,
                "isError": not succeeded(name, None, value),
            },
        )
    if method == "resources/list":
        readable_pages = sorted(
            str(node["path"])
            for node in service.graph_data(consumer=ceiling.value)["nodes"]
            if str(node["id"]).startswith("page:")
        )
        return _result(
            request_id,
            {
                "resources": [
                    {
                        "uri": f"brainskit://vault/{path}",
                        "name": path,
                        "mimeType": "text/markdown",
                    }
                    for path in readable_pages
                ]
            },
        )
    if method == "resources/read":
        uri = str(params.get("uri", ""))
        prefix = "brainskit://vault/"
        if not uri.startswith(prefix):
            raise ValidationError("Unknown resource URI", details={"uri": uri})
        path = uri[len(prefix) :]
        resource = service.read_resource(f"page:{path}", consumer=ceiling.value)
        return _result(
            request_id,
            {
                "contents": [
                    {
                        "uri": uri,
                        "mimeType": "text/markdown",
                        "text": resource["content"],
                    }
                ]
            },
        )
    return _error(request_id, -32601, "Method not found", {"method": method})


def _call_tool(
    service: BrainskitService,
    name: str,
    arguments: dict[str, Any],
    consumer: str | Consumer | None = None,
) -> dict[str, Any]:
    ceiling = server_consumer(consumer)
    scope = ceiling.value
    tools: dict[str, Callable[[], dict[str, Any]]] = {
        "capture": lambda: service.capture(
            arguments.get("source"),
            text=arguments.get("text"),
            title=arguments.get("title"),
            confined=True,
        ),
        "search": lambda: service.search(
            str(arguments["query"]),
            _integer(arguments, "limit", 10),
            consumer=_requested_consumer(arguments, ceiling),
        ),
        "context": lambda: service.context(
            str(arguments["query"]),
            limit=_integer(arguments, "limit", 8),
            max_chars=_integer(arguments, "max_chars", 24_000),
            consumer=_requested_consumer(arguments, ceiling),
        ),
        "apply": lambda: service.apply(arguments["proposal"]),
        "file": lambda: service.file(
            str(arguments["item"]), str(arguments["branch"]), consumer=scope
        ),
        "ask": lambda: service.ask(
            str(arguments["question"]),
            save=_flag(arguments, "save"),
            consumer=scope,
        ),
        "proposals": lambda: service.proposals_for_consumer(
            arguments.get("status"), consumer=scope
        ),
        "approve": lambda: service.approve(
            str(arguments["proposal_id"]), consumer=scope
        ),
        "reject": lambda: service.reject(
            str(arguments["proposal_id"]),
            str(arguments.get("reason", "")),
            consumer=scope,
        ),
        "resurface": lambda: service.resurface(consumer=scope),
        "status": lambda: service.reader_status(consumer=scope),
        "lint": lambda: service.lint(
            semantic=_flag(arguments, "semantic"), consumer=scope
        ),
        "integration_configure": lambda: service.integration_configure(
            str(arguments["name"]),
            enabled=_optional_flag(arguments, "enabled"),
            managed=_optional_flag(arguments, "managed"),
            options=_integration_options(arguments, ceiling),
            consumer=scope,
        ),
        "integration_status": lambda: service.integration_status(
            str(arguments["name"]) if arguments.get("name") else None,
            consumer=scope,
        ),
        "integration_up": lambda: service.integration_up(
            str(arguments["name"]), consumer=scope
        ),
        "integration_down": lambda: service.integration_down(
            str(arguments["name"]), consumer=scope
        ),
        "integration_sync": lambda: service.integration_sync(
            str(arguments["name"]), consumer=scope
        ),
    }
    operation = tools.get(str(name))
    if not operation:
        raise ValidationError("Unknown MCP tool", details={"tool": name})
    try:
        return operation()
    except KeyError as exc:
        raise ValidationError(
            "MCP tool argument is missing",
            details={"tool": name, "argument": str(exc)},
        ) from exc


def _requested_consumer(arguments: dict[str, Any], ceiling: Consumer) -> str:
    """The caller's declared consumer, if it is within the server's.

    Refused rather than clamped: a clamped `local` would come back as a cloud
    answer the caller believes is local, and nothing in the result would say
    evidence was missing for that reason rather than because none matched.
    """

    requested = Consumer.parse(str(arguments["consumer"]))
    if not _narrows(requested, ceiling):
        raise PolicyError(
            "This MCP server answers no wider than its declared consumer",
            details={
                "requested": requested.value,
                "server_consumer": ceiling.value,
                "allowed": _allowed_under(ceiling),
            },
        )
    return requested.value


def _integration_options(
    arguments: dict[str, Any], ceiling: Consumer
) -> dict[str, Any]:
    """Options for `integration_configure`, never naming a wider consumer.

    An integration's `consumer` option is the boundary its sync or viewer
    later serves under. Setting it over MCP is the one way a model could hand
    itself a wider scope than the server was declared with, one step removed.
    """

    options = arguments.get("options", {})
    if not isinstance(options, dict):
        raise ValidationError(
            "MCP tool argument must be a JSON object",
            details={"argument": "options", "received": _json_type(options)},
        )
    if "consumer" in options:
        requested = Consumer.parse(str(options["consumer"]))
        if not _narrows(requested, ceiling):
            raise PolicyError(
                "An integration configured over MCP serves no wider than the "
                "server's declared consumer",
                details={
                    "requested": requested.value,
                    "server_consumer": ceiling.value,
                    "allowed": _allowed_under(ceiling),
                },
            )
    return dict(options)


def _allowed_under(ceiling: Consumer) -> list[str]:
    return [consumer.value for consumer in Consumer if _narrows(consumer, ceiling)]


def _flag(arguments: dict[str, Any], name: str) -> bool:
    """A boolean argument, absent meaning false.

    `bool("false")` is `True`, so coercing here once turned a client that
    serialised its flags as strings into one that saved every answer.
    """

    value = arguments.get(name, False)
    if not isinstance(value, bool):
        raise _not_a(name, "boolean", value)
    return value


def _optional_flag(arguments: dict[str, Any], name: str) -> bool | None:
    """A boolean argument where absent means "leave it as it is"."""

    if name not in arguments:
        return None
    value = arguments[name]
    if not isinstance(value, bool):
        raise _not_a(name, "boolean", value)
    return value


def _integer(arguments: dict[str, Any], name: str, default: int) -> int:
    value = arguments.get(name, default)
    # `bool` is an `int` subclass, and `true` is not a limit.
    if isinstance(value, bool) or not isinstance(value, int):
        raise _not_a(name, "integer", value)
    return int(value)


def _not_a(name: str, expected: str, value: Any) -> ValidationError:
    return ValidationError(
        f"MCP tool argument must be a JSON {expected}",
        details={"argument": name, "expected": expected, "received": _json_type(value)},
    )


def _json_type(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    return "object"


def _tool_definitions(
    consumer: str | Consumer | None = None,
) -> list[dict[str, Any]]:
    ceiling = server_consumer(consumer)
    return [
        _tool(
            "capture",
            "Capture literal text or an http(s) URL into raw/_inbox. A file "
            "path is accepted only inside this vault's project (its code root "
            "or installed workspace, outside the vault itself) and never for a "
            "credential file such as .env, a private key, or anything under "
            "~/.ssh; send the content as text instead.",
            {
                "source": {"type": "string"},
                "text": {"type": "string"},
                "title": {"type": "string"},
            },
        ),
        _tool(
            "search",
            "Search raw and wiki content with FTS5 BM25.",
            {
                "query": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 100},
                "consumer": _consumer_schema(ceiling),
            },
            ["query", "consumer"],
        ),
        _tool(
            "context",
            "Build the bounded evidence contract used before a proposal.",
            {
                "query": {"type": "string"},
                "limit": {"type": "integer"},
                "max_chars": {"type": "integer"},
                "consumer": _consumer_schema(ceiling),
            },
            ["query", "consumer"],
        ),
        _tool(
            "apply",
            "Validate and write an apply-proposal.v1 batch to wiki/.",
            {"proposal": {"type": "object"}},
            ["proposal"],
        ),
        _tool(
            "file",
            "Move a registered raw source to a configured branch.",
            {
                "item": {"type": "string"},
                "branch": {"type": "string"},
            },
            ["item", "branch"],
        ),
        _tool(
            "ask",
            "Answer a question through the configured provider and evidence.",
            {
                "question": {"type": "string"},
                "save": {"type": "boolean"},
            },
            ["question"],
        ),
        _tool(
            "proposals",
            "List durable filing proposals for review.",
            {
                "status": {
                    "type": "string",
                    "enum": ["pending", "applied", "rejected", "failed"],
                }
            },
        ),
        _tool(
            "approve",
            "Approve and execute a pending filing proposal.",
            {"proposal_id": {"type": "string"}},
            ["proposal_id"],
        ),
        _tool(
            "reject",
            "Reject a pending filing proposal without deleting raw evidence.",
            {
                "proposal_id": {"type": "string"},
                "reason": {"type": "string"},
            },
            ["proposal_id"],
        ),
        _tool(
            "resurface",
            "Resurface one durable insight through the configured provider.",
            {},
        ),
        _tool(
            "status",
            "Return vault health and counts, scoped to this server's declared "
            "consumer.",
            {},
        ),
        _tool(
            "lint",
            "Validate registry, frontmatter, citations, and links.",
            {"semantic": {"type": "boolean"}},
        ),
        _tool(
            "integration_configure",
            "Persist an opt-in Obsidian, Neo4j, PostgreSQL, or web policy.",
            {
                "name": _integration_name_schema(),
                "enabled": {"type": "boolean"},
                "managed": {"type": "boolean"},
                "options": {"type": "object"},
            },
            ["name"],
        ),
        _tool(
            "integration_status",
            "Inspect configured policies and live integration lifecycle state.",
            {"name": _integration_name_schema()},
        ),
        _tool(
            "integration_up",
            "Start a managed integration without deleting its persistent data.",
            {"name": _integration_name_schema()},
            ["name"],
        ),
        _tool(
            "integration_down",
            "Stop a managed integration while preserving its persistent data.",
            {"name": _integration_name_schema()},
            ["name"],
        ),
        _tool(
            "integration_sync",
            "Synchronize the privacy-filtered graph with an enabled integration.",
            {"name": _integration_name_schema()},
            ["name"],
        ),
    ]


def _consumer_schema(consumer: str | Consumer | None = None) -> dict[str, Any]:
    ceiling = server_consumer(consumer)
    return {
        "type": "string",
        "enum": _allowed_under(ceiling),
        "description": (
            "Privacy boundary applied to the returned evidence. There is no "
            "default: declare the boundary of whoever will read the result. "
            f"This server was started as '{ceiling.value}' and answers no "
            "wider; a wider value is refused with policy_denied. "
            "'cloud' returns only branches marked cloud, so it is the only "
            "value safe to forward to a third-party model or service. "
            "'local' returns cloud and local-only branches and excludes "
            "never-ingest, so it is the right value for an agent running on "
            "the operator's own machine. never-ingest content is never "
            "served over MCP."
        ),
    }


def _integration_name_schema() -> dict[str, Any]:
    return {
        "type": "string",
        "enum": ["obsidian", "neo4j", "postgres", "web"],
    }


def _tool(
    name: str,
    description: str,
    properties: dict[str, Any],
    required: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "name": name,
        "description": description,
        "inputSchema": {
            "type": "object",
            "properties": properties,
            "required": required or [],
            "additionalProperties": False,
        },
    }


def _result(request_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _error(
    request_id: Any, code: int, message: str, data: dict[str, Any]
) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": code, "message": message, "data": data},
    }
