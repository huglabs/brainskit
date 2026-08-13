"""Model-inferred relationships, admitted through a gate and kept apart.

An agent can already enrich this vault's graph without any of this: propose a
wiki page whose body cites its sources, let `bk apply` gate it, and `bk graph`
derives the `sourced_from` and `links_to` edges from what was written. The edge
is then a *consequence of a cited claim*, and every guarantee holds because the
claim carries source hashes and those hashes carry branches.

This module is for the relationship that does not want to be a page — "these
two entities are the same thing", "this concept supersedes that one" — where
the claim *is* the edge. Three constraints shape it, and all three come from
existing invariants rather than from taste:

- **It never enters `graph/graph.json`.** That file is a projection: `bk graph`
  rewrites it from the wiki every time, so anything written there is destroyed
  on the next build. Enrichment lives in `.brain/enrichment.json` and is joined
  at read time by `Projections.graph_data(enrichment=True)` — reachable as
  `bk export --enrichment` and `bk enrich list`, and off everywhere else. The
  opt-in is the point: an inferred edge is a weaker claim than a derived one,
  so a caller has to ask for it rather than receive it because this module
  exists. Integration exports refuse the flag outright; what they carry is
  their own policy's business.
- **Every edge names the sources it was derived from.** The privacy filter
  decides by the branch a *source record* lives in, so an edge with nothing
  behind it is unclassifiable — and filtering deliberately runs after graph
  expansion precisely so an edge cannot pull a restricted node back into view.
  An edge that cannot name its provenance is refused, not stored and hidden.
- **It is marked wherever it appears.** `provenance: "model"` on the way out,
  and derived edges say `"derived"`, so no reader has to work out which edges
  were extracted and which were argued for.

The gate mirrors `bk apply`: validate the whole batch before storing any of it,
reject dangling endpoints the way unresolved `[[wiki-links]]` are rejected, and
make a re-run idempotent — an edge's identity is its (source, relation, target)
triple, so proposing the same relationship twice is one edge.
"""

from __future__ import annotations

from typing import Any

from brainskit.application.ports import VaultPort
from brainskit.application.privacy import (
    _consumer_allows,
    _privacy_for_record,
    _validate_consumer,
    strictest_privacy,
)
from brainskit.domain.model import (
    EnrichmentEdge,
    NotConfiguredError,
    NotFoundError,
    PrivacyMode,
    ValidationError,
)

STATE = "enrichment"


class Enrichment:
    """The store, the gate, and the privacy of an inferred edge."""

    def __init__(self, vault: VaultPort, graph_port: Any = None):
        self.vault = vault
        self.graph_port = graph_port

    # ---------------------------------------------------------------- write

    def apply(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Validate a batch of proposed edges, then store all or none.

        Whole-batch validation for the reason `bk apply` uses it: a partially
        applied proposal leaves the operator to work out which half landed.
        """

        raw_edges = payload.get("edges")
        if not isinstance(raw_edges, list) or not raw_edges:
            raise ValidationError(
                "An enrichment proposal must carry a non-empty `edges` list"
            )
        edges = [EnrichmentEdge.from_dict(item) for item in raw_edges]

        records = self.vault.registry()
        unresolved = sorted(
            {h for edge in edges for h in edge.derived_from if h not in records}
        )
        if unresolved:
            raise ValidationError(
                "An enrichment edge cites evidence this vault does not hold",
                details={
                    "unresolved": unresolved[:10],
                    "unresolved_total": len(unresolved),
                    "hint": (
                        "Every inferred edge inherits its privacy from the "
                        "sources behind it, so an unknown source makes the "
                        "boundary undecidable"
                    ),
                },
            )

        known = self._node_ids()
        dangling = sorted(
            {
                endpoint
                for edge in edges
                for endpoint in (edge.source, edge.target)
                if endpoint not in known
            }
        )
        if dangling:
            raise ValidationError(
                "An enrichment edge points at a node that is not in the graph",
                details={
                    "unknown": dangling[:10],
                    "unknown_total": len(dangling),
                    "hint": "Run bk graph, or cite a node id the graph contains",
                },
            )

        stored = {edge.id: edge.to_dict() for edge in edges}

        def mutate(state: dict[str, Any]) -> dict[str, Any]:
            existing = state.setdefault("edges", {})
            existing.update(stored)
            return state

        self.vault.mutate_state(STATE, mutate)
        return {
            "stored": len(stored),
            "ids": sorted(stored),
            "total": len(self.vault.read_state(STATE).get("edges", {})),
        }

    def forget(self, identifier: str) -> dict[str, Any]:
        edges = self.vault.read_state(STATE).get("edges", {})
        matches = sorted(key for key in edges if key.startswith(identifier))
        if not matches:
            raise NotFoundError(
                "No enrichment edge with that id", details={"id": identifier}
            )
        if len(matches) > 1:
            raise ValidationError(
                "Enrichment id is ambiguous", details={"matches": matches}
            )

        def mutate(state: dict[str, Any]) -> dict[str, Any]:
            state.get("edges", {}).pop(matches[0], None)
            return state

        self.vault.mutate_state(STATE, mutate)
        return {"forgotten": matches[0]}

    # ----------------------------------------------------------------- read

    def edges(self, *, consumer: str = "human") -> list[dict[str, Any]]:
        """Stored edges this consumer may see, strictest-source-first."""

        _validate_consumer(consumer)
        records = self.vault.registry()
        config = self.vault.config()
        visible = []
        for edge in self.vault.read_state(STATE).get("edges", {}).values():
            privacy = self.privacy_of(edge, records, config)
            if _consumer_allows(consumer, privacy):
                visible.append({**edge, "privacy": privacy.value})
        return sorted(visible, key=lambda edge: str(edge.get("id", "")))

    def privacy_of(
        self, edge: dict[str, Any], records: Any, config: Any
    ) -> PrivacyMode:
        """The strictest policy across the sources this edge was drawn from.

        An edge whose sources have all since been forgotten is treated as
        `never-ingest` rather than as unrestricted: provenance that no longer
        resolves is exactly the case where the safe answer and the convenient
        answer differ, and `bk lint` reports it so it can be repaired instead
        of lingering invisible.
        """

        hashes = [h for h in edge.get("derived_from", []) if h in records]
        if not hashes:
            return PrivacyMode.NEVER_INGEST
        return strictest_privacy(
            (
                _privacy_for_record(config, records[content_hash])
                for content_hash in hashes
            ),
            # `hashes` is non-empty here -- the guard above returns first. The
            # answer is stated rather than defaulted so this call site cannot
            # drift into the failure `_evidence_privacy` had.
            on_empty=PrivacyMode.NEVER_INGEST,
        )

    def orphaned(self) -> list[dict[str, Any]]:
        """Edges whose evidence is gone — the drift `bk lint` has to report."""

        records = self.vault.registry()
        orphans = []
        for edge in self.vault.read_state(STATE).get("edges", {}).values():
            missing = [h for h in edge.get("derived_from", []) if h not in records]
            if missing:
                orphans.append({**edge, "unresolved": missing})
        return orphans

    def merge_into(
        self, graph: dict[str, Any], *, consumer: str = "human"
    ) -> dict[str, Any]:
        """Join visible enrichment onto an already-filtered graph.

        Applied *after* the graph's own privacy filter, and both endpoints must
        have survived it. An inferred edge is not permitted to be the thing
        that reintroduces a node the consumer was not allowed to see — the same
        reason the filter runs after expansion rather than before.
        """

        allowed = {str(node["id"]) for node in graph.get("nodes", [])}
        joined = [
            {**edge, "type": edge["relation"]}
            for edge in self.edges(consumer=consumer)
            if edge["source"] in allowed and edge["target"] in allowed
        ]
        return {
            **graph,
            "edges": [
                {**edge, "provenance": "derived"} for edge in graph.get("edges", [])
            ]
            + joined,
            "enrichment_edges": len(joined),
        }

    # -------------------------------------------------------------- helpers

    def _node_ids(self) -> set[str]:
        if not self.graph_port:
            raise NotConfiguredError("Graph adapter is not configured")
        graph = self.graph_port.build(self.vault)
        return {str(node["id"]) for node in graph.get("nodes", [])}
