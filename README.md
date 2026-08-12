<div align="center">

<img src="./docs/assets/brainskit-mark.svg" width="84" alt="" />

# brainskit `bk`

**Your agent's memory, with receipts.**

Local-first · LLM-agnostic · nothing reaches the wiki without provenance

[![PyPI](https://img.shields.io/pypi/v/brainskit?style=flat-square&color=ee502c&labelColor=0c0c0c&logo=pypi&logoColor=white)](https://pypi.org/project/brainskit/)
[![Python](https://img.shields.io/pypi/pyversions/brainskit?style=flat-square&color=ee502c&labelColor=0c0c0c&logo=python&logoColor=white)](https://pypi.org/project/brainskit/)
[![License](https://img.shields.io/badge/license-Apache--2.0-ee502c?style=flat-square&labelColor=0c0c0c)](./LICENSE)
[![CI](https://img.shields.io/github/actions/workflow/status/huglabs/brainskit/ci.yml?branch=main&style=flat-square&color=ee502c&labelColor=0c0c0c&logo=githubactions&logoColor=white)](https://github.com/huglabs/brainskit/actions/workflows/ci.yml)
[![by HugLabs](https://img.shields.io/badge/by-HugLabs-ee502c?style=flat-square&labelColor=0c0c0c)](https://huglabs.ai)

[Getting started](./docs/getting-started.md) ·
[Commands](./docs/commands.md) ·
[Privacy](./docs/privacy.md) ·
[Architecture](./docs/architecture.md) ·
[All docs](./docs/README.md)

<sub>An open-source project from <a href="https://huglabs.ai"><b>HugLabs</b></a> — the applied research laboratory for enterprise AI that ships.</sub>

</div>

---

```
██████╗ ██████╗  █████╗ ██╗███╗   ██╗███████╗██╗  ██╗██╗████████╗
██╔══██╗██╔══██╗██╔══██╗██║████╗  ██║██╔════╝██║ ██╔╝██║╚══██╔══╝
██████╔╝██████╔╝███████║██║██╔██╗ ██║███████╗█████╔╝ ██║   ██║
██╔══██╗██╔══██╗██╔══██║██║██║╚██╗██║╚════██║██╔═██╗ ██║   ██║
██████╔╝██║  ██║██║  ██║██║██║ ╚████║███████║██║  ██╗██║   ██║
╚═════╝ ╚═╝  ╚═╝╚═╝  ╚═╝╚═╝╚═╝  ╚═══╝╚══════╝╚═╝  ╚═╝╚═╝   ╚═╝
        by HugLabs • Enterprise AI that ships
        www.huglabs.ai • open source, Apache-2.0
```

## Your agent has been writing your knowledge base for six months

Now open any page in it and answer one question: **where did this come from?**

If the answer is "the model said so", you don't have a knowledge base. You have
model output with a filename — confident, well-formatted, and unfalsifiable.
Every retrieval on top of it inherits that. Every decision downstream of it
inherits that too.

A knowledge base an agent can write to freely stops being evidence.

## So brainskit doesn't let it

Markdown and JSON are the source of truth. SQLite FTS5 is a disposable index.
A model may *propose* what your knowledge base should say — and only the
deterministic `bk apply` gate may write it.

**The refusal is the product:**

```console
$ bk apply proposal.json
bk: Apply proposal rejected; no files were written
  failures: path: wiki/concepts/context-rot.md, code: citation_mismatch,
            missing_citations: (none),
            undeclared_citations: 0000000000000000000000000000000000000000000000000000000000000000
$ echo $?
2
```

One citation that doesn't resolve, and the **whole batch** is rejected. Not
partially written. Not written with a warning. Exit code 2 tells the caller to
fix the proposal and try again — and nothing on disk moved.

That is the entire pitch. Everything below is how it holds.

## Six invariants, enforced mechanically

Not conventions. Not linting advice. Not a style guide someone will stop
following in March.

| Invariant | How it is enforced |
|---|---|
| **Raw evidence is immutable** | Captures are identified by SHA-256 of their bytes; `bk lint` compares current bytes against the registered hash, and `bk reconcile` heals a move without rewriting identity. |
| **Only the gate writes the wiki** | `bk apply` validates schema, citations, links and novelty for the whole batch before a single page is replaced; direct wiki edits are reported by `lint`. |
| **Every page carries provenance** | A page declares the source hashes it was derived from, each must be cited in the body, and each must resolve inside the vault before the write is eligible. |
| **Privacy is a declared boundary** | Machine callers must name a consumer (`local`, `cloud`, `human`); the filter runs after graph expansion, so an edge cannot reintroduce restricted evidence. |
| **Mechanical stays LLM-free** | Capture, index, search, apply, export and the structural lint never call a model. Judgment flows are separate, schema-bound, and routed by the strictest policy in the evidence set. |
| **A write is one unit of work** | Wiki pages, freshness, registry status, the raw-file move and the FTS5 update become visible together or are restored from the transaction journal. |

## It runs on your laptop and asks for nothing

No account system. No credentials. No network for any of the mechanical work.
One dependency in the core.

Anthropic, OpenAI, OpenRouter and Ollama are interchangeable drivers behind one
job contract — and evidence you marked `local-only` goes to Ollama or it goes
nowhere.

## Install

`bk` operates on a vault directory, not on the project it is invoked from, so
install it as an isolated tool. It lands on `PATH` for every vault without
becoming a dependency of any project.

```bash
uv tool install brainskit     # or: pipx install brainskit
bk --help
```

Four extras are optional, because the core keeps a single dependency and none of
these capabilities is one every vault wants:

```bash
uv tool install 'brainskit[integrations]'  # Neo4j and PostgreSQL drivers
uv tool install 'brainskit[code]'          # bk code: tree-sitter grammars, ~70 MB
uv tool install 'brainskit[code-all]'      # every language the extractors can drive
uv tool install 'brainskit[convert]'       # capture .docx/.pdf/.pptx via markitdown
```

`code` is the larger commitment: vendoring the analysis source did not vendor a
parser, so the grammars stay compiled wheels and remain a real dependency. A
`bk code` command without it fails with the install hint rather than a stack
trace, and the code-graph tests skip rather than fail — so a vault that never
reads a repository never pays for one. Without `convert`, a non-text capture is
stored verbatim with a "no converter available" note rather than being refused.

Install the working tree, a git ref, or a built wheel with the same command:

```bash
uv tool install /path/to/brainskit
uv tool install 'brainskit @ git+https://github.com/huglabs/brainskit@v0.5.0'
```

To pin `bk` to one project instead of the machine, declare it as a dependency
and run it through the project environment:

```bash
uv add brainskit
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

`bk status` is the vault in one screen — counts, branches, freshness, whether
the derived projections still match the pages they were built from, and whether
the enforcement layers are actually running:

```console
$ bk --vault ./my-vault status
✓ vault healthy

vault       /home/you/my-vault
sources     45
pending     4
wiki pages  36
indexed     81 documents (updated 2026-08-11T04:06:07.222244+00:00)

————————————————————————— wiki freshness —————————————————————————
fresh    19
review   17
stale    0
unknown  0

—————————————————————————— enforcement ———————————————————————————
layer           status
write_gate      ✓ active
session_status  ✓ active
commit_lint     ✓ active
instructions    ✓ active
```

## Your agent can't route around it either

`bk hooks install` wires the gate into your coding agent as a PreToolUse hook,
so a write under `wiki/` or `raw/` is refused at the moment it is attempted —
not reviewed later, not caught in a lint run someone skips.

And `bk doctor` doesn't take the hook's word for it. It *runs* the thing: one
path the gate must refuse, one it must allow. A hook that fails open because
`bk` fell off `PATH` reports as `not_enforcing` and repeats the hook's own
explanation, instead of a green check that guards nothing.

A status check that verifies a file exists is not the same as one that verifies
a file runs.

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
that would need a missing connector fails instead of pretending. If it isn't
built, it says so and exits non-zero.

## Contributing

Issues and pull requests are welcome — see [CONTRIBUTING.md](./CONTRIBUTING.md)
for the gate every change has to pass, and [SECURITY.md](./SECURITY.md) for
reporting a vulnerability privately.

## License

Apache-2.0. See [LICENSE](./LICENSE), and [NOTICE](./NOTICE) for the vendored
code-analysis subset and its attribution.

---

<div align="center">

<a href="https://huglabs.ai">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="./docs/assets/huglabs-dark.png" />
    <img src="./docs/assets/huglabs-light.png" width="168" alt="HugLabs" />
  </picture>
</a>

### Built and maintained by HugLabs

**The applied research laboratory for enterprise AI that ships.**

A Brazilian AI research lab and venture studio that transforms cutting-edge
science into real systems for critical business problems — six product
families, eleven products in production, and an academic partnership with
CEIA-UFG.

We don't sell capabilities. We sell delivery. brainskit is the memory layer
underneath that work, released as open source because a provenance gate is only
worth trusting if you can read it.

[huglabs.ai](https://huglabs.ai) ·
[github.com/huglabs](https://github.com/huglabs) ·
*Academic rigor, startup deadlines.*

</div>
