# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Because a published version is permanent, the `v<version>` tag on the commit an
artifact was built from is the durable record of what shipped.

## [Unreleased]

## [0.6.0] — 2026-08-13

Remediation of a five-agent field audit of 0.5.0. The defects clustered in one
place: the mechanisms meant to *refuse*, and the surfaces reporting on them. A
check verified that a thing existed rather than that it worked, or resolved an
unknown to the permissive answer instead of the safe one.

### Fixed — the privacy boundary

- A wiki page whose cited sources no longer resolve is treated as
  **`never-ingest`**, not `cloud`. Unresolvable hashes were dropped and the
  empty remainder answered `cloud`, so forgetting a `never-ingest` source did
  not redact the pages built from it — it published them, stamped
  `"privacy": "cloud"`.
- **Obsidian sync filters `wiki/` and `raw/`**, not only the graph object. Files
  were chosen by walking the filesystem, so a compiled page leaked under default
  options and raw `never-ingest` bytes leaked under `--include-raw`, into what is
  usually an iCloud- or Dropbox-backed directory.
- `bk graph` writes inside a consumer boundary (default `local`) and stamps which
  one. It previously wrote an unfiltered artifact carrying `never-ingest` hashes,
  filenames and branch names.
- `strictest_privacy` requires an explicit `on_empty`. The old `cloud` default
  was justified by a docstring asserting every caller checked provenance first;
  one did not.

### Fixed — surfaces that reported what they had not checked

- The SessionStart hook renders `enforcement.layers[]` from the status document
  it already holds, instead of recomputing it as `[ -x gate.sh ]` and
  `[ -f .git/hooks/pre-commit ]` — which announced "active" in exactly the two
  cases `bk status` had learned to catch.
- `bk status`'s `healthy` headline means enforcement as well as lint. It printed
  green above three red enforcement rows.
- `bk gate check-write` resolves a relative path against the current directory,
  like every other command. The same file spelled two ways got opposite verdicts.
- Every `bk code` traversal carries a staleness signal. `hubs` cited files
  deleted months earlier, with line numbers and no caveat.
- The graph counts citations it could not resolve, agreeing with `bk lint`
  instead of dropping them silently.

### Fixed — correctness

- An unconfigured branch raises `PolicyError` instead of a bare `KeyError` that
  escaped four read paths after the documented `bk reconcile`, bypassing the JSON
  error envelope entirely.
- `search(limit=N)` returns N. It returned N+1 for N below 4.
- Provider outages report `not_configured` rather than `validation_error`, which
  told an agent to rewrite a well-formed request against a provider that was down.
- Duplicate slugs across page kinds are refused at apply and reported by
  `bk lint`. Two pages with one stem meant every `[[link]]` resolved to whichever
  directory sorted later.
- `bk --version` reports the distribution version. It said `0.4.0` against a
  `0.5.0` release, through a gate built to catch exactly that.

### Added

- `bk init --print-config [--preset …]` — a complete, schema-valid policy on
  stdout, so a vault can be created without a terminal. This unblocks CI,
  containers and agent-driven setup, none of which could initialise a vault at
  all before.
- `taxonomy_seed` has a reader: it marks the vault's declared branches for the
  filing proposal. It was a required key with no readers.
- `bk capture` has a human renderer naming the hash and the next command.
- Help text for 61 options and 17 positionals; every leaf command's help now
  names `--vault` and `--json`.

### Changed

- `cycles` and `diff` are computed on brainskit's own graph. They delegated to
  `graphify.analyze`, which loaded 2,487 lines of vendored builder and networkx
  to reach a thirteen-line helper — so both now answer with no optional
  dependency installed. `analyze.py`, `build.py` and `validate.py` are removed
  from the vendored tree, declared in its `NOTICE`.
- The jsonschema engine moved out of `domain/`, which now imports nothing beyond
  the standard library.
- The web API is documented as what it is: eleven read endpoints and four that
  write, guarded by `--consumer human`.

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
- PostgreSQL and Neo4j now write `BrainskitNode` nodes into a `brainskit`
  schema, matching the documentation. The reasoning that previously kept the old
  names still holds -- creating the new objects beside the originals would
  duplicate rather than move them -- so a store that still holds the pre-rename
  objects is **refused** on sync, with the statement that moves them:

      Neo4j       MATCH (n:BrainkitNode) SET n:BrainskitNode REMOVE n:BrainkitNode
      PostgreSQL  ALTER SCHEMA "brainkit" RENAME TO "brainskit"

  Run it on the server, then sync again. The PostgreSQL **role, database and
  container** names are deliberately unchanged: those identify objects a server
  provisioned rather than objects brainskit writes into one, and renaming them
  would strand a running deployment. Set any of these explicitly in the
  integration policy to override.
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
