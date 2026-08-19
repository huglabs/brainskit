# ADR 0002 — Freshness state has one owner, and `content_hash` means the apply gate wrote this

Date: 2026-08-14 · Status: accepted · Decided during an architecture review
(the same design-it-twice pass that produced 0001, applied to the second file
the application layer writes from many places).

## Context

`.brain/freshness.json` was written by five callers and owned by none.
`compilation._freshness_updates` built the complete entry an apply records;
`services._mark_related_pages_for_review` and `health._review_drifted_code_citations`
each performed the *same* `review` transition; `health._refresh_staleness` aged
entries; `health._record_projection` stamped the projection table;
`jobs.resurface` annotated one page; `services._drop_orphaned_freshness` removed
entries for pages that were gone. Five more sites read the file back —
`health.status`, `health._mechanical_lint`, `jobs.digest`, `reader.reader_status`,
`reader.browse_pages`, `projections.views` — each re-deriving from the raw dict
whatever it needed.

The entry vocabulary (`status`, `updated_at`, `content_hash`, `source_hashes`,
`review_reason`, `review_requested_at`, `age_days`, `last_resurfaced_at`) was
written down nowhere. A field's meaning was whatever the writer that produced it
and the reader that consumed it happened to agree on, and the two rules readers
actually depend on are properties of the *file*, which no single writer is in a
position to enforce.

Both rules were already broken, in the same shape, and each defect is pinned by
a reproduction test in `tests/test_freshness_ledger.py`.

**The never-downgrade rule was enforced in one of two identical writers.**
`health._review_drifted_code_citations` guarded with `if entry.get("status") ==
"stale": continue` and said why in a comment: "a page already `stale` has a
stronger claim on attention". `services._mark_related_pages_for_review`, reached
by every `bk capture`, performed the identical transition with no guard. Because
`_refresh_staleness` skips every `review` entry, that was not a lowered badge —
it took the page out of the ageing loop entirely. A stale page plus a capture
that shared its vocabulary became `review` forever, and only a fresh `bk apply`
could release it.

**A bare entry laundered an untracked page past the integrity check.**
`compilation._freshness_updates` was the only writer producing a `content_hash`,
which made that field, in practice, the mark of "apply wrote this". But
`services._mark_related_pages_for_review` and `jobs.resurface` created entries
with `pages.setdefault(path, {})`, carrying no hash. `health._mechanical_lint`
reported `wiki.outside_apply` only when `expected_hash` was truthy *and*
differed, and routed to `_untracked_page_findings` only when the entry was not a
dict. A bare entry is neither — so a hand-written page under `wiki/` was exempt
from **both** checks the moment any capture happened to relate to it, or
`bk resurface` named it. That is the backstop `_untracked_page_findings` exists
to provide for the write gate failing open, and an annotation switched it off.

## Decision

1. **`FreshnessLedger` owns `.brain/freshness.json`.** Every
   `read_state("freshness")` and `mutate_state("freshness")` in the codebase
   goes through it; no module spells the state name any more (`freshness.STATE`
   does). Transitions are named after the intent that reaches them —
   `mark_applied`, `mark_reviewed`, `record_resurfaced`, `refresh_staleness`,
   `record_projection`, `drop` — so a caller states what happened and the ledger
   decides what that means for an entry.

2. **The never-downgrade rule lives in `mark_reviewed`, once.** Both callers
   reach it, so `bk capture` gained the guard `bk lint` already had. This leaves
   `review` reachable only from `fresh`, which is the coherent reading of
   `_refresh_staleness` skipping it: a current page flagged for a human should
   not also age. `mark_reviewed` takes a reason *per path* rather than one reason
   for a list, because that is the only axis on which the two callers differ — a
   capture names one source for every page it touched, code drift names the file
   that moved for each — and a per-path call would take one lock per page.

3. **`content_hash` means the apply gate wrote this page, and here is what it
   wrote.** `mark_applied` is the only writer that produces one. An entry
   without one is an **annotation**, not proof of provenance, and
   `FreshnessSnapshot.applied_hash` answers `None` for it — the same answer it
   gives for no entry at all and for an entry that is not a dict, because all
   three mean the ledger cannot say what the page looked like when it was
   written. `health._mechanical_lint` asks that one question instead of
   inspecting the entry's shape, so the fix lands in one place for every reader.

4. **Reads take a `FreshnessSnapshot`.** One read of the file, then every
   question asked of that read (`applied_hash`, `status`, `updated_at`,
   `stale_pages`, `summary`, `orphans`, `pages`, `projections`, `state`).
   Request-scoped by the same convention 0001 gives `PrivacyBoundary`: taken,
   questioned, dropped — never held across a write. It is a value rather than
   more methods on the ledger so `lint` can ask about a thousand pages while
   opening the state file once.

5. **The ledger is built at the composition root** (`BrainskitService.__init__`)
   and handed to `ApplyGate`, `Health`, `Projections`, `Reader` and `Jobs` as a
   required constructor parameter. Not optional, for 0001's reason: an optional
   fallback is a second, degraded path kept alive for test reachability. Letting
   each collaborator construct its own would restore exactly the shape this ADR
   removes — an earlier review already found `Health` and `Projections` building
   partially-configured siblings, and this does not add a sixth.

## Alternatives rejected

- **Populate `content_hash` at review time**, so a bare entry stops being a
  special case. This is strictly worse than the bug: it makes expected equal
  observed for the bytes a hand edit left behind, so the tampered page is
  *blessed* rather than merely unreported. The field's meaning is provenance,
  not a cached checksum.
- **A pure-data ledger** (module functions taking the state dict, as today, plus
  a documented vocabulary). It keeps the current shape, and the current shape is
  the defect: nothing prevents the sixth writer from restating the rules
  differently, which is how the first five diverged.
- **Move `_projection_report` into the ledger too.** Rejected as god-object
  gravity, the same cost 0001 named when it left graph filtering in
  `projections`: that method opens artefacts through the vault and classifies
  faults, which is health's question. Only the *write* (`record_projection`) and
  the state it reads move.

## Consequences

- `ApplyGate` is no longer a two-collaborator leaf; its docstring says so. The
  entries `mark_applied` builds are still committed by `commit_wiki_batch`
  rather than by the ledger, because that transaction takes the registry lock
  before the freshness lock and a second writer inside it would invert the
  order and deadlock — the same ordering `record_projection` documents from the
  other side.
- A page carrying a bare entry now reaches `_untracked_page_findings`, which
  keeps the `SEEDED_SYSTEM_PAGES` exemption intact: `bk init`'s two pages stay
  quiet while they hold their seeded shape, whether or not something annotated
  them.
- `Reader.reader_status` no longer builds a filtered copy of the state to count
  it; `summary(present=…)` is the same answer with the page filter stated once.
- The pure helpers (`_fingerprint_row`, `_projection_source_hash`,
  `_age_in_days`, `_freshness_summary`, `_orphaned_freshness`, the projection
  tables, `_graph_integrity`, `_views_integrity`) are unchanged and still
  importable; the ledger composes them, and `tests/test_projections.py` keeps
  driving them directly.
