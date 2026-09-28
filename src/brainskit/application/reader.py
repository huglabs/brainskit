"""The read-only surface the web viewer is built on.

Every method here answers for a named consumer and returns only what that
consumer may see. The viewer never reads vault files directly, which is the
whole reason this module exists: if the browser could reach the filesystem, the
privacy boundary would be enforced in a template rather than in one place.

Nothing here writes. That is a property worth keeping, not an accident of the
current endpoints.
"""

from __future__ import annotations

import shlex
from collections import defaultdict
from pathlib import PurePath, PurePosixPath
from typing import Any

from brainskit.application.filing import Filing
from brainskit.application.freshness import FreshnessLedger
from brainskit.application.health import Health, enforcement_ok
from brainskit.application.pages import parse_frontmatter
from brainskit.application.ports import SearchIndexPort, VaultPort
from brainskit.application.privacy import PrivacyBoundary, for_consumer
from brainskit.application.projections import Projections
from brainskit.domain.model import (
    NotFoundError,
    PolicyError,
    SourceRecord,
    ValidationError,
)
from brainskit.domain.privacy import Consumer, record_branch


def _hint_without_paths(hint: str) -> str:
    """`hint` with every absolute path argument replaced by `<path>`.

    A reinstall hint names the workspace with `--root` when it is not the
    vault, and that is an installation fact (ADR 0009). The command stays
    useful without it: the reader learns what to run and that a root belongs
    there, not where this machine keeps it.
    """

    try:
        tokens = shlex.split(hint)
    except ValueError:
        return "bk hooks install"
    return " ".join(
        "<path>" if PurePath(token).is_absolute() or token.startswith("~") else token
        for token in tokens
    )


def _reportable_enforcement(
    enforcement: dict[str, Any], boundary: PrivacyBoundary
) -> dict[str, Any]:
    """The enforcement report with the machine-specific fields dropped.

    Enforcement is not evidence, so the consumer filter has nothing to say about
    whether a layer is on: a hook is installed or it is not, identically for
    whoever asks. Where it lives is another matter -- an installation fact,
    inside the boundary since ADR 0009 -- and the `vault` key beside this one is
    withheld from `cloud` for that reason. What is dropped here is dropped for
    every consumer, for minimality as much as for privacy. `detail`
    interpolates the workspace root and the redirected hooks directory, and
    `script` is an absolute path added for `bk doctor`, which opens the file;
    the viewer only has to name the layer that is off. A hook path names a local
    filesystem layout, and there is no reason to spend that on a caller with no
    use for it.

    `agent` is kept, on the other side of that line: it is present only when the
    vault is installed for more than one, and without it two identically named
    rows would render as one layer reported twice. It names an agent, not a
    machine.

    `outdated` and its `hint` are kept too: a stale session-status script still
    runs and misreports the vault, and dropping them here meant the viewer never
    said so. The hint is the one field that can carry an installation fact, so
    it goes through the boundary -- whole for `local` and `human`, path-free for
    `cloud`.
    """

    layers = []
    for layer in enforcement.get("layers", []):
        reported = {
            key: layer[key]
            for key in ("layer", "mechanism", "active", "advisory", "agent", "outdated")
            if key in layer
        }
        if "hint" in layer:
            hint = str(layer["hint"])
            reported["hint"] = boundary.installation_facts(hint=hint).get(
                "hint", _hint_without_paths(hint)
            )
        layers.append(reported)
    return {
        "gated": enforcement.get("gated", False),
        "inactive": list(enforcement.get("inactive", [])),
        "outdated": list(enforcement.get("outdated", [])),
        "layers": layers,
    }


class Reader:
    """Consumer-scoped reads for the viewer: status, browse, timeline, resource."""

    def __init__(
        self,
        vault: VaultPort,
        index: SearchIndexPort,
        health: Health,
        filing: Filing,
        projections: Projections,
        ledger: FreshnessLedger,
    ):
        self.vault = vault
        self.index = index
        self.health = health
        self.filing = filing
        self.projections = projections
        self.ledger = ledger

    def _visible(
        self, boundary: PrivacyBoundary, consumer: str
    ) -> tuple[dict[str, SourceRecord], int, set[str]]:
        """The sources and pages this consumer may see, and how many were not."""

        visible_records, redacted_sources = boundary.split_records()
        graph = self.projections.graph_data(consumer=consumer)
        visible_pages = {
            str(node["path"])
            for node in graph["nodes"]
            if str(node["id"]).startswith("page:")
        }
        return visible_records, redacted_sources, visible_pages

    def _visible_findings(
        self,
        findings: list[dict[str, Any]],
        records: dict[str, SourceRecord],
        pages: set[str],
    ) -> list[dict[str, Any]]:
        visible_paths = {*(record.path for record in records.values()), *pages}
        return [
            finding
            for finding in findings
            if not finding.get("path") or finding["path"] in visible_paths
        ]

    def reader_status(self, *, consumer: str = "human") -> dict[str, Any]:
        boundary = for_consumer(consumer, self.vault)
        if consumer == "human":
            return self.health.status()
        visible_records, redacted_sources, visible_pages = self._visible(
            boundary, consumer
        )
        visible_findings = self._visible_findings(
            self.health.lint()["findings"], visible_records, visible_pages
        )
        raw_counts: dict[str, int] = defaultdict(int)
        for record in visible_records.values():
            raw_counts[record_branch(record)] += 1
        freshness = self.ledger.snapshot()
        index_state = self.index.stats()
        enforcement = _reportable_enforcement(self.health.enforcement(), boundary)
        return {
            **boundary.installation_facts(vault=str(self.vault.root)),
            "sources": len(visible_records),
            "pending": sum(
                record.status == "pending" for record in visible_records.values()
            ),
            "wiki_pages": len(visible_pages),
            "by_branch": dict(sorted(raw_counts.items())),
            "index": {
                **index_state,
                "documents": len(visible_records) + len(visible_pages),
            },
            "freshness": freshness.summary(present=visible_pages),
            "enforcement": enforcement,
            # The same two inputs `Health.status` weighs, and for the same
            # reason: this feeds `/api/status`, which the viewer renders as
            # "healthy" or "needs attention". Computed from lint alone, it told
            # an operator the vault was healthy while the write gate protecting
            # it was off -- the divergence `bk status` was fixed for, one layer
            # up, on the surface with no enforcement rows underneath to
            # contradict it.
            #
            # The lint half stays consumer-scoped while the enforcement half
            # does not, and the asymmetry is the point. Findings are evidence:
            # letting one on a redacted page flip this would let restricted
            # content decide a filtered consumer's answer, and `lint_errors`
            # below would then read 0 beside it. Enforcement is not evidence --
            # it describes the installation, which is the same for every
            # consumer asking.
            "healthy": not any(
                finding["severity"] == "error" for finding in visible_findings
            )
            and enforcement_ok(enforcement),
            "lint_errors": sum(
                finding["severity"] == "error" for finding in visible_findings
            ),
            "consumer": consumer,
            "redacted_sources": redacted_sources,
            "redacted_pages": len(self.vault.wiki_pages()) - len(visible_pages),
        }

    def browse_sources(
        self, *, consumer: str = "human", limit: int = 500
    ) -> dict[str, Any]:
        boundary = for_consumer(consumer, self.vault)
        if not 1 <= limit <= 1_000:
            raise ValidationError("Source browse limit must be between 1 and 1000")
        records = sorted(
            boundary.records.values(),
            key=lambda record: record.captured_at,
            reverse=True,
        )
        visible = []
        redacted = 0
        for record in records:
            privacy = boundary.record_privacy(record)
            if not boundary.allows(privacy):
                redacted += 1
                continue
            visible.append(
                {
                    "id": f"raw:{record.content_hash}",
                    "content_hash": record.content_hash,
                    "path": record.path,
                    "title": record.original_name,
                    "branch": record_branch(record),
                    "privacy": privacy.value,
                    "status": record.status,
                    "captured_at": record.captured_at,
                    "size": record.size,
                    "media_type": record.media_type,
                }
            )
            if len(visible) >= limit:
                break
        return {
            "consumer": consumer,
            "count": len(visible),
            "redacted": redacted,
            "sources": visible,
        }

    def browse_pages(
        self, *, consumer: str = "human", limit: int = 500
    ) -> dict[str, Any]:
        boundary = for_consumer(consumer, self.vault)
        if not 1 <= limit <= 1_000:
            raise ValidationError("Page browse limit must be between 1 and 1000")
        freshness = self.ledger.snapshot()
        graph = self.projections.graph_data(consumer=consumer)
        pages = []
        for node in graph["nodes"]:
            if not str(node["id"]).startswith("page:"):
                continue
            path = str(node["path"])
            privacy = boundary.evidence_privacy(node)
            pages.append(
                {
                    **node,
                    "privacy": privacy.value,
                    "freshness": freshness.status(path),
                    "updated_at": freshness.updated_at(path),
                }
            )
        pages.sort(key=lambda page: (str(page["kind"]), str(page["label"])))
        return {
            "consumer": consumer,
            "count": min(len(pages), limit),
            "redacted": graph["redacted_nodes"],
            "pages": pages[:limit],
        }

    def timeline(self, *, consumer: str = "human", limit: int = 500) -> dict[str, Any]:
        # Validation by construction: an unknown consumer fails here, at this
        # method's own boundary, rather than transitively inside a callee.
        for_consumer(consumer, self.vault)
        sources = self.browse_sources(consumer=consumer, limit=limit)
        pages = self.browse_pages(consumer=consumer, limit=limit)
        events = [
            {
                "at": source["captured_at"],
                "type": "captured",
                "id": source["id"],
                "title": source["title"],
                "detail": source["branch"],
            }
            for source in sources["sources"]
        ]
        events.extend(
            {
                "at": page["updated_at"],
                "type": "compiled",
                "id": page["id"],
                "title": page["label"],
                "detail": page["freshness"],
            }
            for page in pages["pages"]
            if page["updated_at"]
        )
        events.sort(key=lambda event: str(event["at"]), reverse=True)
        return {
            "consumer": consumer,
            "count": min(len(events), limit),
            "events": events[:limit],
        }

    def read_resource(
        self, identifier: str, *, consumer: str = "human"
    ) -> dict[str, Any]:
        boundary = for_consumer(consumer, self.vault)
        if identifier.startswith("raw:"):
            content_hash = identifier.removeprefix("raw:")
            record = boundary.records.get(content_hash)
            if not record:
                raise NotFoundError(
                    "Raw graph resource was not found",
                    details={"identifier": identifier},
                )
            privacy = boundary.record_privacy(record)
            if not boundary.allows(privacy):
                raise PolicyError(
                    "Resource is outside the consumer privacy boundary",
                    details={"privacy": privacy.value, "consumer": consumer},
                )
            return {
                "id": identifier,
                "path": record.path,
                "kind": "raw",
                "title": record.original_name,
                "privacy": privacy.value,
                "content": self.vault.raw_text(record),
            }
        path = identifier.removeprefix("page:")
        if path not in self.vault.wiki_pages():
            raise NotFoundError(
                "Wiki graph resource was not found", details={"identifier": identifier}
            )
        content = self.vault.read_text(path)
        privacy = boundary.evidence_privacy({"path": path}, content)
        if not boundary.allows(privacy):
            raise PolicyError(
                "Resource is outside the consumer privacy boundary",
                details={"privacy": privacy.value, "consumer": consumer},
            )
        metadata, _ = parse_frontmatter(content)
        return {
            "id": f"page:{path}",
            "path": path,
            "kind": str(metadata.get("type", "wiki")),
            "title": str(metadata.get("title", PurePosixPath(path).stem)),
            "privacy": privacy.value,
            "content": content,
        }

    def lint_for_consumer(
        self, result: dict[str, Any], *, consumer: str = "human"
    ) -> dict[str, Any]:
        """A lint result with the findings on material `consumer` may not see removed.

        The same rule `reader_status` applies to its `lint_errors`: a finding
        names a path, and a path names a document and its branch. `ok` is
        recomputed from what is left, for the reason `healthy` is there --
        restricted content must not decide a filtered consumer's answer.
        """

        boundary = for_consumer(consumer, self.vault)
        if consumer == "human":
            return result
        records, _, pages = self._visible(boundary, consumer)
        findings = self._visible_findings(result["findings"], records, pages)
        return {
            **result,
            "ok": not any(finding["severity"] == "error" for finding in findings),
            "findings": findings,
            "consumer": consumer,
            "redacted_findings": len(result["findings"]) - len(findings),
        }

    def visible_source(self, identifier: str, *, consumer: str = "human") -> str:
        """The content hash `identifier` names among the sources `consumer` may see.

        A source outside the boundary is not found rather than forbidden: a
        hash prefix or a path is a guess, and answering "exists, but not for
        you" would turn every guess into a probe.
        """

        boundary = for_consumer(consumer, self.vault)
        records = (
            boundary.records
            if boundary.consumer is Consumer.HUMAN
            else boundary.split_records()[0]
        )
        return self.filing._resolve_record(dict(records), identifier).content_hash

    def require_proposal(self, proposal_id: str, *, consumer: str = "human") -> None:
        """Refuse a proposal id whose source `consumer` may not see, as not found."""

        visible = self.proposals_for_consumer(consumer=consumer)["proposals"]
        if not any(proposal.get("proposal_id") == proposal_id for proposal in visible):
            raise NotFoundError(
                "Filing proposal was not found",
                details={"proposal_id": proposal_id},
            )

    def proposals_for_consumer(
        self, status: str | None = None, *, consumer: str = "human"
    ) -> dict[str, Any]:
        boundary = for_consumer(consumer, self.vault)
        result = self.filing.proposals(status)
        if consumer == "human":
            return {**result, "consumer": consumer, "redacted": 0}
        visible = []
        for proposal in result["proposals"]:
            record = boundary.records.get(str(proposal.get("source_hash", "")))
            if record and boundary.allows_record(record):
                visible.append(proposal)
        return {
            "count": len(visible),
            "proposals": visible,
            "consumer": consumer,
            "redacted": result["count"] - len(visible),
        }
