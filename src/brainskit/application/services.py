from __future__ import annotations

import os
from collections import Counter
from collections.abc import Iterator, Sequence
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlparse

from brainskit.application.codegraph import CodeGraph
from brainskit.application.compilation import ApplyGate
from brainskit.application.enrichment import Enrichment
from brainskit.application.filing import Filing
from brainskit.application.freshness import FreshnessLedger
from brainskit.application.gate import check_write
from brainskit.application.health import Health
from brainskit.application.jobs import Jobs
from brainskit.application.judgment import JudgmentRunner
from brainskit.application.pages import (
    FRONTMATTER_BOUNDARY as FRONTMATTER_BOUNDARY,
)
from brainskit.application.pages import (
    GENERATED_MARKER as GENERATED_MARKER,
)
from brainskit.application.pages import (
    _content_tokens,
    _is_salient_term,
    _normalized_tokens,
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
    BrainskitError,
    NotConfiguredError,
    ScanSurvey,
    SourceRecord,
    ValidationError,
    is_ignored,
    normalize_branch,
    resolve_source_path,
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
# Relatedness budget: enough terms to describe a capture, few enough to keep the
# FTS5 MATCH bounded on the capture hot path.
_RELATED_QUERY_TERMS = 12
_RELATED_CANDIDATES = 20
_RELATED_PAGE_LIMIT = 5
_RELATED_MIN_SHARED_TERMS = 2
_RELATED_TEXT_LIMIT = 20_000



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
        if text is not None:
            record, created = self.vault.capture_text(
                text, title or "captured-note", ".md"
            )
        elif source and _is_url(source):
            url_title = title or urlparse(source).netloc or "captured-link"
            body = f"# {url_title}\n\n{source}\n"
            record, created = self.vault.capture_text(body, url_title, ".md")
        elif source:
            record, created = self.vault.capture_file(Path(source))
        else:
            raise ValidationError("capture requires a source path, URL, or --text")
        self.index.upsert_raw(self.vault, record)
        self._mark_related_pages_for_review(record)
        return {"created": created, "source": record.to_dict()}

    def watch_once(self) -> dict[str, Any]:
        """Capture every eligible file under the configured source folders.

        Eligibility is a vault rule, not a caller's: `ignore` prunes whole
        trees, and the vault's own directory is always excluded so a watch
        pointed at a parent of the vault cannot re-capture `raw/` into itself.

        **Where a source resolves is a vault rule too**, so a relative path is
        read against the vault rather than the process's current directory —
        see `resolve_source_path`.

        A source that resolves to nothing is never passed over in silence,
        because a watch that captures nothing looks exactly like a watch with
        nothing to capture:

        - **Every configured source missing** is a configuration the operator
          has to fix, and no run can ever do anything until they do. It gets
          the same refusal, and so the same exit status, as configuring no
          sources at all — the state it amounts to.
        - **Some sources missing** still leaves real work, and a capture
          declined over a typo elsewhere in the policy would be worse than the
          typo. Those are reported alongside the per-file failures and the
          walk continues.
        """

        config = self.vault.config()
        if not config.sources:
            raise NotConfiguredError("No source folders/files are configured")
        present: list[Path] = []
        missing: list[tuple[str, Path]] = []
        for value in config.sources:
            root = resolve_source_path(self.vault.root, value)
            if root.exists():
                present.append(root)
            else:
                missing.append((value, root))
        if not present:
            raise NotConfiguredError(
                "No configured source folder/file exists",
                details={
                    "sources": [_missing_source(value, root) for value, root in missing],
                    "hint": (
                        "A relative source is resolved against the vault. Point "
                        "sources at paths that exist, or use absolute paths."
                    ),
                },
            )
        created = 0
        duplicates = 0
        ignored = 0
        failures: list[dict[str, str]] = [
            {
                "path": str(root),
                "error": _missing_source_error(_missing_source(value, root)),
            }
            for value, root in missing
        ]
        for root in present:
            for candidate, skipped in _walk_source(root, config.ignore, self.vault.root):
                ignored += skipped
                if candidate is None:
                    continue
                try:
                    result = self.capture(str(candidate))
                    created += int(result["created"])
                    duplicates += int(not result["created"])
                except (BrainskitError, OSError) as exc:
                    failures.append({"path": str(candidate), "error": str(exc)})
        return {
            "created": created,
            "duplicates": duplicates,
            "ignored": ignored,
            "failures": failures,
        }

    def reconcile(self) -> dict[str, Any]:
        # The port returns counters; widen here instead of loosening its type.
        result: dict[str, Any] = dict(self.vault.reconcile())
        result["indexed_documents"] = self.index.rebuild(self.vault)
        result["freshness_orphans"] = self._drop_orphaned_freshness()
        return result

    def _drop_orphaned_freshness(self) -> list[str]:
        """Heal freshness state after a wiki page is removed outside the gate.

        The registry is reconciled by content hash; freshness is keyed by path,
        so a deleted page leaves an entry that can never be revived.
        """
        present = set(self.vault.wiki_pages())
        dropped = self.ledger.snapshot().orphans(present)
        self.ledger.drop(dropped)
        return dropped

    def forget(self, identifier: str, *, force: bool = False) -> dict[str, Any]:
        record = self.vault.forget(identifier, force=force)
        self.index.rebuild(self.vault)
        return {
            "forgotten": record.to_dict(),
            "still_cited_by": self._pages_citing(record.content_hash),
        }

    def _pages_citing(self, content_hash: str) -> list[str]:
        """Wiki pages whose frontmatter still declares this source.

        Informational only: forgetting the source does not touch these pages,
        and the next `bk lint` reports them as `wiki.unknown_source` — this
        just surfaces that up front instead of leaving it to be discovered.
        """
        citing = []
        for path in self.vault.wiki_pages():
            metadata, _ = parse_frontmatter(self.vault.read_text(path))
            if content_hash in metadata.get("sources", []):
                citing.append(path)
        return citing

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






































    def _mark_related_pages_for_review(self, record: SourceRecord) -> None:
        terms = self._related_query_terms(record)
        if not terms:
            return
        candidates = [
            hit
            for hit in self.index.search(" ".join(terms), limit=_RELATED_CANDIDATES)
            if hit.kind == "wiki"
        ]
        if not candidates:
            return
        # BM25 orders the candidates but has no absolute scale, so a page only
        # counts as related when it actually shares the capture's vocabulary.
        required = min(_RELATED_MIN_SHARED_TERMS, len(terms))
        wanted = set(terms)
        related: list[str] = []
        for hit in candidates:
            shared = wanted & _content_tokens(self.vault.read_text(hit.path))
            if len(shared) < required:
                continue
            related.append(hit.path)
            if len(related) >= _RELATED_PAGE_LIMIT:
                break
        # `mark_reviewed` carries the never-downgrade rule, which this writer
        # used to lack: a page already `stale` stays `stale`, because `review`
        # is skipped by the ageing pass and would have parked it there.
        self.ledger.mark_reviewed(
            dict.fromkeys(related, f"related source:{record.content_hash}")
        )

    def _related_query_terms(self, record: SourceRecord) -> list[str]:
        """Describe a capture by its own text, not by the name it was saved under."""
        text = self.vault.raw_text(record, max_chars=_RELATED_TEXT_LIMIT)
        content = _normalized_tokens(text)
        body_terms = [
            term
            for term, _ in Counter(
                token for token in content if _is_salient_term(token)
            ).most_common(_RELATED_QUERY_TERMS)
        ]
        # The file name only contributes what the document itself corroborates,
        # so a suggestive name cannot stand in for unrelated content.
        seen = set(content)
        name_terms = [
            token
            for token in _normalized_tokens(
                PurePosixPath(record.original_name).stem.replace("-", " ")
            )
            if _is_salient_term(token) and token in seen
        ]
        terms = dict.fromkeys([*name_terms, *body_terms])
        return list(terms)[:_RELATED_QUERY_TERMS]












def _is_url(value: str) -> bool:
    return urlparse(value).scheme in {"http", "https"}


def _missing_source(value: str, root: Path) -> dict[str, str]:
    """Describe a configured source that points at nothing.

    The `found_at_cwd` case is the upgrade note, delivered to the one person
    it concerns rather than to a changelog they may not read. A relative
    source used to resolve against the process's current directory, so a
    vault whose owner always ran `bk watch` from the same place had a
    configuration that worked by coincidence — and repointing it silently
    would be its own defect. When the old location still holds the folder,
    the message names it, and the remedy is to write that path into `sources`.
    """

    detail = {"source": value, "resolved": str(root)}
    from_cwd = Path(value).expanduser().resolve()
    if from_cwd != root and from_cwd.exists():
        detail["found_at_cwd"] = str(from_cwd)
    return detail


def _missing_source_error(detail: dict[str, str]) -> str:
    """The same fact as prose, for the `failures` list a partial run returns."""

    message = (
        f"Configured source {detail['source']!r} resolves to "
        f"{detail['resolved']}, relative to the vault, and nothing is there"
    )
    if "found_at_cwd" in detail:
        message += (
            f" — it does exist at {detail['found_at_cwd']}, where this path "
            "resolved before sources became vault-relative"
        )
    return message


def _walk_source(
    root: Path, ignore: Sequence[str], vault_root: Path
) -> Iterator[tuple[Path | None, int]]:
    """Yield capturable files under ``root``, pruning ignored directories.

    Pruning matters as much as filtering. `os.walk` lets an ignored directory
    be dropped from the traversal, so `node_modules` costs one comparison
    instead of a stat call per file inside it — on a source folder holding a
    project, that is the difference between a watch tick and a stall.

    The second element of each pair is how many entries that step skipped, so
    the caller can report what it declined without the walker holding state.
    """

    if root.is_file():
        if is_ignored(root.name, ignore):
            yield None, 1
            return
        yield (None, 1) if _inside(root, vault_root) else (root, 0)
        return
    if not root.is_dir():
        return
    for current, directories, filenames in os.walk(root, followlinks=False):
        here = Path(current)
        if _inside(here, vault_root):
            # The vault's own directory is never a source. Pruning it here also
            # covers `raw/`, which a watch pointed at the vault's parent would
            # otherwise re-capture into itself on every tick.
            directories[:] = []
            continue
        kept = []
        for name in directories:
            if is_ignored(_relative(here / name, root), ignore):
                yield None, 1
            else:
                kept.append(name)
        directories[:] = kept
        for name in sorted(filenames):
            candidate = here / name
            if is_ignored(_relative(candidate, root), ignore):
                yield None, 1
            elif candidate.is_file():
                yield candidate, 0


def _relative(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.name


def _inside(path: Path, root: Path) -> bool:
    return path == root or root in path.parents






































# Function words carry no topical signal, and BM25 cannot be trusted to discount
# them in a vault whose corpus is still small.












