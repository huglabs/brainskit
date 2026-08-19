"""The freshness ledger's two invariants, each pinned by the defect it lost.

Freshness state was written by five callers and owned by none, so the rules a
reader depends on lived in whichever writer happened to state them:

- **Never downgrade.** `review` is weaker than `stale`, and `_refresh_staleness`
  skips `review` entirely -- so writing `review` over `stale` does not merely
  lower the badge, it takes the page out of the ageing loop for good. One writer
  guarded against that and the other, reached by `bk capture`, did not.
- **`content_hash` means the apply gate wrote this.** Only
  `compilation._freshness_updates` produces an entry carrying one. The two
  annotation writers create bare entries with `pages.setdefault(path, {})`, and
  the integrity check read "an entry exists" as "apply wrote this page" --
  so a bare entry laundered a hand-written page past `wiki.outside_apply`, the
  backstop that exists for the write gate failing open.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_fix_services import ServiceFixture

# Vocabulary the two pages and the related capture share, so `bk capture`
# clears `_RELATED_MIN_SHARED_TERMS` against the page bodies below.
_SHARED_PROSE = (
    "Memoria compilada organiza conhecimento antes da consulta, evitando "
    "trabalho de recuperacao no momento da pergunta."
)


class FreshnessLedgerFixture(ServiceFixture):
    def setUp(self) -> None:
        super().setUp()
        self.source = self.capture_into(
            "20-research", text=_SHARED_PROSE, title="Memoria compilada"
        )

    def capture_related(self) -> str:
        """A capture whose *content* relates to the pages, not just its name."""

        source = self.root.parent / "nota-relacionada.md"
        source.write_text(
            "A memoria compilada organiza o conhecimento antes da consulta e "
            "remove o trabalho de recuperacao do momento da pergunta.\n",
            encoding="utf-8",
        )
        self.addCleanup(source.unlink)
        return self.service.capture(str(source))["source"]["content_hash"]

    def entry(self, path: str) -> dict[str, Any]:
        return dict(self.vault.read_state("freshness")["pages"][path])

    def lint_codes_for(self, path: str) -> list[str]:
        return [
            finding["code"]
            for finding in self.service.lint()["findings"]
            if finding.get("path") == path
        ]


class NeverDowngradeTest(FreshnessLedgerFixture):
    """A capture relating to a stale page must not quietly un-stale it."""

    def setUp(self) -> None:
        super().setUp()
        self.page = self.upsert_page(
            "memoria-compilada",
            "Memoria compilada",
            f"{_SHARED_PROSE} ",
            self.source,
        )
        self.age_past_the_threshold()

    def age_past_the_threshold(self) -> None:
        """Reach `stale` through the real ageing loop, not by writing it in."""

        def backdate(state: dict[str, Any]) -> dict[str, Any]:
            state["pages"][self.page]["updated_at"] = "2000-01-01T00:00:00+00:00"
            return state

        self.vault.mutate_state("freshness", backdate)
        self.service.lint()
        self.assertEqual(self.entry(self.page)["status"], "stale")

    def test_a_related_capture_leaves_a_stale_page_stale(self) -> None:
        self.capture_related()
        self.assertEqual(self.entry(self.page)["status"], "stale")

    def test_a_downgraded_page_still_ages(self) -> None:
        """The cost of the downgrade: `review` is never re-examined."""

        self.capture_related()
        self.service.lint()
        self.assertEqual(self.entry(self.page)["status"], "stale")

    def test_lint_still_reports_the_page_as_stale(self) -> None:
        self.capture_related()
        self.assertIn("wiki.stale", self.lint_codes_for(self.page))

    def test_a_fresh_page_is_still_marked_for_review(self) -> None:
        """Control: the never-downgrade rule must not disable the transition."""

        fresh = self.upsert_page(
            "recuperacao-incremental",
            "Recuperacao incremental",
            f"{_SHARED_PROSE} A recuperacao incremental sustenta isso. ",
            self.source,
        )
        self.capture_related()
        self.assertEqual(self.entry(fresh)["status"], "review")


class AnnotationDoesNotLaunderTest(FreshnessLedgerFixture):
    """A bare entry is an annotation; it must not stand in for provenance."""

    def setUp(self) -> None:
        super().setUp()
        self.planted = "wiki/concepts/memoria-forjada.md"
        target = self.root / self.planted
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            "---\n"
            'id: "concept:memoria-forjada"\n'
            'type: "concept"\n'
            'title: "Memoria forjada"\n'
            "aliases:\n"
            "sources:\n"
            f'  - "{self.source}"\n'
            'updated_at: "2026-08-14T00:00:00+00:00"\n'
            "---\n"
            "\n"
            "# Memoria forjada\n"
            "\n"
            f"{_SHARED_PROSE}[^source:{self.source}]\n",
            encoding="utf-8",
        )
        # The page has to be findable for a capture to relate to it, exactly as
        # it would be in a vault where the write gate failed open and the next
        # command reindexed.
        self.service.reindex()

    def test_the_planted_page_is_reported_before_any_annotation(self) -> None:
        """Control: the check this test is about does fire to begin with."""

        self.assertIn("wiki.outside_apply", self.lint_codes_for(self.planted))

    def test_a_related_capture_does_not_silence_the_untracked_finding(self) -> None:
        self.capture_related()
        self.assertIn(self.planted, self.vault.read_state("freshness")["pages"])
        self.assertIn("wiki.outside_apply", self.lint_codes_for(self.planted))

    def test_the_annotation_is_still_recorded(self) -> None:
        """The entry is kept -- it is just not proof of provenance."""

        self.capture_related()
        self.assertNotIn("content_hash", self.entry(self.planted))

    def test_an_applied_page_is_not_reported(self) -> None:
        """Control: annotating a page the gate wrote must not accuse it.

        The applied page is deliberately related to the capture too, so it
        receives the same annotation the planted page does -- over an entry
        that already carries the hash apply recorded.
        """

        applied = self.upsert_page(
            "recuperacao-incremental",
            "Recuperacao incremental",
            "A recuperacao incremental atualiza o indice a cada captura e "
            "mantem barata a consulta que a memoria compilada organiza. ",
            self.source,
        )
        self.capture_related()
        self.assertIn("content_hash", self.entry(applied))
        self.assertNotIn("wiki.outside_apply", self.lint_codes_for(applied))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
