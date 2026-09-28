# ADR 0010 — The MCP transport declares its consumer, and every call answers under it

Date: 2026-09-28 · Status: accepted · Phase A5 of the 0.8.0 release. Follows
ADR 0001 (the boundary is one object), ADR 0006 (one reading of `error.code`)
and ADR 0009 (installation facts are inside the boundary), each of which left
MCP answering under a scope nobody had declared.

## Context

`interfaces/mcp.py` read under one hardcoded constant, `MCP_CONSUMER = "local"`,
wherever a call named no consumer: `resources/list`, `resources/read` and
`integration_status`. `search` and `context` required the caller to declare
one, and accepted any of the three — `human` included. Everything else read
under no boundary at all:

- **`status`** returned `Health.status`, the operator's report: the vault's
  absolute path, `by_branch` with every branch name, counts that included
  never-ingest sources. ADR 0009 filtered `/api/status` and recorded MCP
  `status` as out of scope, on the reasoning that it reached only `local`. It
  reached whoever was on the other end of the pipe.
- **`proposals`** called `Filing.proposals`, not `proposals_for_consumer`, so
  the review queue arrived whole — never-ingest `apply_proposal` payloads
  included.
- **`lint`** returned every finding, and a finding names a path.
- **`ask`, `resurface` and `lint --semantic`** read evidence as `local` before
  consulting the route. With a local model mapped, local-only evidence went to
  that model and its answer came back over MCP to a client that may not see it.
- **`file`** resolved any source by hash prefix or path and moved it to any
  configured branch. `file {"item": "<first hex digits>", "branch": "30-public"}`
  moved a never-ingest source into a cloud branch, where `search` then served
  it. Filing is how a source's privacy changes; this was declassification by
  request.
- **`approve` / `reject`** took any proposal id.
- **`integration_configure`, `_up`, `_down`, `_sync`** echoed stored options
  and runtime state — filesystem paths, container names, `*_env` names — that
  `integration_status` already withholds from machine consumers (ADR 0001), and
  `integration_configure` accepted `options.consumer = "human"`, handing an
  integration a wider scope than the caller had.

Separately, MCP `capture` copied any file the server process could read into
`raw/_inbox` — `~/.ssh/id_rsa`, a project's `.env`, and the vault's own
`.brain/index.sqlite`, which holds the text of every source, never-ingest
included. `save` and `semantic` were read with `bool(arguments[...])`, so
`"false"` saved.

The server cannot see where its answers go. A stdio child of a desktop client
forwards to a cloud model as readily as one of a local agent. So the scope has
to be declared by the one person who knows, the operator starting the server,
and it has to hold for every call rather than for the calls that ask for it.

## Decision

1. **`bk serve --mcp --consumer <cloud|local>` declares the server's consumer.
   The default is `cloud`.** It is the only boundary safe to forward anywhere,
   and a server that does not know its reader has to assume the widest audience.
   `--consumer local` is the operator stating that this client runs on this
   machine. Both transports take it; the flag has the name and vocabulary `bk
   web --consumer` already uses.
2. **`human` is refused for MCP**, with `policy_denied` before the server reads
   a byte. A model-facing transport is never the person `human` means. The
   allowed set is derived, not listed: every consumer that narrows `local`
   (`MCP_CONSUMERS`).
3. **The declared consumer is a ceiling.** Every tool and resource answers under
   it. A per-call `consumer` on `search` and `context` stays required and may
   only *narrow* it (`cloud` under `local`); a wider value is **refused with
   `policy_denied`, not clamped**. "Narrows" is computed from
   `Consumer.allows` over every `PrivacyMode` plus `sees_installation`, so the
   lattice is still stated once, in the domain.
4. **`tools/list` advertises only what the server will answer**: the `consumer`
   enum on `search` and `context` is the set within the ceiling, so a
   conforming client never offers `human` and a cloud server offers only
   `cloud`.
5. **Over MCP, `capture` of a file path is confined.** Text and `http(s)` URLs
   are unchanged. A path must resolve, symlinks followed, under the vault's
   code root or a workspace an agent was installed into (`bk hooks install
   --root`), never under the vault itself, and never at or above the home
   directory. It must not be a credential file, checked on both the name asked
   for and the name it resolves to. The list is small, has one owner
   (`application/capture.py`), and names only files whose whole content is a
   credential: `.env*` except `.env.example|.sample|.template|.dist`, `*.pem`,
   `*.key`, `id_rsa|dsa|ecdsa|ed25519[_sk]`, `.netrc`, `.npmrc`, `.pypirc`,
   anything under `~/.ssh`, `~/.aws` or `~/.config/gcloud`, and anything under
   a `.git` directory, whose `config` is where a token pasted into a remote URL
   lands. The refusal is `policy_denied` with `reason` (`secret_shaped` or
   `outside_project`), what is allowed, and a hint; it names neither the path
   nor the project roots (installation facts, ADR 0009), and nothing is read
   before it is decided. `bk capture <path>` is the operator typing, and is
   unchanged.
6. **A `cloud` server does not operate integrations.** `integration_configure`,
   `integration_up`, `integration_down` and `integration_sync` start and stop
   containers, write exports to disk, and store the options a later sync
   serves under: operator actions on this machine. A server whose consumer
   does not `sees_installation` refuses them with `policy_denied` before any
   argument is read or any service method runs; `details` carry `tool`,
   `server_consumer`, `run_instead` (`bk integration <verb> <name>`) and a
   hint naming `--consumer local`, and no path. `integration_status` is a
   read, already scoped, and stays available. The predicate is
   `Consumer.sees_installation` rather than a comparison with `cloud`, so the
   rule is the ADR 0009 one: a consumer not on this machine is not told its
   layout and does not act on it.
7. **`tools/list` omits a tool the server will not run**, rather than listing
   it as unavailable. It is the same rule as the `consumer` enum in (4), which
   leaves out the values the server refuses instead of annotating them, and
   MCP has no field for "listed but unavailable" — tool annotations are
   behaviour hints, and a note in `description` is free text a client need not
   read. A client that calls an omitted lifecycle tool anyway gets the
   `policy_denied` refusal above, not `Unknown MCP tool`, so it learns why.
8. **MCP tool arguments are typed at the boundary.** A flag is a JSON boolean or
   absent; an integer is a JSON integer, not `true` and not `"5"`; `arguments`
   and `options` are objects. Anything else is `validation_error` naming the
   argument, raised before any service method runs.

### Refuse, not clamp

ADR 0006 gives `policy_denied` to "the privacy boundary refusing", and says
the status narrows the family while the code names the member. A clamp would
answer a `local` request with a `cloud` result the caller believes is local:
the bundle's `redacted` count would include material withheld *by the server's
declaration* and nothing would say so. The caller forwards it, or writes a
proposal from it, on a false premise. A refusal costs one round trip and tells
the client the one thing it needs — the server's consumer is in
`details.server_consumer`, and `details.allowed` lists what it may ask for.

### Not found, not forbidden, for `file`, `approve` and `reject`

These take identifiers a caller can guess: a hash prefix, a raw path, a
proposal id. Resolving them only among what the ceiling may see makes a hidden
source or proposal *absent* (`not_found`), so a guess learns nothing. This is
not the case ADR 0006 argued for `read_resource`, where `not_found` is raised
first for anything missing and `policy_denied` only ever confirms something
the caller already located by an exact id.

## Classification

Every method the MCP server dispatches, and what it answers under since this
ADR. *Ceiling* is the server's declared consumer.

| Method / tool | Kind | Before | Now |
|---|---|---|---|
| `initialize`, `ping` | protocol | no vault data | unchanged |
| `tools/list` | protocol | `consumer` enum listed all three; every tool listed | enum is the set within the ceiling; the four integration lifecycle tools omitted under `cloud` |
| `resources/list` | read | `graph_data(local)` | `graph_data(ceiling)` |
| `resources/read` | read | `read_resource(local)` | `read_resource(ceiling)` |
| `search`, `context` | read | any declared consumer, `human` included | declared consumer, refused if wider than the ceiling |
| `status` | read | `Health.status`, unfiltered, absolute path | `reader_status(ceiling)` — the `/api/status` path; `vault` omitted for `cloud` |
| `proposals` | read | `Filing.proposals`, unfiltered | `proposals_for_consumer(ceiling)` |
| `lint` | read | every finding | findings on material outside the ceiling removed and counted (`redacted_findings`); `ok` recomputed from what remains |
| `ask`, `resurface` | read via a model | evidence read as `local`, whatever the caller | evidence read no wider than the ceiling (`local` for `human` callers, as before) |
| `lint` with `semantic` | read via a model | as above | as above |
| `integration_status` | read | `local` | ceiling; machine layout withheld as since ADR 0001 |
| `capture` | write | any readable file | text, URL, or a confined project file |
| `file` | write that changes privacy | any source, by prefix or path | only a source the ceiling may see; otherwise `not_found` |
| `approve`, `reject` | write | any proposal id | only a proposal whose source the ceiling may see; otherwise `not_found` |
| `apply` | write | caller-authored proposal | unchanged — see Out of scope |
| `integration_configure` | write | echoed every stored option; accepted any `options.consumer` | `cloud`: `policy_denied` and not in `tools/list`. `local`: echo scrubbed of machine layout; `options.consumer` wider than the ceiling is `policy_denied` |
| `integration_up`, `_down`, `_sync` | operation | echoed paths, container names, file listings | `cloud`: `policy_denied` and not in `tools/list`. `local`: same runtime scrub as `integration_status` |

A `local` server keeps what it had — the vault path on `status`, local-only
evidence on `search`, `context`, resources and the model-backed tools — with
two narrowings a `local` reader always should have had: `status` and `lint`
counts exclude never-ingest, and `proposals` drops never-ingest proposals.
`status` over MCP no longer carries `projections`, which `/api/status` never
did; `bk status` still reports it.

## Alternatives rejected

- **Keep `local` as the default.** It would keep every existing MCP client
  working unchanged, and it is the default that made every row above reachable
  by a cloud model through a desktop client's stdio child. A secure default
  that breaks a configuration loudly is recoverable with one flag; an insecure
  one that works is not recoverable at all.
- **Clamp a wider per-call consumer.** Rejected above.
- **Drop the per-call `consumer` now that the server declares one.** It would
  make the declaration the only one, and a `local` server whose client forwards
  one particular answer to a cloud model needs to say so for that call. The
  argument stays required, so no existing client silently changes scope.
- **A path allow-list from the process's current directory.** A desktop client
  commonly starts its stdio children in `/` or `~`, which would confine
  nothing. The code root is bounded below home by `code_root_reason` already.
- **A broad secret scanner (content regexes, entropy).** It reads the file to
  decide, which is the disclosure it is meant to prevent, and it would drift
  into a second policy engine. The name list is the whole rule, and a file it
  misses is still refused if it is outside the project.

## Consequences

- **Breaking:** an MCP client that relied on the old implicit `local` scope, or
  asked for `local`/`human` on `search`/`context`, now gets `cloud` answers or
  `policy_denied`. Restore it with `bk serve --mcp --consumer local`. The same
  flag is the only way to run integration lifecycle tools over MCP; the
  `dsh-brainskit` bundle's `BRAINSKIT_ALLOW_MUTATIONS=1` no longer reaches them
  under its default `cloud` declaration.
- `run_stdio`, `run_http` and `_handle` take `consumer`; the server's consumer
  is validated once, by `server_consumer`, before any request is read.
- `BrainskitService.file`, `approve`, `reject`, `lint`, `ask`, `resurface` and
  the four `integration_*` operations take a `consumer` (default `human`, so the
  CLI and the web viewer are unchanged). `Jobs._judgment_context` and
  `Health.lint` take a `ceiling` for the first evidence read.
- `MCP_CONSUMER` is gone; `MCP_DEFAULT_CONSUMER` and `MCP_CONSUMERS` replace it.
- `tests/test_fix_interfaces.py` carries `McpServerConsumerTest`,
  `ServeConsumerCliTest`, `McpIntegrationLifecycleConsumerTest`,
  `McpBooleanArgumentsTest` and `McpCaptureConfinementTest`; `tests/test_engine.py` carries the model-backed
  ceiling tests in `JudgmentReadsUnderTheRoutesBoundaryTest`.

## Out of scope

- **`apply` over MCP.** The proposal is the caller's own; pages inherit the
  strictest privacy of their sources, so a cloud caller that cites a
  never-ingest source writes a page it cannot read back. What remains is an
  oracle: `conflict` versus success says whether a wiki path exists, and a
  citation check says whether a full source hash is registered. Neither
  discloses content; both are recorded here rather than silently accepted.
- **The web viewer's `capture`** accepts only `human` writes and is unchanged.
