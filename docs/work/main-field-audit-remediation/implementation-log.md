<!-- Running log of what was built, and what building it revealed. -->

# Implementation log

Branch `fix/field-audit-remediation`. Baseline at branch point: **911 tests**,
`ruff check` clean, `ruff format --check` failing on 54 of 64 files.

Test command is `uv run python -m pytest`. Bare `python`/`python3` pick up the
machine's global environment, where `deepeval` auto-loads a pytest plugin that
needs pydantic v2 and aborts collection before a single test runs.

---

## Phase 1 — complete

| Task | Requirement | Negative control (reverted → restored) |
|---|---|---|
| 4.7 | unblock the formatter check | 3 fail → 0 fail (harness) |
| 1.5 | one version source | **4 fail** → 4 pass |
| 1.1 | provenance fails closed | **5 fail** → 8 pass |
| 1.2 | Obsidian sync filters | **4 fail** → 6 pass |
| 1.3 | honest session banner | **5 fail** → 6 pass |
| 1.4 | unambiguous `check-write` | **2 fail** → 4 pass |

Every control confirmed the edit had actually landed (`grep -c`) before the run
was trusted, and every control left its in-class *controls* green — otherwise
the test would be asserting "nothing ever works" rather than the fix.

---

## Phase 2 — complete

Suite 939 → **977**. `ruff check` clean. All 11 tasks complete.

| Task | Requirement | Negative control |
|---|---|---|
| 2.1 | `bk graph` filters + stamps a consumer | 3 fail → 4 pass |
| 2.2 | graph and lint agree on unknown sources | 1 fail → 3 pass |
| 2.3 | `strictest_privacy` requires `on_empty` | 6 fail → 7 pass (with 2.8) |
| 2.4 | every code traversal carries staleness | 2 fail → 4 pass |
| 2.5 | `bk init --print-config` | 7 fail → 7 pass |
| 2.6 | `taxonomy_seed` biases filing | 5 fail → 5 pass |
| 2.7 | the false here-doc deleted | covered by 2.5's round trip |
| 2.8 | branch lookup raises `PolicyError` | 6 fail → 7 pass (with 2.3) |
| 2.9 | `search(limit=N)` returns N | 1 fail → 2 pass |
| 2.10 | provider outages are `not_configured` | 3 fail → 6 pass |

### Decisions taken during Phase 2

- **`taxonomy_seed` biases filing** (operator's choice). The `file-proposal`
  job now receives `"seed": true|false` per branch plus the seed list itself, so
  a branch added for one stray document is not an equal candidate to the shape
  the operator declared. The prompt half is blocked — see below.
- **`bk graph` defaults to `local`**, matching every other file target. This is
  a real behaviour narrowing: `test_enrichment`'s fixture needed
  `graph(consumer="human")` to keep seeing its never-ingest node. That is the
  fix working, not a regression.
- **2.8 found a twin the audit did not name.** `filing.py:99` had the same
  `config().branches[...]` subscript as `privacy.py:77`. Both now route through
  `_branch_policy`, and a test asserts `filing.py` contains no direct subscript.
- **`diff` is excluded from 2.4's traversal set.** It re-extracts in order to
  compare, so it needs an extractor and cannot answer from the stored artifact —
  the one traversal that cannot be stale by construction.

### More tautologies caught in my own tests

- 2.9's first fixture had no `[[links]]` between pages, so the graph-expansion
  pass never ran and the off-by-one could not reproduce. It passed against the
  bug. Pages are now created linked.
- 2.9's second fixture then hit `insufficient_novelty` at similarity 1.0 — the
  novelty gate working correctly on six near-identical bodies. The bodies now
  differ.
- 2.10's control asserted a bad `base_url` was `validation_error`. It is already
  `not_configured`, and correctly so — a bad base URL *is* configuration. The
  control was wrong, not the code; it now uses `_validate_consumer` instead.

### 2.11 — the release spike, resolved

**The audit's premise was wrong, and the correction matters.** It reported the
`Release` workflow showing *Success* on `v0.5.0` with nothing published — which
framed this as a silent no-op. It is not. All three `v0.5.0` runs are
**failures**:

```
31566358459  failure  Release  v0.5.0  1m39s
31565072392  failure  Release  v0.5.0  1m27s
31564467740  failure  Release  v0.5.0     9s
```

The audit flagged this as unverified (`gh` was unauthenticated for it too) and
raised it as an open question rather than a finding, which was the right call.

Root cause, from run `31566358459`:

```
X Trusted publishing exchange failure:
  `invalid-publisher`: valid token, but no corresponding publisher
  (Publisher with matching claims was not found)

sub: repo:huglabs@160436971/brainskit@1330481082:environment:pypi
```

`gate` passed; `pypi` failed in 13s at the OIDC exchange; `github release` was
skipped (`needs: publish`). **The workflow is correct.** PyPI simply has no
Trusted Publisher registered for it — and since `brainskit` does not exist on
PyPI at all, this needs a *pending* publisher, created before the first upload:

| Field | Value |
|---|---|
| PyPI project name | `brainskit` |
| Owner | `huglabs` |
| Repository | `brainskit` |
| Workflow filename | `release.yml` |
| Environment | `pypi` |

Created at <https://pypi.org/manage/account/publishing/> while signed in as a
user who may own the name. That is an account action, not a code change, so it
is the operator's to take.

**Guard added regardless** (`release.yml`, `publish` job): after the upload
step, poll `pypi.org/simple/brainskit/` up to six times and fail the job if the
version never appears. Exercised locally on all three shapes — version present
→ pass, a different version present → fail, empty body → fail. A release step
that cannot fail is not a gate, which is the general form of this finding.

### One blocked, and why

- **2.6's prompt half.** `src/brainskit/jobs/file-proposal.md` needs one
  paragraph telling the model what `"seed": true` means, or the flag is inert
  data. The documentation-curation hook refuses edits to it: those files are
  *packaged prompt templates* — `verify-wheel.sh` asserts they ship in the
  wheel — not documentation. Same mis-scoping class as 4.7. Not widened
  unilaterally, because widening a guard to permit my own edit is exactly the
  move that deserves a second pair of eyes.

### Another finding: the test suite pollutes the machine-wide vault registry

`FileVault.initialize` registers every vault in `~/.config/brainskit/vaults.json`,
including the temp-directory vaults the suite creates and discards. The registry
held **26 entries, 15 of them dead paths**. Cleaned to 11 real projects. Tests
should unregister, or `initialize` should take a `register=False` for throwaways.

---

## Phase 3 — 4 of 8

Suite 977 → **996**. `ruff check` clean.

| Task | Requirement | Negative control |
|---|---|---|
| 3.5 (B3) | `bk status` headline means enforcement too | 2 fail → restored green |
| 3.3 (F3) | installer constants have one owner | 1 fail → restored green |

`INSTRUCTION_START`, `INSTRUCTION_END`, `HOOK_SENTINEL` and the gate's deny
prefixes now live in `application/gate.py`; `cli.py` (the writer) and
`health.py` (a reader) both import them. `test_layering.py` asserts each literal
is spelled out in exactly one file, so a future copy fails a test rather than
drifting in silence.

**One consequential test change.** `test_a_stale_projection_is_a_warning_not_an_error`
asserted `status()["healthy"]`, using it as a stand-in for "lint is clean".
`healthy` now also means every non-advisory enforcement layer is live -- true of
a real vault, not of a bare temp directory. Repointed at `lint_errors == 0`,
which is what the test is actually about.

Also landed: **3.2** (jsonschema out of `domain/` -- 98 lines to
`application/schema.py`; `domain/model.py` now imports nothing outside the
stdlib, asserted by a new layering test), **3.4** (both layering-test gaps), and
**3.6** (slug uniqueness at apply plus a `wiki.duplicate_slug` lint code).

3.4's `ALLOWED` narrowing is worth reading before extending it. Infrastructure
may now import `{infrastructure, domain}` plus `application.ports` only; the two
adapters that reach further are named in `DOCUMENTED_EXCEPTIONS` with reasons,
and one of those entries says plainly that it is debt rather than a design.

### Two attempted, reverted, and why

**The `integrations.py` privacy import (part of 3.4).** The architecturally
correct fix is for the application layer to compute the copy predicate and hand
it to the adapter, instead of the adapter importing `application.privacy`.
Attempted: threading a callable through the graph dict broke five tests, because
that same dict also feeds the Neo4j and Postgres adapters and gets serialised.
Reverted. Recorded in `DOCUMENTED_EXCEPTIONS` as debt, which at least makes it
visible -- it was invisible before.

**3.7 (scoped-build pruning).** Implemented `still_on_disk(path)` in
`_merge_scoped` using `vault.code_hash(path)`, with a test. The *control* --
"a file that is out of scope but still exists must be kept" -- failed: the node
kept getting pruned. `code_hash` resolves against `code_root`, which is not
necessarily the base the stored node paths were recorded against, so the prune
would silently delete valid nodes whenever those two differ.

Reverted, because a false prune destroys graph data while the bug it fixes only
produces stale answers -- and 2.4 now discloses staleness on every traversal,
which is what the spec said makes this non-urgent. Whoever picks it up needs to
resolve the path-base question first: prune on a path the graph itself can
confirm, not on a hash lookup with a different root.

This is the second time a control caught a change that all its sibling
assertions were happy with.

### Remaining in Phase 3

3.1 (native cycles/diff, drop the networkx pin) · 3.7 (scoped-build pruning,
with the path-base question resolved) · 3.8 (BrainskitNode rename + migration)

---

## Pre-flight (§0.4, §0.5)

- Branched off `main`.
- **This repo's own write gate was not installed.** `.claude/settings.json`
  registered hooks from `…/hug-collective/.claude/hooks/`, and the local copies
  still carried the pre-rename `brainkit-` names. Reinstalled and verified by
  *exercising* it: a write under `wiki/` exits 2, a write outside the vault
  exits 0, and `bk doctor` reports `a write to wiki/ is refused (exit 2)`.
- **`docs/work/` was gitignored.** `.gitignore:35` was an unanchored `work/`,
  which matches a directory of that name at any depth. Nothing under
  `docs/work/` had ever been tracked — not the README, not `_TEMPLATE`, not this
  log. Anchored to `/work/`; confirmed `.mypy_cache/**/work/` and
  `env/**/work/` stay ignored by their own rules, and that `docs/work` is the
  only directory the change affects.

---

## Findings that were not in the audit

### The formatter check has never passed

Fixing 4.7 made `ruff format --check` runnable for the first time, and it
reports **54 of 64 files would be reformatted** — at pristine `HEAD`, confirmed
in a detached worktree, with `ruff check` passing cleanly. This is the deeper
form of the 4.7 finding: the check was unrunnable, so drift accumulated
invisibly for the life of the repo.

Deliberately **not** swept here — 54 files would bury a five-fix diff. Both
files I touched (`integrations.py`, `test_fix_services.py`) were already in that
set, so this branch adds no new drift. Needs its own task; §0.3's gate cannot
pass as written until then.

### The destructive-command hook under-blocked as well as over-blocked

The harness for 4.7 checks both directions. The shipped pattern
(`mkfs\.|format\s+`) missed `diskutil eraseDisk` entirely — on macOS, the actual
way to wipe a disk. So it refused linters and prose while letting the real thing
through. The narrowed pattern covers `mkfs.`, a command-word token followed by a
drive letter or device path, and `diskutil erase|partition`: 9/9 cases correct.

The RED state was itself the proof — the hook refused the command that *wrote
the test*, because the test data contained the token.

### `_evidence_privacy`'s sibling was already right

`Enrichment.privacy_of` implements the correct rule and is pinned by a test; the
wiki-page classifier did the opposite and had none. Fixing 1.1 meant copying a
rule that was already written down eleven lines away.

### Two of my own tests were tautologies before they were tests

Recorded because the same trap is waiting in every remaining phase:

1. `GateCliPathBaseTest` initially ran against a directory that merely *looked*
   like a vault. Every invocation exited 2 with `Not a brainskit vault` — the
   same exit code the gate uses to deny — so "relative agrees with absolute"
   passed while comparing two identical errors. Fixed by initialising a real
   vault and asserting the output contains no `Not a brainskit vault`,
   `unhandled internal error` or `Traceback`.
2. That same guard then caught a `NameError` (`os` is not imported in `cli.py`)
   which had *also* made both spellings agree, for the second wrong reason.
3. `test_the_version_is_not_a_second_literal_in_the_package` first asserted the
   absence of `"0.5.0"` from `__init__.py`, which held `"0.4.0"` — it passed
   while the bug was present. Rewritten to reject any version-shaped literal.
4. `UnresolvableProvenanceFailsClosedTest` first built a page whose body cited
   the source without containing anything sensitive, so `read_resource` had
   nothing to leak and passed. The page body now carries the substance, which is
   what a page compiled from never-ingest evidence actually looks like.

---

## Consequential changes beyond the spec's letter

- **1.2 landed in `tests/test_fix_services.py`, not `test_fix_integrations.py`.**
  The spec named the latter because that is where the four weak tests live. The
  strong harness (`ServiceFixture`, real vault, `capture_into`/`upsert_page`)
  is in the former. All four weak tests still pass, untouched.
- **1.2's raw-file rule derives the branch from the path**, not from a registry
  lookup. My first version required a registry entry and broke
  `test_raw_is_excluded_unless_requested`, which writes an unregistered file to
  `raw/_inbox/`. A raw file's branch *is* its directory — that is exactly what
  `_record_branch` reads — so an un-reconciled inbox file still has a knowable
  policy. Requiring registration would have over-blocked the one directory files
  arrive in.
- **1.3 removed `WORKSPACE` from the status template.** With enforcement read
  from `bk status`, nothing referenced it, and the comment above it described a
  check that no longer existed there. `test_the_status_hook_looks_for_git_in_the_workspace`
  was rewritten to assert the new contract; the intent it protected (commit lint
  judged against the enclosing repository, not the vault) is covered by seven
  passing tests in `test_enforcement_status.py`.
- **1.3 made the session banner report three layers**, not two. `session_status`
  was always in the JSON and never rendered.
- **1.4 resolves in the interface layer.** `check_write`'s vault-root-relative
  contract is unchanged and still correct for the installed hook, which always
  passes absolute paths. Only `cli.py`, where a shell context exists, applies
  the caller's directory.

---

## Still open

- **2.11 (release spike) is unstarted.** `gh` is unauthenticated in this
  environment, so the `v0.5.0` run logs remain unread. This blocks 4.6.
- **`__version__` now reports 0.5.0**, matching the distribution. The bump to
  0.6.0 is task 4.6 and deliberately not done.

---
<!-- doc-tracking -->
- Created: 2026-08-12
- Updated: 2026-08-12 12:36
- Updated: 2026-08-12 14:49
- Updated: 2026-08-12 15:05
- Updated: 2026-08-12 15:13
