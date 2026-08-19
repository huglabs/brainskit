"""Two-phase commit for the only path that writes `wiki/`.

An apply stages every page beside the vault, backs up everything it is about
to overwrite, writes a journal saying what it is doing, and only then starts
replacing files. If the process dies at any point in that sequence, the next
`bk` command opens the vault, finds the journal, and puts the vault back the
way it was. `state: committed` is the line: before it the whole apply is
undone, after it nothing is.

This lived inside `FileVault` and could only be reached by interrupting a real
apply, which is why its rollback branches went untested for the life of the
feature -- and why a characterization pass over them
(`tests/test_apply_journal_recovery.py`) found a real data-loss defect the
moment they were exercised at all. So the engine takes a `FailurePoint`: a
constructor-injected instruction to stop at one named checkpoint, as if the
power had gone. Production never supplies one.

The vault keeps the locks. Everything here runs with `write.lock`,
`registry.lock`, `applied.lock` and `freshness.lock` already held by
`FileVault.commit_wiki_batch`, in that order -- which is also why this takes
narrow accessors instead of the vault itself. `VaultPort`'s public methods
each take the lock they need; calling one from in here would ask for a lock
this process already holds on another descriptor, and `flock` would block
forever rather than notice.
"""

from __future__ import annotations

import copy
import json
import os
import re
import shutil
import uuid
from collections.abc import Callable, Collection
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from brainskit.application.ports import ApplyPlan
from brainskit.domain.model import (
    ConflictError,
    NotConfiguredError,
    NotFoundError,
    SourceRecord,
    ValidationError,
    normalize_branch,
    proposal_id_reuse_error,
    utc_now,
)

#: Every moment this engine can be asked to die at. Each names a point where
#: the journal on disk has just been brought up to date, so "crashed at X"
#: describes a state a real crash can leave behind rather than an internal
#: instant no journal describes.
CHECKPOINTS: tuple[str, ...] = (
    "prepared",
    "page-inflight",
    "page-replaced",
    "wiki-written",
    "raw-move-inflight",
    "raw-move-applied",
    "state-written",
    "index-written",
    "applied-recorded",
    "committed",
)


class InterruptedApply(BaseException):
    """The injected failure: the power cut, the SIGKILL or the full disk.

    Deliberately not an `Exception`. `commit` rolls back in its own `except
    Exception` clause, and a test that asked for a crash wants the
    half-finished vault a crash leaves behind -- not the tidy rollback an
    error gets. Deriving from `BaseException` is what makes the two
    distinguishable at the one place it matters.
    """


@dataclass(frozen=True, slots=True)
class FailurePoint:
    """Where an apply should stop, as if the process had died there.

    `step` must name a checkpoint; a typo raises here rather than producing a
    test that silently never fires. `detail` narrows a per-page checkpoint to
    one page, and `occurrence` selects the nth time the checkpoint is reached
    -- which is how "die at the third page replace" is spelled.
    """

    step: str
    detail: str | None = None
    occurrence: int = 1

    def __post_init__(self) -> None:
        if self.step not in CHECKPOINTS:
            raise ValueError(
                f"unknown apply checkpoint {self.step!r}; "
                f"expected one of {', '.join(CHECKPOINTS)}"
            )
        if self.occurrence < 1:
            raise ValueError("occurrence counts from 1")


class ApplyTransaction:
    """Commits one `ApplyPlan`, and undoes an interrupted one.

    Constructed with the vault capabilities it needs and nothing else. The
    list is the audit: this engine cannot reach any other part of the vault,
    and every one of these is an *unlocked* accessor because the caller is
    holding the locks already.
    """

    def __init__(
        self,
        root: Path,
        *,
        resolve: Callable[[str], Path],
        read_registry: Callable[[], dict[str, SourceRecord]],
        write_registry: Callable[[dict[str, SourceRecord]], None],
        read_state: Callable[[str], dict[str, Any]],
        write_state: Callable[[str, dict[str, Any]], None],
        branches: Callable[[], Collection[str]],
        page_version: Callable[[str], str | None],
        resolve_record: Callable[[dict[str, SourceRecord], str], SourceRecord],
        write_json: Callable[[Path, dict[str, Any]], None],
        write_text: Callable[[Path, str], None],
        fail_at: FailurePoint | None = None,
    ):
        self.root = root
        self._resolve = resolve
        self._read_registry = read_registry
        self._write_registry = write_registry
        self._read_state = read_state
        self._write_state = write_state
        self._branches = branches
        self._page_version = page_version
        self._resolve_record = resolve_record
        self._write_json = write_json
        self._write_text = write_text
        self._fail_at = fail_at
        self._reached = 0

    @property
    def journal_path(self) -> Path:
        return self.root / ".brain" / "apply-journal.json"

    def crashing_at(self, point: FailurePoint) -> ApplyTransaction:
        """A copy of this engine that stops at `point`.

        The only way to install a failure point after construction, and the
        one tests use: `vault._apply = vault._apply.crashing_at(...)`. There
        is no environment variable and no default -- an engine crashes only
        because a caller named the checkpoint, which is what keeps the seam
        from firing in production.
        """

        clone = copy.copy(self)
        clone._fail_at = point
        clone._reached = 0
        return clone

    def _checkpoint(self, step: str, detail: str | None = None) -> None:
        point = self._fail_at
        if point is None or point.step != step:
            return
        if point.detail is not None and point.detail != detail:
            return
        self._reached += 1
        if self._reached < point.occurrence:
            return
        raise InterruptedApply(
            f"interrupted at {step}"
            + (f" ({detail})" if detail is not None else "")
            + (f", occurrence {point.occurrence}" if point.occurrence > 1 else "")
        )

    def commit(self, plan: ApplyPlan) -> dict[str, Any]:
        applied = self._read_state("applied")
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
            observed = self._page_version(relative)
            if observed != expected:
                raise ConflictError(
                    "Wiki page changed after context was built",
                    details={
                        "path": relative,
                        "expected": expected,
                        "observed": observed,
                    },
                )
        records = self._read_registry()
        raw_move_entry: dict[str, Any] | None = None
        if plan.raw_move:
            content_hash, branch = plan.raw_move
            branch = normalize_branch(branch)
            if branch not in self._branches():
                raise NotConfiguredError(
                    "Destination branch is not configured",
                    details={"branch": branch},
                )
            record = self._resolve_record(records, content_hash)
            source = self._resolve(record.path)
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
        transaction_root = self.root / ".brain" / "transactions" / transaction_id
        staged_root = transaction_root / "staged"
        backup_root = transaction_root / "backups"
        staged_root.mkdir(parents=True, exist_ok=True)
        backup_root.mkdir(parents=True, exist_ok=True)
        entries: list[dict[str, Any]] = []
        for index, (relative, content) in enumerate(sorted(plan.pages.items())):
            pure = PurePosixPath(relative)
            if not pure.parts or pure.parts[0] != "wiki" or pure.suffix != ".md":
                raise ValidationError(
                    "Apply can write only Markdown pages under wiki/",
                    details={"path": relative},
                )
            target = self._resolve(relative)
            staged = staged_root / f"{index}.md"
            self._write_text(staged, content)
            backup = backup_root / f"{index}.md"
            existed = target.is_file()
            if existed:
                shutil.copy2(target, backup)
            entries.append(
                {
                    "path": relative,
                    "staged": staged.relative_to(self.root).as_posix(),
                    "backup": (
                        backup.relative_to(self.root).as_posix() if existed else None
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
            target = self._resolve(relative)
            existed = target.is_file()
            backup = backup_root / f"state-{index}.bak"
            if existed:
                shutil.copy2(target, backup)
            backups.append(
                {
                    "target": relative,
                    "backup": (
                        backup.relative_to(self.root).as_posix() if existed else None
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
        journal_path = self.journal_path
        self._write_json(journal_path, journal)
        self._checkpoint("prepared")
        try:
            for entry in entries:
                target = self._resolve(entry["path"])
                target.parent.mkdir(parents=True, exist_ok=True)
                journal["inflight"] = entry["path"]
                self._write_json(journal_path, journal)
                self._checkpoint("page-inflight", entry["path"])
                os.replace(self._resolve(entry["staged"]), target)
                replaced_paths = journal["replaced"]
                if not isinstance(replaced_paths, list):
                    raise ValidationError("Apply journal is corrupt")
                replaced_paths.append(entry["path"])
                journal["inflight"] = None
                self._write_json(journal_path, journal)
                self._checkpoint("page-replaced", entry["path"])
            journal["phase"] = "wiki-written"
            self._write_json(journal_path, journal)
            self._checkpoint("wiki-written")
            if raw_move_entry:
                source = self._resolve(raw_move_entry["source"])
                destination = self._resolve(raw_move_entry["destination"])
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
                    self._write_json(journal_path, journal)
                    self._checkpoint("raw-move-inflight")
                    os.replace(source, destination)
                    raw_move_entry["moved"] = True
                    raw_move_entry["inflight"] = False
                records[raw_move_entry["content_hash"]].path = raw_move_entry[
                    "destination"
                ]
                self._write_json(journal_path, journal)
                self._checkpoint("raw-move-applied")
            for content_hash, status in plan.source_statuses.items():
                if content_hash not in records:
                    raise ValidationError(
                        "Cannot update an unknown source",
                        details={"source_hash": content_hash},
                    )
                records[content_hash].status = status
            self._write_registry(records)
            freshness = self._read_state("freshness")
            freshness["version"] = 2
            freshness_pages = freshness.setdefault("pages", {})
            for path, update in plan.freshness_updates.items():
                prior_freshness = freshness_pages.get(path, {})
                freshness_pages[path] = {
                    **(prior_freshness if isinstance(prior_freshness, dict) else {}),
                    **update,
                }
            self._write_state("freshness", freshness)
            journal["phase"] = "state-written"
            self._write_json(journal_path, journal)
            self._checkpoint("state-written")
            indexed_documents = plan.index_rebuild(records)
            journal["phase"] = "index-written"
            self._write_json(journal_path, journal)
            self._checkpoint("index-written")
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
            self._write_state("applied", applied)
            self._checkpoint("applied-recorded")
            journal["state"] = "committed"
            self._write_json(journal_path, journal)
            self._checkpoint("committed")
        except Exception:
            self.recover()
            raise
        self._cleanup_transaction(journal)
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

    def recover(self) -> None:
        journal_path = self.journal_path
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
                target = self._resolve(path)
                backup = entry.get("backup")
                if backup:
                    shutil.copy2(self._resolve(str(backup)), target)
                else:
                    target.unlink(missing_ok=True)
            raw_move = journal.get("raw_move")
            if isinstance(raw_move, dict) and (
                raw_move.get("moved") or raw_move.get("inflight")
            ):
                source = self._resolve(str(raw_move.get("source", "")))
                destination = self._resolve(str(raw_move.get("destination", "")))
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
                    target = self._resolve(str(entry.get("target", "")))
                    target.unlink(missing_ok=True)
                    backup = entry.get("backup")
                    if backup:
                        target.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(self._resolve(str(backup)), target)
            else:
                for backup_key, target_name in (
                    ("registry_backup", "registry.json"),
                    ("applied_backup", "applied.json"),
                ):
                    backup = journal.get(backup_key)
                    if backup:
                        shutil.copy2(
                            self._resolve(str(backup)),
                            self.root / ".brain" / target_name,
                        )
        self._cleanup_transaction(journal)

    def _cleanup_transaction(self, journal: dict[str, Any]) -> None:
        transaction_id = str(journal.get("transaction_id", ""))
        if re.fullmatch(r"[0-9a-f]{32}", transaction_id):
            transaction_root = self.root / ".brain" / "transactions" / transaction_id
            if transaction_root.is_dir():
                shutil.rmtree(transaction_root)
        self.journal_path.unlink(missing_ok=True)
