"""The judgment jobs an operator runs on purpose: ask, digest, resurface.

Each one gathers its own evidence, hands it to the shared repair loop, and
writes the result to `output/`. They are grouped by that shape rather than by
subject matter -- what they have in common is that a model proposes and the
engine only ever stores schema-valid output.

None of them writes to `wiki/`. That is the apply gate's job, and keeping these
away from it is what makes "a model cannot write the wiki" a structural fact
rather than a convention.
"""

from __future__ import annotations

import json
import re
from typing import Any

from brainskit.application.filing import Filing
from brainskit.application.health import Health
from brainskit.application.judgment import JudgmentRunner
from brainskit.application.ports import VaultPort
from brainskit.application.privacy import (
    _context_branches,
    _privacy_for_record,
    _record_branch,
)
from brainskit.application.retrieval import Retrieval
from brainskit.domain.model import PrivacyMode, utc_now


class Jobs:
    """Operator-facing judgment jobs that write to `output/`, never `wiki/`."""

    def __init__(
        self,
        vault: VaultPort,
        retrieval: Retrieval,
        judgment_runner: JudgmentRunner,
        health: Health,
        filing: Filing,
    ):
        self.vault = vault
        self.retrieval = retrieval
        self.judgment_runner = judgment_runner
        self.health = health
        self.filing = filing


    def ask(self, question: str, *, save: bool = False) -> dict[str, Any]:
        # `ask` only ever reads; the apply-proposal shape belongs to callers
        # about to write one (see `Retrieval.context`).
        context = self.retrieval.context(question, include_apply_contract=False)
        response = self.judgment_runner.run(
            job="query",
            branches=_context_branches(context),
            variables={
                "question": question,
                "context": json.dumps(context, ensure_ascii=False),
            },
        )
        answer = str(response["answer"])
        path: str | None = None
        if save:
            slug = re.sub(r"[^a-z0-9]+", "-", question.lower()).strip("-")[:60]
            slug = slug or "answer"
            path = f"output/answers/{utc_now()[:10]}-{slug}.md"
            self.vault.write_generated(
                path, f"# {question}\n\n{answer.rstrip()}\n"
            )
        return {
            "question": question,
            "answer": answer,
            "citations": response["citations"],
            "uncertainty": response["uncertainty"],
            "saved_to": path,
        }

    def digest(self, since: str = "7d") -> dict[str, Any]:
        status = self.health.status()
        recent = sorted(
            self.vault.registry().values(),
            key=lambda item: item.captured_at,
            reverse=True,
        )[:50]
        config = self.vault.config()
        allowed_recent = [
            record
            for record in recent
            if _privacy_for_record(config, record) != PrivacyMode.NEVER_INGEST
        ]
        digest_branches = sorted({_record_branch(record) for record in allowed_recent})
        if not digest_branches:
            digest_branches = ["_inbox"]
        digest_payload = self.judgment_runner.run(
            job="digest",
            branches=digest_branches,
            variables={
                "since": since,
                "status": json.dumps(status, ensure_ascii=False),
                "sources": json.dumps(
                    [item.to_dict() for item in allowed_recent], ensure_ascii=False
                ),
                "proposals": json.dumps(
                    self.filing.proposals(), ensure_ascii=False
                ),
                "freshness": json.dumps(
                    self.vault.read_state("freshness"), ensure_ascii=False
                ),
            },
        )
        digest = str(digest_payload["markdown"])
        path = f"output/digests/{utc_now()[:10]}.md"
        self.vault.write_generated(path, digest.rstrip() + "\n")
        return {
            "digest": digest,
            "actions": digest_payload["actions"],
            "resurfaced": digest_payload["resurfaced"],
            "path": path,
        }

    def resurface(self) -> dict[str, Any]:
        freshness = self.vault.read_state("freshness")
        # `resurface` only ever reads (see `ask`, above, for why this stays off).
        context = self.retrieval.context(
            "durable insight worth revisiting", limit=20, include_apply_contract=False
        )
        result = self.judgment_runner.run(
            job="resurface",
            branches=_context_branches(context),
            variables={"context": json.dumps(context, ensure_ascii=False)},
        )
        path = f"output/resurface/{utc_now()[:10]}.md"
        self.vault.write_generated(path, str(result["markdown"]).rstrip() + "\n")
        page = str(result["page"])

        def mutate(state: dict[str, Any]) -> dict[str, Any]:
            pages = state.setdefault("pages", {})
            entry = pages.setdefault(page, {})
            entry["last_resurfaced_at"] = utc_now()
            return state

        if page in freshness.get("pages", {}) or page in self.vault.wiki_pages():
            self.vault.mutate_state("freshness", mutate)
        return {**result, "path": path}
