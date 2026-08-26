# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Because a published version is permanent, the `v<version>` tag on the commit an
artifact was built from is the durable record of what shipped.

## [Unreleased]

### Added

- `bk update` — check PyPI for a newer brainskit and upgrade this installation
  in place. The upgrade command is derived from how `bk` was installed
  (`uv tool upgrade`, `pipx upgrade`, or an in-place pip upgrade), so it works
  where the old advice (`pip install -U`) could not. `--check` reports only;
  `--json` without `--yes` returns the plan instead of mutating; an
  unreachable pypi.org degrades to `state: "unavailable"`, never a traceback.
- `bk doctor` now also audits installed grammar *versions* against the pins
  brainskit declares in its own package metadata, reporting outdated grammars
  with the violated range and one upgrade command covering both missing and
  outdated distributions.
- A query beginning with `-` (`bk search -retrieval`) now parses: unknown
  dash-leading tokens after `search`/`context`/`ask` are hoisted behind `--`
  before argparse sees them.

### Fixed

- **Symlink shadowing under parallel extraction:** a file plus a symlink to
  it raced through the process pool, and the symlink sometimes took the
  credit — the real file contributed nothing while the graph pointed at an
  alias. Paths are now collapsed to their canonical file before extraction,
  deterministically. The AST cache namespace covers the adapter too, so
  misattributed entries written before the fix cannot be served afterwards.
- **A scoped rebuild no longer blesses edits it never extracted.**
  `bk code build <scope>` re-hashed every recorded file from disk, so an edit
  outside the scope was absorbed into freshness and `bk code status` answered
  `fresh` over nodes describing code that no longer exists. Out-of-scope
  digests are now carried from the stored graph until a full rebuild reads them.
- **The unexplained-files gap is persisted** in the code-graph artefact and
  reported by `bk code status` as `partial` with a count, instead of expiring
  with the build output that mentioned it once.

- `providers.<name>.reasoning` on the OpenAI-compatible driver, forwarded
  verbatim to the provider. Absent by default, so a model that reasons keeps
  doing so until an operator says otherwise. Measured on OpenRouter with
  `nvidia/nemotron-3-nano-30b-a3b:free` running the real ingest job:
  `{"enabled": false, "exclude": true}` took a call from 12.7s to 3.8s and its
  reasoning tokens from 898 to 0, with identical output. An endpoint that
  refuses to skip reasoning — `openai/gpt-oss-20b:free` answers *"Reasoning is
  mandatory for this endpoint"* — is retried without the option, because
  suppression is a cost and latency preference and never a correctness one.

### Fixed

- An empty completion from an OpenAI-compatible provider is refused instead of
  returned as an answer. A reasoning model that spends its whole budget
  thinking returns a well-formed response whose `content` is empty;
  `OpenAICompatibleDriver` passed that back, so the repair loop chased it as
  malformed JSON — three attempts, three empty strings, and a final
  `model_response_invalid` naming `json.invalid` rather than the cause, after
  6m46s of wall clock. The refusal now carries `finish_reason` and names the
  `reasoning` option. `AnthropicDriver._text` already had this guard; the
  asymmetry is what shipped.

- A standalone `graphify` distribution installed alongside Brainskit no longer
  silently replaces the vendored extractors. The alias shim's idempotency guard
  accepted any `sys.modules["graphify"]`, so "the name is taken" stood in for
  "we already took it" — and upstream Graphify is a real, installable package,
  so the name can be held by a foreign one. It failed both ways: where that
  package lacked `graphify.ids`, importing `codeanalysis` died as
  `ModuleNotFoundError: No module named 'graphify.ids'`, naming neither the
  conflict nor its cause; where it had one, the import succeeded and supplied a
  *different* `normalize_id` — the recipe node ids are built from — with nothing
  raised. The guard now recognises the alias by its search path and refuses a
  foreign package with `not_configured`, naming what holds the name and how to
  free it. Overriding it instead would fork the extractor rather than repair it:
  a process binds a top-level name to exactly one package, and the foreign
  package's submodules may already be imported.

- A spawned extraction worker now resolves the same `graphify` its parent did.
  `_enable_parallel_workers` writes a generated `graphify` package for the child
  to find on `sys.path` — a `spawn`ed worker inherits the path but not
  `sys.modules`, so the in-process alias and the refusal above cannot reach it —
  and *appended* it, documented as deliberate so that "a genuine Graphify
  installation earlier on the path keeps winning". That is the same fork one
  process boundary out: parent on the vendored tree, child on the installed
  distribution, and the pool pickles its work by qualified name, so the child's
  copy is what extracts. It surfaced as `AttributeError: Can't get attribute
  '_extract_single_file' on module 'graphify.extract'` while unpickling, that
  helper being this tree's and not upstream's; an upstream carrying a same-named
  helper would have run silently instead. The entry is now prepended, and moved
  rather than skipped when already present. Safe on both counts: the directory
  holds exactly one package, so it can shadow nothing else, and `_shim_root`
  already refuses a directory that is not 0700 and owned by the current user.

## [0.7.0] — 2026-08-14

An end-to-end overhaul of the web viewer (`bk web`). Nothing in this release
changes a configuration value, an error code, or an on-disk artifact — a vault
that was correct under 0.6.2 needs nothing done before or after this upgrade.

### Added

- The graph renders as a molecule. Every node is an instanced sphere whose
  radius encodes its degree, lit with Phong shading and a fresnel rim injected
  through `onBeforeCompile` — the vendored three.js build carries no
  postprocessing to do it any other way. Edges are half-cylinder bonds split at
  the midpoint, each half taking its endpoint's colour, so a `sourced_from`
  bond visibly flows evidence-cyan into the page's kind colour instead of
  asserting one end's colour for the whole edge. Past 6,000 edges the bonds
  fall back to gradient `LineSegments`, the eight biggest hubs carry additive
  halos, and the legend gained a chip explaining the bond convention.
- The layout runs a continuous force simulation and sleeps when it settles.
  Springs, repulsion and gravity are scaled by a decaying alpha temperature, so
  a fresh graph churns into place and a settled one costs nothing per frame
  until an interaction reheats it. Dragging pins the grabbed atom to the
  pointer while the springs propagate the pull through the network, and
  releasing flings it with the pointer's smoothed velocity — measured mid-drag
  at 120 fps with 1,100 nodes and 10,102 bond instances.
- The graph assembles itself on load: atoms pop in in BFS order from the
  biggest hub, and each bond appears only once both of its endpoints exist,
  growing from its midpoint — so the reveal shows the connections being made
  rather than presenting a finished tangle. Any interaction interrupts it, it
  re-arms on every graph build, and under `prefers-reduced-motion` the graph
  is simply there.
- Navigation: inertial orbit, eased wheel zoom, click selects without moving
  the camera, double-click flies to the node, `0` or ⌂ resets, and the camera
  drifts after six seconds idle. Every ambient motion is gated behind
  `prefers-reduced-motion`.
- Ask is a full chat view, replacing the modal and its inspector dump: a
  message thread, markdown answers carrying the citation count, the
  uncertainty badge and the saved-to path, a composer that sends on Enter, and
  a thread persisted to localStorage (fifty turns) with a Clear.
- `/api/ask` accepts a bounded conversation `history` — the last six exchanges
  within 4,000 characters, oldest trimmed first, validated field by field. The
  query job's prompt gains a delimited "Conversation so far" section telling
  the model that conversation is context for interpreting the question while
  claims must still come from cited evidence, and retrieval stays keyed on the
  bare current question, so BM25 is never polluted by chat history. The result
  — and every chat answer — now names the answering `provider` and `model`,
  resolved from the vault's job-model config.

### Fixed

- Switching graph sources with a node selected left stale indices behind, so
  the first hover threw a `TypeError` and highlighting was broken from then on.
- The selection ring's pulse compounded its own scale every frame, visibly
  ballooning and deflating, and overlays were sized from the camera distance
  at the moment of their creation, so they ballooned mid-fly too. Both now
  derive from stored base units every frame.
- The graph caption could render underneath the centred toolbar, hiding its
  tail — "N beyond server cap" — on exactly the large graphs where that tail
  matters. Zero-count segments ("0 hidden for rendering") no longer render at
  all.
- Timeline feed excerpts painted their paragraphs on top of each other — a
  line-clamp over block children with an inherited min-height. Now a
  max-height with a fade mask.
- The capture modal kept the previous capture's title, and neither
  drag-and-drop capture nor a proposal decision invalidated the cached
  collections, graph and status — so the viewer went stale after its own
  writes.
- Search responses could resolve out of order and overwrite newer results, or
  repopulate a box the operator had already cleared.
- The Services view reported the `web` integration "disabled" while that same
  integration was serving the page saying so. Cards no longer repeat the state
  in both the detail line and the badge, and single-value filter groups no
  longer render as noise.
- The favicon 404'd on every load, and the header wordmark still read
  "brainkit", left over from before the 0.5.0 rename.

## [0.6.2] — 2026-08-13

### Changed — before you upgrade: two paths in your config resolve somewhere else

Both entries change what an existing, working `.brain/config.json` does. Neither
is opt-in, and a configuration that was correct under the old rule can be wrong
under the new one without anything about it changing.

- **A relative `sources` entry is now resolved against the vault root instead of
  the directory `bk` happens to be run from, so a vault that was capturing files
  may capture none after this upgrade — and now says so instead of reporting
  success.** Under the old rule the same policy meant a different folder
  depending on where the operator was standing, and the documented automation
  path is the one where that always went wrong: `bk schedule` hands you a cron
  expression and a command to register, and cron runs jobs from `$HOME`. A
  source root that does not exist was walked in silence — the walker returns
  immediately on anything that is not a directory — so a scheduled watch
  reported `created 0` and exit 0 forever, and neither `bk status` nor `bk lint`
  mentioned it.

  When **no** configured source resolves, `bk watch` now refuses with
  `not_configured` and exit 2, because that is a configuration nobody can work
  around and amounts to the "no sources configured" state that was already
  refused. The refusal names each source, the path it resolved to, and — when
  the folder is still sitting where the old rule would have found it —
  `found_at_cwd`, so the fix is a path you can paste back into `sources`. When
  **some** sources resolve there is still real work to do, so the walk runs, the
  command still succeeds, and each missing source is reported in `failures[]`
  with the same sentence. Absolute paths are unaffected, and `~` is still
  expanded.

  A `sources` entry that is not a string, or is blank, is now rejected as
  `validation_error` when the policy is read, naming the offending index.
  `str()` coercion used to accept anything: `{"type": "folder", "path":
  "../inbox"}` — a reasonable guess at the schema — became the literal filename
  `"{'type': 'folder', 'path': '../inbox'}"`, which cannot exist and was skipped
  without a word.

- **The Obsidian integration's `path` option is resolved against the vault too,
  and unlike `sources` this one writes.** A relative destination that has been
  syncing to one directory will sync to another after this upgrade. The old code
  resolved against the current directory and then called `mkdir(parents=True)`,
  so a sync started from anywhere else did not merely look in the wrong place —
  it built a complete Obsidian vault, `.obsidian/app.json` and all, wherever the
  process started. The guard that refuses a target nested inside the source
  vault was validating that same unnamed location, so it was answering about a
  directory the operator had never chosen either.

  Where the recorded manifest shows this vault already mirrored somewhere that
  is not under the new target, sync **refuses** with `not_configured` and names
  both locations rather than silently starting a second copy — an orphaned
  Obsidian vault keeps opening for its owner while every later sync updates a
  mirror nobody reads. Three cases pass through untouched: an absolute
  destination, which was never ambiguous; a first sync, which has orphaned
  nothing; and a mirror already inside the new target, which is the operator
  whose relative path was unambiguous all along. `bk integration status
  obsidian` resolves the same way, so it stops reporting `ready` or `not-synced`
  for one unchanged vault according to which directory the question was asked
  from, and an enabled integration with an empty `path` reports `not-synced`
  rather than resolving to the vault itself and reporting `ready` forever. See
  [Obsidian](docs/integrations.md#obsidian).

### Changed

- **An HTTP failure from a provider is coded by its status, which changes the
  `error.code` an agent branches on.** Every status used to be
  `validation_error` — *"the request is malformed, fix it and send it again"* —
  which is true of a 400 and false of everything else a provider returns. The
  split is one question: is the failure about the bytes we sent? A **400** says
  yes and stays `validation_error`. **401** (the credential was rejected),
  **403** (it authenticated but is not entitled to this model or endpoint),
  **404** (the provider does not know this endpoint or model id), **429** (the
  quota outlasted all three attempts, `Retry-After` honoured) and **5xx** (the
  provider accepted the request and failed to answer it on all three) are now
  `not_configured`, because no request an agent can construct clears any of
  them. Each refusal names its next step: the environment variable holding the
  rejected key for a 401 and a 403, and `job_models` in `.brain/config.json`
  plus the model the job was routed to for a 404. `details` now carries the
  provider, the model and a `hint` alongside the status and the response body.
  A status not in that table keeps `validation_error` deliberately, so nothing
  changes meaning as a side effect of the table existing.

- **A wrapper can no longer downgrade a narrowed error.** `bk integration up
  postgres` caught the `NotConfiguredError` that the Docker probe raises, added
  context, and re-raised it as a plain `ValidationError` — so the same stopped
  daemon reported `not_configured` when reached directly and `validation_error`
  through the command an operator actually runs, telling an agent to rewrite a
  request against a machine where Docker was not running. Both container
  clauses now rebuild the class from the instance they caught, leaving the
  remedy the decision of whoever diagnosed the failure. The branch-policy reader
  in `domain/model.py` had the identical clause and was fixed with it; nothing
  downgrades there today, which is exactly why it was worth closing — a wrapper
  that widens the remedy is invisible until the day something it wraps learns to
  raise a sharper class.

### Fixed

- **`bk status`'s headline names the layer that is off.** It restated `healthy`
  as a lint-error count, which was true only while `healthy` meant lint alone;
  since 0.6.0 it also means every enforcement layer that enforces is running. So
  a vault with clean pages and no gate printed `✗ 0 lint error(s)` — a red cross
  above a zero — and that was the permanent, default outcome of the documented
  quickstart, because `bk init` outside a git repository can never make
  `commit_lint` active. It now reads `✗ enforcement off: commit_lint`, names
  every input `healthy` has, and falls back to `not healthy; see the rows below`
  rather than inventing a reason it cannot name. The enforcement table draws the
  advisory layer as `instructions (advisory)` and mutes it, so its tick stops
  reading as a fourth guarantee the vault does not have — said in the layer name
  rather than in colour, because this output is usually read through a pipe.
- **`bk doctor` had the identical defect and now says `✗ write gate
  not_enforcing`.** With every grammar installed and a gate failing open it
  printed `✗ 0 language(s) cannot be parsed`, never mentioning the gate, on the
  one run whose whole purpose is to exercise it.
- **`/api/status` and the web viewer use the same definition of `healthy` as the
  CLI**, computed by one shared predicate rather than by each surface taking its
  own slice of the same report. The viewer said `healthy` while the write gate
  was off — the divergence `bk status` was fixed for in 0.6.0, on the one
  surface with no enforcement rows underneath to contradict it. `/api/status`
  now carries an `enforcement` object (`gated`, `inactive`, and per-layer
  `layer`, `mechanism`, `active`, `advisory`), and the viewer's header names the
  reason: *needs attention: 2 lint errors; write_gate not active*. The `detail`
  and `script` fields are withheld, for minimality rather than privacy — they
  interpolate local filesystem layout a viewer has no use for. Lint findings
  stay consumer-scoped and enforcement state does not, deliberately: a finding
  on a redacted page must not flip a filtered consumer's headline while
  `lint_errors` reads 0 beside it, whereas a hook is installed or it is not,
  identically for whoever asks.
- **`graph/graph.json` and `views/` report `malformed` instead of `fresh` when
  the artefact is unusable.** The fingerprint lives in `freshness.json`, so
  overwriting `graph/graph.json` with `{{{ not json at all` left every input
  untouched and the comparison — which never opens the file — answered `fresh`.
  Integrity is now asked first and separately: the graph is checked with the
  same detector the code graph uses, since both are node/edge documents whose
  readers subscript `id`, `source`, `target` and `type` directly, and `views/` is
  checked for the generated marker on the first line of `views/home.md`, matched
  by shape so a marker written before the 0.5.0 rename still counts as generated.
  `bk lint` reports *Derived graph/graph.json is not what bk graph writes, so it
  answers nothing; run bk graph*. The `stale` boolean is set from an allowlist of
  the states that mean "regenerate this", so a state added later cannot default
  into looking healthy, and `bk status` renders `malformed` in red — the state
  colours were a denylist with a calm fallback, which drew both `malformed` and
  `partial` in the same grey as `missing`.
- **`bk lint` covers `wiki/index.md` and `wiki/log.md`, and no page can exempt
  itself from `wiki.outside_apply` any more.** The exemption keyed on a page's
  own `type: "system"` frontmatter, so the file being checked decided whether it
  would be checked: writing four words into any header under `wiki/` bought
  permanent silence, and the two pages `bk init` genuinely does seed were never
  looked at either — appending a fabricated claim to `wiki/index.md` produced no
  finding at all while `bk gate check-write` refused the same path. The two
  seeded pages are now named in a constant, and one still holding nothing but
  its heading passes; anything appended reports *Wiki page changed outside the
  apply gate*. The gate hook may fail open only because lint reports the bypass
  afterwards — its own header comment says so — which has to hold for every page
  the gate covers rather than for seven of nine.
- **The apply gate's duplicate-detection catalog no longer skips `type:
  "system"` pages either.** A page hand-written under `wiki/` with that type and
  a stolen title vanished from the catalog entirely, so `duplicate_identity`
  never fired against it. The seeded pages are *not* exempt here, unlike in the
  lint check, because the two lists answer different questions that only happen
  to agree today: `wiki/index.md` genuinely occupies the title *Brainskit index*
  and the slug `index`, so a proposal claiming either is a duplicate and refusing
  it is the check working.
- **`bk hooks install` migrates a pre-rename install instead of leaving it
  registered beside the new one.** If you installed the agent contract before
  the 0.5.0 rename, this is the release that finishes it: every lookup keyed on
  the current name, so the old `brainkit-gate` stayed registered and kept firing
  every session against whatever vault it was baked with, the old scripts stayed
  on disk beside the current ones, and the instruction file ended up with **two**
  managed blocks disagreeing about which vault the workspace has. `--force` did
  not help, because it decides whether to clobber an install that is currently
  the right one — so migration now runs without it, or the default upgrade path
  would keep silently running two gates. A legacy hook command is unregistered;
  its script is deleted when it still carries the sentinel proving an earlier
  install generated it, and otherwise unregistered and **left on disk with the
  reason**, because deleting a file the operator edited is a different act from
  dropping a settings entry. A legacy `.claude/skills/` directory is reported and
  never removed — markdown carries no such proof, and deleting on a guess is
  worse than the debris. A legacy managed block is retired from the instruction
  file, and the first one retired inherits the new block's position, so the
  contract stays where it was last read. Every action, including each file left
  in place and why, prints to stderr under `bk: RENAME`.

## [0.6.1] — 2026-08-13

A four-track review of the published 0.6.0 — a fresh install from PyPI, an
enforcement harness that broke each layer deliberately, a code and test-quality
pass, and a wheel-against-tag supply-chain check. The wheel verified byte for
byte; the two criticals below were introduced by the release it verified.

### Changed

- **Reusing a `proposal_id` with a different payload reports
  `validation_error`, not `conflict`.** This changes an error code agents branch
  on, deliberately. `conflict` names the remedy "re-read, rebuild, retry with the
  same id" — which, for this refusal, never clears: measured against unmodified
  code, all five retry cycles were refused, while a new id or no id succeeded on
  the first. An id is not a version, so re-reading cannot make a reused one
  valid. The refusal now says so and names the remedy, and the same message is
  raised from both sites that can produce it. The generated CLAUDE.md block and
  the agent skill said "retries carry a stable `proposal_id`", which is what
  steered agents into the loop; both now say otherwise.

### Fixed

- **A scoped `bk code build` no longer destroys nodes it cannot account for.**
  Pruning compared each stored node against `code_hash`, which resolves through
  `code_root()` — re-evaluated on every call, against an artifact that recorded
  no root to compare it with. When the two disagreed, every node read as deleted:
  on this repository's own graph, `2364 → 1559`, **805 nodes destroyed** by a
  build of one directory. Keeping is now the default and pruning requires
  positive evidence, so the same build reports `2364 → 2441`. Where the base
  cannot be established the graph is disclosed as `stale` rather than answered as
  `fresh`. `bk code build .` also scoped to nothing, and edges carrying an empty
  path were pruned while both of their endpoints were alive.
- **A stored code graph with malformed edges is refused rather than traversed.**
  Eight JSON-valid shapes reached the traversals; the fault is checked once at
  the read boundary, so all seven of them refuse together, and `bk code build`
  never merges, so the remedy — rebuild — is always reachable. The edges are
  **not** repaired: an edge missing its `type` renders in `bk code affected` as
  `via: <type>`, so normalising one would invent a relation that nothing
  extracted.
- **`bk code status` reports `malformed`.** It blessed a graph every other
  command refuses. It says `malformed` exactly when a read would refuse and
  `missing` exactly when a read would find nothing, and keeps exit 0 — like
  `stale` and `missing` — because scripts run it to decide whether to rebuild.
- **The wheel and the sdist carry the licence of the code they contain.** 43
  vendored files are MIT-covered, and neither `LICENSE-MIT` nor the vendored
  `NOTICE` was packaged; the root `NOTICE` that did ship pointed at a `src/` path
  that exists in the repository and not in an installation. Both files ship now,
  asserted by `verify-wheel.sh`, and `NOTICE` gives the repository and installed
  path for every vendored file — including three.js, a second vendored third
  party the web viewer serves and the file did not mention.
- **The assertion that proves it no longer refuses a sound sdist.** It was
  spelled `tar tzf "$SDIST" | grep -q`, which is a false negative under
  `set -o pipefail`: `grep -q` exits at its first match — entry 37 of 166 — and
  closes the pipe while tar is still writing the other 129. GNU tar dies of
  EPIPE, `pipefail` adopts that status for the whole pipeline, and the leading
  `!` inverts it into "missing". macOS ships bsdtar, which finishes writing
  before grep can leave and exits 0, so this passed on the maintainer's machine
  and failed on every run of CI's GNU tar — the worst shape a gate can fail in,
  a red asserting the artifact is broken while the artifact is fine. It blocked
  this release over the two files the entry above had just made ship, seconds
  after the wheel built from that same sdist was found to contain them. The
  listing is now read once into a variable and matched with a here-string:
  `printf … | grep -q` is measurably the same defect, surviving only while the
  listing fits the 64 KiB pipe buffer and returning 141 on one that does not.
  The condition is reproduced in the suite against a stub producer that reports
  a write error the moment its reader goes away, because the platform tar on a
  macOS checkout cannot show it.
- `verify-wheel.sh` isolates `XDG_CONFIG_HOME`, so verifying a wheel no longer
  writes to the machine-wide vault registry. The isolation is applied after the
  `uv` steps, because `uv` reads its own configuration from the same variable.
  The test suite gained the same isolation, at `tests/conftest.py`.
- The filing prompt explains what `seed` means. `taxonomy_seed` gained a reader
  in 0.6.0 and no sentence telling the model what the flag was for, which made it
  inert data on the wire.

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

[Unreleased]: https://github.com/huglabs/brainskit/compare/v0.7.0...HEAD
[0.7.0]: https://github.com/huglabs/brainskit/releases/tag/v0.7.0
[0.6.2]: https://github.com/huglabs/brainskit/releases/tag/v0.6.2
[0.6.1]: https://github.com/huglabs/brainskit/releases/tag/v0.6.1
[0.6.0]: https://github.com/huglabs/brainskit/releases/tag/v0.6.0
[0.5.0]: https://github.com/huglabs/brainskit/releases/tag/v0.5.0
[0.4.0]: https://github.com/huglabs/brainskit/releases/tag/v0.4.0
