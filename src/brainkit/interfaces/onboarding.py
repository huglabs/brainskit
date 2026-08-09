"""The guided `bk init` flow: detect what is true, then ask what is left.

The wizard this replaces asked twenty questions to reach its own defaults.
Ten of them were per-branch `privacy`/`filing` pairs -- so naming a fourth
branch bought you two more prompts -- and four asked the operator to hand-author
JSON at a terminal prompt, including a thirty-line integrations object.

Two of its failures were not cosmetic:

- **Its happy path built a vault that could not run.** It offered `qwen3:8b`
  for all six jobs without ever asking ollama what was installed, so accepting
  every default on a machine without that model produced a config whose every
  LLM job would fail at first use, silently.
- **A late rejection discarded everything.** Answers were validated only when
  `VaultConfig.from_dict` assembled them, after the last prompt. Pasting a
  partial integrations object -- the natural way to enable one integration --
  failed the completeness check and took all twenty answers with it, leaving no
  vault and no way to resume.

So the order here is inverted: probe first, then ask only what probing cannot
settle, and validate every answer at the prompt that produced it. What is left
is three screens -- purpose, model, extras -- and a confirmation you can walk
back into.
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from collections.abc import Sequence
from dataclasses import dataclass, field
from importlib.resources import files
from pathlib import Path
from typing import Any

from ..domain.model import (
    DEFAULT_IGNORE_PATTERNS,
    INTEGRATION_NAMES,
    FilingMode,
    PrivacyMode,
)
from ..infrastructure.vault import workspace_repos
from . import console, prompt
from .prompt import Choice

DEFAULT_OLLAMA_URL = "http://127.0.0.1:11434"
_PROBE_TIMEOUT = 2.0

#: Mapped from `$LANG`, which is the only signal the machine actually has.
#: Anything unrecognised falls back to English rather than guessing, and the
#: operator can still overwrite it -- the previous wizard hardcoded
#: "Portuguese (Brazil)" as *the* default for every machine on earth.
_LANGUAGES = {
    "pt_BR": "Portuguese (Brazil)",
    "pt": "Portuguese",
    "es": "Spanish",
    "fr": "French",
    "de": "German",
    "it": "Italian",
    "en": "English",
}


@dataclass(frozen=True)
class OllamaModel:
    name: str
    parameter_size: str
    context_length: int
    tools: bool

    @property
    def note(self) -> str:
        bits = [b for b in (self.parameter_size, _context(self.context_length)) if b]
        if not self.tools:
            bits.append("no tool support")
        return " · ".join(bits)

    @property
    def billions(self) -> float:
        match = re.match(r"([\d.]+)\s*B", self.parameter_size or "", re.IGNORECASE)
        return float(match.group(1)) if match else 0.0


@dataclass(frozen=True)
class OllamaProbe:
    """What the machine says about ollama, asked once before any question."""

    base_url: str
    reachable: bool
    models: tuple[OllamaModel, ...] = ()
    error: str = ""

    @property
    def usable(self) -> tuple[OllamaModel, ...]:
        return tuple(m for m in self.models if m.tools) or self.models


@dataclass(frozen=True)
class Environment:
    vault: Path
    workspace: Path
    is_git_repo: bool
    has_agent_dir: bool
    language: str
    #: Git repositories one level below the candidate directory. Two or more
    #: mean it is a workspace holding projects rather than a project -- the
    #: shape that produced a vault at `~/Projetos/tools` whose code graph then
    #: indexed every sibling checkout.
    child_repos: tuple[Path, ...] = ()
    #: A vault above this one. Nesting makes which vault a command addresses
    #: depend on the directory it was run from, so it is refused rather than
    #: warned about.
    enclosing_vault: Path | None = None


@dataclass(frozen=True)
class Preset:
    key: str
    label: str
    #: branch name -> (privacy, filing)
    branches: dict[str, tuple[PrivacyMode, FilingMode]]

    @property
    def note(self) -> str:
        return ", ".join(self.branches)


@dataclass
class Outcome:
    """What the wizard decided: where, what policy, and whether to wire an agent."""

    policy: dict[str, Any]
    wire_agent: bool = False
    summary: list[tuple[str, str]] = field(default_factory=list)
    #: The directory the operator chose. `None` for `--config`, which names its
    #: own path on the command line and never runs the location screen.
    vault: Path | None = None


# A private branch is `never-ingest` in every preset. It is the one policy
# choice with a consequence the operator cannot undo by editing config later:
# anything already sent to a provider has been sent.
_PRIVATE = (PrivacyMode.NEVER_INGEST, FilingMode.APPROVE_EACH)
_WORKING = (PrivacyMode.LOCAL_ONLY, FilingMode.APPROVE_EACH)
_REFERENCE = (PrivacyMode.LOCAL_ONLY, FilingMode.AUTO_REVIEW)

PRESETS: tuple[Preset, ...] = (
    Preset(
        "work",
        "Work",
        {"10-work": _WORKING, "20-research": _REFERENCE, "90-private": _PRIVATE},
    ),
    Preset(
        "personal",
        "Personal",
        {"10-life": _WORKING, "20-learning": _REFERENCE, "90-private": _PRIVATE},
    ),
    Preset(
        "research",
        "Research",
        {"10-papers": _REFERENCE, "20-notes": _WORKING, "90-private": _PRIVATE},
    ),
)


def job_names() -> list[str]:
    """The six judgment jobs, read from the shipped prompts.

    Derived rather than restated: `job_models` must cover exactly the jobs
    `JobSpecs.prompt` can resolve, and a hardcoded list here would drift the
    moment a job is added or renamed.
    """

    jobs = files("brainkit").joinpath("jobs")
    return sorted(
        entry.name[:-3]
        for entry in jobs.iterdir()
        if entry.name.endswith(".md") and not entry.name.startswith("_")
    )


def detect(vault: Path) -> Environment:
    workspace = _repo_root(vault) or vault
    return Environment(
        vault=vault,
        workspace=workspace,
        is_git_repo=(workspace / ".git").exists(),
        has_agent_dir=(workspace / ".claude").is_dir(),
        language=_language(),
        child_repos=tuple(workspace_repos(vault)),
        enclosing_vault=_enclosing_vault(vault),
    )


def _enclosing_vault(start: Path) -> Path | None:
    for parent in start.resolve().parents:
        if (parent / ".brain" / "config.json").is_file():
            return parent
    return None


def probe_ollama(base_url: str = DEFAULT_OLLAMA_URL) -> OllamaProbe:
    """Ask ollama what it has, with a short timeout and no exceptions escaping.

    A provider that is down is a fact to report on screen, never a reason for
    onboarding to fail -- the vault is still valid, its jobs simply will not
    run until the provider is up.
    """

    # The scheme is checked rather than trusted: `base_url` reaches here from
    # config or a `--config` file, and `urlopen` would happily follow a
    # `file:` URL and report the contents of a local path as a model list.
    if not base_url.startswith(("http://", "https://")):
        return OllamaProbe(
            base_url=base_url,
            reachable=False,
            error="provider URL must be http:// or https://",
        )
    url = base_url.rstrip("/") + "/api/tags"
    try:
        with urllib.request.urlopen(url, timeout=_PROBE_TIMEOUT) as response:  # noqa: S310 -- scheme checked above
            payload = json.load(response)
    except (urllib.error.URLError, OSError, ValueError, json.JSONDecodeError) as exc:
        return OllamaProbe(base_url=base_url, reachable=False, error=_reason(exc))
    models = []
    for raw in payload.get("models", []):
        details = raw.get("details") or {}
        models.append(
            OllamaModel(
                name=str(raw.get("name", "")),
                parameter_size=str(details.get("parameter_size") or ""),
                context_length=int(details.get("context_length") or 0),
                tools="tools" in (raw.get("capabilities") or []),
            )
        )
    models = [m for m in models if m.name]
    return OllamaProbe(base_url=base_url, reachable=True, models=tuple(models))


def run(vault: Path) -> Outcome:
    """Drive the three screens and return a policy that is valid by construction."""

    environment = detect(vault)
    probe = probe_ollama()
    print(console.banner())
    print(_context_panel(environment, probe))
    print()

    vault = _ask_location(environment)
    if vault != environment.vault:
        environment = detect(vault)

    while True:
        branches = _ask_purpose()
        model = _ask_model(probe)
        extras = _ask_extras(environment)
        policy = _assemble(environment, probe, branches, model, extras)
        summary = _summary(environment, branches, model, extras)
        print()
        print(console.kv_panel(summary))
        print()
        decision = prompt.select(
            "Create it?",
            [
                Choice("create", "Create the vault"),
                Choice("edit", "Change an answer", "start the three questions over"),
                Choice("cancel", "Cancel", "nothing is written"),
            ],
        )
        if decision == "create":
            return Outcome(
                policy=policy,
                wire_agent="agent" in extras,
                summary=summary,
                vault=environment.vault,
            )
        if decision == "cancel":
            raise prompt.Cancelled()
        print()


def _ask_location(environment: Environment) -> Path:
    """Where the vault goes — the question that was never asked.

    `bk init` defaulted its path to `.` and had no location prompt at all, so
    running it one directory too high was invisible until the code graph had
    indexed every repository below that directory. Asking costs one screen and
    is the only point at which the answer is cheap to change.

    The default is `<repo>/.brainkit`: hidden and tool-owned, beside `.git` and
    `.claude`. Not `docs/`, which is the repository's own published
    documentation and no place for machine-managed state.

    Three shapes, three different questions:

    - **a repository** — offer `.brainkit` inside it;
    - **a directory holding repositories** — offer each of them, because this
      is the mistake, and demote "here anyway" with its consequence stated;
    - **inside another vault** — refuse outright.
    """

    if environment.enclosing_vault is not None:
        raise prompt.Cancelled(
            f"{_short(environment.vault)} is already inside the vault at "
            f"{_short(environment.enclosing_vault)}. Nesting vaults makes which "
            "one a command addresses depend on where it is run."
        )

    here = environment.vault
    choices: list[Choice[Path | None]] = []
    if len(environment.child_repos) >= 2:
        names = ", ".join(repo.name for repo in environment.child_repos[:3])
        print(
            console.style(
                f"  {_short(here)} holds {len(environment.child_repos)} "
                f"repositories ({names}) — a vault here would take all of them "
                "as its code root.",
                console.WARN,
            )
        )
        print()
        for repo in environment.child_repos:
            choices.append(
                Choice(repo / ".brainkit", f"{repo.name}/.brainkit", "inside that project")
            )
    elif environment.is_git_repo or (here / ".git").exists():
        choices.append(
            Choice(here / ".brainkit", "./.brainkit", "inside this repository")
        )
    else:
        choices.append(Choice(here, _short(here), "here — this is a standalone vault"))
        choices.append(
            Choice(here / ".brainkit", "./.brainkit", "in a subdirectory of it")
        )
    choices.append(Choice(None, "Custom…", "type a path"))

    picked = prompt.select("Where should this vault live?", choices)
    if picked is not None:
        return Path(picked)
    typed = prompt.text("Vault path", str(here / ".brainkit"), validate=_valid_location)
    return Path(typed).expanduser()


def _valid_location(value: str) -> str | None:
    candidate = Path(value).expanduser()
    if not value.strip():
        return "Name a path."
    if (candidate / ".brain" / "config.json").is_file():
        return f"{value} is already a vault."
    if _enclosing_vault(candidate) is not None:
        return f"{value} is inside another vault."
    return None


def _ask_purpose() -> dict[str, tuple[PrivacyMode, FilingMode]]:
    choices: list[Choice[Preset | None]] = [
        Choice(p, p.label, p.note) for p in PRESETS
    ]
    choices.append(Choice(None, "Custom", "name your own branches"))
    picked = prompt.select("What is this vault for?", choices)
    if picked is None:
        return _ask_custom_branches()
    return dict(picked.branches)


def _ask_custom_branches() -> dict[str, tuple[PrivacyMode, FilingMode]]:
    """Custom branches without the old two-prompts-per-branch tax.

    The old wizard asked `privacy` and `filing` for every branch, so the cost
    of naming branches grew with how many you named. Both questions are asked
    once here -- privacy as "which of these never leave the machine", filing as
    a single policy for the rest -- because per-branch divergence beyond that
    is a config edit, not an onboarding decision.
    """

    names = prompt.text(
        "Branch names (comma-separated)",
        "10-work,20-research,90-private",
        validate=_valid_branches,
    )
    branches = [n.strip() for n in names.split(",") if n.strip()]
    private = prompt.multiselect(
        "Which never leave this machine?",
        [Choice(b, b) for b in branches],
        selected=[i for i, b in enumerate(branches) if "priv" in b or "person" in b],
        hint="↑↓ · space · Enter · these are never sent to an LLM",
    )
    filing = prompt.select(
        "How should the rest be filed?",
        [
            Choice(FilingMode.APPROVE_EACH, "Approve each", "you review every filing"),
            Choice(
                FilingMode.AUTO_REVIEW,
                "Automatic",
                "filed on capture, reviewed in the digest",
            ),
        ],
    )
    return {
        name: (_PRIVATE if name in private else (PrivacyMode.LOCAL_ONLY, filing))
        for name in branches
    }


def _ask_model(probe: OllamaProbe) -> str | None:
    """Choose from what is installed, or say plainly that nothing is."""

    if not probe.reachable:
        print(
            console.style(
                f"  ollama is not reachable at {probe.base_url} — {probe.error}",
                console.WARN,
            )
        )
        typed = prompt.text(
            "Model to configure anyway (jobs stay idle until ollama runs)",
            "qwen2.5:3b",
        )
        return typed or None
    if not probe.models:
        print(
            console.style(
                "  ollama is running but has no models — pull one with "
                "`ollama pull qwen2.5:3b`",
                console.WARN,
            )
        )
        typed = prompt.text("Model to configure anyway", "qwen2.5:3b")
        return typed or None

    usable = sorted(probe.usable, key=lambda m: -m.billions)
    choices = [Choice(m.name, m.name, m.note) for m in usable]
    return str(
        prompt.select(
            "Model for the 6 local jobs",
            choices,
            hint="↑↓ · Enter · only models found on this machine",
        )
    )


def _ask_extras(environment: Environment) -> list[str]:
    agent_note = (
        "detected .claude/ in this project"
        if environment.has_agent_dir
        else f"writes .claude/ and CLAUDE.md in {_short(environment.workspace)}"
    )
    choices = [
        Choice("agent", "Wire up Claude Code", agent_note),
        Choice("obsidian", "Obsidian sync", "mirror wiki/ and views/ into a vault"),
        Choice("web", "Local web UI", "127.0.0.1:8765"),
    ]
    return [
        str(value)
        for value in prompt.multiselect(
            "Anything else?", choices, selected=[0], hint="↑↓ · space · Enter"
        )
    ]


def _assemble(
    environment: Environment,
    probe: OllamaProbe,
    branches: dict[str, tuple[PrivacyMode, FilingMode]],
    model: str | None,
    extras: Sequence[str],
) -> dict[str, Any]:
    """Build a complete, schema-valid policy. Never a partial one.

    Every key `VaultConfig.from_dict` requires is written here, and the
    integration set is generated from `INTEGRATION_NAMES` rather than from
    whatever the operator happened to name -- which is what makes the
    "integration policy set is incomplete" rejection structurally unreachable
    instead of merely less likely.
    """

    chosen_model = model or "qwen2.5:3b"
    return {
        "version": 3,
        "wiki_language": environment.language,
        "inbox_policy": {
            "privacy": PrivacyMode.LOCAL_ONLY.value,
            "filing": FilingMode.APPROVE_EACH.value,
        },
        "branches": {
            name: {"privacy": privacy.value, "filing": filing.value}
            for name, (privacy, filing) in branches.items()
        },
        "providers": {"ollama": {"base_url": probe.base_url}},
        "job_models": {
            job: {"provider": "ollama", "model": chosen_model} for job in job_names()
        },
        "sources": [],
        "ignore": list(DEFAULT_IGNORE_PATTERNS),
        # Written explicitly rather than left to discovery. Discovery is a
        # walk, and a walk that finds nothing has to fall back to *something*
        # -- which is how a vault came to claim the directory above it. Naming
        # the root here means the vault records the answer it was created with.
        "code_root": _relative_code_root(environment),
        "schedule": {"digest": "0 8 * * *"},
        "taxonomy_seed": sorted(branches),
        "novelty": {
            "duplicate_similarity_threshold": 0.9,
            "min_new_token_ratio": 0.15,
            "stale_after_days": 30,
        },
        "integrations": {
            name: {
                "enabled": name in extras,
                "managed": name in extras,
                "options": (
                    {"host": "127.0.0.1", "port": 8765, "consumer": "human"}
                    if name == "web"
                    else {}
                ),
            }
            for name in sorted(INTEGRATION_NAMES)
        },
    }


def _summary(
    environment: Environment,
    branches: dict[str, tuple[PrivacyMode, FilingMode]],
    model: str | None,
    extras: Sequence[str],
) -> list[tuple[str, str]]:
    private = [n for n, (privacy, _) in branches.items() if privacy is PrivacyMode.NEVER_INGEST]
    integrations = [e for e in extras if e in INTEGRATION_NAMES]
    rows = [
        ("vault", f"{_short(environment.vault)} · {environment.language}"),
        # The row whose absence made the incident invisible: nothing said which
        # directory a build would scan until after it had scanned it.
        ("code root", _code_root_row(environment)),
        ("branches", ", ".join(branches)),
        ("never sent to an LLM", ", ".join(private) or "none"),
        ("models", f"{model or 'unset'} → all {len(job_names())} jobs"),
    ]
    if integrations:
        rows.append(("integrations", ", ".join(sorted(integrations))))
    rows.append(
        (
            "agent",
            f".claude/ + CLAUDE.md in {_short(environment.workspace)}"
            if "agent" in extras
            else "not wired — run `bk hooks install --agent claude` later",
        )
    )
    return rows


def _context_panel(environment: Environment, probe: OllamaProbe) -> str:
    if not probe.reachable:
        ollama = console.style(f"not reachable at {probe.base_url}", console.WARN)
    elif not probe.models:
        ollama = console.style("running · no models pulled", console.WARN)
    else:
        count = len(probe.models)
        ollama = console.style(
            f"{console.CHECK} running · {count} model{'s' if count != 1 else ''}",
            console.OK,
        )
    where = _short(environment.vault)
    if environment.is_git_repo:
        where += console.style("  git repo", console.MUTED)
    return console.indent(console.kv_panel([("vault", where), ("ollama", ollama)]))


def _valid_branches(value: str) -> str | None:
    names = [n.strip() for n in value.split(",") if n.strip()]
    if not names:
        return "Name at least one branch."
    if len(set(names)) != len(names):
        return "Branch names must be unique."
    for name in names:
        if name.startswith("_"):
            return f"{name!r} is reserved — `_inbox` is created for you."
    return None


def _repo_root(start: Path) -> Path | None:
    current = start.resolve()
    for candidate in (current, *current.parents):
        if (candidate / ".git").exists():
            return candidate
    return None


def _language() -> str:
    raw = os.environ.get("LANG") or os.environ.get("LC_ALL") or ""
    tag = raw.split(".")[0]
    return _LANGUAGES.get(tag) or _LANGUAGES.get(tag.split("_")[0], "English")


def _context(length: int) -> str:
    if not length:
        return ""
    if length >= 1000:
        return f"{length // 1000}k ctx"
    return f"{length} ctx"


def _short(path: Path) -> str:
    try:
        relative = str(path.resolve().relative_to(Path.cwd()))
        return "." if relative == "." else "./" + relative
    except ValueError:
        home = Path.home()
        try:
            return "~/" + str(path.resolve().relative_to(home))
        except ValueError:
            return str(path)


def _reason(exc: BaseException) -> str:
    if isinstance(exc, urllib.error.URLError) and exc.reason is not None:
        return str(exc.reason)
    return str(exc) or exc.__class__.__name__


def _relative_code_root(environment: Environment) -> str:
    """The code root as a vault-relative path, or "" for the vault itself.

    `""` is the honest way to say "this vault indexes only what it owns",
    which is what a standalone vault wants and what the old `self.root.parent`
    fallback got wrong.
    """

    repo = _repo_root(environment.vault)
    if repo is None:
        return ""
    try:
        return os.path.relpath(repo, environment.vault.resolve())
    except ValueError:
        return ""


def _code_root_row(environment: Environment) -> str:
    repo = _repo_root(environment.vault)
    if repo is None:
        return f"{_short(environment.vault)}  (no repository — the vault only)"
    return f"{_short(repo)}  ({_count_files(repo)} files)"


def _count_files(root: Path) -> str:
    """A rough file count, capped so a mis-sited vault does not walk forever.

    Deliberately not the extractor's own walk: this runs before any vault
    exists, must never fail, and only has to be right enough to make an
    obviously-too-large root obvious.
    """

    limit = 20_000
    seen = 0
    skip = {".git", "node_modules", ".venv", "venv", "__pycache__", "target", "dist"}
    for _, directories, filenames in os.walk(root):
        directories[:] = [d for d in directories if d not in skip]
        seen += len(filenames)
        if seen > limit:
            return f"{limit}+"
    return str(seen)
