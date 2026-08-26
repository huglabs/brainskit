# Stress Test Report #3 — Projections & Egress: Graph, Views, Export, Enrichment, Web

**Date:** 2026-08-25 · **Version:** `bk` 0.7.0 · **Surface:** everything *derived* from the vault — graph projection, views, file exports, enrichment gate, freshness surgery + reconcile, read-only web server
**Harness:** `stress/harness3.py` — 6 cases over 10 fresh vaults (~90 applied pages), all through the real CLI plus 60 live HTTP requests; results in `stress/results-projections.json`.

## Verdict at a glance

| # | Case | Result |
|---|------|--------|
| 1 | Graph of a 40-page link chain: complete, idempotent | **PASS** — 43 nodes / 79 edges / 39 `links_to`, identical across rebuilds |
| 2 | Export privacy across three consumers | **PASS** — cloud carries no private hash, page, or branch string |
| 3 | Written `graph/graph.json` excludes never-ingest | **PASS** |
| 4 | Integration targets refuse `--consumer` / `--enrichment` overrides | **PASS** — neo4j, postgres, obsidian each refuse cleanly |
| 5 | File exports: 5 targets × 2 consumers | **PASS** — json, graphml, cypher, kuzu, llms-txt all write |
| 6 | Views: rewrite, foreign files kept, narrowing wins | **PASS** (one gap, finding F-2) |
| 7 | Enrichment gates (8 checks) | **PASS** — provenance, endpoints, identity, strictest-source, fail-closed orphans |
| 8 | Freshness surgery (manual move + delete) then reconcile | **PASS** — registry healed, search recovers, lint cleans up |
| 9 | Web viewer under 60 concurrent reads | **PASS** — 60/60 HTTP 200 |

No correctness bug was found this round. Two gaps are reported below — one diagnostic blind spot and one stale-file inconsistency — plus one API-shape note.

## What was confirmed

**Privacy egress is tight everywhere it was probed.** A vault seeded with sources on `30-shared` (cloud) / `10-work` (local-only) / `90-private` (never-ingest) and pages citing one or both restricted classes:

- `export --target json --consumer cloud` contains neither the private content hash, nor the `salary-notes` page, nor even the string `90-private`; human sees all three.
- The written `graph/graph.json` (default local) has zero never-ingest hashes.
- Every integration target refuses a consumer override and refuses `--enrichment` outright instead of silently dropping the flag.
- All five file targets export cleanly at both extremes of the consumer range.

**Enrichment behaves exactly as its contract claims.** Edges without `derived_from`, with uncapturable evidence, or with dangling endpoints are refused before anything is stored. Identity is the triple — resubmitting the same relationship leaves total at 1. An edge derived from shared+private evidence is invisible to a cloud consumer (strictest-source inheritance). After `bk forget --force` of its entire evidence, the edge becomes unclassifiable: it never reaches any consumer, and `bk lint` reports it (`enrichment.unresolved_source`) so it can be repaired.

**Reconcile heals real damage.** With a raw file moved by hand into another configured branch and a wiki page deleted behind the gate's back: lint flags the drift, `reconcile` heals the registry, search finds the moved content again, and the orphaned freshness state is cleaned without touching status.

**The web surface holds under load.** 60 concurrent requests over 7 read endpoints answered 200 across the board while serving a populated vault.

## Findings

### F-1 · Lint does not name a source sitting in an unconfigured branch (diagnostic blind spot)

Reproduce: capture a source, move its raw file by hand into `raw/stray-dir/`, run `bk reconcile`. Reconcile heals the registry path faithfully — into a branch with no privacy policy. From then on:

- **every** search/context/graph read refuses closed with `policy_denied: No privacy policy exists for this branch (stray-dir)` — deliberate and correct (`resolve_branch_policy` exists precisely because this used to be a bare `KeyError`);
- but `bk lint` reports **only `views.stale`**. Nothing names the branch, the record, or the remedy. The operator discovers the cause only by running the command that just failed.

The fail-closed refusal is right; the silence around it is not. One stray directory quietly disables all retrieval, and the tool whose job is to say what is wrong stays quiet about the one thing that matters. **Suggested fix:** a mechanical lint finding — `registry.unconfigured_branch`, naming the record's path and hinting `bk file --to <configured>` or adding the branch to policy — so `bk lint` (and therefore `bk status`, which runs lint) surfaces it before the first failed query does.

### F-2 · Views never rewrite a branch map once its last source leaves

`Projections.views()` iterates `status.by_branch`, which omits empty branches — so a branch whose sources were all moved away or forgotten keeps whatever map file an earlier, wider run wrote, indefinitely. This contradicts the function's own stated intent ("Every known branch is rewritten … so a narrower run always overwrites a wider run's rows"): narrowed *consumers* are covered, emptied *branches* are not. Observed as `branches_without_map: []` alongside `by_branch` missing `20-research`. Minor, but it is exactly the stale-derived-file class the manifest design was meant to eliminate. **Suggested fix:** iterate the configured branch list from policy rather than `by_branch`.

### F-3 · `bk graph --json` returns counts, not the graph

The JSON result carries `nodes: 43, edges: 79` while the payload lives only in `graph/graph.json`. Machine consumers that want the graph must read the file anyway, which makes `--json` misleading as a contract. Harness tripped on this twice. Not a defect — but either include the payload under a key or document that `--json` is a receipt.

## Conclusion

Three rounds in, the pattern holds: the privacy boundaries and transactional guarantees are the strongest parts of the system, and what slips is diagnosability (F-1) and derived-file hygiene at the edges (F-2). Both have small, well-scoped fixes.
