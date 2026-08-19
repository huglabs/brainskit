"""Crash an apply at a named checkpoint, then prove the vault is untouched.

`tests/test_apply_journal_recovery.py` reaches the same rollback branches by
patching the module's writer and raising from inside it. That works, but it
can only stop where a journal write happens to be, it has to neutralise the
engine's own rollback to keep the wreckage on disk, and every test that wants
a different moment needs a new predicate.

`ApplyTransaction` takes a `FailurePoint` instead: the caller names one of the
ten checkpoints the engine passes through and the engine stops there, raising
`InterruptedApply` -- a `BaseException`, so the engine's own `except Exception`
rollback does not catch it and the half-finished vault survives exactly as a
SIGKILL would leave it. Nothing is patched. Recovery is then triggered the way
production triggers it: by opening the vault.

What every test here asserts, in the same words: after recovery the vault is
indistinguishable from one where the apply was never attempted. Every page
holds its pre-apply bytes, `registry.json`, `applied.json` and `freshness.json`
hold their pre-apply bytes, no proposal id was consumed, and neither the
journal nor the transaction directory is left behind.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from typing import Any

from brainskit.infrastructure.apply_transaction import (
    CHECKPOINTS,
    ApplyTransaction,
    FailurePoint,
    InterruptedApply,
)
from brainskit.infrastructure.vault import FileVault

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_fix_services import ServiceFixture

_STATE_FILES = (
    ".brain/registry.json",
    ".brain/applied.json",
    ".brain/freshness.json",
)

#: Three pages, so "the third page replace" is a moment that exists.
_PAGES: tuple[tuple[str, str, str], ...] = (
    ("alfa", "Alfa", "Memoria compilada responde sem reler a evidencia bruta. "),
    ("beta", "Beta", "Indice invertido encontra trechos por termo, nao por caminho. "),
    ("gama", "Gama", "Reconciliacao reencontra fontes movidas pelo hash do conteudo. "),
)


class SeamFixture(ServiceFixture):
    def setUp(self) -> None:
        super().setUp()
        self.source = self.capture_into(
            "20-research",
            text="Evidencia base para as tres paginas deste teste.",
            title="Evidencia base",
        )
        self.paths = [
            self.upsert_page(slug, title, body, self.source)
            for slug, title, body in _PAGES
        ]
        self.original_pages = {
            path: (self.root / path).read_bytes() for path in self.paths
        }
        self.journal_path = self.root / ".brain" / "apply-journal.json"
        # `crashing_at` returns a copy, so this one stays inert and every crash
        # in a loop starts from the same engine rather than from the last one.
        self.pristine = self.vault._apply

    def update_proposal(self, proposal_id: str, marker: str) -> dict[str, Any]:
        return {
            "proposal_id": proposal_id,
            "operations": [
                {
                    "action": "upsert",
                    "kind": "concept",
                    "slug": slug,
                    "title": title,
                    "aliases": [],
                    "source_hashes": [self.source],
                    "body": f"{marker} {body}[^source:{self.source}]",
                    "links": [],
                    "base_hash": self.vault.wiki_version(f"wiki/concepts/{slug}.md"),
                }
                for slug, title, body in _PAGES
            ],
        }

    def crash_at(self, step: str, **kwargs: Any) -> None:
        """Arm the seam. Production never calls this; nothing else can."""

        self.vault._apply = self.pristine.crashing_at(FailurePoint(step, **kwargs))

    def state_snapshot(self) -> dict[str, bytes | None]:
        return {
            relative: (
                (self.root / relative).read_bytes()
                if (self.root / relative).is_file()
                else None
            )
            for relative in _STATE_FILES
        }

    def page_snapshot(self) -> dict[str, bytes | None]:
        return {
            path: (
                (self.root / path).read_bytes()
                if (self.root / path).is_file()
                else None
            )
            for path in self.paths
        }

    def read_journal(self) -> dict[str, Any]:
        return json.loads(self.journal_path.read_text(encoding="utf-8"))

    def transaction_dir(self, journal: dict[str, Any]) -> Path:
        return self.root / ".brain" / "transactions" / str(journal["transaction_id"])

    def reopen(self) -> FileVault:
        return FileVault(self.root)

    def assert_never_happened(
        self,
        proposal_id: str,
        pages: dict[str, bytes | None],
        state: dict[str, bytes | None],
        transaction: Path,
    ) -> None:
        for path, expected in pages.items():
            self.assertEqual(expected, (self.root / path).read_bytes(), path)
        for relative, expected in state.items():
            self.assertEqual(expected, self.state_snapshot()[relative], relative)
        self.assertNotIn(
            proposal_id,
            json.loads((self.root / ".brain/applied.json").read_text())["proposals"],
            "a rolled-back apply must not consume its proposal id",
        )
        self.assertFalse(self.journal_path.exists())
        self.assertFalse(transaction.exists())


class FailurePointIsInertByDefaultTest(SeamFixture):
    """The seam must be impossible to trip by accident."""

    def test_a_vault_builds_its_engine_without_a_failure_point(self) -> None:
        """There is no env var and no default; production never arms it."""

        self.assertIsNone(self.vault._apply._fail_at)
        self.assertIsNone(FileVault(self.root)._apply._fail_at)

    def test_arming_a_copy_leaves_the_original_engine_inert(self) -> None:
        armed = self.pristine.crashing_at(FailurePoint("prepared"))
        self.assertIsNot(armed, self.pristine)
        self.assertIsNone(self.pristine._fail_at)
        # And the unarmed engine still commits, which is the control for every
        # crash test below: they fail because of the point, not because the
        # engine is broken.
        self.service.apply(self.update_proposal("unarmed", "Sem falha."))
        for path, before in self.original_pages.items():
            self.assertNotEqual(before, (self.root / path).read_bytes(), path)

    def test_an_unknown_checkpoint_is_refused_at_construction(self) -> None:
        """A typo must fail loudly, not produce a test that never fires."""

        with self.assertRaises(ValueError):
            FailurePoint("wiki_written")
        with self.assertRaises(ValueError):
            FailurePoint("prepared", occurrence=0)

    def test_every_checkpoint_is_reachable(self) -> None:
        """A named checkpoint the engine never calls is a lie in the vocabulary.

        Each one is armed against an apply that exercises it -- pages, a raw
        move and a full commit -- and has to stop that apply. `page-inflight`
        and `page-replaced` are covered by the same run as the rest.
        """

        for step in CHECKPOINTS:
            with self.subTest(step=step):
                # A `committed` iteration keeps its raw move, and a source
                # already in the destination branch is deliberately not moved
                # again -- which would make the two raw-move checkpoints
                # unreachable depending on the order of this loop. Put it back
                # so each step is judged on its own.
                if self.vault.registry()[self.source].path.startswith("raw/30-public/"):
                    self.service.file(self.source, "20-research")
                self.crash_at(step)
                with self.assertRaises(InterruptedApply):
                    self.service.gate.commit(
                        self.update_proposal(f"reach-{step}", f"Ate {step}."),
                        raw_move=(self.source, "30-public"),
                    )
                self.reopen()


class CrashAtEachPhaseTest(SeamFixture):
    """The four journal phases, from the seam instead of from a predicate."""

    def test_a_crash_at_any_phase_leaves_no_trace_of_the_apply(self) -> None:
        for phase in ("prepared", "wiki-written", "state-written", "index-written"):
            with self.subTest(phase=phase):
                proposal_id = f"crash-{phase}"
                pages = self.page_snapshot()
                state = self.state_snapshot()

                self.crash_at(phase)
                with self.assertRaises(InterruptedApply):
                    self.service.apply(
                        self.update_proposal(proposal_id, f"Corpo em {phase}.")
                    )

                journal = self.read_journal()
                self.assertEqual(phase, journal["phase"])
                self.assertEqual("committing", journal["state"])
                transaction = self.transaction_dir(journal)

                self.reopen()

                self.assert_never_happened(proposal_id, pages, state, transaction)

    def test_the_wiki_really_was_written_before_the_crash_rolled_it_back(self) -> None:
        """The control for the test above: at `index-written` every page and
        every state file already holds the new apply's bytes, so the restore is
        undoing real work rather than finding nothing to do.
        """

        # A source no page cites yet, so the apply has a registry change to
        # make -- `commit` flips every cited source to `ingested`, and without
        # this the registry bytes would be identical either way and the restore
        # below would prove nothing.
        second = self.service.capture(
            None,
            text="Uma segunda evidencia, ainda nao citada por nenhuma pagina.",
            title="Segunda evidencia",
        )["source"]["content_hash"]
        proposal = self.update_proposal("really-written", "Corpo novo.")
        proposal["operations"][0]["source_hashes"].append(second)
        proposal["operations"][0]["body"] += f" Mais uma.[^source:{second}]"

        pages = self.page_snapshot()
        state = self.state_snapshot()

        self.crash_at("index-written")
        with self.assertRaises(InterruptedApply):
            self.service.apply(proposal)

        for path, before in pages.items():
            self.assertNotEqual(before, (self.root / path).read_bytes(), path)
        for relative, before in state.items():
            if relative == ".brain/applied.json":
                continue  # applied.json lands after this phase
            self.assertNotEqual(before, self.state_snapshot()[relative], relative)

        self.reopen()

        for path, before in pages.items():
            self.assertEqual(before, (self.root / path).read_bytes(), path)
        for relative, before in state.items():
            self.assertEqual(before, self.state_snapshot()[relative], relative)


class CrashMidPageReplaceTest(SeamFixture):
    """The page loop: dying between two of three replaces."""

    def test_a_crash_after_the_second_of_three_replaces_restores_all_three(
        self,
    ) -> None:
        """Incident: the process dies with the batch half installed.

        Two pages carry the new proposal's bytes, the third still carries its
        own -- and the vault is now a mixture no proposal ever described. This
        is the state that makes a partial apply indistinguishable from a hand
        edit, which is the guarantee the whole gate exists to make.
        """

        pages = self.page_snapshot()
        state = self.state_snapshot()

        self.crash_at("page-replaced", occurrence=2)
        with self.assertRaises(InterruptedApply):
            self.service.apply(self.update_proposal("half-batch", "Metade."))

        journal = self.read_journal()
        self.assertEqual("prepared", journal["phase"])
        self.assertEqual(sorted(self.paths)[:2], journal["replaced"])
        self.assertIsNone(journal["inflight"])
        # The vault really is a mixture at this point.
        replaced, untouched = sorted(self.paths)[:2], sorted(self.paths)[2]
        for path in replaced:
            self.assertNotEqual(pages[path], (self.root / path).read_bytes(), path)
        self.assertEqual(
            pages[untouched], (self.root / untouched).read_bytes(), untouched
        )
        transaction = self.transaction_dir(journal)

        self.reopen()

        self.assert_never_happened("half-batch", pages, state, transaction)

    def test_a_crash_inside_the_third_rename_restores_all_three(self) -> None:
        """Incident: the crash lands *during* `os.replace`, not around it.

        The journal cannot record half a rename, so the page is named in
        `inflight` before the rename starts. Stopping there and then writing
        the page by hand stands in for the rename having completed after the
        journal write -- the side of the window recovery cannot observe.
        """

        pages = self.page_snapshot()
        state = self.state_snapshot()
        third = sorted(self.paths)[2]

        self.crash_at("page-inflight", detail=third)
        with self.assertRaises(InterruptedApply):
            self.service.apply(self.update_proposal("mid-rename", "No meio."))

        journal = self.read_journal()
        self.assertEqual(third, journal["inflight"])
        self.assertEqual(sorted(self.paths)[:2], journal["replaced"])
        (self.root / third).write_bytes(b"metade de um rename")
        transaction = self.transaction_dir(journal)

        self.reopen()

        self.assert_never_happened("mid-rename", pages, state, transaction)

    def test_the_proposal_can_simply_be_sent_again(self) -> None:
        """The point of rolling back cleanly: nothing about the crashed attempt
        blocks the retry. The pages are back at the bytes their `base_hash`
        names, and the proposal id was never bound to a result.
        """

        pages = self.page_snapshot()
        self.crash_at("state-written")
        proposal = self.update_proposal("retryable", "Primeira tentativa.")
        with self.assertRaises(InterruptedApply):
            self.service.apply(proposal)

        self.reopen()
        for path, before in pages.items():
            self.assertEqual(before, (self.root / path).read_bytes(), path)

        self.vault._apply = self.pristine
        result = self.service.apply(proposal)

        self.assertEqual(3, result["applied"])
        self.assertFalse(result["idempotent"])
        for path, before in pages.items():
            self.assertNotEqual(before, (self.root / path).read_bytes(), path)


class CommitBoundaryTest(SeamFixture):
    """`state: committed` decides whether the crash costs the apply."""

    def test_a_crash_one_write_before_committed_loses_the_whole_apply(self) -> None:
        """`applied-recorded` is the last moment rollback still happens, and
        the only one where all six backup targets hold new bytes: the result is
        already in `applied.json` while the journal still says `committing`.
        """

        pages = self.page_snapshot()
        state = self.state_snapshot()

        self.crash_at("applied-recorded")
        with self.assertRaises(InterruptedApply):
            self.service.apply(self.update_proposal("one-write-early", "Quase."))

        self.assertEqual("committing", self.read_journal()["state"])
        self.assertIn(
            "one-write-early",
            json.loads((self.root / ".brain/applied.json").read_text())["proposals"],
            "the crash really did land after applied.json was written",
        )
        transaction = self.transaction_dir(self.read_journal())

        self.reopen()

        self.assert_never_happened("one-write-early", pages, state, transaction)

    def test_a_crash_one_write_after_committed_keeps_the_whole_apply(self) -> None:
        """Everything the apply promised is durable by then. Rolling back here
        would destroy a completed write, so recovery only cleans up.
        """

        self.crash_at("committed")
        with self.assertRaises(InterruptedApply):
            self.service.apply(self.update_proposal("kept", "Corpo mantido."))

        journal = self.read_journal()
        self.assertEqual("committed", journal["state"])
        transaction = self.transaction_dir(journal)
        committed_pages = self.page_snapshot()
        committed_state = self.state_snapshot()
        for path, before in self.original_pages.items():
            self.assertNotEqual(before, committed_pages[path], path)

        self.reopen()

        self.assertEqual(committed_pages, self.page_snapshot())
        self.assertEqual(committed_state, self.state_snapshot())
        self.assertIn(
            "kept",
            json.loads((self.root / ".brain/applied.json").read_text())["proposals"],
        )
        self.assertFalse(self.journal_path.exists())
        self.assertFalse(transaction.exists())


class CrashDuringRawMoveTest(SeamFixture):
    """Filing a source as part of an apply, interrupted from the seam."""

    def test_a_crash_after_the_move_puts_the_evidence_back(self) -> None:
        pages = self.page_snapshot()
        state = self.state_snapshot()
        origin = self.vault.registry()[self.source].path
        origin_bytes = (self.root / origin).read_bytes()

        self.crash_at("raw-move-applied")
        with self.assertRaises(InterruptedApply):
            self.service.gate.commit(
                self.update_proposal("moved-then-died", "Movido."),
                raw_move=(self.source, "30-public"),
            )

        raw_move = self.read_journal()["raw_move"]
        self.assertTrue(raw_move["moved"])
        self.assertFalse((self.root / origin).exists())
        transaction = self.transaction_dir(self.read_journal())

        self.reopen()

        self.assertTrue((self.root / origin).is_file())
        self.assertEqual(origin_bytes, (self.root / origin).read_bytes())
        self.assertFalse((self.root / raw_move["destination"]).exists())
        self.assert_never_happened("moved-then-died", pages, state, transaction)
        self.assertEqual(origin, FileVault(self.root).registry()[self.source].path)

    def test_a_crash_before_the_move_lands_leaves_the_evidence_alone(self) -> None:
        """`inflight: true`, `moved: false` -- announced, never performed."""

        origin = self.vault.registry()[self.source].path
        origin_bytes = (self.root / origin).read_bytes()

        self.crash_at("raw-move-inflight")
        with self.assertRaises(InterruptedApply):
            self.service.gate.commit(
                self.update_proposal("announced-only", "Anunciado."),
                raw_move=(self.source, "30-public"),
            )

        raw_move = self.read_journal()["raw_move"]
        self.assertTrue(raw_move["inflight"])
        self.assertFalse(raw_move["moved"])
        self.assertFalse((self.root / raw_move["destination"]).exists())

        self.reopen()

        self.assertTrue((self.root / origin).is_file())
        self.assertEqual(origin_bytes, (self.root / origin).read_bytes())
        self.assertFalse((self.root / raw_move["destination"]).exists())


class TransactionCollaboratorTest(unittest.TestCase):
    """What the engine may reach, stated as a test rather than as a comment."""

    def test_the_engine_is_handed_accessors_and_never_the_vault(self) -> None:
        """Handing it the vault would give it the lock-taking public methods,
        and calling one from inside the transaction would block on a lock this
        process already holds. The constructor is the audit of what it can do.
        """

        import inspect

        parameters = inspect.signature(ApplyTransaction.__init__).parameters
        self.assertEqual(
            [
                "self",
                "root",
                "resolve",
                "read_registry",
                "write_registry",
                "read_state",
                "write_state",
                "branches",
                "page_version",
                "resolve_record",
                "write_json",
                "write_text",
                "fail_at",
            ],
            list(parameters),
        )
        self.assertFalse(
            [
                name
                for name, value in vars(ApplyTransaction).items()
                if name.startswith("_vault") or name == "vault"
            ]
        )


if __name__ == "__main__":
    unittest.main()
