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
from pathlib import PurePosixPath
from typing import Any

from brainskit.application.filing import Filing
from brainskit.application.freshness import FreshnessLedger
from brainskit.application.health import Health
from brainskit.application.judgment import JudgmentRunner
from brainskit.application.ports import VaultPort
from brainskit.application.privacy import PrivacyBoundary, for_consumer
from brainskit.application.retrieval import Retrieval
from brainskit.domain.model import (
    BrainskitError,
    PolicyError,
    utc_now,
)
from brainskit.domain.privacy import (
    context_branches,
    record_branch,
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


_CITED_SOURCE_RE = re.compile(r"source:([0-9a-f]{64})")


def _hashes_allowed(boundary: PrivacyBoundary, hashes: list[Any]) -> bool:
    """Whether every hash names a source the boundary lets through.

    A hash the registry no longer resolves is unknown provenance, and unknown
    provenance is withheld -- the same answer `_evidence_privacy` gives.
    """

    for content_hash in hashes:
        record = boundary.records.get(str(content_hash))
        if record is None or not boundary.allows_record(record):
            return False
    return True


def _branch_allowed(boundary: PrivacyBoundary, branch: str) -> bool:
    return boundary.allows_path(PurePosixPath("raw", branch))


def _status_within(
    boundary: PrivacyBoundary, status: dict[str, Any]
) -> dict[str, Any]:
    """`bk status` output without what the boundary withholds.

    Totals stay: a count is what a redacted source is allowed to contribute.
    A branch name is not, and neither is an installation fact (ADR 0009) --
    the vault root, a hook script's path, a hint or detail that names one.
    """

    by_branch = status.get("by_branch") or {}
    reduced = {
        **status,
        "by_branch": {
            branch: count
            for branch, count in by_branch.items()
            if _branch_allowed(boundary, branch)
        },
    }
    return dict(_without_installation_facts(boundary, reduced))


# An absolute path anywhere in a string: POSIX (at least two segments, so a
# lone `/` in prose does not count), home-relative, or a Windows drive.
_ABSOLUTE_PATH_RE = re.compile(
    r"(?:^|[\s'\"(=:])(?:/[^\s/'\"]+/|~/|[A-Za-z]:[\\/])"
)


def _without_installation_facts(boundary: PrivacyBoundary, value: Any) -> Any:
    """`value` with every path-bearing string put to `installation_facts`.

    Walked rather than listed key by key, because the status payload grows:
    a key added later that carries a path is withheld without anyone having
    to remember this function exists.
    """

    if isinstance(value, dict):
        kept: dict[Any, Any] = {}
        for key, item in value.items():
            if isinstance(item, str) and _ABSOLUTE_PATH_RE.search(item):
                kept.update(boundary.installation_facts(**{str(key): item}))
            else:
                kept[key] = _without_installation_facts(boundary, item)
        return kept
    if isinstance(value, list):
        return [
            _without_installation_facts(boundary, item)
            for item in value
            if not (isinstance(item, str) and _ABSOLUTE_PATH_RE.search(item))
            or boundary.installation_facts(item=item)
        ]
    return value


def _freshness_within(
    boundary: PrivacyBoundary, state: dict[str, Any]
) -> tuple[dict[str, Any], int]:
    """The freshness state minus every page the boundary withholds.

    An entry goes when its page's provenance is withheld or unresolvable, when
    it records a source hash that is, or when its review reason names one (a
    capture marks related pages with the capture's own hash, and that capture
    may since have been filed into a never-ingest branch). A page that can no
    longer be read cannot be judged, so it goes too.
    """

    pages = state.get("pages")
    if not isinstance(pages, dict):
        return dict(state), 0
    kept: dict[str, Any] = {}
    for path, entry in pages.items():
        details = entry if isinstance(entry, dict) else {}
        hashes = list(details.get("source_hashes") or [])
        hashes += _CITED_SOURCE_RE.findall(str(details.get("review_reason") or ""))
        try:
            allowed = boundary.allows_path(
                PurePosixPath(str(path))
            ) and _hashes_allowed(boundary, hashes)
        except (BrainskitError, OSError):
            allowed = False
        if allowed:
            kept[path] = entry
    return {**state, "pages": kept}, len(pages) - len(kept)


def _proposals_within(
    boundary: PrivacyBoundary, payload: dict[str, Any]
) -> tuple[dict[str, Any], int]:
    """Filing proposals whose source, destination and cited hashes may pass.

    A proposal carries its apply payload -- page bodies compiled from the
    source -- so one for a source since filed into a never-ingest branch is
    that source's content by another route.
    """

    proposals = payload.get("proposals") or []
    kept = []
    for proposal in proposals:
        apply_payload = proposal.get("apply_proposal") or {}
        hashes = [proposal.get("source_hash")]
        for operation in apply_payload.get("operations") or []:
            hashes += list(operation.get("source_hashes") or [])
        try:
            allowed = _branch_allowed(
                boundary, str(proposal.get("destination_branch", ""))
            ) and _hashes_allowed(boundary, hashes)
        except BrainskitError:
            allowed = False
        if allowed:
            kept.append(proposal)
    return {"count": len(kept), "proposals": kept}, len(proposals) - len(kept)


class Jobs:
    """Operator-facing judgment jobs that write to `output/`, never `wiki/`."""

    def __init__(
        self,
        vault: VaultPort,
        retrieval: Retrieval,
        judgment_runner: JudgmentRunner,
        health: Health,
        filing: Filing,
        ledger: FreshnessLedger,
    ):
        self.vault = vault
        self.retrieval = retrieval
        self.judgment_runner = judgment_runner
        self.health = health
        self.filing = filing
        self.ledger = ledger

    def _judgment_context(
        self, query: str, **kwargs: Any
    ) -> tuple[dict[str, Any], int]:
        """Evidence a model may read for `query`, and how much was withheld.

        Read under the `local` boundary because it excludes exactly
        `never-ingest` -- the set the judgment router refuses outright. Read
        as `human`, one such match anywhere in BM25 recall made the router
        refuse the whole question; narrowing here leaves that refusal as the
        last line of defence rather than the first. The withheld side is a
        count only: its path would name the document and its branch.
        """

        context = self.retrieval.context(
            query, consumer="local", include_apply_contract=False, **kwargs
        )
        withheld = int(context["redacted"])
        if withheld and not context["evidence"]:
            raise PolicyError(
                "Every source matching this request is withheld from models "
                "by its branch privacy policy",
                details={
                    "withheld_sources": withheld,
                    "hint": (
                        "Rephrase toward material a model may read, or read the "
                        "withheld evidence yourself with bk search -- it is "
                        "withheld from models, not from you"
                    ),
                },
            )
        return context, withheld

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
        context, withheld = self._judgment_context(question)
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
        # Asked after `run` succeeded, so it names the route that answered;
        # None only for a substitute port that cannot say.
        route = self.judgment_runner.route_for(job="query", branches=branches)
        provider = route.provider if route else None
        model = route.model if route else None
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
            "withheld_sources": withheld,
        }

    def digest(self, since: str = "7d") -> dict[str, Any]:
        status = self.health.status()
        recent = sorted(
            self.vault.registry().values(),
            key=lambda item: item.captured_at,
            reverse=True,
        )[:50]
        # `local` is the boundary that excludes exactly never-ingest evidence,
        # and the sources it keeps decide the route, as they always have.
        local = for_consumer("local", self.vault)
        routed = [record for record in recent if local.allows_record(record)]
        digest_branches = sorted({record_branch(record) for record in routed})
        if not digest_branches:
            digest_branches = ["_inbox"]
        # The metadata is then held to the boundary of the model it will
        # actually reach, as the router itself reports it: a branch name, a
        # page path, a source hash and an absolute path are each disclosure in
        # their own right. A port that cannot say is assumed to be cloud.
        route = self.judgment_runner.route_for(job="digest", branches=digest_branches)
        boundary = (
            local if route is not None and route.local
            else for_consumer("cloud", self.vault)
        )
        allowed_recent = [
            record for record in routed if boundary.allows_record(record)
        ]
        freshness, withheld_pages = _freshness_within(
            boundary, self.ledger.snapshot().state
        )
        proposals, withheld_proposals = _proposals_within(
            boundary, self.filing.proposals()
        )
        withheld = (
            len(recent) - len(allowed_recent) + withheld_pages + withheld_proposals
        )
        digest_payload = self.judgment_runner.run(
            job="digest",
            branches=digest_branches,
            variables={
                "since": since,
                "status": json.dumps(
                    _status_within(boundary, status), ensure_ascii=False
                ),
                "sources": json.dumps(
                    [item.to_dict() for item in allowed_recent], ensure_ascii=False
                ),
                "proposals": json.dumps(proposals, ensure_ascii=False),
                "freshness": json.dumps(freshness, ensure_ascii=False),
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
            "withheld_sources": withheld,
        }

    def resurface(self) -> dict[str, Any]:
        # `resurface` only ever reads (see `ask`, above, for why the apply
        # contract stays off).
        context, withheld = self._judgment_context(
            "durable insight worth revisiting", limit=20
        )
        result = self.judgment_runner.run(
            job="resurface",
            branches=context_branches(context),
            variables={"context": json.dumps(context, ensure_ascii=False)},
        )
        path = f"output/resurface/{utc_now()[:10]}.md"
        self.vault.write_generated(path, str(result["markdown"]).rstrip() + "\n")
        # An annotation, not a freshness verdict -- see `record_resurfaced`,
        # which also refuses to invent an entry for a page that is not there.
        self.ledger.record_resurfaced(str(result["page"]))
        return {**result, "path": path, "withheld_sources": withheld}
