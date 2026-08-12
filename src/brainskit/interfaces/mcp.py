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
    ValidationError,
)

MCP_PROTOCOL_VERSION = "2025-06-18"
MCP_SUPPORTED_VERSIONS = {MCP_PROTOCOL_VERSION}
MCP_MAX_REQUEST_BYTES = 1_048_576
MCP_INTERNAL_ERROR = -32603
_MAX_DISCARDED_BYTES = 4 * MCP_MAX_REQUEST_BYTES
_MAX_REASON_CHARS = 200
_FILESYSTEM_PATH = re.compile(r"(?:/[^\s'\"]+){2,}")


class ProtocolVersionError(ValidationError):
    """MCP-Protocol-Version failure that must surface as HTTP 400."""

    code = "protocol_version_invalid"


def run_stdio(service: BrainskitService) -> None:
    """Minimal MCP JSON-RPC stdio transport; stdout is protocol-only."""

    for line in sys.stdin:
        if not line.strip():
            continue
        request: dict[str, Any] | None = None
        try:
            request = json.loads(line)
            response = _handle(service, request)
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
                -32000,
                str(exc),
                {"code": exc.code, **exc.details},
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
) -> None:
    """Serve stateless MCP Streamable HTTP with pre-shared Bearer auth."""

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
                _error(None, -32600, "Invalid Request", {"reason": str(exc)}),
                status=HTTPStatus.BAD_REQUEST,
            )
            return
        request: dict[str, Any] = parsed
        status = HTTPStatus.OK
        try:
            self._validate_http_contract(request)
            response = _handle(self.server.service, request)
        except ProtocolVersionError as exc:
            # The Streamable HTTP transport requires a hard 400 here so a
            # conforming client cannot read a version mismatch as success.
            status = HTTPStatus.BAD_REQUEST
            response = _error(
                request.get("id"),
                -32600,
                "Invalid Request",
                {"reason": str(exc), **exc.details},
            )
        except (json.JSONDecodeError, TypeError, ValidationError) as exc:
            response = _error(
                request.get("id"),
                -32600,
                "Invalid Request",
                {"reason": str(exc)},
            )
        except BrainskitError as exc:
            response = _error(
                request.get("id"),
                -32000,
                str(exc),
                {"code": exc.code, **exc.details},
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
            raise ValidationError("MCP requires JSON-RPC 2.0 and a method")
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
            raise ValidationError("Mcp-Method does not match the JSON-RPC method")
        mirrored_name = self.headers.get("Mcp-Name")
        params = request.get("params")
        if mirrored_name and isinstance(params, dict):
            expected_name = params.get("name", params.get("uri"))
            if expected_name is not None and mirrored_name != str(expected_name):
                raise ValidationError(
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
        body = json.dumps({"ok": False, "error": {"code": code}}).encode()
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
    service: BrainskitService, request: dict[str, Any]
) -> dict[str, Any] | None:
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
        return _result(request_id, {"tools": _tool_definitions()})
    if method == "tools/call":
        name = params.get("name")
        arguments = params.get("arguments") or {}
        if not isinstance(name, str):
            raise ValidationError("MCP tool name must be a string")
        value = _call_tool(service, name, arguments)
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
                "isError": False,
            },
        )
    if method == "resources/list":
        readable_pages = sorted(
            str(node["path"])
            for node in service.graph_data(consumer="local")["nodes"]
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
        resource = service.read_resource(f"page:{path}", consumer="local")
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
    service: BrainskitService, name: str, arguments: dict[str, Any]
) -> dict[str, Any]:
    tools: dict[str, Callable[[], dict[str, Any]]] = {
        "capture": lambda: service.capture(
            arguments.get("source"),
            text=arguments.get("text"),
            title=arguments.get("title"),
        ),
        "search": lambda: service.search(
            str(arguments["query"]),
            int(arguments.get("limit", 10)),
            consumer=str(arguments["consumer"]),
        ),
        "context": lambda: service.context(
            str(arguments["query"]),
            limit=int(arguments.get("limit", 8)),
            max_chars=int(arguments.get("max_chars", 24_000)),
            consumer=str(arguments["consumer"]),
        ),
        "apply": lambda: service.apply(arguments["proposal"]),
        "file": lambda: service.file(
            str(arguments["item"]), str(arguments["branch"])
        ),
        "ask": lambda: service.ask(
            str(arguments["question"]), save=bool(arguments.get("save", False))
        ),
        "proposals": lambda: service.proposals(arguments.get("status")),
        "approve": lambda: service.approve(str(arguments["proposal_id"])),
        "reject": lambda: service.reject(
            str(arguments["proposal_id"]), str(arguments.get("reason", ""))
        ),
        "resurface": service.resurface,
        "status": service.status,
        "lint": lambda: service.lint(
            semantic=bool(arguments.get("semantic", False))
        ),
        "integration_configure": lambda: service.integration_configure(
            str(arguments["name"]),
            enabled=arguments.get("enabled"),
            managed=arguments.get("managed"),
            options=dict(arguments.get("options", {})),
        ),
        "integration_status": lambda: service.integration_status(
            str(arguments["name"]) if arguments.get("name") else None
        ),
        "integration_up": lambda: service.integration_up(str(arguments["name"])),
        "integration_down": lambda: service.integration_down(
            str(arguments["name"])
        ),
        "integration_sync": lambda: service.integration_sync(
            str(arguments["name"])
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


def _tool_definitions() -> list[dict[str, Any]]:
    return [
        _tool(
            "capture",
            "Capture a file path, URL, or literal text into raw/_inbox.",
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
                "consumer": _consumer_schema(),
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
                "consumer": _consumer_schema(),
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
        _tool("status", "Return vault health and counts.", {}),
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


def _consumer_schema() -> dict[str, Any]:
    return {
        "type": "string",
        "enum": ["human", "local", "cloud"],
        "description": (
            "Privacy boundary applied to the returned evidence. There is no "
            "default: declare the boundary of whoever will read the result. "
            "'cloud' returns only branches marked cloud, so it is the only "
            "value safe to forward to a third-party model or service. "
            "'local' returns cloud and local-only branches and excludes "
            "never-ingest, so it is the right value for an agent running on "
            "the operator's own machine. 'human' applies NO restriction at "
            "all: it returns never-ingest content verbatim and is intended "
            "only for a surface a person reads directly and does not relay."
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
