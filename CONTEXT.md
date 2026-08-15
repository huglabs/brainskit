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

## Freshness

- **FreshnessLedger** — the one owner of `.brain/freshness.json`. Every read and
  write goes through it; no other module names the state file. Transitions are
  named after intent (`mark_applied`, `mark_reviewed`, `record_resurfaced`,
  `refresh_staleness`, `record_projection`, `drop`), so the rules that hold
  across the file are stated once instead of in each writer. Built at the
  composition root and handed to its collaborators, never constructed by them.
- **FreshnessSnapshot** — one read of the ledger, and every question asked of
  that read. Request-scoped by the same convention as `PrivacyBoundary`: taken,
  questioned, dropped — never held across a write.
- **applied entry vs annotation** — `content_hash` means "the apply gate wrote
  this page, and here is what it wrote", and `mark_applied` is the only writer
  that produces one. An entry without it is an **annotation** (a review request,
  a resurface note): it records something *about* the page and vouches for
  nothing. `applied_hash` answers `None` for it, exactly as for no entry at all,
  so `wiki.outside_apply` still reports the page. Populating the field outside
  apply would bless a hand edit rather than report it.
- **never-downgrade** — `review` is a weaker claim on attention than `stale`,
  and the ageing pass skips `review`, so writing it over `stale` removes the
  page from the loop rather than lowering a badge. `mark_reviewed` refuses,
  which leaves `review` reachable only from `fresh`.

## Enforcement

- **enforcement layer** — one named way a vault's write discipline is enforced:
  `write_gate`, `session_status`, `commit_lint`, `instructions`. The first three
  run and either let a write through or refuse it; `instructions` is
  **advisory** — it tells an agent the rules and stops nothing, so it is read
  past by `enforcement_ok` and by the `healthy` headline, and drawn as advisory
  rather than as a fourth guarantee.
- **`gated`** — specifically that the write gate is live, never "some layer is
  on". `session_status` is observability and `commit_lint` catches a bypass only
  after the fact; letting either imply it would report a guarded vault a Write
  tool can walk straight into.
- **AgentInstall** — what `bk hooks install --agent X` writes, as data:
  the instruction file, the hooks, whether the skill is installed. The one owner
  of the per-agent table, in `application/install.py`, iterated by the installer
  and by `Health` so neither can restate it — the divergence ADR 0004 records,
  and the third instance of the one `ConstantsHaveOneOwnerTest` was written for.
  An agent with no hooks reports no `write_gate` layer rather than an inactive
  one: "there is no guard here" and "the guard fell off" are different claims.
- **adapter** — `.brain/agent-<agent>.json`, the only thing on disk that records
  an install. It carries the resolved workspace (which is not always the vault)
  and the gate's deny rules, so `installed_agents` reading the directory is the
  whole answer to "who is this vault installed for". Its path is `adapter_path`,
  spelled once, because the gate, the installer and the reader all open it.
- **workspace vs vault** — `.claude/`, the instruction file and the git
  pre-commit hook belong to the project an agent is opened on; `.brain/` and the
  graph belong to the vault. A reader that assumes they are the same directory
  reports every layer off for any vault nested inside the project it guards.
- **exercised, not believed** — every enforcement answer except one says an
  artefact is installed and registered, which is not the claim "a write to
  `wiki/` is refused". `bk doctor` runs the gate hook on one path it must deny
  and one it must allow (`enforcing` / `not_enforcing` / `over_blocking` /
  `unknown` / `absent`), because the hook fails open by design in eight places.
  It lives in `application/doctor.py`, apart from the installer whose output it
  refuses to take on trust.
- **decide here, say it there** — where `interfaces/cli.py` ends. Writing an
  install is `application/installer.py`'s (`install_agent`); the stderr banners
  naming an inactive layer, a superseded hook, a former brand's debris or an
  unbuilt code graph are the CLI's, and `_install_hooks` is the four lines that
  join them. A function that asks the operator anything, or prints, does not
  cross. See ADR 0005.
- **EnvironmentPort** — the one thing `bk doctor` cannot ask for itself: which
  interpreter `bk` was installed into, and how to add a package to it. It is
  `infrastructure`'s to classify, so it crosses as a required parameter of
  `doctor()` rather than as an import, the way `SyncBoundaryPort` crosses into
  `IntegrationPort.sync`. The verdict is still decided on the application side —
  what counts as healthy is not a fact about the interpreter.
- **not over MCP** — `install_agent` is on `BrainskitService` and deliberately
  not a tool. It writes `.claude/settings.json`, the hook scripts and the git
  hook: that is, it writes the write gate. An agent that can reinstall or
  `--force` its way through those artefacts holds the switch on the mechanism
  constraining it, with no operator watching a terminal.

## Apply

- **ApplyTransaction** — the two-phase commit that is the only thing that writes
  `wiki/`: stage, back up, journal, replace, and undo all of it if the process
  dies. Lives in `infrastructure/apply_transaction.py`, one `commit(plan)` and
  one `recover()`. Takes no locks; it is handed ten unlocked accessors rather
  than the vault, because the vault's public methods each take the lock they
  need and calling one from inside the transaction would block on a lock this
  process already holds. The constructor is the audit of what an apply may
  touch.
- **lock ordering** — `FileVault.commit_wiki_batch` takes `write.lock`, then
  `registry.lock`, then `applied.lock`, then `freshness.lock`, and then
  delegates. Stated once, there. This is why `FreshnessLedger.mark_applied`
  returns entries instead of writing them: a second writer inside this block
  would invert the order and deadlock.
- **committed boundary** — `state: committed` in the journal is the line between
  losing an interrupted apply and keeping it. Before it recovery restores
  everything; after it recovery only cleans up. `phase` is written by every step
  and read by nothing — recovery decides from `replaced`, `inflight`, `backups`
  and `raw_move`. The phase is a forensic breadcrumb, not a control input.
- **checkpoint** — one of the ten moments at which the journal on disk has just
  been brought up to date (`prepared`, `page-inflight`, `page-replaced`,
  `wiki-written`, `raw-move-inflight`, `raw-move-applied`, `state-written`,
  `index-written`, `applied-recorded`, `committed`). Named so that "crashed at
  X" describes a state a real crash can leave, not an instant no journal
  describes.
- **FailurePoint** — the injected crash: a checkpoint, optionally narrowed to
  one page and to the *n*th time it is reached. A constructor argument with no
  default and no environment variable; `crashing_at` returns a copy, so an
  engine crashes only because a caller named the point. Production never arms
  one.
- **InterruptedApply** — what a `FailurePoint` raises. A `BaseException` on
  purpose: `commit` rolls back in its own `except Exception`, and a test that
  asked for a crash wants the half-finished vault a crash leaves, not the tidy
  rollback an error gets.

## Ingestion

- **Ingestion** — the ingestion path as one object: capture a source, sweep the
  watched folders, decide what the new source relates to, and heal the state a
  vanished page leaves. In `application/capture.py`, built at the composition
  root and handed `vault`, `index` and the `FreshnessLedger`; it constructs no
  sibling of its own. It is what makes `BrainskitService` a facade that owns
  nothing rather than nearly nothing — see ADR 0007.
- **relatedness floor** — a page counts as related to a capture only when it
  shares at least `_RELATED_MIN_SHARED_TERMS` of the capture's own vocabulary,
  measured against the page body. BM25 orders the candidates and has no
  absolute scale, so a rank is not evidence on a small corpus; and the file
  name contributes only what the document itself corroborates, so a suggestive
  name cannot stand in for unrelated content. Marking a page is a durable claim
  on a human's attention, which is why it takes a floor rather than a ranking.
- **prune, not filter** — `_walk_source` drops an ignored directory from
  `os.walk`'s traversal instead of rejecting its files afterwards, so
  `node_modules` costs one comparison rather than a stat per file. The
  observable difference is the skip count: one for the tree, not one per file
  inside it.
- **capture annotates, it never grades** — a capture asks the ledger to mark a
  page for review and nothing else. `mark_reviewed` carries the never-downgrade
  rule; this is the caller that used to lack it, and a writer that set the
  status itself would park a stale page in `review` until the next apply.

## Errors

- **presentation table** — `interfaces/errors.py`'s `PRESENTATIONS`: one row per
  `error.code`, three columns, one per surface — process exit status, HTTP
  status, JSON-RPC code. A surface reads the column it speaks and decides
  nothing itself. Adding a `BrainskitError` subclass without a row fails
  `ErrorTableHasOneOwnerTest`, which reads every `code = "…"` out of the tree;
  the fourth thing this repository was restating on both sides of a boundary.
  See ADR 0006.
- **status is the family, `code` is the member** — two codes may share an HTTP
  status (`refused` and `policy_denied` are both 403) because the status
  narrows what happened and the body's `code` names it. The reverse is what the
  table exists to stop: one status, `400`, for every error the vault can raise.
- **remedy decides the status** — the same rule ADR 0002 used to pick the code.
  `conflict` is 409 because 409 means re-read and resubmit; `not_configured` is
  501 and never 503, because 503 promises "try again later" and this code's
  whole content is that retrying is pointless; `model_response_invalid` is 502
  because brainskit was the gateway and the provider's output was the invalid
  response. A status that names the wrong remedy sends an agent into a retry
  loop that cannot terminate — the trap `proposal_id_reuse_error` records.
- **envelope vs refusal** — `error_envelope(exc)` carries code, message and
  enriched details; `refusal_envelope(code, …)` is for a guard that refuses
  before an exception exists (denied Host, foreign Origin, missing token,
  unrouted path) and omits what it has nothing to say about, rather than
  emitting empty fields a client must test for.
- **install hint at the render point** — a `needs` list becomes the command
  that installs it *on this machine*. It belongs to whichever module renders
  errors, not to whichever one happens to be the CLI: while it lived in
  `interfaces/cli.py`, MCP and web callers got `{"needs": [...]}` and no
  command — the failure the hint was written to end.
- **reached the dispatcher** — MCP's line between an HTTP failure and a
  JSON-RPC one. A `JsonRpcRequestError` (bad envelope, mismatched mirror
  header, unsupported protocol version) is `-32600` **and** a real 400;
  anything raised by the call inside answers 200 with the error in the body,
  per the Streamable HTTP spec. So `not_found` is 404 in the viewer and 200
  over MCP, and that is two protocols, not two tables.
- **surface default vs domain parse** — `Consumer.parse` (ADR 0001) is the one
  place an unknown consumer becomes an error. Which boundary an *unnamed* read
  runs under is the surface's own decision and lives in
  `_consumer_for_args(args, *, default)`: `human` for an interactive read, with
  a `--json` caller required to declare instead; `local` for a read whose
  output is a file or a graph, with no refusal, because the artifact outlives
  the command.

## Vendored extraction

- **Vendored tree** — `infrastructure/codeanalysis/`, 27,618 lines of Graphify
  against 22,393 first-party: 57% of this repository is code we may not edit.
  `NOTICE` is the contract; adaptation lives outside, in the adapter.
- **graphify alias** — the synthetic top-level package
  `codeanalysis/__init__.py` registers, so vendored files keep importing each
  other by upstream's absolute names without being edited. Reach a vendored
  module through `graphify.<module>` and never by its real dotted path:
  importing one file both ways imports it twice, under two names, with two sets
  of module-level state. That is a rule, not a preference — the suite once broke
  it in nine places and ran against two `_DISPATCH` tables and two resolver
  registries holding nine identically-named, non-identical resolvers.
  Enforced as source text by `VendoredModulesAreReachedOnlyThroughTheAliasTest`;
  the shim itself is the one exemption.
- **CodeExtractorPort** — the seam brainskit owns over the vendored extractor
  (`extract`, `available`, `survey`) and the only surface behavioural tests bind
  to. Binding to `graphify.extract` instead would tie tests to the thing a
  re-vendor is most likely to move.
- **Language corpus** — `tests/fixtures/<language>/{source/, expected.json}`,
  discovered rather than listed, one directory per language. Chosen for spread
  across the three extractor paradigms (`config-engine`,
  `standalone-tree-sitter`, `no-grammar`), declared per fixture, not for count.
  Nodes and edges compared in full as a normalised projection: sorted,
  path-relative, posix, JSON-native, nothing dropped.
- **Regeneration verdict** — a golden is a claim about inputs → outputs, so
  `--regenerate` records the sha256 of every fixture source and classifies
  before writing: sources changed → written; nothing changed → no-op; sources
  unchanged and graph changed → **refused**, with the diff. That last case is
  the regression case by definition, and unlocking it takes a second,
  differently-named flag. A golden any red build can refresh is not a test.
