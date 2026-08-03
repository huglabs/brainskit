from __future__ import annotations

from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, Protocol

from brainkit.domain.model import SearchHit, SourceRecord, VaultConfig


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

    def file_source(self, identifier: str, branch: str) -> SourceRecord: ...

    def read_text(self, relative_path: str) -> str: ...

    def content_hash(self, relative_path: str) -> str: ...

    def code_root(self) -> Path: ...

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

    def commit_wiki_batch(
        self,
        pages: dict[str, str],
        expected_versions: dict[str, str | None],
        source_statuses: dict[str, str],
        proposal_id: str,
        request_hash: str,
        freshness_state: dict[str, Any],
        raw_move: tuple[str, str] | None,
        index_rebuild: Callable[[dict[str, SourceRecord]], int],
    ) -> dict[str, Any]: ...

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

    def sync(self, name: str, graph: dict[str, Any]) -> dict[str, Any]: ...
