<!-- Stage 01. Product requirements for this feature/bug. -->

# PRD — Field audit remediation → brainskit 0.6.0

**Evidence:** [brainskit Field Audit](https://claude.ai/code/artifact/6d5de80a-c222-4c63-ac52-6244f1e56b4b)
· 5 parallel agents · `0.5.0` @ `8216766` · 12 Aug 2026
**Scope decision:** full remediation *including* the architecture track.
**Release decision:** fix first, then cut a new version — `0.5.0` is not re-published.

---

## 1. Problem

brainskit's product claim is mechanical enforcement. The audit found that claim
is false in six ship-blocking places and eleven medium ones, all instances of one
root cause: **a check verifies that a thing exists rather than that it works, or
resolves an unknown to the permissive answer instead of the safe one.**

Two of these are data-leak criticals, each found independently by two agents
using different methods, each backed by a reproduction rather than a reading.

### Verified before planning

Every ship-blocker was re-checked against the tree during planning rather than
taken on report:

| # | Finding | Location | Verification |
|---|---------|----------|--------------|
| 1 | Unresolvable provenance fails open → `never-ingest` content served to `cloud` | `application/privacy.py:94–98, :116–121` | Read. `_evidence_privacy` filters `if content_hash in records`; empty set → `strictest_privacy` → `CLOUD`. `Enrichment.privacy_of` in the same repo returns `NEVER_INGEST` for the identical case. |
| 2 | Obsidian sync copies `wiki/` + `raw/` with no privacy filter | `infrastructure/integrations.py:256–270` | Read. `rglob("*.md")` over `wiki`/`views`; `include_raw` walks `raw/` wholesale. Only `graph` is the filtered object. |
| 3 | Package is not on PyPI; README's primary install fails | `README.md:105,113–116,137` | Re-run. `brainskit` → 404, `brainkit` → 404, control `jsonschema` → 200. |
| 4 | SessionStart hook reports dead enforcement layers as active | `templates/agents/brainskit-status.sh:83–96` | Read. `[ -x gate.sh ]` and `[ -f .git/hooks/pre-commit ]`. `$STATUS_JSON` at line 46 **already holds** `enforcement.layers[]` with `active`/`detail`/`script` — confirmed by running `bk status --json`. |
| 5 | `bk gate check-write` answers "allowed" for a gated page | `application/gate.py:172–173` | Read. Docstring confirms resolution is against *vault root*, not cwd — deliberate, undocumented in `--help` and `docs/commands.md`. |
| 6 | `bk --version` reports `0.4.0`; distribution is `0.5.0` | `src/brainskit/__init__.py:3` vs `pyproject.toml:10` | Read. Tag at HEAD is `v0.5.0`. `release.yml:41` gates only `[project].version`; `__version__` is never checked. |

Architecture-track claims verified the same way: `analyze.py:6` does a
module-level `from graphify.build import edge_data`, where `edge_data` is
**13 lines** and `build.py` is 1,643 lines importing `validate.py` (95) plus
`networkx` at load — against a hard `networkx>=3.4` pin at `pyproject.toml:82`.
`taxonomy_seed` has five write/parse sites in `domain/model.py`, one writer at
`onboarding.py:504` (`sorted(branches)`) and **zero readers**.

---

## 2. Goals

- **G1** No boundary fails open. Unknown provenance resolves to `never-ingest` on
  every path, not only in `Enrichment`.
- **G2** No surface claims a mechanism is active without exercising it.
- **G3** Version identity is single-sourced and gated in CI.
- **G4** A vault can be initialised without a TTY, from documentation alone.
- **G5** Graph answers carry their own staleness; no query answers authoritatively
  about a tree that is gone.
- **G6** The vendored tier costs what it delivers: drop the `networkx` pin and the
  dead regions, keep the extractors.
- **G7** Docs and code agree — where they disagree today, code moves.
- **G8** `0.6.0` published to PyPI, with the README's install line true.

## 3. Non-goals

- Reworking the apply gate, the privacy redaction that *is* applied, the security
  posture, or the test-quality bar. The audit found these sound; §7 pins them.
- Rewriting the vendored `codeanalysis/` extractors. 27 modules, ~12,000 LOC,
  ~93% live. Only the *tier above* them is in scope.
- Re-publishing `0.5.0`. Remediation lands first; the release is `0.6.0`.
- The 23 low/cosmetic findings as a blocking set — they ride along in Track G
  where cheap, and otherwise go to `later.md`.

---

## 4. Cross-cutting requirement — the negative control

**Every fix in this programme ships with a test that fails when the fix is
reverted.** This is a requirement, not a convention, and it is the audit's
central lesson:

> `tests/test_fix_integrations.py:1182–1216` has four Obsidian consumer tests.
> All four assert only that the stored string validates — `"clod"` rejected,
> `"cloud"` accepted. Not one asserts the value changes what gets written, so
> **all four pass with the filtering deleted**, which is effectively the current
> state.

A test that asserts a value was accepted is not a test that the value does
anything. Assert on **content** — the literal secret string, the branch name, the
file that should not exist — the way the privacy tests already do.

Each issue below carries an explicit **Negative control** line. An issue whose
control passes with the fix reverted is not done; the control is broken.

---

## 5. Requirements

Priority key: **P0** ship-blocker · **P1** must, pre-release · **P2** should ·
**P3** ride-along.

### Track A — Close the fail-open boundary

| # | Requirement | Priority |
|---|-------------|----------|
| A1 | `_evidence_privacy` distinguishes "declares no sources" (system page → cloud) from "declares sources that do not resolve" (unknown provenance → `NEVER_INGEST`). Same for `_evidence_branches` returning `[]`. | **P0** |
| A2 | Obsidian sync filters `wiki/` and `raw/` by consumer, not just the graph object. | **P0** |
| A3 | `bk graph` stamps a consumer and filters, matching every sibling path at `projections.py:143–161`. | P1 |
| A4 | A `sourced_from` edge with an unknown hash gets **one** answer, not two — `infrastructure/graph.py:45` silently drops it while `health.py:310` reports it as a lint finding. | P1 |
| A5 | `strictest_privacy`'s docstring asserts an invariant ("every caller checks provenance resolves first") that A1 makes true. Enforce it rather than assert it — make the empty case a refusal at the helper, so a future caller cannot reintroduce the bug. | P1 |

### Track B — Make the reporting surfaces honest

| # | Requirement | Priority |
|---|-------------|----------|
| B1 | `brainskit-status.sh` reads `enforcement.layers[]` from the JSON it already holds. Delete lines 83–96; add no new logic. | **P0** |
| B2 | `bk gate check-write` is unambiguous — resolve against cwd, or state the base in `--help`, in `docs/commands.md`, and in the output. | **P0** |
| B3 | `bk status` never prints `✓ vault healthy` above `✗` enforcement rows (`health.py:173` sets it from lint alone). | P2 |
| B4 | `bk code` traversals (`hubs`, `affected`, `path`, `communities`, `cycles`, `diff`, `data`) attach `staleness()` to their answer. `data()["state"]` stops being permanently `None`. | P1 |

### Track C — Release integrity

| # | Requirement | Priority |
|---|-------------|----------|
| C1 | One source of version truth. `release.yml` asserts tag == `[project].version` == `__version__`; `verify-wheel.sh` compares them too. | **P0** |
| C2 | Diagnose why `publish` and `github-release` produced no output on a *Success* run. Neither job may be able to no-op silently again. | **P0** |
| C3 | Publish `0.6.0` to PyPI for real; README install lines and badges become true. Cut the GitHub Release. | P1 |
| C4 | `SECURITY.md`'s "latest release only" is satisfiable — a reporter following the policy names a supported version. | P3 |

### Track D — Onboarding

| # | Requirement | Priority |
|---|-------------|----------|
| D1 | `bk init --print-config [--preset …]` emits a complete, valid policy. The wizard already assembles one at `onboarding.py:464–522`; expose it. Both refusals name the new command. | P1 |
| D2 | **Implement `taxonomy_seed`** — wire it into the ingest job's branch taxonomy as originally intended. It currently has zero readers while being `required`. | P1 |
| D3 | Delete the here-doc sentence at `docs/getting-started.md:29`; it documents behaviour that does not exist. | P1 |
| D4 | `bk capture` gets a human renderer (it is the second command in every quickstart and dumps raw JSON). Apply refusals name the next command; `missing_base_hash` says that `observed` is the value to paste. | P2 |

### Track E — Correctness

| # | Requirement | Priority |
|---|-------------|----------|
| E1 | `config.branches[branch]` becomes `.get()` + `PolicyError` — matching `llm.py:119` for the identical question. A bare `KeyError` currently escapes four read paths after the *documented* `bk reconcile`, bypassing the JSON error envelope. | P1 |
| E2 | `search(limit=N)` returns N, not N+1, when N < 4. It propagates into `context`, where `limit` bounds how much evidence reaches a model. | P1 |
| E3 | Provider outages raise `NotConfiguredError`, not `validation_error` — an agent currently rewrites a well-formed request against a down provider. The two re-wrapping handlers (`integrations.py:536, :1189`) must re-raise `type(exc)` or they flatten it back. | P1 |
| E4 | Slug uniqueness enforced at apply, with a lint code. Duplicate slugs across page kinds silently mis-route every `[[link]]`, last-writer-wins by directory sort order. | P2 |
| E5 | Scoped code-graph builds prune. `_unexplained` and `parseable_files` persist into the artifact so a coverage gap survives longer than one line of build output. | P2 |

### Track F — Architecture

| # | Requirement | Priority |
|---|-------------|----------|
| F1 | Implement `cycles` (Tarjan) and `diff` (set difference) natively on brainskit's own normalised `code.json` — it already builds the `nx.DiGraph`. Removes the `graphify.build` import chain: 1,738 LOC pulled in for a 13-line helper, plus the mandatory `networkx>=3.4` pin. `cluster.py` remains the only genuine delegation. | P2 |
| F2 | Move the jsonschema engine (`domain/model.py:1174–1271`) to `application/`. It is the domain layer's only third-party dependency, and every caller is already in `application`. Zero coupling cost. | P2 |
| F3 | De-triplicate the installer contract. Gate constants already live in `application/gate.py` and `test_layering.py:34` permits `interfaces → application` — `cli.py` imports them (two-line fix). Only the managed-block sentinel moves downward. | P2 |
| F4 | Close the two layering-test gaps: the dynamic-import scan filtered with `if "codeanalysis" in node.value` closes only the hole already known; `ALLOWED` permits `infrastructure → application` more broadly than `architecture.md` shows (which is how `graph.py:8` imports a plain function rather than a port unnoticed). | P2 |
| F5 | Remove the precisely-measured dead vendored regions (1,938 LOC ≈ 6.4%): `build.py` builder + `validate.py` (845), `detect.py` pipeline + `google_workspace.py` (634), `security.py` HTTP fetch stack (211). Update `codeanalysis/NOTICE` and keep `test_vendoring.py:40`'s pin honest. | P3 |

### Track G — Docs truth

| # | Requirement | Priority |
|---|-------------|----------|
| G1 | Correct "read-only" in `docs/serving.md:24` and `docs/architecture.md:4` — `do_POST` handles four write endpoints. Document `/api/code-graph` (the real GET surface is 11, not 10) and that the bearer token is optional (`web.py:480` returns `True` when none is configured). | P1 |
| G2 | **Rename graph objects to brainskit branding** — `BrainkitNode` → `BrainskitNode`, `brainkit` schema → `brainskit`, matching `docs/integrations.md:33`. Ship a migration path so an existing subgraph is not orphaned; update `CHANGELOG.md:64–67`, which currently records the opposite decision. | P2 |
| G3 | 26 flags gain help text; the 32 subcommands mention `--json` and `--vault`. | P3 |
| G4 | `--force`'s help promises a guard against initialising over other projects that is not implemented. Implement it or correct the help — notable given the incident at `codegraph.py:226` where a vault "merely holding projects" indexed 55,295 files. | P3 |

---

## 6. Sequencing

Four phases. Each is independently shippable and leaves the tree green.

**Phase 1 — Stop the leak, stop the lying** (P0) → `A1 A2 B1 B2 C1`
The two criticals, the session-opening banner, the command people use to verify
the central claim, and the version gate. Ordered exactly as the audit's own
suggested order, because a boundary that fails open is worse than no boundary —
it reports `redacted_nodes: 0` while leaking.

**Phase 2 — Honest answers and the way in** (P1) → `A3 A4 A5 B4 D1 D2 D3 E1 E2 E3 C2`
Everything that makes an answer trustworthy, plus non-interactive init.
`C2` (diagnose the silent release no-op) lands here so Phase 4 can publish.

**Phase 3 — Architecture** (P2) → `F1 F2 F3 F4 B3 E4 E5 G2`
The tier above the vendored tree, the layer boundary, and the object rename with
its migration. Largest phase; `F1` is the single biggest item.

**Phase 4 — Docs truth and release** (P1/P3) → `G1 G3 G4 F5 C3 C4`
Reconcile the docs against what Phases 1–3 made true, then cut `0.6.0`.

**Gate between every phase:** full suite green (911 baseline, rising), `ruff`
clean, `bk lint` clean on `docs/brain`, and every negative control for that
phase's issues demonstrated failing-then-passing.

---

## 7. Do not regress these

The audit states these explicitly because *five agents finding this much means
the absence of findings elsewhere is a result, not a gap in coverage*. Each is a
regression tripwire for this programme:

- Security fundamentals — path traversal (lexical + realpath + symlink
  containment), SQL/Cypher parameterisation, all eight subprocess sites (fixed
  argv, no `shell=True`, all timed out), secret redaction in driver errors,
  zip-bomb and billion-laughs defences in the docx converter.
- The apply gate — whole-batch validation before staging, citations equalling
  declared sources exactly, journal-based rollback from `FileVault.__init__`,
  idempotent `proposal_id` retries raising `ConflictError` on payload change.
- Test quality — zero tautological assertions and zero assertion-free tests
  across all 23 files, by AST sweep.
- `bk doctor`'s live gate probe, which creates no probe file. The exists-vs-runs
  bug **is** fixed there. `bk status`'s enforcement detection is also correct,
  catching both `core.hooksPath` redirection and unregistered hooks — only its
  two consumers throw the answer away, which is B1.
- Privacy redaction where applied — `{"redacted": 1, "count": 0}` with no leak of
  filename, branch or content. `graph_data` filters after expansion.
- Benchmarks reproducing from `benchmarks/baseline.json`; `docs/commands.md` with
  zero wrong command names or flags across 32 commands; the CHANGELOG.
- Vendoring governance — `codeanalysis/NOTICE` declares the three files departing
  from byte-identity, and `test_vendoring.py:40` pins that list. **F5 must extend
  this discipline, not weaken it.**
- Bare `bk` on a TTY opening an interactive command picker.

---

## 8. Acceptance criteria

- [x] `_evidence_privacy` returns `NEVER_INGEST` for unresolvable provenance; the
      reproduction from the audit (forget a record, read the orphaned page as
      `cloud`) no longer leaks, and the page is not stamped `"privacy": "cloud"`.
- [x] Obsidian sync with `consumer="cloud"` writes no file containing the secret,
      with and without `include_raw`.
- [x] `sh brainskit-status.sh | tail -2` agrees with `bk status` in both the Husky
      case and the hooks-present-but-unregistered case.
- [x] `bk gate check-write` returns the same verdict for a relative and an
      absolute path to the same file — or names its base in `--help`, in
      `docs/commands.md`, and in its output.
- [x] `bk --version`, `pyproject.toml`, the git tag and MCP `serverInfo.version`
      all report the same string; CI fails if they diverge.
- [x] `bk init ./v --config <(bk init --print-config)` succeeds off a TTY.
- [x] `taxonomy_seed` has at least one reader, reachable from `bk ingest`.
- [x] No `bk code` traversal answers without a staleness signal; `data()["state"]`
      is non-null.
- [x] `search(limit=N)` returns exactly N for N ∈ {1,2,3}.
- [x] `networkx` is no longer a required dependency; `graphify.build` is not
      imported at module level anywhere.
- [x] Neo4j/Postgres object names match `docs/integrations.md`, with a documented
      migration for existing subgraphs.
- [x] `pypi.org/simple/brainskit/` returns 200 and `uv tool install brainskit`
      installs `0.6.0`.
- [x] Every issue's negative control demonstrated: reverted → fails, restored →
      passes.
- [x] Full suite green, `ruff` clean, `bk lint` clean.

---

## 9. Open questions

Resolved during planning (12 Aug 2026):

| Question | Decision |
|---|---|
| Scope | Full remediation **including** the architecture track. |
| Release | **Fix first, then dispatch a new version.** `0.5.0` is not re-published; remediation ships as `0.6.0`. |
| `taxonomy_seed` | **Implement it** (D2). |
| Neo4j/Postgres object names | **Code follows the docs** (G2) — rename + migration path. |

Still open:

- **Why did the release workflow report Success while publishing nothing?**
  `gh` is unauthenticated in this environment, so the run logs were not readable
  during planning. C2 is the spike; the answer determines whether C3 is a
  configuration fix or a workflow rewrite.
- **What is `taxonomy_seed` a seed *for*, precisely?** `onboarding.py:504` writes
  `sorted(branches)`, which suggests the ingest job's branch taxonomy — but with
  zero readers, the intended semantics are inferred, not recorded. D2 needs this
  pinned before implementation, or it will encode a guess.

---

## 10. Method note from the audit

Worth carrying into execution, because two agents corrected themselves mid-report
and both corrections mattered:

- The graph agent's "47-file coverage gap" was an artifact of comparing today's
  survey against a **pre-rename** graph. A fresh whole-root extraction has a gap
  of **exactly zero** — the pipeline is healthy today. E5's defect is that if it
  stops being healthy, the evidence is unrecoverable.
- The architecture agent's first reachability pass followed only `graphify.`-
  prefixed imports and missed relative ones, over-reporting dead modules. The
  vendored tree is 30,105 LOC and **~93% live**, not the "~20k of unused bulk"
  it was briefed as.

Also flagged: `ruff format --check` could not be run during the audit because the
string `format` trips this project's own `block-destructive-commands.sh` hook.
**That false positive is worth fixing** — it currently blocks a standard
formatting check.

---
<!-- doc-tracking -->
- Created: 2026-08-12
- Updated: 2026-08-12 10:06
