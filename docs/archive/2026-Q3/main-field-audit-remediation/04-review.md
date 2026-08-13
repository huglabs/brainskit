<!-- Stage 04. Quality review before merge. -->

# Review — field audit remediation → brainskit 0.6.0

## Scope reviewed

A four-track end-to-end review of **brainskit 0.6.0** — published on PyPI, tagged
`v0.6.0`, commit `f04a0edf7196dc018c3f672c0bd3a5bed7445633` — run by parallel
agents, followed by nine fixes and an adversarial verification pass over them.

The tracks were chosen to attack the release from four directions that do not
share assumptions:

| Track | Question | Method |
|---|---|---|
| 1 | Does a fresh user succeed? | Install `0.6.0` from PyPI into a clean venv, follow `docs/getting-started.md` literally |
| 2 | Do the enforcement layers actually enforce? | Break each layer deliberately in throwaway vault copies, then ask the reporting surfaces |
| 3 | Is the code and the test suite what it claims? | Layering scan, vendoring audit, targeted neutralisation of production behaviour against the green suite |
| 4 | Is the artifact people install the artifact we built? | Wheel-vs-tag byte comparison, provenance chain, workflow and repository settings |

Working state at the end of this review: **1103 tests passing** (1051 at the
start of the day), `ruff check` clean, `mypy --strict` clean across 39 files,
32 changed paths in `git status`.

`ruff format --check` fails on **62 of 78 files** tree-wide. This is
pre-existing and deliberately outside CI (`.github/workflows/ci.yml:9`); it is
not a regression from this work and is tracked on `later.md`.

---

## Findings

Severity key: **S1** critical (data loss or an unclearable agent loop) · **S2**
must fix before the next release · **S3/P/D/T/U/R** everything else, ordered by
the track that found it.

| Severity | Finding | Status |
|----------|---------|--------|
| S1 | Scoped `bk code build` prunes live nodes — 805 destroyed on this repo's own graph | **fixed** |
| S1 | `proposal_id` reuse coded `conflict`, whose remedy never clears | **fixed** |
| S2-1 | Wheel and sdist shipped 43 MIT-covered vendored files with no `LICENSE-MIT` and no vendored `NOTICE` | **fixed** |
| S2-4 | Every `HTTPError` — 401, 404, 429, 5xx — coded `validation_error` | open |
| S2-5 | `integrations.py` re-raises `ValidationError` plain, downgrading `NotConfiguredError` | open |
| S2-6 | `tests/conftest.py`'s registry isolation is bypassed by `python -m unittest` | open |
| S3 | `templates/web/three.min.js` (three.js r148) absent from `NOTICE` | **fixed** |
| — | Malformed stored code-graph edges laundered through `_merge_scoped` | **fixed** |
| — | `bk code status` blessed a graph every other command refuses | **fixed** |
| — | `file-proposal.md` never explained `seed`; `{{taxonomy_seed}}` passed and unread | **fixed** |
| — | Test suite polluted the machine-wide vault registry | **fixed** |
| — | `md-timestamp-tracker` / `knowledge-structure-enforcer` mis-scoped on packaged templates | **fixed** |
| — | The `proposal_id` instruction in the generated CLAUDE.md block caused the loop in S1-2 | **fixed** |
| P1 | `bk hooks install --force` cannot migrate the pre-rename `brainkit-*` hooks | open |
| P2 | `bk lint` does not cover `wiki/index.md` or `wiki/log.md` | open |
| P3 | The `instructions` enforcement layer is a sentinel-presence check rendered as a real row | open |
| P4 | `bk doctor`'s `healthy` is dominated by missing tree-sitter grammars | open |
| D1 | `bk status` headline reads `✗ 0 lint error(s)` for the documented quickstart | open |
| D2 | `bk forget --force` is undone by the `bk reconcile` that `bk lint` recommends | open |
| D3 | `bk watch` silently captures nothing when its source folder is a relative path | open |
| D4 | `bk code build` on a fresh vault graphs only the vault's own hook scripts | open |
| D5 | A hint names `--code-only`, a flag no command accepts | open |
| D8 | `bk lint --json` emits `{"ok": true, "result": {"ok": false}}` | open |
| T1–T7 | Docs-truth gaps found by the fresh-user track | open |
| U1–U4 | UX friction found by the fresh-user track | open |
| R1 | `SECURITY.md`'s only sanctioned reporting channel does not exist | open |
| R4 | The PyPI-visibility guard matches the version as a substring | open |
| R5 | Publishing is unattended — no required reviewers on `environments/pypi` | open |
| R7 | The workflow comment's premise is not what the run logs show | see below |
| — | 14 production behaviours neutralised with the full suite still green | open |
| — | `health.py:563-587` reports `fresh` for a `graph/graph.json` that is not JSON | open |

Everything marked open is carried on `docs/product/roadmap/{now,next,later}.md`
with the same identifiers.

---

### Track 1 — the fresh-user end-to-end

Installed `0.6.0` from PyPI into a clean venv and followed
`docs/getting-started.md` literally. The verdict was **a genuinely good first
ten minutes, then a wall of small dishonesty.**

Confirmed working, by exercise rather than by reading:

- the write gate refuses and writes nothing;
- `bk lint` catches both out-of-gate wiki edits and raw tampering;
- `bk reconcile` heals a moved source by hash;
- `never-ingest` evidence is excluded from `local` and from `cloud`, with only a
  count reported;
- `bk doctor`'s probe actually executes the hook;
- MCP over stdio served 17 tools, honouring `--consumer`.

Defects:

- **D1 — the documented quickstart's default outcome is a permanent red ✗.**
  The wizard's own three "Next" commands all fail from the directory it leaves
  you in, and the first `bk status` a fresh user runs prints
  `✗ 0 lint error(s)`. Cause: `health.py:185` widened `healthy` to include the
  enforcement layers, `cli.py:2785`'s headline was never widened with it, and
  `bk init` in a non-git directory can never make `commit_lint` active.
  Proven causal, both directions: the same vault inside a git repo prints
  `✓ vault healthy`; disabling the pre-commit hook returns it to `✗`.
- **D2 — `bk forget --force` is undone by the next documented step.** `bk lint`
  tells you to run `bk reconcile`; `bk reconcile` re-registers the record that
  `forget` dropped.
- **D3 — `bk watch` silently captures nothing** when its configured source
  folder is a relative path and the process is not standing in the vault:
  `services.py:155` resolves it against the current directory rather than the
  vault. Exit 0, `created 0`, and neither `bk status` nor `bk lint` mentions it.
  This matters more than it reads, because `bk schedule` emits cron lines and
  cron runs from `$HOME`.
- **D4 — `bk code build` on a fresh vault graphs only the vault's own hook
  scripts**, which `docs/code-graph.md` explicitly says are excluded.
- **D5 —** a hint names `--code-only`, a flag no command accepts.
- **D8 —** `bk lint --json` emits `{"ok": true, "result": {"ok": false}}`.

Also raised: docs-truth gaps **T1–T7** and UX friction **U1–U4**. The sharpest
of the latter is `bk ask` refusing an entire query because BM25 recall brushed a
`never-ingest` source — on a question unrelated to the private content, with no
next step offered. These are recorded as a group; they were not individually
re-derived in this write-up and are carried to the roadmap as a batch.

### Track 2 — the enforcement harness

Every layer was broken deliberately in throwaway copies rather than reasoned
about.

**The write gate is genuinely enforcing.** A 14-path matrix — including `..`
traversal, relative paths, and a symlink pointing into the vault — produced the
correct verdict in every case. `bk doctor`'s probe caught **all four** fail-open
modes: `bk` off `PATH` → `not_enforcing`; `chmod -x` on the hook → `unknown`;
the hook deleted → `absent`; the hook unregistered → `gated=false`.

Product defects:

- **P1 — `bk hooks install --force` cannot migrate the pre-rename brand.**
  `_prune_stale_hook_entries` keys on the *current* template name, so
  `brainkit-*` entries are invisible to it. Reproduced on an isolated throwaway:
  the old gate stays registered beside the new one, and the project ends with
  **two** managed CLAUDE.md blocks. Every user upgrading from a pre-rename
  install lands here.
- **P2 — `bk lint` does not cover `wiki/index.md` or `wiki/log.md`.** They have
  no `freshness.json` entries — 7 entries against 9 pages — so `wiki.outside_apply`
  can never fire for them. This is worse than an ordinary coverage gap because
  the gate hook's own header comment cites `bk lint` as the backstop that
  *justifies* failing open.
- **P3 — the `instructions` layer is a sentinel-presence check.** It passes with
  an empty managed block, with a block naming a different vault, and with a block
  instructing the agent to write the wiki directly. It is rendered as a
  `✓ active` row indistinguishable from the three layers that are real.
- **P4 — `bk doctor`'s `healthy` is dominated by missing tree-sitter grammars.**
  On a default install (no `code` extra) it is permanently `False`, so a genuine
  enforcement regression changes nothing a reader can see.

### Track 3 — architecture, code and test quality

The suite is green and reproducible. The layering rules genuinely hold under a
correct import resolver: **0 violations, 0 cycles**, and all three
`DOCUMENTED_EXCEPTIONS` entries are live rather than stale. The 4.5 vendored
deletion is clean — 43 modules, 27,618 lines, **0 unreachable**.

**But 1051 green was a weaker signal than it read.** Fourteen targeted
neutralisations of production behaviour survived the full suite. Named examples:

- `bk status`'s source count was protected by nothing.
- `_merge_scoped`'s dangling-edge guard had no test at all — its
  similarly-named test executes its loop body **zero times** on the fixture.
- `test_vendoring.py:67` asserts the *absence* of a string that appears nowhere
  in the file, so it cannot fail under any edit to `NOTICE`.
- `test_code_grammars.py:229` asserts the opposite of what its name says.
- Two tests whose docstrings promise discrimination do not discriminate.

Error-taxonomy findings, both consequences of the additive-subclass strategy
introduced in 0.5.0:

- **S2-4 —** every `HTTPError` (401, 404, 429, 5xx) is coded `validation_error`,
  twenty lines above a comment making exactly the opposing argument for
  `URLError`. `tests/test_provider_outage_codes.py` contains no HTTP-status case
  at all. **Not fixed.**
- **S2-5 —** `integrations.py:648` and `:1305` catch `ValidationError` and
  re-raise it plain, silently downgrading the `NotConfiguredError` that `_docker`
  raises. The same Docker outage is therefore `not_configured` when reached
  directly and `validation_error` through `bk integration up postgres`, which is
  the path a user actually takes. **Not fixed.**
- **S2-6 —** `tests/conftest.py`'s registry isolation is bypassed entirely by
  `python -m unittest`, which **29 of 36** test files support. **Not fixed.**

### Track 4 — release and supply chain

**The published wheel is byte-identical to the `v0.6.0` tag** across all 100
payload files, and the provenance chain closes end to end: download sha == PyPI
digests == in-toto attestation subjects == GitHub Release assets.

- **S2-1 — the wheel and sdist shipped 43 MIT-covered vendored files**
  (© Safi Shamsi) without `LICENSE-MIT` and without the vendored `NOTICE`. The
  root `NOTICE` that *is* distributed pointed at `src/brainskit/…`, a path that
  does not exist in an installed copy, and `LICENSE` was still the unfilled
  Apache template. **Fixed** — see fix 5.
- **S3 — `templates/web/three.min.js`** (three.js r148, 608 KB, served at
  `interfaces/web.py:271-272`) was a second vendored third party absent from
  `NOTICE`. **Fixed** in the same pass: it is now declared at `NOTICE:35-47`
  with both the repository and installed paths.
- **R1 — `SECURITY.md`'s only sanctioned reporting channel does not exist.**
  `private-vulnerability-reporting` is `{"enabled": false}` while the policy
  forbids public issues, so a reporter following it has nowhere to go. Secret
  scanning and Dependabot security updates are also disabled. This is a
  repository setting, not code. **Not fixed.**
- **R4 —** the PyPI-visibility guard is a real gate that can fail, but its
  `case "$body" in *"brainskit-$version"*` is a substring test (`0.6` matches
  `0.6.0`), and it asserts that *a* file exists rather than that both the wheel
  and the sdist landed. **Not fixed.**
- **R5 —** publishing is unattended: `environments/pypi` has no required
  reviewers, and any `v*` tag push publishes. **Not fixed.**
- **R7 — record this one carefully.** The workflow comment at
  `release.yml:92-95` says a release "reported Success while publishing
  nothing". The complete run history is four runs — `v0.5.0` × 3 failure,
  `v0.6.0` success — and none has that shape. The guard added in response is
  good hardening and should stay; the *premise* is not what the logs show.
  `implementation-log.md` §2.11 already records the spike as resolved on exactly
  this point, so the stale claim is the comment, not the investigation.

Dependabot queue: **four of the five red X's are a billing lock, not test
failures** — those jobs ran for 2s with zero steps. Only #3 is genuinely green.
**#1 (tree-sitter 0.26) must not be merged as-is**: it removes `Language.query`,
`Language.version` and `Parser.timeout_micros`, and makes `Point` a tuple
subclass — and the vendored extractors that would break are excluded from both
`ruff` and `mypy`, so nothing in CI would catch it.

---

## The nine fixes

Each fix landed with a negative control: revert the change, confirm the named
tests fail, restore, confirm they pass.

**1 — `file-proposal.md`'s missing half (task 2.6).** The prompt now explains
`seed` as a tie-breaker. `{{taxonomy_seed}}` was passed to the template and
never referenced; it was **dropped rather than wired**, because it is derivable
from the per-branch flag and `from_dict` does not constrain it to be a subset of
`branches`. A contract test now pins that every variable passed is read and every
placeholder is supplied. Control: 3 fail → 6 pass.

**2 — test-suite registry pollution.** `tests/conftest.py` sets
`XDG_CONFIG_HOME` at import. One premise correction worth recording:
`FileVault.initialize` does **not** register — the write site is
`interfaces/cli.py:1297` `_register_new_vault`, inside the `bk init` handler. The
`register=False` affordance originally proposed in `implementation-log.md` would
have guarded nothing. The registry was byte-identical across a 377-test run.
Control: 2 fail → 2 pass.

**3 — S1-1, the scoped-build prune (CRITICAL).** Task 3.7's reinstatement
argument — that `code_hash` and `staleness()` share a path base *by
construction* — is **true within a single build and false across builds**:
`code_root()` re-resolves on every call, and the stored artifact recorded no
`code_root` to compare against. Verified before and after on three triggers,
including this repository's real `code.json`: pre-fix **2364 → 1559 nodes, 805
destroyed**; post-fix **2364 → 2441**, with `prune_skipped` and
`stored_code_root: null` reported. Keeping is now the default, pruning requires
positive evidence, and drift is disclosed as `stale` instead of reported
`fresh`. Two adjacent bugs fell out of the same change: `build .` scoping to
nothing, and edges with an empty `path` being pruned while both endpoints were
alive.

**4 — S1-2, `proposal_id` reuse coded `conflict` (CRITICAL).** Classification
was settled empirically against unmodified code, not by reading: `conflict`'s
remedy (re-read, rebuild, retry with the same id) refused on all **5** cycles,
while a new id — or omitting it — succeeded. It is now `validation_error`, via a
shared `proposal_id_reuse_error()` used by both `compilation.py:63` and the
locked twin at `vault.py:664`, with a hint naming the remedy. `ConflictError`'s
docstring was corrected: it listed this case as canonical six lines below
stating the criterion the case fails. Before: 5 retries, never clears. After:
converges in 3. Controls: 5 of 8 fail; with only the twin reverted, exactly 1
fails.

**5 — S2-1, licence attribution.** `LICENSE-MIT` and the vendored `NOTICE`
(`src/brainskit/infrastructure/codeanalysis/`) now ship in **wheel and sdist**,
asserted by `verify-wheel.sh`. The decisive control was control C: revert the
`package-data` glob while leaving both files on disk → `exit 1, missing packaged
resources`. A real behavioural consequence closed with it (S3-8): the AST cache
marker in an installed wheel went `unversioned` → `d763d836bdf3a031`.
`test_vendoring.py`'s three blind spots now each fail when neutralised alone.

**6 — H5, the packaged-template hook mis-scoping.** `knowledge-structure-enforcer.sh`
exempts `src/**/templates/agents/`; `md-timestamp-tracker.sh` excludes `src/`
wholesale. The two guards want opposite boundaries **on purpose**: the enforcer
*routes*, so it must stay narrow, while the tracker *mutates*, and no `.md` under
`src/` should ever receive a footer — a stray one cannot exist, because the
enforcer blocks its creation. Both are anchored at the project root, so
`docs/src/note.md` still gets its footer.

**7 — the `proposal_id` instruction itself.** `claude-skill.md` and
`instructions.md` corrected. The latter is the root fix, because it generates
every user's CLAUDE.md block. "Retries carry a stable `proposal_id`" was
precisely the instruction that steered agents into the unclearable loop in fix 4.

**8 — malformed stored edges.** The fault was **eight JSON-valid shapes**, not
one field. The decision was to **refuse, not normalise**: repairing a missing
`type` would *fabricate a relation*, because `bk code affected` renders it as
`via: <type>`. The check sits at the boundary in `_read` (covering all seven
traversal commands) and in `_merge_scoped` (stopping the laundering). A full
`bk code build` never merges, so the remedy — rebuild — stays reachable by
construction. `verify-wheel.sh` also gained `XDG_CONFIG_HOME` isolation, placed
*after* the `uv` steps, since `uv` reads its own configuration from that variable.

**9 — `bk code status` reports `malformed`.** It had been blessing a graph that
every other command refuses. The rule is now stated in the docstring: `status`
says `malformed` exactly when `_read` would refuse, and `missing` exactly when
`_read` would raise not-found. It returns `ok: true` and exit 0 — consistent with
`stale` and `missing`, and deliberately so: a non-zero exit would break scripts
that run `bk code status` to decide whether to rebuild.

---

## Acceptance-criteria check

Judged against `01-prd.md` §8.

- [x] `_evidence_privacy` returns `NEVER_INGEST` for unresolvable provenance.
- [x] Obsidian sync with `consumer="cloud"` writes no file containing the secret,
      with and without `include_raw`.
- [x] `brainskit-status.sh` agrees with `bk status` in the Husky case and the
      hooks-present-but-unregistered case.
- [x] `bk gate check-write` returns the same verdict for a relative and an
      absolute path.
- [x] Version identity is single-sourced and CI fails on divergence.
- [x] `bk init ./v --config <(bk init --print-config)` succeeds off a TTY.
- [x] `taxonomy_seed` has a reader reachable from `bk ingest` — **and, as of fix
      1, a prompt that explains it.** It was a reader with no meaning until today.
- [x] No `bk code` traversal answers without a staleness signal.
- [x] `search(limit=N)` returns exactly N for N ∈ {1,2,3}.
- [x] `networkx` is not a required dependency; `graphify.build` is not imported
      at module level.
- [x] Neo4j/Postgres object names match `docs/integrations.md`, with a migration.
- [x] `pypi.org/simple/brainskit/` returns 200; `uv tool install brainskit`
      installs `0.6.0`. Track 4 goes further: the wheel is byte-identical to the
      tag across all 100 payload files.
- [~] **Every issue's negative control demonstrated.** Met per task — every entry
      in `implementation-log.md` carries its reverted-then-restored numbers. But
      Track 3 showed the *suite around them* is weaker than the count suggests:
      14 production behaviours were neutralised with 1051 tests still green, and
      four controls this cycle passed when they should have failed. The criterion
      is satisfied as written; the thing it was meant to guarantee is not yet.
- [~] **Full suite green, `ruff` clean, `bk lint` clean.** 1103 passing,
      `ruff check` clean, `mypy --strict` clean over 39 files. `bk lint` on
      `docs/brain` was **not re-run in this review pass** — it is unchanged from
      the state recorded at release and is not re-asserted here.

`ruff format --check` (62/78) is not an acceptance criterion and never was — it
is outside CI at `.github/workflows/ci.yml:9`, by decision.

---

## Verdict

**Approve, with one qualification that is the more useful sentence.**

The remediation met its own goals. All eight PRD goals G1–G8 are satisfied, the
two data-leak criticals are closed with reproductions, the release is published
and its provenance chain verifies end to end, and Track 2 confirms independently
that the product's central claim — mechanical enforcement — is true of the write
gate under a 14-path adversarial matrix.

**And the review found a critical regression the remediation itself
introduced.** Task 3.7 was reverted once for the right reason, reinstated on an
argument that was true in the scope it was tested in and false one scope wider,
and shipped in `0.6.0` destroying **805 of 2364 nodes** on this repository's own
code graph. The second critical, the `proposal_id` loop, came from a *documentation*
instruction that the programme itself generates into every user's CLAUDE.md.
Both are the same shape as the defects the programme set out to fix: a correct
statement, applied one step outside where it holds.

That is not a reason to reject the work. It is the reason the four-track review
existed, and it argues for keeping that review as a gate rather than as an event.

**State this plainly for anyone reading later: `0.6.0` as published still
contains every defect fixed today.** Closing this work folder records the work;
a **0.6.1** is what reaches users. `docs/product/roadmap/now.md` leads with it.

---
<!-- doc-tracking -->
- Created: 2026-08-13 13:12
