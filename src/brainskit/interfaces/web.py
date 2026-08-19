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

from brainskit.application.jobs import MAX_HISTORY_EXCHANGES
from brainskit.application.services import BrainskitService
from brainskit.domain.model import BrainskitError, ValidationError
from brainskit.domain.privacy import Consumer
from brainskit.interfaces.errors import error_envelope, present, refusal_envelope

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
    # `Consumer.parse` is the one place an unknown consumer becomes an error
    # (ADR 0001); a fourth copy of the three names here could only ever go out
    # of date with it, and its message names the three where this one did not.
    Consumer.parse(consumer)
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
            self._send_refusal("host_denied", HTTPStatus.FORBIDDEN)
            return
        if not self._origin_allowed():
            self._send_refusal("origin_denied", HTTPStatus.FORBIDDEN)
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
            self._send_refusal("unauthorized", HTTPStatus.UNAUTHORIZED)
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
                value = self.server.service.integration_status(
                    consumer=self.server.consumer
                )
            else:
                self._send_refusal("not_found", HTTPStatus.NOT_FOUND)
                return
        except (ValueError, BrainskitError) as exc:
            self._send_error(exc)
            return
        self._send_json({"ok": True, "result": value})

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if not self._host_allowed():
            self._send_refusal("host_denied", HTTPStatus.FORBIDDEN)
            return
        if not self._origin_allowed():
            self._send_refusal("origin_denied", HTTPStatus.FORBIDDEN)
            return
        if not self._authorized():
            self._send_refusal("unauthorized", HTTPStatus.UNAUTHORIZED)
            return
        # The write surface belongs to a person at a keyboard. A viewer bound
        # to a machine consumer (`local`/`cloud`) stays read-only, so a script
        # pointed at a viewer cannot mutate the vault through it.
        if self.server.consumer != "human":
            self._send_refusal(
                "writes_refused",
                HTTPStatus.FORBIDDEN,
                message="The web viewer only writes at --consumer human",
                details={"consumer": self.server.consumer},
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
                self._send_refusal("not_found", HTTPStatus.NOT_FOUND)
                return
        except (ValueError, BrainskitError) as exc:
            self._send_error(exc)
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
        raw_history = body.get("history")
        history: list[dict[str, str]] | None = None
        if raw_history is not None:
            if not isinstance(raw_history, list):
                raise ValidationError(
                    "ask history must be a list of question/answer objects",
                    details={"field": "history"},
                )
            # Excess length is truncated silently to the jobs-layer bound
            # rather than rejected: a chat thread grows past the bound in
            # normal use, and the jobs layer keeps only the last
            # MAX_HISTORY_EXCHANGES anyway -- refusing here would break every
            # conversation at exactly the point it becomes one. Items are
            # validated after truncation (dropped items never reach the
            # model), with field names indexed against the list as sent.
            kept = raw_history[-MAX_HISTORY_EXCHANGES:]
            first_kept = len(raw_history) - len(kept)
            history = []
            for offset, item in enumerate(kept):
                position = first_kept + offset
                if not isinstance(item, dict):
                    raise ValidationError(
                        "ask history items must be objects",
                        details={"field": f"history[{position}]"},
                    )
                item_question = item.get("question")
                if not isinstance(item_question, str):
                    raise ValidationError(
                        "ask history item question must be a string",
                        details={"field": f"history[{position}].question"},
                    )
                item_answer = item.get("answer")
                if not isinstance(item_answer, str):
                    raise ValidationError(
                        "ask history item answer must be a string",
                        details={"field": f"history[{position}].answer"},
                    )
                history.append(
                    {"question": item_question, "answer": item_answer}
                )
        return self.server.service.ask(
            question.strip(), save=save, history=history
        )

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

    def _send_error(self, exc: BaseException) -> None:
        """Answer an error with the status its code means.

        Both handlers used to inline the same fifteen lines and both ended them
        with `HTTPStatus.BAD_REQUEST`, so a missing page arrived as a bad
        request and a privacy refusal did too. The status now comes from the
        one table in `interfaces/errors.py`; a bare `ValueError` (an unparsable
        query parameter, which this deliberately still catches) keeps the 400
        it always had.
        """

        self._send_json(error_envelope(exc), status=present(exc).http_status)

    def _send_refusal(
        self,
        code: str,
        status: HTTPStatus,
        *,
        message: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        """Answer a guard that refused before any error was raised.

        A denied Host, a foreign Origin, a missing token, an unrouted path:
        there is no exception to describe, only the name of the rule that said
        no -- and the status is that rule's, not a code's.
        """

        self._send_json(
            refusal_envelope(code, message=message, details=details), status=status
        )

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
<link rel="icon" href="data:image/svg+xml,%3Csvg%20xmlns='http://www.w3.org/2000/svg'%20viewBox='0%200%2064%2064'%3E%3Crect%20width='64'%20height='64'%20rx='14'%20fill='%23111824'/%3E%3Ccircle%20cx='32'%20cy='32'%20r='17'%20fill='none'%20stroke='%2355d6be'%20stroke-opacity='.45'%20stroke-width='3'/%3E%3Ccircle%20cx='32'%20cy='32'%20r='8'%20fill='%2355d6be'/%3E%3C/svg%3E">
<script src="/static/three.min.js"></script>
<style>
:root{color-scheme:dark;--bg:#090d14;--panel:#111824;--panel2:#151f2e;--line:#27364b;--text:#e8eef8;--muted:#8fa1b8;--blue:#63a5ff;--cyan:#55d6be;--amber:#f4bf6a;--red:#ff7b83}
*{box-sizing:border-box}body{margin:0;background:radial-gradient(circle at 75% -15%,#18304d 0,transparent 38%),var(--bg);color:var(--text);font:14px Inter,ui-sans-serif,system-ui,sans-serif;height:100vh;overflow:hidden}
button,input{font:inherit}.shell{height:100vh;display:grid;grid-template-rows:64px 1fr}.top{display:flex;align-items:center;gap:14px;padding:0 22px;border-bottom:1px solid var(--line);background:#090d14df;backdrop-filter:blur(18px)}
.brand{font-size:18px;font-weight:750;letter-spacing:-.03em}.brand span{color:var(--cyan)}.search{position:relative;flex:1;max-width:720px}.search input{width:100%;border:1px solid var(--line);background:#111824;color:var(--text);border-radius:10px;padding:11px 42px 11px 14px;outline:none}.search input:focus{border-color:var(--blue);box-shadow:0 0 0 3px #63a5ff1c}.key{position:absolute;right:10px;top:9px;color:var(--muted);border:1px solid var(--line);border-radius:5px;padding:2px 6px;font-size:11px}
.nav{display:flex;gap:4px}.view-btn{border:1px solid transparent;background:transparent;color:var(--muted);padding:7px 9px;border-radius:7px;cursor:pointer}.view-btn:hover,.view-btn.active{color:var(--text);background:var(--panel2);border-color:var(--line)}
.actions{display:flex;gap:6px;margin-left:6px}.action-btn{border:1px solid var(--line);background:var(--panel2);color:var(--text);padding:7px 12px;border-radius:8px;cursor:pointer;font-weight:600}.action-btn:hover,.action-btn.active{border-color:var(--cyan);color:var(--cyan)}
.health{margin-left:auto;display:flex;align-items:center;gap:8px;color:var(--muted)}.dot{width:8px;height:8px;border-radius:50%;background:var(--cyan);box-shadow:0 0 12px var(--cyan)}
.layout{min-height:0;display:grid;grid-template-columns:270px minmax(360px,1fr) 350px}.side,.detail{overflow:auto;background:#0d131d}.side{border-right:1px solid var(--line);padding:18px}.detail{border-left:1px solid var(--line);padding:18px}.main{min-width:0;min-height:0;position:relative;background:linear-gradient(#ffffff05 1px,transparent 1px),linear-gradient(90deg,#ffffff05 1px,transparent 1px);background-size:36px 36px}
.detail-head{display:flex;align-items:center;justify-content:space-between;margin:20px 0 10px}.detail-head h2{margin:0}.expand-btn{border:1px solid var(--line);background:var(--panel2);color:var(--muted);width:26px;height:26px;border-radius:7px;cursor:pointer;font-size:13px;line-height:1;display:flex;align-items:center;justify-content:center;flex:none}.expand-btn:hover{color:var(--text);border-color:var(--cyan)}.expand-btn.active{color:var(--cyan);border-color:var(--cyan)}
h2{font-size:11px;text-transform:uppercase;letter-spacing:.14em;color:var(--muted);margin:20px 0 10px}.stats{display:grid;grid-template-columns:1fr 1fr;gap:8px}.stat{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:12px}.stat b{font-size:22px;display:block;letter-spacing:-.04em}.stat span{font-size:11px;color:var(--muted)}
.rows{display:grid;gap:7px}.row{display:flex;justify-content:space-between;gap:12px;padding:8px 10px;border-radius:7px;background:var(--panel);color:var(--muted)}.row strong{color:var(--text)}.badge{border-radius:999px;padding:3px 8px;background:#243248;color:#aecaef;font-size:11px}.badge.good{background:#173b35;color:#70e0c8}.badge.warn{background:#49361c;color:#ffd183}.badge.bad{background:#492127;color:#ff9ba2}
canvas{width:100%;height:100%;display:block}.graph-meta{position:absolute;left:16px;top:64px;background:#0b111bd9;border:1px solid var(--line);border-radius:10px;padding:10px 12px;color:var(--muted);backdrop-filter:blur(12px);z-index:2}.graph-meta b{color:var(--text)}
.graph-view{position:absolute;inset:0;background:radial-gradient(circle at 50% 35%,#0d1b2c 0,#090d14 62%)}.graph-tools{position:absolute;top:16px;left:50%;transform:translateX(-50%);display:flex;gap:4px;background:#0b111bd9;border:1px solid var(--line);border-radius:10px;padding:5px;backdrop-filter:blur(12px);z-index:3}.graph-tools button{border:1px solid transparent;background:transparent;color:var(--muted);padding:6px 12px;border-radius:7px;cursor:pointer;font-size:12px}.graph-tools button.active,.graph-tools button:hover{color:var(--text);background:var(--panel2);border-color:var(--line)}#displayTools{left:auto;right:16px;transform:none}
.graph-legend{position:absolute;left:16px;bottom:14px;display:flex;gap:10px;flex-wrap:wrap;background:#0b111bd9;border:1px solid var(--line);border-radius:10px;padding:8px 10px;backdrop-filter:blur(12px);z-index:2}.legend-chip{display:inline-flex;align-items:center;gap:6px;color:var(--muted);font-size:11px}.legend-chip i{width:9px;height:9px;border-radius:50%;display:inline-block;box-shadow:0 0 6px currentColor}.legend-chip i.bond{width:20px;height:8px;border-radius:999px;box-shadow:none}
.graph-loading{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;background:#0b111bcc;z-index:2;opacity:1;transition:opacity .3s;backdrop-filter:blur(4px)}.graph-loading.hidden{opacity:0;pointer-events:none}.spinner{width:36px;height:36px;border:3px solid var(--line);border-top-color:var(--cyan);border-radius:50%;animation:spin .9s linear infinite}@keyframes spin{to{transform:rotate(360deg)}}
.graph-empty{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;z-index:2;pointer-events:none}.graph-empty.hidden{display:none}.graph-empty-card{pointer-events:auto;width:min(340px,80vw);text-align:center;background:#0b111bd9;border:1px solid var(--line);border-radius:14px;padding:26px 24px;backdrop-filter:blur(12px)}.graph-empty-card h3{margin:0 0 8px;font-size:16px;letter-spacing:-.02em}.graph-empty-card p{margin:0 0 16px;color:var(--muted);line-height:1.55}.graph-empty-card .btn{width:100%}.graph-empty-card .btn.hidden{display:none}.graph-empty-card .hint{margin:14px 0 0;font-size:11px;color:var(--muted)}.graph-empty-card .hint.hidden{display:none}.graph-empty-card .hint code{font:11px ui-monospace,SFMono-Regular,Menlo,monospace;background:var(--panel2);border-radius:4px;padding:1px 5px}
.collection{position:absolute;inset:0;overflow:auto;padding:22px;background:#0b111b}.hidden{display:none}.collection-head{display:flex;align-items:end;justify-content:space-between;margin-bottom:16px}.collection-head h1{margin:0;font-size:24px}.collection-head span{color:var(--muted)}.collection-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(250px,1fr));gap:10px}.item-card{border:1px solid var(--line);background:var(--panel);color:var(--text);padding:13px;border-radius:10px;text-align:left;cursor:pointer}.item-card:hover{border-color:var(--blue);transform:translateY(-1px)}.item-card b{display:block;margin-bottom:7px}.item-card small{display:block;color:var(--muted);line-height:1.45}.item-card .badge{display:inline-block;margin-top:9px}
.result{display:block;width:100%;text-align:left;border:1px solid transparent;background:var(--panel);color:var(--text);border-radius:9px;padding:11px;margin-bottom:7px;cursor:pointer}.result:hover{border-color:var(--blue);background:var(--panel2)}.result small{display:block;color:var(--muted);margin-top:4px}.empty{color:var(--muted);padding:14px;border:1px dashed var(--line);border-radius:9px}
.title{font-size:22px;letter-spacing:-.035em;margin:4px 0}.meta{color:var(--muted);margin-bottom:14px}.content{white-space:pre-wrap;word-break:break-word;line-height:1.6;font:13px ui-monospace,SFMono-Regular,Menlo,monospace;background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:14px;min-height:180px}.proposal{padding:10px;border:1px solid var(--line);border-radius:9px;margin-bottom:7px}.proposal b{display:block}.proposal small{color:var(--muted)}.proposal-actions{display:flex;gap:6px;margin-top:9px}.proposal-actions button{flex:1;border:1px solid var(--line);background:var(--panel2);color:var(--text);border-radius:7px;padding:6px;cursor:pointer;font-size:12px}.proposal-actions .approve:hover{border-color:var(--cyan);color:var(--cyan)}.proposal-actions .reject:hover{border-color:var(--red);color:var(--red)}
.modal-backdrop{position:fixed;inset:0;background:#000a;z-index:20;display:flex;align-items:center;justify-content:center;backdrop-filter:blur(6px)}.modal-backdrop.hidden{display:none}.modal{width:min(560px,92vw);background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:20px;max-height:86vh;overflow:auto}.modal h3{margin:0 0 4px}.modal .hint{color:var(--muted);font-size:12px;margin:0 0 6px}.modal label{display:block;font-size:11px;text-transform:uppercase;letter-spacing:.12em;color:var(--muted);margin:12px 0 6px}.modal textarea,.modal input[type=text]{width:100%;border:1px solid var(--line);background:#0d1522;color:var(--text);border-radius:9px;padding:10px;outline:none;font:inherit}.modal textarea{min-height:110px;resize:vertical;font:13px ui-monospace,ui-sans-serif,monospace}.modal textarea:focus,.modal input:focus{border-color:var(--blue);box-shadow:0 0 0 3px #63a5ff1c}.modal .check{display:flex;align-items:center;gap:8px;text-transform:none;letter-spacing:0;font-size:13px;color:var(--text);margin-top:14px}.modal .row{display:flex;gap:8px;justify-content:flex-end;margin-top:18px}.modal input[type=range]{width:100%;accent-color:var(--cyan)}.modal input[type=color]{width:38px;height:32px;padding:2px;border:1px solid var(--line);border-radius:7px;background:var(--panel2);cursor:pointer}.color-mode-row{display:flex;align-items:center;gap:8px}.btn{border:1px solid var(--line);background:var(--panel2);color:var(--text);padding:9px 16px;border-radius:9px;cursor:pointer;font-weight:600}.btn:hover{border-color:var(--cyan)}.btn.ghost{background:transparent;color:var(--muted)}.btn.ghost:hover{color:var(--text);border-color:var(--line)}.btn:disabled{opacity:.5;cursor:default}
.toast{position:fixed;bottom:20px;left:50%;transform:translateX(-50%);background:#0b111be6;border:1px solid var(--line);border-radius:10px;padding:10px 16px;color:var(--text);z-index:30;opacity:0;transition:opacity .25s;backdrop-filter:blur(12px);max-width:80vw}.toast.show{opacity:1}
.loading{opacity:.55}.footer-note{position:absolute;right:14px;bottom:12px;color:var(--muted);font-size:11px;background:#0b111bc9;padding:5px 8px;border-radius:6px;z-index:2;max-width:340px;text-align:right;line-height:1.6}@media(max-width:1050px){.layout{grid-template-columns:220px 1fr}.detail{position:absolute;right:0;top:64px;bottom:0;width:min(88vw,380px);transform:translateX(100%);transition:.2s;z-index:5}.detail.open{transform:none}}@media(max-width:700px){.layout{grid-template-columns:1fr}.side{display:none}.top{padding:0 12px;gap:8px}.health{display:none}.actions .action-btn{padding:7px 8px;font-size:12px}.search{max-width:220px}}
.detail.expanded{position:fixed;inset:0;z-index:16;transform:none;border:none;padding:40px 5vw;overflow:auto;background:var(--bg);display:flex;flex-direction:column;align-items:center}.detail.expanded .detail-head,.detail.expanded #results{width:100%;max-width:820px}.detail.expanded .content{padding:20px 24px}
.content-head{display:flex;align-items:center;justify-content:space-between;gap:10px;flex-wrap:wrap}.view-toggle{display:flex;gap:4px;flex:none}.toggle-btn{border:1px solid var(--line);background:var(--panel2);color:var(--muted);padding:4px 11px;border-radius:999px;cursor:pointer;font-size:11px;font-weight:600}.toggle-btn:hover{color:var(--text);border-color:var(--cyan)}.toggle-btn.active{color:var(--cyan);border-color:var(--cyan)}
.md-content{line-height:1.65;font:13px Inter,ui-sans-serif,system-ui,sans-serif;background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:14px 16px;min-height:180px;overflow-wrap:anywhere}.md-content>*:first-child{margin-top:0}.md-content h1,.md-content h2,.md-content h3,.md-content h4,.md-content h5,.md-content h6{margin:18px 0 8px;line-height:1.3;color:var(--text);letter-spacing:-.02em;text-transform:none}.md-content h1{font-size:20px}.md-content h2{font-size:18px}.md-content h3{font-size:16px}.md-content h4,.md-content h5,.md-content h6{font-size:14px;color:var(--muted)}.md-content p{margin:0 0 10px}.md-content code{font:12px ui-monospace,SFMono-Regular,Menlo,monospace;background:var(--panel2);border-radius:4px;padding:1px 5px;overflow-wrap:anywhere}.md-content pre{font:12px ui-monospace,SFMono-Regular,Menlo,monospace;background:var(--panel2);border:1px solid var(--line);border-radius:8px;padding:10px 12px;overflow:auto;margin:0 0 12px}.md-content pre code{background:none;padding:0}.md-content a{color:var(--blue);overflow-wrap:anywhere}.md-content blockquote{margin:0 0 12px;padding:2px 14px;border-left:3px solid var(--line);color:var(--muted)}.md-content ul,.md-content ol{margin:0 0 12px;padding-left:22px}.md-content li{margin:3px 0}.md-content hr{border:none;border-top:1px solid var(--line);margin:16px 0}.md-content .table-wrap{overflow-x:auto;margin:0 0 12px}.md-content table{border-collapse:collapse;width:100%;margin:0}.md-content th,.md-content td{border:1px solid var(--line);padding:6px 9px;text-align:left;overflow-wrap:anywhere}.md-content th{color:var(--text);background:var(--panel2)}
.filter-bar{display:flex;flex-wrap:wrap;gap:16px;margin-bottom:16px}.filter-group{display:flex;align-items:center;gap:6px;flex-wrap:wrap}.filter-group-label{color:var(--muted);font-size:10px;text-transform:uppercase;letter-spacing:.1em}.filter-chip{border:1px solid var(--line);background:var(--panel);color:var(--muted);padding:4px 10px;border-radius:999px;cursor:pointer;font-size:11px}.filter-chip:hover{color:var(--text);border-color:var(--blue)}.filter-chip.active{background:var(--panel2);border-color:var(--cyan);color:var(--cyan)}
.timeline-view{position:relative}.timeline-day{margin-bottom:28px}.timeline-day-head{position:sticky;top:0;background:#0b111bf2;backdrop-filter:blur(6px);padding:8px 0 6px;font:11px ui-monospace,SFMono-Regular,Menlo,monospace;text-transform:uppercase;letter-spacing:.12em;color:var(--muted);z-index:1}.timeline-rail{position:relative;margin-top:2px}.timeline-rail::before{content:'';position:absolute;left:4px;top:2px;bottom:2px;width:1px;background:var(--line)}.timeline-row{position:relative;display:flex;align-items:center;gap:10px;width:100%;text-align:left;background:transparent;border:none;color:var(--text);padding:4px 0 4px 20px;cursor:pointer;border-radius:6px}.timeline-row:hover{background:var(--panel)}.timeline-dot{position:absolute;left:0;top:50%;transform:translateY(-50%)}.dot.blue{background:var(--blue);box-shadow:0 0 12px var(--blue)}.timeline-time{font:11px ui-monospace,SFMono-Regular,Menlo,monospace;color:var(--muted);flex:none;width:40px}.timeline-label{flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.timeline-now{display:flex;align-items:center;gap:8px;color:var(--muted);font:11px ui-monospace,SFMono-Regular,Menlo,monospace;text-transform:uppercase;letter-spacing:.12em;margin-bottom:14px}.timeline-now-dot{width:7px;height:7px;border-radius:50%;background:var(--cyan);box-shadow:0 0 10px var(--cyan);animation:timelinePulse 1.6s ease-in-out infinite}@keyframes timelinePulse{0%,100%{opacity:1;transform:scale(1)}50%{opacity:.35;transform:scale(1.5)}}
.feed-card{position:relative;padding:4px 0 4px 20px;margin-bottom:14px}.feed-card .timeline-dot{top:22px}.feed-card-head{display:flex;align-items:center;gap:12px;width:100%;text-align:left;border:1px solid var(--line);background:var(--panel);border-radius:12px;padding:10px 12px;cursor:pointer;color:var(--text)}.feed-card-head:hover{border-color:var(--blue)}.feed-cover{flex:none;width:64px;height:64px;border-radius:10px;overflow:hidden;line-height:0}.feed-cover svg{display:block;width:100%;height:100%}.feed-meta{flex:1;min-width:0;display:flex;flex-direction:column;gap:4px}.feed-time{font:11px ui-monospace,SFMono-Regular,Menlo,monospace;color:var(--muted)}.feed-title{font-weight:650;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.feed-body{margin-top:8px;padding:12px 14px;border:1px solid var(--line);border-radius:10px;background:var(--panel2)}.feed-body.hidden{display:none}.feed-excerpt{overflow:hidden;max-height:120px;min-height:0;-webkit-mask-image:linear-gradient(#000 60%,transparent);mask-image:linear-gradient(#000 60%,transparent)}.feed-open{margin-top:8px;border:1px solid var(--line);background:transparent;color:var(--cyan);padding:5px 12px;border-radius:999px;cursor:pointer;font-size:11px;font-weight:600}.feed-open:hover{border-color:var(--cyan)}
.chat{position:absolute;inset:0;display:flex;flex-direction:column;background:#0b111b}.chat.hidden{display:none}.chat-head{display:flex;align-items:center;justify-content:space-between;width:100%;max-width:760px;margin:0 auto;padding:18px 22px 6px}.chat-head h1{margin:0;font-size:24px}.chat-thread{flex:1;min-height:0;overflow:auto}.chat-col{max-width:760px;margin:0 auto;padding:10px 22px 18px;display:flex;flex-direction:column;gap:12px}
.chat-user{align-self:flex-end;max-width:85%;background:var(--panel2);border:1px solid var(--line);border-radius:12px 12px 4px 12px;padding:9px 12px;white-space:pre-wrap;overflow-wrap:anywhere}.chat-answer{min-height:0}.chat-answer-wrap{display:flex;flex-direction:column;gap:6px}.chat-answer-meta{display:flex;align-items:center;gap:8px;flex-wrap:wrap;color:var(--muted);font-size:11px}.chat-unc{flex:1 1 100%;line-height:1.5}.chat-error{align-self:flex-start;max-width:85%;border:1px solid #49212799;background:#49212733;color:#ff9ba2;border-radius:12px 12px 12px 4px;padding:9px 12px;font-size:13px;overflow-wrap:anywhere}.chat-empty{border:1px dashed var(--line);border-radius:12px;padding:26px 22px;text-align:center;color:var(--muted)}.chat-empty p{margin:0 0 14px}.chat-chips{display:flex;gap:8px;justify-content:center;flex-wrap:wrap}
.chat-thinking{align-self:flex-start;color:var(--muted);font-size:12px;padding:4px 2px}.chat-thinking i{display:inline-block;width:5px;height:5px;border-radius:50%;background:var(--muted);margin-right:4px;vertical-align:middle}@media(prefers-reduced-motion:no-preference){.chat-thinking i{animation:chatPulse 1.2s ease-in-out infinite}.chat-thinking i:nth-child(2){animation-delay:.2s}.chat-thinking i:nth-child(3){animation-delay:.4s}}@keyframes chatPulse{0%,100%{opacity:.25}50%{opacity:1}}
.chat-composer{border-top:1px solid var(--line);background:#0d131d;padding:12px 22px}.composer-inner{max-width:760px;margin:0 auto}.composer-box{display:flex;gap:8px;align-items:flex-end}.composer-box textarea{flex:1;border:1px solid var(--line);background:#0d1522;color:var(--text);border-radius:10px;padding:10px 12px;outline:none;font:inherit;resize:none;max-height:150px;line-height:1.45}.composer-box textarea:focus{border-color:var(--blue);box-shadow:0 0 0 3px #63a5ff1c}.composer-row{display:flex;align-items:center;justify-content:space-between;gap:10px;margin-top:8px;flex-wrap:wrap}.chat-save{display:flex;align-items:center;gap:7px;font-size:12px;color:var(--muted)}.chat-note{margin:6px 0 0;font-size:11px;color:var(--muted)}
</style>
</head>
<body>
<div class="shell">
  <header class="top"><div class="brand">brains<span>kit</span></div><nav class="nav"><button class="view-btn active" data-view="graph">Graph</button><button class="view-btn" data-view="sources">Sources</button><button class="view-btn" data-view="pages">Wiki</button><button class="view-btn" data-view="timeline">Timeline</button><button class="view-btn" data-view="integrations">Services</button></nav><div class="search"><input id="search" placeholder="Search the compiled brain…" autocomplete="off"><span class="key">⌘ K</span></div><div class="actions"><button id="captureBtn" class="action-btn" title="Add a note, document or URL to the vault">Capture</button><button id="askBtn" class="action-btn" title="Ask the compiled brain">Ask</button></div><div class="health"><span class="dot"></span><span id="health">loading</span></div></header>
  <div class="layout">
    <aside class="side"><h2>Vault</h2><div class="stats" id="stats"></div><h2>Freshness</h2><div class="rows" id="freshness"></div><h2>Branches</h2><div class="rows" id="branches"></div><h2>Review queue</h2><div id="proposals"></div></aside>
    <main class="main"><div class="graph-view" id="graphView"><div class="graph-tools" id="graphTools"><button data-source="knowledge" class="active">Knowledge</button><button data-source="enrichment">+ Inferred</button><button data-source="code">Code</button></div><div class="graph-tools" id="displayTools"><button type="button" id="resetViewBtn" title="Reset view (0)">⌂</button><button type="button" id="displayBtn">Display</button></div><canvas id="graph"></canvas><div class="graph-meta" id="graphMeta">Loading graph…</div><div class="graph-legend" id="graphLegend"></div><div class="graph-loading" id="graphLoading"><div class="spinner"></div></div><div class="graph-empty hidden" id="graphEmpty"><div class="graph-empty-card"><h3 id="graphEmptyTitle"></h3><p id="graphEmptyBody"></p><button type="button" class="btn" id="graphEmptyAction"></button><p class="hint hidden" id="graphEmptyCli"></p></div></div><div class="footer-note">drag to orbit · scroll to zoom · right-drag to pan · drag a node · click to inspect · double-click to fly · 0 to reset</div></div><section class="collection hidden" id="collection"><div class="collection-head"><h1 id="collectionTitle">Sources</h1><span id="collectionMeta"></span></div><div class="filter-bar" id="collectionFilters"></div><div class="collection-grid" id="collectionGrid"></div><div class="timeline-view hidden" id="timelineView"></div></section><section class="chat hidden" id="chatView"><div class="chat-head"><h1>Ask the brain</h1><button type="button" class="btn ghost" id="chatClear">Clear</button></div><div class="chat-thread" id="chatThread"><div class="chat-col" id="chatCol"></div></div><div class="chat-composer"><div class="composer-inner"><div class="composer-box"><textarea id="chatInput" rows="1" placeholder="Ask the compiled brain…"></textarea><button type="button" class="btn" id="chatSend">Send</button></div><div class="composer-row"><label class="chat-save"><input type="checkbox" id="askSave"> Save the answer to output/answers</label></div><p class="chat-note">Answers cite evidence from the vault; the conversation is context for follow-ups.</p></div></div></section></main>
    <aside class="detail" id="detail"><div class="detail-head"><h2>Inspector</h2><button id="expandDetail" class="expand-btn" type="button" title="Expand to fill the screen" aria-label="Expand inspector">⤢</button></div><div id="results"><div class="empty">Select a graph node or search the vault.</div></div></aside>
  </div>
</div>
<div class="modal-backdrop hidden" id="captureModal"><div class="modal"><h3>Capture into the vault</h3><p class="hint">You can also drop a text file anywhere on this page.</p><label>Pasted text / a document's contents</label><textarea id="captureText" placeholder="Paste a note, article, snippet…"></textarea><label>Or a URL (stored as a link note)</label><input type="text" id="captureUrl" placeholder="https://…"><label>Title (optional)</label><input type="text" id="captureTitle" placeholder="Leave blank to auto-name"><div class="row"><button class="btn ghost" data-close>Cancel</button><button class="btn" id="captureGo">Capture</button></div></div></div>
<div class="modal-backdrop hidden" id="displayModal"><div class="modal"><h3>Display</h3><p class="hint">Node size, edge visibility and color mode — applied live, saved to this browser.</p><label>Node size</label><input type="range" id="nodeSizeRange" min="2" max="20" step="1"><label>Edge opacity</label><input type="range" id="edgeOpacityRange" min="0" max="100" step="5"><label>Color mode</label><div class="color-mode-row"><button type="button" class="toggle-btn active" id="colorModeMulti">Multi-color</button><button type="button" class="toggle-btn" id="colorModeMono">Single color</button><input type="color" id="monoColorPicker" value="#63a5ff"></div><div class="row"><button class="btn ghost" data-close>Close</button></div></div></div>
<div class="toast" id="toast"></div>
<script>
const NODES_LIMIT=1100;
const KIND_RGB={raw:[0.33,0.84,0.75],concept:[0.39,0.65,1.0],entity:[0.96,0.75,0.42],synthesis:[0.49,0.91,0.53],system:[0.71,0.61,1.0],source:[0.33,0.84,0.75],page:[0.39,0.65,1.0],default:[0.71,0.61,1.0]};
const EXT_RGB={py:[0.39,0.65,1.0],ts:[0.33,0.84,0.75],js:[0.96,0.75,0.42],go:[0.42,0.87,0.8],rs:[0.96,0.62,0.45],java:[0.96,0.55,0.48],md:[0.8,0.76,0.62],sql:[0.7,0.6,0.95],sh:[0.66,0.76,0.87],json:[0.58,0.58,0.66],html:[0.96,0.75,0.42],css:[0.6,0.6,0.96]};
const state={graph:null,nodes:[],edges:[],pos:null,colTarget:null,bondColTarget:null,scaleFactors:null,idx:{},adj:null,edgeVerts:[],degree:{},hovered:null,selected:null,dragging:null,userMoved:false,source:'knowledge',cache:{},graph_cache:{},sim:null,labelCache:{},collectionFilters:{},coverCache:{},resourceCache:{},chatThread:[],askPending:false,reveal:null,revealDelay:null};
const G={renderer:null,scene:null,camera:null,atoms:null,bonds:null,bondMode:null,stars:null,label:null,rings:[],halos:[],raycaster:null,textureCache:{},scr:null,cam:{theta:0.6,phi:1.05,dist:330,distTarget:330,tx:0,ty:0,tz:0},fly:null,spin:{t:0,p:0},spinT:0,lastClick:null,lastInteract:0,colorsAnimating:false,fogR:null};
let tGlobal=0;
const RING_PULSE_AMPLITUDE=0.04;
const ORBIT_DAMPING=0.92,ZOOM_EASE=0.18,COLOR_EASE=0.3,IDLE_DRIFT=0.0004,IDLE_DELAY_MS=6000,DBLCLICK_MS=300;
const ATOM_SCALE_K=0.35,ATOM_SCALE_MAX=2.4,BOND_SPLIT_MAX_EDGES=6000;
const FOG_NEAR_K=1.5,FOG_FAR_K=2.5,FOG_REFIT_MS=200;
const ALPHA_DECAY=0.995,ALPHA_MIN=0.003,WAKE_ALPHA=0.3,KE_SLEEP=0.0001,FLING_GAIN=0.5,FLING_MAX=6,DRAG_VEL_STALE_MS=90;
const REVEAL_SPAN_MIN=1200,REVEAL_SPAN_MAX=2500,REVEAL_ATOM_MS=300,REVEAL_BOND_MS=250;
const RIM_INTENSITY=0.35,RIM_POWER=2.5,HALO_COUNT=8,HALO_SCALE=2.2,HALO_OPACITY=0.35;
const REDUCED_MOTION=matchMedia('(prefers-reduced-motion: reduce)').matches;
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
function invalidateData(){state.cache={};state.graph_cache={};state.resourceCache={}}
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
async function decideProposal(p,action){let reason='';if(action==='reject'){reason=window.prompt('Reason for rejecting '+p.proposal_id,'')||''}try{await api('/api/proposals/'+action,{method:'POST',body:JSON.stringify({id:p.proposal_id,reason})});toast(action==='approve'?'Proposal applied to the wiki':'Proposal rejected');invalidateData();load()}catch(error){toast(error.message)}}
let timer;let searchSeq=0;document.getElementById('search').addEventListener('input',event=>{clearTimeout(timer);let q=event.target.value.trim();if(!q){searchSeq++;let root=document.getElementById('results');root.classList.remove('loading');root.innerHTML='<div class="empty">Select a graph node or search the vault.</div>';return}timer=setTimeout(()=>search(q),180)});async function search(q){const seq=++searchSeq;let root=document.getElementById('results');root.classList.add('loading');try{let data=await api('/api/search?q='+encodeURIComponent(q)+'&limit=20');if(seq!==searchSeq)return;root.replaceChildren(...data.hits.map(hit=>{let b=document.createElement('button');b.className='result';let title=document.createElement('span');let meta=document.createElement('small');escText(title,hit.title);escText(meta,hit.kind+' · '+hit.privacy+' · '+hit.path);b.append(title,meta);b.onclick=()=>showResource(hit.content_hash?'raw:'+hit.content_hash:'page:'+hit.path);return b}));if(!data.hits.length)root.innerHTML='<div class="empty">No matching evidence.</div>';document.getElementById('detail').classList.add('open')}catch(error){if(seq!==searchSeq)return;root.innerHTML='<div class="empty"></div>';escText(root.firstChild,error.message)}finally{if(seq===searchSeq)root.classList.remove('loading')}}
const COLLECTION_TITLES={sources:'Sources',pages:'Compiled wiki',timeline:'Timeline',integrations:'Persistent services'};
const COLLECTION_FILTER_FIELDS={sources:['branch','privacy'],pages:['kind','freshness'],timeline:['type'],integrations:['state']};
function itemsFor(name,data){return name==='sources'?data.sources:name==='pages'?data.pages:name==='timeline'?data.events:data.integrations}
function collectionCountLabel(filtered,total,redacted,unit){unit=unit||'items';let label=filtered===total?total+' '+unit:filtered+' of '+total+' '+unit;if(redacted)label+=' · '+redacted+' private';return label}
function buildFilterBar(name,items){let fields=COLLECTION_FILTER_FIELDS[name]||[];let frag=document.createDocumentFragment();if(!fields.length)return frag;let selected=state.collectionFilters[name]||(state.collectionFilters[name]={});fields.forEach(field=>{if(!selected[field])selected[field]=new Set();let counts={};items.forEach(it=>{let v=it[field];if(v===undefined||v===null||v==='')return;counts[v]=(counts[v]||0)+1});let values=Object.keys(counts).sort();if(values.length<2)return;let group=document.createElement('div');group.className='filter-group';let label=document.createElement('span');label.className='filter-group-label';escText(label,field);group.append(label);values.forEach(value=>{let chip=document.createElement('button');chip.type='button';chip.className='filter-chip'+(selected[field].has(value)?' active':'');escText(chip,value+' ('+counts[value]+')');chip.onclick=()=>{if(selected[field].has(value)){selected[field].delete(value)}else{selected[field].add(value)}renderCollectionView(name,state.cache[name])};group.append(chip)});frag.append(group)});return frag}
function applyFilters(name,items){let selected=state.collectionFilters[name];if(!selected)return items;return items.filter(it=>Object.keys(selected).every(field=>{let set=selected[field];return !set||!set.size||set.has(it[field])}))}
async function switchView(name){document.querySelectorAll('.view-btn').forEach(button=>button.classList.toggle('active',button.dataset.view===name));document.getElementById('chatView').classList.add('hidden');document.getElementById('askBtn').classList.remove('active');let graph=name==='graph';document.getElementById('graphView').classList.toggle('hidden',!graph);document.getElementById('collection').classList.toggle('hidden',graph);if(graph){resize3D();return}state.collectionFilters[name]={};let endpoints={sources:'/api/sources',pages:'/api/pages',timeline:'/api/timeline',integrations:'/api/integrations'};try{let data=state.cache[name]||await api(endpoints[name]);state.cache[name]=data;renderCollectionView(name,data)}catch(error){renderCollectionView(name,{error:error.message})}}
function renderCollectionView(name,data){let filterBar=document.getElementById('collectionFilters');if(data.error){filterBar.replaceChildren();renderCollection(name,data);return}let all=itemsFor(name,data);filterBar.replaceChildren(buildFilterBar(name,all));let filtered=applyFilters(name,all);if(name==='timeline'){document.getElementById('collectionGrid').classList.add('hidden');escText(document.getElementById('collectionTitle'),COLLECTION_TITLES.timeline);escText(document.getElementById('collectionMeta'),collectionCountLabel(filtered.length,all.length,data.redacted,'events'));renderTimeline(filtered)}else{renderCollection(name,data,filtered)}}
function renderCollection(name,data,itemsOverride){let title=document.getElementById('collectionTitle'),meta=document.getElementById('collectionMeta'),grid=document.getElementById('collectionGrid');document.getElementById('timelineView').classList.add('hidden');grid.classList.remove('hidden');escText(title,COLLECTION_TITLES[name]);if(data.error){meta.textContent='';grid.innerHTML='<div class="empty"></div>';escText(grid.firstChild,data.error);return}let all=itemsFor(name,data);let items=itemsOverride||all;escText(meta,collectionCountLabel(items.length,all.length,data.redacted));if(!items.length){grid.innerHTML='<div class="empty">No items match the selected filters.</div>';return}grid.replaceChildren(...items.map(item=>{let card=document.createElement('button');card.className='item-card';let heading=document.createElement('b'),info=document.createElement('small'),badge=document.createElement('span');badge.className='badge '+(item.state==='running'||item.state==='ready'||item.freshness==='fresh'?'good':'');let badgeText=item.freshness||item.state||item.type;let label=item.title||item.label||item.name||item.type;let details=name==='sources'?item.branch+' · '+item.privacy+' · '+item.status:name==='pages'?item.kind+' · '+item.privacy+' · '+item.path:name==='timeline'?item.type+' · '+item.detail+' · '+String(item.at).slice(0,19):(item.managed?'managed':'external');if(name==='integrations'&&item.name==='web'){badge.className='badge good';badgeText='live';details='serving this tab · persistent service '+item.state+' · '+(item.managed?'managed':'external')}escText(heading,label);escText(info,details);escText(badge,badgeText);card.append(heading,info,badge);let id=item.id;if(id)card.onclick=()=>showResource(id);return card}))}
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
document.getElementById('captureGo').onclick=async()=>{let text=document.getElementById('captureText').value;let url=document.getElementById('captureUrl').value.trim();let title=document.getElementById('captureTitle').value.trim();if(!text.trim()&&!url){toast('Paste text or enter a URL');return}let body={};if(text.trim())body.text=text;if(url)body.url=url;if(title)body.title=title;let btn=document.getElementById('captureGo');btn.disabled=true;try{let r=await api('/api/capture',{method:'POST',body:JSON.stringify(body)});toast(r.created?'Captured '+r.source.original_name:'Already in the vault: '+r.source.original_name);closeModals();document.getElementById('captureText').value='';document.getElementById('captureUrl').value='';document.getElementById('captureTitle').value='';showResource('raw:'+r.source.content_hash);invalidateData();if(state.source!=='code')loadGraph(state.source);load()}catch(error){toast(error.message)}finally{btn.disabled=false}};
/* Ask is a full chat view, not a modal. Retrieval stays keyed on the bare
   question (history inside the BM25 query would bury it), but completed
   exchanges ride along as `history` so follow-ups can resolve their
   pronouns; the server bounds it to the last exchanges, mirrored here. */
const CHAT_STORAGE_KEY='brainskit-ask-thread',CHAT_TURN_CAP=50,CHAT_HISTORY_MAX=6;
const CHAT_EXAMPLES=['What changed this week?','What do we know so far?','Where are the open questions?'];
function loadChatThread(){try{let raw=localStorage.getItem(CHAT_STORAGE_KEY);let turns=raw?JSON.parse(raw):[];return Array.isArray(turns)?turns.slice(-CHAT_TURN_CAP):[]}catch(error){return[]}}
function saveChatThread(){try{localStorage.setItem(CHAT_STORAGE_KEY,JSON.stringify(state.chatThread.slice(-CHAT_TURN_CAP)))}catch(error){}}
state.chatThread=loadChatThread();
function openChat(){document.querySelectorAll('.view-btn').forEach(b=>b.classList.remove('active'));document.getElementById('graphView').classList.add('hidden');document.getElementById('collection').classList.add('hidden');document.getElementById('chatView').classList.remove('hidden');document.getElementById('askBtn').classList.add('active');renderChat();document.getElementById('chatInput').focus()}
function chatAnswerCard(r){let wrap=document.createElement('div');wrap.className='chat-answer-wrap';let box=document.createElement('div');box.className='md-content chat-answer';box.innerHTML=mdToHtml(r.answer);let meta=document.createElement('div');meta.className='chat-answer-meta';let count=r.citations||0;let cites=document.createElement('span');escText(cites,count+' citation'+(count===1?'':'s'));meta.append(cites);if(r.provider&&r.model){let mc=document.createElement('span');mc.className='chat-model';escText(mc,r.provider+' · '+r.model);meta.append(mc)}let unc=String(r.uncertainty||'').trim();if(unc){let level=unc.toLowerCase();let b=document.createElement('span');b.className='badge '+(level.indexOf('high')!==-1?'warn':(level.indexOf('low')!==-1||level.indexOf('none')!==-1?'good':''));let short=unc.length<=40;escText(b,short?'uncertainty '+unc:'uncertainty');meta.append(b);if(!short){let note=document.createElement('span');note.className='chat-unc';escText(note,unc);meta.append(note)}}if(r.saved_to){let saved=document.createElement('span');escText(saved,'saved to '+r.saved_to);meta.append(saved)}wrap.append(box,meta);return wrap}
function renderChat(){let col=document.getElementById('chatCol');col.replaceChildren();if(!state.chatThread.length&&!state.askPending){let empty=document.createElement('div');empty.className='chat-empty';let p=document.createElement('p');escText(p,'Ask the compiled brain — answers cite the evidence they came from.');let chips=document.createElement('div');chips.className='chat-chips';CHAT_EXAMPLES.forEach(example=>{let chip=document.createElement('button');chip.type='button';chip.className='filter-chip';escText(chip,example);chip.onclick=()=>{let input=document.getElementById('chatInput');input.value=example;growComposer();input.focus()};chips.append(chip)});empty.append(p,chips);col.append(empty)}state.chatThread.forEach(turn=>{if(turn.role==='user'){let bubble=document.createElement('div');bubble.className='chat-user';escText(bubble,turn.text);col.append(bubble)}else if(turn.role==='error'){let bubble=document.createElement('div');bubble.className='chat-error';escText(bubble,turn.text);col.append(bubble)}else{col.append(chatAnswerCard(turn))}});if(state.askPending){let think=document.createElement('div');think.className='chat-thinking';think.append(document.createElement('i'),document.createElement('i'),document.createElement('i'),document.createTextNode(' thinking'));col.append(think)}let thread=document.getElementById('chatThread');thread.scrollTop=thread.scrollHeight}
function pushChatTurn(turn){state.chatThread.push(turn);if(state.chatThread.length>CHAT_TURN_CAP)state.chatThread=state.chatThread.slice(-CHAT_TURN_CAP);saveChatThread();renderChat()}
function setChatBusy(busy){state.askPending=busy;document.getElementById('chatSend').disabled=busy;document.getElementById('chatInput').disabled=busy;renderChat()}
/* Completed exchanges only — a question that errored has no answer to quote.
   The last CHAT_HISTORY_MAX mirror the server's own bound so payloads stay
   small, and history is built BEFORE the new user turn joins the thread. */
function chatHistory(){let out=[],question=null;for(let turn of state.chatThread){if(turn.role==='user'){question=turn.text}else if(turn.role==='answer'&&question!=null){out.push({question,answer:turn.answer});question=null}else if(turn.role==='error'){question=null}}return out.slice(-CHAT_HISTORY_MAX)}
async function sendChat(){let input=document.getElementById('chatInput');let q=input.value.trim();if(!q||state.askPending)return;let save=document.getElementById('askSave').checked;let history=chatHistory();input.value='';growComposer();pushChatTurn({role:'user',text:q});setChatBusy(true);try{let r=await api('/api/ask',{method:'POST',body:JSON.stringify({question:q,save,history})});pushChatTurn({role:'answer',answer:r.answer,citations:r.citations?r.citations.length:0,uncertainty:r.uncertainty?String(r.uncertainty):'',saved_to:r.saved_to?String(r.saved_to):'',provider:r.provider?String(r.provider):'',model:r.model?String(r.model):''})}catch(error){pushChatTurn({role:'error',text:error.message})}finally{setChatBusy(false);document.getElementById('chatInput').focus()}}
const chatInputEl=document.getElementById('chatInput');
function growComposer(){chatInputEl.style.height='auto';chatInputEl.style.height=Math.min(chatInputEl.scrollHeight,150)+'px'}
chatInputEl.addEventListener('input',growComposer);
chatInputEl.addEventListener('keydown',e=>{if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();sendChat()}});
document.getElementById('askBtn').onclick=openChat;
document.getElementById('chatSend').onclick=sendChat;
document.getElementById('chatClear').onclick=()=>{state.chatThread=[];localStorage.removeItem(CHAT_STORAGE_KEY);renderChat()};
let dragDepth=0;addEventListener('dragenter',e=>{e.preventDefault();dragDepth++});addEventListener('dragover',e=>e.preventDefault());addEventListener('dragleave',e=>{e.preventDefault();if(--dragDepth<=0)dragDepth=0});addEventListener('drop',async e=>{e.preventDefault();dragDepth=0;let files=[...e.dataTransfer.files];if(!files.length)return;let f=files[0];let text=await f.text();try{let r=await api('/api/capture',{method:'POST',body:JSON.stringify({text,title:f.name})});toast(r.created?'Captured '+f.name:'Already in the vault: '+f.name);showResource('raw:'+r.source.content_hash);invalidateData();if(state.source!=='code')loadGraph(state.source);load()}catch(error){toast(error.message)}});
addEventListener('paste',e=>{let active=document.activeElement,tag=active&&active.tagName;if(tag==='INPUT'||tag==='TEXTAREA'||(active&&active.isContentEditable))return;let text=((e.clipboardData&&e.clipboardData.getData('text/plain'))||'').trim();if(!text)return;e.preventDefault();document.getElementById('captureText').value=text;openModal('captureModal');toast('Pasted — review and Capture')});
/* ---------------- 3D graph ---------------- */
function hexToRgb01(hex){hex=String(hex||'').replace('#','');if(hex.length===3)hex=hex.split('').map(c=>c+c).join('');let n=parseInt(hex,16)||0;return [((n>>16)&255)/255,((n>>8)&255)/255,(n&255)/255]}
function colorFor(node,source){if(display.colorMode==='mono')return hexToRgb01(display.monoColor);if(source==='code'){let ext=(node.path||node.label||'').split('.').pop();return EXT_RGB[ext]||[0.55,0.65,0.85]}return KIND_RGB[node.kind||'default']||KIND_RGB.default}
function dotTexture(){if(G.textureCache._dot)return G.textureCache._dot;let c=document.createElement('canvas');c.width=c.height=64;let g=c.getContext('2d');let gr=g.createRadialGradient(32,32,2,32,32,30);gr.addColorStop(0,'rgba(255,255,255,1)');gr.addColorStop(0.45,'rgba(255,255,255,0.8)');gr.addColorStop(1,'rgba(255,255,255,0)');g.fillStyle=gr;g.fillRect(0,0,64,64);G.textureCache._dot=new THREE.CanvasTexture(c);return G.textureCache._dot}
function roundRect(g,x,y,w,h,r){g.beginPath();g.moveTo(x+r,y);g.arcTo(x+w,y,x+w,y+h,r);g.arcTo(x+w,y+h,x,y+h,r);g.arcTo(x,y+h,x,y,r);g.arcTo(x,y,x+w,y,r);g.closePath()}
function makeStars(){let n=520,arr=new Float32Array(n*3);for(let i=0;i<n;i++){let p=Math.random()*2-1,a=Math.random()*Math.PI*2,r=560+Math.random()*520,rr=r*Math.sqrt(1-p*p);arr[i*3]=Math.cos(a)*rr;arr[i*3+1]=r*p;arr[i*3+2]=Math.sin(a)*rr}let g=new THREE.BufferGeometry();g.setAttribute('position',new THREE.BufferAttribute(arr,3));let m=new THREE.PointsMaterial({size:2.5,color:0x8fb6d8,transparent:true,opacity:0.65,map:dotTexture(),depthWrite:false,fog:false});let p=new THREE.Points(g,m);p.frustumCulled=false;return p}
function labelTexture(text){if(state.labelCache[text])return state.labelCache[text];let c=document.createElement('canvas');c.height=64;let g=c.getContext('2d');g.font='600 32px Inter,system-ui,sans-serif';let w=Math.ceil(g.measureText(text).width);c.width=Math.max(64,w+44);g.font='600 32px Inter,system-ui,sans-serif';g.fillStyle='rgba(8,13,22,0.85)';g.strokeStyle='rgba(38,54,75,0.95)';g.lineWidth=2;roundRect(g,2,4,c.width-4,56,12);g.fill();g.stroke();g.fillStyle='#eef4ff';g.textBaseline='middle';g.textAlign='left';g.fillText(text,22,33);let tex=new THREE.CanvasTexture(c);tex.minFilter=THREE.LinearFilter;tex.generateMipmaps=false;state.labelCache[text]=tex;return tex}
function makeRingMaterial(rgb){let key='ring'+rgb.join(',');if(G.textureCache[key])return G.textureCache[key];let c=document.createElement('canvas');c.width=c.height=128;let g=c.getContext('2d');let gr=g.createRadialGradient(64,64,28,64,64,62);gr.addColorStop(0,'rgba(255,255,255,0)');gr.addColorStop(0.66,'rgba(255,255,255,0)');gr.addColorStop(0.74,'rgba('+(rgb[0]*255|0)+','+(rgb[1]*255|0)+','+(rgb[2]*255|0)+',0.9)');gr.addColorStop(1,'rgba(0,0,0,0)');g.fillStyle=gr;g.fillRect(0,0,128,128);let tex=new THREE.CanvasTexture(c);let mat=new THREE.SpriteMaterial({map:tex,transparent:true,blending:THREE.AdditiveBlending,depthWrite:false});G.textureCache[key]=mat;return mat}
function init3D(){let canvas=document.getElementById('graph');G.renderer=new THREE.WebGLRenderer({canvas,antialias:true,alpha:true});G.renderer.setPixelRatio(Math.min(devicePixelRatio||1,2));G.scene=new THREE.Scene();G.scene.fog=new THREE.Fog(0x0a1019,400,820);G.camera=new THREE.PerspectiveCamera(55,1,0.5,3000);G.scene.add(new THREE.HemisphereLight(0xffffff,0x223344,0.7));let key=new THREE.DirectionalLight(0xffffff,0.9);key.position.set(-0.7,1.2,0.9);G.scene.add(key);G.scr={v1:new THREE.Vector3(),v2:new THREE.Vector3(),s:new THREE.Vector3(),q:new THREE.Quaternion(),m:new THREE.Matrix4(),up:new THREE.Vector3(0,1,0),c:new THREE.Color()};G.raycaster=new THREE.Raycaster();G.stars=makeStars();G.scene.add(G.stars);resize3D();setCamera()}
function resize3D(){if(!G.renderer)return;let rect=G.renderer.domElement.getBoundingClientRect();if(rect.width===0||rect.height===0)return;G.renderer.setSize(rect.width,rect.height,false);G.camera.aspect=rect.width/rect.height;G.camera.updateProjectionMatrix()}
function setCamera(){let c=G.cam,sp=Math.sin(c.phi);G.camera.position.set(c.tx+c.dist*sp*Math.cos(c.theta),c.ty+c.dist*Math.cos(c.phi),c.tz+c.dist*sp*Math.sin(c.theta));G.camera.lookAt(c.tx,c.ty,c.tz)}
function flyTo(px,py,pz,dist,from){G.spin.t=0;G.spin.p=0;if(from)G.cam.dist=from;G.cam.distTarget=dist;G.fly={sx:G.cam.tx,sy:G.cam.ty,sz:G.cam.tz,ex:px,ey:py,ez:pz,sd:G.cam.dist,ed:dist,t:0}}
function flyToOrigin(R,from){flyTo(0,0,0,Math.max(150,R*2.7),from)}
function nodePos(i){return{x:state.pos[i*3],y:state.pos[i*3+1],z:state.pos[i*3+2]}}
function graphRadius(){let r=0;for(let i=0;i<state.nodes.length;i++){r=Math.max(r,Math.hypot(state.pos[i*3],state.pos[i*3+1],state.pos[i*3+2]))}return r}
/* The fog is a depth cue, not a curtain. Pinned to the distance one camera move
   happened to end at, it swallows the whole graph as soon as you scroll past its
   far plane -- zooming out went black. Refitting it to where the camera actually
   is keeps the same near-bright/far-dim gradient at every zoom, so a node is only
   ever dimmed relative to its neighbours, never fogged out of existence. The
   radius is sampled rather than measured per frame: it drifts slowly while the
   simulation settles, and the fog does not need to track it exactly. */
function fogRadius(){let now=performance.now();if(G.fogR&&now-G.fogR.t<FOG_REFIT_MS)return G.fogR.r;let r=Math.max(graphRadius(),40);G.fogR={t:now,r};return r}
function fitFog(dist){if(!G.scene||!G.scene.fog)return;let R=fogRadius();G.scene.fog.near=Math.max(1,dist-R*FOG_NEAR_K);G.scene.fog.far=dist+R*FOG_FAR_K}
function buildGraph(data,source){showGraphEmpty(false);state.selected=null;state.hovered=null;state.dragging=null;state.userMoved=false;G.lastClick=null;G.fogR=null;state.source=source;state.graph=data;state.degree={};for(let e of data.edges){state.degree[e.source]=(state.degree[e.source]||0)+1;state.degree[e.target]=(state.degree[e.target]||0)+1}let nodes=[...data.nodes].sort((a,b)=>(state.degree[b.id]||0)-(state.degree[a.id]||0));let clientHidden=Math.max(0,nodes.length-NODES_LIMIT);nodes=nodes.slice(0,NODES_LIMIT);let ids=new Set(nodes.map(n=>n.id));let edges=data.edges.filter(e=>ids.has(e.source)&&ids.has(e.target));state.nodes=nodes;state.edges=edges;state.idx={};nodes.forEach((nd,i)=>state.idx[nd.id]=i);state.adj=nodes.map(()=>[]);let edgeVerts=[];for(let e of edges){let a=state.idx[e.source],b=state.idx[e.target];if(a===undefined||b===undefined)continue;state.adj[a].push(b);state.adj[b].push(a);edgeVerts.push([a,b])}state.edgeVerts=edgeVerts;
let n=nodes.length,pos=new Float32Array(n*3);let kinds=[...new Set(nodes.map(nd=>nd.kind||'default'))];let band={};kinds.forEach((k,i)=>band[k]=i+1);for(let i=0;i<n;i++){let nd=nodes[i],b=band[nd.kind||'default'],a=(hash(nd.id)%100000)/100000*Math.PI*2,p=(hash(nd.id+'p')%100000)/100000*2-1,r=46+b*38+(hash(nd.id+'r')%60),rr=r*Math.sqrt(1-p*p);pos[i*3]=Math.cos(a)*rr;pos[i*3+1]=r*p;pos[i*3+2]=Math.sin(a)*rr}state.pos=pos;
/* load reveal: BFS order from the biggest hub (nodes are degree-sorted, so index order IS hub order) makes the assembly read as connections spreading outward. Re-armed on every build; skipped outright under reduced motion. */
state.revealDelay=computeRevealDelays();
state.reveal=(REDUCED_MOTION||!n)?null:{start:performance.now(),span:clamp(REVEAL_SPAN_MIN+n/NODES_LIMIT*(REVEAL_SPAN_MAX-REVEAL_SPAN_MIN),REVEAL_SPAN_MIN,REVEAL_SPAN_MAX),done:false};
/* atoms: instanced shaded spheres; per-instance radius encodes degree so hubs read as heavy atoms */
if(G.atoms){G.scene.remove(G.atoms);G.atoms.geometry.dispose();G.atoms.material.dispose()}
let ageom=new THREE.SphereGeometry(1,16,12);let amat=new THREE.MeshPhongMaterial({color:0xffffff,shininess:40,specular:0x404050});
/* Fresnel rim: a view-angle glow injected into the Phong fragment (the vendored three has no postprocessing). Scaled by the lit color's luminance so hover-dimmed atoms stay quiet; injected before tonemapping/fog so distance still attenuates it. */
amat.onBeforeCompile=shader=>{shader.fragmentShader=shader.fragmentShader.replace('#include <output_fragment>','#include <output_fragment>\n\tfloat rimLum=dot(gl_FragColor.rgb,vec3(0.299,0.587,0.114));\n\tfloat rim=pow(1.0-clamp(dot(normalize(vViewPosition),normalize(normal)),0.0,1.0),'+RIM_POWER.toFixed(1)+');\n\tgl_FragColor.rgb+=vec3(0.72,0.9,1.0)*rim*'+RIM_INTENSITY.toFixed(2)+'*clamp(rimLum*1.6,0.0,1.0);')};
G.atoms=new THREE.InstancedMesh(ageom,amat,n);G.atoms.instanceMatrix.setUsage(THREE.DynamicDrawUsage);G.atoms.frustumCulled=false;
state.scaleFactors=new Float32Array(n);for(let i=0;i<n;i++){let deg=state.degree[nodes[i].id]||0;state.scaleFactors[i]=clamp(1+ATOM_SCALE_K*Math.log(1+deg),1,ATOM_SCALE_MAX)}
state.colTarget=new Float32Array(n*3);for(let i=0;i<n;i++){let c=colorFor(nodes[i],source);G.atoms.setColorAt(i,G.scr.c.setRGB(c[0],c[1],c[2]))}
if(G.atoms.instanceColor)G.atoms.instanceColor.needsUpdate=true;
updateAtomMatrices();G.scene.add(G.atoms);
/* bonds: classic split-color molecular bond — two instanced half-cylinders per edge, each colored by its endpoint atom; past the instancing budget, gradient-colored line segments keep the same endpoint coloring */
if(G.bonds){G.scene.remove(G.bonds);G.bonds.geometry.dispose();G.bonds.material.dispose()}
state.bondColTarget=new Float32Array(edgeVerts.length*6);
if(edgeVerts.length<=BOND_SPLIT_MAX_EDGES){let bgeom=new THREE.CylinderGeometry(1,1,1,8,1,true);let bmat=new THREE.MeshPhongMaterial({shininess:30,transparent:true,opacity:display.edgeOpacity});G.bonds=new THREE.InstancedMesh(bgeom,bmat,edgeVerts.length*2);G.bonds.instanceMatrix.setUsage(THREE.DynamicDrawUsage);G.bondMode='split';for(let i=0;i<edgeVerts.length*2;i++)G.bonds.setColorAt(i,G.scr.c.setRGB(1,1,1))}
else{let egeom=new THREE.BufferGeometry();egeom.setAttribute('position',new THREE.BufferAttribute(new Float32Array(edgeVerts.length*6),3));egeom.setAttribute('color',new THREE.BufferAttribute(new Float32Array(edgeVerts.length*6),3));G.bonds=new THREE.LineSegments(egeom,new THREE.LineBasicMaterial({vertexColors:true,transparent:true,opacity:display.edgeOpacity}));G.bondMode='line'}
G.bonds.frustumCulled=false;G.scene.add(G.bonds);updateBondTransforms();refreshHighlight(true);
for(let s of G.rings)G.scene.remove(s.sprite);G.rings=[];if(G.label){G.scene.remove(G.label);G.label=null}
/* hub halos: one additive glow per top-degree atom — static structural anchors, deliberately kept under reduced motion */
for(let h of G.halos)G.scene.remove(h);G.halos=[];for(let i=0;i<Math.min(HALO_COUNT,n);i++){let sp=new THREE.Sprite(makeHaloMaterial(colorFor(nodes[i],source)));sp.userData={i};G.scene.add(sp);G.halos.push(sp)}syncHalos();
let meta=document.getElementById('graphMeta');let metaParts=[(data.total_nodes||data.nodes.length)+' nodes',(data.total_edges||data.edges.length)+' edges'];if(clientHidden>0)metaParts.push(clientHidden+' hidden for rendering');if(data.hidden_nodes)metaParts.push(data.hidden_nodes+' beyond server cap');escText(meta,metaParts.join(' · '));renderLegend(source);
if(source!=='code'&&!data.nodes.some(nd=>nd.kind!=='system')){showGraphEmpty(true,{title:'Nothing captured yet',body:'This graph fills in as you add sources — capture a note, a URL, or a file, and it lands here as soon as brainskit indexes it.',action:{label:'Capture something',onClick:()=>openModal('captureModal')},cli:'bk capture'})}
startSim();let R=Math.max(graphRadius(),40);flyToOrigin(R,900);showGraphLoading(false)}
function renderLegend(source){let el=document.getElementById('graphLegend');let items=source==='code'?[['Python','#63a5ff'],['TypeScript','#55d6be'],['JavaScript','#f4bf6a'],['Go/Rust','#6be0cc'],['Other','#8aa0d9']]:[['raw','#55d6be'],['concept','#63a5ff'],['entity','#f4bf6a'],['synthesis','#7ee787'],['system','#b49cff']];let chips=items.map(([name,c])=>{let chip=document.createElement('span');chip.className='legend-chip';let dot=document.createElement('i');dot.style.background=c;let t=document.createElement('span');escText(t,name);chip.append(dot,t);return chip});let bond=document.createElement('span');bond.className='legend-chip';let cap=document.createElement('i');cap.className='bond';let pair=source==='code'?['#63a5ff','#55d6be']:['#55d6be','#63a5ff'];cap.style.background='linear-gradient(90deg,'+pair[0]+' 50%,'+pair[1]+' 50%)';let bt=document.createElement('span');escText(bt,source==='code'?'bond = its endpoints':'evidence → page');bond.append(cap,bt);chips.push(bond);el.replaceChildren(...chips)}
function atomBaseRadius(){return display.nodeSize*0.38}
function bondRadius(){return atomBaseRadius()*0.18}
/* ---------------- load reveal: the graph assembles hub-outward ---------------- */
function computeRevealDelays(){let n=state.nodes.length,order=new Int32Array(n).fill(-1),queue=new Int32Array(n),k=0;for(let root=0;root<n;root++){if(order[root]!==-1)continue;let head=0,tail=0;queue[tail++]=root;order[root]=k++;while(head<tail){let cur=queue[head++],nb=state.adj[cur];for(let j=0;j<nb.length;j++){let nx=nb[j];if(order[nx]===-1){order[nx]=k++;queue[tail++]=nx}}}}let delays=new Float32Array(n);for(let i=0;i<n;i++)delays[i]=order[i]/Math.max(1,n);return delays}
function revealEase(t){t=clamp(t,0,1);let u=1-t;return 1-u*u*u}
function revealAtomScale(i,now){let rv=state.reveal;return revealEase((now-rv.start-state.revealDelay[i]*rv.span)/REVEAL_ATOM_MS)}
/* A bond only exists once BOTH endpoints have begun appearing; it then grows from its midpoint outward, so connections read as forming through the network. */
function revealBondScale(a,b,now){let rv=state.reveal,begun=rv.start+Math.max(state.revealDelay[a],state.revealDelay[b])*rv.span;return revealEase((now-begun)/REVEAL_BOND_MS)}
function finishReveal(){let rv=state.reveal;if(!rv||rv.done)return;rv.done=true;updateAtomMatrices();updateBondTransforms();if(G.bondMode==='line')refreshHighlight(true)}
function stepReveal(now){let rv=state.reveal;if(now-rv.start>rv.span+Math.max(REVEAL_ATOM_MS,REVEAL_BOND_MS)){finishReveal();return}if(!state.sim||state.sim.sleeping){updateAtomMatrices();updateBondTransforms()}if(G.bondMode==='line'&&G.bonds){let arr=bondColorArray(),bt=state.bondColTarget,ev=state.edgeVerts;for(let i=0;i<ev.length;i++){let g=revealBondScale(ev[i][0],ev[i][1],now);for(let c=0;c<6;c++)arr[i*6+c]=bt[i*6+c]*g}bondColorsChanged()}}
function makeHaloMaterial(rgb){let key='halo'+rgb.join(',');if(G.textureCache[key])return G.textureCache[key];let mat=new THREE.SpriteMaterial({map:dotTexture(),color:new THREE.Color(rgb[0],rgb[1],rgb[2]),transparent:true,opacity:HALO_OPACITY,blending:THREE.AdditiveBlending,depthWrite:false});G.textureCache[key]=mat;return mat}
function syncHalos(){let base=atomBaseRadius(),rv=state.reveal&&!state.reveal.done?performance.now():0;for(let h of G.halos){let i=h.userData.i,d=base*state.scaleFactors[i]*HALO_SCALE*2*(rv?Math.max(revealAtomScale(i,rv),0.001):1);h.position.set(state.pos[i*3],state.pos[i*3+1],state.pos[i*3+2]);h.scale.set(d,d,1)}}
function updateAtomMatrices(){if(!G.atoms)return;let n=state.nodes.length,pos=state.pos,base=atomBaseRadius(),s=G.scr,rv=state.reveal&&!state.reveal.done?performance.now():0;for(let i=0;i<n;i++){let r=base*state.scaleFactors[i];if(rv)r*=Math.max(revealAtomScale(i,rv),0.001);s.m.makeScale(r,r,r);s.m.setPosition(pos[i*3],pos[i*3+1],pos[i*3+2]);G.atoms.setMatrixAt(i,s.m)}G.atoms.instanceMatrix.needsUpdate=true}
function setBondHalf(idx,x1,y1,z1,x2,y2,z2,r,s){let dx=x2-x1,dy=y2-y1,dz=z2-z1,len=Math.sqrt(dx*dx+dy*dy+dz*dz)||0.001;s.v1.set((x1+x2)/2,(y1+y2)/2,(z1+z2)/2);s.v2.set(dx/len,dy/len,dz/len);s.q.setFromUnitVectors(s.up,s.v2);s.s.set(r,len,r);s.m.compose(s.v1,s.q,s.s);G.bonds.setMatrixAt(idx,s.m)}
function updateBondTransforms(){if(!G.bonds)return;let ev=state.edgeVerts,pos=state.pos;if(G.bondMode==='line'){let arr=G.bonds.geometry.attributes.position.array;for(let i=0;i<ev.length;i++){let a=ev[i][0],b=ev[i][1];arr[i*6]=pos[a*3];arr[i*6+1]=pos[a*3+1];arr[i*6+2]=pos[a*3+2];arr[i*6+3]=pos[b*3];arr[i*6+4]=pos[b*3+1];arr[i*6+5]=pos[b*3+2]}G.bonds.geometry.attributes.position.needsUpdate=true;syncHalos();return}
let r=bondRadius(),s=G.scr,rv=state.reveal&&!state.reveal.done?performance.now():0;for(let i=0;i<ev.length;i++){let a=ev[i][0],b=ev[i][1],ax=pos[a*3],ay=pos[a*3+1],az=pos[a*3+2],bx=pos[b*3],by=pos[b*3+1],bz=pos[b*3+2],mx=(ax+bx)/2,my=(ay+by)/2,mz=(az+bz)/2;if(rv){let g=revealBondScale(a,b,rv);ax=mx+(ax-mx)*g;ay=my+(ay-my)*g;az=mz+(az-mz)*g;bx=mx+(bx-mx)*g;by=my+(by-my)*g;bz=mz+(bz-mz)*g}setBondHalf(i*2,ax,ay,az,mx,my,mz,r,s);setBondHalf(i*2+1,bx,by,bz,mx,my,mz,r,s)}G.bonds.instanceMatrix.needsUpdate=true;syncHalos()}
function bondColorArray(){if(!G.bonds)return null;if(G.bondMode==='split')return G.bonds.instanceColor&&G.bonds.instanceColor.array;return G.bonds.geometry.attributes.color.array}
function bondColorsChanged(){if(!G.bonds)return;if(G.bondMode==='split'){if(G.bonds.instanceColor)G.bonds.instanceColor.needsUpdate=true}else{G.bonds.geometry.attributes.color.needsUpdate=true}}
function computeColorTargets(){let set=focusSet(),ct=state.colTarget;for(let i=0;i<state.nodes.length;i++){let base=colorFor(state.nodes[i],state.source),m=set?set.has(i)?1:0.22:1;ct[i*3]=base[0]*m;ct[i*3+1]=base[1]*m;ct[i*3+2]=base[2]*m}
let bt=state.bondColTarget,ev=state.edgeVerts;for(let i=0;i<ev.length;i++){let a=ev[i][0],b=ev[i][1],lit=set&&(set.has(a)||set.has(b)),f=set?(lit?1.6:0.22):1,ca=colorFor(state.nodes[a],state.source),cb=colorFor(state.nodes[b],state.source);bt[i*6]=Math.min(1,ca[0]*f);bt[i*6+1]=Math.min(1,ca[1]*f);bt[i*6+2]=Math.min(1,ca[2]*f);bt[i*6+3]=Math.min(1,cb[0]*f);bt[i*6+4]=Math.min(1,cb[1]*f);bt[i*6+5]=Math.min(1,cb[2]*f)}}
function easeColorArray(arr,target){let maxd=0;for(let i=0;i<arr.length;i++){let d=target[i]-arr[i],ad=d<0?-d:d;if(ad>maxd)maxd=ad;arr[i]+=d*COLOR_EASE}if(maxd<0.005){arr.set(target);return true}return false}
/* Continuous force simulation — d3-style temperature. Bond springs, sampled
   repulsion and center gravity run every frame scaled by a decaying alpha;
   dragging a node pins it kinematically while the springs pull its whole
   neighborhood along, so every node's motion propagates through the network.
   There is no "end": the sim sleeps (zero stepping, zero matrix churn) when
   alpha cools below ALPHA_MIN or kinetic energy stills below KE_SLEEP, and
   wakes on graph rebuild, node drag, and node release. */
function startSim(){let n=state.nodes.length,vel=new Float32Array(n*3);let hubs=Array.from({length:n},(_,i)=>i).sort((a,b)=>(state.degree[state.nodes[b].id]||0)-(state.degree[state.nodes[a].id]||0)).slice(0,40);let sample=[...hubs],step=Math.max(1,Math.floor(n/40));for(let i=0;i<n;i+=step){if(!sample.includes(i))sample.push(i)}state.sim={vel,sample,alpha:1,sleeping:false,fitted:false,rest:14,k:0.02,rep:5000,grav:0.0018,damp:0.84,maxD:1.6,dragVX:0,dragVY:0,dragVZ:0,dragT:0}}
function wakeSim(a){let s=state.sim;if(!s)return;if(a>s.alpha)s.alpha=a;s.sleeping=false}
/* First sleep after a build is the settle moment the old one-shot sim called
   "the end": refit the camera once there, unless the user already moved it. */
function simSleep(s){if(state.reveal&&!state.reveal.done)return;s.sleeping=true;if(!s.fitted){s.fitted=true;if(!state.userMoved)flyToOrigin(graphRadius())}}
function stepSim(s){if(s.sleeping)return;let pos=state.pos,vel=s.vel,sample=s.sample,rest=s.rest,k=s.k,rep=s.rep,grav=s.grav,damp=s.damp,maxD=s.maxD,n=state.nodes.length,drag=state.dragging,stepped=false;
for(let it=0;it<2;it++){
if(drag!=null&&!REDUCED_MOTION&&s.alpha<WAKE_ALPHA)s.alpha=WAKE_ALPHA;
if(s.alpha<ALPHA_MIN){simSleep(s);break}
let a=s.alpha;
for(let i=0;i<n;i++){vel[i*3]-=pos[i*3]*grav*a;vel[i*3+1]-=pos[i*3+1]*grav*a;vel[i*3+2]-=pos[i*3+2]*grav*a}
for(let e=0;e<state.edgeVerts.length;e++){let ea=state.edgeVerts[e][0],eb=state.edgeVerts[e][1],dx=pos[eb*3]-pos[ea*3],dy=pos[eb*3+1]-pos[ea*3+1],dz=pos[eb*3+2]-pos[ea*3+2],d=Math.sqrt(dx*dx+dy*dy+dz*dz)||0.01,f=k*(d-rest)/d*a;vel[ea*3]+=dx*f;vel[ea*3+1]+=dy*f;vel[ea*3+2]+=dz*f;vel[eb*3]-=dx*f;vel[eb*3+1]-=dy*f;vel[eb*3+2]-=dz*f}
for(let i=0;i<n;i++){for(let s0=0;s0<sample.length;s0++){let j=sample[s0];if(j===i)continue;let dx=pos[i*3]-pos[j*3],dy=pos[i*3+1]-pos[j*3+1],dz=pos[i*3+2]-pos[j*3+2],d2=Math.max(dx*dx+dy*dy+dz*dz,1),d=Math.sqrt(d2),f=rep/d2*a;vel[i*3]+=dx/d*f;vel[i*3+1]+=dy/d*f;vel[i*3+2]+=dz/d*f}}
let ke=0;
for(let i=0;i<n;i++){if(i===drag){vel[i*3]=0;vel[i*3+1]=0;vel[i*3+2]=0;continue}let vx=clamp(vel[i*3]*damp,-maxD,maxD),vy=clamp(vel[i*3+1]*damp,-maxD,maxD),vz=clamp(vel[i*3+2]*damp,-maxD,maxD);pos[i*3]+=vx;pos[i*3+1]+=vy;pos[i*3+2]+=vz;vel[i*3]=vx;vel[i*3+1]=vy;vel[i*3+2]=vz;ke+=vx*vx+vy*vy+vz*vz}
s.alpha*=ALPHA_DECAY;stepped=true;
if(drag==null&&(!n||ke/n<KE_SLEEP)){simSleep(s);break}
}
if(stepped){updateAtomMatrices();updateBondTransforms()}}
function focusSet(){let base=null,key=state.dragging!=null?state.dragging:state.hovered;if(key!=null){base=new Set(state.adj[key]);base.add(key)}return base}
function refreshHighlight(instant){if(!G.atoms)return;computeColorTargets();if(instant){let na=G.atoms.instanceColor,ba=bondColorArray();if(na){na.array.set(state.colTarget);na.needsUpdate=true}if(ba){ba.set(state.bondColTarget);bondColorsChanged()}G.colorsAnimating=false}else{G.colorsAnimating=true}syncOverlays()}
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
let ring=new THREE.Sprite(makeRingMaterial(rgb));ring.position.set(p.x,p.y,p.z);ring.scale.set(26*dscale,26*dscale,1);G.scene.add(ring);G.rings.push({sprite:ring,base:26});if(state.selected!=null&&state.selected!==focus){let q=nodePos(state.selected),r2=new THREE.Sprite(makeRingMaterial([1,1,1]));r2.position.set(q.x,q.y,q.z);r2.scale.set(18*dscale,18*dscale,1);G.scene.add(r2);G.rings.push({sprite:r2,base:18})}let mat=G.textureCache['label:'+nd.label];if(!mat){mat=new THREE.SpriteMaterial({map:labelTexture(nd.label),transparent:true,depthTest:false});G.textureCache['label:'+nd.label]=mat}let img=mat.map.image,ar=img.width/img.height,label=new THREE.Sprite(mat);label.userData={ar,baseY:p.y};label.position.set(p.x,p.y+22*dscale,p.z);label.scale.set(ar*8*dscale,8*dscale,1);G.scene.add(label);G.label=label}
function setHover(index){if(state.hovered===index)return;state.hovered=index;refreshHighlight()}
function setSelected(index){state.selected=index;if(index==null)state.hovered=null;refreshHighlight()}
function selectNode(i){setSelected(i);if(state.source==='code'){showCodeNode(state.nodes[i])}else{showResource(state.nodes[i].id)}}
function handleClick(i){let now=performance.now();if(G.lastClick&&G.lastClick.i===i&&now-G.lastClick.t<DBLCLICK_MS){G.lastClick=null;selectNode(i);flyToNode(i);return}G.lastClick={i,t:now};selectNode(i)}
function flyToNode(i){let p=nodePos(i),R=graphRadius();flyTo(p.x,p.y,p.z,Math.max(40,R*0.55),G.cam.dist)}
function resetView(){if(!state.nodes.length)return;state.userMoved=false;flyToOrigin(Math.max(graphRadius(),40))}
function showCodeNode(nd){let root=document.getElementById('results');root.replaceChildren();let title=document.createElement('div');title.className='title';let meta=document.createElement('div');meta.className='meta';let pre=document.createElement('pre');pre.className='content';escText(title,nd.label||nd.id);escText(meta,(nd.kind||'code symbol')+' · '+(nd.path||'')+(nd.line?' · line '+nd.line:''));escText(pre,'id: '+nd.id+'\npath: '+(nd.path||'')+(nd.line?'\nline: '+nd.line:'')+'\n\ntype: '+(nd.type||'references'));root.append(title,meta,pre);document.getElementById('detail').classList.add('open')}
function pick(cx,cy){if(!G.atoms)return null;let rect=G.renderer.domElement.getBoundingClientRect();let ndc=new THREE.Vector2((cx-rect.left)/rect.width*2-1,-(cy-rect.top)/rect.height*2+1);G.raycaster.setFromCamera(ndc,G.camera);let hits=G.raycaster.intersectObject(G.atoms);return hits.length?hits[0]:null}
function dragTo(cx,cy){let i=state.dragging;let rect=G.renderer.domElement.getBoundingClientRect();let ndc=new THREE.Vector2((cx-rect.left)/rect.width*2-1,-(cy-rect.top)/rect.height*2+1);G.raycaster.setFromCamera(ndc,G.camera);let p=new THREE.Vector3(state.pos[i*3],state.pos[i*3+1],state.pos[i*3+2]);let normal=G.camera.getWorldDirection(new THREE.Vector3());let plane=new THREE.Plane().setFromNormalAndCoplanarPoint(normal,p);let hit=new THREE.Vector3();if(G.raycaster.ray.intersectPlane(plane,hit)){let s=state.sim;if(s){s.dragVX=0.5*s.dragVX+0.5*(hit.x-state.pos[i*3]);s.dragVY=0.5*s.dragVY+0.5*(hit.y-state.pos[i*3+1]);s.dragVZ=0.5*s.dragVZ+0.5*(hit.z-state.pos[i*3+2]);s.dragT=performance.now()}state.pos[i*3]=hit.x;state.pos[i*3+1]=hit.y;state.pos[i*3+2]=hit.z;if(!REDUCED_MOTION)wakeSim(WAKE_ALPHA);if(!state.sim||state.sim.sleeping){updateAtomMatrices();updateBondTransforms()}}}
function panBy(dx,dy){let k=G.cam.dist*0.0011;let right=new THREE.Vector3();G.camera.getWorldDirection(right);right.cross(new THREE.Vector3(0,1,0)).normalize();let up=new THREE.Vector3(0,1,0);G.cam.tx-=right.x*dx*k;G.cam.ty-=up.y*dy*k;G.cam.tz-=right.z*dx*k}
let canvas3d=document.getElementById('graph');let downX=0,downY=0,downMode=null,movedDist=0;
canvas3d.addEventListener('pointerdown',e=>{if(!G.renderer)return;finishReveal();canvas3d.setPointerCapture(e.pointerId);G.lastInteract=performance.now();G.spin.t=0;G.spin.p=0;downX=e.clientX;downY=e.clientY;movedDist=0;if(e.button===2||e.button===1||e.metaKey||e.ctrlKey){downMode='pan';return}let hit=pick(e.clientX,e.clientY);if(hit&&hit.instanceId!=null){downMode='node';state.dragging=hit.instanceId;let s=state.sim;if(s){s.dragVX=0;s.dragVY=0;s.dragVZ=0;s.dragT=0}refreshHighlight()}else{downMode='rotate'}});
canvas3d.addEventListener('contextmenu',e=>e.preventDefault());
canvas3d.addEventListener('pointermove',e=>{if(!G.renderer)return;G.lastInteract=performance.now();movedDist=Math.max(movedDist,Math.hypot(e.clientX-downX,e.clientY-downY));if(downMode==='node'){dragTo(e.clientX,e.clientY)}else if(downMode==='rotate'){let dt=-(e.clientX-downX)*0.005,dp=-(e.clientY-downY)*0.005;G.cam.theta+=dt;G.cam.phi=clamp(G.cam.phi+dp,0.15,Math.PI-0.15);G.spin.t=dt;G.spin.p=dp;G.spinT=performance.now();state.userMoved=true;downX=e.clientX;downY=e.clientY}else if(downMode==='pan'){panBy(e.clientX-downX,e.clientY-downY);state.userMoved=true;downX=e.clientX;downY=e.clientY}else{let hit=pick(e.clientX,e.clientY);setHover(hit&&hit.instanceId!=null?hit.instanceId:null)}});
canvas3d.addEventListener('pointerup',e=>{if(!G.renderer)return;if(performance.now()-G.spinT>90){G.spin.t=0;G.spin.p=0}let wasNode=downMode==='node',idx=state.dragging,wasMoved=movedDist;downMode=null;if(wasNode){state.dragging=null;let s=state.sim;if(s&&wasMoved>=5&&!REDUCED_MOTION){if(performance.now()-s.dragT<=DRAG_VEL_STALE_MS){s.vel[idx*3]=clamp(s.dragVX*FLING_GAIN,-FLING_MAX,FLING_MAX);s.vel[idx*3+1]=clamp(s.dragVY*FLING_GAIN,-FLING_MAX,FLING_MAX);s.vel[idx*3+2]=clamp(s.dragVZ*FLING_GAIN,-FLING_MAX,FLING_MAX)}wakeSim(WAKE_ALPHA)}if(wasMoved<5){handleClick(idx)}else{setSelected(idx)}}else if(wasMoved<5){let hit=pick(e.clientX,e.clientY);if(hit&&hit.instanceId!=null){handleClick(hit.instanceId)}else{setSelected(null)}}});
canvas3d.addEventListener('wheel',e=>{e.preventDefault();finishReveal();G.lastInteract=performance.now();state.userMoved=true;G.cam.distTarget=clamp(G.cam.distTarget*Math.exp(e.deltaY*0.0012),40,1600)},{passive:false});
const graphViewEl=document.getElementById('graphView');
function animate(){requestAnimationFrame(animate);if(graphViewEl.classList.contains('hidden'))return;let now=performance.now();if(G.fly){let f=G.fly;f.t+=0.04;let k=Math.min(1,f.t),e=1-Math.pow(1-k,3);G.cam.tx=f.sx+(f.ex-f.sx)*e;G.cam.ty=f.sy+(f.ey-f.sy)*e;G.cam.tz=f.sz+(f.ez-f.sz)*e;G.cam.dist=f.sd+(f.ed-f.sd)*e;if(k>=1)G.fly=null}
else{if(G.spin.t||G.spin.p){G.cam.theta+=G.spin.t;G.cam.phi=clamp(G.cam.phi+G.spin.p,0.15,Math.PI-0.15);G.spin.t*=ORBIT_DAMPING;G.spin.p*=ORBIT_DAMPING;if(Math.abs(G.spin.t)<2e-5&&Math.abs(G.spin.p)<2e-5){G.spin.t=0;G.spin.p=0}}
let dd=G.cam.distTarget-G.cam.dist;if(dd)G.cam.dist=Math.abs(dd)<0.05?G.cam.distTarget:G.cam.dist+dd*ZOOM_EASE;
if(!REDUCED_MOTION&&downMode===null&&now-G.lastInteract>IDLE_DELAY_MS)G.cam.theta+=IDLE_DRIFT}
if(state.sim&&!state.sim.sleeping)stepSim(state.sim);
if(G.colorsAnimating&&G.atoms){let settled=true,na=G.atoms.instanceColor;if(na){settled=easeColorArray(na.array,state.colTarget)&&settled;na.needsUpdate=true}let ba=bondColorArray();if(ba){settled=easeColorArray(ba,state.bondColTarget)&&settled;bondColorsChanged()}if(settled)G.colorsAnimating=false}
if(state.reveal&&!state.reveal.done)stepReveal(now);
if(G.rings.length||G.label){tGlobal+=0.05;let dscale=G.cam.dist/150,p=REDUCED_MOTION?1:1+RING_PULSE_AMPLITUDE*Math.sin(tGlobal*2.2);for(let r of G.rings){let s=r.base*dscale*p;r.sprite.scale.set(s,s,1)}if(G.label){let u=G.label.userData;G.label.scale.set(u.ar*8*dscale,8*dscale,1);G.label.position.y=u.baseY+22*dscale}}fitFog(G.cam.dist);setCamera();G.renderer.render(G.scene,G.camera)}
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
document.getElementById('resetViewBtn').onclick=resetView;
let nodeSizeRange=document.getElementById('nodeSizeRange'),edgeOpacityRange=document.getElementById('edgeOpacityRange'),monoColorPicker=document.getElementById('monoColorPicker'),colorModeMulti=document.getElementById('colorModeMulti'),colorModeMono=document.getElementById('colorModeMono');
nodeSizeRange.value=display.nodeSize;edgeOpacityRange.value=Math.round(display.edgeOpacity*100);monoColorPicker.value=display.monoColor;colorModeMulti.classList.toggle('active',display.colorMode==='multi');colorModeMono.classList.toggle('active',display.colorMode==='mono');
nodeSizeRange.addEventListener('input',()=>{display.nodeSize=Number(nodeSizeRange.value);if(G.atoms){updateAtomMatrices();updateBondTransforms()}saveDisplayPrefs()});
edgeOpacityRange.addEventListener('input',()=>{display.edgeOpacity=Number(edgeOpacityRange.value)/100;if(G.bonds)G.bonds.material.opacity=display.edgeOpacity;saveDisplayPrefs()});
function setColorMode(mode){display.colorMode=mode;colorModeMulti.classList.toggle('active',mode==='multi');colorModeMono.classList.toggle('active',mode==='mono');saveDisplayPrefs();if(state.nodes.length)refreshHighlight()}
colorModeMulti.onclick=()=>setColorMode('multi');
colorModeMono.onclick=()=>setColorMode('mono');
monoColorPicker.addEventListener('input',()=>{display.monoColor=monoColorPicker.value;saveDisplayPrefs();if(display.colorMode==='mono'&&state.nodes.length)refreshHighlight()});
/* ---------------- boot ---------------- */
document.querySelectorAll('.view-btn').forEach(button=>button.onclick=()=>switchView(button.dataset.view));document.getElementById('expandDetail').onclick=()=>{document.getElementById('detail').classList.toggle('expanded');document.getElementById('expandDetail').classList.toggle('active')};addEventListener('keydown',e=>{if((e.metaKey||e.ctrlKey)&&e.key==='k'){e.preventDefault();document.getElementById('search').focus()}if(e.key==='0'&&!e.metaKey&&!e.ctrlKey&&!e.altKey){let a=document.activeElement,tg=a&&a.tagName;if(tg!=='INPUT'&&tg!=='TEXTAREA'&&!(a&&a.isContentEditable)&&!graphViewEl.classList.contains('hidden'))resetView()}if(e.key==='Escape'){closeModals();document.getElementById('detail').classList.remove('expanded');document.getElementById('expandDetail').classList.remove('active')}});addEventListener('resize',resize3D);
function boot(){if(typeof THREE==='undefined'){document.getElementById('graphMeta').textContent='3D engine failed to load';showGraphLoading(false);return}try{init3D()}catch(error){document.getElementById('graphMeta').textContent='WebGL unavailable: '+error.message;showGraphLoading(false)}load();if(G.renderer)animate()}
boot();
</script>
</body></html>

'''
