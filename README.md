<div align="center">

# brainkit `bk`

**A local-first, LLM-agnostic second-brain engine —
the compilation gate between raw evidence and knowledge an agent may act on.**

[![PyPI](https://img.shields.io/pypi/v/brainkit?style=flat-square)](https://pypi.org/project/brainkit/)
[![Python](https://img.shields.io/pypi/pyversions/brainkit?style=flat-square)](https://pypi.org/project/brainkit/)
[![License](https://img.shields.io/badge/license-Apache--2.0-blue?style=flat-square)](./LICENSE)
[![CI](https://img.shields.io/github/actions/workflow/status/huglabs/brainskit/ci.yml?branch=main&style=flat-square)](https://github.com/huglabs/brainskit/actions/workflows/ci.yml)

</div>

---

A knowledge base an agent writes to freely stops being evidence and becomes
model output with a filename.

brainkit takes the opposite position and enforces it mechanically. Markdown and
JSON are the source of truth; SQLite FTS5 is a disposable search index. A model
may *propose* what your knowledge base should say, but only the deterministic
`bk apply` gate may *write* it — and only if every claim cites a source that
resolves inside the vault.

| Invariant | How it is enforced |
|---|---|
| **Raw evidence is immutable** | Captures are identified by SHA-256 of their bytes; `bk lint` compares current bytes against the registered hash, and `bk reconcile` heals a move without rewriting identity. |
| **Only the gate writes the wiki** | `bk apply` validates schema, citations, links and novelty for the whole batch before a single page is replaced; direct wiki edits are reported by `lint`. |
| **Every claim carries provenance** | A page body cites the source hashes it was derived from, and those sources must resolve inside the vault before the write is eligible. |
| **Privacy is a declared boundary** | Machine callers must name a consumer (`local`, `cloud`, `human`); the filter runs after graph expansion, so an edge cannot reintroduce restricted evidence. |
| **Mechanical stays LLM-free** | Capture, index, search, apply, export and the structural lint never call a model. Judgment flows are separate, schema-bound, and routed by the strictest policy in the evidence set. |
| **A write is one unit of work** | Wiki pages, freshness, registry status, the raw-file move and the FTS5 update become visible together or are restored from the transaction journal. |

It runs on a workstation, owns no account system, holds no credentials, and
needs no network to do its mechanical work. Anthropic, OpenAI, OpenRouter and
Ollama are interchangeable drivers behind one job contract, and `local-only`
evidence is routed to Ollama or not at all.

## Install

`bk` operates on a vault directory, not on the project it is invoked from, so
install it as an isolated tool. It lands on `PATH` for every vault without
becoming a dependency of any project.

```bash
uv tool install brainkit     # or: pipx install brainkit
bk --help
```

Four extras are optional, because the core keeps a single dependency and none of
these capabilities is one every vault wants:

```bash
uv tool install 'brainkit[integrations]'  # Neo4j and PostgreSQL drivers
uv tool install 'brainkit[code]'          # bk code: tree-sitter grammars, ~70 MB
uv tool install 'brainkit[code-all]'      # every language the extractors can drive
uv tool install 'brainkit[convert]'       # capture .docx/.pdf/.pptx via markitdown
```

`code` is the larger commitment: vendoring the analysis source did not vendor a
parser, so the grammars stay compiled wheels and remain a real dependency. A
`bk code` command without it fails with the install hint rather than a stack
trace, and the code-graph tests skip rather than fail — so a vault that never
reads a repository never pays for one. Without `convert`, a non-text capture is
stored verbatim with a "no converter available" note rather than being refused.

Install the working tree, a git ref, or a built wheel with the same command:

```bash
uv tool install /path/to/brainkit
uv tool install 'brainkit @ git+https://github.com/huglabs/brainskit@v0.4.0'
```

To pin `bk` to one project instead of the machine, declare it as a dependency
and run it through the project environment:

```bash
uv add brainkit
uv run bk --vault ./my-vault status
```

No `.env` file is loaded. Provider secrets are read only from the environment
variable explicitly named in the vault configuration.

## Quick start

```bash
bk init ./my-vault
```

`init` probes the machine before it asks anything — whether this is a git
repository, what `$LANG` implies, whether ollama is running and which models are
actually pulled — then asks only what it cannot work out: what the vault is for,
which model runs the six judgment jobs, and whether to wire up Obsidian, the
local web UI and your coding agent. If ollama is down, it says so and still
produces a valid vault; the jobs stay idle until a provider is up.

```bash
bk --vault ./my-vault capture notes.md --json
bk --vault ./my-vault search "retrieval memory" --consumer local --json
bk --vault ./my-vault context "retrieval memory" --consumer local --json
bk --vault ./my-vault apply proposal.json --json
bk --vault ./my-vault status
```

`context` returns the bounded evidence bundle an agent needs to build an apply
proposal; `apply` validates the complete batch before any page is replaced. The
full contract is in [Getting started](./docs/getting-started.md).

## Documentation

| | |
|---|---|
| [Getting started](./docs/getting-started.md) | First vault, the apply proposal contract, vault layout |
| [Command reference](./docs/commands.md) | Every command, and what each failure code tells a caller to do |
| [The privacy boundary](./docs/privacy.md) | Consumers, and why a redaction is never described |
| [Filing and review](./docs/filing.md) | `bk ingest`, the proposal queue, freshness and integrity |
| [The code graph](./docs/code-graph.md) | `bk code`: the repository a vault documents, as structure |
| [Enrichment](./docs/enrichment.md) | Model-proposed edges, and the rules that make one admissible |
| [Persistent integrations](./docs/integrations.md) | Obsidian, Neo4j, PostgreSQL, many vaults into one store |
| [Serving a vault](./docs/serving.md) | The local web viewer, and MCP over stdio or HTTP |
| [Coding agents](./docs/agents.md) | `bk hooks install`, and proving the write gate actually guards |
| [Architecture](./docs/architecture.md) | Layering, application modules, judgment routing |
| [Benchmarks](./docs/benchmarks.md) | Code-graph coverage and LOCOMO retrieval |
| [Development](./docs/development.md) | Local setup, the delivery gate, releasing |

## What the engine implements

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
- a dependency-free web viewer with search, graph navigation, health, freshness,
  branch counts, review queue and source/page inspection.

## The privacy boundary in one paragraph

Machine callers must declare a consumer. `cloud` receives only cloud-eligible
evidence, `local` excludes `never-ingest`, and `human` applies no restriction —
it is the interactive default, and a machine caller gets it only by naming it.
Filtering covers bodies and metadata alike, because a filename and its branch
are themselves disclosure, and it runs *after* graph expansion so a neighbour
cannot pull restricted evidence back into view. `search` and `context` report
`redacted` as a count and never describe what was withheld. Details:
[the privacy boundary](./docs/privacy.md).

## Status

The engine contract is suitable for local dogfooding. The end-to-end daily habit
still needs the external delivery layer:

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

## Contributing

Issues and pull requests are welcome — see [CONTRIBUTING.md](./CONTRIBUTING.md)
for the gate every change has to pass, and [SECURITY.md](./SECURITY.md) for
reporting a vulnerability privately.

## License

Apache-2.0. See [LICENSE](./LICENSE), and [NOTICE](./NOTICE) for the vendored
code-analysis subset and its attribution.

Built and maintained by [HugLabs](https://hugyourcustomer.ai).
