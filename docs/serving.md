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
unless `--token-env` names a populated bearer-token environment variable. The
read-only API exposes `/api/health`, `/api/status`, `/api/graph`, `/api/search`,
`/api/proposals`, `/api/resource`, `/api/sources`, `/api/pages`, `/api/timeline`
and `/api/integrations`.

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

`bk vaults` is deliberately absent from that list: an MCP server is started for
one vault and answers under that vault's declared boundary, so a tool that
reached into unrelated vaults would widen the boundary the caller was granted.
