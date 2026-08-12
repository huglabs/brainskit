from __future__ import annotations

import hashlib
import io
import json
import sqlite3
import tempfile
import unittest
import urllib.error
from collections.abc import Callable
from contextlib import closing, redirect_stderr, redirect_stdout
from pathlib import Path
from typing import ClassVar
from unittest import mock

from brainskit.application.judgment import JudgmentRunner
from brainskit.application.services import BrainskitService
from brainskit.domain.model import (
    LEGACY_WIKI_DIRECTORIES,
    PAGE_DIRECTORIES,
    WIKI_DIRECTORIES,
    ConflictError,
    ModelResponseError,
    NotConfiguredError,
    PageKind,
    PageOperation,
    PolicyError,
    RefusalError,
    ValidationError,
    VaultConfig,
    _compiled_validator,
    validate_schema,
)
from brainskit.infrastructure.graph import MarkdownGraph
from brainskit.infrastructure.index import SqliteFtsIndex, _iter_documents
from brainskit.infrastructure.integrations import NativeIntegrations
from brainskit.infrastructure.llm import (
    DEFAULT_ANTHROPIC_MAX_TOKENS,
    DEFAULT_OLLAMA_OPTIONS,
    AnthropicDriver,
    JobSpecs,
    OllamaDriver,
    PolicyJudgmentRouter,
    _create_driver,
    _strictly_expressible,
    _structured_schema,
)
from brainskit.infrastructure.vault import FileVault
from brainskit.interfaces import cli


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
                "api_key_env": "BRAINSKIT_TEST_ANTHROPIC_KEY",
            },
            "openai": {
                "base_url": "https://api.openai.com/v1",
                "api_key_env": "BRAINSKIT_TEST_OPENAI_KEY",
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

    def fake_urlopen(request, timeout=None):
        sent.append(json.loads(request.data.decode("utf-8")))
        return _FakeResponse(encoded)

    return sent, fake_urlopen


def _wire_sequence(*responses: object) -> tuple[list[dict], object]:
    """Like `_wire_capture`, but serves one reply per call.

    An `HTTPError` in the sequence is raised instead of returned, which is how
    a rejected request reaches the driver. Needed to exercise the paths where
    the first attempt is refused and the driver has to retry differently.
    """

    sent: list[dict] = []
    queue = list(responses)

    def fake_urlopen(request, timeout=None):
        sent.append(json.loads(request.data.decode("utf-8")))
        reply = queue.pop(0) if queue else {}
        if isinstance(reply, Exception):
            raise reply
        return _FakeResponse(json.dumps(reply).encode("utf-8"))

    return sent, fake_urlopen


def _http_error(status: int, body: str) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        "https://api.example/v1/messages",
        status,
        "Bad Request",
        {},  # type: ignore[arg-type]
        io.BytesIO(body.encode("utf-8")),
    )


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
        self.service = BrainskitService(
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

    def _service(self, vault: FileVault) -> BrainskitService:
        return BrainskitService(
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
            ("anthropic", "BRAINSKIT_TEST_ANTHROPIC_KEY"),
            ("openai", "BRAINSKIT_TEST_OPENAI_KEY"),
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
        # `temperature` is an OpenAI-only field now: the Anthropic frontier
        # models reject it outright, so asserting it for both providers would
        # be asserting the defect. See AnthropicRequestShapeTest.
        self.assertEqual(sent[0]["temperature"], 0)


class AnthropicRequestShapeTest(unittest.TestCase):
    """The Messages API request must be one the current models accept.

    Every assertion here corresponds to a way the previous request shape
    failed against a frontier model: a rejected sampling parameter, a schema
    described in prose instead of enforced, and two terminal responses that
    parsed cleanly while carrying no answer.
    """

    def driver(self, **kwargs: object) -> AnthropicDriver:
        return AnthropicDriver(
            base_url="https://api.example/v1", api_key="k", **kwargs
        )

    def send(
        self, *responses: object, schema: dict | None = None
    ) -> tuple[list[dict], str]:
        sent, fake_urlopen = _wire_sequence(*responses)
        with mock.patch("urllib.request.urlopen", fake_urlopen):
            text = self.driver().complete("prompt", model="m", output_schema=schema)
        return sent, text

    def test_no_sampling_parameter_is_sent(self) -> None:
        sent, _ = self.send({"content": [{"type": "text", "text": "{}"}]})
        for parameter in ("temperature", "top_p", "top_k"):
            self.assertNotIn(parameter, sent[0])

    def test_max_tokens_leaves_room_for_thinking(self) -> None:
        sent, _ = self.send({"content": [{"type": "text", "text": "{}"}]})
        self.assertEqual(sent[0]["max_tokens"], DEFAULT_ANTHROPIC_MAX_TOKENS)
        self.assertGreater(sent[0]["max_tokens"], 8192)

    def test_a_schema_is_enforced_not_described(self) -> None:
        schema = JobSpecs().schema("query")
        sent, _ = self.send(
            {"content": [{"type": "text", "text": "{}"}]}, schema=schema
        )
        self.assertEqual(
            sent[0]["output_config"]["format"]["type"], "json_schema"
        )
        self.assertNotIn("Return only JSON", sent[0]["messages"][0]["content"])

    def test_a_free_form_object_falls_back_to_the_described_schema(self) -> None:
        # The ingest schema carries a free-form `metadata` object, which
        # constrained decoding cannot express without forbidding exactly the
        # custom frontmatter a vault is allowed to declare.
        sent, _ = self.send(
            {"content": [{"type": "text", "text": "{}"}]},
            schema=JobSpecs().schema("ingest"),
        )
        self.assertEqual(len(sent), 1)
        self.assertNotIn("output_config", sent[0])
        self.assertIn("Return only JSON", sent[0]["messages"][0]["content"])

    def test_a_model_without_structured_output_degrades_instead_of_failing(
        self,
    ) -> None:
        sent, text = self.send(
            _http_error(400, '{"error":{"message":"output_config: unsupported"}}'),
            {"content": [{"type": "text", "text": "{}"}]},
            schema=JobSpecs().schema("query"),
        )
        self.assertEqual(len(sent), 2)
        self.assertIn("output_config", sent[0])
        self.assertNotIn("output_config", sent[1])
        self.assertIn("Return only JSON", sent[1]["messages"][0]["content"])
        self.assertEqual(text, "{}")

    def test_an_unrelated_400_is_not_retried_in_a_weaker_mode(self) -> None:
        sent, fake_urlopen = _wire_sequence(
            _http_error(400, '{"error":{"message":"model: not_found"}}')
        )
        with mock.patch("urllib.request.urlopen", fake_urlopen):
            with self.assertRaises(ValidationError) as caught:
                self.driver().complete(
                    "prompt", model="m", output_schema=JobSpecs().schema("query")
                )
        self.assertEqual(len(sent), 1)
        self.assertIn("not_found", str(caught.exception.details))

    def test_a_refusal_is_a_policy_denial_not_an_empty_answer(self) -> None:
        sent, fake_urlopen = _wire_sequence(
            {
                "content": [],
                "stop_reason": "refusal",
                "stop_details": {"type": "refusal", "category": "cyber"},
            }
        )
        with mock.patch("urllib.request.urlopen", fake_urlopen):
            with self.assertRaises(PolicyError) as caught:
                self.driver().complete("prompt", model="m", output_schema=None)
        self.assertEqual(caught.exception.details["category"], "cyber")

    def test_truncated_output_names_the_budget_that_truncated_it(self) -> None:
        sent, fake_urlopen = _wire_sequence(
            {
                "content": [{"type": "text", "text": '{"answer": "half'}],
                "stop_reason": "max_tokens",
            }
        )
        with mock.patch("urllib.request.urlopen", fake_urlopen):
            with self.assertRaises(ValidationError) as caught:
                self.driver(max_tokens=64).complete(
                    "prompt", model="m", output_schema=None
                )
        self.assertEqual(caught.exception.details["max_tokens"], 64)
        self.assertIn("max_tokens", str(caught.exception))

    def test_an_answer_with_no_text_is_rejected(self) -> None:
        sent, fake_urlopen = _wire_sequence(
            {"content": [{"type": "thinking", "thinking": ""}], "stop_reason": "end_turn"}
        )
        with mock.patch("urllib.request.urlopen", fake_urlopen):
            with self.assertRaises(ValidationError):
                self.driver().complete("prompt", model="m", output_schema=None)

    def test_the_token_budget_is_configurable_per_vault(self) -> None:
        with mock.patch.dict("os.environ", {"BRAINSKIT_TEST_ANTHROPIC_KEY": "k"}):
            driver = _create_driver(
                "anthropic",
                {
                    "base_url": "https://api.example/v1",
                    "api_key_env": "BRAINSKIT_TEST_ANTHROPIC_KEY",
                    "max_tokens": 32000,
                },
            )
        assert isinstance(driver, AnthropicDriver)
        self.assertEqual(driver.max_tokens, 32000)

    def test_a_mistyped_budget_is_rejected_rather_than_defaulted(self) -> None:
        for value in ("32000", 0, -1, True):
            with self.subTest(value=value):
                with mock.patch.dict(
                    "os.environ", {"BRAINSKIT_TEST_ANTHROPIC_KEY": "k"}
                ):
                    with self.assertRaises(ValidationError):
                        _create_driver(
                            "anthropic",
                            {
                                "base_url": "https://api.example/v1",
                                "api_key_env": "BRAINSKIT_TEST_ANTHROPIC_KEY",
                                "max_tokens": value,
                            },
                        )


class StructuredSchemaProjectionTest(unittest.TestCase):
    """Constrained decoding accepts a narrower schema than the vault enforces.

    The projection may only ever *widen* what a provider will emit — the full
    schema still runs locally in the repair loop, so a dropped keyword costs a
    retry, while a wrongly narrowed one would reject valid output forever.
    """

    def objects(self, node: object):
        if isinstance(node, dict):
            if "properties" in node:
                yield node
            for value in node.values():
                yield from self.objects(value)
        elif isinstance(node, list):
            for value in node:
                yield from self.objects(value)

    def test_every_shipped_schema_projects_to_a_closed_object_graph(self) -> None:
        for job in ("ingest", "query", "digest", "resurface", "lint-semantic"):
            with self.subTest(job=job):
                projected = _structured_schema(JobSpecs().schema(job))
                for node in self.objects(projected):
                    self.assertIs(node["additionalProperties"], False)
                    self.assertEqual(
                        sorted(node["required"]), sorted(node["properties"])
                    )

    def test_unsupported_keywords_are_dropped(self) -> None:
        projected = json.dumps(_structured_schema(JobSpecs().schema("ingest")))
        for keyword in ("minLength", "pattern", "minItems", "uniqueItems"):
            self.assertNotIn(keyword, projected)

    def test_an_optional_property_becomes_required_and_nullable(self) -> None:
        # Dropping it instead would forbid a field the vault permits.
        projected = _structured_schema(JobSpecs().schema("ingest"))
        operation = projected["properties"]["operations"]["items"]
        self.assertIn("metadata", operation["required"])
        self.assertEqual(operation["properties"]["metadata"]["type"], ["object", "null"])

    def test_a_free_form_object_disqualifies_strict_mode(self) -> None:
        self.assertFalse(
            _strictly_expressible(_structured_schema(JobSpecs().schema("ingest")))
        )
        for job in ("query", "digest", "resurface", "lint-semantic"):
            with self.subTest(job=job):
                self.assertTrue(
                    _strictly_expressible(_structured_schema(JobSpecs().schema(job)))
                )

    def test_a_free_form_object_is_found_at_any_depth(self) -> None:
        closed = {"type": "object", "properties": {}, "additionalProperties": False}
        for nesting in (
            {"type": "array", "items": {"type": "object"}},
            {"anyOf": [closed, {"type": "object"}]},
            {"$defs": {"loose": {"type": "object"}}, **closed},
        ):
            with self.subTest(nesting=sorted(nesting)):
                self.assertFalse(_strictly_expressible(nesting))


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


class ValidatorCompilationTest(unittest.TestCase):
    """Compiling a JSON Schema is the expensive half of validating against it."""

    SCHEMA: ClassVar[dict[str, object]] = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "required": ["id"],
        "properties": {"id": {"type": "string", "minLength": 1}},
    }

    def test_an_equal_schema_read_again_reuses_the_compiled_validator(self) -> None:
        # The vault re-reads schema.json from disk per call, so the cache has
        # to key on the schema's content, not on object identity.
        _compiled_validator.cache_clear()
        validate_schema({"id": "a"}, json.loads(json.dumps(self.SCHEMA)))
        first = _compiled_validator.cache_info()
        validate_schema({"id": "b"}, json.loads(json.dumps(self.SCHEMA)))
        second = _compiled_validator.cache_info()
        self.assertEqual(second.misses, first.misses)
        self.assertEqual(second.hits, first.hits + 1)

    def test_key_order_does_not_defeat_the_cache(self) -> None:
        _compiled_validator.cache_clear()
        reordered = dict(reversed(list(self.SCHEMA.items())))
        validate_schema({"id": "a"}, self.SCHEMA)
        validate_schema({"id": "a"}, reordered)
        self.assertEqual(_compiled_validator.cache_info().misses, 1)

    def test_an_invalid_schema_keeps_being_reported(self) -> None:
        # lru_cache does not memoize exceptions; a schema that fails to compile
        # must fail on the second call too, not pass silently.
        broken = {"type": "not-a-type"}
        for _ in range(2):
            failures = validate_schema({"id": "a"}, broken)
            self.assertEqual([failure["code"] for failure in failures], ["schema.invalid"])

    def test_an_unserializable_schema_still_validates(self) -> None:
        # A set cannot be cache-keyed. The schema must still be enforced —
        # a caching problem must never be answered with "valid".
        schema = {
            "type": "object",
            "required": ["id"],
            "properties": {"id": {"type": "string"}},
            "x-annotation": {1, 2},
        }
        self.assertEqual(validate_schema({"id": "a"}, schema), [])
        self.assertEqual(
            [failure["code"] for failure in validate_schema({}, schema)],
            ["schema.required"],
        )


class IndexStreamingTest(unittest.TestCase):
    """A rebuild must not hold the whole corpus in memory to count it."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.vault = FileVault.initialize(self.root, policy())
        self.index = SqliteFtsIndex(self.vault.index_path)
        self.service = BrainskitService(self.vault, self.index, graph=MarkdownGraph())
        for number in range(4):
            record, _ = self.vault.capture_text(f"body {number}", f"note-{number}")
            self.vault.file_source(record.content_hash, "20-research")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_documents_are_produced_lazily(self) -> None:
        reads: list[str] = []
        original = self.vault.raw_text

        def counting_raw_text(record, max_chars=None):
            reads.append(record.path)
            return original(record, max_chars)

        self.vault.raw_text = counting_raw_text  # type: ignore[method-assign]
        try:
            documents = _iter_documents(self.vault, self.vault.registry())
            self.assertEqual(reads, [])
            next(documents)
            self.assertEqual(len(reads), 1)
        finally:
            self.vault.raw_text = original  # type: ignore[method-assign]

    def test_a_streamed_rebuild_indexes_and_counts_every_document(self) -> None:
        counted = self.index.rebuild(self.vault)
        self.assertEqual(counted, len(list(_iter_documents(self.vault, self.vault.registry()))))
        self.assertEqual(self.index.stats()["documents"], counted)
        self.assertEqual(len(self.index.search("body", limit=10)), 4)

    def test_a_rebuild_replaces_rather_than_appends(self) -> None:
        first = self.index.rebuild(self.vault)
        second = self.index.rebuild(self.vault)
        self.assertEqual(first, second)
        self.assertEqual(self.index.stats()["documents"], second)


class ErrorContractTest(unittest.TestCase):
    """One code per distinct remedy, and no change to what anything catches.

    `validation_error` used to answer for five unrelated situations, so a
    caller had to read English to decide whether to retry, rephrase, or stop.
    These narrow it. The invariants that make that safe -- still a
    `ValidationError`, still exit 2 -- are asserted here rather than assumed,
    because the whole design rests on them.
    """

    CODES: ClassVar[dict[type[ValidationError], str]] = {
        ValidationError: "validation_error",
        ConflictError: "conflict",
        NotConfiguredError: "not_configured",
        RefusalError: "refused",
        ModelResponseError: "model_response_invalid",
    }

    def test_each_error_carries_its_own_stable_code(self) -> None:
        for error, code in self.CODES.items():
            with self.subTest(error=error.__name__):
                self.assertEqual(error("x").code, code)

    def test_no_two_errors_share_a_code(self) -> None:
        """A duplicated code silently merges two remedies back into one.

        Read off the classes, never off the table above: comparing the table
        with itself is a tautology that passes even with every code collapsed.
        """
        codes = [error.code for error in self.CODES]
        self.assertEqual(len(codes), len(set(codes)), codes)

    def test_every_narrowed_error_is_still_a_validation_error(self) -> None:
        """This is what keeps every existing `except ValidationError` correct."""
        for error in self.CODES:
            with self.subTest(error=error.__name__):
                self.assertTrue(issubclass(error, ValidationError))
                self.assertIsInstance(error("x"), ValidationError)

    def test_they_are_not_policy_errors(self) -> None:
        """`PolicyError` exits 3. Reclassifying into it would change exit codes."""
        for error in self.CODES:
            with self.subTest(error=error.__name__):
                self.assertNotIsInstance(error("x"), PolicyError)

    def test_details_survive_the_narrowing(self) -> None:
        raised = ConflictError("stale", details={"path": "wiki/a.md"})
        self.assertEqual(raised.details, {"path": "wiki/a.md"})


class ErrorClassificationTest(unittest.TestCase):
    """The codes as the paths that raise them actually report them.

    Asserting the class table alone would pass with every raise site still
    saying `ValidationError`, so these drive real calls.
    """

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.vault = FileVault.initialize(self.root, policy())
        self.service = BrainskitService(
            self.vault, SqliteFtsIndex(self.vault.index_path), graph=MarkdownGraph()
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def assert_code(self, code: str, call: Callable[[], object]) -> ValidationError:
        with self.assertRaises(ValidationError) as caught:
            call()
        self.assertEqual(caught.exception.code, code)
        return caught.exception

    def test_a_malformed_request_is_still_a_plain_validation_error(self) -> None:
        """The control: input validation must not have moved."""
        self.assert_code("validation_error", lambda: self.service.search(""))

    def test_reinitialising_a_vault_is_a_conflict(self) -> None:
        self.assert_code(
            "conflict", lambda: FileVault.initialize(self.root, policy())
        )

    def test_applying_against_a_moved_page_is_a_conflict(self) -> None:
        """The stale-context case: same intent, rebuilt, would succeed."""
        captured = self.service.capture(None, text="Evidence", title="Evidence")
        content_hash = captured["source"]["content_hash"]
        operation = {
            "action": "upsert",
            "kind": "concept",
            "slug": "thing",
            "title": "Thing",
            "aliases": [],
            "source_hashes": [content_hash],
            "body": f"A claim.[^source:{content_hash}]",
            "links": [],
        }
        self.service.apply({"operations": [operation]})
        error = self.assert_code(
            "conflict",
            lambda: self.service.apply(
                {
                    "proposal_id": "stale",
                    "operations": [
                        {
                            **operation,
                            "body": f"Revised.[^source:{content_hash}]",
                            "base_hash": "f" * 64,
                        }
                    ],
                }
            ),
        )
        self.assertEqual(
            [failure["code"] for failure in error.details["failures"]], ["stale_page"]
        )

    def test_a_stale_page_mixed_with_a_content_failure_is_not_a_conflict(self) -> None:
        """Re-reading fixes the version. It does not fix an uncited claim.

        Calling that a conflict would send an agent into a retry loop that
        cannot terminate.
        """
        captured = self.service.capture(None, text="Evidence", title="Evidence")
        content_hash = captured["source"]["content_hash"]
        operation = {
            "action": "upsert",
            "kind": "concept",
            "slug": "mixed",
            "title": "Mixed",
            "aliases": [],
            "source_hashes": [content_hash],
            "body": f"A claim.[^source:{content_hash}]",
            "links": [],
        }
        self.service.apply({"operations": [operation]})
        error = self.assert_code(
            "validation_error",
            lambda: self.service.apply(
                {
                    "proposal_id": "mixed",
                    "operations": [
                        {
                            **operation,
                            "body": "Revised, citing nothing at all.",
                            "base_hash": "f" * 64,
                        }
                    ],
                }
            ),
        )
        codes = {failure["code"] for failure in error.details["failures"]}
        self.assertIn("stale_page", codes)
        self.assertGreater(len(codes), 1)

    def test_reusing_a_proposal_id_for_a_new_payload_is_a_conflict(self) -> None:
        captured = self.service.capture(None, text="Evidence", title="Evidence")
        content_hash = captured["source"]["content_hash"]
        operation = {
            "action": "upsert",
            "kind": "concept",
            "slug": "reused",
            "title": "Reused",
            "aliases": [],
            "source_hashes": [content_hash],
            "body": f"First.[^source:{content_hash}]",
            "links": [],
        }
        self.service.apply({"proposal_id": "same-id", "operations": [operation]})
        self.assert_code(
            "conflict",
            lambda: self.service.apply(
                {
                    "proposal_id": "same-id",
                    "operations": [
                        {**operation, "body": f"Second.[^source:{content_hash}]"}
                    ],
                }
            ),
        )

    def test_a_missing_extractor_is_not_configured(self) -> None:
        self.assert_code("not_configured", self.service.code_build)

    def test_the_local_only_code_graph_is_a_refusal(self) -> None:
        self.assert_code(
            "refused", lambda: self.service.code_hubs(consumer="cloud")
        )

    def test_nesting_a_vault_is_a_refusal(self) -> None:
        self.assert_code(
            "refused",
            lambda: FileVault.initialize(self.root / "wiki" / "inner", policy()),
        )

    def test_an_exhausted_repair_loop_blames_the_model(self) -> None:
        """The remedy is retry or reroute, never "fix your request"."""

        class NeverValid:
            def run(self, **_: object) -> str:
                return "not json at all"

        class Specs:
            def schema(self, job: str) -> dict[str, object]:
                return {"type": "object", "required": ["answer"]}

            def render(self, job: str, variables: dict[str, object]) -> str:
                return job

        runner = JudgmentRunner(Specs(), NeverValid())  # type: ignore[arg-type]
        error = self.assert_code(
            "model_response_invalid",
            lambda: runner.run(job="ask", branches=[], variables={}),
        )
        self.assertEqual(error.details["attempts"], 3)

    def test_an_unconfigured_judgment_layer_says_so(self) -> None:
        runner = JudgmentRunner(None, None)
        self.assert_code(
            "not_configured",
            lambda: runner.run(job="ask", branches=[], variables={}),
        )


class ErrorExitCodeTest(unittest.TestCase):
    """Narrowing the code must not move any command's exit status."""

    def test_every_narrowed_error_still_exits_two(self) -> None:
        for error in (
            ValidationError,
            ConflictError,
            NotConfiguredError,
            RefusalError,
            ModelResponseError,
        ):
            with self.subTest(error=error.__name__):
                buffer = io.StringIO()
                with mock.patch.object(
                    cli, "_dispatch", side_effect=error("boom")
                ), redirect_stderr(buffer):
                    status = cli.main(["status"])
                self.assertEqual(status, 2)

    def test_a_policy_error_still_exits_three(self) -> None:
        """The one code that does carry a different status keeps it."""
        buffer = io.StringIO()
        with mock.patch.object(
            cli, "_dispatch", side_effect=PolicyError("nope")
        ), redirect_stderr(buffer):
            self.assertEqual(cli.main(["status"]), 3)

    def test_the_code_reaches_the_json_envelope(self) -> None:
        """What an agent actually reads is the serialised envelope."""
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(
            cli, "_dispatch", side_effect=ConflictError("stale", details={"a": 1})
        ), redirect_stdout(out), redirect_stderr(err):
            cli.main(["--json", "status"])
        payload = json.loads(out.getvalue() or err.getvalue())
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"]["code"], "conflict")
        self.assertEqual(payload["error"]["details"], {"a": 1})


if __name__ == "__main__":
    unittest.main()
