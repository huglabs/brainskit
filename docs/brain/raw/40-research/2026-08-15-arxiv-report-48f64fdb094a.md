# arXiv Literature Sweep for brainkit (2026-08-14)

Scope: papers 2023–2026 whose techniques could improve brainkit's performance or utility.
Every arXiv ID below was fetched this session from the arXiv API export endpoint
(`export.arxiv.org/api/query`) — none are from memory. **Repo links are from memory and
marked unverified** unless stated otherwise; the arXiv IDs themselves are all verified.

Pain-point key:
- **A** retrieval quality (FTS5 keyword-only today)
- **B** wiki compilation quality (citation-grounded synthesis)
- **C** memory organization (freshness, consolidation, dedup/novelty — the ollama ~14 items/min bottleneck)
- **D** graph construction & use (GraphRAG, multi-hop, community summarization)
- **E** code graph (repo-level context)
- **F** privacy (cloud/local consumer split)

---

## Tier 1 — highest actionable value (ranked)

### 1. vstash: Local-First Hybrid Retrieval with Adaptive Fusion for LLM Agents
**arXiv 2604.15484** (2026) — pain point **A**
A SQLite-based retrieval system combining vector similarity with keyword matching via
reciprocal rank fusion (RRF) and adaptive IDF weighting — explicitly local-first, explicitly
SQLite. This is almost a blueprint for brainkit's next retrieval step: same storage engine,
same deployment constraint (no server, no cloud).
**Lever:** add a `vec` table (e.g. sqlite-vec) beside the existing FTS5 index, embed pages/sources
with a small local model, fuse FTS5 + vector hits with RRF. The paper's adaptive-IDF weighting
answers the "when does keyword beat dense" question at brainkit's scale.

### 2. SemDeDup: Data-efficient learning at web-scale through semantic deduplication
**arXiv 2303.09540** (2023) — pain point **C** (the novelty bottleneck)
Uses embedding clustering + cosine-similarity thresholds inside clusters to find and drop
semantic near-duplicates cheaply, at web scale, with no LLM in the loop.
**Lever:** put an embedding-similarity pre-filter *in front of* the ollama novelty check.
Items whose nearest neighbor in the vault exceeds a similarity threshold are auto-rejected
(or auto-accepted if below a floor); only the ambiguous middle band reaches the LLM. At the
observed ~14 items/min, cutting 80–95% of LLM calls is the single biggest throughput win
available (the openharness 14k-item / ~14h ingestion run is the concrete motivating case).
Repo (memory, unverified): github.com/facebookresearch/SemDeDup

### 3. RecMem: Recurrence-based Memory Consolidation for Efficient and Effective Long-Running LLM Agents
**arXiv 2605.16045** (2026) — pain point **C**
Reduces consolidation cost by invoking the LLM **only when sustained recurrence is observed**
for semantically similar interactions — i.e., don't pay LLM judgment per item; pay it per
recurring cluster worth extracting.
**Lever:** complementary to SemDeDup at the other end: batch pending sources into semantic
clusters first, run one ollama novelty/synthesis judgment per cluster instead of per item.
Directly restructures `label_all.py`-style ingestion from per-item to per-cluster.

### 4. HippoRAG (2405.14831, 2024) + HippoRAG 2: From RAG to Memory (2502.14802, 2025)
Pain points **D + A**
HippoRAG orchestrates an LLM-built KG with **Personalized PageRank** for multi-hop retrieval,
mimicking hippocampal indexing; HippoRAG 2 adds deeper passage integration and stronger
associative memory, framed as non-parametric continual learning.
**Lever:** brainkit already maintains exactly the graph HippoRAG has to build first
(pages→sources `sourced_from`, pages→pages `links_to`). Minimal adoption: seed Personalized
PageRank from FTS5 hit nodes and return the top-ranked neighborhood as `bk context` evidence —
graph-aware retrieval with **no embeddings required**, a pure-Python step over graph.json.
Repo (memory, unverified): github.com/OSU-NLP-Group/HippoRAG

### 5. LightRAG: Simple and Fast Retrieval-Augmented Generation
**arXiv 2410.05779** (2024) — pain points **D + A**
Graph-structured RAG with **dual-level retrieval** (low-level entity lookups + high-level
thematic queries) and — critically for brainkit — **incremental graph updates** instead of full
re-indexing, designed to be much cheaper than Microsoft GraphRAG.
**Lever:** the dual-level pattern maps onto brainkit's entity pages vs synthesis pages. Adopt
the query-routing idea: keyword/entity queries hit FTS5 + graph neighbors; "what do we know
about X" queries hit synthesis pages and community summaries. Incremental update discipline
matches `bk apply`'s per-page writes.
Repo (memory, unverified): github.com/HKUDS/LightRAG

### 6. RAPTOR: Recursive Abstractive Processing for Tree-Organized Retrieval
**arXiv 2401.18059** (2024) — pain points **A + B**
Builds a tree of recursive cluster summaries over chunks so retrieval can match at multiple
abstraction levels; big gains on questions needing whole-corpus understanding.
**Lever:** brainkit's wiki synthesis pages *are* hand-grown RAPTOR internal nodes. Formalize it:
have `bk` cluster un-synthesized sources per branch and propose synthesis pages for dense
clusters (with citations, through the apply gate) — turning the wiki into a retrieval-usable
abstraction hierarchy instead of an ad-hoc one. Follow-up worth noting: **2410.01736**
(Recursive Abstractive Processing for Retrieval in Dynamic Datasets, 2024) handles keeping such
trees valid under insert/delete — brainkit's reconcile/freshness situation.
Repo (memory, unverified): github.com/parthsarthi03/raptor

### 7. Enabling Large Language Models to Generate Text with Citations (ALCE)
**arXiv 2305.14627** (2023) — pain point **B**
The canonical benchmark + automatic metrics for citation quality: **citation recall** (is every
statement supported by its cited source?) and **citation precision** (is every citation
necessary/relevant?), measured with an NLI model.
**Lever:** brainkit's apply gate enforces that citations *exist*; ALCE-style NLI checking would
enforce that citations are *true*. Minimal adoption: a `bk lint --verify-claims` pass that runs
a small local NLI model over (claim sentence, cited source excerpt) pairs and flags unsupported
claims — upgrading provenance from structural to semantic.

### 8. RepoGraph: Enhancing AI Software Engineering with Repository-level Code Graph
**arXiv 2410.14684** (2024) — pain point **E**
A plug-in line-level repository code graph that any coding-agent framework can mount for
repository-wide navigation; consistent gains across agents on SWE-bench.
**Lever:** validates `bk code`'s design and suggests the missing piece: an **ego-graph
retrieval primitive** — given a symbol or error line, return its k-hop subgraph as compact
context (the paper's core operation). `bk code context SYMBOL --hops 2` emitting an
agent-ready digest would make code.json consumable in prompts, not just queryable.
Repo (memory, unverified): github.com/ozyyshr/RepoGraph

---

## Tier 2 — strong, adopt-later or design-informing

### 9. Mem0: Building Production-Ready AI Agents with Scalable Long-Term Memory
**arXiv 2504.19413** (2025) — pain point **C**
Extraction + consolidation pipeline where each candidate memory is resolved against existing
ones via ADD / UPDATE / DELETE / NOOP operations; reports large latency and token savings vs
full-context.
**Lever:** brainkit's novelty check is a binary accept/reject; Mem0's four-way resolution is
the richer contract — "this source UPDATES page X" routes straight into an apply proposal with
`base_hash` instead of being rejected as insufficiently novel.
Repo (memory, unverified): github.com/mem0ai/mem0

### 10. Zep: A Temporal Knowledge Graph Architecture for Agent Memory
**arXiv 2501.13956** (2025) — pain points **C + D**
Graphiti engine: a **bi-temporal** KG where edges carry validity intervals; new facts
*invalidate* old edges rather than deleting them, preserving history while keeping "current
truth" queryable.
**Lever:** the freshness ledger ages whole pages; Zep suggests aging **claims/edges**. Minimal
adoption: when an apply supersedes a claim, record invalidated-at on the old claim's citation
edge instead of just re-aging the page — giving `bk` a "what did we believe on date T" query.
Repo (memory, unverified): github.com/getzep/graphiti

### 11. A-MEM: Agentic Memory for LLM Agents
**arXiv 2502.12110** (2025) — pain points **C + D**
Zettelkasten-style dynamic memory: each new note triggers link generation to related existing
notes and **memory evolution** — updating old notes' contextual descriptions when new related
ones arrive.
**Lever:** on `bk apply` of a new page, propose link/update candidates for its graph neighbors
(through the gate, cited) instead of leaving old pages untouched until they age stale — the
freshness ledger's `review` state gains an active trigger, not just a timer.

### 12. When Memory Becomes Authority: Benchmarking Authority Collapse at the Memory Consolidation Boundary
**arXiv 2608.01679** (2026) — pain point **F**
Names and benchmarks the exact failure brainkit's privacy model must avoid: consolidation
"erases the source constraints governing its authorized use" — a private source's content
leaking into a derived summary that no longer carries the restriction.
**Lever:** an audit rule: a wiki page's effective consumer scope must be the **strictest fold**
of its cited sources' branches (brainkit already has strictest-privacy fold vocabulary from the
privacy-boundary seam work); `bk lint` should verify no page is more visible than any source it
cites. Companion: **2607.29167** (Memory Provenance Laundering, 2026) — a non-amplification
firewall so untrusted/low-privilege observations can't gain authority through consolidation.

### 13. Citation-Grounded Code Comprehension: Preventing LLM Hallucination Through Hybrid Retrieval and Graph-Augmented Context
**arXiv 2512.12117** (2025) — pain points **B + E + A**
Combines lexical + semantic + structural (graph) evidence for code questions; reports 92%
citation accuracy with zero hallucinations by forcing every answer span to cite retrieved
evidence.
**Lever:** the design sketch for joining brainkit's two graphs: answer code questions from
`code.json` context but force citations back to vault evidence — a `bk code explain SYMBOL`
that emits apply-ready, cited output.

### 14. From Local to Global: A Graph RAG Approach to Query-Focused Summarization (Microsoft GraphRAG)
**arXiv 2404.16130** (2024) — pain point **D**
The origin of community-detection + pre-computed community summaries for corpus-level
("global") questions.
**Lever:** brainkit already computes communities for the code graph; do it for the wiki graph
and generate **cited community-summary synthesis pages** through the apply gate — answering
"what does this vault know about X" without walking every page. Adopt via LightRAG-style
incremental indexing, not the full (expensive) GraphRAG pipeline.
Repo (memory, unverified): github.com/microsoft/graphrag
Reality check also verified this session: **2604.09666** "Do We Still Need GraphRAG?" (2026)
finds agentic/iterative search narrows the dense-vs-graph gap except on complex multi-hop —
supporting a cheap-first (FTS5+PPR) strategy before any heavy graph summarization.

### 15. MemoRAG: Boosting Long Context Processing with Global Memory-Enhanced Retrieval Augmentation
**arXiv 2409.05591** (2024) — pain points **A + B**
A dual-system design: a light "memory model" over the whole corpus drafts clue answers that
then guide precise retrieval for the generator.
**Lever:** lower priority for brainkit (wants a resident compressed-memory model), but the
clue-then-retrieve pattern is adoptable cheaply: have the local model expand a `bk context`
query into clue terms before hitting FTS5 — poor-man's query expansion for a keyword-only index.
Repo (memory, unverified): github.com/qhjqhj00/MemoRAG

### 16. MemGPT: Towards LLMs as Operating Systems
**arXiv 2310.08560** (2023) — pain point **C**
The foundational paged/tiered memory design (main context vs external storage with
self-directed paging). Mostly design-informing for brainkit: the vault is the external tier;
`bk context` is the pager. Repo lineage (memory, unverified): github.com/letta-ai/letta

### 17. Code Graph Model (CGM)
**arXiv 2505.16901** (2025) — pain point **E**
Integrates repository code-graph structure directly into LLM attention; top open-weight results
on repo-level SWE tasks. For brainkit this is a horizon marker rather than an adoption target —
the graph-as-first-class-input trend validates investing in richer `code.json` exports.

### 18. Caching for the Future: Scrub Jay Episodic Memory Principles for Agent Memory Systems
**arXiv 2608.04746** (2026) — pain point **C**
Type-conditioned temporal decay: different memory types age at different rates to prevent
outdated-fact contamination.
**Lever:** brainkit's freshness ledger ages every page on one clock; adopt per-type decay
coefficients (entity pages age slower than synthesis pages; tool/version facts age fastest) —
a config-level change to the existing fresh→review→stale ageing pass.

---

## Also verified, noted for completeness
- **2608.11879** Total Recall at What Cost? (2026) — benchmarks serving cost of agentic memory
  systems; useful evaluation framing for brainkit's own ingestion economics (C).
- **2606.25161** TRUSTMEM (2026) — verifier for memory updates on coverage/preservation/
  faithfulness; maps to apply-gate acceptance criteria (B/C).
- **2605.30237** GRASP (2026) — plan-guided graph retrieval with adaptive fusion + reranking
  on semi-structured KBs (D).
- **2606.29652** As We May Search (2026) — local-first IR manifesto; keeps indexes + inference
  on-device; philosophical alignment with brainkit (A/F).
- **2606.25533** Security and Privacy in RAG survey (2026) — threat taxonomy across retrieval/
  context/generation stages; checklist material for the consumer-boundary design (F).
- **2402.04315** Training LMs to generate citations via fine-grained rewards (2024) — only
  relevant if brainkit ever fine-tunes a local synthesis model (B).
- **2404.03381** Learning to Plan and Generate Text with Citations (2024) — blueprint-based
  attribution; relevant to structuring apply proposals as plan-then-cite (B).
- **2607.11074** ResearchQA (2026) — citation-grounded QA benchmark, evaluation material (B).

## Unverified / not found on arXiv this session
- **Aider repo-map**: an engineering artifact (tree-sitter + PageRank ranking of symbols), not
  an arXiv paper — no listing sought; treat as implementation reference only.
- Repo URLs listed above are **from memory, not fetched this session** — verify before citing
  in durable docs. All arXiv IDs, titles and dates were fetched and are verbatim.
- "nano-graphrag" is a GitHub lineage, not a paper; its ideas are covered by 2404.16130 /
  2410.05779.

## Suggested adoption sequence (cheapest-first)
1. **C**: SemDeDup-style embedding pre-filter + RecMem-style cluster-batched novelty → attack
   the ollama bottleneck (no product-surface change).
2. **A**: vstash-style sqlite-vec + RRF hybrid beside FTS5.
3. **D/A**: HippoRAG-style Personalized PageRank over the existing graph, seeded by FTS5 hits
   (works even before embeddings land).
4. **B**: ALCE-style NLI claim verification in `bk lint`.
5. **C**: Zep-style claim-level temporal invalidation + type-conditioned decay in the ledger.
6. **F**: strictest-fold consumer-scope lint (authority-collapse guard).
7. **E**: RepoGraph-style ego-graph context primitive for `bk code`.
