# brainskit job: semantic lint

Review the evidence in `{{wiki_language}}`. Identify contradictions, unsupported
synthesis, stale-looking claims, and likely duplicates. Report evidence paths
and confidence. Do not rewrite pages and do not invent findings.

{{context}}

Return only JSON with a `findings` array.

If the previous output failed validation, correct every reported issue:

{{repair_feedback}}
