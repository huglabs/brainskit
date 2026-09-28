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

try:
    from . import _harness
except ImportError:
    import _harness  # noqa: F401

import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from brainskit.application.codegraph import CODE_PROJECTION, _malformation
from brainskit.application.freshness import (
    GRAPH_PROJECTION,
    PROJECTION_RAW_FIELDS,
    VIEWS_PROJECTION,
    _fingerprint_row,
    _graph_integrity,
    _projection_source_hash,
)
from brainskit.application.pages import GENERATED_MARKER, parse_frontmatter
from brainskit.application.services import BrainskitService
from brainskit.domain.model import (
    SEED_PAGES,
    SourceRecord,
    is_seed_template,
    render_seed_page,
)
from brainskit.infrastructure.graph import MarkdownGraph
from brainskit.infrastructure.index import SqliteFtsIndex
from brainskit.infrastructure.vault import FileVault

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
        self.service = BrainskitService(
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
        # Asserted on `lint_errors`, not on `healthy`. This test is about a
        # stale projection being a warning rather than an error, and `healthy`
        # now also means "every non-advisory enforcement layer is live" -- true
        # of a real vault, not of a bare temp directory. Using the composite
        # headline as a stand-in for "lint is clean" is what made it fail here.
        self.assertEqual(self.service.status()["lint_errors"], 0)

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
        # What a vault written by an older brainskit looks like: the files are
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
            "from brainskit.application.freshness import"
            " _projection_source_hash, PROJECTION_RAW_FIELDS;"
            "from brainskit.domain.model import SourceRecord;"
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


class UnusableProjectionTest(ProjectionFixture):
    """An artefact can be perfectly current and still answer nothing.

    Freshness compares a fingerprint recorded in `freshness.json` against the
    vault. Nothing in that comparison opens the artefact, so overwriting
    `graph/graph.json` with `{{{ not json at all` left every input untouched and
    `bk status` reported it `fresh` — a surface reporting on something it never
    checked, the same shape as a hook reported active from the existence of its
    file. `bk code status` was fixed for exactly this in 0.6.1; these pin the
    same answer for the vault's own two projections.
    """

    def setUp(self) -> None:
        super().setUp()
        self.source = self.capture(text="Evidencia usavel.", title="usavel")
        self.apply_page("pagina-usavel", self.source)
        self.service.graph()
        self.service.views()

    def write_graph(self, text: str) -> None:
        (self.root / GRAPH_PROJECTION).write_text(text, encoding="utf-8")

    def write_home(self, text: str) -> None:
        (self.root / "views" / "home.md").write_text(text, encoding="utf-8")

    def graph_report(self) -> dict:
        return dict(self.projections()[GRAPH_PROJECTION])

    def views_report(self) -> dict:
        return dict(self.projections()[VIEWS_PROJECTION])

    def test_a_graph_that_is_not_json_is_malformed_not_fresh(self) -> None:
        self.write_graph("{{{ not json at all\n")
        report = self.graph_report()
        self.assertEqual(report["state"], "malformed")
        self.assertEqual(report["problem"], "not JSON")

    def test_the_fingerprint_alone_would_still_have_said_fresh(self) -> None:
        # The defect in one assertion: every input the freshness check looks at
        # is untouched, and the artefact is garbage. If this ever fails because
        # the hashes diverged, the test below it stops proving anything.
        self.write_graph("{{{ not json at all\n")
        self.assertEqual(
            self.recorded()[GRAPH_PROJECTION]["source_hash"],
            self.source_hash(GRAPH_PROJECTION),
        )
        self.assertEqual(self.graph_report()["state"], "malformed")

    def test_a_malformed_artefact_is_never_reported_as_healthy(self) -> None:
        self.write_graph('{"nodes": [], "edges": {}}\n')
        self.assertTrue(self.graph_report()["stale"])

    def test_json_that_is_not_an_object_is_malformed(self) -> None:
        for text in ("[]\n", '"a graph"\n', "null\n"):
            with self.subTest(text=text):
                self.write_graph(text)
                report = self.graph_report()
                self.assertEqual(report["state"], "malformed")
                self.assertIn("not an object", report["problem"])

    def test_an_untraversable_graph_names_the_field_it_lacks(self) -> None:
        self.write_graph('{"nodes": [{"label": "x"}], "edges": []}\n')
        report = self.graph_report()
        self.assertEqual(report["state"], "malformed")
        self.assertEqual(report["collection"], "nodes")
        self.assertEqual(report["missing"], ["id"])

    def test_an_edge_without_a_type_is_malformed(self) -> None:
        # `infrastructure/graph.py` and both database writers subscript
        # `edge["type"]`, so a graph without it cannot be exported at all.
        self.write_graph(
            '{"nodes": [{"id": "a"}, {"id": "b"}], '
            '"edges": [{"source": "a", "target": "b"}]}\n'
        )
        report = self.graph_report()
        self.assertEqual(report["state"], "malformed")
        self.assertEqual(report["missing"], ["type"])

    def test_the_graph_detector_is_the_code_graph_detector(self) -> None:
        # Reuse pinned, not just documented: a second copy of the rule would let
        # `bk status` and `bk code status` drift while both looked authoritative.
        for graph in (
            {"nodes": [{"label": "x"}], "edges": []},
            {"nodes": [], "edges": {}},
            {"nodes": [{"id": "a"}], "edges": [{"source": "a", "target": "a"}]},
            {"nodes": [{"id": "a"}], "edges": []},
        ):
            with self.subTest(graph=graph):
                self.assertEqual(
                    _graph_integrity(json.dumps(graph)), _malformation(graph)
                )

    def test_a_hand_written_views_home_is_malformed(self) -> None:
        self.write_home("# my own notes\n")
        report = self.views_report()
        self.assertEqual(report["state"], "malformed")
        self.assertEqual(report["problem"], "changed since it was generated")

    def test_an_empty_views_home_is_malformed(self) -> None:
        self.write_home("")
        self.assertEqual(self.views_report()["state"], "malformed")

    def test_a_views_home_gutted_to_its_marker_is_malformed(self) -> None:
        # TC2 (#33). The marker line was the whole identity check, so a home
        # page emptied down to it still read `fresh`.
        self.write_home(GENERATED_MARKER + "\n")
        report = self.views_report()
        self.assertEqual(report["state"], "malformed")
        self.assertTrue(report["stale"])

    def test_a_graph_that_still_parses_but_was_edited_is_malformed(self) -> None:
        # The twin for the graph: its structural check passes any traversable
        # graph, including one with nodes deleted by hand.
        self.write_graph('{"nodes": [], "edges": []}\n')
        self.assertIsNone(_graph_integrity('{"nodes": [], "edges": []}'))
        report = self.graph_report()
        self.assertEqual(report["state"], "malformed")
        self.assertEqual(report["problem"], "changed since it was generated")

    def test_generation_stamps_the_bytes_it_wrote(self) -> None:
        for artifact, anchor in (
            (GRAPH_PROJECTION, GRAPH_PROJECTION),
            (VIEWS_PROJECTION, "views/home.md"),
        ):
            with self.subTest(artifact=artifact):
                self.assertEqual(
                    self.recorded()[artifact]["artefact_hash"],
                    self.vault.content_hash(anchor),
                )

    def test_a_malformed_artefact_is_a_warning_that_names_its_fault(self) -> None:
        self.write_graph("{{{ not json at all\n")
        findings = [
            item
            for item in self.service.lint()["findings"]
            if item["code"] == "graph.stale"
        ]
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["severity"], "warning")
        # Not "built from a different set of wiki pages": that sentence sends a
        # reader looking for a page that changed, and no page did.
        self.assertNotIn("different set", findings[0]["message"])
        self.assertIn("answers nothing", findings[0]["message"])
        self.assertIn("run bk graph", findings[0]["message"])

    def test_regenerating_clears_it(self) -> None:
        self.write_graph("{{{ not json at all\n")
        self.write_home("# my own notes\n")
        self.service.graph()
        self.service.views()
        self.assertEqual(self.graph_report()["state"], "fresh")
        self.assertEqual(self.views_report()["state"], "fresh")
        self.assertEqual(self.projection_codes(), set())

    def test_an_absent_artefact_is_still_missing_not_malformed(self) -> None:
        # `missing` is healthy: an on-demand artefact nobody generated is not a
        # fault. Only a file that exists and cannot be used is malformed.
        (self.root / GRAPH_PROJECTION).unlink()
        report = self.graph_report()
        self.assertEqual(report["state"], "missing")
        self.assertFalse(report["stale"])

    def test_a_directory_where_the_anchor_belongs_reads_as_missing(self) -> None:
        (self.root / GRAPH_PROJECTION).unlink()
        (self.root / GRAPH_PROJECTION).mkdir()
        self.assertEqual(self.graph_report()["state"], "missing")

    def test_status_answers_rather_than_raising_on_every_fault(self) -> None:
        # `bk status` is what you run when things are already broken, so no
        # input may turn the diagnostic itself into the failure.
        for graph, home in (
            ("{{{ not json at all\n", "# notes\n"),
            ("[]\n", ""),
            ("\x00\x01\x02", "\x00"),
            ("", "\n\n"),
        ):
            with self.subTest(graph=graph[:8]):
                self.write_graph(graph)
                self.write_home(home)
                states = {
                    name: report["state"]
                    for name, report in self.service.status()["projections"].items()
                }
                self.assertEqual(states[GRAPH_PROJECTION], "malformed")
                self.assertEqual(states[VIEWS_PROJECTION], "malformed")


class UnstampedProjectionTest(ProjectionFixture):
    """An artefact generated before brainskit stamped what it writes.

    Every vault upgraded into this release holds one: the recorded fingerprint
    is there, the `artefact_hash` is not. It must not read `fresh`, which is the
    answer a gutted `views/home.md` got, and it must not read `malformed`,
    which would call a legitimately generated view broken on every upgraded
    vault -- including one whose marker still says `brainkit`.
    """

    def setUp(self) -> None:
        super().setUp()
        self.source = self.capture(text="Evidencia de antes.", title="antes")
        self.apply_page("pagina-de-antes", self.source)
        self.service.graph()
        self.service.views()
        self.unstamp()

    def unstamp(self) -> None:
        def mutate(state: dict) -> dict:
            for entry in state["projections"].values():
                entry.pop("artefact_hash", None)
                entry.pop("inputs", None)
            return state

        self.vault.mutate_state("freshness", mutate)

    def test_an_unstamped_artefact_with_current_inputs_is_unverified(self) -> None:
        for artifact in (GRAPH_PROJECTION, VIEWS_PROJECTION):
            with self.subTest(artifact=artifact):
                report = self.projections()[artifact]
                self.assertEqual(report["state"], "unverified")
                self.assertTrue(report["stale"])

    def test_a_pre_rename_view_is_unverified_not_malformed(self) -> None:
        home = self.root / "views" / "home.md"
        home.write_text(
            home.read_text(encoding="utf-8").replace("brainskit", "brainkit", 1),
            encoding="utf-8",
        )
        self.assertEqual(self.projections()[VIEWS_PROJECTION]["state"], "unverified")

    def test_lint_names_the_remedy_without_calling_it_broken(self) -> None:
        messages = [
            item["message"]
            for item in self.service.lint()["findings"]
            if item["code"] == "views.stale"
        ]
        self.assertEqual(len(messages), 1)
        self.assertIn("cannot be verified", messages[0])
        self.assertIn("run bk views", messages[0])
        self.assertNotIn("answers nothing", messages[0])

    def test_an_unstamped_graph_is_still_checked_for_structure(self) -> None:
        (self.root / GRAPH_PROJECTION).write_text("{{{ nope\n", encoding="utf-8")
        self.assertEqual(self.projections()[GRAPH_PROJECTION]["state"], "malformed")

    def test_unstamped_and_behind_its_inputs_is_stale(self) -> None:
        second = self.capture(text="Evidencia nova e diferente.", title="nova")
        self.apply_page("pagina-nova", second)
        self.assertEqual(self.projections()[VIEWS_PROJECTION]["state"], "stale")

    def test_regenerating_stamps_it(self) -> None:
        self.service.graph()
        self.service.views()
        self.assertEqual(self.projection_codes(), set())
        self.assertEqual(self.projections()[VIEWS_PROJECTION]["state"], "fresh")


class StaleReasonTest(ProjectionFixture):
    """A stale projection says which input moved, not always "wiki pages".

    T7 (#29): after a raw file was moved and `bk reconcile` re-linked it, lint
    said the graph "was built from a different set of wiki pages". No page had
    changed; the fingerprint covers the raw registry too, and the sentence
    named only half of it.
    """

    def setUp(self) -> None:
        super().setUp()
        self.source = self.capture(text="Evidencia que se move.", title="movel")
        self.service.graph()
        self.service.views()

    def messages(self) -> dict[str, str]:
        return {
            item["code"]: item["message"]
            for item in self.service.lint()["findings"]
            if item["code"] in PROJECTION_CODES
        }

    def test_a_raw_file_moved_and_reconciled_is_named_as_a_move(self) -> None:
        record = self.vault.registry()[self.source]
        moved = self.root / "raw" / "20-research" / Path(record.path).name
        (self.root / record.path).rename(moved)
        self.assertEqual(self.service.reconcile()["moved"], 1)
        messages = self.messages()
        self.assertEqual(set(messages), PROJECTION_CODES)
        for message in messages.values():
            with self.subTest(message=message):
                self.assertIn("a raw source moved", message)
                self.assertNotIn("wiki pages", message)

    def test_a_new_page_is_named_as_wiki_pages(self) -> None:
        self.apply_page("pagina-nova", self.source)
        for message in self.messages().values():
            with self.subTest(message=message):
                self.assertIn("wiki pages changed", message)
                self.assertNotIn("raw source", message)

    def test_a_capture_is_named_as_raw_sources(self) -> None:
        self.capture(text="Outra evidencia sem relacao.", title="outra")
        for message in self.messages().values():
            with self.subTest(message=message):
                self.assertIn("raw sources were captured or forgotten", message)

    def test_a_record_from_before_the_breakdown_says_it_cannot_tell(self) -> None:
        def mutate(state: dict) -> dict:
            for entry in state["projections"].values():
                entry.pop("inputs", None)
            return state

        self.vault.mutate_state("freshness", mutate)
        self.apply_page("pagina-nova", self.source)
        for message in self.messages().values():
            with self.subTest(message=message):
                self.assertIn("different wiki pages or raw sources", message)


class StatusBlockTest(ProjectionFixture):
    """`bk status` is where the four states are told apart."""

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
        projections = self.projections()
        self.assertEqual(
            set(projections), {GRAPH_PROJECTION, VIEWS_PROJECTION, CODE_PROJECTION}
        )
        for artifact, report in projections.items():
            with self.subTest(artifact=artifact):
                self.assertIn("generated_at", report)
                self.assertIn("stale", report)
                self.assertIsInstance(report["stale"], bool)
                self.assertIn(
                    report["state"],
                    {"missing", "malformed", "stale", "unverified", "fresh"},
                )

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

    def test_the_four_states_are_reported_distinctly(self) -> None:
        self.assertEqual(self.projections()[GRAPH_PROJECTION]["state"], "missing")
        self.assertEqual(self.projections()[VIEWS_PROJECTION]["state"], "stale")
        self.service.graph()
        self.service.views()
        self.assertEqual(self.projections()[GRAPH_PROJECTION]["state"], "fresh")
        self.assertEqual(self.projections()[VIEWS_PROJECTION]["state"], "fresh")
        (self.root / GRAPH_PROJECTION).write_text("nope\n", encoding="utf-8")
        self.assertEqual(self.projections()[GRAPH_PROJECTION]["state"], "malformed")

    def test_the_page_freshness_summary_is_untouched(self) -> None:
        summary = self.service.status()["freshness"]
        self.assertEqual(set(summary), {"fresh", "review", "stale", "unknown"})
        self.assertEqual(summary["fresh"], 1)

    def test_the_projections_survive_a_round_trip_through_json(self) -> None:
        self.service.graph()
        restored = json.loads(json.dumps(self.service.status()))["projections"]
        self.assertEqual(restored, self.projections())


# The seeded-page classes below are about `wiki.outside_apply`, not projections,
# so their natural home is `tests/test_enforcement_status.py`. They live here
# because that file was being edited concurrently when these were written and
# splitting a fix across two agents' files loses more than the misfiling costs.
# Move them when that settles; they depend on nothing in this module but the
# fixture's vault-and-service setup.
def impostor_page(slug: str = "impostor", title: str = "Impostor") -> str:
    """The exact shape `bk init` seeds, for a page init never wrote."""
    return render_seed_page(slug, title, "2026-01-01T00:00:00+00:00")


class SeededSystemPageTest(ProjectionFixture):
    """The seeded pages are hash-checked like every other page.

    `wiki.outside_apply` is the finding that says a page reached `wiki/` without
    passing the apply gate. The two pages `bk init` seeds used to have no ledger
    record, so they were exempted by path and checked for a *shape* -- a single
    `# ` heading -- which any other single heading passed (TC3, #34). Before
    that the exemption was `metadata["type"] == "system"`, read from the file
    being checked. Init now records what it wrote, and lint compares against
    it; neither the path nor the frontmatter buys anything.
    """

    def outside_apply(self) -> dict[str, str]:
        """Every `wiki.outside_apply` finding, keyed by the path it names."""
        return {
            str(item["path"]): str(item["message"])
            for item in self.service.lint()["findings"]
            if item["code"] == "wiki.outside_apply"
        }

    def write_wiki(self, relative: str, text: str) -> None:
        """Put bytes under `wiki/` the way a bypass does -- around the gate."""
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def test_a_pristine_vault_reports_nothing(self) -> None:
        # The control, and the one that must survive every neutralisation
        # below: `bk init` writes these two pages itself, so a check that
        # reports findings against brainskit's own output is worse than the
        # defect it replaced.
        self.assertEqual(self.outside_apply(), {})

    def test_a_seed_whose_heading_was_replaced_is_reported(self) -> None:
        # TC3 itself. The body is still one heading -- the shape the old
        # check accepted -- and only the words changed.
        for relative in SEED_PAGES:
            with self.subTest(path=relative):
                original = (self.root / relative).read_text(encoding="utf-8")
                _, body = parse_frontmatter(original)
                self.write_wiki(relative, original.replace(body, "# Outra coisa\n"))
                self.assertIn("changed outside", self.outside_apply()[relative])
                self.write_wiki(relative, original)

    def test_the_seeded_bytes_are_recorded_before_init_returns(self) -> None:
        # The fixture does what `bk init` does after `initialize`: reindex and
        # generate the views, which runs a lint, which records the seeds.
        seeded = self.freshness()["seeded"]
        self.assertEqual(set(seeded), set(SEED_PAGES))
        for relative in SEED_PAGES:
            with self.subTest(path=relative):
                self.assertEqual(seeded[relative]["hash"], self.vault.content_hash(relative))

    def test_a_seed_record_is_not_an_applied_entry(self) -> None:
        # `content_hash` means the apply gate wrote the page. A seed carries no
        # freshness: it must not age, count in the summary, or enter the
        # projection fingerprint that `views` was just stamped with.
        pages = self.freshness().get("pages", {})
        for relative in SEED_PAGES:
            with self.subTest(path=relative):
                self.assertNotIn(relative, pages)
        self.assertEqual(sum(self.service.status()["freshness"].values()), 0)
        self.assertEqual(self.projections()[VIEWS_PROJECTION]["state"], "fresh")

    def test_a_page_declaring_itself_system_is_still_reported(self) -> None:
        # These are the exact bytes `bk init` writes for a seeded page --
        # `type: "system"` frontmatter and a lone heading -- at a path init
        # never writes. Under the frontmatter rule this page was silent.
        self.write_wiki("wiki/concepts/impostor.md", impostor_page())
        self.assertIn("wiki/concepts/impostor.md", self.outside_apply())

    def test_the_frontmatter_claim_buys_nothing_at_any_path(self) -> None:
        for relative in (
            "wiki/impostor.md",
            "wiki/concepts/impostor.md",
            "wiki/entities/deep/impostor.md",
        ):
            with self.subTest(path=relative):
                self.write_wiki(relative, impostor_page())
                self.assertIn(relative, self.outside_apply())
                (self.root / relative).unlink()

    def test_a_page_named_like_a_seed_elsewhere_is_still_reported(self) -> None:
        # Seeds are whole paths, not basenames, and the seed template at
        # another path is not adopted.
        for relative in ("wiki/concepts/index.md", "wiki/concepts/log.md"):
            with self.subTest(path=relative):
                self.write_wiki(relative, impostor_page("index", "Brainskit index"))
                self.assertIn("not tracked", self.outside_apply()[relative])
                (self.root / relative).unlink()

    def test_appending_to_a_seeded_page_is_reported(self) -> None:
        # A fabricated claim appended to `wiki/index.md` must be found: the
        # gate's header comment names this backstop as the reason it may fail
        # open, so it has to hold for these two pages too.
        for relative in SEED_PAGES:
            with self.subTest(path=relative):
                original = (self.root / relative).read_text(encoding="utf-8")
                self.write_wiki(relative, original + "\nUma alegacao inventada.\n")
                self.assertIn(relative, self.outside_apply())
                self.write_wiki(relative, original)

    def test_a_modified_seed_is_reported_as_changed_not_as_untracked(self) -> None:
        # One says a page appeared from nowhere, the other says a known page
        # was edited. A reader acts differently on each.
        relative = "wiki/index.md"
        original = (self.root / relative).read_text(encoding="utf-8")
        self.write_wiki(relative, original + "\nUma alegacao inventada.\n")
        self.assertIn("changed outside", self.outside_apply()[relative])

    def test_an_unseeded_page_is_reported_as_untracked(self) -> None:
        self.write_wiki("wiki/concepts/impostor.md", impostor_page())
        self.assertIn(
            "not tracked", self.outside_apply()["wiki/concepts/impostor.md"]
        )

    def test_replacing_a_seed_body_wholesale_is_reported(self) -> None:
        original = (self.root / "wiki" / "index.md").read_text(encoding="utf-8")
        _, body = parse_frontmatter(original)
        self.write_wiki(
            "wiki/index.md", original.replace(body, "# Outra coisa\n\nCorpo.\n")
        )
        self.assertIn("wiki/index.md", self.outside_apply())

    def test_restoring_the_seed_clears_the_finding(self) -> None:
        relative = "wiki/log.md"
        original = (self.root / relative).read_text(encoding="utf-8")
        self.write_wiki(relative, original + "\nAlterado.\n")
        self.assertIn(relative, self.outside_apply())
        self.write_wiki(relative, original)
        self.assertEqual(self.outside_apply(), {})

    def test_a_seed_that_gains_an_applied_entry_is_checked_against_it(self) -> None:
        # Once the gate writes a seeded path, what it wrote is the newer claim.
        recorded = self.vault.wiki_version("wiki/index.md")

        def track(state: dict[str, Any]) -> dict[str, Any]:
            state.setdefault("pages", {})["wiki/index.md"] = {
                "content_hash": recorded,
                "status": "fresh",
            }
            return state

        self.vault.mutate_state("freshness", track)
        self.assertEqual(self.outside_apply(), {})
        original = (self.root / "wiki" / "index.md").read_text(encoding="utf-8")
        self.write_wiki("wiki/index.md", original + "\nAlterado.\n")
        self.assertIn("wiki/index.md", self.outside_apply())


class SeededPageUpgradeTest(SeededSystemPageTest):
    """A vault initialised before seeds were recorded.

    It has the two pages and no record of them. The first lint (or `status`,
    which runs one) records a page equal to the template some release seeded --
    the current title or the pre-rename `Brainkit` one -- and reports anything
    else. Inherits every seeded-page test, so each also holds on this path.
    """

    def setUp(self) -> None:
        super().setUp()
        self.forget_seeds()

    def forget_seeds(self) -> None:
        def mutate(state: dict[str, Any]) -> dict[str, Any]:
            state.pop("seeded", None)
            return state

        self.vault.mutate_state("freshness", mutate)

    def test_the_seeded_bytes_are_recorded_before_init_returns(self) -> None:
        self.assertNotIn("seeded", self.freshness())

    def test_the_first_lint_records_an_untouched_seed(self) -> None:
        self.assertEqual(self.outside_apply(), {})
        seeded = self.freshness()["seeded"]
        self.assertEqual(set(seeded), set(SEED_PAGES))
        for relative in SEED_PAGES:
            with self.subTest(path=relative):
                self.assertEqual(
                    seeded[relative]["hash"], self.vault.content_hash(relative)
                )

    def test_status_records_too(self) -> None:
        self.service.status()
        self.assertEqual(set(self.freshness()["seeded"]), set(SEED_PAGES))

    def test_a_seed_from_before_the_rename_is_recorded(self) -> None:
        for relative, (slug, _) in SEED_PAGES.items():
            title = {"index": "Brainkit index", "log": "Brainkit log"}[slug]
            self.write_wiki(
                relative, render_seed_page(slug, title, "2026-08-11T20:14:38.507711+00:00")
            )
        self.assertEqual(self.outside_apply(), {})
        self.assertEqual(set(self.freshness()["seeded"]), set(SEED_PAGES))

    def test_an_edited_seed_is_reported_and_never_recorded(self) -> None:
        relative = "wiki/index.md"
        original = (self.root / relative).read_text(encoding="utf-8")
        _, body = parse_frontmatter(original)
        self.write_wiki(relative, original.replace(body, "# Outra coisa\n"))
        for _ in range(2):
            self.assertIn("changed outside", self.outside_apply()[relative])
        self.assertNotIn(relative, self.freshness().get("seeded", {}))

    def test_a_recorded_seed_is_hash_checked_afterwards(self) -> None:
        self.assertEqual(self.outside_apply(), {})
        relative = "wiki/log.md"
        original = (self.root / relative).read_text(encoding="utf-8")
        self.write_wiki(relative, original + "x")
        self.assertIn(relative, self.outside_apply())


class SeedTemplateTest(unittest.TestCase):
    """`is_seed_template` is equality with a rendered seed, not a shape."""

    STAMP = "2026-08-11T20:14:38.507711+00:00"

    def test_every_title_a_release_seeded_is_recognised(self) -> None:
        for path, slug, title in (
            ("wiki/index.md", "index", "Brainskit index"),
            ("wiki/index.md", "index", "Brainkit index"),
            ("wiki/log.md", "log", "Brainskit log"),
            ("wiki/log.md", "log", "Brainkit log"),
        ):
            with self.subTest(title=title):
                self.assertTrue(
                    is_seed_template(path, render_seed_page(slug, title, self.STAMP))
                )

    def test_anything_else_is_not(self) -> None:
        seed = render_seed_page("index", "Brainskit index", self.STAMP)
        for path, text in (
            ("wiki/index.md", seed.replace("# Brainskit index", "# Outra coisa")),
            ("wiki/index.md", seed + "\n"),
            ("wiki/index.md", seed.replace('title: "Brainskit index"', 'title: "X"')),
            ("wiki/index.md", render_seed_page("log", "Brainskit log", self.STAMP)),
            ("wiki/concepts/index.md", seed),
            ("wiki/index.md", ""),
        ):
            with self.subTest(path=path, text=text[-24:]):
                self.assertFalse(is_seed_template(path, text))


class InitLayoutTest(unittest.TestCase):
    """`initialize` scaffolds exactly the layout `docs/getting-started.md` shows.

    It used to create `output/reports`, which nothing writes, and not
    `output/resurface`, which `bk resurface` does.
    """

    DOC = Path(__file__).resolve().parents[1] / "docs" / "getting-started.md"

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        FileVault.initialize(self.root, policy())

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_the_directories_are_the_documented_ones(self) -> None:
        created = {
            path.relative_to(self.root).as_posix()
            for path in self.root.rglob("*")
            if path.is_dir()
        }
        self.assertEqual(
            created,
            {
                "raw", "raw/_inbox", "raw/_assets", "raw/20-research",
                "wiki", "wiki/sources", "wiki/entities", "wiki/concepts",
                "wiki/syntheses",
                "views", "views/map", "views/domains",
                "graph",
                "output", "output/digests", "output/resurface", "output/answers",
                ".brain",
            },
        )

    def test_every_output_directory_is_in_the_documented_layout(self) -> None:
        text = self.DOC.read_text(encoding="utf-8")
        layout = text[text.index("## Vault layout") :]
        layout = layout[: layout.index("```", layout.index("```text") + 1)]
        for path in sorted((self.root / "output").iterdir()):
            with self.subTest(directory=path.name):
                self.assertIn(f"── {path.name}/", layout)


class SeededPageDriftTest(unittest.TestCase):
    """What `bk init` writes and what lint recognises must be the same pages.

    Read off a real `initialize` rather than restated: a seed added to
    `SEED_PAGES` is written from the template lint compares against, so this
    pins that the loop, the template and the record agree.
    """

    def setUp(self) -> None:
        # Every test below loops over the constant; were it empty they would all
        # pass against an init that seeds nothing.
        self.assertEqual(set(SEED_PAGES), {"wiki/index.md", "wiki/log.md"})
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.vault = FileVault.initialize(self.root, policy())

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_init_writes_exactly_the_seeded_paths(self) -> None:
        self.assertEqual(set(self.vault.wiki_pages()), set(SEED_PAGES))

    def test_every_seeded_page_is_the_template(self) -> None:
        for path in sorted(SEED_PAGES):
            with self.subTest(path=path):
                text = (self.root / path).read_text(encoding="utf-8")
                self.assertTrue(is_seed_template(path, text))

    def test_the_seeded_pages_declare_type_system(self) -> None:
        # Pins the premise of the frontmatter fix: the seeds really do carry
        # the claim that used to buy an exemption.
        for path in sorted(SEED_PAGES):
            with self.subTest(path=path):
                metadata, _ = parse_frontmatter(
                    (self.root / path).read_text(encoding="utf-8")
                )
                self.assertEqual(metadata.get("type"), "system")

    def test_initialize_alone_records_nothing(self) -> None:
        # Infrastructure does not reach into the ledger; the record is lint's.
        self.assertNotIn("seeded", self.vault.read_state("freshness"))

    def test_the_first_lint_records_every_seed_as_written(self) -> None:
        service = BrainskitService(self.vault, SqliteFtsIndex(self.vault.index_path))
        self.assertEqual(
            [f for f in service.lint()["findings"] if f["code"] == "wiki.outside_apply"],
            [],
        )
        seeded = self.vault.read_state("freshness")["seeded"]
        for path in sorted(SEED_PAGES):
            with self.subTest(path=path):
                self.assertEqual(seeded[path]["hash"], self.vault.content_hash(path))


if __name__ == "__main__":
    unittest.main()
