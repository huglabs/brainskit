## brainkit vault

This project is a brainkit vault at `{{vault}}`. Markdown and JSON are the
source of truth; the SQLite FTS5 index is disposable and rebuilt on demand.

### How the graph is formed

The graph is derived from the vault on every build — it is a projection, never
a stored artifact you may edit.

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

`bk graph` writes `graph/graph.json`. `bk views` regenerates `views/`.

### Privacy is applied after expansion, not before

Filtering runs on the finished graph, once every node and edge exists. Filtering
the direct hits first would let an outgoing link or a backlink pull a restricted
node back in through its neighbour.

A redacted source contributes nothing — not its body, not its filename, not its
branch. Treat a filename and a branch name as disclosure in their own right.

Every read that crosses a boundary declares a consumer: `cloud` sees only
cloud-eligible branches, `local` sees everything except `never-ingest`, and
`human` applies no restriction at all. `human` is the interactive default, so
name it explicitly only when the operator asked for unrestricted evidence.

### How to operate on this vault

- Read evidence with `bk context "QUERY" --consumer local --json`. It returns
  the source hashes and the proposal contract you need.
- Write wiki pages **only** through `bk apply`. Every claim carries a
  `[^source:<sha256>]` citation, updates carry the `base_hash` returned by
  `context`, and retries carry a stable `proposal_id`.
- Never edit anything under `raw/` — sources are immutable and identified by
  their hash. Never hand-edit anything under `wiki/`; `bk lint` reports it as
  `wiki.outside_apply`.
- Run `bk reconcile` after moving or deleting files outside the tooling. It
  re-links moved sources by hash and drops freshness entries whose page is gone.
- A failed apply writes nothing, and an interrupted one is rolled back when the
  vault is next opened.

### Exporting the graph

File targets default to `--consumer local`, so an export never emits
`never-ingest` evidence unless `human` is named deliberately:

```bash
bk --vault {{vault}} export --target json      # also graphml, cypher, kuzu, llms-txt
```

Persistent integrations carry their own configured consumer, and passing
`--consumer` to them is rejected rather than silently applied:

- **Obsidian** — manifest-based sync of `wiki/`, `views/` and the graph. It
  deletes only paths it previously wrote, so human-owned notes survive. Consumer
  is optional and defaults to `local`.
- **Neo4j** — a real Bolt transaction writing `BrainkitNode` nodes with
  `SOURCED_FROM` and `LINKS_TO` relationships. Every identity is namespaced with
  a per-vault id, so a refresh replaces only this vault's subgraph. Consumer is
  mandatory.
- **PostgreSQL** — portable `nodes`/`edges` tables with JSONB properties,
  foreign keys and a recursive `graph_walk(start_node, max_depth)` function. No
  graph extension required. Consumer is mandatory.
