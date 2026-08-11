<!--
Thanks for the change. Keep this to one concern: if it fixes a bug and
refactors around it, a reviewer needs to be able to see the fix.
-->

## What this changes

<!-- What changes in the world, not which files moved. -->

## Why

<!-- Link the issue if there is one. -->

## Which invariant it touches

<!--
One of: raw evidence is immutable · only the gate writes the wiki · every claim
carries provenance · privacy is a declared boundary · mechanical stays LLM-free
· a write is one unit of work · none of them.

If it touches one, say how you proved it still holds.
-->

## How it was verified

<!--
Beyond "the suite passes". A negative control is the strongest evidence there
is: revert the change, run the new test, confirm it fails.
-->

## Checklist

- [ ] `uv run ruff check`
- [ ] `uv run mypy src`
- [ ] `uv run pytest`
- [ ] `./scripts/verify-wheel.sh` (required if packaging, prompts, schemas or templates changed)
- [ ] Tests assert behaviour, and fail without the change
- [ ] Any new defect class is recorded in `AGENTS.md`
- [ ] No unrelated reformatting (this project is lint-clean, not format-clean)
