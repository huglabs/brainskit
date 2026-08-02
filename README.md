# brainkit (`bk`)

`brainkit` is a local-first, CLI-first second-brain engine. Markdown and JSON are
the source of truth; SQLite FTS5 is a disposable search index. LLMs may propose
editorial changes, but only the deterministic `bk apply` gate can write the
`wiki/`.

The current Python engine implements the M0–M3 local walking skeleton:

- policy-first vault initialization;
- immutable capture with SHA-256 identity and registry reconciliation;
- FTS5 indexing/search, evidence context, lint, generated views and graph;
- one crash-recoverable unit of work for wiki, freshness, FTS5 and raw filing;
- schema, citation, link and novelty gates on every `bk apply`;
- durable approve/reject filing proposals driven by per-branch policy;
- freshness lifecycle (`fresh`, `review`, `stale`) and resurfacing;
- consumer-aware privacy filtering across BM25 and graph expansion;
- schema-bound judgment outputs with automatic repair feedback;
- provider-neutral jobs with Anthropic, OpenAI, OpenRouter and Ollama drivers;
- JSON CLI mode plus stdio and authenticated Streamable HTTP MCP transports;
- persistent native integrations for Obsidian, Neo4j and PostgreSQL graphs;
- opt-in lifecycle management with durable Docker volumes for graph databases;
- a complete, privacy-aware web viewer with search, graph navigation, health,
  freshness, branch counts, review queue and source/page inspection.

It also includes the M3 local sockets (`serve --mcp`, watched local folders,
schedule registration output and the web API). Google Drive OAuth/polling,
hosted gateway-agent adapters and a native Kuzu driver remain connector
milestones; they are not silently simulated by this repository.

## Install

```bash
python -m pip install -e .
bk --help
```

Install only the native database drivers you use:

```bash
python -m pip install -e '.[neo4j]'
python -m pip install -e '.[postgres]'
# or both
python -m pip install -e '.[integrations]'
```

No `.env` file is loaded. Provider secrets are read only from the environment
variable explicitly named in the vault configuration.

## Fast start

Run `bk init ./my-vault` and answer every policy question. For automation, pass
a complete config file:

```bash
bk init ./my-vault --config policy.json --json
bk --vault ./my-vault capture notes.md --json
bk --vault ./my-vault reindex --json
bk --vault ./my-vault search "retrieval memory" --consumer local --json
```

An `apply` proposal is a JSON document:

```json
{
  "operations": [
    {
      "action": "upsert",
      "kind": "concept",
      "slug": "compiled-memory",
      "title": "Compiled memory",
      "aliases": ["memória compilada"],
      "source_hashes": ["<64-char sha256>"],
      "body": "Evidence-backed text.[^source:<64-char sha256>]",
      "links": [],
      "base_hash": null
    }
  ]
}
```

`bk context QUERY --consumer local --json` provides the evidence bundle an
agent needs to create that proposal. `bk apply proposal.json --json` validates
the complete batch before any wiki page is replaced. For updates, `base_hash`
must match the page version returned by `context`; retries with the same
`proposal_id` and payload are idempotent, while key reuse with another payload
is rejected. An interrupted multi-page commit is rolled back when the vault is
opened again. Filing uses the same unit of work: wiki pages, page freshness,
registry/source status, the raw-file move and the SQLite index either become
visible together or are restored from the transaction journal. The index update
is incremental, so a normal apply does not pay for a full rebuild.

`.brain/schema.json` is validated as the JSON Schema draft declared by its
`$schema` URI. The gate supports the complete vocabulary implemented by
`jsonschema` for that draft, including combinators, conditionals, formats,
`$defs`, local `$ref`, `dependentRequired` and `unevaluatedProperties`, before
applying brainkit's provenance, citation, link and reserved-field invariants.
Remote `$ref` retrieval is deliberately denied: a vault schema cannot cause an
implicit network request or leak local policy data. Bundle referenced schemas
under local `$defs` instead.

Machine callers must declare their privacy boundary:

```bash
bk --vault ./my-vault context "topic" --consumer cloud --json
bk --vault ./my-vault search "topic" --consumer local --json
```

`cloud` receives only cloud-eligible evidence and `local` excludes
`never-ingest`. `human` applies no restriction at all: it is the default for
interactive, non-JSON use, and a machine caller that names it explicitly —
through `--json`, MCP, or a `--consumer human` integration such as the local
web viewer — receives `never-ingest` bodies. Declaring the boundary is
mandatory for machine callers precisely because the unrestricted value has to
be a deliberate choice rather than a silent default. Privacy filtering also
applies to graph-expanded search neighbors.

## Persistent integrations

Every integration is opt-in and stored in `.brain/config.json`; lifecycle and
sync checkpoints are stored in `.brain/integration-state.json`. Secrets are
never persisted. Configuration stores only the name of an environment
variable. `bk integration status` combines the durable policy with live
process/container state.

All capabilities are available through JSON CLI and the MCP tools
`integration_configure`, `integration_status`, `integration_up`,
`integration_down` and `integration_sync`.

### Obsidian

Obsidian sync is manifest-based. It copies the generated `wiki/`, `views/` and
`graph/graph.json` into the selected vault and removes only files that brainkit
previously managed. Human-owned Obsidian content is never deleted. Raw evidence
is excluded unless `--include-raw` is explicitly selected.

```bash
bk --vault ./my-vault integration configure obsidian \
  --enable --external --path "$HOME/Obsidian" --subdirectory brainkit
bk --vault ./my-vault export --target obsidian
bk --vault ./my-vault integration status obsidian
```

Point `--path` at the brainkit vault itself for in-place Obsidian use. In that
mode brainkit creates only the minimal `.obsidian/app.json` when absent and does
not duplicate the knowledge files.

### Neo4j

Neo4j uses the official Python driver and writes `BrainkitNode` nodes plus
`SOURCED_FROM` and `LINKS_TO` relationships in one database transaction. This
is a real Bolt push, not a Cypher-file export. Every node is namespaced with a
stable vault ID, so a refresh replaces only that vault's subgraph and repeated
syncs are idempotent. It can connect to an operator-owned service (`--external`)
or create a Docker service (`--managed`) whose data survives stop/start under
`.brain/services/neo4j/data`.

```bash
export BRAINKIT_NEO4J_PASSWORD='use-a-secret-manager-in-production'
bk --vault ./my-vault integration configure neo4j \
  --enable --managed --uri bolt://127.0.0.1:7687 --user neo4j \
  --password-env BRAINKIT_NEO4J_PASSWORD --database neo4j --consumer local
bk --vault ./my-vault integration up neo4j
bk --vault ./my-vault integration sync neo4j
bk --vault ./my-vault integration down neo4j
```

### PostgreSQL graph

The PostgreSQL target is native, portable PostgreSQL: a JSONB-enriched
`nodes`/`edges` graph, indexed adjacency columns, referential integrity and a
recursive `graph_walk(start_node, max_depth)` SQL function. It does not require
a graph extension. Managed mode runs PostgreSQL in Docker with durable data at
`.brain/services/postgres/data`; external mode reads a DSN from the named
environment variable.

```bash
export BRAINKIT_POSTGRES_PASSWORD='use-a-secret-manager-in-production'
bk --vault ./my-vault integration configure postgres \
  --enable --managed --password-env BRAINKIT_POSTGRES_PASSWORD \
  --user brainkit --database brainkit --schema brainkit --port 5432 \
  --consumer local
bk --vault ./my-vault integration up postgres
bk --vault ./my-vault export --target postgres
bk --vault ./my-vault integration down postgres
```

For an existing service:

```bash
export BRAINKIT_POSTGRES_DSN='postgresql://user:password@host/database'
bk --vault ./my-vault integration configure postgres \
  --enable --external --dsn-env BRAINKIT_POSTGRES_DSN \
  --schema brainkit --consumer cloud
bk --vault ./my-vault integration sync postgres
```

`bk integration up` waits for the service to accept connections the same way a
client will, and requires it to stay up before reporting `ready` — PostgreSQL
answers on its unix socket from the temporary server it runs during `initdb`,
which then shuts down and restarts. A first boot also has to chown the
vault-local data directory, which is slow on macOS bind mounts, so the deadline
is 300 seconds and can be raised per integration with
`ready_timeout_seconds` in its stored options.

`consumer` is mandatory for Neo4j and PostgreSQL and optional for Obsidian,
where it defaults to `local`. `cloud` exports only cloud-eligible evidence;
`local` also permits local-only evidence but always redacts `never-ingest`. The
filter runs after graph expansion, so edges cannot reintroduce restricted nodes.

Every egress carries the same boundary, including the file targets:

```bash
bk --vault ./my-vault export --target json                    # local (default)
bk --vault ./my-vault export --target cypher --consumer cloud
bk --vault ./my-vault export --target llms-txt --consumer human
```

`--consumer` defaults to `local`, so an export never emits `never-ingest`
evidence unless `human` is named deliberately. Passing it to `--target
obsidian`, `neo4j` or `postgres` is rejected rather than applied: those targets
carry their own configured consumer, and silently overriding it could widen the
boundary past what the integration was configured to permit.

Filtering covers node bodies and node metadata alike. A filename and its branch
are themselves disclosure, so a redacted source contributes neither. For the
same reason `search` and `context` report `redacted` as a count and never
describe what was withheld: the bundle `context` returns is the payload handed
to a cloud model, and naming a withheld source there would defeat the boundary
that dropped it.

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

## Filing and review

`bk ingest` first proposes a destination branch and then produces a
schema-valid apply proposal. The configured destination policy controls the
outcome:

- `auto+digest-review`: file and apply immediately, retaining the audit record;
- `approve-each`: store the proposal without moving or writing anything.

```bash
bk --vault ./my-vault ingest --all --json
bk --vault ./my-vault proposals --status pending --json
bk --vault ./my-vault approve <proposal-id> --json
bk --vault ./my-vault reject <proposal-id> --reason "not useful" --json
```

Judgment jobs are validated against `jobs/_output-schemas/`. Invalid model
output is retried with structured validation feedback; no hardcoded answer is
substituted.

## Onboarding a coding agent

`bk hooks install` teaches an agent the vault contract instead of hoping it
infers one:

```bash
bk --vault ./my-vault hooks install --agent claude
```

It installs `.claude/skills/brainkit/SKILL.md`, appends a managed block to the
agent's instruction file (`CLAUDE.md`, or `AGENTS.md`/`GEMINI.md` for the other
agents) covering how the graph is formed, where the privacy boundary applies and
which commands may write, and installs a `pre-commit` hook running `bk lint`
when the vault is a git repository.

Everything it writes is safe to re-run. The instruction block is fenced by
`<!-- brainkit:start -->` / `<!-- brainkit:end -->` and replaced in place, so
your own instructions keep their content and their position. An existing skill
or a pre-existing `pre-commit` hook is reported rather than overwritten; pass
`--force` to replace them. A vault without git still installs everything else.

## Freshness and integrity

Applied pages are tracked in `.brain/freshness.json`. A new related capture
marks affected pages for review, the configured age threshold marks pages
stale, and `bk resurface` selects a durable insight through the configured
provider. `bk lint` reports raw-source mutation, direct wiki edits outside the
apply gate, unresolved provenance, broken links, and stale pages.

Freshness is keyed by path, so a page deleted outside the gate leaves an entry
that can never be revived. `bk lint` reports it as `freshness.orphaned`,
`bk status` stops counting it, and `bk reconcile` removes it — the same command
that re-links a moved source by its hash.

## Architecture

```text
interfaces (CLI, MCP, read-only web API/viewer)
        ↓
application (use cases and ports)
        ↓
domain (entities, values, policies, invariants)
        ↑
infrastructure (vault, FTS5, LLM and persistent integration adapters)
```

The domain has no dependency on the CLI, filesystem, SQLite or an LLM vendor.

When evidence spans branches, the judgment router applies the strictest policy:
`never-ingest` denies the call, `local-only` requires Ollama, and cloud routing
is allowed only when every contributing branch permits it. A job mapping may
define privacy-specific routes:

```json
{
  "query": {
    "cloud": { "provider": "openai", "model": "gpt-example" },
    "local-only": { "provider": "ollama", "model": "qwen-example" }
  }
}
```

Because `local-only` evidence may only reach Ollama, that route must not be the
narrowest one. Ollama otherwise applies its own 4096-token context regardless of
the window a model advertises, which is smaller than a digest prompt on a vault
of any size. `providers.ollama.options` is forwarded verbatim to the Ollama API
and defaults to `{"temperature": 0, "num_ctx": 16384}`; operator values override
it per key. `temperature` stays at `0` by default because judgment output is
schema-bound and determinism matters.

```json
{
  "providers": {
    "ollama": {
      "base_url": "http://127.0.0.1:11434",
      "options": { "num_ctx": 32768 }
    }
  }
}
```

## Remaining product milestones

The engine contract is now suitable for local dogfooding, but the end-to-end
daily habit still needs the external delivery layer: Google Drive OAuth/delta
polling, gateway-agent adapters for real WhatsApp/Telegram delivery, a
production scheduler and end-to-end tests against live LLM/database providers.
The native Kuzu adapter and seed-corpus importers remain later roadmap work.
