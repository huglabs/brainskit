# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Because a published version is permanent, the `v<version>` tag on the commit an
artifact was built from is the durable record of what shipped.

## [Unreleased]

### Added

- An installable `dsh-brainskit` bundle under `plugins/dsh-brainskit`. It
  launches Brainskit over stdio through DSH's official MCP client, contributes
  privacy-aware memory guidance, and denies mutable wiki, filing and
  integration operations unless the operator explicitly sets
  `BRAINSKIT_ALLOW_MUTATIONS=1`. Append-only capture and retrieval stay
  available in the default posture.

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

### Changed

- **MCP clients now default to the `cloud` scope — breaking.** `bk serve --mcp`
  takes `--consumer cloud|local` (both transports; default `cloud`; `human` is
  refused with `policy_denied`), and every tool and resource answers under it
  ([ADR 0010](docs/knowledge/decisions/architecture/0010-mcp-declares-its-consumer.md)).
  Before, MCP read as `local` where no consumer was named and `status`
  returned the unfiltered operator report. Now `status` is the filtered
  `/api/status` report (no `vault` key at `cloud`, never-ingest excluded at
  `local`, no `projections`), `proposals` is consumer-scoped, `lint` withholds
  findings on material outside the scope (`redacted_findings`), and a
  `search`/`context` `consumer` wider than the server's is refused with
  `policy_denied` rather than clamped. `tools/list` offers only the consumers
  the server will answer. **To keep the previous behaviour for an agent on
  this machine, start the server with `--consumer local`.**
- **`--json`'s `ok` now means the command succeeded, and agrees with the exit
  status** ([#10](https://github.com/huglabs/brainskit/issues/10)). `_emit`
  wrote `"ok": true` as a literal, so `bk lint` with an error and `bk vaults
  sync` with a failed vault printed `{"ok": true, "result": {…}}` and exited 1.
  The envelope's `ok` is now `false` whenever the exit is non-zero, with
  `result` still carrying the findings or the per-vault outcomes. A
  `bk update --yes` whose upgrade command fails now reports `ok: false` and
  **exits 1 (it exited 0)**; `state: "unavailable"` — pypi.org unreachable —
  is unchanged, `ok: true` and exit 0. The MCP `lint` tool reports
  `isError: true` when lint finds an error. All three read one rule,
  `succeeded()` in `interfaces/errors.py`; every other command is a report
  whose answer is its `state` and keeps `ok: true`. Scripts that read `result`
  only when `ok` is true must read it whenever `result` is present.
- **A `cloud` consumer is no longer told where the vault lives**
  ([#12](https://github.com/huglabs/brainskit/issues/12)). Installation facts —
  absolute local paths — are now inside the privacy boundary
  ([ADR 0009](docs/knowledge/decisions/architecture/0009-installation-facts-inside-the-privacy-boundary.md)).
  `/api/status` omits the `vault` key at `--consumer cloud` rather than
  substituting a label; `local` and `human` are unchanged. Clients that read
  `vault` from a cloud-scoped status must treat it as optional. The inventory
  found one more reachable site: `bk code communities --consumer cloud` checked
  for `networkx` before the boundary, so its install hint, which names local
  interpreter paths, reached a cloud caller. It now refuses first.
- **Judgment jobs say when privacy policy narrowed what the model read.**
  `bk ask`, `bk resurface`, `bk digest` and `bk lint --semantic` report
  `withheld_sources` in their JSON — a count, never a name, branch or hash — and
  print "N source(s) withheld from the model by privacy policy" when it is
  above zero. The web viewer shows the same line under an ask answer.
- Tests: the suite's machine isolation no longer depends on pytest —
  `python -m unittest` or running a test file directly can no longer write the
  operator's `~/.config/brainskit/vaults.json`
  ([#20](https://github.com/huglabs/brainskit/issues/20)); every in-process CLI
  call in the suite refuses a run that never reached the command under test
  ([#21](https://github.com/huglabs/brainskit/issues/21)).

### Security

- **MCP `capture` no longer copies any readable file into the vault.** A file
  path is accepted only inside the vault's project (code root or an installed
  workspace, symlinks resolved), outside the vault itself, and never a
  credential file — `.env*` except `.env.example|.sample|.template|.dist`,
  `*.pem`, `*.key`, SSH private keys, `.netrc`, `.npmrc`, `.pypirc`, anything
  under `~/.ssh`, `~/.aws`, `~/.config/gcloud` or a `.git` directory. Refusals
  are `policy_denied` with a `reason` and name neither the path nor the file's
  content. Text and URLs are unchanged, and so is `bk capture <path>`.
- **MCP `file` could declassify a source.** It resolved any source by hash
  prefix and moved it to any branch, so a cloud client could move a
  never-ingest source into a cloud branch and then search it. `file`,
  `approve` and `reject` now resolve only what the server's consumer may see,
  and answer `not_found` for the rest.
- **MCP `ask`, `resurface` and `lint --semantic` no longer read local-only
  evidence for a `cloud` server** when a local model is mapped; evidence is read
  no wider than the server's consumer.
- **MCP integration operations** (`integration_configure`, `_up`, `_down`,
  `_sync`) no longer echo paths, container names or `*_env` names to a machine
  consumer, and `integration_configure` refuses an `options.consumer` wider
  than the server's.
- **MCP flags are JSON booleans.** `save`, `semantic`, `enabled` and `managed`
  were coerced with `bool()`, so `"false"` saved an answer; limits accepted
  `true` and `"5"`. Anything but a JSON boolean (or integer) is now
  `validation_error` naming the argument, before anything runs.

### Fixed

- **`bk code status` no longer calls a code graph with no `files` key `fresh`**:
  a missing input set is now `stale` ("cannot be verified"), like a non-map one.
- **The Claude Code SessionStart hook counted lint errors as 0** once
  `bk lint --json` began reporting `ok: false`: it read `result` only from an
  `ok` document. It now reads any document that carries a `result`.
  **Re-run `bk hooks install --agent claude`** (no `--force` needed — the
  managed script is rewritten in place) to refresh an installed copy;
  `bk status` and `bk doctor` flag a stale copy as `outdated` with that command.
- **The release gate now proves both artefacts reached PyPI**
  ([#7](https://github.com/huglabs/brainskit/issues/7)). The visibility check
  matched `*"brainskit-$version"*` anywhere in the simple index, so a `v0.6`
  tag was satisfied by the existing `brainskit-0.6.0-py3-none-any.whl`, and one
  matching file was enough for a wheel-only or sdist-only upload to go green.
  `scripts/check-pypi-visibility.sh` requires `brainskit-$V-py3-none-any.whl`
  and `brainskit-$V.tar.gz` each, anchored as the link text of the index
  (`>…<`). It runs in its own `visible` job after `publish` — `publish` holds
  `id-token: write` and still runs no project code — and `github-release` now
  waits on `visible`. A new first step refuses a PEP 440 local version
  (`0.8.0+huglabs.1`) on tag push and `workflow_dispatch` alike, so a private
  build never looks releasable.
- **`bk code build` no longer graphs the vault itself when the vault is the
  code root** ([#8](https://github.com/huglabs/brainskit/issues/8)) — a vault
  outside any repository, or one configured with `code_root: ""`. The
  exclusion was a path-prefix test, the prefix is empty in exactly that case,
  and an empty prefix excluded nothing, so `bk init`'s unattended bootstrap
  build indexed the vault's own files, the hook scripts it had just installed
  among them. The vault's
  own directories (`raw/`, `wiki/`, `views/`, `graph/`, `output/`, `.brain/`)
  are now excluded by name there, and the installed hook scripts by exact path
  wherever the vault sits. Excluded files count as covered, so `bk code status`
  does not call the graph `partial` for them. A vault with nothing but itself
  is refused with `code_root`, why that root was chosen, and the next step:
  set `code_root` in `.brain/config.json`, then run `bk code build`.
- **Refusal hints no longer name `--code-only`**
  ([#11](https://github.com/huglabs/brainskit/issues/11)), a flag no command
  accepts. `bk code import` of a graph with no code nodes now says only
  `file_type: "code"` nodes are kept and points at `bk code build`.
- **`bk forget --force` is no longer undone by the `bk reconcile` that
  `bk lint` recommends next** ([#9](https://github.com/huglabs/brainskit/issues/9)).
  `--force` leaves the raw file on disk by definition, and `reconcile`
  re-registered any hash it did not find. `bk forget` now records a tombstone,
  keyed by content hash, under `"forgotten"` in `.brain/registry.json`:
  `reconcile` skips it and reports a `forgotten` count, `bk watch` no longer
  recaptures it (counted under `forgotten` too), `bk lint` no longer reports it
  as untracked, and an explicit `bk capture` of the same content clears it.
  Every other registry writer — an apply, `file` — carries the tombstones over.
  The key is omitted while empty, so an existing registry is unchanged.
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
- **`bk ask` no longer refuses a whole question because search recall brushed
  a `never-ingest` source** ([#14](https://github.com/huglabs/brainskit/issues/14)).
  `ask`, `resurface` and `lint --semantic` read their context as the default
  `human` consumer and relied on the judgment router to refuse any
  `never-ingest` branch in it — which refused too much (one private hit sank an
  unrelated question) and too little (a wiki page whose provenance does not
  resolve yields no branch, so it reached the model). They now read under the
  `local` boundary after link expansion, so `never-ingest` sources, the pages
  compiled from them and pages of unresolvable provenance are withheld from the
  model instead of blocking the call. When every match is withheld the job
  refuses with `policy_denied` and a hint (`bk search` for ask and resurface,
  `bk lint` without `--semantic` for lint) instead of calling a model. The
  router's own refusals are unchanged and remain the last defence. `local-only`
  evidence on a cloud route is covered by the entry below.
- **`bk digest` no longer tells the model about material its route may not
  see.** The route is chosen as before — from the `local`-visible recent
  sources — but is now asked of the router itself (`route_for`, which `run`
  goes through), and the prompt's status, freshness ledger and filing proposals
  are held to that route's boundary: `local` for Ollama, `cloud` otherwise.
  Branch names it may not see, freshness entries for pages compiled from
  withheld or unresolvable sources, and proposals for such sources are dropped;
  on a cloud route the vault's absolute path and hook script paths go too
  (ADR 0009). `withheld_sources` counts the dropped sources, pages and
  proposals together.
- **`bk doctor --json`'s `healthy` no longer depends on the optional `code`
  extra** ([#13](https://github.com/huglabs/brainskit/issues/13),
  [#24](https://github.com/huglabs/brainskit/issues/24)). It was `false` on
  every default install, and on the recommended `[code]` extra too, which
  carries 13 of the 29 grammars. Absent grammars are now a choice; only a
  broken install counts — a partly installed `[code]` (named in
  `code.grammars_broken`) or a grammar outside its pinned version. New fields:
  `code.grammars_state` (`absent`/`complete`/`partial`), `code.extras_complete`
  (the extras fully installed), and `code.graph_without_grammars` when a vault
  uses a code graph this machine cannot rebuild — reported, not counted. The
  install command suggests the smallest useful step: the missing grammars of a
  partial `[code]`, else `brainskit[code]`, else `brainskit[code-all]`. The
  text report reads `13/29 (code extra complete)`.
- **`bk status` and `bk doctor` now detect hook scripts older than this
  version installs.** A brainskit-generated script is compared with what
  `bk hooks install` would write now; a stale one is `outdated: true` with a
  `bk hooks install --agent <agent> [--root …]` hint and is listed in
  `enforcement.outdated`. An outdated write gate counts as not gated and not
  healthy; an outdated session-status script is a warning. `/api/status` and
  the web viewer header report it too (for `cloud`, absolute paths in the hint
  become `<path>`, ADR 0009), and the SessionStart summary names outdated hooks
  with the refresh command.
- **`bk doctor` no longer reports `healthy` for a write gate the agent does
  not run.** A script that refused the probe but is unregistered or outdated
  now fails the check, and doctor draws the advisory `instructions` layer as
  `bk status` does — it used to show a fourth green tick.
- **`ask`, `resurface`, `digest` and `lint --semantic` no longer refuse the
  whole job when the job is mapped to a cloud provider and a `local-only` source
  appears in recall** (or among recent sources, for `digest`). Evidence is read
  under the boundary of the route the router picks: on a cloud route
  `local-only` and `never-ingest` evidence is withheld and counted in
  `withheld_sources`; a local route — flat or privacy-keyed — still receives it.
  If every match is withheld the job returns `policy_denied` with a hint naming
  how to route `local-only` evidence to a local provider
  (`job_models.<job>.local-only`). The router's refusal stays as the last line
  of defence.
- **The git pre-commit hook quoted the vault path as JSON**, so a vault under a
  non-ASCII directory (e.g. `Operação/`) was never found and every commit
  failed; a path containing `$` or backticks was expanded or executed. The hook
  is now shell-quoted and carries the `# brainskit:generated` marker;
  `bk hooks install` upgrades a hook written by an earlier release without
  `--force`, and `bk status` reports it as outdated. Claude Code hook commands
  in `.claude/settings.json` are shell-quoted too — a workspace path with a
  space made the write gate exit 127 and let every write through; stale
  unquoted entries are replaced on reinstall.
- **`bk status` no longer reports `commit_lint` active for a pre-commit hook
  that lints some other vault.** The layer was judged by the file's existence
  and content, so while this repository's own hook named a directory that did
  not exist — the JSON-quoted path above — `bk status` said "vault healthy" and
  every commit failed. It now reads the `--vault` a brainskit-generated hook
  passes, as sh will see it, and a path that is not this vault makes the layer
  inactive with the path, whether it exists, and the reinstall command. This
  also catches a repository moved since install. Operator-written hooks are
  not judged.
- **`bk doctor` runs the pre-commit hook** as well as the write gate
  (`enforcement.commit_lint_probe`): the hook git will run — `core.hooksPath`
  honoured — executed from the workspace. Exit 0 (clean) or 1 (lint found
  errors) is `enforcing`; anything else — 126/127 from the shell, 2 from a `bk`
  error such as "Not a brainskit vault" — is `not_enforcing` with the hook's
  first stderr line, as are a hook naming another vault (not run) and one
  without its executable bit, which git skips. An operator-written hook is
  `not_judged` and never run. `not_enforcing` and `unknown` make `healthy`
  false: such a hook refuses every commit without checking one, and
  `bk status` already counts an inactive `commit_lint` against its own
  `healthy`. `gated` still means the write gate alone. The lint the probe runs
  refreshes page ages in the freshness ledger, as every commit does.
- **`bk doctor`'s write-gate probe runs the command `.claude/settings.json`
  registers**, through `sh -c` as Claude Code does (or directly, for an
  exec-form entry with `args`), with `CLAUDE_PROJECT_DIR` set and the workspace
  as working directory. It ran the script path, so the unquoted registration
  above — exit 127 under the shell, every write allowed — passed the probe
  because the script itself denied correctly. The report names the command
  (`command`) and says what was run (`exercised`: `registered_command`, or
  `script` when nothing registers it, which `gated: false` already fails). A
  gate script without its executable bit is now `not_enforcing` (the shell's
  exit 126) rather than `unknown`.
- **A vault whose repository was moved says so.** The adapter records the
  workspace an install went to; once that directory is gone `bk status`
  printed "enforcement off: write_gate, session_status, commit_lint" and each
  row said a file was missing, and `bk doctor` found no gate to run and called
  the install healthy. Every layer now says the recorded workspace no longer
  exists and carries `workspace_missing: true` and a reinstall hint —
  `bk hooks install --agent <agent> --root <project>`, naming the repository
  the vault now sits in when there is one — which `bk status` prints under the
  table and appends to its headline. Doctor's probes report `unknown` with the
  same hint, and `healthy` is false. The reinstall itself works without
  `--force`: an unedited skill file rendered for the old vault path is now
  recognised as brainskit's and rewritten, where it used to refuse.
- **A deleted gate script that is still registered no longer reads as
  healthy.** With `.claude/hooks/brainskit-gate.sh` gone but its
  `.claude/settings.json` entry left in place, `bk doctor` reported the gate
  `absent`, a state it treats as healthy. Claude Code still runs the registered
  command, `sh -c` exits 127, and Claude Code does not treat that as a block,
  so every write went through. Doctor now runs the registered command and
  reports `not_enforcing`: "the registered command cannot run", with the
  shell's error quoted and `bk hooks install --agent <agent>` as the hint.
  `healthy` is false. `bk status` reads the layer inactive, says it is
  registered but missing, and carries the same hint. `absent` now means no
  registration and no script. Likewise, a brainskit pre-commit hook that
  `core.hooksPath` leaves unrun is now `not_enforcing` in doctor rather than
  `absent`, which matches `bk status`. Both give the installer's `lint --changed`
  hint.
- **An empty-evidence refusal says what happened.** When a cloud-mapped `ask`,
  `resurface`, `digest` or `lint --semantic` has no evidence, the router falls
  back to the `_inbox` policy. On a vault whose inbox is `local-only`, it
  refused with "Local-only content can only be routed to Ollama", about content
  that did not exist. The refusal stands and is still `policy_denied`, but the
  message now says nothing in the vault matched (or, for `digest`, that nothing
  a model may read remained) and that nothing was sent to any model. The hint
  suggests rephrasing, `bk search`, or mapping `job_models.<job>.local-only` to
  a local provider.

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

- `bk` now imports and locks vault state on Windows. The vault keeps the same
  blocking shared/exclusive lock contract and ordering: POSIX uses `flock`,
  while Windows locks a stable one-byte region through `LockFileEx`.
  Previously the module-level `fcntl` import made every Windows command fail
  before argument parsing.

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
