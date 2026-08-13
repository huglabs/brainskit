from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.error import HTTPError
from urllib.parse import quote, unquote, urlsplit
from urllib.request import urlopen

from brainskit.application.ports import VaultPort
from brainskit.application.privacy import (
    _consumer_allows,
    _evidence_privacy,
)
from brainskit.domain.model import (
    INTEGRATION_NAMES,
    BrainskitError,
    IntegrationPolicy,
    NotConfiguredError,
    PrivacyMode,
    RefusalError,
    ValidationError,
    VaultConfig,
    utc_now,
)

SECRET_PLACEHOLDER = "***"
# A first boot has to chown the vault-local bind mount, which takes minutes on
# macOS, so the deadline is generous and configurable per integration.
READY_TIMEOUT_SECONDS = 300
READY_STABLE_SECONDS = 2.0
_DSN_CREDENTIALS_RE = re.compile(r"(?<=://)[^\s/@]*:[^\s/@]*(?=@)")
_STARTABLE_CONTAINER_STATUSES = frozenset({"running", "exited"})
_AUTHENTICATION_ERROR_NAMES = frozenset(
    {"AuthConfigurationError", "AuthError", "TokenExpired"}
)
_AUTHENTICATION_MARKERS = (
    "authentication failed",
    "authentication failure",
    "no password supplied",
    "password authentication",
    "pg_hba.conf",
    "unauthorized",
)
_CONNECTION_ERROR_NAMES = frozenset(
    {
        "ConfigurationError",
        "OperationalError",
        "ServiceUnavailable",
        "SessionExpired",
    }
)
_VENDOR_MESSAGES: dict[str, dict[str, str]] = {
    "neo4j": {
        "connection": "Neo4j integration could not reach the configured server",
        "authentication": "Neo4j integration rejected the configured credentials",
        "query": "Neo4j integration failed while writing the graph",
    },
    "postgres": {
        "connection": "PostgreSQL integration could not reach the configured server",
        "authentication": (
            "PostgreSQL integration rejected the configured credentials"
        ),
        "query": "PostgreSQL integration failed while writing the graph",
    },
}


class NativeIntegrations:
    """Persistent opt-in adapters for external graph and reading surfaces."""

    def __init__(self, vault: VaultPort):
        self.vault = vault

    def configure(
        self,
        name: str,
        *,
        enabled: bool | None,
        managed: bool | None,
        options: dict[str, Any],
    ) -> dict[str, Any]:
        name = _integration_name(name)
        config = self.vault.config()
        current = config.integrations[name]
        policy = IntegrationPolicy(
            enabled=current.enabled if enabled is None else enabled,
            managed=current.managed if managed is None else managed,
            options={**current.options, **options},
        )
        _validate_policy(name, policy)
        raw = config.to_dict()
        raw["version"] = 3
        raw["integrations"][name] = policy.to_dict()
        updated = VaultConfig.from_dict(raw)
        self.vault.save_config(updated)
        self._record(name, {"configured_at": utc_now()})
        return {"integration": name, "policy": policy.to_dict()}

    def status(self, name: str | None = None) -> dict[str, Any]:
        selected = [_integration_name(name)] if name else sorted(INTEGRATION_NAMES)
        config = self.vault.config()
        runtime = self.vault.read_state("integration-state").get("integrations", {})
        values: list[dict[str, Any]] = []
        for integration_name in selected:
            policy = config.integrations[integration_name]
            live_state = self._live_state(integration_name, policy)
            values.append(
                {
                    "name": integration_name,
                    "enabled": policy.enabled,
                    "managed": policy.managed,
                    "state": live_state,
                    "options": _public_options(policy.options),
                    "runtime": runtime.get(integration_name, {}),
                }
            )
        return {"count": len(values), "integrations": values}

    def up(self, name: str) -> dict[str, Any]:
        name = _integration_name(name)
        policy = self._enabled_policy(name)
        if name == "obsidian":
            raise ValidationError("Obsidian up must be routed through sync")
        if name == "web":
            result = self._web_up(policy)
        elif not policy.managed:
            result = {
                "integration": name,
                "state": "external",
                "message": "External service remains owned by the operator.",
            }
        else:
            result = self._database_up(name, policy)
        self._record(name, {"last_up_at": utc_now(), **result})
        return result

    def down(self, name: str) -> dict[str, Any]:
        name = _integration_name(name)
        policy = self._enabled_policy(name)
        if name == "obsidian":
            result = {"integration": name, "state": "ready"}
        elif name == "web":
            result = self._web_down(policy)
        elif not policy.managed:
            result = {
                "integration": name,
                "state": "external",
                "message": "External service was not changed.",
            }
        else:
            container = _container_name(self.vault.root, name, policy.options)
            state = _docker_container_state(container)
            if state == "running":
                _docker(["stop", container])
            result = {
                "integration": name,
                "state": "not-created" if state == "not-created" else "stopped",
                "container": container,
            }
        self._record(name, {"last_down_at": utc_now(), **result})
        return result

    def sync(self, name: str, graph: dict[str, Any]) -> dict[str, Any]:
        name = _integration_name(name)
        policy = self._enabled_policy(name)
        if name == "obsidian":
            result = self._sync_obsidian(policy, graph)
        elif name == "neo4j":
            result = self._sync_neo4j(policy, graph)
        elif name == "postgres":
            result = self._sync_postgres(policy, graph)
        else:
            result = {
                "integration": "web",
                "nodes": len(graph["nodes"]),
                "edges": len(graph["edges"]),
                "state": self._live_state("web", policy),
            }
        self._record(name, {"last_sync_at": utc_now(), **result})
        return result

    def _enabled_policy(self, name: str) -> IntegrationPolicy:
        policy = self.vault.config().integrations[name]
        if not policy.enabled:
            raise NotConfiguredError(
                "Integration is disabled",
                details={"integration": name, "hint": "Configure it with --enable"},
            )
        _validate_policy(name, policy)
        return policy

    def _record(self, name: str, values: dict[str, Any]) -> None:
        def mutate(state: dict[str, Any]) -> dict[str, Any]:
            state["version"] = 1
            integrations = state.setdefault("integrations", {})
            integrations[name] = {**integrations.get(name, {}), **values}
            return state

        self.vault.mutate_state("integration-state", mutate)

    def _live_state(self, name: str, policy: IntegrationPolicy) -> str:
        if not policy.enabled:
            return "disabled"
        if name == "obsidian":
            target = Path(str(policy.options.get("path", ""))).expanduser()
            return "ready" if target.is_dir() else "not-synced"
        if name == "web":
            runtime = self.vault.read_state("integration-state").get(
                "integrations", {}
            ).get("web", {})
            return (
                "running"
                if _web_instance_owned(runtime, policy)
                else "stopped"
            )
        if not policy.managed:
            return "external"
        container = _container_name(self.vault.root, name, policy.options)
        return _docker_container_state(container)

    def _sync_boundary(self, consumer: str) -> Callable[[Path], bool]:
        """Whether a vault-relative path may be copied out to `consumer`.

        The graph object was filtered carefully and then the files were chosen
        by walking the filesystem, so the boundary never reached the copy. The
        compiled page leaked under default options and raw never-ingest bytes
        leaked under `include_raw` -- into what is usually an iCloud- or
        Dropbox-backed directory.

        `views/` needs no check here: `ProjectionService.integration_sync`
        regenerates it filtered under this same consumer immediately before the
        copy. It is the only tree that was ever safe, and it was safe by that
        accident rather than by this decision.
        """

        records = self.vault.registry()
        config = self.vault.config()

        def allows(relative: Path) -> bool:
            posix = relative.as_posix()
            if posix.startswith("wiki/"):
                content = (self.vault.root / relative).read_text(
                    encoding="utf-8", errors="replace"
                )
                privacy = _evidence_privacy({"path": posix}, content, records, config)
                return _consumer_allows(consumer, privacy)
            if posix.startswith("raw/"):
                # Branch comes from the path, the way `_record_branch` reads it,
                # rather than from a registry lookup: a file that landed in the
                # inbox but has not been reconciled yet still sits in a branch
                # whose policy is known, and refusing it would over-block the
                # one directory files arrive in.
                parts = PurePosixPath(posix).parts
                branch = parts[1] if len(parts) > 1 else ""
                if branch == "_inbox":
                    return _consumer_allows(
                        consumer, PrivacyMode(config.inbox_policy.privacy)
                    )
                branch_policy = config.branches.get(branch)
                if branch_policy is None:
                    # Not a configured branch, so there is no policy that says
                    # this may leave the vault.
                    return consumer == "human"
                return _consumer_allows(consumer, PrivacyMode(branch_policy.privacy))
            return True

        return allows

    def _sync_obsidian(
        self, policy: IntegrationPolicy, graph: dict[str, Any]
    ) -> dict[str, Any]:
        target = Path(str(policy.options["path"])).expanduser().resolve()
        if target != self.vault.root and self.vault.root in target.parents:
            raise ValidationError(
                "Obsidian target cannot be nested inside the source vault"
            )
        target.mkdir(parents=True, exist_ok=True)
        obsidian_dir = target / ".obsidian"
        obsidian_dir.mkdir(exist_ok=True)
        app_config = obsidian_dir / "app.json"
        if not app_config.exists():
            _atomic_text(app_config, json.dumps({"alwaysUpdateLinks": True}, indent=2) + "\n")
        if target == self.vault.root:
            return {
                "integration": "obsidian",
                "state": "ready",
                "path": str(target),
                "managed_files": 0,
                "mode": "in-place",
            }
        subdirectory = _safe_subdirectory(
            str(policy.options.get("subdirectory", "brainkit"))
        )
        managed_root = target / subdirectory if subdirectory else target
        include_raw = bool(policy.options.get("include_raw", False))
        source_paths: list[Path] = []
        for relative_root in ("wiki", "views"):
            root = self.vault.root / relative_root
            source_paths.extend(path for path in root.rglob("*.md") if path.is_file())
        if include_raw:
            source_paths.extend(
                path for path in (self.vault.root / "raw").rglob("*") if path.is_file()
            )
        allows = self._sync_boundary(str(graph.get("consumer", "local")))
        managed: set[str] = set()
        for source in sorted(set(source_paths)):
            relative = source.relative_to(self.vault.root)
            if not allows(relative):
                # Excluded files are deliberately left out of `managed`, so the
                # stale sweep below deletes any copy an earlier, wider sync
                # wrote. Narrowing the consumer has to remove, not just stop
                # adding.
                continue
            destination = managed_root / relative
            _atomic_copy(source, destination)
            managed.add(relative.as_posix())
        _atomic_text(
            managed_root / "graph" / "graph.json",
            json.dumps(graph, ensure_ascii=False, indent=2) + "\n",
        )
        managed.add("graph/graph.json")
        runtime = self.vault.read_state("integration-state").get(
            "integrations", {}
        ).get("obsidian", {})
        for relative in runtime.get("managed_files", []):
            if relative in managed:
                continue
            stale = (managed_root / Path(*PurePosixPath(relative).parts)).resolve()
            if managed_root == stale or managed_root not in stale.parents:
                continue
            stale.unlink(missing_ok=True)
        return {
            "integration": "obsidian",
            "state": "ready",
            "path": str(managed_root),
            "managed_files": sorted(managed),
            "managed_file_count": len(managed),
            "include_raw": include_raw,
        }

    def _sync_neo4j(
        self, policy: IntegrationPolicy, graph: dict[str, Any]
    ) -> dict[str, Any]:
        try:
            from neo4j import (
                GraphDatabase,
            )
            from neo4j import (
                exceptions as neo4j_exceptions,
            )
        except ImportError as exc:
            raise NotConfiguredError(
                "Neo4j sync requires the official driver",
                details={"hint": "Install brainskit[neo4j]"},
            ) from exc
        password = _secret(policy.options, "password_env", "neo4j")
        uri = str(policy.options["uri"])
        user = str(policy.options["user"])
        database = str(policy.options.get("database", "neo4j"))
        vault_id = _vault_id(self.vault.root)
        nodes = [
            {
                **dict(node),
                "original_id": str(node["id"]),
                "id": f'{vault_id}:{node["id"]}',
                "vault_id": vault_id,
            }
            for node in graph["nodes"]
        ]
        edges = [
            {
                **dict(edge),
                "source": f'{vault_id}:{edge["source"]}',
                "target": f'{vault_id}:{edge["target"]}',
            }
            for edge in graph["edges"]
        ]
        sourced = [edge for edge in edges if edge["type"] == "sourced_from"]
        linked = [edge for edge in edges if edge["type"] == "links_to"]
        with _vendor_boundary(
            "neo4j",
            errors=_neo4j_errors(neo4j_exceptions),
            details={
                "integration": "neo4j",
                "uri": _redact_secrets(uri, (password,)),
                "user": user,
                "database": database,
            },
            secrets=(password,),
        ):
            driver = GraphDatabase.driver(uri, auth=(user, password))
            try:
                driver.verify_connectivity()
                with driver.session(database=database) as session:
                    session.run(
                        "CREATE CONSTRAINT brainkit_node_id IF NOT EXISTS "
                        "FOR (n:BrainkitNode) REQUIRE n.id IS UNIQUE"
                    ).consume()
                    session.execute_write(
                        _replace_neo4j_graph,
                        vault_id,
                        nodes,
                        sourced,
                        linked,
                    )
            finally:
                driver.close()
        return {
            "integration": "neo4j",
            "state": "synced",
            "nodes": len(nodes),
            "edges": len(graph["edges"]),
            "database": database,
            "vault_id": vault_id,
        }

    def _sync_postgres(
        self, policy: IntegrationPolicy, graph: dict[str, Any]
    ) -> dict[str, Any]:
        try:
            import psycopg
        except ImportError as exc:
            raise NotConfiguredError(
                "PostgreSQL sync requires psycopg",
                details={"hint": "Install brainskit[postgres]"},
            ) from exc
        schema = _sql_identifier(str(policy.options.get("schema", "brainkit")))
        dsn = _postgres_dsn(policy)
        vault_id = _vault_id(self.vault.root)
        # Stored ids carry the vault namespace exactly as the Neo4j export's do.
        # One schema holds every vault pointed at it, while the graph's natural
        # ids (`page:<path>`, `raw:<hash>`) are only unique within a vault --
        # `page:wiki/index.md` exists in all of them. The natural id is not lost:
        # `properties` is the untouched node, so it stays readable as
        # properties->>'id'.
        nodes = [
            (
                f'{vault_id}:{node["id"]}',
                vault_id,
                node["label"],
                node["kind"],
                node["path"],
                json.dumps(node, ensure_ascii=False),
            )
            for node in graph["nodes"]
        ]
        edges = [
            (
                f'{vault_id}:{edge["source"]}',
                f'{vault_id}:{edge["target"]}',
                vault_id,
                edge["type"],
                json.dumps(edge, ensure_ascii=False),
            )
            for edge in graph["edges"]
        ]
        with _vendor_boundary(
            "postgres",
            errors=(psycopg.Error, OSError),
            details={
                "integration": "postgres",
                "schema": schema,
                **_postgres_target(dsn),
            },
            secrets=_postgres_secrets(policy, dsn),
        ):
            with psycopg.connect(dsn) as connection:
                with connection.cursor() as cursor:
                    for statement, parameters in _postgres_schema_statements(
                        schema, vault_id
                    ):
                        cursor.execute(statement, parameters or None)
                    # Every delete names this vault. An unscoped refresh would
                    # take the whole schema with it, which is another
                    # application's data, not just another copy of ours. Edges
                    # go first so the foreign key back to `nodes` still holds.
                    cursor.execute(
                        f'DELETE FROM "{schema}".edges WHERE vault_id = %s',
                        (vault_id,),
                    )
                    cursor.execute(
                        f'DELETE FROM "{schema}".nodes WHERE vault_id = %s',
                        (vault_id,),
                    )
                    cursor.executemany(
                        f'INSERT INTO "{schema}".nodes '
                        '(id, vault_id, label, kind, path, properties) '
                        'VALUES (%s, %s, %s, %s, %s, %s::jsonb)',
                        nodes,
                    )
                    cursor.executemany(
                        f'INSERT INTO "{schema}".edges '
                        '(source, target, vault_id, type, properties) '
                        'VALUES (%s, %s, %s, %s, %s::jsonb)',
                        edges,
                    )
        return {
            "integration": "postgres",
            "state": "synced",
            "nodes": len(nodes),
            "edges": len(edges),
            "schema": schema,
            "vault_id": vault_id,
        }

    def _database_up(
        self, name: str, policy: IntegrationPolicy
    ) -> dict[str, Any]:
        if not shutil.which("docker"):
            raise NotConfiguredError("Managed integrations require Docker")
        container = _container_name(self.vault.root, name, policy.options)
        desired = _managed_container_spec(name, policy)
        inspected = _docker_container_inspect(container)
        drift = _container_drift(inspected, desired) if inspected else []
        recreated = False
        if inspected and drift:
            _docker(["rm", "--force", container])
            inspected = None
            recreated = True
        if inspected and _container_status(inspected) == "running":
            _wait_database_ready(name, policy, container, desired)
            return {
                "integration": name,
                "state": "ready",
                "container": container,
                "recreated": False,
            }
        if inspected:
            _start_managed_container(name, container, desired)
        else:
            self._run_managed_container(name, policy, container, desired)
        _wait_database_ready(name, policy, container, desired)
        result: dict[str, Any] = {
            "integration": name,
            "state": "ready",
            "container": container,
            "recreated": recreated,
        }
        if drift:
            result["reconciled"] = drift
        return result

    def _run_managed_container(
        self,
        name: str,
        policy: IntegrationPolicy,
        container: str,
        desired: dict[str, Any],
    ) -> None:
        data = self.vault.root / ".brain" / "services" / name / "data"
        data.mkdir(parents=True, exist_ok=True)
        password = _secret(policy.options, "password_env", name)
        docker_environment: dict[str, str] = {}
        if name == "neo4j":
            docker_environment["NEO4J_AUTH"] = (
                f'{policy.options.get("user", "neo4j")}/{password}'
            )
            volume = f"{data}:/data"
            variables = ["NEO4J_AUTH"]
        else:
            docker_environment.update(
                {
                    "POSTGRES_USER": str(policy.options.get("user", "brainkit")),
                    "POSTGRES_PASSWORD": password,
                    "POSTGRES_DB": str(policy.options.get("database", "brainkit")),
                }
            )
            volume = f"{data}:/var/lib/postgresql/data"
            variables = ["POSTGRES_USER", "POSTGRES_PASSWORD", "POSTGRES_DB"]
        arguments = [
            "run", "--detach", "--name", container,
            "--restart", "unless-stopped",
        ]
        for published in _publish_arguments(desired):
            arguments.extend(["-p", published])
        arguments.extend(["-v", volume])
        for variable in variables:
            arguments.extend(["-e", variable])
        arguments.append(str(desired["image"]))
        try:
            _docker(arguments, environment=docker_environment)
        except ValidationError as exc:
            raise ValidationError(
                "Managed database container could not be created",
                details={
                    "integration": name,
                    "container": container,
                    "image": desired["image"],
                    "published_ports": _publish_arguments(desired),
                    "busy_ports": _busy_ports(desired),
                    "reason": str(exc),
                    **exc.details,
                },
            ) from exc

    def _web_up(self, policy: IntegrationPolicy) -> dict[str, Any]:
        runtime = self.vault.read_state("integration-state").get(
            "integrations", {}
        ).get("web", {})
        if _web_instance_owned(runtime, policy):
            return {
                "integration": "web",
                "state": "running",
                "pid": runtime["pid"],
                "url": runtime.get("url"),
            }
        host = str(policy.options.get("host", "127.0.0.1"))
        port = int(policy.options.get("port", 8765))
        consumer = str(policy.options.get("consumer", "human"))
        token_env = str(policy.options.get("token_env", ""))
        instance_id = secrets.token_urlsafe(24)
        if host not in {"127.0.0.1", "localhost", "::1"}:
            if not token_env or not os.environ.get(token_env):
                raise NotConfiguredError(
                    "Remote web binding requires a configured token environment variable"
                )
        service_root = self.vault.root / ".brain" / "services" / "web"
        service_root.mkdir(parents=True, exist_ok=True)
        log_path = service_root / "web.log"
        command = [
            sys.executable,
            "-m",
            "brainskit",
            "--vault",
            str(self.vault.root),
            "web",
            "serve",
            "--host",
            host,
            "--port",
            str(port),
            "--consumer",
            consumer,
            "--instance-id",
            instance_id,
        ]
        if token_env:
            command.extend(["--token-env", token_env])
        with log_path.open("ab") as log:
            process = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=log,
                start_new_session=True,
                env=os.environ.copy(),
            )
        url = f"http://{host}:{port}"
        _wait_web_ready(
            process,
            host=host,
            port=port,
            log_path=log_path,
            instance_id=instance_id,
        )
        result = {
            "integration": "web",
            "state": "running",
            "pid": process.pid,
            "url": url,
            "log": str(log_path),
            "instance_id": instance_id,
        }
        self._record("web", result)
        return result

    def _web_down(self, policy: IntegrationPolicy) -> dict[str, Any]:
        runtime = self.vault.read_state("integration-state").get(
            "integrations", {}
        ).get("web", {})
        pid = runtime.get("pid")
        if _pid_running(pid):
            if not _web_instance_owned(runtime, policy):
                raise RefusalError(
                    "Refusing to stop a process not owned by this vault",
                    details={"pid": pid},
                )
            os.kill(int(pid), signal.SIGTERM)
            for _ in range(30):
                if not _pid_running(pid):
                    break
                time.sleep(0.1)
            if _pid_running(pid):
                raise ValidationError(
                    "Web integration did not stop cleanly",
                    details={"pid": pid},
                )
        return {"integration": "web", "state": "stopped", "pid": pid}


def _integration_name(value: str | None) -> str:
    name = str(value or "").strip().lower()
    if name not in INTEGRATION_NAMES:
        raise ValidationError(
            "Unknown integration", details={"integration": value}
        )
    return name


def _validate_policy(name: str, policy: IntegrationPolicy) -> None:
    if not policy.enabled:
        return
    options = policy.options
    required = {
        "obsidian": {"path"},
        "neo4j": {"uri", "user", "password_env", "consumer"},
        "postgres": (
            {"dsn_env", "consumer"}
            if not policy.managed
            else {"password_env", "consumer"}
        ),
        "web": set(),
    }[name]
    missing = sorted(key for key in required if not options.get(key))
    if missing:
        raise NotConfiguredError(
            "Integration configuration is incomplete",
            details={"integration": name, "missing": missing},
        )
    # Neo4j, PostgreSQL and the web viewer always resolve a consumer. Obsidian
    # falls back to the export default when unset, but a value that was stored
    # must still be valid here: `integration_configure` accepts a free-form
    # options object over MCP, so an unchecked typo would otherwise persist and
    # only surface at sync time.
    stored_consumer = str(options.get("consumer") or "")
    if name in {"neo4j", "postgres", "web"} or stored_consumer:
        consumer = stored_consumer or ("human" if name == "web" else "")
        if consumer not in {"human", "local", "cloud"}:
            raise ValidationError(
                "Integration privacy consumer is invalid",
                details={"integration": name, "consumer": consumer},
            )
    if name == "web":
        port = int(options.get("port", 8765))
        if not 1 <= port <= 65535:
            raise ValidationError("Web integration port is invalid")
    if name == "postgres":
        _sql_identifier(str(options.get("schema", "brainkit")))


def _public_options(options: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in options.items()
        if key not in {"password", "token", "dsn"}
    }


def _secret(options: dict[str, Any], field: str, integration: str) -> str:
    variable = str(options.get(field, ""))
    value = os.environ.get(variable) if variable else None
    if not value:
        raise NotConfiguredError(
            "Integration secret environment variable is not set",
            details={"integration": integration, "environment": variable},
        )
    return value


def _redact_secrets(text: str, secrets: Sequence[str]) -> str:
    """Remove known credentials and any embedded DSN credentials from text."""
    cleaned = text
    for secret in sorted({value for value in secrets if value}, key=len, reverse=True):
        cleaned = cleaned.replace(secret, SECRET_PLACEHOLDER)
    return _DSN_CREDENTIALS_RE.sub(SECRET_PLACEHOLDER, cleaned)


def _vendor_stage(error: BaseException) -> str:
    name = type(error).__name__
    sqlstate = str(getattr(error, "sqlstate", "") or "")
    code = str(getattr(error, "code", "") or "")
    message = str(error).lower()
    if (
        name in _AUTHENTICATION_ERROR_NAMES
        or sqlstate.startswith("28")
        or ".Security." in code
        or any(marker in message for marker in _AUTHENTICATION_MARKERS)
    ):
        return "authentication"
    if name in _CONNECTION_ERROR_NAMES or isinstance(error, OSError):
        return "connection"
    return "query"


@contextmanager
def _vendor_boundary(
    integration: str,
    *,
    errors: tuple[type[BaseException], ...],
    details: dict[str, Any],
    secrets: Sequence[str] = (),
) -> Iterator[None]:
    """Translate vendor driver failures into the brainskit error contract."""
    try:
        yield
    except BrainskitError:
        raise
    except errors as exc:
        stage = _vendor_stage(exc)
        raise ValidationError(
            _VENDOR_MESSAGES[integration][stage],
            details={
                **details,
                "stage": stage,
                "driver_error": type(exc).__name__,
                "reason": _redact_secrets(
                    str(exc).strip() or type(exc).__name__, secrets
                ),
            },
        ) from exc


def _neo4j_errors(exceptions: Any) -> tuple[type[BaseException], ...]:
    """Driver error bases resolved lazily so neo4j stays optional."""
    bases = tuple(
        getattr(exceptions, name)
        for name in ("Neo4jError", "DriverError")
        if hasattr(exceptions, name)
    )
    return (*bases, OSError)


def _postgres_target(dsn: str) -> dict[str, Any]:
    """Non-secret connection coordinates parsed from a PostgreSQL DSN."""
    try:
        parsed = urlsplit(dsn)
    except ValueError:
        return {}
    if parsed.scheme not in {"postgres", "postgresql"}:
        return {}
    target: dict[str, Any] = {}
    if parsed.hostname:
        target["host"] = parsed.hostname
    try:
        if parsed.port:
            target["port"] = parsed.port
    except ValueError:
        pass
    database = parsed.path.lstrip("/")
    if database:
        target["database"] = unquote(database)
    if parsed.username:
        target["user"] = unquote(parsed.username)
    return target


def _postgres_secrets(policy: IntegrationPolicy, dsn: str) -> tuple[str, ...]:
    """Every value that must never surface in an error payload."""
    values = [dsn]
    variable = str(policy.options.get("password_env", ""))
    configured = os.environ.get(variable) if variable else None
    if configured:
        values.append(configured)
    try:
        password = urlsplit(dsn).password
    except ValueError:
        password = None
    if password:
        values.extend([password, unquote(password)])
    return tuple(values)


def _postgres_dsn(policy: IntegrationPolicy) -> str:
    dsn_env = str(policy.options.get("dsn_env", ""))
    if dsn_env and os.environ.get(dsn_env):
        return str(os.environ[dsn_env])
    if not policy.managed:
        raise NotConfiguredError(
            "PostgreSQL DSN environment variable is not set",
            details={"environment": dsn_env},
        )
    password = quote(_secret(policy.options, "password_env", "postgres"), safe="")
    user = quote(str(policy.options.get("user", "brainkit")), safe="")
    database = quote(str(policy.options.get("database", "brainkit")), safe="")
    port = int(policy.options.get("port", 5432))
    return f"postgresql://{user}:{password}@127.0.0.1:{port}/{database}"


def _postgres_schema_statements(
    schema: str, vault_id: str
) -> list[tuple[str, tuple[str, ...]]]:
    """Statements that bring a fresh and an already-deployed schema to one shape.

    `CREATE TABLE IF NOT EXISTS` is inert against a table that already exists, so
    it can create `vault_id` but never add it. Every column later statements rely
    on is therefore also stated as `ALTER ... ADD COLUMN IF NOT EXISTS`, which is
    a no-op on a fresh table and the actual migration on a deployed one. Both
    orders converge, and re-running changes nothing.

    The backfill adopts pre-existing rows into the syncing vault. That is sound
    precisely because of the defect being fixed: the old refresh emptied both
    tables outright, so whatever is on disk is exactly one vault's last complete
    sync and never a mixture. Those rows are then replaced by the scoped delete
    on this same run, leaving no unreclaimable debris behind -- which a sentinel
    like 'unknown' would, since no vault's delete would ever name it.
    """
    namespace = f'"{schema}"'
    empty: tuple[str, ...] = ()
    return [
        (f"CREATE SCHEMA IF NOT EXISTS {namespace}", empty),
        (
            f"""CREATE TABLE IF NOT EXISTS {namespace}.nodes(
            id text PRIMARY KEY,
            vault_id text NOT NULL,
            label text NOT NULL,
            kind text NOT NULL,
            path text NOT NULL,
            properties jsonb NOT NULL DEFAULT '{{}}'::jsonb
        )""",
            empty,
        ),
        (
            f"""CREATE TABLE IF NOT EXISTS {namespace}.edges(
            source text NOT NULL REFERENCES {namespace}.nodes(id) ON DELETE CASCADE,
            target text NOT NULL REFERENCES {namespace}.nodes(id) ON DELETE CASCADE,
            vault_id text NOT NULL,
            type text NOT NULL,
            properties jsonb NOT NULL DEFAULT '{{}}'::jsonb,
            PRIMARY KEY(source, target, type)
        )""",
            empty,
        ),
        (
            f"ALTER TABLE {namespace}.nodes ADD COLUMN IF NOT EXISTS vault_id text",
            empty,
        ),
        (
            f"ALTER TABLE {namespace}.edges ADD COLUMN IF NOT EXISTS vault_id text",
            empty,
        ),
        (
            f"UPDATE {namespace}.nodes SET vault_id = %s WHERE vault_id IS NULL",
            (vault_id,),
        ),
        (
            f"UPDATE {namespace}.edges SET vault_id = %s WHERE vault_id IS NULL",
            (vault_id,),
        ),
        (
            f"ALTER TABLE {namespace}.nodes ALTER COLUMN vault_id SET NOT NULL",
            empty,
        ),
        (
            f"ALTER TABLE {namespace}.edges ALTER COLUMN vault_id SET NOT NULL",
            empty,
        ),
        (
            f"CREATE INDEX IF NOT EXISTS brainkit_nodes_vault_idx ON {namespace}.nodes(vault_id)",
            empty,
        ),
        (
            f"CREATE INDEX IF NOT EXISTS brainkit_edges_vault_idx ON {namespace}.edges(vault_id)",
            empty,
        ),
        (
            f"CREATE INDEX IF NOT EXISTS brainkit_edges_source_idx ON {namespace}.edges(source)",
            empty,
        ),
        (
            f"CREATE INDEX IF NOT EXISTS brainkit_edges_target_idx ON {namespace}.edges(target)",
            empty,
        ),
        (
            f"CREATE INDEX IF NOT EXISTS brainkit_nodes_properties_idx ON {namespace}.nodes USING gin(properties)",
            empty,
        ),
        # graph_walk needs no vault predicate, and deliberately keeps its
        # two-argument signature. Every stored id is prefixed with the vault
        # namespace, so `edge.source = walk.node_id` can only match an edge
        # belonging to the same vault as start_node: a walk cannot leave the
        # vault it started in. Isolation holds by construction rather than by
        # the caller remembering to filter -- which also means a caller must
        # pass the prefixed id, not the natural one.
        (
            f"""CREATE OR REPLACE FUNCTION {namespace}.graph_walk(
            start_node text, max_depth integer DEFAULT 3
        ) RETURNS TABLE(node_id text, depth integer, path text[])
        LANGUAGE sql STABLE AS $$
        -- start_node is the stored id, '<vault_id>:<natural id>'. The prefix is
        -- what confines the walk to one vault; see _postgres_schema_statements.
        WITH RECURSIVE walk(node_id, depth, path) AS (
            SELECT start_node, 0, ARRAY[start_node]
            UNION ALL
            SELECT edge.target, walk.depth + 1, walk.path || edge.target
            FROM walk JOIN {namespace}.edges edge ON edge.source = walk.node_id
            WHERE walk.depth < max_depth AND NOT edge.target = ANY(walk.path)
        ) SELECT * FROM walk
        $$""",
            empty,
        ),
    ]


def _sql_identifier(value: str) -> str:
    if not re.fullmatch(r"[a-z_][a-z0-9_]{0,62}", value):
        raise ValidationError(
            "PostgreSQL schema must be a lowercase SQL identifier",
            details={"schema": value},
        )
    return value


def _safe_subdirectory(value: str) -> str:
    pure = PurePosixPath(value.strip().strip("/"))
    if value and (pure.is_absolute() or ".." in pure.parts):
        raise ValidationError("Obsidian subdirectory is invalid")
    return pure.as_posix() if pure.parts else ""


def vault_id(root: Path) -> str:
    """The namespace every shared graph target uses to tell vaults apart.

    Both graph backends write into a store an operator may point several vaults
    at, so the two must agree on this value exactly; deriving it in one place is
    what keeps them from drifting apart. The machine-level vault registry
    reports it too, which is why it carries a public name: an operator reading
    rows out of a shared schema has to be able to say which vault wrote them.
    """
    return hashlib.sha256(str(root).encode("utf-8")).hexdigest()[:24]


# The in-module spelling, kept so the call sites and the tests that pin this
# behaviour do not have to move. New callers should use `vault_id`.
_vault_id = vault_id


def _container_name(root: Path, name: str, options: dict[str, Any]) -> str:
    configured = str(options.get("container_name", "")).strip()
    if configured:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", configured):
            raise ValidationError("Container name is invalid")
        return configured
    suffix = hashlib.sha256(str(root).encode("utf-8")).hexdigest()[:10]
    return f"brainskit-{name}-{suffix}"


def _replace_neo4j_graph(
    transaction: Any,
    vault_id: str,
    nodes: list[dict[str, Any]],
    sourced: list[dict[str, Any]],
    linked: list[dict[str, Any]],
) -> None:
    transaction.run(
        "MATCH (n:BrainkitNode {vault_id: $vault_id}) DETACH DELETE n",
        vault_id=vault_id,
    ).consume()
    transaction.run(
        "UNWIND $nodes AS item CREATE (n:BrainkitNode {id: item.id}) "
        "SET n.original_id = item.original_id, n.vault_id = item.vault_id, "
        "n.label = item.label, n.kind = item.kind, n.path = item.path",
        nodes=nodes,
    ).consume()
    for relationship, edges in (
        ("SOURCED_FROM", sourced),
        ("LINKS_TO", linked),
    ):
        if edges:
            transaction.run(
                "UNWIND $edges AS edge "
                "MATCH (a:BrainkitNode {id: edge.source}), "
                "(b:BrainkitNode {id: edge.target}) "
                f"MERGE (a)-[:{relationship}]->(b)",
                edges=edges,
            ).consume()


def _docker(
    arguments: Sequence[str], *, environment: dict[str, str] | None = None
) -> str:
    try:
        result = subprocess.run(
            ["docker", *arguments],
            check=False,
            capture_output=True,
            text=True,
            timeout=120,
            env={**os.environ, **(environment or {})},
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        # Docker not being installed or not running is the operator's
        # environment, not a malformed call.
        raise NotConfiguredError(
            "Docker command failed", details={"reason": str(exc)}
        ) from exc
    if result.returncode != 0:
        raise NotConfiguredError(
            "Docker command failed",
            details={"response": result.stderr.strip()[-2_000:]},
        )
    return result.stdout.strip()


def _docker_container_state(container: str) -> str:
    if not shutil.which("docker"):
        return "docker-unavailable"
    try:
        result = subprocess.run(
            ["docker", "inspect", "--format", "{{.State.Running}}", container],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return "unknown"
    if result.returncode != 0:
        return "not-created"
    return "running" if result.stdout.strip() == "true" else "stopped"


def _docker_container_inspect(container: str) -> dict[str, Any] | None:
    """Return the raw docker inspection payload, or None when absent."""
    try:
        result = subprocess.run(
            ["docker", "inspect", container],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, list) or not payload:
        return None
    entry = payload[0]
    return entry if isinstance(entry, dict) else None


def _docker_container_logs(container: str, *, secrets: Sequence[str] = ()) -> str:
    try:
        result = subprocess.run(
            ["docker", "logs", "--tail", "20", container],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    output = f"{result.stdout}{result.stderr}".strip()
    return _redact_secrets(output[-1_000:], secrets)


def _managed_container_spec(name: str, policy: IntegrationPolicy) -> dict[str, Any]:
    """The docker runtime spec the current policy implies."""
    if name == "neo4j":
        return {
            "image": str(policy.options.get("image", "neo4j:5-community")),
            "ports": {
                "7474/tcp": [str(int(policy.options.get("http_port", 7474)))],
                "7687/tcp": [str(int(policy.options.get("bolt_port", 7687)))],
            },
        }
    return {
        "image": str(policy.options.get("image", "postgres:17")),
        "ports": {"5432/tcp": [str(int(policy.options.get("port", 5432)))]},
    }


def _publish_arguments(desired: dict[str, Any]) -> list[str]:
    published: list[str] = []
    for container_port, host_ports in sorted(desired["ports"].items()):
        published.extend(f"{host}:{container_port}" for host in host_ports)
    return published


def _container_status(inspected: dict[str, Any]) -> str:
    state = inspected.get("State")
    if not isinstance(state, dict):
        return "unknown"
    return str(state.get("Status", "") or "unknown").lower()


def _published_ports(inspected: dict[str, Any]) -> dict[str, list[str]]:
    host_config = inspected.get("HostConfig")
    bindings = host_config.get("PortBindings") if isinstance(host_config, dict) else None
    if not isinstance(bindings, dict):
        return {}
    published: dict[str, list[str]] = {}
    for container_port, entries in bindings.items():
        hosts = [
            str(entry.get("HostPort", ""))
            for entry in (entries or [])
            if isinstance(entry, dict)
        ]
        published[str(container_port)] = sorted(host for host in hosts if host)
    return published


def _container_drift(
    inspected: dict[str, Any] | None, desired: dict[str, Any]
) -> list[str]:
    """Reasons the existing container cannot serve the current policy."""
    if inspected is None:
        return []
    reasons: list[str] = []
    status = _container_status(inspected)
    if status not in _STARTABLE_CONTAINER_STATUSES:
        reasons.append(f"container state is {status!r} and cannot be started")
    config = inspected.get("Config")
    image = str(config.get("Image", "")) if isinstance(config, dict) else ""
    if image != str(desired["image"]):
        reasons.append(f"image {image or 'unknown'!r} does not match {desired['image']!r}")
    actual_ports = _published_ports(inspected)
    expected_ports = {
        str(port): sorted(str(host) for host in hosts)
        for port, hosts in desired["ports"].items()
    }
    if actual_ports != expected_ports:
        reasons.append(
            f"published ports {_format_ports(actual_ports)} "
            f"do not match {_format_ports(expected_ports)}"
        )
    return reasons


def _format_ports(ports: dict[str, list[str]]) -> str:
    if not ports:
        return "none"
    return ", ".join(
        f"{host}->{container}"
        for container, hosts in sorted(ports.items())
        for host in hosts
    )


def _start_managed_container(
    name: str, container: str, desired: dict[str, Any]
) -> None:
    try:
        _docker(["start", container])
    except ValidationError as exc:
        raise ValidationError(
            "Managed database container could not be started",
            details={
                "integration": name,
                "container": container,
                "published_ports": _publish_arguments(desired),
                "busy_ports": _busy_ports(desired),
                "reason": str(exc),
                **exc.details,
            },
        ) from exc


def _busy_ports(desired: dict[str, Any]) -> list[str]:
    """Host ports the policy needs that another listener already answers on."""
    busy: list[str] = []
    for host_ports in desired["ports"].values():
        for host_port in host_ports:
            try:
                port = int(host_port)
            except (TypeError, ValueError):
                continue
            if _port_in_use(port):
                busy.append(str(port))
    return sorted(set(busy))


def _port_in_use(port: int) -> bool:
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.settimeout(0.25)
        return probe.connect_ex(("127.0.0.1", port)) == 0
    except OSError:
        return False
    finally:
        probe.close()


def _pid_running(value: Any) -> bool:
    try:
        pid = int(value)
        if pid <= 0:
            return False
        os.kill(pid, 0)
        return True
    except (TypeError, ValueError, OSError):
        return False


def _web_instance_owned(
    runtime: dict[str, Any], policy: IntegrationPolicy
) -> bool:
    if not _pid_running(runtime.get("pid")):
        return False
    expected = str(runtime.get("instance_id", ""))
    if not expected:
        return False
    host = str(policy.options.get("host", "127.0.0.1"))
    port = int(policy.options.get("port", 8765))
    return _probe_web_identity(host, port) == expected


def _probe_web_identity(host: str, port: int) -> str | None:
    probe_host = "127.0.0.1" if host in {"0.0.0.0", "::"} else host
    if ":" in probe_host and not probe_host.startswith("["):
        probe_host = f"[{probe_host}]"
    try:
        with urlopen(
            f"http://{probe_host}:{port}/api/health", timeout=0.3
        ) as response:
            payload = json.loads(response.read())
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict) or payload.get("service") != "brainskit-web":
        return None
    return str(payload.get("instance_id", "")) or None


def _ready_timeout(policy: IntegrationPolicy) -> float:
    """Readiness deadline in seconds; operators may raise it per integration."""
    try:
        configured = float(
            policy.options.get("ready_timeout_seconds", READY_TIMEOUT_SECONDS)
        )
    except (TypeError, ValueError):
        return float(READY_TIMEOUT_SECONDS)
    return configured if configured > 0 else float(READY_TIMEOUT_SECONDS)


def _wait_database_ready(
    name: str,
    policy: IntegrationPolicy,
    container: str,
    desired: dict[str, Any] | None = None,
) -> None:
    desired = desired or _managed_container_spec(name, policy)
    deadline = time.monotonic() + _ready_timeout(policy)
    reason = "readiness probe timed out"
    # PostgreSQL answers pg_isready from the temporary server it runs during
    # initdb, then shuts it down and restarts. Requiring consecutive successes
    # spanning READY_STABLE_SECONDS refuses to report a server that is about to
    # disappear, which otherwise fails the very next command.
    stable_until: float | None = None
    while time.monotonic() < deadline:
        state = _docker_container_state(container)
        if state not in {"running", "unknown"}:
            reason = f"container state is {state!r} before it became ready"
            break
        ready = False
        if name == "neo4j":
            port = int(policy.options.get("http_port", 7474))
            try:
                with urlopen(f"http://127.0.0.1:{port}", timeout=0.5) as response:
                    ready = response.status < 500
            except HTTPError as exc:
                ready = exc.code < 500
            except OSError:
                pass
        else:
            user = str(policy.options.get("user", "brainkit"))
            database = str(policy.options.get("database", "brainkit"))
            try:
                # Probe over TCP, not the unix socket. During initdb the
                # entrypoint runs a temporary server with listen_addresses='',
                # which answers on the socket but not on TCP, so this never
                # mistakes it for the real server that clients will connect to.
                result = subprocess.run(
                    [
                        "docker",
                        "exec",
                        container,
                        "pg_isready",
                        "-h",
                        "127.0.0.1",
                        "-p",
                        "5432",
                        "-U",
                        user,
                        "-d",
                        database,
                    ],
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=2,
                )
                ready = result.returncode == 0
            except (OSError, subprocess.TimeoutExpired):
                pass
        if not ready:
            stable_until = None
        elif stable_until is None:
            stable_until = time.monotonic() + READY_STABLE_SECONDS
        elif time.monotonic() >= stable_until:
            return
        time.sleep(0.25)
    variable = str(policy.options.get("password_env", ""))
    secrets_seen = (os.environ.get(variable) or "",) if variable else ()
    inspected = _docker_container_inspect(container)
    raw_state = inspected.get("State") if inspected else None
    container_state: dict[str, Any] = raw_state if isinstance(raw_state, dict) else {}
    details: dict[str, Any] = {
        "integration": name,
        "container": container,
        "reason": reason,
        "status": _container_status(inspected) if inspected else "not-created",
        "published_ports": _publish_arguments(desired),
    }
    if container_state.get("ExitCode") is not None:
        details["exit_code"] = container_state["ExitCode"]
    docker_error = str(container_state.get("Error", "") or "")
    if docker_error:
        details["docker_error"] = _redact_secrets(docker_error, secrets_seen)
    if details["status"] != "running":
        busy = _busy_ports(desired)
        if busy:
            details["busy_ports"] = busy
    logs = _docker_container_logs(container, secrets=secrets_seen)
    if logs:
        details["logs"] = logs
    raise ValidationError(
        "Managed database integration did not become ready", details=details
    )


def _wait_web_ready(
    process: subprocess.Popen[bytes],
    *,
    host: str,
    port: int,
    log_path: Path,
    instance_id: str,
) -> None:
    url = f"http://{host}:{port}/api/health"
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if process.poll() is not None:
            break
        if _probe_web_identity(host, port) == instance_id:
            return
        time.sleep(0.1)
    if process.poll() is None:
        process.terminate()
    raise ValidationError(
        "Web integration did not become ready",
        details={"url": url, "log": str(log_path)},
    )


def _atomic_copy(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        mode="wb", dir=target.parent, prefix=f".{target.name}.", delete=False
    )
    try:
        with source.open("rb") as reader, handle:
            shutil.copyfileobj(reader, handle, length=1024 * 1024)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(handle.name, target)
    except Exception:
        Path(handle.name).unlink(missing_ok=True)
        raise


def _atomic_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", delete=False
    )
    try:
        with handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(handle.name, path)
    except Exception:
        Path(handle.name).unlink(missing_ok=True)
        raise
