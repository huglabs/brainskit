<!-- Stage 02. Execution specification. Numbered tasks, built in order. -->

# Spec — Field audit remediation → brainskit 0.6.0

Derived from [`01-prd.md`](01-prd.md). Every task maps to one PRD requirement ID.
Component diagrams: [`03-diagrams/components.md`](03-diagrams/components.md).

**31 tasks · 4 phases.** Task numbering is `<phase>.<n>`. Tasks within a phase are
independent unless a **Blocked by** line says otherwise, so they parallelise.

---

## 0. Conventions — read before task 1.1

### 0.1 TDD order is not optional

Per phase, per task: write the test → watch it fail for the *right reason* →
implement → watch it pass. A test written after the fix cannot demonstrate the
fix; it only demonstrates the current behaviour.

### 0.2 The negative control is the deliverable

Every task carries a **Negative control** line naming exactly what to revert and
how many tests must fail. Run it:

```bash
cp <file> /tmp/ctl.bak && <revert the change> && python -m pytest <test file> -q   # expect N failures
cp /tmp/ctl.bak <file>  && python -m pytest <test file> -q                          # expect green
```

Two rules, both learned the hard way in this repo's history:

- **A negative control that passes means the control is broken, not the code.**
  Confirm the edit landed (`grep -c`) before trusting a green control run.
- **Assert on content, never on absence-of-error.** The four existing Obsidian
  tests (`tests/test_fix_integrations.py:1182–1216`) assert only that `"clod"` is
  rejected and `"cloud"` is stored. Both pass with the filtering deleted. Assert
  the literal secret string, the branch name, the file that must not exist.

### 0.3 Phase gate

No phase starts until the previous one clears all five:

```bash
python -m pytest -q                      # 911 baseline, must rise, never fall
ruff check . && ruff format --check .    # see task 4.7 if the hook blocks this
bk --vault docs/brain lint --json        # 0 errors, 0 wiki.outside_apply
bk --vault docs/brain status --json      # enforcement layers agree with reality
git diff --stat                          # every changed file attributable to a task
```

### 0.4 Pre-flight — do this before task 1.1

This repo's own write gate is **not installed**: `.claude/settings.json` registers
hooks from `…/hug-collective/.claude/hooks/`, and the local copies still carry the
pre-rename `brainkit-` names. Agents can currently hand-write into
`docs/brain/wiki/` with nothing refusing them.

```bash
bk hooks install --vault docs/brain --force
bk --vault docs/brain doctor            # write gate must report "enforcing"
```

Separately, `docs/work/` is swallowed by the unanchored `work/` pattern at
`.gitignore:35`, so this spec is untracked. Decide before Phase 1 whether to
anchor it (`/work/`) — otherwise nothing in this folder is committed.

### 0.5 Branch

Do not build on `main`. `git switch -c fix/field-audit-remediation`.

---

## Phase 1 — Stop the leak, stop the lying

Both criticals, the session-opening banner, the verification command, the version
gate. **Nothing else starts until this phase is green.**

### 1.1 — Unresolvable provenance must fail closed (A1) · CRITICAL

**Files:** `src/brainskit/application/privacy.py`
**Test:** `tests/test_fix_services.py` (privacy path) — new class

`_evidence_privacy` builds its generator with `if content_hash in records`,
silently dropping hashes that no longer resolve. When none resolve the generator
is empty and `strictest_privacy` returns `PrivacyMode.CLOUD`. Removing a source
record does not redact the pages built from it — it **declassifies** them, and
stamps them `"privacy": "cloud"`.

**Change.** Mirror `Enrichment.privacy_of` (`application/enrichment.py:173–186`),
which already implements the correct rule and is pinned by a test. Distinguish
three cases, not two:

| Frontmatter `sources` | Meaning | Answer |
|---|---|---|
| absent, or `[]` | a system page that declares no provenance | `CLOUD` |
| non-empty, **all** resolve | ordinary derived page | `strictest_privacy(...)` |
| non-empty, **none/some** resolve | unknown provenance | `NEVER_INGEST` |

The third row is the fix. A partial resolution is still unknown provenance — the
missing source could have been the restricted one.

Apply the identical three-way split to `_evidence_branches` returning `[]`.

**Test asserts on content:**
1. Build a vault with a `never-ingest` source and a page citing it.
2. `bk forget --force` the source (it already returns `still_cited_by` naming the
   orphaned page, and proceeds).
3. `search(consumer="cloud")` → `count == 0`, `redacted == 1`.
4. The literal secret string appears in **no** value of the returned payload.
5. `read_resource(consumer="cloud")` refuses; the page body is not returned.
6. The page is **not** stamped `"privacy": "cloud"`.

**Negative control:** restore the `if content_hash in records` filter → ≥4 failures.

**Done when:** the audit's reproduction (forget → read as cloud) no longer leaks,
and `graph_data` no longer reports `redacted_nodes: 0` for an orphaned page.

---

### 1.2 — Obsidian sync must filter what it copies (A2) · CRITICAL

**Files:** `src/brainskit/infrastructure/integrations.py:256–270`
**Test:** `tests/test_fix_integrations.py` — new class alongside the four weak ones
**Blocked by:** 1.1 (it supplies the correct per-page privacy answer)

`sync` filters the *graph object* carefully, then selects files by walking the
filesystem:

```python
for relative_root in ("wiki", "views"):
    source_paths.extend(path for path in root.rglob("*.md") if path.is_file())
if include_raw:
    source_paths.extend(path for path in (self.vault.root / "raw").rglob("*") ...)
```

`views/` is safe **by accident** — `ProjectionService.integration_sync` regenerates
it filtered (`self.views(consumer=str(graph["consumer"]))`) just before the copy.
`wiki/` and `raw/` are never regenerated, so nothing touches them. The compiled
page leaks under **default options**; raw `never-ingest` bytes leak additionally
under `--include-raw`. Sync targets are typically iCloud/Dropbox-backed.

**Change.** The consumer is already in hand as `graph["consumer"]`. Filter each
candidate before it reaches `_atomic_copy`:

- `wiki/*.md` → keep only if `_consumer_allows(consumer, _evidence_privacy(...))`.
- `raw/*` → keep only if the record's branch privacy allows the consumer. Resolve
  by content hash through the registry; a file under `raw/` with no registry entry
  is unknown provenance → **exclude** (same rule as 1.1).
- A page excluded here must also be pruned from the `managed` set so the stale
  sweep removes any previously-synced copy.

`docs/integrations.md:172` already promises `local` "always redacts never-ingest".
This makes the promise true rather than restating it.

**Test asserts on content** — the test the audit says would have caught it:

```
sync(consumer="cloud"), default options   → no file under target contains SECRET
sync(consumer="cloud"), include_raw=true  → no file under target contains SECRET
sync(consumer="human")                    → the secret IS present (control:
                                             proves the test can fail)
```
Walk every file under the target and assert on bytes. Assert the excluded page's
path is absent, and that a previously-synced copy is deleted on re-sync.

**Negative control:** delete the filter → ≥3 failures. The `human` case must stay
green in that run, or the test is asserting "nothing is ever written".

**Done when:** all four pre-existing consumer tests still pass **and** at least one
new test fails when filtering is removed.

---

### 1.3 — The SessionStart hook must read the answer it already holds (B1)

**Files:** `src/brainskit/templates/agents/brainskit-status.sh:83–96`
**Test:** `tests/test_enforcement_status.py`

This is the exists-vs-runs pattern in the highest-leverage place in the product —
the block an agent reads at the start of *every* session. `bk status` was fixed
for both failure modes; the generated shell reimplements the check as
`[ -x "$HOOK_DIR/brainskit-gate.sh" ]` and `[ -f "$WORKSPACE/.git/hooks/pre-commit" ]`.

**Change — deletion, not new logic.** `$STATUS_JSON` is fetched at line 46 and
already contains `enforcement.layers[]` with `active`, `detail` and `script`
(verified by running `bk status --json`). Therefore:

1. Delete lines 83–96 entirely.
2. Drop `write_gate, commit_lint = sys.argv[1], sys.argv[2]` from the renderer and
   stop passing those two argv values.
3. Render from `status.get("enforcement", {}).get("layers", [])`: a layer is
   `active` or it prints its own `detail`. Do not re-derive, re-word or summarise
   the detail — the Python already phrased it correctly.

Also regenerate the stale installed copies: `.claude/hooks/brainkit-{gate,status}.sh`
carry the pre-rename names and the same broken block (see 0.4).

**Test asserts on content**, both audit-reproduced cases, using the existing
`ShellHookCase` fake-`bk` shim so no real `bk` is spawned:

| Case | `bk status` says | hook must say |
|---|---|---|
| A — Husky repo (`core.hooksPath=.husky/_`) | `commit_lint ✗` | `commit lint OFF …` + the reason |
| B — hooks on disk, unregistered in settings.json | `write_gate ✗`, `session_status ✗` | both OFF, not "active" |
| C — genuinely installed (control) | all `✓` | `active` |

**Negative control:** restore lines 83–96 → ≥2 failures, case C still green.

**Done when:** `sh brainskit-status.sh | tail -2` agrees with `bk status` in all
three cases, and the shell contains no `[ -x ]`/`[ -f ]` deciding an "active" claim.

---

### 1.4 — `bk gate check-write` must be unambiguous (B2)

**Files:** `src/brainskit/application/gate.py:172–173`, `docs/commands.md`,
CLI help for `gate check-write`
**Test:** `tests/test_gate.py`

Same file, same command, opposite answers:

```
bk gate check-write docs/brain/wiki/concepts/x.md         → ✓ allowed        exit 0
bk gate check-write $PWD/docs/brain/wiki/concepts/x.md    → gated, use bk apply  exit 2
```

Paths resolve against **vault root**, not cwd — deliberate per the `check_write`
docstring, but the opposite of every other command, and stated in neither `--help`
nor `docs/commands.md`. Production enforcement is safe (the installed hook always
passes absolute paths); this is the command a human or agent uses to *verify* the
gate by hand, and it returns a confident wrong answer with exit 0.

**Change — pick one and make it total.** Recommended: **resolve relative paths
against cwd** in the CLI interface layer before calling `check_write`, leaving the
application-layer contract (relative-to-vault-root) intact and documented. Then:

- a relative path a human would type resolves the way every other command does;
- `--help` and `docs/commands.md` state the base explicitly;
- the output names the resolved absolute path it actually judged.

If instead the vault-root base is kept, all three disclosure points are still
mandatory — the defect is the silence, not the choice.

**Test asserts on content:**
1. Relative and absolute paths to the same gated file → identical verdict and
   identical exit code.
2. The printed output contains the resolved absolute path.
3. A path outside the vault is still allowed (the gate governs the vault, not the
   filesystem — do not over-deny).
4. Symlink and `..` containment cases from the existing suite still pass.

**Negative control:** revert the resolution → ≥2 failures, case 3 still green.

---

### 1.5 — One source of version truth, gated in CI (C1)

**Files:** `src/brainskit/__init__.py:3`, `pyproject.toml:10`,
`.github/workflows/release.yml:41–50`, `scripts/verify-wheel.sh`
**Test:** `tests/test_fix_domain.py` or a new `tests/test_version.py`

Verified: `__version__ = "0.4.0"`, `[project].version = "0.5.0"`, tag `v0.5.0`.
`bk --version` and MCP `serverInfo.version` both report the wrong value.
`release.yml` asserts tag == `[project].version` but never checks `__version__`,
and `verify-wheel.sh` does not compare them — so the drift shipped **through a
gate designed to catch exactly this**.

**Change.** Single-source it. Preferred: `__version__` derives from installed
distribution metadata via `importlib.metadata.version("brainskit")`, with the
`pyproject.toml` value as the only literal. Whichever direction is chosen, the
literal must exist in exactly one file.

Then close the gate in both places:

- `release.yml`: assert tag == `[project].version` == `__version__` (import the
  built wheel, do not re-read the source tree — that is what makes it a wheel
  check rather than a repo check).
- `verify-wheel.sh`: compare the wheel's metadata version against
  `brainskit.__version__` imported from that same wheel.

**Test asserts on content:** `brainskit.__version__` equals the version parsed
from `pyproject.toml`. This test is the guard that survives after CI changes.

**Negative control:** set `__version__ = "0.0.1"` → ≥1 failure.

**Done when:** `bk --version`, `pyproject.toml`, the git tag and MCP
`serverInfo.version` all report one string, and CI fails if they diverge.
**Do not bump to 0.6.0 yet** — that is task 4.6.

---

## Phase 2 — Honest answers and the way in

**Blocked by:** Phase 1 gate.

### 2.1 — `bk graph` stamps a consumer and filters (A3)

**Files:** `src/brainskit/application/projections.py` (`ProjectionService.graph`)
**Test:** `tests/test_projections.py`

`graph()` calls `self.graph_port.build(self.vault)` and writes the result raw,
while `graph_data()` takes a `consumer` and filters. Two files called "the graph"
carry different boundaries and nothing on either says which. Impact is limited
(`graph/` is gitignored) but the inconsistency is the bug.

**Change.** `graph(*, consumer: str = "local", html: bool = False)`. Filter through
the same path `graph_data` uses, and stamp `"consumer": <value>` into the written
artifact. Default `local`, matching every other file target.

**Test:** a `never-ingest` node is absent from `graph/graph.json` at `local`;
`"consumer"` is present in the written JSON. **Negative control:** drop the filter
→ ≥1 failure.

---

### 2.2 — One answer for an unknown `sourced_from` hash (A4)

**Files:** `src/brainskit/infrastructure/graph.py:45`,
`src/brainskit/application/health.py:310`
**Test:** `tests/test_code_citations.py` or `tests/test_projections.py`

`graph.py` guards `if raw_id in nodes:` and **silently drops** the edge;
`health.py:310` reports the same condition as a lint finding. Two code paths, two
answers about one fact — in the artifact whose whole purpose is to make provenance
structural.

**Change.** The graph must not silently discard provenance. Emit the edge with the
target marked unresolved (so the graph shows the hole), **or** omit it and have the
builder surface the count in its return value. Either way the two paths must agree,
and `bk lint` remains the place a human is told to repair it.

**Test:** a page citing an unknown hash produces the same verdict from `bk graph`
and `bk lint`. **Negative control:** restore the silent drop → ≥1 failure.

---

### 2.3 — Enforce `strictest_privacy`'s invariant instead of asserting it (A5)

**Files:** `src/brainskit/application/privacy.py`
**Test:** `tests/test_fix_services.py`
**Blocked by:** 1.1

The docstring justifies the `CLOUD` default by asserting *"every caller checks
provenance resolves first"*. Task 1.1 makes that true for the one caller that
didn't. This task makes it **unable to become false again**.

**Change.** Make the empty case impossible to reach by accident — either
`strictest_privacy` raises on an empty iterable (callers must decide explicitly),
or it takes a required `on_empty: PrivacyMode` argument. Update the docstring to
describe enforcement rather than an assumption.

**Test:** calling with an empty iterable raises (or requires the argument); no
call site passes an implicitly-empty iterable. **Negative control:** restore the
silent `CLOUD` default → ≥1 failure.

---

### 2.4 — Code-graph traversals carry staleness (B4)

**Files:** `src/brainskit/application/codegraph.py:874–894` (`_read`, `data`)
**Test:** `tests/test_code_graph.py`

`staleness()` is honest and correct. **Nothing consults it.** `hubs`, `affected`,
`path`, `communities`, `cycles`, `diff` and `data` all go through `_read()`, which
checks the privacy boundary and returns whatever is on disk. Reproduced on this
vault: `code status` says `stale` with 41 removed files, while `code hubs` returns
`src/brainkit/domain/model.py` with an exact line number — a path that ceased to
exist at the rename. An agent asking "what is load-bearing here" is answered
authoritatively about a tree that is gone.

Separately, `data()["state"]` is **always** `None` — it reads a key `_write` never
stores, so the web viewer gets a null state the backend could classify in one call.

**Change.**
1. `_read` returns the graph **and** the staleness verdict; every traversal
   includes it in its payload (`{"staleness": {...}, ...}`).
2. Human renderers print a one-line caveat when not `fresh`.
3. `_write` stores `state`, or `data()` computes it from `staleness()`. Either
   way `data()["state"]` is never unconditionally `None`.
4. Do **not** refuse on stale — refusing would break the legitimate "rebuild is
   in progress" workflow. Disclose, don't block.

**Test:** with a deliberately stale graph, every one of the seven traversals
carries a non-`fresh` staleness signal; `data()["state"]` is non-null in both fresh
and stale cases. **Negative control:** strip the signal from `_read` → ≥7 failures.

---

### 2.5 — `bk init --print-config` (D1)

**Files:** `src/brainskit/interfaces/cli.py:1492–1495`,
`src/brainskit/interfaces/onboarding.py:464–522`, `docs/getting-started.md`
**Test:** `tests/test_onboarding.py`

Non-interactive init is unusable from the documentation alone. Off a TTY:
`bk init ./v` refuses; the here-doc that `docs/getting-started.md:29` promises
refuses identically; `--config` with `{}` lists **nine** missing keys with no
shapes, no template, no example and no next command. The only complete specimen in
the repo is `docs/brain/.brain/config.json` — the project's own vault, which a
user never receives. This blocks CI, containers, agent-driven setup and any
non-TTY shell: precisely the audiences a local-first agent tool has.

**Change.**
1. Add `bk init --print-config [--preset <name>]`. The wizard already assembles a
   valid policy at `onboarding.py:464–522` — expose that object rather than
   writing a second one, or the two drift.
2. Both refusals (`Interactive init needs a terminal` and `Vault policy is
   incomplete`) name the new command in their hint.
3. The incomplete-policy error keeps listing missing keys **and** gains the shape
   of each.

**Test asserts on content:** the round trip works end to end —
`bk init ./v --config <(bk init --print-config)` succeeds off a TTY and produces a
vault that `bk status` reports healthy. **Negative control:** break `--print-config`
output → the round-trip test fails.

---

### 2.6 — Implement `taxonomy_seed` (D2)

**Files:** `src/brainskit/domain/model.py:746,774,793,829,860`,
`src/brainskit/interfaces/onboarding.py:504`, the ingest job
**Test:** `tests/test_engine.py`
**Blocked by:** the open question below — resolve before writing code

Verified: five write/parse sites in `domain/model.py`, one writer at
`onboarding.py:504` (`sorted(branches)`), and **zero readers**. It is
simultaneously dead and load-bearing on first-run friction, because
`domain/model.py:774` puts it in `required`.

> **Open question, must be answered first.** `onboarding.py:504` writing
> `sorted(branches)` *suggests* the ingest job's branch taxonomy, but with zero
> readers the intended semantics are inferred, not recorded. Pin the contract —
> what consumes it, what it changes about filing behaviour, what an empty list
> means — before implementing, or this task encodes a guess.

**Change.** Wire it into the ingest job's branch taxonomy so it has at least one
reader reachable from `bk ingest`. Keep it `required` only if the implemented
behaviour justifies it; if the honest answer turns out to be "optional with a
sensible default", make it optional — that also helps 2.5.

**Test:** a vault with a distinctive `taxonomy_seed` produces observably different
ingest filing than one without. **Negative control:** ignore the value in the
reader → ≥1 failure. A test that only asserts the key round-trips through
serialisation is **not** acceptable here — that is the tautology that let it stay
dead.

---

### 2.7 — Delete the here-doc promise (D3)

**Files:** `docs/getting-started.md:29`
**Test:** `tests/test_fix_interfaces.py` (docs-truth assertion) or manual

The sentence documents behaviour that does not exist — piping to `bk init` refuses
exactly like a bare invocation. Delete it and point at `--print-config` from 2.5.

**Blocked by:** 2.5. **Done when:** every command in `getting-started.md` runs
off a TTY as written.

**Negative control:** N/A — a documentation deletion, no behaviour to revert. The
guard is 2.5's round-trip test: if `--print-config` regresses, the replacement
instruction this task points at stops working.

---

### 2.8 — `KeyError` must not escape four read paths (E1)

**Files:** `src/brainskit/application/privacy.py:77` (`_privacy_for_record`)
**Test:** `tests/test_fix_services.py`

`config.branches[branch]` is a direct subscript where `llm.py:119` correctly uses
`.get()` + `PolicyError` for the identical question. Drop a file into a directory
that isn't a configured branch, run the **documented** `bk reconcile`, and
`search(human)`, `browse_sources`, `graph_data(local)` and `export(json)` all raise
a bare `KeyError('personal')`. It is not a `BrainskitError`, so it bypasses the
JSON error envelope and the exit-code machinery: an unhandled traceback on the CLI,
a 500 in the web viewer. `bk status` stays green because `health.py:150` has an
`"unknown"` fallback.

**Change.** `.get()` + `PolicyError` naming the branch and the configured set.
`PolicyError` already subclasses the right base, so every `except` and exit code is
unchanged.

**Test asserts on content:** after the documented reconcile of an unconfigured
directory, each of the four read paths raises `PolicyError` (not `KeyError`), the
JSON envelope is well-formed, and the message names the offending branch.
**Negative control:** restore the subscript → ≥4 failures.

---

### 2.9 — `search(limit=N)` returns N (E2)

**Files:** `src/brainskit/application/retrieval.py:88–90`
**Test:** `tests/test_fix_services.py`

Exact mechanism, confirmed by reading:

```python
target_graph  = max(1, limit // 4) if limit >= 4 else 0   # 0 when limit < 4
direct_limit  = max(1, limit - target_graph) if target_graph else limit
hits          = ranked[:direct_limit]                      # == limit
reserve       = limit - len(hits)                          # == 0
...
    expanded.append(hit)          # append precedes the bound check
    if len(expanded) >= reserve:  # 1 >= 0 → break, but one was already added
        break
```

So exactly one link-neighbour is always appended and `count == limit + 1`.
Reproduced at limits 1, 2 and 3. It propagates into `context`, where `limit` is the
caller's bound on how much evidence reaches a model.

**Change.** Skip the expansion entirely when `reserve <= 0`, or bound the iteration
(`expanded_candidates[:reserve]`) so the append cannot precede the check. Prefer
the explicit `if reserve <= 0` guard — it states the intent.

**Test:** `count == N` and `len(hits) + len(expanded) == N` for N ∈ {1,2,3}, plus a
control at N = 8 proving graph expansion still happens when there is room for it.
**Negative control:** restore the unbounded loop → ≥3 failures, N = 8 still green.

---

### 2.10 — Provider outages are not `validation_error` (E3)

**Files:** `src/brainskit/application/llm.py:601,617`,
`src/brainskit/infrastructure/integrations.py:1035,1037,1370,1393` and the
re-wrapping handlers at `:536` and `:1189`
**Test:** `tests/test_fix_integrations.py`

Six sites raise "Provider is unreachable" / "Docker command failed" with
`validation_error`, whose documented meaning is *"the request itself is wrong — fix
it and send it again"*. An agent reading that will keep rewriting a well-formed
request against a down provider.

**Change.** Raise `NotConfiguredError` (already a `ValidationError` subclass, so
every `except` and exit code is unchanged — this is the additive-subclass property
the error taxonomy was designed for). **Then fix the two re-wrapping handlers at
`integrations.py:536` and `:1189`** — they will silently flatten it back to the base
class unless they re-raise `type(exc)`.

**Test asserts on the code, through the re-wrapping path:** an unreachable provider
surfaces `code == "not_configured"` *after* passing through both handlers.
**Negative control:** revert either handler → ≥1 failure. A test that only checks
the raise site will pass with the handlers broken — that is the trap here.

---

### 2.11 — Diagnose the release no-op (C2) · SPIKE

**Files:** `.github/workflows/release.yml`
**Output:** a written finding in `implementation-log.md`, then either a fix here or
a re-scoped 4.6

The `Release` workflow reports **Success** on `v0.5.0`, yet nothing is on PyPI and
no GitHub Release exists — so neither the `publish` nor the `github-release` job
produced output. The workflow file itself reads correctly (OIDC Trusted Publishing,
`environment: pypi`). Run logs were unreadable during planning (`gh` unauthenticated).

**Steps.**
1. `gh auth login`, then `gh run view --log` the `v0.5.0` release run.
2. Determine which of these it is: job skipped by a condition · environment
   approval never granted · Trusted Publishing not configured on PyPI for
   `huglabs/brainskit` · job ran and silently no-op'd.
3. **Whatever the cause, neither job may be able to no-op silently again** — add a
   post-publish assertion that queries `pypi.org/simple/brainskit/` and fails the
   workflow if the new version is absent.

**Done when:** the cause is written down, the guard exists, and 4.6 is either
unblocked or re-scoped with a stated reason.

**Negative control:** N/A as a code revert — this is a spike. But its deliverable
*is* a control, so prove it: point the new post-publish assertion at a version
that was never published and confirm the workflow goes red. A guard that cannot
fail is what produced this finding in the first place.

---

## Phase 3 — Architecture

**Blocked by:** Phase 2 gate. Largest phase; 3.1 is the biggest single item.

### 3.1 — Native `cycles` and `diff`; drop the `networkx` pin (F1)

**Files:** `src/brainskit/application/codegraph.py:990–1022`,
`src/brainskit/infrastructure/codeanalysis/analyze.py:6`, `pyproject.toml:82`
**Test:** `tests/test_code_analysis.py`, `tests/test_vendoring.py`

Verified: `analyze.py:6` does a module-level `from graphify.build import edge_data`.
`edge_data` is **13 lines**. `build.py` is **1,643 lines** and imports `validate.py`
(95) plus `networkx` at load — against a hard `networkx>=3.4` runtime pin. The proof
this is a known wound is in the tree: `_load_analysis` exists purely as a lazy-import
shim, and its docstring says so.

**Change — do not edit the vendored tree.** Reconsider the tier above it:

1. Implement `cycles` natively (Tarjan SCC) on brainskit's own normalised
   `code.json`. The application layer already builds the `nx.DiGraph` itself.
2. Implement `diff` natively (set difference over node/edge identity).
3. Remove the module-level `graphify.build` import; keep `cluster.py` (320 LOC) as
   the only genuine delegation.
4. Move `networkx` from required dependencies to the `code` extra (or drop it if
   nothing else needs it) — verify with `uv lock` in a scratch dir, not by reading.

**Test:** `cycles` and `diff` produce byte-identical output to the current
implementation on a fixture graph containing at least one multi-node cycle and one
self-loop. Capture that fixture output **before** changing anything — it is the
only way to prove equivalence rather than plausibility. Then assert
`graphify.build` is not imported at module scope anywhere, and that importing
`brainskit` does not import `networkx`.

**Negative control:** re-add the module-level import → the import-scope test fails.

**Done when:** `pip install brainskit` pulls no `networkx`, and the equivalence
fixture passes.

---

### 3.2 — Move the jsonschema engine out of `domain/` (F2)

**Files:** `src/brainskit/domain/model.py:1174–1271` → `src/brainskit/application/`
**Test:** `tests/test_layering.py`

It is the domain layer's **only** third-party dependency, and every caller is
already in `application`. Zero coupling cost.

**Change.** Move the module; update imports; the layering test should now be able
to assert `domain` has no third-party imports at all. Add that assertion — it is
the thing that keeps the move from being undone.

**Negative control:** move it back → the new domain-purity assertion fails.

---

### 3.3 — De-triplicate the installer contract (F3)

**Files:** `src/brainskit/interfaces/cli.py:1531–2155`,
`src/brainskit/application/health.py:688–693`, `src/brainskit/application/gate.py`
**Test:** `tests/test_layering.py`, `tests/test_hooks_install.py`

The gate constants, the managed-block sentinel and the mechanism strings each exist
in two places. The reason is stated outright in a comment: *"The sentinel is
duplicated from the installer rather than imported: the application layer must not
depend on interfaces."* **The rule is right; the response was to copy rather than
move.** Writer (`bk hooks install`) and readers (`bk status`, `bk doctor`) can now
silently disagree about what "installed" means — and per this repo's history that
divergence has already shipped twice.

**Change.**
1. **Cheapest half first:** the gate constants already live in
   `application/gate.py`, and `test_layering.py:34` permits `interfaces →
   application`. `cli.py` imports them. Two lines.
2. The managed-block sentinel moves **downward** into `application/`; `cli.py`
   imports it. The layering rule is satisfied by moving, not copying.
3. The mechanism strings get one owner — `health.py` already renders them.

**Test:** assert each constant is defined exactly once repo-wide (count definition
sites, not usages). **Negative control:** re-introduce a copy → that count assertion
fails.

---

### 3.4 — Close the two layering-test gaps (F4)

**Files:** `tests/test_layering.py:34` and its dynamic-import scan
**Test:** the same file

The layering test is real and good — it enforces direction and acyclicity, reads
the graph with `ast` to avoid import side effects, and guards against vacuous
passes. Two gaps:

1. The dynamic-import scan is filtered with `if "codeanalysis" in node.value`, so
   it identifies the hole and closes **only the instance already known**. Generalise
   it to any dynamic import.
2. `ALLOWED` permits `infrastructure → application` more broadly than
   `architecture.md` shows — which is how `graph.py:8` imports a plain function
   rather than a port without anything noticing. Narrow `ALLOWED` to match the
   documented architecture, then fix the violations it surfaces (`graph.py:8` is
   the known one; expect others).

**Negative control:** widen `ALLOWED` back → the `graph.py` violation stops being
reported.

---

### 3.5 — `bk status` must not claim health above failures (B3)

**Files:** `src/brainskit/application/health.py:173`
**Test:** `tests/test_enforcement_status.py`

`✓ vault healthy` is set from lint alone, so it prints above three `✗` enforcement
rows. **Change:** the headline reflects lint **and** enforcement (and any other
`✗` row it sits above). **Test:** a vault with clean lint and a dead enforcement
layer does not print a healthy headline. **Negative control:** revert → ≥1 failure.

---

### 3.6 — Enforce slug uniqueness (E4)

**Files:** `src/brainskit/infrastructure/graph.py` (`slug_nodes[slug] = node_id`),
the apply gate, a new lint code
**Test:** `tests/test_projections.py`, `tests/test_gate.py`

Page paths are `wiki/{kind}/{slug}.md`, so the same slug under two kinds yields two
files with one stem. Link resolution keys by stem alone — last writer wins. Apply
doesn't prevent the collision; lint has no code for it. The winner is decided by
directory sort order: deterministic but arbitrary, so **adding a page-kind
directory that sorts later would silently flip every such edge across the whole
vault**. The concept page gets zero inbound edges while its author believes it is
linked.

**Change.**
1. Apply **refuses** a proposal that would create a duplicate slug across kinds
   (`ConflictError` — the caller must rename, which is a re-read-and-retry remedy).
2. A new lint code reports existing collisions so vaults already carrying one can
   be repaired rather than blocked.
3. `[[link]]` resolution to an ambiguous slug is a lint error, not a silent pick.

**Test:** the audit's reproduction — apply `concept:widget`, `entity:widget`,
`synthesis:overview` with `[[widget]]` — now refuses at apply, and an
already-colliding vault produces a lint finding instead of `[]`.
**Negative control:** remove the apply check → the refusal test fails and lint
returns `[]` again.

---

### 3.7 — Scoped builds prune; coverage evidence persists (E5)

**Files:** `src/brainskit/application/codegraph.py:285–290` (`_merge_scoped`), `:396`
**Test:** `tests/test_code_graph.py`
**Blocked by:** 2.4 (staleness must already be attached)

`_merge_scoped` keeps every stored node whose path falls outside the current scope,
so a deleted file keeps its nodes indefinitely unless someone runs a whole-root
build. Combined with 2.4 the failure reads: rebuild one file → `status` truthfully
says stale → every query silently includes phantoms.

Separately, `_unexplained` exists specifically to catch silent extraction loss —
its docstring names the two incidents that motivated it, including a build
reporting 7.7% coverage. Its result is merged into the build's **return value**,
never into the artifact, and `parseable_files` isn't stored either, so nobody can
recompute it. `staleness()` derives `partial` from missing grammars alone, so an
unexplained gap can never produce anything but `fresh`.

> Honest scoping note from the audit: a fresh whole-root extraction of this repo
> has a coverage gap of **exactly zero** — the pipeline is healthy today. The
> defect is that if it stops being healthy, the evidence survives for one line of
> build output and is then unrecoverable.

**Change.**
1. `_merge_scoped` drops stored nodes whose file no longer exists, even out of scope.
2. Persist `unexplained` and `parseable_files` into the artifact.
3. `staleness()` reports `partial` for an unexplained gap, not only for missing
   grammars.

**Test:** delete a file, rebuild a *different* single file's scope, assert the
deleted file's nodes are gone; assert a seeded coverage gap survives a
write-then-read round trip and turns `staleness()` non-`fresh`.
**Negative control:** revert the prune → the phantom-node test fails.

---

### 3.8 — Rename graph objects to brainskit, with migration (G2)

**Files:** `src/brainskit/infrastructure/integrations.py:351,999,1003,1015,1016`
(and the Postgres schema name), `CHANGELOG.md:64–67`, `docs/integrations.md:33`
**Test:** `tests/test_fix_integrations.py`

Decision: **code follows the docs.** `docs/integrations.md:33` says
`BrainskitNode`; the code says `BrainkitNode`, and `CHANGELOG.md:64–67` records
that as deliberate so a rename wouldn't orphan existing subgraphs.

**Change.**
1. `BrainkitNode` → `BrainskitNode`; Postgres schema `brainkit` → `brainskit`.
2. **Ship the migration** the CHANGELOG was protecting against: a documented
   Cypher/SQL step that relabels an existing subgraph, and a runtime path that
   detects old-named objects and reports the migration command rather than
   silently creating a parallel graph.
3. Update `CHANGELOG.md:64–67` — it currently records the opposite decision.

**Test:** a fresh sync creates `BrainskitNode`; a store holding old-named nodes
produces the migration message, not a silent second graph. **Negative control:**
skip the detection → the old-store test fails.

---

## Phase 4 — Docs truth, then ship

**Blocked by:** Phase 3 gate. Reconcile docs against what Phases 1–3 made true.

### 4.1 — The web API is not read-only (G1)

**Files:** `docs/serving.md:24`, `docs/architecture.md:4`,
`src/brainskit/interfaces/web.py:363–407,480`

Documented twice as read-only. `do_POST` handles `/api/capture` (writes `raw/` +
index), `/api/ask` (writes `output/answers/`), `/api/proposals/reject`, and
`/api/proposals/approve` — which routes through the apply gate and writes `wiki/`.
What actually protects it is `web.py:386–398`: `consumer != "human"` → 403
`writes_refused`. The bearer token is **optional** (`web.py:480` returns `True`
when none is configured), so a `--consumer human` viewer with no `--token-env` is
guarded by Host/Origin checks alone. The documented endpoint list also omits
`/api/code-graph` — the real GET surface is **11, not 10**.

**Change:** state all four write endpoints and what guards them; document the
token's optionality as a deliberate choice with its consequence; add
`/api/code-graph` and correct the count.
**Test:** a docs-truth assertion comparing the documented endpoint list against the
routes the server actually registers — same trick as `docs/commands.md`, which the
audit found has zero wrong names across 32 commands.

**Negative control:** remove one route from the documented list → the assertion
fails. If it stays green, the assertion is comparing the list to itself.

---

### 4.2 — Help text for the 26 bare flags (G3)

**Files:** `src/brainskit/interfaces/cli.py`
**Test:** `tests/test_console.py`

26 flags carry no help text; **0 of 32** subcommands mention `--json` or `--vault`.
**Change:** help text for every flag; `--json`/`--vault` documented per subcommand.
**Test:** assert no flag in the parser tree has an empty help string — a property
test, so it stays true for flags added later. **Negative control:** blank one →
the assertion fails.

---

### 4.3 — `--force` must do what its help says (G4)

**Files:** `src/brainskit/interfaces/cli.py`

`--force`'s help promises a guard against initialising over other projects that is
**not implemented**. Notable given the incident recorded at `codegraph.py:226`,
where a vault "merely holding projects" indexed 55,295 files.

**Change:** implement the guard (refuse when the target looks like an unrelated
project root, overridable by an explicit second signal), **or** correct the help to
describe what `--force` actually does. Implementing is preferred — the incident is
in the tree. **Test:** initialising over a directory that looks like another
project refuses without the override and proceeds with it.

**Negative control:** disable the guard → the refuse case fails while the
with-override case stays green. If both fail, the test is asserting "init never
works" rather than "the guard fires".

---

### 4.4 — `bk capture` gets a human renderer (D4)

**Files:** `src/brainskit/interfaces/cli.py`
**Test:** `tests/test_console.py`

`capture` is the **second command in every quickstart** and dumps raw JSON with no
human renderer. Additionally: apply refusals name the failure but never the next
command, and `missing_base_hash` hands you `observed` — which *is* the value to
paste — without ever saying so.

**Change:** a human renderer for `capture` (hash, branch, what to run next); apply
refusals name the next command; `missing_base_hash` says `observed` is the value to
paste. **Test:** non-`--json` output contains the hash and a next command; the
`missing_base_hash` message names `observed`.

**Negative control:** revert to the raw JSON dump → the human-output assertions
fail while the `--json` output tests stay green.

---

### 4.5 — Remove the dead vendored regions (F5)

**Files:** `src/brainskit/infrastructure/codeanalysis/{build,validate,detect,google_workspace,security}.py`,
`codeanalysis/NOTICE`
**Test:** `tests/test_vendoring.py`
**Blocked by:** 3.1 (which removes the last live caller of `build.py`)

Precisely measured dead: `build.py` builder + `validate.py` (845), `detect.py`
pipeline + `google_workspace.py` (634), `security.py` HTTP fetch stack (211) —
**1,938 LOC, ~6.4% of the tree.**

**Vendoring governance here is better than most first-party code** and must not be
weakened: `codeanalysis/NOTICE` declares the three files that depart from
byte-identity with reasons, and `test_vendoring.py:40` pins that list so a fourth
undeclared edit fails a test. Deletions must be **declared in NOTICE** and the pin
updated in the same commit.

**Test:** the existing vendoring pin still passes, and NOTICE declares every
departure including the deletions. **Negative control:** delete a region without
declaring it → `test_vendoring.py` fails. *(If that control passes, the pin is not
covering deletions — fix the pin, that is a finding in its own right.)*

---

### 4.6 — Publish 0.6.0 (C3, C4)

**Files:** `pyproject.toml`, `src/brainskit/__init__.py` (per 1.5's single source),
`CHANGELOG.md`, `README.md`, `SECURITY.md`
**Blocked by:** every preceding task, and 2.11's finding

1. Bump to `0.6.0` in the one place 1.5 established.
2. CHANGELOG entry covering all four phases.
3. Tag `v0.6.0`; the release gate now asserts tag == `[project].version` ==
   `__version__`.
4. Confirm the publish landed: `pypi.org/simple/brainskit/` returns **200** and
   `uv tool install brainskit` installs `0.6.0` in a clean environment. The
   post-publish assertion from 2.11 must fail the workflow if not.
5. README install lines and the PyPI/Python-version badges become true.
6. Cut the GitHub Release.
7. `SECURITY.md`'s "latest release only" is now satisfiable — a reporter following
   the policy names a supported version (C4).

**Done when:** a clean machine can run the README's first command successfully.

**Negative control:** point the post-publish check at a version that does not
exist → the workflow must go red. A release step that cannot fail is not a gate,
and that is exactly what shipped as *Success* on `v0.5.0`.

---

### 4.7 — Unblock `ruff format --check` (ride-along)

**Files:** `.claude/hooks/block-destructive-commands.sh`

The audit could not run `ruff format --check`: the string `format` trips this
project's own destructive-command hook. That is a false positive blocking a
standard formatting check — and it means §0.3's phase gate cannot run as written
until it is fixed. **Do this during Phase 1**, not Phase 4, despite its numbering.

**Change:** narrow the pattern so it matches destructive `format` invocations
(disk/partition), not `ruff format`. **Test:** `ruff format --check .` runs; a
genuinely destructive command is still blocked.

**Reproduced live during specification.** Writing this spec, a `python3` heredoc
whose *text* merely contained the word was refused:
`🚫 BLOCKED: filesystem formatting command`. So the hook matches the substring
anywhere in the command — including inside a quoted string that never invokes
anything. It blocks documentation about the check as readily as the check itself.

**Negative control:** restore the broad pattern → `ruff format --check .` is
blocked again, and the genuinely-destructive case stays blocked in *both* runs.
If the destructive case ever passes, the narrowing went too far.

---

## Traceability

| Phase | Tasks | PRD requirements |
|---|---|---|
| 1 | 1.1–1.5 | A1 A2 B1 B2 C1 |
| 2 | 2.1–2.11 | A3 A4 A5 B4 D1 D2 D3 E1 E2 E3 C2 |
| 3 | 3.1–3.8 | F1 F2 F3 F4 B3 E4 E5 G2 |
| 4 | 4.1–4.7 | G1 G3 G4 D4 F5 C3 C4 + hook fix |

All 31 PRD requirements are covered. Tasks 0.4 (pre-flight) and 4.7 (hook) are
additions found during specification, not PRD requirements.

## Task checklist

**Phase 1** — [x] 1.1 · [x] 1.2 · [x] 1.3 · [x] 1.4 · [x] 1.5 · [x] 4.7 (early)
**Phase 2** — [x] 2.1 · [x] 2.2 · [x] 2.3 · [x] 2.4 · [x] 2.5 · [x] 2.6 · [x] 2.7 · [x] 2.8 · [x] 2.9 · [x] 2.10 · [x] 2.11
**Phase 3** — [x] 3.1 · [x] 3.2 · [x] 3.3 · [x] 3.4 · [x] 3.5 · [x] 3.6 · [ ] 3.7 · [x] 3.8
**Phase 4** — [x] 4.1 · [x] 4.2 · [x] 4.3 · [x] 4.4 · [x] 4.5 · [ ] 4.6

---
<!-- doc-tracking -->
- Created: 2026-08-12
- Updated: 2026-08-12 10:21
- Updated: 2026-08-12 10:23
- Updated: 2026-08-12 12:36
- Updated: 2026-08-12 14:49
