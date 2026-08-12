# Architecture

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

## The application modules

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

## How the knowledge graph is formed

The graph is derived from the vault on every build — it is a projection, never
a stored artifact anyone may edit.

**Nodes** come from two places:

- `raw:<sha256>` — one per registered source in `raw/`. Its `kind` is `raw` and
  its `label` is the original filename. Identity is the content hash, so moving
  a file does not create a new node; `bk reconcile` re-links the path.
- `page:<path>` — one per markdown file under `wiki/`. Its `kind` is the
  frontmatter `type` (`source`, `entity`, `concept`, `synthesis`) and its
  `label` is the frontmatter `title`.

**Edges** are both derived, never declared by hand:

- `sourced_from` — from a page to each hash in its frontmatter `sources`. This
  is what the apply gate's citation check guarantees, so provenance is a
  structural property of the graph rather than a convention.
- `links_to` — from a page to another page, resolved from `[[wiki-links]]` in
  the body by slug. Unresolvable links are rejected at apply time, so the graph
  has no dangling edges.

Model-proposed edges are stored apart from this projection and joined at read
time — see [Enrichment](./enrichment.md).

## Judgment routing

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

Anthropic, OpenAI, OpenRouter and Ollama are interchangeable drivers behind one
job contract. Provider neutrality is a requirement rather than a preference:
`local-only` evidence is routed to Ollama or not at all.
