# Work — active feature & bug lifecycle

`docs/work/` holds the **temporary, per-feature working set**: everything we are
building *right now*. Each feature or bug gets one folder keyed to its ClickUp
task. When the work ships and is reviewed, the folder is archived to
`docs/archive/YYYY-QN/<slug>/`. Durable, cross-feature knowledge never lives
here — it is only linked (see below).

## The cycle

```
create work/<slug>  →  work through the numbered stages  →  review  →  archive
```

A feature moves through numbered stages, in order:

| Stage | File | Purpose |
|-------|------|---------|
| 00 | `00-user-story.md` (feature) or `00-bug-report.md` (bug) | Why we're doing this, from the user's POV |
| 01 | `01-prd.md` | Product requirements — scope, acceptance criteria |
| 02 | `02-spec.md` | Technical spec — how we'll build it |
| 03 | `03-diagrams/` | Excalidraw / diagram assets (templates provided) |
| 04 | `04-review.md` | Quality review before merge |
| 05 | `05-retro.md` | Retrospective — what we learned |

Not every stage is mandatory for every task; small bugs may only need 00 + 04.

## Slug convention

```
work/CU-<clickup-id>-<short-name>
```

- `CU-` prefix marks the folder as ClickUp-keyed.
- `<clickup-id>` is the raw ClickUp task id (e.g. `abc123`).
- `<short-name>` is a kebab-case summary (e.g. `add-inventory`).

Example: `work/CU-abc123-add-inventory`.

Bugs use the same slug shape but start from `00-bug-report.md` instead of
`00-user-story.md`.

## `_meta.yaml` — the only place state lives

Every work folder carries a `_meta.yaml` that binds the feature to its external
context: the ClickUp task, the Hermes session, and the git branch. It also holds
`links[]` — the list of durable docs this feature touches or produced.

It also carries the **authoritative** lifecycle state:

```yaml
status: in-progress       # planned | in-progress | blocked | code-complete | done
completed_at: null        # ISO-8601 UTC, stamped when status becomes 'done'
```

Move `status` along as the work moves, and stamp `completed_at` when it reaches
`done`. **Never introduce a second status field** — not in the implementation
log, not in a spec preamble, not in a review header. Every archiving bug this
harness has had came from two vocabularies disagreeing in silence.

## When is a folder "complete"?

`.claude/scripts/work-archive.sh` owns that answer. Ask it; never test for files
yourself:

```bash
.claude/scripts/work-archive.sh --scan          # verdict + reason per folder
.claude/scripts/work-archive.sh --check <slug>  # one folder; exit 0 iff DONE
```

It answers two separate questions that look alike and are not:

| | Question | Satisfied by |
|---|---|---|
| **TRAIL** | has this been *specced*? | a written story + a written `02-spec.md` |
| **DONE** | is this *finished*? | `status: done` + `completed_at` + a written `04-review.md` |

**"Written" is not "non-empty".** A folder created by `cp -R _TEMPLATE/` has
every stage file present and non-empty on day one — including `04-review.md`,
whose template ships the literal line `approve / request-changes — with
reasoning.` So `[ -s 04-review.md ]` and `grep request-changes` both report
untouched work as reviewed. A file counts as written when it carries at least
three lines the template does not.

Archiving needs **both**. To archive something unfinished on purpose, say why —
it is recorded in the archive's `_manifest.yaml` as `forced: true` with your
reason, so a deliberate early archive never looks like a clean pass:

```bash
.claude/scripts/work-archive.sh --force "<why>" <slug>
```

`--force` waives DONE only. A folder with no story and no spec is never
archivable.

## Adopting this in an existing repo

Work folders created before `status:`/`completed_at:` existed (or carrying the
old `active | done` vocabulary) will read as `IN-PROGRESS` and block — the safe
direction, and the message names the exact fix. Seed them in one pass:

```bash
.claude/scripts/work-archive.sh --migrate           # dry run: shows every change
.claude/scripts/work-archive.sh --migrate --apply   # writes
```

It maps `active` → `in-progress`, fills in a missing `status:` from what is
actually written in the folder, recovers a missing `_meta.yaml`, and — for
folders that already said `done` — stamps `completed_at` from the last git
commit touching them rather than inventing a date (untracked folders are
reported for a hand-written stamp instead).

**It never infers `done`.** The most it will claim is `code-complete`, even for
a folder with a fully written review. Deciding that something is finished is a
human's call — inferring a terminal state from file shape is the exact defect
this gate exists to remove. Re-running the migration is a no-op.

## Cross-feature docs are LINKED, not copied

Durable, cross-feature knowledge stays in its home and is referenced from
`_meta.yaml` `links[]` — never duplicated into `work/`:

- ADRs → `docs/knowledge/decisions/`
- Patterns → `docs/knowledge/patterns/`
- Domain models / glossary → `docs/knowledge/domain/`
- Product context → `docs/product/`
- Discoveries / research → the brainskit vault `docs/brain/` (`bk capture` → `bk file --to 40-research`)

## Starting a new feature

1. Copy `_TEMPLATE/` to `work/CU-<clickup-id>-<short-name>/`.
2. Fill in `_meta.yaml` (ids, branch, links).
3. Delete the stage file you don't need (`00-bug-report.md` for a feature, or
   `00-user-story.md` for a bug).
4. Work the stages in order, moving `_meta.yaml` `status:` along as you go.
5. When it ships: set `status: done`, stamp `completed_at`, write `04-review.md`,
   then `/dev archive` (or `.claude/scripts/work-archive.sh <slug>`).

---
<!-- doc-tracking -->
- Created: 2026-08-10 14:28
- Updated: 2026-08-10 14:31
- Updated: 2026-08-10 14:50
