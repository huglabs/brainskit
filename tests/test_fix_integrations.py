from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from inspect import signature
from pathlib import Path
from typing import Any
from unittest.mock import patch

from brainkit.domain.model import BrainkitError, ValidationError
from brainkit.infrastructure import integrations as integrations_module
from brainkit.infrastructure.integrations import (
    READY_STABLE_SECONDS,
    READY_TIMEOUT_SECONDS,
    NativeIntegrations,
    _busy_ports,
    _container_drift,
    _container_name,
    _container_status,
    _managed_container_spec,
    _postgres_secrets,
    _postgres_target,
    _publish_arguments,
    _published_ports,
    _ready_timeout,
    _redact_secrets,
    _vendor_boundary,
    _vendor_stage,
    _wait_database_ready,
)
from brainkit.infrastructure.vault import FileVault

NEO4J_PASSWORD = "n30-canary-password"
POSTGRES_PASSWORD = "pg-canary-password"
EMBEDDED_PASSWORD = "dsn-canary-password"


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
        },
        "providers": {"ollama": {"base_url": "http://127.0.0.1:11434"}},
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
        "taxonomy_seed": ["work", "research"],
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


def graph(*, canary: str | None = None) -> dict:
    nodes = [{"id": "page:wiki/index.md", "label": "index", "kind": "wiki", "path": "wiki/index.md"}]
    if canary:
        nodes.append(
            {"id": f"raw:{canary}", "label": canary, "kind": "source", "path": f"raw/{canary}.md"}
        )
    return {"nodes": nodes, "edges": []}


def docker_available() -> bool:
    if not shutil.which("docker"):
        return False
    try:
        result = subprocess.run(
            ["docker", "info", "--format", "{{.ServerVersion}}"],
            check=False,
            capture_output=True,
            text=True,
            timeout=20,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


HAS_DOCKER = docker_available()


class FakeNeo4jDriver:
    """Minimal stand-in for neo4j.Driver used to raise vendor failures."""

    def __init__(self, *, connect_error=None, session_error=None):
        self.connect_error = connect_error
        self.session_error = session_error
        self.closed = False

    def verify_connectivity(self) -> None:
        if self.connect_error is not None:
            raise self.connect_error

    def session(self, **_: object) -> FakeNeo4jDriver:
        return self

    def __enter__(self) -> FakeNeo4jDriver:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def run(self, *_: object, **__: object) -> FakeNeo4jDriver:
        if self.session_error is not None:
            raise self.session_error
        return self

    def consume(self) -> None:
        return None

    def execute_write(self, *_: object, **__: object) -> None:
        if self.session_error is not None:
            raise self.session_error

    def close(self) -> None:
        self.closed = True


def fake_neo4j_module() -> dict[str, types.ModuleType]:
    """A single fake neo4j package; its exception classes stay identity-stable."""
    exceptions = types.ModuleType("neo4j.exceptions")

    class DriverError(Exception):
        pass

    class Neo4jError(Exception):
        code = ""

    class ServiceUnavailable(DriverError):
        pass

    class AuthError(Neo4jError):
        code = "Neo.ClientError.Security.Unauthorized"

    class ClientError(Neo4jError):
        code = "Neo.ClientError.Statement.SyntaxError"

    exceptions.DriverError = DriverError  # type: ignore[attr-defined]
    exceptions.Neo4jError = Neo4jError  # type: ignore[attr-defined]
    exceptions.ServiceUnavailable = ServiceUnavailable  # type: ignore[attr-defined]
    exceptions.AuthError = AuthError  # type: ignore[attr-defined]
    exceptions.ClientError = ClientError  # type: ignore[attr-defined]
    module = types.ModuleType("neo4j")
    module.exceptions = exceptions  # type: ignore[attr-defined]
    module.driver_instance = None  # type: ignore[attr-defined]
    module.GraphDatabase = types.SimpleNamespace(  # type: ignore[attr-defined]
        driver=lambda *_, **__: module.driver_instance  # type: ignore[attr-defined]
    )
    return {"neo4j": module, "neo4j.exceptions": exceptions}


class FakePostgresConnection:
    def __init__(self, error: BaseException | None):
        self.error = error

    def __enter__(self) -> FakePostgresConnection:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def cursor(self) -> FakePostgresConnection:
        return self

    def execute(self, *_: object, **__: object) -> None:
        if self.error is not None:
            raise self.error

    def executemany(self, *_: object, **__: object) -> None:
        if self.error is not None:
            raise self.error


def fake_psycopg_module() -> types.ModuleType:
    """A single fake psycopg module; its exception classes stay identity-stable."""
    module = types.ModuleType("psycopg")

    class Error(Exception):
        sqlstate: str | None = None

    class OperationalError(Error):
        pass

    class ProgrammingError(Error):
        sqlstate = "42601"

    failures: dict[str, BaseException | None] = {"connect": None, "query": None}

    def connect(_: str) -> FakePostgresConnection:
        if failures["connect"] is not None:
            raise failures["connect"]
        return FakePostgresConnection(failures["query"])

    module.Error = Error  # type: ignore[attr-defined]
    module.OperationalError = OperationalError  # type: ignore[attr-defined]
    module.ProgrammingError = ProgrammingError  # type: ignore[attr-defined]
    module.failures = failures  # type: ignore[attr-defined]
    module.connect = connect  # type: ignore[attr-defined]
    return module


class VaultFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="agent-a-")
        self.root = Path(self.temporary.name)
        self.vault = FileVault.initialize(self.root, policy())
        self.integrations = NativeIntegrations(self.vault)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def payload(self, error: BrainkitError) -> str:
        return json.dumps(
            {"message": str(error), "details": error.details}, ensure_ascii=False
        )


class VendorErrorContractTest(VaultFixture):
    """Defect 1: vendor driver exceptions must never escape the error contract."""

    def configure_neo4j(self) -> None:
        self.integrations.configure(
            "neo4j",
            enabled=True,
            managed=False,
            options={
                "uri": "bolt://127.0.0.1:7691",
                "user": "neo4j",
                "password_env": "BRAINKIT_TEST_NEO4J_PASSWORD",
                "database": "neo4j",
                "consumer": "local",
            },
        )

    def configure_postgres(self, *, managed: bool = True) -> None:
        options: dict[str, Any] = {"consumer": "local", "schema": "brainkit"}
        if managed:
            options.update(
                {
                    "password_env": "BRAINKIT_TEST_PG_PASSWORD",
                    "user": "brainkit",
                    "database": "brainkit",
                    "port": 5461,
                }
            )
        else:
            options["dsn_env"] = "BRAINKIT_TEST_PG_DSN"
        self.integrations.configure(
            "postgres", enabled=True, managed=managed, options=options
        )

    def sync_neo4j(self, modules: dict[str, types.ModuleType]) -> BrainkitError:
        self.configure_neo4j()
        with patch.dict(os.environ, {"BRAINKIT_TEST_NEO4J_PASSWORD": NEO4J_PASSWORD}):
            with patch.dict(sys.modules, modules):
                with self.assertRaises(BrainkitError) as caught:
                    self.integrations.sync("neo4j", graph())
        return caught.exception

    def neo4j_modules(self) -> tuple[dict[str, types.ModuleType], FakeNeo4jDriver]:
        modules = fake_neo4j_module()
        driver = FakeNeo4jDriver()
        modules["neo4j"].driver_instance = driver  # type: ignore[attr-defined]
        return modules, driver

    def sync_postgres(
        self, module: types.ModuleType, *, managed: bool = True
    ) -> BrainkitError:
        self.configure_postgres(managed=managed)
        environment = {
            "BRAINKIT_TEST_PG_PASSWORD": POSTGRES_PASSWORD,
            "BRAINKIT_TEST_PG_DSN": (
                f"postgresql://brainkit:{EMBEDDED_PASSWORD}@127.0.0.1:5999/brainkit"
            ),
        }
        with patch.dict(os.environ, environment):
            with patch.dict(sys.modules, {"psycopg": module}):
                with self.assertRaises(BrainkitError) as caught:
                    self.integrations.sync("postgres", graph())
        return caught.exception

    def test_neo4j_connection_failure_becomes_a_brainkit_error(self) -> None:
        modules, driver = self.neo4j_modules()
        driver.connect_error = modules["neo4j.exceptions"].ServiceUnavailable(  # type: ignore[attr-defined]
            "Couldn't connect to 127.0.0.1:7691"
        )
        error = self.sync_neo4j(modules)
        self.assertIsInstance(error, ValidationError)
        self.assertEqual(error.details["integration"], "neo4j")
        self.assertEqual(error.details["stage"], "connection")
        self.assertEqual(error.details["database"], "neo4j")
        self.assertEqual(error.details["uri"], "bolt://127.0.0.1:7691")
        self.assertIn("could not reach", str(error))

    def test_neo4j_authentication_failure_is_classified_and_redacted(self) -> None:
        modules, driver = self.neo4j_modules()
        driver.connect_error = modules["neo4j.exceptions"].AuthError(  # type: ignore[attr-defined]
            f"The client is unauthorized: {NEO4J_PASSWORD}"
        )
        error = self.sync_neo4j(modules)
        self.assertEqual(error.details["stage"], "authentication")
        self.assertIn("rejected the configured credentials", str(error))
        self.assertNotIn(NEO4J_PASSWORD, self.payload(error))

    def test_neo4j_transaction_failure_is_translated(self) -> None:
        modules, driver = self.neo4j_modules()
        driver.session_error = modules["neo4j.exceptions"].ClientError(  # type: ignore[attr-defined]
            "Invalid Cypher"
        )
        error = self.sync_neo4j(modules)
        self.assertEqual(error.details["stage"], "query")
        self.assertEqual(error.details["driver_error"], "ClientError")

    def test_neo4j_driver_is_closed_even_when_the_sync_fails(self) -> None:
        modules, driver = self.neo4j_modules()
        driver.session_error = modules["neo4j.exceptions"].ClientError("boom")  # type: ignore[attr-defined]
        self.sync_neo4j(modules)
        self.assertTrue(driver.closed)

    def test_neo4j_missing_driver_keeps_its_install_hint(self) -> None:
        self.configure_neo4j()
        with patch.dict(os.environ, {"BRAINKIT_TEST_NEO4J_PASSWORD": NEO4J_PASSWORD}):
            with patch.dict(sys.modules, {"neo4j": None}):
                with self.assertRaises(ValidationError) as caught:
                    self.integrations.sync("neo4j", graph())
        self.assertEqual(str(caught.exception), "Neo4j sync requires the official driver")
        self.assertEqual(caught.exception.details, {"hint": "Install brainkit[neo4j]"})

    def test_postgres_connection_failure_becomes_a_brainkit_error(self) -> None:
        module = fake_psycopg_module()
        module.failures["connect"] = module.OperationalError(  # type: ignore[attr-defined]
            'connection to server at "127.0.0.1", port 5461 failed: Connection refused'
        )
        error = self.sync_postgres(module)
        self.assertIsInstance(error, ValidationError)
        self.assertEqual(error.details["integration"], "postgres")
        self.assertEqual(error.details["stage"], "connection")
        self.assertEqual(error.details["host"], "127.0.0.1")
        self.assertEqual(error.details["port"], 5461)
        self.assertEqual(error.details["database"], "brainkit")

    def test_postgres_authentication_failure_never_echoes_the_password(self) -> None:
        module = fake_psycopg_module()
        module.failures["connect"] = module.OperationalError(  # type: ignore[attr-defined]
            'FATAL:  password authentication failed for user "brainkit"'
        )
        error = self.sync_postgres(module)
        self.assertEqual(error.details["stage"], "authentication")
        self.assertNotIn(POSTGRES_PASSWORD, self.payload(error))

    def test_postgres_error_never_echoes_a_dsn_carrying_a_password(self) -> None:
        module = fake_psycopg_module()
        module.failures["connect"] = module.OperationalError(  # type: ignore[attr-defined]
            "connection failed for "
            f"postgresql://brainkit:{EMBEDDED_PASSWORD}@127.0.0.1:5999/brainkit"
        )
        error = self.sync_postgres(module, managed=False)
        payload = self.payload(error)
        self.assertNotIn(EMBEDDED_PASSWORD, payload)
        self.assertIn("***", payload)

    def test_postgres_query_failure_is_translated(self) -> None:
        module = fake_psycopg_module()
        module.failures["query"] = module.ProgrammingError(  # type: ignore[attr-defined]
            "syntax error at or near"
        )
        error = self.sync_postgres(module)
        self.assertEqual(error.details["stage"], "query")
        self.assertEqual(error.details["driver_error"], "ProgrammingError")
        self.assertEqual(error.details["schema"], "brainkit")

    def test_postgres_missing_driver_keeps_its_install_hint(self) -> None:
        self.configure_postgres()
        with patch.dict(os.environ, {"BRAINKIT_TEST_PG_PASSWORD": POSTGRES_PASSWORD}):
            with patch.dict(sys.modules, {"psycopg": None}):
                with self.assertRaises(ValidationError) as caught:
                    self.integrations.sync("postgres", graph())
        self.assertEqual(str(caught.exception), "PostgreSQL sync requires psycopg")
        self.assertEqual(caught.exception.details, {"hint": "Install brainkit[postgres]"})

    def test_boundary_lets_brainkit_errors_through_unchanged(self) -> None:
        original = ValidationError("Integration is disabled", details={"integration": "neo4j"})
        with self.assertRaises(ValidationError) as caught:
            with _vendor_boundary(
                "neo4j", errors=(Exception,), details={"integration": "neo4j"}
            ):
                raise original
        self.assertIs(caught.exception, original)

    def test_stage_classification_covers_both_drivers(self) -> None:
        class ServiceUnavailable(Exception):
            pass

        class AuthError(Exception):
            code = "Neo.ClientError.Security.Unauthorized"

        class OperationalError(Exception):
            sqlstate = "28P01"

        class DataError(Exception):
            sqlstate = "22P02"

        self.assertEqual(_vendor_stage(ServiceUnavailable("refused")), "connection")
        self.assertEqual(_vendor_stage(AuthError("nope")), "authentication")
        self.assertEqual(_vendor_stage(OperationalError("bad password")), "authentication")
        self.assertEqual(_vendor_stage(DataError("bad literal")), "query")
        self.assertEqual(_vendor_stage(ConnectionResetError("reset")), "connection")

    def test_redaction_removes_known_secrets_and_embedded_credentials(self) -> None:
        text = f"failed for postgresql://brainkit:{EMBEDDED_PASSWORD}@host:5432/db"
        self.assertNotIn(EMBEDDED_PASSWORD, _redact_secrets(text, ()))
        self.assertNotIn(
            NEO4J_PASSWORD, _redact_secrets(f"auth {NEO4J_PASSWORD}", (NEO4J_PASSWORD,))
        )

    def test_postgres_target_and_secrets_are_derived_without_the_password(self) -> None:
        dsn = f"postgresql://brainkit:{EMBEDDED_PASSWORD}@db.internal:6543/graph"
        target = _postgres_target(dsn)
        self.assertEqual(target["host"], "db.internal")
        self.assertEqual(target["port"], 6543)
        self.assertEqual(target["database"], "graph")
        self.assertNotIn(EMBEDDED_PASSWORD, json.dumps(target))
        self.configure_postgres(managed=False)
        stored = self.vault.config().integrations["postgres"]
        self.assertIn(EMBEDDED_PASSWORD, _postgres_secrets(stored, dsn))

    def test_postgres_target_ignores_non_uri_dsn_forms(self) -> None:
        self.assertEqual(_postgres_target("host=127.0.0.1 port=5432 dbname=brainkit"), {})


class ManagedContainerReconciliationTest(VaultFixture):
    """Defect 2: a managed container must be reconciled against the policy."""

    def postgres_policy(self, port: int = 5461):
        self.integrations.configure(
            "postgres",
            enabled=True,
            managed=True,
            options={
                "password_env": "BRAINKIT_TEST_PG_PASSWORD",
                "user": "brainkit",
                "database": "brainkit",
                "schema": "brainkit",
                "port": port,
                "consumer": "local",
            },
        )
        return self.vault.config().integrations["postgres"]

    @staticmethod
    def inspected(status: str, image: str, host_port: str) -> dict:
        return {
            "State": {"Status": status, "ExitCode": 0},
            "Config": {"Image": image},
            "HostConfig": {
                "PortBindings": {
                    "5432/tcp": [{"HostIp": "", "HostPort": host_port}]
                }
            },
        }

    def test_matching_running_container_reports_no_drift(self) -> None:
        desired = _managed_container_spec("postgres", self.postgres_policy())
        payload = self.inspected("running", "postgres:17", "5461")
        self.assertEqual(_container_drift(payload, desired), [])
        self.assertEqual(_container_status(payload), "running")

    def test_created_container_is_reported_as_unstartable(self) -> None:
        desired = _managed_container_spec("postgres", self.postgres_policy())
        drift = _container_drift(self.inspected("created", "postgres:17", "5461"), desired)
        self.assertTrue(any("created" in reason for reason in drift))

    def test_stale_port_binding_is_detected_after_reconfigure(self) -> None:
        desired = _managed_container_spec("postgres", self.postgres_policy(port=5461))
        drift = _container_drift(self.inspected("exited", "postgres:17", "5462"), desired)
        self.assertEqual(len(drift), 1)
        self.assertIn("5462->5432/tcp", drift[0])
        self.assertIn("5461->5432/tcp", drift[0])

    def test_image_change_is_detected(self) -> None:
        desired = _managed_container_spec("postgres", self.postgres_policy())
        drift = _container_drift(self.inspected("exited", "postgres:16", "5461"), desired)
        self.assertTrue(any("postgres:16" in reason for reason in drift))

    def test_missing_container_reports_no_drift(self) -> None:
        desired = _managed_container_spec("postgres", self.postgres_policy())
        self.assertEqual(_container_drift(None, desired), [])

    def test_neo4j_spec_publishes_both_configured_ports(self) -> None:
        self.integrations.configure(
            "neo4j",
            enabled=True,
            managed=True,
            options={
                "uri": "bolt://127.0.0.1:7691",
                "user": "neo4j",
                "password_env": "BRAINKIT_TEST_NEO4J_PASSWORD",
                "http_port": 7481,
                "bolt_port": 7691,
                "consumer": "local",
            },
        )
        desired = _managed_container_spec(
            "neo4j", self.vault.config().integrations["neo4j"]
        )
        self.assertEqual(desired["image"], "neo4j:5-community")
        self.assertEqual(
            _publish_arguments(desired), ["7481:7474/tcp", "7691:7687/tcp"]
        )

    def test_published_ports_tolerates_missing_bindings(self) -> None:
        self.assertEqual(_published_ports({}), {})
        self.assertEqual(_published_ports({"HostConfig": {"PortBindings": None}}), {})

    def test_busy_ports_reports_nothing_for_a_free_port(self) -> None:
        desired = {"ports": {"5432/tcp": ["1"]}}
        self.assertEqual(_busy_ports(desired), [])

    @unittest.skipUnless(HAS_DOCKER, "Docker is not available")
    def test_stale_container_is_recreated_and_keeps_its_durable_volume(self) -> None:
        container = _container_name(self.vault.root, "postgres", {})
        policy_value = self.postgres_policy()
        data = self.vault.root / ".brain" / "services" / "postgres" / "data"
        self.addCleanup(self.remove_container, container)
        with patch.dict(os.environ, {"BRAINKIT_TEST_PG_PASSWORD": POSTGRES_PASSWORD}):
            first = self.integrations.up("postgres")
            self.assertEqual(first["state"], "ready")
            sentinel = data / "agent-a-sentinel.txt"
            sentinel.write_text("durable", encoding="utf-8")
            self.remove_container(container)
            self.create_stale_container(container, data)
            self.assertEqual(self.container_status(container), "created")
            second = self.integrations.up("postgres")
        self.assertEqual(second["state"], "ready")
        self.assertTrue(second["recreated"])
        self.assertTrue(any("5462" in reason for reason in second["reconciled"]))
        self.assertEqual(self.container_status(container), "running")
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "durable")
        self.assertEqual(
            _published_ports(self.docker_json(container)),
            {"5432/tcp": [str(policy_value.options["port"])]},
        )

    @staticmethod
    def create_stale_container(container: str, data: Path) -> None:
        subprocess.run(
            [
                "docker", "create", "--name", container,
                "-p", "5462:5432",
                "-v", f"{data}:/var/lib/postgresql/data",
                "-e", f"POSTGRES_PASSWORD={POSTGRES_PASSWORD}",
                "postgres:17",
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=60,
        )

    @staticmethod
    def remove_container(container: str) -> None:
        subprocess.run(
            ["docker", "rm", "--force", container],
            check=False,
            capture_output=True,
            text=True,
            timeout=60,
        )

    @staticmethod
    def docker_json(container: str) -> dict:
        result = subprocess.run(
            ["docker", "inspect", container],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
        payload = json.loads(result.stdout)
        return dict(payload[0])

    def container_status(self, container: str) -> str:
        return _container_status(self.docker_json(container))


class ObsidianExportBoundaryTest(VaultFixture):
    """Defect 3: the export must serialize the filtered graph it was handed."""

    def configure(self, destination: Path, **options: object) -> None:
        self.integrations.configure(
            "obsidian",
            enabled=True,
            managed=False,
            options={"path": str(destination), "subdirectory": "brainkit", **options},
        )

    def destination(self) -> Path:
        target = Path(tempfile.mkdtemp(prefix="agent-a-obsidian-"))
        self.addCleanup(shutil.rmtree, target, True)
        return target

    def test_export_serializes_the_supplied_graph_not_the_vault_file(self) -> None:
        target = self.destination()
        self.vault.write_generated(
            "graph/graph.json",
            json.dumps(graph(canary="neverIngestCanary"), ensure_ascii=False) + "\n",
        )
        self.configure(target, consumer="local")
        result = self.integrations.sync("obsidian", graph())
        exported = (target / "brainkit" / "graph" / "graph.json").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("neverIngestCanary", exported)
        self.assertEqual(json.loads(exported), graph())
        self.assertIn("graph/graph.json", result["managed_files"])

    def test_export_does_not_overwrite_the_vault_graph(self) -> None:
        target = self.destination()
        self.vault.write_generated(
            "graph/graph.json",
            json.dumps(graph(canary="vaultOwnedCanary"), ensure_ascii=False) + "\n",
        )
        self.configure(target, consumer="cloud")
        self.integrations.sync("obsidian", graph())
        vault_graph = (self.root / "graph" / "graph.json").read_text(encoding="utf-8")
        self.assertIn("vaultOwnedCanary", vault_graph)

    def test_human_owned_files_survive_a_resync(self) -> None:
        target = self.destination()
        self.configure(target, consumer="human")
        self.integrations.sync("obsidian", graph())
        outside = target / "human-note.md"
        inside = target / "brainkit" / "minha-anotacao.md"
        nested = target / "brainkit" / "wiki" / "human-inside.md"
        nested.parent.mkdir(parents=True, exist_ok=True)
        for path in (outside, inside, nested):
            path.write_text("humano", encoding="utf-8")
        self.integrations.sync("obsidian", graph())
        for path in (outside, inside, nested):
            self.assertTrue(path.is_file(), path)

    def test_stale_managed_files_are_still_pruned(self) -> None:
        target = self.destination()
        self.configure(target, consumer="human")
        self.integrations.sync("obsidian", graph())
        stale = target / "brainkit" / "wiki" / "gone.md"
        stale.write_text("stale", encoding="utf-8")
        self.integrations._record(
            "obsidian", {"managed_files": ["wiki/gone.md", "graph/graph.json"]}
        )
        self.integrations.sync("obsidian", graph())
        self.assertFalse(stale.exists())
        self.assertTrue((target / "brainkit" / "graph" / "graph.json").is_file())

    def test_raw_is_excluded_unless_requested(self) -> None:
        target = self.destination()
        raw = self.root / "raw" / "_inbox" / "note.md"
        raw.parent.mkdir(parents=True, exist_ok=True)
        raw.write_text("raw bytes", encoding="utf-8")
        self.configure(target, consumer="human")
        self.integrations.sync("obsidian", graph())
        self.assertFalse((target / "brainkit" / "raw").exists())
        self.configure(target, include_raw=True)
        result = self.integrations.sync("obsidian", graph())
        self.assertTrue((target / "brainkit" / "raw" / "_inbox" / "note.md").is_file())
        self.assertIn("graph/graph.json", result["managed_files"])

    def test_in_place_export_only_writes_the_obsidian_app_config(self) -> None:
        self.integrations.configure(
            "obsidian",
            enabled=True,
            managed=False,
            options={"path": str(self.root)},
        )
        result = self.integrations.sync("obsidian", graph())
        self.assertEqual(result["mode"], "in-place")
        self.assertEqual(result["managed_files"], 0)
        self.assertEqual(
            [path.name for path in (self.root / ".obsidian").iterdir()], ["app.json"]
        )

    def test_sync_obsidian_signature_is_stable(self) -> None:
        parameters = list(
            signature(NativeIntegrations._sync_obsidian).parameters
        )
        self.assertEqual(parameters, ["self", "policy", "graph"])


class ReadinessProbeTest(unittest.TestCase):
    """A managed service is ready only when it stays ready."""

    def policy_for(self, **options: Any) -> Any:
        from brainkit.domain.model import IntegrationPolicy

        return IntegrationPolicy(enabled=True, managed=True, options=options)

    def test_the_deadline_is_generous_by_default(self) -> None:
        self.assertGreaterEqual(READY_TIMEOUT_SECONDS, 120)
        self.assertEqual(_ready_timeout(self.policy_for()), float(
            READY_TIMEOUT_SECONDS))

    def test_an_operator_can_raise_the_deadline(self) -> None:
        self.assertEqual(
            _ready_timeout(self.policy_for(ready_timeout_seconds=600)), 600.0
        )

    def test_a_nonsense_deadline_falls_back_to_the_default(self) -> None:
        for value in ("abc", None, 0, -5):
            with self.subTest(value=value):
                self.assertEqual(
                    _ready_timeout(self.policy_for(ready_timeout_seconds=value)),
                    float(READY_TIMEOUT_SECONDS),
                )

    def _probe(self, sequence: list[bool], *, elapsed: list[float]) -> None:
        """Drive the probe loop over a scripted readiness sequence."""
        answers = iter(sequence)
        clock = {"now": 0.0}

        def fake_monotonic() -> float:
            return clock["now"]

        def fake_sleep(seconds: float) -> None:
            clock["now"] += seconds

        def fake_run(argv, **kwargs):  # noqa: ANN001 - subprocess signature
            code = 0 if next(answers, False) else 1
            return subprocess.CompletedProcess(argv, code, "", "")

        with (
            patch.object(integrations_module.time, "monotonic", fake_monotonic),
            patch.object(integrations_module.time, "sleep", fake_sleep),
            patch.object(integrations_module.subprocess, "run", fake_run),
            patch.object(
                integrations_module, "_docker_container_state", lambda _: "running"
            ),
        ):
            _wait_database_ready(
                "postgres",
                self.policy_for(user="brainkit", database="brainkit"),
                "container",
            )
        elapsed.append(clock["now"])

    def test_postgres_is_probed_over_tcp_not_the_unix_socket(self) -> None:
        """The initdb temporary server listens on the socket only."""
        seen: list[list[str]] = []

        def fake_run(argv, **kwargs):  # noqa: ANN001 - subprocess signature
            seen.append(list(argv))
            return subprocess.CompletedProcess(argv, 0, "", "")

        clock = {"now": 0.0}
        with (
            patch.object(integrations_module.time, "monotonic", lambda: clock["now"]),
            patch.object(
                integrations_module.time,
                "sleep",
                lambda s: clock.__setitem__("now", clock["now"] + s),
            ),
            patch.object(integrations_module.subprocess, "run", fake_run),
            patch.object(
                integrations_module, "_docker_container_state", lambda _: "running"
            ),
        ):
            _wait_database_ready(
                "postgres",
                self.policy_for(user="brainkit", database="brainkit"),
                "container",
            )
        self.assertTrue(seen, "readiness never probed")
        argv = seen[0]
        self.assertIn("pg_isready", argv)
        self.assertIn("-h", argv)
        self.assertEqual(argv[argv.index("-h") + 1], "127.0.0.1")

    def test_a_single_success_is_not_enough(self) -> None:
        # Ready once, then the initdb server shuts down, then the real server
        # comes up and stays. The probe must wait for the stable run.
        elapsed: list[float] = []
        flapping = [True, False] + [True] * 200
        self._probe(flapping, elapsed=elapsed)
        self.assertGreaterEqual(elapsed[0], READY_STABLE_SECONDS)

    def test_a_stable_service_is_accepted_promptly(self) -> None:
        elapsed: list[float] = []
        self._probe([True] * 200, elapsed=elapsed)
        self.assertLess(elapsed[0], READY_STABLE_SECONDS + 1.0)

    def test_a_service_that_never_settles_times_out(self) -> None:
        answers = iter([True, False] * 5000)
        clock = {"now": 0.0}
        with (
            patch.object(integrations_module.time, "monotonic", lambda: clock["now"]),
            patch.object(
                integrations_module.time,
                "sleep",
                lambda s: clock.__setitem__("now", clock["now"] + s),
            ),
            patch.object(
                integrations_module.subprocess,
                "run",
                lambda argv, **kw: subprocess.CompletedProcess(
                    argv, 0 if next(answers, False) else 1, "", ""
                ),
            ),
            patch.object(
                integrations_module, "_docker_container_state", lambda _: "running"
            ),
            patch.object(integrations_module, "_docker_container_inspect", lambda _: None),
            patch.object(integrations_module, "_docker_container_logs",
                         lambda *a, **k: ""),
            patch.object(integrations_module, "_busy_ports", lambda _: []),
        ):
            with self.assertRaises(ValidationError) as timed_out:
                _wait_database_ready(
                    "postgres",
                    self.policy_for(ready_timeout_seconds=10),
                    "container",
                )
        self.assertEqual(timed_out.exception.details["reason"], "readiness probe timed out")


class StoredConsumerValidationTest(VaultFixture):
    """A stored consumer must be validated when it is written, not at sync."""

    def obsidian_options(self, **extra: Any) -> dict[str, Any]:
        return {"path": str(self.root / "obsidian"), **extra}

    def test_configure_rejects_an_invalid_obsidian_consumer(self) -> None:
        with self.assertRaises(ValidationError) as rejected:
            self.integrations.configure(
                "obsidian",
                enabled=True,
                managed=False,
                options=self.obsidian_options(consumer="clod"),
            )
        self.assertEqual(rejected.exception.details["consumer"], "clod")

    def test_an_invalid_obsidian_consumer_is_never_persisted(self) -> None:
        with self.assertRaises(ValidationError):
            self.integrations.configure(
                "obsidian",
                enabled=True,
                managed=False,
                options=self.obsidian_options(consumer="clod"),
            )
        stored = self.vault.config().integrations["obsidian"]
        self.assertNotIn("consumer", stored.options)

    def test_obsidian_consumer_stays_optional(self) -> None:
        result = self.integrations.configure(
            "obsidian", enabled=True, managed=False, options=self.obsidian_options()
        )
        self.assertNotIn("consumer", result["policy"]["options"])

    def test_a_valid_obsidian_consumer_is_accepted(self) -> None:
        for consumer in ("human", "local", "cloud"):
            with self.subTest(consumer=consumer):
                result = self.integrations.configure(
                    "obsidian",
                    enabled=True,
                    managed=False,
                    options=self.obsidian_options(consumer=consumer),
                )
                self.assertEqual(result["policy"]["options"]["consumer"], consumer)

    def test_database_consumer_is_still_mandatory(self) -> None:
        with self.assertRaises(ValidationError) as rejected:
            self.integrations.configure(
                "neo4j",
                enabled=True,
                managed=True,
                options={
                    "uri": "bolt://127.0.0.1:7687",
                    "user": "neo4j",
                    "password_env": "UNSET_FOR_TEST",
                },
            )
        self.assertEqual(rejected.exception.details["missing"], ["consumer"])


if __name__ == "__main__":
    unittest.main()
