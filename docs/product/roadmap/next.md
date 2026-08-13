# Next

What the close-out review left open that did not block `0.6.2`: one privacy
boundary that needs a decision, the reporting surfaces that are
advisory-but-look-authoritative, the release pipeline's own gaps, and the
test-quality work the review argued for.

Full findings and their evidence:
[`04-review.md`](../../archive/2026-Q3/main-field-audit-remediation/04-review.md).
`RV*` are review findings with no identifier from the original audit; `TC*` are
findings raised by the twin checks of 13 Aug, which carry no identifier at all.
Each row below is tracked as an issue under the
[**Next**](https://github.com/huglabs/brainskit/milestone/2) milestone — the row
carries the evidence, the issue carries the workflow.

## A consumer boundary that needs deciding

Not a patch — a question about what `consumer` means. Whichever way it goes has
to be written down, because the next reader will ask it again.

| ID | Intent | Size | Issue |
|----|--------|------|-------|
| TC1 | `reader_status` returns `"vault": str(self.vault.root)` unconditionally, so a `cloud` consumer is told the installation's absolute path on this machine. Every other key on that response is filtered — `sources`, `wiki_pages`, `freshness` and the lint findings are all consumer-scoped, and `redacted_sources` exists precisely to report a count instead of content. The vault's own doctrine is that a filename and a branch name are disclosure in their own right; a filesystem path is the same class of fact. Deciding to filter it means deciding that installation facts are inside the boundary, which would have to apply to the `vault` key on every response that carries one — so it is a contract decision, not a one-line redaction. Deciding *not* to filter it is also fine, and then it needs a comment saying so | medium | [#12](https://github.com/huglabs/brainskit/issues/12) |

## Enforcement rows that overstate themselves

Both were confirmed by breaking the layer and asking the surface, not by reading.
**P3 shipped** — advisory layers now render as `instructions (advisory)`, muted —
and RV2 and RV3 shipped with it: `_STATE_COLORS` now draws `malformed` in `ERR`
with a `WARN` default rather than a calm fallback, and `partial` is documented at
`docs/code-graph.md:63`. What remains is the half of P4 that the headline fix
did not reach.

| ID | Intent | Size | Issue |
|----|--------|------|-------|
| P4 | `bk doctor`'s **headline** is fixed — it now names the gate and the grammars together, so an enforcement regression is visible even on a machine that is missing grammars. The **boolean** is not: `"healthy": not missing and probe["state"] in {...}` at `cli.py:2677`, where `missing` is every tree-sitter grammar absent. `code` is an optional extra of ~29 wheels, so on a default `pip install brainskit` the field is permanently `False`. Anyone gating CI on `bk doctor --json`'s `healthy` gets a constant, and the fault it was meant to report is invisible again one layer down from where it was fixed. Either the extra stops feeding `healthy`, or `healthy` gains a companion the JSON caller can use | small | [#13](https://github.com/huglabs/brainskit/issues/13) |

## Fresh-user defects that did not make the release cut

`bk ask`'s refusal is the sharpest thing left from the fresh-user track and the
only member of `U1–U4` that has been re-derived against code; the rest of that
group is carried on [`later.md`](later.md).

| ID | Intent | Size | Issue |
|----|--------|------|-------|
| U1 | `bk ask` refuses an entire query because BM25 recall brushed a `never-ingest` source, on a question unrelated to the private content, with no next step offered. `Jobs.ask` passes `_context_branches(context)` straight into the runner, which raises `PolicyError("Branch policy forbids judgment ingestion")` if **any** branch in that set is `never-ingest`. The refusal is correct — that evidence must not reach a model — but the remedy is to narrow the context, not to refuse the question, and today nothing tells the user which of the two happened | `application/jobs.py:53-56` · `infrastructure/llm.py:140-144`, medium | [#14](https://github.com/huglabs/brainskit/issues/14) |

## Release pipeline

R1 and R5 are repository settings rather than code, and are the operator's to
take; they are listed here so they are not forgotten because nothing in the tree
changes. **R4 moved to [`now.md`](now.md)** — `0.6.2` has since shipped without
it, so it is now cheapest to fold into the tag after that one. **R7 is closed**:
the stale premise it asked to correct is not in `release.yml` and, per
`git log -S`, never was; the comment at `release.yml:91-98` already tells the
corrected `v0.5.0` `invalid-publisher` story.

| ID | Intent | Size | Issue |
|----|--------|------|-------|
| R1 | `SECURITY.md`'s only sanctioned reporting channel does not exist — `private-vulnerability-reporting` is `{"enabled": false}` while the policy forbids public issues, so a reporter following `SECURITY.md:17-19` has nowhere to go. Secret scanning and Dependabot security updates are also disabled | setting | [#15](https://github.com/huglabs/brainskit/issues/15) |
| R5 | Publishing is unattended. `release.yml:17-20` triggers on any `v*` tag push and `release.yml:74-89` declares `environment: pypi` with no manual-approval step of its own — so the only possible human gate is required reviewers on that GitHub environment, which is a repo setting and is not visible in the tree. Confirm it, or add the gate | setting | [#16](https://github.com/huglabs/brainskit/issues/16) |
| DEP1 | Work the Dependabot queue. `.github/dependabot.yml` is configured (github-actions and uv, monthly, with `tree-sitter*` and dev-tools grouped) but the open PRs have not been triaged. Five were open and **#3 — the only genuinely green one — was closed unmerged on 13 Aug**, leaving **#1, #2, #4 and #5**. Every one of those four shows a red X that is **a billing lock, not a test failure**: `lint and types` fails in about 2s with zero steps and the rest of the matrix skipped. **#1 (tree-sitter 0.26) must not be merged as-is**: it removes `Language.query`, `Language.version` and `Parser.timeout_micros` and makes `Point` a tuple subclass, `pyproject.toml` pins `tree-sitter>=0.23.0,<0.26` against exactly that, and the vendored extractors that would break are excluded from both ruff (`pyproject.toml:170`) and mypy (`:231`) — so CI would not catch it | medium | [#17](https://github.com/huglabs/brainskit/issues/17) |

## Test quality

The review's central negative result: **14 production behaviours were neutralised
with the full suite still passing.** That still holds at 1213 tests.

| ID | Intent | Size | Issue |
|----|--------|------|-------|
| TQ1 | Close the 14 known gaps. **The line references in `04-review.md` no longer resolve** — `test_vendoring.py:67` is an import block today and `test_code_grammars.py:229` is a legitimate assertion — so re-derive by name, not by line. The one starting point that does re-derive: `test_vendoring.py`'s `test_no_vendored_string_points_at_graphifyy` (now line 449) scans every vendored `*.py` for the literal `"graphifyy["`, which appears nowhere in the tree, so `offenders` is empty by construction. The package really is spelled `graphifyy` (`codeanalysis/cache.py:31`), so the needle is not a typo — but nothing establishes that the bracketed extras form it hunts for has ever been present, which is what a regression guard has to show | medium | [#18](https://github.com/huglabs/brainskit/issues/18) |
| TQ2 | Sweep the shape that produced most of them: an assertion reachable only through a `for` loop over a production-controlled collection that is empty in the fixture. A loop that never runs is a test that cannot fail. **The previously reported "84 sites across 33 files" does not reproduce**: an AST count gives **57 sites across 19 files** where the assert is a direct statement of the loop body, and **158 across 26** counting nested blocks. Neither matches, and no pattern reaches 33 files. Use the strict figure as the working scope, and re-measure before quoting a number | large | [#19](https://github.com/huglabs/brainskit/issues/19) |
| S2-6 | `tests/conftest.py`'s registry isolation is bypassed by `python -m unittest`. It is not a fixture: `conftest.py:37-42` sets `XDG_CONFIG_HOME` to a temp dir as an import-time side effect, deliberately, so that a module touching the registry at import is covered too. Only pytest imports `conftest.py`, so `python tests/test_x.py` writes to the operator's real `~/.config/brainskit/vaults.json`. **29 of 35** test files carry a `__main__`-guarded `unittest.main()` and are directly runnable that way. The isolation must not depend on the runner | small | [#20](https://github.com/huglabs/brainskit/issues/20) |
| TQ3 | Adopt `tests/test_gate.py`'s vacuity guard suite-wide — its `run_cli` refuses to proceed when output contains `"Not a brainskit vault"` or `"Traceback"`, which is what caught two tautologies during the remediation | medium | [#21](https://github.com/huglabs/brainskit/issues/21) |

## Process

| ID | Intent | Size | Issue |
|----|--------|------|-------|
| PR1 | Make the four-track review a release gate rather than a one-off. **Track 4 already is one** — `scripts/verify-wheel.sh` runs in `ci.yml:75` and `release.yml:66`. Tracks 1 and 2 are not, and they are the two that found defects no amount of source reading produced: a fresh install from PyPI followed literally, and an enforcement harness that breaks each layer in a throwaway copy. Both are cheap to re-run against a candidate wheel, and both would have caught D4 before it reached three releases | medium | [#22](https://github.com/huglabs/brainskit/issues/22) |

---
<!-- doc-tracking -->
- Created: 2026-08-12
- Updated: 2026-08-12 10:08
- Updated: 2026-08-13 13:15
- Updated: 2026-08-13 15:50
- Updated: 2026-08-13 17:04
- Updated: 2026-08-13 17:05
- Updated: 2026-08-13 17:06
- Updated: 2026-08-13 17:08
