"""The privacy boundary: which evidence a named consumer may receive.

This is the smallest module in the application layer and the one with the most
callers, which is the point. Search, context, export, the graph, the web reader
and every integration have to answer "may this consumer see this?" identically;
a second implementation of that question is a leak waiting to happen.

Pure functions only -- the decision depends on the branch a source lives in and
the consumer that asked, never on how the caller reached it.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import urlparse

from brainkit.application.pages import parse_frontmatter
from brainkit.domain.model import (
    PrivacyMode,
    SourceRecord,
    ValidationError,
)


def _is_url(value: str) -> bool:
    return urlparse(value).scheme in {"http", "https"}


def _view_branch(record: SourceRecord) -> str:
    parts = PurePosixPath(record.path).parts
    return parts[1] if len(parts) > 1 else "unknown"


def _record_branch(record: SourceRecord) -> str:
    parts = PurePosixPath(record.path).parts
    if len(parts) < 2 or parts[0] != "raw":
        raise ValidationError("Source is outside raw/", details={"path": record.path})
    return parts[1]


def _evidence_branches(
    hit: dict[str, Any],
    content: str,
    records: dict[str, SourceRecord],
) -> list[str]:
    content_hash = hit.get("content_hash")
    if content_hash and content_hash in records:
        return [_record_branch(records[content_hash])]
    metadata, _ = parse_frontmatter(content)
    source_hashes = metadata.get("sources", [])
    if not isinstance(source_hashes, list):
        return []
    return sorted(
        {
            _record_branch(records[content_hash])
            for content_hash in source_hashes
            if content_hash in records
        }
    )


def _context_branches(context: dict[str, Any]) -> list[str]:
    branches = sorted(
        {
            branch
            for evidence in context.get("evidence", [])
            for branch in evidence.get("branches", [])
        }
    )
    return branches or ["_inbox"]


def _privacy_for_record(config: Any, record: SourceRecord) -> PrivacyMode:
    branch = _record_branch(record)
    policy = config.inbox_policy if branch == "_inbox" else config.branches[branch]
    return PrivacyMode(policy.privacy)


def _evidence_privacy(
    hit: dict[str, Any],
    content: str,
    records: dict[str, SourceRecord],
    config: Any,
) -> PrivacyMode:
    content_hash = hit.get("content_hash")
    if content_hash and content_hash in records:
        return _privacy_for_record(config, records[content_hash])
    metadata, _ = parse_frontmatter(content)
    source_hashes = metadata.get("sources", [])
    if not isinstance(source_hashes, list):
        return PrivacyMode.CLOUD
    return strictest_privacy(
        _privacy_for_record(config, records[content_hash])
        for content_hash in source_hashes
        if content_hash in records
    )


def strictest_privacy(modes: Iterable[PrivacyMode]) -> PrivacyMode:
    """The most restrictive policy in `modes`, defaulting to the most open.

    Named and shared rather than repeated: derived evidence and model-inferred
    enrichment both answer "what may this be shown to" by taking the strictest
    policy across everything that contributed, which is the same rule the
    judgment router applies when evidence spans branches. Two copies of a
    privacy rule is one copy too many -- the second is where they drift.

    An empty `modes` means nothing classifiable contributed. `CLOUD` is the
    honest answer there only because every caller checks provenance resolves
    *first*: `Enrichment.apply` refuses an edge whose sources are unknown, so
    "no contributing source" never reaches this function from that path.
    """

    collected = set(modes)
    if PrivacyMode.NEVER_INGEST in collected:
        return PrivacyMode.NEVER_INGEST
    if PrivacyMode.LOCAL_ONLY in collected:
        return PrivacyMode.LOCAL_ONLY
    return PrivacyMode.CLOUD


def _validate_consumer(consumer: str) -> None:
    if consumer not in {"human", "local", "cloud"}:
        raise ValidationError(
            "Consumer must be human, local, or cloud",
            details={"consumer": consumer},
        )


def _consumer_allows(consumer: str, privacy: PrivacyMode) -> bool:
    if consumer == "human":
        return True
    if consumer == "local":
        return privacy != PrivacyMode.NEVER_INGEST
    return privacy == PrivacyMode.CLOUD
