from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import shutil
import tempfile
import unicodedata
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from copy import deepcopy
from importlib.resources import files
from pathlib import Path, PurePosixPath
from typing import Any, ClassVar

from brainskit.application.ports import ApplyPlan, SearchIndexPort
from brainskit.domain.model import (
    LEGACY_WIKI_DIRECTORIES,
    WIKI_DIRECTORIES,
    ConflictError,
    NotConfiguredError,
    NotFoundError,
    RefusalError,
    SourceRecord,
    ValidationError,
    VaultConfig,
    normalize_branch,
    proposal_id_reuse_error,
    utc_now,
)
from brainskit.infrastructure.converters import extract_document, guess_media_type
from brainskit.infrastructure.index import SqliteFtsIndex

#: The only directories `write_generated` may write into. Everything here is
#: derived from `raw/` and `wiki/`, which are the source of truth.
GENERATED_DIRECTORIES: tuple[str, ...] = ("views", "graph", "output", ".brain")

#: Of those, the ones whose *entire* contents are reproducible, and so belong
#: in `.gitignore`. `.brain/` is excluded because it is mixed: `config.json`,
#: `registry.json` and `freshness.json` are the vault's own state and are meant
#: to be committed, while the index, the locks and the AST cache are not --
#: hence `_GITIGNORE_STATE_ENTRIES` naming those individually.
#:
#: Kept beside `GENERATED_DIRECTORIES` rather than spelled into the ignore file
#: because the two drifted: `graph/`, `views/` and `output/` were generated from
#: the beginning and never ignored, so every vault sited inside a repository was
#: committing its own derived output -- here, a 683 MB `graph/code.json`. A test
#: asserts the two lists still account for each other, so a fourth generated
#: directory cannot be added without deciding whether it is committable.
DERIVED_DIRECTORIES: tuple[str, ...] = ("graph", "output", "views")

_GITIGNORE_STATE_ENTRIES: tuple[str, ...] = (
    ".brain/index.db",
    ".brain/index.db-shm",
    ".brain/index.db-wal",
    ".brain/*.lock",
    ".brain/services/",
    ".brain/code-cache/",
    ".brain/extracted/",
)

_GITIGNORE_BEGIN = "# >>> brainskit >>>"
_GITIGNORE_END = "# <<< brainskit <<<"


def gitignore_block() -> str:
    """The lines brainskit owns in a vault's `.gitignore`, fenced by markers.

    Fenced so the block can be rewritten in place on a later run without
    touching anything around it -- the same idempotency key the agent-hook
    installer uses, and for the same reason.
    """

    entries = [f"{name}/" for name in DERIVED_DIRECTORIES]
    entries.extend(_GITIGNORE_STATE_ENTRIES)
    body = "\n".join(entries)
    return f"{_GITIGNORE_BEGIN}\n{body}\n{_GITIGNORE_END}\n"


def merge_gitignore(existing: str, block: str) -> str:
    """Splice brainskit's block into `existing`, replacing a previous one.

    `initialize` used to write this file unconditionally, so running `bk init`
    at the root of a repository that already had a `.gitignore` destroyed it --
    silently, and with no way back short of git. Appending instead means the
    only file brainskit writes outside its own directories can no longer lose
    anyone's work.
    """

    if _GITIGNORE_BEGIN in existing and _GITIGNORE_END in existing:
        head, _, rest = existing.partition(_GITIGNORE_BEGIN)
        _, _, tail = rest.partition(_GITIGNORE_END)
        return head + block + tail.lstrip("\n")
    if not existing.strip():
        return block
    return existing.rstrip("\n") + "\n\n" + block


class FileVault:
    """Filesystem adapter for the Markdown source of truth."""

    STATE_NAMES: ClassVar[set[str]] = {
        "applied",
        "freshness",
        "proposals",
        "integration-state",
        "enrichment",
    }

    def __init__(self, root: Path):
        self.root = root.expanduser().resolve()
        if not (self.root / ".brain" / "config.json").is_file():
            raise NotFoundError(
                "Not a brainskit vault",
                details={"vault": str(self.root), "hint": "Run bk init first"},
            )
        with self._write_lock():
            self._ensure_runtime_state_unlocked()
            self._recover_apply_unlocked()
            self._migrate_wiki_directories_unlocked()

    @classmethod
    def initialize(
        cls, root: Path, raw_config: dict[str, Any], *, force: bool = False
    ) -> FileVault:
        root = root.expanduser().resolve()
        config = VaultConfig.from_dict(raw_config)
        config_path = root / ".brain" / "config.json"
        if config_path.exists():
            raise ConflictError(
                "Vault is already initialized", details={"vault": str(root)}
            )
        cls._refuse_bad_site(root, force=force)
        cls._refuse_orphaned_brain(root, force=force)
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
        _atomic_json(root / ".brain" / "enrichment.json", {"version": 1, "edges": {}})
        _atomic_json(
            root / ".brain" / "integration-state.json",
            {"version": 1, "integrations": {}},
        )
        schema_text = (
            files("brainskit")
            .joinpath("templates/default/schema.json")
            .read_text(encoding="utf-8")
        )
        _atomic_text(root / ".brain" / "schema.json", schema_text.rstrip() + "\n")
        gitignore_path = root / ".gitignore"
        try:
            existing = gitignore_path.read_text(encoding="utf-8")
        except OSError:
            existing = ""
        _atomic_text(gitignore_path, merge_gitignore(existing, gitignore_block()))
        index_page = _system_page("index", "Brainskit index")
        log_page = _system_page("log", "Brainskit log")
        _atomic_text(root / "wiki" / "index.md", index_page)
        _atomic_text(root / "wiki" / "log.md", log_page)
        return cls(root)

    @staticmethod
    def _refuse_bad_site(root: Path, *, force: bool) -> None:
        """Refuse the three sitings that cannot be what anyone meant.

        A vault's location decides what its code graph scans, and by the time
        that is visible the scan has already happened. All three of these
        produced real damage rather than a tidiness complaint:

        - **Home, or anything above it.** Everything on the machine becomes one
          project.
        - **Inside another vault.** `discover()` walks up and stops at the first
          marker, so which vault a command addresses would depend on the
          directory it was run from.
        - **A workspace holding projects.** This is the one that actually
          happened: `~/Projetos/tools` holds three checkouts, so the vault's
          code root resolved to `~/Projetos` and indexed 55,295 files.

        Only the third is overridable, because only the third is ever a
        deliberate choice -- a monorepo whose sub-projects each carry a `.git`
        is a legitimate single project.
        """

        if _is_above_projects(root):
            raise RefusalError(
                "Refusing to create a vault here",
                details={
                    "vault": str(root),
                    "reason": (
                        "this is your home directory or above it, so the code "
                        "graph would treat the whole machine as one project"
                    ),
                    "hint": "Create it inside a project, e.g. <repo>/.brainskit",
                },
            )
        for parent in root.parents:
            if (parent / ".brain" / "config.json").is_file():
                raise RefusalError(
                    "Refusing to nest a vault inside another vault",
                    details={
                        "vault": str(root),
                        "enclosing_vault": str(parent),
                        "hint": (
                            "Commands discover the nearest vault above the "
                            "working directory, so nesting makes which one "
                            "they address depend on where they are run"
                        ),
                    },
                )
        if force or (root / ".git").exists():
            return
        repos = workspace_repos(root)
        if len(repos) >= 2:
            raise RefusalError(
                "Refusing to create a vault in a directory that holds projects",
                details={
                    "vault": str(root),
                    "repositories": [repo.name for repo in repos],
                    "reason": (
                        f"{len(repos)} git repositories sit directly inside it, "
                        "so this is a workspace rather than a project and the "
                        "code graph would scan all of them"
                    ),
                    "hint": (
                        f"Create it inside one of them, e.g. "
                        f"{repos[0].name}/.brainskit — or pass --force"
                    ),
                },
            )

    @staticmethod
    def _refuse_orphaned_brain(root: Path, *, force: bool) -> None:
        """Refuse to initialize over a `.brain/` a lost setup left behind.

        `.brain/` is internal bookkeeping nobody hand-creates, so finding real
        content in it -- a cache directory, a lock file, a queued transaction
        -- while `config.json` itself is absent can only mean one thing: a
        previous vault setup here was interrupted or lost its config (a `git
        stash` mid-relocation, an aborted `bk init`, a manual deletion).
        Proceeding anyway does not start fresh; it silently creates a second,
        disconnected vault on top of the wreckage, and the only way back is
        reconstructing what happened from git stash and reflog after the fact
        -- which is exactly what happened the one time this went unnoticed.

        Never triggered by `raw/` or `wiki/` holding real content: bootstrapping
        a vault around markdown someone already has is a normal, legitimate
        workflow, and neither directory is internal state.
        """

        if force:
            return
        brain_dir = root / ".brain"
        if (brain_dir / "config.json").is_file():
            return  # already initialized -- the ConflictError above covers it
        found = _brain_directory_contents(brain_dir)
        if not found:
            return
        raise RefusalError(
            "Refusing to initialize: .brain/ already holds content with no "
            "config.json",
            details={
                "vault": str(root),
                "found": found,
                "reason": (
                    "`.brain/` is internal bookkeeping a human never "
                    "hand-creates, so this usually means a previous vault "
                    "setup here was interrupted or lost its config -- a "
                    "`git stash`, an aborted `bk init`, or a manual deletion"
                ),
                "hint": (
                    "Check `git stash list` and `git log --all -- "
                    f"{brain_dir / 'config.json'}` before proceeding -- the "
                    "original config may be sitting in a stash rather than "
                    "gone. Pass --force to start fresh over the leftovers."
                ),
            },
        )

    @classmethod
    def discover(cls, start: Path | None = None) -> FileVault:
        current = (start or Path.cwd()).expanduser().resolve()
        if current.is_file():
            current = current.parent
        for candidate in (current, *current.parents):
            if (candidate / ".brain" / "config.json").is_file():
                return cls(candidate)
            # Two conventional nested layouts, neither a guess: `docs/brain`
            # sits beside a repository's own published documentation, and
            # `.brainskit` is the guided wizard's own default -- hidden and
            # tool-owned, beside `.git`. A vault at either is one `bk init`
            # created, not a directory this walk is hoping is a vault.
            # `.brainkit` is the pre-0.5 spelling of the same convention. A
            # vault created under the old name is still a vault, and refusing
            # to see it would strand it for no reason.
            for conventional in (
                candidate / "docs" / "brain",
                candidate / ".brainskit",
                candidate / ".brainkit",
            ):
                if (conventional / ".brain" / "config.json").is_file():
                    return cls(conventional)
        raise NotFoundError(
            "No brainskit vault found",
            details={"start": str(current), "hint": "Pass --vault or run bk init"},
        )

    @property
    def index_path(self) -> Path:
        return self.root / ".brain" / "index.db"

    @property
    def code_cache_dir(self) -> Path:
        """Persistent home for the code extractor's own AST cache.

        The vendored Graphify extraction (`infrastructure/extractor.py`) keeps
        a per-file parse cache that skips reparsing files whose content hash
        it has already seen — a real win on a large repository, but only if
        it is given somewhere durable to write between calls. `.brain/` is
        already where this vault keeps every other piece of its own private
        state (`index_path` above, the registry, freshness); nesting the code
        cache there rather than in the scanned repository or the process cwd
        follows that precedent: it can never be mistaken for the user's own
        files, and `FileVault.initialize` already git-ignores it. A caller
        that does not have a vault handy (or does not want persistence) can
        still call the extractor without this — it just gets a cold,
        throwaway cache instead, same as before this existed.

        Deliberately unrelated to `CodeGraph.staleness`'s freshness fingerprint
        (`application/codegraph.py`) even though both hash file content — see
        `infrastructure/extractor.py`'s module docstring for why re-reading
        content on every staleness check is not the same job as memoising a
        parse, and must not be merged into it.
        """
        return self.root / ".brain" / "code-cache"

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
                    media_type=guess_media_type(source.name),
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
                media_type=guess_media_type(name, default="text/markdown"),
                size=len(payload),
                captured_at=utc_now(),
            )
            records[content_hash] = record
            self._write_registry_unlocked(records)
            return record, True

    def reconcile(self) -> dict[str, int]:
        """Heal paths after manual moves; add untracked raw files.

        Reports `missing` (registered files no longer on disk) but never
        removes those entries — a moved file is indistinguishable from a
        briefly-absent one mid-move, so deleting on sight would be a race.
        `forget` is the explicit, one-record way to drop a genuinely gone
        source.
        """
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
                    media_type=guess_media_type(path.name),
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

    def forget(self, identifier: str, *, force: bool = False) -> SourceRecord:
        """Drop one source record from the registry.

        The supported exit for `registry.missing_file`: a record whose raw
        file is gone stays in the registry forever otherwise, since
        `reconcile` only ever heals paths and additions, never removes an
        entry (see its docstring). Refuses to drop a record whose raw file
        is still on disk unless `force` is set, so this cannot be used to
        silently lose a source that is still captured.
        """
        with self._registry_lock(shared=False):
            records = self._read_registry_unlocked()
            record = _resolve_record(records, identifier)
            if not force and (self.root / record.path).is_file():
                raise RefusalError(
                    "Source is still on disk; pass force to forget it anyway",
                    details={"path": record.path},
                )
            del records[record.content_hash]
            self._write_registry_unlocked(records)
            return record

    def file_source(self, identifier: str, branch: str) -> SourceRecord:
        branch = normalize_branch(branch)
        config = self.config()
        if branch not in config.branches:
            raise NotConfiguredError(
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

    def code_root(self) -> Path:
        """Directory that `code_sources` paths are relative to."""

        return self.code_root_reason()[0]

    def code_root_reason(self) -> tuple[Path, str]:
        """The code root and, in one phrase, why it is that directory.

        Configured, or discovered by walking up for a `.git` directory. The
        common layout puts a vault inside the repository it documents
        (`repo/.brainskit`), and nothing else about that layout says where the
        repository starts — so the marker git already maintains is a better
        answer than counting `..` segments and hoping.

        **The walk is bounded in both directions, which it once was not.** The
        old fallback, when no `.git` was found anywhere, was `self.root.parent`
        — so a vault created in a directory that merely *holds* projects handed
        the extractor that whole directory. That is not hypothetical: a vault at
        `~/Projetos/tools` scanned `~/Projetos`, 55,295 files across every
        unrelated repository on the machine, because a non-VCS root also
        collapses the extractor's own `.gitignore` ceiling to the scan root and
        nothing is excluded. The fallback is now the vault itself: a vault with
        no discoverable project indexes only what it owns, which is empty and
        obviously wrong, rather than enormous and quietly wrong.

        The upward walk stops before `~` and the filesystem root for the same
        reason. Neither is a project, and returning one is always a mistake,
        however the vault came to be sited there.

        The reason string is returned alongside because "which directory does
        this vault consider its code" is exactly the question nobody thought to
        ask before running a build, and `bk status`/`bk doctor` now put the
        answer on screen.
        """

        configured = self.config().code_root
        if configured is not None:
            resolved = (self.root / configured).resolve()
            return resolved, f"configured code_root {configured!r}"
        for candidate in (self.root, *self.root.parents):
            if _is_above_projects(candidate):
                break
            if (candidate / ".git").exists():
                return candidate, f"git repository at {candidate}"
        return self.root, "no repository found — the vault indexes only itself"

    def code_hash(self, relative_path: str) -> str | None:
        """SHA-256 of a file under the code root, or None when it is not there.

        Missing is a legitimate answer rather than an error: a cited file being
        deleted is exactly the kind of drift lint exists to report, and raising
        here would stop the whole lint on the first one.
        """

        root = self.code_root()
        candidate = (root / relative_path).resolve()
        # A path that escapes the root reads a file the vault never sanctioned.
        # `CodeSource` rejects `..` on the way in; this is the second lock, for
        # symlinks and for any path that reached the registry another way.
        if not candidate.is_relative_to(root) or not candidate.is_file():
            return None
        content_hash, _ = _hash_file(candidate)
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

    def commit_wiki_batch(self, plan: ApplyPlan) -> dict[str, Any]:
        with self._write_lock():
            self._recover_apply_unlocked()
            with self._registry_lock(shared=False), self._state_lock(
                "applied", shared=False
            ), self._state_lock("freshness", shared=False):
                applied = self._read_state_unlocked("applied")
                prior = applied.get("proposals", {}).get(plan.proposal_id)
                if isinstance(prior, dict):
                    if prior.get("request_hash") != plan.request_hash:
                        # The gate checked this before taking the lock; this is
                        # the authoritative answer, and it has to be the *same*
                        # answer, so both raise through one constructor.
                        raise proposal_id_reuse_error(
                            plan.proposal_id,
                            applied_request_hash=prior.get("request_hash"),
                            request_hash=plan.request_hash,
                        )
                    return {**prior, "idempotent": True}
                for relative, expected in plan.expected_versions.items():
                    observed = self.wiki_version(relative)
                    if observed != expected:
                        raise ConflictError(
                            "Wiki page changed after context was built",
                            details={
                                "path": relative,
                                "expected": expected,
                                "observed": observed,
                            },
                        )
                records = self._read_registry_unlocked()
                raw_move_entry: dict[str, Any] | None = None
                if plan.raw_move:
                    content_hash, branch = plan.raw_move
                    branch = normalize_branch(branch)
                    if branch not in self.config().branches:
                        raise NotConfiguredError(
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
                for index, (relative, content) in enumerate(sorted(plan.pages.items())):
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
                    "proposal_id": plan.proposal_id,
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
                        # A source already sitting in the destination branch is
                        # not moved: `_transaction_move_destination` hands back
                        # the source path itself. Marking that `moved` would
                        # journal a rollback instruction for something that
                        # never happened, and its reversal has one file under
                        # both names -- so rollback would delete the only copy
                        # of raw evidence, which no backup here covers. Leaving
                        # both flags false gives rollback nothing to reverse.
                        if destination != source:
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
                    for content_hash, status in plan.source_statuses.items():
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
                    for path, update in plan.freshness_updates.items():
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
                    indexed_documents = plan.index_rebuild(records)
                    journal["phase"] = "index-written"
                    _atomic_json(journal_path, journal)
                    result = {
                        "transaction_id": transaction_id,
                        "proposal_id": plan.proposal_id,
                        "request_hash": plan.request_hash,
                        "paths": sorted(plan.pages),
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
                    applied.setdefault("proposals", {})[plan.proposal_id] = result
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
        if not pure.parts or pure.parts[0] not in GENERATED_DIRECTORIES:
            raise RefusalError(
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
            raise RefusalError("Path escapes the vault")
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
                "enrichment": "edges",
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
        """Tell the index where the pages went.

        `merged` and `quarantined` arrive separately because the *vault* has to
        tell them apart -- one discards a byte-identical duplicate, the other
        preserves a divergent page outside `wiki/` -- but both leave no file at
        the old path, which is the only thing the index can act on. Collapsing
        them here keeps that classification on this side of the port instead of
        asking the index to learn a legacy-directory vocabulary.

        The adapter is constructed here rather than injected because this runs
        from `__init__`, and the index is built *from* `index_path`: a vault has
        to exist before its index can. The vault already owns where that file
        lives, and now stops claiming to know what is inside it.
        """

        index: SearchIndexPort = SqliteFtsIndex(self.index_path)
        index.rename_paths(moved, [*merged, *quarantined])

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
                # Recovery reads a journal some earlier process wrote, which may
                # be an older build that still marked a same-path move `moved`.
                # The writer no longer produces that record, but this side has
                # to survive one: both names reach a single file, so the
                # reversal below would take its `source.exists()` branch and
                # unlink the vault's only copy of that raw evidence. There is
                # nothing to reverse when the two paths are one path.
                if destination != source:
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


def _is_above_projects(candidate: Path) -> bool:
    """Whether `candidate` is too high up to ever be one project's root.

    True for the filesystem root, for the home directory, and for anything
    between them (`/Users`, `/home`). Those directories hold *many* unrelated
    trees, so returning one as a code root means scanning all of them — and an
    upward walk that is allowed to reach `/` will always find something up
    there eventually, since `.git` is not the only marker a walk might use.

    Deliberately not extended to "any directory containing several
    repositories": `~/Projetos` is such a directory, but so is a legitimate
    monorepo with vendored submodules, and refusing that at *read* time would
    break vaults that are working. The shape is caught where it is actually
    decidable — `bk init` refuses to site a vault in one (`workspace_repos`),
    and `CodeGraph.build` refuses to scan more files than a project has.
    """

    home = Path.home().resolve()
    return candidate == candidate.parent or candidate == home or home.is_relative_to(candidate)


def workspace_repos(directory: Path, limit: int = 8) -> list[Path]:
    """Git repositories one level below `directory`, up to `limit`.

    Two or more of these mean `directory` is a workspace holding projects
    rather than a project itself — the shape that produced a vault at
    `~/Projetos/tools` sitting beside `brainskit/`, `gitlab-mcp/` and `lp-hp/`.
    Capped because the answer is only ever compared against two, and a home
    directory full of checkouts should not cost a full listing.
    """

    found: list[Path] = []
    try:
        entries = sorted(directory.iterdir())
    except OSError:
        return found
    for entry in entries:
        if entry.is_symlink() or not entry.is_dir():
            continue
        if (entry / ".git").exists():
            found.append(entry)
            if len(found) >= limit:
                break
    return found


def _brain_directory_contents(brain_dir: Path) -> list[str]:
    """Top-level `.brain/` entries that count as real, orphaned content.

    A file counts regardless of size -- the incident this guards against left
    behind five 0-byte `*.lock` files. A subdirectory counts only if it is not
    itself empty, so a bare `mkdir -p .brain` with nothing in it is not
    mistaken for wreckage. Returns `[]`, rather than raising, when `.brain/`
    does not exist at all.
    """

    found: list[str] = []
    try:
        entries = sorted(brain_dir.iterdir())
    except OSError:
        return found
    for entry in entries:
        if entry.is_file():
            found.append(entry.name)
        elif entry.is_dir() and any(entry.iterdir()):
            found.append(entry.name)
    return found


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
