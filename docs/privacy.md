# The privacy boundary

Machine callers must declare their privacy boundary:

```bash
bk --vault ./my-vault context "topic" --consumer cloud --json
bk --vault ./my-vault search "topic" --consumer local --json
```

`cloud` receives only cloud-eligible evidence and `local` excludes
`never-ingest`. `human` applies no restriction at all: it is the default for
interactive, non-JSON use, and a machine caller that names it explicitly —
through `--json`, MCP, or a `--consumer human` integration such as the local
web viewer — receives `never-ingest` bodies. Declaring the boundary is
mandatory for machine callers precisely because the unrestricted value has to
be a deliberate choice rather than a silent default. Privacy filtering also
applies to graph-expanded search neighbors.

## An MCP server declares its consumer

MCP hands its answers to a model, and the server cannot see where that model
runs. So the operator declares it when starting the server:

```bash
bk --vault ./my-vault serve --mcp --transport stdio                    # cloud
bk --vault ./my-vault serve --mcp --transport stdio --consumer local   # an agent on this machine
```

`cloud` is the default and `human` is refused. The declared consumer is a
ceiling on every tool and resource: `status` is the filtered `/api/status`
report (no vault path at `cloud`), `proposals`, `lint` and resources drop what
the ceiling may not see, and `ask`, `resurface` and `lint --semantic` read no
wider than it even when a local model is mapped. `search` and `context` still
require a `consumer` argument, and it may only narrow the server's; a wider one
is refused with `policy_denied` rather than silently answered narrower. `file`,
`approve` and `reject` cannot reach a source or proposal outside the ceiling —
moving a source is how its privacy changes. See
[ADR 0010](./knowledge/decisions/architecture/0010-mcp-declares-its-consumer.md).

Over MCP, `capture` takes text and `http(s)` URLs as always, but a file path
only inside the vault's project (its code root, or a workspace `bk hooks
install --root` recorded), outside the vault itself, and never a credential
file: `.env*` (templates such as `.env.example` excepted), `*.pem`, `*.key`,
SSH private keys, `.netrc`, `.npmrc`, `.pypirc`, anything under `~/.ssh`,
`~/.aws`, `~/.config/gcloud` or a `.git` directory. `bk capture <path>` is the
operator's own command and is not restricted.

## Filtering runs after expansion, not before

Filtering the direct hits first would let an outgoing link or a backlink pull a
restricted node back into view through its neighbour. So the filter runs on the
finished graph, once every node and edge exists.

## A filename is disclosure

Filtering covers node bodies and node metadata alike. A filename and its branch
are themselves disclosure, so a redacted source contributes neither. For the
same reason `search` and `context` report `redacted` as a count and never
describe what was withheld: the bundle `context` returns is the payload handed
to a cloud model, and naming a withheld source there would defeat the boundary
that dropped it.

## Judgment inherits the strictest policy

When evidence spans branches, the judgment router applies the strictest policy
in the set: `never-ingest` denies the call, `local-only` requires Ollama, and
cloud routing is allowed only when every contributing branch permits it.
Enrichment edges follow the same rule through the same function — see
[Enrichment](./enrichment.md).

`ask`, `resurface` and `digest` never hand the router evidence it would refuse
in the first place. Each reads under the `local` boundary, after expansion,
asks the router where that evidence would be routed, and then reads under the
boundary of that route: `local` when it is a model on this machine, `cloud`
otherwise (including a provider that cannot say). So a question that merely
brushes a `never-ingest` source is answered from the rest, and on a job mapped
to a cloud provider a `local-only` match is withheld rather than refusing the
whole call. A privacy-keyed mapping (`job_models.<job>.local-only` → Ollama)
still sends `local-only` evidence to the local model. The result carries
`withheld_sources` — a count, never a name. When every match was withheld, the
job refuses with `policy_denied` and a hint instead of calling a model; the
router's own refusal remains as the last line of defence. When nothing matched
at all, there is no branch to route by, and the router falls back to the
`_inbox` policy. If that route is refused (a cloud-mapped job on a vault whose
inbox is `local-only`), `ask`, `resurface`, `digest` and `lint --semantic` say
so: nothing in the vault matched, or nothing a model may read remained, and
nothing was sent to any model. The refusal is still `policy_denied`, and its
hint suggests rephrasing, `bk search`, or mapping `job_models.<job>.local-only`
to a local provider. For `digest` the
same boundary also governs the metadata it sends: branch names, freshness
entries for pages compiled from withheld or unresolvable sources, and filing
proposals for such sources are dropped and counted in `withheld_sources`; on a
cloud route, installation facts (the vault path, hook script paths) are
withheld as well.

## Every egress carries it

Exports and persistent integrations are governed by the same boundary, and file
targets default to `local` so an export never emits `never-ingest` evidence
unless `human` is named deliberately. See
[Egress carries the boundary](./integrations.md#egress-carries-the-boundary).
