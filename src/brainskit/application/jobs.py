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
from brainskit.application.privacy import for_consumer
from brainskit.application.retrieval import Retrieval
from brainskit.domain.model import (
    PolicyError,
    PrivacyMode,
    VaultConfig,
    utc_now,
)
from brainskit.domain.privacy import (
    context_branches,
    record_branch,
    resolve_branch_policy,
)

#: Conversation bounds for `Jobs.ask`. History is model context only -- it
#: rides the prompt so pronouns and follow-ups resolve, and never reaches
#: retrieval -- so these are token budgets, not correctness limits.
MAX_HISTORY_EXCHANGES = 6
MAX_HISTORY_CHARS = 4_000


def _serialize_history(items: list[dict[str, str]]) -> str:
    """Compact transcript for the query prompt's conversation section."""

    if not items:
        return "(none)"
    return "\n\n".join(
        f"Q: {item['question']}\nA: {item['answer']}" for item in items
    )


def _bounded_history(
    history: list[dict[str, Any]] | None,
) -> list[dict[str, str]]:
    """The most recent exchanges, oldest first, within both named bounds.

    Trims whole exchanges oldest-first: the newest turns are what the current
    question's pronouns resolve against. When the one remaining exchange alone
    exceeds `MAX_HISTORY_CHARS`, the tail of its answer is cut instead of
    dropping the only context there is.
    """

    items = [
        {
            "question": str(item.get("question", "")),
            "answer": str(item.get("answer", "")),
        }
        for item in (history or [])
    ][-MAX_HISTORY_EXCHANGES:]
    while len(items) > 1 and len(_serialize_history(items)) > MAX_HISTORY_CHARS:
        items.pop(0)
    if items:
        excess = len(_serialize_history(items)) - MAX_HISTORY_CHARS
        if excess > 0:
            answer = items[0]["answer"]
            items[0]["answer"] = answer[: max(0, len(answer) - excess)]
    return items


def _resolved_query_route(
    config: VaultConfig, branches: list[str]
) -> tuple[str | None, str | None]:
    """The provider/model the judgment router selects for job="query".

    Mirrors the selection in `PolicyJudgmentRouter.run` (infrastructure/llm.py),
    which stays authoritative: this runs only after that call succeeded, so
    every state the router refuses -- an unknown branch, a never-ingest policy,
    a missing or malformed mapping -- has already raised there. A miss here
    therefore means the judgment port is a substitute (tests), and the honest
    answer is None, not a guess.
    """

    mapping = config.job_models.get("query")
    if not isinstance(mapping, dict):
        return None, None
    try:
        policies = [
            resolve_branch_policy(config, branch) for branch in (branches or ["_inbox"])
        ]
    except PolicyError:
        return None, None
    effective = (
        PrivacyMode.LOCAL_ONLY
        if any(policy.privacy == PrivacyMode.LOCAL_ONLY for policy in policies)
        else PrivacyMode.CLOUD
    )
    route = mapping.get(effective.value, mapping)
    if not isinstance(route, dict):
        return None, None
    provider = route.get("provider")
    model = route.get("model")
    return (
        provider if isinstance(provider, str) else None,
        model if isinstance(model, str) else None,
    )


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


    def ask(
        self,
        question: str,
        *,
        save: bool = False,
        history: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        # `ask` only ever reads; the apply-proposal shape belongs to callers
        # about to write one (see `Retrieval.context`).
        #
        # Retrieval stays keyed on the CURRENT question only, never the
        # conversation. Concatenating past exchanges into the retrieval query
        # would pollute BM25 term matching with every word already discussed,
        # burying the terms this question is actually about. History is model
        # context for *interpreting* the question, and rides only the prompt.
        context = self.retrieval.context(question, include_apply_contract=False)
        branches = context_branches(context)
        response = self.judgment_runner.run(
            job="query",
            branches=branches,
            variables={
                "question": question,
                "context": json.dumps(context, ensure_ascii=False),
                "history": _serialize_history(_bounded_history(history)),
            },
        )
        answer = str(response["answer"])
        provider, model = _resolved_query_route(self.vault.config(), branches)
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
            "provider": provider,
            "model": model,
        }

    def digest(self, since: str = "7d") -> dict[str, Any]:
        status = self.health.status()
        recent = sorted(
            self.vault.registry().values(),
            key=lambda item: item.captured_at,
            reverse=True,
        )[:50]
        # `local` is the boundary that excludes exactly never-ingest evidence.
        boundary = for_consumer("local", self.vault)
        allowed_recent = [record for record in recent if boundary.allows_record(record)]
        digest_branches = sorted({record_branch(record) for record in allowed_recent})
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
            branches=context_branches(context),
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
