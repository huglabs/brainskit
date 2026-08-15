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
