# ADR 0006 — One reading of `error.code`, and the status each surface answers with

Date: 2026-08-14 · Status: accepted · The consumer half of ADR 0002. 0002 gave
the raiser five codes to choose between; this gives the three surfaces one
table to read them from.

A file of its own rather than a section appended to 0002 because they are
different subjects with different incidents. 0002's context is a *raiser*
problem — 214 sites saying `validation_error` for five unrelated remedies. This
one's is a *reader* problem, and it is the older of the two: the divergence
below predates the taxonomy and survived it untouched, because sharpening the
codes did nothing about the surfaces that were not reading them.

## Context

`BrainskitError.code` exists precisely so a caller can branch without parsing
English. Three surfaces answer with it, and none of them shared a reading of
it. The mapping was duplicated **even within a single file**.

`interfaces/mcp.py` carried two `except` ladders, one per transport. They
differed in one line of source and in three ways on the wire:

| | stdio | HTTP |
|---|---|---|
| `ValidationError` → JSON-RPC | `-32000` | `-32600` |
| `data.code` | present | **absent** |
| `data` from `details` | present | **absent** |

So the same refusal — `bk`'s MCP server rejecting an unknown tool name, or a
`ConflictError` from an apply — arrived at an agent as a server error over a
pipe and as "your JSON-RPC request object is malformed" over HTTP, with the
machine-readable code the whole taxonomy exists for stripped out on the
transport an agent is more likely to be using. `-32600` was also simply wrong
there: JSON-RPC reserves it for "the JSON sent is not a valid Request object",
and a `tools/call` naming a tool that does not exist is a perfectly valid
Request object.

`interfaces/web.py` carried the same fifteen-line block in its GET handler and
again in its POST handler, and both ended `status=HTTPStatus.BAD_REQUEST`.
Every error the vault can raise was a 400:

- `NotFoundError` — code `not_found`, raised by `Reader.read_resource` for a
  page or hash that is not there — arrived as **400**, not 404.
- `PolicyError` — code `policy_denied`, the privacy boundary refusing — arrived
  as **400**, not 403.
- A `ConflictError` on an approve, a `NotConfiguredError` with no provider
  mapped, a `ModelResponseError` from a truncated completion: **400**, all of
  them. A browser, a `curl`, a proxy and a log aggregator all saw one status
  for "you asked wrong", "it is not here", "you may not", "this machine cannot"
  and "the model misbehaved".

Both copies read the code with `getattr(exc, "code", "invalid_request")` rather
than the class attribute, which is not sloppiness: the same handler
deliberately catches bare `ValueError`, because an unparsable `?limit=` is a
bad request and should not become a stack trace.

The `{"ok": false, "error": {…}}` envelope had three spellings for one shape.
`interfaces/web.py` inlined the literal **eleven** times, `interfaces/cli.py`
built it a second way in `_emit_error`, and `interfaces/mcp.py`'s
`_send_http_error` a third.

And one enrichment existed on one surface only. `_install_hint_for` turned an
application-layer `{"needs": ["brainskit[code]"]}` into the command that
installs it *on this machine* — the `uv tool` versus `pip` problem ADR 0005
Decision 4 records. It lived in `interfaces/cli.py`, so an MCP agent or a web
caller that hit a missing optional extra received the bare list and no command
at all: the exact failure mode the hint was written to end, reintroduced by
which file it happened to live in.

## Decision

1. **`interfaces/errors.py` owns one table**, `PRESENTATIONS`, mapping each
   `code` to a `Presentation(exit_status, http_status, jsonrpc_code)`. Every
   code defined anywhere in the tree has exactly one row. The three columns are
   read by the three surfaces, and no surface decides its own:

   | code | exit | HTTP | JSON-RPC | why |
   |---|---|---|---|---|
   | `brainskit_error` | 2 | 500 | −32000 | raised but unclassified, plus the `OSError`/`JSONDecodeError` the CLI wraps at its boundary. Nothing the caller can act on. |
   | `validation_error` | 2 | **400** | −32000 | the request itself is wrong. Unchanged; pinned from the other side by `test_engine.py`'s `/api/ask` rejections. |
   | `conflict` | 2 | **409** | −32000 | "re-read the current state, rebuild the request against it, send it again" is RFC 9110's own gloss on 409. |
   | `not_configured` | 2 | **501** | −32000 | this installation cannot serve it, and retrying is pointless — see the rejection below. |
   | `refused` | 2 | **403** | −32000 | 403 is "understood the request, refuses to fulfil it, re-sending will not help", which is this code's docstring almost verbatim. |
   | `model_response_invalid` | 2 | **502** | −32000 | brainskit called a provider and the *provider's output* failed validation. 502 is defined as an invalid response from an inbound server, received while acting as a gateway. |
   | `not_found` | 2 | **404** | −32000 | — |
   | `policy_denied` | **3** | **403** | −32000 | see below. |
   | `internal_error` | 2 | 500 | **−32603** | the CLI's safety net caught something brainskit does not model; `-32603` is JSON-RPC's own internal error. |
   | `jsonrpc_request_invalid` | 2 | **400** | **−32600** | new; see Decision 3. |
   | `protocol_version_invalid` | 2 | **400** | **−32600** | unchanged. |
   | `invalid_request` | 2 | 400 | −32000 | the row for an exception carrying no code at all — the bare `ValueError` web catches on purpose. |

   Two rows want their reasoning stated rather than tabulated.

   **`policy_denied` is 403, not 404.** There is a real argument for 404: a
   privacy refusal that says "forbidden" has confirmed the thing exists.
   It does not apply here, because `Reader.read_resource` raises `NotFoundError`
   *first* for anything absent — so this answer is only ever reached for
   something that is there, and dressing it as absent would be a lie the very
   next request contradicts. It also shares 403 with `refused`, which is fine:
   the status narrows the family and the `code` in the body names the member.

   **`policy_denied` keeps exit 3.** It is the one code that carries a
   different process status, and that status is a shipped contract `bk` scripts
   branch on. Verified before touching anything: `ErrorExitCodeTest` in
   `tests/test_fix_domain.py` already pinned it, alongside all five narrowed
   codes at 2. Every exit-column value in this table is what the two-branch
   `except` ladder produced before the table existed; `CliExitStatusTest` now
   drives all eleven classes through `cli.main` to say so, including the two
   the CLI never raises.

2. **The envelope is built once.** `error_envelope(exc)` for anything with a
   message, `refusal_envelope(code, …)` for a guard that refuses before an
   exception exists — a denied Host, a foreign Origin, a missing token, an
   unrouted path. Two functions rather than one because they are two situations
   and the difference is visible on the wire: a refusal names a rule and has
   nothing else to say, so `message` and `details` are omitted rather than
   emitted empty. That is what those responses already carried; the shape is
   preserved, not redesigned.

3. **The two transport-contract failures become a family.** `-32600` really
   does mean "not a valid Request object", and `interfaces/mcp.py` raises about
   exactly that in four places: a body whose `jsonrpc` is not `"2.0"`, a
   non-string `method`, an `Mcp-Method` or `Mcp-Name` header contradicting the
   body, and an unsupported `MCP-Protocol-Version`. Those are now
   `JsonRpcRequestError`, with `ProtocolVersionError` a subclass of it. The
   HTTP handler needs one `except` where it had two, both the code and the
   status come from the table, and the remaining `except BrainskitError` says
   what it means: this reached the dispatcher, so it answers `200` with a
   JSON-RPC error inside, per the Streamable HTTP spec.

   The first three of those used to answer HTTP `200`. They now answer `400`,
   which is the file's own existing reasoning applied one step further — the
   comment above the parse guard already says "a body brainskit cannot even
   parse is a malformed HTTP request, not a successful call that happens to
   carry an error", and a body that parses but is not a request is the same
   claim.

4. **MCP's HTTP transport reads the status column only for that family.** The
   Streamable HTTP spec is explicit that an application-level failure is a
   successful HTTP exchange carrying a JSON-RPC error, and
   `test_tool_level_validation_stays_a_jsonrpc_error_at_200` has documented
   that here since it shipped. So `not_found` is 404 in the viewer and `200`
   with `-32000` over MCP, and that is not an inconsistency in the table — it
   is two protocols, one of which puts its errors inside the body by
   construction. `McpTransportParityTest` asserts the `200` for every class as
   part of the same loop that asserts the codes match.

5. **The install hint moved to the presenter**, so all three surfaces enrich.
   It is still the CLI's reasoning — `pyenv` knows how `bk` was installed, and
   `application` may not import it — but the place errors are *rendered* is
   `interfaces/errors.py` now, not `interfaces/cli.py`.

6. **The table has a mechanical owner.** `ErrorTableHasOneOwnerTest` parses
   every `code = "…"` class attribute out of `src/brainskit/**/*.py` and fails
   if one has no row, or if a row names a code no class defines. Same
   discipline, and the same reason, as `ConstantsHaveOneOwnerTest` in
   `tests/test_layering.py`: per this repository's history, a shared constant
   restated on both sides of a boundary has already shipped as a divergence
   three times, and this is the fourth thing that was being restated.

7. **The interface layer's `--consumer` default is one function.** ADR 0001
   settled the domain half — `Consumer.parse` is the one place an unknown
   consumer becomes an error — and nothing on that side changed. What was
   scattered is which boundary an *unnamed* read runs under, decided at six
   places. `_consumer_for_args(args, *, default)` now states both rules
   together, because they are two decisions and were being made as none:

   - An interactive read defaults to `human` — the operator is the reader and
     the result goes no further than the terminal — but a `--json` caller is
     not a person and must say who is. That refusal is unchanged.
   - A read whose output is a file or a graph defaults to `local` and carries
     no refusal, because the artifact outlives the command and `human`
     withholds nothing.

   The two `args.consumer or "local"` sites are gone, and one of them was dead:
   `bk export`'s `argparse` already declared `default="local"`, so the fallback
   restated a rule the parser had stated first — the exact failure mode a
   second spelling produces. `interfaces/mcp.py`'s three hardcoded `"local"`
   became one `MCP_CONSUMER`, and the list `["human", "local", "cloud"]` — ten
   copies in `interfaces/cli.py`, one in the MCP tool schema, one in the
   viewer's validation — is derived from `Consumer` everywhere.

## Alternatives rejected

- **`not_configured` → 503.** The obvious 5xx, and the wrong one. 503 means
  "temporarily unavailable, try again later" and carries `Retry-After`; this
  code's whole purpose is the opposite instruction — retrying is pointless and
  rephrasing is pointless, someone has to change the configuration or the
  machine. 501 is the only status that says "this server cannot do this" without
  promising it might later. 500 was rejected too: nothing failed.
- **Make stdio adopt HTTP's `-32600`.** It would have unified the two ladders
  with a one-line change. It unifies them on the wrong value — the Request
  object is valid, the call inside it failed — and it would have kept the
  information loss, since the HTTP branch built its `data` from `str(exc)`
  alone.
- **`refused` → 409.** It reads plausibly ("the situation forbids this"), and
  it hands the caller the one remedy that cannot work. 409 means re-read and
  resubmit; `refused` is documented as "changing the request does not help, the
  circumstance has to change". This is the same trap `proposal_id_reuse_error`
  in `domain/model.py` records from the other direction, where a durable
  refusal dressed as a conflict sends an agent into a retry loop that cannot
  terminate.
- **One `envelope()` with optional fields instead of two functions.** The
  distinction it collapses is real: an exception has a message and details, a
  guard has neither, and the responses have always differed accordingly. A
  single builder either emits `"message": ""` on every guard — a field clients
  must now test — or grows the same two branches inside itself, unnamed.
- **Keep `getattr(exc, "code", …)` at each web call site.** It is not wrong,
  and there were two of it. `error_code()` in the presenter is the same
  expression with one owner, which is the entire point of this ADR.
- **Give MCP's HTTP transport the status column outright**, so `not_found`
  became a real 404 there too. It contradicts the Streamable HTTP spec and the
  test that already documents the rule; an MCP client reading a 404 would treat
  the *endpoint* as missing, not the resource.
- **Fold the `--consumer` unification into `application/`.** ADR 0001 drew that
  line and `Consumer.parse` is where it lands. What moved here is only which
  default each *surface* picks when the operator named none, which is a fact
  about the surface — a terminal, a file, an MCP session — and belongs on this
  side of the boundary.
- **Move `bk web`'s `args.consumer or str(options.get("consumer", "human"))`
  into the same helper.** Its fallback is a config read, not a constant: the
  viewer inherits the consumer the integration policy declared. Same shape,
  different rule, deliberately left. `bk vaults sync`'s `--consumer` was left
  for the opposite reason — it exists only to be refused.

## Consequences

- `interfaces/errors.py` is 221 lines. `interfaces/web.py` lost its two
  duplicated blocks and all eleven inline envelopes; `interfaces/mcp.py` lost
  one of its two `except` ladders; `interfaces/cli.py` lost `_install_hint_for`
  and one of its two exit-status branches.
- **Six statuses changed**, all of them on the web viewer: `not_found` 400→404,
  `policy_denied` 400→403, `conflict` 400→409, `refused` 400→403,
  `not_configured` 400→501, `model_response_invalid` 400→502. `brainskit_error`
  and `internal_error` become 500 there. A client that branched on the status
  rather than the body's `code` sees new values; a client that branched on
  `code` sees exactly what it saw before.
- **Two JSON-RPC changes.** Every `ValidationError` and its four subclasses now
  answer `-32000` over HTTP as they always did over stdio, and every HTTP
  response now carries `data.code` and the raiser's `details`, which it did not
  before. Three of MCP HTTP's contract failures move from `200` to `400`.
- One existing assertion changed: `test_tool_level_validation_stays_a_jsonrpc_
  error_at_200` asserted `-32600` for a tool-level error. Its HTTP status claim
  — the reason it exists — is untouched at `200`, and it gained an assertion
  that `data.code` survives the transport.
- The full suite was 1363 before, and this change adds 28: the table row by
  row, its one-owner guard, the two surfaces that were wrong, and the consumer
  default. Nothing was weakened. (The tree it was verified in also carried two
  parallel changes, so the suite as run reports 1441 passing, 0 failing.)
- `interfaces/cli.py` still fails `ruff format --check`, at 46 hunks where HEAD
  fails at 47. `interfaces/mcp.py` and `interfaces/web.py` are unchanged at 6
  and 7. The two new files are formatted; the pre-existing debt was left where
  ADR 0005 left it, for the reason ADR 0005 gives.
