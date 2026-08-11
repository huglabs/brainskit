# Contributing to brainkit

Thanks for taking the time. This project has a small, opinionated set of
invariants, and most of the review effort goes into keeping them true — so the
fastest path to a merged change is to know which one your change touches.

## Before you start

Open an issue first for anything that changes a contract: the apply gate, the
privacy filter, an error code, the vault layout, or an integration's lifecycle.
Those are the places where a reasonable-looking change can quietly weaken a
guarantee, and it is cheaper to agree on the shape before the code exists.

Bug fixes, documentation, new tests and new language extractors need no
preamble — send the pull request.

## Setting up

```bash
git clone https://github.com/huglabs/brainskit
cd brainskit
uv sync --all-extras --group dev
```

Full instructions, including how to run `bk` from the working tree, are in
[docs/development.md](./docs/development.md).

## The gate

Run all four before opening a pull request. CI runs the same ones, in the same
order:

```bash
uv run ruff check
uv run mypy src
uv run pytest
./scripts/verify-wheel.sh
```

`verify-wheel.sh` builds the sdist, builds the wheel from it, installs that
wheel in a throwaway environment and drives the real CLI. Packaged prompt
specs, output schemas and templates cannot be verified from the source tree, so
this is the only check that proves what actually ships.

The project is **lint-clean, not format-clean**: `ruff format` produces a large
pre-existing diff and is not part of the gate. Do not reformat files you are not
otherwise changing.

`src/brainkit/infrastructure/codeanalysis/` is vendored third-party source and
is excluded from lint and type checks. Keep it byte-identical to upstream so a
re-vendor stays a copy rather than a merge; changes belong in the adapter that
calls it. See [`NOTICE`](./NOTICE).

## What a good change looks like

- **Tests assert behaviour, not implementation.** A test that would pass with
  the feature reverted is not a test. If you are unsure, revert your change,
  run the test, and confirm it fails.
- **A refusal names the next move.** Every error carries a machine code (see
  [the failure table](./docs/commands.md#what-a-failure-tells-you-to-do)); pick
  the one whose *remedy* matches, and keep the human message specific enough
  that an operator knows what to configure or change.
- **No hardcoded model output.** Judgment jobs are schema-bound and retried with
  validation feedback; a fallback string that pretends to be an answer is worse
  than a failure.
- **Nothing unimplemented is simulated.** A command that needs a missing
  connector fails; it does not return a plausible-looking placeholder.
- **The mechanical half stays LLM-free.** Capture, index, search, apply, export
  and the structural lint must not acquire a model call.
- **Record a new defect class in [`AGENTS.md`](./AGENTS.md).** That file is the
  project's memory of what has already gone wrong; adding to it is part of the
  change, not paperwork after it.

## Commits and pull requests

Write commit subjects that say what changed in the world, not which files moved
— the existing history is the style guide. Keep a pull request to one concern;
if it fixes a bug and refactors around it, the reviewer needs to be able to see
the fix.

Note in the description which invariant the change touches and how you proved
it still holds.

## Reporting security issues

Do not open a public issue. See [SECURITY.md](./SECURITY.md).

## License

By contributing you agree that your contributions are licensed under the
[Apache License 2.0](./LICENSE), the same terms that cover the project.
