"""A slug may exist under one page kind only.

Pages live at `wiki/{kind}/{slug}.md` and the graph resolves `[[link]]` by stem
alone -- `slug_nodes[slug] = node_id`, last writer wins. So `concept:widget` and
`entity:widget` yield two files with one stem, and every `[[widget]]` in the
vault resolves to whichever directory sorts later.

Deterministic but arbitrary: adding a page-kind directory that sorts after the
existing ones would flip every such edge across the whole vault at once. The
losing page shows zero inbound links while its author believes it is connected,
and lint had no code for any of it.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_fix_services import ServiceFixture

from brainskit.domain.model import ValidationError


class SlugUniquenessTest(ServiceFixture):
    def setUp(self) -> None:
        super().setUp()
        self.source = self.capture_into(
            "30-public", text="Nota publica sobre widgets.", title="publico"
        )

    def apply_page(self, kind: str, slug: str, body: str) -> dict:
        return self.service.apply(
            {
                "operations": [
                    {
                        "action": "upsert",
                        "kind": kind,
                        "slug": slug,
                        "title": slug.title(),
                        "aliases": [],
                        "source_hashes": [self.source],
                        "body": f"{body}[^source:{self.source}]",
                        "links": [],
                    }
                ]
            }
        )

    def test_the_same_slug_under_a_second_kind_is_refused(self) -> None:
        self.apply_page("concept", "widget", "O conceito de widget aqui. ")
        with self.assertRaises(ValidationError) as refused:
            self.apply_page("entity", "widget", "A entidade widget, distinta. ")
        codes = [f["code"] for f in refused.exception.details["failures"]]
        self.assertIn("duplicate_slug", codes)

    def test_the_refusal_names_what_it_collides_with(self) -> None:
        self.apply_page("concept", "widget", "O conceito de widget aqui. ")
        with self.assertRaises(ValidationError) as refused:
            self.apply_page("entity", "widget", "A entidade widget, distinta. ")
        failure = next(
            f for f in refused.exception.details["failures"]
            if f["code"] == "duplicate_slug"
        )
        self.assertIn("wiki/concepts/widget.md", failure["conflicts_with"])

    def test_nothing_is_written_when_the_collision_is_refused(self) -> None:
        self.apply_page("concept", "widget", "O conceito de widget aqui. ")
        with self.assertRaises(ValidationError):
            self.apply_page("entity", "widget", "A entidade widget, distinta. ")
        self.assertFalse((self.root / "wiki" / "entities" / "widget.md").exists())

    def test_updating_the_same_page_is_not_a_collision(self) -> None:
        """Control: a slug colliding with itself is just an update."""

        self.apply_page("concept", "widget", "O conceito de widget aqui. ")
        self.assertTrue((self.root / "wiki" / "concepts" / "widget.md").is_file())

    def test_distinct_slugs_under_different_kinds_are_fine(self) -> None:
        """Control: the check must not refuse ordinary writes."""

        self.apply_page("concept", "widget", "O conceito de widget aqui. ")
        self.apply_page("entity", "engrenagem", "A engrenagem como entidade. ")
        self.assertTrue((self.root / "wiki" / "entities" / "engrenagem.md").is_file())

    def test_lint_reports_a_collision_that_already_exists(self) -> None:
        """A vault already carrying one must be repairable, not blocked."""

        self.apply_page("concept", "widget", "O conceito de widget aqui. ")
        planted = self.root / "wiki" / "entities" / "widget.md"
        planted.parent.mkdir(parents=True, exist_ok=True)
        planted.write_text(
            (self.root / "wiki" / "concepts" / "widget.md").read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        codes = [f["code"] for f in self.service.lint()["findings"]]
        self.assertIn("wiki.duplicate_slug", codes)

    def test_a_clean_vault_reports_no_duplicate_slug_finding(self) -> None:
        """Control: the sweep must not fire on every vault."""

        self.apply_page("concept", "widget", "O conceito de widget aqui. ")
        codes = [f["code"] for f in self.service.lint()["findings"]]
        self.assertNotIn("wiki.duplicate_slug", codes)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
