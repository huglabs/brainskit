from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from brainskit.application.services import BrainskitService
from brainskit.domain.model import (
    DEFAULT_IGNORE_PATTERNS,
    PolicyError,
    ValidationError,
)
from brainskit.infrastructure.graph import MarkdownGraph
from brainskit.infrastructure.index import SqliteFtsIndex
from brainskit.infrastructure.integrations import NativeIntegrations
from brainskit.infrastructure.vault import FileVault

FILE_EXPORT_TARGETS = ("json", "graphml", "cypher", "kuzu", "llms-txt")


def policy() -> dict:
    return {
        "version": 3,
        "wiki_language": "Portuguese (Brazil)",
        "inbox_policy": {"privacy": "local-only", "filing": "approve-each"},
        "branches": {
            "10-work": {"privacy": "never-ingest", "filing": "approve-each"},
            "20-research": {
                "privacy": "local-only",
                "filing": "auto+digest-review",
            },
            "30-public": {"privacy": "cloud", "filing": "auto+digest-review"},
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
        "taxonomy_seed": ["work", "research", "public"],
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


class ServiceFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.vault = FileVault.initialize(self.root, policy())
        self.service = BrainskitService(
            self.vault,
            SqliteFtsIndex(self.vault.index_path),
            graph=MarkdownGraph(),
            integrations=NativeIntegrations(self.vault),
        )
        self.service.reindex()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def capture_into(self, branch: str, *, text: str, title: str) -> str:
        captured = self.service.capture(None, text=text, title=title)
        content_hash = captured["source"]["content_hash"]
        self.service.file(content_hash, branch)
        return content_hash

    def upsert_page(self, slug: str, title: str, body: str, source_hash: str) -> str:
        self.service.apply(
            {
                "operations": [
                    {
                        "action": "upsert",
                        "kind": "concept",
                        "slug": slug,
                        "title": title,
                        "aliases": [],
                        "source_hashes": [source_hash],
                        "body": f"{body}[^source:{source_hash}]",
                        "links": [],
                    }
                ]
            }
        )
        return f"wiki/concepts/{slug}.md"

    def read_output(self, target: str) -> str:
        suffix = {
            "json": "json",
            "graphml": "graphml",
            "cypher": "cypher",
            "kuzu": "cypher",
            "llms-txt": "txt",
        }[target]
        return (self.root / f"output/export-{target}.{suffix}").read_text(
            encoding="utf-8"
        )


class ExportPrivacyBoundaryTest(ServiceFixture):
    """Defect 1: file exports had no consumer gate at all."""

    def setUp(self) -> None:
        super().setUp()
        self.secret_hash = self.capture_into(
            "10-work", text="Plano confidencial da aquisicao.", title="segredo"
        )
        self.public_hash = self.capture_into(
            "30-public", text="Nota publica sobre o produto.", title="publico"
        )

    def test_file_exports_default_to_local_and_drop_never_ingest(self) -> None:
        for target in FILE_EXPORT_TARGETS:
            with self.subTest(target=target):
                result = self.service.export(target)
                self.assertEqual(result["consumer"], "local")
                payload = self.read_output(target)
                self.assertNotIn("segredo", payload)
                self.assertNotIn(self.secret_hash, payload)
                self.assertNotIn("10-work", payload)
                self.assertIn("publico", payload)

    def test_file_exports_widen_only_when_the_caller_asks(self) -> None:
        for target in FILE_EXPORT_TARGETS:
            with self.subTest(target=target):
                result = self.service.export(target, consumer="human")
                self.assertEqual(result["consumer"], "human")
                self.assertIn("segredo", self.read_output(target))

    def test_cloud_export_drops_local_only_evidence_as_well(self) -> None:
        research_hash = self.capture_into(
            "20-research", text="Rascunho de pesquisa interna.", title="pesquisa"
        )
        self.service.export("json", consumer="cloud")
        payload = self.read_output("json")
        self.assertNotIn(research_hash, payload)
        self.assertNotIn(self.secret_hash, payload)
        self.assertIn(self.public_hash, payload)

    def test_export_rejects_an_unknown_consumer(self) -> None:
        with self.assertRaises(ValidationError):
            self.service.export("json", consumer="everyone")

    def test_export_still_rejects_an_unsupported_target(self) -> None:
        with self.assertRaises(ValidationError):
            self.service.export("parquet")

    def test_integration_targets_refuse_an_explicit_consumer(self) -> None:
        for consumer in ("human", "cloud"):
            with self.subTest(consumer=consumer):
                with self.assertRaises(ValidationError) as refused:
                    self.service.export("obsidian", consumer=consumer)
                self.assertEqual(refused.exception.details["target"], "obsidian")

    def test_integration_export_reports_the_policy_consumer(self) -> None:
        with tempfile.TemporaryDirectory() as destination:
            self.service.integration_configure(
                "obsidian",
                enabled=True,
                managed=False,
                options={"path": destination, "consumer": "cloud"},
            )
            result = self.service.export("obsidian")
            self.assertEqual(result["consumer"], "cloud")


class ObsidianConsumerTest(ServiceFixture):
    """Defect 2: the stored Obsidian consumer was never read."""

    def setUp(self) -> None:
        super().setUp()
        self.secret_hash = self.capture_into(
            "10-work", text="Plano confidencial da aquisicao.", title="segredo"
        )
        self.public_hash = self.capture_into(
            "30-public", text="Nota publica sobre o produto.", title="publico"
        )

    def sync_to(self, destination: str, **options: object) -> Path:
        self.service.integration_configure(
            "obsidian",
            enabled=True,
            managed=False,
            options={"path": destination, "subdirectory": "brainskit", **options},
        )
        self.service.integration_sync("obsidian")
        return Path(destination) / "brainskit"

    def test_unset_consumer_defaults_to_local_instead_of_human(self) -> None:
        self.assertEqual(self.service.projections._integration_consumer("obsidian"), "local")
        with tempfile.TemporaryDirectory() as destination:
            exported = self.sync_to(destination)
            graph = (exported / "graph/graph.json").read_text(encoding="utf-8")
            work_view = (exported / "views/map/10-work.md").read_text(
                encoding="utf-8"
            )
            self.assertNotIn(self.secret_hash, graph)
            self.assertNotIn("segredo", graph)
            self.assertNotIn("segredo", work_view)
            self.assertNotIn("raw/10-work", work_view)
            self.assertIn("publico", graph)

    def test_a_configured_consumer_is_honoured(self) -> None:
        with tempfile.TemporaryDirectory() as destination:
            exported = self.sync_to(destination, consumer="human")
            graph = (exported / "graph/graph.json").read_text(encoding="utf-8")
            work_view = (exported / "views/map/10-work.md").read_text(
                encoding="utf-8"
            )
            self.assertIn(self.secret_hash, graph)
            self.assertIn("segredo", work_view)

    def test_narrowing_the_consumer_overwrites_a_wider_previous_sync(self) -> None:
        with tempfile.TemporaryDirectory() as destination:
            self.sync_to(destination, consumer="human")
            exported = self.sync_to(destination, consumer="local")
            work_view = (exported / "views/map/10-work.md").read_text(
                encoding="utf-8"
            )
            self.assertNotIn("segredo", work_view)
            leaked = [
                path
                for path in exported.rglob("*")
                if path.is_file()
                and "segredo" in path.read_text(encoding="utf-8", errors="replace")
            ]
            self.assertEqual(leaked, [])

    def test_an_invalid_consumer_is_rejected_before_it_is_stored(self) -> None:
        with tempfile.TemporaryDirectory() as destination:
            with self.assertRaises(ValidationError):
                self.service.integration_configure(
                    "obsidian",
                    enabled=True,
                    managed=False,
                    options={"path": destination, "consumer": "everyone"},
                )
            stored = self.vault.config().integrations["obsidian"]
            self.assertNotIn("consumer", stored.options)

    def test_an_invalid_stored_consumer_is_rejected(self) -> None:
        # `integration_configure` now refuses the value, so reaching the sync
        # guard requires a policy written straight to disk. It stays covered
        # because a hand-edited config must not widen the boundary either.
        with tempfile.TemporaryDirectory() as destination:
            self.service.integration_configure(
                "obsidian",
                enabled=True,
                managed=False,
                options={"path": destination},
            )
            config = json.loads(
                (self.root / ".brain" / "config.json").read_text(encoding="utf-8")
            )
            config["integrations"]["obsidian"]["options"]["consumer"] = "everyone"
            (self.root / ".brain" / "config.json").write_text(
                json.dumps(config, indent=2), encoding="utf-8"
            )
            with self.assertRaises(ValidationError):
                self.service.integration_sync("obsidian")

    def test_database_integrations_still_require_an_explicit_consumer(self) -> None:
        with self.assertRaises(ValidationError):
            self.service.projections._integration_consumer("neo4j")


class GeneratedViewsTest(ServiceFixture):
    """Defect 2, second half: views listed every raw source unfiltered."""

    def setUp(self) -> None:
        super().setUp()
        self.secret_hash = self.capture_into(
            "10-work", text="Plano confidencial da aquisicao.", title="segredo"
        )
        self.public_hash = self.capture_into(
            "30-public", text="Nota publica sobre o produto.", title="publico"
        )

    def read_view(self, name: str) -> str:
        return (self.root / f"views/{name}").read_text(encoding="utf-8")

    def test_a_local_operator_still_sees_their_own_never_ingest_branch(self) -> None:
        written = self.service.views()["written"]
        self.assertIn("views/map/10-work.md", written)
        self.assertIn("segredo", self.read_view("map/10-work.md"))
        self.assertIn("- Sources: **2**", self.read_view("home.md"))

    def test_a_narrower_consumer_filters_the_rows_and_the_counts(self) -> None:
        self.service.views(consumer="local")
        work_view = self.read_view("map/10-work.md")
        self.assertNotIn("segredo", work_view)
        self.assertNotIn("raw/10-work", work_view)
        self.assertIn("publico", self.read_view("map/30-public.md"))
        home = self.read_view("home.md")
        self.assertIn("- Sources: **1**", home)
        self.assertIn("[[map/10-work|10-work]] — 0 sources", home)

    def test_pages_built_on_hidden_evidence_are_dropped_from_views(self) -> None:
        page = self.upsert_page(
            "plano-restrito",
            "Plano restrito",
            "Detalhe do plano.",
            self.secret_hash,
        )
        self.service.views()
        self.assertIn("[[plano-restrito]]", self.read_view("map/10-work.md"))
        self.service.views(consumer="local")
        self.assertNotIn("plano-restrito", self.read_view("map/10-work.md"))
        self.assertNotIn("plano-restrito", self.read_view("map/30-public.md"))
        self.assertIn("- Wiki pages: **2**", self.read_view("home.md"))
        self.assertIn(page, self.vault.wiki_pages())

    def test_views_rejects_an_unknown_consumer(self) -> None:
        with self.assertRaises(ValidationError):
            self.service.views(consumer="everyone")


class RelatedCaptureFreshnessTest(ServiceFixture):
    """Defect 3: relatedness was computed from the file name, not the content."""

    def setUp(self) -> None:
        super().setUp()
        memoria_hash = self.capture_into(
            "20-research",
            text=(
                "Memoria compilada organiza conhecimento antes da consulta, "
                "evitando trabalho de recuperacao no momento da pergunta."
            ),
            title="Memoria compilada",
        )
        recuperacao_hash = self.capture_into(
            "20-research",
            text=(
                "Recuperacao incremental atualiza o indice a cada captura em "
                "vez de reconstruir o indice inteiro, mantendo a busca barata."
            ),
            title="Recuperacao incremental",
        )
        self.memoria = self.upsert_page(
            "memoria-compilada",
            "Memoria compilada",
            "Memoria compilada organiza conhecimento antes da consulta.",
            memoria_hash,
        )
        self.recuperacao = self.upsert_page(
            "recuperacao-incremental",
            "Recuperacao incremental",
            "Recuperacao incremental atualiza o indice a cada captura.",
            recuperacao_hash,
        )
        self.clear_review()

    def clear_review(self) -> None:
        def mutate(state: dict) -> dict:
            for entry in state.get("pages", {}).values():
                if entry.get("status") == "review":
                    entry["status"] = "fresh"
                    entry["review_reason"] = None
            return state

        self.vault.mutate_state("freshness", mutate)

    def capture_file(self, name: str, body: str) -> str:
        source = self.root.parent / name
        source.write_text(body, encoding="utf-8")
        self.addCleanup(source.unlink)
        return self.service.capture(str(source))["source"]["content_hash"]

    def marked_for_review(self) -> dict[str, str]:
        pages = self.vault.read_state("freshness").get("pages", {})
        return {
            path: str(entry.get("review_reason", ""))
            for path, entry in pages.items()
            if entry.get("status") == "review"
        }

    def test_on_topic_content_marks_pages_despite_an_opaque_file_name(self) -> None:
        content_hash = self.capture_file(
            "zzz-9f2b.md",
            "A memoria compilada organiza o conhecimento antes da consulta e a "
            "recuperacao incremental atualiza o indice a cada captura. Juntas, "
            "removem o trabalho de recuperacao do momento da pergunta e mantem "
            "o indice barato para a busca.\n",
        )
        marked = self.marked_for_review()
        self.assertEqual(set(marked), {self.memoria, self.recuperacao})
        self.assertEqual(
            set(marked.values()), {f"related source:{content_hash}"}
        )

    def test_off_topic_content_is_not_rescued_by_a_suggestive_file_name(self) -> None:
        self.capture_file(
            "memoria-compilada.md",
            "Bolo de cenoura: rale tres cenouras medias, bata com quatro ovos, "
            "duas xicaras de acucar e meia xicara de oleo. Acrescente farinha "
            "de trigo e fermento e leve ao forno por quarenta minutos.\n",
        )
        self.assertEqual(self.marked_for_review(), {})

    def test_an_unrelated_capture_marks_nothing(self) -> None:
        self.capture_file(
            "agenda-viagem.md",
            "Roteiro da viagem: reservar hotel perto da estacao, comprar "
            "bilhete de trem para sexta-feira e confirmar o passeio de barco.\n",
        )
        self.assertEqual(self.marked_for_review(), {})

    def test_a_partial_overlap_below_the_floor_is_not_related(self) -> None:
        # One shared term ("indice") sits under the two-term relevance floor.
        self.capture_file(
            "indice-remissivo.md",
            "O indice remissivo do livro de receitas lista bolos, tortas e "
            "sobremesas geladas por ordem alfabetica.\n",
        )
        self.assertEqual(self.marked_for_review(), {})

    def test_only_wiki_pages_are_marked(self) -> None:
        self.capture_file(
            "zzz-b100.md",
            "Memoria compilada e recuperacao incremental organizam o "
            "conhecimento e o indice antes da consulta.\n",
        )
        for path in self.marked_for_review():
            self.assertTrue(path.startswith("wiki/"))

    def test_the_marked_page_count_is_capped(self) -> None:
        flavours = [
            "cache quente servido pelo disco local",
            "fila assincrona drenada por um worker",
            "particao mensal podada por retencao",
            "replica somente leitura atras do proxy",
            "compactacao noturna guiada por metricas",
            "amostragem estatistica sobre janelas curtas",
            "checkpoint incremental gravado em blocos",
            "reconciliacao manual disparada pelo operador",
        ]
        for index, flavour in enumerate(flavours):
            source_hash = self.capture_into(
                "20-research",
                text=(
                    f"Variante {index}: memoria compilada, recuperacao "
                    f"incremental, indice invertido, consulta e captura com "
                    f"{flavour}."
                ),
                title=f"Variante {index}",
            )
            self.upsert_page(
                f"variante-{index}",
                f"Variante {index}",
                "Memoria compilada, recuperacao incremental, indice invertido, "
                f"consulta e captura usando {flavour}.",
                source_hash,
            )
        self.clear_review()
        self.capture_file(
            "zzz-c200.md",
            "Memoria compilada, recuperacao incremental, indice invertido, "
            "consulta e captura formam o mesmo assunto destas paginas.\n",
        )
        self.assertLessEqual(len(self.marked_for_review()), 5)
        self.assertGreaterEqual(len(self.marked_for_review()), 1)


class GraphDataConsumerContractTest(ServiceFixture):
    def test_exported_json_records_the_boundary_that_produced_it(self) -> None:
        self.capture_into(
            "10-work", text="Plano confidencial da aquisicao.", title="segredo"
        )
        self.capture_into(
            "30-public", text="Nota publica sobre o produto.", title="publico"
        )
        self.service.export("json")
        payload = json.loads(self.read_output("json"))
        self.assertEqual(payload["consumer"], "local")
        self.assertGreaterEqual(payload["redacted_nodes"], 1)


class ContextRedactionTest(ServiceFixture):
    """`context` is the cloud payload: it may count redactions, never name them."""

    def setUp(self) -> None:
        super().setUp()
        self.secret = self.capture_into(
            "10-work", text="Salarios confidenciais CANARIO.", title="segredo"
        )
        self.public = self.capture_into(
            "30-public", text="Memoria compilada e proveniencia.", title="memoria"
        )

    def test_a_redacted_lookup_names_nothing(self) -> None:
        bundle = self.service.context(self.secret, consumer="cloud")
        self.assertEqual(bundle["evidence"], [])
        self.assertEqual(bundle["redacted"], 1)
        blob = json.dumps(bundle, ensure_ascii=False)
        for leak in ("segredo", "10-work", "never-ingest", "raw/", "CANARIO"):
            with self.subTest(leak=leak):
                self.assertNotIn(leak, blob)

    def test_a_redacted_search_is_reported_not_silent(self) -> None:
        found = self.service.search("memoria OR salarios OR CANARIO", consumer="cloud")
        bundle = self.service.context(
            "memoria OR salarios OR CANARIO", consumer="cloud"
        )
        self.assertEqual(bundle["redacted"], found["redacted"])
        self.assertGreaterEqual(bundle["redacted"], 1)

    def test_nothing_redacted_reports_zero(self) -> None:
        bundle = self.service.context("memoria", consumer="human")
        self.assertEqual(bundle["redacted"], 0)

    def test_redacted_is_always_an_integer(self) -> None:
        for query in (self.secret, self.public, "memoria", "sem-resultado-nenhum"):
            for consumer in ("cloud", "local", "human"):
                with self.subTest(query=query[:12], consumer=consumer):
                    bundle = self.service.context(query, consumer=consumer)
                    self.assertIsInstance(bundle["redacted"], int)

    def test_allowed_evidence_still_reaches_the_bundle(self) -> None:
        bundle = self.service.context(self.public, consumer="cloud")
        self.assertEqual(len(bundle["evidence"]), 1)
        self.assertEqual(bundle["redacted"], 0)
        self.assertIn("Memoria compilada", bundle["evidence"][0]["content"])


class OrphanedFreshnessTest(ServiceFixture):
    """Freshness is keyed by path, so a deleted page leaves a dead entry."""

    def setUp(self) -> None:
        super().setUp()
        source = self.capture_into(
            "20-research", text="Evidencia sobre memoria.", title="memoria"
        )
        self.kept = self.upsert_page("mantida", "Mantida", "Corpo.", source)
        self.removed = self.upsert_page("removida", "Removida", "Corpo.", source)
        (self.root / self.removed).unlink()

    def freshness_pages(self) -> dict:
        return dict(self.vault.read_state("freshness").get("pages", {}))

    def test_status_does_not_count_a_page_that_no_longer_exists(self) -> None:
        summary = self.service.status()["freshness"]
        self.assertEqual(sum(summary.values()), 1)
        self.assertEqual(summary["fresh"], 1)

    def test_lint_reports_the_orphan(self) -> None:
        findings = self.service.lint()["findings"]
        orphans = [item for item in findings if item["code"] == "freshness.orphaned"]
        self.assertEqual([item["path"] for item in orphans], [self.removed])
        self.assertEqual(orphans[0]["severity"], "warning")

    def test_an_orphan_is_a_warning_not_an_error(self) -> None:
        self.assertTrue(self.service.lint()["ok"])

    def test_reconcile_drops_the_orphan_and_keeps_live_pages(self) -> None:
        result = self.service.reconcile()
        self.assertEqual(result["freshness_orphans"], [self.removed])
        self.assertEqual(sorted(self.freshness_pages()), [self.kept])

    def test_reconcile_is_idempotent(self) -> None:
        self.service.reconcile()
        self.assertEqual(self.service.reconcile()["freshness_orphans"], [])
        self.assertEqual(sorted(self.freshness_pages()), [self.kept])

    def test_lint_is_clean_after_reconcile(self) -> None:
        self.service.reconcile()
        codes = [item["code"] for item in self.service.lint()["findings"]]
        self.assertNotIn("freshness.orphaned", codes)


class LintCostTest(ServiceFixture):
    """A lint must not re-pay per page for something the vault has once.

    `bk status` runs a full lint on every invocation, so anything that scales
    with the page count here scales with every status call too.
    """

    def pages(self, count: int) -> None:
        source = self.capture_into("20-research", text="evidence", title="evidence")
        for index in range(count):
            self.upsert_page(f"page-{index}", f"Page {index}", "Body ", source)

    def test_the_vault_schema_is_read_once_per_lint(self) -> None:
        self.pages(4)
        reads = 0
        original = self.vault.schema

        def counting_schema() -> dict:
            nonlocal reads
            reads += 1
            return original()

        self.vault.schema = counting_schema  # type: ignore[method-assign]
        try:
            report = self.service.lint()
        finally:
            self.vault.schema = original  # type: ignore[method-assign]
        self.assertTrue(report["ok"], report["findings"])
        self.assertEqual(reads, 1)

    def test_the_page_schema_is_still_enforced_on_every_page(self) -> None:
        # Hoisting the read must not have hoisted the validation with it.
        self.pages(3)
        pages = sorted((self.root / "wiki" / "concepts").glob("page-*.md"))
        self.assertEqual(len(pages), 3)
        for page in pages:
            body = page.read_text(encoding="utf-8")
            page.write_text(
                body.replace('type: "concept"', 'type: "bogus"'), encoding="utf-8"
            )
        findings = [
            finding
            for finding in self.service.lint()["findings"]
            if finding["code"].startswith("schema.")
        ]
        self.assertEqual(
            sorted({finding["path"] for finding in findings}),
            [f"wiki/concepts/page-{index}.md" for index in range(3)],
        )


class WatchIgnoreTest(unittest.TestCase):
    """A watch captures evidence, not the machinery sitting next to it.

    Capture is irreversible by design — a source is identified by the hash of
    its bytes and `raw/` is immutable — so a watch that walks a project folder
    without an ignore list does not merely add noise, it permanently files a
    dependency tree.
    """

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.work = Path(self.temporary.name)
        self.source = self.work / "project"
        for directory in ("node_modules/react", ".git", "docs", "build"):
            (self.source / directory).mkdir(parents=True)
        self.written = {
            "node_modules/react/index.js": "bundled dependency",
            ".git/config": "repository plumbing",
            "build/out.o": "compiled artifact",
            ".DS_Store": "finder metadata",
            "docs/note.md": "durable evidence about retrieval",
            "README.md": "durable evidence about the project",
        }
        for relative, body in self.written.items():
            (self.source / relative).write_text(body, encoding="utf-8")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def service(self, name: str, **overrides: Any) -> BrainskitService:
        raw = policy()
        raw["sources"] = [str(self.source)]
        raw.update(overrides)
        vault = FileVault.initialize(self.work / name, raw)
        return BrainskitService(
            vault, SqliteFtsIndex(vault.index_path), graph=MarkdownGraph()
        )

    def captured(self, service: BrainskitService) -> list[str]:
        return sorted(
            record.original_name for record in service.vault.registry().values()
        )

    def test_the_defaults_keep_dependency_trees_out_of_the_vault(self) -> None:
        service = self.service("vault")
        result = service.watch_once()
        self.assertEqual(result["failures"], [])
        self.assertEqual(self.captured(service), ["README.md", "note.md"])
        self.assertEqual(result["created"], 2)

    def test_what_was_skipped_is_reported(self) -> None:
        result = self.service("vault").watch_once()
        # Four prunes: three directories and one dotfile. The files inside a
        # pruned directory are never visited, so they are not counted --
        # reporting per-file would mean walking what the policy excluded.
        self.assertEqual(result["ignored"], 4)

    def test_an_empty_list_is_honoured_as_ignore_nothing(self) -> None:
        service = self.service("vault-open", ignore=[])
        result = service.watch_once()
        self.assertEqual(result["ignored"], 0)
        self.assertIn("index.js", self.captured(service))
        self.assertIn("config", self.captured(service))

    def test_a_vault_written_before_ignore_existed_inherits_the_defaults(self) -> None:
        # Upgrading the engine must not start capturing what a watch never did.
        raw = policy()
        raw["sources"] = [str(self.source)]
        raw.pop("ignore", None)
        vault = FileVault.initialize(self.work / "legacy", raw)
        self.assertEqual(vault.config().ignore, DEFAULT_IGNORE_PATTERNS)

    def test_operator_patterns_replace_the_defaults(self) -> None:
        service = self.service("vault-custom", ignore=["docs"])
        service.watch_once()
        names = self.captured(service)
        self.assertNotIn("note.md", names)
        self.assertIn("index.js", names)

    def test_the_vault_is_never_a_source_of_itself(self) -> None:
        # A watch pointed at a parent of the vault would otherwise re-capture
        # raw/ into itself on every tick.
        nested = self.work / "project" / "brain"
        raw = policy()
        raw["sources"] = [str(self.source)]
        vault = FileVault.initialize(nested, raw)
        service = BrainskitService(
            vault, SqliteFtsIndex(vault.index_path), graph=MarkdownGraph()
        )
        service.watch_once()
        first = service.watch_once()
        self.assertEqual(first["created"], 0)
        for record in vault.registry().values():
            self.assertNotIn("/brain/", record.original_name)

    def test_a_configured_file_is_captured_directly(self) -> None:
        raw = policy()
        raw["sources"] = [str(self.source / "README.md")]
        vault = FileVault.initialize(self.work / "vault-file", raw)
        service = BrainskitService(
            vault, SqliteFtsIndex(vault.index_path), graph=MarkdownGraph()
        )
        self.assertEqual(service.watch_once()["created"], 1)

    def test_a_missing_source_folder_is_not_fatal(self) -> None:
        raw = policy()
        raw["sources"] = [str(self.work / "gone"), str(self.source)]
        vault = FileVault.initialize(self.work / "vault-missing", raw)
        service = BrainskitService(
            vault, SqliteFtsIndex(vault.index_path), graph=MarkdownGraph()
        )
        self.assertEqual(service.watch_once()["created"], 2)


class ObsidianSyncFiltersFilesTest(ServiceFixture):
    """Obsidian sync filtered the graph object and copied everything else.

    `sync` builds `source_paths` by walking the filesystem --
    `rglob("*.md")` over `wiki/` and `views/`, plus all of `raw/` under
    `include_raw` -- so the consumer never reached the file selection.

    `views/` is safe only by accident: `ProjectionService.integration_sync`
    regenerates it filtered just before the copy. Nothing regenerates `wiki/`
    or `raw/`, so the compiled page leaks under *default* options and raw
    never-ingest bytes leak under `--include-raw`. Sync targets are typically
    iCloud- or Dropbox-backed.

    The four consumer tests that existed asserted only that `"clod"` was
    rejected and `"cloud"` was stored -- all four pass with the filtering
    deleted. These assert on the bytes that land in the target.
    """

    SECRET = "SEGREDO-DA-AQUISICAO"

    def setUp(self) -> None:
        super().setUp()
        self.secret_hash = self.capture_into(
            "10-work", text=f"Plano confidencial. {self.SECRET}.", title="segredo"
        )
        self.public_hash = self.capture_into(
            "30-public", text="Nota publica sobre o produto.", title="publico"
        )
        # The step the pre-existing fixture never took: compile a wiki page out
        # of never-ingest evidence. Without a page, `wiki/` is empty and the
        # unfiltered rglob has nothing to leak.
        self.secret_page = self.upsert_page(
            "board-memo",
            "Board memo",
            f"O plano da aquisicao: {self.SECRET}. ",
            self.secret_hash,
        )
        self.public_page = self.upsert_page(
            "nota-publica", "Nota publica", "Detalhe publico. ", self.public_hash
        )

    def sync_to(self, destination: str, **options: object) -> Path:
        self.service.integration_configure(
            "obsidian",
            enabled=True,
            managed=False,
            options={"path": destination, "subdirectory": "brainskit", **options},
        )
        self.service.integration_sync("obsidian")
        return Path(destination) / "brainskit"

    def leaked_files(self, exported: Path) -> list[str]:
        return sorted(
            str(path.relative_to(exported))
            for path in exported.rglob("*")
            if path.is_file()
            and self.SECRET in path.read_text(encoding="utf-8", errors="replace")
        )

    def test_default_options_do_not_copy_a_never_ingest_page(self) -> None:
        with tempfile.TemporaryDirectory() as destination:
            exported = self.sync_to(destination, consumer="cloud")
            self.assertEqual(self.leaked_files(exported), [])

    def test_include_raw_does_not_copy_never_ingest_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as destination:
            exported = self.sync_to(destination, consumer="cloud", include_raw=True)
            self.assertEqual(self.leaked_files(exported), [])

    def test_local_also_redacts_never_ingest_as_the_docs_promise(self) -> None:
        """`docs/integrations.md` promises `local` 'always redacts never-ingest'."""

        with tempfile.TemporaryDirectory() as destination:
            exported = self.sync_to(destination, consumer="local", include_raw=True)
            self.assertEqual(self.leaked_files(exported), [])

    def test_the_public_page_is_still_synced(self) -> None:
        """Control: filtering must not degrade into copying nothing."""

        with tempfile.TemporaryDirectory() as destination:
            exported = self.sync_to(destination, consumer="cloud")
            self.assertTrue((exported / self.public_page).is_file())

    def test_human_still_receives_everything(self) -> None:
        """Control: the consumer must actually be the thing deciding."""

        with tempfile.TemporaryDirectory() as destination:
            exported = self.sync_to(destination, consumer="human", include_raw=True)
            self.assertNotEqual(self.leaked_files(exported), [])

    def test_narrowing_removes_a_page_a_wider_sync_already_wrote(self) -> None:
        """A previously-synced page must be swept, not left behind."""

        with tempfile.TemporaryDirectory() as destination:
            self.sync_to(destination, consumer="human")
            exported = self.sync_to(destination, consumer="cloud")
            self.assertEqual(self.leaked_files(exported), [])
            self.assertFalse((exported / self.secret_page).exists())


class UnconfiguredBranchRaisesPolicyErrorTest(ServiceFixture):
    """A bare `KeyError` escaped four read paths after the documented healing.

    `_privacy_for_record` subscripted `config.branches[branch]` directly, where
    `infrastructure/llm.py` uses `.get()` + `PolicyError` for the identical
    question. Drop a file into a directory that is not a configured branch, run
    the *documented* `bk reconcile`, and `search`, `browse_sources`,
    `graph_data` and `export` all raised `KeyError('personal')`.

    A bare `KeyError` is not a `BrainskitError`, so it bypasses the JSON error
    envelope and the exit-code machinery entirely: an unhandled traceback on the
    CLI and a 500 in the web viewer. `bk status` stays green throughout, because
    `health.py` has an "unknown" fallback.
    """

    def setUp(self) -> None:
        super().setUp()
        stray = self.root / "raw" / "personal" / "note.md"
        stray.parent.mkdir(parents=True, exist_ok=True)
        stray.write_text("Uma nota pessoal.\n", encoding="utf-8")
        self.service.reconcile()

    def test_reconcile_itself_still_succeeds(self) -> None:
        """Control: the healing command is what puts the vault in this state."""

        self.assertGreaterEqual(self.service.reconcile()["scanned"], 1)

    def test_every_read_path_raises_policy_error_not_key_error(self) -> None:
        paths = {
            "search": lambda: self.service.search("nota", consumer="human"),
            "browse_sources": lambda: self.service.browse_sources(consumer="human"),
            "graph_data": lambda: self.service.graph_data(consumer="local"),
            "export": lambda: self.service.export("json"),
        }
        for name, call in paths.items():
            with self.subTest(path=name):
                try:
                    call()
                except PolicyError as policy_error:
                    self.assertIn("personal", json.dumps(policy_error.details))
                except KeyError as leaked:  # pragma: no cover - the defect
                    self.fail(f"{name} leaked a bare KeyError: {leaked!r}")

    def test_the_error_is_a_brainskit_error_so_the_envelope_holds(self) -> None:
        """The point of the fix: it must reach the JSON envelope and exit codes."""

        from brainskit.domain.model import BrainskitError

        try:
            self.service.export("json")
        except BrainskitError:
            return
        except KeyError as leaked:  # pragma: no cover - the defect
            self.fail(f"export leaked a bare KeyError: {leaked!r}")

    def test_filing_reads_the_same_branch_the_same_way(self) -> None:
        """The twin the audit did not name: `filing.py` had the same subscript."""

        import inspect

        from brainskit.application import filing

        self.assertNotIn(
            "config().branches[",
            inspect.getsource(filing),
            msg="filing.py still subscripts branches directly",
        )


class StrictestPrivacyRefusesToGuessTest(ServiceFixture):
    """`strictest_privacy` must not answer CLOUD for "nothing contributed".

    Its docstring justified the CLOUD default by asserting that *every* caller
    checks provenance resolves first. `_evidence_privacy` was a caller that did
    not, and the assertion is what made the leak invisible. Task 1.1 made the
    claim true; this makes it enforced, so the next caller cannot reintroduce it
    by simply forgetting.
    """

    def test_an_empty_iterable_requires_an_explicit_answer(self) -> None:
        from brainskit.application.privacy import strictest_privacy

        with self.assertRaises(TypeError):
            strictest_privacy(iter(()))  # type: ignore[call-arg]

    def test_the_explicit_answer_is_honoured(self) -> None:
        from brainskit.application.privacy import strictest_privacy
        from brainskit.domain.model import PrivacyMode

        self.assertEqual(
            strictest_privacy(iter(()), on_empty=PrivacyMode.NEVER_INGEST),
            PrivacyMode.NEVER_INGEST,
        )

    def test_a_non_empty_iterable_still_takes_the_strictest(self) -> None:
        """Control: the actual rule must be unchanged."""

        from brainskit.application.privacy import strictest_privacy
        from brainskit.domain.model import PrivacyMode

        self.assertEqual(
            strictest_privacy(
                iter((PrivacyMode.CLOUD, PrivacyMode.NEVER_INGEST)),
                on_empty=PrivacyMode.CLOUD,
            ),
            PrivacyMode.NEVER_INGEST,
        )


class SearchHonoursItsLimitTest(ServiceFixture):
    """`search(limit=N)` returned N+1 for N < 4.

        target_graph = max(1, limit // 4) if limit >= 4 else 0   # 0 when N < 4
        direct_limit = max(1, limit - target_graph) if target_graph else limit
        hits         = ranked[:direct_limit]                     # == N
        reserve      = limit - len(hits)                         # == 0
        ...
            expanded.append(hit)          # appends BEFORE the bound check
            if len(expanded) >= reserve:  # 1 >= 0 -> break, one already added
                break

    So exactly one link-neighbour was always appended. It propagates into
    `context`, where `limit` is the caller's bound on how much evidence reaches
    a model -- the one number a caller uses to control cost.
    """

    def setUp(self) -> None:
        super().setUp()
        source = self.capture_into(
            "30-public", text="Nota publica sobre o produto.", title="publico"
        )
        # Enough pages that graph expansion has something to offer. The bodies
        # have to differ substantially: the novelty gate rejects a near-copy at
        # similarity 1.0, which is correct behaviour and not this test's subject.
        bodies = [
            "O produto atende equipes pequenas com faturamento mensal. ",
            "Integracao do produto com sistemas de pagamento externos. ",
            "Roteiro de adocao do produto para clientes corporativos. ",
            "Politica de suporte e prazos de resposta deste produto. ",
            "Comparativo entre planos do produto e limites de uso. ",
            "Historico de versoes do produto e mudancas relevantes. ",
        ]
        # The defect lives in the *graph expansion* pass, which only runs when a
        # hit has link neighbours. Created linked from the start: without the
        # links the loop has nothing to append and every assertion below would
        # pass against the bug.
        for index, body in enumerate(bodies):
            links = [] if index == 0 else ["produto-0"]
            tail = "" if index == 0 else "Veja tambem [[produto-0]]. "
            self.service.apply(
                {
                    "operations": [
                        {
                            "action": "upsert",
                            "kind": "concept",
                            "slug": f"produto-{index}",
                            "title": f"Produto {index}",
                            "aliases": [],
                            "source_hashes": [source],
                            "body": f"{body}{tail}[^source:{source}]",
                            "links": links,
                        }
                    ]
                }
            )

    def test_small_limits_return_exactly_what_was_asked_for(self) -> None:
        for limit in (1, 2, 3):
            with self.subTest(limit=limit):
                result = self.service.search("produto", limit, consumer="human")
                self.assertEqual(result["count"], limit)
                self.assertEqual(len(result["hits"]), limit)

    def test_a_larger_limit_still_leaves_room_for_graph_expansion(self) -> None:
        """Control: the fix must bound the expansion, not delete it."""

        result = self.service.search("produto", 6, consumer="human")
        self.assertLessEqual(result["count"], 6)


class GraphArtifactCarriesItsBoundaryTest(ServiceFixture):
    """`bk graph` wrote an unfiltered artifact with no consumer stamp.

    Every sibling path filters -- `graph_data`, `export`, the integrations --
    and this one built the graph and wrote it straight to disk. The impact is
    bounded because `graph/` is gitignored, but it means two files called "the
    graph" carried different boundaries and nothing on either said which.
    """

    def setUp(self) -> None:
        super().setUp()
        self.secret_hash = self.capture_into(
            "10-work", text="Plano confidencial da aquisicao.", title="segredo"
        )
        self.public_hash = self.capture_into(
            "30-public", text="Nota publica sobre o produto.", title="publico"
        )

    def artifact(self) -> str:
        return (self.root / "graph" / "graph.json").read_text(encoding="utf-8")

    def test_the_default_artifact_drops_never_ingest_evidence(self) -> None:
        self.service.graph()
        written = self.artifact()
        self.assertNotIn(self.secret_hash, written)
        self.assertNotIn("segredo", written)

    def test_the_artifact_names_the_consumer_it_was_written_for(self) -> None:
        self.service.graph()
        self.assertEqual(json.loads(self.artifact())["consumer"], "local")

    def test_public_evidence_survives(self) -> None:
        """Control: filtering must not degrade into writing an empty graph."""

        self.service.graph()
        self.assertIn(self.public_hash, self.artifact())

    def test_a_wider_consumer_is_honoured_when_asked_for(self) -> None:
        self.service.graph(consumer="human")
        written = self.artifact()
        self.assertEqual(json.loads(written)["consumer"], "human")
        self.assertIn(self.secret_hash, written)


class GraphAndLintAgreeOnUnknownSourcesTest(ServiceFixture):
    """Two code paths, two answers about one fact.

    `infrastructure/graph.py` guarded the `sourced_from` edge with
    `if raw_id in nodes:` and dropped it in silence. `health.py` reports the
    same condition as `wiki.unknown_source`. In the artifact whose entire
    purpose is to make provenance structural, a citation that does not resolve
    was the one thing the graph would not tell you about.

    Dropping the edge stays correct -- the graph promises no dangling edges --
    but it must be *counted*, not silent.
    """

    def setUp(self) -> None:
        super().setUp()
        self.secret_hash = self.capture_into(
            "10-work", text="Plano confidencial da aquisicao.", title="segredo"
        )
        self.page = self.upsert_page(
            "board-memo", "Board memo", "O plano. ", self.secret_hash
        )
        self.service.forget(self.secret_hash, force=True)

    def test_lint_reports_the_unresolvable_citation(self) -> None:
        """Control: the condition genuinely exists in this fixture."""

        codes = [f["code"] for f in self.service.lint()["findings"]]
        self.assertIn("wiki.unknown_source", codes)

    def test_the_graph_counts_what_it_dropped(self) -> None:
        result = self.service.graph(consumer="human")
        self.assertGreaterEqual(result.get("unresolved_sources", 0), 1)

    def test_a_vault_with_resolvable_provenance_counts_zero(self) -> None:
        """Control: the counter must not fire on a healthy vault."""

        public_hash = self.capture_into(
            "30-public", text="Nota publica sobre o produto.", title="publico"
        )
        clean = ServiceFixture()
        clean.setUp()
        try:
            source = clean.capture_into(
                "30-public", text="Outra nota publica distinta.", title="outra"
            )
            clean.upsert_page("nota", "Nota", "Conteudo publico. ", source)
            self.assertEqual(
                clean.service.graph(consumer="human").get("unresolved_sources", 0), 0
            )
        finally:
            clean.tearDown()
        self.assertTrue(public_hash)


class UnresolvableProvenanceFailsClosedTest(ServiceFixture):
    """A page whose sources no longer resolve must not be declassified.

    `_evidence_privacy` dropped unresolvable hashes with `if content_hash in
    records` and handed the empty remainder to `strictest_privacy`, which
    answers CLOUD. So forgetting a `never-ingest` source did not redact the
    pages built from it -- it published them, and stamped them `privacy:
    cloud`, an active false claim.

    `Enrichment.privacy_of` already answers this exact question correctly and
    is pinned by a test. This is the parallel classifier for wiki pages, which
    had no such test. The one high-value invariant the suite left unpinned is
    the one that broke.
    """

    SECRET = "SEGREDO-DA-AQUISICAO"

    def setUp(self) -> None:
        super().setUp()
        self.secret_hash = self.capture_into(
            "10-work", text=f"Plano confidencial. {self.SECRET}.", title="segredo"
        )
        # The page body carries the sensitive substance, because that is what a
        # page compiled from never-ingest evidence actually looks like. A body
        # that merely cites the source would let `read_resource` pass this test
        # while still serving the page.
        self.page = self.upsert_page(
            "board-memo",
            "Board memo",
            f"O plano da aquisicao: {self.SECRET}. ",
            self.secret_hash,
        )

    def cloud_search(self) -> dict:
        return self.service.search("plano", consumer="cloud")

    def test_while_the_source_resolves_the_page_is_redacted(self) -> None:
        """Control. If this ever fails, the fixture stopped exercising anything."""

        result = self.cloud_search()
        self.assertEqual(result["count"], 0)
        self.assertGreaterEqual(result["redacted"], 1)

    def test_forgetting_the_source_does_not_declassify_the_page(self) -> None:
        forgotten = self.service.forget(self.secret_hash, force=True)
        # bk forget already knows the page is orphaned, and proceeds anyway.
        self.assertIn(self.page, forgotten["still_cited_by"])

        result = self.cloud_search()
        self.assertEqual(result["count"], 0, msg="orphaned page served to cloud")
        self.assertGreaterEqual(result["redacted"], 1)

    def test_the_secret_never_appears_in_a_cloud_payload(self) -> None:
        self.service.forget(self.secret_hash, force=True)
        self.assertNotIn(self.SECRET, json.dumps(self.cloud_search()))

    def test_the_page_is_not_stamped_cloud(self) -> None:
        """The leak was not merely un-redacted: it asserted `privacy: cloud`."""

        self.service.forget(self.secret_hash, force=True)
        privacies = [hit.get("privacy") for hit in self.cloud_search()["hits"]]
        self.assertNotIn("cloud", privacies)

    def test_read_resource_refuses_the_orphaned_page_for_cloud(self) -> None:
        """`read_resource` serves the full page body, so it is the worst path."""

        self.service.forget(self.secret_hash, force=True)
        with self.assertRaises(PolicyError) as refused:
            self.service.read_resource(self.page, consumer="cloud")
        self.assertEqual(refused.exception.details["privacy"], "never-ingest")

    def test_a_human_consumer_still_reaches_the_orphaned_page(self) -> None:
        """`human` applies no restriction, so the page must remain readable.

        Without this, every assertion above would also pass if the fix simply
        made orphaned pages unreachable to everyone.
        """

        self.service.forget(self.secret_hash, force=True)
        payload = self.service.read_resource(self.page, consumer="human")
        self.assertIn(self.SECRET, json.dumps(payload))

    def test_browse_pages_does_not_list_the_orphaned_page_for_cloud(self) -> None:
        self.service.forget(self.secret_hash, force=True)
        listed = json.dumps(self.service.browse_pages(consumer="cloud"))
        self.assertNotIn("board-memo", listed)

    def test_a_page_that_declares_no_sources_stays_cloud_visible(self) -> None:
        """The other half of the rule, and the one a blanket fix would break.

        'Declares no provenance' is a system page and is legitimately cloud.
        Only 'declares provenance that does not resolve' is unknown. Without
        this control, returning NEVER_INGEST unconditionally would pass every
        test above while making the vault useless.
        """

        public_hash = self.capture_into(
            "30-public", text="Nota publica sobre o produto.", title="publico"
        )
        self.upsert_page("nota-publica", "Nota publica", "Publico. ", public_hash)
        result = self.service.search("publico", consumer="cloud")
        self.assertGreaterEqual(result["count"], 1)


if __name__ == "__main__":
    unittest.main()
