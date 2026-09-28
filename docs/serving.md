# Serving a vault

## Web viewer

The viewer is dependency-free and served by the engine. Its Graph, Sources,
Wiki, Timeline and Services workspaces provide a responsive graph canvas with
pan/zoom, FTS5 search, source/page inspection, ingestion chronology, persistent
integration state, vault health, freshness, branch distribution and the pending
review queue. All API reads reuse application use cases and their privacy
boundary; the viewer never bypasses the engine to read vault files.

```bash
bk --vault ./my-vault integration configure web \
  --enable --managed --host 127.0.0.1 --port 8765 --consumer human
bk --vault ./my-vault integration up web
bk --vault ./my-vault integration status web
# foreground alternative
bk --vault ./my-vault web serve
bk --vault ./my-vault integration down web
```

The local URL is `http://127.0.0.1:8765`. Binding beyond loopback is rejected
unless `--token-env` names a populated bearer-token environment variable.

Eleven endpoints read: `/api/health`, `/api/status`, `/api/graph`,
`/api/code-graph`, `/api/search`, `/api/proposals`, `/api/resource`,
`/api/sources`, `/api/pages`, `/api/timeline` and `/api/integrations`.

**Four endpoints write**, so the viewer is not a read-only surface:

| Endpoint | What it writes |
|---|---|
| `POST /api/capture` | a source into `raw/`, plus the index |
| `POST /api/ask` | an answer into `output/answers/` |
| `POST /api/proposals/approve` | routes through the apply gate, so `wiki/` |
| `POST /api/proposals/reject` | the proposal's state |

What protects them is the consumer, not the method: every write is refused with
`writes_refused` unless the server was started at `--consumer human`. A viewer
run at `local` or `cloud` reads and nothing more.

The bearer token is **optional**, deliberately. Without `--token-env` the server
binds loopback only and is guarded by Host and Origin checks alone, which is the
right trade for a single-user local viewer and the wrong one for anything else.
Naming a token is what makes a non-loopback bind possible at all, so the two
decisions are the same decision.

## An MCP server declares its consumer

MCP hands every answer to a model, and the server cannot see where that model
runs. So both transports answer under the consumer the operator starts them
with:

```bash
bk --vault ./my-vault serve --mcp --transport stdio                    # cloud, the default
bk --vault ./my-vault serve --mcp --transport stdio --consumer local   # an agent on this machine
```

- **`cloud` is the default**, because it is the only boundary safe to forward
  anywhere. `--consumer local` is the operator stating that the client runs on
  this machine. `human` is refused with `policy_denied` before the server reads
  a request: a model is never the reader `human` means.
- **The declared consumer is a ceiling, and a call can only narrow it.** Every
  tool and resource answers under it. `search` and `context` still require a
  `consumer` argument; `cloud` under a `local` server is answered, and anything
  wider than the server's is refused with `policy_denied` (with
  `server_consumer` and `allowed` in the details) rather than silently answered
  narrower. `tools/list` offers only the values the server will answer.
- **`capture` of a file path is confined.** Text and `http(s)` URLs are
  unchanged; a file is accepted only inside the vault's project (its code root
  or a workspace `bk hooks install --root` recorded), outside the vault itself,
  and never a credential file such as `.env*`, a private key, or anything under
  `~/.ssh`. `bk capture <path>` is not restricted.
- **A `cloud` server does not operate integrations.** `integration_configure`,
  `integration_up`, `integration_down` and `integration_sync` are refused with
  `policy_denied` and left out of `tools/list`; the refusal names `bk
  integration <verb> <name>` and `--consumer local` as the ways to run them.
  `integration_status` stays available. A `local` server runs all four, with
  machine layout (paths, container names, env-var names) scrubbed from the
  response.

See [the privacy boundary](./privacy.md#an-mcp-server-declares-its-consumer)
and [ADR 0010](./knowledge/decisions/architecture/0010-mcp-declares-its-consumer.md).

## MCP over the network

The stdio transport remains the zero-network default. Network clients use the
stateless MCP Streamable HTTP endpoint at `/mcp`; every request requires the
same pre-shared Bearer token, loaded only from the explicitly named environment
variable.

```bash
export BRAINKIT_MCP_TOKEN='use-a-secret-manager-in-production'
bk --vault ./my-vault serve --mcp --transport http \
  --host 127.0.0.1 --port 8766 --token-env BRAINKIT_MCP_TOKEN
```

The HTTP transport takes the same `--consumer`, with the same `cloud` default.

A direct initialization request looks like this:

```bash
curl http://127.0.0.1:8766/mcp \
  -H "Authorization: Bearer $BRAINKIT_MCP_TOKEN" \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  --data '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"example","version":"1"}}}'
```

Subsequent requests must include `MCP-Protocol-Version: 2025-06-18`. Browser
Origins are checked against repeatable `--allowed-origin` values, request bodies
are bounded, and responses disable caching. A non-loopback bind also requires
`--tls-cert` and `--tls-key`; plain HTTP is permitted only on loopback. This is
intentionally pre-shared-token authentication for trusted agents, not an OAuth
authorization-server implementation. The server returns each POST response as
JSON and runs without sessions; standalone SSE `GET` streams are not enabled.

Both transports expose the same tools, backed by the same application use cases
as the CLI:

| Group | Tools |
|---|---|
| Evidence | `capture`, `search`, `context`, `file` |
| Compilation | `apply`, `ask`, `resurface` |
| Review | `proposals`, `approve`, `reject` |
| Operations | `status`, `lint` |
| Integrations | `integration_configure`, `integration_status`, `integration_up`, `integration_down`, `integration_sync` |

A `cloud` server lists and answers every row except the four integration
lifecycle tools; only `integration_status` remains in that group.

`bk vaults` is deliberately absent from that list: an MCP server is started for
one vault and answers under that vault's declared boundary, so a tool that
reached into unrelated vaults would widen the boundary the caller was granted.
