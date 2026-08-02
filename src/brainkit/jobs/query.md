# brainkit job: query

Answer the question in `{{wiki_language}}` from the evidence bundle only.
Clearly distinguish supported facts, inference, and missing evidence. Cite raw
evidence as `[^source:<sha256>]` and wiki evidence by its page path. Do not
invent citations.

Return only JSON with `answer`, `citations`, and `uncertainty`.

Question:

{{question}}

Evidence:

{{context}}

If the previous output failed validation, correct every reported issue:

{{repair_feedback}}
