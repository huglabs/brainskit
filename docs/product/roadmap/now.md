# Now

Everything here is Phase 1 and Phase 2 of the field-audit remediation.
Full context: [`docs/work/main-field-audit-remediation/01-prd.md`](../../work/main-field-audit-remediation/01-prd.md)
Evidence: [brainskit Field Audit](https://claude.ai/code/artifact/6d5de80a-c222-4c63-ac52-6244f1e56b4b) (`0.5.0` @ `8216766`)

## Phase 1 — stop the leak, stop the lying

| ID | Intent | Where |
|----|--------|-------|
| A1 | Unresolvable provenance must resolve to `never-ingest`, not `cloud` | `application/privacy.py:94–121` |
| A2 | Obsidian sync must filter `wiki/` and `raw/`, not only the graph object | `infrastructure/integrations.py:256–270` |
| B1 | SessionStart hook must read `enforcement.layers[]` it already holds | `templates/agents/brainskit-status.sh:83–96` |
| B2 | `bk gate check-write` must not answer "allowed" for a gated page | `application/gate.py:172–173` |
| C1 | One source of version truth, asserted in CI | `__init__.py:3` · `pyproject.toml:10` · `release.yml:41` |

## Phase 2 — honest answers and the way in

| ID | Intent | Where |
|----|--------|-------|
| A3 | `bk graph` stamps a consumer and filters, like every sibling path | `projections.py:143–161` |
| A4 | One answer for an unknown `sourced_from` hash, not two | `graph.py:45` vs `health.py:310` |
| A5 | Make `strictest_privacy`'s asserted invariant enforced | `application/privacy.py` |
| B4 | Every `bk code` traversal carries a staleness signal | `application/codegraph.py:874–894` |
| D1 | `bk init --print-config` — unblock CI, containers, agents | `interfaces/onboarding.py:464–522` |
| D2 | Implement `taxonomy_seed` (5 write sites, 0 readers) | `domain/model.py:746–860` |
| D3 | Delete the here-doc promise that does not exist | `docs/getting-started.md:29` |
| E1 | `branches[branch]` → `.get()` + `PolicyError` (bare `KeyError` escapes 4 read paths) | `application/privacy.py:77` |
| E2 | `search(limit=N)` returns N, not N+1, for N < 4 | `retrieval.py:88–90` |
| E3 | Provider outages are `NotConfiguredError`, not `validation_error` | `llm.py:601,617` · `integrations.py` |
| C2 | Diagnose the release workflow reporting Success while publishing nothing | `.github/workflows/release.yml` |

**Gate before Phase 3:** suite green · `ruff` clean · `bk lint` clean · every
negative control demonstrated failing-then-passing.

---
<!-- doc-tracking -->
- Created: 2026-08-12
- Updated: 2026-08-12 10:07
