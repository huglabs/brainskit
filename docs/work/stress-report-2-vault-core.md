# Stress Test Report #2 — Vault Core: Capture, Search, Privacy, Apply

**Date:** 2026-08-25 · **Version:** `bk` 0.7.0 · **Surface:** capture pipeline, FTS5 search, privacy boundaries, apply gate
**Harness:** `stress/harness2.py` — 12 cases over 6 fresh vaults (~600 captures), all driven through the real CLI; results in `stress/results-vault.json`.

## Verdict at a glance

| # | Case | Result |
|---|------|--------|
| 1 | Capture scale: 200 files via CLI, registry integrity | **PASS** — 200/200, unique hashes, lint clean |
| 2 | Duplicate content under a new name | **PASS** — deduped by content hash (`created: false`) |
| 3 | Concurrent captures: 24 processes × 8 workers | **PASS** — all exit 0, locked registry intact |
| 4 | Adversarial FTS5 queries (12 shapes) | **PASS** — clean envelopes everywhere |
| 5 | Search limit boundaries (0 / 1 / 5 / 10 000) | **PASS** |
| 6 | Privacy across three branches | **PASS** — human 3 / local 2 / cloud 1 hits |
| 7 | Wiki page citing only never-ingest evidence | **PASS** — invisible to cloud consumer |
| 8 | Apply scale: 50 linked pages in one transaction | **PASS** — 50 written, 0.23 s, lint clean |
| 9 | Apply idempotent re-submit (same id + payload) | **PASS** — `idempotent: true`, same transaction |
| 10 | Apply id reuse with different payload | **PASS** — refused cleanly |
| 11 | Stale `base_hash` conflict | **PASS** — refused, disk unchanged |
| 12 | Unresolvable citation (uncaptured hash) | **PASS** — refused before staging |
| 13 | SIGKILL mid-apply (7 windows, up to 300 ops) | **PASS** — vault always opens consistent |

The core is solid. No bugs were found this round; three minor findings below.

## What was exercised

**Capture integrity.** 200 CLI captures at ~141 ms each (process spawn dominates; the engine itself is single-digit ms per capture per the v0.2 benchmark). Every hash unique, registry consistent, lint clean afterwards. Identical content captured under a second filename is deduped — identity is the content hash, exactly as documented.

**Concurrency.** 24 simultaneous captures across 8 threads: zero lost updates, zero corruption. The locked/atomic registry write holds.

**Search robustness.** FTS5 operator abuse (`AND`, `OR`, `NEAR(`, unbalanced quotes, parens, leading wildcards), CJK and accented unicode, empty query, 4 KB query — every one returns either a result envelope or a clean error envelope. No traceback ever escapes. Limit edges behave (`--limit 0` is rejected by argparse cleanly).

**Privacy, including the expansion path.** With branches at `local-only` / `cloud` / `never-ingest`: human sees 3 sources, local 2, cloud 1 — filtering exact at every consumer. The sharper case: a wiki page whose only citation is never-ingest evidence applies successfully to the wiki, and a cloud-consumer search for its distinctive claim text returns **zero hits, redacted: 1**. Page-level privacy inheritance from cited sources works after retrieval expansion, which is where the historical leak class lived.

**Apply gate.**
- 50 cross-linked pages in one proposal: atomic batch, all-or-nothing, FTS updated, lint clean.
- Re-submitting the same `proposal_id` + payload → `idempotent: true`, same transaction id.
- Same id, tampered payload → refused ("proposal_id was already used for a different payload").
- Wrong `base_hash` on an existing page → refused, page on disk untouched.
- Citation of a hash nothing captured → refusal before any file was staged.
- Near-duplicate pages trip the novelty gate (`insufficient_novelty`, similarity 1.0) — duplicate-content pages cannot inflate the wiki silently.

**Crash safety.** SIGKILL at 7 windows (4 ms → 85 % of a 0.92 s / 300-op apply): every killed run left the vault openable, status and lint clean, and the outcome strictly all-or-nothing (0 or all pages — never a fraction). The write-ahead journal does its job; the commit window is narrow enough that kills land either side of it, not inside it.

## Findings (minor)

1. **A query starting with `-` cannot be typed.** `bk search -retrieval …` died with "the following arguments are required: query" (exit 2, no traceback) — argparse refuses any positional beginning with `-`. **FIXED (2026-08-25):** the CLI now hoists unknown dash-leading tokens after `search`/`context`/`ask` behind `--`, so the natural spelling parses to the intended query. Guard rails: known flags keep their values (`--limit -1` still means minus one), an explicit user-supplied `--` is left untouched, a real positional beside a mistyped flag still goes to argparse's error, and non-free-text commands (`capture -`) are untouched. Tests: `DashLeadingQueryTest` in `tests/test_onboarding.py`; stress harness case updated to require a real hit.
2. **`insufficient_novelty` refusals are strict about templated bodies.** Legitimately structured pages differing by a counter (e.g., generated notes) will be refused at similarity 1.0. Correct behavior, but worth knowing when scripting bulk applies: vary the prose, not just the numbers.
3. **CLI capture throughput is process-spawn-bound** (~140 ms/capture). Bulk-import paths that need thousands of captures should use the in-process service or watch mode rather than looping the CLI.

## Conclusion

Unlike round 1 (code graph, 4 real bugs), the vault core came through clean: privacy filtering held at every consumer including the expansion path, apply transactions are genuinely atomic and recoverable, and adversarial input never produced a traceback. All three findings are usability notes, not correctness defects.
