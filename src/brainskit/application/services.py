from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from brainskit.application.capture import Ingestion
from brainskit.application.codegraph import CodeGraph
from brainskit.application.compilation import ApplyGate
from brainskit.application.doctor import doctor_report
from brainskit.application.enrichment import Enrichment
from brainskit.application.filing import Filing
from brainskit.application.freshness import FreshnessLedger
from brainskit.application.gate import check_write
from brainskit.application.health import Health
from brainskit.application.installer import install_agent
from brainskit.application.jobs import Jobs
from brainskit.application.judgment import JudgmentRunner
from brainskit.application.pages import (
    FRONTMATTER_BOUNDARY as FRONTMATTER_BOUNDARY,
)
from brainskit.application.pages import (
    GENERATED_MARKER as GENERATED_MARKER,
)
from brainskit.application.pages import (
    page_metadata as page_metadata,
)
from brainskit.application.pages import (
    parse_frontmatter as parse_frontmatter,
)
from brainskit.application.pages import (
    render_page as render_page,
)
from brainskit.application.ports import (
    CodeExtractorPort,
    EnvironmentPort,
    GraphPort,
    IntegrationPort,
    JobSpecPort,
    JudgmentPort,
    SearchIndexPort,
    VaultPort,
)
from brainskit.application.projections import DEFAULT_EXPORT_CONSUMER, Projections
from brainskit.application.reader import Reader
from brainskit.application.retrieval import Retrieval
from brainskit.domain.model import (
    ScanSurvey,
    normalize_branch,
)

INTEGRATION_TARGETS = ("obsidian", "neo4j", "postgres")
EXPORT_SUFFIXES = {
    "json": "json",
    "graphml": "graphml",
    "cypher": "cypher",
    "neo4j": "cypher",
    "kuzu": "cypher",
    "llms-txt": "txt",
}



class BrainskitService:
    """Application facade. It coordinates ports without owning infrastructure."""

    def __init__(
        self,
        vault: VaultPort,
        index: SearchIndexPort,
        *,
        judgment: JudgmentPort | None = None,
        jobs: JobSpecPort | None = None,
        graph: GraphPort | None = None,
        integrations: IntegrationPort | None = None,
        extractor: CodeExtractorPort | None = None,
    ):
        self.vault = vault
        self.index = index
        # Built once, here, and handed to every collaborator that reads or
        # writes freshness. Letting each one construct its own would put five
        # partially-configured owners back on a file whose invariants only hold
        # when one object states them -- the shape ADR 0002 exists to remove.
        self.ledger = FreshnessLedger(vault)
        self.ingestion = Ingestion(vault, index, self.ledger)
        self.gate = ApplyGate(vault, index, self.ledger)
        self.retrieval = Retrieval(vault, index)
        self.judgment_runner = JudgmentRunner(jobs, judgment)
        self.health = Health(
            vault, index, self.retrieval, self.judgment_runner, self.ledger
        )
        self.filing = Filing(
            vault, index, self.gate, self.retrieval, self.judgment_runner
        )
        self.projections = Projections(
            vault, self.health, self.ledger, graph, integrations
        )
        self.code_graph = CodeGraph(vault, extractor)
        self.enrichment = Enrichment(vault, graph)
        self.reader = Reader(
            vault, index, self.health, self.filing, self.projections, self.ledger
        )
        self.jobs_runner = Jobs(
            vault,
            self.retrieval,
            self.judgment_runner,
            self.health,
            self.filing,
            self.ledger,
        )
        self.judgment = judgment
        self.jobs = jobs
        self.graph_port = graph
        self.integration_port = integrations

    def gate_check_write(self, target: str, *, agent: str = "claude") -> dict[str, Any]:
        """Decide whether a direct write to ``target`` is allowed by the gate."""
        return check_write(self.vault.root, target, agent=agent).to_dict()

    def capture(
        self, source: str | None, *, text: str | None = None, title: str | None = None
    ) -> dict[str, Any]:
        """Register one source and mark what it relates to. See `Ingestion`."""
        return self.ingestion.capture(source, text=text, title=title)

    def watch_once(self) -> dict[str, Any]:
        """Sweep the configured source folders. See `Ingestion.watch_once`."""
        return self.ingestion.watch_once()

    def reconcile(self) -> dict[str, Any]:
        # The port returns counters; widen here instead of loosening its type.
        result: dict[str, Any] = dict(self.vault.reconcile())
        result["indexed_documents"] = self.index.rebuild(self.vault)
        result["freshness_orphans"] = self.ingestion.drop_orphaned_freshness()
        return result

    def forget(self, identifier: str, *, force: bool = False) -> dict[str, Any]:
        record = self.vault.forget(identifier, force=force)
        self.index.rebuild(self.vault)
        return {
            "forgotten": record.to_dict(),
            "still_cited_by": self.ingestion.pages_citing(record.content_hash),
        }

    def file(self, identifier: str, branch: str) -> dict[str, Any]:
        record = self.vault.file_source(identifier, normalize_branch(branch))
        self.index.rebuild(self.vault)
        return {"source": record.to_dict()}

    def reindex(self) -> dict[str, Any]:
        return {"indexed_documents": self.index.rebuild(self.vault)}





    def views(self, *, consumer: str = "human") -> dict[str, Any]:
        """Regenerate the navigation views. See `Projections`."""
        return self.projections.views(consumer=consumer)

    def code_import(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Import an extractor's graph. See `CodeGraph`."""
        return self.code_graph.import_graph(payload)

    def code_build(self, paths: list[str] | None = None) -> dict[str, Any]:
        """Extract in-process and import the result. See `CodeGraph`."""
        scoped = [Path(path) for path in paths] if paths else None
        return self.code_graph.build(scoped)

    def enrich_apply(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Gate and store model-inferred edges. See `Enrichment`."""
        return self.enrichment.apply(payload)

    def enrich_list(self, *, consumer: str = "human") -> dict[str, Any]:
        """Stored enrichment this consumer may see."""
        edges = self.enrichment.edges(consumer=consumer)
        return {"consumer": consumer, "count": len(edges), "edges": edges}

    def enrich_forget(self, identifier: str) -> dict[str, Any]:
        return self.enrichment.forget(identifier)

    def code_survey(self, paths: list[str] | None = None) -> ScanSurvey | None:
        """What a build would cover, without building. See `CodeGraph.survey`."""
        scoped = [Path(path) for path in paths] if paths else None
        return self.code_graph.survey(scoped)

    def code_status(self) -> dict[str, Any]:
        """Whether the code graph still describes the repository."""
        return self.code_graph.staleness()

    def code_affected(
        self, symbol: str, *, depth: int = 2, consumer: str = "local"
    ) -> dict[str, Any]:
        """Reverse traversal: what breaks if this changes."""
        return self.code_graph.affected(symbol, depth=depth, consumer=consumer)

    def code_path(
        self, source: str, target: str, *, consumer: str = "local"
    ) -> dict[str, Any]:
        """Shortest chain of edges between two symbols."""
        return self.code_graph.path(source, target, consumer=consumer)

    def code_hubs(self, *, top: int = 10, consumer: str = "local") -> dict[str, Any]:
        """The most connected symbols."""
        return self.code_graph.hubs(top=top, consumer=consumer)

    def code_communities(
        self, *, resolution: float = 1.0, consumer: str = "local"
    ) -> dict[str, Any]:
        """Structurally cohesive clusters in the graph."""
        return self.code_graph.communities(resolution=resolution, consumer=consumer)

    def code_cycles(
        self, *, max_length: int = 5, top: int = 20, consumer: str = "local"
    ) -> dict[str, Any]:
        """Import cycles among files."""
        return self.code_graph.cycles(max_length=max_length, top=top, consumer=consumer)

    def code_diff(
        self, against: dict[str, Any] | None = None, *, consumer: str = "local"
    ) -> dict[str, Any]:
        """Structural change between the stored graph and a second one."""
        return self.code_graph.diff(against, consumer=consumer)

    def graph(
        self, *, consumer: str = "local", html: bool = False
    ) -> dict[str, Any]:
        """Regenerate the knowledge graph. See `Projections`."""
        return self.projections.graph(consumer=consumer, html=html)

    def graph_data(
        self,
        *,
        consumer: str = "human",
        enrichment: bool = False,
        limit: int = 0,
    ) -> dict[str, Any]:
        """The graph, filtered for a consumer and optionally bounded. See `Projections`."""
        return self.projections.graph_data(
            consumer=consumer, enrichment=enrichment, limit=limit
        )

    def code_graph_data(
        self, *, consumer: str = "local", limit: int = 1500
    ) -> dict[str, Any]:
        """The stored code graph, bounded for a viewer. See `CodeGraph`."""
        return self.code_graph.data(consumer=consumer, limit=limit)

    def export(
        self,
        target: str,
        *,
        consumer: str = DEFAULT_EXPORT_CONSUMER,
        enrichment: bool = False,
    ) -> dict[str, Any]:
        """Write the graph to a target, carrying the boundary. See `Projections`."""
        return self.projections.export(
            target, consumer=consumer, enrichment=enrichment
        )

    def integration_configure(
        self,
        name: str,
        *,
        enabled: bool | None = None,
        managed: bool | None = None,
        options: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Store an integration's policy. See `Projections`."""
        return self.projections.integration_configure(
            name, enabled=enabled, managed=managed, options=options
        )

    def integration_status(
        self, name: str | None = None, *, consumer: str = "human"
    ) -> dict[str, Any]:
        """Durable policy plus live process state. See `Projections`."""
        return self.projections.integration_status(name, consumer=consumer)

    def integration_up(self, name: str) -> dict[str, Any]:
        """Start a managed integration. See `Projections`."""
        return self.projections.integration_up(name)

    def integration_down(self, name: str) -> dict[str, Any]:
        """Stop a managed integration. See `Projections`."""
        return self.projections.integration_down(name)

    def integration_sync(self, name: str) -> dict[str, Any]:
        """Push the graph to an integration. See `Projections`."""
        return self.projections.integration_sync(name)

    def ask(
        self,
        question: str,
        *,
        save: bool = False,
        history: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Answer from compiled evidence. See `Jobs`."""
        return self.jobs_runner.ask(question, save=save, history=history)

    def digest(self, since: str = "7d") -> dict[str, Any]:
        """Generate the configured digest. See `Jobs`."""
        return self.jobs_runner.digest(since)

    def resurface(self) -> dict[str, Any]:
        """Surface one durable insight. See `Jobs`."""
        return self.jobs_runner.resurface()

    def reader_status(self, *, consumer: str = "human") -> dict[str, Any]:
        """Vault status scoped to a consumer. See `Reader`."""
        return self.reader.reader_status(consumer=consumer)

    def browse_sources(self, *, consumer: str = "human", limit: int = 500) -> dict[str, Any]:
        """Raw sources this consumer may see. See `Reader`."""
        return self.reader.browse_sources(consumer=consumer, limit=limit)

    def browse_pages(self, *, consumer: str = "human", limit: int = 500) -> dict[str, Any]:
        """Compiled pages this consumer may see. See `Reader`."""
        return self.reader.browse_pages(consumer=consumer, limit=limit)

    def timeline(self, *, consumer: str = "human", limit: int = 500) -> dict[str, Any]:
        """Ingestion chronology for this consumer. See `Reader`."""
        return self.reader.timeline(consumer=consumer, limit=limit)

    def read_resource(self, identifier: str, *, consumer: str = "human") -> dict[str, Any]:
        """One source or page, if this consumer may see it. See `Reader`."""
        return self.reader.read_resource(identifier, consumer=consumer)

    def proposals_for_consumer(
        self, status: str | None = None, *, consumer: str = "human"
    ) -> dict[str, Any]:
        """The review queue scoped to a consumer. See `Reader`."""
        return self.reader.proposals_for_consumer(status, consumer=consumer)

    def ingest(
        self,
        identifier: str | None = None,
        *,
        all_pending: bool = False,
        target_branch: str | None = None,
    ) -> dict[str, Any]:
        """Propose a branch and a wiki proposal per source. See `Filing`."""
        return self.filing.ingest(
            identifier, all_pending=all_pending, target_branch=target_branch
        )

    def proposals(self, status: str | None = None) -> dict[str, Any]:
        """The filing review queue. See `Filing`."""
        return self.filing.proposals(status)

    def approve(self, proposal_id: str) -> dict[str, Any]:
        """Execute a stored filing proposal. See `Filing`."""
        return self.filing.approve(proposal_id)

    def reject(self, proposal_id: str, reason: str = "") -> dict[str, Any]:
        """Decline a stored filing proposal. See `Filing`."""
        return self.filing.reject(proposal_id, reason)

    def lint(self, *, semantic: bool = False) -> dict[str, Any]:
        """Validate the vault's structural contracts. See `Health`."""
        return self.health.lint(semantic=semantic)

    def status(self) -> dict[str, Any]:
        """Vault health, counts and enforcement state. See `Health`."""
        return self.health.status()

    def install_agent(
        self, agent: str, *, root: str | None = None, force: bool = False
    ) -> dict[str, Any]:
        """Write an agent's install into `root`. See `installer.install_agent`."""
        return install_agent(self.vault, agent, root=root, force=force)

    def doctor(
        self,
        *,
        environment: EnvironmentPort,
        grammars: Mapping[str, bool],
        grammar_versions: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Whether this installation still enforces. See `doctor_report`."""
        return doctor_report(
            self.vault,
            self.health.enforcement(),
            environment=environment,
            grammars=grammars,
            grammar_versions=grammar_versions,
        )

    def search(
        self, query: str, limit: int = 10, *, consumer: str = "human"
    ) -> dict[str, Any]:
        """BM25 search inside the consumer's boundary. See `Retrieval`."""
        return self.retrieval.search(query, limit, consumer=consumer)

    def context(
        self,
        query: str,
        *,
        limit: int = 8,
        max_chars: int = 24_000,
        consumer: str = "human",
    ) -> dict[str, Any]:
        """The bounded evidence bundle an agent compiles from. See `Retrieval`."""
        return self.retrieval.context(
            query, limit=limit, max_chars=max_chars, consumer=consumer
        )

    def apply(self, raw_proposal: dict[str, Any]) -> dict[str, Any]:
        """Validate and commit a batch of wiki writes. See `ApplyGate`."""
        return self.gate.apply(raw_proposal)
