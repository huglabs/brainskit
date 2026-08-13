# Next

What the close-out review left open that does not block `0.6.1`: the reporting
surfaces that are advisory-but-look-authoritative, the release pipeline's own
gaps, and the test-quality work the review argued for.

Full findings and their evidence:
[`04-review.md`](../../work/main-field-audit-remediation/04-review.md).
`RV*` are review findings with no identifier from the original audit.

## Enforcement rows that overstate themselves

Both were confirmed by breaking the layer and asking the surface, not by reading.

| ID | Intent | Size |
|----|--------|------|
| P3 | The `instructions` layer is a sentinel-presence check: it passes with an empty managed block, with a block naming a *different* vault, and with a block instructing the agent to write the wiki directly. It renders as a `✓ active` row identical to the three layers that are mechanically enforced. Either verify the block's content, or render advisory layers differently from enforcing ones | medium |
| P4 | `bk doctor`'s `healthy` is dominated by missing tree-sitter grammars, so on a default install (no `code` extra) it is permanently `False` — and a genuine enforcement regression changes nothing a reader can see. An optional extra must not be able to mask a real fault | small |
| RV2 | `state_tag` does not colour `malformed` as a fault, so a refused graph reads as ordinary output | `console.py:179`, small |
| RV3 | The `partial` state is undocumented — it appears in output with no entry in `docs/commands.md` or `docs/code-graph.md` | small |

## Fresh-user defects below the headline

| ID | Intent | Size |
|----|--------|------|
| D2 | `bk forget --force` must not be undone by the `bk reconcile` that `bk lint` tells you to run next — today the two documented steps cancel each other | medium |
| D4 | `bk code build` on a fresh vault graphs only the vault's own hook scripts, which `docs/code-graph.md` explicitly says are excluded | medium |
| D8 | `bk lint --json` emits `{"ok": true, "result": {"ok": false}}` — two answers to one question, in one document | small |
| D5 | A hint names `--code-only`, a flag no command accepts | small |

## Release pipeline

R1 and R5 are repository settings rather than code, and are the operator's to
take; they are listed here so they are not forgotten because nothing in the tree
changes.

| ID | Intent | Size |
|----|--------|------|
| R1 | `SECURITY.md`'s only sanctioned reporting channel does not exist — `private-vulnerability-reporting` is `{"enabled": false}` while the policy forbids public issues, so a reporter following it has nowhere to go. Secret scanning and Dependabot security updates are also disabled | setting |
| R4 | The PyPI-visibility guard matches the version as a **substring** (`0.6` matches `0.6.0`) and asserts that *a* file exists rather than that both the wheel and the sdist landed. The gate is real and can fail; it is the comparison that is loose | `release.yml`, small |
| R5 | Publishing is unattended: `environments/pypi` has no required reviewers, and any `v*` tag push publishes | setting |
| DEP1 | Work the Dependabot queue. **Four of the five red X's are a billing lock, not test failures** — those jobs ran 2s with zero steps; only #3 is genuinely green. **#1 (tree-sitter 0.26) must not be merged as-is**: it removes `Language.query`, `Language.version` and `Parser.timeout_micros` and makes `Point` a tuple subclass, and the vendored extractors that would break are excluded from both `ruff` and `mypy`, so CI would not catch it | medium |

## Test quality

The review's central negative result: **14 production behaviours were neutralised
with the full 1051-test suite still passing.**

| ID | Intent | Size |
|----|--------|------|
| TQ1 | Close the 14 known gaps. Named starting points: `bk status`'s source count (protected by nothing), `_merge_scoped`'s dangling-edge guard (its named test executes the loop body zero times), `test_vendoring.py:67` (asserts the absence of a string appearing nowhere in the file), `test_code_grammars.py:229` (asserts the opposite of its name) | medium |
| TQ2 | Sweep the shape that produced most of them: an assertion reachable only through a `for` loop over a production-controlled collection that is empty in the fixture — **84 sites across 33 files**. A loop that never runs is a test that cannot fail | large |
| S2-6 | `tests/conftest.py`'s registry isolation is bypassed by `python -m unittest`, which **29 of 36** test files support. The isolation must not depend on the runner | small |
| TQ3 | Adopt `tests/test_gate.py`'s vacuity guard suite-wide — its `run_cli` refuses to proceed when output contains `"Not a brainskit vault"` or `"Traceback"`, which is what caught two tautologies during the remediation | medium |

## Process

| ID | Intent | Size |
|----|--------|------|
| PR1 | Make the four-track review a release gate rather than a one-off. Tracks 1 and 2 — a fresh install from PyPI, and an enforcement harness that breaks each layer in a throwaway copy — found defects no amount of source reading produced, and both are cheap to re-run against a candidate wheel | medium |

---
<!-- doc-tracking -->
- Created: 2026-08-12
- Updated: 2026-08-12 10:08
- Updated: 2026-08-13 13:15
