"""The apply gate: the only path by which a wiki page is written.

Everything here exists to make one guarantee checkable -- that a page in
`wiki/` was produced by a validated proposal and nothing else. The whole batch
is validated before a single page is staged, because a partially applied
proposal is indistinguishable, afterwards, from a hand edit.

This is a leaf of the application layer: it needs the vault and the index and
nothing else, which is what lets `apply` and the filing workflow share it
without either one owning it.
"""

from __future__ import annotations

import hashlib
from pathlib import PurePosixPath
from typing import Any

from brainkit.application.pages import (
    _content_tokens,
    _normalize_identity,
    _stable_payload_hash,
    page_metadata,
    parse_frontmatter,
    render_page,
)
from brainkit.application.ports import SearchIndexPort, VaultPort
from brainkit.domain.model import (
    CITATION_RE,
    CODE_CITATION_RE,
    ApplyProposal,
    PageOperation,
    ValidationError,
    utc_now,
    validate_schema,
)


class ApplyGate:
    """Validates and commits a proposal as one recoverable unit of work."""

    def __init__(self, vault: VaultPort, index: SearchIndexPort):
        self.vault = vault
        self.index = index


    def apply(self, raw_proposal: dict[str, Any]) -> dict[str, Any]:
        return self.commit(raw_proposal)

    def commit(
        self,
        raw_proposal: dict[str, Any],
        *,
        raw_move: tuple[str, str] | None = None,
    ) -> dict[str, Any]:
        proposal = ApplyProposal.from_dict(raw_proposal)
        request_hash = _stable_payload_hash(proposal.to_dict())
        proposal_id = proposal.proposal_id or request_hash
        prior = self.vault.read_state("applied").get("proposals", {}).get(proposal_id)
        if isinstance(prior, dict):
            if prior.get("request_hash") != request_hash:
                raise ValidationError(
                    "proposal_id was already used for a different payload",
                    details={"proposal_id": proposal_id},
                )
            return {
                "applied": len(prior.get("paths", [])),
                "paths": prior.get("paths", []),
                "transaction_id": prior.get("transaction_id"),
                "proposal_id": proposal_id,
                "idempotent": True,
                "indexed_documents": self.index.stats()["documents"],
            }
        failures, pages, expected_versions, referenced_hashes = (
            self._prepare_apply(proposal)
        )
        if failures:
            raise ValidationError(
                "Apply proposal rejected; no files were written",
                details={"failures": failures},
            )
        transaction = self.vault.commit_wiki_batch(
            pages,
            expected_versions,
            dict.fromkeys(referenced_hashes, "ingested"),
            proposal_id,
            request_hash,
            self._freshness_updates(proposal.operations, pages),
            raw_move,
            lambda records: self.index.apply_snapshot(
                self.vault,
                records,
                sorted(pages),
                raw_move[0] if raw_move else None,
            ),
        )
        return {
            "applied": len(pages),
            "paths": sorted(pages),
            "transaction_id": transaction["transaction_id"],
            "proposal_id": proposal_id,
            "idempotent": transaction["idempotent"],
            "indexed_documents": transaction["indexed_documents"],
            "raw_move": transaction.get("raw_move"),
        }

    def validation_failures(self, proposal: ApplyProposal) -> list[dict[str, Any]]:
        """Why this proposal would be rejected, without writing anything.

        Filing needs to store a proposal that cannot be applied yet and say why.
        Exposing the question keeps that caller off `_prepare_apply`, whose
        other three return values only mean something to a commit.
        """
        failures, _, _, _ = self._prepare_apply(proposal)
        return failures

    def _prepare_apply(
        self, proposal: ApplyProposal
    ) -> tuple[
        list[dict[str, Any]],
        dict[str, str],
        dict[str, str | None],
        set[str],
    ]:
        records = self.vault.registry()
        known_hashes = set(records)
        existing_slugs = self.vault.existing_wiki_slugs()
        proposed_slugs = {operation.slug for operation in proposal.operations}
        catalog = self._wiki_catalog()
        failures: list[dict[str, Any]] = []
        pages: dict[str, str] = {}
        expected_versions: dict[str, str | None] = {}
        referenced_hashes: set[str] = set()
        for operation in proposal.operations:
            failures.extend(
                self._validate_operation(
                    operation, known_hashes, existing_slugs | proposed_slugs
                )
            )
            failures.extend(self._validate_novelty(operation, catalog))
            failures.extend(
                validate_schema(page_metadata(operation), self.vault.schema())
            )
            observed_version = self.vault.wiki_version(operation.relative_path)
            if observed_version is not None and operation.base_hash is None:
                failures.append(
                    {
                        "path": operation.relative_path,
                        "code": "missing_base_hash",
                        "observed": observed_version,
                    }
                )
            elif operation.base_hash != observed_version:
                failures.append(
                    {
                        "path": operation.relative_path,
                        "code": "stale_page",
                        "expected": operation.base_hash,
                        "observed": observed_version,
                    }
                )
            expected_versions[operation.relative_path] = operation.base_hash
            pages[operation.relative_path] = render_page(operation)
            referenced_hashes.update(operation.source_hashes)
            catalog.append(
                {
                    "path": operation.relative_path,
                    "type": operation.kind.value,
                    "title": operation.title,
                    "aliases": list(operation.aliases),
                    "body": operation.body,
                }
            )
        return failures, pages, expected_versions, referenced_hashes

    def _validate_operation(
        self,
        operation: PageOperation,
        known_hashes: set[str],
        known_slugs: set[str],
    ) -> list[dict[str, Any]]:
        failures: list[dict[str, Any]] = []
        sources = set(operation.source_hashes)
        unknown = sorted(sources - known_hashes)
        if unknown:
            failures.append(
                {"path": operation.relative_path, "code": "unknown_sources", "values": unknown}
            )
        citations = set(CITATION_RE.findall(operation.body))
        if citations != sources:
            failures.append(
                {
                    "path": operation.relative_path,
                    "code": "citation_mismatch",
                    "missing_citations": sorted(sources - citations),
                    "undeclared_citations": sorted(citations - sources),
                }
            )
        # Same rule for code, and for the same reason: a declared source nobody
        # cites is a claim of provenance the body does not make, and a citation
        # nobody declared cannot be re-verified because nothing records where the
        # file lives.
        code_declared = {entry.content_hash for entry in operation.code_sources}
        code_cited = set(CODE_CITATION_RE.findall(operation.body))
        if code_cited != code_declared:
            failures.append(
                {
                    "path": operation.relative_path,
                    "code": "code_citation_mismatch",
                    "missing_citations": sorted(code_declared - code_cited),
                    "undeclared_citations": sorted(code_cited - code_declared),
                }
            )
        unresolved = sorted(
            link
            for link in operation.links
            if PurePosixPath(link).name not in known_slugs
        )
        if unresolved:
            failures.append(
                {
                    "path": operation.relative_path,
                    "code": "unresolved_links",
                    "values": unresolved,
                }
            )
        return failures

    def _validate_novelty(
        self,
        operation: PageOperation,
        catalog: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        failures: list[dict[str, Any]] = []
        policy = self.vault.config().novelty
        proposed_names = {
            _normalize_identity(value)
            for value in (operation.title, *operation.aliases)
            if _normalize_identity(value)
        }
        proposed_tokens = _content_tokens(operation.body)
        for existing in catalog:
            if existing["path"] == operation.relative_path:
                continue
            existing_names = {
                _normalize_identity(value)
                for value in (existing["title"], *existing["aliases"])
                if _normalize_identity(value)
            }
            overlap = sorted(proposed_names & existing_names)
            if overlap:
                failures.append(
                    {
                        "path": operation.relative_path,
                        "code": "duplicate_identity",
                        "existing_path": existing["path"],
                        "values": overlap,
                    }
                )
                continue
            if existing["type"] != operation.kind.value:
                continue
            existing_tokens = _content_tokens(str(existing["body"]))
            union = proposed_tokens | existing_tokens
            similarity = (
                len(proposed_tokens & existing_tokens) / len(union) if union else 1.0
            )
            new_ratio = (
                len(proposed_tokens - existing_tokens) / len(proposed_tokens)
                if proposed_tokens
                else 0.0
            )
            if (
                similarity >= policy.duplicate_similarity_threshold
                and new_ratio < policy.min_new_token_ratio
            ):
                failures.append(
                    {
                        "path": operation.relative_path,
                        "code": "insufficient_novelty",
                        "existing_path": existing["path"],
                        "similarity": round(similarity, 4),
                        "new_token_ratio": round(new_ratio, 4),
                    }
                )
        return failures

    def _wiki_catalog(self) -> list[dict[str, Any]]:
        catalog: list[dict[str, Any]] = []
        for path in self.vault.wiki_pages():
            content = self.vault.read_text(path)
            metadata, body = parse_frontmatter(content)
            if metadata.get("type") == "system":
                continue
            catalog.append(
                {
                    "path": path,
                    "type": metadata.get("type"),
                    "title": str(metadata.get("title", PurePosixPath(path).stem)),
                    "aliases": [
                        str(value)
                        for value in metadata.get("aliases", [])
                        if isinstance(value, str)
                    ],
                    "body": body,
                }
            )
        return catalog

    def _freshness_updates(
        self,
        operations: tuple[PageOperation, ...],
        pages: dict[str, str],
    ) -> dict[str, dict[str, Any]]:
        now = utc_now()
        return {
            operation.relative_path: {
                "status": "fresh",
                "updated_at": now,
                "content_hash": hashlib.sha256(
                    pages[operation.relative_path].encode("utf-8")
                ).hexdigest(),
                "source_hashes": list(operation.source_hashes),
                "review_reason": None,
            }
            for operation in operations
        }
