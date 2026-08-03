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
  stored credential clones and fetches, yet `/api/v4/projects/...` answers
  `insufficient_scope` under `PRIVATE-TOKEN`, `Bearer` and `JOB-TOKEN`.
  Publishing to the GitLab PyPI registry therefore needs a separate token with
  `write_package_registry`, plus the numeric project id the API would return —
  a working `git clone` proves nothing about the package registry.

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
