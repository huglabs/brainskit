# Stress Test Report — Code-Graph Node Generation

**Date:** 2026-08-24 · **Version:** `bk` 0.7.0 (repo `.venv`, 29/29 grammars) · **Scope:** `bk code build` / `status` / query surface
**Harness:** `/var/folders/.../T/opencode/stress/harness.py` — 9 corpora, ~600 files, driven through the real CLI (`--json`), results in `stress/results.json`.

## Verdict at a glance

| # | Case | Result |
|---|------|--------|
| 1 | Ground truth: 10 modules, 70 known symbols | **PASS** — all 70 symbols + 10 module nodes, ids collision-safe |
| 2 | Polyglot: 22 languages, one file each | **PASS** — every language yielded ≥2 nodes |
| 3 | Scale: 500 files (parallel pool), 1,500 expected nodes | **PASS** — exactly 1,500 nodes, cold 1.6 s / warm 1.1 s |
| 4 | Adversarial: syntax errors, binary-as-.py, BOM+CRLF, unicode ids, 5,000-fn file, emoji filenames, 39-deep nesting, import cycles | **PASS** — no crash; every parseable file credited |
| 5 | Symlink shadowing (parallel path) | **BUG B-1** |
| 6 | Duplicate content, two paths | **PASS** — both paths credited (sequential and parallel) |
| 7 | Incremental edit + delete + rebuild | **PASS** — stale detection, add/drop of symbols correct |
| 8 | Scoped build on missing path | **PASS** — clean `not_found`, exit 2 |
| 9 | Concurrent builds (3 × simultaneous) | **PASS** — atomic write held, graph intact |
| 10 | Scoped build after an out-of-scope edit | **BUG B-2** |
| 11 | Unexplained gap vs. stored artefact | **BUG B-3** |
| 12 | Symbol lookup by bare function name | **BUG B-4** |

Node generation itself is fundamentally sound: ground-truth counts are exact, the parallel pool loses nothing on ordinary trees, adversarial inputs never corrupt or crash the graph. The four bugs are all at the seams — symlinks, scoped merges, persistence of coverage gaps, and label-based lookup.

---

## Bugs

### B-1 · A symlink steals node credit from the real file under parallel extraction

**Repro:** 30 files + `alias.py → f0.py` (>20 files forces the process pool). Result: `f0.py` gets **zero** nodes; `alias.py` carries them. With ≤20 files (sequential path) *both* paths are credited.

- Build output: `"files": 30` (undercounted — 31 eligible), `unexplained_files: 1`.
- Same-content twins without a symlink are fine under both paths, so this is specific to symlink→same-file dedup racing in the pool.
- Which path wins is an artifact of worker scheduling: in a sequential corpus the real file won; in two parallel corpora it lost. Attribution is nondeterministic.
- Impact: the graph says the *alias* holds code that lives in the real file; tooling that reads `path` to navigate lands on a symlink.

**Fix direction:** resolve symlinks to their real path before content-dedup, credit the canonical file, and either skip alias paths explicitly (reported as skipped) or emit nodes for the canonical path only — deterministically.

### B-2 · A scoped rebuild marks out-of-scope edits as fresh without re-extracting them

**Repro:** full build of `{keep/x.py, drop/y.py}` → edit `drop/y.py` (`dy` → `dy_new`) → `bk code build keep`.

- Graph still contains `dy()`, not `dy_new()` — correct, it was out of scope.
- But `files["drop/y.py"]` was refreshed to the hash of the **edited** bytes, so `code status` reports `state: "fresh", changed: []`.
- The graph now lies until the next full build, with freshness asserting otherwise — the exact failure the freshness system exists to prevent.

**Fix direction:** a scoped build may update hash records only for files inside its scope; out-of-scope files must keep their recorded hashes so `staleness` keeps reporting them as changed.

### B-3 · `unexplained_files` is reported but never persisted — `code status` then says "fresh"

**Repro:** same corpus as B-1. `code build --json` prints `unexplained_files: 1, unexplained_ratio: 0.0323`; the stored `graph/code.json` coverage block holds only `{root, files, grammars_missing, unreachable_files}`; `code status` reports `state: "fresh"`.

- Code path: `_unexplained()` (`application/codegraph.py:657`) is merged into the command *result* (`codegraph.py:606-607`) but never into the artefact's coverage dict, and `staleness` (`codegraph.py:851-879`) reads only `grammars_missing` / `unreachable_files`.
- So the one channel designed to catch "a file contributed nothing for no stated reason" survives only as long as the build output stays on screen. This is what let B-1 end in a `fresh` verdict.

**Fix direction:** persist `unexplained_files` (+ ratio) in the stored coverage block and have `staleness` downgrade to `partial` while it is > 0.

### B-4 · `code path` cannot find functions by bare name

**Repro:** graph contains `helper()` (`lib.py`). Queries:

- `code path App helper` → `not_found`
- `code path App "helper()"` → found, 2 hops
- qualified forms `App.run`, `lib.helper` → not_found

Cause: `_resolve` (`application/codegraph.py:1266`) matches id, then exact label, then case-folded label — but function labels are minted as `name()`. Every natural spelling of a function name fails with a message ("No such symbol") that misdescribes the problem. Classes work only because their labels have no suffix.

**Fix direction:** in `_resolve`, also try `wanted + "()"` and the paren-stripped form; optionally accept `file.name` / `Class.method` qualified names. Cheap, local, and removes the most common dead end on the query surface.

---

## Environment observation (user's machine)

The installed uv-tool `bk` currently has **14/29 grammars**; missing: swift, kotlin, lua, scala, elixir, zig, julia, fortran, groovy, hcl, objc, pascal, powershell, dm, verilog. Files in those languages contribute **zero nodes**, exit code stays 0. Mitigation is good — a loud stderr warning naming each grammar and a runnable install command, plus `status.state: "partial"` with `missing_grammars` — but any scripted/CI use that ignores stderr gets silently smaller graphs. Install:

```
uv pip install --python ~/.local/share/uv/tools/brainskit/bin/python \
  tree-sitter-swift tree-sitter-kotlin tree-sitter-lua tree-sitter-scala \
  tree-sitter-elixir tree-sitter-zig   # + others as needed
```

If "nodes aren't processing properly" was observed on a polyglot repo, this is the first thing to check: `bk code status` will say `partial` and list exactly what fell out.

## Points of improvement (non-bug)

1. **Persist and act on coverage gaps** (B-3) — the single highest-leverage change; it converts every silent-loss class into a visible `partial`.
2. **Deterministic attribution for duplicate/symlinked content** (B-1): sort candidates canonically before dedup so repeated builds attribute identically.
3. **Scoped-build semantics** (B-2): scope should bound *extraction*, never *freshness accounting*.
4. **Symbol lookup ergonomics** (B-4): bare-name and paren-tolerant matching; document the ambiguity rule (it already lists candidates nicely when >1 match).
5. **Build output undercount:** `"files"` reports credited files, not eligible ones; pairing it with persisted `unexplained_files` would make the number self-explaining.
6. **Performance headroom is excellent** (500 files ≈ 1.6 s cold, warm rebuilds ~0.19 s after a one-file edit); no work needed there.

## What was verified working

Exact ground-truth node counts; module-id collision handling across same-named modules (`pkg1_util_shared` ≠ `pkg2_util_shared`); 22-language extraction; 5,000-function single file; unicode identifiers; CRLF+BOM; emoji/spaced filenames; 39-level directory nesting; import cycles; deleted-file cleanup on rebuild; staleness transitions fresh→stale→fresh; concurrent-build atomicity; refusal of nonexistent scoped paths; privacy/consumer gating on every read answer.

---

## Resolution (2026-08-25)

All four bugs fixed; improvement points 1–5 implemented. Regression tests in
`tests/test_fix_node_generation.py` (21 cases); full suite 1,568 passing,
ruff + mypy clean.

- **B-1** — `_canonical_files()` (`infrastructure/extractor.py`) collapses every
  collected path to one entry per real file before extraction: a real file
  always beats a symlink to it, ties break lexicographically, distinct files
  with identical bytes both survive. Applied identically in `survey`, so
  `unexplained` arithmetic stays consistent.
- **B-1's ghost** — discovered during the fix: the AST cache salts entries by
  the *resolved* path, so alias and target share a key and the misattributed
  payload was served back after the fix. `_cache_format_marker` now hashes the
  adapter itself, so this change (and any future adapter change) starts a
  fresh cache namespace.
- **B-2** — `_merge_scoped` now returns a freshness carry: stored digests for
  out-of-scope files, which `_write` uses instead of re-hashing from disk.
  A scoped build leaves an out-of-scope edit visible as `stale/changed` until
  a full rebuild reads it (verified end-to-end).
- **B-3** — `_coverage` persists `unexplained_files` (explicit `0` on a
  whole-root build) in the artefact; `staleness` answers `partial` with the
  count while it is > 0; `bk code status` renders a loud line for it;
  pre-existing graphs without the field stay `fresh`.
- **B-4** — `_resolve` matches labels with the `()` suffix optional on either
  side, case-insensitively, then accepts qualified `file-stem.name` spellings
  (`lib.helper`). Ambiguity still refuses with candidates; unknown names still
  raise not-found.
- **Grammar health + update check** — new `grammar_audit()` reports each
  distribution's installed version; `doctor_report` compares against the pins
  brainskit declares in its own dependency metadata (no second table), flags
  `grammars_outdated` with the violated range, prints an "outdated grammars"
  table plus a single upgrade command covering missing *and* outdated, counts
  outdated toward `healthy`, and the headline names it. Comparator handles
  `>= <= == != > < ~=`; unparseable clauses never produce a false alarm.

Re-running the original stress harness: **9/9 PASS**, including the two cases
that failed at report time.
