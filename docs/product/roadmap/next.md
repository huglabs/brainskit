# Next

Phase 3 of the field-audit remediation — the architecture track.
Full context: [`docs/work/main-field-audit-remediation/01-prd.md`](../../work/main-field-audit-remediation/01-prd.md)

Gated on Phase 2 landing green. Largest phase; `F1` is the single biggest item.

| ID | Intent | Size |
|----|--------|------|
| F1 | Native `cycles` (Tarjan) + `diff` (set difference) on brainskit's own `code.json`; drop the `graphify.build` import chain — 1,738 LOC and a hard `networkx>=3.4` pin bought for a 13-line helper. `cluster.py` stays as the only genuine delegation. | large |
| F2 | Move the jsonschema engine out of `domain/` into `application/` — the domain layer's only third-party dependency; every caller is already there. | small |
| F3 | De-triplicate the installer contract. Gate constants: `cli.py` imports from `application/gate.py` (two lines). Only the managed-block sentinel moves downward. | small |
| F4 | Close the layering-test gaps — the dynamic-import scan filtered with `if "codeanalysis" in node.value` closes only the known hole; `ALLOWED` permits `infrastructure → application` more broadly than `architecture.md` shows. | medium |
| B3 | `bk status` must not print `✓ vault healthy` above `✗` enforcement rows. | small |
| E4 | Enforce slug uniqueness at apply, with a lint code — duplicate slugs across page kinds silently mis-route every `[[link]]`, last-writer-wins by directory sort order. | medium |
| E5 | Scoped code-graph builds prune; persist `_unexplained` and `parseable_files` into the artifact so a coverage gap outlives one line of build output. | medium |
| G2 | Rename Neo4j/Postgres objects to brainskit branding (`BrainkitNode` → `BrainskitNode`, `brainkit` schema → `brainskit`) with a migration path for existing subgraphs. Decision: **code follows the docs**; update `CHANGELOG.md:64–67`, which records the opposite. | medium |

---
<!-- doc-tracking -->
- Created: 2026-08-12
- Updated: 2026-08-12 10:08
