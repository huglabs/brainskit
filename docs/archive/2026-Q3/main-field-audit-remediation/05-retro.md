<!-- Stage 05. Retrospective — capture durable learnings, then promote them to knowledge/ or vault/. -->

# Retro — field audit remediation → brainskit 0.6.0

## What went well

- **The negative control worked as a requirement, not a convention.** Every task
  in `implementation-log.md` carries its reverted-then-restored numbers, and the
  discipline caught real defects rather than decorating finished work: task 3.7
  was reverted the first time *because its own control failed*, and four of this
  cycle's controls exposed broken tests rather than broken code.
- **Reproductions beat readings, repeatedly.** The two data-leak criticals, the
  `bk gate check-write` ambiguity, the `--force` guard that turned out to already
  exist, the `proposal_id` remedy classification, and the 805-node prune were all
  settled by running something, not by arguing about the source.
- **Four premises were wrong, and each correction was recorded rather than
  quietly absorbed** — the release "silent no-op" (§2.11), the `networkx`
  "mandatory pin" (§3.1), the dead-vendored-regions list (§4.5), and `--force`'s
  unimplemented guard (§4.3). Add R7 from this review, which is the same premise
  as §2.11 surviving in a workflow comment. An audit that is 4-for-N wrong on its
  premises is still a good audit; an audit whose wrong premises are executed
  unchecked would have broken extraction.
- **The four-track review was worth its cost.** No single track found the
  critical prune: Track 3 read the code and did not flag it, Track 1 never
  exercised a scoped build. It surfaced from re-verifying a task that the
  implementation log had already marked complete with a passing control.

## What didn't

- **A completion log is not a verification.** 3.7 was closed with a control, a
  test, and a written argument for why the argument held — and shipped a critical
  data-loss bug. The gap was that the argument's scope was never stated.
- **Three agents stalled mid-verification**, leaving edits landed and controls
  unproven. One had explicitly flagged its own controls as depending on lucky
  ordering. Resuming those claims would have inherited unproven work.
- **The suite's green was doing less work than its count implied.** 14 production
  behaviours could be neutralised with 1051 tests passing. The programme raised
  the count by 192 tests and did not raise the floor.
- **Docs generated the bug.** The `proposal_id` loop came out of
  `instructions.md`, which writes the managed CLAUDE.md block in every user's
  project. The programme treated documentation as a downstream artifact to
  reconcile (Track G) rather than as executable instruction with the same defect
  classes as code.

## Learnings to promote

Durable insights that should graduate out of `work/` into their permanent home
(`docs/knowledge/`, `docs/product/`, or the brainskit vault via `bk capture`):

**Defect classes — belong in `AGENTS.md` alongside the existing record, because
each is a shape to search for, not a story:**

1. **A correctness argument must name its scope.** 3.7's "shared by construction"
   was true within one call and false across calls, because `code_root()`
   re-resolves and the artifact recorded no `code_root`. The question that would
   have caught it: *over what extent does this invariant hold, and what
   re-resolves outside it?* An invariant claimed without an extent is a guess
   with good grammar.
2. **A fixture fix can delete the only configuration in which the bug is
   reachable.** 3.7's control failed; the fixture was moved so its files sat
   inside the vault; the move removed the diverging-base case entirely, and the
   control then passed for a reason unrelated to the code. **When a control
   fails, establish whether the fixture or the code is wrong *before* changing
   either** — changing the fixture first converts a finding into a false
   negative.
3. **An editable-install `.pth` silently invalidates scratch-copy controls.** It
   defeated four separate controls in this cycle; two agents caught themselves
   and redid the work. **Always assert `brainskit.__file__` before trusting a
   control result.** Related and separately load-bearing: `bk` on `PATH` is a
   distinct `uv tool` install, not the working tree.
4. **A control that passes when it should fail means the control is broken.**
   Four instances this cycle, including one where a renderer already emitted the
   expected string incidentally, so the assertion could not discriminate. Treat a
   green control run as a *finding about the control* until the revert is
   confirmed to have landed (`grep -c`).
5. **A green suite is a starting point, not a result.** Technique that found the
   14: for a test asserting an invariant, ask what edit would make it fail — and
   if the answer is "none", say so. The specific shape to hunt: **assertions
   reachable only through a `for` loop over a production-controlled collection
   that is empty in the fixture.** 84 such sites across 33 files.
6. **Interrupted agents leave code without proof.** Re-verify from scratch rather
   than resuming a claim; an unfinished verification is not a partial
   verification, it is an unverified change with a confident log entry.
7. **`tests/test_gate.py` is the pattern to copy.** Its `run_cli` refuses to
   proceed when the output contains `"Not a brainskit vault"` or `"Traceback"` —
   the vacuity guard the rest of the suite lacks. Two tautologies in this
   programme (`GateCliPathBaseTest`, and the `NameError` it then exposed) were
   caught by exactly that guard.

**Product-level, belongs in `docs/product/`:** the four-track review should be a
release gate rather than a one-off event. Tracks 1 and 2 (fresh-user install,
enforcement harness) found defects that no amount of source reading produced, and
both are cheap to re-run against a candidate wheel.

**Vault-worthy via `bk capture`:** the `proposal_id` classification experiment —
5 refused cycles under `conflict`'s remedy versus success with a new id — is the
kind of empirical answer that is expensive to re-derive and easy to re-argue
wrongly from the class hierarchy.

## Follow-ups

Everything still open from the review is mapped on the roadmap rather than
duplicated here:

- [`docs/product/roadmap/now.md`](../../product/roadmap/now.md) — **0.6.1**,
  because `0.6.0` as published still contains every defect fixed today; plus P1,
  P2, D1 and the two taxonomy miscodings.
- [`docs/product/roadmap/next.md`](../../product/roadmap/next.md) — the honesty
  and diagnostics work (P3, P4, D2–D5, D8), the release-pipeline hardening (R1,
  R4, R5), the dependabot queue with #1 flagged unsafe, and the test-vacuity
  sweep.
- [`docs/product/roadmap/later.md`](../../product/roadmap/later.md) — the
  docs-truth and UX batches (T1–T7, U1–U4) and the one-off `ruff format` sweep.

Full findings and their evidence: [`04-review.md`](04-review.md).

---
<!-- doc-tracking -->
- Created: 2026-08-13 13:13
