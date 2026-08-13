"""The read-only surface the web viewer is built on.

Every method here answers for a named consumer and returns only what that
consumer may see. The viewer never reads vault files directly, which is the
whole reason this module exists: if the browser could reach the filesystem, the
privacy boundary would be enforced in a template rather than in one place.

Nothing here writes. That is a property worth keeping, not an accident of the
current endpoints.
"""

from __future__ import annotations

from collections import defaultdict
from pathlib import PurePosixPath
from typing import Any

from brainskit.application.filing import Filing
from brainskit.application.freshness import _freshness_summary
from brainskit.application.health import Health, enforcement_ok
from brainskit.application.pages import parse_frontmatter
from brainskit.application.ports import SearchIndexPort, VaultPort
from brainskit.application.privacy import (
    _consumer_allows,
    _evidence_privacy,
    _privacy_for_record,
    _record_branch,
    _validate_consumer,
)
from brainskit.application.projections import Projections
from brainskit.domain.model import NotFoundError, PolicyError, ValidationError


def _reportable_enforcement(enforcement: dict[str, Any]) -> dict[str, Any]:
    """The enforcement report with the machine-specific fields dropped.

    Enforcement is not evidence, so the consumer filter has nothing to say about
    it: a hook is installed or it is not, identically for whoever asks, and this
    surface already reports `vault` and `index` on the same footing. What is
    withheld here is withheld for minimality rather than for privacy. `detail`
    interpolates the workspace root and the redirected hooks directory, and
    `script` is an absolute path added for `bk doctor`, which opens the file;
    the viewer only has to name the layer that is off. A hook path names a local
    filesystem layout, and there is no reason to spend that on a caller with no
    use for it.
    """

    return {
        "gated": enforcement.get("gated", False),
        "inactive": list(enforcement.get("inactive", [])),
        "layers": [
            {
                key: layer[key]
                for key in ("layer", "mechanism", "active", "advisory")
                if key in layer
            }
            for layer in enforcement.get("layers", [])
        ],
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
    ):
        self.vault = vault
        self.index = index
        self.health = health
        self.filing = filing
        self.projections = projections


    def reader_status(self, *, consumer: str = "human") -> dict[str, Any]:
        _validate_consumer(consumer)
        if consumer == "human":
            return self.health.status()
        records = self.vault.registry()
        config = self.vault.config()
        visible_records = {
            content_hash: record
            for content_hash, record in records.items()
            if _consumer_allows(consumer, _privacy_for_record(config, record))
        }
        graph = self.projections.graph_data(consumer=consumer)
        visible_pages = {
            str(node["path"])
            for node in graph["nodes"]
            if str(node["id"]).startswith("page:")
        }
        visible_paths = {
            *(record.path for record in visible_records.values()),
            *visible_pages,
        }
        findings = self.health.lint()["findings"]
        visible_findings = [
            finding
            for finding in findings
            if not finding.get("path") or finding["path"] in visible_paths
        ]
        raw_counts: dict[str, int] = defaultdict(int)
        for record in visible_records.values():
            raw_counts[_record_branch(record)] += 1
        freshness = self.vault.read_state("freshness")
        filtered_freshness = {
            **freshness,
            "pages": {
                path: value
                for path, value in freshness.get("pages", {}).items()
                if path in visible_pages
            },
        }
        index_state = self.index.stats()
        enforcement = _reportable_enforcement(self.health.enforcement())
        return {
            "vault": str(self.vault.root),
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
            "freshness": _freshness_summary(filtered_freshness),
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
            "redacted_sources": len(records) - len(visible_records),
            "redacted_pages": len(self.vault.wiki_pages()) - len(visible_pages),
        }

    def browse_sources(
        self, *, consumer: str = "human", limit: int = 500
    ) -> dict[str, Any]:
        _validate_consumer(consumer)
        if not 1 <= limit <= 1_000:
            raise ValidationError("Source browse limit must be between 1 and 1000")
        config = self.vault.config()
        records = sorted(
            self.vault.registry().values(),
            key=lambda record: record.captured_at,
            reverse=True,
        )
        visible = []
        redacted = 0
        for record in records:
            privacy = _privacy_for_record(config, record)
            if not _consumer_allows(consumer, privacy):
                redacted += 1
                continue
            visible.append(
                {
                    "id": f"raw:{record.content_hash}",
                    "content_hash": record.content_hash,
                    "path": record.path,
                    "title": record.original_name,
                    "branch": _record_branch(record),
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
        _validate_consumer(consumer)
        if not 1 <= limit <= 1_000:
            raise ValidationError("Page browse limit must be between 1 and 1000")
        records = self.vault.registry()
        config = self.vault.config()
        freshness = self.vault.read_state("freshness").get("pages", {})
        graph = self.projections.graph_data(consumer=consumer)
        pages = []
        for node in graph["nodes"]:
            if not str(node["id"]).startswith("page:"):
                continue
            path = str(node["path"])
            content = self.vault.read_text(path)
            privacy = _evidence_privacy(node, content, records, config)
            page_freshness = freshness.get(path, {})
            pages.append(
                {
                    **node,
                    "privacy": privacy.value,
                    "freshness": (
                        page_freshness.get("status", "unknown")
                        if isinstance(page_freshness, dict)
                        else "unknown"
                    ),
                    "updated_at": (
                        page_freshness.get("updated_at")
                        if isinstance(page_freshness, dict)
                        else None
                    ),
                }
            )
        pages.sort(key=lambda page: (str(page["kind"]), str(page["label"])))
        return {
            "consumer": consumer,
            "count": min(len(pages), limit),
            "redacted": graph["redacted_nodes"],
            "pages": pages[:limit],
        }

    def timeline(
        self, *, consumer: str = "human", limit: int = 500
    ) -> dict[str, Any]:
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
        _validate_consumer(consumer)
        records = self.vault.registry()
        config = self.vault.config()
        if identifier.startswith("raw:"):
            content_hash = identifier.removeprefix("raw:")
            record = records.get(content_hash)
            if not record:
                raise NotFoundError(
                    "Raw graph resource was not found",
                    details={"identifier": identifier},
                )
            privacy = _privacy_for_record(config, record)
            if not _consumer_allows(consumer, privacy):
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
        privacy = _evidence_privacy({"path": path}, content, records, config)
        if not _consumer_allows(consumer, privacy):
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

    def proposals_for_consumer(
        self, status: str | None = None, *, consumer: str = "human"
    ) -> dict[str, Any]:
        _validate_consumer(consumer)
        result = self.filing.proposals(status)
        if consumer == "human":
            return {**result, "consumer": consumer, "redacted": 0}
        records = self.vault.registry()
        config = self.vault.config()
        visible = []
        for proposal in result["proposals"]:
            record = records.get(str(proposal.get("source_hash", "")))
            if record and _consumer_allows(
                consumer, _privacy_for_record(config, record)
            ):
                visible.append(proposal)
        return {
            "count": len(visible),
            "proposals": visible,
            "consumer": consumer,
            "redacted": result["count"] - len(visible),
        }
