# brainkit job: file proposal

Given the configured branches and a captured source, choose exactly one existing
destination branch. Never invent a branch merely for one item.

Return only JSON with `branch`, `reason`, and `confidence`. Write `reason` in
`{{wiki_language}}`.

Configured branches:

{{branches}}

Evidence:

{{context}}

If the previous output failed validation, correct every reported issue:

{{repair_feedback}}
