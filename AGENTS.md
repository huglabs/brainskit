# AGENTS.md

## Brainkit Python implementation learnings (2026-07-30)

- Preserve the DDD dependency direction: `interfaces → application → domain`;
  infrastructure implements application ports and the domain stays vendor-free.
- Treat `raw/` identity as content identity. Capture hashes in streaming chunks,
  registry writes are locked/atomic, moves are healed by `reconcile`, and lint
  must compare current bytes with the registered SHA-256.
- The human-owned `.brain/schema.json` is enforced by `apply` and lint. Custom
  frontmatter belongs in an operation's `metadata`; core provenance fields
  cannot be overridden.
- Validate the entire apply proposal before staging any page. Source hashes,
  citations and wiki links must resolve before a write is eligible.
- Keep mechanical commands LLM-free. Judgment flows use job specs and the
  policy router; a mixed evidence set inherits the strictest privacy policy.
- Do not load `.env`. Provider configuration stores only an environment variable
  name, and the runtime reads that variable explicitly.
- Prefer incremental FTS5 upserts on capture/apply and reserve full rebuilds for
  `reindex`, file moves and reconciliation. SQLite runs in WAL mode.
- Expose the same application cases through CLI JSON mode and MCP; do not
  duplicate vault rules in interface adapters.
- Smoke the actual CLI/MCP contracts before expanding automated tests. The
  baseline suite uses only the standard library and must pass Ruff, mypy,
  ResourceWarning-as-error, and wheel resource checks.
- Local v0.2 benchmark on this machine: 500 durable captures in 1.52 s,
  rebuild of 502 documents in 39 ms, and 100 privacy-aware searches in 253 ms
  (about 2.5 ms per query).
- Do not commit automatically at the end of a task.

## Brainkit apply, privacy and lifecycle learnings (2026-07-30)

- Treat an apply as a recoverable domain transaction, not a sequence of atomic
  file writes. Stage the full batch, persist a write-ahead journal, back up
  pages plus registry/idempotency state, and recover an incomplete commit when
  the vault opens.
- Require a stable `proposal_id` plus its canonical request hash for retry
  safety, and a `base_hash` for updates. Reject reuse of the idempotency key
  with another payload. Recheck page versions while holding the vault write
  lock so validation and commit cannot be separated by a concurrent write.
- The apply gate needs a detectable invariant. Store each committed page hash
  in freshness state and make lint reject new or modified non-system wiki pages
  that do not match that state.
- Privacy filtering belongs after every retrieval expansion. Filtering only
  direct BM25 hits is insufficient because outgoing or backlink graph neighbors
  can reintroduce `local-only` or `never-ingest` evidence.
- Filing policy is a durable workflow: propose a branch, store the apply payload,
  then either execute it for `auto+digest-review` or wait for explicit
  approve/reject. Never move the source before an approve-each decision.
- Every judgment job has an output schema and shares one bounded repair loop.
  Validation feedback is returned to the model; the engine must never replace a
  failed judgment with a hardcoded answer.
- Freshness is operational state, not a page decoration. Successful applies
  mark pages fresh, related captures request review, configured age marks stale,
  and generated views/digests consume the same state.
- Build and install the wheel in an isolated target before delivery to verify
  that prompt specs, output schemas and templates are packaged.

## Brainkit persistent graph and web integration learnings (2026-08-01)

- Model every optional integration as a vault policy plus separate runtime
  state. Persist enablement, ownership mode and non-secret options in
  `.brain/config.json`; persist sync checkpoints, PIDs and container state in
  `.brain/integration-state.json`.
- Never persist credentials or read `.env`. Neo4j passwords, PostgreSQL DSNs
  and remote web bearer tokens are resolved only from explicitly configured
  environment-variable names.
- Apply privacy policy to the final graph, after all nodes and edges have been
  expanded. Neo4j and PostgreSQL must declare `consumer=local|cloud` before a
  sync so graph relationships cannot reintroduce restricted evidence.
- Obsidian export is a manifest-based synchronization. Generate views first,
  copy atomically, and delete only paths previously recorded as brainkit-owned;
  never treat the user's whole Obsidian vault as disposable output.
- Managed Neo4j/PostgreSQL services use stable Docker container names and
  vault-local durable volumes under `.brain/services/`; `down` stops compute
  without deleting data. External mode never mutates operator-owned service
  lifecycle.
- Portable PostgreSQL graph support can remain extension-free: indexed
  nodes/edges tables, JSONB properties, foreign keys and a bounded recursive
  `graph_walk` function provide a native query surface.
- A managed web `up` is complete only after `/api/health` confirms its random
  instance identity. Recheck that identity before signaling a stored PID, so a
  stale/reused PID cannot stop an unrelated process. The viewer and API should
  be read-only, dependency-free, bearer-protected off loopback, and reuse
  application use cases instead of reading vault files directly.
- Prove the public service contract with real `curl` calls before adding HTTP
  regression tests; keep the same lifecycle operations available through CLI
  JSON mode and MCP tools.

## Brainkit network MCP and unified transaction learnings (2026-08-01)

- Keep stdio as the zero-network MCP default. A network MCP socket should be a
  bounded, stateless Streamable HTTP endpoint with Bearer authentication on
  every request, constant-time token checks, Origin validation and mandatory
  TLS when bound outside loopback.
- Implement the full declared JSON Schema draft with a maintained validator and
  format checker, then layer brainkit's provenance invariants on top. Permit
  local references while refusing implicit remote schema retrieval so schema
  validation cannot leak vault data or acquire a hidden network dependency.
- Treat wiki replacement, raw filing, registry/status, freshness, idempotency
  and the FTS5 update as one recoverable unit of work. Back up SQLite WAL/SHM
  files along with JSON state, journal every phase and restore all surfaces on
  any failure before exposing the vault again.
- Real Neo4j sync must use the driver and a database write transaction. Prefix
  graph identities with a stable per-vault namespace, delete only that vault's
  prior projection and MERGE relationships so repeated pushes are isolated and
  idempotent.
- Curl-smoke authentication failures, valid initialization and every public web
  endpoint before encoding those contracts as HTTP regression tests.

## Brainkit full-system verification learnings (2026-08-02)

Method: static checks + the 25-test suite, then a live vault exercised through
CLI, MCP stdio/HTTP, the web API, Docker-backed Neo4j/PostgreSQL, and a real
Ollama provider. Everything below was a gap the 25 in-process tests did not
catch, which is the point: the suite asserts behaviour the engine mediates, so
it cannot see layout drift, foreign-library exceptions, or a flag that is
stored and then never read. All of it is now fixed and covered by regression
tests (127 tests across `test_engine.py` plus four `test_fix_*.py` modules).

Two process lessons from the repair round itself:

- Partition parallel repair by *file*, not by defect. These nine defects
  touched six files, and three of them landed in `integrations.py` alone —
  splitting by defect would have had two agents overwrite each other. Where a
  fix genuinely spans files (`export` needs both a CLI flag and a service
  signature), fix the contract up front and let each side implement against it.
- Re-verify every reported fix yourself — and re-verify the dismissals too. An
  agent reported that a first `up neo4j` fails the 30 s readiness deadline
  while the macOS bind mount is chowned. A clean run reached ready in 6.7 s, so
  the report was dismissed as measurement noise. **That dismissal was wrong.**
  A later end-to-end sweep reproduced the timeout with Docker idle: the chown
  takes 7 s or 40 s depending on virtiofs cache state, so a single passing run
  disproves nothing about a deadline. One observation cannot refute an
  intermittent failure — reproduce the *reported* condition, not a convenient
  one. Conversely, asking an agent to check whether a sibling case was also
  broken found `entity` pages landing in `wiki/entitys/`: half the page kinds
  were misfiled, not one.

- Derive a plural directory name from a table, not from `f"{value}s"`.
  `PageKind.SYNTHESIS` yields `synthesiss`, so synthesis pages never land in
  the `wiki/syntheses` that `init` scaffolds. Assert that every scaffolded
  directory is reachable by some `PageKind`, or the two definitions drift.
- An interface may only promise the error envelope it can actually produce.
  CLI and MCP catch `BrainkitError`; the integration adapters wrap only
  `ImportError`, so `psycopg.OperationalError` and `neo4j.ServiceUnavailable`
  escape as tracebacks and terminate the MCP process. Adapters must translate
  every vendor exception at the boundary they own.
- A privacy flag that is accepted, persisted and never read is worse than an
  absent one. Obsidian stores `consumer` and still exports `never-ingest`
  filenames and branch paths into `graph.json` and the branch views; the file
  export targets take no consumer at all. Filename and branch are disclosure
  even when the body is withheld, and `cypher`/`kuzu` output exists to be
  loaded elsewhere. Gate every egress on the same boundary the databases use.
- Managed container lifecycle must reconcile the running container against the
  current policy. `up` reuses a container by name, so one port collision leaves
  it wedged in `Created`; a later `configure --port` is silently ignored and
  only a manual `docker rm` recovers. Compare the container spec before reuse.
- The Ollama driver sends only `temperature`, so prompts run at Ollama's 4096
  default no matter what window the model advertises; `digest` fails at ~4.7k
  tokens. Because `local-only` evidence is *required* to route to Ollama, the
  privacy-preserving path is also the most context-starved one. Pass `num_ctx`
  through from provider configuration.
- Relatedness for freshness is computed from the capture's filename stem, not
  its content, so an on-topic note named `zzz-9f2b.md` marks nothing and an
  off-topic note named `memoria-compilada.md` marks the page for review. Query
  the indexed body.
- `human` is documented as interactive-only but implemented as the default
  value: `--json` and MCP both accept it explicitly and return `never-ingest`
  bodies. Either enforce the restriction at the machine boundaries or document
  it as a default.
- Validate a stored value where it is written, not where it is read. Only the
  CLI constrained `consumer`, through an argparse `choices`; the MCP
  `integration_configure` tool takes a free-form `options` object, so a typo
  persisted into `.brain/config.json` and surfaced much later at sync. An
  optional field still has to be valid when present — `_validate_policy` now
  checks any stored consumer, while leaving it optional for Obsidian and
  mandatory for the databases. The sync-time guard stays as defence in depth
  against a hand-edited config.
- A rejected value must be named. `init` used to reject a branch policy with
  empty `details`, which said nothing about which branch or which field was
  wrong. Report the branch, the field, the observed value and the valid
  choices; `inbox_policy` reports under its own name.
- A redaction notice must not re-disclose what it redacted. `context` reported
  `redacted` as a list of `{path, privacy, reason}` — so asking for a
  `never-ingest` source as a `cloud` consumer returned the filename and the
  branch it lives in, in the one payload that is handed to a cloud model. The
  same field stayed empty on the search path, where filtering actually
  happened, so it under-reported exactly when it mattered. It is now a count on
  both paths, matching `search`. Chasing the cosmetic inconsistency is what
  surfaced the leak: an inconsistent report is worth opening even when it looks
  like formatting.
- Reconcile every durable surface, not just the registry. The registry is keyed
  by content hash and heals a moved file; freshness is keyed by path, so a page
  deleted outside the gate left an entry that could never be revived and still
  counted in `bk status`. `lint` now reports `freshness.orphaned`, `status`
  ignores it and `reconcile` drops it. Widen the return type at the application
  layer rather than loosening the `dict[str, int]` port contract.
- A readiness probe must prove the service will still be there for the next
  command. PostgreSQL's entrypoint answers `pg_isready` from the temporary
  server it runs during `initdb`, then shuts it down and restarts: measured
  transitions were ready at 1.09 s, gone at 1.44 s, back at 1.64 s. A single
  successful probe inside that window reported ready and the very next `sync`
  hit `server closed the connection unexpectedly`. Require the probe to stay
  green across a stability window, and never time a first boot with a deadline
  tuned to a warm cache — the deadline is now 300 s and per-integration
  configurable via `ready_timeout_seconds`.
- Teach the agent the contract instead of hoping it infers one.
  `bk hooks install` seeds `.claude/skills/brainkit/SKILL.md` and a managed
  block in the agent's instruction file, from templates packaged alongside the
  job prompts. Anything written into a file the operator owns must be fenced
  and replaced **in place**, so re-running neither duplicates the block nor
  reorders the instructions written around it; an existing skill or pre-commit
  hook is reported rather than overwritten. Git is now optional there — a
  missing repository skips the hook instead of failing the whole install, since
  the skill has nothing to do with git.

## Brainkit uv packaging learnings (2026-08-02)

- `bk` acts on a vault passed by `--vault`, not on the current project, so the
  distribution unit is `uv tool install`, not a project dependency. The tool
  environment is isolated from every consumer, which is why extras belong in the
  install target (`uv tool install '/path/to/brainkit[integrations]'`) and not
  in the consumer's lockfile. `-e` keeps the PATH `bk` on the working tree while
  developing the engine; `--force` is required to change extras or re-point an
  existing tool.
- The setuptools backend and `[project.scripts]` were already uv-compatible, so
  nothing in the build had to change for uv: `uv add <path>`, `uv tool install`,
  wheel targets and PEP 508 direct references all resolved the same metadata.
  Verify a packaging claim by installing, not by reading `pyproject.toml`.
- `uv` picks the first interpreter it finds unless the project pins one. It
  selected 3.12 while the repository's own caches were 3.13, so `.python-version`
  now pins the dev interpreter. It constrains only development; `requires-python`
  still governs what an installed `bk` accepts.
- Packaged resources cannot be verified from the source tree, where `jobs/`,
  `jobs/_output-schemas/` and `templates/` exist regardless of what the wheel
  contains. `scripts/verify-wheel.sh` builds, installs into a throwaway venv,
  asserts all 15 resources through `importlib.resources`, then drives
  `init → capture → status → lint` and fails on `ok: false`. An import check
  would pass on a wheel that cannot initialize a vault.
- The wheel smoke test needs a complete vault policy, and the policy contract
  has one source: the script imports the `policy()` fixture from
  `tests/test_engine.py` instead of embedding a copy that would silently rot
  when `VaultConfig` gains a required area.
- `ruff check` and `mypy src` pass; `ruff format --check` does not (20 files).
  The project is lint-clean, not format-clean — do not treat a formatting diff
  as a regression introduced by a change.

## README learnings (2026-08-02)

- Derive every command/flag table in the README from `build_parser()` and the
  MCP dispatch map, never from memory. Writing the table from recall put
  `lint --semantic` under "mechanical, never calls a model" when it runs the
  `lint-semantic` structured job — the same class of claim the engine exists to
  prevent.
- A full-file `Write` on a document another session may be editing is unsafe:
  the README's install section gained the HTTPS/`@<ref>` pinning guidance
  between the read and the rewrite, and only the stale-read guard prevented
  losing it. Re-read immediately before a full overwrite; a partial `Read`
  (with `limit`) does not satisfy that guard.
- The origin serves git over HTTPS only: port 22 never answers (`ssh` reports
  `Operation timed out` and a raw connect hangs with no reset), while 443
  responds. Install from `git+https://…`, which authenticates through the git
  credential helper and pins the resolved commit in the tool receipt. Diagnose a
  transport before rewriting a URL; the SSH failure was upstream, not local.
- Verify the artifact that is actually shipped. `uv build --wheel` reads the
  working tree, while a plain `uv build` writes the sdist and then builds the
  wheel *from it* — the pipeline publishing uses. The first version of the gate
  verified the working-tree wheel and would have uploaded the sdist-derived one,
  so a file the sdist dropped would ship unverified. `scripts/verify-wheel.sh`
  now builds both and proves the sdist-derived wheel, and `scripts/publish.sh`
  uploads those exact files instead of rebuilding. (setuptools does carry all 15
  resources into the sdist here, so the gap was latent, not yet a defect.)
- A published version is permanent, so publishing refuses a dirty working tree:
  a registry filename that maps to a working tree nobody can reconstruct is
  worse than a failed upload. Tokens reach uv through `UV_PUBLISH_PASSWORD`
  rather than `-p`, keeping them out of the process arguments.
- Git access and API access are different privileges on the same host. The
  stored credential cloned and fetched, yet `/api/v4/projects/...` answered
  `insufficient_scope` under `PRIVATE-TOKEN`, `Bearer` and `JOB-TOKEN` — so
  publishing needed a second token and the numeric project id, and a working
  `git clone` proved nothing about the package registry. The registry this was
  learned against is no longer the target: releases now go to PyPI through
  Trusted Publishing, which has no token at all (see `docs/development.md`).
  The lesson survives the move — read access to a repository is not evidence of
  publish access to anything.

## Agent workspace learnings (2026-08-02)

- **The vault is not the agent's workspace.** `hooks install` resolved every
  agent-facing path from `vault.root`, so a vault nested in a project wrote
  `.claude/` and `CLAUDE.md` *inside the vault*, where no agent ever reads
  them. This is the worst shape a defect can take here: every file lands, the
  enforcement summary reports success, and the gate never runs once. `--root`
  now names the workspace; the default stays the vault for standalone use.
  Every helper already took `(root, vault)` separately — only the call site
  conflated them, which is why the fix is small and the bug lasted.
- **A silent wrong default needs a loud warning, not just a flag.** Adding
  `--root` alone would have left the old invocation silently broken. An install
  that fits the mistake's shape — a vault with no agent configuration of its
  own, nested inside a directory that has some — now prints a `WORKSPACE` block
  on stderr naming the directory and the exact flag to re-run with.
- **Compute a diagnostic before the side effect it inspects.** The first
  version of that check ran after the installer had already created `.claude/`
  in the workspace, so it observed its own output and concluded all was well.
  It fired only for the case that needed no warning. Any "does this look
  wrong?" check must read the pre-install state.
- **Two consumers, one bug.** `bk status` resolved enforcement from
  `vault.root` too, so a *correctly* installed gate still reported every layer
  off. The workspace is therefore persisted in `.brain/agent-<agent>.json`
  (`version` 2) rather than recomputed — nothing else on disk remembers it. A
  v1 adapter without the field falls back to the vault, so existing installs
  report unchanged. When fixing a path-resolution bug, grep every *reader* of
  that path, not just the writer.
- **A path under another path defeats a substring assertion.** The test for
  "the hook still governs the vault" used `assertNotIn("VAULT=<project>")`
  while the correct value is `VAULT=<project>/docs/brain` — which contains it.
  It failed against correct code. Compare whole assignment lines, not
  substrings, whenever one candidate value is a prefix of another.
- **A store documented as per-vault namespaced had the namespace in its ids but
  not in its delete predicate.** The PostgreSQL export prefixed nothing and ran
  `DELETE FROM edges` / `DELETE FROM nodes` with no `WHERE`, so syncing any
  vault destroyed every other vault's subgraph in that schema — another
  application's data, not a stale copy of our own. Neo4j, the same feature one
  method away, scoped its delete correctly (`MATCH (n {vault_id: $vault_id})`),
  which is what made the gap invisible: the design was right, one of its two
  implementations was not. When a guarantee is claimed for a capability, check
  it once per backend; a correct sibling is evidence about intent, never about
  the code you have not read.
- **Read where a value is defined, not where it looks defined.** The namespacing
  `hashlib.sha256(...)[:24]` sat in `_sync_neo4j`, close enough to `_sync_postgres`
  to be misread as shared by both. It was not, so the natural ids
  (`page:<path>`, `raw:<hash>`) went to PostgreSQL unprefixed and collided
  across vaults. Both backends now derive it from one `_vault_id()`; two
  implementations of one guarantee should not each own a copy of the rule.
- **Fixing half of this defect is worse than shipping it.** Scoping the delete
  alone would have left `id text PRIMARY KEY` receiving colliding natural ids —
  silent data loss becoming a duplicate-key crash on the second vault. The
  unscoped wipe was the only reason the collision never fired. Before scoping a
  delete, check what the full-table delete was masking: uniqueness constraints
  are held up by the very statement you are about to constrain.
- **`CREATE TABLE IF NOT EXISTS` cannot add a column to a table that exists.**
  Deployed schemas would have kept a shape the new code assumed. Every column
  the code depends on is also stated as `ALTER ... ADD COLUMN IF NOT EXISTS`,
  then backfilled, then `SET NOT NULL` — inert on a fresh table, the actual
  migration on an old one, and idempotent either way. Backfill adopts existing
  rows into the syncing vault rather than a sentinel: the old truncating refresh
  guarantees those rows are one vault's last complete sync, and the scoped
  delete then reclaims them. A sentinel no vault ever names is permanent debris.
- **Assert the outcome, not only the mechanism.** Tests that the SQL contains
  `WHERE vault_id = %s` pass against code that emits the right string and still
  loses data. `FakePostgresStore` applies the statements to a dict — honouring a
  missing `WHERE` by emptying the table, as PostgreSQL would — so a test can say
  what the user cares about: after vault B syncs, vault A's rows are still there
  and the row count is the sum, not the max. That is the test that fails against
  the old code.

## Provider drift, lint config and the viewer boundary (2026-08-03)

- **A provider request shape rots even though nothing in the repo changed.**
  The Anthropic driver sent `temperature: 0`; sampling parameters were removed
  on the current frontier models and are now rejected with a 400, so every
  judgment job would have failed against the flagship provider while the suite
  stayed green — the tests exercised the driver against a fake `urlopen`, which
  accepts anything. One of them asserted `temperature == 0` for Anthropic *and*
  OpenAI, so the suite was pinning the defect in place. When a test asserts a
  provider's wire format, it is only as current as the day it was written.
- **Read `stop_reason` before `content`.** Both terminal cases produce a
  response that parses fine and answers nothing: a refusal carries an empty
  content list, and a `max_tokens` truncation carries JSON that stops
  mid-token. Returning either sends the repair loop after output that no retry
  can fix — three attempts, three identical outcomes, and a final error naming
  the schema rather than the cause. `max_tokens` now also bounds thinking,
  which is on by default, so a budget that fit before can truncate now.
- **Constrained decoding cannot express a free-form object.** Strict structured
  output requires every object to close `additionalProperties`, and the ingest
  schema's `metadata` is deliberately open — closing it would forbid exactly
  the custom frontmatter `.brain/schema.json` exists to let a vault declare. So
  the projection sent to a provider is an allow-list of keywords (unverified
  ones degrade to "not sent", never to "rejected"), and a schema that cannot be
  expressed exactly drops to guidance rather than being silently narrowed. The
  repair loop validates the full schema either way, so the provider projection
  can only cost a retry.
- **`bk status` runs a full lint, so anything per-page in lint is per-page in
  status.** `schema()` re-reads and re-parses `.brain/schema.json` on every
  call and was called once per page, and the JSON Schema validator was
  recompiled each time with it. Hoisting the read and memoizing compilation on
  the schema's canonical serialization (not its identity — the vault hands back
  a new object every call) made lint 6× faster at 200 pages, and the gap widens
  with the page count.
- **Origin does not stop DNS rebinding; Host does.** The MCP endpoint has
  checked `Origin` since it shipped and the viewer checked nothing, which reads
  as an oversight rather than a decision. Origin is the wrong instrument
  anyway: a same-origin GET carries none at all, so a rebound page passes the
  check trivially. What it cannot forge is `Host`, which still names the
  attacker's domain. Both are now enforced, ahead of `/api/health`, because a
  loopback viewer needs no token and answers at `--consumer human`, which
  withholds nothing.
- **A test that assembles a server by hand cannot see a check added to it.**
  The web test built `BrainkitWebServer` field by field, so a handler that
  began reading two new attributes failed with `AttributeError` instead of
  exercising them. `build_server()` is now the only way in, tests included.
- **Ruff's default is four rule groups.** With no `[tool.ruff]` section the gate
  meant roughly "no undefined names, no unused imports", and with no
  `[tool.mypy]` section `mypy src` never checked an untyped function's body.
  Turning both up found a `.docx` entity-expansion path, a `urlopen` that would
  honour `file:`, and 13 unannotated definitions — under 40 real findings
  total, which is the argument for having done it earlier. Deliberately not
  selected: `SIM` (its findings here are the atomic-write idiom and nested
  `with` in tests) and `E501` (a formatter's job; this tree is lint-clean, not
  format-clean, and selecting it would bury the rules that matter).
- **Check for a second writer before trusting `git diff --stat` as your own.**
  This round's tree also grew a Postgres vault-scoping feature and its tests
  from a concurrent session. The tell was arithmetic: the suite went 356 → 400
  while the changes made here account for ~34. Read-modify-write patches over
  whole files are unsafe under that condition; targeted edits are not.
- **A loop over many vaults is a different command from the one it calls, and
  the difference is entirely in what it refuses.** `bk vaults sync` (400 → 429)
  syncs every registered vault into the shared store, so the three decisions
  that matter are all negative: a vault that has not enabled the target is
  *skipped* rather than enabled on its behalf, a vault that fails is reported
  and the loop continues (the exit status carries it, since nothing is raised),
  and `--consumer` is refused outright — `export` already refuses it per vault
  because the policy owns the boundary, and here a refresh deletes the vault's
  rows before reinserting, so a narrowed run would silently replace what the
  store holds with less, for every vault at once. The registry itself is
  declared, never discovered: a scan would be slow, would miss vaults outside
  the trees it was given, and would find checkouts and backups that must never
  write into a shared schema under their own `vault_id`. Store the path
  `expanduser().resolve()`d exactly as `FileVault` does — the id is a hash of
  that string, so an entry differing by a symlink reports an id no row carries.

## Splitting the service, and the watch ignore list (2026-08-03)

- **Draw the seams from the call graph, not from the headings.** The 2,500-line
  `BrainkitService` looked entangled because the methods sat in one file, but
  almost every private helper had one or two callers inside a single cluster.
  Walking `self.<attr>` references with `ast` produced the decomposition in one
  pass — and named the only genuinely shared machinery, the judgment repair
  loop and the apply gate. Reading the file top to bottom would have suggested
  the opposite.
- **Extract leaves first and the layering falls out.** `compilation`,
  `retrieval` and `judgment` need only ports, so they moved with no rewiring at
  all. Everything above them then had something to depend on, and the finished
  layer is an import DAG with `services` as the only module that sees all of
  it. A cycle check on the import graph is worth keeping — it is the property
  that makes the split real rather than cosmetic.
- **Facade methods must survive the move.** `apply`, `search`, `status`,
  `ingest` and the rest are called by the CLI, MCP and the viewer. Moving the
  body and leaving a one-line delegation kept every call site and the whole
  suite unchanged, which is what let the refactor be verified rather than
  believed. mypy's `has no attribute` errors enumerated the list for free.
- **Do not "fix" indentation with a regex, and never with a heuristic.**
  Deleting a line with `re.sub(r"x = y\(\)\n(\s*)", r"\1", s)` leaves the next
  statement carrying both indents. The repair pass I then wrote — re-indent any
  line deeper than its predecessor allows — flattened bullet lists inside
  docstrings, because a docstring's continuation lines are not statements. Two
  lessons: prefer exact-string edits when removing a line, and if a mechanical
  pass touches whole files, re-scan the result for the damage it can cause
  (`ast.get_docstring` against each node's `col_offset` found it immediately).
- **An AST extractor must carry decorators.** `node.lineno` points at `def`,
  not at `@staticmethod`, so a moved method arrived without its decorator and
  the orphan stayed behind. mypy caught it as "invalid self argument", which is
  a confusing symptom for a missing line; `grep -n "@staticmethod"` across the
  new modules is the direct check.
- **A watch that captures everything is worse than noisy — it is permanent.**
  `_watch` walked `rglob("*")` over every configured folder, so pointing it at
  a project filed `node_modules` and `.git` into `raw/`, where identity is the
  hash of the bytes and nothing can be removed. The ignore list prunes at the
  directory level (`os.walk` + `directories[:] = kept`), so an excluded tree
  costs one comparison rather than a stat per file inside it.
- **Absent and empty are different answers.** A vault written before `ignore`
  existed must inherit the defaults, or upgrading the engine silently starts
  capturing what it never did; a vault storing `[]` has answered the question.
  Reading `"ignore" not in raw` rather than a falsy check is the whole
  difference, and it is the same distinction `--consumer` needed.
- **Selection is a vault rule, so it does not live in the CLI.** The walk moved
  to `BrainkitService.watch_once()`; the CLI keeps only the loop and the
  interval. Any second caller that walked a folder its own way would file what
  the policy excluded.

## Vendored code-analysis subset (2026-08-03)

- `src/brainkit/infrastructure/codeanalysis/` is vendored from Graphify
  (Apache-2.0, upstream `00efd6e`, v0.9.32). It is the **only** directory in this
  repo excluded from Ruff and mypy, and the only place a stdlib-plus-jsonschema
  claim does not hold. Both exclusions are scoped to that path and stated in
  `pyproject.toml` next to the setting.
- Keep it **byte-identical to upstream**. A re-vendor should be a copy, not a
  merge, and a behaviour seen there should be reportable against Graphify
  without first subtracting our edits. Every adaptation belongs in the adapter
  that implements `CodeExtractorPort`.
- Vendoring source did not vendor a parser. The tree-sitter grammars are
  compiled wheels and remain a real dependency, which is why the code graph is
  the `code` extra rather than a base requirement: ~70 MB for a capability most
  vaults never use. The core stays at one dependency.
- Attribution is not optional and not decorative: Apache-2.0 §4 requires the
  retained `NOTICE`, the copyright line, and a statement of changes. They live in
  `NOTICE` and `src/brainkit/infrastructure/codeanalysis/NOTICE`.
- Brainkit keeps its own traversal (`affected`/`path`/`hubs`) rather than the
  vendored networkx one. It needs no dependency and already answers under a
  `--consumer`. Take vendored analysis only for what Brainkit cannot do:
  community detection, import cycles, graph diff.

## CLI terminal UI: colors, tables, grouped help, guided onboarding (2026-08-03)

- The CLI had zero rendering of its own before this: every human-mode command
  fell through one `_emit()` that printed `json.dumps(value, indent=2)`, and
  no color/table library was imported anywhere in `src/`. Given "the core
  stays at one dependency," `rich` was rejected in favor of a hand-rolled
  ANSI module, `interfaces/console.py` — pure string builders (`style`,
  `table`, `kv_panel`, `rule`, `status_line`, `banner`), no `print` calls of
  its own, so every primitive is trivially unit-testable.
- **Gate all color on `stream.isatty()`, never on an ambient flag.** Because
  `io.StringIO().isatty()` and pytest's default capture both report `False`,
  every existing test that asserted exact human-mode text kept passing
  untouched the moment color was gated this way — structure could still
  change freely (JSON dump → table), but escape codes only ever appear when
  the actual target stream is a real terminal. This is what let a from-scratch
  renderer rewrite of ~20 commands land with zero test breakage.
- **A cell that already carries ANSI codes breaks naive width math — this
  only shows up on a real TTY, never in a piped/test run.** `len()` on a
  `status_line()`/`state_tag()` result counts the escape bytes as columns,
  so raw-length width calculation both misaligns columns across rows (one
  colored, one not) and — worse — lets truncation slice into the middle of
  an escape sequence, cutting off the trailing reset and leaking that
  cell's color into everything printed afterward in the terminal. Fixed by
  measuring `_visible_len()` (ANSI-stripped) everywhere width/padding is
  computed, and by truncating an overflowing colored cell on its
  ANSI-stripped text rather than its raw string. **This bug was invisible to
  the entire automated suite** (piped/StringIO output never carries color)
  and only surfaced by actually driving the CLI over a `pty` with
  `TERM=xterm-256color` and reading the raw bytes — confirm real-terminal
  rendering this way before calling a terminal UI done, not just via tests.
- **Driving an interactive prompt (`input()`) in a test harness needs a real
  pty, not a piped stdin.** `sys.stdin.isatty()` gates the wizard
  deliberately (`init` refuses non-interactively without `--config`), so
  verifying the guided wizard end-to-end means `pty.openpty()` +
  `subprocess.Popen(stdin=slave, stdout=slave, stderr=slave)`, sending `\r\n`
  on the write end whenever `select()` reports the read end has gone idle
  (i.e. the child is blocked on `input()`). A fixed-count "send N enters"
  script is fragile against prompt count changes; idle-triggered sending
  is not.
- **Group a flat argparse subcommand list without touching argument
  parsing.** Rather than subclassing `HelpFormatter` (fighting internals),
  `bk --help`/`-h` is detected before `parse_args()` runs — scan argv left to
  right, and if the help flag appears before any non-flag token, print a
  custom grouped+branded help and return, otherwise fall through to
  argparse untouched. `bk <command> --help` is never intercepted, so every
  subcommand keeps argparse's default formatting exactly as before. The
  one-line `help=` string each command was registered with has no public
  accessor; it lives on the subparsers action's `_choices_actions`, read
  once rather than duplicated into a second source of truth.
- A mechanical test (every command name appears in exactly one
  `HELP_CATEGORIES` bucket) is what makes "grouped help stays complete"
  a fact instead of a hope the next added command quietly breaks.

## Onboarding used to install the code graph's absence, not the graph (2026-08-05)

- **The gap:** `bk hooks install` wrote the skill, the instruction block and
  both Claude Code hooks, but never called `bk code build`. `bk init` didn't
  either. So a fresh onboarding left `bk code status` reporting `missing`
  forever, unless an agent happened to notice the `code build` row in the
  skill's own command table and ran it unprompted — and the SessionStart hook
  never asked it to, because it only ever read `bk status`/`proposals`/`lint`.
  An agent talking to brainkit only over MCP could not reach the code graph
  at all: none of the 16 registered tools wrap `code build`/`status`/
  `affected`/`path`/`hubs`/`communities`/`cycles`/`diff` — that whole surface
  was, and still is, CLI-only.
- **The fix:** `_install_hooks` now calls `service.code_build(None)` itself,
  best-effort, right after the enforcement summary. Caught as `BrainkitError`
  first (the extractor's own clean failures — no extractor configured, the
  `code` extra not installed — already carry the right hint) and `Exception`
  second (mirrors `_sync_one_vault`: a producer's own failure belongs to the
  one best-effort step it happened in, not to the onboarding it must not
  cancel). `--skip-code-build` opts out for a vault that documents something
  other than a code repository, or when a slow first extraction should not
  block onboarding. Reported in `result["code_graph"]` and, on anything but
  `state: "built"`, on stderr — the same "a skip has to say so" rule
  `_warn_about_inactive_enforcement` already enforces for the other layers.
- **The other half:** `brainkit-status.sh` (SessionStart) now also calls
  `bk code status --json` alongside the three calls it already made, and
  prints one more line: `missing`/`stale (N changed, N removed)`/`fresh (N
  files)`. `code status` never needs the `code` extra — it only re-reads the
  stored `graph/code.json` and re-hashes the files it names — so this is safe
  to call unconditionally, including on a vault that skipped the build.
  Placed after the existing `bk status` early-exit, alongside
  `PROPOSALS_JSON`/`LINT_JSON`, with the same `2>/dev/null` + `null`-on-empty
  tolerance: a `code status` failure degrades to no line, never a hook
  failure.
- **Verified live, not just unit-tested:** scaffolded a real git repo with
  two Python files, ran `bk init` + `bk hooks install --agent claude --root
  .` against it for real (no stubs). First run: `code_graph.state == "built"`,
  15 nodes / 18 edges / 4 files, and the installed `brainkit-status.sh`
  printed `code graph fresh (4 files indexed)`. Editing a source file after
  install flipped it to `code graph stale (1 changed, 0 removed) - refresh
  with: bk code build`. Re-running with `--skip-code-build` on a second vault
  produced `code graph missing - build it with: bk code build` and the
  matching stderr notice. All three states confirmed against the real CLI
  before writing a single test.
- **Test-suite consequence worth knowing:** every existing `_install_hooks`
  call site in `tests/test_hooks_install.py` and `tests/test_fix_interfaces.py`
  builds its `BrainkitService` without an `extractor` (the same fixture every
  other test in those files already used), so `code_build`'s first check —
  `if self.extractor is None: raise ValidationError(...)` — fires before any
  filesystem walk. The new step is a no-op `state: "skipped"` in every one of
  them; nothing about the existing 618 tests changed. A real `state: "built"`
  path needed its own fixture (`CodeGraphBootstrapBuildTest`, gated on
  `test_code_graph._HAS_CODE_EXTRA`, same real-repo shape as
  `test_code_graph.CodeBuildFixture`) rather than retrofitting `VaultCase`.

## The settings.json merge's idempotency key was the wrong thing (2026-08-06)

- **The bug, confirmed by reproduction before fixing:** `_register_claude_hooks`
  deduped purely on the literal command *string*. Onboarding `eigent` the day
  before had left a `.claude/settings.json` carrying a `brainkit-gate.sh`/
  `brainkit-status.sh` pair pointing at a completely different project's vault
  (copied in, or written by an earlier install at a different `--root`) —
  since that path differs from the one this install writes, the old idempotency
  check saw two distinct commands to keep, not one to replace. Reproduced with
  a minimal repro (seed `settings.json` with a hook at `/some/other/repo/...`,
  install fresh) before writing a single line of the fix — both entries
  survived, exactly as onboarding found it.
- **The fix:** the idempotency key is now the hook's *identity* — template name
  (`brainkit-gate`, `brainkit-status`) plus event — not its command string.
  `_is_stale_hook_command` recognises any registered command whose basename is
  `<template>.sh` but isn't the one being installed now; `_prune_stale_hook_
  entries` removes those before the existing "already registered?" check runs,
  and drops an entry that loses every command this way rather than leaving
  `{"hooks": []}` debris. Reported in `settings.pruned` and warned on stderr
  by `_note_pruned_stale_hooks`, the same "a silent change is the expensive
  part" rule `_warn_about_inactive_enforcement` already follows.
- **What had to keep working:** unrelated tooling on the *same* event
  (`Bash`/other hooks) is untouched by the prune — only a command whose
  basename literally matches brainkit's own naming is ever a candidate. A
  second install at the *same* path prunes nothing and stays byte-identical
  (verified both as a manual repro and as a test:
  `test_reinstalling_at_the_same_path_prunes_nothing_and_stays_idempotent`).
- **6 new tests** in `StaleHookReplacementTest`
  (`tests/test_hooks_install.py`): stale replaced not duplicated, replacement
  reported in the return value, replacement warned on stderr, unrelated
  tooling survives, an emptied entry is dropped whole, same-path reinstall
  stays idempotent. Full suite 631 passed, ruff clean. Not yet applied to
  `eigent` itself — that vault's `settings.json` was already hand-fixed during
  onboarding; this closes the bug for the *next* multi-vault onboarding.

## `bk init` asked twenty questions to reach its own defaults (2026-08-07)

- **Measured before changing anything**, by driving the real wizard through a
  pty and pressing Enter for every default: **20 prompts**, under a header that
  said "Step 1/4". Ten were per-branch `privacy`/`filing` pairs, so naming a
  fourth branch bought two more questions. Four asked the operator to hand-author
  JSON at a terminal prompt, one of them a thirty-line integrations object. Zero
  arrow-key selections: choosing a privacy mode meant *typing* `local-only` and
  being re-asked on a typo.
- **Two failures were not cosmetic.** (1) The happy path built a vault that
  could not run: `qwen3:8b` was hardcoded for all six jobs and nothing ever
  asked ollama what was installed, so accepting every default on a machine
  without that model configured six jobs to fail silently. (2) Answers were
  validated only when `VaultConfig.from_dict` assembled them, *after* the last
  prompt — pasting a partial integrations object (the natural way to enable one
  integration) hit the completeness check and discarded all twenty answers,
  leaving no vault and no way to resume. Both reproduced before fixing.
- **The root cause was structural**: `console.py` had grown rich *output*
  primitives and the CLI had no *input* ones, so every question degraded to
  `input()` with the suggestion spelled into the prompt string. New
  `interfaces/prompt.py` (stdlib `termios`/`tty`/`select`, no new dependency)
  supplies `select`/`multiselect`/`text`/`confirm`; `interfaces/onboarding.py`
  probes first and asks three questions.
- **`sys.stdin.read(1)` cannot be used to read escape sequences.** It pulls the
  rest of `\x1b[B` into `TextIOWrapper`'s buffer, so `select()` — which only
  sees the OS buffer — reports nothing pending and every arrow key resolves as a
  bare Esc, i.e. "cancel". Only a live pty run caught it; the unit tests took
  the non-tty fallback path and passed throughout. Read the **fd** (`os.read`)
  so the only buffer in play is the one `select` measures.
- **Do not assert `tcgetattr(fd)` equality to prove the terminal was restored.**
  `PENDIN` is a transient kernel status bit meaning "input is pending" — true
  precisely because a key was just sent — so an exact comparison reports a leak
  that is not there. Mask it and compare the settable flags; `ECHO`, `ICANON`
  and `ISIG` are what actually matter.
- **The completeness crash is now unreachable, not just unlikely**: `_assemble`
  generates the integration set from `INTEGRATION_NAMES` rather than from
  whatever the operator named, and `job_models` is keyed off `jobs/*.md` rather
  than a restated list. A test asserts all 8 extras combinations pass
  `VaultConfig.from_dict`, and another asserts the job list matches what ships.
- **Onboarding used to end before the product started**: the "Next" panel named
  `bk status`/`capture`/`ask` and never `bk hooks install`, the one step that
  writes the skill and CLAUDE.md. `init` now offers it as a checked extra
  (writing outside the vault only after the vault exists) and, when declined,
  leads the "Next" list with it.
- **20 new tests** in `tests/test_onboarding.py`, three of them pty-driven.
  Negative control run for the escape-sequence bug: reverting `_read_key` fails
  exactly the two arrow/space cases and leaves the Ctrl-C case passing, which is
  correct — a single byte is unaffected by the buffering. Full suite 651 passed,
  ruff clean. Result: 20 keystrokes → 5, and the model comes from the machine.

- **`bk` with no command, and `bk <typo>`, are the two first impressions** —
  and argparse answered both by reprinting all thirty command names: once as a
  brace-delimited usage blob, again as `invalid choice: (choose from ...)`. Bare
  `bk` now prints the grouped help and exits 0. The guard is `if not argv`, not
  "the scan found no command": `bk --version` also exhausts the loop without
  naming one and must still reach argparse. The usage line no longer enumerates
  the commands, because the grouped listing below it already is that
  enumeration. A typo gets a git-style suggestion, ranked **prefix matches
  first** — `bk co` is a truncation, which `difflib.get_close_matches` scores
  poorly on its own but which obviously means `code` or `context`. Argparse
  keeps ownership of every other argument error; `_mistyped_command` returns
  `None` for a valid command so a bad flag stays the subparser's message to
  write. 7 tests, negative-controlled.

- **Bare `bk` at a terminal browses; every other path stays flat text.** Eight
  group rows is a better first screen than thirty command rows, so a tty gets a
  two-level `prompt.select` browser (group -> command -> that command's own
  `--help`, delegated to argparse so the browser cannot drift from what the
  operator will type next time). `-h`, a pipe and a redirect all still get the
  flat grouped listing, because that is what gets read, grepped and scripted
  against. Back-navigation is `Cancelled` handling and nothing else: Esc pops a
  level, Esc at the top returns 0. `select(quiet=True)` exists for this --
  a browser you can back out of must not leave a checkmark beside an abandoned
  path, while a linear wizard *wants* that transcript. The browser never runs
  what it highlights: `bk` is typed by people who do not yet know what these do.
- **Driving a pty test by marker or by fixed sleep is a race; drive it by
  quiescence.** A prompt writes its question line *before* entering raw mode, so
  a key sent when the question appears can land on a still-canonical tty, where
  Esc is buffered awaiting a newline that never arrives and the child hangs to
  the timeout. Waiting for output to go idle is the only signal that the child
  is genuinely blocked on a read, and it re-synchronises per screen, which a
  multi-level browser needs. It must also require that *some* output has
  arrived -- a slow import is silent too. This cut the pty suite from 82s of
  timeouts to 7s green. Negative-controlled: stubbing the browser out fails all
  four cases.

## A 140-character help string corrupted the interactive prompts (2026-08-08)

- **Symptom:** picking `Vault & capture` in the `bk` browser painted the same
  rows over and over down the screen. Only that group, and only that group,
  because `bk forget`'s one-line help is **140 characters** -- the rendered row
  came to **162**, which wraps onto a second physical line on any terminal
  narrower than that.
- **Cause:** `_redraw` moves the cursor up by the number of lines it *wrote*,
  while the terminal counts the rows it *occupied*. One wrapped row makes the
  two disagree forever after, so every subsequent redraw paints over the wrong
  lines. `console.table()` had always fitted its cells to the terminal;
  `_render_rows` never did. The invariant is now stated where it is relied on:
  `_redraw` is sound only while every line is one physical row and the whole
  block fits on screen, which `_render_rows` (truncation, shared
  `console.truncate`) and `_viewport` (windowing) are what guarantee.
- **`_viewport` closes the same bug from the other side.** A list taller than
  the screen cannot be redrawn by cursor arithmetic at all: once its top
  scrolls past row zero `\x1b[A` stops moving -- the cursor *clamps* -- and the
  up-count silently under-shoots. Latent here (9 rows max) but a general
  primitive should not corrupt on a short terminal.
- **A byte-stream assertion cannot catch this class, and mine did not.** The
  emitted bytes were perfectly balanced -- every label appearing an equal
  number of times -- while the painted screen was garbage. Tests now run the
  output through a ~50-line ANSI `Screen` emulator modelling the only two
  things that matter: the cursor clamping at row 0, and text wrapping. Assert
  on **row count** (9 commands must paint 9 rows), not on alignment: with the
  bug present, alignment happened to stay at column 4 and only duplication
  showed, so an alignment-only check passed while the screen was broken.
- **Test-harness lesson that cost real time twice over.** Reading a captured
  pty stream back with `open(path)` applies universal-newline translation and
  turns every `\r` into `\n`, so the emulator never sees a carriage return and
  reports a staircase that does not exist. Read captures with `newline=""`.
  The pytest cases were right the whole time because `_drive` hands the decoded
  bytes straight to `Screen`; only the ad-hoc inspection round-tripped a file.
- **Negative-controlled:** removing the truncation makes `capture` paint 4
  times where it should paint once, reproducing the reported symptom exactly.
  672 tests pass, ruff clean.

## The vendored extractors could name grammars nothing installed (2026-08-08)

- **The gap:** `codeanalysis/` imports **29** tree-sitter grammars and ships an
  extractor for each; `[code]` pinned **13**. The other 16 were unreachable
  code, and the failure is silent by construction -- `bk code build` prints
  "contributed nothing" per file (#1745) and then reports success, so a
  repository of SQL, Swift and Terraform produced a two-node graph. Reproduced
  with a four-file project: only `app.py` was extracted.
- **The hint was a dead end, not just terse.** Upstream pointed every such
  message at `pip install "graphifyy[<extra>]"`, and brainkit does **not depend
  on graphifyy at all** -- it vendors this code. Following the instruction
  installed an unrelated distribution while the grammar brainkit actually
  imports stayed missing. Installing `tree-sitter-sql` directly took the same
  build from 2 nodes/1 edge/1 file to 5/4/2.
- **Derive the hint from the error, do not tabulate it.** Upstream's
  `_EXTRA_FOR_EXTENSION` covered six extensions, so Swift -- the second largest
  gap -- got no hint at all. The message already names the module, and
  `tree_sitter_x` -> `tree-sitter-x` is the packaging rule, so deriving it gives
  all 29 a correct hint and leaves no table to fall out of step. The old table
  is now dead and removed.
- **Split rather than fold in:** `code` keeps the common 13, `code-all` adds the
  rest via `brainkit[code]` + 16 pins. Grammars are compiled wheels and `code`
  is already ~70 MB; tripling it by default would trade one silent problem for a
  loud one. All 16 exist on PyPI, so this is purely mechanical.
- **`tests/test_code_grammars.py` (8 tests)** asserts both directions -- every
  imported grammar is pinned by some extra, and no extra pins one nothing
  imports -- plus that no user-facing string advertises graphifyy and that
  graphifyy is absent from every dependency list. Negative-controlled: dropping
  one pin names the exact grammar. Beware the regex trap: `tree_sitter_version`
  matches inside `_check_tree_sitter_version`, so it is excluded explicitly.
- **Side effect to know about:** editing `pyproject.toml` made the local uv
  rewrite `uv.lock` from `revision = 1` to `revision = 3`. No package was
  dropped (71 -> 87, exactly the 16 grammars added), but it is a format
  migration and most of the 734-line deletion count is reformatting, not change.

## #1666's empties warning fired on what #1224 deliberately skipped (2026-08-08)

- **Not transient, and the advice looped.** The warning says "A re-run will
  retry them (empties are no longer cached)". Built the same 111-file tree three
  times: the same six files every time, identical counts. The #1666 fix (don't
  cache zero-node results, so a hiccup self-heals) is real, but it only helps an
  *anomalous* empty -- a deterministic one re-derives itself and warns forever.
- **Cause: three outcomes, two of them checked.** `extract_json` skips
  data-shaped JSON on purpose (#1224 removed it after datasets swamped the graph
  with orphan key-nodes) and *already said so* by returning a `skipped` marker.
  The empties loop tested only `nodes` and `error`, so a deliberate skip read as
  an anomaly. Honouring `skipped` silenced all six and left the graph
  byte-identical (1330 nodes / 3783 edges / 85 files) -- it was pure noise, not
  lost coverage.
- **Proof the skip is recognition, not failure:** the identical document with
  one extra `$schema` key goes from **0 nodes to 59**. Recognition is by
  filename (package.json, tsconfig.json…) or a top-level probe
  (dependencies/extends/$ref/$schema/compilerOptions); a JSON Schema document
  has none of them.
- **Second-order: a skip was never cached either**, since the cache write also
  keyed on `result.get("nodes")`. Every data-shaped JSON was re-parsed on every
  build forever. Caching it is safe *because* of how the cache is keyed --
  content hash (so editing the file re-extracts) inside a directory namespaced
  by extractor version (so changing the recognition rule invalidates it).
- **A passing negative control means the control is broken.** Reverting the
  cache fix left the test green: there are **two** cache-write sites, and
  `parallel=False` exercises the one in `_extract_sequential`, not the one in
  `_extract_single_file` that I had reverted. Each site now has its own test and
  its own control; the parallel one needs `_PARALLEL_THRESHOLD` (20) files
  before the parallel path is taken at all. Always confirm which branch the test
  actually runs before trusting a control.

## A vault sited one directory too high indexed 55,295 files (2026-08-08)

- **The incident, measured before anything was changed:** `bk init` run in
  `~/Projetos/tools` — a directory that *holds* three checkouts rather than
  being one — produced `graph/code.json` at **683 MB** (614,944 nodes,
  1,317,441 edges) over **55,295 files** from every unrelated repository on the
  machine, plus **1.6 GB** of AST cache. Nothing refused, nothing asked, and
  nothing on screen named the scanned directory until it was done.
- **Three defects had to line up, so there are three independent locks.**
  (1) `FileVault.code_root()` fell back to `self.root.parent` when no `.git`
  was found — the origin; and because that parent is not a VCS root, the
  extractor's own `.gitignore` ceiling collapses to the scan root, so nothing
  was excluded either. It now falls back to the *vault itself* and stops the
  upward walk before `~`. (2) `initialize` refuses `$HOME`, a nested vault, and
  (without `--force`) a directory holding ≥2 child repositories. (3)
  `code_scan_limit` (default 20,000) refuses the scan itself — the backstop
  that holds whatever a future layout does, because it measures the real scan
  instead of reasoning about where the vault sits.
- **`bk init` never asked where the vault goes.** It defaulted `path` to `.`
  and had no location prompt at all. It now asks, defaulting to
  `<repo>/.brainkit` — hidden and tool-owned, beside `.git` and `.claude`.
  Not `docs/`, which is the repository's own published documentation.
- **Changing the layout exposed the next bug immediately**, and the existing
  `_workspace_advisory` caught it: with the vault at `<repo>/.brainkit`, the
  agent hooks installed *into the vault* instead of the repository, so every
  file landed and nothing would ever load. Under the old `bk init .` layout the
  two directories coincided, so nothing had to choose. Only a real end-to-end
  run surfaced this — the unit tests were green throughout.
- **`graph/`, `views/` and `output/` were generated from day one and never
  gitignored.** `vault.py` already classified them as generated
  (`write_generated`'s allow-list) — the ignore file simply never agreed, so an
  in-repo vault committed its own derived output. The list is now derived from
  that allow-list, and a test asserts the two account for each other.
  Separately, `initialize` wrote `.gitignore` unconditionally: `bk init .` at a
  repository root **destroyed that repository's `.gitignore`**. It now splices a
  marked block.

## A build that dropped three of four languages reported success (2026-08-08)

- **The lie, reproduced:** a four-file repo (`.py/.sql/.swift/.tf`) built a
  two-node graph, exited **0**, and `bk code status` then answered
  `"state": "fresh"` — because every check asked "does the graph match the files
  it *recorded*" and none asked "did everything parseable get parsed".
  `staleness` now returns **`partial`**, and the coverage is written into the
  artefact so the answer survives the build output scrolling away.
- **Every install hint was unrunnable on this machine.** `bk` is a `uv tool`,
  and a uv tool environment ships **no `pip`** (`find_spec("pip") is None`), so
  `pip install "brainkit[code]"` either failed or installed into an unrelated
  interpreter and produced the identical message on the retry. New
  `infrastructure/pyenv.py` classifies the interpreter (uv tool / pipx / venv /
  system) and emits the command that works — `uv pip install --python
  <sys.executable> …`, verified end to end.
- **Layering told me twice where the code belonged, and it was right both
  times.** `application` may not import `infrastructure`, so the application
  layer now raises `details={"needs": [...]}` and the CLI resolves it to a
  command at the single point errors are rendered; and `bk doctor` — which is
  entirely about the interpreter and the installation — is assembled in
  `interfaces`, not in `Health`. Adding a `DOCUMENTED_EXCEPTIONS` entry would
  have been the easy answer and the wrong one.
- **Read grammars off the *function*, not its module.** `graphify.extract` is
  one 260 KB file holding many extractors, so module-level attribution reported
  that `.swift` needed `tree_sitter_python`. `co_names` plus the live
  `LanguageConfig.ts_module` is exact, covers both the direct-import and
  engine-driven shapes, and needs no edit inside the vendored tree. 83
  extensions map; the ~21 that resolve to nothing are the non-tree-sitter
  extractors (`.md`, `.sln`, `.csproj`), which is the correct answer for them.

## The vendored tree was not byte-identical, and the cache did not notice (2026-08-08)

- `NOTICE` claimed "nothing inside this directory" while three files had been
  edited. The operational half is the one that matters: `_cache_format_marker`
  namespaced the AST cache by **hashing `NOTICE` alone**, so an edit to
  `extract.py` that changed *which results are cached* left the namespace
  unchanged and entries written by the old extractor stayed live under the new
  one — the exact "stale cache, wrong graph" failure the marker exists to
  prevent. The marker now hashes the vendored source tree; `tests/
  test_vendoring.py` pins the declared-modification list and both controls fire.

## Benchmarks: coverage, not node count (2026-08-08)

- **The metric has to be coverage** (files yielding ≥1 node ÷ files with an
  extractor). A node count can rise while a whole language falls out, which is
  precisely what happened. Two tiers: a hermetic 26-language fixture that runs
  in pytest, and 10 real repositories pinned by commit.
- **Two harness traps, both of which read as a catastrophic regression in the
  tool rather than a bug in the harness.** (1) The vendored extractor's process
  pool *spawns* on macOS: children re-import `__main__` and inherit no
  `sys.modules`, so the synthetic `graphify` alias is unresolvable and every
  worker dies quietly — first fixture run measured **7.7%** coverage on files
  that all parse correctly in isolation. `bk` is unaffected because its console
  script imports the CLI. (2) Clones were cached in `benchmarks/.cache/`, and
  the extractor prunes `.cache` as a noise directory: **all ten repositories
  measured zero nodes**. A benchmark whose fixtures live somewhere the tool
  refuses to look measures nothing.
- **The metric counted correct behaviour as failure, and that pointed the
  investigation at the wrong place.** The first baseline read Alamofire at
  **82.4%** and I wrote it up as a Swift extraction problem worth chasing. All
  98 Swift files were covered; the entire shortfall was 23 fixture `.json`
  files that `extract_json` declines on purpose (#1224). `skipped` is a third
  outcome and coverage must exclude it -- the same three-way distinction the
  empties warning had to learn. Corrected, every repository is **100%**, and
  the skip count is reported separately so "not indexed" stays distinguishable
  from "failed to index".
- **`ruff --fix` reordered an import and silently broke `bk doctor`.**
  `_extension_grammars` registered the synthetic `graphify` alias and then
  imported through it; the sorter hoisted the `from graphify.extract import`
  above the registration, so in a *fresh* process the lookup raised, the
  `except` returned `{}`, and doctor reported **0 of 0 grammars known** while
  looking healthy. It only appeared correct earlier because the CLI had already
  imported the vendored package for another reason. `importlib.import_module`
  is a call, not an import statement, so nothing can reorder it.
- **Baseline (all 29 grammars installed):** every repository 100% coverage --
  1,766 files, 26,706 nodes, 54,745 edges, 21.7 MB of graph in 30.2 s
  (~58 files/s, ~811 bytes per node). Largest is commons-lang at 625 files /
  13.6 s. `peak_rss_mb` was dropped rather than reported: `ru_maxrss` is a
  process high-water mark, so every repository after the first measured 0.0.

## Both harness bugs, fixed at the root this time (2026-08-08)

- **Spawned workers now resolve the extractor.** The first pass raised
  `_PARALLEL_THRESHOLD` inside the test — a workaround that left every
  non-`bk` embedder silently losing files. The fix is a real, importable
  `graphify` package (three lines: `__path__` pointing at the vendored tree)
  written to a temp dir keyed by the vendored-source hash and **appended** to
  `sys.path`, which `spawn` hands to the child. `BK_NO_PARALLEL=1` forces the
  sequential path, and `extract(parallel=…)` — always in the vendored
  signature, never declared in our Protocol — is the seam it uses. Tests now
  assert the parallel and sequential paths cover identically, and a subprocess
  probe proves a fresh interpreter resolves `graphify.extract`.
- **An ignored scan root now says so.** `.cache` pruning produced "No code
  nodes in the imported graph", which reads as a broken extractor. `ScanSurvey`
  gained `present` (a plain walk honouring no ignore rules); collected == 0
  while present > 0 raises a refusal naming the mechanism and the likely
  directory. The corpus clones also had to leave `benchmarks/.cache/`, and the
  root `.gitignore` had to name them — **the extractor reads the ignore file at
  its scan root and never descends into nested ones**, so `benchmarks/.gitignore`
  did nothing and ~1,400 files of ripgrep and commons-lang took every top hub
  slot in brainkit's own graph.
- **The general fix, and its own false positive.** `unexplained_files` reports
  any file with an installed grammar that produced nothing for no stated
  reason — the silent fourth category both bugs fell into. It immediately
  accused six JSON Schema documents that `extract_json` declines on purpose:
  the same skipped-is-not-failed confusion already fixed in the benchmark's
  coverage metric, repeated one layer down. `ScanSurvey.skipped` now probes
  only extensions whose extractor *has* a skip path, detected by looking for
  the marker in its source rather than by keeping a list.

## LOCOMO, and what it does and does not say (2026-08-08)

- **recall@10 = 0.537** over n=300 (seed 7, adversarial category 5 excluded),
  5,882 turns indexed in 34 s, 4.65 ms median query, **0 LLM calls**. Per
  category: single-hop 0.67, temporal 0.58, multi-hop 0.25, open-domain 0.20.
- **It is not a re-run of Graphify's table.** Those figures are for
  conversational *memory* systems; brainkit vendors Graphify's code-extraction
  closure only, never its memory or retrieval stack. This measures brainkit's
  own `capture → FTS5 → search` on the same task. The protocol also differs in
  a way that flatters it: one turn per document makes retrieval units align
  exactly with `evidence` ids. Report the protocol with the number, always.
- **recall@k is the metric to publish here** because LOCOMO ships gold evidence
  ids, so it needs no judge — no confound from which model answered. QA
  accuracy was deliberately not run: the local models available (qwen2.5:3b,
  a 14B) would measure the model far more than the retrieval.

## `bk code path` printed the arrow backwards (2026-08-08)

- The traversal is undirected on purpose — "how are these related" is not "what
  calls what", and insisting on direction reports no path between symbols that
  plainly connect. But the *renderer* printed `--via-->` for every hop
  regardless, so a chain of callers read exactly like a chain of calls. In the
  first path tested, hop 1 was a reverse edge. Direction now rides along in the
  payload (`forward`) and renders as `◂──`.
- The payload always carried `path` and `line`; the old one-line renderer threw
  both away, which defeats the command's whole promise of not having to go
  looking. Now a descending tree with right-aligned `file:line`, dropped when
  the terminal is too narrow.

## CLI dead ends: 20 of 46 commands could be reached and not run (2026-08-08)

- **Measured, not guessed.** Walking the parser: 46 invocable commands, **20**
  cannot run without an argument (`search`, `ask`, `code path`, `export`,
  `file`, `hooks install`, …), 7 have an optional positional, 19 run bare.
  Every one of the 20 was a place to arrive and be stuck.
- **The browser was the worst of them.** `bk` → group → command printed
  `--help` and exited, so having navigated to what you wanted, the tool's last
  act was to make you retype it — including the argument you came here not
  knowing. It now offers **Run it now / Show me the command / Back**, prompting
  for what is missing and printing the composed line before running anything.
  The old safety property is kept and narrowed rather than dropped: selecting a
  command still never runs it, running is a separate defaulted-to-no choice,
  and `forget`/`reject`/`apply` are printed only.
- **argparse cannot see every requirement.** `bk capture` has an *optional*
  positional plus `--text`, so the parser reports nothing required and the
  command refuses at runtime with "capture requires a source path, URL, or
  --text" — a requirement that existed only inside an error string, which is
  why neither entry point could ask for it. `_ALTERNATIVES` declares the
  "one of these" cases (`capture`, `ingest`); everything else is read off the
  parser so it cannot drift.
- **Bare commands now ask instead of printing usage** — but only at a tty,
  never under `--json`, and never when anything beyond the command name was
  already typed. A half-typed command is a mistake to report, not a form to
  fill in, and a script missing an argument must fail rather than block on a
  question nobody will answer.
- **Found while auditing:** `bk code build ./typo` extracted nothing, merged
  that nothing into the stored graph, and printed the *existing* counts under a
  success line — a mistyped path reported as a completed build. Scoping to a
  path that does not exist is now `NotFoundError`.
- **Correction to an earlier note in this file:** the "exits 0 on error" claim
  was wrong — it came from reading `$?` through a pipe. Every error path exits
  2. Check the exit code of the command, not of `head`.

## Both systems through one harness: what a single recall column hides (2026-08-08)

- **Result** (full LOCOMO population, n=1,536, k=10, same questions, same
  retrieval unit, same scorer):

      system     recall@10   ceiling   ranking eff.   index    query
      brainkit       0.519     1.000          0.519   0.6 min    4 ms
      graphify       0.302     0.575          0.525  58.4 min  132 ms

- **The n=383 result did not survive.** At three conversations graphify's
  ranking efficiency led 0.594 to 0.542 and I reported "graphify ranks better".
  Over the full population the margin is **+0.006** (0.525 vs 0.519) -- nothing.
  It was flagged as at-risk when reported, because 0.05 sat inside the band a
  383-question sample produces; the flag was right and the finding was noise.
  A margin smaller than the sampling band is not a result no matter how
  mechanistically satisfying its story is.

- **The metric that mattered was not recall.** graphify condenses 1,207 turns
  into entities and keeps 15–21% of them, so **132 of 383 questions have no
  evidence turn in its graph at all** — decided before ranking begins. Adding
  `ceiling` (best recall a perfect ranker could reach over what a system
  retained) and `ranking efficiency` (recall ÷ ceiling) turns "graphify
  retrieves worse" into "graphify retains less and ranks better", which are
  different claims warranting different responses.
- **The condensation is selective, not lossy-at-random.** 15–21% of turns
  survive, yet the ceiling is 0.59–0.64 — extraction preferentially keeps
  answer-bearing turns. Reporting raw coverage alone would have understated it
  by 3×.
- **A weak model does not measure the system.** Piloted with `qwen2.5:3b`,
  graphify extracted **two distinct entities from thirty turns** (both speaker
  names), returned invalid JSON, and needed adaptive bisection. With
  `claude-cli`: 23 meaningful entities and the gold turn at rank 2. Any
  published number for an LLM-backed system is a number about that LLM; name it
  or the figure means nothing.
- **My cost estimate was 6× wrong, and I quoted it to the operator.**
  Extrapolating linearly from a 30-turn pilot predicted ~30 min per
  conversation; the real figure is ~4.5 min, because per-chunk prompt overhead
  amortises at real conversation size. I had flagged the *token* figure as an
  upper bound and should have flagged the *time* the same way. Scope decisions
  get made on these estimates — a pilot that is 1/14th of the real size is a
  bad basis for one.
- **Cached indexing reported zero cost.** `index()` returned `(0.0, 0)` when a
  graph already existed, so a re-run showed graphify's 15 minutes as `0.0m` in
  the very table built to compare cost. The cost is now written to disk beside
  the graph. A number that is only true the first time you run it is not a
  measurement.
- **Fairness checks worth keeping:** both systems returned ~9.8/10 candidates,
  so neither was starved at k=10, and quadrupling graphify's `--budget` changed
  nothing. Without that check, a low score could have been my truncation rather
  than its retrieval.
- **What this does NOT establish.** graphify scores 0.497 in its own published
  harness and 0.352 here, so this is not reproducing their setup. The per-turn
  document unit was chosen to make scoring exact against LOCOMO's turn-level
  evidence, and it suits a system that retrieves turns while handicapping one
  that retrieves entities. Same-harness is necessary for comparability, not
  sufficient for a verdict.

## The prompt took a pasted path literally, quotes and all (2026-08-08)

- **Reported from a real session.** Dragging a PDF into the terminal and
  pasting at `bk capture`'s new prompt produced
  `'/Users/…/Vant · … negócio.pdf'` — quotes included — which went straight
  through as a filename. The result was `Capture source is not a file`, a path
  resolved under the *cwd* because a leading `'` is not absolute, and an echoed
  command line reading `bk capture ''"'"'/Users/…'"'"''`. Every one of those
  reads as brainkit being broken; all three are one layer of shell quoting
  nobody removed.
- **A prompt is not `argv`.** Everything typed at a shell is unquoted by the
  shell before a program sees it; a value typed at a *prompt* has had nothing
  done to it. Adding interactive prompts silently moved that responsibility
  into the application, and none of the new code did it.
- **`shlex` already knows both conventions** — quoting *and* drag-and-drop's
  backslash escaping — so the rule is: if the value parses to exactly one
  token, that token is what was meant; several tokens is ordinary prose (a
  search query, a `--text` note) and is returned untouched; unbalanced quotes
  raise and are likewise left alone rather than guessed at. That single rule
  covers every shape without a special case per argument.
- **Validate at the prompt, not after dispatch.** `_Alternative` now carries an
  optional validator, and capture's file option checks the path exists (or is a
  URL) before the command is assembled — one re-ask instead of restarting the
  whole flow.
- **8 tests, negative-controlled** (removing the one-token rule fails exactly
  the quoted-paste and backslash cases). One asserts the *wiring*, not the
  logic: a validator defined and never attached is precisely this bug's shape.

## Enrichment: a model's own edge, admitted through a gate (2026-08-08)

- **The question was "can models enrich the graphs", and the answer had to
  distinguish two things.** Enriching *through* `bk apply` already works and
  needs nothing new: an agent proposes a cited page, the gate writes it, and
  `bk graph` derives the edges — the edge is a consequence of a cited claim.
  What did not exist is the relationship that *is* the claim ("these are the
  same entity", "this supersedes that"), which has no page to live on.
- **Three refusals, each inherited from an invariant rather than invented.**
  (1) Enrichment never enters `graph/graph.json`, because that file is a
  projection and `bk graph` would destroy it on the next build — it lives in
  `.brain/enrichment.json`, joined at read time. (2) An edge must name the
  sources it was derived from: the privacy filter decides by the branch a
  *source record* lives in, so an edge with nothing behind it is
  unclassifiable, and the filter runs after expansion precisely so an edge
  cannot pull a restricted node back into view. (3) Everything is marked
  `provenance: model`, with derived edges labelled `derived`.
- **Privacy inheritance reused an existing rule instead of writing a second
  one.** `_evidence_privacy` already computed "strictest across contributing
  branches" inline; extracting it as `strictest_privacy` and calling it from
  both is what keeps a privacy rule from drifting between two copies.
  Unresolvable provenance fails **closed** (`never-ingest`), and `bk lint`
  reports it as `enrichment.unresolved_source` so a restriction nobody chose is
  visible rather than silent.
- **Identity is the `(source, relation, target)` triple**, not a random id, so
  an agent that re-runs proposes one edge rather than inflating the graph.
- **20 tests, four negative controls**, one per load-bearing rule: accept
  edges with no provenance (1 fails), take the first source instead of the
  strictest (1 fails), skip the endpoint re-check on merge (1 fails), merge
  enrichment into the written projection (1 fails).

## `git checkout --` destroyed an hour of uncommitted work (2026-08-08)

- Reverting one experimental edit with `git checkout -- src/brainkit/domain/
  model.py` reset the file to **HEAD**, not to its pre-experiment state — and
  every change made to it this session was uncommitted. `EnrichmentEdge`,
  `ScanSurvey`, `GrammarNeed`, `DEFAULT_CODE_SCAN_LIMIT`, `_code_scan_limit`
  and the `code_scan_limit` field all vanished in one command.
- **Never use `git checkout --` to undo a scratch edit in a dirty tree.** The
  only safe undo is the one used for every other control in this session:
  `cp file /tmp/file.bak` before, `cp` back after. It restores the *working*
  state, which is what an experiment perturbs; git restores the *committed*
  state, which is a different thing entirely.
- Recovery was possible only because the test suite covers every lost symbol:
  rewriting them and watching 764 tests go green is what proved the file whole
  again. Coverage is a restore receipt, not only a regression net.
- Related, same session: a "negative control" that only added a comment passed
  and proved nothing. **A control that does not fail has not run** — re-do it
  until it fails, or drop the claim.
