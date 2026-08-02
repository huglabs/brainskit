from __future__ import annotations

import fcntl
import hashlib
import json
import mimetypes
import os
import re
import shutil
import sqlite3
import tempfile
import unicodedata
import uuid
from contextlib import closing, contextmanager
from copy import deepcopy
from importlib.resources import files
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterator

from brainkit.domain.model import (
    LEGACY_WIKI_DIRECTORIES,
    WIKI_DIRECTORIES,
    NotFoundError,
    SourceRecord,
    ValidationError,
    VaultConfig,
    normalize_branch,
    utc_now,
)
from brainkit.infrastructure.converters import extract_document


class FileVault:
    """Filesystem adapter for the Markdown source of truth."""

    STATE_NAMES = {"applied", "freshness", "proposals", "integration-state"}

    def __init__(self, root: Path):
        self.root = root.expanduser().resolve()
        if not (self.root / ".brain" / "config.json").is_file():
            raise NotFoundError(
                "Not a brainkit vault",
                details={"vault": str(self.root), "hint": "Run bk init first"},
            )
        with self._write_lock():
            self._ensure_runtime_state_unlocked()
            self._recover_apply_unlocked()
            self._migrate_wiki_directories_unlocked()

    @classmethod
    def initialize(cls, root: Path, raw_config: dict[str, Any]) -> FileVault:
        root = root.expanduser().resolve()
        config = VaultConfig.from_dict(raw_config)
        config_path = root / ".brain" / "config.json"
        if config_path.exists():
            raise ValidationError(
                "Vault is already initialized", details={"vault": str(root)}
            )
        directories = [
            "raw/_inbox",
            "raw/_assets",
            *(f"wiki/{name}" for name in WIKI_DIRECTORIES),
            "views/map",
            "views/domains",
            "graph",
            "output/digests",
            "output/reports",
            "output/answers",
            ".brain",
        ]
        directories.extend(f"raw/{branch}" for branch in config.branches)
        for relative in directories:
            (root / relative).mkdir(parents=True, exist_ok=True)
        _atomic_json(config_path, config.to_dict())
        _atomic_json(root / ".brain" / "registry.json", {"version": 1, "sources": {}})
        _atomic_json(root / ".brain" / "freshness.json", {"version": 2, "pages": {}})
        _atomic_json(root / ".brain" / "proposals.json", {"version": 1, "proposals": {}})
        _atomic_json(root / ".brain" / "applied.json", {"version": 1, "proposals": {}})
        _atomic_json(
            root / ".brain" / "integration-state.json",
            {"version": 1, "integrations": {}},
        )
        schema_text = (
            files("brainkit")
            .joinpath("templates/default/schema.json")
            .read_text(encoding="utf-8")
        )
        _atomic_text(root / ".brain" / "schema.json", schema_text.rstrip() + "\n")
        _atomic_text(
            root / ".gitignore",
            ".brain/index.db\n"
            ".brain/index.db-shm\n"
            ".brain/index.db-wal\n"
            ".brain/*.lock\n"
            ".brain/services/\n",
        )
        index_page = _system_page("index", "Brainkit index")
        log_page = _system_page("log", "Brainkit log")
        _atomic_text(root / "wiki" / "index.md", index_page)
        _atomic_text(root / "wiki" / "log.md", log_page)
        return cls(root)

    @classmethod
    def discover(cls, start: Path | None = None) -> FileVault:
        current = (start or Path.cwd()).expanduser().resolve()
        if current.is_file():
            current = current.parent
        for candidate in (current, *current.parents):
            if (candidate / ".brain" / "config.json").is_file():
                return cls(candidate)
        raise NotFoundError(
            "No brainkit vault found",
            details={"start": str(current), "hint": "Pass --vault or run bk init"},
        )

    @property
    def index_path(self) -> Path:
        return self.root / ".brain" / "index.db"

    def config(self) -> VaultConfig:
        raw = json.loads(
            (self.root / ".brain" / "config.json").read_text(encoding="utf-8")
        )
        return VaultConfig.from_dict(raw)

    def save_config(self, config: VaultConfig) -> None:
        validated = VaultConfig.from_dict(config.to_dict())
        _atomic_json(self.root / ".brain" / "config.json", validated.to_dict())

    def schema(self) -> dict[str, Any]:
        raw = json.loads(
            (self.root / ".brain" / "schema.json").read_text(encoding="utf-8")
        )
        if not isinstance(raw, dict):
            raise ValidationError("Vault schema must be a JSON object")
        return raw

    def registry(self) -> dict[str, SourceRecord]:
        with self._registry_lock(shared=True):
            return self._read_registry_unlocked()

    def save_registry(self, records: dict[str, SourceRecord]) -> None:
        with self._registry_lock(shared=False):
            self._write_registry_unlocked(records)

    def capture_file(self, source: Path) -> tuple[SourceRecord, bool]:
        source = source.expanduser().resolve()
        if not source.is_file():
            raise NotFoundError(
                "Capture source is not a file", details={"source": str(source)}
            )
        content_hash, size = _hash_file(source)
        with self._registry_lock(shared=False):
            records = self._read_registry_unlocked()
            if content_hash in records:
                record, created = records[content_hash], False
            else:
                destination = self._capture_destination(
                    source.name, content_hash, suffix=source.suffix
                )
                if source != destination:
                    _atomic_copy(source, destination)
                record = SourceRecord(
                    content_hash=content_hash,
                    path=destination.relative_to(self.root).as_posix(),
                    original_name=source.name,
                    media_type=mimetypes.guess_type(source.name)[0]
                    or "application/octet-stream",
                    size=size,
                    captured_at=utc_now(),
                )
                records[content_hash] = record
                self._write_registry_unlocked(records)
                created = True
        self.raw_text(record)
        return record, created

    def capture_text(
        self, text: str, title: str, suffix: str = ".md"
    ) -> tuple[SourceRecord, bool]:
        payload = text.encode("utf-8")
        content_hash = hashlib.sha256(payload).hexdigest()
        with self._registry_lock(shared=False):
            records = self._read_registry_unlocked()
            if content_hash in records:
                return records[content_hash], False
            name = f"{_slugify(title) or 'capture'}{suffix}"
            destination = self._capture_destination(name, content_hash, suffix=suffix)
            _atomic_bytes(destination, payload)
            record = SourceRecord(
                content_hash=content_hash,
                path=destination.relative_to(self.root).as_posix(),
                original_name=name,
                media_type=mimetypes.guess_type(name)[0] or "text/markdown",
                size=len(payload),
                captured_at=utc_now(),
            )
            records[content_hash] = record
            self._write_registry_unlocked(records)
            return record, True

    def reconcile(self) -> dict[str, int]:
        scanned = 0
        added = 0
        moved = 0
        duplicates = 0
        with self._registry_lock(shared=False):
            records = self._read_registry_unlocked()
            seen_hashes: set[str] = set()
            for path in self._iter_raw_paths():
                scanned += 1
                content_hash, size = _hash_file(path)
                relative = path.relative_to(self.root).as_posix()
                if content_hash in seen_hashes:
                    duplicates += 1
                    continue
                seen_hashes.add(content_hash)
                if content_hash in records:
                    record = records[content_hash]
                    if record.path != relative:
                        record.path = relative
                        moved += 1
                    continue
                records[content_hash] = SourceRecord(
                    content_hash=content_hash,
                    path=relative,
                    original_name=path.name,
                    media_type=mimetypes.guess_type(path.name)[0]
                    or "application/octet-stream",
                    size=size,
                    captured_at=utc_now(),
                )
                added += 1
            missing = [
                content_hash
                for content_hash, record in records.items()
                if not (self.root / record.path).is_file()
            ]
            self._write_registry_unlocked(records)
        return {
            "scanned": scanned,
            "added": added,
            "moved": moved,
            "duplicates": duplicates,
            "missing": len(missing),
        }

    def file_source(self, identifier: str, branch: str) -> SourceRecord:
        branch = normalize_branch(branch)
        config = self.config()
        if branch not in config.branches:
            raise ValidationError(
                "Destination branch is not configured", details={"branch": branch}
            )
        with self._registry_lock(shared=False):
            records = self._read_registry_unlocked()
            record = _resolve_record(records, identifier)
            source = self._resolve_relative(record.path)
            if not source.is_file():
                raise NotFoundError(
                    "Registered source file is missing", details={"path": record.path}
                )
            destination = self.root / "raw" / branch / source.name
            if destination.exists() and destination != source:
                existing_hash, _ = _hash_file(destination)
                if existing_hash != record.content_hash:
                    destination = destination.with_name(
                        f"{destination.stem}-{record.content_hash[:10]}{destination.suffix}"
                    )
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination != source:
                os.replace(source, destination)
            record.path = destination.relative_to(self.root).as_posix()
            records[record.content_hash] = record
            self._write_registry_unlocked(records)
            return record

    def read_text(self, relative_path: str) -> str:
        path = self._resolve_relative(relative_path)
        try:
            return path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            return path.read_bytes().decode("utf-8", errors="replace")

    def content_hash(self, relative_path: str) -> str:
        content_hash, _ = _hash_file(self._resolve_relative(relative_path))
        return content_hash

    def raw_text(self, record: SourceRecord, max_chars: int | None = None) -> str:
        path = self._resolve_relative(record.path)
        cache = self.root / ".brain" / "extracted" / f"{record.content_hash}.txt"
        if cache.is_file():
            text = cache.read_text(encoding="utf-8")
        else:
            text, derived = extract_document(path, record.media_type)
            if derived:
                _atomic_text(cache, text.rstrip() + "\n")
        return text if max_chars is None else text[:max_chars]

    def wiki_pages(self) -> list[str]:
        return [
            path.relative_to(self.root).as_posix()
            for path in sorted((self.root / "wiki").rglob("*.md"))
            if path.is_file()
        ]

    def raw_files(self) -> list[str]:
        return [
            path.relative_to(self.root).as_posix() for path in self._iter_raw_paths()
        ]

    def wiki_version(self, relative_path: str) -> str | None:
        path = self._resolve_relative(relative_path)
        if not path.is_file():
            return None
        content_hash, _ = _hash_file(path)
        return content_hash

    def commit_wiki_batch(
        self,
        pages: dict[str, str],
        expected_versions: dict[str, str | None],
        source_statuses: dict[str, str],
        proposal_id: str,
        request_hash: str,
        freshness_updates: dict[str, dict[str, Any]],
        raw_move: tuple[str, str] | None,
        index_rebuild: Callable[[dict[str, SourceRecord]], int],
    ) -> dict[str, Any]:
        with self._write_lock():
            self._recover_apply_unlocked()
            with self._registry_lock(shared=False), self._state_lock(
                "applied", shared=False
            ), self._state_lock("freshness", shared=False):
                applied = self._read_state_unlocked("applied")
                prior = applied.get("proposals", {}).get(proposal_id)
                if isinstance(prior, dict):
                    if prior.get("request_hash") != request_hash:
                        raise ValidationError(
                            "proposal_id was already used for a different payload",
                            details={"proposal_id": proposal_id},
                        )
                    return {**prior, "idempotent": True}
                for relative, expected in expected_versions.items():
                    observed = self.wiki_version(relative)
                    if observed != expected:
                        raise ValidationError(
                            "Wiki page changed after context was built",
                            details={
                                "path": relative,
                                "expected": expected,
                                "observed": observed,
                            },
                        )
                records = self._read_registry_unlocked()
                raw_move_entry: dict[str, Any] | None = None
                if raw_move:
                    content_hash, branch = raw_move
                    branch = normalize_branch(branch)
                    if branch not in self.config().branches:
                        raise ValidationError(
                            "Destination branch is not configured",
                            details={"branch": branch},
                        )
                    record = _resolve_record(records, content_hash)
                    source = self._resolve_relative(record.path)
                    if not source.is_file():
                        raise NotFoundError(
                            "Registered source file is missing",
                            details={"path": record.path},
                        )
                    destination = self._transaction_move_destination(
                        source, branch, record.content_hash
                    )
                    raw_move_entry = {
                        "content_hash": record.content_hash,
                        "source": source.relative_to(self.root).as_posix(),
                        "destination": destination.relative_to(self.root).as_posix(),
                        "inflight": False,
                        "moved": False,
                    }
                transaction_id = uuid.uuid4().hex
                transaction_root = (
                    self.root / ".brain" / "transactions" / transaction_id
                )
                staged_root = transaction_root / "staged"
                backup_root = transaction_root / "backups"
                staged_root.mkdir(parents=True, exist_ok=True)
                backup_root.mkdir(parents=True, exist_ok=True)
                entries: list[dict[str, Any]] = []
                for index, (relative, content) in enumerate(sorted(pages.items())):
                    pure = PurePosixPath(relative)
                    if (
                        not pure.parts
                        or pure.parts[0] != "wiki"
                        or pure.suffix != ".md"
                    ):
                        raise ValidationError(
                            "Apply can write only Markdown pages under wiki/",
                            details={"path": relative},
                        )
                    target = self._resolve_relative(relative)
                    staged = staged_root / f"{index}.md"
                    _atomic_text(staged, content)
                    backup = backup_root / f"{index}.md"
                    existed = target.is_file()
                    if existed:
                        shutil.copy2(target, backup)
                    entries.append(
                        {
                            "path": relative,
                            "staged": staged.relative_to(self.root).as_posix(),
                            "backup": (
                                backup.relative_to(self.root).as_posix()
                                if existed
                                else None
                            ),
                            "existed": existed,
                        }
                    )
                backup_targets = [
                    ".brain/registry.json",
                    ".brain/applied.json",
                    ".brain/freshness.json",
                    ".brain/index.db",
                    ".brain/index.db-shm",
                    ".brain/index.db-wal",
                ]
                backups: list[dict[str, Any]] = []
                for index, relative in enumerate(backup_targets):
                    target = self._resolve_relative(relative)
                    existed = target.is_file()
                    backup = backup_root / f"state-{index}.bak"
                    if existed:
                        shutil.copy2(target, backup)
                    backups.append(
                        {
                            "target": relative,
                            "backup": (
                                backup.relative_to(self.root).as_posix()
                                if existed
                                else None
                            ),
                            "existed": existed,
                        }
                    )
                journal = {
                    "version": 2,
                    "transaction_id": transaction_id,
                    "proposal_id": proposal_id,
                    "state": "committing",
                    "phase": "prepared",
                    "entries": entries,
                    "replaced": [],
                    "inflight": None,
                    "backups": backups,
                    "raw_move": raw_move_entry,
                }
                journal_path = self.root / ".brain" / "apply-journal.json"
                _atomic_json(journal_path, journal)
                try:
                    for entry in entries:
                        target = self._resolve_relative(entry["path"])
                        target.parent.mkdir(parents=True, exist_ok=True)
                        journal["inflight"] = entry["path"]
                        _atomic_json(journal_path, journal)
                        os.replace(self._resolve_relative(entry["staged"]), target)
                        replaced_paths = journal["replaced"]
                        if not isinstance(replaced_paths, list):
                            raise ValidationError("Apply journal is corrupt")
                        replaced_paths.append(entry["path"])
                        journal["inflight"] = None
                        _atomic_json(journal_path, journal)
                    journal["phase"] = "wiki-written"
                    _atomic_json(journal_path, journal)
                    if raw_move_entry:
                        source = self._resolve_relative(raw_move_entry["source"])
                        destination = self._resolve_relative(
                            raw_move_entry["destination"]
                        )
                        destination.parent.mkdir(parents=True, exist_ok=True)
                        raw_move_entry["inflight"] = True
                        _atomic_json(journal_path, journal)
                        os.replace(source, destination)
                        raw_move_entry["moved"] = True
                        raw_move_entry["inflight"] = False
                        records[raw_move_entry["content_hash"]].path = (
                            raw_move_entry["destination"]
                        )
                        _atomic_json(journal_path, journal)
                    for content_hash, status in source_statuses.items():
                        if content_hash not in records:
                            raise ValidationError(
                                "Cannot update an unknown source",
                                details={"source_hash": content_hash},
                            )
                        records[content_hash].status = status
                    self._write_registry_unlocked(records)
                    freshness = self._read_state_unlocked("freshness")
                    freshness["version"] = 2
                    freshness_pages = freshness.setdefault("pages", {})
                    for path, update in freshness_updates.items():
                        prior_freshness = freshness_pages.get(path, {})
                        freshness_pages[path] = {
                            **(
                                prior_freshness
                                if isinstance(prior_freshness, dict)
                                else {}
                            ),
                            **update,
                        }
                    self._write_state_unlocked("freshness", freshness)
                    journal["phase"] = "state-written"
                    _atomic_json(journal_path, journal)
                    indexed_documents = index_rebuild(records)
                    journal["phase"] = "index-written"
                    _atomic_json(journal_path, journal)
                    result = {
                        "transaction_id": transaction_id,
                        "proposal_id": proposal_id,
                        "request_hash": request_hash,
                        "paths": sorted(pages),
                        "applied_at": utc_now(),
                        "idempotent": False,
                        "indexed_documents": indexed_documents,
                        "raw_move": (
                            {
                                "content_hash": raw_move_entry["content_hash"],
                                "from": raw_move_entry["source"],
                                "to": raw_move_entry["destination"],
                            }
                            if raw_move_entry
                            else None
                        ),
                    }
                    applied.setdefault("proposals", {})[proposal_id] = result
                    self._write_state_unlocked("applied", applied)
                    journal["state"] = "committed"
                    _atomic_json(journal_path, journal)
                except Exception:
                    self._recover_apply_unlocked()
                    raise
                self._cleanup_transaction_unlocked(journal)
                return result

    def _transaction_move_destination(
        self, source: Path, branch: str, content_hash: str
    ) -> Path:
        destination = self.root / "raw" / branch / source.name
        if destination == source or not destination.exists():
            return destination
        candidate = destination.with_name(
            f"{destination.stem}-{content_hash[:10]}{destination.suffix}"
        )
        counter = 2
        while candidate.exists():
            candidate = destination.with_name(
                f"{destination.stem}-{content_hash[:10]}-{counter}{destination.suffix}"
            )
            counter += 1
        return candidate

    def read_state(self, name: str) -> dict[str, Any]:
        self._validate_state_name(name)
        with self._state_lock(name, shared=True):
            return deepcopy(self._read_state_unlocked(name))

    def mutate_state(
        self,
        name: str,
        mutator: Callable[[dict[str, Any]], dict[str, Any]],
    ) -> dict[str, Any]:
        self._validate_state_name(name)
        with self._state_lock(name, shared=False):
            current = deepcopy(self._read_state_unlocked(name))
            updated = mutator(current)
            if not isinstance(updated, dict):
                raise ValidationError("State mutator must return an object")
            self._write_state_unlocked(name, updated)
            return deepcopy(updated)

    def write_generated(self, relative_path: str, content: str) -> None:
        pure = PurePosixPath(relative_path)
        if not pure.parts or pure.parts[0] not in {"views", "graph", "output", ".brain"}:
            raise ValidationError(
                "Generated output is outside an allowed directory",
                details={"path": relative_path},
            )
        _atomic_text(self._resolve_relative(relative_path), content)

    def existing_wiki_slugs(self) -> set[str]:
        return {PurePosixPath(path).stem for path in self.wiki_pages()}

    def _capture_destination(
        self, original_name: str, content_hash: str, *, suffix: str
    ) -> Path:
        clean_stem = _slugify(Path(original_name).stem) or "source"
        normalized_suffix = suffix.lower() if suffix else ".bin"
        name = f"{utc_now()[:10]}-{clean_stem}-{content_hash[:12]}{normalized_suffix}"
        return self.root / "raw" / "_inbox" / name

    def _iter_raw_paths(self) -> Iterator[Path]:
        raw = self.root / "raw"
        if not raw.exists():
            return
        for path in sorted(raw.rglob("*")):
            if path.is_file() and not path.is_symlink():
                yield path

    def _resolve_relative(self, relative_path: str) -> Path:
        pure = PurePosixPath(relative_path)
        if pure.is_absolute() or ".." in pure.parts:
            raise ValidationError(
                "Path must be vault-relative", details={"path": relative_path}
            )
        path = (self.root / Path(*pure.parts)).resolve()
        if path != self.root and self.root not in path.parents:
            raise ValidationError("Path escapes the vault")
        return path

    def _read_registry_unlocked(self) -> dict[str, SourceRecord]:
        path = self.root / ".brain" / "registry.json"
        if not path.exists():
            return {}
        raw = json.loads(path.read_text(encoding="utf-8"))
        return {
            content_hash: SourceRecord.from_dict(value)
            for content_hash, value in raw.get("sources", {}).items()
        }

    def _write_registry_unlocked(self, records: dict[str, SourceRecord]) -> None:
        _atomic_json(
            self.root / ".brain" / "registry.json",
            {
                "version": 1,
                "sources": {
                    key: value.to_dict() for key, value in sorted(records.items())
                },
            },
        )

    def _read_state_unlocked(self, name: str) -> dict[str, Any]:
        path = self.root / ".brain" / f"{name}.json"
        if not path.exists():
            collection = {
                "freshness": "pages",
                "integration-state": "integrations",
            }.get(name, "proposals")
            return {"version": 1, collection: {}}
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValidationError(
                "Brain state must be a JSON object", details={"state": name}
            )
        return raw

    def _write_state_unlocked(self, name: str, value: dict[str, Any]) -> None:
        _atomic_json(self.root / ".brain" / f"{name}.json", value)

    def _validate_state_name(self, name: str) -> None:
        if name not in self.STATE_NAMES:
            raise ValidationError("Unknown brain state", details={"state": name})

    def _ensure_runtime_state_unlocked(self) -> None:
        defaults = {
            "freshness": {"version": 2, "pages": {}},
            "proposals": {"version": 1, "proposals": {}},
            "applied": {"version": 1, "proposals": {}},
            "integration-state": {"version": 1, "integrations": {}},
        }
        for name, value in defaults.items():
            path = self.root / ".brain" / f"{name}.json"
            if not path.exists():
                _atomic_json(path, value)

    def _migrate_wiki_directories_unlocked(self) -> None:
        """Relocate pages filed under a legacy misspelled wiki directory.

        Older builds derived the directory of a page kind by appending "s",
        which produced `wiki/synthesiss` and `wiki/entitys` while the scaffold
        created `wiki/syntheses` and `wiki/entities`. Pages already written
        there are moved to the directory that owns them, and every piece of
        state keyed by page path is rewritten alongside the move so the apply
        gate keeps recognizing them. The whole routine is a no-op once no
        legacy directory is left.

        Collision policy: the canonical page wins. A legacy copy that is
        byte-identical to it is discarded; a divergent one is quarantined
        under `.brain/legacy-pages/` (outside `wiki/`, so it is neither
        linted nor indexed) rather than overwriting live content.
        """

        moved: dict[str, str] = {}
        merged: dict[str, str] = {}
        quarantined: list[str] = []
        for legacy, canonical in sorted(LEGACY_WIKI_DIRECTORIES.items()):
            legacy_root = self.root / "wiki" / legacy
            if not legacy_root.is_dir() or legacy_root.is_symlink():
                continue
            canonical_root = self.root / "wiki" / canonical
            for source in sorted(legacy_root.glob("*.md")):
                if not source.is_file() or source.is_symlink():
                    continue
                relative = source.relative_to(self.root).as_posix()
                destination = canonical_root / source.name
                target = destination.relative_to(self.root).as_posix()
                if not destination.exists():
                    canonical_root.mkdir(parents=True, exist_ok=True)
                    os.replace(source, destination)
                    moved[relative] = target
                    continue
                content_hash, _ = _hash_file(source)
                if content_hash == _hash_file(destination)[0]:
                    source.unlink()
                    merged[relative] = target
                    continue
                keep = (
                    self.root
                    / ".brain"
                    / "legacy-pages"
                    / legacy
                    / f"{source.stem}-{content_hash[:12]}{source.suffix}"
                )
                keep.parent.mkdir(parents=True, exist_ok=True)
                os.replace(source, keep)
                quarantined.append(relative)
            if not any(legacy_root.iterdir()):
                legacy_root.rmdir()
        if not moved and not merged and not quarantined:
            return
        self._migrate_state_paths_unlocked(moved, merged, quarantined)
        self._migrate_index_paths_unlocked(moved, merged, quarantined)

    def _migrate_state_paths_unlocked(
        self,
        moved: dict[str, str],
        merged: dict[str, str],
        quarantined: list[str],
    ) -> None:
        renames = {**moved, **merged}
        with self._state_lock("applied", shared=False):
            applied = self._read_state_unlocked("applied")
            proposals = applied.get("proposals")
            changed = False
            if isinstance(proposals, dict):
                for result in proposals.values():
                    if not isinstance(result, dict):
                        continue
                    paths = result.get("paths")
                    if not isinstance(paths, list):
                        continue
                    updated = sorted(renames.get(str(path), str(path)) for path in paths)
                    if updated != paths:
                        result["paths"] = updated
                        changed = True
            if changed:
                self._write_state_unlocked("applied", applied)
        with self._state_lock("freshness", shared=False):
            freshness = self._read_state_unlocked("freshness")
            pages = freshness.get("pages")
            if not isinstance(pages, dict):
                return
            changed = False
            for old, new in renames.items():
                if old not in pages:
                    continue
                entry = pages.pop(old)
                changed = True
                # The move preserves content, so the tracked content_hash stays
                # valid at the new path. An entry already filed under the
                # canonical path describes the surviving file and wins.
                if new not in pages:
                    pages[new] = entry
            for old in quarantined:
                if pages.pop(old, None) is not None:
                    changed = True
            if changed:
                self._write_state_unlocked("freshness", freshness)

    def _migrate_index_paths_unlocked(
        self,
        moved: dict[str, str],
        merged: dict[str, str],
        quarantined: list[str],
    ) -> None:
        database = self.root / ".brain" / "index.db"
        if not database.is_file():
            return
        try:
            with closing(sqlite3.connect(database, timeout=10)) as connection:
                with connection:
                    for old, new in moved.items():
                        connection.execute(
                            "DELETE FROM search_fts WHERE path = ?", (new,)
                        )
                        connection.execute(
                            "UPDATE search_fts SET path = ? WHERE path = ?", (new, old)
                        )
                    for old in (*merged, *quarantined):
                        connection.execute(
                            "DELETE FROM search_fts WHERE path = ?", (old,)
                        )
        except sqlite3.Error:
            # The index is disposable: drop it and let the next reindex rebuild.
            for suffix in ("", "-wal", "-shm"):
                database.with_name(database.name + suffix).unlink(missing_ok=True)

    def _recover_apply_unlocked(self) -> None:
        journal_path = self.root / ".brain" / "apply-journal.json"
        if not journal_path.is_file():
            return
        journal = json.loads(journal_path.read_text(encoding="utf-8"))
        if journal.get("state") != "committed":
            replaced = set(journal.get("replaced", []))
            if journal.get("inflight"):
                replaced.add(str(journal["inflight"]))
            for entry in journal.get("entries", []):
                path = str(entry.get("path", ""))
                if path not in replaced:
                    continue
                target = self._resolve_relative(path)
                backup = entry.get("backup")
                if backup:
                    shutil.copy2(self._resolve_relative(str(backup)), target)
                else:
                    target.unlink(missing_ok=True)
            raw_move = journal.get("raw_move")
            if isinstance(raw_move, dict) and (
                raw_move.get("moved") or raw_move.get("inflight")
            ):
                source = self._resolve_relative(str(raw_move.get("source", "")))
                destination = self._resolve_relative(
                    str(raw_move.get("destination", ""))
                )
                source.parent.mkdir(parents=True, exist_ok=True)
                if destination.is_file() and not source.exists():
                    os.replace(destination, source)
                elif destination.is_file() and source.exists():
                    destination.unlink()
            backups = journal.get("backups")
            if isinstance(backups, list):
                for entry in backups:
                    if not isinstance(entry, dict):
                        continue
                    target = self._resolve_relative(str(entry.get("target", "")))
                    target.unlink(missing_ok=True)
                    backup = entry.get("backup")
                    if backup:
                        target.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(self._resolve_relative(str(backup)), target)
            else:
                for backup_key, target_name in (
                    ("registry_backup", "registry.json"),
                    ("applied_backup", "applied.json"),
                ):
                    backup = journal.get(backup_key)
                    if backup:
                        shutil.copy2(
                            self._resolve_relative(str(backup)),
                            self.root / ".brain" / target_name,
                        )
        self._cleanup_transaction_unlocked(journal)

    def _cleanup_transaction_unlocked(self, journal: dict[str, Any]) -> None:
        transaction_id = str(journal.get("transaction_id", ""))
        if re.fullmatch(r"[0-9a-f]{32}", transaction_id):
            transaction_root = self.root / ".brain" / "transactions" / transaction_id
            if transaction_root.is_dir():
                shutil.rmtree(transaction_root)
        (self.root / ".brain" / "apply-journal.json").unlink(missing_ok=True)

    @contextmanager
    def _registry_lock(self, *, shared: bool) -> Iterator[None]:
        path = self.root / ".brain" / "registry.lock"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a+", encoding="utf-8") as handle:
            fcntl.flock(handle, fcntl.LOCK_SH if shared else fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    @contextmanager
    def _state_lock(self, name: str, *, shared: bool) -> Iterator[None]:
        path = self.root / ".brain" / f"{name}.lock"
        with path.open("a+", encoding="utf-8") as handle:
            fcntl.flock(handle, fcntl.LOCK_SH if shared else fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    @contextmanager
    def _write_lock(self) -> Iterator[None]:
        path = self.root / ".brain" / "write.lock"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a+", encoding="utf-8") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)


def _resolve_record(
    records: dict[str, SourceRecord], identifier: str
) -> SourceRecord:
    if identifier in records:
        return records[identifier]
    matches = [
        record
        for content_hash, record in records.items()
        if content_hash.startswith(identifier) or record.path == identifier
    ]
    if not matches:
        raise NotFoundError("Source was not found", details={"identifier": identifier})
    if len(matches) > 1:
        raise ValidationError(
            "Source identifier is ambiguous", details={"identifier": identifier}
        )
    return matches[0]


def _hash_file(path: Path, chunk_size: int = 1024 * 1024) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        while chunk := handle.read(chunk_size):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def _slugify(value: str) -> str:
    lowered = "".join(
        character
        for character in unicodedata.normalize("NFKD", value.lower())
        if not unicodedata.combining(character)
    )
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", lowered)).strip("-")


def _atomic_copy(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        mode="wb", dir=target.parent, prefix=f".{target.name}.", delete=False
    )
    try:
        with source.open("rb") as reader, handle:
            shutil.copyfileobj(reader, handle, length=1024 * 1024)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(handle.name, target)
    except Exception:
        Path(handle.name).unlink(missing_ok=True)
        raise


def _atomic_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        mode="wb", dir=path.parent, prefix=f".{path.name}.", delete=False
    )
    try:
        with handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(handle.name, path)
    except Exception:
        Path(handle.name).unlink(missing_ok=True)
        raise


def _atomic_text(path: Path, content: str) -> None:
    _atomic_bytes(path, content.encode("utf-8"))


def _atomic_json(path: Path, content: dict[str, Any]) -> None:
    _atomic_text(path, json.dumps(content, indent=2, ensure_ascii=False) + "\n")


def _system_page(slug: str, title: str) -> str:
    now = utc_now()
    return (
        "---\n"
        f'id: "system:{slug}"\n'
        'type: "system"\n'
        f"title: {json.dumps(title)}\n"
        "aliases:\n"
        "sources:\n"
        f'updated_at: "{now}"\n'
        "---\n\n"
        f"# {title}\n"
    )
