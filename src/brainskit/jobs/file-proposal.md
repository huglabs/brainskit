# brainskit job: file proposal

Given the configured branches and a captured source, choose exactly one existing
destination branch. Never invent a branch merely for one item.

Return only JSON with `branch`, `reason`, and `confidence`. Write `reason` in
`{{wiki_language}}`.

Each branch carries `seed`: true when the operator declared it as part of this
vault's taxonomy. Use it only to break a tie — where the evidence fits two
branches equally well, prefer the seed one. Never let it outweigh evidence.

Configured branches:

{{branches}}

Evidence:

{{context}}

If the previous output failed validation, correct every reported issue:

{{repair_feedback}}
