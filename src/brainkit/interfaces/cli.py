from __future__ import annotations

import argparse
import json
import re
import shlex
import sys
import time
import traceback
from collections import Counter
from collections.abc import Sequence
from importlib.resources import files
from pathlib import Path
from typing import Any, NamedTuple

from brainkit import __version__
from brainkit.application.services import BrainkitService
from brainkit.domain.model import (
    DEFAULT_IGNORE_PATTERNS,
    BrainkitError,
    FilingMode,
    PolicyError,
    PrivacyMode,
    ValidationError,
)
from brainkit.infrastructure.extractor import GraphifyExtractor
from brainkit.infrastructure.graph import MarkdownGraph
from brainkit.infrastructure.index import SqliteFtsIndex
from brainkit.infrastructure.integrations import NativeIntegrations, vault_id
from brainkit.infrastructure.llm import JobSpecs, PolicyJudgmentRouter
from brainkit.infrastructure.vault import FileVault
from brainkit.infrastructure.vaults import RegisteredVault, VaultRegistry


class InternalError(BrainkitError):
    """An unmodelled adapter failure that reached the CLI boundary."""

    code = "internal_error"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bk", description="Local-first, LLM-agnostic second-brain engine"
    )
    parser.add_argument("--vault", help="Vault root (otherwise discover from cwd)")
    parser.add_argument("--json", action="store_true", help="Machine-readable output")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    commands = parser.add_subparsers(dest="command", required=True)

    init = commands.add_parser("init", help="Initialize a policy-complete vault")
    init.add_argument("path", nargs="?", default=".")
    init.add_argument(
        "--config", help="Complete policy JSON path, or - for standard input"
    )

    capture = commands.add_parser("capture", help="Capture a file, URL, or text")
    capture.add_argument("source", nargs="?")
    capture.add_argument("--text", help="Capture literal text")
    capture.add_argument("--title")

    commands.add_parser("status", help="Show vault health and counts")
    commands.add_parser("reconcile", help="Heal registry paths after manual moves")
    commands.add_parser("reindex", help="Rebuild the disposable FTS5 index")

    file_command = commands.add_parser("file", help="Move a raw source to a branch")
    file_command.add_argument("item", help="Full/prefix hash or raw path")
    file_command.add_argument("--to", required=True, dest="branch")

    forget_command = commands.add_parser(
        "forget",
        help=(
            "Drop one source record from this vault's registry (not to be "
            "confused with `bk vaults forget`, which unregisters a whole "
            "vault from this machine)"
        ),
    )
    forget_command.add_argument("item", help="Full/prefix hash or raw path")
    forget_command.add_argument(
        "--force",
        action="store_true",
        help="Forget it even though the raw file is still on disk",
    )

    lint = commands.add_parser("lint", help="Validate registry and wiki contracts")
    lint.add_argument("--changed", action="store_true")
    lint.add_argument("--semantic", action="store_true")

    search = commands.add_parser("search", help="Search with FTS5 BM25")
    search.add_argument("query")
    search.add_argument("--limit", type=int, default=10)
    search.add_argument("--consumer", choices=["human", "local", "cloud"])

    context = commands.add_parser("context", help="Build a bounded evidence bundle")
    context.add_argument("query")
    context.add_argument("--limit", type=int, default=8)
    context.add_argument("--max-chars", type=int, default=24_000)
    context.add_argument("--consumer", choices=["human", "local", "cloud"])

    apply_command = commands.add_parser(
        "apply", help="Validate and atomically stage wiki writes"
    )
    apply_command.add_argument("proposal", help="Proposal JSON path, or - for stdin")

    gate = commands.add_parser(
        "gate", help="Answer whether a direct write is permitted"
    )
    gate_sub = gate.add_subparsers(dest="gate_command", required=True)
    gate_check = gate_sub.add_parser(
        "check-write", help="Exit 0 when a direct write is allowed, 2 when denied"
    )
    gate_check.add_argument("path", metavar="PATH", help="Path a tool wants to write")
    gate_check.add_argument(
        "--agent", default="claude", help="Policy adapter to consult"
    )

    commands.add_parser("views", help="Regenerate Obsidian views")
    graph = commands.add_parser("graph", help="Regenerate the knowledge graph")
    graph.add_argument("--html", action="store_true")

    # `build` extracts in-process (the vendored Graphify closure, gated on the
    # `code` extra); `import` accepts a graph produced any other way. Both
    # normalise through the same `nodes`/`edges` boundary and the same write,
    # so neither path is more trusted than the other. `build` alone can be
    # scoped to a subset of paths, in which case that subset is merged into
    # whatever graph is already stored (`CodeGraph._merge_scoped`) rather than
    # replacing it.
    code = commands.add_parser("code", help="The repository graph and queries over it")
    code_commands = code.add_subparsers(dest="code_command", required=True)

    code_build = code_commands.add_parser(
        "build", help="Extract the repository graph in-process and import it"
    )
    code_build.add_argument(
        "paths",
        nargs="*",
        metavar="PATH",
        help=(
            "Scope the scan to these files/directories (default: the whole "
            "code root). Merged into the stored graph rather than replacing it"
        ),
    )

    code_import = code_commands.add_parser(
        "import", help="Import an extractor's graph as graph/code.json"
    )
    code_import.add_argument("graph", metavar="GRAPH_JSON")

    code_commands.add_parser(
        "status", help="Whether the graph still describes the repository"
    )

    code_affected = code_commands.add_parser(
        "affected", help="What breaks if this symbol changes"
    )
    code_affected.add_argument("symbol")
    code_affected.add_argument("--depth", type=int, default=2)

    code_path = code_commands.add_parser(
        "path", help="Shortest chain of edges between two symbols"
    )
    code_path.add_argument("source")
    code_path.add_argument("target")

    code_hubs = code_commands.add_parser("hubs", help="The most connected symbols")
    code_hubs.add_argument("--top", type=int, default=10)

    code_communities = code_commands.add_parser(
        "communities", help="Group the graph into structurally cohesive clusters"
    )
    code_communities.add_argument(
        "--resolution",
        type=float,
        default=1.0,
        help="Higher = more, smaller communities; lower = fewer, larger ones",
    )

    code_cycles = code_commands.add_parser("cycles", help="Import cycles among files")
    code_cycles.add_argument(
        "--max-length", type=int, default=5, dest="max_length",
        help="Longest cycle (in files) worth reporting",
    )
    code_cycles.add_argument("--top", type=int, default=20)

    code_diff = code_commands.add_parser(
        "diff", help="What changed structurally since the stored graph"
    )
    code_diff.add_argument(
        "graph",
        nargs="?",
        metavar="GRAPH_JSON",
        help=(
            "Diff against this extractor-shaped graph (same shape as `code "
            "import`, not brainkit's own stored graph/code.json) instead of "
            "extracting the repository fresh"
        ),
    )

    for reader in (
        code_affected, code_path, code_hubs, code_communities, code_cycles, code_diff,
    ):
        reader.add_argument(
            "--consumer",
            choices=["human", "local", "cloud"],
            default="local",
            help="The code graph carries repository paths and never leaves the machine",
        )

    export = commands.add_parser("export", help="Export the graph")
    export.add_argument(
        "--target",
        required=True,
        choices=[
            "json",
            "graphml",
            "cypher",
            "obsidian",
            "neo4j",
            "postgres",
            "kuzu",
            "llms-txt",
        ],
    )
    export.add_argument(
        "--consumer",
        choices=["human", "local", "cloud"],
        default="local",
        help=(
            "Privacy boundary written into the export; defaults to local, "
            "which excludes never-ingest branches"
        ),
    )

    ingest = commands.add_parser("ingest", help="Run the configured ingest judgment")
    ingest.add_argument("item", nargs="?")
    ingest.add_argument("--all", action="store_true", dest="all_pending")
    ingest.add_argument("--to", dest="target_branch")

    proposals = commands.add_parser(
        "proposals", help="List filing and wiki proposals"
    )
    proposals.add_argument(
        "--status", choices=["pending", "applied", "rejected", "failed"]
    )

    approve = commands.add_parser("approve", help="Approve a pending proposal")
    approve.add_argument("proposal_id")

    reject = commands.add_parser("reject", help="Reject a pending proposal")
    reject.add_argument("proposal_id")
    reject.add_argument("--reason", default="")

    ask = commands.add_parser("ask", help="Answer from compiled vault evidence")
    ask.add_argument("question")
    ask.add_argument("--save", action="store_true")

    digest = commands.add_parser("digest", help="Generate the configured digest")
    digest.add_argument("--since", default="7d")
    commands.add_parser("resurface", help="Resurface one durable insight")

    serve = commands.add_parser("serve", help="Serve robot interfaces")
    serve.add_argument("--mcp", action="store_true", required=True)
    serve.add_argument(
        "--transport", choices=["stdio", "http"], default="stdio"
    )
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8766)
    serve.add_argument("--token-env")
    serve.add_argument("--allowed-origin", action="append", default=[])
    serve.add_argument("--tls-cert")
    serve.add_argument("--tls-key")

    watch = commands.add_parser("watch", help="Watch configured source folders")
    watch.add_argument("--once", action="store_true")
    watch.add_argument("--interval", type=float, default=5.0)

    hooks = commands.add_parser("hooks", help="Install agent-facing vault hooks")
    hooks_sub = hooks.add_subparsers(dest="hooks_command", required=True)
    hooks_install = hooks_sub.add_parser("install")
    hooks_install.add_argument(
        "--agent",
        required=True,
        choices=["claude", "codex", "gemini", "opencode"],
    )
    hooks_install.add_argument(
        "--root",
        help=(
            "Directory that owns the agent's configuration -- .claude/, "
            "CLAUDE.md and the git pre-commit hook. Defaults to the vault, "
            "which is right only when the vault is itself the project the "
            "agent opens"
        ),
    )
    hooks_install.add_argument(
        "--force",
        action="store_true",
        help=(
            "Overwrite an existing brainkit skill, pre-commit hook and hook "
            "scripts; settings.json is merged, never overwritten"
        ),
    )

    commands.add_parser("schedule", help="Show configured habit job registrations")

    integration = commands.add_parser(
        "integration", help="Configure and operate persistent integrations"
    )
    integration_sub = integration.add_subparsers(
        dest="integration_command", required=True
    )
    integration_configure = integration_sub.add_parser("configure")
    integration_configure.add_argument(
        "name", choices=["obsidian", "neo4j", "postgres", "web"]
    )
    enabled_group = integration_configure.add_mutually_exclusive_group()
    enabled_group.add_argument("--enable", action="store_true", default=None)
    enabled_group.add_argument("--disable", action="store_true", default=None)
    managed_group = integration_configure.add_mutually_exclusive_group()
    managed_group.add_argument("--managed", action="store_true", default=None)
    managed_group.add_argument("--external", action="store_true", default=None)
    integration_configure.add_argument("--path")
    integration_configure.add_argument("--subdirectory")
    integration_configure.add_argument(
        "--include-raw", action="store_true", default=None
    )
    integration_configure.add_argument("--uri")
    integration_configure.add_argument("--user")
    integration_configure.add_argument("--password-env")
    integration_configure.add_argument("--database")
    integration_configure.add_argument("--dsn-env")
    integration_configure.add_argument("--schema")
    integration_configure.add_argument("--image")
    integration_configure.add_argument("--container-name")
    integration_configure.add_argument("--host")
    integration_configure.add_argument("--port", type=int)
    integration_configure.add_argument("--http-port", type=int)
    integration_configure.add_argument("--bolt-port", type=int)
    integration_configure.add_argument("--token-env")
    integration_configure.add_argument(
        "--consumer", choices=["human", "local", "cloud"]
    )
    integration_status = integration_sub.add_parser("status")
    integration_status.add_argument(
        "name", nargs="?", choices=["obsidian", "neo4j", "postgres", "web"]
    )
    for operation in ("up", "down", "sync"):
        command = integration_sub.add_parser(operation)
        command.add_argument(
            "name", choices=["obsidian", "neo4j", "postgres", "web"]
        )

    vaults = commands.add_parser(
        "vaults",
        help="Register the vaults on this machine and sync them as one set",
    )
    vaults_sub = vaults.add_subparsers(dest="vaults_command", required=True)
    vaults_register = vaults_sub.add_parser(
        "register", help="Add a vault to this machine's registry"
    )
    vaults_register.add_argument(
        "path",
        nargs="?",
        help=(
            "Vault root. Defaults to --vault, and otherwise to the vault "
            "discovered from the current directory"
        ),
    )
    vaults_register.add_argument(
        "--label",
        default="",
        help="Name for this vault; defaults to its directory name",
    )
    vaults_sub.add_parser(
        "list",
        help=(
            "Show every registered vault: label, path, whether it is still "
            "there, and the vault id its rows carry in a shared store"
        ),
    )
    vaults_forget = vaults_sub.add_parser(
        "forget",
        help=(
            "Remove a vault from the registry. This unregisters only: the "
            "vault's pages, raw evidence and configuration are never touched, "
            "and registering it again restores the entry"
        ),
    )
    vaults_forget.add_argument(
        "selector", metavar="PATH|LABEL", help="Registered path or label"
    )
    vaults_sync = vaults_sub.add_parser(
        "sync",
        help=(
            "Run one integration's sync for every registered vault. A vault "
            "that fails does not stop the rest; exit is non-zero if any did"
        ),
    )
    vaults_sync.add_argument(
        "--target",
        choices=["postgres", "neo4j", "obsidian"],
        default="postgres",
        help="Integration to sync into; defaults to the shared PostgreSQL graph",
    )
    vaults_sync.add_argument(
        "--consumer",
        choices=["human", "local", "cloud"],
        help=(
            "Refused on purpose. Each vault's privacy boundary comes from its "
            "own integration policy; see bk integration configure --consumer"
        ),
    )

    web = commands.add_parser("web", help="Run the complete local web viewer")
    web_sub = web.add_subparsers(dest="web_command", required=True)
    web_serve = web_sub.add_parser("serve")
    web_serve.add_argument("--host")
    web_serve.add_argument("--port", type=int)
    web_serve.add_argument("--consumer", choices=["human", "local", "cloud"])
    web_serve.add_argument("--token-env")
    web_serve.add_argument(
        "--allowed-origin",
        action="append",
        default=[],
        help=(
            "Browser origin permitted to call the API, repeatable. Its hostname "
            "is also accepted in the Host header, so a viewer fronted by a real "
            "name needs this flag once rather than twice. Defaults to the "
            "loopback origins the viewer is served from"
        ),
    )
    web_serve.add_argument("--instance-id", help=argparse.SUPPRESS)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    effective_argv = list(argv if argv is not None else sys.argv[1:])
    effective_argv, global_values = _extract_global_options(effective_argv)
    parser = build_parser()
    args = parser.parse_args(effective_argv)
    if global_values["vault"] is not None:
        args.vault = global_values["vault"]
    if global_values["json"]:
        args.json = True
    try:
        result = _dispatch(args)
        if args.command == "gate":
            # The gate answers with an exit code, and its denial text is prose
            # for a model rather than the ok/result envelope.
            return _emit_gate(result, json_mode=args.json)
        if result is not None:
            _emit(result, json_mode=args.json)
        if args.command == "lint" and isinstance(result, dict) and not result["ok"]:
            return 1
        if (
            args.command == "vaults"
            and args.vaults_command == "sync"
            and isinstance(result, dict)
            and result["failed"]
        ):
            # A failure inside the loop is reported, not raised, so the status
            # is the only thing a scheduler can branch on. Skipped vaults are
            # not failures: declining an integration is the policy working.
            return 1
        return 0
    except PolicyError as exc:
        _emit_error(exc, json_mode=args.json)
        return 3
    except BrainkitError as exc:
        _emit_error(exc, json_mode=args.json)
        return 2
    except (OSError, json.JSONDecodeError) as exc:
        wrapped = BrainkitError(str(exc))
        _emit_error(wrapped, json_mode=args.json)
        return 2
    except KeyboardInterrupt:
        _emit_error(BrainkitError("Interrupted"), json_mode=args.json)
        return 130
    except Exception as exc:
        # An adapter raised something brainkit does not model. Never let a raw
        # traceback replace the machine-readable envelope on stdout.
        _emit_error(_internal_error(exc), json_mode=args.json)
        return 2


def create_service(vault_path: str | None) -> BrainkitService:
    vault = FileVault(Path(vault_path)) if vault_path else FileVault.discover()
    index = SqliteFtsIndex(vault.index_path)
    jobs = JobSpecs()
    judgment = PolicyJudgmentRouter(vault.config(), jobs)
    graph = MarkdownGraph()
    return BrainkitService(
        vault,
        index,
        judgment=judgment,
        jobs=jobs,
        graph=graph,
        integrations=NativeIntegrations(vault),
        extractor=GraphifyExtractor(),
    )


def _dispatch(args: argparse.Namespace) -> Any:
    if args.command == "init":
        raw_config = (
            _read_json(args.config) if args.config else _interactive_policy_wizard()
        )
        vault = FileVault.initialize(Path(args.vault or args.path), raw_config)
        service = create_service(str(vault.root))
        indexed = service.reindex()
        views = service.views()
        return {
            "vault": str(vault.root),
            "config": vault.config().to_dict(),
            **indexed,
            "views": views["written"],
        }

    # Registry commands span vaults, so they must not be gated on the current
    # directory being one: `bk vaults list` has to work from anywhere, and
    # `bk vaults sync` builds a service per registered vault instead.
    if args.command == "vaults":
        return _vaults(args)

    service = create_service(args.vault)
    if args.command == "capture":
        return service.capture(args.source, text=args.text, title=args.title)
    if args.command == "status":
        return service.status()
    if args.command == "reconcile":
        return service.reconcile()
    if args.command == "reindex":
        return service.reindex()
    if args.command == "file":
        return service.file(args.item, args.branch)
    if args.command == "forget":
        return service.forget(args.item, force=args.force)
    if args.command == "lint":
        return service.lint(semantic=args.semantic)
    if args.command == "search":
        return service.search(
            args.query,
            args.limit,
            consumer=_consumer_for_args(args),
        )
    if args.command == "context":
        return service.context(
            args.query,
            limit=args.limit,
            max_chars=args.max_chars,
            consumer=_consumer_for_args(args),
        )
    if args.command == "apply":
        return service.apply(_read_json(args.proposal))
    if args.command == "gate":
        return service.gate_check_write(args.path, agent=args.agent)
    if args.command == "views":
        return service.views()
    if args.command == "graph":
        return service.graph(html=args.html)
    if args.command == "code":
        if args.code_command == "build":
            return service.code_build(args.paths or None)
        if args.code_command == "import":
            return service.code_import(_read_json(args.graph))
        if args.code_command == "status":
            return service.code_status()
        if args.code_command == "affected":
            return service.code_affected(
                args.symbol, depth=args.depth, consumer=args.consumer
            )
        if args.code_command == "path":
            return service.code_path(args.source, args.target, consumer=args.consumer)
        if args.code_command == "hubs":
            return service.code_hubs(top=args.top, consumer=args.consumer)
        if args.code_command == "communities":
            return service.code_communities(
                resolution=args.resolution, consumer=args.consumer
            )
        if args.code_command == "cycles":
            return service.code_cycles(
                max_length=args.max_length, top=args.top, consumer=args.consumer
            )
        if args.code_command == "diff":
            against = _read_json(args.graph) if args.graph else None
            return service.code_diff(against, consumer=args.consumer)
    if args.command == "export":
        return service.export(args.target, consumer=args.consumer or "local")
    if args.command == "ingest":
        return service.ingest(
            args.item,
            all_pending=args.all_pending,
            target_branch=args.target_branch,
        )
    if args.command == "proposals":
        return service.proposals(args.status)
    if args.command == "approve":
        return service.approve(args.proposal_id)
    if args.command == "reject":
        return service.reject(args.proposal_id, args.reason)
    if args.command == "ask":
        return service.ask(args.question, save=args.save)
    if args.command == "digest":
        return service.digest(args.since)
    if args.command == "resurface":
        return service.resurface()
    if args.command == "serve":
        from brainkit.interfaces.mcp import run_http, run_stdio

        if args.transport == "http":
            run_http(
                service,
                host=args.host,
                port=args.port,
                token_env=args.token_env or "",
                allowed_origins=args.allowed_origin,
                tls_cert=args.tls_cert,
                tls_key=args.tls_key,
            )
        else:
            run_stdio(service)
        return None
    if args.command == "watch":
        return _watch(service, once=args.once, interval=args.interval, json_mode=args.json)
    if args.command == "hooks":
        return _install_hooks(
            service, args.agent, root=args.root, force=args.force
        )
    if args.command == "schedule":
        return _schedule(service)
    if args.command == "integration":
        if args.integration_command == "configure":
            enabled = True if args.enable else False if args.disable else None
            managed = True if args.managed else False if args.external else None
            option_names = (
                "path",
                "subdirectory",
                "include_raw",
                "uri",
                "user",
                "password_env",
                "database",
                "dsn_env",
                "schema",
                "image",
                "container_name",
                "host",
                "port",
                "http_port",
                "bolt_port",
                "token_env",
                "consumer",
            )
            options = {
                name: getattr(args, name)
                for name in option_names
                if getattr(args, name) is not None
            }
            return service.integration_configure(
                args.name,
                enabled=enabled,
                managed=managed,
                options=options,
            )
        if args.integration_command == "status":
            return service.integration_status(args.name)
        if args.integration_command == "up":
            return service.integration_up(args.name)
        if args.integration_command == "down":
            return service.integration_down(args.name)
        if args.integration_command == "sync":
            return service.integration_sync(args.name)
    if args.command == "web":
        from brainkit.interfaces.web import run_web

        policy = service.vault.config().integrations["web"]
        if not policy.enabled:
            raise ValidationError(
                "Web integration is disabled",
                details={"hint": "Run bk integration configure web --enable"},
            )
        options = policy.options
        configured_origins = options.get("allowed_origins", [])
        run_web(
            service,
            host=args.host or str(options.get("host", "127.0.0.1")),
            port=args.port or int(options.get("port", 8765)),
            consumer=args.consumer or str(options.get("consumer", "human")),
            token_env=args.token_env or str(options.get("token_env", "")),
            instance_id=args.instance_id or "",
            allowed_origins=list(args.allowed_origin)
            or (
                [str(origin) for origin in configured_origins]
                if isinstance(configured_origins, list)
                else []
            ),
        )
        return None
    raise ValidationError("Unknown command")


def _read_json(path: str) -> dict[str, Any]:
    raw = sys.stdin.read() if path == "-" else Path(path).read_text(encoding="utf-8")
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValidationError("Expected a JSON object", details={"path": path})
    return value


def _interactive_policy_wizard() -> dict[str, Any]:
    if not sys.stdin.isatty():
        raise ValidationError(
            "Interactive init needs a terminal; use --config with a complete policy"
        )
    print("brainkit policy wizard — every question must be answered.")
    wiki_language = _ask("Wiki language", "Portuguese (Brazil)")
    branch_names = [
        item.strip()
        for item in _ask(
            "Raw branches (comma-separated)",
            "10-work,20-research,30-learning,90-personal",
        ).split(",")
        if item.strip()
    ]
    inbox_policy = _ask_policy("_inbox")
    branches = {branch: _ask_policy(branch) for branch in branch_names}
    providers = _ask_json(
        "Provider configuration JSON",
        {
            "ollama": {"base_url": "http://127.0.0.1:11434"},
        },
    )
    job_models = _ask_json(
        "Model-per-job JSON",
        {
            "ingest": {"provider": "ollama", "model": "qwen3:8b"},
            "query": {"provider": "ollama", "model": "qwen3:8b"},
            "digest": {"provider": "ollama", "model": "qwen3:8b"},
            "lint-semantic": {"provider": "ollama", "model": "qwen3:8b"},
            "file-proposal": {"provider": "ollama", "model": "qwen3:8b"},
            "resurface": {"provider": "ollama", "model": "qwen3:8b"},
        },
    )
    sources = _comma_values(_ask("Source folders/files (comma-separated)", ""))
    # Offered with the defaults filled in rather than as "anything to add?":
    # the operator has to see what is already excluded to judge it, and a
    # capture cannot be taken back once a watch has made it.
    ignore = _comma_values(
        _ask(
            "Ignore patterns for watched folders (comma-separated)",
            ",".join(DEFAULT_IGNORE_PATTERNS),
        )
    )
    digest_schedule = _ask("Morning digest cron schedule", "0 8 * * *")
    taxonomy = _comma_values(
        _ask("Taxonomy seed (comma-separated)", ",".join(branch_names))
    )
    novelty = _ask_json(
        "Novelty and freshness policy JSON",
        {
            "duplicate_similarity_threshold": 0.9,
            "min_new_token_ratio": 0.15,
            "stale_after_days": 30,
        },
    )
    integrations = _ask_json(
        "Persistent integrations JSON",
        {
            "obsidian": {"enabled": False, "managed": False, "options": {}},
            "neo4j": {"enabled": False, "managed": False, "options": {}},
            "postgres": {"enabled": False, "managed": False, "options": {}},
            "web": {
                "enabled": False,
                "managed": True,
                "options": {
                    "host": "127.0.0.1",
                    "port": 8765,
                    "consumer": "human",
                },
            },
        },
    )
    return {
        "version": 3,
        "wiki_language": wiki_language,
        "inbox_policy": inbox_policy,
        "branches": branches,
        "providers": providers,
        "job_models": job_models,
        "sources": sources,
        "ignore": ignore,
        "schedule": {"digest": digest_schedule},
        "taxonomy_seed": taxonomy,
        "novelty": novelty,
        "integrations": integrations,
    }


def _ask_policy(branch: str) -> dict[str, str]:
    privacy = _ask_choice(
        f"{branch} privacy",
        [mode.value for mode in PrivacyMode],
        PrivacyMode.LOCAL_ONLY.value,
    )
    filing = _ask_choice(
        f"{branch} filing",
        [mode.value for mode in FilingMode],
        FilingMode.APPROVE_EACH.value,
    )
    return {"privacy": privacy, "filing": filing}


def _ask(prompt: str, suggestion: str) -> str:
    suffix = f" [{suggestion}]" if suggestion else ""
    value = input(f"{prompt}{suffix}: ").strip()
    return value or suggestion


def _ask_choice(prompt: str, values: list[str], suggestion: str) -> str:
    while True:
        value = _ask(f"{prompt} ({'/'.join(values)})", suggestion)
        if value in values:
            return value
        print(f"Choose one of: {', '.join(values)}")


def _ask_json(prompt: str, suggestion: dict[str, Any]) -> dict[str, Any]:
    while True:
        raw = _ask(prompt, json.dumps(suggestion, ensure_ascii=False))
        try:
            value = json.loads(raw)
        except json.JSONDecodeError:
            print("Enter a valid JSON object.")
            continue
        if isinstance(value, dict):
            return value
        print("Enter a JSON object.")


def _comma_values(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def _consumer_for_args(args: argparse.Namespace) -> str:
    if args.consumer:
        return str(args.consumer)
    if args.json:
        raise ValidationError(
            "Machine-readable search/context requires --consumer",
            details={"choices": ["human", "local", "cloud"]},
        )
    return "human"


def _watch(
    service: BrainkitService, *, once: bool, interval: float, json_mode: bool
) -> dict[str, Any] | None:
    """Drive the watch loop; which files it may capture is a vault rule.

    The CLI owns only the loop and the interval. Selection lives in the
    application layer, because `bk watch` and any other caller must exclude the
    same paths — a capture is irreversible, so an interface that walked a
    folder its own way would file a dependency tree the vault said to skip.
    """

    if interval <= 0:
        raise ValidationError("Watch interval must be positive")
    while True:
        result = service.watch_once()
        if once:
            return result
        _emit(result, json_mode=json_mode)
        time.sleep(interval)


INSTRUCTION_FILES = {
    "claude": "CLAUDE.md",
    "codex": "AGENTS.md",
    "gemini": "GEMINI.md",
    "opencode": "AGENTS.md",
}
INSTRUCTION_START = "<!-- brainkit:start -->"
INSTRUCTION_END = "<!-- brainkit:end -->"
_MANAGED_BLOCK_RE = re.compile(
    rf"{re.escape(INSTRUCTION_START)}.*?{re.escape(INSTRUCTION_END)}\n?",
    re.DOTALL,
)


class ClaudeHook(NamedTuple):
    """A Claude Code hook brainkit ships, installs and registers."""

    template: str
    event: str
    matcher: str | None
    timeout: int


CLAUDE_HOOKS: tuple[ClaudeHook, ...] = (
    ClaudeHook("brainkit-gate", "PreToolUse", "Write|Edit|MultiEdit", 10),
    # SessionStart carries no matcher so every session source is covered; the
    # settings schema treats an absent matcher as "all", which is how the other
    # session-scoped hooks in a real settings file are written.
    ClaudeHook("brainkit-status", "SessionStart", None, 15),
)
HOOK_SENTINEL = "# brainkit:generated"

GATE_DENY_PREFIXES: tuple[str, ...] = ("wiki/", "raw/")
GATE_REMEDIATION: dict[str, str] = {
    "wiki/": "Wiki pages are written only by the apply gate. Use: bk apply",
    "raw/": "Sources are immutable and hash-identified. Use: bk capture",
}


def _agent_policy(agent: str, workspace: Path) -> dict[str, Any]:
    """The adapter file, written as policy the gate reads rather than prose.

    `rules` stays for the human who opens the vault. `gate` is what code reads,
    which is the whole point: metadata with a consumer stays accurate, and
    metadata without one rots into something that merely looks like enforcement.

    `workspace` records where the agent's configuration was installed, because
    that is not always the vault and nothing else on disk remembers it. Without
    it `bk status` would look for hooks beside the vault, find none, and report
    every layer off while they are in fact guarding the project correctly.
    """
    return {
        "agent": agent,
        "version": 2,
        "workspace": str(workspace),
        "gate": {
            "deny_prefixes": list(GATE_DENY_PREFIXES),
            "remediation": dict(GATE_REMEDIATION),
        },
        "rules": [
            "Read evidence with bk context --json --consumer local",
            "Write wiki pages only with bk apply",
            "Never edit raw content",
        ],
    }


def _agent_template(name: str, vault: Path) -> str:
    resource = files("brainkit").joinpath("templates", "agents", f"{name}.md")
    if not resource.is_file():
        raise ValidationError(
            "Agent template is missing from the installation",
            details={"template": name},
        )
    return resource.read_text(encoding="utf-8").replace("{{vault}}", str(vault))


def _hook_script(name: str, vault: Path, workspace: Path | None = None) -> str:
    """Render a shipped hook script with the vault and workspace baked in.

    Both paths are shell-quoted, not interpolated raw: real paths carry spaces
    and non-ASCII, and a hook that cannot parse its own argument would fail
    open on every write it was installed to govern.

    The workspace is separate because the git repository, the instruction file
    and the hooks live with the project, not with the vault. A script that
    looked for them beside the vault would report a live enforcement layer as
    OFF for every vault nested inside the project it guards.
    """
    resource = files("brainkit").joinpath("templates", "agents", f"{name}.sh")
    if not resource.is_file():
        raise ValidationError(
            "Agent hook script is missing from the installation",
            details={"script": name},
        )
    return (
        resource.read_text(encoding="utf-8")
        .replace("{{vault}}", shlex.quote(str(vault)))
        .replace("{{workspace}}", shlex.quote(str(workspace or vault)))
    )


def _install_skill(root: Path, vault: Path, *, force: bool) -> dict[str, Any]:
    """Install the Claude Code skill that teaches the vault contract."""
    skill = root / ".claude" / "skills" / "brainkit" / "SKILL.md"
    content = _agent_template("claude-skill", vault)
    if skill.is_file() and not force:
        if skill.read_text(encoding="utf-8") == content:
            return {"path": str(skill), "state": "current"}
        raise ValidationError(
            "A brainkit skill already exists; re-run with --force to replace it",
            details={"path": str(skill)},
        )
    updated = skill.is_file()
    skill.parent.mkdir(parents=True, exist_ok=True)
    skill.write_text(content, encoding="utf-8")
    return {"path": str(skill), "state": "updated" if updated else "created"}


def _install_instructions(root: Path, vault: Path, agent: str) -> dict[str, Any]:
    """Append the graph contract, replacing any block a previous run wrote.

    The block is fenced by HTML comments so re-running never duplicates it and
    never disturbs instructions the operator wrote around it.
    """
    target = root / INSTRUCTION_FILES[agent]
    block = (
        f"{INSTRUCTION_START}\n"
        f"{_agent_template('instructions', vault).strip()}\n"
        f"{INSTRUCTION_END}\n"
    )
    existing = target.read_text(encoding="utf-8") if target.is_file() else ""
    if INSTRUCTION_START in existing:
        # Replace in place so instructions written after the block keep their
        # position; a lambda avoids re.sub interpreting escapes in the block.
        content = _MANAGED_BLOCK_RE.sub(lambda _: block, existing, count=1)
        state = "current" if existing == content else "updated"
    else:
        stripped = existing.strip()
        content = f"{stripped}\n\n{block}" if stripped else block
        state = "appended" if stripped else "created"
    if content != existing:
        target.write_text(content, encoding="utf-8")
    return {"path": str(target), "state": state}


COMMIT_LINT_OFF = (
    "Commit-time linting is OFF: nothing runs bk lint --changed, so a page "
    "written outside the apply gate is only reported when somebody runs bk lint."
)


def _install_pre_commit(root: Path, vault: Path, *, force: bool) -> dict[str, Any]:
    """Install the lint hook when the workspace is a git repository.

    A missing repository is reported rather than raised: the skill and the
    instructions are useful on their own and have nothing to do with git. It is
    reported *loudly* — a bare `{"state": "skipped"}` reads as a detail, and the
    detail it hides is that a whole enforcement layer does not exist.

    The repository that matters is the workspace's, not the vault's: a vault
    nested in a project is committed through that project, so its pre-commit
    hook is the one a wiki write actually passes through.
    """
    git_dir = root / ".git"
    if not git_dir.is_dir():
        return {
            "state": "skipped",
            "reason": f"{root} is not a git repository",
            "hint": (
                "Run git init there, or point --root at the repository that "
                "tracks the vault, then install again"
            ),
            "enforcement": "off",
            "consequence": COMMIT_LINT_OFF,
        }
    hook = git_dir / "hooks" / "pre-commit"
    content = f"#!/bin/sh\nexec bk --vault {json.dumps(str(vault))} lint --changed\n"
    if hook.exists() and not force:
        if hook.read_text(encoding="utf-8") == content:
            return {"path": str(hook), "state": "current"}
        return {
            "path": str(hook),
            "state": "skipped",
            "reason": "a pre-commit hook already exists",
            "hint": "Merge brainkit lint into it, or re-run with --force",
            "enforcement": "off",
            "consequence": COMMIT_LINT_OFF,
        }
    updated = hook.exists()
    hook.parent.mkdir(parents=True, exist_ok=True)
    hook.write_text(content, encoding="utf-8")
    hook.chmod(0o755)
    return {"path": str(hook), "state": "updated" if updated else "created"}


def _write_hook_script(
    root: Path, vault: Path, hook: ClaudeHook, *, force: bool
) -> dict[str, Any]:
    """Write one hook script, refusing to clobber a file brainkit did not write.

    The sentinel comment is what makes a rewrite safe: a script carrying it is
    ours to replace, and a script without it belongs to the operator.
    """
    target = root / ".claude" / "hooks" / f"{hook.template}.sh"
    content = _hook_script(hook.template, vault, root)
    existed = target.is_file()
    if existed:
        existing = target.read_text(encoding="utf-8")
        if HOOK_SENTINEL not in existing and not force:
            return {
                "path": str(target),
                "state": "skipped",
                "reason": "an unmanaged script already occupies this path",
                "hint": "Move it aside, or re-run with --force",
            }
        if existing == content:
            # The mode, not the bytes, is what silently disables a hook.
            target.chmod(0o755)
            return {"path": str(target), "state": "current"}
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    target.chmod(0o755)
    return {"path": str(target), "state": "updated" if existed else "created"}


def _hook_command_registered(group: Sequence[Any], command: str) -> bool:
    """Whether any entry in a settings hook array already runs this command."""
    for entry in group:
        if not isinstance(entry, dict):
            continue
        commands = entry.get("hooks")
        if not isinstance(commands, list):
            continue
        for item in commands:
            if isinstance(item, dict) and item.get("command") == command:
                return True
    return False


def _register_claude_hooks(
    root: Path, entries: Sequence[tuple[ClaudeHook, str]]
) -> dict[str, Any]:
    """Register hook commands in `.claude/settings.json` without clobbering it.

    The file belongs to the operator and routinely carries unrelated tooling on
    the same events, so this reads, mutates and writes: unknown top-level keys
    survive, existing arrays are appended to rather than replaced, and a file
    that does not parse is left exactly as it is instead of being rebuilt. The
    idempotency key is the hook's command path, so a second install appends
    nothing and the file stays byte-identical.
    """
    target = root / ".claude" / "settings.json"
    settings: dict[str, Any] = {}
    existed = target.is_file()
    if existed:
        try:
            parsed = json.loads(target.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            return {
                "path": str(target),
                "state": "skipped",
                "reason": f"settings.json is not valid JSON: {exc}",
                "hint": "Repair the file, then run bk hooks install again",
            }
        if not isinstance(parsed, dict):
            return {
                "path": str(target),
                "state": "skipped",
                "reason": "settings.json is not a JSON object",
                "hint": "Repair the file, then run bk hooks install again",
            }
        settings = parsed

    hooks = settings.get("hooks", {})
    if not isinstance(hooks, dict):
        return {
            "path": str(target),
            "state": "skipped",
            "reason": "the hooks key in settings.json is not an object",
            "hint": "Repair the file, then run bk hooks install again",
        }
    for hook, _ in entries:
        if not isinstance(hooks.get(hook.event, []), list):
            return {
                "path": str(target),
                "state": "skipped",
                "reason": f"hooks.{hook.event} in settings.json is not an array",
                "hint": "Repair the file, then run bk hooks install again",
            }

    registered: list[dict[str, Any]] = []
    changed = False
    for hook, command in entries:
        group = list(hooks.get(hook.event, []))
        if _hook_command_registered(group, command):
            registered.append(
                {"event": hook.event, "command": command, "state": "current"}
            )
            continue
        entry: dict[str, Any] = {}
        if hook.matcher:
            entry["matcher"] = hook.matcher
        entry["hooks"] = [
            {"type": "command", "command": command, "timeout": hook.timeout}
        ]
        group.append(entry)
        hooks[hook.event] = group
        registered.append(
            {"event": hook.event, "command": command, "state": "appended"}
        )
        changed = True

    if not changed:
        return {"path": str(target), "state": "current", "registered": registered}
    settings["hooks"] = hooks
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(settings, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return {
        "path": str(target),
        "state": "updated" if existed else "created",
        "registered": registered,
    }


def _install_claude_hook(
    root: Path, vault: Path, *, force: bool = False
) -> dict[str, Any]:
    """Install the Claude Code hooks and register them in settings.json.

    The instructions and the skill *ask* the model to route writes through the
    apply gate. This is the part that does not depend on the model agreeing: a
    PreToolUse hook that refuses the write while it is being attempted, and a
    SessionStart hook that says what the vault looks like before the first one.
    """
    scripts: dict[str, Any] = {}
    registrable: list[tuple[ClaudeHook, str]] = []
    for hook in CLAUDE_HOOKS:
        outcome = _write_hook_script(root, vault, hook, force=force)
        scripts[hook.template] = outcome
        if outcome["state"] != "skipped":
            registrable.append((hook, str(outcome["path"])))
    return {"scripts": scripts, "settings": _register_claude_hooks(root, registrable)}


def _enforcement_layer(
    name: str, mechanism: str, outcome: dict[str, Any], *, active: bool
) -> dict[str, Any]:
    layer: dict[str, Any] = {"layer": name, "mechanism": mechanism, "active": active}
    if not active:
        for key in ("reason", "hint", "consequence"):
            if outcome.get(key):
                layer[key] = outcome[key]
    return layer


def _enforcement_summary(result: dict[str, Any]) -> dict[str, Any]:
    """Name every enforcement layer and say plainly whether it is on.

    An installer that reports only what it managed to write hides the layers it
    could not, which is exactly how an invariant ends up guarded by nothing at
    all while the install output still reads like success.
    """
    layers: list[dict[str, Any]] = []
    hook = result.get("claude_hook")
    if isinstance(hook, dict):
        settings = hook["settings"]
        registered = {
            str(item["command"]) for item in settings.get("registered", [])
        }
        for name, template, mechanism in (
            (
                "write_gate",
                "brainkit-gate",
                "Claude Code PreToolUse hook on Write|Edit|MultiEdit",
            ),
            (
                "session_status",
                "brainkit-status",
                "Claude Code SessionStart hook reporting vault state",
            ),
        ):
            script = hook["scripts"][template]
            active = script["state"] != "skipped" and script["path"] in registered
            outcome = script if script["state"] == "skipped" else settings
            layers.append(
                _enforcement_layer(name, mechanism, outcome, active=active)
            )
    pre_commit = result["pre_commit"]
    layers.append(
        _enforcement_layer(
            "commit_lint",
            ".git/hooks/pre-commit running bk lint --changed",
            pre_commit,
            active=pre_commit["state"] != "skipped",
        )
    )
    layers.append(
        {
            "layer": "instructions",
            "mechanism": f"{INSTRUCTION_FILES[result['agent']]} managed block",
            "active": True,
            "advisory": True,
        }
    )
    return {
        "layers": layers,
        "inactive": [layer["layer"] for layer in layers if not layer["active"]],
    }


def _warn_about_inactive_enforcement(
    enforcement: dict[str, Any], advisory: dict[str, Any] | None = None
) -> None:
    """Put every missing enforcement layer on stderr, where it cannot be piped away."""
    if advisory is not None:
        print("", file=sys.stderr)
        print("bk: WORKSPACE - everything installed, nothing will load:", file=sys.stderr)
        print(f"      {advisory['reason']}", file=sys.stderr)
        print(f"      {advisory['hint']}", file=sys.stderr)
    inactive = [layer for layer in enforcement["layers"] if not layer["active"]]
    if not inactive:
        if advisory is not None:
            print("", file=sys.stderr)
        return
    print("", file=sys.stderr)
    print("bk: ENFORCEMENT GAP - these layers are NOT active:", file=sys.stderr)
    for layer in inactive:
        print(f"  - {layer['layer']}: {layer['mechanism']}", file=sys.stderr)
        for key in ("reason", "consequence", "hint"):
            if layer.get(key):
                print(f"      {layer[key]}", file=sys.stderr)
    print("", file=sys.stderr)


def _resolve_workspace(vault: Path, root: str | None) -> Path:
    """Where the agent's configuration belongs, which is not always the vault.

    An agent reads `.claude/` and `CLAUDE.md` from the project it was opened
    on. A vault nested inside that project is a different directory, and
    installing into it produces the worst possible outcome: every file lands,
    the installer reports success, and not one hook is ever loaded. So the
    caller may name the workspace, and the default stays the vault because
    that is what a standalone vault wants.
    """
    if root is None:
        return vault
    candidate = Path(root).expanduser()
    if not candidate.is_dir():
        raise ValidationError(
            "The --root workspace must be an existing directory",
            details={"root": str(candidate)},
        )
    return candidate.resolve()


def _workspace_advisory(vault: Path, workspace: Path) -> dict[str, Any] | None:
    """Warn when the vault was used as a workspace it does not look like.

    Silence here is what the separation exists to prevent, so this fires on
    the shape of the mistake rather than on certainty about it: a vault with
    no agent configuration of its own, sitting inside a directory that has
    some. Advisory only -- a standalone vault is a legitimate workspace, and
    an operator who passed --root has already answered the question.
    """
    if workspace != vault:
        return None
    if (vault / ".claude").is_dir() or (vault / ".git").is_dir():
        return None
    for parent in vault.parents:
        if (parent / ".claude").is_dir() or (parent / ".git").is_dir():
            return {
                "state": "advisory",
                "reason": (
                    "The vault is not a project root, so an agent opened on "
                    f"{parent} will never load what was just installed here."
                ),
                "hint": f"Reinstall with --root {parent}",
            }
    return None


def _install_hooks(
    service: BrainkitService,
    agent: str,
    *,
    root: str | None = None,
    force: bool = False,
) -> dict[str, Any]:
    vault = service.vault.root
    workspace = _resolve_workspace(vault, root)
    # Decided before anything is written: the installer creates `.claude/` in
    # the workspace, and reading that back afterwards would be this check
    # observing its own side effect and concluding all is well.
    advisory = _workspace_advisory(vault, workspace)
    adapter_path = f".brain/agent-{agent}.json"
    service.vault.write_generated(
        adapter_path, json.dumps(_agent_policy(agent, workspace), indent=2) + "\n"
    )
    result: dict[str, Any] = {
        "agent": agent,
        "adapter": adapter_path,
        "workspace": str(workspace),
        "instructions": _install_instructions(workspace, vault, agent),
        "pre_commit": _install_pre_commit(workspace, vault, force=force),
    }
    if agent == "claude":
        result["skill"] = _install_skill(workspace, vault, force=force)
        result["claude_hook"] = _install_claude_hook(workspace, vault, force=force)
    if advisory is not None:
        result["workspace_advisory"] = advisory
    result["enforcement"] = _enforcement_summary(result)
    _warn_about_inactive_enforcement(result["enforcement"], advisory)
    return result


def _vaults(args: argparse.Namespace) -> dict[str, Any]:
    registry = VaultRegistry()
    if args.vaults_command == "register":
        return registry.register(_vault_to_register(args), label=args.label)
    if args.vaults_command == "list":
        return registry.describe()
    if args.vaults_command == "forget":
        return registry.forget(args.selector)
    if args.vaults_command == "sync":
        if args.consumer:
            # The same position `export` takes for a single vault, and the
            # stakes are higher here: a sync refreshes by deleting the vault's
            # rows first, so a narrowed run would quietly replace what a shared
            # store already holds with less, across every registered vault at
            # once. The boundary stays where it is declared.
            raise ValidationError(
                "bk vaults sync takes each vault's consumer from its own policy",
                details={
                    "consumer": args.consumer,
                    "hint": (
                        "Set it per vault with bk --vault <path> integration "
                        f"configure {args.target} --consumer"
                    ),
                },
            )
        return _sync_registered_vaults(registry, args.target)
    raise ValidationError("Unknown vaults command")


def _vault_to_register(args: argparse.Namespace) -> Path:
    named = args.path or args.vault
    return Path(named) if named else FileVault.discover().root


def _sync_registered_vaults(
    registry: VaultRegistry, target: str
) -> dict[str, Any]:
    """Sync every registered vault, letting each one succeed or fail alone.

    This exists to refresh a shared store from many applications in one run, so
    a single vault must not decide the fate of the others: an unmounted disk, a
    service that is down, a project deleted without being unregistered. Each
    vault is reported with its own outcome and the summary carries the counts
    the exit status is derived from.
    """
    results = [_sync_one_vault(entry, target) for entry in registry.entries()]
    counts = Counter(str(result["status"]) for result in results)
    return {
        "target": target,
        "registry": str(registry.path),
        "count": len(results),
        "ok": counts["ok"],
        "skipped": counts["skipped"],
        "failed": counts["failed"],
        "vaults": results,
    }


def _sync_one_vault(entry: RegisteredVault, target: str) -> dict[str, Any]:
    report: dict[str, Any] = {
        "vault": str(entry.path),
        "label": entry.label,
        "vault_id": vault_id(entry.path),
    }
    try:
        service = create_service(str(entry.path))
        if not service.vault.config().integrations[target].enabled:
            # A vault that has not opted into this integration is skipped, not
            # failed. Enabling it here would make a machine-level command
            # override a decision the vault owns, and writing its evidence into
            # a store it deliberately stayed out of is not a recoverable
            # mistake.
            return {
                **report,
                "status": "skipped",
                "reason": f"{target} is not enabled in this vault's policy",
            }
        result = service.integration_sync(target)
    except BrainkitError as exc:
        return {
            **report,
            "status": "failed",
            "code": exc.code,
            "reason": str(exc),
            "details": exc.details,
        }
    except Exception as exc:
        # Whatever an adapter raises, it belongs to one vault. The loop is the
        # feature; a traceback that escaped it would cancel every vault behind
        # this one.
        return {
            **report,
            "status": "failed",
            "code": "internal_error",
            "reason": f"{type(exc).__name__}: {exc}".strip(),
            "details": {},
        }
    return {**report, "status": "ok", "result": result}


def _schedule(service: BrainkitService) -> dict[str, Any]:
    schedules = service.vault.config().schedule
    return {
        "jobs": [
            {
                "job": job,
                "schedule": expression,
                "command": f"bk --vault {shlex.quote(str(service.vault.root))} {job}",
            }
            for job, expression in schedules.items()
        ],
        "ownership": "Register these jobs with cron or the gateway agent that owns the user channel.",
    }


def _extract_global_options(argv: list[str]) -> tuple[list[str], dict[str, Any]]:
    result: list[str] = []
    values: dict[str, Any] = {"json": False, "vault": None}
    index = 0
    while index < len(argv):
        token = argv[index]
        if token == "--json":
            values["json"] = True
            index += 1
            continue
        if token == "--vault":
            if index + 1 >= len(argv):
                raise SystemExit("--vault requires a path")
            values["vault"] = argv[index + 1]
            index += 2
            continue
        if token.startswith("--vault="):
            values["vault"] = token.split("=", 1)[1]
            index += 1
            continue
        result.append(token)
        index += 1
    return result, values


def _emit(value: Any, *, json_mode: bool) -> None:
    if json_mode:
        print(json.dumps({"ok": True, "result": value}, ensure_ascii=False))
        return
    if isinstance(value, dict) and isinstance(value.get("answer"), str):
        print(value["answer"])
        if value.get("saved_to"):
            print(f"\nSaved to {value['saved_to']}")
        return
    if isinstance(value, dict) and isinstance(value.get("digest"), str):
        print(value["digest"])
        print(f"\nSaved to {value['path']}")
        return
    print(json.dumps(value, indent=2, ensure_ascii=False))


def _emit_gate(decision: Any, *, json_mode: bool) -> int:
    """Report a gate decision and return the process exit code.

    Exit 0 is allowed and exit 2 is denied, so a hook can branch on the status
    alone. The denial text goes to stderr because that is the stream a Claude
    Code hook feeds back to the model.
    """
    if not isinstance(decision, dict) or "allowed" not in decision:
        raise InternalError("The gate returned no decision")
    allowed = bool(decision["allowed"])
    if json_mode:
        print(json.dumps(decision, ensure_ascii=False))
    elif allowed:
        print(f"allowed: {decision.get('path', '')}")
    else:
        reason = str(decision.get("reason") or "This write is not permitted")
        remediation = str(decision.get("remediation") or "")
        line = f"bk: {reason} {remediation}".rstrip()
        print(line, file=sys.stderr)
    return 0 if allowed else 2


def _internal_error(exc: BaseException) -> InternalError:
    """Summarize an unmodelled failure; the traceback stays on stderr."""

    print("bk: unhandled internal error", file=sys.stderr)
    traceback.print_exception(exc, file=sys.stderr)
    sys.stderr.flush()
    name = type(exc).__name__
    message = str(exc).strip()
    return InternalError(
        f"{name}: {message}" if message else name,
        details={"kind": name},
    )


def _emit_error(error: BrainkitError, *, json_mode: bool) -> None:
    payload = {
        "ok": False,
        "error": {
            "code": error.code,
            "message": str(error),
            "details": error.details,
        },
    }
    stream = sys.stdout if json_mode else sys.stderr
    if json_mode:
        print(json.dumps(payload, ensure_ascii=False), file=stream)
    else:
        print(f"bk: {error}", file=stream)
        if error.details:
            print(json.dumps(error.details, indent=2, ensure_ascii=False), file=stream)


if __name__ == "__main__":
    raise SystemExit(main())
