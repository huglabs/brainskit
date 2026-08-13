from __future__ import annotations

import argparse
import importlib
import json
import re
import shlex
import subprocess
import sys
import time
import traceback
from collections import Counter
from collections.abc import Callable, Sequence
from importlib.resources import files
from pathlib import Path
from typing import IO, Any, NamedTuple

from brainskit import __version__
from brainskit.application.gate import (
    DEFAULT_DENY_PREFIXES,
    HOOK_SENTINEL,
    INSTRUCTION_END,
    INSTRUCTION_START,
)
from brainskit.application.health import redirected_git_hooks_path
from brainskit.application.services import BrainskitService
from brainskit.domain.model import (
    BrainskitError,
    NotConfiguredError,
    NotFoundError,
    PolicyError,
    ValidationError,
)
from brainskit.infrastructure import pyenv
from brainskit.infrastructure.extractor import (
    GraphifyExtractor,
    _extension_grammars,
    grammar_inventory,
)
from brainskit.infrastructure.graph import MarkdownGraph
from brainskit.infrastructure.index import SqliteFtsIndex
from brainskit.infrastructure.integrations import NativeIntegrations, vault_id
from brainskit.infrastructure.llm import JobSpecs, PolicyJudgmentRouter
from brainskit.infrastructure.vault import FileVault
from brainskit.infrastructure.vaults import RegisteredVault, VaultRegistry
from brainskit.interfaces import console, onboarding, prompt


class InternalError(BrainskitError):
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
    init.add_argument("path", nargs="?", default=".", help="Directory to create the vault in (default: current directory)")
    init.add_argument(
        "--config", help="Complete policy JSON path, or - for standard input"
    )
    init.add_argument(
        "--print-config",
        action="store_true",
        help=(
            "Print a complete, schema-valid policy on stdout and exit without "
            "creating anything -- the non-interactive path for CI, containers "
            "and agents. Pipe it back in with --config"
        ),
    )
    init.add_argument(
        "--preset",
        default="work",
        help="Branch layout for --print-config (default: work)",
    )
    init.add_argument(
        "--force",
        action="store_true",
        help=(
            "Create the vault even in a directory that holds other projects, "
            "or over a .brain/ left behind by a lost setup"
        ),
    )
    # `hooks install` has had this since the code-graph bootstrap landed; `init`
    # runs the same bootstrap and had no way to decline it.
    init.add_argument(
        "--skip-code-build",
        action="store_true",
        help="Do not build the code graph while onboarding the agent",
    )

    capture = commands.add_parser("capture", help="Capture a file, URL, or text")
    capture.add_argument("source", nargs="?", help="File path, URL, or - to read text from standard input")
    capture.add_argument("--text", help="Capture literal text")
    capture.add_argument("--title", help="Title for captured text or a URL, when the source has none")

    commands.add_parser("status", help="Show vault health and counts")
    commands.add_parser(
        "doctor", help="Check the installation itself: deps, grammars, code root"
    )
    commands.add_parser("reconcile", help="Heal registry paths after manual moves")
    commands.add_parser("reindex", help="Rebuild the disposable FTS5 index")

    file_command = commands.add_parser("file", help="Move a raw source to a branch")
    file_command.add_argument("item", help="Full/prefix hash or raw path")
    file_command.add_argument("--to", required=True, dest="branch", help="Destination branch, e.g. 20-knowledge")

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
    lint.add_argument("--changed", action="store_true", help="Only files staged in git, for a pre-commit hook")
    lint.add_argument("--semantic", action="store_true", help="Also run the model-judged consistency pass")

    search = commands.add_parser("search", help="Search with FTS5 BM25")
    search.add_argument("query", help="Full-text query")
    search.add_argument("--limit", type=int, default=10, help="Maximum hits to return (default: 10)")
    search.add_argument("--consumer", choices=["human", "local", "cloud"], help="Privacy boundary to read under; required with --json")

    context = commands.add_parser("context", help="Build a bounded evidence bundle")
    context.add_argument("query", help="Topic to gather evidence for")
    context.add_argument("--limit", type=int, default=8, help="Maximum evidence items to bundle (default: 8)")
    context.add_argument("--max-chars", type=int, default=24_000, help="Truncate the bundle to roughly this many characters")
    context.add_argument("--consumer", choices=["human", "local", "cloud"], help="Privacy boundary to read under; required with --json")

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
    gate_check.add_argument(
        "path",
        metavar="PATH",
        help=(
            "Path a tool wants to write. A relative path resolves against the "
            "current directory, like every other command; the installed hook "
            "passes absolute paths and is unaffected"
        ),
    )
    gate_check.add_argument(
        "--agent", default="claude", help="Policy adapter to consult"
    )

    # Model-inferred edges are stored apart from `graph/graph.json` and joined
    # at read time: that file is a projection and would destroy them on the
    # next `bk graph`. See `application/enrichment.py`.
    enrich = commands.add_parser(
        "enrich", help="Model-inferred graph edges, gated and kept apart"
    )
    enrich_sub = enrich.add_subparsers(dest="enrich_command", required=True)
    enrich_apply = enrich_sub.add_parser(
        "apply", help="Validate and store proposed edges"
    )
    enrich_apply.add_argument("proposal", help="Proposal JSON path, or - for stdin")
    enrich_list = enrich_sub.add_parser("list", help="Stored edges, privacy-filtered")
    enrich_list.add_argument("--consumer", choices=["human", "local", "cloud"], help="Privacy boundary to read under; required with --json")
    enrich_forget = enrich_sub.add_parser("forget", help="Drop one stored edge")
    enrich_forget.add_argument("id", metavar="ID", help="Full or prefix edge id")

    commands.add_parser("views", help="Regenerate Obsidian views")
    graph = commands.add_parser("graph", help="Regenerate the knowledge graph")
    graph.add_argument("--html", action="store_true", help="Also write a standalone graph/graph.html viewer")
    # A file target, so it defaults to `local` like every other one: the written
    # artifact used to carry never-ingest hashes, filenames and branch names
    # with nothing on it saying which boundary it was built under.
    graph.add_argument(
        "--consumer",
        choices=["human", "local", "cloud"],
        help="Privacy boundary the written graph is built inside (default: local)",
    )

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
    # A grammar that is not installed does not fail the build -- it removes a
    # language from it, silently, and the graph then reports itself complete.
    # The scan is surveyed first so the shortfall is offered before the time is
    # spent, and the offer is a question rather than an action because
    # installing into someone's environment is not a build step's decision.
    code_build.add_argument(
        "--install-missing",
        dest="install_missing",
        action="store_true",
        default=None,
        help="Install any missing tree-sitter grammars without asking",
    )
    code_build.add_argument(
        "--no-install-missing",
        dest="install_missing",
        action="store_false",
        help="Never install; report the shortfall and build without them",
    )

    code_import = code_commands.add_parser(
        "import", help="Import an extractor's graph as graph/code.json"
    )
    code_import.add_argument("graph", metavar="GRAPH_JSON", help="Extractor output as JSON, or - for standard input")

    code_commands.add_parser(
        "status", help="Whether the graph still describes the repository"
    )

    code_affected = code_commands.add_parser(
        "affected", help="What breaks if this symbol changes"
    )
    code_affected.add_argument("symbol", help="Symbol id or label to traverse backwards from")
    code_affected.add_argument("--depth", type=int, default=2, help="How many edges to follow backwards (default: 2)")

    code_path = code_commands.add_parser(
        "path", help="Shortest chain of edges between two symbols"
    )
    code_path.add_argument("source", help="Symbol to start from")
    code_path.add_argument("target", help="Symbol to reach")

    code_hubs = code_commands.add_parser("hubs", help="The most connected symbols")
    code_hubs.add_argument("--top", type=int, default=10, help="How many of the most connected symbols to return (default: 10)")

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
    code_cycles.add_argument("--top", type=int, default=20, help="How many cycles to return, shortest first (default: 20)")

    code_diff = code_commands.add_parser(
        "diff", help="What changed structurally since the stored graph"
    )
    code_diff.add_argument(
        "graph",
        nargs="?",
        metavar="GRAPH_JSON",
        help=(
            "Diff against this extractor-shaped graph (same shape as `code "
            "import`, not brainskit's own stored graph/code.json) instead of "
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
        help="Export format: json, graphml, cypher, kuzu, llms-txt, or an integration")
    export.add_argument(
        "--consumer",
        choices=["human", "local", "cloud"],
        default="local",
        help=(
            "Privacy boundary written into the export; defaults to local, "
            "which excludes never-ingest branches"
        ),
    )
    export.add_argument(
        "--enrichment",
        action="store_true",
        help="Also emit model-inferred edges, each marked provenance: model",
    )

    ingest = commands.add_parser("ingest", help="Run the configured ingest judgment")
    ingest.add_argument("item", nargs="?", help="Source hash or path to ingest")
    ingest.add_argument("--all", action="store_true", dest="all_pending", help="Ingest every pending source instead of one")
    ingest.add_argument("--to", dest="target_branch", help="File into this branch instead of asking the model to choose")

    proposals = commands.add_parser(
        "proposals", help="List filing and wiki proposals"
    )
    proposals.add_argument(
        "--status", choices=["pending", "applied", "rejected", "failed"],
        help="Only proposals in this state, e.g. pending")

    approve = commands.add_parser("approve", help="Approve a pending proposal")
    approve.add_argument("proposal_id", help="Proposal to approve")

    reject = commands.add_parser("reject", help="Reject a pending proposal")
    reject.add_argument("proposal_id", help="Proposal to reject")
    reject.add_argument("--reason", default="", help="Why it was rejected, recorded with the proposal")

    ask = commands.add_parser("ask", help="Answer from compiled vault evidence")
    ask.add_argument("question", help="Question to answer from the vault")
    ask.add_argument("--save", action="store_true", help="Write the answer into output/answers/ as well as printing it")

    digest = commands.add_parser("digest", help="Generate the configured digest")
    digest.add_argument("--since", default="7d", help="Only sources captured on or after this ISO date")
    commands.add_parser("resurface", help="Resurface one durable insight")

    serve = commands.add_parser("serve", help="Serve robot interfaces")
    serve.add_argument("--mcp", action="store_true", required=True, help="Serve MCP instead of the web viewer")
    serve.add_argument(
        "--transport", choices=["stdio", "http"], default="stdio",
        help="MCP transport: stdio (default) or http")
    serve.add_argument("--host", default="127.0.0.1", help="Interface to bind; non-loopback requires --token-env")
    serve.add_argument("--port", type=int, default=8766, help="Port to bind")
    serve.add_argument("--token-env", help="Environment variable holding the bearer token")
    serve.add_argument("--allowed-origin", action="append", default=[], help="Extra allowed Origin header; repeatable")
    serve.add_argument("--tls-cert", help="PEM certificate, to serve over HTTPS")
    serve.add_argument("--tls-key", help="PEM private key matching --tls-cert")

    watch = commands.add_parser("watch", help="Watch configured source folders")
    watch.add_argument("--once", action="store_true", help="Scan the inbox a single time and exit")
    watch.add_argument("--interval", type=float, default=5.0, help="Seconds between scans when running continuously")

    hooks = commands.add_parser("hooks", help="Install agent-facing vault hooks")
    hooks_sub = hooks.add_subparsers(dest="hooks_command", required=True)
    hooks_install = hooks_sub.add_parser("install")
    hooks_install.add_argument(
        "--agent",
        required=True,
        choices=["claude", "codex", "gemini", "opencode"],
        help="Which agent's configuration to write")
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
            "Overwrite an existing brainskit skill, pre-commit hook and hook "
            "scripts; settings.json is merged, never overwritten"
        ),
    )
    hooks_install.add_argument(
        "--skip-code-build",
        action="store_true",
        help=(
            "Do not run bk code build as part of onboarding; leave the code "
            "graph exactly as bk code status finds it"
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
        "name", choices=["obsidian", "neo4j", "postgres", "web"],
        help="Integration to configure")
    enabled_group = integration_configure.add_mutually_exclusive_group()
    enabled_group.add_argument(
        "--enable", action="store_true", default=None,
        help="Turn the integration on")
    enabled_group.add_argument(
        "--disable", action="store_true", default=None,
        help="Turn the integration off, leaving its settings")
    managed_group = integration_configure.add_mutually_exclusive_group()
    managed_group.add_argument(
        "--managed", action="store_true", default=None,
        help="Let brainskit run the container for this service")
    managed_group.add_argument(
        "--external", action="store_true", default=None,
        help="The operator runs the service; brainskit only connects")
    integration_configure.add_argument("--path", help="Destination directory (Obsidian)")
    integration_configure.add_argument("--subdirectory", help="Folder inside the destination to own (Obsidian)")
    integration_configure.add_argument(
        "--include-raw", action="store_true", default=None,
        help="Also copy raw/ evidence the consumer may see (Obsidian)")
    integration_configure.add_argument("--uri", help="Bolt URI, e.g. bolt://127.0.0.1:7687 (Neo4j)")
    integration_configure.add_argument("--user", help="Database or graph user")
    integration_configure.add_argument("--password-env", help="Environment variable holding the password")
    integration_configure.add_argument("--database", help="Database name to write into")
    integration_configure.add_argument("--dsn-env", help="Environment variable holding a full DSN (PostgreSQL)")
    integration_configure.add_argument("--schema", help="Schema to own (PostgreSQL, default: brainskit)")
    integration_configure.add_argument("--image", help="Container image to run when managed")
    integration_configure.add_argument("--container-name", help="Name for the managed container")
    integration_configure.add_argument("--host", help="Interface to bind")
    integration_configure.add_argument("--port", type=int, help="Port to bind or connect to")
    integration_configure.add_argument("--http-port", type=int, help="HTTP port (Neo4j browser)")
    integration_configure.add_argument("--bolt-port", type=int, help="Bolt port (Neo4j)")
    integration_configure.add_argument("--token-env", help="Environment variable holding the bearer token")
    integration_configure.add_argument(
        "--consumer", choices=["human", "local", "cloud"],
        help="Privacy boundary this integration receives")
    integration_status = integration_sub.add_parser("status")
    integration_status.add_argument(
        "name", nargs="?", choices=["obsidian", "neo4j", "postgres", "web"],
        help="Integration to report on; omit for all")
    for operation in ("up", "down", "sync"):
        command = integration_sub.add_parser(operation)
        command.add_argument(
            "name",
            choices=["obsidian", "neo4j", "postgres", "web"],
            help=f"Integration to {operation}",
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
    web.add_argument("--host", help="Interface to bind; non-loopback requires --token-env")
    web.add_argument("--port", type=int, help="Port to bind")
    web.add_argument("--consumer", choices=["human", "local", "cloud"], help="Privacy boundary the viewer reads under; writes need human")
    web.add_argument("--token-env", help="Environment variable holding the bearer token")
    web.add_argument(
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
    web.add_argument("--instance-id", help=argparse.SUPPRESS)
    web.add_argument(
        "--no-browser",
        action="store_true",
        help="Do not open a browser automatically (scripted, CI, or headless use)",
    )
    web_sub = web.add_subparsers(dest="web_command")
    web_serve = web_sub.add_parser("serve", help="Compatibility alias for `bk web`")
    web_serve.add_argument(
        "--host", help="Interface to bind; non-loopback requires --token-env"
    )
    web_serve.add_argument("--port", type=int, help="Port to bind")
    web_serve.add_argument("--consumer", choices=["human", "local", "cloud"], help="Privacy boundary the viewer reads under; writes need human")
    web_serve.add_argument(
        "--token-env", help="Environment variable holding the bearer token"
    )
    web_serve.add_argument(
        "--allowed-origin",
        action="append",
        default=[],
        help=argparse.SUPPRESS,
    )
    web_serve.add_argument("--instance-id", help=argparse.SUPPRESS)
    web_serve.add_argument("--no-browser", action="store_true", help=argparse.SUPPRESS)
    _document_global_options(parser)
    return parser


#: `--vault` and `--json` are extracted from argv before the subparsers ever
#: see them, so they appear in `bk --help` and in no subcommand's help at all.
#: An agent reading `bk search --help` -- which is the only contract it has --
#: had no way to learn either exists.
GLOBAL_OPTIONS_EPILOG = (
    "global options (accepted anywhere in the command):\n"
    "  --vault PATH  vault root, otherwise discovered from the current directory\n"
    "  --json        machine-readable output on stdout"
)


def _document_global_options(parser: argparse.ArgumentParser) -> None:
    """Name the global options in every leaf command's help.

    Applied by walking the finished tree rather than at each `add_parser` call,
    so a subcommand added later is covered without anyone remembering to.
    """

    children = [
        sub
        for action in parser._actions
        if isinstance(action, argparse._SubParsersAction)
        for sub in action.choices.values()
    ]
    if not children:
        if parser.epilog is None:
            parser.epilog = GLOBAL_OPTIONS_EPILOG
            parser.formatter_class = argparse.RawDescriptionHelpFormatter
        return
    for child in children:
        _document_global_options(child)


#: Every top-level command, grouped for `bk --help`. A command absent from
#: every tuple here still appears (see `_grouped_help`'s "Other" fallback),
#: so a future command can go unlisted in a *section* but never vanish
#: entirely -- and the mechanical test in test_fix_interfaces.py asserts the
#: stronger claim, that nothing ever lands in "Other" unnoticed.
HELP_CATEGORIES: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "Vault & capture",
        ("init", "capture", "status", "doctor", "reconcile", "reindex", "file", "forget", "watch", "lint"),
    ),
    ("Search & context", ("search", "context", "ask")),
    ("Code graph", ("code",)),
    ("Filing & review", ("ingest", "proposals", "approve", "reject", "apply")),
    ("Automation", ("digest", "resurface", "views", "graph", "enrich", "schedule")),
    ("Governance", ("gate", "hooks")),
    ("Integrations & registry", ("export", "integration", "vaults")),
    ("Serve", ("serve", "web")),
)


def _wants_top_level_help(argv: Sequence[str]) -> bool:
    """Is this `-h`/`--help` for `bk` itself, rather than for a subcommand?

    Scans left to right and stops at the first token that could be the
    command: if that token is the help flag, this is top-level help; if it
    is anything else (the command name), argparse's own per-subcommand help
    already handles it correctly and is left untouched.

    Naming no command at all counts. A bare `bk` is someone asking what this
    thing does, and argparse's answer -- a usage block listing thirty commands
    on one line, then `error: the following arguments are required: command`
    -- is the least useful reply available to a first-time reader. Note this
    is deliberately "the token list is empty", not "it held no command":
    `bk --version` exhausts the loop without one and must still reach argparse.
    """

    if not argv:
        return True
    for token in argv:
        if token in ("-h", "--help"):
            return True
        if not token.startswith("-"):
            return False
    return False


def _command_help_index(parser: argparse.ArgumentParser) -> dict[str, str]:
    """The one-line help string each top-level command was registered with.

    Argparse keeps this only on the subparsers action's `_choices_actions`,
    with no public accessor -- reading it here avoids hand-duplicating every
    command's description a second time in `HELP_CATEGORIES`.
    """

    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return {
                choice.dest: choice.help or "" for choice in action._choices_actions
            }
    return {}


def _grouped_help(parser: argparse.ArgumentParser) -> str:
    # Argparse's own usage line spells out all thirty command names in one
    # brace-delimited blob, which wraps to several lines and is unreadable at
    # exactly the moment someone is trying to find their bearings. The grouped
    # listing below *is* the enumeration; the usage line only has to show the
    # shape of an invocation.
    help_index = _command_help_index(parser)
    usage = (
        console.style("usage:", console.MUTED)
        + " bk [--vault PATH] [--json] "
        + console.style("<command>", console.BOLD)
        + " [args]"
    )
    lines = [console.banner(), "", usage, ""]
    grouped = {name for _, names in HELP_CATEGORIES for name in names}
    for title, names in HELP_CATEGORIES:
        lines.append(console.style(title, console.BOLD, console.ACCENT))
        for name in names:
            lines.append(f"  {name:<12} {help_index.get(name, '')}".rstrip())
        lines.append("")
    leftover = sorted(set(help_index) - grouped)
    if leftover:
        lines.append(console.style("Other", console.BOLD, console.ACCENT))
        for name in leftover:
            lines.append(f"  {name:<12} {help_index.get(name, '')}".rstrip())
        lines.append("")
    lines.append("Run `bk <command> --help` for that command's own flags.")
    return "\n".join(lines).rstrip() + "\n"


def _browse_commands(parser: argparse.ArgumentParser) -> int:
    """Walk the command set two levels deep instead of printing all thirty.

    Progressive disclosure, on the reasoning that the first question a reader
    actually has is not "which of thirty commands" but "which *kind* of thing
    am I doing" -- and there are eight of those. Picking a group narrows to a
    handful; picking a command prints that command's own `--help`, which is
    where the flags live and which this function deliberately does not try to
    reproduce.

    Esc goes back one level and Esc at the top exits cleanly, so the browser
    is `prompt.Cancelled` handling and nothing more -- no new primitive, and
    no state to get out of step with the parser.

    Nothing here runs a command. A browser that executed what it highlighted
    would make `bk` -- typed by someone who does not yet know what these do --
    the most dangerous keystroke in the tool.
    """

    index = _command_help_index(parser)
    groups: list[prompt.Choice[tuple[str, tuple[str, ...]] | None]] = [
        prompt.Choice(
            (title, names),
            title,
            f"{len(names)} command{'s' if len(names) != 1 else ''}",
        )
        for title, names in HELP_CATEGORIES
    ]
    groups.append(prompt.Choice(None, "Show every command", "the full flat list"))

    print(console.banner())
    while True:
        try:
            group = prompt.select(
                "What are you trying to do?",
                groups,
                hint="↑↓ · Enter · Esc quits",
                quiet=True,
            )
        except prompt.Cancelled:
            return 0
        if group is None:
            print(_grouped_help(parser))
            return 0
        title, names = group
        commands: list[prompt.Choice[str]] = [
            prompt.Choice(name, name, index.get(name, "")) for name in names
        ]
        try:
            chosen = prompt.select(
                title, commands, hint="↑↓ · Enter · Esc goes back", quiet=True
            )
        except prompt.Cancelled:
            continue
        # Delegating to argparse rather than rendering the flags here keeps one
        # source of truth: `bk <cmd> --help` is what the operator will type next
        # time, and it must not differ from what the browser just showed them.
        print(_subcommand_help(parser, chosen))
        outcome = _offer_to_run(parser, chosen)
        # Narrowed on the type, not on identity with `_BACK`: an exit code is
        # an int and the sentinel deliberately is not, so this cannot be
        # confused with returning 0.
        if not isinstance(outcome, int):
            continue
        return outcome


#: `_offer_to_run` returning "the operator wants the previous screen", which is
#: not an exit code and must not be confused with one.
_BACK = object()


def _offer_to_run(
    parser: argparse.ArgumentParser, command: str
) -> int | object:
    """Let the browser finish the job it just helped someone find.

    The browser used to end at `--help` and exit. That is a dead end in the
    literal sense: you have navigated to the command you wanted, and the tool's
    last act is to make you retype it from scratch — with its arguments, which
    are exactly what you came here not knowing. Twenty of the forty-six
    commands cannot run without one, so this was the common case rather than
    the edge.

    It still never runs what it merely highlighted — the browser is typed by
    people who do not yet know what these do. Running is a separate, explicit
    choice, defaulted to no, taken after the composed command line is on screen
    in full. Commands that destroy something are not offered at all: they are
    printed to be run deliberately, which is the same standard `--force`
    already holds them to.
    """

    needed = requirements(parser, command.split())
    wants_input = bool(needed) or command in _ALTERNATIVES
    destructive = command.split()[0] in _DESTRUCTIVE

    choices: list[prompt.Choice[str]] = []
    if not destructive:
        choices.append(
            prompt.Choice(
                "run",
                "Run it now",
                "asks for what it needs first"
                if wants_input
                else "no arguments needed",
            )
        )
    choices.append(prompt.Choice("compose", "Show me the command", "to copy and edit"))
    choices.append(prompt.Choice("back", "Back", "pick something else"))

    try:
        decision = prompt.select("What next?", choices, hint="↑↓ · Enter · Esc goes back")
    except prompt.Cancelled:
        return _BACK

    if decision == "back":
        return _BACK

    extra: list[str] = []
    if wants_input:
        collected = _ask_for(needed, command)
        if collected is None:
            return _BACK
        extra = collected

    line = shlex.join(["bk", *command.split(), *extra])
    print()
    print("  " + console.style(line, console.ACCENT))
    print()
    if decision == "compose":
        return 0
    return main([*command.split(), *extra])


#: Commands that remove or overwrite something. The browser shows their
#: composed command line and stops there -- a keystroke reached by wandering
#: through a menu is not the right way to delete a source or take an
#: integration down.
_DESTRUCTIVE = frozenset({"forget", "reject", "apply"})


def _fill_missing_arguments(
    parser: argparse.ArgumentParser,
    argv: list[str],
    global_values: dict[str, Any],
) -> list[str]:
    """Ask for a required argument instead of printing usage and quitting.

    `bk search` at a terminal used to answer with a usage block and exit 2,
    which tells someone who already knows what they want that they typed too
    little. Now it asks for the query. The same applies to the other nineteen
    commands that cannot run without an argument.

    Deliberately narrow, because a prompt in the wrong place is worse than a
    usage message:

    - **only at a terminal**, and never under `--json`: a script that is
      missing an argument must fail, not block forever on a question nobody
      will answer;
    - **only when nothing was supplied** — a partially-typed command is a
      mistake to report, not a form to fill in, and guessing which of two
      arguments was meant is how you get the wrong one;
    - **never for flags the operator may legitimately be replacing** with an
      alternative, which is why `requirements` ignores optional positionals.
    """

    if global_values["json"] or not prompt.supports_interactive():
        return argv
    command = [token for token in argv if not token.startswith("-")]
    if not command:
        return argv
    needed = requirements(parser, command)
    joined = " ".join(command)
    # Anything already typed beyond the command name means the operator is
    # mid-thought; argparse's own message is the right answer then.
    if len(command) < len(argv):
        return argv
    if not needed and joined not in _ALTERNATIVES:
        return argv
    collected = _ask_for(needed, joined)
    if collected is None:
        raise SystemExit(130)
    return [*argv, *collected]


class _Needed(NamedTuple):
    """One argument a command cannot run without."""

    dest: str
    flag: str | None
    help: str
    choices: tuple[str, ...]



#: Lives in `prompt` so it can run *before* a prompt's validator rather than on
#: its result -- the ordering is what makes a quoted path pass the check that
#: exists to accept it. Re-exported under the old name because this is where
#: the convention is documented for the CLI's own callers.
_typed_value = prompt.shell_value


def _looks_like_source(value: str) -> str | None:
    """Reject a capture target that is neither a URL nor a file that exists.

    Caught at the prompt, where the operator can fix it in place, instead of
    after the command has been assembled and dispatched -- the difference
    between one re-ask and starting the whole flow again.
    """

    if "://" in value:
        return None
    if Path(value).expanduser().is_file():
        return None
    return f"No file at {value!r}. Paste a path that exists, or a URL."


class _Alternative(NamedTuple):
    """One of several ways to satisfy a command that needs *something*."""

    label: str
    note: str
    flag: str | None
    #: False for a switch like `--all`, which is complete on its own.
    takes_value: bool = True
    #: Checked at the prompt, so a bad value is re-asked rather than dispatched.
    validate: Callable[[str], str | None] | None = None


#: Commands whose argument is optional to argparse and mandatory in practice,
#: because the requirement is a choice between forms rather than a single slot.
#: `bk capture` is the case that prompted this: argparse sees an optional
#: positional and a `--text` flag, reports nothing as required, and the command
#: then refuses at runtime with "capture requires a source path, URL, or
#: --text" -- a requirement that existed only in that error string, so neither
#: the browser nor the bare invocation could ask for it.
#:
#: Declared rather than inferred: "one of these" is not something an argparse
#: parser records, and guessing it from a mutually-exclusive group would still
#: miss this one, which is not in a group.
_ALTERNATIVES: dict[str, tuple[str, tuple[_Alternative, ...]]] = {
    "capture": (
        "What do you want to capture?",
        (
            _Alternative(
                "A file or URL",
                "path on disk, or a link",
                None,
                validate=_looks_like_source,
            ),
            _Alternative("Type the text", "captured as a note", "--text"),
        ),
    ),
    "ingest": (
        "What should be ingested?",
        (
            _Alternative("One source", "full or prefix hash, or a raw path", None),
            _Alternative(
                "Everything pending", "every unfiled source", "--all", takes_value=False
            ),
        ),
    ),
}


def requirements(parser: argparse.ArgumentParser, path: Sequence[str]) -> list[_Needed]:
    """What `bk <path...>` must be given before it can run.

    Read off the parser rather than restated, because the answer has to stay
    true as commands change: 20 of the 46 invocable commands cannot run without
    an argument, and every one of them was a place the operator could arrive
    and be stuck.

    Optional positionals are excluded even when the command errors without one
    (`capture`, `ingest`): those carry their own alternatives (`--text`,
    `--all`), and prompting for a path would hide them.
    """

    current = parser
    for token in path:
        subparsers = _subparsers_of(current)
        if subparsers is None or token not in subparsers.choices:
            return []
        current = subparsers.choices[token]
    if _subparsers_of(current) is not None:
        return []

    needed = []
    for action in current._actions:
        if action.dest in ("help", "command"):
            continue
        required = (
            action.required
            if action.option_strings
            else action.nargs not in ("?", "*")
        )
        if not required:
            continue
        needed.append(
            _Needed(
                dest=action.dest,
                flag=action.option_strings[-1] if action.option_strings else None,
                help=action.help or "",
                choices=tuple(str(c) for c in (action.choices or ())),
            )
        )
    return needed


def _subparsers_of(
    parser: argparse.ArgumentParser,
) -> argparse._SubParsersAction[argparse.ArgumentParser] | None:
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return action
    return None



def _ask_for(needed: Sequence[_Needed], command: str) -> list[str] | None:
    """Collect the missing arguments at the prompt, or None if cancelled."""

    print()
    print(console.style(f"bk {command}", console.BOLD, console.ACCENT))
    collected: list[str] = []
    alternatives = _ALTERNATIVES.get(command)
    if alternatives is not None:
        question, options = alternatives
        try:
            picked = prompt.select(
                question,
                [prompt.Choice(o, o.label, o.note) for o in options],
            )
            if not picked.takes_value:
                return [picked.flag] if picked.flag else []
            # Unquoting happens inside `text`, before the validator runs. Doing
            # it to the *return* value -- which is what this used to do -- meant
            # a pasted `'/a/b c.pdf'` was checked with its quotes still on, so
            # `_looks_like_source` rejected a path that exists and re-asked
            # forever.
            value = prompt.text(
                picked.flag or "value", "", validate=picked.validate or _non_empty
            )
        except prompt.Cancelled:
            return None
        return [picked.flag, value] if picked.flag else [value]
    for item in needed:
        label = item.flag or item.dest
        try:
            if item.choices:
                value = str(
                    prompt.select(
                        label,
                        [prompt.Choice(c, c) for c in item.choices],
                        hint=item.help or "",
                    )
                )
            else:
                value = prompt.text(label, "", validate=_non_empty)
        except prompt.Cancelled:
            return None
        if item.flag:
            collected += [item.flag, value]
        else:
            collected.append(value)
    return collected


def _non_empty(value: str) -> str | None:
    return None if value.strip() else "This one is required."


def _subcommand_help(parser: argparse.ArgumentParser, name: str) -> str:
    subparsers = _subparsers_of(parser)
    if subparsers is None or name not in subparsers.choices:
        return ""
    return subparsers.choices[name].format_help().rstrip()


def _mistyped_command(
    argv: Sequence[str], parser: argparse.ArgumentParser
) -> str | None:
    """The first token that should have been a command but names none.

    Returns `None` when the command is valid, so argparse keeps ownership of
    every *other* argument error -- a bad flag or a missing operand belongs to
    the subparser that declared it, and second-guessing those here would
    duplicate messages argparse already writes well.
    """

    for token in argv:
        if token.startswith("-"):
            continue
        return None if token in _command_help_index(parser) else token
    return None


def _did_you_mean(unknown: str, parser: argparse.ArgumentParser) -> str:
    """Argparse's answer to a typo is to reprint all thirty command names.

    That is the same wall the usage line was trimmed of, at the moment it helps
    least: the reader already knows roughly what they wanted and mistyped it.
    A close match is almost always the whole answer, so lead with it and leave
    the full list one keystroke away.
    """

    import difflib

    commands = sorted(_command_help_index(parser))
    lines = [
        console.style(f"bk: '{unknown}' is not a bk command.", console.ERR),
    ]
    close = difflib.get_close_matches(unknown, commands, n=3, cutoff=0.6)
    # Prefix matches are what a truncated or half-typed command produces, and
    # `get_close_matches` scores those poorly when the prefix is short: `bk co`
    # is far more likely to be `context`/`code` than anything a ratio picks.
    prefixed = [name for name in commands if name.startswith(unknown)]
    suggestions = list(dict.fromkeys(prefixed + close))[:3]
    if suggestions:
        lines.append("")
        lines.append("Did you mean?")
        index = _command_help_index(parser)
        width = max(len(name) for name in suggestions)
        for name in suggestions:
            lines.append(
                f"  {console.style(name, console.ACCENT):<{width}}  "
                f"{console.style(index.get(name, ''), console.MUTED)}"
            )
    lines.append("")
    lines.append(f"Run `bk` to see all {len(commands)} commands.")
    return "\n".join(lines)


def _gate_target(value: str) -> str:
    """A relative gate target means "relative to where I am standing".

    `check_write` resolves a relative target against the *vault root*, which is
    right for the installed hook -- it always passes absolute paths -- but it is
    the opposite of every other command, and it was documented nowhere. So
    `bk gate check-write docs/brain/wiki/x.md` answered "allowed" with exit 0
    while the absolute spelling of the same file was correctly gated.

    That is the command a human or an agent runs to verify the central claim by
    hand, so a confident wrong answer there is worse than not shipping it. The
    application-layer contract is unchanged; only this interface, where a user's
    shell context exists, applies it.
    """

    if Path(value).expanduser().is_absolute():
        return value
    return str(Path.cwd() / value)


def main(argv: Sequence[str] | None = None) -> int:
    effective_argv = list(argv if argv is not None else sys.argv[1:])
    effective_argv, global_values = _extract_global_options(effective_argv)
    parser = build_parser()
    if _wants_top_level_help(effective_argv):
        # A bare `bk` at a terminal is someone looking around, and gets the
        # browser. An explicit `-h` is someone asking for the help *text* --
        # usually to read, pipe or grep it -- so it stays the flat listing, as
        # does any non-tty invocation. Diverging on the flag rather than on the
        # terminal alone is what keeps `bk -h | grep` meaning what it always did.
        if not effective_argv and prompt.supports_interactive():
            try:
                return _browse_commands(parser)
            except KeyboardInterrupt:
                # Between prompts the tty is briefly back in cooked mode, so an
                # interrupt arrives as a signal rather than as a key the raw
                # reader would have turned into `Cancelled`.
                return 130
        print(_grouped_help(parser))
        return 0
    mistyped = _mistyped_command(effective_argv, parser)
    if mistyped is not None:
        print(_did_you_mean(mistyped, parser), file=sys.stderr)
        return 2
    effective_argv = _fill_missing_arguments(parser, effective_argv, global_values)
    args = parser.parse_args(effective_argv)
    if getattr(args, "print_config", False):
        # Raw policy on stdout, never the `{"ok": ..., "result": ...}` envelope
        # and never a human rendering: the documented use is
        # `bk init --print-config > policy.json && bk init . --config policy.json`,
        # and that only works if what comes out is exactly what --config expects.
        print(
            json.dumps(
                onboarding.default_policy(
                    Path(global_values["vault"] or args.path), args.preset
                ),
                indent=2,
                ensure_ascii=False,
            )
        )
        return 0
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
            _emit(
                result,
                args.command,
                code_command=getattr(args, "code_command", None),
                json_mode=args.json,
            )
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
    except BrainskitError as exc:
        _emit_error(exc, json_mode=args.json)
        return 2
    except (OSError, json.JSONDecodeError) as exc:
        wrapped = BrainskitError(str(exc))
        _emit_error(wrapped, json_mode=args.json)
        return 2
    except prompt.Cancelled as exc:
        # Dismissing a prompt is the operator declining, not the tool failing:
        # same exit status as the interrupt it stands in for, and no error
        # envelope claiming something went wrong. A `Cancelled` raised *with* a
        # reason keeps it -- `bk init` inside another vault explains the nesting
        # rule, and swallowing that left "Cancelled" as the entire answer.
        reason = str(exc) or "Cancelled — nothing was written"
        _emit_error(BrainskitError(reason), json_mode=args.json)
        return 130
    except KeyboardInterrupt:
        _emit_error(BrainskitError("Interrupted"), json_mode=args.json)
        return 130
    except Exception as exc:
        # An adapter raised something brainskit does not model. Never let a raw
        # traceback replace the machine-readable envelope on stdout.
        _emit_error(_internal_error(exc), json_mode=args.json)
        return 2


def create_service(vault_path: str | None) -> BrainskitService:
    vault = FileVault(Path(vault_path)) if vault_path else FileVault.discover()
    index = SqliteFtsIndex(vault.index_path)
    jobs = JobSpecs()
    judgment = PolicyJudgmentRouter(vault.config(), jobs)
    graph = MarkdownGraph()
    return BrainskitService(
        vault,
        index,
        judgment=judgment,
        jobs=jobs,
        graph=graph,
        integrations=NativeIntegrations(vault),
        extractor=GraphifyExtractor(),
    )


def _finish_init(
    target: Path, outcome: onboarding.Outcome, *, force: bool, skip_code_build: bool
) -> tuple[FileVault, BrainskitService, dict[str, Any]]:
    """Turn a wizard/`--config` outcome into an initialized, registered vault.

    Shared by `bk init` and the `bk web` no-vault prompt below -- both start
    from an `Outcome` and need the same sequence: write the vault, index it,
    register it, and wire an agent only once everything before it succeeded.
    """

    # The wizard now asks where the vault goes, so its answer wins over the
    # `.` this command defaults to. `--config` never runs the wizard and
    # keeps naming its path on the command line.
    target = outcome.vault or target
    vault = FileVault.initialize(target, outcome.policy, force=force)
    service = create_service(str(vault.root))
    indexed = service.reindex()
    views = service.views()
    result = {
        "vault": str(vault.root),
        "config": vault.config().to_dict(),
        **indexed,
        "views": views["written"],
        # Registered here rather than left for a second command nobody was
        # told to run: `bk init` was the only way to create a vault and the
        # only path that never registered one, so `bk vaults list` silently
        # omitted every vault the wizard made.
        "registered": _register_new_vault(vault.root),
    }
    # Wiring the agent is deliberately the *last* step: it writes outside
    # the vault, into the operator's project, and doing it before the vault
    # exists would leave a `.claude/` pointing at something that was never
    # created if initialization failed.
    if outcome.wire_agent:
        # The workspace is the *project*, not the vault. With the vault at
        # `<repo>/.brainskit` these are different directories, and installing
        # into the vault is the failure `_workspace_advisory` exists to
        # report: every file lands, the installer says success, and an agent
        # opened on the repository loads none of it. Under the old
        # `bk init .` layout they coincided, so nothing had to choose.
        result["hooks"] = _install_hooks(
            service,
            "claude",
            root=_project_root_for(vault),
            skip_code_build=skip_code_build,
        )
    return vault, service, result


def _service_for_web(args: argparse.Namespace) -> BrainskitService:
    """`bk web` is usually a click, not a script -- offer to fix a missing
    vault on the spot instead of only printing the hint every other command
    gets.

    Gated on an interactive terminal and unset `--json`, so the scriptable
    contract (a pipe, a cron job, `--json`) keeps the plain refusal every
    other command has and never blocks on a prompt nobody is there to answer.
    Declining, or a discovery failure the wizard itself refuses (nesting,
    orphaned `.brain/`), falls through to that same refusal.
    """

    try:
        return create_service(args.vault)
    except NotFoundError:
        if args.json or not prompt.supports_interactive():
            raise
        target = (
            Path(args.vault).expanduser().resolve() if args.vault else Path.cwd()
        )
        if not prompt.confirm(
            f"No brainskit vault found at {target}. Run `bk init` now?"
        ):
            raise
        outcome = _guided_init(target)
        _vault, service, _result = _finish_init(
            target, outcome, force=False, skip_code_build=False
        )
        return service


def _dispatch(args: argparse.Namespace) -> Any:
    if args.command == "init":
        target = Path(args.vault or args.path)
        if args.print_config:
            # Deliberately before any filesystem work: this prints a policy, it
            # does not create a vault, so it is safe to run anywhere.
            return onboarding.default_policy(target, args.preset)
        outcome = (
            onboarding.Outcome(policy=_read_json(args.config))
            if args.config
            else _guided_init(target)
        )
        _vault, _service, result = _finish_init(
            target, outcome, force=args.force, skip_code_build=args.skip_code_build
        )
        return result

    # Registry commands span vaults, so they must not be gated on the current
    # directory being one: `bk vaults list` has to work from anywhere, and
    # `bk vaults sync` builds a service per registered vault instead.
    if args.command == "vaults":
        return _vaults(args)

    service = (
        _service_for_web(args) if args.command == "web" else create_service(args.vault)
    )
    if args.command == "capture":
        return service.capture(args.source, text=args.text, title=args.title)
    if args.command == "status":
        return service.status()
    if args.command == "doctor":
        return _doctor(service)
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
        return service.gate_check_write(_gate_target(args.path), agent=args.agent)
    if args.command == "enrich":
        if args.enrich_command == "apply":
            return service.enrich_apply(_read_json(args.proposal))
        if args.enrich_command == "list":
            return service.enrich_list(consumer=_consumer_for_args(args))
        if args.enrich_command == "forget":
            return service.enrich_forget(args.id)
    if args.command == "views":
        return service.views()
    if args.command == "graph":
        return service.graph(
            consumer=args.consumer or "local", html=args.html
        )
    if args.command == "code":
        if args.code_command == "build":
            paths = args.paths or None
            _offer_missing_grammars(
                service, paths, choice=args.install_missing, json_mode=args.json
            )
            return service.code_build(paths)
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
        return service.export(
            args.target,
            consumer=args.consumer or "local",
            enrichment=args.enrichment,
        )
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
        from brainskit.interfaces.mcp import run_http, run_stdio

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
            service,
            args.agent,
            root=args.root,
            force=args.force,
            skip_code_build=args.skip_code_build,
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
        from brainskit.interfaces.web import run_web

        policy = service.vault.config().integrations["web"]
        options = policy.options if policy.enabled else {}
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
            open_browser=not args.no_browser,
        )
        return None
    raise ValidationError("Unknown command")


def _read_json(path: str) -> dict[str, Any]:
    raw = sys.stdin.read() if path == "-" else Path(path).read_text(encoding="utf-8")
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValidationError("Expected a JSON object", details={"path": path})
    return value




def _project_root_for(vault: FileVault) -> str | None:
    """The repository a nested vault belongs to, or None for a standalone one.

    `None` keeps `_resolve_workspace`'s existing default (the vault itself),
    which is right when the vault is not inside a project.
    """

    root, _ = vault.code_root_reason()
    if root != vault.root and (root / ".git").exists():
        return str(root)
    return None


def _register_new_vault(root: Path) -> dict[str, Any] | None:
    """Add a freshly created vault to this machine's registry, best-effort.

    Never fatal: the vault exists and works whether or not the registry on this
    machine could be written, and failing an otherwise successful `bk init`
    over a bookkeeping file would be the wrong trade.
    """

    try:
        return VaultRegistry().register(root, label=root.parent.name or root.name)
    except (BrainskitError, OSError):
        return None


def _guided_init(target: Path) -> onboarding.Outcome:
    """Run the guided onboarding, refusing rather than guessing without a tty.

    `--config` remains the complete-policy path for scripts and CI. This one is
    for a human, and it needs a terminal: silently answering its own questions
    from defaults nobody saw is how the previous wizard shipped a vault
    configured for a model that was not installed.
    """

    if not sys.stdin.isatty():
        raise ValidationError(
            "Interactive init needs a terminal",
            details={
                "hint": (
                    "bk init --print-config > policy.json "
                    "&& bk init <path> --config policy.json"
                ),
                "presets": list(onboarding.PRESET_KEYS),
            },
        )
    return onboarding.run(target)


def _consumer_for_args(args: argparse.Namespace) -> str:
    if args.consumer:
        return str(args.consumer)
    if args.json:
        raise ValidationError(
            "Machine-readable output requires --consumer",
            details={"choices": ["human", "local", "cloud"]},
        )
    return "human"


def _watch(
    service: BrainskitService, *, once: bool, interval: float, json_mode: bool
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
        _emit(result, "watch", json_mode=json_mode)
        time.sleep(interval)


INSTRUCTION_FILES = {
    "claude": "CLAUDE.md",
    "codex": "AGENTS.md",
    "gemini": "GEMINI.md",
    "opencode": "AGENTS.md",
}
_MANAGED_BLOCK_RE = re.compile(
    rf"{re.escape(INSTRUCTION_START)}.*?{re.escape(INSTRUCTION_END)}\n?",
    re.DOTALL,
)


class ClaudeHook(NamedTuple):
    """A Claude Code hook brainskit ships, installs and registers."""

    template: str
    event: str
    matcher: str | None
    timeout: int


CLAUDE_HOOKS: tuple[ClaudeHook, ...] = (
    ClaudeHook("brainskit-gate", "PreToolUse", "Write|Edit|MultiEdit", 10),
    # SessionStart carries no matcher so every session source is covered; the
    # settings schema treats an absent matcher as "all", which is how the other
    # session-scoped hooks in a real settings file are written.
    ClaudeHook("brainskit-status", "SessionStart", None, 15),
)

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
            "deny_prefixes": list(DEFAULT_DENY_PREFIXES),
            "remediation": dict(GATE_REMEDIATION),
        },
        "rules": [
            "Read evidence with bk context --json --consumer local",
            "Write wiki pages only with bk apply",
            "Never edit raw content",
        ],
    }


def _agent_template(name: str, vault: Path) -> str:
    resource = files("brainskit").joinpath("templates", "agents", f"{name}.md")
    if not resource.is_file():
        raise NotConfiguredError(
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
    resource = files("brainskit").joinpath("templates", "agents", f"{name}.sh")
    if not resource.is_file():
        raise NotConfiguredError(
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
    skill = root / ".claude" / "skills" / "brainskit" / "SKILL.md"
    content = _agent_template("claude-skill", vault)
    if skill.is_file() and not force:
        if skill.read_text(encoding="utf-8") == content:
            return {"path": str(skill), "state": "current"}
        raise ValidationError(
            "A brainskit skill already exists; re-run with --force to replace it",
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

    A repository whose `core.hooksPath` points elsewhere gets nothing written
    at all. Writing `.git/hooks/pre-commit` there would produce a file git
    never reads, and `--force` cannot change that -- force decides whether to
    clobber an existing hook, not which directory git executes.
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
    redirected_hooks = redirected_git_hooks_path(root)
    if redirected_hooks is not None:
        return {
            "state": "skipped",
            "reason": (
                f"git runs hooks from {redirected_hooks}, not .git/hooks, "
                "because core.hooksPath is set"
            ),
            "hint": (
                "Add `bk --vault "
                f"{shlex.quote(str(vault))} lint --changed` to "
                f"{redirected_hooks / 'pre-commit'} and commit it"
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
            "hint": "Merge brainskit lint into it, or re-run with --force",
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
    """Write one hook script, refusing to clobber a file brainskit did not write.

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


def _is_stale_hook_command(command: Any, template: str, current: str) -> bool:
    """Whether `command` is a brainskit `template` hook at some path other than `current`.

    Every install writes to `<root>/.claude/hooks/<template>.sh`, so the command
    this workspace should be running for a given template never has more than
    one live value at a time. A registered command matching that name but not
    equal to the one being installed now is not "unrelated tooling on the same
    event" — the case this file otherwise takes care never to touch — it is a
    previous install this one supersedes: a settings.json carried over from a
    different `--root`, or copied wholesale from another project's `.claude/`.
    Left alone, it keeps firing every session against a vault or workspace this
    one no longer is.
    """
    if not isinstance(command, str) or command == current:
        return False
    return Path(command).name == f"{template}.sh"


def _prune_stale_hook_entries(
    group: list[Any], template: str, current: str
) -> tuple[list[Any], list[str]]:
    """Drop any `template` hook command other than `current`; report what left.

    An entry that loses every command this way is dropped whole rather than
    kept as `{"hooks": []}` — an empty hooks list is not a shape Claude Code
    itself ever writes, and leaving one behind would be a second kind of debris
    in a file this module otherwise treats as the operator's.
    """
    removed: list[str] = []
    pruned: list[Any] = []
    for entry in group:
        if not isinstance(entry, dict) or not isinstance(entry.get("hooks"), list):
            pruned.append(entry)
            continue
        kept = []
        for item in entry["hooks"]:
            command = item.get("command") if isinstance(item, dict) else None
            if isinstance(command, str) and _is_stale_hook_command(
                command, template, current
            ):
                removed.append(command)
                continue
            kept.append(item)
        if kept:
            pruned.append({**entry, "hooks": kept})
    return pruned, removed


def _register_claude_hooks(
    root: Path, entries: Sequence[tuple[ClaudeHook, str]]
) -> dict[str, Any]:
    """Register hook commands in `.claude/settings.json` without clobbering it.

    The file belongs to the operator and routinely carries unrelated tooling on
    the same events, so this reads, mutates and writes: unknown top-level keys
    survive, existing arrays are appended to rather than replaced, and a file
    that does not parse is left exactly as it is instead of being rebuilt. The
    idempotency key is the hook's command path, so a second install at the same
    path appends nothing and the file stays byte-identical — but a *different*
    path registered under the same template name is recognised as stale and
    pruned first, so reinstalling against a new `--root` or vault replaces the
    old entry instead of running alongside it.
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
    pruned_stale: list[dict[str, Any]] = []
    changed = False
    for hook, command in entries:
        group = list(hooks.get(hook.event, []))
        group, removed = _prune_stale_hook_entries(group, hook.template, command)
        if removed:
            hooks[hook.event] = group
            changed = True
            pruned_stale.extend(
                {"event": hook.event, "command": stale} for stale in removed
            )
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
    result: dict[str, Any] = {
        "path": str(target),
        "state": "updated" if existed else "created",
        "registered": registered,
    }
    if pruned_stale:
        result["pruned"] = pruned_stale
    return result


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
                "brainskit-gate",
                "Claude Code PreToolUse hook on Write|Edit|MultiEdit",
            ),
            (
                "session_status",
                "brainskit-status",
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


def _note_pruned_stale_hooks(claude_hook: dict[str, Any] | None) -> None:
    """Say, on stderr, which stale hook commands this install just replaced.

    `_register_claude_hooks` prunes silently because `settings.json` is the
    operator's file and this module otherwise never announces an edit to it —
    but a removed entry is exactly the kind of change `_warn_about_inactive_
    enforcement` exists to never leave quiet: the operator may have expected
    that command to still be the one guarding a different vault or workspace.
    """
    if not isinstance(claude_hook, dict):
        return
    pruned = claude_hook.get("settings", {}).get("pruned")
    if not pruned:
        return
    print("", file=sys.stderr)
    print("bk: STALE HOOKS replaced in settings.json:", file=sys.stderr)
    for item in pruned:
        print(f"  - {item['event']}: {item['command']}", file=sys.stderr)
    print("      Each was a brainskit hook this install superseded.", file=sys.stderr)
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
    service: BrainskitService,
    agent: str,
    *,
    root: str | None = None,
    force: bool = False,
    skip_code_build: bool = False,
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
    result["code_graph"] = _bootstrap_code_graph(service, skip=skip_code_build)
    _warn_about_inactive_enforcement(result["enforcement"], advisory)
    _note_pruned_stale_hooks(result.get("claude_hook"))
    _note_code_graph_bootstrap(result["code_graph"])
    return result


#: The probe payload is the shape Claude Code sends a PreToolUse hook.
_GATE_PROBE_NAME = "brainskit-doctor-probe.md"


def _run_gate_hook(script: Path, target: Path) -> tuple[int | None, str]:
    """Ask the installed hook about one path, exactly as the agent would.

    Runs the script itself rather than `sh script`, because the executable bit
    is part of what makes a hook fire and `sh` would paper over its absence.
    Never raises: a hook that cannot run is a finding, not a crash.
    """
    payload = json.dumps(
        {"tool_name": "Write", "tool_input": {"file_path": str(target)}}
    )
    try:
        # The path is the hook brainskit installed and `Health` reports, not
        # caller input, and it is a one-element argument vector with no shell.
        # Executing it is the entire point: reading the file instead is the
        # bug this probe exists to catch.
        done = subprocess.run(  # noqa: S603
            [str(script)],
            input=payload,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return None, str(exc)
    return done.returncode, done.stderr.strip()


def _probe_write_gate(service: BrainskitService, layers: list[dict[str, Any]]) -> dict[str, Any]:
    """Run the write gate instead of confirming that its file exists.

    Every enforcement answer above this one is "the artefact is installed and
    registered". That is not the same claim as "a write to `wiki/` is refused",
    and the two have already come apart in the field: the hook script fails
    open by design -- no `python3`, no `bk` on PATH, an unreachable vault, a
    lost executable bit -- and each of those makes it exit 0 on a write it was
    installed to deny, while `bk status` still reports the layer active.

    So: one path the gate must deny, one it must allow. Both are decisions
    only; `gate check-write` writes nothing, and neither probe file is ever
    created. Denying everything is reported too -- a gate that blocks ordinary
    edits is broken in the direction that stops work rather than the direction
    that loses provenance, but it is still broken.
    """

    entry = next((layer for layer in layers if layer["layer"] == "write_gate"), None)
    script = Path(entry["script"]) if entry and entry.get("script") else None
    if script is None or not script.is_file():
        return {
            "state": "absent",
            "detail": "no write-gate hook is installed; nothing to exercise",
        }

    vault_root = service.vault.root
    gated = vault_root / "wiki" / _GATE_PROBE_NAME
    ordinary = vault_root.parent / _GATE_PROBE_NAME

    denied_status, denied_note = _run_gate_hook(script, gated)
    allowed_status, allowed_note = _run_gate_hook(script, ordinary)
    denies_gated = denied_status == 2
    allows_ordinary = allowed_status == 0

    if denied_status is None or allowed_status is None:
        state, detail = "unknown", f"the hook could not be run: {denied_note or allowed_note}"
    elif denies_gated and allows_ordinary:
        state, detail = "enforcing", f"a write to wiki/ is refused (exit {denied_status})"
    elif not denies_gated:
        state = "not_enforcing"
        detail = (
            f"a direct write to wiki/{_GATE_PROBE_NAME} was allowed "
            f"(hook exited {denied_status}); the gate is installed but not guarding"
        )
    else:
        state = "over_blocking"
        detail = (
            f"an ordinary write outside the vault was refused "
            f"(hook exited {allowed_status})"
        )

    report: dict[str, Any] = {
        "state": state,
        "detail": detail,
        "script": str(script),
        "denies_a_gated_write": denies_gated,
        "allows_an_ordinary_write": allows_ordinary,
    }
    # The script explains its own fail-open on stderr, and that sentence names
    # the missing piece far better than anything inferred from an exit code.
    note = denied_note or allowed_note
    if note and state != "enforcing":
        report["hook_said"] = note
    return report


def _doctor(service: BrainskitService) -> dict[str, Any]:
    """Whether this installation can do what it advertises.

    `bk status` answers "is the vault healthy". This answers the question that
    went unasked until three separate failures traced back to it: is the machine
    underneath it wired up. Each section is here because its absence was silent —

    - **environment**: every "install the optional X" message named `pip`, which
      a `uv tool` install does not have, so the advice could not be followed.
    - **grammars**: 13 of 29 shipped and the rest unreachable, discoverable only
      by building a graph and noticing a language missing from it.
    - **code root**: the directory a build would scan, *and why that one*. A
      vault resolved this to a parent holding every repository on the machine,
      and nothing said so until the graph reached 683 MB.
    - **enforcement**: reused from `Health`, because "is the write gate live" is
      part of any health question -- and then *exercised* rather than believed,
      because every layer above reports that a file is installed, which is a
      different claim from "a write to `wiki/` is actually refused".

    Assembled in the interface layer rather than in `Health`: every fact here is
    about the interpreter and the installation, which `application` may not
    import (`tests/test_layering.py`) and should not have to know.
    """

    vault = service.vault
    environment = pyenv.describe_environment()
    root, reason = vault.code_root_reason()
    grammars = grammar_inventory()
    missing = [name for name, installed in grammars.items() if not installed]
    enforcement = service.health.enforcement()
    probe = _probe_write_gate(service, enforcement["layers"])
    enforcement["write_gate_probe"] = probe
    return {
        "vault": str(vault.root),
        "environment": {
            "kind": environment.kind,
            "label": environment.label,
            "executable": environment.executable,
            "installable": environment.installable,
        },
        "code": {
            "root": str(root),
            "why_this_root": reason,
            "scan_limit": vault.config().code_scan_limit,
            "grammars_installed": sum(grammars.values()),
            "grammars_known": len(grammars),
            "grammars_missing": missing,
            **(
                {"install": environment.install_hint(missing)}
                if missing and environment.installable
                else {}
            ),
        },
        "enforcement": enforcement,
        # An allowlist, not a denylist: only two states are compatible with a
        # healthy installation -- the gate refused what it must ("enforcing"),
        # or there is no gate to judge ("absent", which an operator may have
        # chosen). Everything else, including a hook that could not be run at
        # all, means nobody has confirmed that a write to wiki/ is refused, and
        # an installed gate that does not guard is worse than none: every other
        # layer keeps reporting success while writes go around it.
        "healthy": not missing and probe["state"] in {"enforcing", "absent"},
    }


def _offer_missing_grammars(
    service: BrainskitService,
    #: As argparse produced them. `code_survey` resolves them against the code
    #: root, which is the only place that knows what they are relative to.
    paths: list[str] | None,
    *,
    choice: bool | None,
    json_mode: bool,
) -> None:
    """Say which grammars this scan needs, and offer to install them.

    Before the scan, because afterwards is too late to be useful: the build
    succeeds either way, and the only difference a missing grammar makes is
    that a language is absent from a graph that then calls itself complete.

    `choice` is the operator's standing answer -- `--install-missing`,
    `--no-install-missing`, or `None` for "ask". Asking requires a terminal;
    `--json` and a pipe never prompt, because a command whose output is being
    parsed must not block on a question nobody will see. In that case the
    shortfall is still printed, with the command that fixes it.
    """

    survey = service.code_survey(paths)
    if survey is None or not survey.missing:
        return
    distributions = [need.distribution for need in survey.missing]
    environment = pyenv.describe_environment()
    command = environment.install_command(distributions)

    print("", file=sys.stderr)
    print(
        f"bk: {len(distributions)} tree-sitter grammar(s) missing for this scan:",
        file=sys.stderr,
    )
    for need in survey.missing:
        extensions = " ".join(need.extensions)
        print(
            f"  - {need.distribution:<24} {need.files} file(s)  {extensions}",
            file=sys.stderr,
        )
    print(
        f"      Without them those {survey.unreachable_files} file(s) contribute "
        "nothing to the graph.",
        file=sys.stderr,
    )
    print(f"      {environment.install_hint(distributions)}", file=sys.stderr)
    print("", file=sys.stderr)

    if choice is False or not environment.installable:
        return
    if choice is None:
        if json_mode or not prompt.supports_interactive():
            return
        try:
            if not prompt.confirm("Install them now?", default=False):
                return
        except prompt.Cancelled:
            return
    _run_install(command)


def _run_install(command: list[str]) -> None:
    """Run an install command, reporting rather than raising on failure.

    A failed install must not fail the build: the build was always going to
    work, just over fewer languages. The output is left on stderr so the
    operator sees the package manager's own reason rather than a summary of it.
    """

    print(f"bk: {shlex.join(command)}", file=sys.stderr)
    try:
        # No shell, and nothing here comes from the operator: the executable is
        # `sys.executable` or a `shutil.which` hit, and every package name is a
        # `tree_sitter_*` module the vendored dispatch table already names,
        # hyphenated. The whole point of this function is to run a command the
        # operator did not have to compose.
        completed = subprocess.run(command, check=False)  # noqa: S603
    except OSError as exc:
        print(f"bk: could not run the installer: {exc}", file=sys.stderr)
        return
    if completed.returncode != 0:
        print(
            f"bk: install exited {completed.returncode}; building without them",
            file=sys.stderr,
        )
        return
    # The grammars were absent when this process started, so `find_spec` has
    # already cached their absence. A fresh scan of the import machinery is
    # what makes them visible to the build that follows.
    importlib.invalidate_caches()
    _extension_grammars.cache_clear()


def _bootstrap_code_graph(service: BrainskitService, *, skip: bool) -> dict[str, Any]:
    """Run the first `bk code build` on the same run that onboards the agent.

    Without this, `bk code status` keeps reporting `missing` until an agent
    happens to notice the `code build` row in the skill's command table and
    runs it unprompted -- and the SessionStart hook never asks it to, because
    it only ever reports vault/wiki health. Onboarding is the one moment a
    human is already watching this command run, so it is also the moment a
    slow first extraction is least surprising.

    Best-effort, never fatal: the tree-sitter grammars are the `code` extra,
    not a base dependency (see `pyproject.toml`), so a vault that never
    installed it gets exactly the install hint `bk code build` would already
    give on its own, not a failed onboarding. A caller that wants onboarding
    to stay fast, or whose vault documents nothing that is a code repository,
    opts out with `--skip-code-build`.
    """
    if skip:
        return {"state": "skipped", "reason": "--skip-code-build was passed"}
    try:
        built = service.code_build(None)
    except BrainskitError as exc:
        return {
            "state": "skipped",
            "reason": str(exc),
            "hint": _install_hint_for(exc.details or {}).get("hint"),
        }
    except Exception as exc:
        # Mirrors _sync_one_vault: an extractor's own failure belongs to this
        # best-effort step, not to the onboarding it must not cancel.
        return {
            "state": "skipped",
            "reason": f"{type(exc).__name__}: {exc}".strip(),
        }
    return {"state": "built", **built}


def _note_code_graph_bootstrap(code_graph: dict[str, Any]) -> None:
    """Say once, on stderr, whether the first `bk code build` actually ran.

    Silence on a skip is the same failure `_warn_about_inactive_enforcement`
    exists to prevent for the other layers: nothing else on this path ever
    tells an agent the code graph is still missing.
    """
    if code_graph.get("state") == "built":
        return
    print("", file=sys.stderr)
    print("bk: CODE GRAPH not built during onboarding:", file=sys.stderr)
    if code_graph.get("reason"):
        print(f"      {code_graph['reason']}", file=sys.stderr)
    if code_graph.get("hint"):
        print(f"      {code_graph['hint']}", file=sys.stderr)
    print("      Build it yourself with: bk code build", file=sys.stderr)
    print("", file=sys.stderr)


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
    except BrainskitError as exc:
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


def _schedule(service: BrainskitService) -> dict[str, Any]:
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


def _scalar(value: Any) -> str:
    if value is None:
        return "-"
    if isinstance(value, bool):
        return "yes" if value else "no"
    return str(value)


def _render_auto(value: Any) -> str:
    """The fallback for every command without a renderer below.

    A flat dict becomes a panel; a dict with exactly one list-valued key
    (and no nested dicts otherwise) adds a table or a bullet list for it.
    Anything shaped differently falls through to the same indented JSON dump
    `_emit` always printed -- this never raises on a shape it does not
    recognise, so no command can regress by gaining a worse rendering than
    it had before.
    """

    if not isinstance(value, dict):
        return json.dumps(value, indent=2, ensure_ascii=False)
    list_items = [(key, item) for key, item in value.items() if isinstance(item, list)]
    has_nested_dict = any(isinstance(item, dict) for item in value.values())
    if len(list_items) > 1 or has_nested_dict:
        return json.dumps(value, indent=2, ensure_ascii=False)
    scalars = [
        (key, _scalar(item)) for key, item in value.items() if not isinstance(item, list)
    ]
    parts = [console.kv_panel(scalars)] if scalars else []
    for key, items in list_items:
        if not items:
            continue
        parts.append("")
        parts.append(console.rule(key.replace("_", " ")))
        if all(isinstance(item, dict) for item in items):
            headers = list(items[0].keys())
            rows = [[_scalar(item.get(header)) for header in headers] for item in items]
            parts.append(console.table(headers, rows))
        else:
            parts.append("\n".join(f"{console.BULLET} {_scalar(item)}" for item in items))
    return "\n".join(part for part in parts if part) or console.style(
        "(empty)", console.MUTED
    )


def _render_status(value: dict[str, Any]) -> str:
    healthy = value["healthy"]
    headline = "vault healthy" if healthy else f"{value['lint_errors']} lint error(s)"
    parts = [
        console.status_line(healthy, headline),
        "",
        console.kv_panel(
            [
                ("vault", value["vault"]),
                ("sources", str(value["sources"])),
                ("pending", str(value["pending"])),
                ("wiki pages", str(value["wiki_pages"])),
                (
                    "indexed",
                    f"{value['index']['documents']} documents"
                    + (
                        f" (updated {value['index']['updated_at']})"
                        if value["index"]["updated_at"]
                        else ""
                    ),
                ),
            ]
        ),
    ]
    by_branch = value.get("by_branch") or {}
    if by_branch:
        parts += [
            "",
            console.rule("by branch"),
            console.table(
                ["branch", "sources"], [[b, str(n)] for b, n in by_branch.items()]
            ),
        ]
    freshness = value.get("freshness") or {}
    if freshness:
        parts += [
            "",
            console.rule("wiki freshness"),
            console.kv_panel([(state, str(n)) for state, n in freshness.items()]),
        ]
    projections = value.get("projections") or {}
    if projections:
        rows = [
            [name, console.state_tag(info.get("state", "?")), info.get("generated_at") or "-"]
            for name, info in projections.items()
        ]
        parts += [
            "",
            console.rule("projections"),
            console.table(["artifact", "state", "generated_at"], rows),
        ]
    layers = (value.get("enforcement") or {}).get("layers") or []
    if layers:
        rows = [
            [layer["layer"], console.status_line(layer["active"], layer.get("detail", ""))]
            for layer in layers
        ]
        parts += ["", console.rule("enforcement"), console.table(["layer", "status"], rows)]
    return "\n".join(parts)


def _render_search(value: dict[str, Any]) -> str:
    hits = value.get("hits") or []
    header = console.status_line(True, f"{value['count']} hit(s) for \"{value['query']}\"")
    if value.get("redacted"):
        header += "  " + console.style(f"({value['redacted']} redacted)", console.WARN)
    if not hits:
        return header
    rows = [
        [hit["title"] or hit["path"], hit["kind"], f"{hit['score']:.3f}", hit["excerpt"]]
        for hit in hits
    ]
    return "\n".join([header, "", console.table(["title", "kind", "score", "excerpt"], rows)])


def _render_context(value: dict[str, Any]) -> str:
    evidence = value.get("evidence") or []
    header = console.status_line(
        True, f"{len(evidence)} evidence item(s) for \"{value['query']}\""
    )
    if value.get("redacted"):
        header += "  " + console.style(f"({value['redacted']} redacted)", console.WARN)
    blocks = [header]
    for item in evidence:
        blocks.append("")
        blocks.append(console.rule(item["citation"]))
        blocks.append(
            console.kv_panel(
                [("path", item["path"]), ("kind", item["kind"]), ("privacy", item["privacy"])]
            )
        )
        blocks.append("")
        blocks.append(item.get("content", ""))
    return "\n".join(blocks)


def _render_lint(value: dict[str, Any]) -> str:
    findings = value.get("findings") or []
    if not findings:
        return console.status_line(True, "no lint findings")
    errors = sum(finding["severity"] == "error" for finding in findings)
    warnings = len(findings) - errors
    summary = ", ".join(
        part
        for part in (
            f"{errors} error(s)" if errors else "",
            f"{warnings} warning(s)" if warnings else "",
        )
        if part
    )
    header = console.status_line(value["ok"], summary)
    rows = [
        [
            console.style(
                finding["severity"],
                console.ERR if finding["severity"] == "error" else console.WARN,
            ),
            finding["code"],
            finding.get("path") or "-",
            finding["message"],
        ]
        for finding in findings
    ]
    return "\n".join([header, "", console.table(["severity", "code", "path", "message"], rows)])


def _render_proposals(value: dict[str, Any]) -> str:
    proposals = value.get("proposals") or []
    header = console.status_line(True, f"{value['count']} proposal(s)")
    if not proposals:
        return header
    rows = [
        [p["proposal_id"], p["destination_branch"], p["filing_mode"], p["status"], p["created_at"]]
        for p in proposals
    ]
    return "\n".join(
        [header, "", console.table(["id", "branch", "mode", "status", "created"], rows)]
    )


def _render_ingest(value: dict[str, Any]) -> str:
    results = value.get("results") or []
    header = console.status_line(True, f"{value['ingested']} source(s) ingested")
    if not results:
        return header
    rows = []
    for result in results:
        proposal = result.get("proposal") or {}
        state = "queued for approval" if result.get("queued") else "applied"
        rows.append([result["source"][:12], proposal.get("destination_branch", "-"), state])
    return "\n".join([header, "", console.table(["source", "branch", "state"], rows)])


def _render_approve(value: dict[str, Any]) -> str:
    proposal = value.get("proposal") or {}
    note = "already applied" if value.get("idempotent") else "applied"
    return console.status_line(True, f"proposal {proposal.get('proposal_id', '?')} {note}")


def _render_reject(value: dict[str, Any]) -> str:
    proposal = value.get("proposal") or {}
    return console.status_line(True, f"proposal {proposal.get('proposal_id', '?')} rejected")


def _render_forget(value: dict[str, Any]) -> str:
    forgotten = value.get("forgotten") or {}
    lines = [console.status_line(True, f"forgot {forgotten.get('path', '?')}")]
    still_cited = value.get("still_cited_by") or []
    if still_cited:
        lines.append(console.style(f"still cited by {len(still_cited)} page(s):", console.WARN))
        lines.append("\n".join(f"{console.BULLET} {p}" for p in still_cited))
    return "\n".join(lines)


def _render_capture(value: dict[str, Any]) -> str:
    """`capture` is the second command in every quickstart and had no renderer.

    It fell through to the raw-JSON dump, so the first thing a new user saw
    after `bk init` was a payload -- and the one field they need next, the
    content hash, was buried in it unlabelled.
    """

    source = value.get("source") or {}
    content_hash = str(source.get("content_hash", ""))
    created = bool(value.get("created", True))
    verb = "captured" if created else "already captured"
    lines = [
        console.status_line(True, f"{verb} {source.get('original_name', '?')}"),
        f"  hash  {content_hash}",
        f"  path  {source.get('path', '?')}",
    ]
    if str(source.get("status", "")) == "pending":
        lines.append(f"  next  bk file {content_hash[:16]} --to <branch>")
    return "\n".join(lines)


def _render_file(value: dict[str, Any]) -> str:
    source = value.get("source") or {}
    return console.status_line(
        True, f"moved {source.get('original_name', '?')} to {source.get('path', '?')}"
    )


#: Shown after `bk init`, whether it ran the interactive wizard or took
#: `--config` -- the celebratory banner belongs to "a vault now exists",
#: not to the Q&A that may or may not have produced it.
_INIT_NEXT_STEPS: tuple[tuple[str, str], ...] = (
    ("bk status", "see vault health"),
    ("bk capture <file>", "add your first note"),
    ('bk ask "..."', "ask a question"),
)


def _render_init(value: dict[str, Any]) -> str:
    config = value.get("config") or {}
    hooks = value.get("hooks")
    rows = [
        ("wiki language", str(config.get("wiki_language", "-"))),
        ("branches", ", ".join(config.get("branches", {})) or "-"),
        ("indexed", str(value.get("indexed_documents", 0))),
        ("views written", str(len(value.get("views", [])))),
    ]
    if hooks:
        rows.append(("agent", f"claude {console.DASH} {hooks['workspace']}"))
    parts = [
        console.banner(),
        "",
        console.status_line(True, f"vault initialized at {value['vault']}"),
        console.kv_panel(rows),
        "",
        console.style("Next", console.BOLD, console.ACCENT),
    ]
    # Onboarding used to end without ever naming `bk hooks install`, so the one
    # step that turns a vault into something an agent can use was left for the
    # operator to discover in the README. It leads the list when it is still
    # pending, and is dropped only once it has actually run.
    steps = list(_INIT_NEXT_STEPS)
    if not hooks:
        steps.insert(0, ("bk hooks install --agent claude", "wire up your agent"))
    width = max(len(command) for command, _ in steps)
    for command, description in steps:
        parts.append(
            f"  {console.ARROW} {command:<{width}}  "
            f"{console.style(description, console.MUTED)}"
        )
    return "\n".join(parts)


def _render_ask(value: dict[str, Any]) -> str:
    parts = [console.rule("answer"), value["answer"]]
    citations = value.get("citations") or []
    if citations:
        parts += ["", console.style("citations", console.BOLD, console.ACCENT)]
        parts.append("\n".join(f"{console.BULLET} {c}" for c in citations))
    if value.get("uncertainty"):
        parts += ["", console.style(f"uncertainty: {value['uncertainty']}", console.WARN)]
    if value.get("saved_to"):
        parts += ["", console.style(f"Saved to {value['saved_to']}", console.MUTED)]
    return "\n".join(parts)


def _render_digest(value: dict[str, Any]) -> str:
    parts = [console.rule("digest"), value["digest"]]
    actions = value.get("actions") or []
    if actions:
        parts += ["", console.style("actions", console.BOLD, console.ACCENT)]
        parts.append("\n".join(f"{console.BULLET} {a}" for a in actions))
    if value.get("resurfaced"):
        parts += ["", console.style(f"resurfaced: {value['resurfaced']}", console.MUTED)]
    parts += ["", console.style(f"Saved to {value['path']}", console.MUTED)]
    return "\n".join(parts)


def _render_resurface(value: dict[str, Any]) -> str:
    parts = [console.rule(value.get("page", "resurfaced")), value["markdown"]]
    if value.get("question"):
        parts += ["", console.style(f"question: {value['question']}", console.MUTED)]
    parts += ["", console.style(f"Saved to {value['path']}", console.MUTED)]
    return "\n".join(parts)


def _render_code_affected(value: dict[str, Any]) -> str:
    affected = value.get("affected") or []
    header = console.status_line(True, f"{value['count']} symbol(s) affected by {value['symbol']}")
    if not affected:
        return header
    rows = [
        [node.get("label", node.get("id")), node.get("path", "-"), str(node.get("via", "-")), str(node.get("depth", ""))]
        for node in affected
    ]
    return "\n".join([header, "", console.table(["symbol", "path", "via", "depth"], rows)])


def _render_code_path(value: dict[str, Any]) -> str:
    """The chain as a descending tree, with each hop's real direction and site.

    Three things the previous one-line version got wrong or left out, in order
    of how much they cost a reader:

    - **The arrow could be backwards.** Every hop printed `--via-->` while the
      traversal is undirected, so a chain of callers rendered exactly like a
      chain of calls. `forward` now decides which way the arrow points, and
      half the hops in a typical path turn out to be reverse edges.
    - **`path` and `line` were discarded.** The graph knows where each symbol
      is, and the whole promise of the command is not having to go and look.
    - **A flat line hides the shape.** Indentation makes hop count legible
      without counting arrows.

    Locations are right-aligned into whatever width the terminal gives, and
    dropped entirely when it is too narrow -- the chain is the answer, the
    file is the follow-up.
    """

    if not value.get("found"):
        return console.status_line(
            False, f"no path from {value['from']} to {value['to']}"
        )
    chain = value.get("path") or []
    width = console.terminal_width()

    rows: list[tuple[str, str]] = []
    for depth, node in enumerate(chain):
        label = str(node.get("label", node.get("id", "?")))
        via = node.get("via")
        indent = "  " + "   " * depth
        if via:
            # `◂──` reads as "this one points back at the previous", which is
            # what a reverse edge means, without needing a legend.
            arrow = "──▸ " if node.get("forward", True) else "◂── "
            left = f"{indent}└─ {console.style(str(via), console.ACCENT)} {arrow}{label}"
        else:
            left = f"{indent}{console.style(label, console.BOLD)}"
        location = ""
        if node.get("path"):
            line = node.get("line")
            location = f"{node['path']}:{line}" if line else str(node["path"])
        rows.append((left, location))

    longest = max((len(_plain(left)) for left, _ in rows), default=0)
    body = []
    for left, location in rows:
        if not location or longest + len(location) + 4 > width:
            body.append(left)
            continue
        pad = " " * (longest - len(_plain(left)) + 2)
        body.append(left + pad + console.style(location, console.MUTED))

    symbols = len(chain)
    footer = console.style(
        f"  {value['hops']} hop(s) {console.BULLET} {symbols} symbols "
        f"{console.BULLET} no files opened",
        console.MUTED,
    )
    header = console.status_line(
        True, f"shortest path {console.DASH} {value['hops']} hops"
    )
    return "\n".join([header, "", *body, "", footer])


def _plain(text: str) -> str:
    """Visible length helper: the console owns the escape-stripping rule."""

    return console.strip_ansi(text)


def _render_code_hubs(value: dict[str, Any]) -> str:
    hubs = value.get("hubs") or []
    header = console.status_line(True, f"{len(hubs)} hub(s)")
    if not hubs:
        return header
    rows = [
        [hub.get("label", hub.get("id")), hub.get("path", "-"), str(hub.get("edges", 0))]
        for hub in hubs
    ]
    return "\n".join([header, "", console.table(["symbol", "path", "edges"], rows)])


def _render_code_communities(value: dict[str, Any]) -> str:
    communities = value.get("communities") or []
    header = console.status_line(True, f"{value['count']} community cluster(s)")
    if not communities:
        return header
    rows = [
        [community["label"], str(community["size"]), f"{community['cohesion']:.3f}"]
        for community in communities
    ]
    return "\n".join([header, "", console.table(["community", "size", "cohesion"], rows)])


def _render_code_cycles(value: dict[str, Any]) -> str:
    cycles = value.get("cycles") or []
    header = console.status_line(not cycles, f"{value['count']} import cycle(s)")
    if not cycles:
        return header
    rows = [
        [f" {console.ARROW} ".join(cycle.get("cycle", [])), str(cycle.get("length", "")), cycle.get("why", "")]
        for cycle in cycles
    ]
    return "\n".join([header, "", console.table(["cycle", "length", "why"], rows)])


def _render_code_diff(value: dict[str, Any]) -> str:
    parts = [console.status_line(True, value.get("summary", "diff computed"))]
    for key, label in (
        ("new_nodes", "new nodes"),
        ("removed_nodes", "removed nodes"),
        ("new_edges", "new edges"),
        ("removed_edges", "removed edges"),
    ):
        items = value.get(key) or []
        if not items:
            continue
        parts.append("")
        parts.append(console.rule(label))
        if key.endswith("_nodes"):
            parts.append(console.table(["symbol"], [[item.get("label", item.get("id"))] for item in items]))
        else:
            parts.append(
                console.table(
                    ["source", "relation", "target"],
                    [[item.get("source"), item.get("relation", ""), item.get("target")] for item in items],
                )
            )
    return "\n".join(parts)


def _render_doctor(value: dict[str, Any]) -> str:
    environment = value.get("environment") or {}
    code = value.get("code") or {}
    known = code.get("grammars_known", 0)
    installed = code.get("grammars_installed", 0)
    missing = code.get("grammars_missing") or []
    parts = [
        console.status_line(
            bool(value.get("healthy")),
            "installation complete"
            if value.get("healthy")
            else f"{len(missing)} language(s) cannot be parsed",
        ),
        "",
        console.kv_panel(
            [
                ("install method", str(environment.get("label", "?"))),
                ("interpreter", str(environment.get("executable", "?"))),
                ("grammars", f"{installed}/{known}"),
                # The two facts the incident turned on: which directory a build
                # scans, and why it is that one.
                ("code root", str(code.get("root", "?"))),
                ("", console.style(str(code.get("why_this_root", "")), console.MUTED)),
                ("scan limit", f"{code.get('scan_limit', '?')} files"),
            ]
        ),
    ]
    if missing:
        parts += ["", console.rule("missing grammars")]
        parts.append(console.style("  " + ", ".join(missing), console.WARN))
        if code.get("install"):
            parts += ["", console.style("  " + str(code["install"]), console.MUTED)]
    enforcement = value.get("enforcement") or {}
    layers = enforcement.get("layers") or []
    if layers:
        parts += ["", console.rule("enforcement")]
        parts.append(
            console.table(
                ["layer", "state"],
                [
                    [layer["layer"], console.status_line(layer["active"], layer.get("detail", ""))]
                    for layer in layers
                ],
            )
        )
    probe = enforcement.get("write_gate_probe")
    if probe:
        # Rendered apart from the table above: that table says what is
        # installed, and this one line says what actually happened when the
        # gate was asked to refuse a write.
        parts += ["", console.rule("write gate, exercised")]
        parts.append(
            "  " + console.status_line(
                probe["state"] in {"enforcing", "absent"},
                str(probe.get("detail", "")),
            )
        )
        if probe.get("hook_said"):
            parts.append(console.style(f"  {probe['hook_said']}", console.MUTED))
    return "\n".join(parts)


def _render_code_build(value: dict[str, Any]) -> str:
    rows = [
        ("written", str(value.get("written", "-"))),
        ("nodes", str(value.get("nodes", 0))),
        ("edges", str(value.get("edges", 0))),
        ("files", str(value.get("files", 0))),
    ]
    parts = [console.kv_panel(rows)]
    # A build that dropped a language is the one result worth interrupting for:
    # it succeeded, and the number above is wrong in a way nothing else says.
    missing = value.get("missing_grammars") or []
    if missing:
        names = ", ".join(str(entry.get("distribution", "")) for entry in missing)
        parts += [
            "",
            console.style(
                f"  {value.get('unreachable_files', 0)} file(s) contributed nothing — "
                f"missing: {names}",
                console.WARN,
            ),
        ]
    # Louder than a missing grammar, because a missing grammar at least says
    # what it needs. This is the category with no explanation attached, and it
    # is what a broken worker pool or an unwalkable scan root looks like from
    # here — the two failures that previously showed up as a plausible number.
    unexplained = int(value.get("unexplained_files") or 0)
    if unexplained:
        ratio = float(value.get("unexplained_ratio") or 0)
        parts += [
            "",
            console.style(
                f"  {console.CROSS} {unexplained} file(s) ({ratio:.0%}) have an "
                "installed grammar but produced nothing, and no reason was given.",
                console.ERR,
            ),
            console.style(
                "     Something stopped extraction rather than a language being "
                "absent. Re-run with BK_NO_PARALLEL=1 to rule out the worker pool.",
                console.MUTED,
            ),
        ]
    return "\n".join(parts)


def _render_code_status(value: dict[str, Any]) -> str:
    """The freshness verdict, plus a loud line when the graph is unreadable.

    Every existing state keeps exactly the rendering it had -- `_render_auto`
    is still what draws it -- because `fresh`, `stale` and `missing` were never
    the problem. `malformed` is, and it is the one verdict a reader must not
    have to notice inside a dump: the graph parses, its `generated_at` looks
    recent, and nothing about the block says the artefact answers nothing.

    Same treatment `_render_code_build` gives a build that dropped a language,
    and for the same reason: the panel above is not wrong, it is beside the
    point, and only an interruption says so.
    """

    rendered = _render_auto(value)
    if value.get("state") != "malformed":
        return rendered
    where = str(value.get("collection", "the graph"))
    index = value.get("index")
    if index is not None:
        where = f"{where}[{index}]"
    missing = value.get("missing") or []
    problem = (
        f"missing {', '.join(str(field) for field in missing)}"
        if missing
        else str(value.get("problem", "malformed"))
    )
    return "\n".join(
        [
            rendered,
            "",
            console.style(
                f"  {console.CROSS} {where} is {problem} — every other "
                "bk code command refuses this graph.",
                console.ERR,
            ),
            console.style(
                f"     {value.get('hint', '')}",
                console.MUTED,
            ),
        ]
    )


_RENDERERS: dict[str, Callable[[dict[str, Any]], str]] = {
    "init": _render_init,
    "capture": _render_capture,
    "status": _render_status,
    "doctor": _render_doctor,
    "search": _render_search,
    "context": _render_context,
    "lint": _render_lint,
    "proposals": _render_proposals,
    "ingest": _render_ingest,
    "approve": _render_approve,
    "reject": _render_reject,
    "forget": _render_forget,
    "file": _render_file,
    "ask": _render_ask,
    "digest": _render_digest,
    "resurface": _render_resurface,
}

_CODE_RENDERERS: dict[str, Callable[[dict[str, Any]], str]] = {
    "build": _render_code_build,
    "status": _render_code_status,
    "affected": _render_code_affected,
    "path": _render_code_path,
    "hubs": _render_code_hubs,
    "communities": _render_code_communities,
    "cycles": _render_code_cycles,
    "diff": _render_code_diff,
}


def _render(value: Any, command: str, *, code_command: str | None = None) -> str:
    if command == "code" and isinstance(value, dict):
        renderer = _CODE_RENDERERS.get(code_command or "")
        if renderer is not None:
            return renderer(value)
        return _render_auto(value)
    renderer = _RENDERERS.get(command) if isinstance(value, dict) else None
    if renderer is not None:
        return renderer(value)
    return _render_auto(value)


def _emit(
    value: Any, command: str, *, code_command: str | None = None, json_mode: bool
) -> None:
    if json_mode:
        print(json.dumps({"ok": True, "result": value}, ensure_ascii=False))
        return
    print(_render(value, command, code_command=code_command))


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
        print(console.status_line(True, f"allowed: {decision.get('path', '')}"))
    else:
        reason = str(decision.get("reason") or "This write is not permitted")
        remediation = str(decision.get("remediation") or "")
        line = f"bk: {reason} {remediation}".rstrip()
        print(console.style(line, console.ERR, stream=sys.stderr), file=sys.stderr)
    return 0 if allowed else 2


def _internal_error(exc: BaseException) -> InternalError:
    """Summarize an unmodelled failure; the traceback stays on stderr."""

    print(
        console.style("bk: unhandled internal error", console.ERR, stream=sys.stderr),
        file=sys.stderr,
    )
    traceback.print_exception(exc, file=sys.stderr)
    sys.stderr.flush()
    name = type(exc).__name__
    message = str(exc).strip()
    return InternalError(
        f"{name}: {message}" if message else name,
        details={"kind": name},
    )


def _install_hint_for(details: dict[str, Any]) -> dict[str, Any]:
    """Turn a `needs` list into the command that works on *this* machine.

    The application layer names what is missing (`{"needs": ["brainskit[code]"]}`)
    and stops there, because which command installs it is a fact about the
    running interpreter — not something a layer that must not import
    infrastructure can know, and not something to hardcode. Every hint that did
    hardcode it said `pip install …`, which is unrunnable under `uv tool`: that
    environment ships no `pip`, so the operator either saw a failure or silently
    installed the package into an unrelated interpreter and hit the identical
    message on the retry.

    Enriching here, at the one place errors are rendered, means every raiser
    gets a correct hint without any of them knowing how `bk` was installed.
    """

    needs = details.get("needs")
    if not isinstance(needs, list) or not needs or "hint" in details:
        return details
    return {**details, "hint": pyenv.install_hint([str(item) for item in needs])}


def _emit_error(error: BrainskitError, *, json_mode: bool) -> None:
    payload = {
        "ok": False,
        "error": {
            "code": error.code,
            "message": str(error),
            "details": _install_hint_for(error.details or {}),
        },
    }
    stream = sys.stdout if json_mode else sys.stderr
    if json_mode:
        print(json.dumps(payload, ensure_ascii=False), file=stream)
    else:
        print(console.style(f"bk: {error}", console.ERR, stream=stream), file=stream)
        details = _install_hint_for(error.details or {})
        for line in _human_detail_lines(details, stream=stream):
            print(line, file=stream)


def _human_detail_lines(details: dict[str, Any], *, stream: IO[str]) -> list[str]:
    """Render error `details` as plain sentences, never as a JSON blob.

    A `--json` caller gets `details` verbatim in the envelope above; a person
    at a terminal should not see the same `{...}` dump. `hint` becomes a
    follow-up sentence (the common case -- most raise sites carry only that);
    anything else becomes a `key: value` line with no braces or quotes, even
    where the value is itself a small list of conflicts -- structured enough
    to read, never literal `json.dumps`.
    """

    lines: list[str] = []
    hint = details.get("hint")
    if isinstance(hint, str) and hint:
        lines.append(
            console.style(f"  {console.ARROW} {hint}", console.MUTED, stream=stream)
        )
    for key, value in details.items():
        if key == "hint":
            continue
        lines.append(f"  {key}: {_human_detail_value(value)}")
    return lines


def _human_detail_value(value: Any) -> str:
    """A plain-text rendering of one detail value -- never `{`, `}`, or `"`."""

    if isinstance(value, list):
        if not value:
            return "(none)"
        if all(isinstance(item, dict) for item in value):
            return "; ".join(
                ", ".join(f"{k}: {_human_detail_value(v)}" for k, v in item.items())
                for item in value
            )
        return ", ".join(str(item) for item in value)
    if isinstance(value, dict):
        return ", ".join(f"{k}: {_human_detail_value(v)}" for k, v in value.items())
    return str(value)


if __name__ == "__main__":
    raise SystemExit(main())
