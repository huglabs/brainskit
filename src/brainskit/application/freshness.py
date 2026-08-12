"""Freshness and projection state: what is current, and what has aged out.

Freshness answers "is this page still backed by what it was compiled from",
projections answer "does this generated artefact still describe the vault".
They share a module because they share a shape: both are a stored fingerprint
compared against the live inputs, and both are read by `lint` and `status`
through the same state file.

Pure functions only; the state dict is passed in.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import Any

from brainskit.domain.model import SourceRecord

GRAPH_PROJECTION = "graph/graph.json"

VIEWS_PROJECTION = "views"

# `views/` is a tree whose branch maps come and go with the branches that hold
# sources, so the artefact is identified by the one file every run writes:
# `views/home.md`. The recorder, the lint check and `status` all use this anchor,
# so "the artefact exists" means the same thing in all three.
PROJECTION_ANCHORS: dict[str, str] = {
    GRAPH_PROJECTION: GRAPH_PROJECTION,
    VIEWS_PROJECTION: "views/home.md",
}

PROJECTION_COMMANDS: dict[str, str] = {
    GRAPH_PROJECTION: "bk graph",
    VIEWS_PROJECTION: "bk views",
}

# Which registry fields each artefact actually renders, measured by mutating one
# field, regenerating, and diffing the output — not read off the source. Both
# artefacts also cover the whole wiki page set, so that is not per-artefact.
#
# The graph labels a raw node with `original_name` and carries `path`; `views`
# renders both plus the Status and Captured columns. `media_type` and `size`
# reach neither, so re-extracting a document does not age a projection.
PROJECTION_RAW_FIELDS: dict[str, tuple[str, ...]] = {
    GRAPH_PROJECTION: ("path", "original_name"),
    VIEWS_PROJECTION: ("path", "original_name", "status", "captured_at"),
}

PROJECTION_LINT_CODES: dict[str, str] = {
    GRAPH_PROJECTION: "graph.stale",
    VIEWS_PROJECTION: "views.stale",
}


def _freshness_summary(
    state: dict[str, Any], *, present: set[str] | None = None
) -> dict[str, int]:
    """Count freshness entries, ignoring pages that no longer exist on disk.

    Deleting a wiki page by hand leaves its entry behind. Counting it would
    report freshness for a page nobody can open; `lint` surfaces the orphan and
    `reconcile` removes it.
    """
    summary: dict[str, int] = {"fresh": 0, "review": 0, "stale": 0, "unknown": 0}
    for path, entry in state.get("pages", {}).items():
        if present is not None and path not in present:
            continue
        status = entry.get("status", "unknown") if isinstance(entry, dict) else "unknown"
        summary[status if status in summary else "unknown"] += 1
    return summary


def _fingerprint_row(namespace: str, *fields: str) -> str:
    """Encode one input row so that no two distinct rows can encode alike.

    The namespace tag comes first, which is what keeps the wiki and raw domains
    apart: a page path can never land in the position a content hash occupies,
    so no transposition of one into the other collides. Every field is then
    length-prefixed, so a separator appearing inside a value cannot fake a field
    boundary either — `("a", "b:c")` and `("a:b", "c")` stay distinct.
    """
    return "|".join([namespace, *(f"{len(field)}:{field}" for field in fields)])


def _projection_source_hash(
    pages: Any,
    records: dict[str, SourceRecord],
    raw_fields: tuple[str, ...],
) -> str:
    """Fingerprint the inputs a derived artefact is built from.

    Deliberately not mtime. A `git checkout` rewrites every mtime in the working
    tree, which would call a current graph stale and — after a checkout that
    restores an old graph — call a stale one current. Content hashes travel with
    the content, so they survive any clone, checkout or rsync.

    Two input domains, because both artefacts render both: the wiki page set,
    and the raw registry. A page-only fingerprint let a `bk capture` add a
    `raw:` node to the graph with nothing reporting it — the exact silent drift
    this check exists to catch. `raw_fields` names the registry fields the
    calling artefact actually renders, so a field only one of them shows cannot
    age the other.

    What stays out: the `status` and `age_days` that `_refresh_staleness`
    rewrites on every `bk lint`. `age_days` reaches no artefact at all, and
    while a page's freshness badge does appear in `views/map/*.md`, that badge
    moves with the clock rather than with anything a user did — folding it in
    would make `views.stale` appear spontaneously on an untouched vault.

    Serialization is pinned so two processes agree: pages sorted by path, raw
    records sorted by content hash, pages before raw, newline between rows,
    UTF-8.
    """
    rows: list[str] = []
    if not isinstance(pages, dict):
        pages = {}
    for path in sorted(pages):
        entry = pages[path]
        content_hash = entry.get("content_hash") if isinstance(entry, dict) else None
        if not isinstance(content_hash, str):
            content_hash = ""
        rows.append(_fingerprint_row("page", path, content_hash))
    for content_hash in sorted(records):
        record = records[content_hash]
        rows.append(
            _fingerprint_row(
                "raw",
                content_hash,
                *(str(getattr(record, field)) for field in raw_fields),
            )
        )
    return hashlib.sha256("\n".join(rows).encode("utf-8")).hexdigest()


def _age_in_days(timestamp: str | None, now: datetime) -> int | None:
    if not timestamp:
        return None
    try:
        moment = datetime.fromisoformat(timestamp)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return (now - moment).days


def _orphaned_freshness(state: dict[str, Any], present: set[str]) -> list[str]:
    return sorted(path for path in state.get("pages", {}) if path not in present)
