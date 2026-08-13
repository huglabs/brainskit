"""Derived artefacts and the egress that carries them out of the vault.

Views, the graph, file exports and the database integrations are one module
because they are one act seen at different distances: taking the compiled vault
and rendering it somewhere else. Every one of them crosses the privacy
boundary, and every one of them applies it *after* the graph is expanded --
edges are exactly how restricted evidence gets reintroduced.

`consumer` is therefore not a formatting option here. It is the argument that
decides what leaves.
"""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import PurePosixPath
from typing import Any

from brainskit.application.freshness import GRAPH_PROJECTION, VIEWS_PROJECTION
from brainskit.application.health import Health
from brainskit.application.pages import GENERATED_MARKER
from brainskit.application.ports import GraphPort, IntegrationPort, VaultPort
from brainskit.application.privacy import (
    _consumer_allows,
    _evidence_privacy,
    _privacy_for_record,
    _validate_consumer,
    _view_branch,
)
from brainskit.domain.model import (
    NotConfiguredError,
    SourceRecord,
    ValidationError,
)

# File exports leave the vault, so they default to the local boundary and never
# carry never-ingest evidence unless the caller widens it on purpose.
DEFAULT_EXPORT_CONSUMER = "local"
INTEGRATION_TARGETS = ("obsidian", "neo4j", "postgres")
EXPORT_SUFFIXES = {
    "json": "json",
    "graphml": "graphml",
    "cypher": "cypher",
    "neo4j": "cypher",
    "kuzu": "cypher",
    "llms-txt": "txt",
}


class Projections:
    """Generated views and graph, plus every path evidence takes out."""

    def __init__(
        self,
        vault: VaultPort,
        health: Health,
        graph_port: GraphPort | None = None,
        integrations: IntegrationPort | None = None,
    ):
        self.vault = vault
        self.health = health
        self.graph_port = graph_port
        self.integration_port = integrations


    def views(self, *, consumer: str = "human") -> dict[str, Any]:
        _validate_consumer(consumer)
        status = self.health.status()
        config = self.vault.config()
        all_records = self.vault.registry()
        records = {
            content_hash: record
            for content_hash, record in all_records.items()
            if _consumer_allows(consumer, _privacy_for_record(config, record))
        }
        freshness = self.vault.read_state("freshness").get("pages", {})
        pages = {}
        for path in self.vault.wiki_pages():
            content = self.vault.read_text(path)
            privacy = _evidence_privacy({"path": path}, content, all_records, config)
            if _consumer_allows(consumer, privacy):
                pages[path] = content
        home = [
            GENERATED_MARKER,
            "# Brainskit",
            "",
            f"- Sources: **{len(records)}**",
            f"- Pending ingestion: "
            f"**{sum(record.status == 'pending' for record in records.values())}**",
            f"- Wiki pages: **{len(pages)}**",
            f"- Health: **{'healthy' if status['healthy'] else 'needs attention'}**",
            "",
            "## Branches",
            "",
        ]
        written = ["views/home.md"]
        by_branch: dict[str, list[SourceRecord]] = defaultdict(list)
        for record in sorted(records.values(), key=lambda item: item.path):
            by_branch[_view_branch(record)].append(record)
        # Every known branch is rewritten, including the ones the consumer cannot
        # see, so a narrower run always overwrites a wider run's rows.
        for branch in status["by_branch"]:
            safe_branch = branch.replace("/", "-")
            view_path = f"views/map/{safe_branch}.md"
            visible = by_branch.get(branch, [])
            home.append(f"- [[map/{safe_branch}|{branch}]] — {len(visible)} sources")
            rows = [
                GENERATED_MARKER,
                f"# {branch}",
                "",
                "| Raw source | Wiki pages | Freshness | Status | Captured |",
                "| --- | --- | --- | --- | --- |",
            ]
            for record in visible:
                linked_paths = [
                    path
                    for path, content in pages.items()
                    if record.content_hash in content
                ]
                linked = [
                    f"[[{PurePosixPath(path).stem}]]" for path in linked_paths
                ]
                freshness_badges = sorted(
                    {
                        str(freshness.get(path, {}).get("status", "unknown"))
                        for path in linked_paths
                    }
                )
                rows.append(
                    "| "
                    f"[[../{record.path}|{record.original_name}]] | "
                    f"{', '.join(linked) or '—'} | "
                    f"{', '.join(freshness_badges) or '—'} | {record.status} | "
                    f"{record.captured_at[:10]} |"
                )
            self.vault.write_generated(view_path, "\n".join(rows) + "\n")
            written.append(view_path)
        self.vault.write_generated("views/home.md", "\n".join(home) + "\n")
        self.health._record_projection(VIEWS_PROJECTION)
        return {"written": written}

    def graph(
        self, *, consumer: str = "local", html: bool = False
    ) -> dict[str, Any]:
        """Write `graph/graph.json` inside a consumer's boundary.

        This used to build the graph and write it straight to disk while every
        sibling path -- `graph_data`, `export`, the integrations -- filtered
        first. A never-ingest source contributed its hash, its *filename* and
        its *branch name* to the artifact, each of which is disclosure on its
        own. `graph/` being gitignored bounded the damage; it did not make the
        file correct, and nothing written in it said which boundary it carried.

        Defaults to `local`, matching every other file target.
        """

        if not self.graph_port:
            raise NotConfiguredError("Graph adapter is not configured")
        graph = self.graph_data(consumer=consumer, limit=0)
        self.vault.write_generated(
            "graph/graph.json", json.dumps(graph, indent=2, ensure_ascii=False) + "\n"
        )
        written = ["graph/graph.json"]
        if html:
            self.vault.write_generated(
                "graph/graph.html", self.graph_port.export(graph, "html")
            )
            written.append("graph/graph.html")
        self.health._record_projection(GRAPH_PROJECTION)
        return {
            "consumer": consumer,
            "nodes": len(graph["nodes"]),
            "edges": len(graph["edges"]),
            "redacted_nodes": graph.get("redacted_nodes", 0),
            "unresolved_sources": graph.get("unresolved_sources", 0),
            "written": written,
        }

    def graph_data(
        self,
        *,
        consumer: str = "human",
        enrichment: bool = False,
        limit: int = 0,
    ) -> dict[str, Any]:
        """The knowledge graph, filtered for a consumer and optionally bounded.

        `limit` defaults to 0 ("no cap") because `export()` and
        `integration_sync()` both call this to produce someone's *complete*
        graph — a Neo4j/Postgres/JSON export silently missing everything past
        the first 1,500 nodes is data loss, not a viewer being considerate.
        Only the web viewer's `/api/graph` passes a nonzero `limit`: the same
        3D view already re-ranks and slices to its own render cap client-side
        (see `WEB_VIEWER_HTML`'s `NODES_LIMIT`), so a vault with tens of
        thousands of pages was shipping an 8 MB, 18-second payload to throw
        most of it away the moment it arrived. Bounding here, the same way
        `CodeGraph.data` already bounds the code graph, means the browser
        never has to receive what it was never going to render.
        """

        if not self.graph_port:
            raise NotConfiguredError("Graph adapter is not configured")
        _validate_consumer(consumer)
        graph = self.graph_port.build(self.vault)
        records = self.vault.registry()
        config = self.vault.config()
        allowed: set[str] = set()
        for node in graph["nodes"]:
            node_id = str(node["id"])
            if node_id.startswith("raw:"):
                content_hash = node_id.removeprefix("raw:")
                record = records.get(content_hash)
                if record and _consumer_allows(
                    consumer, _privacy_for_record(config, record)
                ):
                    allowed.add(node_id)
                continue
            path = str(node.get("path", ""))
            content = self.vault.read_text(path)
            privacy = _evidence_privacy(node, content, records, config)
            if _consumer_allows(consumer, privacy):
                allowed.add(node_id)
        kept_nodes = [node for node in graph["nodes"] if node["id"] in allowed]
        kept_edges = [
            edge
            for edge in graph["edges"]
            if edge["source"] in allowed and edge["target"] in allowed
        ]
        filtered = {
            **graph,
            "nodes": kept_nodes,
            "edges": kept_edges,
            "consumer": consumer,
            "redacted_nodes": len(graph["nodes"]) - len(allowed),
        }
        if limit and len(kept_nodes) > limit:
            # Ranked by degree within *this consumer's* filtered set, not the
            # unfiltered graph — a redacted node must never spend a limit
            # slot, and the reported totals below must describe what this
            # consumer could see in the first place.
            degree: dict[str, int] = {}
            for edge in kept_edges:
                degree[edge["source"]] = degree.get(edge["source"], 0) + 1
                degree[edge["target"]] = degree.get(edge["target"], 0) + 1
            ranked = sorted(
                kept_nodes, key=lambda node: degree.get(node["id"], 0), reverse=True
            )
            bounded_nodes = ranked[:limit]
            bounded_ids = {node["id"] for node in bounded_nodes}
            bounded_edges = [
                edge
                for edge in kept_edges
                if edge["source"] in bounded_ids and edge["target"] in bounded_ids
            ]
            filtered = {
                **filtered,
                "nodes": bounded_nodes,
                "edges": bounded_edges,
                "total_nodes": len(kept_nodes),
                "total_edges": len(kept_edges),
                "hidden_nodes": len(kept_nodes) - len(bounded_nodes),
                "hidden_edges": len(kept_edges) - len(bounded_edges),
            }
        if not enrichment:
            return filtered
        # This is the "joined at read time" half of `application/enrichment.py`:
        # model-inferred edges are never written into `graph/graph.json`, which
        # `bk graph` rewrites from the wiki, so the only place they can appear
        # is here. Off by default and named by the caller, because an inferred
        # edge is a weaker claim than a derived one and nothing should start
        # emitting them because a dependency grew a feature.
        #
        # Deliberately after the filter above: `merge_into` keeps only edges
        # whose *both* endpoints survived it, so an inferred edge cannot be the
        # thing that reintroduces a node this consumer was not allowed to see.
        from brainskit.application.enrichment import Enrichment

        return Enrichment(self.vault).merge_into(filtered, consumer=consumer)

    def export(
        self,
        target: str,
        *,
        consumer: str = DEFAULT_EXPORT_CONSUMER,
        enrichment: bool = False,
    ) -> dict[str, Any]:
        if not self.graph_port:
            raise NotConfiguredError("Graph adapter is not configured")
        _validate_consumer(consumer)
        if target in INTEGRATION_TARGETS:
            # An integration's contents follow its own policy, and enrichment
            # is not part of one. Refuse rather than quietly ignore the flag:
            # silently dropping it would read as "these edges were included".
            if enrichment:
                raise ValidationError(
                    "Integration exports do not carry enrichment",
                    details={
                        "target": target,
                        "hint": "Export a file target, or read bk enrich list",
                    },
                )
            # An integration owns its boundary through its own policy. Refuse a
            # different consumer instead of silently widening or narrowing it.
            if consumer != DEFAULT_EXPORT_CONSUMER:
                raise ValidationError(
                    "Integration exports take their consumer from the policy",
                    details={
                        "target": target,
                        "consumer": consumer,
                        "hint": "Set it with integration configure --consumer",
                    },
                )
            return {
                **self.integration_sync(target),
                "consumer": self._integration_consumer(target),
            }
        suffix = EXPORT_SUFFIXES.get(target)
        if suffix is None:
            raise ValidationError("Unsupported export target", details={"target": target})
        graph = self.graph_data(consumer=consumer, enrichment=enrichment)
        payload = self.graph_port.export(graph, target)
        path = f"output/export-{target}.{suffix}"
        self.vault.write_generated(path, payload)
        result = {"target": target, "path": path, "consumer": consumer}
        if enrichment:
            result["enrichment_edges"] = graph.get("enrichment_edges", 0)
        return result

    def integration_configure(
        self,
        name: str,
        *,
        enabled: bool | None = None,
        managed: bool | None = None,
        options: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self._require_integrations().configure(
            name,
            enabled=enabled,
            managed=managed,
            options=options or {},
        )

    def integration_status(self, name: str | None = None) -> dict[str, Any]:
        return self._require_integrations().status(name)

    def integration_up(self, name: str) -> dict[str, Any]:
        if name == "obsidian":
            return self.integration_sync(name)
        return self._require_integrations().up(name)

    def integration_down(self, name: str) -> dict[str, Any]:
        return self._require_integrations().down(name)

    def integration_sync(self, name: str) -> dict[str, Any]:
        graph = self._integration_graph(name)
        if name == "obsidian":
            # Views are copied into the Obsidian vault, so they are generated
            # under the same boundary as the graph that ships with them.
            self.views(consumer=str(graph["consumer"]))
        return self._require_integrations().sync(name, graph)

    def _require_integrations(self) -> IntegrationPort:
        if not self.integration_port:
            raise NotConfiguredError("Integration adapters are not configured")
        return self.integration_port

    def _build_graph(self) -> dict[str, Any]:
        if not self.graph_port:
            raise NotConfiguredError("Graph adapter is not configured")
        return self.graph_port.build(self.vault)

    def _integration_consumer(self, name: str) -> str:
        config = self.vault.config()
        policy = config.integrations.get(name)
        if policy is None:
            raise ValidationError(
                "Unknown integration", details={"integration": name}
            )
        consumer = str(policy.options.get("consumer", ""))
        if not consumer:
            # Obsidian writes to a directory the operator already owns, so an
            # unset consumer falls back to the export default instead of failing.
            if name == "obsidian":
                return DEFAULT_EXPORT_CONSUMER
            raise NotConfiguredError(
                "Integration privacy consumer is not configured",
                details={
                    "integration": name,
                    "hint": "Set consumer to local or cloud before syncing",
                },
            )
        _validate_consumer(consumer)
        return consumer

    def _integration_graph(self, name: str) -> dict[str, Any]:
        return self.graph_data(consumer=self._integration_consumer(name))
