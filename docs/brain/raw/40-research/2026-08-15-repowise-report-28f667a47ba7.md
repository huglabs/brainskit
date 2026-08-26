# Repowise — Overview Lane Report (for the brainkit leverage study)

Investigated: https://github.com/repowise-dev/repowise · 2026-08-15
Method: WebFetch of the repo page, GitHub API metadata, README, git tree (recursive), `packages/` contents listing, and five docs (`docs/architecture/ARCHITECTURE.md`, `docs/architecture/graph-algorithms.md`, `docs/layers/INTELLIGENCE_LAYERS.md`, `docs/agent/MCP_TOOLS.md`, `docs/agent/HOOKS.md`, `docs/layers/WIKI.md`, `docs/agent/DISTILL.md`). No clone, no build. Demo-GIF and frontend-source deep dives are sibling lanes and are only touched in passing here.

---

## 1. Identity and metadata

Source: https://api.github.com/repos/repowise-dev/repowise

| Field | Value |
|---|---|
| Full name | `repowise-dev/repowise` |
| Description | "Codebase intelligence for AI and humans: code health scores, auto-generated docs, git analytics, dead code detection, and architectural decisions via MCP." |
| Stars / forks / open issues | **5,856 / 604 / 131** |
| License | **AGPL-3.0** (GNU Affero GPL v3) |
| Created / last push | 2026-03-23 / 2026-08-14 (active daily) |
| Primary language | Python (TypeScript for web/api-client/vscode packages) |
| Default branch | `main` · Homepage: https://repowise.dev |
| Topics | ai, code-complexity, code-health, code-intelligence, code-quality, dead-code, developer-tools, documentation, git-analytics, mcp, open-source, python, refactoring, static-analysis, technical-debt |

**Maturity signals:** ~5 months old but shipping hard — 5.9k stars, ~1,190 commits (rendered repo page; approximate), 131 open issues, PyPI package (`pip install repowise`), a GitHub App PR bot (`github.com/apps/repowise-bot`), Docker setup, pre-commit config, a `glama.json` (MCP registry listing), `.claude-plugin/` + `.agents/plugins/` marketplace manifests, a public self-hosted index of its own repo re-indexed on every push (README → repowise.dev/repo/repowise-dev/repowise), and a written benchmark methodology (`docs/BENCHMARKS.md`). Dual-licensed commercially (docs/business/COMMERCIAL.md — enterprise buys out the AGPL obligation).

## 2. Purpose

From the README (https://raw.githubusercontent.com/repowise-dev/repowise/main/README.md): *"Your AI agent burns most of its budget rediscovering your codebase. Index it once, and it never has to again."* Repowise is a **local-first codebase-intelligence layer for AI coding agents** (and secondarily humans via a dashboard): it indexes a repo once into five deterministic "intelligence layers," keeps the index synced per commit, and serves it to agents through 10–17 task-shaped MCP tools, proactive agent hooks, a CLI, and a web dashboard. Audience: teams running Claude Code / Codex / Cursor / VS Code / OpenCode / Hermes against non-trivial repos.

Self-reported benchmarks (README; **self-reported, not independently verified**): 0.876 vs next tool's 0.610 file-coverage on a sealed 42-instance ContextBench split; −31.6% agent output tokens; 35.6× commit-context compression; defect-prediction ROC AUC 0.737 across 21 repos / 9 languages / 2,826 files; "2.3× the defects found under a fixed review budget" vs CodeScene (p=0.003).

## 3. Tech stack and architecture

Source: https://raw.githubusercontent.com/repowise-dev/repowise/main/docs/architecture/ARCHITECTURE.md and https://api.github.com/repos/repowise-dev/repowise/contents/packages

**Monorepo of 8 packages** (`packages/`): `core` (Python ingestion+generation engine), `cli` (Click), `server` (FastAPI REST + MCP), `web` (Next.js 15 dashboard), `api-client` (TS), `types` (TS), `ui` (TS), `vscode` (extension). Note: the git-tree fetch was truncated and a rendered-page summary mentioned a `website/` dir; the authoritative `packages/` listing above is what exists.

**Backend (Python `packages/core`):**
- Ingestion: `traverser.py` (six exclusion layers incl. per-directory `.repowiseIgnore`), one `ASTParser` for all languages driven by tree-sitter `.scm` query files + a `LANGUAGE_CONFIGS` dict (no per-language subclasses), `graph.py` (NetworkX two-tier file+symbol DiGraph), `call_resolver.py` (3-tier confidence: same-file 0.95, import-scoped ~0.85–0.93, globally-unique 0.50), `git_indexer.py`, `change_detector.py`, `special_handlers.py` (OpenAPI/Protobuf/GraphQL/Dockerfile/CI YAML).
- Generation: `page_generator.py` (8-level page hierarchy), `context_assembler.py` (9-tier context in a 12K-token budget), `job_system.py` (checkpointed resumable jobs), Jinja2 templates overridable via `.repowise/prompts/`, `editor_files/` (CLAUDE.md marker-merge).
- Providers: abstract `LLMProvider` + Anthropic/OpenAI/Ollama/LiteLLM adapters; LLM strictly optional.

**Three storage systems** (ARCHITECTURE.md):
1. **SQL** — SQLAlchemy over SQLite (dev) or PostgreSQL (multi-worker): repos, wiki_pages, page_versions, symbols, jobs, git_metadata, graph_nodes/edges, dead_code_findings, decision_records, answer_cache, chat.
2. **Vector** — LanceDB embedded (`.repowise/lancedb/`) in SQLite mode, pgvector+HNSW in Postgres mode, behind one `vector_store.py` abstraction.
3. **Graph** — NetworkX in memory, persisted to `.repowise/graph.json` up to ~30K nodes, auto-switching to SQLite-backed tables (+networkit) above that.

**Server** (`packages/server`): FastAPI on :7337 (REST + SSE job progress + Prometheus `/metrics` + HMAC-verified GitHub/GitLab webhooks), MCP server over stdio / streamable-http / SSE with tool implementations calling Python functions in-process ("no subprocess overhead"), APScheduler for 15-min polling fallback and nightly staleness resolution.

**Data flow:** init = traverse → parse → graph (PageRank/SCC/communities) → git-index → dead-code → 8-level page generation → persist to all three stores. Maintenance = webhook/hook/watcher/poll → git diff → affected-pages walk over the dependency graph → regenerate top-N under a `cascade_budget` (default 30 pages/push), rename-patch or confidence-decay the rest → nightly background job (budget 100 pages) catches deferred work.

**License:** AGPL-3.0 per API metadata + LICENSE file at repo root (tree). Consequence for brainkit: **ideas are freely portable; code is not** unless brainkit accepts AGPL contamination. Everything below is idea-level.

## 4. The five (nine) intelligence layers

Source: https://raw.githubusercontent.com/repowise-dev/repowise/main/docs/layers/INTELLIGENCE_LAYERS.md — all computed **without model calls**.

1. **Graph** — two-tier dep graph, 3-tier confidence call resolution, heritage edges, Leiden communities, PageRank/betweenness/SCC, framework-aware edges across 22 frameworks (Django route→handler, FastAPI include_router, Flask blueprints, pytest conftest…).
2. **Git** — hotspots (exponentially decayed churn + 3-commits/90-days floor), per-author ownership, co-change pairs *without* import links, bus factor (authors covering 80% of commits), up to 50 "significant commits" per file (merges/bots filtered), contributor silo-risk profiles, 0–100 module health, reviewer suggestions weighted authorship×1.0 / co-change×0.5 / recency×0.4.
3. **Documentation** — wiki from deterministic templates (keyless) or model prose; per-page freshness; hybrid FTS+vector search with PageRank bias and 1–2-hop graph expansion.
4. **Decisions** — ADRs mined from five sources (ADR files, PR bodies, inline `# WHY:` / `# DECISION:` / `# TRADEOFF:` markers, git archaeology, code comments) with evidence spans and **anti-hallucination stamps: exact / fuzzy / unverified**; typed edges (supersedes/refines/relates_to/conflicts_with).
5. **Code health** — 49 deterministic detectors (26 may move the defect score), per-file 1–10 on defect/maintainability/performance, weights calibrated offline on a bug corpus (AUC 0.737), concrete refactoring plans (Extract Class/Method, Move Method, Break Cycle, Split File); full health pass <30 s on a 3,000-file repo.

Derived layers: **change risk** (calibrated 0–10 for a revspec, percentile vs recent commits), **test intelligence** (LCOV/Cobertura/Clover coverage merged in; `impacted-tests`), **bug history** (bug-fix attribution per file/function, "bug magnet" flag), **security + dead code** (dead code = pure graph traversal + SQL, 17 dynamic-pattern exclusions like `*Plugin`/`*Handler`, `safe_to_delete` requires confidence ≥0.70).

## 5. Feature inventory (user-facing, with evidence)

### 5.1 CLI (README "CLI Command Reference"; `packages/cli/src/repowise/cli/commands/` in the tree confirms one `*_cmd.py` per command)
`init` (interactive / `--no-prose -y` keyless / `--prose -y`), `generate`, `serve`, `update`, `watch`, `search`, `ask`, `context`, `symbol`, `why`, `health`, `risk main..HEAD` / `risk -t <file>`, `impacted-tests`, `dead-code`, `decision list|deprecate`, `export --format structurizr`, `distill <cmd>`, `expand <ref>`, `saved`, `workspace add`, `doctor`, `uninstall`, `telemetry disable`, `hook install|status|stats|backfill`, plus `agents`, `corrections`, `costs`, `coverage`, `claude_md`, `restyle`, `security`, `reindex`, `login`, `whats_new` (cmd files in tree).

### 5.2 MCP tools (docs/agent/MCP_TOOLS.md)
17 total; 11 advertised by default in single-repo mode; 2 auto-added in workspace mode; 4 opt-in.
- Default: `get_overview`, `get_answer` (one-call RAG with citations + high/med/low confidence), `get_context` (batch triage cards; optional skeleton views), `get_symbol` (`file.py::Name`, exact bounds, 200-line max range reads, resolves omission refs), `search_codebase` (routes by query shape: identifier→symbol, path→file, prose→wiki), `get_risk`, `get_change_risk`, `get_why`, `get_dead_code`, `get_health`, `list_repos`.
- Workspace: `get_architecture` (propagation cost, cyclic core, Core/Shared/Control/Peripheral roles), `get_blast_radius` (cross-repo).
- Opt-in: `get_dependency_path`, `get_execution_flows`, `generate_refactoring_code` (double opt-in, content-hash cached), `get_conformance` (declared dependency rules vs live graph).
- `_meta` envelope on every response: `timing_ms`, `hint`, `cached`, `indexed_commit`/`live_head`, `stale_warning` (fires only on real signals: HEAD mismatch touching the answered files, or index >~90 days), `index_behind`, `embedder_degraded`, `omitted` (see 5.4).
- **Tool-surface config**: delta (`["+get_execution_flows","-get_dead_code"]`), allowlist, or presets `all`/`lean` — the `lean` profile costs ~2.1k schema tokens vs 4.1k default. Stated philosophy: *fewer, richer, task-shaped tools; `directive` blocks over raw data enumeration*.

### 5.3 Proactive agent hooks (docs/agent/HOOKS.md) — all "no LLM calls, no network, <500 ms cold start, fail open"
- **SessionStart** (Claude Code): index freshness one-liner + **relevance-ranked standing decisions**, scored against the working set (dirty/staged files, branch diff vs main, edited files, branch-name tokens) expanded one hop through imports + co-change; capped ~400 tokens. Deliberately excluded from the `compact` event (block survives summaries; re-emitting doubles it).
- **PostToolUse Grep/Glob enrichment**: appends symbols, imported-by, depends-on, and git signals (HOTSPOT, bus-factor, owner) for matched files.
- **Search-flood digest**: 50+ grep matches → per-file counts + anchor lines ranked by graph centrality; never digests `-A/-B/-C` context greps or `files_with_matches`.
- **Read-intelligence**: stale-read notice when a file changed since it was last read this session; optional **read-skeleton** (large-file Read returns signatures with `... N lines (a–b)` elided bodies; range-read recovers any span; edit-before-full-read raises a one-time warning); optional **re-read suppression** (identical re-read returns a notice instead of bytes; byte-hash guarded; never twice in a row).
- **PostToolUseFailure wrong-path rescue**: failed Read/Edit on a nonexistent path names the file iff exactly one indexed file has that basename; silent otherwise ("silence preferred over confident wrong answers").
- **Edit-time decision injection**: editing a governed file surfaces the decision one line, once per session; a session miner later checks whether guidance was followed and **bumps/relaxes decision staleness accordingly** (feedback loop via `.repowise/sessions/sessions.db`).
- **Git post-commit hook** (marker-delimited block, coexists with husky-style setups) triggering background `repowise update`; Codex gets SessionStart + shell-watching PostToolUse instead.
- **`repowise hook stats`**: local efficacy ledger — firings paired with subsequent tool calls → action rates per surface; `hook backfill` seeds it from existing Claude Code transcripts.

### 5.4 Output distillation (docs/agent/DISTILL.md)
`repowise distill <cmd>` compresses command output errors-first before an agent reads it: 11 filters (test/build/lint/install/terraform/git/search/ls/logs…), **guarantee that every error/failure-classified line survives**, net-positive gate (small outputs pass through), inline markers `[repowise#<12-hex>: 230 lines omitted (~6.1k tokens)]` backed by a **SQLite omission store** (`.repowise/omissions/omissions.db`, TTL 7 days, 50 MB cap) with byte-for-byte `repowise expand <ref>` reversal — also resolvable through MCP `get_symbol("repowise#<ref>")` for non-shell clients. Measured: pytest 61%, `git log -50` 89%, big diff 86%, `npm ci` 99.5% reduction. A PreToolUse rewrite hook does bounded substitution (`pytest -x` → `repowise distill pytest -x`; never compounds/redirects/watch modes). `repowise saved` prices savings at the agent input rate and mines transcripts for **missed** savings.

### 5.5 Wiki generation and freshness (docs/layers/WIKI.md)
- Two modes sharing one page-selection pipeline: **keyless structure-derived** (whole wiki from extraction, confidence 1.0) vs **prose** (only 4 page types get model text — module pages, repo overview, architecture diagrams, onboarding — confidence 0.8; "the axis is trust, not completeness").
- 8-level page hierarchy: API contract → symbol spotlight → file page → SCC page → module page → repo overview / architecture diagram → infrastructure page → onboarding.
- Staleness: expiry-by-age first, then source-hash mismatch. Model pages skip regeneration when the **hash of the rendered prompt** is unchanged (zero tokens spent); structure pages fold the subject hash with a **renderer fingerprint** (template source + style + output language), so a template update triggers exactly one regeneration per page type.
- Incremental: changed files → dependency-graph walk → re-render only impacted pages, capped by `--cascade-budget`.
- Search: SQLite FTS + vector fused by **Reciprocal Rank Fusion, biased by PageRank, then 1–2-hop expansion along imports/projected-calls for flow-shaped questions**. Four prose styles (`comprehensive`, `caveman`, `reference`, `tutorial`) + custom styles; output-language config.

### 5.6 Dashboard (sibling lane; enumerated from README only)
Local web UI on :3000 / Next.js 15 app in `packages/web`: Architecture graph, Code-health bubble map, Chat (streaming, tool results), Docs wiki with Mermaid, C4 views, zoomable Knowledge Graph, Risk/Hotspots/Coupling, Contributors, Decisions timeline + evidence drawer, Symbols, Security, Dead Code, Stats, Costs (token+dollar accounting), Workspace.

### 5.7 Other
- **PR bot** (free GitHub App): deterministic zero-LLM change-risk comments — symbol-level blast radius, co-change partners, risk percentile vs repo distribution, public analysis page per PR, silent on clean PRs (README).
- **Workspace / multi-repo**: contract matching between producer/consumer APIs, cross-repo co-change, federated MCP with a `repo` parameter (README + MCP_TOOLS.md).
- **Editor files**: auto-generated CLAUDE.md/AGENTS.md between `<!-- REPOWISE:START/END -->` markers — index freshness, trust protocol, MCP tool table, architecture summary, "files that need care" ordered bug-fix-history-first-then-churn, standing decisions, build commands; pure synthesis from the index, no LLM (INTELLIGENCE_LAYERS.md).
- **Worktree auto-detection** with incremental seeding; auto-sync via hook/watcher/webhook/polling (README, docs/scale/).
- **Privacy**: self-hosted, BYO-key, offline-capable via Ollama + local embedders, opt-out anonymous telemetry (`DO_NOT_TRACK=1`) (README).

## 6. Engineering techniques worth stealing (idea-level; AGPL bars code reuse)

1. **Reversible truncation everywhere** — one omission store (SQLite, hash-keyed, TTL'd) shared by MCP responses and CLI output; every elision carries a ref and a restore instruction. Turns "response shaping" from lossy to lossless.
2. **Renderer-fingerprint freshness** — page hash = subject hash ⊕ (template + style + language) fingerprint; prompt-hash reuse for model pages. Precise, zero-clock staleness.
3. **Cascade budget + nightly catch-up** — bounded incremental work per event; a scheduler drains the deferred queue. Confidence *decays* transitively instead of a binary stale bit.
4. **Trust-labeled generation** — extracted content = 1.0, model prose = 0.8, decisions stamped exact/fuzzy/unverified. Confidence is about provenance, not completeness.
5. **RRF hybrid search biased by PageRank + graph expansion of hits** — cheap fusion (no learned ranker), then let the link graph pull in neighbors for "how does X flow" questions.
6. **Task-shaped MCP with a `lean` preset and delta config** — measured schema-token cost per surface (2.1k vs 4.1k) as a first-class design number; `_meta.hint` as a conservative next-step suggestion; stale warnings only on *real* signals.
7. **file_subgraph() metric hygiene** — centrality computed on a filtered view (files+packages only); co-change edges excluded from PageRank with an explicit written justification; betweenness sampled (k=500) above 30K nodes; Louvain/Leiden seeded for reproducibility; PageRank convergence fallback to uniform.
8. **Hook efficacy ledger** — hooks record their own firings and whether the agent acted; injections that stop being followed stop being injected. Closes the loop most hook systems leave open.
9. **One parser, N `.scm` files** — adding a language = one query file + one config entry; no per-language subclass tax.
10. **Marker-merge editor files** + guard-wrapped hook commands (`if command -v repowise-augment …`) so partial installs never break a shell.
11. **Silence over confident wrong answers** as a written hook design principle (wrong-path rescue fires only on unique basename).
12. **In-process MCP tool dispatch** (tools call the engine's Python functions directly, no subprocess) and `answer_cache` table for repeated questions.

## 7. Classification for brainkit

**(a) Directly portable** (concepts re-implemented cleanly — brainkit surfaces named):

| Repowise feature | brainkit landing surface | Note |
|---|---|---|
| Omission store + `expand <ref>` reversible truncation | MCP responses + `bk context`/`bk search --json`; new `bk expand` | brainkit already shapes output; this makes shaping lossless. SQLite table beside the FTS index. |
| Renderer-fingerprint + prompt-hash freshness | Freshness ledger / `bk lint` ageing | Complements `content_hash`: today a template/style change in wiki rendering is invisible to freshness. |
| Cascade budget + background catch-up + confidence decay | `bk lint` staleness pass, apply pipeline | Bounded per-event work; ageing already exists — add transitive decay through `sourced_from`/`links_to`. |
| RRF fusion + PageRank bias + 1–2-hop graph expansion of hits | `bk search` / `bk context` | FTS5 + link graph already exist; RRF is ~20 lines stdlib; expansion reuses the graph projection. |
| `lean`/delta MCP tool-surface config with measured schema token cost | brainkit MCP server | Same "consolidate + measure" doctrine the user already applies in gitlab-mcp. |
| Stale warning on real signals (`indexed_commit` vs `live_head`) | `bk status`, MCP `_meta` | Maps to registry/reconcile state vs on-disk vault. |
| Session-start standing-decisions injection ranked by working set | `bk hooks install` (SessionStart template) | Rank wiki decision/synthesis pages against dirty files via the code graph; ~400-token cap. |
| PostToolUse grep/read enrichment + flood digest + wrong-path rescue | `bk hooks install` (new PostToolUse template) | brainkit ships PreToolUse gate + status hooks already; the enrichment direction is unbuilt and cheap (reads `code.json` + FTS only). |
| Hook efficacy ledger (`hook stats`) | `bk doctor` / hooks | Extends the "exercised, not existing" doctor philosophy from verification to *usefulness*. |
| Trust-labeled pages (extracted 1.0 vs synthesized 0.8) | wiki frontmatter, `bk lint` | brainkit's citation gate already proves provenance; a per-page trust axis is one field. |
| Decision mining from inline `# WHY:`/`# DECISION:` markers + git archaeology | `bk capture` / ingestion path (`application/capture.py`) | New evidence source class feeding raw/ with automatic branch suggestion. |
| Editor-file synthesis from the index (freshness, key pages, decisions) into the CLAUDE.md marker block | `bk hooks install` CLAUDE.md block | Today the block is static prose; generate the "state of the vault" section from registry+freshness. |
| 3-tier confidence call resolution; co-change edges kept out of centrality; betweenness sampling; seeded communities | `bk code build` / `hubs` / `communities` | Direct upgrades to the tree-sitter graph; the metric-hygiene rules transfer verbatim. |

**(b) Inspiration only:**
- **Git intelligence layer** (hotspots, bus factor, ownership, co-change, significant commits) — powerful, but a whole new layer for `bk code`; scope decision first.
- **Code-health detectors + calibrated defect scores + refactoring plans** — a different product; the *pattern* (deterministic detectors, offline-calibrated weights, prescriptions not scores) is the lesson.
- **Distill** — the user's RTK already occupies this niche; repowise's differentiators (reversible omission store, errors-always-survive guarantee, `saved` counterfactual accounting) are the upgrade path for RTK, not brainkit.
- **PR bot / webhooks / SaaS tier** — brainkit is deliberately local-first, no CI surface today.
- **Read-skeleton / re-read suppression hooks** — clever but riskier (repowise itself documents the edit-before-full-read hazard); watch, don't copy first.
- **8-level structure-derived wiki with keyless mode** — brainkit's wiki is evidence-compiled, not code-derived; the "zero-LLM first index" stance is a positioning lesson (brainkit's apply gate already needs no model for validation).
- **C4/Structurizr export** — nice-to-have for `bk export` targets someday.

**(c) Not applicable:**
- **Federated multi-repo MCP with a `repo` parameter** — brainkit's ADR deliberately keeps `bk vaults` CLI-only because an MCP server answers under *one* vault's privacy boundary; repowise has no per-consumer privacy model to protect. Their design confirms brainkit's is a real differentiator, not a gap.
- **PostgreSQL/pgvector scale-out, Prometheus, APScheduler server mode** — contradicts brainkit's stdlib-only, single-operator posture.
- **LLM provider registry for prose generation** — brainkit already has its own provider config in policy.

**What repowise lacks that brainkit has** (positioning, for the report's consumers): per-claim citation enforcement at write time (repowise cites at retrieval, brainkit at *apply*), consumer-scoped privacy (cloud/local/human) applied post-expansion, immutable hash-identified evidence with branch policy, and a write gate that mechanically refuses hand edits. Repowise's "anti-hallucination stamps" are labels; brainkit's citations are a transaction precondition.

## 8. Unverified / discrepancies

- **Doc drift inside repowise**: README says "ten MCP tools"; `docs/agent/MCP_TOOLS.md` documents 17 (11 default). `docs/architecture/ARCHITECTURE.md` says 14 tree-sitter languages / Louvain / "11 tools"; README and `INTELLIGENCE_LAYERS.md` say 18+ languages and **Leiden** — the architecture doc appears older. Treat README+layers docs as current.
- **1,190 commits** and language-bar percentages come from a rendered-page summary, not the API — approximate.
- All benchmark numbers (0.876 coverage, −31.6% tokens, AUC 0.737, CodeScene p=0.003, distill percentages) are **self-reported** in README/docs/BENCHMARKS.md; not independently reproduced here.
- Git tree fetch was truncated; `packages/` membership was confirmed via the contents API, but per-file listings inside `core/`, `server/`, `web/` rest on ARCHITECTURE.md's description rather than the raw tree.
- Did not fetch: LICENSE body (SPDX from API deemed sufficient), `docs/BENCHMARKS.md`, `docs/architecture/pluggable-storage.md`, homepage repowise.dev, the hosted demo.

## 9. Sources

- https://api.github.com/repos/repowise-dev/repowise (metadata, license, dates, counts)
- https://github.com/repowise-dev/repowise (rendered page: commit count, layout)
- https://raw.githubusercontent.com/repowise-dev/repowise/main/README.md (features, CLI, benchmarks, positioning, deployment, privacy)
- https://api.github.com/repos/repowise-dev/repowise/git/trees/HEAD?recursive=1 (file tree; truncated)
- https://api.github.com/repos/repowise-dev/repowise/contents/packages (8 packages)
- https://raw.githubusercontent.com/repowise-dev/repowise/main/docs/architecture/ARCHITECTURE.md
- https://raw.githubusercontent.com/repowise-dev/repowise/main/docs/architecture/graph-algorithms.md
- https://raw.githubusercontent.com/repowise-dev/repowise/main/docs/layers/INTELLIGENCE_LAYERS.md
- https://raw.githubusercontent.com/repowise-dev/repowise/main/docs/agent/MCP_TOOLS.md
- https://raw.githubusercontent.com/repowise-dev/repowise/main/docs/agent/HOOKS.md
- https://raw.githubusercontent.com/repowise-dev/repowise/main/docs/layers/WIKI.md
- https://raw.githubusercontent.com/repowise-dev/repowise/main/docs/agent/DISTILL.md
