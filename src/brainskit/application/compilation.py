"""The apply gate: the only path by which a wiki page is written.

Everything here exists to make one guarantee checkable -- that a page in
`wiki/` was produced by a validated proposal and nothing else. The whole batch
is validated before a single page is staged, because a partially applied
proposal is indistinguishable, afterwards, from a hand edit.

This is a leaf of the application layer: it needs the vault, the index and the
freshness ledger and nothing else, which is what lets `apply` and the filing
workflow share it without either one owning it. The ledger is here because an
apply is the one writer that records provenance -- see `mark_applied` for why
the entries it builds are committed by the transaction rather than by the
ledger itself.
"""

from __future__ import annotations

from pathlib import PurePosixPath
from typing import Any

from brainskit.application.freshness import FreshnessLedger
from brainskit.application.pages import (
    _content_tokens,
    _normalize_identity,
    _stable_payload_hash,
    page_metadata,
    parse_frontmatter,
    render_page,
)
from brainskit.application.ports import ApplyPlan, SearchIndexPort, VaultPort
from brainskit.application.schema import validate_schema
from brainskit.domain.model import (
    CITATION_RE,
    CODE_CITATION_RE,
    ApplyProposal,
    ConflictError,
    PageOperation,
    ValidationError,
    proposal_id_reuse_error,
)


class ApplyGate:
    """Validates and commits a proposal as one recoverable unit of work."""

    def __init__(
        self, vault: VaultPort, index: SearchIndexPort, ledger: FreshnessLedger
    ):
        self.vault = vault
        self.index = index
        self.ledger = ledger


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
                # Not a conflict: the binding below is durable, so re-reading
                # the vault returns this same refusal forever. See
                # `proposal_id_reuse_error`.
                raise proposal_id_reuse_error(
                    proposal_id,
                    applied_request_hash=prior.get("request_hash"),
                    request_hash=request_hash,
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
            # A rejection made *only* of version disagreements is the one an
            # agent can actually recover from on its own: re-read the pages,
            # rebuild the same proposal against what is there now, send it
            # again. Mixed with a content failure it is not -- re-reading will
            # not repair a citation -- so `conflict` is claimed only when every
            # failure is one, and the caller is told to change the request
            # otherwise.
            conflicting = {"stale_page", "missing_base_hash"}
            error = (
                ConflictError
                if all(failure.get("code") in conflicting for failure in failures)
                else ValidationError
            )
            raise error(
                "Apply proposal rejected; no files were written",
                details={"failures": failures},
            )
        transaction = self.vault.commit_wiki_batch(
            ApplyPlan(
                pages=pages,
                expected_versions=expected_versions,
                source_statuses=dict.fromkeys(referenced_hashes, "ingested"),
                proposal_id=proposal_id,
                request_hash=request_hash,
                freshness_updates=self.ledger.mark_applied(proposal.operations, pages),
                raw_move=raw_move,
                index_rebuild=lambda records: self.index.apply_snapshot(
                    self.vault,
                    records,
                    sorted(pages),
                    raw_move[0] if raw_move else None,
                ),
            )
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


    def _validate_slug_uniqueness(
        self, operation: Any, proposal: Any
    ) -> list[dict[str, Any]]:
        """A slug may exist under one page kind only.

        Pages live at `wiki/{kind}/{slug}.md`, and the graph resolves
        `[[link]]` by *stem alone* -- `slug_nodes[slug] = node_id`, last writer
        wins. So `concept:widget` and `entity:widget` produce two files with one
        stem, and every `[[widget]]` in the vault silently resolves to whichever
        directory sorts later. Deterministic, but arbitrary: adding a page-kind
        directory that sorts after the current ones would flip every such edge
        at once, and the losing page shows zero inbound links while its author
        believes it is connected.

        Refused at apply, because renaming is the remedy and the caller has the
        proposal in hand. Vaults already carrying a collision are reported by
        `bk lint` instead, so an existing one can be repaired rather than
        blocking every subsequent write.
        """

        target = operation.relative_path
        clashes = sorted(
            path
            for path in self.vault.wiki_pages()
            if PurePosixPath(path).stem == operation.slug and path != target
        )
        clashes += sorted(
            other.relative_path
            for other in proposal.operations
            if other.slug == operation.slug and other.relative_path != target
        )
        if not clashes:
            return []
        return [
            {
                "path": target,
                "code": "duplicate_slug",
                "slug": operation.slug,
                "conflicts_with": clashes,
                "hint": "Rename the slug; wiki links resolve by slug alone",
            }
        ]

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
            failures.extend(self._validate_slug_uniqueness(operation, proposal))
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
                        # `observed` *is* the value to send back, and the
                        # refusal never said so -- leaving a caller holding the
                        # answer without knowing it was the answer.
                        "hint": (
                            "Set base_hash to the observed value above and "
                            "retry; it is this page's current version"
                        ),
                    }
                )
            elif operation.base_hash != observed_version:
                failures.append(
                    {
                        "path": operation.relative_path,
                        "code": "stale_page",
                        "expected": operation.base_hash,
                        "observed": observed_version,
                        "hint": (
                            "The page moved on since base_hash was read. "
                            "Re-read it with bk context, then retry with the "
                            "observed value"
                        ),
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
        """Every page under `wiki/`, as the identities and bodies it occupies.

        There is no exemption, and that is the point. This used to skip any page
        whose own frontmatter said `type: "system"`, which let a page opt itself
        out of the duplicate check by writing four words into its own header --
        the same fault `wiki.outside_apply` was fixed for, where the file being
        checked decided whether it would be checked. A page hand-written under
        `wiki/` with `type: "system"` and a stolen title vanished from the
        catalog entirely, so `duplicate_identity` never fired against it.

        The seeded pages `bk init` writes are *not* exempted here, unlike in
        `SEEDED_SYSTEM_PAGES`, because the two lists answer different questions
        that only coincidentally agree today. That constant answers "which pages
        may exist with no entry in the freshness ledger" -- a provenance
        question, where init's pages are a genuine special case. This asks "which
        titles, aliases and bodies are already taken", and `wiki/index.md`
        genuinely takes the title "Brainskit index" and the slug `index`. A
        proposal claiming either is a duplicate, and refusing it is the check
        working rather than a false positive. Sharing one constant would couple
        them, so that seeding a third page would silently grant it a dedupe
        exemption nobody argued for -- which is this defect again, one release
        later.

        `_validate_novelty` still compares bodies only within a kind. That reads
        `type` off the page too, but it is a domain rule rather than an
        exemption: a `source` page and a `concept` page about the same evidence
        are different artefacts and comparing their prose means nothing. For a
        page `bk apply` wrote, that field is what apply itself wrote; for one it
        did not, `wiki.outside_apply` is the finding that says so.
        """

        catalog: list[dict[str, Any]] = []
        for path in self.vault.wiki_pages():
            content = self.vault.read_text(path)
            metadata, body = parse_frontmatter(content)
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
