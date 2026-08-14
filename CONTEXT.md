# Design vocabulary

Names for the seams of this codebase. Architecture reviews read this file
first; a concept named here is a decision, not a suggestion. ADRs live in
`docs/knowledge/decisions/architecture/`.

## Privacy

- **Consumer** — a named reader of the vault: `human`, `local`, or `cloud`.
  Parsed once, at the boundary, into the `Consumer` enum; an unknown consumer
  fails at construction, never inside a decision.
- **PrivacyBoundary** — the request-scoped object that is the one answer to
  "may this consumer see this?". Built by `privacy.for_consumer(consumer,
  vault)`, which snapshots the registry and config once. Request-scoped is a
  convention the type cannot enforce: never cache a boundary across writes.
- **strictest-privacy fold** — the most restrictive policy across everything
  that contributed wins. `on_empty` is a mandatory argument by doctrine: an
  asserted invariant is a bug that has not happened yet.
- **resolve_branch_policy** — branch → policy resolution: `_inbox` maps to the
  inbox policy, an unconfigured branch is a `PolicyError` naming the branch and
  the configured list. Lives in `domain/privacy.py`; named to stay clear of
  `domain/model.py`'s private `_branch_policy` (a config parser, unrelated).
- **Egress** — a file leaving the vault (integration sync, export). Judged by
  `PrivacyBoundary.allows_path`: wiki pages by frontmatter provenance, raw
  files by path-derived branch (an unreconciled inbox file must not
  over-block), unconfigured branches human-only.
- **SyncBoundaryPort** — what crosses to an integration adapter: a consumer
  name and one path predicate, as a required parameter of
  `IntegrationPort.sync`. Never inside the graph payload — the graph dict is
  pure JSON data end to end; its `consumer` key is artifact metadata that
  nothing decides from.
- **Privacy after expansion** — filtering runs on the finished graph, once
  every node and edge exists, so a link cannot pull a redacted node back in
  through its neighbour. A redacted source contributes nothing: not its body,
  not its filename, not its branch.
