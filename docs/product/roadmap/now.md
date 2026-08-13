# Now

The field-audit remediation is **complete and closed** — see
[`04-review.md`](../../work/main-field-audit-remediation/04-review.md) for the
four-track review that closed it, and
[`01-prd.md`](../../work/main-field-audit-remediation/01-prd.md) for the
programme it reviewed.

Everything below is what that review left open, ordered by how much it costs a
user who is running the published release today.

`RV*` are findings the close-out review raised that carry no identifier from the
original audit. Every other identifier is the one used in `04-review.md`.

## Ship the fixes

**`0.6.0` as published still contains every defect fixed on 13 Aug.** The two
criticals below were introduced *by* that release. Closing the work folder
recorded the work; a release is what reaches users.

| ID | Intent | Where |
|----|--------|-------|
| C5 | Cut **`0.6.1`**: the scoped-build prune, the `proposal_id` contract change, the licence files, the malformed-graph refusal and `verify-wheel.sh`'s isolation | `CHANGELOG.md` `[Unreleased]` · `.github/workflows/release.yml` |

The Trusted Publisher is registered and `v0.6.0` published cleanly, so this is a
tag push rather than a spike. Fold R4 in first if it is cheap — the visibility
guard is what would catch a partial upload.

## Surfaces that still report what they have not checked

The same root cause the whole programme was about. Each of these is live in the
published release.

| ID | Intent | Where |
|----|--------|-------|
| RV1 | `graph/graph.json` freshness must read the file, not stat it — it reports `fresh` for a file that is not JSON at all, verified by overwriting one with `{{{ not json at all`. Direct twin of the `code.json` state just fixed | `application/health.py:563-587` |
| D1 | The `bk status` headline must mean what `healthy` means — a fresh vault created by the documented quickstart prints `✗ 0 lint error(s)`, permanently, because `bk init` outside a git repo can never make `commit_lint` active | `health.py:185` · `cli.py:2785` |
| P2 | `bk lint` must cover `wiki/index.md` and `wiki/log.md`. They have no `freshness.json` entries (7 entries, 9 pages), so `wiki.outside_apply` cannot fire for them — and the gate hook's header comment cites lint as the backstop *justifying* failing open | `application/health.py` · `templates/agents/brainskit-gate.sh` |
| D3 | `bk watch` must resolve its source folder against the vault, not the current directory. A relative path captures nothing, exits 0 and reports `created 0`; neither `status` nor `lint` mentions it, and `bk schedule` emits cron lines that run from `$HOME` | `application/services.py:155` |

## The upgrade path

| ID | Intent | Where |
|----|--------|-------|
| P1 | `bk hooks install --force` must migrate the pre-rename `brainkit-*` hooks. `_prune_stale_hook_entries` keys on the current template name, so the old entries are invisible to it: the old gate stays registered beside the new one and the project ends with **two** managed CLAUDE.md blocks. Every user upgrading from a pre-rename install lands here | `interfaces/cli.py` `_prune_stale_hook_entries` |

## The error taxonomy, finished

0.5.0's additive-subclass strategy was correct and is not in question. These are
the two places it was not carried through.

| ID | Intent | Where |
|----|--------|-------|
| S2-5 | Stop re-raising `ValidationError` plain — it downgrades the `NotConfiguredError` that `_docker` raises, so the same Docker outage is `not_configured` directly and `validation_error` through `bk integration up postgres`, which is the path a user takes. **A regression the subclass strategy introduced** | `infrastructure/integrations.py:648` · `:1305` |
| S2-4 | Code `HTTPError` by status — 401, 404, 429 and 5xx are all `validation_error` today, twenty lines above a comment making exactly the opposing argument for `URLError`. `tests/test_provider_outage_codes.py` contains no HTTP-status case at all | `infrastructure/llm.py` · `tests/test_provider_outage_codes.py` |

**Gate before moving on:** suite green · `ruff check` clean · `mypy --strict`
clean · `bk lint` clean on `docs/brain` · every negative control demonstrated
failing-then-passing, with `brainskit.__file__` asserted before the result is
trusted.

---
<!-- doc-tracking -->
- Created: 2026-08-12
- Updated: 2026-08-12 10:07
- Updated: 2026-08-13 13:15
