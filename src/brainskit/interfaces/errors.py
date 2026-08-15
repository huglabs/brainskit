"""One reading of `BrainskitError.code`, for every surface that answers with it.

`code` exists so a caller can branch without parsing English, and ADR 0002
sharpened it into five remedies. What no surface had was a shared reading of
those codes. `interfaces/mcp.py` carried two `except` ladders that disagreed
with each other -- one `ValidationError` was JSON-RPC `-32000` on stdio and
`-32600` over HTTP, and the HTTP one dropped `code` and `details` from the
payload entirely. `interfaces/web.py` carried the same fifteen lines in its GET
and POST handlers, both answering `400` to every error there is, so a missing
page and a privacy refusal arrived as bad requests. Only `interfaces/cli.py`
turned a `needs` list into a runnable install command.

This module is the one table. A code maps to exactly one presentation, and each
surface reads the column it speaks:

- **exit status** -- `bk`'s process status, a shipped contract that scripts
  branch on. Every row is `2` except `policy_denied`, which is `3`. Both are
  unchanged from before this table existed, and `ErrorExitCodeTest` in
  `tests/test_fix_domain.py` pins them from the other side.
- **HTTP status** -- what `interfaces/web.py` answers, where the status *is*
  the answer. MCP's HTTP transport reads this column only for the failures it
  decides are transport-level: a JSON-RPC call that reached the dispatcher
  answers `200` with a JSON-RPC error inside, per the Streamable HTTP spec.
- **JSON-RPC code** -- what `interfaces/mcp.py` answers on **both** transports.

Adding a `BrainskitError` subclass without adding its row here fails
`ErrorTableHasOneOwnerTest`, which reads every `code = "..."` out of the tree.

See ADR 0006.
"""

from __future__ import annotations

from dataclasses import dataclass
from http import HTTPStatus
from typing import Any

from brainskit.domain.model import BrainskitError
from brainskit.infrastructure import pyenv

#: JSON-RPC 2.0 reserved codes. `-32600` is specifically "the JSON sent is not
#: a valid Request object", which is why a *tool* failure never carries it: the
#: Request object was perfectly valid, the call inside it failed. Those land in
#: the implementation-defined server range instead.
JSONRPC_INVALID_REQUEST = -32600
JSONRPC_INTERNAL_ERROR = -32603
JSONRPC_SERVER_ERROR = -32000

#: What an exception with no `code` is presented as. `interfaces/web.py`
#: deliberately catches bare `ValueError` alongside `BrainskitError` -- an
#: `int()` over a query parameter is the common case -- and a query parameter
#: that will not parse is a bad request by every reading.
UNCLASSIFIED_CODE = "invalid_request"


@dataclass(frozen=True)
class Presentation:
    """How one error code reads on each surface."""

    exit_status: int
    http_status: HTTPStatus
    jsonrpc_code: int


#: The table. Every `code` defined anywhere in the tree appears exactly once.
PRESENTATIONS: dict[str, Presentation] = {
    # The base class: an error brainskit raised but did not classify, plus the
    # `OSError`/`JSONDecodeError` the CLI wraps at its boundary. Nothing the
    # caller can act on beyond the message, so it reads as a server fault.
    "brainskit_error": Presentation(
        2, HTTPStatus.INTERNAL_SERVER_ERROR, JSONRPC_SERVER_ERROR
    ),
    # The request itself is wrong. Already what web answered; pinned from the
    # other side by the `/api/ask` rejection tests in `test_engine.py`.
    "validation_error": Presentation(2, HTTPStatus.BAD_REQUEST, JSONRPC_SERVER_ERROR),
    # "Re-read the current state, rebuild the request against it, send it
    # again" is RFC 9110's own description of 409.
    "conflict": Presentation(2, HTTPStatus.CONFLICT, JSONRPC_SERVER_ERROR),
    # This installation cannot serve the request, and retrying is pointless --
    # which is why this is 501 and not 503. 503 means "try again later", and
    # that instruction is the one thing this code exists to withhold.
    "not_configured": Presentation(2, HTTPStatus.NOT_IMPLEMENTED, JSONRPC_SERVER_ERROR),
    # A well-formed request the situation forbids: 403's "the server understood
    # the request but refuses to fulfil it, and re-sending will not help".
    "refused": Presentation(2, HTTPStatus.FORBIDDEN, JSONRPC_SERVER_ERROR),
    # brainskit called a provider and the provider's *output* failed
    # validation. That is 502's definition: an invalid response from an inbound
    # server, received while acting as a gateway.
    "model_response_invalid": Presentation(
        2, HTTPStatus.BAD_GATEWAY, JSONRPC_SERVER_ERROR
    ),
    "not_found": Presentation(2, HTTPStatus.NOT_FOUND, JSONRPC_SERVER_ERROR),
    # The privacy boundary refused. 403 rather than 404: `read_resource` raises
    # `NotFoundError` first for anything absent, so this answer is only ever
    # reached for something that exists, and dressing it as absent would be a
    # lie the very next request contradicts.
    "policy_denied": Presentation(3, HTTPStatus.FORBIDDEN, JSONRPC_SERVER_ERROR),
    # An adapter raised something brainskit does not model and the CLI's safety
    # net caught it at the boundary. `-32603` is JSON-RPC's own internal error.
    "internal_error": Presentation(
        2, HTTPStatus.INTERNAL_SERVER_ERROR, JSONRPC_INTERNAL_ERROR
    ),
    # The two transport-contract failures. Both are literally "the JSON sent is
    # not a valid Request object", so both carry `-32600` and, unlike anything
    # that reached the dispatcher, a real HTTP failure status.
    "jsonrpc_request_invalid": Presentation(
        2, HTTPStatus.BAD_REQUEST, JSONRPC_INVALID_REQUEST
    ),
    "protocol_version_invalid": Presentation(
        2, HTTPStatus.BAD_REQUEST, JSONRPC_INVALID_REQUEST
    ),
    UNCLASSIFIED_CODE: Presentation(2, HTTPStatus.BAD_REQUEST, JSONRPC_SERVER_ERROR),
}


def error_code(error: BaseException) -> str:
    """The machine-facing code for anything a surface's `except` may catch."""

    code = getattr(error, "code", None)
    return code if isinstance(code, str) else UNCLASSIFIED_CODE


def present(error: BaseException) -> Presentation:
    """How this error reads on every surface.

    An unknown code is presented as the base class rather than raised on. A
    surface's error path is the last thing that should be able to fail, and
    `ErrorTableHasOneOwnerTest` is what makes the gap loud at development time
    instead.
    """

    return PRESENTATIONS.get(error_code(error), PRESENTATIONS["brainskit_error"])


def install_hint_for(details: dict[str, Any]) -> dict[str, Any]:
    """Turn a `needs` list into the command that works on *this* machine.

    The application layer names what is missing (`{"needs": ["brainskit[code]"]}`)
    and stops there, because which command installs it is a fact about the
    running interpreter — not something a layer that must not import
    infrastructure can know, and not something to hardcode. Every hint that did
    hardcode it said `pip install …`, which is unrunnable under `uv tool`: that
    environment ships no `pip`, so the operator either saw a failure or silently
    installed the package into an unrelated interpreter and hit the identical
    message on the retry.

    Enriching here, at the one place errors are rendered, means every raiser
    gets a correct hint without any of them knowing how `bk` was installed --
    and every *surface* gets it, which was not true while this lived in
    `interfaces/cli.py`: an MCP or web caller received the bare
    `{"needs": [...]}` and no command at all.
    """

    needs = details.get("needs")
    if not isinstance(needs, list) or not needs or "hint" in details:
        return details
    return {**details, "hint": pyenv.install_hint([str(item) for item in needs])}


def error_details(error: BaseException) -> dict[str, Any]:
    """This error's `details`, enriched with a runnable install command."""

    details = getattr(error, "details", None)
    return install_hint_for(details if isinstance(details, dict) else {})


def error_envelope(error: BaseException) -> dict[str, Any]:
    """The `{"ok": false, "error": {...}}` envelope, built once for everyone.

    Three surfaces used to assemble this by hand, in three shapes. The CLI's
    was the complete one; web inlined a near-copy of it twice and the literal
    `{"ok": False, "error": {"code": ...}}` nine times more.
    """

    return {
        "ok": False,
        "error": {
            "code": error_code(error),
            "message": str(error),
            "details": error_details(error),
        },
    }


def refusal_envelope(
    code: str,
    *,
    message: str | None = None,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """The same envelope for a guard that refuses before any error exists.

    A denied Host, a foreign Origin, a missing token: there is no exception to
    describe, only the name of the rule that said no. The shape stays identical
    so a client parses one thing.

    `message` and `details` are omitted rather than emitted empty. A guard that
    has nothing to add should not send an empty string a client has to test for
    -- the code is the whole answer, and that is what these responses have
    always carried.
    """

    error: dict[str, Any] = {"code": code}
    if message is not None:
        error["message"] = message
    if details is not None:
        error["details"] = details
    return {"ok": False, "error": error}


def jsonrpc_error_data(error: BrainskitError) -> dict[str, Any]:
    """The `data` member of a JSON-RPC error, identical on both transports.

    `code` is what an agent branches on and was missing from every HTTP
    response before this existed. `reason` is the field the HTTP transport has
    always carried and remote clients already read; it says the same thing as
    the envelope's `message`, which is the point -- the two transports used to
    disagree about which of them carried it.
    """

    return {"code": error.code, "reason": str(error), **error_details(error)}
