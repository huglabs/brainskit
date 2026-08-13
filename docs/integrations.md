# Persistent integrations

Every integration is opt-in and stored in `.brain/config.json`; lifecycle and
sync checkpoints are stored in `.brain/integration-state.json`. Secrets are
never persisted. Configuration stores only the name of an environment
variable. `bk integration status` combines the durable policy with live
process/container state.

All capabilities are available through JSON CLI and the MCP tools
`integration_configure`, `integration_status`, `integration_up`,
`integration_down` and `integration_sync`.

## Obsidian

Obsidian sync is manifest-based. It copies the generated `wiki/`, `views/` and
`graph/graph.json` into the selected vault and removes only files that brainskit
previously managed. Human-owned Obsidian content is never deleted. Raw evidence
is excluded unless `--include-raw` is explicitly selected.

```bash
bk --vault ./my-vault integration configure obsidian \
  --enable --external --path "$HOME/Obsidian" --subdirectory brainskit
bk --vault ./my-vault export --target obsidian
bk --vault ./my-vault integration status obsidian
```

Point `--path` at the brainskit vault itself for in-place Obsidian use. In that
mode brainskit creates only the minimal `.obsidian/app.json` when absent and does
not duplicate the knowledge files.

**A relative `--path` is resolved against the vault, never against the directory
you run from.** This is the rule `sources` and `code_root` already follow: where
a vault mirrors itself to is a property of the vault, so it answers the same
whether you type the command in the vault, in a sibling checkout, or from the
`$HOME` that cron hands a scheduled job. Absolute paths — `~/Obsidian/MyVault`
and the like — are the normal case and are used exactly as written.

This matters more here than for a path that is only read: sync creates the
destination. A relative path resolved against the current directory did not
merely look in the wrong place, it built a whole Obsidian vault,
`.obsidian/app.json` and all, wherever the process happened to start.

If a relative destination was already synced somewhere else, sync refuses rather
than quietly repointing — orphaning a real Obsidian vault would be worse than
the ambiguity it replaces. The refusal names the existing mirror, and the remedy
is to write an absolute path: the directory holding that mirror to keep it, or
the new location to adopt it and remove the old one yourself.

## Neo4j

Neo4j uses the official Python driver and writes `BrainskitNode` nodes plus
`SOURCED_FROM` and `LINKS_TO` relationships in one database transaction. This
is a real Bolt push, not a Cypher-file export. Every node is namespaced with a
stable vault ID, so a refresh replaces only that vault's subgraph and repeated
syncs are idempotent. It can connect to an operator-owned service (`--external`)
or create a Docker service (`--managed`) whose data survives stop/start under
`.brain/services/neo4j/data`.

```bash
export BRAINKIT_NEO4J_PASSWORD='use-a-secret-manager-in-production'
bk --vault ./my-vault integration configure neo4j \
  --enable --managed --uri bolt://127.0.0.1:7687 --user neo4j \
  --password-env BRAINKIT_NEO4J_PASSWORD --database neo4j --consumer local
bk --vault ./my-vault integration up neo4j
bk --vault ./my-vault integration sync neo4j
bk --vault ./my-vault integration down neo4j
```

## PostgreSQL graph

The PostgreSQL target is native, portable PostgreSQL: a JSONB-enriched
`nodes`/`edges` graph, indexed adjacency columns, referential integrity and a
recursive `graph_walk(start_node, max_depth)` SQL function. It does not require
a graph extension. Managed mode runs PostgreSQL in Docker with durable data at
`.brain/services/postgres/data`; external mode reads a DSN from the named
environment variable.

One schema holds as many vaults as you point at it. Every row carries the
`vault_id` of the vault that wrote it, and a sync deletes only that vault's
rows, so refreshing one vault never touches another's — including a vault owned
by a different application sharing the schema. Both tables index `vault_id`.

Because the schema is shared, the stored `id` is namespaced the same way the
Neo4j export namespaces its own: `<vault_id>:<natural id>`. The natural id is
not lost — `properties` holds the untouched node, so `properties->>'id'` is the
unprefixed id (`page:wiki/index.md`, `raw:<content hash>`), and on `edges`,
`properties->>'source'` and `properties->>'target'` likewise. Filter by column
and read the natural key from JSONB:

```sql
SELECT properties->>'id' AS id, label, path
FROM brainskit.nodes WHERE vault_id = $1 AND kind = 'wiki';
```

`graph_walk` needs no vault argument and takes none: since every id carries its
vault's prefix, a walk cannot leave the vault it started in. Pass it the stored
prefixed id, not the natural one.

Upgrading an existing deployment needs no manual migration. The sync adds the
`vault_id` column if the tables predate it, adopts the rows already there into
the vault performing that first sync — safe because the previous behaviour
truncated both tables on every refresh, so what is on disk is exactly one
vault's last complete sync — and replaces them on the same run.

```bash
export BRAINKIT_POSTGRES_PASSWORD='use-a-secret-manager-in-production'
bk --vault ./my-vault integration configure postgres \
  --enable --managed --password-env BRAINKIT_POSTGRES_PASSWORD \
  --user brainskit --database brainskit --schema brainskit --port 5432 \
  --consumer local
bk --vault ./my-vault integration up postgres
bk --vault ./my-vault export --target postgres
bk --vault ./my-vault integration down postgres
```

For an existing service:

```bash
export BRAINKIT_POSTGRES_DSN='postgresql://user:password@host/database'
bk --vault ./my-vault integration configure postgres \
  --enable --external --dsn-env BRAINKIT_POSTGRES_DSN \
  --schema brainskit --consumer cloud
bk --vault ./my-vault integration sync postgres
```

`bk integration up` waits for the service to accept connections the same way a
client will, and requires it to stay up before reporting `ready` — PostgreSQL
answers on its unix socket from the temporary server it runs during `initdb`,
which then shuts down and restarts. A first boot also has to chown the
vault-local data directory, which is slow on macOS bind mounts, so the deadline
is 300 seconds and can be raised per integration with
`ready_timeout_seconds` in its stored options.

### When `up` fails

A managed integration that cannot start reports `not_configured`, not
`validation_error`: Docker being absent, its daemon being stopped, or the
container refusing to start are all facts about the machine, and no change to
the command clears any of them. The refusal carries the daemon's own words in
`details.response`, alongside the container name and the ports the policy asked
for — `details.busy_ports` names any host port something else already answers
on, which is the usual cause when Docker itself is healthy.

```console
$ bk --vault ./my-vault integration up postgres --json
{"ok": false, "error": {"code": "not_configured",
  "message": "Managed database container could not be created",
  "details": {"integration": "postgres", "container": "brainskit-postgres-…",
              "busy_ports": [], "reason": "Docker command failed",
              "response": "Cannot connect to the Docker daemon …"}}}
```

The exit status is 2, the same as any other refusal; it is the `code` that says
whether to retry, rewrite, or go fix something.

## Many vaults, one store

`bk integration sync` syncs the one vault it was pointed at. When an operator
runs several applications, each with its own vault, `bk vaults` keeps the list
and syncs them as a set, so the shared graph is the union of all of them.

There is no discovery step, and that is deliberate: vaults live in unrelated
projects, and a filesystem scan would be slow, would miss anything outside the
trees it was pointed at, and would find checkouts, copies and backups that must
never be synced into a shared store under their own identity. The list is
declared once and lives at `$XDG_CONFIG_HOME/brainskit/vaults.json` (default
`~/.config/brainskit/vaults.json`), file `0600` inside a `0700` directory. It
holds paths and labels and nothing else — the same rule vault configuration
follows, where only the *name* of an environment variable is ever stored.

```bash
bk vaults register ./app-one --label app-one   # PATH defaults to the vault found from the cwd
bk vaults register ./app-two
bk vaults list                                 # label, path, whether it is still there, vault_id
bk vaults sync --target postgres               # --target defaults to postgres
bk vaults forget app-two                       # unregisters only; the vault's files are untouched
```

Each vault keeps its own policy. A vault that has not enabled the target is
**skipped**, not enabled on its behalf, and one vault failing does not stop the
rest — an unmounted disk or a service that is down is reported against that
vault alone:

```json
{"target": "postgres", "count": 4, "ok": 2, "skipped": 1, "failed": 1,
 "vaults": [{"label": "app-one", "vault_id": "2e2389340edfb82b1fe52ba9", "status": "ok", "result": {}},
            {"label": "app-optout", "status": "skipped", "reason": "postgres is not enabled in this vault's policy"},
            {"label": "app-gone", "status": "failed", "code": "not_found", "reason": "Not a brainskit vault"}]}
```

Exit is `1` when any vault failed and `0` when every vault succeeded or was
skipped, so a scheduled run can branch on the status alone. `list` reports a
vault whose directory has been deleted rather than failing, and still prints its
`vault_id` — which is what you need to find the rows it left behind in a shared
schema before running `bk vaults forget`.

`bk vaults sync` takes no `--consumer`, for the reason `export` refuses one on
an integration target, and one more: a sync refreshes by deleting the vault's
rows first, so a narrowed run would quietly replace what a shared store already
holds with less, across every registered vault at once. Set the boundary per
vault with `bk --vault <path> integration configure <target> --consumer`.

This group is CLI-only. Unlike the `integration_*` tools, it is not exposed over
MCP: an MCP server is started for one vault and answers under that vault's
declared boundary, so a tool that reached into unrelated vaults would widen the
boundary the caller was granted.

## Egress carries the boundary

`consumer` is mandatory for Neo4j and PostgreSQL and optional for Obsidian,
where it defaults to `local`. `cloud` exports only cloud-eligible evidence;
`local` also permits local-only evidence but always redacts `never-ingest`. The
filter runs after graph expansion, so edges cannot reintroduce restricted nodes.

Every egress carries the same boundary, including the file targets:

```bash
bk --vault ./my-vault export --target json                    # local (default)
bk --vault ./my-vault export --target cypher --consumer cloud
bk --vault ./my-vault export --target llms-txt --consumer human
```

`--consumer` defaults to `local`, so an export never emits `never-ingest`
evidence unless `human` is named deliberately. Passing it to `--target
obsidian`, `neo4j` or `postgres` is rejected rather than applied: those targets
carry their own configured consumer, and silently overriding it could widen the
boundary past what the integration was configured to permit.

---
<!-- doc-tracking -->
- Created: 2026-08-13 14:50
- Updated: 2026-08-13 15:18
