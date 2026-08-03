"""Derived artefacts must declare what they were built from.

`graph/graph.json` and `views/` are projections of the vault. Nothing used to
notice when the vault moved on without them, so a structural query could answer
from a snapshot taken weeks earlier and look perfectly healthy while doing it.

Two properties carry the whole design, and each has its own failure mode:

- The fingerprint is content, never mtime. A `git checkout` rewrites every mtime
  in the working tree, so an mtime rule would call a current graph stale and,
  after a checkout that restores an old graph, call a stale one current.
- It covers what the artefact actually reads, and only that. Too little and the
  drift goes unreported — a `bk capture` adds a `raw:` node to the graph without
  touching a single page. Too much and the check cries wolf, which loses the
  signal just as thoroughly. `ArtefactInputTest` pins the measured boundary.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from brainkit.application.codegraph import CODE_PROJECTION
from brainkit.application.freshness import (
    GRAPH_PROJECTION,
    PROJECTION_RAW_FIELDS,
    VIEWS_PROJECTION,
    _fingerprint_row,
    _projection_source_hash,
)
from brainkit.application.services import BrainkitService
from brainkit.domain.model import SourceRecord
from brainkit.infrastructure.graph import MarkdownGraph
from brainkit.infrastructure.index import SqliteFtsIndex
from brainkit.infrastructure.vault import FileVault

PROJECTION_CODES = {"graph.stale", "views.stale"}


def policy() -> dict:
    return {
        "version": 3,
        "wiki_language": "Portuguese (Brazil)",
        "inbox_policy": {"privacy": "local-only", "filing": "approve-each"},
        "branches": {
            "20-research": {"privacy": "local-only", "filing": "auto+digest-review"},
        },
        "providers": {"ollama": {"base_url": "http://127.0.0.1:11434"}},
        "job_models": {
            job: {"provider": "ollama", "model": "test"}
            for job in (
                "ingest",
                "query",
                "digest",
                "lint-semantic",
                "file-proposal",
                "resurface",
            )
        },
        "sources": [],
        "schedule": {"digest": "0 8 * * *"},
        "taxonomy_seed": ["research"],
        "novelty": {
            "duplicate_similarity_threshold": 0.9,
            "min_new_token_ratio": 0.15,
            "stale_after_days": 30,
        },
        "integrations": {
            "obsidian": {"enabled": False, "managed": False, "options": {}},
            "neo4j": {"enabled": False, "managed": False, "options": {}},
            "postgres": {"enabled": False, "managed": False, "options": {}},
            "web": {"enabled": False, "managed": True, "options": {}},
        },
    }


class ProjectionFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.vault = FileVault.initialize(self.root, policy())
        self.service = BrainkitService(
            self.vault,
            SqliteFtsIndex(self.vault.index_path),
            graph=MarkdownGraph(),
        )
        # `bk init` reindexes and then writes the views, so a real vault has
        # `views/home.md` from its first minute. Mirror that here: the point of
        # the "fresh vault" test is what an actual `bk init` leaves behind.
        self.service.reindex()
        self.service.views()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def capture(self, *, text: str, title: str, branch: str | None = None) -> str:
        captured = self.service.capture(None, text=text, title=title)
        content_hash = str(captured["source"]["content_hash"])
        if branch is not None:
            self.service.file(content_hash, branch)
        return content_hash

    def apply_page(self, slug: str, source_hash: str, body: str = "Corpo.") -> str:
        path = f"wiki/concepts/{slug}.md"
        operation = {
            "action": "upsert",
            "kind": "concept",
            "slug": slug,
            "title": slug.replace("-", " ").title(),
            "aliases": [],
            "source_hashes": [source_hash],
            "body": f"{body}[^source:{source_hash}]",
            "links": [],
        }
        # Rewriting an existing page needs the version it is replacing, which is
        # how the apply gate refuses a blind overwrite.
        base_hash = self.vault.wiki_version(path)
        if base_hash is not None:
            operation["base_hash"] = base_hash
        self.service.apply({"operations": [operation]})
        return path

    def lint_codes(self) -> list[str]:
        return [item["code"] for item in self.service.lint()["findings"]]

    def projection_codes(self) -> set[str]:
        return PROJECTION_CODES.intersection(self.lint_codes())

    def projections(self) -> dict:
        return dict(self.service.status()["projections"])

    def recorded(self) -> dict:
        state = self.vault.read_state("freshness")
        return dict(state.get("projections", {}))

    def freshness(self) -> dict:
        return self.vault.read_state("freshness")

    def source_hash(self, artifact: str) -> str:
        return _projection_source_hash(
            self.freshness().get("pages", {}),
            self.vault.registry(),
            PROJECTION_RAW_FIELDS[artifact],
        )

    def artifact_bytes(self) -> dict[str, str]:
        """The rendered artefacts, for asserting a mutation really shows up."""
        out: dict[str, str] = {}
        graph = self.root / GRAPH_PROJECTION
        if graph.is_file():
            out[GRAPH_PROJECTION] = graph.read_text(encoding="utf-8")
        for path in sorted((self.root / "views").rglob("*.md")):
            out[path.relative_to(self.root).as_posix()] = path.read_text(
                encoding="utf-8"
            )
        return out

    def regenerate(self) -> dict[str, str]:
        self.service.graph()
        self.service.views()
        return self.artifact_bytes()

    def set_raw_status(self, content_hash: str, status: str) -> None:
        records = self.vault.registry()
        records[content_hash].status = status
        self.vault.save_registry(records)


class FreshVaultTest(ProjectionFixture):
    """A first-time user must not be greeted by findings they cannot act on."""

    def test_init_leaves_no_projection_findings(self) -> None:
        self.assertEqual(self.projection_codes(), set())

    def test_init_records_the_views_it_generated(self) -> None:
        views = self.projections()[VIEWS_PROJECTION]
        self.assertEqual(views["state"], "fresh")
        self.assertFalse(views["stale"])
        self.assertIsNotNone(views["generated_at"])

    def test_a_graph_that_was_never_built_is_missing_not_stale(self) -> None:
        graph = self.projections()[GRAPH_PROJECTION]
        self.assertEqual(graph["state"], "missing")
        self.assertFalse(graph["stale"])
        self.assertIsNone(graph["generated_at"])
        self.assertNotIn("age_days", graph)
        self.assertFalse((self.root / GRAPH_PROJECTION).exists())

    def test_generating_a_graph_on_an_empty_vault_stays_clean(self) -> None:
        self.service.graph()
        self.assertEqual(self.projections()[GRAPH_PROJECTION]["state"], "fresh")
        self.assertEqual(self.projection_codes(), set())


class StaleAfterApplyTest(ProjectionFixture):
    """The audit case: the vault moved on, the projections did not."""

    def setUp(self) -> None:
        super().setUp()
        self.service.graph()
        self.source = self.capture(text="Evidencia sobre memoria.", title="memoria")
        self.page = self.apply_page("memoria-compilada", self.source)

    def test_both_projections_go_stale(self) -> None:
        self.assertEqual(self.projection_codes(), PROJECTION_CODES)
        for artifact in (GRAPH_PROJECTION, VIEWS_PROJECTION):
            with self.subTest(artifact=artifact):
                report = self.projections()[artifact]
                self.assertEqual(report["state"], "stale")
                self.assertTrue(report["stale"])

    def test_each_finding_names_the_command_that_repairs_it(self) -> None:
        messages = {
            item["code"]: item["message"]
            for item in self.service.lint()["findings"]
            if item["code"] in PROJECTION_CODES
        }
        self.assertIn("bk graph", messages["graph.stale"])
        self.assertIn("bk views", messages["views.stale"])

    def test_a_stale_projection_is_a_warning_not_an_error(self) -> None:
        severities = {
            item["severity"]
            for item in self.service.lint()["findings"]
            if item["code"] in PROJECTION_CODES
        }
        self.assertEqual(severities, {"warning"})
        self.assertTrue(self.service.lint()["ok"])
        self.assertTrue(self.service.status()["healthy"])

    def test_rebuilding_the_graph_clears_only_the_graph_finding(self) -> None:
        self.service.graph()
        self.assertEqual(self.projection_codes(), {"views.stale"})
        self.assertEqual(self.projections()[GRAPH_PROJECTION]["state"], "fresh")
        self.assertEqual(self.projections()[VIEWS_PROJECTION]["state"], "stale")

    def test_rebuilding_both_clears_every_projection_finding(self) -> None:
        self.service.graph()
        self.service.views()
        self.assertEqual(self.projection_codes(), set())
        self.assertEqual(self.projections()[VIEWS_PROJECTION]["state"], "fresh")

    def test_a_second_page_makes_them_stale_again(self) -> None:
        self.service.graph()
        self.service.views()
        second = self.capture(text="Outra evidencia distinta.", title="outra")
        self.apply_page("outra-pagina", second)
        self.assertEqual(self.projection_codes(), PROJECTION_CODES)


class IdempotencyTest(ProjectionFixture):
    """Regenerating an unchanged vault must not move the fingerprint."""

    def setUp(self) -> None:
        super().setUp()
        self.source = self.capture(text="Evidencia estavel.", title="estavel")
        self.apply_page("pagina-estavel", self.source)
        self.service.graph()
        self.service.views()

    def test_running_graph_twice_keeps_the_same_source_hash(self) -> None:
        first = self.recorded()[GRAPH_PROJECTION]["source_hash"]
        self.service.graph()
        self.assertEqual(self.recorded()[GRAPH_PROJECTION]["source_hash"], first)

    def test_running_views_twice_keeps_the_same_source_hash(self) -> None:
        first = self.recorded()[VIEWS_PROJECTION]["source_hash"]
        self.service.views()
        self.assertEqual(self.recorded()[VIEWS_PROJECTION]["source_hash"], first)

    def test_a_second_run_produces_no_finding(self) -> None:
        self.service.graph()
        self.service.views()
        self.assertEqual(self.projection_codes(), set())

    def test_repeated_lints_do_not_invent_staleness(self) -> None:
        # `_mechanical_lint` refreshes page staleness in place on every call. If
        # the fingerprint covered anything refresh rewrites, lint would report
        # its own projections stale from the second run onward.
        for attempt in range(3):
            with self.subTest(attempt=attempt):
                self.assertEqual(self.projection_codes(), set())

    def test_aging_a_page_does_not_disturb_the_projections(self) -> None:
        page = "wiki/concepts/pagina-estavel.md"

        def age(state: dict) -> dict:
            state["pages"][page]["updated_at"] = (
                datetime.now(UTC) - timedelta(days=45)
            ).isoformat()
            return state

        self.vault.mutate_state("freshness", age)
        codes = self.lint_codes()
        self.assertIn("wiki.stale", codes)
        self.assertEqual(PROJECTION_CODES.intersection(codes), set())

    def test_marking_a_page_for_review_does_not_disturb_the_projections(self) -> None:
        # A page flipped to `review` keeps its content hash, so the input set is
        # unchanged. The badge in `views/map/*.md` does move — see
        # ArtefactInputTest for why that one field stays out of the fingerprint.
        def review(state: dict) -> dict:
            for entry in state["pages"].values():
                entry["status"] = "review"
                entry["review_reason"] = "related source"
            return state

        self.vault.mutate_state("freshness", review)
        self.assertEqual(self.projection_codes(), set())


class MtimeIndependenceTest(ProjectionFixture):
    """A checkout rewrites mtimes. Freshness must not notice."""

    def setUp(self) -> None:
        super().setUp()
        self.source = self.capture(text="Evidencia duravel.", title="duravel")
        self.page = self.apply_page("pagina-duravel", self.source)
        self.service.graph()
        self.service.views()

    def touch(self, relative: str, *, days: int) -> None:
        stamp = (datetime.now(UTC) + timedelta(days=days)).timestamp()
        os.utime(self.root / relative, (stamp, stamp))

    def test_touching_the_projections_keeps_them_fresh(self) -> None:
        self.touch(GRAPH_PROJECTION, days=90)
        self.touch("views/home.md", days=90)
        self.assertEqual(self.projection_codes(), set())
        self.assertEqual(self.projections()[GRAPH_PROJECTION]["state"], "fresh")
        self.assertEqual(self.projections()[VIEWS_PROJECTION]["state"], "fresh")

    def test_touching_a_page_newer_than_its_projections_keeps_them_fresh(self) -> None:
        # The exact shape of the bug this feature exists to catch, inverted: a
        # page modified after the graph was built. Under an mtime rule this
        # reports stale; the content did not change, so it is not.
        self.touch(self.page, days=90)
        self.assertEqual(self.projection_codes(), set())

    def test_backdating_a_page_does_not_hide_a_real_change(self) -> None:
        second = self.capture(text="Segunda evidencia distinta.", title="segunda")
        self.apply_page("segunda-pagina", second)
        self.touch("wiki/concepts/segunda-pagina.md", days=-365)
        self.assertEqual(self.projection_codes(), PROJECTION_CODES)


class DeletedProjectionTest(ProjectionFixture):
    """`views/` is a tree; `graph/graph.json` is a file. Both need an anchor."""

    def setUp(self) -> None:
        super().setUp()
        self.source = self.capture(text="Evidencia presente.", title="presente")
        self.apply_page("pagina-presente", self.source)
        self.service.graph()
        self.service.views()

    def test_a_deleted_graph_reports_missing_and_keeps_its_history(self) -> None:
        (self.root / GRAPH_PROJECTION).unlink()
        graph = self.projections()[GRAPH_PROJECTION]
        self.assertEqual(graph["state"], "missing")
        self.assertFalse(graph["stale"])
        self.assertIsNotNone(graph["generated_at"])
        self.assertEqual(self.projection_codes(), set())

    def test_a_deleted_views_home_reports_missing(self) -> None:
        (self.root / "views" / "home.md").unlink()
        self.assertEqual(self.projections()[VIEWS_PROJECTION]["state"], "missing")

    def test_the_scaffolded_views_directories_are_not_the_artefact(self) -> None:
        # `bk init` creates `views/map` and `views/domains` before anything is
        # written into them, so directory existence cannot stand for "generated".
        (self.root / "views" / "home.md").unlink()
        self.assertTrue((self.root / "views" / "map").is_dir())
        self.assertEqual(self.projections()[VIEWS_PROJECTION]["state"], "missing")

    def test_regenerating_restores_the_artefact(self) -> None:
        (self.root / GRAPH_PROJECTION).unlink()
        self.service.graph()
        self.assertEqual(self.projections()[GRAPH_PROJECTION]["state"], "fresh")


class UnrecordedProjectionTest(ProjectionFixture):
    """A projection whose provenance is unknown is not trusted."""

    def setUp(self) -> None:
        super().setUp()
        self.source = self.capture(text="Evidencia antiga.", title="antiga")
        self.apply_page("pagina-antiga", self.source)
        self.service.graph()
        self.service.views()

    def drop_records(self) -> None:
        def mutate(state: dict) -> dict:
            state.pop("projections", None)
            return state

        self.vault.mutate_state("freshness", mutate)

    def test_an_artefact_with_no_record_is_stale(self) -> None:
        # What a vault written by an older brainkit looks like: the files are
        # there, nothing says which pages they cover.
        self.drop_records()
        self.assertEqual(self.projection_codes(), PROJECTION_CODES)
        for artifact in (GRAPH_PROJECTION, VIEWS_PROJECTION):
            with self.subTest(artifact=artifact):
                report = self.projections()[artifact]
                self.assertEqual(report["state"], "stale")
                self.assertIsNone(report["generated_at"])

    def test_regenerating_adopts_it(self) -> None:
        self.drop_records()
        self.service.graph()
        self.service.views()
        self.assertEqual(self.projection_codes(), set())

    def test_a_malformed_record_degrades_to_stale(self) -> None:
        def corrupt(state: dict) -> dict:
            state["projections"] = {GRAPH_PROJECTION: "not-an-object"}
            return state

        self.vault.mutate_state("freshness", corrupt)
        self.assertEqual(self.projections()[GRAPH_PROJECTION]["state"], "stale")


class CaptureOnlyStalenessTest(ProjectionFixture):
    """A capture with no apply used to slip past the check entirely.

    The trace this reproduces: `nodes=2 lint=clean projections=fresh` before the
    capture and `nodes=2 lint=clean projections=fresh` after it, with a
    regeneration then jumping to 3 nodes. The graph was already wrong and
    nothing said so, which is the whole failure this check exists to eliminate.
    """

    def setUp(self) -> None:
        super().setUp()
        self.source = self.capture(
            text="Evidencia inicial.", title="inicial", branch="20-research"
        )
        self.apply_page("pagina-inicial", self.source)
        self.service.graph()
        self.service.views()

    def graph_nodes(self) -> int:
        payload = json.loads((self.root / GRAPH_PROJECTION).read_text(encoding="utf-8"))
        return len(payload["nodes"])

    def test_the_baseline_is_clean(self) -> None:
        self.assertEqual(self.projection_codes(), set())

    def test_a_capture_alone_makes_the_graph_stale(self) -> None:
        self.capture(text="Uma evidencia inteiramente nova.", title="nova")
        self.assertIn("graph.stale", self.projection_codes())
        self.assertTrue(self.projections()[GRAPH_PROJECTION]["stale"])

    def test_the_staleness_is_real_and_regeneration_clears_it(self) -> None:
        before = self.graph_nodes()
        self.capture(text="Uma evidencia inteiramente nova.", title="nova")
        self.assertIn("graph.stale", self.projection_codes())
        self.service.graph()
        # The node the stale graph was missing.
        self.assertEqual(self.graph_nodes(), before + 1)
        self.assertNotIn("graph.stale", self.projection_codes())

    def test_a_capture_alone_also_makes_the_views_stale(self) -> None:
        # Measured, not assumed: `views/home.md` counts sources and the branch
        # maps list them, so a capture changes that output too.
        self.capture(text="Uma evidencia inteiramente nova.", title="nova")
        self.assertEqual(self.projection_codes(), PROJECTION_CODES)

    def test_filing_a_source_into_a_branch_makes_both_stale(self) -> None:
        # `bk file` moves the raw record, which is the node path in the graph
        # and the branch map a source appears in.
        second = self.capture(text="Evidencia para arquivar.", title="arquivar")
        self.service.graph()
        self.service.views()
        self.service.file(second, "20-research")
        self.assertEqual(self.projection_codes(), PROJECTION_CODES)

    def test_the_page_set_alone_would_not_have_noticed(self) -> None:
        # The defect stated precisely: the wiki page set is untouched by a
        # capture, so a pages-only fingerprint cannot move.
        before = dict(self.freshness().get("pages", {}))
        self.capture(text="Uma evidencia inteiramente nova.", title="nova")
        self.assertEqual(dict(self.freshness().get("pages", {})), before)
        self.assertIn("graph.stale", self.projection_codes())


class ArtefactInputTest(ProjectionFixture):
    """Each artefact is measured against what it actually renders.

    These tests assert the input boundary in both directions: the mutation
    changes the artefact's bytes AND ages it, or changes neither. That is what
    keeps the fingerprint honest as the renderers evolve — a graph that starts
    rendering a field it used to ignore fails here rather than going quietly
    stale in the field.
    """

    def setUp(self) -> None:
        super().setUp()
        self.source = self.capture(
            text="Evidencia sobre memoria.", title="memoria", branch="20-research"
        )
        self.apply_page("pagina-memoria", self.source)
        self.before = self.regenerate()

    def test_a_raw_status_change_ages_views_and_not_the_graph(self) -> None:
        self.set_raw_status(self.source, "pending")
        self.assertEqual(self.projection_codes(), {"views.stale"})

    def test_a_raw_status_change_really_only_moves_the_views_bytes(self) -> None:
        self.set_raw_status(self.source, "pending")
        after = self.regenerate()
        self.assertEqual(after[GRAPH_PROJECTION], self.before[GRAPH_PROJECTION])
        self.assertNotEqual(
            {k: v for k, v in after.items() if k.startswith("views/")},
            {k: v for k, v in self.before.items() if k.startswith("views/")},
        )

    def test_a_page_freshness_status_change_ages_neither(self) -> None:
        # This one is a deliberate exclusion, not an oversight. The badge in
        # `views/map/*.md` does move — but it moves with the clock, not with
        # anything a user did, so folding it in would make `views.stale` appear
        # on a vault nobody touched. `bk status` reports page freshness live.
        def review(state: dict) -> dict:
            for entry in state["pages"].values():
                entry["status"] = "review"
            return state

        self.vault.mutate_state("freshness", review)
        self.assertEqual(self.projection_codes(), set())

    def test_page_age_days_reaches_no_artefact_at_all(self) -> None:
        def age(state: dict) -> dict:
            for entry in state["pages"].values():
                entry["age_days"] = 999
            return state

        self.vault.mutate_state("freshness", age)
        after = self.regenerate()
        self.assertEqual(after, self.before)
        self.assertEqual(self.projection_codes(), set())

    def test_the_declared_field_sets_match_what_is_rendered(self) -> None:
        self.assertEqual(
            PROJECTION_RAW_FIELDS[GRAPH_PROJECTION], ("path", "original_name")
        )
        self.assertEqual(
            PROJECTION_RAW_FIELDS[VIEWS_PROJECTION],
            ("path", "original_name", "status", "captured_at"),
        )

    def test_the_two_artefacts_do_not_share_a_fingerprint(self) -> None:
        # A shared hash would age both on any change either one renders.
        self.assertNotEqual(
            self.source_hash(GRAPH_PROJECTION), self.source_hash(VIEWS_PROJECTION)
        )


class SourceHashTest(ProjectionFixture):
    """The fingerprint has to mean the same thing in every process."""

    def setUp(self) -> None:
        super().setUp()
        self.source = self.capture(
            text="Evidencia inicial.", title="inicial", branch="20-research"
        )
        self.apply_page("pagina-inicial", self.source)

    def hash_in_subprocess(self, seed: str, artifact: str) -> str:
        program = (
            "import json, sys;"
            "from brainkit.application.freshness import"
            " _projection_source_hash, PROJECTION_RAW_FIELDS;"
            "from brainkit.domain.model import SourceRecord;"
            "pages = json.load(open(sys.argv[1]))['pages'];"
            "raw = json.load(open(sys.argv[2]))['sources'];"
            "records = {k: SourceRecord.from_dict(v) for k, v in raw.items()};"
            "print(_projection_source_hash("
            "pages, records, PROJECTION_RAW_FIELDS[sys.argv[3]]))"
        )
        completed = subprocess.run(
            [
                sys.executable,
                "-c",
                program,
                str(self.root / ".brain" / "freshness.json"),
                str(self.root / ".brain" / "registry.json"),
                artifact,
            ],
            capture_output=True,
            text=True,
            check=True,
            env={**os.environ, "PYTHONHASHSEED": seed},
        )
        return completed.stdout.strip()

    def test_the_same_inputs_hash_identically_in_another_process(self) -> None:
        # Dict iteration order and str hashing are the two things that could
        # make this drift, so the subprocesses run under a different
        # PYTHONHASHSEED than the one pytest is using.
        for artifact in (GRAPH_PROJECTION, VIEWS_PROJECTION):
            expected = self.source_hash(artifact)
            self.assertEqual(len(expected), 64)
            for seed in ("0", "1", "12345"):
                with self.subTest(artifact=artifact, seed=seed):
                    self.assertEqual(self.hash_in_subprocess(seed, artifact), expected)

    def test_the_recorded_hash_matches_the_inputs_it_covers(self) -> None:
        self.service.graph()
        self.service.views()
        for artifact in (GRAPH_PROJECTION, VIEWS_PROJECTION):
            with self.subTest(artifact=artifact):
                self.assertEqual(
                    self.recorded()[artifact]["source_hash"],
                    self.source_hash(artifact),
                )

    def test_applying_a_page_changes_the_hash(self) -> None:
        before = self.source_hash(GRAPH_PROJECTION)
        second = self.capture(text="Evidencia posterior distinta.", title="posterior")
        self.apply_page("pagina-posterior", second)
        self.assertNotEqual(self.source_hash(GRAPH_PROJECTION), before)

    def test_editing_a_page_through_the_gate_changes_the_hash(self) -> None:
        before = self.source_hash(GRAPH_PROJECTION)
        self.apply_page("pagina-inicial", self.source, body="Corpo revisado.")
        self.assertNotEqual(self.source_hash(GRAPH_PROJECTION), before)

    def test_capturing_changes_the_hash_without_touching_a_page(self) -> None:
        before = self.source_hash(GRAPH_PROJECTION)
        self.capture(text="Mais uma evidencia distinta.", title="mais")
        self.assertNotEqual(self.source_hash(GRAPH_PROJECTION), before)

    def test_an_empty_vault_still_hashes(self) -> None:
        digest = _projection_source_hash({}, {}, ("path",))
        self.assertEqual(len(digest), 64)
        self.assertEqual(digest, _projection_source_hash(None, {}, ("path",)))

    def test_the_hash_ignores_the_fields_refresh_rewrites(self) -> None:
        pages = dict(self.freshness()["pages"])
        records = self.vault.registry()
        fields = PROJECTION_RAW_FIELDS[VIEWS_PROJECTION]
        expected = _projection_source_hash(pages, records, fields)
        for path, entry in pages.items():
            entry["status"] = "review"
            entry["age_days"] = 999
            entry["review_reason"] = f"noise for {path}"
        self.assertEqual(_projection_source_hash(pages, records, fields), expected)

    def test_input_order_does_not_matter(self) -> None:
        pages = {
            "wiki/concepts/b.md": {"content_hash": "b" * 64},
            "wiki/concepts/a.md": {"content_hash": "a" * 64},
        }
        records = self.vault.registry()
        fields = PROJECTION_RAW_FIELDS[GRAPH_PROJECTION]
        self.assertEqual(
            _projection_source_hash(pages, records, fields),
            _projection_source_hash(
                dict(reversed(list(pages.items()))),
                dict(reversed(list(records.items()))),
                fields,
            ),
        )


class DomainSeparationTest(unittest.TestCase):
    """A page path and a raw content hash must never encode alike."""

    def record(self, content_hash: str, path: str) -> SourceRecord:
        return SourceRecord(
            content_hash=content_hash,
            path=path,
            original_name="x.md",
            media_type="text/markdown",
            size=1,
            captured_at="2026-01-01T00:00:00+00:00",
        )

    def test_the_namespace_tag_separates_the_two_domains(self) -> None:
        self.assertNotEqual(
            _fingerprint_row("page", "a", "b"), _fingerprint_row("raw", "a", "b")
        )

    def test_a_value_cannot_fake_a_field_boundary(self) -> None:
        self.assertNotEqual(
            _fingerprint_row("page", "a", "b:c"), _fingerprint_row("page", "a:b", "c")
        )
        self.assertNotEqual(
            _fingerprint_row("page", "ab", "c"), _fingerprint_row("page", "a", "bc")
        )
        self.assertNotEqual(
            _fingerprint_row("page", "a|b"), _fingerprint_row("page", "a", "b")
        )

    def test_the_same_pair_in_either_domain_hashes_differently(self) -> None:
        # The exact transposition the namespace tag exists to prevent. A page
        # row is (path, content_hash); a raw row with raw_fields=("path",) is
        # (content_hash, path). Line them up so both rows carry the identical
        # two values in the identical order — without the tag these collide,
        # and one vault's page set would be indistinguishable from another
        # vault's registry.
        value = "a" * 64
        path = "raw/_inbox/x.md"
        fields = ("path",)
        as_page = _projection_source_hash({value: {"content_hash": path}}, {}, fields)
        as_raw = _projection_source_hash(
            {}, {value: self.record(value, path)}, fields
        )
        self.assertNotEqual(as_page, as_raw)

    def test_a_page_hash_and_a_raw_hash_are_not_interchangeable(self) -> None:
        value = "a" * 64
        left = _projection_source_hash(
            {"wiki/a.md": {"content_hash": value}},
            {value: self.record(value, "raw/_inbox/a.md")},
            ("path",),
        )
        right = _projection_source_hash(
            {"raw/_inbox/a.md": {"content_hash": value}},
            {value: self.record(value, "wiki/a.md")},
            ("path",),
        )
        self.assertNotEqual(left, right)


class StatusBlockTest(ProjectionFixture):
    """`bk status` is where the three states are told apart."""

    def setUp(self) -> None:
        super().setUp()
        self.source = self.capture(text="Evidencia para o status.", title="status")
        self.apply_page("pagina-status", self.source)

    def test_the_block_covers_exactly_the_known_artefacts(self) -> None:
        # The code graph joined the block rather than getting a key of its own:
        # it is derived, regenerable and worthless once its inputs move, which
        # is the definition the other two already satisfy.
        self.assertEqual(
            set(self.projections()),
            {GRAPH_PROJECTION, VIEWS_PROJECTION, CODE_PROJECTION},
        )

    def test_each_entry_carries_the_contracted_fields(self) -> None:
        self.service.graph()
        for artifact, report in self.projections().items():
            with self.subTest(artifact=artifact):
                self.assertIn("generated_at", report)
                self.assertIn("stale", report)
                self.assertIsInstance(report["stale"], bool)
                self.assertIn(report["state"], {"missing", "stale", "fresh"})

    def test_age_days_appears_once_the_artefact_has_been_generated(self) -> None:
        self.assertNotIn("age_days", self.projections()[GRAPH_PROJECTION])
        self.service.graph()
        self.assertEqual(self.projections()[GRAPH_PROJECTION]["age_days"], 0)

    def test_age_days_counts_from_the_recorded_generation(self) -> None:
        self.service.graph()

        def backdate(state: dict) -> dict:
            state["projections"][GRAPH_PROJECTION]["generated_at"] = (
                datetime.now(UTC) - timedelta(days=28)
            ).isoformat()
            return state

        self.vault.mutate_state("freshness", backdate)
        self.assertEqual(self.projections()[GRAPH_PROJECTION]["age_days"], 28)

    def test_the_three_states_are_reported_distinctly(self) -> None:
        self.assertEqual(self.projections()[GRAPH_PROJECTION]["state"], "missing")
        self.assertEqual(self.projections()[VIEWS_PROJECTION]["state"], "stale")
        self.service.graph()
        self.service.views()
        self.assertEqual(self.projections()[GRAPH_PROJECTION]["state"], "fresh")
        self.assertEqual(self.projections()[VIEWS_PROJECTION]["state"], "fresh")

    def test_the_page_freshness_summary_is_untouched(self) -> None:
        summary = self.service.status()["freshness"]
        self.assertEqual(set(summary), {"fresh", "review", "stale", "unknown"})
        self.assertEqual(summary["fresh"], 1)

    def test_the_projections_survive_a_round_trip_through_json(self) -> None:
        self.service.graph()
        restored = json.loads(json.dumps(self.service.status()))["projections"]
        self.assertEqual(restored, self.projections())


if __name__ == "__main__":
    unittest.main()
