# ADR 0001 — The privacy boundary is one bound object, and only data plus a typed port cross to adapters

Date: 2026-08-14 · Status: accepted · Decided during an architecture review
(design-it-twice: two competing interface designs judged against the ~30 real
call sites).

## Context

`application/privacy.py` was documented as "the one answer to may this
consumer see this?" but exported 11 functions (10 underscore-prefixed) that
eight modules imported privately and re-assembled by hand. The rule existed
three times: the application module, a hand-rolled copy in
`infrastructure/llm.py`'s judgment router (layering forbids
infrastructure→application), and `infrastructure/integrations.py` importing
the private functions through a `DOCUMENTED_EXCEPTIONS` entry marked "real
debt". The consumer travelled as a back-channel inside the graph payload
(`projections` stamped `graph["consumer"]`, the adapter read it back).

A previous fix was attempted and reverted: it threaded a callable through the
graph dict, which also feeds the Obsidian JSON export and the Neo4j/Postgres
serializers, so a non-data member broke five tests at once. The revert left
the debt entry; this ADR records the diagnosis so the shape is not retried.

## Decision

1. **Pure rules move to `domain/privacy.py`** (stdlib-only): the `Consumer`
   enum with `parse` and `allows`, the `strictest_privacy` fold (mandatory
   `on_empty` doctrine intact), and `resolve_branch_policy` (the `_inbox`
   special case and the missing-branch `PolicyError`). The judgment router
   imports these — infrastructure→domain is legal — and keeps its
   never-ingest refusal, which is router policy, not the shared rule.
2. **`application/privacy.py` exposes one constructor**,
   `for_consumer(consumer, vault) → PrivacyBoundary`: consumer parsed once,
   registry and config snapshotted once. Methods answer every privacy
   question (`allows_record`, `evidence_privacy`, `allows_evidence`,
   `allows_path`, `split_records`, …). Page-level provenance resolution stays
   in application because `parse_frontmatter` lives there. The boundary does
   not own graph filtering — the node/edge loop and `redacted_nodes` stay in
   `projections`; god-object gravity was an explicitly rejected cost.
3. **`IntegrationPort.sync(name, graph, boundary)` takes the boundary as a
   required third parameter**, typed as a `SyncBoundaryPort` protocol in
   `application.ports` (consumer name + `allows_path`). The graph dict stays
   pure JSON; its `consumer` key remains as artifact metadata only. The
   parameter is required, not optional: an optional fallback would be a
   second, degraded decision path kept alive for test reachability, and it
   would outlive that excuse. Direct-call adapter tests construct a real
   boundary instead.
4. The `("infrastructure.integrations", "application.privacy")` layering
   exception is deleted. Two behavioral gaps close with the seam:
   `/api/integrations` becomes consumer-scoped (machine consumers no longer
   receive filesystem paths, container names, DSN env names), and
   `Reader.timeline` validates by construction — no Reader method can reach a
   decision without building a boundary.

## Alternatives rejected

- **Pure-data snapshot (competing design)**: a frozen `EgressBoundary` value
  carrying a pre-computed wiki-privacy manifest, so only data crosses the
  port. Rejected for the TOCTOU window (a page written mid-sync is silently
  withheld), the full-vault pre-read on every sync, and call-site ergonomics —
  three value types and two-level function calls at 30+ sites reintroduce the
  "which spelling do I use" problem this refactor exists to kill.
- **A callable inside the graph dict**: already attempted, already reverted;
  the payload is serialized three ways and must stay data.

## Consequences

- The boundary performs I/O inside predicates (`allows_path` reads wiki
  content via the vault port at decision time — the same instant it is read
  today). Unit tests need a vault fake.
- The snapshot lifetime is a documented convention: boundaries are
  request-scoped and never cached across writes.
- Tests that imported the underscore functions migrate to the boundary object
  or to `domain/privacy`; behavior tests through the service surface
  (`ContextRedactionTest`, `ExportPrivacyBoundaryTest`,
  `StoredConsumerValidationTest`, `ObsidianExportBoundaryTest`,
  `GeneratedViewsTest`) survive unchanged.
