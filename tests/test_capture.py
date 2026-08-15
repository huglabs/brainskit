"""Capture's own decisions, driven against a stub rather than through a vault.

Every rule here was already covered end to end -- `RelatedCaptureFreshnessTest`
files real sources into a real vault and reads `.brain/freshness.json` back --
and those tests stay, because they are what proves the wiring. What they cannot
do is put a decision under a microscope: a relatedness floor measured through
`bk capture` needs an index, an apply, four pages of Portuguese prose and a
freshness file, and when it fails it does not say which of those moved.

So these drive `Ingestion` directly, with a vault and an index that do nothing
but answer. The extraction is what makes that possible: while this logic lived
on the facade, reaching it meant constructing every collaborator the facade
composes.
"""

from __future__ import annotations

import copy
import tempfile
import unittest
from collections.abc import Callable
from pathlib import Path
from typing import Any

from brainskit.application.capture import Ingestion, _walk_source
from brainskit.application.freshness import FreshnessLedger
from brainskit.domain.model import SearchHit, SourceRecord

HASH = "a" * 64
OTHER_HASH = "b" * 64


class StubVault:
    """Only what `Ingestion` and the ledger actually ask for."""

    def __init__(
        self,
        *,
        pages: dict[str, str] | None = None,
        raw_text: str = "",
        state: dict[str, Any] | None = None,
    ):
        self.root = Path("/vault")
        self.pages = pages or {}
        self._raw_text = raw_text
        self.state: dict[str, Any] = state or {}
        self.reads: list[str] = []

    def read_text(self, relative_path: str) -> str:
        self.reads.append(relative_path)
        return self.pages[relative_path]

    def wiki_pages(self) -> list[str]:
        return sorted(self.pages)

    def raw_text(self, record: SourceRecord, max_chars: int | None = None) -> str:
        return self._raw_text[:max_chars] if max_chars else self._raw_text

    def read_state(self, name: str) -> dict[str, Any]:
        return copy.deepcopy(self.state)

    def mutate_state(
        self, name: str, mutator: Callable[[dict[str, Any]], dict[str, Any]]
    ) -> dict[str, Any]:
        self.state = mutator(copy.deepcopy(self.state))
        return self.state


class StubIndex:
    """A search that returns exactly what a test tells it to."""

    def __init__(self, hits: list[SearchHit] | None = None):
        self.hits = hits or []
        self.queries: list[tuple[str, int]] = []

    def search(self, query: str, limit: int = 10) -> list[SearchHit]:
        self.queries.append((query, limit))
        return self.hits[:limit]

    def upsert_raw(self, vault: Any, record: SourceRecord) -> None:
        return None


def hit(path: str, kind: str = "wiki") -> SearchHit:
    return SearchHit(path=path, kind=kind, title=path, excerpt="", score=1.0)


def record(name: str = "note.md", content_hash: str = HASH) -> SourceRecord:
    return SourceRecord(
        content_hash=content_hash,
        path=f"raw/_inbox/{name}",
        original_name=name,
        media_type="text/markdown",
        size=64,
        captured_at="2026-01-01T00:00:00+00:00",
    )


def ingestion(vault: StubVault, index: StubIndex) -> Ingestion:
    return Ingestion(vault, index, FreshnessLedger(vault))  # type: ignore[arg-type]


class RelatednessFloorTest(unittest.TestCase):
    """BM25 ranks; it does not measure. The floor is what decides.

    A search index always returns its best matches, so "the index returned it"
    is not evidence of anything on a small corpus. Marking a page for review is
    a durable claim on a human's attention, and the floor is the whole of what
    separates a claim from a coincidence.
    """

    CAPTURE = (
        "Memoria compilada organiza conhecimento antes da consulta, evitando "
        "trabalho de recuperacao no momento da pergunta."
    )

    def marked(self, page_body: str) -> dict[str, Any]:
        vault = StubVault(
            pages={"wiki/concepts/p.md": page_body}, raw_text=self.CAPTURE
        )
        engine = ingestion(vault, StubIndex([hit("wiki/concepts/p.md")]))
        engine._mark_related_pages_for_review(record())
        return vault.state.get("pages", {})

    def test_one_shared_term_is_below_the_floor(self) -> None:
        # "consulta" and nothing else: a real word in common, and still not a
        # reason to tell a human this page needs looking at.
        self.assertEqual(self.marked("A consulta ao cardapio da cantina."), {})

    def test_two_shared_terms_clear_it(self) -> None:
        marked = self.marked("Memoria compilada acelera a consulta.")
        self.assertEqual(
            {path: entry["status"] for path, entry in marked.items()},
            {"wiki/concepts/p.md": "review"},
        )

    def test_the_top_ranked_hit_is_still_rejected_on_its_own_content(self) -> None:
        # The index is stubbed to swear this page is the best match in the
        # vault. The decision reads the page anyway.
        vault = StubVault(
            pages={"wiki/concepts/p.md": "Bolo de cenoura com cobertura."},
            raw_text=self.CAPTURE,
        )
        index = StubIndex([hit("wiki/concepts/p.md")])
        ingestion(vault, index)._mark_related_pages_for_review(record())
        self.assertEqual(vault.state, {})
        self.assertEqual(vault.reads, ["wiki/concepts/p.md"])

    def test_a_raw_hit_is_never_a_page_to_mark(self) -> None:
        vault = StubVault(
            pages={"raw/20-research/note.md": self.CAPTURE}, raw_text=self.CAPTURE
        )
        index = StubIndex([hit("raw/20-research/note.md", kind="raw")])
        ingestion(vault, index)._mark_related_pages_for_review(record())
        self.assertEqual(vault.state, {})
        # Not even read: a non-wiki hit is dropped before the body is opened.
        self.assertEqual(vault.reads, [])

    def test_the_reason_names_the_source_that_caused_it(self) -> None:
        marked = self.marked("Memoria compilada acelera a consulta.")
        self.assertEqual(
            marked["wiki/concepts/p.md"]["review_reason"], f"related source:{HASH}"
        )


class RelatedCaptureNeverDowngradesTest(unittest.TestCase):
    """ADR 0002, proved from the writer's side.

    `bk capture` is the caller that used to perform this transition with no
    guard, and `refresh_staleness` skips `review` -- so writing `review` over
    `stale` did not lower a badge, it removed the page from the ageing loop
    until the next apply. The guard lives in `mark_reviewed`; what this asserts
    is that capture still reaches it rather than writing the entry itself.
    """

    CAPTURE = "Memoria compilada organiza conhecimento antes da consulta."

    def setUp(self) -> None:
        self.vault = StubVault(
            pages={"wiki/concepts/p.md": "Memoria compilada acelera a consulta."},
            raw_text=self.CAPTURE,
            state={
                "pages": {
                    "wiki/concepts/p.md": {
                        "status": "stale",
                        "age_days": 91,
                        "content_hash": OTHER_HASH,
                    }
                }
            },
        )
        self.engine = ingestion(self.vault, StubIndex([hit("wiki/concepts/p.md")]))

    def test_a_stale_page_stays_stale(self) -> None:
        self.engine._mark_related_pages_for_review(record())
        self.assertEqual(
            self.vault.state["pages"]["wiki/concepts/p.md"]["status"], "stale"
        )

    def test_the_ageing_the_page_had_accrued_survives(self) -> None:
        self.engine._mark_related_pages_for_review(record())
        entry = self.vault.state["pages"]["wiki/concepts/p.md"]
        self.assertEqual(entry["age_days"], 91)
        self.assertNotIn("review_reason", entry)

    def test_provenance_is_untouched_by_an_annotation(self) -> None:
        # An annotation vouches for nothing, so it must neither add a
        # `content_hash` nor disturb the one the apply gate wrote.
        self.engine._mark_related_pages_for_review(record())
        self.assertEqual(
            self.vault.state["pages"]["wiki/concepts/p.md"]["content_hash"],
            OTHER_HASH,
        )

    def test_a_fresh_page_is_still_reachable(self) -> None:
        # The control: the guard must refuse a downgrade, not every transition.
        self.vault.state = {"pages": {"wiki/concepts/p.md": {"status": "fresh"}}}
        self.engine._mark_related_pages_for_review(record())
        self.assertEqual(
            self.vault.state["pages"]["wiki/concepts/p.md"]["status"], "review"
        )


class OrphanHealingTest(unittest.TestCase):
    """Freshness is keyed by path, the registry by hash.

    A page removed outside the gate leaves an entry nothing can revive, so
    `bk reconcile` drops it. What matters is the *scope* of the drop: an entry
    whose page is still on disk is not an orphan, and dropping it would discard
    the provenance record the integrity check reads.
    """

    def engine(
        self, pages: dict[str, str], entries: list[str]
    ) -> tuple[Any, StubVault]:
        vault = StubVault(
            pages=pages,
            state={"pages": {path: {"status": "fresh"} for path in entries}},
        )
        return ingestion(vault, StubIndex()), vault

    def test_an_entry_whose_page_is_gone_is_dropped_and_reported(self) -> None:
        engine, vault = self.engine(
            {"wiki/concepts/live.md": "body"},
            ["wiki/concepts/live.md", "wiki/concepts/gone.md"],
        )
        self.assertEqual(engine.drop_orphaned_freshness(), ["wiki/concepts/gone.md"])
        self.assertEqual(list(vault.state["pages"]), ["wiki/concepts/live.md"])

    def test_a_page_still_on_disk_keeps_its_entry(self) -> None:
        engine, vault = self.engine(
            {"wiki/concepts/live.md": "body"}, ["wiki/concepts/live.md"]
        )
        self.assertEqual(engine.drop_orphaned_freshness(), [])
        self.assertEqual(list(vault.state["pages"]), ["wiki/concepts/live.md"])

    def test_healing_twice_is_the_same_as_healing_once(self) -> None:
        engine, vault = self.engine({}, ["wiki/concepts/gone.md"])
        self.assertEqual(engine.drop_orphaned_freshness(), ["wiki/concepts/gone.md"])
        self.assertEqual(engine.drop_orphaned_freshness(), [])
        self.assertEqual(vault.state["pages"], {})


class PagesCitingTest(unittest.TestCase):
    def test_only_the_pages_that_declare_the_hash_are_named(self) -> None:
        vault = StubVault(
            pages={
                "wiki/concepts/cites.md": f"---\nsources:\n  - {HASH}\n---\n\nbody\n",
                "wiki/concepts/other.md": (
                    f"---\nsources:\n  - {OTHER_HASH}\n---\n\nbody\n"
                ),
                "wiki/concepts/none.md": "---\ntitle: bare\n---\n\nbody\n",
            }
        )
        self.assertEqual(
            ingestion(vault, StubIndex()).pages_citing(HASH),
            ["wiki/concepts/cites.md"],
        )


class WalkPruningTest(unittest.TestCase):
    """An ignored directory is pruned from the traversal, not filtered after it.

    `os.walk` lets a matched directory be dropped before it is descended into,
    which is the difference between a watch tick and a stall on a source folder
    holding a project. Filtering after the fact would produce the same capture
    set, so the observable proof is what the walk *reports*: one skip for the
    directory, not one per file inside it.
    """

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "project"
        for directory in ("node_modules/react/lib", "docs"):
            (self.root / directory).mkdir(parents=True)
        for relative in (
            "node_modules/react/index.js",
            "node_modules/react/lib/deep.js",
            "node_modules/react/lib/deeper.js",
            "docs/note.md",
            "README.md",
        ):
            (self.root / relative).write_text("body", encoding="utf-8")
        self.vault_root = Path(self.temporary.name) / "brain"
        self.vault_root.mkdir()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def walk(self, ignore: tuple[str, ...]) -> tuple[list[str], int]:
        found: list[str] = []
        skipped = 0
        for candidate, count in _walk_source(self.root, ignore, self.vault_root):
            skipped += count
            if candidate is not None:
                found.append(candidate.relative_to(self.root).as_posix())
        return sorted(found), skipped

    def test_an_ignored_tree_costs_one_skip_not_one_per_file(self) -> None:
        found, skipped = self.walk(("node_modules",))
        self.assertEqual(found, ["README.md", "docs/note.md"])
        # Three files and two directories sit under `node_modules`. The walk
        # never visits them, so the report is 1 -- the prune itself.
        self.assertEqual(skipped, 1)

    def test_ignoring_nothing_descends_the_whole_tree(self) -> None:
        found, skipped = self.walk(())
        self.assertEqual(
            found,
            [
                "README.md",
                "docs/note.md",
                "node_modules/react/index.js",
                "node_modules/react/lib/deep.js",
                "node_modules/react/lib/deeper.js",
            ],
        )
        self.assertEqual(skipped, 0)

    def test_a_pattern_that_matches_a_file_prunes_only_that_file(self) -> None:
        found, skipped = self.walk(("*.js",))
        self.assertEqual(found, ["README.md", "docs/note.md"])
        self.assertEqual(skipped, 3)

    def test_the_vault_is_pruned_even_when_it_sits_inside_the_source(self) -> None:
        nested = self.root / "brain"
        nested.mkdir()
        (nested / "raw").mkdir()
        (nested / "raw" / "captured.md").write_text("evidence", encoding="utf-8")
        found: list[str] = []
        for candidate, _ in _walk_source(self.root, (), nested):
            if candidate is not None:
                found.append(candidate.relative_to(self.root).as_posix())
        self.assertNotIn("brain/raw/captured.md", found)


if __name__ == "__main__":
    unittest.main()
