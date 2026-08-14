"""Search and evidence bundles -- everything that reads the vault to answer.

Retrieval and the privacy boundary are inseparable here: filtering runs after
graph expansion, because an outgoing link or a backlink can reintroduce a
source the consumer was never allowed to see. Keeping both in one module means
there is no way to add an expansion step that forgets to re-filter.

A leaf: vault and index only.
"""

from __future__ import annotations

from pathlib import PurePosixPath
from typing import Any

from brainskit.application.pages import _focused_excerpt, parse_frontmatter
from brainskit.application.ports import SearchIndexPort, VaultPort
from brainskit.application.privacy import for_consumer
from brainskit.domain.model import WIKI_LINK_RE, ValidationError
from brainskit.domain.privacy import record_branch


class Retrieval:
    """BM25 search and the bounded evidence bundle built on top of it."""

    def __init__(self, vault: VaultPort, index: SearchIndexPort):
        self.vault = vault
        self.index = index

    def search(
        self, query: str, limit: int = 10, *, consumer: str = "human"
    ) -> dict[str, Any]:
        if not query.strip():
            raise ValidationError("Search query cannot be empty")
        if limit < 1:
            raise ValidationError("Search limit must be positive")
        boundary = for_consumer(consumer, self.vault)
        ranked: list[Any] = []
        redacted = 0
        privacy_by_path: dict[str, str] = {}
        candidate_limits = [min(100, limit)]
        expanded_limit = min(100, limit * 3)
        if expanded_limit > candidate_limits[0]:
            candidate_limits.append(expanded_limit)
        for candidate_limit in candidate_limits:
            ranked = []
            redacted = 0
            privacy_by_path = {}
            for hit in self.index.search(query, candidate_limit):
                if hit.content_hash and hit.content_hash in boundary.records:
                    privacy = boundary.record_privacy(
                        boundary.records[hit.content_hash]
                    )
                else:
                    content = self.vault.read_text(hit.path)
                    privacy = boundary.evidence_privacy(hit.to_dict(), content)
                if not boundary.allows(privacy):
                    redacted += 1
                    continue
                privacy_by_path[hit.path] = privacy.value
                ranked.append(hit)
                if len(ranked) >= limit:
                    break
            if len(ranked) >= limit or redacted == 0:
                break
        target_graph = max(1, limit // 4) if limit >= 4 else 0
        direct_limit = max(1, limit - target_graph) if target_graph else limit
        hits = ranked[:direct_limit]
        reserve = limit - len(hits)
        # A limit below 4 reserves no room for graph expansion, so there is
        # nothing to expand into. Without this guard the loop below appended a
        # neighbour *before* testing `len(expanded) >= reserve`, so a reserve of
        # zero still admitted exactly one -- and `search(limit=N)` returned N+1
        # for N in 1, 2, 3. It propagates into `context`, where `limit` is the
        # caller's bound on how much evidence reaches a model.
        expanded_candidates = (
            self._expand_link_neighbors(hits, 100) if reserve > 0 else []
        )
        expanded = []
        for hit in expanded_candidates:
            content = self.vault.read_text(hit.path)
            privacy = boundary.evidence_privacy(hit.to_dict(), content)
            if not boundary.allows(privacy):
                redacted += 1
                continue
            privacy_by_path[hit.path] = privacy.value
            expanded.append(hit)
            if len(expanded) >= reserve:
                break
        if len(expanded) < reserve:
            hits.extend(ranked[len(hits) : len(hits) + reserve - len(expanded)])
        return {
            "query": query,
            "consumer": consumer,
            "redacted": redacted,
            "count": len(hits) + len(expanded),
            "hits": [
                {
                    **hit.to_dict(),
                    "privacy": privacy_by_path.get(hit.path)
                    or boundary.evidence_privacy(hit.to_dict()).value,
                }
                for hit in (*hits, *expanded)
            ],
        }

    def context(
        self,
        query: str,
        *,
        limit: int = 8,
        max_chars: int = 24_000,
        consumer: str = "human",
        include_apply_contract: bool = True,
    ) -> dict[str, Any]:
        boundary = for_consumer(consumer, self.vault)
        evidence: list[dict[str, Any]] = []
        # A count, never a description. `context` is the payload handed to a
        # cloud model, and the path of a redacted source names the document and
        # the branch it lives in — disclosure in its own right.
        redacted = 0
        if query in boundary.records:
            record = boundary.records[query]
            privacy = boundary.record_privacy(record)
            if boundary.allows(privacy):
                evidence.append(
                    {
                        "citation": f"source:{record.content_hash}",
                        "path": record.path,
                        "kind": "raw",
                        "branches": [record_branch(record)],
                        "privacy": privacy.value,
                        "content": self.vault.raw_text(record, max_chars=max_chars),
                    }
                )
            else:
                redacted = 1
        else:
            result = self.search(query, limit, consumer=consumer)
            redacted = int(result["redacted"])
            remaining = max_chars
            for hit in result["hits"]:
                if remaining <= 0:
                    break
                content = self.vault.read_text(hit["path"])
                if hit.get("content_hash") and hit["content_hash"] in boundary.records:
                    content = self.vault.raw_text(boundary.records[hit["content_hash"]])
                excerpt = _focused_excerpt(content, query, min(4_000, remaining))
                remaining -= len(excerpt)
                branches = boundary.evidence_branches(hit, content)
                evidence.append(
                    {
                        "citation": (
                            f"source:{hit['content_hash']}"
                            if hit.get("content_hash")
                            else f"page:{hit['path']}"
                        ),
                        "path": hit["path"],
                        "kind": hit["kind"],
                        "branches": branches,
                        "privacy": hit["privacy"],
                        "version": (
                            self.vault.wiki_version(hit["path"])
                            if hit["kind"] != "raw"
                            else None
                        ),
                        "content": excerpt,
                    }
                )
        bundle: dict[str, Any] = {
            "contract_version": 1,
            "query": query,
            "consumer": consumer,
            "wiki_language": self.vault.config().wiki_language,
            "evidence": evidence,
            "redacted": redacted,
        }
        if include_apply_contract:
            # Instructions for a *caller* about to write a proposal, not
            # evidence about the vault's content. `bk context` (the CLI/MCP
            # tool an agent calls before `bk apply`) and the `ingest` job
            # (which produces exactly this shape) both need it in scope.
            # A job that only ever reads -- `ask`, `resurface`,
            # `lint-semantic` -- does not, and handing it one anyway is not
            # inert: with `[^source:<sha256>]`, `upsert`, `source_hashes` and
            # a schema sitting right next to the real evidence, a model asked
            # a question thin evidence does not answer reached for the most
            # structured, self-describing thing in the prompt and described
            # *brainskit's own* raw/wiki/apply mechanics back as if they were
            # the vault's content -- confirmed live against a 20k-source
            # vault whose actual evidence was an unrelated skill blurb.
            bundle["apply_contract"] = {
                "format": "brainskit.apply-proposal.v1",
                "citation": "[^source:<sha256>]",
                "operations": {
                    "action": "upsert",
                    "kind": "source|entity|concept|synthesis",
                    "slug": "lowercase-kebab-case",
                    "title": "string",
                    "aliases": ["string"],
                    "source_hashes": ["sha256"],
                    "body": "markdown with declared citations and links",
                    "links": ["target-slug"],
                    "metadata": {"human_schema_field": "value"},
                    "base_hash": "required current page hash when updating",
                },
            }
        return bundle

    def _expand_link_neighbors(self, hits: list[Any], remaining: int) -> list[Any]:
        if not hits or remaining <= 0:
            return []
        from brainskit.domain.model import SearchHit

        wiki_hits = [hit for hit in hits if hit.kind.startswith("wiki")]
        if not wiki_hits:
            return []
        known_paths = {hit.path for hit in hits}
        known_slugs = {PurePosixPath(hit.path).stem for hit in wiki_hits}
        outgoing_slugs: set[str] = set()
        for hit in wiki_hits:
            outgoing_slugs.update(
                PurePosixPath(value.strip()).name
                for value in WIKI_LINK_RE.findall(self.vault.read_text(hit.path))
            )
        expanded: list[SearchHit] = []
        for path in self.vault.wiki_pages():
            if path in known_paths:
                continue
            text = self.vault.read_text(path)
            links = {PurePosixPath(v.strip()).name for v in WIKI_LINK_RE.findall(text)}
            if PurePosixPath(path).stem not in outgoing_slugs and not (
                links & known_slugs
            ):
                continue
            metadata, body = parse_frontmatter(text)
            expanded.append(
                SearchHit(
                    path=path,
                    kind="wiki-neighbor",
                    title=str(metadata.get("title", PurePosixPath(path).stem)),
                    excerpt=_focused_excerpt(body, "", 300),
                    score=0.0,
                )
            )
            if len(expanded) >= remaining:
                break
        return expanded
