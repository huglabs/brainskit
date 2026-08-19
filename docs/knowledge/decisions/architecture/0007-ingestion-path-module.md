# ADR 0007 — The ingestion path is a module, so the facade owns nothing rather than nearly nothing

Date: 2026-08-14 · Status: accepted · Decided during the same architecture
review that produced 0001–0005, applied to the one claim in
`docs/architecture.md` that was still aspirational.

## Context

`docs/architecture.md` says, of the application layer:

> Inside the application layer, `BrainskitService` is a facade that owns
> nothing: it composes the collaborators below and delegates.

It then lists one module per owned concept. That was true of forty-odd methods
— `views`, `graph`, `ask`, `lint`, `status`, `search`, `apply` and the rest are
one line each, and their docstrings say so out loud (`"See \`Projections\`."`).
It was not true of `capture`. Roughly 250 of the file's 780 lines were a real
subsystem with no module of its own:

- `capture` — the source/URL/text branch, the index upsert, and the relatedness
  call that follows every capture.
- `watch_once` — the sweep, the present/missing partition of configured
  sources, the two different answers to "a source resolves to nothing", and the
  per-file failure list.
- `_mark_related_pages_for_review` and `_related_query_terms` — the BM25
  relatedness heuristic, plus five tuning constants (`_RELATED_QUERY_TERMS`,
  `_RELATED_CANDIDATES`, `_RELATED_PAGE_LIMIT`, `_RELATED_MIN_SHARED_TERMS`,
  `_RELATED_TEXT_LIMIT`) that no other method read.
- `_drop_orphaned_freshness` and `_pages_citing` — two lookups that heal or
  report on state a capture and a `forget` leave behind.
- `_is_url`, `_missing_source`, `_missing_source_error`, `_walk_source`,
  `_relative`, `_inside` — six module-level functions, one of them an `os.walk`
  traversal with its own pruning contract.

Three consequences followed, and each is the reason this is an ADR rather than
a tidy-up.

**The facade reached into a sibling's privates to do it.** `services.py`
imported `pages._content_tokens`, `pages._is_salient_term` and
`pages._normalized_tokens` — a module whose *stated* export list is
`render_page`, `parse_frontmatter`, `page_metadata` and two constants. A facade
that owns nothing has no business needing a tokenizer; that import was the
diagnosis, not the defect.

**The decisions could not be tested as decisions.** Every rule above is covered
end to end by `RelatedCaptureFreshnessTest`, `WatchIgnoreTest` and
`WatchSourceResolutionTest`, which is what proves the wiring and is why those
tests stay. But reaching the relatedness floor meant a real vault, a real FTS5
index, an apply, and four pages of prose chosen so that BM25 would rank them —
so a failure did not say which of those moved, and a case the index would never
produce (a top-ranked hit that shares no vocabulary) could not be constructed at
all.

**A reader looking for the ingestion rules had nowhere to look.** The module
table in `docs/architecture.md` answers "where does X live" for twelve concepts.
For "what is a watch allowed to walk" the answer was "the middle of the facade,
and also 140 lines below the class".

## Decision

1. **`application/capture.py` owns the ingestion path**, exporting one class,
   `Ingestion`: `capture`, `watch_once`, `drop_orphaned_freshness`,
   `pages_citing`, and the two private relatedness methods with their five
   constants. The six module-level helpers move with it, unchanged. The class
   is named for the concept and the module for the command, the way
   `compilation.py` owns `ApplyGate` and `freshness.py` owns `FreshnessLedger`.

2. **It is built at the composition root and handed its collaborators** —
   `Ingestion(vault, index, ledger)`, constructed in `BrainskitService.__init__`
   beside the other ten. It constructs no sibling of its own. ADR 0002 already
   refused to let a sixth owner appear on the freshness file, and an earlier
   review found `Health` and `Projections` each building a partially-configured
   instance of another module; this does not add a third instance of that shape.
   The ledger in particular is passed, never built, because
   `_mark_related_pages_for_review` is one of the two callers whose divergence
   ADR 0002 exists to record.

3. **Three private imports from `pages` follow the code rather than being
   duplicated**: `_content_tokens`, `_is_salient_term`, `_normalized_tokens`.
   They are pure functions over text, they encode the *page format's* notion of
   a token (citations stripped, accents folded, stopwords in both vault
   languages), and `compilation.py` already imports `_content_tokens` the same
   way for the novelty check. Copying them into `capture.py` would put the
   tokenizer in three places and make "related" and "duplicate" answerable
   differently — precisely the drift 0002 was written about. Promoting them to
   public names in `pages.py` is the better end state and is left as a
   follow-up, because it is a change to a module this decision does not own.

4. **`reconcile` and `forget` stay on the facade** and call
   `ingestion.drop_orphaned_freshness()` and `ingestion.pages_citing(…)`. Both
   are three-line orderings of `vault`, `index` and one collaborator — facade
   work, not a concept — and moving them would have `Ingestion` owning the
   registry lifecycle it merely reports on.

5. **The extraction adds no behaviour and changes no assertion.** Every moved
   docstring is carried verbatim, including the four that record incidents (the
   `found_at_cwd` upgrade note, the walk's pruning rationale, the
   never-downgrade comment, the orphan-key mismatch). The four suites that
   cover this code — 207 tests across `test_fix_services`, `test_projections`,
   `test_freshness_ledger`, `test_privacy_boundary` — pass unchanged, and no
   test needed a new import target, because every one of them drives the
   service's public surface.

## Alternatives rejected

- **Leave it and amend the doc** to say the facade owns capture. This is the
  cheapest change and the worst one: the sentence is load-bearing. It is what
  tells a reader that `services.py` can be skimmed, and it is what makes the
  module table a map rather than a partial index. A documented exception to a
  one-line rule is how the rule stops being read.

- **Move `capture` into `filing`.** They are adjacent — filing is what a
  captured source is waiting for — and it would have added no module. Rejected
  because filing's own docstring is about the moment "a judgment call and an
  irreversible write meet", and it depends on the apply gate and the judgment
  runner to say so. Capture needs neither, and joining them would give the
  merged module two reasons to change and a constructor listing five
  collaborators for work that uses three.

- **Split further: a `watch` module beside a `capture` one.** The walk is the
  largest single piece and is genuinely separable. Rejected because
  `watch_once`'s body *is* a loop over `self.capture`, so the split would put a
  hard dependency between two modules to save 90 lines, and the reader looking
  for "what enters the vault" would have two places to look instead of none.

- **Make the relatedness methods public so tests need no underscore.** They
  have exactly one caller, `capture`. A public method with no external caller
  is an invitation to acquire one, and the repository already drives private
  helpers from tests deliberately — 0002 records `tests/test_projections.py`
  doing it to the freshness helpers.

## Consequences

- `services.py` drops from 780 lines to 406, and every method on
  `BrainskitService` is now either composition or a delegation whose docstring
  names its owner. The claim in `docs/architecture.md` is true as written;
  that file gains a `capture` row (`The ingestion path: what enters the vault,
  and what that entry disturbs`) so the table stays a complete map.

- `tests/test_capture.py` drives the decisions directly against a stub vault
  and a stub index, which makes two cases reachable that the end-to-end tests
  cannot construct: a top-ranked hit whose body shares no vocabulary (the index
  cannot be made to return one), and a non-wiki hit proved to be dropped
  *before* its body is read. Removing the shared-term floor fails two of these
  and one end-to-end test; the end-to-end suite alone would have caught it once.

- `pages.py` now has two application-layer importers of its private tokenizers
  (`compilation` and `capture`). That is the argument for promoting them, and
  it is now visible in one grep instead of being a single site easy to read as
  an accident.

- `INTEGRATION_TARGETS` and `EXPORT_SUFFIXES` remain declared in both
  `services.py` and `projections.py`, unread in the former. They predate this
  change and are left alone deliberately: deleting a public name is a contract
  change, not an extraction, and they are exactly the shape
  `ConstantsHaveOneOwnerTest` was written for — a follow-up that deserves its
  own justification rather than a ride on this one.
