from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Protocol

from brainskit.domain.model import ScanSurvey, SearchHit, SourceRecord, VaultConfig


@dataclass(frozen=True, slots=True)
class ApplyPlan:
    """The complete description of one apply, so that a port and its adapter
    can no longer disagree about a parameter's name.

    `commit_wiki_batch` took these as eight positional parameters and had one
    production caller, which passed all eight positionally. A positional call
    cannot disagree about a name, so nothing noticed when the port came to
    declare `freshness_state` while the adapter went on implementing
    `freshness_updates` -- two live spellings of one argument, each of them
    wrong depending on which file you were reading, and neither reachable by a
    test that only ever *calls* the thing. The same silence hid a weaker type
    on the port (`dict[str, Any]` for what `FreshnessLedger.mark_applied`
    actually returns). One argument object means one spelling.

    It lives beside the port rather than in `domain/model.py` because
    `index_rebuild` is a callback into the index adapter: behaviour, not data.
    Every value the domain holds is data that refers to nothing that runs, and
    `DomainHasNoThirdPartyImportsTest` would not have caught this one --
    `Callable` is stdlib -- so the placement has to be argued rather than
    left to the guard.
    """

    pages: dict[str, str]
    expected_versions: dict[str, str | None]
    source_statuses: dict[str, str]
    proposal_id: str
    request_hash: str
    freshness_updates: dict[str, dict[str, Any]]
    raw_move: tuple[str, str] | None
    index_rebuild: Callable[[dict[str, SourceRecord]], int]


class VaultPort(Protocol):
    root: Path

    def config(self) -> VaultConfig: ...

    def save_config(self, config: VaultConfig) -> None: ...

    def schema(self) -> dict[str, Any]: ...

    def registry(self) -> dict[str, SourceRecord]: ...

    def save_registry(self, records: dict[str, SourceRecord]) -> None: ...

    def capture_file(self, source: Path) -> tuple[SourceRecord, bool]: ...

    def capture_text(
        self, text: str, title: str, suffix: str = ".md"
    ) -> tuple[SourceRecord, bool]: ...

    def reconcile(self) -> dict[str, int]: ...

    def forget(self, identifier: str, *, force: bool = False) -> SourceRecord: ...

    def file_source(self, identifier: str, branch: str) -> SourceRecord: ...

    def read_text(self, relative_path: str) -> str: ...

    def content_hash(self, relative_path: str) -> str: ...

    def code_root(self) -> Path: ...

    def code_root_reason(self) -> tuple[Path, str]: ...

    #: A property rather than a method, matching the adapter. Declared here
    #: because the application layer is what hands it to the extractor, and a
    #: port that omits it forces the caller to reach past the boundary.
    @property
    def code_cache_dir(self) -> Path: ...

    def code_hash(self, relative_path: str) -> str | None: ...

    def raw_text(self, record: SourceRecord, max_chars: int | None = None) -> str: ...

    def wiki_pages(self) -> list[str]: ...

    def raw_files(self) -> list[str]: ...

    def wiki_version(self, relative_path: str) -> str | None: ...

    def commit_wiki_batch(self, plan: ApplyPlan) -> dict[str, Any]: ...

    def read_state(self, name: str) -> dict[str, Any]: ...

    def mutate_state(
        self,
        name: str,
        mutator: Callable[[dict[str, Any]], dict[str, Any]],
    ) -> dict[str, Any]: ...

    def write_generated(self, relative_path: str, content: str) -> None: ...

    def existing_wiki_slugs(self) -> set[str]: ...


class SearchIndexPort(Protocol):
    def rebuild(self, vault: VaultPort) -> int: ...

    def rebuild_snapshot(
        self, vault: VaultPort, records: dict[str, SourceRecord]
    ) -> int: ...

    def apply_snapshot(
        self,
        vault: VaultPort,
        records: dict[str, SourceRecord],
        wiki_paths: Sequence[str],
        raw_content_hash: str | None,
    ) -> int: ...

    def upsert_raw(self, vault: VaultPort, record: SourceRecord) -> None: ...

    def upsert_wiki(self, vault: VaultPort, paths: Sequence[str]) -> None: ...

    #: Rename indexed documents in place. Declared on the port because the
    #: vault performs this migration and used to reach into `search_fts` --
    #: another adapter's private schema -- to do it.
    def rename_paths(
        self, moved: Mapping[str, str], removed: Sequence[str]
    ) -> None: ...

    def search(self, query: str, limit: int = 10) -> list[SearchHit]: ...

    def stats(self) -> dict[str, Any]: ...


class JudgmentPort(Protocol):
    def run(
        self,
        *,
        job: str,
        branches: Sequence[str],
        variables: dict[str, Any],
        output_schema: dict[str, Any] | None = None,
    ) -> str: ...


class JobSpecPort(Protocol):
    def prompt(self, job: str, variables: dict[str, Any]) -> str: ...

    def schema(self, job: str) -> dict[str, Any] | None: ...


class GraphPort(Protocol):
    def build(self, vault: VaultPort) -> dict[str, Any]: ...

    def export(self, graph: dict[str, Any], target: str) -> str: ...


class CodeExtractorPort(Protocol):
    def extract(
        self,
        root: Path,
        paths: list[Path] | None = None,
        *,
        cache_root: Path | None = None,
    ) -> dict[str, Any]: ...

    def available(self) -> bool: ...

    #: What a scan would cover, measured without performing it. Declared on the
    #: port because `CodeGraph` refuses an oversized scan and offers to install
    #: missing grammars *before* extracting, and both need this answer. Callers
    #: reach it defensively (`getattr`) so an extractor that predates it still
    #: satisfies the protocol at runtime.
    def survey(self, root: Path, paths: list[Path] | None = None) -> ScanSurvey: ...


class SyncBoundaryPort(Protocol):
    """What crosses to an integration adapter: a consumer name and one path
    predicate. Never inside the graph payload -- the graph dict stays pure
    JSON data end to end."""

    @property
    def consumer(self) -> str: ...

    def allows_path(self, relative: PurePosixPath) -> bool: ...


class IntegrationPort(Protocol):
    def configure(
        self,
        name: str,
        *,
        enabled: bool | None,
        managed: bool | None,
        options: dict[str, Any],
    ) -> dict[str, Any]: ...

    def status(self, name: str | None = None) -> dict[str, Any]: ...

    def up(self, name: str) -> dict[str, Any]: ...

    def down(self, name: str) -> dict[str, Any]: ...

    def sync(
        self,
        name: str,
        graph: dict[str, Any],
        boundary: SyncBoundaryPort,
    ) -> dict[str, Any]: ...
