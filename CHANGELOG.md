# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Because a published version is permanent, the `v<version>` tag on the commit an
artifact was built from is the durable record of what shipped.

## [Unreleased]

## [0.5.0] — 2026-08-12

### Added

- `bk doctor` exercises the installed write gate instead of only reporting that
  it exists: one path it must refuse, one it must allow, reported as
  `enforcement.write_gate_probe` with the hook's own explanation when it fails
  open.
- Four narrower error codes — `conflict`, `not_configured`, `refused` and
  `model_response_invalid` — as subclasses of `ValidationError`, so every
  existing handler and exit code is unchanged while a caller can tell "change
  the request" from "configure this installation".
- `bk forget ITEM`, dropping one source record whose raw file is gone.
- `bk vaults register|list|forget|sync`: the vaults on this machine, synced into
  one shared store as a set, each keeping its own policy.
- `bk enrich`: model-proposed graph edges, gated on named provenance and stored
  apart from the derived projection.
- A guided `bk init` wizard that probes the machine — git, `$LANG`, running
  ollama and its pulled models — before asking anything, and a grouped CLI help
  surface.
- The first `bk code build` now runs during `bk hooks install`, so a new vault's
  code graph exists rather than reporting `missing` until someone notices.

### Changed

- **Renamed to brainskit.** The distribution is now `brainskit`, the import
  package is `brainskit`, and the machine-wide registry lives at
  `$XDG_CONFIG_HOME/brainskit/vaults.json`. The command is still `bk`.
  Install with `uv tool install brainskit`.
- The CLI opens with a `BRAINSKIT` masthead on a terminal at least 65 columns
  wide, carrying HugLabs, the site and the licence as OSC 8 hyperlinks, and
  falls back to a single line anywhere narrower or off a terminal.
- `bk hooks install` refuses to write `.git/hooks/pre-commit` when
  `core.hooksPath` points elsewhere, naming the directory git actually uses and
  the line to add to it. `commit_lint` is reported inactive until it is wired
  up, instead of reporting a file git will never read as active.
- Stale `brainskit-gate`/`brainskit-status` entries in `.claude/settings.json` are
  pruned by hook identity rather than by literal command path, so a `.claude/`
  carried over from another project no longer leaves two gates registered.
- `bk code build PATH …` merges that subset into the stored graph instead of
  replacing the whole graph with it.
- A code-graph build reports the coverage it actually achieved — files that
  produced at least one node over files whose extension has an extractor —
  rather than a node count that can grow while a language falls out entirely.

### Compatibility

- A pre-rename `$XDG_CONFIG_HOME/brainkit/vaults.json` is still read when no
  `brainskit` registry exists yet, so an upgrade does not report an empty
  registry and strand every vault on the machine.
- A vault at `<repo>/.brainkit` is still discovered alongside `<repo>/.brainskit`
  and `docs/brain`.
- PostgreSQL and Neo4j keep their `brainkit` role, database, schema, index and
  constraint names: those name objects that already exist on a user's server,
  and renaming them would create a second set beside the originals rather than
  move anything. Set them explicitly in the integration policy to change them.
- Agent hooks are named `brainskit-gate` and `brainskit-status` and the skill
  installs to `.claude/skills/brainskit/`. Re-run `bk hooks install --force` in
  each project that has the old ones.

### Fixed

- An unbounded scan is refused rather than walking a tree that was never meant
  to be a vault's code root.
- Two prompt flows that could loop, and two graphs that overstated what they
  covered.

## [0.4.0] — 2026-08-02

First tagged release: the M0–M3 local walking skeleton.

### Added

- Policy-first vault initialization, and immutable capture with SHA-256 identity
  plus registry reconciliation.
- FTS5 indexing and BM25 search, bounded evidence `context`, structural `lint`,
  generated views and the derived knowledge graph.
- The `bk apply` gate: schema, citation, link and novelty validation for the
  whole batch before any page is replaced, as one crash-recoverable unit of work
  covering wiki pages, freshness, registry status, the raw-file move and the
  index.
- Durable approve/reject filing proposals driven by per-branch policy, and the
  freshness lifecycle (`fresh`, `review`, `stale`) with resurfacing.
- Consumer-aware privacy filtering applied after graph expansion, across search,
  context and every egress.
- Schema-bound judgment jobs with automatic repair feedback, over
  provider-neutral Anthropic, OpenAI, OpenRouter and Ollama drivers.
- JSON CLI mode, MCP over stdio and authenticated Streamable HTTP, and a
  dependency-free read-only web viewer.
- Persistent Obsidian, Neo4j and PostgreSQL integrations with opt-in lifecycle
  management and durable Docker volumes.
- `bk code`: a second graph describing the repository a vault documents, with a
  vendored analysis subset behind the `code` extra.
- Delivery gated on the shipped wheel — built from the sdist, installed in a
  throwaway environment and driven through the real CLI contract.

[Unreleased]: https://github.com/huglabs/brainskit/compare/v0.5.0...HEAD
[0.5.0]: https://github.com/huglabs/brainskit/releases/tag/v0.5.0
[0.4.0]: https://github.com/huglabs/brainskit/releases/tag/v0.4.0
