# Later

The batches from the close-out review that are real but individually small, the
heuristics that work today and have a named clean end-state, the mechanical
sweeps that have never been run, and the things decided rather than deferred.

Full findings and their evidence:
[`04-review.md`](../../archive/2026-Q3/main-field-audit-remediation/04-review.md).
Each row below is tracked as an issue under the
[**Later**](https://github.com/huglabs/brainskit/milestone/3) milestone — the row
carries the evidence, the issue carries the workflow.

## Docs truth and UX, from the fresh-user track

Both series were raised as groups by the agent that installed `0.6.0` from PyPI
and followed `docs/getting-started.md` literally, and neither was individually
re-derived in the write-up. They now have one issue each, but the caution that
came with the group applies to every row: re-derive against the tree before
working one, since several are likely obsoleted by the `0.6.1` and `0.6.2`
work — `docs/commands.md`, `docs/getting-started.md` and `docs/integrations.md`
all changed in the `0.6.2` batch. The lines below are the shape of each finding,
not its evidence; the issue carries that.

`T*` are docs-truth gaps — places the documentation describes behaviour the
installed release does not have, distinct from the D-series, which are defects in
the product rather than in the prose. `U*` are points of onboarding friction; the
sharpest of them, `U1` — `bk ask` refusing an entire query because BM25 recall
brushed a `never-ingest` source — has since been re-derived to
`application/jobs.py:53-56` and `infrastructure/llm.py:140-144` and is carried on
[`next.md`](next.md), so only `U2`–`U4` are here. None of the three has a code
citation yet.

| ID | Intent | Issue |
|----|--------|-------|
| T1 | `bk init`'s "Next" block lists three commands that all fail from where it leaves you. **Fixed on `release-0.8.0`, pending release:** each command carries `--vault <path>` exactly when discovery from the current directory would miss the vault, and `hooks install` carries `--root <project>` when the vault is nested in a git repository (`interfaces/cli.py` `_init_next_steps`). The steps are in `--json` as `next`, and `bk capture`'s `next` line follows the same rule | [#23](https://github.com/huglabs/brainskit/issues/23) |
| T2 | The recommended `[code]` extra still lands on grammars 13/29 and `healthy: false`. **Fixed on `release-0.8.0`, pending release**, by the same change as P4 ([`next.md`](next.md)): 13/29 is what the extra carries, so the report now reads `13/29 (code extra complete)`, and `healthy` no longer depends on the extra | [#24](https://github.com/huglabs/brainskit/issues/24) |
| T3 | The no-graph hint names `bk code import`, but the docs — and a fresh user — need `bk code build`. **Fixed on `release-0.8.0`, pending release:** `CodeGraph.staleness` and the no-graph refusal name `CODE_REBUILD_COMMAND` (`bk code build`) for `missing`, unverifiable and `stale`; `docs/code-graph.md`'s table says the same | [#25](https://github.com/huglabs/brainskit/issues/25) |
| T4 | `bk init`'s header reports 4 ollama models; the picker offers 3. **Fixed on `release-0.8.0`, pending release:** the picker lists every model ollama reports, those without tool support dimmed and unselectable, and the header counts them: `4 models (1 without tool support)` (`interfaces/onboarding.py` `_ollama_choices`, `_model_count`) | [#26](https://github.com/huglabs/brainskit/issues/26) |
| T5 | `output/` layout: `resurface/` is undocumented, `reports/` is never written. **Fixed on `release-0.8.0`, pending release:** `bk init` creates `output/resurface` instead of `output/reports`, and `docs/getting-started.md` (and its pt-BR twin) documents `digests/`, `resurface/`, `answers/` and `export-<target>.<ext>` | [#27](https://github.com/huglabs/brainskit/issues/27) |
| T6 | `README` pins `@v0.5.0` and calls 18 wheels "one dependency". **Fixed on `release-0.8.0`, pending release:** both READMEs pin `@v0.8.0` and name the one declared dependency, `jsonschema[format]`, and the ~17 packages it resolves to; the release checklist in `docs/development.md` now says to bump the pin | [#28](https://github.com/huglabs/brainskit/issues/28) |
| T7 | `bk reconcile` after moving a raw file blames the wiki-page set. **Fixed on `release-0.8.0`, pending release:** projection records keep per-input digests (`inputs`: pages, raw sources, raw paths), and lint names what changed — "was built before a raw source moved" (`application/freshness.py` `_drift_reason`) | [#29](https://github.com/huglabs/brainskit/issues/29) |
| U2 | The `bk init` default model (`qwen2.5:3b`) cannot complete `bk ingest`. **Fixed on `release-0.8.0`, pending release:** the default stays; `bk init` warns when `job_models.ingest` routes to an ollama model under 7B parameters (size from what ollama reports, else the tag; unknown size does not warn — `interfaces/onboarding.py` `small_ingest_model_warnings`), and `--json` carries the list as `warnings`. A judgment job that exhausts its repair attempts (ingest's `citation_mismatch`) names the provider and model in `details` and hints at routing `job_models.<job>` to a larger model (`application/judgment.py` `_exhausted_remedy`) | [#30](https://github.com/huglabs/brainskit/issues/30) |
| U3 | Renderer discipline is uneven — `hooks install` and `code status` dump raw JSON in human mode. **Fixed on `release-0.8.0`, pending release:** both have human renderers (`interfaces/cli.py` `_render_hooks`, `_render_code_status`); `--json` is unchanged | [#31](https://github.com/huglabs/brainskit/issues/31) |
| U4 | `bk lint` prints `✓ 1 warning(s)`: a green tick above warning text. **Fixed on `release-0.8.0`, pending release:** a third headline, `console.warn_line`, prints `!`; a warnings-only lint uses it, and so does `bk code status`'s unexplained-files note, which was a `✗` in warning colour | [#32](https://github.com/huglabs/brainskit/issues/32) |

## Integrity checks that are shape, not identity

Two checks that hold today, are documented as deliberate in the code, and each
has an end-state that *deletes* the heuristic rather than tightening it. Neither
is urgent; both are the kind of thing that is cheap while the surrounding code is
fresh and expensive once it is not.

| ID | Intent | Issue |
|----|--------|-------|
| TC2 | `views/` integrity matches the generated marker by **shape** — `^<!-- generated by \S+; do not edit -->` at `application/freshness.py:96` — rather than against `GENERATED_MARKER` itself. That is correct as written: this repo's own `docs/brain/views/home.md` still opens with `<!-- generated by brainkit; do not edit -->`, pre-rename, and a strict comparison reported the live vault as `malformed`. The consequence is that a `views/home.md` gutted to its marker alone reads `fresh`, since the fingerprint comparison never opens the file either. Clean end-state: have `_record_projection` (`application/health.py:762-786`, already storing `source_hash`) stamp the artefact's own hash at generation time, which answers "is this the artefact we wrote" exactly and lets the shape match be deleted rather than tightened. **Fixed on `release-0.8.0`, pending release:** done as described — `record_projection` stores `artefact_hash`, the hash of the anchor just written, and an anchor whose bytes differ is `malformed`; `_views_integrity` and the marker-shape regex are deleted. An artefact written before the stamp existed reads the new state `unverified` (regenerate-class, so `stale: true`) until one run of its command | [#33](https://github.com/huglabs/brainskit/issues/33) |
| TC3 | `_is_seeded_shape` (`application/health.py:74-90`) decides whether `wiki/index.md` and `wiki/log.md` are untouched by asserting the body is a single `# ` heading. Its own docstring says what it cannot do: replacing the heading with a *different* heading passes. It exists because the seeded pages have no freshness entry and so no hash to compare against. Clean end-state: have `Vault.initialize` (`infrastructure/vault.py:125`, seeding both pages at `:174-175`) record freshness entries for them, which deletes the exemption, the heuristic and `SEEDED_SYSTEM_PAGES` in one move and gives those two pages the same hash comparison every other page gets. **Fixed on `release-0.8.0`, pending release:** by a different route than the one sketched — not freshness entries written by `Vault.initialize` (infrastructure may not reach into the application layer, and an entry would age and enter the projection fingerprint), but **seed records**, a `seeded` table in `.brain/freshness.json` that lint writes via `FreshnessLedger.record_seeded` while a seed page is byte-identical to the template (`domain/model.py` `is_seed_template`). `bk init` records them before it returns; lint then compares the page against `seeded_hash` as it compares `applied_hash`. `SEEDED_SYSTEM_PAGES` and `_is_seeded_shape` are deleted | [#34](https://github.com/huglabs/brainskit/issues/34) |

## Test quality, left over from TQ2

TQ2 ([`next.md`](next.md), [#19](https://github.com/huglabs/brainskit/issues/19))
closed the strict shape — an assertion directly in the body of a loop over a
production-controlled collection — and left the nested one as a known, measured
remainder rather than widening that sweep.

| ID | Intent | Issue |
|----|--------|-------|
| TQ2-nested | Extend `NoAssertionHidesInAnEmptyLoopTest` to assertions one block deeper (e.g. inside `with self.subTest():`); ~28 more sites across the suite at 0.8.0 | not yet filed |

## Decided, not deferred

Recorded so they are not re-raised as findings. Each was examined on 13 Aug and
judged correct as written. Neither has an issue, and that absence is the
decision: filing one would put back on the board the thing this section exists
to keep off it. They are the only entries on this page without one, apart from
TQ2-nested above, which is simply not filed yet.

- **`application/compilation.py:337`** partitions novelty comparison by a page's
  self-declared `type` (`if existing["type"] != operation.kind.value: continue`).
  This looks like the same fault as the two `type: "system"` exemptions closed in
  `0.6.2` — a page's own frontmatter deciding how it is checked — and it is not.
  Those two let a page opt *out* of being checked at all; this one only says a
  `concept` and an `entity` are not near-duplicates of each other, which is a
  domain partition and the reason `_wiki_catalog` (`:364-386`) has no exemption
  sitting above it. The distinction to preserve if this is ever revisited:
  `duplicate_identity` runs before the partition and is not subject to it.
- **R7** — the workflow comment the previous `Later` asked to correct. The stale
  premise is not in `.github/workflows/release.yml` and `git log -S` finds it in
  no revision of that file; `release.yml:91-98` already carries the corrected
  `v0.5.0` `invalid-publisher` account. The guard stays, as the original entry
  said it should. Nothing to do.

## The formatter sweep

**FMT** — [#35](https://github.com/huglabs/brainskit/issues/35).

`ruff format` has **never been applied to this repository**, and the check is
deliberately outside CI — `.github/workflows/ci.yml:9-11` says so in a comment,
and `pyproject.toml:188-193` ignores `E501` for the same reason.

This is a single mechanical sweep, not a gap: nothing is wrong with the tree, and
`ruff check` and `mypy --strict` both pass over it today. Worth doing on a quiet
branch with nothing else in the diff, because the sweep will bury any change it
travels with. It was unrunnable at all until the `block-destructive-commands.sh`
false positive was fixed during the remediation, which is why the drift
accumulated invisibly for the life of the repo. Re-measure the file count before
quoting one — the last figure, 62 of 78, predates the `0.6.2` batch's 25 changed
paths.

## Deferred decisions

- **DEF1** — [#36](https://github.com/huglabs/brainskit/issues/36). The remaining
  low/cosmetic findings from the original field audit that were never promoted
  into the remediation. Several are likely obsoleted by Phases 1–4 and by the
  `0.6.1`/`0.6.2` work; revisit as a batch rather than individually.

---
<!-- doc-tracking -->
- Created: 2026-08-12
- Updated: 2026-08-12 10:08
- Updated: 2026-08-13 13:15
- Updated: 2026-08-13 15:51
- Updated: 2026-08-13 17:06
- Updated: 2026-08-13 17:07
- Updated: 2026-09-27
- Updated: 2026-09-28
