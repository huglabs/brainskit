"""The privacy boundary: which evidence a named consumer may receive.

Search, context, export, the graph, the web reader and every integration have
to answer "may this consumer see this?" identically; a second implementation
of that question is a leak waiting to happen.

The pure rules -- the consumer lattice, the strictest-privacy fold, branch
policy resolution -- live in `brainskit.domain.privacy`. What this module owns
is the binding of those rules to one vault: `for_consumer(consumer, vault)`
builds the request-scoped `PrivacyBoundary` whose methods are the one answer
for that consumer against that vault. Page-level provenance resolution stays
here because `parse_frontmatter` does, and the boundary reads page content
through the vault at decision time when resolving it.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import PurePosixPath
from types import MappingProxyType
from typing import Any

from brainskit.application.pages import parse_frontmatter
from brainskit.application.ports import VaultPort
from brainskit.domain.model import PrivacyMode, SourceRecord
from brainskit.domain.privacy import (
    Consumer,
    branch_privacy,
    record_branch,
    strictest_privacy,
)

__all__ = [
    "Consumer",
    "PrivacyBoundary",
    "for_consumer",
]


def _evidence_branches(
    hit: dict[str, Any],
    content: str,
    records: dict[str, SourceRecord],
) -> list[str]:
    content_hash = hit.get("content_hash")
    if content_hash and content_hash in records:
        return [record_branch(records[content_hash])]
    metadata, _ = parse_frontmatter(content)
    source_hashes = metadata.get("sources", [])
    if not isinstance(source_hashes, list):
        return []
    return sorted(
        {
            record_branch(records[content_hash])
            for content_hash in source_hashes
            if content_hash in records
        }
    )


def _evidence_privacy(
    hit: dict[str, Any],
    content: str,
    records: dict[str, SourceRecord],
    config: Any,
) -> PrivacyMode:
    content_hash = hit.get("content_hash")
    if content_hash and content_hash in records:
        return branch_privacy(config, record_branch(records[content_hash]))
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
        (branch_privacy(config, record_branch(record)) for record in resolved),
        # Unreachable: `resolved` is non-empty and fully resolved by here. Stated
        # anyway, because the parameter exists precisely so that no call site can
        # leave the question implicit.
        on_empty=PrivacyMode.NEVER_INGEST,
    )


def for_consumer(consumer: str | Consumer, vault: VaultPort) -> PrivacyBoundary:
    """The one constructor: parse the consumer once, snapshot the vault once."""

    return PrivacyBoundary(consumer, vault)


class PrivacyBoundary:
    """Every privacy question one consumer can ask of one vault.

    Request-scoped by convention: the boundary snapshots the registry and the
    config at construction, so every answer it gives describes the vault as it
    stood at that moment. Build one per request and let it go -- never cache a
    boundary across writes, or it answers for a vault that no longer exists.
    The one read it defers is page content (`evidence_privacy`, `allows_path`),
    fetched through the vault at decision time -- the same instant it is read
    today -- which is what closes the TOCTOU window a pre-read manifest had.
    """

    def __init__(self, consumer: str | Consumer, vault: VaultPort) -> None:
        self.consumer = Consumer.parse(consumer)
        self._vault = vault
        self._records: dict[str, SourceRecord] = dict(vault.registry())
        self._config = vault.config()
        self.records: Mapping[str, SourceRecord] = MappingProxyType(self._records)

    def allows(self, privacy: PrivacyMode) -> bool:
        return self.consumer.allows(privacy)

    def record_privacy(self, record: SourceRecord) -> PrivacyMode:
        return branch_privacy(self._config, record_branch(record))

    def allows_record(self, record: SourceRecord) -> bool:
        return self.allows(self.record_privacy(record))

    def evidence_privacy(
        self, hit: dict[str, Any], content: str | None = None
    ) -> PrivacyMode:
        if content is None:
            content = self._vault.read_text(str(hit["path"]))
        return _evidence_privacy(hit, content, self._records, self._config)

    def allows_evidence(self, hit: dict[str, Any], content: str | None = None) -> bool:
        return self.allows(self.evidence_privacy(hit, content))

    def evidence_branches(self, hit: dict[str, Any], content: str) -> list[str]:
        return _evidence_branches(hit, content, self._records)

    def branch_privacy(self, branch: str) -> PrivacyMode:
        return branch_privacy(self._config, branch)

    def split_records(self) -> tuple[dict[str, SourceRecord], int]:
        """The records this consumer may see, and how many were redacted.

        The count is deliberately all a caller learns about the redacted side:
        a redacted source contributes nothing -- not its body, not its
        filename, not its branch.
        """

        visible: dict[str, SourceRecord] = {}
        redacted = 0
        for content_hash, record in self._records.items():
            if self.allows_record(record):
                visible[content_hash] = record
            else:
                redacted += 1
        return visible, redacted

    def allows_path(self, relative: PurePosixPath) -> bool:
        """Whether a vault-relative path may be copied out to this consumer.

        The egress rule. The graph object was filtered carefully and then sync
        chose files by walking the filesystem, so the boundary never reached
        the copy: the compiled page leaked under default options and raw
        never-ingest bytes leaked under `include_raw` -- into what is usually
        an iCloud- or Dropbox-backed directory.

        Wiki pages are judged by reading their content and resolving the
        frontmatter provenance, at decision time. Raw files are judged by the
        branch the path names, the way `record_branch` reads it, rather than
        by a registry lookup: a file that landed in the inbox but has not been
        reconciled yet still sits in a branch whose policy is known, and
        refusing it would over-block the one directory files arrive in. A path
        under a branch nobody configured has no policy that says it may leave,
        so only a human may take it. Everything else (`views/`, `graph/`)
        passes: `ProjectionService.integration_sync` regenerates those
        filtered under this same consumer immediately before the copy -- they
        used to be the only tree that was ever safe, and they were safe by
        that accident rather than by this decision.
        """

        posix = relative.as_posix()
        if posix.startswith("wiki/"):
            content = self._vault.read_text(posix)
            privacy = _evidence_privacy(
                {"path": posix}, content, self._records, self._config
            )
            return self.allows(privacy)
        if posix.startswith("raw/"):
            parts = relative.parts
            branch = parts[1] if len(parts) > 1 else ""
            if branch == "_inbox":
                return self.allows(PrivacyMode(self._config.inbox_policy.privacy))
            branch_policy = self._config.branches.get(branch)
            if branch_policy is None:
                return self.consumer is Consumer.HUMAN
            return self.allows(PrivacyMode(branch_policy.privacy))
        return True
