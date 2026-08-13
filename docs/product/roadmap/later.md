# Later

Phase 4 of the field-audit remediation (docs truth + the release), plus the
low/cosmetic findings that do not block `0.6.0`.
Full context: [`docs/work/main-field-audit-remediation/01-prd.md`](../../work/main-field-audit-remediation/01-prd.md)

## Phase 4 — docs truth, then ship

| ID | Intent |
|----|--------|
| G1 | Correct "read-only" in `docs/serving.md:24` and `docs/architecture.md:4` — `do_POST` handles four write endpoints. Document `/api/code-graph` (real GET surface is 11, not 10) and that the bearer token is optional. |
| G3 | Help text for the 26 flags that carry none; mention `--json` and `--vault` across the 32 subcommands. |
| G4 | `--force`'s help promises an unimplemented guard against initialising over other projects — implement or correct. See the incident at `codegraph.py:226` (a vault "merely holding projects" indexed 55,295 files). |
| F5 | Remove the 1,938 LOC of precisely-measured dead vendored regions; update `codeanalysis/NOTICE` and keep `test_vendoring.py:40`'s pin honest. |
| C3 | Publish `0.6.0` to PyPI; README install lines and badges become true; cut the GitHub Release. |
| C4 | Make `SECURITY.md`'s "latest release only" satisfiable. |

## Unblocked ride-alongs

| Intent |
|--------|
| `bk capture` gets a human renderer — it is the second command in every quickstart and dumps raw JSON (D4). |
| Apply refusals name the next command; `missing_base_hash` says that `observed` is the value to paste (D4). |
| Fix the `block-destructive-commands.sh` false positive: the string `format` blocks `ruff format --check`, a standard formatting check the audit could not run. |

## Deferred decisions

- The remaining low/cosmetic findings from the audit that are not listed above.
  Revisit after `0.6.0` ships; several may be obsoleted by Phases 1–3.

---
<!-- doc-tracking -->
- Created: 2026-08-12
- Updated: 2026-08-12 10:08
