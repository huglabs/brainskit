from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
import unittest
from collections.abc import Callable
from contextlib import closing
from pathlib import Path
from unittest import mock

from brainkit.application.services import BrainkitService
from brainkit.domain.model import (
    LEGACY_WIKI_DIRECTORIES,
    PAGE_DIRECTORIES,
    WIKI_DIRECTORIES,
    PageKind,
    PageOperation,
    ValidationError,
    VaultConfig,
)
from brainkit.infrastructure.graph import MarkdownGraph
from brainkit.infrastructure.index import SqliteFtsIndex
from brainkit.infrastructure.integrations import NativeIntegrations
from brainkit.infrastructure.llm import (
    DEFAULT_OLLAMA_OPTIONS,
    JobSpecs,
    OllamaDriver,
    PolicyJudgmentRouter,
    _create_driver,
)
from brainkit.infrastructure.vault import FileVault


def policy() -> dict:
    return {
        "version": 3,
        "wiki_language": "Portuguese (Brazil)",
        "inbox_policy": {
            "privacy": "local-only",
            "filing": "approve-each",
        },
        "branches": {
            "10-work": {
                "privacy": "never-ingest",
                "filing": "approve-each",
            },
            "20-research": {
                "privacy": "local-only",
                "filing": "auto+digest-review",
            },
            "30-public": {
                "privacy": "cloud",
                "filing": "auto+digest-review",
            },
        },
        "providers": {
            "ollama": {"base_url": "http://127.0.0.1:11434"},
            "anthropic": {
                "base_url": "https://api.anthropic.com/v1",
                "api_key_env": "BRAINKIT_TEST_ANTHROPIC_KEY",
            },
            "openai": {
                "base_url": "https://api.openai.com/v1",
                "api_key_env": "BRAINKIT_TEST_OPENAI_KEY",
            },
        },
        "job_models": {
            "ingest": {"provider": "ollama", "model": "test"},
            "query": {"provider": "ollama", "model": "test"},
            "digest": {"provider": "ollama", "model": "test"},
            "lint-semantic": {"provider": "ollama", "model": "test"},
            "file-proposal": {"provider": "ollama", "model": "test"},
            "resurface": {"provider": "ollama", "model": "test"},
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


class _FakeResponse:
    """Minimal stand-in for the object urlopen yields as a context manager."""

    def __init__(self, payload: bytes):
        self._payload = payload

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *exception: object) -> bool:
        return False

    def read(self) -> bytes:
        return self._payload


def _wire_capture(response: dict) -> tuple[list[dict], object]:
    """Patch target for urlopen that records the JSON body actually sent."""

    sent: list[dict] = []
    encoded = json.dumps(response).encode("utf-8")

    def fake_urlopen(request, timeout=None):  # noqa: ANN001 - urlopen signature
        sent.append(json.loads(request.data.decode("utf-8")))
        return _FakeResponse(encoded)

    return sent, fake_urlopen


class PageDirectoryTest(unittest.TestCase):
    def test_every_page_kind_maps_to_its_irregular_plural(self) -> None:
        self.assertEqual(
            {kind.value: kind.directory for kind in PageKind},
            {
                "source": "sources",
                "entity": "entities",
                "concept": "concepts",
                "synthesis": "syntheses",
            },
        )

    def test_no_page_kind_directory_is_derived_by_appending_s(self) -> None:
        self.assertEqual(PageKind.SYNTHESIS.directory, "syntheses")
        self.assertNotEqual(PageKind.SYNTHESIS.directory, "synthesiss")
        self.assertEqual(PageKind.ENTITY.directory, "entities")
        self.assertNotEqual(PageKind.ENTITY.directory, "entitys")

    def test_relative_path_targets_the_scaffolded_directory(self) -> None:
        for kind, directory in PAGE_DIRECTORIES.items():
            operation = PageOperation.from_dict(
                {
                    "action": "upsert",
                    "kind": kind.value,
                    "slug": "pagina",
                    "title": "Página",
                    "body": "Corpo.",
                }
            )
            self.assertEqual(operation.relative_path, f"wiki/{directory}/pagina.md")

    def test_legacy_directories_describe_the_old_naive_rule(self) -> None:
        self.assertEqual(
            LEGACY_WIKI_DIRECTORIES,
            {"synthesiss": "syntheses", "entitys": "entities"},
        )
        self.assertFalse(set(LEGACY_WIKI_DIRECTORIES) & set(WIKI_DIRECTORIES))


class VaultScaffoldGuardTest(unittest.TestCase):
    """The drift that produced `wiki/synthesiss` must be unrepresentable."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        FileVault.initialize(self.root, policy())

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_init_scaffolds_exactly_the_page_kind_directories(self) -> None:
        scaffolded = {
            path.name for path in (self.root / "wiki").iterdir() if path.is_dir()
        }
        self.assertEqual(scaffolded, set(WIKI_DIRECTORIES))

    def test_every_page_kind_directory_is_scaffolded_by_init(self) -> None:
        for kind in PageKind:
            self.assertTrue(
                (self.root / "wiki" / kind.directory).is_dir(),
                f"bk init does not scaffold wiki/{kind.directory}",
            )


class ApplyDirectoryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.vault = FileVault.initialize(self.root, policy())
        self.service = BrainkitService(
            self.vault,
            SqliteFtsIndex(self.vault.index_path),
            graph=MarkdownGraph(),
            integrations=NativeIntegrations(self.vault),
        )
        self.service.reindex()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _apply_every_kind(self) -> dict:
        captured = self.service.capture(
            None, text="Evidência de teste.", title="Evidência"
        )
        content_hash = captured["source"]["content_hash"]
        return self.service.apply(
            {
                "operations": [
                    {
                        "action": "upsert",
                        "kind": kind.value,
                        "slug": f"pagina-{kind.value}",
                        "title": f"Página {kind.value}",
                        "source_hashes": [content_hash],
                        "body": f"Corpo da página.[^source:{content_hash}]",
                        "links": [],
                    }
                    for kind in PageKind
                ]
            }
        )

    def test_apply_files_every_kind_in_its_scaffolded_directory(self) -> None:
        applied = self._apply_every_kind()
        self.assertEqual(
            applied["paths"],
            [
                "wiki/concepts/pagina-concept.md",
                "wiki/entities/pagina-entity.md",
                "wiki/sources/pagina-source.md",
                "wiki/syntheses/pagina-synthesis.md",
            ],
        )
        for path in applied["paths"]:
            self.assertTrue((self.root / path).is_file())
        self.assertFalse((self.root / "wiki" / "synthesiss").exists())
        self.assertFalse((self.root / "wiki" / "entitys").exists())

    def test_applied_pages_are_lint_clean_and_counted(self) -> None:
        self._apply_every_kind()
        report = self.service.lint()
        self.assertTrue(report["ok"], report["findings"])
        self.assertEqual(report["findings"], [])
        status = self.service.status()
        self.assertEqual(status["wiki_pages"], 2 + len(PageKind))
        self.assertEqual(status["freshness"]["fresh"], len(PageKind))


class LegacyDirectoryMigrationTest(unittest.TestCase):
    """Vaults written by the misspelling build must heal when reopened."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        vault = FileVault.initialize(self.root, policy())
        service = self._service(vault)
        service.reindex()
        captured = service.capture(
            None, text="Evidência da síntese.", title="Evidência"
        )
        self.content_hash = captured["source"]["content_hash"]
        service.apply(
            {
                "proposal_id": "legacy-proposal",
                "operations": [
                    {
                        "action": "upsert",
                        "kind": "synthesis",
                        "slug": "sintese-legada",
                        "title": "Síntese legada",
                        "source_hashes": [self.content_hash],
                        "body": (
                            "Corpo da síntese legada."
                            f"[^source:{self.content_hash}]"
                        ),
                        "links": [],
                    },
                    {
                        "action": "upsert",
                        "kind": "entity",
                        "slug": "entidade-legada",
                        "title": "Entidade legada",
                        "source_hashes": [self.content_hash],
                        "body": (
                            "Corpo da entidade legada."
                            f"[^source:{self.content_hash}]"
                        ),
                        "links": [],
                    },
                ],
            }
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _service(self, vault: FileVault) -> BrainkitService:
        return BrainkitService(
            vault,
            SqliteFtsIndex(vault.index_path),
            graph=MarkdownGraph(),
            integrations=NativeIntegrations(vault),
        )

    def _rewind_to_legacy_layout(self) -> dict[str, str]:
        """Recreate exactly what the misspelling build left on disk."""

        renames = {
            f"wiki/{canonical}/{slug}.md": f"wiki/{legacy}/{slug}.md"
            for legacy, canonical in LEGACY_WIKI_DIRECTORIES.items()
            for slug in ("sintese-legada", "entidade-legada")
            if (self.root / "wiki" / canonical / f"{slug}.md").is_file()
        }
        for canonical_path, legacy_path in renames.items():
            target = self.root / legacy_path
            target.parent.mkdir(parents=True, exist_ok=True)
            (self.root / canonical_path).rename(target)
        freshness_path = self.root / ".brain" / "freshness.json"
        freshness = json.loads(freshness_path.read_text(encoding="utf-8"))
        freshness["pages"] = {
            renames.get(path, path): entry
            for path, entry in freshness["pages"].items()
        }
        freshness_path.write_text(
            json.dumps(freshness, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        applied_path = self.root / ".brain" / "applied.json"
        applied = json.loads(applied_path.read_text(encoding="utf-8"))
        result = applied["proposals"]["legacy-proposal"]
        result["paths"] = sorted(
            renames.get(path, path) for path in result["paths"]
        )
        applied_path.write_text(
            json.dumps(applied, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        with closing(sqlite3.connect(self.root / ".brain" / "index.db")) as connection:
            with connection:
                for canonical_path, legacy_path in renames.items():
                    connection.execute(
                        "UPDATE search_fts SET path = ? WHERE path = ?",
                        (legacy_path, canonical_path),
                    )
        return renames

    def _snapshot(self) -> dict[str, str]:
        return {
            path.relative_to(self.root).as_posix(): hashlib.sha256(
                path.read_bytes()
            ).hexdigest()
            for path in sorted(self.root.rglob("*"))
            if path.is_file() and path.suffix != ".lock"
        }

    def _index_wiki_paths(self) -> list[str]:
        with closing(sqlite3.connect(self.root / ".brain" / "index.db")) as connection:
            return sorted(
                row[0]
                for row in connection.execute(
                    "SELECT path FROM search_fts WHERE kind = 'wiki'"
                )
            )

    def test_reopening_moves_legacy_pages_and_rewrites_every_index(self) -> None:
        renames = self._rewind_to_legacy_layout()
        self.assertEqual(len(renames), 2)
        self.assertTrue((self.root / "wiki/synthesiss/sintese-legada.md").is_file())
        self.assertTrue((self.root / "wiki/entitys/entidade-legada.md").is_file())

        vault = FileVault(self.root)

        self.assertFalse((self.root / "wiki" / "synthesiss").exists())
        self.assertFalse((self.root / "wiki" / "entitys").exists())
        self.assertTrue((self.root / "wiki/syntheses/sintese-legada.md").is_file())
        self.assertTrue((self.root / "wiki/entities/entidade-legada.md").is_file())

        freshness = vault.read_state("freshness")["pages"]
        self.assertIn("wiki/syntheses/sintese-legada.md", freshness)
        self.assertIn("wiki/entities/entidade-legada.md", freshness)
        self.assertNotIn("wiki/synthesiss/sintese-legada.md", freshness)
        self.assertNotIn("wiki/entitys/entidade-legada.md", freshness)
        for path, entry in freshness.items():
            self.assertEqual(entry["content_hash"], vault.wiki_version(path))

        applied = vault.read_state("applied")["proposals"]["legacy-proposal"]
        self.assertEqual(
            applied["paths"],
            [
                "wiki/entities/entidade-legada.md",
                "wiki/syntheses/sintese-legada.md",
            ],
        )
        self.assertIn("wiki/syntheses/sintese-legada.md", self._index_wiki_paths())

    def test_migrated_vault_reports_no_outside_apply_finding(self) -> None:
        self._rewind_to_legacy_layout()
        service = self._service(FileVault(self.root))
        report = service.lint()
        self.assertEqual(
            [item for item in report["findings"] if item["code"] == "wiki.outside_apply"],
            [],
        )
        self.assertTrue(report["ok"], report["findings"])

    def test_search_finds_the_page_at_its_new_path(self) -> None:
        self._rewind_to_legacy_layout()
        service = self._service(FileVault(self.root))
        hits = service.index.search("legada", limit=20)
        paths = {hit.path for hit in hits}
        self.assertIn("wiki/syntheses/sintese-legada.md", paths)
        self.assertIn("wiki/entities/entidade-legada.md", paths)
        self.assertNotIn("wiki/synthesiss/sintese-legada.md", paths)

    def test_migration_is_byte_identical_when_run_twice(self) -> None:
        self._rewind_to_legacy_layout()
        FileVault(self.root)
        first = self._snapshot()
        FileVault(self.root)
        self.assertEqual(self._snapshot(), first)

    def test_migration_is_a_no_op_on_a_clean_vault(self) -> None:
        before = self._snapshot()
        FileVault(self.root)
        self.assertEqual(self._snapshot(), before)

    def test_identical_collision_discards_the_legacy_copy(self) -> None:
        self._rewind_to_legacy_layout()
        legacy = self.root / "wiki/synthesiss/sintese-legada.md"
        canonical = self.root / "wiki/syntheses/sintese-legada.md"
        canonical.parent.mkdir(parents=True, exist_ok=True)
        canonical.write_bytes(legacy.read_bytes())

        vault = FileVault(self.root)

        self.assertFalse(legacy.exists())
        self.assertTrue(canonical.is_file())
        self.assertFalse((self.root / ".brain" / "legacy-pages").exists())
        freshness = vault.read_state("freshness")["pages"]
        self.assertIn("wiki/syntheses/sintese-legada.md", freshness)
        self.assertNotIn("wiki/synthesiss/sintese-legada.md", freshness)

    def test_divergent_collision_quarantines_instead_of_overwriting(self) -> None:
        self._rewind_to_legacy_layout()
        legacy = self.root / "wiki/synthesiss/sintese-legada.md"
        canonical = self.root / "wiki/syntheses/sintese-legada.md"
        canonical.parent.mkdir(parents=True, exist_ok=True)
        canonical.write_text("página canônica preservada\n", encoding="utf-8")
        legacy_body = legacy.read_text(encoding="utf-8")

        vault = FileVault(self.root)

        self.assertFalse(legacy.exists())
        self.assertEqual(
            canonical.read_text(encoding="utf-8"), "página canônica preservada\n"
        )
        quarantined = sorted(
            (self.root / ".brain" / "legacy-pages" / "synthesiss").iterdir()
        )
        self.assertEqual(len(quarantined), 1)
        self.assertEqual(quarantined[0].read_text(encoding="utf-8"), legacy_body)
        self.assertNotIn(
            "wiki/synthesiss/sintese-legada.md",
            vault.read_state("freshness")["pages"],
        )


class OllamaContextOptionTest(unittest.TestCase):
    """Ollama defaults num_ctx to 4096 unless the request says otherwise."""

    def _complete(
        self, driver: OllamaDriver, *, model: str = "test-model"
    ) -> dict:
        sent, fake_urlopen = _wire_capture({"message": {"content": "{}"}})
        with mock.patch("urllib.request.urlopen", fake_urlopen):
            driver.complete("prompt", model=model, output_schema=None)
        self.assertEqual(len(sent), 1)
        return sent[0]

    def test_default_request_raises_the_context_window_above_4096(self) -> None:
        payload = self._complete(OllamaDriver(base_url="http://127.0.0.1:11434"))
        self.assertGreater(payload["options"]["num_ctx"], 4096)
        self.assertEqual(
            payload["options"]["num_ctx"], DEFAULT_OLLAMA_OPTIONS["num_ctx"]
        )

    def test_temperature_stays_deterministic_by_default(self) -> None:
        payload = self._complete(OllamaDriver(base_url="http://127.0.0.1:11434"))
        self.assertEqual(payload["options"]["temperature"], 0)

    def test_operator_options_override_the_defaults(self) -> None:
        driver = OllamaDriver(
            base_url="http://127.0.0.1:11434",
            options={"num_ctx": 512, "temperature": 0.7, "num_predict": 64},
        )
        payload = self._complete(driver)
        self.assertEqual(
            payload["options"],
            {"num_ctx": 512, "temperature": 0.7, "num_predict": 64},
        )

    def test_defaults_are_not_mutated_between_requests(self) -> None:
        driver = OllamaDriver(base_url="http://127.0.0.1:11434")
        first = self._complete(driver)
        first["options"]["num_ctx"] = 1
        second = self._complete(driver)
        self.assertEqual(
            second["options"]["num_ctx"], DEFAULT_OLLAMA_OPTIONS["num_ctx"]
        )
        self.assertEqual(DEFAULT_OLLAMA_OPTIONS["num_ctx"], 16384)

    def test_provider_config_options_reach_the_driver(self) -> None:
        driver = _create_driver(
            "ollama",
            {
                "base_url": "http://127.0.0.1:11434",
                "timeout_seconds": 900,
                "options": {"num_ctx": 32768},
            },
        )
        assert isinstance(driver, OllamaDriver)
        self.assertEqual(driver.timeout, 900)
        self.assertEqual(driver.options, {"temperature": 0, "num_ctx": 32768})

    def test_provider_options_must_be_an_object(self) -> None:
        with self.assertRaises(ValidationError):
            _create_driver(
                "ollama",
                {"base_url": "http://127.0.0.1:11434", "options": 32768},
            )

    def test_router_sends_num_ctx_for_a_local_only_branch(self) -> None:
        raw = policy()
        raw["providers"]["ollama"]["options"] = {"num_ctx": 8192}
        config = VaultConfig.from_dict(raw)
        router = PolicyJudgmentRouter(config, JobSpecs())
        sent, fake_urlopen = _wire_capture({"message": {"content": "{}"}})
        with mock.patch("urllib.request.urlopen", fake_urlopen):
            router.run(
                job="resurface",
                branches=["20-research"],
                variables={"context": "{}", "repair_feedback": ""},
            )
        self.assertEqual(sent[0]["options"]["num_ctx"], 8192)

    def test_other_providers_keep_their_own_request_shape(self) -> None:
        raw = policy()
        raw["providers"]["ollama"]["options"] = {"num_ctx": 8192}
        for provider, key in (
            ("anthropic", "BRAINKIT_TEST_ANTHROPIC_KEY"),
            ("openai", "BRAINKIT_TEST_OPENAI_KEY"),
        ):
            with self.subTest(provider=provider):
                with mock.patch.dict("os.environ", {key: "test-key"}):
                    driver = _create_driver(provider, raw["providers"][provider])
                sent, fake_urlopen = _wire_capture(
                    {
                        "content": [{"type": "text", "text": "{}"}],
                        "choices": [{"message": {"content": "{}"}}],
                    }
                )
                with mock.patch("urllib.request.urlopen", fake_urlopen):
                    driver.complete("prompt", model="m", output_schema=None)
                self.assertNotIn("options", sent[0])
                self.assertEqual(sent[0]["temperature"], 0)


class BranchPolicyDetailTest(unittest.TestCase):
    """A rejected branch policy must name the branch, field and valid values."""

    def config_with(self, mutate: Callable[[dict], None]) -> dict:
        raw = policy()
        mutate(raw)
        return raw

    def reject(self, mutate: Callable[[dict], None]) -> dict:
        with self.assertRaises(ValidationError) as rejected:
            VaultConfig.from_dict(self.config_with(mutate))
        return rejected.exception.details

    def test_an_invalid_privacy_value_is_named(self) -> None:
        def mutate(raw: dict) -> None:
            raw["branches"]["20-research"]["privacy"] = "cloud-ok"

        details = self.reject(mutate)
        self.assertEqual(details["branch"], "20-research")
        self.assertEqual(details["field"], "privacy")
        self.assertEqual(details["value"], "cloud-ok")
        self.assertIn("cloud", details["choices"])

    def test_a_missing_field_is_reported_as_missing(self) -> None:
        def mutate(raw: dict) -> None:
            del raw["branches"]["20-research"]["filing"]

        details = self.reject(mutate)
        self.assertEqual(details["branch"], "20-research")
        self.assertEqual(details["field"], "filing")
        self.assertTrue(details["missing"])
        self.assertNotIn("value", details)

    def test_a_non_object_branch_policy_is_reported(self) -> None:
        def mutate(raw: dict) -> None:
            raw["branches"]["20-research"] = "local-only"

        details = self.reject(mutate)
        self.assertEqual(details["branch"], "20-research")
        self.assertEqual(details["expected"], "object")
        self.assertEqual(details["observed"], "str")

    def test_the_inbox_policy_is_identified_by_name(self) -> None:
        def mutate(raw: dict) -> None:
            raw["inbox_policy"]["privacy"] = "privado"

        details = self.reject(mutate)
        self.assertEqual(details["branch"], "inbox_policy")
        self.assertEqual(details["field"], "privacy")

    def test_a_valid_policy_is_still_accepted(self) -> None:
        config = VaultConfig.from_dict(policy())
        self.assertIn("20-research", config.branches)


if __name__ == "__main__":
    unittest.main()
