# Later

The batches from the close-out review that are real but individually small, plus
the mechanical sweeps that have never been run.

Full findings and their evidence:
[`04-review.md`](../../work/main-field-audit-remediation/04-review.md).

## Docs truth and UX, from the fresh-user track

Both were raised as groups by the agent that installed `0.6.0` from PyPI and
followed `docs/getting-started.md` literally. They are carried here as groups
because that is how they were found; each needs re-deriving against the tree
before it is worked, since several may be obsoleted by the `0.6.1` items.

| ID | Intent |
|----|--------|
| T1–T7 | Seven docs-truth gaps — places the documentation describes behaviour the installed release does not have. Distinct from the D-series, which are defects in the product rather than in the prose |
| U1–U4 | Four points of onboarding friction. The sharpest: `bk ask` refuses an entire query because BM25 recall brushed a `never-ingest` source — on a question unrelated to the private content, with no next step offered. The refusal is correct; refusing the whole answer with no route forward is the defect |

## Corrections to the record

| ID | Intent |
|----|--------|
| R7 | Correct the comment at `release.yml:92-95`. It says a release "reported Success while publishing nothing"; the complete run history is four runs — `v0.5.0` × 3 failure, `v0.6.0` success — and none has that shape. The **guard stays**: a release step that cannot fail is not a gate. It is the premise in the comment that is stale, and `implementation-log.md` §2.11 already records the correction |

## The formatter sweep

`ruff format` has **never been applied to this repository**: `ruff format --check`
fails on **62 of 78 files** tree-wide, and the check is deliberately outside CI
(`.github/workflows/ci.yml:9`).

This is a single mechanical sweep, not a gap — nothing is wrong with the tree,
and `ruff check` and `mypy --strict` both pass over it today. Worth doing on a
quiet branch with nothing else in the diff, because 62 files will bury any change
it travels with. It was unrunnable at all until the `block-destructive-commands.sh`
false positive was fixed during the remediation, which is why the drift
accumulated invisibly for the life of the repo.

## Deferred decisions

- The remaining low/cosmetic findings from the original field audit that were
  never promoted into the remediation. Several are likely obsoleted by Phases 1–4
  and by the `0.6.1` work; revisit as a batch rather than individually.

---
<!-- doc-tracking -->
- Created: 2026-08-12
- Updated: 2026-08-12 10:08
- Updated: 2026-08-13 13:15
