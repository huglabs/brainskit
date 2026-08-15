"""What rollback actually does after an interrupted `bk apply`.

`FileVault.commit_wiki_batch` is the only path that writes `wiki/`, and
`_recover_apply_unlocked` is its undo. Until this file existed the undo was
covered by exactly one test, which hand-wrote a `"version": 1` journal carrying
`registry_backup`/`applied_backup` -- the legacy `else` branch, and a shape the
writer has not produced for two schema versions. Every branch the *current*
writer can reach (the `backups` list, `raw_move` reversal, the four phases, the
`inflight` window, the committed boundary) was unexercised.

These are characterization tests: they record what recovery does today so a
refactor of the same machinery has something to disagree with. They are written
against journals the real writer produced, not invented ones -- `_journal_writes`
patches the module's `_atomic_json` to record every journal it writes and to
raise at a chosen moment, and neutralises `_recover_apply_unlocked` for the
duration so the half-finished vault survives on disk exactly as a crash would
leave it. Recovery is then triggered the way it is in production: by opening the
vault (`FileVault(root)` runs it under the write lock).

One test here is an `expectedFailure`, not a characterization: writing these
turned up a real defect. Rolling back a `raw_move` whose destination equals its
source deletes the raw evidence file, which nothing backs up. See
`RawMoveReversalTest.test_move_whose_destination_equals_its_source_must_not_delete_it`
for the mechanism and for why the only production caller does not hit it today.

One thing worth stating because the tests below depend on it: `phase` is
*written* by every step and *read* by nothing. Recovery decides what to restore
from `replaced`, `inflight`, `backups` and `raw_move` alone. The phase is a
forensic breadcrumb, not a control input.
"""

from __future__ import annotations

import contextlib
import json
import sys
import unittest
from collections.abc import Callable
from copy import deepcopy
from pathlib import Path, PurePosixPath
from typing import Any
from unittest import mock

from brainskit.infrastructure import vault as vault_module
from brainskit.infrastructure.apply_transaction import ApplyTransaction
from brainskit.infrastructure.vault import FileVault

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_fix_services import ServiceFixture

_JOURNAL_NAME = "apply-journal.json"

# The order `commit_wiki_batch` backs these up in is the order it restores them
# in, and the index files trail the JSON state deliberately: the index is
# disposable, the three JSON files are not.
_BACKUP_TARGETS = [
    ".brain/registry.json",
    ".brain/applied.json",
    ".brain/freshness.json",
    ".brain/index.db",
    ".brain/index.db-shm",
    ".brain/index.db-wal",
]

_PHASES = ["prepared", "wiki-written", "state-written", "index-written"]


class _Interrupted(RuntimeError):
    """Stands in for the power cut, the SIGKILL or the full disk."""


Predicate = Callable[[Path, dict[str, Any]], bool]


def _is_journal(path: Path) -> bool:
    return path.name == _JOURNAL_NAME


def at_phase(phase: str) -> Predicate:
    """The first journal write that records `phase`."""

    def predicate(path: Path, content: dict[str, Any]) -> bool:
        return _is_journal(path) and content.get("phase") == phase

    return predicate


def at_page_inflight(page: str) -> Predicate:
    """The write that announces `page` is about to be replaced."""

    def predicate(path: Path, content: dict[str, Any]) -> bool:
        return _is_journal(path) and content.get("inflight") == page

    return predicate


def at_raw_move(field: str) -> Predicate:
    """The first write where the raw move's `field` flag is set."""

    def predicate(path: Path, content: dict[str, Any]) -> bool:
        raw_move = content.get("raw_move")
        return (
            _is_journal(path)
            and isinstance(raw_move, dict)
            and bool(raw_move.get(field))
        )

    return predicate


def at_committed() -> Predicate:
    """The write that flips the journal to `committed`, before cleanup runs."""

    def predicate(path: Path, content: dict[str, Any]) -> bool:
        return _is_journal(path) and content.get("state") == "committed"

    return predicate


def after_applied_state(proposal_id: str) -> Predicate:
    """The narrow window between `applied.json` landing and `state: committed`.

    This is the last moment an interrupted apply is still rolled back, and the
    only one in which every one of the six backup targets holds new bytes.
    """

    def predicate(path: Path, content: dict[str, Any]) -> bool:
        return path.name == "applied.json" and proposal_id in content.get(
            "proposals", {}
        )

    return predicate


class ApplyJournalFixture(ServiceFixture):
    def setUp(self) -> None:
        super().setUp()
        self.source = self.capture_into(
            "20-research",
            text="Memoria compilada evita trabalho no momento da consulta.",
            title="Memoria compilada",
        )
        self.page = self.upsert_page(
            "recoverable", "Recoverable", "Original body. ", self.source
        )
        self.page_path = self.root / self.page
        self.original_page = self.page_path.read_bytes()
        self.journal_path = self.root / ".brain" / _JOURNAL_NAME

    def update_proposal(self, proposal_id: str, body: str) -> dict[str, Any]:
        """An update of the page created in `setUp`, carrying its current hash."""

        return {
            "proposal_id": proposal_id,
            "operations": [
                {
                    "action": "upsert",
                    "kind": "concept",
                    "slug": "recoverable",
                    "title": "Recoverable",
                    "aliases": [],
                    "source_hashes": [self.source],
                    "body": f"{body}[^source:{self.source}]",
                    "links": [],
                    "base_hash": self.vault.wiki_version(self.page),
                }
            ],
        }

    @contextlib.contextmanager
    def _journal_writes(self, predicate: Predicate | None = None) -> Any:
        original = vault_module._atomic_json
        recorded: list[dict[str, Any]] = []
        fired = False

        def patched(path: Path, content: dict[str, Any]) -> None:
            nonlocal fired
            original(path, content)
            if _is_journal(path):
                recorded.append(deepcopy(content))
            if predicate is not None and not fired and predicate(path, content):
                fired = True
                raise _Interrupted(f"interrupted after writing {path.name}")

        with contextlib.ExitStack() as stack:
            stack.enter_context(
                mock.patch.object(vault_module, "_atomic_json", patched)
            )
            if predicate is not None:
                # The writer rolls back in its own `except` clause. Neutralising
                # that is what leaves a genuinely half-finished vault on disk,
                # which is the state this file exists to test recovery against.
                # The clause lives on `ApplyTransaction` now;
                # `FileVault._recover_apply_unlocked` is the lock around the
                # same method, so patching the vault would leave the rollback
                # this has to suppress running.
                stack.enter_context(
                    mock.patch.object(ApplyTransaction, "recover", lambda self: None)
                )
            yield recorded

    def interrupt(
        self, predicate: Predicate, call: Callable[[], Any]
    ) -> list[dict[str, Any]]:
        with (
            self._journal_writes(predicate) as recorded,
            self.assertRaises(_Interrupted),
        ):
            call()
        self.assertTrue(
            self.journal_path.is_file(),
            "an interrupted apply must leave its journal behind to be recovered",
        )
        return recorded

    def read_journal(self) -> dict[str, Any]:
        return json.loads(self.journal_path.read_text(encoding="utf-8"))

    def write_journal(self, journal: dict[str, Any]) -> None:
        self.journal_path.write_text(json.dumps(journal, indent=2), encoding="utf-8")

    def transaction_dir(self, journal: dict[str, Any] | None = None) -> Path:
        journal = self.read_journal() if journal is None else journal
        return self.root / ".brain" / "transactions" / str(journal["transaction_id"])

    def reopen(self) -> FileVault:
        """Recovery runs on open, exactly as it does for the next `bk` call."""

        return FileVault(self.root)

    def state_bytes(self, relative: str) -> bytes | None:
        path = self.root / relative
        return path.read_bytes() if path.is_file() else None


class JournalShapeTest(ApplyJournalFixture):
    """The journal these tests are built from is the one the writer emits."""

    def test_writer_emits_a_version_2_journal_through_four_phases(self) -> None:
        """Pins `commit_wiki_batch`'s on-disk contract, keys and all.

        Every other test in this file hand-places or mutates a journal. If the
        writer's shape drifts, this fails first and says so, instead of the
        others quietly testing a format nothing produces any more.
        """

        with self._journal_writes() as recorded:
            self.service.gate.commit(
                self.update_proposal("shape", "Second body. "),
                raw_move=(self.source, "30-public"),
            )

        first = recorded[0]
        self.assertEqual(2, first["version"])
        self.assertEqual("committing", first["state"])
        self.assertEqual("prepared", first["phase"])
        self.assertEqual(
            {
                "version",
                "transaction_id",
                "proposal_id",
                "state",
                "phase",
                "entries",
                "replaced",
                "inflight",
                "backups",
                "raw_move",
            },
            set(first),
        )
        self.assertEqual([], first["replaced"])
        self.assertIsNone(first["inflight"])
        self.assertEqual(
            {"path", "staged", "backup", "existed"}, set(first["entries"][0])
        )
        self.assertEqual(self.page, first["entries"][0]["path"])
        self.assertTrue(first["entries"][0]["existed"])
        self.assertEqual(
            _BACKUP_TARGETS, [entry["target"] for entry in first["backups"]]
        )
        self.assertEqual({"target", "backup", "existed"}, set(first["backups"][0]))
        self.assertEqual(
            {"content_hash", "source", "destination", "inflight", "moved"},
            set(first["raw_move"]),
        )
        self.assertEqual(_PHASES, list(dict.fromkeys(row["phase"] for row in recorded)))
        self.assertEqual("committed", recorded[-1]["state"])
        # A completed apply leaves nothing to recover.
        self.assertFalse(self.journal_path.exists())
        self.assertFalse(self.transaction_dir(first).exists())


class StateBackupRestoreTest(ApplyJournalFixture):
    """The `backups` list -- the v2 branch the legacy test never reaches."""

    def test_backups_list_restores_state_files_byte_for_byte(self) -> None:
        """Incident: a crash after `applied.json` lands leaves every state file
        holding the new apply's bytes while the journal still says `committing`.

        Rollback has to put all three JSON files back exactly as they were, or
        the vault keeps a proposal id bound to a wiki page that was reverted --
        which makes the retry idempotent against an apply that no longer exists.
        """

        # A source the page does not cite yet, so the apply has a registry
        # change to make: `commit_wiki_batch` flips every cited source to
        # `ingested`, and without this the registry bytes would be unchanged and
        # the restore below would prove nothing.
        second = self.service.capture(
            None,
            text="Uma segunda evidencia, ainda nao citada por nenhuma pagina.",
            title="Segunda evidencia",
        )["source"]["content_hash"]
        proposal = self.update_proposal("late-crash", "Second body. ")
        proposal["operations"][0]["source_hashes"] = [self.source, second]
        proposal["operations"][0]["body"] += f" Mais uma.[^source:{second}]"

        before = {
            relative: self.state_bytes(relative)
            for relative in (
                ".brain/registry.json",
                ".brain/applied.json",
                ".brain/freshness.json",
            )
        }
        self.interrupt(
            after_applied_state("late-crash"),
            lambda: self.service.apply(proposal),
        )
        # The crash really did leave new bytes behind, in all three.
        for relative, original in before.items():
            self.assertNotEqual(original, self.state_bytes(relative), relative)
        self.assertNotEqual(self.original_page, self.page_path.read_bytes())

        self.reopen()

        for relative, original in before.items():
            self.assertEqual(original, self.state_bytes(relative), relative)
        self.assertEqual(self.original_page, self.page_path.read_bytes())
        self.assertNotIn(
            "late-crash",
            json.loads((self.root / ".brain/applied.json").read_text())["proposals"],
        )

    def test_backups_list_removes_a_state_file_that_did_not_exist_before(self) -> None:
        """Incident: applying on a vault with no search index yet.

        `.brain/index.db` is a backup target whose backup is `None` when it was
        absent at prepare time. Rollback restores *absence*: the file the apply
        created is unlinked, and a missing backup is not an error.
        """

        for suffix in ("", "-shm", "-wal"):
            (self.root / f".brain/index.db{suffix}").unlink(missing_ok=True)

        self.interrupt(
            after_applied_state("no-index"),
            lambda: self.service.apply(
                self.update_proposal("no-index", "Second body. ")
            ),
        )
        journal = self.read_journal()
        entry = next(
            row for row in journal["backups"] if row["target"] == ".brain/index.db"
        )
        self.assertFalse(entry["existed"])
        self.assertIsNone(entry["backup"])
        self.assertTrue(
            (self.root / ".brain/index.db").is_file(),
            "the interrupted apply rebuilt the index it found missing",
        )

        self.reopen()

        self.assertFalse((self.root / ".brain/index.db").exists())
        self.assertEqual(self.original_page, self.page_path.read_bytes())


class RawMoveReversalTest(ApplyJournalFixture):
    """Filing a source into a branch moves a file; rollback moves it back."""

    def commit_with_move(self, proposal_id: str) -> None:
        self.service.gate.commit(
            self.update_proposal(proposal_id, "Second body. "),
            raw_move=(self.source, "30-public"),
        )

    def move_paths(self) -> tuple[Path, Path]:
        raw_move = self.read_journal()["raw_move"]
        return (
            self.root / raw_move["source"],
            self.root / raw_move["destination"],
        )

    def test_move_is_reversed_when_only_the_destination_survives(self) -> None:
        """Incident: crash between `os.replace` and the registry write.

        The registry still points at the old path while the file sits at the new
        one, so the source is unreachable by every later read. Rollback must
        move it back, not merely forget the move.
        """

        self.interrupt(at_raw_move("moved"), lambda: self.commit_with_move("moved"))
        source, destination = self.move_paths()
        self.assertFalse(source.exists())
        self.assertTrue(destination.is_file())
        moved_bytes = destination.read_bytes()

        self.reopen()

        self.assertTrue(source.is_file())
        self.assertEqual(moved_bytes, source.read_bytes())
        self.assertFalse(destination.exists())

    def test_move_drops_the_destination_when_the_source_is_also_present(self) -> None:
        """Incident: a source file reappears at its old path before recovery
        runs -- restored from a backup, re-synced, or written by a second tool.

        Rollback refuses to overwrite the surviving source. It removes the
        destination copy instead, so the pre-apply path stays authoritative.
        """

        self.interrupt(at_raw_move("moved"), lambda: self.commit_with_move("both"))
        source, destination = self.move_paths()
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(b"resurrected source\n")

        self.reopen()

        self.assertFalse(destination.exists())
        self.assertEqual(b"resurrected source\n", source.read_bytes())

    def test_move_whose_destination_equals_its_source_must_not_delete_it(self) -> None:
        """Incident: a journal records a move whose two paths are one path.

        `_transaction_move_destination` returns the source unchanged when the
        file is already where the move would put it, and the writer used to
        mark that no-op `moved`. Recovery then evaluated, on a single file
        reachable by both names:

            elif destination.is_file() and source.exists():
                destination.unlink()

        Both conditions hold, and the unlink removed the only copy. Nothing
        restores it: the transaction directory backs up `wiki/` pages and the
        six `.brain/` state files, never anything under `raw/`. The registry
        was then put back pointing at a path that no longer existed, so the
        loss was silent as well as permanent -- and `raw/` is the vault's
        immutable evidence, the one thing that cannot be recompiled.

        The writer no longer emits this record (see
        `test_a_source_already_in_its_branch_is_never_journalled_as_moved`),
        which is why the journal below is placed by hand. Recovery reads
        whatever the *previous* process wrote, and that process may be an
        older build; it has to survive this shape rather than assume it away.
        """

        self.interrupt(at_raw_move("moved"), lambda: self.commit_with_move("same-path"))
        legacy = self.read_journal()
        legacy["raw_move"]["source"] = legacy["raw_move"]["destination"]
        self.write_journal(legacy)

        raw_move = self.read_journal()["raw_move"]
        self.assertEqual(raw_move["source"], raw_move["destination"])
        source = self.root / raw_move["source"]
        self.assertTrue(source.is_file())

        self.reopen()

        self.assertTrue(
            source.is_file(),
            "rollback deleted the raw source file it was supposed to preserve",
        )

    def test_a_source_already_in_its_branch_is_never_journalled_as_moved(self) -> None:
        """Filing a source into the branch it already occupies moves nothing.

        The writer has to record that. A `moved` flag on an entry whose two
        paths name one file is a rollback instruction to undo a move that never
        happened, and reversing it deletes the file -- the case the test above
        covers from recovery's side. Asserting the flags here is what keeps the
        two halves independent: recovery's guard would hide a writer that
        started emitting the record again.
        """

        record = self.vault.registry()[self.source]
        branch = PurePosixPath(record.path).parts[1]
        source = self.root / record.path
        source_bytes = source.read_bytes()

        self.interrupt(
            at_phase("state-written"),
            lambda: self.service.gate.commit(
                self.update_proposal("same-path-writer", "Second body. "),
                raw_move=(self.source, branch),
            ),
        )
        raw_move = self.read_journal()["raw_move"]
        self.assertEqual(raw_move["source"], raw_move["destination"])
        self.assertFalse(raw_move["moved"])
        self.assertFalse(raw_move["inflight"])

        self.reopen()

        self.assertTrue(source.is_file())
        self.assertEqual(source_bytes, source.read_bytes())
        restored = self.vault.registry()[self.source]
        self.assertEqual(record.path, restored.path)
        self.assertTrue((self.root / restored.path).is_file())

    def test_move_marked_inflight_before_it_happens_corrupts_nothing(self) -> None:
        """Incident: crash in the window between announcing the move and
        performing it -- `inflight: true`, `moved: false`, nothing on disk moved.

        Recovery enters the reversal branch on `inflight` alone, so it has to
        cope with a destination that was never created.
        """

        recorded = self.interrupt(
            at_raw_move("inflight"), lambda: self.commit_with_move("inflight")
        )
        self.assertFalse(recorded[-1]["raw_move"]["moved"])
        source, destination = self.move_paths()
        source_bytes = source.read_bytes()
        self.assertTrue(source.is_file())
        self.assertFalse(destination.exists())

        self.reopen()

        self.assertTrue(source.is_file())
        self.assertEqual(source_bytes, source.read_bytes())
        self.assertFalse(destination.exists())
        self.assertEqual(self.original_page, self.page_path.read_bytes())


class PhaseRollbackTest(ApplyJournalFixture):
    """Whatever the apply had reached, the page goes back to its old bytes."""

    def test_every_phase_restores_the_page_to_its_pre_apply_bytes(self) -> None:
        """Incident: a crash at any point in an update leaves a page that cites
        sources the registry has not accepted yet.

        What each phase means for the page:

        - `prepared`   -- nothing written; recovery is a no-op on `wiki/`.
        - `wiki-written` -- every page replaced; all restored from `backups/N.md`.
        - `state-written` -- registry and freshness are new too; the `backups`
          list puts them back alongside the page.
        - `index-written` -- as above plus a rebuilt index, itself restored.

        Recovery does not read `phase` to decide any of this; it reads
        `replaced` and `inflight`. The guarantee is the same at all four.
        """

        for phase in _PHASES:
            with self.subTest(phase=phase):
                self.interrupt(
                    at_phase(phase),
                    lambda phase=phase: self.service.apply(
                        self.update_proposal(f"crash-{phase}", f"Body at {phase}. ")
                    ),
                )
                journal = self.read_journal()
                self.assertEqual(phase, journal["phase"])
                transaction = self.transaction_dir(journal)

                self.reopen()

                self.assertEqual(self.original_page, self.page_path.read_bytes())
                self.assertFalse(self.journal_path.exists())
                self.assertFalse(transaction.exists())

    def test_a_page_marked_inflight_is_restored_as_if_replaced(self) -> None:
        """Incident: crash during the `os.replace` that installs a page.

        The journal cannot record a half-done rename, so the writer names the
        page in `inflight` *before* replacing it. Recovery unions `inflight`
        into `replaced` and restores it either way -- which is what makes the
        rename window safe whichever side of it the crash landed on.
        """

        recorded = self.interrupt(
            at_page_inflight(self.page),
            lambda: self.service.apply(
                self.update_proposal("mid-replace", "Second body. ")
            ),
        )
        self.assertEqual(self.page, recorded[-1]["inflight"])
        self.assertEqual([], recorded[-1]["replaced"])
        # Stand in for the replace having landed after the journal write.
        self.page_path.write_bytes(b"partial replacement")

        self.reopen()

        self.assertEqual(self.original_page, self.page_path.read_bytes())


class CommittedBoundaryTest(ApplyJournalFixture):
    """`state: committed` is the line between losing and keeping an apply."""

    def test_a_committed_journal_is_cleaned_up_and_never_rolled_back(self) -> None:
        """Incident: crash between the journal flipping to `committed` and the
        transaction directory being removed.

        Everything the apply promised is already durable at that point, so
        rolling back here would destroy a completed write. Recovery must skip
        restoration entirely and do nothing but clean up.
        """

        self.interrupt(
            at_committed(),
            lambda: self.service.apply(
                self.update_proposal("kept", "Committed body. ")
            ),
        )
        journal = self.read_journal()
        self.assertEqual("committed", journal["state"])
        applied_bytes = self.state_bytes(".brain/applied.json")
        registry_bytes = self.state_bytes(".brain/registry.json")
        new_page = self.page_path.read_bytes()
        self.assertNotEqual(self.original_page, new_page)
        transaction = self.transaction_dir(journal)

        self.reopen()

        self.assertEqual(new_page, self.page_path.read_bytes())
        self.assertEqual(applied_bytes, self.state_bytes(".brain/applied.json"))
        self.assertEqual(registry_bytes, self.state_bytes(".brain/registry.json"))
        self.assertIn(
            "kept",
            json.loads((self.root / ".brain/applied.json").read_text())["proposals"],
        )
        self.assertFalse(self.journal_path.exists())
        self.assertFalse(transaction.exists())


class TransactionCleanupTest(ApplyJournalFixture):
    """Cleanup runs on every path, and only inside the directory it owns."""

    def test_cleanup_removes_the_transaction_directory_and_the_journal(self) -> None:
        """Incident: staged copies and backups accumulating under `.brain/`
        after every interrupted apply, one directory per crash, forever.
        """

        self.interrupt(
            at_phase("wiki-written"),
            lambda: self.service.apply(self.update_proposal("swept", "Second body. ")),
        )
        journal = self.read_journal()
        transaction = self.transaction_dir(journal)
        self.assertTrue(transaction.is_dir())
        self.assertTrue(any(transaction.rglob("*.md")))

        self.reopen()

        self.assertFalse(transaction.exists())
        self.assertFalse(self.journal_path.exists())

    def test_a_malformed_transaction_id_deletes_nothing(self) -> None:
        """Incident: a truncated or hand-edited journal reaching cleanup.

        The id is only trusted when it is 32 hex characters. Anything else is
        left alone -- the rollback still happens and the journal is still
        removed, but no directory is deleted on the strength of a bad id.
        """

        self.interrupt(
            at_phase("wiki-written"),
            lambda: self.service.apply(
                self.update_proposal("malformed", "Second body. ")
            ),
        )
        journal = self.read_journal()
        real_transaction = self.transaction_dir(journal)
        decoy = self.root / ".brain/transactions/short"
        decoy.mkdir(parents=True)
        (decoy / "keep.txt").write_text("keep", encoding="utf-8")
        journal["transaction_id"] = "short"
        self.write_journal(journal)

        self.reopen()

        self.assertTrue((decoy / "keep.txt").is_file())
        # The real directory is now unreachable by id, so it leaks rather than
        # being deleted. Losing bytes is the worse failure of the two.
        self.assertTrue(real_transaction.is_dir())
        self.assertFalse(self.journal_path.exists())
        # The rollback itself is unaffected by the bad id.
        self.assertEqual(self.original_page, self.page_path.read_bytes())

    def test_a_traversal_shaped_transaction_id_cannot_escape(self) -> None:
        """Incident: a journal whose id walks out of `.brain/transactions/`.

        `shutil.rmtree` would follow it. The 32-hex check is what stops a
        crafted or corrupted journal from deleting an unrelated directory.
        """

        self.interrupt(
            at_phase("wiki-written"),
            lambda: self.service.apply(
                self.update_proposal("traversal", "Second body. ")
            ),
        )
        outside = self.root / "escape"
        outside.mkdir()
        (outside / "keep.txt").write_text("keep", encoding="utf-8")
        journal = self.read_journal()
        journal["transaction_id"] = "../../escape"
        self.write_journal(journal)

        self.reopen()

        self.assertTrue((outside / "keep.txt").is_file())
        self.assertFalse(self.journal_path.exists())


class RecoveryIdempotenceTest(ApplyJournalFixture):
    """Opening a recovered vault again must be a no-op, not a second rollback."""

    def test_a_second_open_finds_nothing_left_to_recover(self) -> None:
        """Incident: several `bk` processes racing to open a crashed vault, or
        an operator simply running the same command twice.

        Cleanup deletes the journal on the way out, so the second open takes the
        early return and changes nothing.
        """

        self.interrupt(
            at_phase("index-written"),
            lambda: self.service.apply(self.update_proposal("twice", "Second body. ")),
        )

        self.reopen()

        snapshot = {
            relative: self.state_bytes(relative)
            for relative in (
                ".brain/registry.json",
                ".brain/applied.json",
                ".brain/freshness.json",
            )
        }
        page_after_first = self.page_path.read_bytes()
        self.assertEqual(self.original_page, page_after_first)
        self.assertFalse(self.journal_path.exists())

        self.reopen()

        self.assertEqual(page_after_first, self.page_path.read_bytes())
        for relative, expected in snapshot.items():
            self.assertEqual(expected, self.state_bytes(relative), relative)
        self.assertFalse(self.journal_path.exists())


if __name__ == "__main__":
    unittest.main()
