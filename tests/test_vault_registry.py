from __future__ import annotations

import io
import json
import os
import shutil
import stat
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any
from unittest.mock import patch

from brainkit.domain.model import NotFoundError, ValidationError
from brainkit.infrastructure.integrations import NativeIntegrations, vault_id
from brainkit.infrastructure.vault import FileVault
from brainkit.infrastructure.vaults import VaultRegistry, default_registry_path
from brainkit.interfaces import cli


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
        "taxonomy_seed": ["work", "research"],
        "novelty": {
            "duplicate_similarity_threshold": 0.9,
            "min_new_token_ratio": 0.15,
            "stale_after_days": 30,
        },
        "integrations": {
            name: {"enabled": False, "managed": False, "options": {}}
            for name in ("obsidian", "neo4j", "postgres", "web")
        },
    }


class StubService:
    """A real vault with a stubbed sync -- the loop is what is under test.

    The configuration stays real so 'disabled' is decided by the vault's own
    policy file rather than by the fake, which is the behaviour that matters.
    """

    def __init__(self, vault: FileVault, failure: Exception | None = None):
        self.vault = vault
        self.failure = failure
        self.synced: list[str] = []

    def integration_sync(self, name: str) -> dict[str, Any]:
        if self.failure is not None:
            raise self.failure
        self.synced.append(name)
        return {"integration": name, "state": "synced", "nodes": 1, "edges": 0}


class RegistryFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="vault-registry-")
        self.root = Path(self.temporary.name).resolve()
        self.config_home = self.root / "config"
        self.environment = patch.dict(
            os.environ, {"XDG_CONFIG_HOME": str(self.config_home)}
        )
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.addCleanup(self.temporary.cleanup)
        self.services: dict[Path, StubService] = {}
        self.original_create_service = cli.create_service

    def make_vault(self, name: str, *, enable: str | None = None) -> Path:
        root = (self.root / name).resolve()
        vault = FileVault.initialize(root, policy())
        if enable is not None:
            NativeIntegrations(vault).configure(
                enable,
                enabled=True,
                managed=False,
                options={
                    "dsn_env": "BRAINKIT_TEST_PG_DSN",
                    "uri": "bolt://127.0.0.1:7687",
                    "user": "neo4j",
                    "password_env": "BRAINKIT_TEST_NEO4J_PASSWORD",
                    "path": str(self.root / "obsidian"),
                    "consumer": "local",
                },
            )
        return root

    def stub(self, root: Path, failure: Exception | None = None) -> StubService:
        service = StubService(FileVault(root), failure)
        self.services[root] = service
        return service

    def use_stubs(self) -> None:
        """Serve stubs for prepared vaults and the real thing for anything else."""

        def factory(vault: str | None) -> Any:
            root = Path(str(vault)).expanduser().resolve()
            prepared = self.services.get(root)
            return prepared if prepared else self.original_create_service(vault)

        patched = patch.object(cli, "create_service", factory)
        patched.start()
        self.addCleanup(patched.stop)

    def run_cli(self, argv: list[str]) -> tuple[int, dict[str, Any]]:
        stream = io.StringIO()
        with redirect_stdout(stream):
            code = cli.main(["--json", *argv])
        return code, json.loads(stream.getvalue())

    def result(self, argv: list[str]) -> dict[str, Any]:
        code, payload = self.run_cli(argv)
        self.assertEqual(code, 0, payload)
        self.assertTrue(payload["ok"], payload)
        result = payload["result"]
        assert isinstance(result, dict)
        return result


class RegistryRoundTripTest(RegistryFixture):
    """register -> list -> forget, the loop an operator actually runs."""

    def test_a_registered_vault_is_listed_and_then_forgotten(self) -> None:
        first = self.make_vault("alpha")
        second = self.make_vault("beta")
        self.result(["vaults", "register", str(first)])
        self.result(["vaults", "register", str(second), "--label", "beta-app"])

        listed = self.result(["vaults", "list"])
        self.assertEqual(listed["count"], 2)
        self.assertEqual(
            sorted(entry["path"] for entry in listed["vaults"]),
            sorted([str(first), str(second)]),
        )
        self.assertEqual(
            {entry["path"]: entry["label"] for entry in listed["vaults"]},
            {str(first): "alpha", str(second): "beta-app"},
        )
        self.assertTrue(all(entry["exists"] for entry in listed["vaults"]))

        forgotten = self.result(["vaults", "forget", "beta-app"])
        self.assertEqual(forgotten["vault"], str(second))
        self.assertEqual(self.result(["vaults", "list"])["count"], 1)

    def test_listing_an_empty_registry_reports_nothing_rather_than_failing(self) -> None:
        listed = self.result(["vaults", "list"])
        self.assertEqual(listed["count"], 0)
        self.assertEqual(listed["vaults"], [])

    def test_forget_removes_the_entry_and_leaves_the_vault_untouched(self) -> None:
        root = self.make_vault("keepme")
        before = sorted(path.name for path in root.iterdir())
        self.result(["vaults", "register", str(root)])
        self.result(["vaults", "forget", str(root)])
        self.assertEqual(self.result(["vaults", "list"])["count"], 0)
        self.assertTrue((root / ".brain" / "config.json").is_file())
        self.assertEqual(sorted(path.name for path in root.iterdir()), before)

    def test_forget_refuses_a_selector_that_matches_nothing(self) -> None:
        registry = VaultRegistry(self.config_home / "brainkit" / "vaults.json")
        with self.assertRaises(NotFoundError):
            registry.forget("no-such-label")

    def test_forget_refuses_an_ambiguous_label_instead_of_guessing(self) -> None:
        first = self.make_vault("one/docs")
        second = self.make_vault("two/docs")
        self.result(["vaults", "register", str(first)])
        self.result(["vaults", "register", str(second)])
        registry = VaultRegistry(self.config_home / "brainkit" / "vaults.json")
        with self.assertRaises(ValidationError) as raised:
            registry.forget("docs")
        self.assertEqual(len(raised.exception.details["paths"]), 2)
        # The path still resolves it, so the operator is never stuck.
        registry.forget(str(second))
        self.assertEqual(len(registry.entries()), 1)


class RegistrationContractTest(RegistryFixture):
    def test_registering_twice_is_a_no_op_rather_than_a_duplicate(self) -> None:
        root = self.make_vault("twice")
        first = self.result(["vaults", "register", str(root)])
        second = self.result(["vaults", "register", str(root)])
        self.assertTrue(first["registered"])
        self.assertFalse(second["registered"])
        self.assertEqual(self.result(["vaults", "list"])["count"], 1)

    def test_registering_again_with_a_label_relabels_without_duplicating(self) -> None:
        root = self.make_vault("relabel")
        self.result(["vaults", "register", str(root)])
        again = self.result(["vaults", "register", str(root), "--label", "renamed"])
        self.assertFalse(again["registered"])
        self.assertTrue(again["relabelled"])
        listed = self.result(["vaults", "list"])
        self.assertEqual(listed["count"], 1)
        self.assertEqual(listed["vaults"][0]["label"], "renamed")

    def test_a_path_that_is_not_a_vault_is_refused(self) -> None:
        stranger = self.root / "not-a-vault"
        stranger.mkdir()
        code, payload = self.run_cli(["vaults", "register", str(stranger)])
        self.assertEqual(code, 2)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"]["code"], "validation_error")
        self.assertIn("Not a brainkit vault", payload["error"]["message"])
        self.assertFalse((self.config_home / "brainkit" / "vaults.json").exists())

    def test_a_missing_path_is_refused_rather_than_registered_blindly(self) -> None:
        code, payload = self.run_cli(
            ["vaults", "register", str(self.root / "never-existed")]
        )
        self.assertEqual(code, 2)
        self.assertFalse(payload["ok"])

    def test_the_stored_path_is_the_one_the_shared_store_hashes(self) -> None:
        """A registry entry that normalised differently would report a
        vault_id no row in Postgres carries."""
        root = self.make_vault("identity")
        registered = self.result(
            ["vaults", "register", str(root / "." / "..") + f"/{root.name}"]
        )
        self.assertEqual(registered["vault"], str(root))
        self.assertEqual(registered["vault_id"], vault_id(FileVault(root).root))
        listed = self.result(["vaults", "list"])
        self.assertEqual(listed["vaults"][0]["vault_id"], vault_id(root))

    def test_register_without_a_path_discovers_the_vault_from_the_cwd(self) -> None:
        root = self.make_vault("discovered")
        self.addCleanup(os.chdir, Path.cwd())
        os.chdir(root / "wiki")
        registered = self.result(["vaults", "register"])
        self.assertEqual(registered["vault"], str(root))


class RegistryFilePermissionsTest(RegistryFixture):
    def test_the_file_is_private_and_so_is_its_directory(self) -> None:
        root = self.make_vault("private")
        self.result(["vaults", "register", str(root)])
        path = self.config_home / "brainkit" / "vaults.json"
        self.assertTrue(path.is_file())
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)

    def test_a_widened_directory_is_tightened_on_the_next_write(self) -> None:
        first = self.make_vault("widen-one")
        second = self.make_vault("widen-two")
        self.result(["vaults", "register", str(first)])
        directory = self.config_home / "brainkit"
        # Deliberately permissive: the widened directory is the precondition,
        # and the assertion below is that the next write closes it again.
        os.chmod(directory, 0o755)  # noqa: S103
        self.result(["vaults", "register", str(second)])
        self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o700)

    def test_the_registry_stores_paths_and_labels_and_nothing_else(self) -> None:
        root = self.make_vault("no-secrets", enable="postgres")
        self.result(["vaults", "register", str(root)])
        raw = json.loads(
            (self.config_home / "brainkit" / "vaults.json").read_text(encoding="utf-8")
        )
        self.assertEqual(raw["version"], 1)
        for entry in raw["vaults"]:
            self.assertEqual(sorted(entry), ["label", "path"])


class RegistryLocationTest(unittest.TestCase):
    def test_xdg_config_home_is_honoured_when_it_is_absolute(self) -> None:
        self.assertEqual(
            default_registry_path({"XDG_CONFIG_HOME": "/opt/cfg"}),
            Path("/opt/cfg/brainkit/vaults.json"),
        )

    def test_an_unset_or_relative_value_falls_back_to_the_home_default(self) -> None:
        expected = Path.home() / ".config" / "brainkit" / "vaults.json"
        for environment in ({}, {"XDG_CONFIG_HOME": ""}, {"XDG_CONFIG_HOME": "cfg"}):
            with self.subTest(environment=environment):
                self.assertEqual(default_registry_path(environment), expected)


class MissingVaultTest(RegistryFixture):
    """A registry outlives the projects in it, so staleness must be reportable."""

    def test_list_reports_a_deleted_vault_instead_of_crashing(self) -> None:
        alive = self.make_vault("alive")
        doomed = self.make_vault("doomed")
        self.result(["vaults", "register", str(alive)])
        self.result(["vaults", "register", str(doomed)])
        shutil.rmtree(doomed)

        listed = self.result(["vaults", "list"])
        self.assertEqual(listed["count"], 2)
        existence = {entry["path"]: entry["exists"] for entry in listed["vaults"]}
        self.assertTrue(existence[str(alive)])
        self.assertFalse(existence[str(doomed)])
        # The id is still reported, so the operator can find and clear the
        # abandoned rows the vault left in a shared store.
        self.assertTrue(all(entry["vault_id"] for entry in listed["vaults"]))

    def test_a_deleted_vault_fails_its_sync_and_says_why(self) -> None:
        doomed = self.make_vault("gone", enable="postgres")
        self.result(["vaults", "register", str(doomed)])
        shutil.rmtree(doomed)
        code, payload = self.run_cli(["vaults", "sync"])
        self.assertEqual(code, 1)
        result = payload["result"]
        self.assertEqual(result["failed"], 1)
        entry = result["vaults"][0]
        self.assertEqual(entry["status"], "failed")
        self.assertEqual(entry["code"], "not_found")

    def test_forget_clears_a_stale_entry_for_a_directory_that_is_gone(self) -> None:
        doomed = self.make_vault("stale")
        self.result(["vaults", "register", str(doomed)])
        shutil.rmtree(doomed)
        self.result(["vaults", "forget", str(doomed)])
        self.assertEqual(self.result(["vaults", "list"])["count"], 0)


class MultiVaultSyncTest(RegistryFixture):
    def register_all(self, *roots: Path) -> None:
        for root in roots:
            self.result(["vaults", "register", str(root)])

    def test_every_registered_vault_is_synced_into_the_shared_target(self) -> None:
        first = self.make_vault("sync-a", enable="postgres")
        second = self.make_vault("sync-b", enable="postgres")
        self.register_all(first, second)
        services = [self.stub(first), self.stub(second)]
        self.use_stubs()

        result = self.result(["vaults", "sync"])
        self.assertEqual(result["target"], "postgres")
        self.assertEqual((result["count"], result["ok"]), (2, 2))
        self.assertEqual(
            [service.synced for service in services], [["postgres"], ["postgres"]]
        )
        self.assertEqual(
            {entry["vault_id"] for entry in result["vaults"]},
            {vault_id(first), vault_id(second)},
        )

    def test_one_vault_failing_does_not_stop_the_others(self) -> None:
        """The outcome the per-vault isolation exists for."""
        first = self.make_vault("iso-a", enable="postgres")
        broken = self.make_vault("iso-b", enable="postgres")
        third = self.make_vault("iso-c", enable="postgres")
        self.register_all(first, broken, third)
        before, after = self.stub(first), self.stub(third)
        self.stub(broken, ValidationError("PostgreSQL integration could not connect"))
        self.use_stubs()

        code, payload = self.run_cli(["vaults", "sync"])
        result = payload["result"]
        self.assertEqual((result["ok"], result["failed"]), (2, 1))
        outcomes = {entry["vault"]: entry["status"] for entry in result["vaults"]}
        self.assertEqual(outcomes[str(first)], "ok")
        self.assertEqual(outcomes[str(broken)], "failed")
        self.assertEqual(outcomes[str(third)], "ok")
        # Both healthy vaults ran, including the one queued behind the failure.
        self.assertEqual(before.synced, ["postgres"])
        self.assertEqual(after.synced, ["postgres"])
        self.assertEqual(code, 1)

    def test_an_unmodelled_adapter_error_fails_one_vault_not_the_run(self) -> None:
        first = self.make_vault("raw-a", enable="postgres")
        broken = self.make_vault("raw-b", enable="postgres")
        self.register_all(first, broken)
        healthy = self.stub(first)
        self.stub(broken, RuntimeError("driver exploded"))
        self.use_stubs()

        code, payload = self.run_cli(["vaults", "sync"])
        result = payload["result"]
        self.assertEqual((result["ok"], result["failed"]), (1, 1))
        self.assertEqual(healthy.synced, ["postgres"])
        failure = next(
            entry for entry in result["vaults"] if entry["status"] == "failed"
        )
        self.assertEqual(failure["code"], "internal_error")
        self.assertIn("RuntimeError", failure["reason"])
        self.assertEqual(code, 1)

    def test_a_vault_with_the_integration_disabled_is_skipped_not_failed(self) -> None:
        enabled = self.make_vault("opted-in", enable="postgres")
        opted_out = self.make_vault("opted-out")
        self.register_all(enabled, opted_out)
        service = self.stub(enabled)
        untouched = self.stub(opted_out)
        self.use_stubs()

        code, payload = self.run_cli(["vaults", "sync"])
        result = payload["result"]
        self.assertEqual((result["ok"], result["skipped"], result["failed"]), (1, 1, 0))
        skipped = next(
            entry for entry in result["vaults"] if entry["status"] == "skipped"
        )
        self.assertEqual(skipped["vault"], str(opted_out))
        self.assertIn("not enabled", skipped["reason"])
        self.assertEqual(untouched.synced, [])
        self.assertEqual(service.synced, ["postgres"])
        # Skipped is not a failure: the vault's own policy declined, correctly.
        self.assertEqual(code, 0)

    def test_a_registry_where_every_vault_skips_still_exits_zero(self) -> None:
        self.register_all(self.make_vault("all-out-a"), self.make_vault("all-out-b"))
        code, payload = self.run_cli(["vaults", "sync"])
        self.assertEqual(code, 0)
        self.assertEqual(payload["result"]["skipped"], 2)

    def test_the_target_selects_which_integration_each_vault_syncs(self) -> None:
        root = self.make_vault("targeted", enable="neo4j")
        self.register_all(root)
        service = self.stub(root)
        self.use_stubs()
        result = self.result(["vaults", "sync", "--target", "neo4j"])
        self.assertEqual(result["target"], "neo4j")
        self.assertEqual(service.synced, ["neo4j"])
        # The same vault has no Postgres policy, so that target skips it.
        self.assertEqual(self.result(["vaults", "sync"])["skipped"], 1)

    def test_consumer_is_refused_because_each_vault_declares_its_own(self) -> None:
        root = self.make_vault("boundary", enable="postgres")
        self.register_all(root)
        service = self.stub(root)
        self.use_stubs()
        code, payload = self.run_cli(["vaults", "sync", "--consumer", "cloud"])
        self.assertEqual(code, 2)
        self.assertEqual(payload["error"]["code"], "validation_error")
        self.assertEqual(payload["error"]["details"]["consumer"], "cloud")
        self.assertEqual(service.synced, [])

    def test_syncing_an_empty_registry_succeeds_and_says_so(self) -> None:
        code, payload = self.run_cli(["vaults", "sync"])
        self.assertEqual(code, 0)
        self.assertEqual(payload["result"]["count"], 0)


class RegistryIntegrityTest(RegistryFixture):
    def test_a_corrupt_registry_is_reported_rather_than_silently_emptied(self) -> None:
        path = self.config_home / "brainkit" / "vaults.json"
        path.parent.mkdir(parents=True)
        path.write_text("{not json", encoding="utf-8")
        code, payload = self.run_cli(["vaults", "list"])
        self.assertEqual(code, 2)
        self.assertIn("not valid JSON", payload["error"]["message"])

    def test_an_entry_without_a_path_is_refused(self) -> None:
        path = self.config_home / "brainkit" / "vaults.json"
        path.parent.mkdir(parents=True)
        path.write_text(
            json.dumps({"version": 1, "vaults": [{"label": "orphan"}]}),
            encoding="utf-8",
        )
        with self.assertRaises(ValidationError):
            VaultRegistry(path).entries()


if __name__ == "__main__":
    unittest.main()
