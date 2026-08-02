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
