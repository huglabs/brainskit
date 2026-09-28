---
name: brainskit
description: Read and write a brainskit vault — a local-first second brain where markdown is the source of truth and only `bk apply` may write the wiki. Use when asked to research, summarize, file, or answer from the vault at {{vault}}, or whenever a task would otherwise mean editing files under wiki/ or raw/.
---

# brainskit

Read `CLAUDE.md` in this project before your first `bk` call. It states the
full vault contract this skill assumes — how the graph is formed, the privacy
model, and, when the vault sits in a subdirectory rather than at this
project's root, where the two paths diverge.

The vault at `{{vault}}` is a compiled knowledge base. `raw/` holds immutable
evidence, `wiki/` holds pages compiled from that evidence, and every page must
cite the source hashes it was built from. SQLite FTS5 is a disposable index.

## The only write path

You may never create or edit a file under `wiki/` or `raw/` directly. This is
not advice you are being asked to follow: `bk hooks install` registers a
PreToolUse hook that refuses the write while you are attempting it, and returns
the command to use instead. Ask it yourself before writing anywhere unusual:

```bash
bk --vault {{vault}} gate check-write PATH --json   # exit 0 allowed, 2 denied
```

A write that slips past the hook is still caught afterwards: `bk lint` compares
each page against the hash the apply gate recorded, and the vault reports
`wiki.outside_apply` until it is reverted.

```bash
bk --vault {{vault}} context "QUERY" --consumer local --json   # 1. get evidence
bk --vault {{vault}} apply proposal.json --json                # 2. write pages
```

`context` returns the evidence bundle and the exact proposal contract. Build the
proposal from that bundle — never from memory, and never from a source hash you
did not receive.

## Declare a privacy boundary on every read

`--consumer` is mandatory for machine callers. Pick the narrowest one that still
answers the question:

| consumer | receives |
| --- | --- |
| `cloud` | only cloud-eligible branches |
| `local` | everything except `never-ingest` |
| `human` | no restriction, including `never-ingest` |

Choose `human` only when the operator asked for it. A filename and its branch
are disclosure on their own, so a redacted source contributes nothing at all —
not its body, not its name.

## Writing a proposal

```json
{
  "proposal_id": "id-for-this-exact-payload",
  "operations": [
    {
      "action": "upsert",
      "kind": "concept",
      "slug": "lowercase-kebab-case",
      "title": "Title",
      "aliases": [],
      "source_hashes": ["<64-char sha256 from context>"],
      "body": "Claim backed by evidence.[^source:<same sha256>]",
      "links": ["other-page-slug"],
      "base_hash": null
    }
  ]
}
```

- Every claim needs a `[^source:<hash>]` citation, and every cited hash must
  appear in `source_hashes`. The gate rejects a mismatch in either direction.
- Updating an existing page requires `base_hash` — the current page hash from
  `context`. A stale value is rejected rather than overwritten.
- Each `links` target must already exist or be created in the same batch.
- Validation covers the whole batch: one bad operation writes nothing.
- `proposal_id` binds to the bytes it applied, not to the turn that sent it.
  Re-sending that exact payload under it is a no-op — which is what makes a
  retry safe after a timeout. Sending a changed payload under it is rejected
  as `validation_error` and re-reading the vault never clears it: a body you
  repaired is a new payload, so give it a new id, or omit `proposal_id` and
  one is derived from the payload.

## Commands

| Goal | Command |
| --- | --- |
| Add evidence | `bk --vault {{vault}} capture FILE --json` |
| Search | `bk --vault {{vault}} search "Q" --consumer local --json` |
| Answer from the vault | `bk --vault {{vault}} ask "Q" --json` |
| Check integrity | `bk --vault {{vault}} lint --json` |
| Heal state after manual moves/deletes | `bk --vault {{vault}} reconcile --json` |
| Drop one source record | `bk --vault {{vault}} forget ITEM --json` |
| Vault health | `bk --vault {{vault}} status --json` |

`forget` needs `--force` when the raw file is still on disk. It drops one
source from this vault's own registry — not `bk vaults forget`, which
unregisters a whole vault from this machine and never touches its files.

## The code graph

A question about this repository's own code — not the vault's evidence — is
`bk code`, answered from structure rather than a grep:

| Goal | Command |
| --- | --- |
| Extract or refresh the graph | `bk --vault {{vault}} code build [PATH …] --json` |
| Is it still accurate? | `bk --vault {{vault}} code status --json` |
| What breaks if this changes | `bk --vault {{vault}} code affected SYMBOL --json` |
| Shortest chain between two symbols | `bk --vault {{vault}} code path FROM TO --json` |
| Most connected symbols | `bk --vault {{vault}} code hubs --json` |
| Structural clusters | `bk --vault {{vault}} code communities --json` |
| Import cycles among files | `bk --vault {{vault}} code cycles --json` |
| What changed structurally | `bk --vault {{vault}} code diff --json` |

Given `PATH`s, `build` merges that subset into the stored graph instead of
replacing it. `build`, `communities`, `cycles` and `diff` need the `code`
extra installed (`pip install brainskit[code]`); `import`, `status`,
`affected`, `path` and `hubs` read the stored graph and need nothing extra. A
command that needs the extra and lacks it fails with the install hint, never a
stack trace. Every code-graph read defaults to `--consumer local` — it carries
repository paths and is not meant to leave the machine.

Read `CLAUDE.md` for how the vault's own graph is formed and where it can be
exported.
