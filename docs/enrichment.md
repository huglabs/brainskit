# Enrichment

An agent can already enrich this vault's graph with no special machinery:
propose a wiki page whose body cites its sources, let `bk apply` gate it, and
`bk graph` derives the `sourced_from` and `links_to` edges from what was
written. The edge is then a *consequence of a cited claim*, and every guarantee
holds because the claim carries source hashes and those hashes carry branches.

`bk enrich` is for the relationship that does not want to be a page — "these
two entities are the same", "this concept supersedes that one" — where the
claim *is* the edge. Three rules make that admissible, and all three come from
invariants that already existed:

| Rule | Why it is not negotiable |
|---|---|
| **Never enters `graph/graph.json`** | That file is a projection: `bk graph` rewrites it from the wiki every time, so an edge written there is destroyed on the next build. Enrichment lives in `.brain/enrichment.json` and is joined at read time. |
| **Every edge names its evidence** | The privacy filter decides by the branch a *source record* lives in. An edge with nothing behind it is unclassifiable, and the filter runs after graph expansion precisely so an edge cannot pull a restricted node back into view. An edge that cannot name its provenance is refused, not stored and hidden. |
| **Marked wherever it appears** | `provenance: "model"`, with derived edges labelled `"derived"`, so no reader has to work out which edges were extracted and which were argued for. |

An edge inherits the **strictest** policy across the sources it was derived
from — the same rule the judgment router applies to evidence spanning branches,
shared as one function rather than written twice. One `never-ingest` source
withholds the whole edge. Provenance that no longer resolves fails closed
(treated as `never-ingest`) and is reported by `bk lint` as
`enrichment.unresolved_source`, so it can be repaired rather than lingering
invisible.

```json
{
  "edges": [
    {
      "source": "page:wiki/concepts/compiled-memory.md",
      "target": "page:wiki/concepts/provenance.md",
      "relation": "supersedes",
      "derived_from": ["<64-char sha256>"],
      "model": "qwen2.5:3b",
      "note": "proposed during a digest run"
    }
  ]
}
```

```bash
bk --vault ./my-vault enrich apply proposal.json
bk --vault ./my-vault enrich list --consumer local
bk --vault ./my-vault enrich forget 1eeda80f
```

The gate mirrors `bk apply`: the whole batch is validated before any of it is
stored, an endpoint that is not a node in the graph is refused the way an
unresolved `[[wiki-link]]` is, and identity is the `(source, relation, target)`
triple — so an agent that re-runs proposes one edge, not two.

**What this does not do.** It infers nothing on its own; it is a gate for
claims a model has already made. And the [LOCOMO benchmark](./benchmarks.md) is
the reason to be careful with the tier above it: graphify's LLM-extracted graph
reached a coverage ceiling of 0.575 because entity extraction *discards*.
Enrichment that adds edges over a corpus it did not fully retain inherits that
ceiling.
