# Repo Scout Report — knowledge vault / agent memory space vs. brainkit

Date: 2026-08-14. Method: GitHub API metadata + README/docs fetches via WebFetch/WebSearch only (no clones).
Every claimed technique carries its source URL. Impressions without a fetched source are in **Unverified** at the end.

brainkit invariants to preserve (from the brief): provenance-mandatory wiki (claims cite source hashes),
consumer-scoped privacy (cloud/local/human), local-first, mechanical enforcement (hooks/gates),
SQLite FTS5 keyword index that is disposable and rebuildable. Measured bottleneck: per-item LLM novelty
checks via local ollama, ~14 items/min.

---

## 1. Summary table

| Repo | Stars | License | Lang | Last push | One-liner |
|---|---|---|---|---|---|
| mem0ai/mem0 | 63,277 | Apache-2.0 | Python | 2026-08-14 | Universal memory layer for AI agents |
| getzep/graphiti | 29,932 | Apache-2.0 | Python | 2026-08-13 | Real-time temporal knowledge graphs for agents |
| letta-ai/letta | 24,246 | Apache-2.0 | Python | 2026-08-14 | Stateful agents with self-editing hierarchical memory |
| topoteretes/cognee | 30,029 | Apache-2.0 | Python | 2026-08-14 | AI memory platform: graph+vector "cognify" pipeline |
| HKUDS/LightRAG | 38,868 | MIT | Python | 2026-08-15 | Simple/fast graph RAG (EMNLP 2025) with WebUI |
| gusye1234/nano-graphrag | 3,967 | MIT | Python | 2026-01-27 | ~1,100-line hackable GraphRAG |
| khoj-ai/khoj | 36,494 | **AGPL-3.0** | Python | 2026-08-02 | Self-hostable AI second brain over your docs |
| neuml/txtai | 12,890 | Apache-2.0 | Python | 2026-08-12 | Embeddings database: vector+sparse+graph+RDBMS union |
| SciPhi-AI/R2R | 7,961 | MIT | Python | **2025-11-07 (stalled)** | Production agentic RAG with RESTful API |
| microsoft/graphrag | 35,500 | MIT | Python | 2026-08-14 | LLM-derived knowledge-graph RAG (**maintenance mode**) |
| **discovered** basicmachines-co/basic-memory | 3,662 | **AGPL-3.0** | Python | 2026-08-14 | Markdown-source-of-truth knowledge base over MCP |
| **discovered** supermemoryai/supermemory | 28,915 | MIT | TypeScript | 2026-08-15 | Fast memory/context engine, local-capable, MCP |
| **discovered** asg017/sqlite-vec | 8,012 | Apache-2.0 | C | 2026-05-18 | Vector search SQLite extension (infra, not a vault) |
| **discovered** doobidoo/mcp-memory-service | ~609 (unverified) | ? | Python | ? | MCP memory w/ sqlite-vec + ONNX — **repo 404s today, see Unverified** |

Sources: `https://api.github.com/repos/<owner>/<repo>` for each row (fetched 2026-08-14/15 UTC).

---

## 2. Per-repo findings

### mem0ai/mem0 — Apache-2.0
**What it does:** universal memory layer for agents (extract facts from conversations, store, retrieve).

**Steal:**
1. **They abandoned per-item LLM update decisions.** The April 2026 algorithm rewrite is
   "Single-pass ADD-only extraction — one LLM call, no UPDATE/DELETE" and "Memories accumulate;
   nothing is overwritten" (README, https://raw.githubusercontent.com/mem0ai/mem0/main/README.md).
   Conflict resolution moved from write time to read time.
2. **Multi-signal retrieval fusion:** "semantic, BM25 keyword, and entity matching scored in parallel
   and fused", plus "Entity linking — entities are extracted, embedded, and linked across memories for
   retrieval boosting" and time-aware ranking (same README). Benchmarks they claim: 92.5 LoCoMo,
   94.4 LongMemEval, latency 0.88–1.09 s.
3. Their docs confirm the additive pipeline can duplicate ("When you switch to `infer=False`, Mem0
   stores your payload exactly as provided, so duplicates can land",
   https://docs.mem0.ai/core-concepts/memory-operations) — i.e., dedup is no longer an LLM write-path
   guarantee even for the market leader.

**Don't copy:** LLM-extracted "facts" divorced from their source — no per-claim citation, which breaks
brainkit's provenance-mandatory wiki. Cloud-platform tiering of features (dashboard).

### getzep/graphiti — Apache-2.0
**What it does:** real-time, temporally-aware knowledge graphs built incrementally from "episodes".

**Steal:**
1. **No LLM at query time:** "Combines semantic embeddings, keyword (BM25), and graph traversal for
   low-latency, high-precision queries without reliance on LLM summarization"; plus "Reranking search
   results using graph distance" (README, https://raw.githubusercontent.com/getzep/graphiti/main/README.md).
   Graph-distance reranking is directly implementable on brainkit's existing provenance/link graph.
2. **Incremental, never batch:** "New data integrates immediately without batch recomputation."
3. **Bi-temporal invalidation:** "Facts have validity windows. When information changes, old facts are
   invalidated — not deleted. Query what's true now, or what was true at any point in time." This maps
   cleanly onto brainkit's freshness ledger (fresh/review/stale → add `superseded_by`/`invalid_from`).

**Don't copy:** hard server dependency — Neo4j/FalkorDB/Neptune backends only (Kuzu deprecated per README).
Violates local-first. Entity resolution appears LLM-driven (not detailed in README — see Unverified).

### letta-ai/letta — Apache-2.0
**What it does:** platform for stateful agents (MemGPT lineage): in-context "core memory blocks" +
out-of-context archival memory, agent edits its own memory.

**Steal:**
1. **Context-window transparency as a UI pattern:** the ADE shows "all components of its context window
   (memory, state, and prompts)" and archival memory is "your agent's external (out-of-context) memory
   store" you can inspect and search (https://docs.letta.com/memory, ADE section). For brainkit: a
   "what does consumer X actually see for query Q" inspector — a privacy-boundary debugger.
2. Memory hierarchy vocabulary (core/archival/recall) is a clean model for MCP context budgeting.

**Don't copy:** the agent self-editing its own memory without any gate is the exact opposite of
brainkit's apply-gate discipline. README is now marketing-thin; architecture details live in docs.

### topoteretes/cognee — Apache-2.0
**What it does:** "memory platform for agents": add → cognify (build graph+embeddings) → recall, with
Remember/Recall/Forget/Improve operations.

**Steal:**
1. **Fully-embedded local stack:** "Local development stays fully embedded — SQLite, LanceDB, and
   Kuzudb — with no extra services to stand up" (README,
   https://raw.githubusercontent.com/topoteretes/cognee/main/README.md). Validates SQLite + an embedded
   vector file store (LanceDB) as a production-credible local-first pattern — the main alternative to
   sqlite-vec if the extension's pace worries you.
2. **Auto-routing recall:** "picks best search strategy automatically" across vector/graph — a cheap
   heuristic router (keywordish query → FTS5, conceptual → vector, entity → graph walk) beats exposing
   the choice to callers.

**Don't copy:** background "improve" that mutates memory autonomously (conflicts with apply-gate);
MCP server "runs inside a Docker container" (README) — brainkit's MCP should stay a plain process.

### HKUDS/LightRAG — MIT
**What it does:** simple/fast graph RAG with entity/relation extraction, dual-level retrieval, WebUI.

**Steal:**
1. **LLM cache as an indexing asset:** on deletes/updates "the system can use the LLM cache created
   during indexing to quickly rebuild the affected entities and relationships" (README,
   https://raw.githubusercontent.com/HKUDS/LightRAG/main/README.md). Cache LLM verdicts keyed by
   content hash → re-runs, reconciles and rebuilds become free. Directly applicable to brainkit's
   ollama novelty checks.
2. **The WebUI triad:** "insert, query, and visualize LightRAG knowledge through an intuitive web-based
   dashboard" — graph exploration + document management + pipeline status monitoring. The best
   reference model for growing brainkit's minimal web interface.
3. **Four-layer storage abstraction** (KV / vector / graph / doc-status) with file-persisted defaults —
   the doc-status store (per-document pipeline state) is a nice explicit concept brainkit half-has in
   its registry.

**Don't copy:** entity descriptions synthesized by LLM with no per-claim source binding; heuristic
entity-merge thresholds that silently stop writing ("new file names are no longer written to the vector
storage" past a cap) — silent data dropping would violate brainkit's honesty discipline.

### gusye1234/nano-graphrag — MIT
**What it does:** ~1,100-line hackable GraphRAG.

**Steal:**
1. **Content-hash chunk identity:** "md5-hash of the content as the key, so there is no duplicated
   chunk" (readme, https://raw.githubusercontent.com/gusye1234/nano-graphrag/main/readme.md) —
   brainkit already does this better (SHA-256), which confirms exact-dup is solved; the gap is *near*-dup.
2. **Everything swappable via constructor** (LLM, embedder, chunker, all three stores: nano-vectordb /
   networkx / JSON-KV) — same port-style seams brainkit already prefers.

**Don't copy:** communities/reports regenerate on every insert (their own README admits it) — that is
the batch-recompute trap Graphiti avoids. Activity slowed (last push 2026-01).

### khoj-ai/khoj — AGPL-3.0
**What it does:** self-hostable AI second brain: chat over your docs + web, agents, automations.

**Steal (patterns only — AGPL, do not copy code):**
1. UI patterns: "Create agents with custom knowledge, persona, chat model and tools" and "Automate away
   repetitive research. Get personal newsletters and smart notifications"
   (https://raw.githubusercontent.com/khoj-ai/khoj/master/README.md) — a scheduled "what changed in the
   vault this week" digest is a very cheap, high-perceived-value feature on top of brainkit's freshness ledger.
2. Meet-users-where-they-are clients (Obsidian plugin, Emacs, phone) — brainkit already exports to
   Obsidian; a chat surface inside Obsidian is the analogous move.

**Don't copy:** AGPL code (viral); server-centric Django+Postgres architecture.

### neuml/txtai — Apache-2.0
**What it does:** all-in-one embeddings database: "union of vector indexes (sparse and dense), graph
networks and relational databases" (README, https://raw.githubusercontent.com/neuml/txtai/master/README.md).

**Steal:**
1. **SQL over a hybrid index:** "Vector search with SQL, object storage, topic modeling, graph analysis
   and multimodal indexing" — the pattern of keeping content+metadata relational (SQLite) while ANN
   lives beside it, queryable in one SQL surface, is exactly the shape brainkit's disposable index wants.
2. **Incremental upsert/delete** is first-class ("upsert and delete calls",
   https://neuml.github.io/txtai/embeddings/), and "Vectors are stored with the option to also store
   content" — index remains derived/disposable, content remains the source of truth. Same doctrine as brainkit.
3. RAG **citation support** exists in their pipeline docs (README references "how to create citations").

**Don't copy:** it is a framework, not a vault — no provenance gate, no privacy model; adopting it
wholesale would replace brainkit's storage doctrine rather than extend it.

### SciPhi-AI/R2R — MIT
**What it does:** production agentic RAG server with RESTful API, hybrid search, knowledge graph.

**Steal:** its API surface taxonomy (documents/chunks/graphs/search/agent endpoints) is a decent checklist
for brainkit's web/MCP surface completeness. Nothing else clears the bar.

**Don't copy / caution:** last push 2025-11-07 — nine months stale; treat as an architecture museum,
not a dependency.

### microsoft/graphrag — MIT
**What it does:** "a data pipeline and transformation suite… to extract meaningful, structured data
from unstructured text using the power of LLMs" (README,
https://raw.githubusercontent.com/microsoft/graphrag/main/README.md).

**Steal:** nothing operational.
**Don't copy:** its whole cost profile — "GraphRAG indexing can be an expensive operation… start small";
project is "largely in maintenance mode" per its own README. It is the cautionary tale for LLM-heavy
indexing, i.e., the direction brainkit should be moving *away* from.

### basicmachines-co/basic-memory (discovered) — AGPL-3.0
**What it does:** brainkit's closest philosophical cousin: "Plain text on your disk. Forever." —
markdown is the source of truth, SQLite (or Postgres) index derived from it, knowledge graph parsed
from wikilinks, all exposed over MCP (README,
https://raw.githubusercontent.com/basicmachines-co/basic-memory/main/README.md).

**Steal (patterns only — AGPL):**
1. **Typed relations in plain markdown:** observations as `- [category] Statement #tag (context)` and
   relations as `- relation_type [[Entity]]` (bare links default to `links_to`). brainkit's wiki-links
   are untyped; a typed-relation convention would enrich the derived graph at zero schema cost.
2. **MCP tool surface:** `write_note, read_note, edit_note, move_note, delete_note, search_notes,
   build_context, schema_infer, schema_validate` — `build_context` (graph-walk assembly of context for
   a topic) is the tool brainkit's `context` command already is; `schema_infer`/`schema_validate` are
   interesting additions.
3. Obsidian story: "No setup. Point Obsidian at ~/basic-memory" — validates brainkit's folder-first sync.

**Don't copy:** AGPL code; and its writes are *ungated* — any MCP client edits notes directly. Brainkit's
apply gate + provenance is precisely the differentiator against this repo. Also note it upsells a cloud
($15/mo) — the OSS/AGPL+cloud split is their moat, not a technique.

### supermemoryai/supermemory (discovered) — MIT
**What it does:** "Memory and context engine + app that is extremely fast, scalable, and can be run
fully locally" (README, https://raw.githubusercontent.com/supermemoryai/supermemory/main/README.md).

**Steal:**
1. **Local embeddings default:** `Xenova/bge-base-en-v1.5` (ONNX/transformers.js family) — evidence that
   a small local embedding model is production-acceptable for memory workloads; the Python analog
   (bge-base / all-MiniLM via sentence-transformers, or ollama's embed endpoint) is the right class of
   model for brainkit's novelty gate.
2. **Consolidated MCP surface — three tools:** `memory` (save/forget), `recall` (search), `context`
   (inject profiles). Matches the consolidation lesson from gitlab-mcp: few tools with modes beat many
   thin ones. brainkit's MCP should stay in this shape as it grows.
3. Claimed "95% Recall@15 with a 99.4% context reduction" and "~50ms user profiles" — treat numbers as
   marketing, but the *feature* (precomputed per-consumer context profiles, cheap to inject) is a good
   idea for brainkit's consumer-scoped contexts.

**Don't copy:** cloud-tilted product surface; benchmark claims unaudited.

### asg017/sqlite-vec (discovered, infra) — Apache-2.0
8,012 stars, C, last push 2026-05-18, 202 open issues, not archived
(https://api.github.com/repos/asg017/sqlite-vec). See §4.

---

## 3. Special attention A — replacing per-item LLM novelty with embeddings

The field's verdict is unusually consistent: **nobody serious keeps an LLM in the per-item write path.**

- **mem0** (the highest-starred memory layer) explicitly *removed* LLM UPDATE/DELETE decisions in its
  2026 rewrite: "Single-pass ADD-only extraction — one LLM call, no UPDATE/DELETE"; duplicates are
  tolerated at write time and handled by fused retrieval ranking at read time
  (https://raw.githubusercontent.com/mem0ai/mem0/main/README.md;
  https://docs.mem0.ai/core-concepts/memory-operations).
- **nano-graphrag** dedups purely by content hash: "md5-hash of the content as the key, so there is no
  duplicated chunk" (https://raw.githubusercontent.com/gusye1234/nano-graphrag/main/readme.md).
- **Graphiti** keeps LLMs out of the read path entirely ("without reliance on LLM summarization") and
  resolves conflicts structurally via temporal invalidation, not per-item LLM judgment
  (https://raw.githubusercontent.com/getzep/graphiti/main/README.md).
- **LightRAG** amortizes whatever LLM work it does through an indexing-time cache reused on
  update/delete (https://raw.githubusercontent.com/HKUDS/LightRAG/main/README.md).
- **txtai** treats novelty as an index concern: incremental `upsert` + similarity query, no LLM
  (https://neuml.github.io/txtai/embeddings/).

**Concrete design for brainkit** (composed from the above, not copied from any one repo):

1. Keep tier 1 as-is: SHA-256 exact identity (already stronger than nano-graphrag's md5).
2. Tier 2: embed each candidate (local model — bge-base / all-MiniLM class, per supermemory's default;
   ollama's `/api/embed` also batches) and ANN-query the existing corpus.
   - similarity ≥ T_dup (e.g. ~0.92) → auto-reject as duplicate, cite the existing hash;
   - similarity ≤ T_novel (e.g. ~0.75) → auto-accept as novel;
   - only the ambiguous band goes to the ollama LLM check.
3. Cache every LLM verdict keyed by (model, prompt-version, content-hash) — LightRAG's pattern — so
   re-runs/reconciles are free.
4. Thresholds are corpus-dependent: calibrate once against a labeled sample of past accept/reject
   decisions (brainkit has ~14k of them from the openharness run to calibrate on).

Expected effect: embedding throughput is hundreds of items/sec on CPU vs the measured ~14/min; if even
70% of items fall outside the ambiguous band, wall-clock drops by ~3× immediately and scales with the
band's narrowness. (Projection, not a benchmark.)

---

## 4. Special attention B — sqlite-vec beside FTS5, no server

- **The extension itself:** asg017/sqlite-vec — 8,012★, Apache-2.0, C, "A vector search SQLite
  extension that runs anywhere!", not archived, last push 2026-05-18 with 202 open issues
  (https://api.github.com/repos/asg017/sqlite-vec). Honest caveat: pre-v1, single-maintainer cadence,
  and three months quiet at fetch time. Mitigation: brainkit's index is *disposable by doctrine* — if
  the extension dies, drop the table and rebuild elsewhere; nothing durable is lost.
- **The canonical hybrid pattern:** FTS5 (BM25) and a `vec0` virtual table side by side, fused with
  Reciprocal Rank Fusion in pure SQL: "RRF doesn't even attempt to compare them — it uses them purely
  for `row_number()` ranking within each set and combines the results based on that", two CTEs +
  FULL OUTER JOIN, weights `weight_fts`/`weight_vec`, score `1.0/(k+rank)`
  (https://simonwillison.net/2024/Oct/4/hybrid-full-text-search-and-vector-search-with-sqlite/).
  Worked examples: https://deepwiki.com/asg017/sqlite-vec/6.2-hybrid-search-(nbc-headlines) and
  https://github.com/asg017/sqlite-vec/issues/48.
- **Who runs it:** the MCP-memory ecosystem is the production habitat —
  AerionDyseti/vector-memory-mcp ("local embeddings and SQLite with sqlite-vec … all in a single
  file", https://github.com/AerionDyseti/vector-memory-mcp/), cornebidouil's vector-memory MCP
  (https://lobehub.com/mcp/cornebidouil-vector-memory-mcp), doobidoo/mcp-memory-service (see
  Unverified), and the vstash local-first hybrid-retrieval system ("sqlite-vec for approximate nearest
  neighbor search and FTS5 for keyword matching … a single SQLite file",
  https://arxiv.org/pdf/2604.15484).
- **The embedded alternative:** cognee's local mode uses **LanceDB** next to SQLite instead ("Local
  development stays fully embedded — SQLite, LanceDB, and Kuzudb",
  https://raw.githubusercontent.com/topoteretes/cognee/main/README.md). LanceDB is file-based,
  serverless, more actively developed — the fallback if sqlite-vec's cadence is disqualifying. Cost:
  a second storage file + Python dependency instead of one SQLite file.

Fit for brainkit: a `vec0` table in the *same* SQLite file as the FTS5 index keeps the "one disposable
index file" doctrine intact, powers both hybrid search and the §3 novelty gate from a single embedding
write, and adds zero servers.

---

## 5. UI / surface patterns worth taking

1. **LightRAG WebUI** — graph visualization + document management + pipeline status in one dashboard
   (https://raw.githubusercontent.com/HKUDS/LightRAG/main/README.md). The template for evolving
   brainkit's minimal web UI: vault graph explorer, source/registry browser, apply/lint job status.
2. **Letta ADE context inspector** — see "all components of its context window (memory, state, and
   prompts)" (https://docs.letta.com/memory). Brainkit analog: a *consumer view debugger* — "show me
   exactly what `cloud` sees for this query" — which doubles as a privacy-boundary test surface.
3. **basic-memory `build_context` + typed relations in markdown**
   (https://raw.githubusercontent.com/basicmachines-co/basic-memory/main/README.md) — typed wikilinks
   (`- relation_type [[Entity]]`) enrich the derived graph with zero new files; pattern only (AGPL).
4. **supermemory's 3-tool MCP** (`memory`/`recall`/`context`) — keep brainkit's MCP consolidated as it
   grows (https://raw.githubusercontent.com/supermemoryai/supermemory/main/README.md).
5. **khoj automations** — scheduled digests/newsletters over your own knowledge
   (https://raw.githubusercontent.com/khoj-ai/khoj/master/README.md). Brainkit analog: a weekly
   "freshness digest" (what went stale, what got captured, what needs review) — trivially derivable
   from the freshness ledger.
6. **Graphiti bi-temporal ledger** — "old facts are invalidated — not deleted"
   (https://raw.githubusercontent.com/getzep/graphiti/main/README.md): add `superseded_by`/validity
   windows to the freshness ledger so staleness becomes queryable history, not just a badge.

---

## 6. What explicitly NOT to copy (cross-cutting)

- **LLM-synthesized facts without per-claim source binding** (mem0, LightRAG, graphrag) — breaks
  provenance-mandatory wiki. Brainkit's citation-per-claim is the differentiator; keep it.
- **Ungated agent self-editing of memory** (letta, basic-memory, cognee "improve") — the apply gate is
  the moat; every repo above lets the agent write directly.
- **Server-required backends** (graphiti's Neo4j/FalkorDB, khoj's Django/Postgres, R2R) — violates
  local-first. Embedded-only (sqlite-vec or LanceDB).
- **Full-corpus LLM indexing** (microsoft/graphrag — "expensive operation… start small", now
  maintenance mode) — the anti-pattern brainkit's bottleneck already demonstrates in miniature.
- **AGPL code** (khoj, basic-memory) — patterns yes, code never.
- **Silent capacity-based data dropping** (LightRAG's entity-cap behavior) — anything dropped must be
  reported, per brainkit's honesty discipline.

---

## 7. Ranked recommendations (effort → value)

1. **Embedding-gated novelty with a two-threshold band + verdict cache** (§3). Effort: low-medium
   (one embedding model dependency + one ANN query + cache table). Value: directly attacks the
   measured ~14 items/min bottleneck; the field's consensus design. Precedents: mem0 ADD-only pivot,
   nano-graphrag hash dedup, LightRAG LLM cache.
2. **sqlite-vec `vec0` table beside the existing FTS5 index, RRF hybrid search in SQL** (§4).
   Effort: medium. Value: semantic search lands in `bk search`/`bk context` AND supplies the embeddings
   store recommendation 1 needs — one write path, two features. Index stays disposable, zero servers.
3. **LLM response cache keyed by (model, prompt-version, content-hash)** — shippable independently of
   rec 1 and worth it even if rec 1 stalls; makes reconciles/re-runs free (LightRAG precedent).
   Effort: low. Value: medium-high.
4. **Bi-temporal freshness: invalidate, don't just age** (Graphiti) — `superseded_by` + validity
   windows in the freshness ledger; enables "what did we believe on date X" and turns staleness into
   queryable provenance history. Effort: medium. Value: medium, strongly differentiating.
5. **Web UI: LightRAG-style triad (graph explorer / source browser / job status) + Letta-style
   consumer-view inspector** — the inspector doubles as a privacy-boundary test surface. Effort:
   medium-high. Value: medium; the consumer inspector is the piece no competitor has.

Honorable mentions: typed wikilink relations (basic-memory pattern, zero schema cost); weekly freshness
digest (khoj automations analog); keep MCP consolidated at 3–6 tools (supermemory).

---

## 8. Unverified

- **doobidoo/mcp-memory-service**: both `api.github.com/repos/doobidoo/mcp-memory-service` and the
  HTML page returned **404 on 2026-08-14** — renamed, made private, or deleted. Third-party indexes
  (https://awesome.ecosyste.ms/projects/github.com/doobidoo/mcp-memory-service,
  https://github.com/hesreallyhim/awesome-claude-code/issues/912) describe it as ~609★, sqlite-vec /
  Cloudflare / hybrid backends, ONNX local embeddings, dream-inspired consolidation, dashboard.
  Treat all of that as unverified; do not depend on it.
- **Graphiti's entity dedup mechanism** (LLM vs embedding) — not stated in the README I fetched;
  widely described elsewhere as LLM-assisted resolution over embedding-retrieved candidates, but I
  could not cite a primary source this session.
- **txtai's ANN backend list** (Faiss/hnswlib/numpy) and whether vectors can live inside SQLite —
  overview docs fetched did not enumerate backends; from memory: default Faiss, configurable; treat
  as memory/unverified.
- **mem0's exact retrieval-time dedup ranking** — the README documents the fusion signals but not the
  duplicate-collapse logic.
- **supermemory benchmark numbers** (95% Recall@15, 99.4% context reduction, 3.0× tokens) — vendor
  claims from its own README, unaudited.
- **R2R's current roadmap** — repo push date says stalled since 2025-11; the company may have moved
  development private. Impression only.
