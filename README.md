<div align="center">

# brainkit `bk`

**A HugLabs engine — Hug Know family (Knowledge & Intelligence)**

*The compilation gate between raw evidence and knowledge an agent is allowed
to act on.*

[![HugLabs](https://img.shields.io/badge/HugLabs-Hug%20Know-d97757?style=flat-square)](https://gitlab.dev.hugyourcustomer.ai/prototipos-raul/brainkit)
[![version](https://img.shields.io/badge/version-0.4.0-30302e?style=flat-square)](./pyproject.toml)
[![python](https://img.shields.io/badge/python-3.11%2B-30302e?style=flat-square)](./pyproject.toml)
[![license](https://img.shields.io/badge/license-Apache--2.0-30302e?style=flat-square)](./pyproject.toml)
[![status](https://img.shields.io/badge/status-local%20dogfooding-b7b5a9?style=flat-square)](#roadmap)

</div>

---

> **87% of AI projects never reach production. HugLabs exists to be the 13%.**
> brainkit is that principle applied to memory: a model may *propose* what your
> knowledge base should say, but only a deterministic gate may *write* it.

`brainkit` is a local-first, CLI-first second-brain engine. Markdown and JSON are
the source of truth; SQLite FTS5 is a disposable search index. LLMs may propose
editorial changes, but only the deterministic `bk apply` gate can write the
`wiki/`.

## Contents

- [Why brainkit](#why-brainkit)
- [Where it fits at HugLabs](#where-it-fits-at-huglabs)
- [What the engine implements](#what-the-engine-implements)
- [Install](#install)
- [Development](#development)
- [Fast start](#fast-start)
- [Vault layout](#vault-layout)
- [Command reference](#command-reference)
- [The privacy boundary](#the-privacy-boundary)
- [Persistent integrations](#persistent-integrations)
- [Web viewer](#web-viewer)
- [MCP over the network](#mcp-over-the-network)
- [Filing and review](#filing-and-review)
- [Onboarding a coding agent](#onboarding-a-coding-agent)
- [Freshness and integrity](#freshness-and-integrity)
- [Architecture](#architecture)
- [Roadmap](#roadmap)
- [Support and contributing](#support-and-contributing)

## Why brainkit

A knowledge base an agent writes to freely stops being evidence and becomes
model output with a filename. brainkit takes the opposite position and enforces
it mechanically:

| Invariant | How it is enforced |
|---|---|
| **Raw evidence is immutable** | Captures are identified by SHA-256 of their bytes; `bk lint` compares current bytes against the registered hash, and `bk reconcile` heals a move without rewriting identity. |
| **Only the gate writes the wiki** | `bk apply` validates schema, citations, links and novelty for the whole batch before a single page is replaced; direct wiki edits are reported by `lint`. |
| **Every claim carries provenance** | A page body cites the source hashes it was derived from, and those sources must resolve inside the vault before the write is eligible. |
| **Privacy is a declared boundary** | Machine callers must name a consumer (`local`, `cloud`, `human`); the filter runs after graph expansion, so an edge cannot reintroduce restricted evidence. |
| **Mechanical stays LLM-free** | Capture, index, search, apply, export and the structural lint never call a model. Judgment flows are separate, schema-bound, and routed by the strictest policy in the evidence set. |
| **A write is one unit of work** | Wiki pages, freshness, registry status, the raw-file move and the FTS5 update become visible together or are restored from the transaction journal. |

## Where it fits at HugLabs

brainkit belongs to the **Hug Know** surface — knowledge and intelligence — as
the local, single-operator counterpart to the hosted knowledge products. It is
deliberately *not* a service: it runs on a workstation, owns no account system,
holds no credentials, and needs no network to do its mechanical work.

That makes it useful in two places:

- **as an operator tool** — a personal, auditable second brain for research,
  briefs and long-running product context;
- **as a reference implementation** — the provenance, privacy-boundary and
  apply-gate contracts here are the ones HugLabs knowledge products are expected
  to honour, expressed in a codebase small enough to read end to end.

Provider neutrality is a portfolio requirement, not a preference: Anthropic,
OpenAI, OpenRouter and Ollama are interchangeable drivers behind one job
contract, and `local-only` evidence is routed to Ollama or not at all.

## What the engine implements

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

`bk` operates on a vault directory, not on the project it is invoked from, so
install it as an isolated uv tool. It lands on `PATH` for every vault without
becoming a dependency of any project:

```bash
uv tool install /path/to/brainkit
bk --help
```

Install only the native database drivers you use:

```bash
uv tool install '/path/to/brainkit[neo4j]'
uv tool install '/path/to/brainkit[postgres]'
# or both
uv tool install '/path/to/brainkit[integrations]'
```

The same target accepts a built wheel or the repository over HTTPS, which
resolves through the git credential helper and pins the installed commit:

```bash
uv tool install 'brainkit[integrations] @ ./dist/brainkit-0.4.0-py3-none-any.whl'
uv tool install 'brainkit[integrations] @ git+https://gitlab.dev.hugyourcustomer.ai/prototipos-raul/brainkit.git'
```

Append `@<ref>` to pin a branch, tag or commit instead of the default branch
tip. Without a ref the install still records the resolved commit, so upgrading
is always an explicit `uv tool upgrade brainkit`:

```bash
uv tool install 'brainkit[integrations] @ git+https://gitlab.dev.hugyourcustomer.ai/prototipos-raul/brainkit.git@main'
```

Reinstall after changing extras or the checkout with `--force`, drop the tool
with `uv tool uninstall brainkit`, and use `-e` while developing the engine so
`bk` always runs the working tree:

```bash
uv tool install --force -e '/path/to/brainkit[integrations]'
```

To pin `bk` to one project instead of the machine, declare it as a dependency
and run it through the project environment:

```bash
uv add /path/to/brainkit
uv run bk --vault ./my-vault status
```

No `.env` file is loaded. Provider secrets are read only from the environment
variable explicitly named in the vault configuration.

## Development

The repository is uv-managed. `uv.lock` pins the development environment and
`.python-version` pins the interpreter; neither constrains an installed `bk`.

```bash
uv sync --group dev              # engine + pytest, ruff, mypy
uv sync --all-extras --group dev # add the Neo4j and PostgreSQL drivers

uv run pytest
uv run ruff check
uv run mypy src
```

Delivery is gated by a wheel that is built, installed in a throwaway
environment, and driven through the real CLI contract, because packaged prompt
specs, output schemas and templates cannot be verified from the source tree.
The gate builds the sdist first and verifies the wheel produced *from it*,
which is the artifact publishing uploads:

```bash
./scripts/verify-wheel.sh
```

### Publishing to a GitLab package registry

`scripts/publish.sh` runs that gate, then uploads the artifacts it proved. It
refuses a dirty working tree, since a published version is permanent and must
stay reproducible from a commit. Credentials are read from the environment and
passed to uv through `UV_PUBLISH_PASSWORD`, so no token reaches the process
arguments, a config file or the repository:

The token belongs in the macOS keychain, not in a file this repository could
ever contain. Passing `-w` last makes `security` prompt for it, so it reaches
neither the shell history nor the process arguments:

```bash
# once — GitLab: Settings → Access Tokens, scope write_package_registry
security add-generic-password -a "$USER" -s brainkit-gitlab -U -w

# per publish session
export BRAINKIT_GITLAB_TOKEN="$(security find-generic-password -a "$USER" -s brainkit-gitlab -w)"

./scripts/publish.sh --dry-run   # build and gate, upload nothing
./scripts/publish.sh
```

The host and project id default to this repository's own registry; override
`BRAINKIT_GITLAB_HOST` and `BRAINKIT_GITLAB_PROJECT_ID` to publish a fork
somewhere else.

Consumers then install by name and version, with the registry as a named index:

```bash
uv tool install brainkit \
  --index brainkit=https://gitlab.dev.hugyourcustomer.ai/api/v4/projects/129/packages/pypi/simple
```

Bump `[project].version` before every publish; a registry rejects the re-upload
of a filename it already stores.

Engineering conventions — and the defect classes this codebase has already paid
for — are recorded in [`AGENTS.md`](./AGENTS.md). Read it before changing the
apply gate, the privacy filter or an integration lifecycle.

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

## Vault layout

`bk init` scaffolds every directory the engine files into, so a page kind can
never land somewhere the vault does not own:

```text
my-vault/
├── raw/                     immutable evidence, identified by SHA-256
│   ├── _inbox/              landing zone before a filing decision
│   ├── _assets/
│   └── <branch>/            one directory per configured branch
├── wiki/                    the compiled surface; written only by the gate
│   ├── sources/  entities/  concepts/  syntheses/
│   ├── index.md             system pages, maintained by the engine
│   └── log.md
├── views/map/  views/domains/   generated navigation
├── graph/                   generated graph.json
├── output/                  digests/, reports/, answers/
└── .brain/                  policy and durable state
    ├── config.json          branches, providers, integrations (no secrets)
    ├── schema.json          human-owned page schema, enforced by apply/lint
    ├── registry.json        source hash → path and status
    ├── freshness.json       applied page hashes and lifecycle state
    ├── proposals.json       pending filing proposals
    ├── applied.json         idempotency keys for executed applies
    ├── integration-state.json   PIDs, containers, sync checkpoints
    └── index.db             disposable FTS5 index (git-ignored)
```

`.brain/schema.json` is yours to edit. Everything else under `.brain/` is engine
state: change it by running a command, not with a text editor.

## Command reference

Global options come before the subcommand: `--vault PATH` selects the vault
(otherwise it is discovered upward from the working directory) and `--json`
switches to machine-readable output.

**Mechanical — never calls a model**

| Command | Purpose |
|---|---|
| `init [PATH] [--config FILE\|-]` | Initialize a policy-complete vault |
| `capture [SOURCE] [--text T] [--title T]` | Capture a file, URL or literal text |
| `status` | Vault health and counts |
| `reconcile` | Heal registry paths after manual moves; drop orphaned freshness |
| `reindex` | Rebuild the disposable FTS5 index |
| `file` | Move a raw source to a branch |
| `lint [--changed]` | Validate registry and wiki contracts |
| `search QUERY [--limit N] [--consumer C]` | FTS5 BM25 search |
| `context QUERY [--limit N] [--max-chars N] [--consumer C]` | Bounded evidence bundle |
| `apply PROPOSAL\|-` | Validate and atomically commit wiki writes |
| `views` · `graph [--html]` | Regenerate views and the knowledge graph |
| `export --target T [--consumer C]` | Export to `json`, `graphml`, `cypher`, `obsidian`, `neo4j`, `postgres`, `kuzu`, `llms-txt` |
| `proposals [--status S]` · `approve ID` · `reject ID --reason R` | Filing review queue |
| `integration configure\|status\|up\|down\|sync NAME` | Persistent integration lifecycle |
| `web serve [--host H] [--port P] [--consumer C] [--token-env V]` | Foreground web viewer |
| `serve --mcp [--transport stdio\|http] …` | MCP transports |
| `watch [--once] [--interval S]` | Watch configured source folders |
| `schedule` | Show configured habit job registrations |
| `hooks install --agent claude\|codex\|gemini\|opencode [--force]` | Install the agent contract |

**Judgment — routed through job specs and output schemas**

| Command | Purpose |
|---|---|
| `ingest [ITEM] [--all]` | Propose a branch, then a schema-valid apply proposal |
| `ask QUERY` | Answer from compiled vault evidence |
| `digest` | Generate the configured digest |
| `resurface` | Surface one durable insight |
| `lint --semantic` | Add the `lint-semantic` judgment pass to the structural report |

## The privacy boundary

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

Filtering covers node bodies and node metadata alike. A filename and its branch
are themselves disclosure, so a redacted source contributes neither. For the
same reason `search` and `context` report `redacted` as a count and never
describe what was withheld: the bundle `context` returns is the payload handed
to a cloud model, and naming a withheld source there would defeat the boundary
that dropped it.

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

### Egress carries the boundary

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

## Roadmap

The engine contract is now suitable for local dogfooding, but the end-to-end
daily habit still needs the external delivery layer:

| Milestone | State |
|---|---|
| M0–M3 local walking skeleton | Implemented |
| Local sockets: MCP stdio/HTTP, watched folders, web API | Implemented |
| Persistent Obsidian / Neo4j / PostgreSQL integrations | Implemented |
| Google Drive OAuth and delta polling | Connector milestone |
| Gateway-agent adapters for real WhatsApp/Telegram delivery | Connector milestone |
| Production scheduler | Connector milestone |
| End-to-end tests against live LLM and database providers | Planned |
| Native Kuzu adapter, seed-corpus importers | Later roadmap |

Nothing on the unimplemented side is simulated by this repository: a command
that would need a missing connector fails instead of pretending.

## Support and contributing

| | |
|---|---|
| **Repository** | `gitlab.dev.hugyourcustomer.ai/prototipos-raul/brainkit` |
| **Maintainer** | HugLabs |
| **Engineering notes** | [`AGENTS.md`](./AGENTS.md) |
| **License** | Apache-2.0, as declared in [`pyproject.toml`](./pyproject.toml) |

Before opening a merge request, run the full gate — `uv run pytest`,
`uv run ruff check`, `uv run mypy src` and `./scripts/verify-wheel.sh` — and
record any new defect class in `AGENTS.md`. The project is lint-clean, not
format-clean: a `ruff format` diff is pre-existing and is not a regression.
</content>
