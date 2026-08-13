# Implementation Log

Tracks implementation progress from spec completion to merge. Auto-generated when `02-spec.md` is created; updated as implementation progresses.

> **State lives in `_meta.yaml`, not here.** This log is a narrative of what
> happened. The machine-readable `status:` / `completed_at:` that gates
> archiving are in `_meta.yaml` — this file deliberately carries no second
> status vocabulary to disagree with them.

## Spec Completion

- **Date**: YYYY-MM-DD
- **Spec file**: `02-spec.md`

## Implementation Steps

Based on spec, track progress here:

- [ ] Step 1: [describe]
- [ ] Step 2: [describe]
- [ ] Step 3: [describe]

## Blockers

None currently.

## Next Command Suggestion

**Current stage**: `02-spec` (spec complete)  
**Suggested next**: `/dev` or `/van "implement the spec in 02-spec.md"`

---

**Status**: see `_meta.yaml` → `status:` (`planned` · `in-progress` · `blocked` ·
`code-complete` · `done`) and `completed_at:`. Update those as the work moves;
`.claude/scripts/work-archive.sh` reads them and nothing else.

**Stage Transitions**:
1. Spec done (`02-spec` complete) → Suggest `/dev` to start implementation
2. Implementation done → Suggest `/quality` or `/van "run quality gate"`
3. Quality passed → Suggest `/pr` or `/van "create pull request"`
4. PR merged → Move to `04-review` stage

---
<!-- doc-tracking -->
- Created: 2026-08-10 14:23
- Updated: 2026-08-10 14:24
