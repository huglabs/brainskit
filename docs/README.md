# brainskit documentation

The [project README](../README.md) covers what brainskit is, how to install it
and the first ten minutes. Everything below is the detail that would drown it.

## Using a vault

| Document | What it covers |
|---|---|
| [Getting started](./getting-started.md) | `bk init`, the first capture, the apply proposal contract, vault layout |
| [Command reference](./commands.md) | Every command, and what each failure code tells a caller to do |
| [The privacy boundary](./privacy.md) | Consumers, what filtering covers, why a redaction is never described |
| [Filing and review](./filing.md) | `bk ingest`, the proposal queue, freshness and integrity |

## Extending a vault

| Document | What it covers |
|---|---|
| [The code graph](./code-graph.md) | `bk code`, language coverage, what needs the `code` extra |
| [Enrichment](./enrichment.md) | Model-proposed edges, and the three rules that make one admissible |
| [Persistent integrations](./integrations.md) | Obsidian, Neo4j, PostgreSQL, many vaults into one store, egress rules |
| [Serving a vault](./serving.md) | The local web viewer and MCP over stdio or HTTP |
| [Coding agents](./agents.md) | `bk hooks install`, proving the write gate actually guards, what a watch skips |

## Understanding and changing the engine

| Document | What it covers |
|---|---|
| [Architecture](./architecture.md) | Layering, the application modules, judgment routing |
| [Benchmarks](./benchmarks.md) | Code-graph coverage and LOCOMO retrieval, and what those numbers do not establish |
| [Development](./development.md) | Local setup, the delivery gate, releasing to PyPI |

Engineering conventions and the defect classes this codebase has already paid
for are recorded in [`AGENTS.md`](../AGENTS.md). Read it before changing the
apply gate, the privacy filter or an integration lifecycle.
