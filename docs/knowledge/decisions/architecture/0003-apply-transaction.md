# ADR 0003 — The two-phase commit is its own module, and a crash is a constructor argument

Date: 2026-08-14 · Status: accepted · Decided during the same architecture
review that produced 0001 and 0002, applied to the last of the three: the code
that actually writes `wiki/`.

## Context

`FileVault` is the filesystem adapter, and one of the six things it was doing
was running a two-phase commit. `commit_wiki_batch` was a single 229-line
method — stage every page beside the vault, back up the six state files it may
overwrite, write a journal, replace the pages one at a time announcing each
one, move a raw source, write the registry, write freshness, rebuild the index,
record the result, flip the journal to `committed`, delete the transaction
directory — with `_recover_apply_unlocked` (its undo), `_cleanup_transaction_unlocked`
and `_transaction_move_destination` alongside it. Nothing else in `FileVault`
called any of them and nothing outside called them at all.

**The undo could only be reached by a real crash.** That is not a figure of
speech: rollback runs from `FileVault.__init__`, from the `except` clause of
the method it undoes, and from nowhere else. Reaching a specific branch of it
meant interrupting a live apply at a specific instant. So for the life of the
feature the branches went unexercised, and the one test that existed hand-wrote
a `"version": 1` journal carrying `registry_backup`/`applied_backup` — the
legacy `else` branch, a shape the writer had not produced for two schema
versions. The `backups` list, the `raw_move` reversal, the four phases, the
`inflight` window and the committed boundary were all untested.

**Writing the characterization tests found a data-loss defect immediately.**
`_transaction_move_destination` returns the source path unchanged when a source
is already filed in the branch the move would put it in, and the writer marked
that no-op `moved`. Rollback then evaluated, on a single file reachable by both
names:

```python
elif destination.is_file() and source.exists():
    destination.unlink()
```

Both conditions hold, and the unlink removed the only copy. Nothing restores
it — the transaction directory backs up `wiki/` pages and the six `.brain/`
state files, never anything under `raw/` — and the registry was then put back
pointing at a path that no longer existed, so the loss was silent as well as
permanent. `raw/` is the vault's immutable evidence, the one thing that cannot
be recompiled. The fix (writer stops emitting the record, recovery survives one
an older build wrote) and the reproduction are in
`RawMoveReversalTest.test_move_whose_destination_equals_its_source_must_not_delete_it`.

That defect is the argument of this ADR. It was not subtle, it was not recent,
and it survived because there was no way to ask the question.

## Decision

1. **The engine is `infrastructure/apply_transaction.py`.** `ApplyTransaction`
   owns `commit(plan)`, `recover()`, the journal shape, the transaction
   directory layout, `_transaction_move_destination` and `_cleanup_transaction`.
   The move is a move: every line of behaviour, every comment and every branch
   is the one that was in `FileVault`.

2. **The vault keeps the locks, and keeps the ordering.**
   `FileVault.commit_wiki_batch` still takes `write.lock`, then `registry.lock`,
   then `applied.lock`, then `freshness.lock`, and then delegates;
   `FileVault._recover_apply_unlocked` is the same wrapper for the undo, called
   on open. The registry-before-freshness order is load-bearing and is now
   stated in exactly one place — 0002 documents it from the other side, which is
   why `FreshnessLedger.mark_applied` returns entries for this transaction to
   commit instead of writing them itself. The engine takes no locks of its own,
   ever.

3. **The engine is handed accessors, not the vault.** Ten of them: `resolve`,
   `read_registry`, `write_registry`, `read_state`, `write_state`, `branches`,
   `page_version`, `resolve_record`, `write_json`, `write_text` — plus the root.
   This is not tidiness. Every one of them is an *unlocked* accessor, because
   the caller is already holding the locks; handing over the vault would hand
   over `VaultPort`'s public methods, each of which takes the lock it needs, and
   calling one from inside the transaction would ask for a lock this process
   already holds on another descriptor. `flock` associates a lock with the open
   file description, so the second request would block forever rather than
   notice. The constructor is therefore an audit of what an apply may touch, and
   a future edit that needs something else has to say so in the signature.

4. **A crash is a constructor argument.** `FailurePoint(step, detail=None,
   occurrence=1)` names one of ten checkpoints, and the engine stops there.
   The checkpoints are the moments at which the journal on disk has just been
   brought up to date — `prepared`, `page-inflight`, `page-replaced`,
   `wiki-written`, `raw-move-inflight`, `raw-move-applied`, `state-written`,
   `index-written`, `applied-recorded`, `committed` — so "crashed at X" names a
   state a real crash can leave, not an instant no journal describes. An
   unknown step raises at construction, so a typo cannot produce a test that
   silently never fires.

   The failure raises `InterruptedApply`, which derives from **`BaseException`**
   rather than `Exception`. `commit` rolls back in its own `except Exception`
   clause, and a test that asked for a crash wants the half-finished vault a
   crash leaves behind — not the tidy rollback an error gets. That one choice is
   what lets the new tests drive real wreckage onto disk without patching
   anything, and then trigger recovery the way production does: by opening the
   vault.

   It cannot fire by accident. There is no environment variable and no default:
   `FileVault` constructs the engine with `fail_at=None`, and the only other way
   in is `crashing_at(point)`, which returns a copy and leaves the original
   inert. `tests/test_apply_transaction_seam.py` asserts both.

## Alternatives rejected

- **Leave it in `FileVault` and test through the existing fixture.** The
  fixture (`tests/test_apply_journal_recovery.py`) patches the module's
  `_atomic_json` to record journals and raise at a chosen one, and neutralises
  the rollback so the wreckage survives. It works, and it stays — it is what
  proved this move preserved behaviour. But it can only stop where a journal
  write happens to be, it needs a new predicate for every new moment, and it
  has to disable production code to observe production behaviour. A seam that
  belongs to the engine needs none of that.
- **An environment variable, or a module-level flag.** A crash switch that can
  be set from outside the process is a switch that will eventually be set in
  one. A constructor parameter cannot be reached without writing code that
  names it.
- **Make `InterruptedApply` an ordinary `Exception`.** Then the engine's own
  rollback catches it, the vault is tidy, and every test is testing the
  in-process error path instead of the crash path — which is precisely the path
  that was already covered and precisely not the one that was not.
- **Move the atomic write helpers out of `vault.py` too**, so the engine could
  import them instead of being handed `write_json`/`write_text`. Rejected as
  scope: those helpers are used by a dozen unrelated vault methods, the move
  would touch every one of them, and how this vault makes a write durable is
  vault policy the engine has no opinion about. Being handed the primitive also
  leaves exactly one place to intercept every write an apply performs, which is
  what the existing fixture patches.

## Consequences

- **What is now testable that was not.** Crash-at-each-of-the-four-phases;
  crash after the *n*th of several page replaces, with the vault left as a
  genuine mixture no proposal ever described; crash inside the rename window;
  crash one write before `committed` (rolled back) and one write after it
  (kept); crash during a raw move, announced-but-not-performed and performed.
  Each asserts the same thing in the same words: after recovery the vault is
  indistinguishable from one where the apply was never attempted — pages, the
  three JSON state files, no proposal id consumed, no journal, no transaction
  directory. Deliberately breaking one rollback branch fails 4 of the 14 new
  tests; breaking another fails 7.
- **What still is not.** The seam stops the engine *between* operations, which
  is where a journal exists to be read; it cannot stop the process *inside*
  `os.replace`, inside `shutil.copy2`, or between a write and its `fsync`.
  Torn writes, a full disk mid-copy, and a crash of the recovery pass itself
  remain untested, as does concurrent access from a second process — the four
  locks are asserted by ordering and by review, not by a test.
- `FileVault` drops from 1,399 lines to 1,145 and from six responsibilities to
  five. `commit_wiki_batch` is now readable in one screen and says only what it
  is: the locks, in order, around a delegation.
- Two test targets moved with the code, and only targets:
  `test_apply_journal_recovery.py` neutralises `ApplyTransaction.recover`
  instead of `FileVault._recover_apply_unlocked` (the vault's method is the lock
  around it, so patching the vault would leave the rollback running), and
  `test_apply_plan.py` reads the plan's fields across the pair
  `commit_wiki_batch` + `ApplyTransaction.commit`. No assertion changed and the
  pass count did not move.
