"""The filing workflow: propose a destination, then wait or execute.

Filing is the one place where a judgment call and an irreversible write meet,
so the order matters more than the code length suggests. A branch is proposed,
the apply payload is stored, and only then does the branch's own policy decide:
`auto+digest-review` executes immediately and keeps the audit record, while
`approve-each` stores the proposal and moves nothing -- a source that is filed
before a human decides cannot be un-filed.

Depends on the apply gate to commit and on the judgment runner to propose; it
owns neither.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from brainkit.application.compilation import ApplyGate
from brainkit.application.judgment import JudgmentRunner
from brainkit.application.ports import SearchIndexPort, VaultPort
from brainkit.application.privacy import _record_branch
from brainkit.application.retrieval import Retrieval
from brainkit.domain.model import (
    ApplyProposal,
    BrainkitError,
    FilingMode,
    FilingProposal,
    NotFoundError,
    ProposalStatus,
    SourceRecord,
    ValidationError,
    normalize_branch,
    utc_now,
)


class Filing:
    """Durable propose/approve/reject over raw sources awaiting a branch."""

    def __init__(
        self,
        vault: VaultPort,
        index: SearchIndexPort,
        gate: ApplyGate,
        retrieval: Retrieval,
        judgment_runner: JudgmentRunner,
    ):
        self.vault = vault
        self.index = index
        self.gate = gate
        self.retrieval = retrieval
        self.judgment_runner = judgment_runner


    def ingest(
        self,
        identifier: str | None = None,
        *,
        all_pending: bool = False,
        target_branch: str | None = None,
    ) -> dict[str, Any]:
        records = self.vault.registry()
        if all_pending:
            selected_hashes = [
                item.content_hash
                for item in records.values()
                if item.status == "pending"
            ]
        elif identifier:
            selected_hashes = [
                self._resolve_record(records, identifier).content_hash
            ]
        else:
            raise ValidationError("ingest requires a source identifier or --all")
        results: list[dict[str, Any]] = []
        for content_hash in selected_hashes:
            current_records = self.vault.registry()
            record = current_records[content_hash]
            if all_pending and record.status != "pending":
                continue
            branch = _record_branch(record)
            context = self.retrieval.context(record.content_hash)
            destination, filing_reason = self._resolve_filing_destination(
                record,
                context,
                target_branch=target_branch,
            )
            apply_payload = self.judgment_runner.run(
                job="ingest",
                branches=sorted({branch, destination}),
                variables={"context": json.dumps(context, ensure_ascii=False)},
                validator=self._apply_payload_failures,
            )
            apply_payload.setdefault("proposal_id", f"wiki-{uuid.uuid4().hex}")
            filing_policy = self.vault.config().branches[destination].filing
            filing_proposal = FilingProposal(
                proposal_id=f"filing-{uuid.uuid4().hex}",
                source_hash=record.content_hash,
                destination_branch=destination,
                filing_mode=filing_policy,
                apply_proposal=apply_payload,
                reason=filing_reason,
                status=ProposalStatus.PENDING,
                created_at=utc_now(),
            )
            self._save_filing_proposal(filing_proposal)
            if filing_policy == FilingMode.APPROVE_EACH:
                self._set_source_status(record.content_hash, "awaiting-approval")
                results.append(
                    {
                        "source": record.content_hash,
                        "proposal": filing_proposal.to_dict(),
                        "queued": True,
                    }
                )
                continue
            execution = self._execute_filing_proposal(filing_proposal)
            results.append(
                {
                    "source": record.content_hash,
                    "proposal": execution["proposal"],
                    "apply": execution["apply"],
                    "queued": False,
                }
            )
        return {"ingested": len(results), "results": results}

    def proposals(self, status: str | None = None) -> dict[str, Any]:
        selected_status: ProposalStatus | None = None
        if status:
            try:
                selected_status = ProposalStatus(status)
            except ValueError as exc:
                raise ValidationError(
                    "Unknown proposal status", details={"status": status}
                ) from exc
        stored = self.vault.read_state("proposals").get("proposals", {})
        proposals = [
            FilingProposal.from_dict(value)
            for value in stored.values()
            if isinstance(value, dict)
        ]
        if selected_status:
            proposals = [
                proposal
                for proposal in proposals
                if proposal.status == selected_status
            ]
        proposals.sort(key=lambda proposal: proposal.created_at, reverse=True)
        return {
            "count": len(proposals),
            "proposals": [proposal.to_dict() for proposal in proposals],
        }

    def approve(self, proposal_id: str) -> dict[str, Any]:
        proposal = self._get_filing_proposal(proposal_id)
        if proposal.status == ProposalStatus.APPLIED:
            return {
                "proposal": proposal.to_dict(),
                "apply": proposal.result,
                "idempotent": True,
            }
        if proposal.status == ProposalStatus.REJECTED:
            raise ValidationError(
                "Rejected proposal cannot be approved",
                details={"proposal_id": proposal_id},
            )
        return self._execute_filing_proposal(proposal)

    def reject(self, proposal_id: str, reason: str = "") -> dict[str, Any]:
        proposal = self._get_filing_proposal(proposal_id)
        if proposal.status == ProposalStatus.APPLIED:
            raise ValidationError(
                "Applied proposal cannot be rejected",
                details={"proposal_id": proposal_id},
            )
        updated = FilingProposal.from_dict(
            {
                **proposal.to_dict(),
                "status": ProposalStatus.REJECTED.value,
                "decided_at": utc_now(),
                "result": {"reason": reason},
            }
        )
        self._save_filing_proposal(updated)
        self._set_source_status(proposal.source_hash, "rejected")
        return {"proposal": updated.to_dict()}

    def _save_filing_proposal(self, proposal: FilingProposal) -> None:
        def mutate(state: dict[str, Any]) -> dict[str, Any]:
            state.setdefault("version", 1)
            state.setdefault("proposals", {})[proposal.proposal_id] = proposal.to_dict()
            return state

        self.vault.mutate_state("proposals", mutate)

    def _get_filing_proposal(self, proposal_id: str) -> FilingProposal:
        raw = (
            self.vault.read_state("proposals")
            .get("proposals", {})
            .get(proposal_id)
        )
        if not isinstance(raw, dict):
            raise NotFoundError(
                "Filing proposal was not found",
                details={"proposal_id": proposal_id},
            )
        return FilingProposal.from_dict(raw)

    def _execute_filing_proposal(
        self, proposal: FilingProposal
    ) -> dict[str, Any]:
        try:
            records = self.vault.registry()
            record = self._resolve_record(records, proposal.source_hash)
            move = (
                (proposal.source_hash, proposal.destination_branch)
                if _record_branch(record) != proposal.destination_branch
                else None
            )
            apply_result = self.gate.commit(
                proposal.apply_proposal,
                raw_move=move,
            )
            updated = FilingProposal.from_dict(
                {
                    **proposal.to_dict(),
                    "status": ProposalStatus.APPLIED.value,
                    "decided_at": utc_now(),
                    "result": apply_result,
                }
            )
            self._save_filing_proposal(updated)
            return {"proposal": updated.to_dict(), "apply": apply_result}
        except Exception as exc:
            failed = FilingProposal.from_dict(
                {
                    **proposal.to_dict(),
                    "status": ProposalStatus.FAILED.value,
                    "decided_at": utc_now(),
                    "result": {
                        "error": str(exc),
                        "code": getattr(exc, "code", "execution_error"),
                    },
                }
            )
            self._save_filing_proposal(failed)
            self._set_source_status(proposal.source_hash, "proposal-failed")
            self.index.rebuild(self.vault)
            raise

    def _resolve_filing_destination(
        self,
        record: SourceRecord,
        context: dict[str, Any],
        *,
        target_branch: str | None,
    ) -> tuple[str, str]:
        config = self.vault.config()
        current_branch = _record_branch(record)
        if target_branch:
            destination = normalize_branch(target_branch)
            if destination not in config.branches:
                raise ValidationError(
                    "Destination branch is not configured",
                    details={"branch": destination},
                )
            return destination, "Explicit destination supplied by the operator."
        if current_branch != "_inbox":
            if current_branch not in config.branches:
                raise ValidationError(
                    "Current source branch is not configured",
                    details={"branch": current_branch},
                )
            return current_branch, "Source was already filed before ingestion."
        result = self.judgment_runner.run(
            job="file-proposal",
            branches=["_inbox"],
            variables={
                "context": json.dumps(context, ensure_ascii=False),
                "branches": json.dumps(
                    {
                        branch: {
                            "privacy": policy.privacy.value,
                            "filing": policy.filing.value,
                        }
                        for branch, policy in config.branches.items()
                    },
                    ensure_ascii=False,
                ),
            },
        )
        destination = normalize_branch(str(result.get("branch", "")))
        if destination not in config.branches:
            raise ValidationError(
                "Filing provider proposed an unknown branch",
                details={"branch": destination},
            )
        return destination, str(result.get("reason", ""))

    def _set_source_status(self, content_hash: str, status: str) -> None:
        records = self.vault.registry()
        if content_hash not in records:
            raise NotFoundError(
                "Source was not found", details={"source_hash": content_hash}
            )
        records[content_hash].status = status
        self.vault.save_registry(records)

    @staticmethod
    def _resolve_record(
        records: dict[str, SourceRecord], identifier: str
    ) -> SourceRecord:
        if identifier in records:
            return records[identifier]
        matches = [
            record
            for content_hash, record in records.items()
            if content_hash.startswith(identifier) or record.path == identifier
        ]
        if len(matches) == 1:
            return matches[0]
        if not matches:
            raise NotFoundError("Source was not found", details={"identifier": identifier})
        raise ValidationError(
            "Source identifier is ambiguous", details={"identifier": identifier}
        )

    def _apply_payload_failures(
        self, raw_proposal: dict[str, Any]
    ) -> list[dict[str, Any]]:
        try:
            proposal = ApplyProposal.from_dict(raw_proposal)
        except BrainkitError as exc:
            return [
                {
                    "path": "$",
                    "code": exc.code,
                    "message": str(exc),
                    **exc.details,
                }
            ]
        failures = self.gate.validation_failures(proposal)
        return failures
