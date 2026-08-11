# Getting started

Install `bk` first — see [Install](../README.md#install) in the project README.

## Create a vault

Run `bk init ./my-vault`. It probes the machine before it asks anything —
whether this is a git repository, what language `$LANG` implies, whether ollama
is running and which models are actually pulled — and then asks the three
things it cannot work out for itself:

1. **What the vault is for.** A preset (Work / Personal / Research) names the
   branches and their privacy, or `Custom` lets you name your own. Every preset
   keeps a `never-ingest` branch, because that is the one policy choice you
   cannot walk back: what has been sent to a provider has been sent.
2. **Which model runs the six jobs.** Chosen from the models ollama reports,
   never from a hardcoded name — a vault configured for a model you do not have
   is a vault whose every judgment fails at first use.
3. **Anything else** — Obsidian sync, the local web UI, and wiring up a coding
   agent, which is on by default and writes `.claude/` plus a managed
   `CLAUDE.md` block for you.

Answers are validated at the prompt that produced them and shown as a summary
you can walk back into before anything is written. If ollama is down or has no
models, `init` says so and still produces a valid vault — the jobs simply stay
idle until a provider is up.

Arrow keys drive the selections; off a terminal every prompt degrades to
numbered input, so a here-doc can answer it. For automation, skip the questions
entirely and pass a complete config file:

```bash
bk init ./my-vault --config policy.json --json
bk --vault ./my-vault capture notes.md --json
bk --vault ./my-vault reindex --json
bk --vault ./my-vault search "retrieval memory" --consumer local --json
```

No `.env` file is loaded. Provider secrets are read only from the environment
variable explicitly named in the vault configuration.

## Writing to the wiki

An `apply` proposal is a JSON document:

```json
{
  "operations": [
    {
      "action": "upsert",
      "kind": "concept",
      "slug": "compiled-memory",
      "title": "Compiled memory",
      "aliases": ["memória compilada"],
      "source_hashes": ["<64-char sha256>"],
      "body": "Evidence-backed text.[^source:<64-char sha256>]",
      "links": [],
      "base_hash": null
    }
  ]
}
```

`bk context QUERY --consumer local --json` provides the evidence bundle an
agent needs to create that proposal. `bk apply proposal.json --json` validates
the complete batch before any wiki page is replaced. For updates, `base_hash`
must match the page version returned by `context`; retries with the same
`proposal_id` and payload are idempotent, while key reuse with another payload
is rejected. An interrupted multi-page commit is rolled back when the vault is
opened again.

Filing uses the same unit of work: wiki pages, page freshness, registry/source
status, the raw-file move and the SQLite index either become visible together or
are restored from the transaction journal. The index update is incremental, so a
normal apply does not pay for a full rebuild.

## The page schema

`.brain/schema.json` is validated as the JSON Schema draft declared by its
`$schema` URI. The gate supports the complete vocabulary implemented by
`jsonschema` for that draft, including combinators, conditionals, formats,
`$defs`, local `$ref`, `dependentRequired` and `unevaluatedProperties`, before
applying brainkit's provenance, citation, link and reserved-field invariants.

Remote `$ref` retrieval is deliberately denied: a vault schema cannot cause an
implicit network request or leak local policy data. Bundle referenced schemas
under local `$defs` instead.

## Vault layout

`bk init` scaffolds every directory the engine files into, so a page kind can
never land somewhere the vault does not own:

```text
my-vault/
├── raw/                     immutable evidence, identified by SHA-256
│   ├── _inbox/              landing zone before a filing decision
│   ├── _assets/
│   └── <branch>/            one directory per configured branch
├── wiki/                    the compiled surface; written only by the gate
│   ├── sources/  entities/  concepts/  syntheses/
│   ├── index.md             system pages, maintained by the engine
│   └── log.md
├── views/map/  views/domains/   generated navigation
├── graph/                   generated graph.json
├── output/                  digests/, reports/, answers/
└── .brain/                  policy and durable state
    ├── config.json          branches, providers, ignore, integrations (no secrets)
    ├── schema.json          human-owned page schema, enforced by apply/lint
    ├── registry.json        source hash → path and status
    ├── freshness.json       applied page hashes and lifecycle state
    ├── proposals.json       pending filing proposals
    ├── applied.json         idempotency keys for executed applies
    ├── integration-state.json   PIDs, containers, sync checkpoints
    └── index.db             disposable FTS5 index (git-ignored)
```

`.brain/schema.json` is yours to edit. Everything else under `.brain/` is engine
state: change it by running a command, not with a text editor.

---

Next: the [command reference](./commands.md), or
[the privacy boundary](./privacy.md) before you point anything at a cloud model.
