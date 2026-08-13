# Command reference

Global options come before the subcommand: `--vault PATH` selects the vault
(otherwise it is discovered upward from the working directory) and `--json`
switches to machine-readable output.

## Mechanical — never calls a model

| Command | Purpose |
|---|---|
| `init [PATH] [--config FILE\|-]` | Initialize a policy-complete vault |
| `capture [SOURCE] [--text T] [--title T]` | Capture a file, URL or literal text |
| `status` | Vault health and counts |
| `doctor` | Health, plus a live probe of the installed write gate |
| `reconcile` | Heal registry paths after manual moves; drop orphaned freshness |
| `reindex` | Rebuild the disposable FTS5 index |
| `file` | Move a raw source to a branch |
| `forget ITEM [--force]` | Drop one source record whose raw file is gone from the registry |
| `lint [--changed]` | Validate registry and wiki contracts |
| `search QUERY [--limit N] [--consumer C]` | FTS5 BM25 search |
| `context QUERY [--limit N] [--max-chars N] [--consumer C]` | Bounded evidence bundle |
| `apply PROPOSAL\|-` | Validate and atomically commit wiki writes |
| `gate check-write PATH [--agent A]` | Whether a direct write to `PATH` is permitted. A relative `PATH` resolves against the current directory, like every other path you type on the command line — unlike a path stored in `.brain/config.json`, which resolves against the vault |
| `views` · `graph [--html]` | Regenerate views and the knowledge graph |
| `enrich apply PROPOSAL\|-` · `enrich list [--consumer C]` · `enrich forget ID` | Model-inferred edges, gated and stored apart from the projection |
| `code build [PATH …]` · `code import` · `code status` | Extract the repository graph, import one, re-check it — `build` needs `[code]`. Given `PATH`s, merges that subset into the stored graph instead of replacing it |
| `code affected` · `code path` · `code hubs` | Queries over that graph, on the base install |
| `code communities` · `code cycles` · `code diff` | Delegated to the vendored analysis — needs `[code]` |
| `export --target T [--consumer C]` | Export to `json`, `graphml`, `cypher`, `obsidian`, `neo4j`, `postgres`, `kuzu`, `llms-txt` |
| `proposals [--status S]` · `approve ID` · `reject ID --reason R` | Filing review queue |
| `integration configure\|status\|up\|down\|sync NAME` | Persistent integration lifecycle |
| `vaults register\|list\|forget\|sync` | The vaults on this machine, synced into one shared store |
| `web serve [--host H] [--port P] [--consumer C] [--token-env V]` | Foreground web viewer |
| `serve --mcp [--transport stdio\|http] …` | MCP transports |
| `watch [--once] [--interval S]` | Capture new files under the configured source folders — resolved against the vault — minus `ignore` |
| `schedule` | Show configured habit job registrations |
| `hooks install --agent claude\|codex\|gemini\|opencode [--force]` | Install the agent contract |

## Judgment — routed through job specs and output schemas

| Command | Purpose |
|---|---|
| `ingest [ITEM] [--all]` | Propose a branch, then a schema-valid apply proposal |
| `ask QUERY` | Answer from compiled vault evidence |
| `digest` | Generate the configured digest |
| `resurface` | Surface one durable insight |
| `lint --semantic` | Add the `lint-semantic` judgment pass to the structural report |

## What a failure tells you to do

Every refusal carries a stable machine code in `--json` (`error.code`) and over
MCP, alongside the human message. The code exists so a caller does not have to
read English to decide what to do next, because the right next move differs:

| `error.code` | Exit | What it means | What to do |
|---|---|---|---|
| `validation_error` | 2 | The request is malformed — or the policy it was read from is, such as a `sources` entry that is not a string. A provider answering `400` lands here too: it judged the payload we sent | Change the request, or fix the value the message names |
| `conflict` | 2 | The request was fine; the vault moved under it | Re-read state, rebuild the same request, retry |
| `not_configured` | 2 | This installation cannot serve it — no model mapped, optional dependency absent, variable unset, integration off, a configured path that resolves to nothing, or a provider rejecting the key (`401`), refusing that key's access (`403`), not recognising the model id (`404`), rate-limiting past the retries (`429`) or failing to answer (`5xx`) | Stop retrying; report what the operator must configure |
| `refused` | 2 | Well-formed, but a guard forbids it — vault nesting, a scan past its bound, a process this vault does not own | Change the circumstance, or take the escape hatch the message names |
| `model_response_invalid` | 2 | A *provider's* output failed validation or the repair loop ran out of attempts | Retry, raise the token ceiling, or route the job elsewhere |
| `not_found` | 2 | The named thing does not exist | — |
| `policy_denied` | 3 | The privacy boundary refused it | Ask with a consumer that is allowed to see it |

The four middle codes are narrowings of `validation_error`, not replacements:
each is a `ValidationError` subclass, so anything catching that still catches
them and no exit status changed. `conflict` is claimed only when retrying could
actually succeed — an apply rejected for a stale page *and* an uncited claim is
a `validation_error`, because re-reading fixes the version and never the claim.

Because they are subclasses, a command that catches another's failure to attach
context — which container, which branch — re-raises the class it caught rather
than the base. Adding context must not change the remedy: naming
`ValidationError` there would re-raise a `not_configured` as
`validation_error`, so the same stopped Docker daemon would say *tell the
operator* when reached directly and *fix your request* through
`bk integration up`.

---
<!-- doc-tracking -->
- Created: 2026-08-13 15:41
