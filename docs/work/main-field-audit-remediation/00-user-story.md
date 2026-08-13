<!-- Stage 00. Why this work exists, from the user's point of view. -->

# Field audit remediation → brainskit 0.6.0

## The story

**As** someone who adopts brainskit because it promises *mechanical* enforcement —
provenance that is structural rather than conventional, a boundary a caller cannot
forget to apply —
**I need** the mechanisms that refuse, and the surfaces that report on those
mechanisms, to be true,
**so that** when the tool tells me a page is redacted, a gate is active, or a
version is installed, I can act on that answer without checking it myself.

## What triggered it

A five-agent field audit of `0.5.0` at HEAD `8216766` (12 Aug 2026). The
engineering underneath is strong — 911 tests green, ruff clean, ~55 lines of dead
code in 18,640 first-party LOC, security fundamentals held under a dedicated
pass. The defects cluster in one place, and they share one shape.

## The shape of the problem

> In five separate places, a check **verifies that a thing exists rather than that
> it works**, or **resolves an unknown to the permissive answer** instead of the
> safe one.

This project has already diagnosed this pattern twice in its own history. Commit
`485a20e` is titled *"Stop two prompts looping, and two graphs lying about what
they cover."* The write-gate and `commit_lint` honesty fixes landed for the same
reason. Each time, the fix was applied **at the reported site rather than to the
class** — so the pattern survived into the highest-leverage surfaces left.

The sharpest evidence is that the codebase argues against itself in prose.
`application/enrichment.py:173–186` states the correct rule and is pinned by a
test:

> An edge whose sources have all since been forgotten is treated as
> `never-ingest` rather than as unrestricted: provenance that no longer resolves
> is exactly the case where the safe answer and the convenient answer differ.

The parallel classifier for wiki pages does the opposite, and has no such test.
**The one high-value invariant the suite left unpinned is the one that broke.**

## What "done" looks like

A published `0.6.0` on PyPI where:

1. No boundary fails open. Unknown provenance resolves to `never-ingest`
   everywhere, not just in `Enrichment`.
2. No surface reports a mechanism as active without exercising it.
3. Every fix ships with a test that **fails when the fix is reverted** — the
   negative control, because four existing Obsidian tests pass with the
   filtering deleted.
4. `bk --version`, the README install line, and the published artifact all agree.

## Non-negotiable

This is a **remediation** programme, not a refactor with fixes attached. The
audit named seven things that are genuinely strong (see PRD §7). Regressing any
of them to land a fix is a failed outcome, not a tradeoff.

---
<!-- doc-tracking -->
- Created: 2026-08-12
- Updated: 2026-08-12 10:05
