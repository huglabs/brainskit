# Filing and review

`bk ingest` first proposes a destination branch and then produces a
schema-valid apply proposal. The configured destination policy controls the
outcome:

- `auto+digest-review`: file and apply immediately, retaining the audit record;
- `approve-each`: store the proposal without moving or writing anything.

```bash
bk --vault ./my-vault ingest --all --json
bk --vault ./my-vault proposals --status pending --json
bk --vault ./my-vault approve <proposal-id> --json
bk --vault ./my-vault reject <proposal-id> --reason "not useful" --json
```

Judgment jobs are validated against `jobs/_output-schemas/`. Invalid model
output is retried with structured validation feedback; no hardcoded answer is
substituted.

## Freshness and integrity

Applied pages are tracked in `.brain/freshness.json`. A new related capture
marks affected pages for review, the configured age threshold marks pages
stale, and `bk resurface` selects a durable insight through the configured
provider. `bk lint` reports raw-source mutation, direct wiki edits outside the
apply gate, unresolved provenance, broken links, and stale pages.

Freshness is keyed by path, so a page deleted outside the gate leaves an entry
that can never be revived. `bk lint` reports it as `freshness.orphaned`,
`bk status` stops counting it, and `bk reconcile` removes it — the same command
that re-links a moved source by its hash.
