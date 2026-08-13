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

from brainskit.application.pages import parse_frontmatter
from brainskit.domain.model import (
    PolicyError,
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
    return PrivacyMode(_branch_policy(config, branch).privacy)


def _branch_policy(config: Any, branch: str) -> Any:
    """The policy governing `branch`, or a refusal naming it.

    This was `config.branches[branch]`, a direct subscript, where
    `infrastructure/llm.py` already used `.get()` and raised `PolicyError` for
    the identical question. A file dropped into a directory that is not a
    configured branch -- then registered by the *documented* `bk reconcile` --
    made `search`, `browse_sources`, `graph_data` and `export` raise a bare
    `KeyError`. Not being a `BrainskitError`, it bypassed the JSON envelope and
    the exit codes: an unhandled traceback on the CLI, a 500 in the viewer.
    """

    if branch == "_inbox":
        return config.inbox_policy
    policy = config.branches.get(branch)
    if policy is None:
        raise PolicyError(
            "No privacy policy exists for this branch",
            details={
                "branch": branch,
                "configured": sorted(config.branches),
                "hint": (
                    "Move the source into a configured branch with bk file, "
                    "or add the branch to the vault policy"
                ),
            },
        )
    return policy


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
        # `sources` present but not a list: the page declares provenance we
        # cannot read. Same epistemic state as one that does not resolve.
        return PrivacyMode.NEVER_INGEST
    if not source_hashes:
        # Declares no provenance at all -- a system page, legitimately cloud.
        # This is the case a blanket "unknown means never-ingest" would break.
        return PrivacyMode.CLOUD
    resolved = [
        records[content_hash]
        for content_hash in source_hashes
        if content_hash in records
    ]
    if len(resolved) != len(source_hashes):
        # Declares provenance that does not resolve. Dropping the unresolvable
        # hashes and asking `strictest_privacy` for the remainder answered CLOUD
        # on an empty set, so forgetting a never-ingest source *declassified*
        # every page built from it instead of redacting them.
        #
        # A partial resolution is still unknown provenance: the hash that went
        # missing could have been the restricted one, and there is no way left
        # to tell. `Enrichment.privacy_of` already answers this identically.
        return PrivacyMode.NEVER_INGEST
    return strictest_privacy(
        (_privacy_for_record(config, record) for record in resolved),
        # Unreachable: `resolved` is non-empty and fully resolved by here. Stated
        # anyway, because the parameter exists precisely so that no call site can
        # leave the question implicit.
        on_empty=PrivacyMode.NEVER_INGEST,
    )


def strictest_privacy(
    modes: Iterable[PrivacyMode], *, on_empty: PrivacyMode
) -> PrivacyMode:
    """The most restrictive policy in `modes`.

    Named and shared rather than repeated: derived evidence and model-inferred
    enrichment both answer "what may this be shown to" by taking the strictest
    policy across everything that contributed, which is the same rule the
    judgment router applies when evidence spans branches. Two copies of a
    privacy rule is one copy too many -- the second is where they drift.

    `on_empty` is required, and that is the whole point. This function used to
    default an empty `modes` to `CLOUD`, justified by a docstring asserting that
    *every* caller checks provenance resolves first. `_evidence_privacy` was a
    caller that did not, so forgetting a `never-ingest` source declassified every
    page built from it. An invariant that is asserted rather than enforced is
    documentation of a bug that has not happened yet; making the answer a
    required argument means the next caller cannot omit the decision by
    accident.
    """

    collected = set(modes)
    if not collected:
        return on_empty
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
