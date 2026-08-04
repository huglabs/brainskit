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
- [The code graph](#the-code-graph)
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
becoming a dependency of any project.

### From the package registry

Released versions are published to this project's GitLab PyPI registry, which
is the path for anyone who is not developing the engine. The registry is
private — an unauthenticated request to the index returns `401` — so uv needs a
token with `read_package_registry`. Give the index a name and uv takes the
credentials from the environment, keeping the token out of the shell history
and the process arguments:

```bash
# once — GitLab: Settings → Access Tokens, scope read_package_registry.
# -w last makes security prompt, so the token misses the shell history too.
security add-generic-password -a "$USER" -s brainkit-gitlab-read -U -w

export UV_INDEX_BRAINKIT_USERNAME=brainkit-reader   # any value; a PAT ignores it
export UV_INDEX_BRAINKIT_PASSWORD="$(security find-generic-password -a "$USER" -s brainkit-gitlab-read -w)"

uv tool install brainkit \
  --index brainkit=https://gitlab.dev.hugyourcustomer.ai/api/v4/projects/129/packages/pypi/simple
bk --help
```

Extras and exact versions work as usual, and uv records the index in the tool
receipt, so a later `uv tool upgrade brainkit` reuses it and needs only the two
environment variables again:

```bash
uv tool install 'brainkit[integrations]==0.4.0' \
  --index brainkit=https://gitlab.dev.hugyourcustomer.ai/api/v4/projects/129/packages/pypi/simple
```

### From a checkout

Install the working tree, or a specific ref, when you are developing the engine
or need a version that was never published:

```bash
uv tool install /path/to/brainkit
```

Install only the native database drivers you use:

```bash
uv tool install '/path/to/brainkit[neo4j]'
uv tool install '/path/to/brainkit[postgres]'
# or both
uv tool install '/path/to/brainkit[integrations]'
```

Two further extras are optional for the same reason — the core keeps a single
dependency, and neither capability is one every vault wants:

```bash
uv tool install '/path/to/brainkit[code]'     # bk code: tree-sitter grammars, ~70 MB
uv tool install '/path/to/brainkit[convert]'  # capture .docx/.pdf/.pptx via markitdown
```

`code` is the larger commitment: vendoring the analysis source did not vendor a
parser, so the grammars stay compiled wheels and remain a real dependency. A
`bk code` command without it fails with the install hint rather than a stack
trace, and the code-graph tests skip rather than fail — so a vault that never
reads a repository never pays for one. Without `convert`, a non-text capture is
stored verbatim with a "no converter available" note rather than being refused.

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
uv tool install 'brainkit[integrations] @ git+https://gitlab.dev.hugyourcustomer.ai/prototipos-raul/brainkit.git@v0.4.0'
```

Every published version carries a matching `v<version>` tag, so a release is
installable from the registry and from git without knowing a commit hash.

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

Consumers then install by name, as described in
[From the package registry](#from-the-package-registry).

### Versioning and tags

Releases are `MAJOR.MINOR.PATCH` in `[project].version`, and every published
version carries an annotated `v<version>` git tag on the commit it was built
from. The registry rejects the re-upload of a filename it already stores, so a
version is permanent and the tag is the only durable record of which tree
produced it. Bump the version *before* publishing, never after:

```bash
# 1. bump [project].version, then commit
git commit -am 'Release 0.5.0'
# 2. tag the exact commit the artifact will be built from
git tag -a v0.5.0 -m 'brainkit 0.5.0'
# 3. publish — the gate refuses a dirty tree, so this is reproducible
./scripts/publish.sh
# 4. push the commit and the tag together
git push origin main --follow-tags
```

Tags already in the repository:

| Tag | Version | State |
|---|---|---|
| `v0.4.0` | 0.4.0 | Published to the registry |

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
    ├── config.json          branches, providers, ignore, integrations (no secrets)
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
| `forget ITEM [--force]` | Drop one source record whose raw file is gone from the registry |
| `lint [--changed]` | Validate registry and wiki contracts |
| `search QUERY [--limit N] [--consumer C]` | FTS5 BM25 search |
| `context QUERY [--limit N] [--max-chars N] [--consumer C]` | Bounded evidence bundle |
| `apply PROPOSAL\|-` | Validate and atomically commit wiki writes |
| `gate check-write PATH [--agent A]` | Whether a direct write to `PATH` is permitted |
| `views` · `graph [--html]` | Regenerate views and the knowledge graph |
| `code build [PATH …]` · `code import` · `code status` | Extract the repository graph, import one, re-check it — `build` needs `[code]`. Given `PATH`s, merges that subset into the stored graph instead of replacing it |
| `code affected` · `code path` · `code hubs` | Queries over that graph, on the base install |
| `code communities` · `code cycles` · `code diff` | Delegated to the vendored analysis — needs `[code]` |
| `export --target T [--consumer C]` | Export to `json`, `graphml`, `cypher`, `obsidian`, `neo4j`, `postgres`, `kuzu`, `llms-txt` |
| `proposals [--status S]` · `approve ID` · `reject ID --reason R` | Filing review queue |
| `integration configure\|status\|up\|down\|sync NAME` | Persistent integration lifecycle |
| `vaults register\|list\|forget\|sync` | The vaults on this machine, synced into one shared store |
| `web serve [--host H] [--port P] [--consumer C] [--token-env V]` | Foreground web viewer |
| `serve --mcp [--transport stdio\|http] …` | MCP transports |
| `watch [--once] [--interval S]` | Capture new files under the configured source folders, minus `ignore` |
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

## The code graph

`bk code` describes the repository a vault documents, so a question about the
code is answered from its structure rather than from a grep.

Only two things need the `[code]` extra, and they need different halves of it:
`build` needs the tree-sitter grammars to extract, and `communities`, `cycles`
and `diff` need networkx for the vendored analysis. Everything else — `import`,
`status`, `affected`, `path`, `hubs` — reads the stored graph and runs on the
base install, so a graph extracted elsewhere can be imported and queried with
no extra at all. A command that does need it fails with the install hint rather
than a stack trace.

```bash
bk code build                     # extract in-process and store graph/code.json
bk code import GRAPH.json         # or take one an external extractor produced
bk code status                    # does the stored graph still describe the tree?
bk code affected SYMBOL           # what breaks if this changes
bk code path FROM TO              # shortest chain of edges between two symbols
bk code hubs                      # the most connected symbols
bk code communities               # structurally cohesive clusters
bk code cycles                    # import cycles among files
bk code diff                      # what changed structurally since the stored graph
```

`code_root` is read from `.brain/config.json`, is relative to the vault, and is
discovered upward when absent. An explicitly empty string means the vault root,
for a vault that sits at the top of the repository it documents — the same
absent-versus-empty rule the `ignore` patterns follow. The vault's own
directories are excluded from the graph: an extractor pointed at the repository
has no idea one of those folders is the vault asking the question, and left in
they arrive as the most connected nodes in it.

`build` and `import` share one importer, so a graph from an external extractor
is normalised on the way in rather than trusted as given. `communities`,
`cycles` and `diff` are the three questions brainkit does not answer itself and
are delegated to the vendored analysis; `affected`, `path` and `hubs` use
brainkit's own traversal, which needs no dependency and already answers under a
`--consumer`.

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

One schema holds as many vaults as you point at it. Every row carries the
`vault_id` of the vault that wrote it, and a sync deletes only that vault's
rows, so refreshing one vault never touches another's — including a vault owned
by a different application sharing the schema. Both tables index `vault_id`.

Because the schema is shared, the stored `id` is namespaced the same way the
Neo4j export namespaces its own: `<vault_id>:<natural id>`. The natural id is
not lost — `properties` holds the untouched node, so `properties->>'id'` is the
unprefixed id (`page:wiki/index.md`, `raw:<content hash>`), and on `edges`,
`properties->>'source'` and `properties->>'target'` likewise. Filter by column
and read the natural key from JSONB:

```sql
SELECT properties->>'id' AS id, label, path
FROM brainkit.nodes WHERE vault_id = $1 AND kind = 'wiki';
```

`graph_walk` needs no vault argument and takes none: since every id carries its
vault's prefix, a walk cannot leave the vault it started in. Pass it the stored
prefixed id, not the natural one.

Upgrading an existing deployment needs no manual migration. The sync adds the
`vault_id` column if the tables predate it, adopts the rows already there into
the vault performing that first sync — safe because the previous behaviour
truncated both tables on every refresh, so what is on disk is exactly one
vault's last complete sync — and replaces them on the same run.

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

### Many vaults, one store

`bk integration sync` syncs the one vault it was pointed at. When an operator
runs several applications, each with its own vault, `bk vaults` keeps the list
and syncs them as a set, so the shared graph is the union of all of them.

There is no discovery step, and that is deliberate: vaults live in unrelated
projects, and a filesystem scan would be slow, would miss anything outside the
trees it was pointed at, and would find checkouts, copies and backups that must
never be synced into a shared store under their own identity. The list is
declared once and lives at `$XDG_CONFIG_HOME/brainkit/vaults.json` (default
`~/.config/brainkit/vaults.json`), file `0600` inside a `0700` directory. It
holds paths and labels and nothing else — the same rule vault configuration
follows, where only the *name* of an environment variable is ever stored.

```bash
bk vaults register ./app-one --label app-one   # PATH defaults to the vault found from the cwd
bk vaults register ./app-two
bk vaults list                                 # label, path, whether it is still there, vault_id
bk vaults sync --target postgres               # --target defaults to postgres
bk vaults forget app-two                       # unregisters only; the vault's files are untouched
```

Each vault keeps its own policy. A vault that has not enabled the target is
**skipped**, not enabled on its behalf, and one vault failing does not stop the
rest — an unmounted disk or a service that is down is reported against that
vault alone:

```json
{"target": "postgres", "count": 4, "ok": 2, "skipped": 1, "failed": 1,
 "vaults": [{"label": "app-one", "vault_id": "2e2389340edfb82b1fe52ba9", "status": "ok", "result": {}},
            {"label": "app-optout", "status": "skipped", "reason": "postgres is not enabled in this vault's policy"},
            {"label": "app-gone", "status": "failed", "code": "not_found", "reason": "Not a brainkit vault"}]}
```

Exit is `1` when any vault failed and `0` when every vault succeeded or was
skipped, so a scheduled run can branch on the status alone. `list` reports a
vault whose directory has been deleted rather than failing, and still prints its
`vault_id` — which is what you need to find the rows it left behind in a shared
schema before running `bk vaults forget`.

`bk vaults sync` takes no `--consumer`, for the reason `export` refuses one on
an integration target, and one more: a sync refreshes by deleting the vault's
rows first, so a narrowed run would quietly replace what a shared store already
holds with less, across every registered vault at once. Set the boundary per
vault with `bk --vault <path> integration configure <target> --consumer`.

This group is CLI-only. Unlike the `integration_*` tools, it is not exposed over
MCP: an MCP server is started for one vault and answers under that vault's
declared boundary, so a tool that reached into unrelated vaults would widen the
boundary the caller was granted.

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
when the workspace is a git repository.

Everything it writes is safe to re-run. The instruction block is fenced by
`<!-- brainkit:start -->` / `<!-- brainkit:end -->` and replaced in place, so
your own instructions keep their content and their position. An existing skill
or a pre-existing `pre-commit` hook is reported rather than overwritten; pass
`--force` to replace them. A workspace without git still installs everything
else.

### The vault is not always the workspace

An agent reads `.claude/` and its instruction file from the **project it was
opened on**. When the vault is a directory inside that project, those are two
different places, so name the project with `--root`:

```bash
bk --vault ./docs/brain hooks install --agent claude --root .
```

`--root` receives the agent configuration — `.claude/`, the instruction file
and the git `pre-commit` hook — while the vault keeps `.brain/` and the vault
path baked into the hook scripts. The default is the vault itself, which is
what a standalone vault wants.

Getting this wrong used to be silent, and silence is the expensive part: every
file lands, the summary reads like success, and not one hook is ever loaded.
So an install that would repeat that mistake — a vault with no agent
configuration of its own, nested inside a directory that has some — says so on
stderr and names the flag that fixes it:

```text
bk: WORKSPACE - everything installed, nothing will load:
      The vault is not a project root, so an agent opened on /path/to/project
      will never load what was just installed here.
      Reinstall with --root /path/to/project
```

The resolved workspace is recorded in `.brain/agent-<agent>.json`, because
nothing else on disk remembers it and `bk status` has to look in the same place
the installer wrote to. An adapter written before that field existed falls back
to the vault, so an existing install keeps reporting exactly as it did.

## What a watch will not capture

`bk watch` walks every configured source folder and captures what it finds, and
a capture cannot be taken back: a source is identified by the hash of its bytes
and `raw/` is immutable. So the walk is filtered by `ignore` in
`.brain/config.json`, a list of shell globs matched against each path segment:

```json
{
  "ignore": ["node_modules", ".git", "__pycache__", "dist", "*.log", "docs/build"]
}
```

A pattern without a separator prunes that directory anywhere it appears, so
`node_modules` costs one comparison rather than a stat per file inside it. A
pattern with one is anchored to the source folder, so `docs/build` excludes
that tree and leaves every other `build` alone. Matching is case-insensitive,
because the primary target is a case-insensitive filesystem.

`bk init` offers the defaults — version-control metadata, dependency and build
directories, editor and OS droppings — prefilled, so they can be edited rather
than discovered later. A vault created before this field existed inherits those
defaults; a vault that stores `[]` has said "ignore nothing" and gets it.
`watch --json` reports `ignored` alongside `created`, counting pruned trees
once rather than per file inside them.

The vault's own directory is always excluded, so a source folder that contains
the vault cannot re-capture `raw/` into itself.

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

Inside the application layer, `BrainkitService` is a facade that owns nothing:
it composes the collaborators below and delegates. Their imports form a DAG —
each depends only on the ones above it — so any of them can be read, tested or
replaced without loading the rest.

| Module | Owns |
|---|---|
| `pages` | The page document format: render, parse, and the text helpers derived from it |
| `privacy` | The one answer to "may this consumer see this?" |
| `freshness` | Applied-page state and derived-artefact fingerprints |
| `judgment` | The bounded repair loop every schema-bound job shares |
| `compilation` | The apply gate — the only path that writes `wiki/` |
| `retrieval` | BM25 search and the bounded evidence bundle, filtered after expansion |
| `health` | Structural lint, `status`, and the projection report |
| `filing` | Propose a branch, then wait or execute per branch policy |
| `projections` | Views, graph, exports and integrations — every path out of the vault |
| `jobs` | `ask`, `digest`, `resurface`: model output that never reaches `wiki/` |
| `reader` | The read-only, consumer-scoped surface the web viewer is built on |
| `gate` | The pre-write hook's decision, standard library only |

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
