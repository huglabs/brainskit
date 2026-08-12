## brainskit vault

This project is a brainskit vault at `{{vault}}` — or contains one at that
path, if it is a subdirectory below this file rather than this project's
root. Markdown and JSON are the source of truth; the SQLite FTS5 index is
disposable and rebuilt on demand.

### If the vault is nested below this project

`bk hooks install` can be pointed at a project root with `--root` while the
vault itself lives in a subdirectory, which is the case whenever `{{vault}}`
above differs from where this file sits. `.claude/`, this file and the git
`pre-commit` hook belong to the project (the workspace); `.brain/` and the
graph belong to the vault. The resolved workspace is recorded in
`.brain/agent-claude.json` inside the vault — nothing else on disk remembers
it — and `bk status` reads it from there to confirm the installed hooks are
actually loaded by an agent opened on this project rather than silently
guarding a directory nobody reads.

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
  `wiki.outside_apply`. Both are refused mechanically rather than by
  convention: `bk gate check-write PATH` is the decision, and the installed
  PreToolUse hook asks it before every file write.
- Run `bk reconcile` after moving or deleting files outside the tooling. It
  re-links moved sources by hash and drops freshness entries whose page is gone.
- Drop a source you no longer want with `bk forget ITEM` (add `--force` if the
  raw file is still on disk). That is this vault's own registry — unrelated to
  `bk vaults forget`, which unregisters a whole vault from this machine and
  never touches its files.
- `bk watch` only captures new files outside the patterns in `.brain/config.json`'s
  `ignore` list (version-control metadata, dependency and build directories by
  default). Edit that list rather than fighting the watcher.
- A failed apply writes nothing, and an interrupted one is rolled back when the
  vault is next opened.

### The code graph

`bk code` is a second graph, alongside the vault's own — one describing this
repository's code rather than its evidence. It is never confused with `bk
graph`: that regenerates `graph/graph.json` from the vault; `bk code build`
extracts `graph/code.json` from `code_root` (read from `.brain/config.json`,
discovered upward when unset, always excluding the vault's own directories).

- `bk code build [PATH …]` extracts in-process and stores the graph; scoped to
  `PATH`s, it merges that subset into what is already stored instead of
  replacing it. `bk code import GRAPH.json` takes one an external extractor
  produced instead, through the same boundary.
- `bk code status` says whether the stored graph still describes the tree.
- `bk code affected SYMBOL`, `bk code path FROM TO` and `bk code hubs` are
  brainskit's own traversal and need no extra dependency.
- `bk code communities`, `bk code cycles` and `bk code diff` are delegated to
  the vendored analysis and need the `code` extra's `networkx`; `build` needs
  the extra's tree-sitter grammars instead. A command that needs the extra and
  lacks it fails with the install hint, never a stack trace.

Every code-graph read defaults to `--consumer local`, because it carries
repository paths and is not meant to leave the machine.

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

### Other vaults on this machine

`bk vaults` is unrelated to this vault's own commands: it manages the list of
vaults registered on this machine (`bk vaults register|list|forget`) and syncs
all of them into one shared store as a set (`bk vaults sync --target
postgres|neo4j|obsidian`). Each vault keeps its own policy — one that has not
enabled the target is skipped, not enabled on its behalf — and one vault
failing does not stop the rest. This group is CLI-only, unlike `bk
integration`, and is not exposed over MCP: an MCP server answers under one
vault's declared boundary, and reaching into unrelated vaults would widen it.
