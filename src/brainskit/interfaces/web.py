from __future__ import annotations

import gzip
import hmac
import json
import os
import sys
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from typing import Any
from urllib.parse import parse_qs, urlparse

from brainskit.application.services import BrainskitService
from brainskit.domain.model import BrainskitError, ValidationError

LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})

# Below this, gzip's own framing overhead outweighs the bytes it would save.
_COMPRESSIBLE_MIN_BYTES = 500

# A second `bk web` (another vault, a forgotten terminal) is the common case
# a raw `OSError` from binding the requested port would otherwise surface as
# "[Errno 48] Address already in use" -- meaningless to the person this
# command is for. Trying a small, bounded run of the following ports turns
# that into a server that just comes up; the range is small enough that
# exhausting it really does mean something else is wrong.
_PORT_FALLBACK_ATTEMPTS = 10


def build_server(
    service: BrainskitService,
    *,
    host: str,
    port: int,
    consumer: str,
    token: str | None = None,
    instance_id: str = "",
    allowed_origins: list[str] | None = None,
) -> BrainskitWebServer:
    """Validate the policy and return a configured, unstarted server.

    Every caller goes through here, tests included. The handler reads five
    attributes off the server, and a caller that assembles one by hand gets a
    request-time `AttributeError` for whichever it forgets — which is how a
    security check can be added to the handler and silently not reach a test.
    """

    # 0 is not a port, it is a request for an ephemeral one. Refusing it would
    # push every caller that wants a free port — tests above all — into
    # assembling the server by hand, which is what this function exists to stop.
    if port != 0 and not 1 <= port <= 65535:
        raise ValidationError("Web viewer port must be between 1 and 65535")
    if consumer not in {"human", "local", "cloud"}:
        raise ValidationError("Web viewer consumer is invalid")
    if host not in LOOPBACK_HOSTS and not token:
        raise ValidationError(
            "Remote web viewer binding requires --token-env with a populated variable"
        )
    server = _bind_with_fallback(host, port)
    # Bound to port 0, the real port is only known after binding — and it is
    # what the default origins have to name.
    resolved_port = server.server_port
    # Same default as the MCP endpoint: the origins the viewer is actually
    # served from. A same-origin GET sends no Origin at all, so this only has
    # to cover the requests a browser does label.
    origins = set(allowed_origins or ()) or {
        f"http://127.0.0.1:{resolved_port}",
        f"http://localhost:{resolved_port}",
    }
    server.service = service
    server.consumer = consumer
    server.token = token
    server.instance_id = instance_id
    server.allowed_origins = origins
    server.allowed_hosts = _allowed_hosts(host, origins)
    return server


def _bind_with_fallback(host: str, port: int) -> BrainskitWebServer:
    """Bind `port`, or the next few after it when it is already taken.

    `ThreadingHTTPServer.__init__` binds immediately, so a held port fails
    right here. `port == 0` asks the OS for an ephemeral one and cannot
    conflict this way, so it is tried exactly once; any other port is tried
    across a small, bounded range starting at the request. The caller (via
    `server.server_port`) always learns which one actually won.
    """

    if port == 0:
        return BrainskitWebServer((host, port), BrainskitWebHandler)
    last_error: OSError | None = None
    for candidate in range(port, port + _PORT_FALLBACK_ATTEMPTS):
        try:
            return BrainskitWebServer((host, candidate), BrainskitWebHandler)
        except OSError as exc:
            last_error = exc
    last_port = port + _PORT_FALLBACK_ATTEMPTS - 1
    raise ValidationError(
        "No port was free for the web viewer",
        details={
            "ports_tried": f"{port}-{last_port}",
            "hint": "Free one of those ports, or pass --port to try a different range",
        },
    ) from last_error


def run_web(
    service: BrainskitService,
    *,
    host: str,
    port: int,
    consumer: str,
    token_env: str = "",
    instance_id: str = "",
    allowed_origins: list[str] | None = None,
    open_browser: bool = True,
) -> None:
    server = build_server(
        service,
        host=host,
        port=port,
        consumer=consumer,
        token=os.environ.get(token_env) if token_env else None,
        instance_id=instance_id,
        allowed_origins=allowed_origins,
    )
    from brainskit.interfaces import console

    # `server_address` is typed for every socket family the base class supports,
    # so its host element is a union that includes bytes. A TCP bind always
    # yields text, but formatting the union directly would print
    # `http://b'127.0.0.1':8000/` on any family that did not -- a URL nobody can
    # click, in the one line whose whole job is to be clicked.
    bound = server.server_address[0]
    host_text = bound.decode() if isinstance(bound, bytes | bytearray) else str(bound)
    url = f"http://{host_text}:{server.server_port}/"
    message = f"brainskit web viewer ready at {url}"
    if port != 0 and server.server_port != port:
        message += f" ({port} was already in use)"
    print(console.status_line(True, message), file=sys.stderr)
    print(
        console.style(f"  stop it with Ctrl-C ({server.consumer} consumer)", console.MUTED),
        file=sys.stderr,
    )
    if open_browser and consumer == "human":
        _open_browser_best_effort(url)
    try:
        server.serve_forever(poll_interval=0.25)
    finally:
        server.server_close()


def _open_browser_best_effort(url: str) -> None:
    """Open `url` in the OS default browser, never at the cost of the server.

    A headless box, a sandboxed environment, or `$BROWSER` pointed at
    nothing all fail here in different ways: `webbrowser.open` returns
    `False` on some platforms and raises `webbrowser.Error` on others.
    Either way the URL is already printed above, so a failure here is worth
    a quiet note on stderr, never a crash.
    """

    from brainskit.interfaces import console

    try:
        opened = webbrowser.open(url)
    except webbrowser.Error:
        opened = False
    if not opened:
        print(
            console.style(
                "  (could not open a browser automatically — open the URL above)",
                console.MUTED,
            ),
            file=sys.stderr,
        )


def _allowed_hosts(host: str, allowed_origins: set[str]) -> set[str]:
    """Host names this server will answer to.

    The loopback bind is the case that matters. It is the documented default
    and it needs no token, so the only thing standing between a web page and
    the vault is that the page cannot reach 127.0.0.1 — which DNS rebinding
    defeats by pointing its own name at it. The browser then sends
    `Host: attacker.example`, so comparing the host is what actually refuses
    the request; the Origin header cannot, because a same-origin GET carries
    none at all.

    Hostnames named through --allowed-origin are accepted too, so an operator
    who fronts the viewer with a real name does not have to pass it twice.
    """

    names = {host.strip("[]").lower()}
    if host in LOOPBACK_HOSTS:
        names |= LOOPBACK_HOSTS
    for origin in allowed_origins:
        hostname = urlparse(origin).hostname
        if hostname:
            names.add(hostname.lower())
    return names


class BrainskitWebServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    service: BrainskitService
    consumer: str
    token: str | None
    instance_id: str
    allowed_origins: set[str]
    allowed_hosts: set[str]

    def handle_error(self, request: Any, client_address: Any) -> None:
        """Silence the traceback the stdlib prints for a client that vanished.

        `ConnectionResetError`/`BrokenPipeError`/`ConnectionAbortedError` fire
        whenever a browser tab closes, reloads, or tears down a speculative
        connection mid-request -- ordinary browser behaviour, not a server
        bug. The stdlib's default `handle_error` prints a full traceback to
        stderr for every one of these; a single page load firing several
        `/api/*` requests on one kept-alive connection (see `protocol_version`
        below) left exactly that traceback on screen for a request the
        browser itself had already abandoned. Anything else still prints --
        a real bug in a handler must not go silent.
        """

        exc_type = sys.exc_info()[0]
        if exc_type is not None and issubclass(
            exc_type, ConnectionResetError | BrokenPipeError | ConnectionAbortedError
        ):
            return
        super().handle_error(request, client_address)




class BrainskitWebHandler(BaseHTTPRequestHandler):
    server: BrainskitWebServer
    # Every _send_* method below sends an accurate Content-Length, so a
    # persistent connection is safe here: the several requests one page load
    # fires (/api/status, /api/proposals, /api/graph, /static/three.min.js)
    # share one TCP connection instead of a fresh handshake each. `timeout`
    # keeps an idle keep-alive connection from staying open forever — the
    # stdlib closes it via a socket.timeout in handle_one_request().
    protocol_version = "HTTP/1.1"
    timeout = 30

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        # Ahead of the route table, and ahead of health: a rebound page must
        # not be able to confirm a viewer is even running here, let alone read
        # a vault whose consumer is `human` and therefore withholds nothing.
        if not self._host_allowed():
            self._send_json(
                {"ok": False, "error": {"code": "host_denied"}},
                status=HTTPStatus.FORBIDDEN,
            )
            return
        if not self._origin_allowed():
            self._send_json(
                {"ok": False, "error": {"code": "origin_denied"}},
                status=HTTPStatus.FORBIDDEN,
            )
            return
        if parsed.path in {"/", "/index.html"}:
            self._send_html(WEB_VIEWER_HTML)
            return
        if parsed.path == "/static/three.min.js":
            self._send_static("templates/web/three.min.js", "text/javascript; charset=utf-8")
            return
        if parsed.path == "/api/health":
            self._send_json(
                {
                    "ok": True,
                    "service": "brainskit-web",
                    "instance_id": self.server.instance_id,
                }
            )
            return
        if not self._authorized():
            self._send_json(
                {"ok": False, "error": {"code": "unauthorized"}},
                status=HTTPStatus.UNAUTHORIZED,
            )
            return
        query = parse_qs(parsed.query)
        try:
            if parsed.path == "/api/status":
                value = self.server.service.reader_status(
                    consumer=self.server.consumer
                )
            elif parsed.path == "/api/graph":
                value = self.server.service.graph_data(
                    consumer=self.server.consumer,
                    enrichment=_one(query, "enrichment", "") in ("1", "true"),
                    limit=int(_one(query, "limit", "1500")),
                )
            elif parsed.path == "/api/code-graph":
                value = self.server.service.code_graph_data(
                    consumer=self.server.consumer,
                    limit=int(_one(query, "limit", "1500")),
                )
            elif parsed.path == "/api/search":
                phrase = _one(query, "q")
                limit = int(_one(query, "limit", "20"))
                value = self.server.service.search(
                    phrase,
                    limit=limit,
                    consumer=self.server.consumer,
                )
            elif parsed.path == "/api/proposals":
                value = self.server.service.proposals_for_consumer(
                    _one(query, "status", "") or None,
                    consumer=self.server.consumer,
                )
            elif parsed.path == "/api/resource":
                value = self.server.service.read_resource(
                    _one(query, "id"), consumer=self.server.consumer
                )
            elif parsed.path == "/api/sources":
                value = self.server.service.browse_sources(
                    consumer=self.server.consumer,
                    limit=int(_one(query, "limit", "500")),
                )
            elif parsed.path == "/api/pages":
                value = self.server.service.browse_pages(
                    consumer=self.server.consumer,
                    limit=int(_one(query, "limit", "500")),
                )
            elif parsed.path == "/api/timeline":
                value = self.server.service.timeline(
                    consumer=self.server.consumer,
                    limit=int(_one(query, "limit", "500")),
                )
            elif parsed.path == "/api/integrations":
                value = self.server.service.integration_status()
            else:
                self._send_json(
                    {"ok": False, "error": {"code": "not_found"}},
                    status=HTTPStatus.NOT_FOUND,
                )
                return
        except (ValueError, BrainskitError) as exc:
            code = getattr(exc, "code", "invalid_request")
            details = getattr(exc, "details", {})
            self._send_json(
                {
                    "ok": False,
                    "error": {
                        "code": code,
                        "message": str(exc),
                        "details": details,
                    },
                },
                status=HTTPStatus.BAD_REQUEST,
            )
            return
        self._send_json({"ok": True, "result": value})

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if not self._host_allowed():
            self._send_json(
                {"ok": False, "error": {"code": "host_denied"}},
                status=HTTPStatus.FORBIDDEN,
            )
            return
        if not self._origin_allowed():
            self._send_json(
                {"ok": False, "error": {"code": "origin_denied"}},
                status=HTTPStatus.FORBIDDEN,
            )
            return
        if not self._authorized():
            self._send_json(
                {"ok": False, "error": {"code": "unauthorized"}},
                status=HTTPStatus.UNAUTHORIZED,
            )
            return
        # The write surface belongs to a person at a keyboard. A viewer bound
        # to a machine consumer (`local`/`cloud`) stays read-only, so a script
        # pointed at a viewer cannot mutate the vault through it.
        if self.server.consumer != "human":
            self._send_json(
                {
                    "ok": False,
                    "error": {
                        "code": "writes_refused",
                        "message": "The web viewer only writes at --consumer human",
                        "details": {"consumer": self.server.consumer},
                    },
                },
                status=HTTPStatus.FORBIDDEN,
            )
            return
        try:
            body = self._read_json_body()
            if parsed.path == "/api/capture":
                value = self._do_capture(body)
            elif parsed.path == "/api/ask":
                value = self._do_ask(body)
            elif parsed.path == "/api/proposals/approve":
                value = self.server.service.approve(_need(body, "id"))
            elif parsed.path == "/api/proposals/reject":
                value = self.server.service.reject(
                    _need(body, "id"), str(body.get("reason") or "")
                )
            else:
                self._send_json(
                    {"ok": False, "error": {"code": "not_found"}},
                    status=HTTPStatus.NOT_FOUND,
                )
                return
        except (ValueError, BrainskitError) as exc:
            code = getattr(exc, "code", "invalid_request")
            details = getattr(exc, "details", {})
            self._send_json(
                {
                    "ok": False,
                    "error": {
                        "code": code,
                        "message": str(exc),
                        "details": details,
                    },
                },
                status=HTTPStatus.BAD_REQUEST,
            )
            return
        self._send_json({"ok": True, "result": value})

    def _read_json_body(self) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length", "0") or "0")
        except ValueError as exc:
            raise ValidationError("Web API request Content-Length is invalid") from exc
        if not 0 < length <= MAX_REQUEST_BODY:
            raise ValidationError(
                "Web API request body is missing or too large",
                details={"max_bytes": MAX_REQUEST_BODY},
            )
        try:
            value = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValidationError("Web API request body is not valid JSON") from exc
        if not isinstance(value, dict):
            raise ValidationError("Web API request body must be a JSON object")
        return value

    def _do_capture(self, body: dict[str, Any]) -> dict[str, Any]:
        text = body.get("text")
        url = body.get("url")
        if text is not None and url is not None:
            raise ValidationError("capture accepts text or url, not both")
        if text is not None and not isinstance(text, str):
            raise ValidationError("capture text must be a string")
        if url is not None and not isinstance(url, str):
            raise ValidationError("capture url must be a string")
        title = str(body["title"]) if body.get("title") else None
        if text is None and url is None:
            raise ValidationError("capture requires text or url")
        return self.server.service.capture(
            url if url is not None else None, text=text, title=title
        )

    def _do_ask(self, body: dict[str, Any]) -> dict[str, Any]:
        question = body.get("question")
        if not isinstance(question, str) or not question.strip():
            raise ValidationError("ask requires a question")
        save = body.get("save") in (True, "true", "1")
        return self.server.service.ask(question.strip(), save=save)

    # `format` is BaseHTTPRequestHandler's parameter name, not ours.
    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        return

    def _authorized(self) -> bool:
        if not self.server.token:
            return True
        return hmac.compare_digest(
            self.headers.get("Authorization", ""), f"Bearer {self.server.token}"
        )

    def _host_allowed(self) -> bool:
        """Refuse a request addressed to a name this server does not serve.

        A missing Host is refused rather than waved through: HTTP/1.1 requires
        it, so its absence is not a browser reaching a local viewer.
        """

        header = self.headers.get("Host")
        if not header:
            return False
        hostname = urlparse(f"//{header}").hostname
        if hostname is None:
            return False
        return hostname.lower() in self.server.allowed_hosts

    def _origin_allowed(self) -> bool:
        """Mirror the MCP endpoint: an absent Origin is fine, a foreign one is not.

        This is the cross-origin half. It cannot carry the rebinding case on
        its own — see `_allowed_hosts` — but it stops a page that simply asks
        the browser for the vault from a different origin.
        """

        origin = self.headers.get("Origin")
        return origin is None or origin in self.server.allowed_origins

    def _send_json(
        self, value: dict[str, Any], *, status: HTTPStatus = HTTPStatus.OK
    ) -> None:
        body, encoding = self._maybe_compress(
            json.dumps(value, ensure_ascii=False).encode("utf-8")
        )
        self.send_response(status.value)
        self._security_headers(cache_control="no-store")
        self.send_header("Content-Type", "application/json; charset=utf-8")
        if encoding:
            self.send_header("Content-Encoding", encoding)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, value: str) -> None:
        body, encoding = self._maybe_compress(value.encode("utf-8"))
        self.send_response(HTTPStatus.OK.value)
        self._security_headers(cache_control="no-store")
        self.send_header("Content-Type", "text/html; charset=utf-8")
        if encoding:
            self.send_header("Content-Encoding", encoding)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_static(self, resource: str, content_type: str) -> None:
        body, encoding = self._maybe_compress(
            files("brainskit").joinpath(resource).read_bytes()
        )
        self.send_response(HTTPStatus.OK.value)
        # Vendored and immutable, unlike the dynamic HTML shell and every
        # /api/* response (which stay no-store) — safe to cache for a year.
        self._security_headers(cache_control="public, max-age=31536000, immutable")
        self.send_header("Content-Type", content_type)
        if encoding:
            self.send_header("Content-Encoding", encoding)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _maybe_compress(self, body: bytes) -> tuple[bytes, str | None]:
        """Gzip the body when the client accepts it and it is worth the CPU."""

        if len(body) < _COMPRESSIBLE_MIN_BYTES:
            return body, None
        if "gzip" not in self.headers.get("Accept-Encoding", ""):
            return body, None
        return gzip.compress(body, compresslevel=6), "gzip"

    def _security_headers(self, *, cache_control: str = "no-store") -> None:
        self.send_header("Cache-Control", cache_control)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self' 'unsafe-inline'; "
            "style-src 'unsafe-inline'; img-src 'self' data:; connect-src 'self'",
        )


def _one(query: dict[str, list[str]], key: str, default: str | None = None) -> str:
    values = query.get(key)
    if values:
        return values[0]
    if default is not None:
        return default
    raise ValidationError("Web API query parameter is missing", details={"field": key})


def _need(body: dict[str, Any], key: str) -> str:
    value = body.get(key)
    if not isinstance(value, str) or not value:
        raise ValidationError("Web API request field is missing", details={"field": key})
    return value


MAX_REQUEST_BODY = 2 * 1024 * 1024


WEB_VIEWER_HTML = r'''<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>brainskit</title>
<script src="/static/three.min.js"></script>
<style>
:root{color-scheme:dark;--bg:#090d14;--panel:#111824;--panel2:#151f2e;--line:#27364b;--text:#e8eef8;--muted:#8fa1b8;--blue:#63a5ff;--cyan:#55d6be;--amber:#f4bf6a;--red:#ff7b83}
*{box-sizing:border-box}body{margin:0;background:radial-gradient(circle at 75% -15%,#18304d 0,transparent 38%),var(--bg);color:var(--text);font:14px Inter,ui-sans-serif,system-ui,sans-serif;height:100vh;overflow:hidden}
button,input{font:inherit}.shell{height:100vh;display:grid;grid-template-rows:64px 1fr}.top{display:flex;align-items:center;gap:14px;padding:0 22px;border-bottom:1px solid var(--line);background:#090d14df;backdrop-filter:blur(18px)}
.brand{font-size:18px;font-weight:750;letter-spacing:-.03em}.brand span{color:var(--cyan)}.search{position:relative;flex:1;max-width:720px}.search input{width:100%;border:1px solid var(--line);background:#111824;color:var(--text);border-radius:10px;padding:11px 42px 11px 14px;outline:none}.search input:focus{border-color:var(--blue);box-shadow:0 0 0 3px #63a5ff1c}.key{position:absolute;right:10px;top:9px;color:var(--muted);border:1px solid var(--line);border-radius:5px;padding:2px 6px;font-size:11px}
.nav{display:flex;gap:4px}.view-btn{border:1px solid transparent;background:transparent;color:var(--muted);padding:7px 9px;border-radius:7px;cursor:pointer}.view-btn:hover,.view-btn.active{color:var(--text);background:var(--panel2);border-color:var(--line)}
.actions{display:flex;gap:6px;margin-left:6px}.action-btn{border:1px solid var(--line);background:var(--panel2);color:var(--text);padding:7px 12px;border-radius:8px;cursor:pointer;font-weight:600}.action-btn:hover{border-color:var(--cyan);color:var(--cyan)}
.health{margin-left:auto;display:flex;align-items:center;gap:8px;color:var(--muted)}.dot{width:8px;height:8px;border-radius:50%;background:var(--cyan);box-shadow:0 0 12px var(--cyan)}
.layout{min-height:0;display:grid;grid-template-columns:270px minmax(360px,1fr) 350px}.side,.detail{overflow:auto;background:#0d131d}.side{border-right:1px solid var(--line);padding:18px}.detail{border-left:1px solid var(--line);padding:18px}.main{min-width:0;min-height:0;position:relative;background:linear-gradient(#ffffff05 1px,transparent 1px),linear-gradient(90deg,#ffffff05 1px,transparent 1px);background-size:36px 36px}
.detail-head{display:flex;align-items:center;justify-content:space-between;margin:20px 0 10px}.detail-head h2{margin:0}.expand-btn{border:1px solid var(--line);background:var(--panel2);color:var(--muted);width:26px;height:26px;border-radius:7px;cursor:pointer;font-size:13px;line-height:1;display:flex;align-items:center;justify-content:center;flex:none}.expand-btn:hover{color:var(--text);border-color:var(--cyan)}.expand-btn.active{color:var(--cyan);border-color:var(--cyan)}
h2{font-size:11px;text-transform:uppercase;letter-spacing:.14em;color:var(--muted);margin:20px 0 10px}.stats{display:grid;grid-template-columns:1fr 1fr;gap:8px}.stat{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:12px}.stat b{font-size:22px;display:block;letter-spacing:-.04em}.stat span{font-size:11px;color:var(--muted)}
.rows{display:grid;gap:7px}.row{display:flex;justify-content:space-between;gap:12px;padding:8px 10px;border-radius:7px;background:var(--panel);color:var(--muted)}.row strong{color:var(--text)}.badge{border-radius:999px;padding:3px 8px;background:#243248;color:#aecaef;font-size:11px}.badge.good{background:#173b35;color:#70e0c8}.badge.warn{background:#49361c;color:#ffd183}.badge.bad{background:#492127;color:#ff9ba2}
canvas{width:100%;height:100%;display:block}.graph-meta{position:absolute;left:16px;top:16px;background:#0b111bd9;border:1px solid var(--line);border-radius:10px;padding:10px 12px;color:var(--muted);backdrop-filter:blur(12px);z-index:2}.graph-meta b{color:var(--text)}
.graph-view{position:absolute;inset:0;background:radial-gradient(circle at 50% 35%,#0d1b2c 0,#090d14 62%)}.graph-tools{position:absolute;top:16px;left:50%;transform:translateX(-50%);display:flex;gap:4px;background:#0b111bd9;border:1px solid var(--line);border-radius:10px;padding:5px;backdrop-filter:blur(12px);z-index:3}.graph-tools button{border:1px solid transparent;background:transparent;color:var(--muted);padding:6px 12px;border-radius:7px;cursor:pointer;font-size:12px}.graph-tools button.active,.graph-tools button:hover{color:var(--text);background:var(--panel2);border-color:var(--line)}#displayTools{left:auto;right:16px;transform:none}
.graph-legend{position:absolute;left:16px;bottom:14px;display:flex;gap:10px;flex-wrap:wrap;background:#0b111bd9;border:1px solid var(--line);border-radius:10px;padding:8px 10px;backdrop-filter:blur(12px);z-index:2}.legend-chip{display:inline-flex;align-items:center;gap:6px;color:var(--muted);font-size:11px}.legend-chip i{width:9px;height:9px;border-radius:50%;display:inline-block;box-shadow:0 0 6px currentColor}
.graph-loading{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;background:#0b111bcc;z-index:2;opacity:1;transition:opacity .3s;backdrop-filter:blur(4px)}.graph-loading.hidden{opacity:0;pointer-events:none}.spinner{width:36px;height:36px;border:3px solid var(--line);border-top-color:var(--cyan);border-radius:50%;animation:spin .9s linear infinite}@keyframes spin{to{transform:rotate(360deg)}}
.graph-empty{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;z-index:2;pointer-events:none}.graph-empty.hidden{display:none}.graph-empty-card{pointer-events:auto;width:min(340px,80vw);text-align:center;background:#0b111bd9;border:1px solid var(--line);border-radius:14px;padding:26px 24px;backdrop-filter:blur(12px)}.graph-empty-card h3{margin:0 0 8px;font-size:16px;letter-spacing:-.02em}.graph-empty-card p{margin:0 0 16px;color:var(--muted);line-height:1.55}.graph-empty-card .btn{width:100%}.graph-empty-card .btn.hidden{display:none}.graph-empty-card .hint{margin:14px 0 0;font-size:11px;color:var(--muted)}.graph-empty-card .hint.hidden{display:none}.graph-empty-card .hint code{font:11px ui-monospace,SFMono-Regular,Menlo,monospace;background:var(--panel2);border-radius:4px;padding:1px 5px}
.collection{position:absolute;inset:0;overflow:auto;padding:22px;background:#0b111b}.hidden{display:none}.collection-head{display:flex;align-items:end;justify-content:space-between;margin-bottom:16px}.collection-head h1{margin:0;font-size:24px}.collection-head span{color:var(--muted)}.collection-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(250px,1fr));gap:10px}.item-card{border:1px solid var(--line);background:var(--panel);color:var(--text);padding:13px;border-radius:10px;text-align:left;cursor:pointer}.item-card:hover{border-color:var(--blue);transform:translateY(-1px)}.item-card b{display:block;margin-bottom:7px}.item-card small{display:block;color:var(--muted);line-height:1.45}.item-card .badge{display:inline-block;margin-top:9px}
.result{display:block;width:100%;text-align:left;border:1px solid transparent;background:var(--panel);color:var(--text);border-radius:9px;padding:11px;margin-bottom:7px;cursor:pointer}.result:hover{border-color:var(--blue);background:var(--panel2)}.result small{display:block;color:var(--muted);margin-top:4px}.empty{color:var(--muted);padding:14px;border:1px dashed var(--line);border-radius:9px}
.title{font-size:22px;letter-spacing:-.035em;margin:4px 0}.meta{color:var(--muted);margin-bottom:14px}.content{white-space:pre-wrap;word-break:break-word;line-height:1.6;font:13px ui-monospace,SFMono-Regular,Menlo,monospace;background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:14px;min-height:180px}.proposal{padding:10px;border:1px solid var(--line);border-radius:9px;margin-bottom:7px}.proposal b{display:block}.proposal small{color:var(--muted)}.proposal-actions{display:flex;gap:6px;margin-top:9px}.proposal-actions button{flex:1;border:1px solid var(--line);background:var(--panel2);color:var(--text);border-radius:7px;padding:6px;cursor:pointer;font-size:12px}.proposal-actions .approve:hover{border-color:var(--cyan);color:var(--cyan)}.proposal-actions .reject:hover{border-color:var(--red);color:var(--red)}
.modal-backdrop{position:fixed;inset:0;background:#000a;z-index:20;display:flex;align-items:center;justify-content:center;backdrop-filter:blur(6px)}.modal-backdrop.hidden{display:none}.modal{width:min(560px,92vw);background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:20px;max-height:86vh;overflow:auto}.modal h3{margin:0 0 4px}.modal .hint{color:var(--muted);font-size:12px;margin:0 0 6px}.modal label{display:block;font-size:11px;text-transform:uppercase;letter-spacing:.12em;color:var(--muted);margin:12px 0 6px}.modal textarea,.modal input[type=text]{width:100%;border:1px solid var(--line);background:#0d1522;color:var(--text);border-radius:9px;padding:10px;outline:none;font:inherit}.modal textarea{min-height:110px;resize:vertical;font:13px ui-monospace,ui-sans-serif,monospace}.modal textarea:focus,.modal input:focus{border-color:var(--blue);box-shadow:0 0 0 3px #63a5ff1c}.modal .check{display:flex;align-items:center;gap:8px;text-transform:none;letter-spacing:0;font-size:13px;color:var(--text);margin-top:14px}.modal .row{display:flex;gap:8px;justify-content:flex-end;margin-top:18px}.modal input[type=range]{width:100%;accent-color:var(--cyan)}.modal input[type=color]{width:38px;height:32px;padding:2px;border:1px solid var(--line);border-radius:7px;background:var(--panel2);cursor:pointer}.color-mode-row{display:flex;align-items:center;gap:8px}.btn{border:1px solid var(--line);background:var(--panel2);color:var(--text);padding:9px 16px;border-radius:9px;cursor:pointer;font-weight:600}.btn:hover{border-color:var(--cyan)}.btn.ghost{background:transparent;color:var(--muted)}.btn.ghost:hover{color:var(--text);border-color:var(--line)}.btn:disabled{opacity:.5;cursor:default}
.toast{position:fixed;bottom:20px;left:50%;transform:translateX(-50%);background:#0b111be6;border:1px solid var(--line);border-radius:10px;padding:10px 16px;color:var(--text);z-index:30;opacity:0;transition:opacity .25s;backdrop-filter:blur(12px);max-width:80vw}.toast.show{opacity:1}
.loading{opacity:.55}.footer-note{position:absolute;right:14px;bottom:12px;color:var(--muted);font-size:11px;background:#0b111bc9;padding:5px 8px;border-radius:6px;z-index:2}@media(max-width:1050px){.layout{grid-template-columns:220px 1fr}.detail{position:absolute;right:0;top:64px;bottom:0;width:min(88vw,380px);transform:translateX(100%);transition:.2s;z-index:5}.detail.open{transform:none}}@media(max-width:700px){.layout{grid-template-columns:1fr}.side{display:none}.top{padding:0 12px;gap:8px}.health{display:none}.actions .action-btn{padding:7px 8px;font-size:12px}.search{max-width:220px}}
.detail.expanded{position:fixed;inset:0;z-index:16;transform:none;border:none;padding:40px 5vw;overflow:auto;background:var(--bg);display:flex;flex-direction:column;align-items:center}.detail.expanded .detail-head,.detail.expanded #results{width:100%;max-width:820px}.detail.expanded .content{padding:20px 24px}
.content-head{display:flex;align-items:center;justify-content:space-between;gap:10px;flex-wrap:wrap}.view-toggle{display:flex;gap:4px;flex:none}.toggle-btn{border:1px solid var(--line);background:var(--panel2);color:var(--muted);padding:4px 11px;border-radius:999px;cursor:pointer;font-size:11px;font-weight:600}.toggle-btn:hover{color:var(--text);border-color:var(--cyan)}.toggle-btn.active{color:var(--cyan);border-color:var(--cyan)}
.md-content{line-height:1.65;font:13px Inter,ui-sans-serif,system-ui,sans-serif;background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:14px 16px;min-height:180px;overflow-wrap:anywhere}.md-content>*:first-child{margin-top:0}.md-content h1,.md-content h2,.md-content h3,.md-content h4,.md-content h5,.md-content h6{margin:18px 0 8px;line-height:1.3;color:var(--text);letter-spacing:-.02em;text-transform:none}.md-content h1{font-size:20px}.md-content h2{font-size:18px}.md-content h3{font-size:16px}.md-content h4,.md-content h5,.md-content h6{font-size:14px;color:var(--muted)}.md-content p{margin:0 0 10px}.md-content code{font:12px ui-monospace,SFMono-Regular,Menlo,monospace;background:var(--panel2);border-radius:4px;padding:1px 5px;overflow-wrap:anywhere}.md-content pre{font:12px ui-monospace,SFMono-Regular,Menlo,monospace;background:var(--panel2);border:1px solid var(--line);border-radius:8px;padding:10px 12px;overflow:auto;margin:0 0 12px}.md-content pre code{background:none;padding:0}.md-content a{color:var(--blue);overflow-wrap:anywhere}.md-content blockquote{margin:0 0 12px;padding:2px 14px;border-left:3px solid var(--line);color:var(--muted)}.md-content ul,.md-content ol{margin:0 0 12px;padding-left:22px}.md-content li{margin:3px 0}.md-content hr{border:none;border-top:1px solid var(--line);margin:16px 0}.md-content .table-wrap{overflow-x:auto;margin:0 0 12px}.md-content table{border-collapse:collapse;width:100%;margin:0}.md-content th,.md-content td{border:1px solid var(--line);padding:6px 9px;text-align:left;overflow-wrap:anywhere}.md-content th{color:var(--text);background:var(--panel2)}
.filter-bar{display:flex;flex-wrap:wrap;gap:16px;margin-bottom:16px}.filter-group{display:flex;align-items:center;gap:6px;flex-wrap:wrap}.filter-group-label{color:var(--muted);font-size:10px;text-transform:uppercase;letter-spacing:.1em}.filter-chip{border:1px solid var(--line);background:var(--panel);color:var(--muted);padding:4px 10px;border-radius:999px;cursor:pointer;font-size:11px}.filter-chip:hover{color:var(--text);border-color:var(--blue)}.filter-chip.active{background:var(--panel2);border-color:var(--cyan);color:var(--cyan)}
.timeline-view{position:relative}.timeline-day{margin-bottom:28px}.timeline-day-head{position:sticky;top:0;background:#0b111bf2;backdrop-filter:blur(6px);padding:8px 0 6px;font:11px ui-monospace,SFMono-Regular,Menlo,monospace;text-transform:uppercase;letter-spacing:.12em;color:var(--muted);z-index:1}.timeline-rail{position:relative;margin-top:2px}.timeline-rail::before{content:'';position:absolute;left:4px;top:2px;bottom:2px;width:1px;background:var(--line)}.timeline-row{position:relative;display:flex;align-items:center;gap:10px;width:100%;text-align:left;background:transparent;border:none;color:var(--text);padding:4px 0 4px 20px;cursor:pointer;border-radius:6px}.timeline-row:hover{background:var(--panel)}.timeline-dot{position:absolute;left:0;top:50%;transform:translateY(-50%)}.dot.blue{background:var(--blue);box-shadow:0 0 12px var(--blue)}.timeline-time{font:11px ui-monospace,SFMono-Regular,Menlo,monospace;color:var(--muted);flex:none;width:40px}.timeline-label{flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.timeline-now{display:flex;align-items:center;gap:8px;color:var(--muted);font:11px ui-monospace,SFMono-Regular,Menlo,monospace;text-transform:uppercase;letter-spacing:.12em;margin-bottom:14px}.timeline-now-dot{width:7px;height:7px;border-radius:50%;background:var(--cyan);box-shadow:0 0 10px var(--cyan);animation:timelinePulse 1.6s ease-in-out infinite}@keyframes timelinePulse{0%,100%{opacity:1;transform:scale(1)}50%{opacity:.35;transform:scale(1.5)}}
.feed-card{position:relative;padding:4px 0 4px 20px;margin-bottom:14px}.feed-card .timeline-dot{top:22px}.feed-card-head{display:flex;align-items:center;gap:12px;width:100%;text-align:left;border:1px solid var(--line);background:var(--panel);border-radius:12px;padding:10px 12px;cursor:pointer;color:var(--text)}.feed-card-head:hover{border-color:var(--blue)}.feed-cover{flex:none;width:64px;height:64px;border-radius:10px;overflow:hidden;line-height:0}.feed-cover svg{display:block;width:100%;height:100%}.feed-meta{flex:1;min-width:0;display:flex;flex-direction:column;gap:4px}.feed-time{font:11px ui-monospace,SFMono-Regular,Menlo,monospace;color:var(--muted)}.feed-title{font-weight:650;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.feed-body{margin-top:8px;padding:12px 14px;border:1px solid var(--line);border-radius:10px;background:var(--panel2)}.feed-body.hidden{display:none}.feed-excerpt{overflow:hidden;display:-webkit-box;-webkit-line-clamp:4;-webkit-box-orient:vertical}.feed-open{margin-top:8px;border:1px solid var(--line);background:transparent;color:var(--cyan);padding:5px 12px;border-radius:999px;cursor:pointer;font-size:11px;font-weight:600}.feed-open:hover{border-color:var(--cyan)}
</style>
</head>
<body>
<div class="shell">
  <header class="top"><div class="brand">brain<span>kit</span></div><nav class="nav"><button class="view-btn active" data-view="graph">Graph</button><button class="view-btn" data-view="sources">Sources</button><button class="view-btn" data-view="pages">Wiki</button><button class="view-btn" data-view="timeline">Timeline</button><button class="view-btn" data-view="integrations">Services</button></nav><div class="search"><input id="search" placeholder="Search the compiled brain…" autocomplete="off"><span class="key">⌘ K</span></div><div class="actions"><button id="captureBtn" class="action-btn" title="Add a note, document or URL to the vault">Capture</button><button id="askBtn" class="action-btn" title="Ask the compiled brain">Ask</button></div><div class="health"><span class="dot"></span><span id="health">loading</span></div></header>
  <div class="layout">
    <aside class="side"><h2>Vault</h2><div class="stats" id="stats"></div><h2>Freshness</h2><div class="rows" id="freshness"></div><h2>Branches</h2><div class="rows" id="branches"></div><h2>Review queue</h2><div id="proposals"></div></aside>
    <main class="main"><div class="graph-view" id="graphView"><div class="graph-tools" id="graphTools"><button data-source="knowledge" class="active">Knowledge</button><button data-source="enrichment">+ Inferred</button><button data-source="code">Code</button></div><div class="graph-tools" id="displayTools"><button type="button" id="displayBtn">Display</button></div><canvas id="graph"></canvas><div class="graph-meta" id="graphMeta">Loading graph…</div><div class="graph-legend" id="graphLegend"></div><div class="graph-loading" id="graphLoading"><div class="spinner"></div></div><div class="graph-empty hidden" id="graphEmpty"><div class="graph-empty-card"><h3 id="graphEmptyTitle"></h3><p id="graphEmptyBody"></p><button type="button" class="btn" id="graphEmptyAction"></button><p class="hint hidden" id="graphEmptyCli"></p></div></div><div class="footer-note">drag to orbit · scroll to zoom · right-drag to pan · drag a node · click to inspect</div></div><section class="collection hidden" id="collection"><div class="collection-head"><h1 id="collectionTitle">Sources</h1><span id="collectionMeta"></span></div><div class="filter-bar" id="collectionFilters"></div><div class="collection-grid" id="collectionGrid"></div><div class="timeline-view hidden" id="timelineView"></div></section></main>
    <aside class="detail" id="detail"><div class="detail-head"><h2>Inspector</h2><button id="expandDetail" class="expand-btn" type="button" title="Expand to fill the screen" aria-label="Expand inspector">⤢</button></div><div id="results"><div class="empty">Select a graph node or search the vault.</div></div></aside>
  </div>
</div>
<div class="modal-backdrop hidden" id="captureModal"><div class="modal"><h3>Capture into the vault</h3><p class="hint">You can also drop a text file anywhere on this page.</p><label>Pasted text / a document's contents</label><textarea id="captureText" placeholder="Paste a note, article, snippet…"></textarea><label>Or a URL (stored as a link note)</label><input type="text" id="captureUrl" placeholder="https://…"><label>Title (optional)</label><input type="text" id="captureTitle" placeholder="Leave blank to auto-name"><div class="row"><button class="btn ghost" data-close>Cancel</button><button class="btn" id="captureGo">Capture</button></div></div></div>
<div class="modal-backdrop hidden" id="askModal"><div class="modal"><h3>Ask the compiled brain</h3><p class="hint">Answered from cited evidence; the engine never substitutes its own guess.</p><textarea id="askQuestion" placeholder="A question answered from the vault…"></textarea><label class="check"><input type="checkbox" id="askSave"> Save the answer to output/answers</label><div class="row"><button class="btn ghost" data-close>Cancel</button><button class="btn" id="askGo">Ask</button></div></div></div>
<div class="modal-backdrop hidden" id="displayModal"><div class="modal"><h3>Display</h3><p class="hint">Node size, edge visibility and color mode — applied live, saved to this browser.</p><label>Node size</label><input type="range" id="nodeSizeRange" min="2" max="20" step="1"><label>Edge opacity</label><input type="range" id="edgeOpacityRange" min="0" max="100" step="5"><label>Color mode</label><div class="color-mode-row"><button type="button" class="toggle-btn active" id="colorModeMulti">Multi-color</button><button type="button" class="toggle-btn" id="colorModeMono">Single color</button><input type="color" id="monoColorPicker" value="#63a5ff"></div><div class="row"><button class="btn ghost" data-close>Close</button></div></div></div>
<div class="toast" id="toast"></div>
<script>
const NODES_LIMIT=1100;
const KIND_RGB={raw:[0.33,0.84,0.75],concept:[0.39,0.65,1.0],entity:[0.96,0.75,0.42],synthesis:[0.49,0.91,0.53],system:[0.71,0.61,1.0],source:[0.33,0.84,0.75],page:[0.39,0.65,1.0],default:[0.71,0.61,1.0]};
const EXT_RGB={py:[0.39,0.65,1.0],ts:[0.33,0.84,0.75],js:[0.96,0.75,0.42],go:[0.42,0.87,0.8],rs:[0.96,0.62,0.45],java:[0.96,0.55,0.48],md:[0.8,0.76,0.62],sql:[0.7,0.6,0.95],sh:[0.66,0.76,0.87],json:[0.58,0.58,0.66],html:[0.96,0.75,0.42],css:[0.6,0.6,0.96]};
const state={graph:null,nodes:[],edges:[],pos:null,colors:null,idx:{},adj:null,edgeVerts:[],degree:{},hovered:null,selected:null,dragging:null,source:'knowledge',cache:{},graph_cache:{},sim:null,labelCache:{},collectionFilters:{},coverCache:{},resourceCache:{}};
const G={renderer:null,scene:null,camera:null,points:null,edges:null,stars:null,label:null,rings:[],raycaster:null,textureCache:{},cam:{theta:0.6,phi:1.05,dist:330,tx:0,ty:0,tz:0},fly:null};
let tGlobal=0;
const RING_PULSE_AMPLITUDE=0.04;
function hash(value){let h=2166136261;for(let i=0;i<value.length;i++){h^=value.charCodeAt(i);h=Math.imul(h,16777619)}return h>>>0}
function escText(el,value){el.textContent=value??'';return el}
function clamp(v,a,b){return Math.max(a,Math.min(b,v))}
function toast(msg){const t=document.getElementById('toast');escText(t,msg);t.classList.add('show');clearTimeout(t._timer);t._timer=setTimeout(()=>t.classList.remove('show'),3600)}
/* ---------------- markdown rendering (escape first, then transform) ---------------- */
function escapeHtml(raw){return String(raw==null?'':raw).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#39;')}
function safeUrl(url){url=(url||'').trim();if(/^https?:/i.test(url))return url;if(/^[a-zA-Z][a-zA-Z0-9+.-]*:/.test(url))return '#';return url}
function splitTableRow(line){let t=line.trim();if(t.charAt(0)==='|')t=t.slice(1);if(t.charAt(t.length-1)==='|')t=t.slice(0,-1);return t.split('|').map(c=>c.trim())}
function mdInline(text){let spans=[];text=text.replace(/`([^`]+)`/g,(m,code)=>{spans.push('<code>'+code+'</code>');return '\u0000'+(spans.length-1)+'\u0000'});text=text.replace(/\[([^\]]*)\]\(([^)]+)\)/g,(m,label,url)=>'<a href="'+safeUrl(url)+'" target="_blank" rel="noopener noreferrer">'+label+'</a>');text=text.replace(/\*\*([^*]+)\*\*/g,'<strong>$1</strong>').replace(/__([^_]+)__/g,'<strong>$1</strong>');text=text.replace(/\*([^*]+)\*/g,'<em>$1</em>').replace(/(^|[^\w])_([^_]+)_(?!\w)/g,'$1<em>$2</em>');return text.replace(/\u0000(\d+)\u0000/g,(m,i)=>spans[Number(i)])}
function mdBlocks(text){let lines=text.split('\n'),out=[],paragraph=[],i=0;function flush(){if(paragraph.length){out.push('<p>'+mdInline(paragraph.join(' '))+'</p>');paragraph=[]}}while(i<lines.length){let line=lines[i];let fence=line.match(/^```\s*([\w+-]*)\s*$/);if(fence){flush();let lang=fence[1],code=[];i++;while(i<lines.length&&!/^```\s*$/.test(lines[i])){code.push(lines[i]);i++}i++;out.push('<pre><code'+(lang?' class="language-'+lang+'"':'')+'>'+code.join('\n')+'</code></pre>');continue}let heading=line.match(/^(#{1,6})\s+(.*)$/);if(heading){flush();let level=heading[1].length;out.push('<h'+level+'>'+mdInline(heading[2].trim())+'</h'+level+'>');i++;continue}if(/^(-{3,}|\*{3,}|_{3,})\s*$/.test(line.trim())){flush();out.push('<hr>');i++;continue}if(/^&gt;\s?/.test(line)){flush();let quote=[];while(i<lines.length&&/^&gt;\s?/.test(lines[i])){quote.push(lines[i].replace(/^&gt;\s?/,''));i++}out.push('<blockquote>'+mdBlocks(quote.join('\n'))+'</blockquote>');continue}let sep=lines[i+1];if(line.indexOf('|')!==-1&&sep!==undefined&&/^[\s|:-]+$/.test(sep)&&sep.indexOf('-')!==-1){flush();let head=splitTableRow(line);i+=2;let rows=[];while(i<lines.length&&lines[i].trim()!==''&&lines[i].indexOf('|')!==-1){rows.push(splitTableRow(lines[i]));i++}let table='<table><thead><tr>'+head.map(c=>'<th>'+mdInline(c)+'</th>').join('')+'</tr></thead>';if(rows.length)table+='<tbody>'+rows.map(r=>'<tr>'+r.map(c=>'<td>'+mdInline(c)+'</td>').join('')+'</tr>').join('')+'</tbody>';out.push('<div class="table-wrap">'+table+'</table></div>');continue}if(/^\s*[-*+]\s+/.test(line)){flush();let items=[];while(i<lines.length&&/^\s*[-*+]\s+/.test(lines[i])){items.push(lines[i].replace(/^\s*[-*+]\s+/,''));i++}out.push('<ul>'+items.map(it=>'<li>'+mdInline(it)+'</li>').join('')+'</ul>');continue}if(/^\s*\d+\.\s+/.test(line)){flush();let items=[];while(i<lines.length&&/^\s*\d+\.\s+/.test(lines[i])){items.push(lines[i].replace(/^\s*\d+\.\s+/,''));i++}out.push('<ol>'+items.map(it=>'<li>'+mdInline(it)+'</li>').join('')+'</ol>');continue}if(line.trim()===''){flush();i++;continue}paragraph.push(line.trim());i++}flush();return out.join('\n')}
function stripFrontmatter(raw){return String(raw==null?'':raw).replace(/^---\r?\n[\s\S]*?\r?\n---\r?\n?/,'')}
function mdToHtml(raw){return mdBlocks(escapeHtml(stripFrontmatter(raw)))}
function looksLikeMarkdown(item){let path=String((item&&(item.path||item.id))||'');if(/\.md$/i.test(path))return true;return ['source','entity','concept','synthesis'].indexOf(item&&item.kind)!==-1}
async function api(path,options){let token=localStorage.getItem('brainskit-token')||'';let headers=token?{Authorization:'Bearer '+token}:{};if(options&&options.body)headers['Content-Type']='application/json';let response=await fetch(path,{headers,method:options&&options.method||'GET',body:options&&options.body});if(response.status===401){token=prompt('Access token')||'';if(token){localStorage.setItem('brainskit-token',token);return api(path,options)}}let data=await response.json();if(!data.ok)throw new Error(data.error&&data.error.message||data.error&&data.error.code||'Request failed');return data.result}
/* ---------------- status / proposals / search / collections (unchanged surface) ---------------- */
/* Names every input `healthy` has, the way `bk status`'s headline does. The
   header used to read "needs attention" and stop there, which was legible only
   while `healthy` meant lint alone: an enforcement layer being off now sinks it
   too, and "needs attention" beside "Lint errors 0" reads as a bug in the
   viewer rather than a gate that is not running. */
function healthLabel(status){if(status.healthy)return 'healthy';let reasons=[];if(status.lint_errors)reasons.push(status.lint_errors+' lint error'+(status.lint_errors===1?'':'s'));let layers=(status.enforcement&&status.enforcement.layers)||[];let off=layers.filter(layer=>!layer.active&&!layer.advisory).map(layer=>layer.layer);if(off.length)reasons.push(off.join(', ')+' not active');return reasons.length?'needs attention: '+reasons.join('; '):'needs attention'}
async function load(){try{let [status,proposals]=await Promise.all([api('/api/status'),api('/api/proposals?status=pending')]);renderStatus(status);renderProposals(proposals);document.getElementById('health').textContent=healthLabel(status)}catch(error){document.getElementById('health').textContent=error.message}if(!state.nodes.length)loadGraph(state.source)}
function renderStatus(s){let stats=document.getElementById('stats');stats.replaceChildren(...[['Sources',s.sources],['Wiki pages',s.wiki_pages],['Pending',s.pending],['Lint errors',s.lint_errors]].map(([label,value])=>{let d=document.createElement('div');d.className='stat';d.innerHTML='<b></b><span></span>';escText(d.querySelector('b'),value);escText(d.querySelector('span'),label);return d}));let fresh=document.getElementById('freshness');fresh.replaceChildren(...Object.entries(s.freshness||{}).map(([name,value])=>row(name,value,name==='fresh'?'good':name==='stale'?'warn':'')));let branches=document.getElementById('branches');branches.replaceChildren(...Object.entries(s.by_branch||{}).map(([name,value])=>row(name,value,'')))}
function row(name,value,kind){let d=document.createElement('div');d.className='row';let n=document.createElement('strong');let v=document.createElement('span');v.className='badge '+kind;escText(n,name);escText(v,value);d.append(n,v);return d}
function renderProposals(data){let root=document.getElementById('proposals');if(!data.proposals.length){root.innerHTML='<div class="empty">Nothing waiting.</div>';return}root.replaceChildren(...data.proposals.slice(0,8).map(p=>{let d=document.createElement('div');d.className='proposal';d.tabIndex=0;let b=document.createElement('b');let s=document.createElement('small');escText(b,p.destination_branch||p.branch||p.proposal_id);escText(s,p.reason||p.proposal_id);d.append(b,s);d.onclick=()=>showResource('raw:'+p.source_hash);let actions=document.createElement('div');actions.className='proposal-actions';let ok=document.createElement('button');ok.className='approve';ok.textContent='Approve';let no=document.createElement('button');no.className='reject';no.textContent='Reject';ok.onclick=ev=>{ev.stopPropagation();decideProposal(p,'approve')};no.onclick=ev=>{ev.stopPropagation();decideProposal(p,'reject')};actions.append(ok,no);d.append(actions);return d}))}
async function decideProposal(p,action){let reason='';if(action==='reject'){reason=window.prompt('Reason for rejecting '+p.proposal_id,'')||''}try{await api('/api/proposals/'+action,{method:'POST',body:JSON.stringify({id:p.proposal_id,reason})});toast(action==='approve'?'Proposal applied to the wiki':'Proposal rejected');const fresh=await api('/api/proposals?status=pending');renderProposals(fresh)}catch(error){toast(error.message)}}
let timer;document.getElementById('search').addEventListener('input',event=>{clearTimeout(timer);let q=event.target.value.trim();if(!q){document.getElementById('results').innerHTML='<div class="empty">Select a graph node or search the vault.</div>';return}timer=setTimeout(()=>search(q),180)});async function search(q){let root=document.getElementById('results');root.classList.add('loading');try{let data=await api('/api/search?q='+encodeURIComponent(q)+'&limit=20');root.replaceChildren(...data.hits.map(hit=>{let b=document.createElement('button');b.className='result';let title=document.createElement('span');let meta=document.createElement('small');escText(title,hit.title);escText(meta,hit.kind+' · '+hit.privacy+' · '+hit.path);b.append(title,meta);b.onclick=()=>showResource(hit.content_hash?'raw:'+hit.content_hash:'page:'+hit.path);return b}));if(!data.hits.length)root.innerHTML='<div class="empty">No matching evidence.</div>';document.getElementById('detail').classList.add('open')}catch(error){root.innerHTML='<div class="empty"></div>';escText(root.firstChild,error.message)}finally{root.classList.remove('loading')}}
const COLLECTION_TITLES={sources:'Sources',pages:'Compiled wiki',timeline:'Timeline',integrations:'Persistent services'};
const COLLECTION_FILTER_FIELDS={sources:['branch','privacy'],pages:['kind','freshness'],timeline:['type'],integrations:['state']};
function itemsFor(name,data){return name==='sources'?data.sources:name==='pages'?data.pages:name==='timeline'?data.events:data.integrations}
function collectionCountLabel(filtered,total,redacted,unit){unit=unit||'items';let label=filtered===total?total+' '+unit:filtered+' of '+total+' '+unit;if(redacted)label+=' · '+redacted+' private';return label}
function buildFilterBar(name,items){let fields=COLLECTION_FILTER_FIELDS[name]||[];let frag=document.createDocumentFragment();if(!fields.length)return frag;let selected=state.collectionFilters[name]||(state.collectionFilters[name]={});fields.forEach(field=>{if(!selected[field])selected[field]=new Set();let counts={};items.forEach(it=>{let v=it[field];if(v===undefined||v===null||v==='')return;counts[v]=(counts[v]||0)+1});let values=Object.keys(counts).sort();if(!values.length)return;let group=document.createElement('div');group.className='filter-group';let label=document.createElement('span');label.className='filter-group-label';escText(label,field);group.append(label);values.forEach(value=>{let chip=document.createElement('button');chip.type='button';chip.className='filter-chip'+(selected[field].has(value)?' active':'');escText(chip,value+' ('+counts[value]+')');chip.onclick=()=>{if(selected[field].has(value)){selected[field].delete(value)}else{selected[field].add(value)}renderCollectionView(name,state.cache[name])};group.append(chip)});frag.append(group)});return frag}
function applyFilters(name,items){let selected=state.collectionFilters[name];if(!selected)return items;return items.filter(it=>Object.keys(selected).every(field=>{let set=selected[field];return !set||!set.size||set.has(it[field])}))}
async function switchView(name){document.querySelectorAll('.view-btn').forEach(button=>button.classList.toggle('active',button.dataset.view===name));let graph=name==='graph';document.getElementById('graphView').classList.toggle('hidden',!graph);document.getElementById('collection').classList.toggle('hidden',graph);if(graph){resize3D();return}state.collectionFilters[name]={};let endpoints={sources:'/api/sources',pages:'/api/pages',timeline:'/api/timeline',integrations:'/api/integrations'};try{let data=state.cache[name]||await api(endpoints[name]);state.cache[name]=data;renderCollectionView(name,data)}catch(error){renderCollectionView(name,{error:error.message})}}
function renderCollectionView(name,data){let filterBar=document.getElementById('collectionFilters');if(data.error){filterBar.replaceChildren();renderCollection(name,data);return}let all=itemsFor(name,data);filterBar.replaceChildren(buildFilterBar(name,all));let filtered=applyFilters(name,all);if(name==='timeline'){document.getElementById('collectionGrid').classList.add('hidden');escText(document.getElementById('collectionTitle'),COLLECTION_TITLES.timeline);escText(document.getElementById('collectionMeta'),collectionCountLabel(filtered.length,all.length,data.redacted,'events'));renderTimeline(filtered)}else{renderCollection(name,data,filtered)}}
function renderCollection(name,data,itemsOverride){let title=document.getElementById('collectionTitle'),meta=document.getElementById('collectionMeta'),grid=document.getElementById('collectionGrid');document.getElementById('timelineView').classList.add('hidden');grid.classList.remove('hidden');escText(title,COLLECTION_TITLES[name]);if(data.error){meta.textContent='';grid.innerHTML='<div class="empty"></div>';escText(grid.firstChild,data.error);return}let all=itemsFor(name,data);let items=itemsOverride||all;escText(meta,collectionCountLabel(items.length,all.length,data.redacted));if(!items.length){grid.innerHTML='<div class="empty">No items match the selected filters.</div>';return}grid.replaceChildren(...items.map(item=>{let card=document.createElement('button');card.className='item-card';let heading=document.createElement('b'),info=document.createElement('small'),badge=document.createElement('span');badge.className='badge '+(item.state==='running'||item.state==='ready'||item.freshness==='fresh'?'good':'');let label=item.title||item.label||item.name||item.type;let details=name==='sources'?item.branch+' · '+item.privacy+' · '+item.status:name==='pages'?item.kind+' · '+item.privacy+' · '+item.path:name==='timeline'?item.type+' · '+item.detail+' · '+String(item.at).slice(0,19):item.state+' · '+(item.managed?'managed':'external');escText(heading,label);escText(info,details);escText(badge,item.freshness||item.state||item.type);card.append(heading,info,badge);let id=item.id;if(id)card.onclick=()=>showResource(id);return card}))}
function timelineDayKey(d){return d.getFullYear()+'-'+String(d.getMonth()+1).padStart(2,'0')+'-'+String(d.getDate()).padStart(2,'0')}
function timelineDayLabel(d,spansYears){let label=d.toLocaleDateString(undefined,{weekday:'short',month:'short',day:'numeric'});if(spansYears)label+=', '+d.getFullYear();return label}
function timelineTimeLabel(d){return String(d.getHours()).padStart(2,'0')+':'+String(d.getMinutes()).padStart(2,'0')}
function renderCover(seed,type){let key=seed+':'+type;if(state.coverCache[key])return state.coverCache[key];let color=type==='captured'?'var(--cyan)':'var(--blue)';let n=5+hash(seed+'n')%3;let pts=[];for(let i=0;i<n;i++){let hx=hash(seed+'x'+i)%1000/1000,hy=hash(seed+'y'+i)%1000/1000;pts.push([6+hx*52,6+hy*52])}let lineCount=2+hash(seed+'lc')%3;let lines='';for(let i=0;i<lineCount;i++){let a=pts[i%pts.length],b=pts[(i+1)%pts.length];lines+='<line x1="'+a[0].toFixed(1)+'" y1="'+a[1].toFixed(1)+'" x2="'+b[0].toFixed(1)+'" y2="'+b[1].toFixed(1)+'" style="stroke:'+color+';stroke-opacity:.35" stroke-width="1"/>'}let dots=pts.map(p=>'<circle cx="'+p[0].toFixed(1)+'" cy="'+p[1].toFixed(1)+'" r="2.6" style="fill:'+color+'"/>').join('');let svg='<svg viewBox="0 0 64 64" width="64" height="64" xmlns="http://www.w3.org/2000/svg" aria-hidden="true"><rect width="64" height="64" rx="10" style="fill:var(--panel)"/>'+lines+dots+'</svg>';state.coverCache[key]=svg;return svg}
function renderTimeline(events){document.getElementById('collectionGrid').classList.add('hidden');let root=document.getElementById('timelineView');root.classList.remove('hidden');if(!events.length){root.innerHTML='<div class="empty">No matching events.</div>';return}let parsed=events.map(ev=>({ev,date:new Date(ev.at)}));let years=new Set(parsed.map(p=>p.date.getFullYear()));let spansYears=years.size>1;let groups=[],byKey={};parsed.forEach(p=>{let key=timelineDayKey(p.date);if(!byKey[key]){byKey[key]={date:p.date,items:[]};groups.push(byKey[key])}byKey[key].items.push(p)});let frag=document.createDocumentFragment();let now=document.createElement('div');now.className='timeline-now';let nowDot=document.createElement('span');nowDot.className='timeline-now-dot';now.append(nowDot,document.createTextNode('now'));frag.append(now);groups.forEach(group=>{let section=document.createElement('div');section.className='timeline-day';let head=document.createElement('div');head.className='timeline-day-head';escText(head,timelineDayLabel(group.date,spansYears));section.append(head);let rail=document.createElement('div');rail.className='timeline-rail';group.items.forEach(p=>{let ev=p.ev;let seed=String(ev.id||ev.title||'')+String(ev.at||'');let card=document.createElement('div');card.className='feed-card';let dot=document.createElement('span');dot.className='dot timeline-dot'+(ev.type==='captured'?'':' blue');let cover=document.createElement('span');cover.className='feed-cover';cover.innerHTML=renderCover(seed,ev.type);let headBtn=document.createElement('button');headBtn.type='button';headBtn.className='feed-card-head';let metaCol=document.createElement('span');metaCol.className='feed-meta';let time=document.createElement('span');time.className='feed-time';escText(time,timelineTimeLabel(p.date));let title=document.createElement('span');title.className='feed-title';escText(title,ev.title);let badge=document.createElement('span');badge.className='badge'+(ev.detail==='fresh'?' good':'');escText(badge,ev.detail);metaCol.append(time,title,badge);headBtn.append(cover,metaCol);let body=document.createElement('div');body.className='feed-body hidden';let loaded=false;headBtn.onclick=()=>{let opening=body.classList.contains('hidden');body.classList.toggle('hidden');if(!opening||loaded||!ev.id)return;loaded=true;body.classList.add('loading');body.textContent='Loading…';(async()=>{try{let item=state.resourceCache[ev.id];if(!item){item=await api('/api/resource?id='+encodeURIComponent(ev.id));state.resourceCache[ev.id]=item}body.classList.remove('loading');body.innerHTML='';let excerpt=document.createElement('div');excerpt.className='feed-excerpt md-content';excerpt.innerHTML=looksLikeMarkdown(item)?mdToHtml(item.content):'<p>'+escapeHtml(item.content).replace(/\n+/g,' ')+'</p>';let openBtn=document.createElement('button');openBtn.type='button';openBtn.className='feed-open';openBtn.textContent='Open in Inspector';openBtn.onclick=()=>showResource(ev.id);body.append(excerpt,openBtn)}catch(error){body.classList.remove('loading');loaded=false;body.textContent=error.message}})()};card.append(dot,headBtn,body);rail.append(card)});section.append(rail);frag.append(section)});root.replaceChildren(frag)}
async function showResource(id){let root=document.getElementById('results');root.classList.add('loading');try{let item=await api('/api/resource?id='+encodeURIComponent(id));root.replaceChildren();let head=document.createElement('div');head.className='content-head';let title=document.createElement('div');title.className='title';escText(title,item.title);head.append(title);let meta=document.createElement('div');meta.className='meta';escText(meta,item.kind+' · '+item.privacy+' · '+item.path);let raw=document.createElement('pre');raw.className='content';escText(raw,item.content);let rendered=null;if(looksLikeMarkdown(item)){rendered=document.createElement('div');rendered.className='md-content';rendered.innerHTML=mdToHtml(item.content);raw.classList.add('hidden');let toggle=document.createElement('div');toggle.className='view-toggle';let rBtn=document.createElement('button');rBtn.type='button';rBtn.className='toggle-btn active';rBtn.textContent='Rendered';let wBtn=document.createElement('button');wBtn.type='button';wBtn.className='toggle-btn';wBtn.textContent='Raw';rBtn.onclick=()=>{rBtn.classList.add('active');wBtn.classList.remove('active');rendered.classList.remove('hidden');raw.classList.add('hidden')};wBtn.onclick=()=>{wBtn.classList.add('active');rBtn.classList.remove('active');raw.classList.remove('hidden');rendered.classList.add('hidden')};toggle.append(rBtn,wBtn);head.append(toggle)}root.append(head,meta);if(rendered)root.append(rendered);root.append(raw);document.getElementById('detail').classList.add('open')}catch(error){root.innerHTML='<div class="empty"></div>';escText(root.firstChild,error.message)}finally{root.classList.remove('loading')}}
/* ---------------- capture / ask ---------------- */
function openModal(id){document.getElementById(id).classList.remove('hidden')}function closeModals(){document.querySelectorAll('.modal-backdrop').forEach(m=>m.classList.add('hidden'))}
document.querySelectorAll('.modal-backdrop').forEach(m=>m.addEventListener('pointerdown',e=>{if(e.target===m)m.classList.add('hidden')}));document.querySelectorAll('[data-close]').forEach(b=>b.onclick=closeModals);
document.getElementById('captureBtn').onclick=()=>openModal('captureModal');
document.getElementById('captureGo').onclick=async()=>{let text=document.getElementById('captureText').value;let url=document.getElementById('captureUrl').value.trim();let title=document.getElementById('captureTitle').value.trim();if(!text.trim()&&!url){toast('Paste text or enter a URL');return}let body={};if(text.trim())body.text=text;if(url)body.url=url;if(title)body.title=title;let btn=document.getElementById('captureGo');btn.disabled=true;try{let r=await api('/api/capture',{method:'POST',body:JSON.stringify(body)});toast(r.created?'Captured '+r.source.original_name:'Already in the vault: '+r.source.original_name);closeModals();document.getElementById('captureText').value='';document.getElementById('captureUrl').value='';showResource('raw:'+r.source.content_hash);state.graph_cache={};if(state.source!=='code')loadGraph(state.source);load()}catch(error){toast(error.message)}finally{btn.disabled=false}};
document.getElementById('askBtn').onclick=()=>openModal('askModal');
document.getElementById('askGo').onclick=async()=>{let q=document.getElementById('askQuestion').value.trim();if(!q){toast('Ask a question');return}let save=document.getElementById('askSave').checked;let btn=document.getElementById('askGo');btn.disabled=true;btn.textContent='Thinking…';try{let r=await api('/api/ask',{method:'POST',body:JSON.stringify({question:q,save})});closeModals();let root=document.getElementById('results');root.classList.add('loading');root.replaceChildren();let title=document.createElement('div');title.className='title';let meta=document.createElement('div');meta.className='meta';let box=document.createElement('div');box.className='md-content';escText(title,r.question);escText(meta,'answer'+(r.citations&&r.citations.length?' · '+r.citations.length+' citations':'')+(r.uncertainty?' · uncertainty '+r.uncertainty:'')+(r.saved_to?' · saved to '+r.saved_to:''));box.innerHTML=mdToHtml(r.answer);root.append(title,meta,box);document.getElementById('detail').classList.add('open');root.classList.remove('loading')}catch(error){toast(error.message)}finally{btn.disabled=false;btn.textContent='Ask'}};
let dragDepth=0;addEventListener('dragenter',e=>{e.preventDefault();dragDepth++});addEventListener('dragover',e=>e.preventDefault());addEventListener('dragleave',e=>{e.preventDefault();if(--dragDepth<=0)dragDepth=0});addEventListener('drop',async e=>{e.preventDefault();dragDepth=0;let files=[...e.dataTransfer.files];if(!files.length)return;let f=files[0];let text=await f.text();try{let r=await api('/api/capture',{method:'POST',body:JSON.stringify({text,title:f.name})});toast(r.created?'Captured '+f.name:'Already in the vault: '+f.name);showResource('raw:'+r.source.content_hash);load()}catch(error){toast(error.message)}});
addEventListener('paste',e=>{let active=document.activeElement,tag=active&&active.tagName;if(tag==='INPUT'||tag==='TEXTAREA'||(active&&active.isContentEditable))return;let text=((e.clipboardData&&e.clipboardData.getData('text/plain'))||'').trim();if(!text)return;e.preventDefault();document.getElementById('captureText').value=text;openModal('captureModal');toast('Pasted — review and Capture')});
/* ---------------- 3D graph ---------------- */
function hexToRgb01(hex){hex=String(hex||'').replace('#','');if(hex.length===3)hex=hex.split('').map(c=>c+c).join('');let n=parseInt(hex,16)||0;return [((n>>16)&255)/255,((n>>8)&255)/255,(n&255)/255]}
function colorFor(node,source){if(display.colorMode==='mono')return hexToRgb01(display.monoColor);if(source==='code'){let ext=(node.path||node.label||'').split('.').pop();return EXT_RGB[ext]||[0.55,0.65,0.85]}return KIND_RGB[node.kind||'default']||KIND_RGB.default}
function dotTexture(){if(G.textureCache._dot)return G.textureCache._dot;let c=document.createElement('canvas');c.width=c.height=64;let g=c.getContext('2d');let gr=g.createRadialGradient(32,32,2,32,32,30);gr.addColorStop(0,'rgba(255,255,255,1)');gr.addColorStop(0.45,'rgba(255,255,255,0.8)');gr.addColorStop(1,'rgba(255,255,255,0)');g.fillStyle=gr;g.fillRect(0,0,64,64);G.textureCache._dot=new THREE.CanvasTexture(c);return G.textureCache._dot}
function roundRect(g,x,y,w,h,r){g.beginPath();g.moveTo(x+r,y);g.arcTo(x+w,y,x+w,y+h,r);g.arcTo(x+w,y+h,x,y+h,r);g.arcTo(x,y+h,x,y,r);g.arcTo(x,y,x+w,y,r);g.closePath()}
function makeStars(){let n=520,arr=new Float32Array(n*3);for(let i=0;i<n;i++){let p=Math.random()*2-1,a=Math.random()*Math.PI*2,r=560+Math.random()*520,rr=r*Math.sqrt(1-p*p);arr[i*3]=Math.cos(a)*rr;arr[i*3+1]=r*p;arr[i*3+2]=Math.sin(a)*rr}let g=new THREE.BufferGeometry();g.setAttribute('position',new THREE.BufferAttribute(arr,3));let m=new THREE.PointsMaterial({size:2.5,color:0x8fb6d8,transparent:true,opacity:0.65,map:dotTexture(),depthWrite:false,fog:false});let p=new THREE.Points(g,m);p.frustumCulled=false;return p}
function labelTexture(text){if(state.labelCache[text])return state.labelCache[text];let c=document.createElement('canvas');c.height=64;let g=c.getContext('2d');g.font='600 32px Inter,system-ui,sans-serif';let w=Math.ceil(g.measureText(text).width);c.width=Math.max(64,w+44);g.font='600 32px Inter,system-ui,sans-serif';g.fillStyle='rgba(8,13,22,0.85)';g.strokeStyle='rgba(38,54,75,0.95)';g.lineWidth=2;roundRect(g,2,4,c.width-4,56,12);g.fill();g.stroke();g.fillStyle='#eef4ff';g.textBaseline='middle';g.textAlign='left';g.fillText(text,22,33);let tex=new THREE.CanvasTexture(c);tex.minFilter=THREE.LinearFilter;tex.generateMipmaps=false;state.labelCache[text]=tex;return tex}
function makeRingMaterial(rgb){let key='ring'+rgb.join(',');if(G.textureCache[key])return G.textureCache[key];let c=document.createElement('canvas');c.width=c.height=128;let g=c.getContext('2d');let gr=g.createRadialGradient(64,64,28,64,64,62);gr.addColorStop(0,'rgba(255,255,255,0)');gr.addColorStop(0.66,'rgba(255,255,255,0)');gr.addColorStop(0.74,'rgba('+(rgb[0]*255|0)+','+(rgb[1]*255|0)+','+(rgb[2]*255|0)+',0.9)');gr.addColorStop(1,'rgba(0,0,0,0)');g.fillStyle=gr;g.fillRect(0,0,128,128);let tex=new THREE.CanvasTexture(c);let mat=new THREE.SpriteMaterial({map:tex,transparent:true,blending:THREE.AdditiveBlending,depthWrite:false});G.textureCache[key]=mat;return mat}
function init3D(){let canvas=document.getElementById('graph');G.renderer=new THREE.WebGLRenderer({canvas,antialias:true,alpha:true});G.renderer.setPixelRatio(Math.min(devicePixelRatio||1,2));G.scene=new THREE.Scene();G.scene.fog=new THREE.Fog(0x0a1019,400,820);G.camera=new THREE.PerspectiveCamera(55,1,0.5,3000);G.scene.add(new THREE.HemisphereLight(0xffffff,0x223344,0.7));G.raycaster=new THREE.Raycaster();G.stars=makeStars();G.scene.add(G.stars);resize3D();setCamera()}
function resize3D(){if(!G.renderer)return;let rect=G.renderer.domElement.getBoundingClientRect();if(rect.width===0||rect.height===0)return;G.renderer.setSize(rect.width,rect.height,false);G.camera.aspect=rect.width/rect.height;G.camera.updateProjectionMatrix()}
function setCamera(){let c=G.cam,sp=Math.sin(c.phi);G.camera.position.set(c.tx+c.dist*sp*Math.cos(c.theta),c.ty+c.dist*Math.cos(c.phi),c.tz+c.dist*sp*Math.sin(c.theta));G.camera.lookAt(c.tx,c.ty,c.tz)}
function flyTo(px,py,pz,dist,from){G.fly={sx:G.cam.tx,sy:G.cam.ty,sz:G.cam.tz,ex:px,ey:py,ez:pz,sd:G.cam.dist,ed:dist,t:0};if(from)G.cam.dist=from}
function flyToOrigin(R,from){let camDistTarget=Math.max(150,R*2.7);if(G.scene&&G.scene.fog){G.scene.fog.near=Math.max(20,camDistTarget-R*1.5);G.scene.fog.far=camDistTarget+R*2.5}flyTo(0,0,0,camDistTarget,from)}
function nodePos(i){return{x:state.pos[i*3],y:state.pos[i*3+1],z:state.pos[i*3+2]}}
function graphRadius(){let r=0;for(let i=0;i<state.nodes.length;i++){r=Math.max(r,Math.hypot(state.pos[i*3],state.pos[i*3+1],state.pos[i*3+2]))}return r}
function buildGraph(data,source){showGraphEmpty(false);state.source=source;state.graph=data;state.degree={};for(let e of data.edges){state.degree[e.source]=(state.degree[e.source]||0)+1;state.degree[e.target]=(state.degree[e.target]||0)+1}let nodes=[...data.nodes].sort((a,b)=>(state.degree[b.id]||0)-(state.degree[a.id]||0));let clientHidden=Math.max(0,nodes.length-NODES_LIMIT);nodes=nodes.slice(0,NODES_LIMIT);let ids=new Set(nodes.map(n=>n.id));let edges=data.edges.filter(e=>ids.has(e.source)&&ids.has(e.target));state.nodes=nodes;state.edges=edges;state.idx={};nodes.forEach((nd,i)=>state.idx[nd.id]=i);state.adj=nodes.map(()=>[]);let edgeVerts=[];for(let e of edges){let a=state.idx[e.source],b=state.idx[e.target];if(a===undefined||b===undefined)continue;state.adj[a].push(b);state.adj[b].push(a);edgeVerts.push([a,b])}state.edgeVerts=edgeVerts;
let n=nodes.length,pos=new Float32Array(n*3),colors=new Float32Array(n*3);let kinds=[...new Set(nodes.map(nd=>nd.kind||'default'))];let band={};kinds.forEach((k,i)=>band[k]=i+1);for(let i=0;i<n;i++){let nd=nodes[i],b=band[nd.kind||'default'],a=(hash(nd.id)%100000)/100000*Math.PI*2,p=(hash(nd.id+'p')%100000)/100000*2-1,r=46+b*38+(hash(nd.id+'r')%60),rr=r*Math.sqrt(1-p*p);pos[i*3]=Math.cos(a)*rr;pos[i*3+1]=r*p;pos[i*3+2]=Math.sin(a)*rr}state.pos=pos;state.colors=colors;for(let i=0;i<n;i++){let c=colorFor(nodes[i],source);colors[i*3]=c[0];colors[i*3+1]=c[1];colors[i*3+2]=c[2]}
/* points */
let pgeom=new THREE.BufferGeometry();pgeom.setAttribute('position',new THREE.BufferAttribute(pos,3));pgeom.setAttribute('color',new THREE.BufferAttribute(colors,3));if(G.points){G.scene.remove(G.points);G.points.geometry.dispose()}let pm=new THREE.PointsMaterial({size:display.nodeSize,vertexColors:true,transparent:true,opacity:0.96,map:dotTexture(),alphaTest:0.01,depthWrite:false});G.points=new THREE.Points(pgeom,pm);G.points.frustumCulled=false;G.scene.add(G.points);
/* edges */
let earr=new Float32Array(edgeVerts.length*6),ecol=new Float32Array(edgeVerts.length*6);let egeom=new THREE.BufferGeometry();egeom.setAttribute('position',new THREE.BufferAttribute(earr,3));egeom.setAttribute('color',new THREE.BufferAttribute(ecol,3));if(G.edges){G.scene.remove(G.edges);G.edges.geometry.dispose()}G.edges=new THREE.LineSegments(egeom,new THREE.LineBasicMaterial({vertexColors:true,transparent:true,opacity:display.edgeOpacity}));G.edges.frustumCulled=false;G.scene.add(G.edges);updateEdgePositions();updateEdgeColors(null);
for(let s of G.rings)G.scene.remove(s.sprite);G.rings=[];if(G.label){G.scene.remove(G.label);G.label=null}
let meta=document.getElementById('graphMeta');escText(meta,(data.total_nodes||data.nodes.length)+' nodes · '+(data.total_edges||data.edges.length)+' edges · '+clientHidden+' hidden for rendering'+(data.hidden_nodes?' · '+data.hidden_nodes+' beyond server cap':''));renderLegend(source);
if(source!=='code'&&!data.nodes.some(nd=>nd.kind!=='system')){showGraphEmpty(true,{title:'Nothing captured yet',body:'This graph fills in as you add sources — capture a note, a URL, or a file, and it lands here as soon as brainskit indexes it.',action:{label:'Capture something',onClick:()=>openModal('captureModal')},cli:'bk capture'})}
startSim();let R=Math.max(graphRadius(),40);flyToOrigin(R,900);showGraphLoading(false)}
function renderLegend(source){let el=document.getElementById('graphLegend');let items=source==='code'?[['Python','#63a5ff'],['TypeScript','#55d6be'],['JavaScript','#f4bf6a'],['Go/Rust','#6be0cc'],['Other','#8aa0d9']]:[['raw','#55d6be'],['concept','#63a5ff'],['entity','#f4bf6a'],['synthesis','#7ee787'],['system','#b49cff']];el.replaceChildren(...items.map(([name,c])=>{let chip=document.createElement('span');chip.className='legend-chip';let dot=document.createElement('i');dot.style.background=c;let t=document.createElement('span');escText(t,name);chip.append(dot,t);return chip}))}
function updatePointPositions(){G.points.geometry.attributes.position.needsUpdate=true}
function updateEdgePositions(){let arr=G.edges.geometry.attributes.position.array,ev=state.edgeVerts,pos=state.pos;for(let i=0;i<ev.length;i++){let a=ev[i][0],b=ev[i][1];arr[i*6]=pos[a*3];arr[i*6+1]=pos[a*3+1];arr[i*6+2]=pos[a*3+2];arr[i*6+3]=pos[b*3];arr[i*6+4]=pos[b*3+1];arr[i*6+5]=pos[b*3+2]}G.edges.geometry.attributes.position.needsUpdate=true}
function updateEdgeColors(set){if(!G.edges)return;let col=G.edges.geometry.attributes.color.array,ev=state.edgeVerts;for(let i=0;i<ev.length;i++){let lit=set&&(set.has(ev[i][0])||set.has(ev[i][1])),c=lit?[0.42,0.72,1.0]:[0.16,0.24,0.36];col[i*6]=c[0];col[i*6+1]=c[1];col[i*6+2]=c[2];col[i*6+3]=c[0];col[i*6+4]=c[1];col[i*6+5]=c[2]}G.edges.geometry.attributes.color.needsUpdate=true}
function startSim(){let n=state.nodes.length,pos=state.pos,vel=new Float32Array(n*3);let hubs=Array.from({length:n},(_,i)=>i).sort((a,b)=>(state.degree[state.nodes[b].id]||0)-(state.degree[state.nodes[a].id]||0)).slice(0,40);let sample=[...hubs],step=Math.max(1,Math.floor(n/40));for(let i=0;i<n;i+=step){if(!sample.includes(i))sample.push(i)}state.sim={pos,vel,sample,rest:14,k:0.02,rep:5000,grav:0.0018,damp:0.84,maxD:1.6,total:160,iter:0}}
function stepSim(s){let pos=s.pos,vel=s.vel,sample=s.sample,rest=s.rest,k=s.k,rep=s.rep,grav=s.grav,damp=s.damp,maxD=s.maxD,total=s.total,n=state.nodes.length;for(let it=0;it<2&&s.iter<total;it++){for(let i=0;i<n;i++){vel[i*3]-=pos[i*3]*grav;vel[i*3+1]-=pos[i*3+1]*grav;vel[i*3+2]-=pos[i*3+2]*grav}for(let e=0;e<state.edgeVerts.length;e++){let a=state.edgeVerts[e][0],b=state.edgeVerts[e][1],dx=pos[b*3]-pos[a*3],dy=pos[b*3+1]-pos[a*3+1],dz=pos[b*3+2]-pos[a*3+2],d=Math.sqrt(dx*dx+dy*dy+dz*dz)||0.01,f=k*(d-rest)/d;vel[a*3]+=dx*f;vel[a*3+1]+=dy*f;vel[a*3+2]+=dz*f;vel[b*3]-=dx*f;vel[b*3+1]-=dy*f;vel[b*3+2]-=dz*f}for(let i=0;i<n;i++){for(let s0=0;s0<sample.length;s0++){let j=sample[s0];if(j===i)continue;let dx=pos[i*3]-pos[j*3],dy=pos[i*3+1]-pos[j*3+1],dz=pos[i*3+2]-pos[j*3+2],d2=Math.max(dx*dx+dy*dy+dz*dz,1),d=Math.sqrt(d2),f=rep/d2;vel[i*3]+=dx/d*f;vel[i*3+1]+=dy/d*f;vel[i*3+2]+=dz/d*f}}for(let i=0;i<n;i++){let vx=Math.max(-maxD,Math.min(maxD,vel[i*3]*damp)),vy=Math.max(-maxD,Math.min(maxD,vel[i*3+1]*damp)),vz=Math.max(-maxD,Math.min(maxD,vel[i*3+2]*damp));pos[i*3]+=vx;pos[i*3+1]+=vy;pos[i*3+2]+=vz;vel[i*3]=vx;vel[i*3+1]=vy;vel[i*3+2]=vz}s.iter++}updatePointPositions();updateEdgePositions();updateEdgeColors(null);if(s.iter>=total){state.sim=null;let R=graphRadius();flyToOrigin(R)}}
function focusSet(){let base=null,key=state.dragging!=null?state.dragging:state.hovered;if(key!=null){base=new Set(state.adj[key]);base.add(key)}return base}
function refreshHighlight(){let set=focusSet(),colors=state.colors;for(let i=0;i<state.nodes.length;i++){let base=colorFor(state.nodes[i],state.source),m=set?set.has(i)?1:0.22:1;colors[i*3]=base[0]*m;colors[i*3+1]=base[1]*m;colors[i*3+2]=base[2]*m}G.points.geometry.attributes.color.needsUpdate=true;updateEdgeColors(set);syncOverlays()}
function makeLabel(text){let key='label:'+text;if(!G.textureCache[key]){G.textureCache[key]=new THREE.SpriteMaterial({map:labelTexture(text),transparent:true,depthTest:false})}let s=new THREE.Sprite(G.textureCache[key]);s.renderOrder=10;return s}
function syncOverlays(){for(let s of G.rings)G.scene.remove(s.sprite);G.rings=[];if(G.label){G.scene.remove(G.label);G.label=null}let focus=state.hovered!=null?state.hovered:(state.selected!=null?state.selected:null);if(focus==null)return;let nd=state.nodes[focus],p=nodePos(focus),rgb=colorFor(nd,state.source);
/* Rings and the label were a fixed world-space size, tuned against a typical
   crowded graph viewed from ~150 units out. A 2-node vault's own graph has a
   radius small enough that flyToNode's Math.max(40,R*0.55) lands the camera
   well under that, and the label -- unlike a ring, which is just a glow --
   is legible text: at close range it filled most of the viewport, clipped by
   the canvas edge. Scaling every overlay by camera distance over that same
   150-unit reference keeps its apparent screen size roughly constant instead
   of ballooning whenever the graph or the zoom is small. */
let dscale=G.cam.dist/150;
let ring=new THREE.Sprite(makeRingMaterial(rgb));ring.position.set(p.x,p.y,p.z);ring.scale.set(26*dscale,26*dscale,1);G.scene.add(ring);G.rings.push({sprite:ring});if(state.selected!=null&&state.selected!==focus){let q=nodePos(state.selected),r2=new THREE.Sprite(makeRingMaterial([1,1,1]));r2.position.set(q.x,q.y,q.z);r2.scale.set(18*dscale,18*dscale,1);G.scene.add(r2);G.rings.push({sprite:r2})}let mat=G.textureCache['label:'+nd.label];if(!mat){mat=new THREE.SpriteMaterial({map:labelTexture(nd.label),transparent:true,depthTest:false});G.textureCache['label:'+nd.label]=mat}let img=mat.map.image,ar=img.width/img.height,label=new THREE.Sprite(mat);label.position.set(p.x,p.y+22*dscale,p.z);label.scale.set(ar*8*dscale,8*dscale,1);G.scene.add(label);G.label=label}
function setHover(index){if(state.hovered===index)return;state.hovered=index;refreshHighlight()}
function setSelected(index){state.selected=index;if(index==null)state.hovered=null;refreshHighlight()}
function selectNode(i){setSelected(i);flyToNode(i);if(state.source==='code'){showCodeNode(state.nodes[i])}else{showResource(state.nodes[i].id)}}
function flyToNode(i){let p=nodePos(i),R=graphRadius();flyTo(p.x,p.y,p.z,Math.max(40,R*0.55),G.cam.dist)}
function showCodeNode(nd){let root=document.getElementById('results');root.replaceChildren();let title=document.createElement('div');title.className='title';let meta=document.createElement('div');meta.className='meta';let pre=document.createElement('pre');pre.className='content';escText(title,nd.label||nd.id);escText(meta,(nd.kind||'code symbol')+' · '+(nd.path||'')+(nd.line?' · line '+nd.line:''));escText(pre,'id: '+nd.id+'\npath: '+(nd.path||'')+(nd.line?'\nline: '+nd.line:'')+'\n\ntype: '+(nd.type||'references'));root.append(title,meta,pre);document.getElementById('detail').classList.add('open')}
function pick(cx,cy){if(!G.points)return null;let rect=G.renderer.domElement.getBoundingClientRect();let ndc=new THREE.Vector2((cx-rect.left)/rect.width*2-1,-(cy-rect.top)/rect.height*2+1);G.raycaster.setFromCamera(ndc,G.camera);G.raycaster.params.Points.threshold=11;let hits=G.raycaster.intersectObject(G.points);return hits.length?hits[0]:null}
function dragTo(cx,cy){let i=state.dragging;let rect=G.renderer.domElement.getBoundingClientRect();let ndc=new THREE.Vector2((cx-rect.left)/rect.width*2-1,-(cy-rect.top)/rect.height*2+1);G.raycaster.setFromCamera(ndc,G.camera);let p=new THREE.Vector3(state.pos[i*3],state.pos[i*3+1],state.pos[i*3+2]);let normal=G.camera.getWorldDirection(new THREE.Vector3());let plane=new THREE.Plane().setFromNormalAndCoplanarPoint(normal,p);let hit=new THREE.Vector3();if(G.raycaster.ray.intersectPlane(plane,hit)){state.pos[i*3]=hit.x;state.pos[i*3+1]=hit.y;state.pos[i*3+2]=hit.z;updatePointPositions();updateEdgePositions()}}
function panBy(dx,dy){let k=G.cam.dist*0.0011;let right=new THREE.Vector3();G.camera.getWorldDirection(right);right.cross(new THREE.Vector3(0,1,0)).normalize();let up=new THREE.Vector3(0,1,0);G.cam.tx-=right.x*dx*k;G.cam.ty-=up.y*dy*k;G.cam.tz-=right.z*dx*k}
let canvas3d=document.getElementById('graph');let downX=0,downY=0,downMode=null,movedDist=0;
canvas3d.addEventListener('pointerdown',e=>{if(!G.renderer)return;canvas3d.setPointerCapture(e.pointerId);downX=e.clientX;downY=e.clientY;movedDist=0;if(e.button===2||e.button===1||e.metaKey||e.ctrlKey){downMode='pan';return}let hit=pick(e.clientX,e.clientY);if(hit&&hit.index!=null){downMode='node';state.dragging=hit.index;state.sim=null;refreshHighlight()}else{downMode='rotate'}});
canvas3d.addEventListener('contextmenu',e=>e.preventDefault());
canvas3d.addEventListener('pointermove',e=>{if(!G.renderer)return;movedDist=Math.max(movedDist,Math.hypot(e.clientX-downX,e.clientY-downY));if(downMode==='node'){dragTo(e.clientX,e.clientY)}else if(downMode==='rotate'){G.cam.theta-=(e.clientX-downX)*0.005;G.cam.phi=clamp(G.cam.phi-(e.clientY-downY)*0.005,0.15,Math.PI-0.15);downX=e.clientX;downY=e.clientY}else if(downMode==='pan'){panBy(e.clientX-downX,e.clientY-downY);downX=e.clientX;downY=e.clientY}else{let hit=pick(e.clientX,e.clientY);setHover(hit&&hit.index!=null?hit.index:null)}});
canvas3d.addEventListener('pointerup',e=>{if(!G.renderer)return;let wasNode=downMode==='node',idx=state.dragging,wasMoved=movedDist;downMode=null;if(wasNode){state.dragging=null;state.sim=null;if(wasMoved<5){selectNode(idx)}else{setSelected(idx)}}else if(wasMoved<5){let hit=pick(e.clientX,e.clientY);if(hit&&hit.index!=null){selectNode(hit.index)}else{setSelected(null)}}});
canvas3d.addEventListener('wheel',e=>{e.preventDefault();G.cam.dist=clamp(G.cam.dist*Math.exp(e.deltaY*0.0012),40,1600)},{passive:false});
const graphViewEl=document.getElementById('graphView');
function animate(){requestAnimationFrame(animate);if(graphViewEl.classList.contains('hidden'))return;if(G.fly){let f=G.fly;f.t+=0.04;let k=Math.min(1,f.t),e=1-Math.pow(1-k,3);G.cam.tx=f.sx+(f.ex-f.sx)*e;G.cam.ty=f.sy+(f.ey-f.sy)*e;G.cam.tz=f.sz+(f.ez-f.sz)*e;G.cam.dist=f.sd+(f.ed-f.sd)*e;if(k>=1)G.fly=null}if(state.sim)stepSim(state.sim);if(G.rings.length){tGlobal+=0.05;for(let r of G.rings){let s=r.sprite.scale.x,p=1+RING_PULSE_AMPLITUDE*Math.sin(tGlobal*2.2);r.sprite.scale.set(s*p,s*p,1)}}setCamera();G.renderer.render(G.scene,G.camera)}
/* ---------------- graph sources ---------------- */
function showGraphLoading(show){document.getElementById('graphLoading').classList.toggle('hidden',!show)}
function showGraphEmpty(show,opts){let el=document.getElementById('graphEmpty');el.classList.toggle('hidden',!show);if(!show)return;escText(document.getElementById('graphEmptyTitle'),opts.title);escText(document.getElementById('graphEmptyBody'),opts.body);let btn=document.getElementById('graphEmptyAction');btn.classList.toggle('hidden',!opts.action);if(opts.action){escText(btn,opts.action.label);btn.onclick=opts.action.onClick}let cli=document.getElementById('graphEmptyCli');cli.classList.toggle('hidden',!opts.cli);if(opts.cli){cli.replaceChildren(document.createTextNode('or from a terminal: '));let code=document.createElement('code');escText(code,opts.cli);cli.append(code)}}
async function loadGraph(source){state.source=source;document.querySelectorAll('#graphTools button').forEach(b=>b.classList.toggle('active',b.dataset.source===source));showGraphLoading(true);showGraphEmpty(false);try{let data=state.graph_cache[source];if(!data){let endpoint=source==='code'?'/api/code-graph':(source==='enrichment'?'/api/graph?enrichment=1':'/api/graph');data=await api(endpoint);state.graph_cache[source]=data}buildGraph(data,source)}catch(error){showGraphLoading(false);if(source==='code'){toast('Code graph unavailable: '+error.message);document.getElementById('graphMeta').textContent='';showGraphEmpty(true,{title:'No code graph yet',body:'Brainskit can map this repository\'s structure — functions, imports, call graphs — for lint, search, and agent context.',cli:'bk code build'})}else{document.getElementById('graphMeta').textContent=error.message}}}
document.querySelectorAll('#graphTools button').forEach(b=>b.onclick=()=>{if(b.dataset.source!==state.source)loadGraph(b.dataset.source)});
/* ---------------- display controls (node size / edge opacity / color mode) ---------------- */
const DISPLAY_DEFAULTS={nodeSize:9,edgeOpacity:0.6,colorMode:'multi',monoColor:'#63a5ff'};
function loadDisplayPrefs(){try{let raw=localStorage.getItem('brainskit-display');if(!raw)return Object.assign({},DISPLAY_DEFAULTS);return Object.assign({},DISPLAY_DEFAULTS,JSON.parse(raw))}catch(error){return Object.assign({},DISPLAY_DEFAULTS)}}
let display=loadDisplayPrefs();
function saveDisplayPrefs(){localStorage.setItem('brainskit-display',JSON.stringify(display))}
document.getElementById('displayBtn').onclick=()=>openModal('displayModal');
let nodeSizeRange=document.getElementById('nodeSizeRange'),edgeOpacityRange=document.getElementById('edgeOpacityRange'),monoColorPicker=document.getElementById('monoColorPicker'),colorModeMulti=document.getElementById('colorModeMulti'),colorModeMono=document.getElementById('colorModeMono');
nodeSizeRange.value=display.nodeSize;edgeOpacityRange.value=Math.round(display.edgeOpacity*100);monoColorPicker.value=display.monoColor;colorModeMulti.classList.toggle('active',display.colorMode==='multi');colorModeMono.classList.toggle('active',display.colorMode==='mono');
nodeSizeRange.addEventListener('input',()=>{display.nodeSize=Number(nodeSizeRange.value);if(G.points)G.points.material.size=display.nodeSize;saveDisplayPrefs()});
edgeOpacityRange.addEventListener('input',()=>{display.edgeOpacity=Number(edgeOpacityRange.value)/100;if(G.edges)G.edges.material.opacity=display.edgeOpacity;saveDisplayPrefs()});
function setColorMode(mode){display.colorMode=mode;colorModeMulti.classList.toggle('active',mode==='multi');colorModeMono.classList.toggle('active',mode==='mono');saveDisplayPrefs();if(state.nodes.length)refreshHighlight()}
colorModeMulti.onclick=()=>setColorMode('multi');
colorModeMono.onclick=()=>setColorMode('mono');
monoColorPicker.addEventListener('input',()=>{display.monoColor=monoColorPicker.value;saveDisplayPrefs();if(display.colorMode==='mono'&&state.nodes.length)refreshHighlight()});
/* ---------------- boot ---------------- */
document.querySelectorAll('.view-btn').forEach(button=>button.onclick=()=>switchView(button.dataset.view));document.getElementById('expandDetail').onclick=()=>{document.getElementById('detail').classList.toggle('expanded');document.getElementById('expandDetail').classList.toggle('active')};addEventListener('keydown',e=>{if((e.metaKey||e.ctrlKey)&&e.key==='k'){e.preventDefault();document.getElementById('search').focus()}if(e.key==='Escape'){closeModals();document.getElementById('detail').classList.remove('expanded');document.getElementById('expandDetail').classList.remove('active')}});addEventListener('resize',resize3D);
function boot(){if(typeof THREE==='undefined'){document.getElementById('graphMeta').textContent='3D engine failed to load';showGraphLoading(false);return}try{init3D()}catch(error){document.getElementById('graphMeta').textContent='WebGL unavailable: '+error.message;showGraphLoading(false)}load();if(G.renderer)animate()}
boot();
</script>
</body></html>

'''
