"""Model-inferred edges: what is admissible, and what they may never do.

Enrichment is the one place a model's own assertion is stored as structure
rather than as a cited page, so every invariant it could plausibly break is
pinned here:

- it is refused unless it names the evidence behind it, because the privacy
  filter decides by the branch a *source record* lives in;
- it inherits the **strictest** policy across those sources, the rule the
  judgment router already applies to evidence spanning branches;
- it never enters `graph/graph.json`, which `bk graph` rewrites from the wiki
  on every build and would destroy it;
- and it can never be the thing that reintroduces a node the consumer was
  filtered away from — the same reason the graph filter runs after expansion.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from test_projections import policy as _base_policy

from brainkit.application.enrichment import Enrichment
from brainkit.application.services import BrainkitService
from brainkit.domain.model import (
    EnrichmentEdge,
    NotFoundError,
    PrivacyMode,
    ValidationError,
)
from brainkit.infrastructure.graph import MarkdownGraph
from brainkit.infrastructure.index import SqliteFtsIndex
from brainkit.infrastructure.vault import FileVault


def policy() -> dict:
    """Two branches at opposite ends of the privacy scale."""

    base = _base_policy()
    base["branches"] = {
        "10-open": {"privacy": "cloud", "filing": "approve-each"},
        "90-private": {"privacy": "never-ingest", "filing": "approve-each"},
    }
    base["taxonomy_seed"] = ["10-open", "90-private"]
    return base


class EnrichmentTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.vault = FileVault.initialize(
            Path(self._temp.name).resolve() / "vault", policy()
        )
        self.service = BrainkitService(
            self.vault, SqliteFtsIndex(self.vault.index_path), graph=MarkdownGraph()
        )
        self.open_hash = self._capture("A public note.", "open", "10-open")
        self.private_hash = self._capture("Confidential.", "secret", "90-private")
        self.service.graph()
        self.nodes = [
            str(node["id"])
            for node in json.loads(
                (self.vault.root / "graph" / "graph.json").read_text()
            )["nodes"]
            if str(node["id"]).startswith("raw:")
        ]

    def _capture(self, text: str, title: str, branch: str) -> str:
        captured = self.service.capture(None, text=text, title=title)
        content_hash = captured["source"]["content_hash"]
        self.service.file(content_hash, branch)
        return content_hash

    def _edge(self, **overrides: object) -> dict:
        return {
            "source": self.nodes[0],
            "target": self.nodes[1],
            "relation": "relates_to",
            "derived_from": [self.open_hash],
            "model": "qwen2.5:3b",
            **overrides,
        }


class TheGateTest(EnrichmentTestCase):
    def test_an_edge_without_provenance_is_refused(self) -> None:
        # The load-bearing rule. Without sources there is no branch, so there
        # is no answer to "may this consumer see it" — and storing it anyway
        # would put an unclassifiable edge inside a filtered graph.
        with self.assertRaises(ValidationError) as caught:
            self.service.enrich_apply({"edges": [self._edge(derived_from=[])]})
        self.assertIn("derived from", str(caught.exception))

    def test_an_edge_citing_unknown_evidence_is_refused(self) -> None:
        with self.assertRaises(ValidationError) as caught:
            self.service.enrich_apply({"edges": [self._edge(derived_from=["0" * 64])]})
        self.assertEqual(caught.exception.details["unresolved"], ["0" * 64])

    def test_an_edge_with_a_dangling_endpoint_is_refused(self) -> None:
        # The same rule `bk apply` enforces for unresolved `[[wiki-links]]`:
        # a graph with no dangling edges is a structural property, not a habit.
        with self.assertRaises(ValidationError) as caught:
            self.service.enrich_apply({"edges": [self._edge(target="page:nope.md")]})
        self.assertEqual(caught.exception.details["unknown"], ["page:nope.md"])

    def test_a_self_loop_is_refused(self) -> None:
        with self.assertRaises(ValidationError):
            self.service.enrich_apply({"edges": [self._edge(target=self.nodes[0])]})

    def test_an_empty_batch_is_refused(self) -> None:
        with self.assertRaises(ValidationError):
            self.service.enrich_apply({"edges": []})

    def test_nothing_is_stored_when_one_edge_in_the_batch_fails(self) -> None:
        # Whole-batch validation, for the reason `bk apply` uses it: a partial
        # application leaves the operator working out which half landed.
        with self.assertRaises(ValidationError):
            self.service.enrich_apply(
                {"edges": [self._edge(), self._edge(derived_from=["0" * 64])]}
            )
        self.assertEqual(self.service.enrich_list()["count"], 0)

    def test_a_well_formed_edge_is_stored_and_marked(self) -> None:
        result = self.service.enrich_apply({"edges": [self._edge()]})
        self.assertEqual(result["stored"], 1)
        stored = self.service.enrich_list()["edges"][0]
        # A reader must never have to work out which edges were extracted and
        # which were argued for.
        self.assertEqual(stored["provenance"], "model")
        self.assertEqual(stored["model"], "qwen2.5:3b")

    def test_reapplying_the_same_relationship_is_idempotent(self) -> None:
        self.service.enrich_apply({"edges": [self._edge()]})
        again = self.service.enrich_apply({"edges": [self._edge(model="other")]})
        self.assertEqual(again["total"], 1, "an agent re-running must not inflate")

    def test_identity_is_the_triple_not_the_evidence(self) -> None:
        first = EnrichmentEdge.from_dict(self._edge())
        second = EnrichmentEdge.from_dict(
            self._edge(derived_from=[self.private_hash], model="different")
        )
        self.assertEqual(first.id, second.id)
        third = EnrichmentEdge.from_dict(self._edge(relation="supersedes"))
        self.assertNotEqual(first.id, third.id)


class ThePrivacyBoundaryTest(EnrichmentTestCase):
    def test_an_edge_inherits_the_strictest_source_it_was_drawn_from(self) -> None:
        self.service.enrich_apply(
            {
                "edges": [
                    self._edge(derived_from=[self.open_hash, self.private_hash]),
                ]
            }
        )
        self.assertEqual(self.service.enrich_list(consumer="human")["count"], 1)
        # One `never-ingest` source is enough to withhold the whole edge.
        self.assertEqual(self.service.enrich_list(consumer="local")["count"], 0)
        self.assertEqual(self.service.enrich_list(consumer="cloud")["count"], 0)

    def test_an_open_edge_reaches_every_consumer(self) -> None:
        self.service.enrich_apply({"edges": [self._edge()]})
        for consumer in ("human", "local", "cloud"):
            with self.subTest(consumer=consumer):
                self.assertEqual(
                    self.service.enrich_list(consumer=consumer)["count"], 1
                )

    def test_an_edge_whose_evidence_is_gone_fails_closed(self) -> None:
        # The safe answer and the convenient answer differ here, so this pins
        # which one was chosen: unresolvable provenance is `never-ingest`, not
        # unrestricted.
        enrichment = Enrichment(self.vault, MarkdownGraph())
        privacy = enrichment.privacy_of(
            {"derived_from": ["0" * 64]}, self.vault.registry(), self.vault.config()
        )
        self.assertEqual(privacy, PrivacyMode.NEVER_INGEST)

    def test_an_inferred_edge_cannot_reintroduce_a_filtered_node(self) -> None:
        # The reason the graph filter runs *after* expansion, applied to the
        # one kind of edge that did not exist when that rule was written.
        self.service.enrich_apply(
            {"edges": [self._edge(derived_from=[self.open_hash])]}
        )
        enrichment = Enrichment(self.vault, MarkdownGraph())
        for consumer in ("human", "local", "cloud"):
            with self.subTest(consumer=consumer):
                data = self.service.projections.graph_data(consumer=consumer)
                merged = enrichment.merge_into(data, consumer=consumer)
                allowed = {str(node["id"]) for node in merged["nodes"]}
                for edge in merged["edges"]:
                    if edge.get("provenance") == "model":
                        self.assertIn(edge["source"], allowed)
                        self.assertIn(edge["target"], allowed)


class TheProjectionStaysDerivedTest(EnrichmentTestCase):
    def test_enrichment_never_enters_the_written_graph(self) -> None:
        # `bk graph` rewrites this file from the wiki every time, so an edge
        # written into it is destroyed on the next build. Keeping it out is
        # what makes enrichment survive a rebuild at all.
        self.service.enrich_apply({"edges": [self._edge(relation="argued_edge")]})
        self.service.graph()
        written = (self.vault.root / "graph" / "graph.json").read_text()
        self.assertNotIn("argued_edge", written)
        self.assertNotIn("provenance", written)

    def test_enrichment_survives_a_graph_rebuild(self) -> None:
        self.service.enrich_apply({"edges": [self._edge()]})
        self.service.graph()
        self.assertEqual(self.service.enrich_list()["count"], 1)

    def test_derived_edges_are_labelled_when_merged(self) -> None:
        self.service.enrich_apply({"edges": [self._edge()]})
        enrichment = Enrichment(self.vault, MarkdownGraph())
        merged = enrichment.merge_into(
            self.service.projections.graph_data(consumer="human"), consumer="human"
        )
        provenances = {edge.get("provenance") for edge in merged["edges"]}
        self.assertTrue(provenances <= {"derived", "model"})
        self.assertIn("model", provenances)


class ReadTimeJoinTest(EnrichmentTestCase):
    """The join has to be reachable, and off unless it is asked for.

    `merge_into` was correct, tested, and called by nothing outside the tests
    — so every documented consumer of "joined at read time" got a graph with
    no enrichment in it, and no way to ask for one.
    """

    def test_graph_data_omits_enrichment_by_default(self) -> None:
        self.service.enrich_apply({"edges": [self._edge(relation="argued_edge")]})
        data = self.service.projections.graph_data(consumer="human")
        self.assertNotIn("enrichment_edges", data)
        self.assertNotIn(
            "argued_edge", {str(edge.get("type")) for edge in data["edges"]}
        )

    def test_graph_data_joins_enrichment_when_asked(self) -> None:
        self.service.enrich_apply({"edges": [self._edge(relation="argued_edge")]})
        data = self.service.projections.graph_data(
            consumer="human", enrichment=True
        )
        self.assertEqual(data["enrichment_edges"], 1)
        self.assertIn("argued_edge", {str(edge.get("type")) for edge in data["edges"]})

    def test_export_carries_the_flag_through_to_the_written_file(self) -> None:
        self.service.enrich_apply({"edges": [self._edge(relation="argued_edge")]})

        plain = self.service.export("json", consumer="human")
        self.assertNotIn("enrichment_edges", plain)
        self.assertNotIn("argued_edge", (self.vault.root / plain["path"]).read_text())

        joined = self.service.export("json", consumer="human", enrichment=True)
        self.assertEqual(joined["enrichment_edges"], 1)
        self.assertIn("argued_edge", (self.vault.root / joined["path"]).read_text())

    def test_an_exported_edge_still_answers_to_the_consumer(self) -> None:
        """The flag asks for enrichment; it does not widen the boundary.

        This edge spans a `never-ingest` endpoint, so `local` drops it — and
        the export is the path where getting that wrong would write restricted
        evidence to a file on disk.
        """

        self.service.enrich_apply({"edges": [self._edge(relation="argued_edge")]})
        result = self.service.export("json", consumer="local", enrichment=True)
        self.assertEqual(result["enrichment_edges"], 0)
        self.assertNotIn("argued_edge", (self.vault.root / result["path"]).read_text())

    def test_an_integration_export_refuses_the_flag(self) -> None:
        # Silently dropping it would read as "these edges were included".
        with self.assertRaises(ValidationError):
            self.service.export("obsidian", enrichment=True)

    def test_the_cli_exposes_the_flag(self) -> None:
        # Wiring, not logic: a parameter no command can reach is the shape of
        # bug this class exists to catch.
        from brainkit.interfaces import cli

        args = cli.build_parser().parse_args(
            ["export", "--target", "json", "--enrichment"]
        )
        self.assertTrue(args.enrichment)
        self.assertFalse(
            cli.build_parser().parse_args(["export", "--target", "json"]).enrichment
        )


class LintAndForgetTest(EnrichmentTestCase):
    def test_lint_reports_an_edge_whose_evidence_was_forgotten(self) -> None:
        self.service.enrich_apply({"edges": [self._edge()]})
        self.service.forget(self.open_hash, force=True)
        codes = {finding["code"] for finding in self.service.lint()["findings"]}
        self.assertIn("enrichment.unresolved_source", codes)

    def test_a_healthy_vault_reports_no_enrichment_finding(self) -> None:
        self.service.enrich_apply({"edges": [self._edge()]})
        codes = {finding["code"] for finding in self.service.lint()["findings"]}
        self.assertNotIn("enrichment.unresolved_source", codes)

    def test_forget_accepts_an_id_prefix(self) -> None:
        stored = self.service.enrich_apply({"edges": [self._edge()]})["ids"][0]
        self.service.enrich_forget(stored[:6])
        self.assertEqual(self.service.enrich_list()["count"], 0)

    def test_forget_refuses_an_unknown_id(self) -> None:
        with self.assertRaises(NotFoundError):
            self.service.enrich_forget("nosuchedge")


if __name__ == "__main__":
    unittest.main()
