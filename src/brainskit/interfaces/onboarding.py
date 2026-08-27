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
    ValidationError,
)
from ..infrastructure import llm
from ..infrastructure.vault import workspace_repos
from . import console, prompt
from .prompt import Choice

DEFAULT_OLLAMA_URL = "http://127.0.0.1:11434"
OPENROUTER_URL = "https://openrouter.ai/api/v1"
OPENROUTER_KEY_ENV = "OPENROUTER_API_KEY"
_PROBE_TIMEOUT = 2.0
#: The model catalogue is ~400 entries of JSON over the public internet, not a
#: loopback liveness check. Sharing `_PROBE_TIMEOUT` would report a working
#: account as unreachable on any ordinary connection.
_CATALOGUE_TIMEOUT = 10.0
#: How many paid models the first screen offers before the search. Enough to
#: cover "something cheap that works" without turning one screen into the 296
#: rows the search exists to handle.
_SHORTLIST = 12

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
class OpenRouterModel:
    """One row of the catalogue, reduced to what a choice actually turns on."""

    id: str
    name: str
    context_length: int
    tools: bool
    #: Structured decoding, which is not a preference here: every judgment job
    #: sends `response_format: json_schema`, so a model without it answers with
    #: prose the repair loop cannot fix.
    structured: bool
    #: USD per million tokens. Negative means the catalogue declined to say --
    #: `openrouter/auto` reports `-1` because its price is whatever it routes
    #: to -- which is neither free nor comparable, and must not sort as cheapest.
    prompt_price: float
    completion_price: float

    @property
    def free(self) -> bool:
        return self.prompt_price == 0 and self.completion_price == 0

    @property
    def priced(self) -> bool:
        return self.prompt_price >= 0 and self.completion_price >= 0

    @property
    def cost(self) -> str:
        if not self.priced:
            return "price varies by route"
        if self.free:
            return "free"
        return f"${self.prompt_price:g}/M in · ${self.completion_price:g}/M out"

    @property
    def note(self) -> str:
        bits = [self.cost, _context(self.context_length)]
        if not self.structured:
            bits.append("no structured output")
        return " · ".join(b for b in bits if b)


@dataclass(frozen=True)
class OpenRouterProbe:
    """The catalogue, fetched unauthenticated, so a model can be chosen first."""

    reachable: bool
    models: tuple[OpenRouterModel, ...] = ()
    error: str = ""

    @property
    def usable(self) -> tuple[OpenRouterModel, ...]:
        """Models that can actually run a job, or every model if none can.

        Same rule as ollama's: narrowing to nothing would strand the operator,
        so an empty filter falls back to the whole list rather than to an empty
        screen. What is filtered *on* differs, because the two providers are
        asked different questions -- ollama advertises tool support and nothing
        about schemas, while OpenRouter names structured output directly.
        """

        fit = tuple(m for m in self.models if m.structured and m.tools)
        return fit or tuple(m for m in self.models if m.structured) or self.models


@dataclass(frozen=True)
class KeyCheck:
    """What the provider says about a key, asked before it is written anywhere."""

    valid: bool
    label: str = ""
    error: str = ""
    free_tier: bool = False
    limit_remaining: float | None = None

    @property
    def note(self) -> str:
        """What the account is, for a line that is printed to a terminal.

        The label is withheld when it is key-shaped. OpenRouter names an
        auto-generated key after the key itself (`sk-or-v1-819…f92`), so echoing
        it puts something indistinguishable from a credential on screen and into
        every pasted transcript -- for no information, since the operator just
        typed it.
        """

        bits = [] if _looks_like_a_key(self.label) else [self.label]
        if self.free_tier:
            bits.append("free tier")
        if self.limit_remaining is not None:
            bits.append(f"${self.limit_remaining:.2f} left")
        return " · ".join(b for b in bits if b) or "no account details"


@dataclass(frozen=True)
class ModelChoice:
    """Which provider runs the six jobs, and under what credential.

    One value rather than a loose `(provider, model)` pair because the three
    fields have to agree: an `openrouter` choice without `api_key_env` assembles
    a policy the driver refuses at first use, and that failure would surface
    days later as a job that does not run.
    """

    provider: str
    model: str
    base_url: str
    api_key_env: str = ""
    #: The key itself, when the operator typed one the environment did not
    #: already hold. Carried out to `Outcome` and written to the machine's
    #: credential store on create -- never into the policy, which is printed.
    api_key: str = ""

    @property
    def local(self) -> bool:
        """Whether inference happens on this machine.

        Only Ollama does. An agent CLI is a local *process* driving a remote
        model, which is the most plausible misreading of that whole feature.
        """

        return self.provider == "ollama"

    @property
    def provider_config(self) -> dict[str, Any]:
        # An agent CLI has neither: it is spawned, not fetched, and it
        # authenticates with its own session. Writing an empty `base_url` would
        # be a URL nothing ever calls, and `_create_driver` would have to learn
        # to ignore it.
        config: dict[str, Any] = {}
        if self.base_url:
            config["base_url"] = self.base_url
        if self.api_key_env:
            config["api_key_env"] = self.api_key_env
        return config


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
    #: Provider keys to store on this machine, by the variable name they answer
    #: to. Carried out rather than written during the wizard so that cancelling
    #: at the confirmation screen really does write nothing -- and kept out of
    #: `policy`, which is printed by `--print-config` and every `--json` caller.
    credentials: dict[str, str] = field(default_factory=dict)


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

    jobs = files("brainskit").joinpath("jobs")
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

    payload, error = _get_json(base_url.rstrip("/") + "/api/tags", _PROBE_TIMEOUT)
    if payload is None:
        return OllamaProbe(base_url=base_url, reachable=False, error=error)
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


def _get_json(
    url: str, timeout: float, headers: dict[str, str] | None = None
) -> tuple[Any, str]:
    """One GET, no exceptions escaping, `(payload, error)` either way.

    The scheme is checked rather than trusted: a base URL reaches here from
    config or a `--config` file, and `urlopen` would happily follow a `file:`
    URL and report the contents of a local path as a model list.

    An HTTP status is returned as an error *string* rather than raised, because
    every caller is probing: a provider that is down, unauthenticated or
    rate-limited is a fact to put on screen, never a reason for onboarding to
    fail. The vault is still valid; its jobs simply will not run yet.
    """

    if not url.startswith(("http://", "https://")):
        return None, "provider URL must be http:// or https://"
    request = urllib.request.Request(url, headers=headers or {})  # noqa: S310 -- scheme checked above
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 -- scheme checked above
            return json.load(response), ""
    except urllib.error.HTTPError as exc:
        return None, f"HTTP {exc.code}"
    except (urllib.error.URLError, OSError, ValueError, json.JSONDecodeError) as exc:
        return None, _reason(exc)


def probe_openrouter(base_url: str = OPENROUTER_URL) -> OpenRouterProbe:
    """Fetch the model catalogue, which needs no credential.

    Unauthenticated on purpose: it means the model list is on screen whether or
    not the key the operator is about to type turns out to be good, so a typo in
    the key does not also cost them the catalogue.
    """

    payload, error = _get_json(base_url.rstrip("/") + "/models", _CATALOGUE_TIMEOUT)
    if payload is None:
        return OpenRouterProbe(reachable=False, error=error)
    models = []
    for raw in payload.get("data", []) if isinstance(payload, dict) else []:
        identifier = str(raw.get("id", ""))
        if not identifier:
            continue
        supported = {str(p) for p in (raw.get("supported_parameters") or [])}
        pricing = raw.get("pricing") or {}
        models.append(
            OpenRouterModel(
                id=identifier,
                name=str(raw.get("name") or identifier),
                context_length=int(raw.get("context_length") or 0),
                tools="tools" in supported,
                structured="structured_outputs" in supported,
                # Quoted per *token* as a string; every price a human reasons
                # about is per million, and converting once here keeps the
                # factor out of the four places that would otherwise repeat it.
                prompt_price=_price(pricing.get("prompt")),
                completion_price=_price(pricing.get("completion")),
            )
        )
    return OpenRouterProbe(reachable=True, models=tuple(models))


def check_openrouter_key(key: str, base_url: str = OPENROUTER_URL) -> KeyCheck:
    """Ask the provider whether a key works, before anything is written.

    The whole point of asking at the prompt: a key stored unverified fails at
    the first job instead, hours later, as `Provider rejected the API key` from
    a command that was not about credentials at all.
    """

    if not key.strip():
        return KeyCheck(valid=False, error="no key given")
    payload, error = _get_json(
        base_url.rstrip("/") + "/key",
        _PROBE_TIMEOUT * 5,
        {"Authorization": f"Bearer {key.strip()}"},
    )
    if payload is None:
        if error == "HTTP 401":
            return KeyCheck(valid=False, error="the provider rejected this key")
        return KeyCheck(valid=False, error=error)
    data = payload.get("data") if isinstance(payload, dict) else None
    data = data if isinstance(data, dict) else {}
    remaining = data.get("limit_remaining")
    return KeyCheck(
        valid=True,
        label=str(data.get("label") or ""),
        free_tier=bool(data.get("is_free_tier")),
        limit_remaining=float(remaining) if isinstance(remaining, int | float) else None,
    )


def _looks_like_a_key(value: str) -> bool:
    """Anything shaped like a credential, however it reached us.

    Deliberately not "equals the key we hold": the point is that no
    key-shaped string is printed, whether it came back as a label, a name, or
    anything a provider adds later.
    """

    return value.strip().startswith("sk-")


def _price(raw: Any) -> float:
    """Per-million-token price, or `-1` for a catalogue that declined to say."""

    try:
        value = float(str(raw))
    except (TypeError, ValueError):
        return -1.0
    return value * 1_000_000 if value >= 0 else -1.0


PRESET_KEYS = tuple(preset.key for preset in PRESETS)


def default_policy(vault: Path, preset: str = "work") -> dict[str, Any]:
    """A complete, schema-valid policy without asking anyone anything.

    The wizard could already assemble one; there was simply no way to get it
    out without a terminal. So `bk init` off a TTY refused, the here-doc the
    quickstart documented refused identically, and `--config` with an empty
    object listed nine missing keys with no shapes, no template and no next
    command. The only complete specimen in the repository was the project's
    own vault, which a user never receives.

    That blocked CI, containers, agent-driven setup and any non-interactive
    shell -- precisely the audiences a local-first agent tool has.

    Built from the same `_assemble` the wizard uses, so the printed policy
    cannot drift from the one a human would have produced.
    """

    chosen = next((item for item in PRESETS if item.key == preset), None)
    if chosen is None:
        raise ValidationError(
            "Unknown preset",
            details={"preset": preset, "choices": list(PRESET_KEYS)},
        )
    probe = probe_ollama()
    return _assemble(detect(vault), _default_ollama_choice(probe), chosen.branches, [])


def _default_ollama_choice(probe: OllamaProbe) -> ModelChoice:
    """Local, and the largest installed model -- what a headless caller wants.

    `--print-config` and `--preset` must never reach for a cloud provider on
    their own: that would send evidence off the machine because a script asked
    for a default, which is the one decision an operator has to make in person.
    """

    installed = sorted(probe.usable, key=lambda m: -m.billions)
    return ModelChoice(
        provider="ollama",
        model=installed[0].name if installed else "qwen2.5:3b",
        base_url=probe.base_url,
    )


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
        choice = _ask_model(probe, branches)
        extras = _ask_extras(environment)
        policy = _assemble(environment, choice, branches, extras)
        summary = _summary(environment, branches, choice, extras)
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
                # Held rather than written here: "nothing is written" has to
                # include the credential, or cancelling at the last screen still
                # leaves a key on the machine the operator never confirmed.
                credentials=(
                    {choice.api_key_env: choice.api_key} if choice.api_key else {}
                ),
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

    The default is `<repo>/.brainskit`: hidden and tool-owned, beside `.git` and
    `.claude`. Not `docs/`, which is the repository's own published
    documentation and no place for machine-managed state.

    Three shapes, three different questions:

    - **a repository** — offer `.brainskit` inside it;
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
                Choice(repo / ".brainskit", f"{repo.name}/.brainskit", "inside that project")
            )
    elif environment.is_git_repo or (here / ".git").exists():
        choices.append(
            Choice(here / ".brainskit", "./.brainskit", "inside this repository")
        )
    else:
        choices.append(Choice(here, _short(here), "here — this is a standalone vault"))
        choices.append(
            Choice(here / ".brainskit", "./.brainskit", "in a subdirectory of it")
        )
    choices.append(Choice(None, "Custom…", "type a path"))

    picked = prompt.select("Where should this vault live?", choices)
    if picked is not None:
        return Path(picked)
    typed = prompt.text("Vault path", str(here / ".brainskit"), validate=_valid_location)
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


def _ask_model(
    probe: OllamaProbe,
    branches: dict[str, tuple[PrivacyMode, FilingMode]],
) -> ModelChoice:
    """Where the six judgment jobs run, and on which model.

    Provider first, because the second question depends on it: the models on
    this machine, the 400 in a catalogue and the ones an agent CLI is already
    configured for are not one list, and merging them would offer a choice whose
    consequence -- whether evidence leaves the machine, and who pays -- is
    invisible in the row being highlighted.
    """

    picked = _ask_provider(probe)
    if picked == "ollama":
        return _ask_ollama_model(probe)
    if picked == "openrouter":
        return _ask_openrouter(branches)
    return _ask_agent_cli(picked, branches)


def _ask_provider(probe: OllamaProbe) -> str:
    """Where the jobs run, with what is actually true about each on the row.

    Nothing is ever hidden. ollama being down, or an agent CLI not being
    installed, is a reason to dim a row and say why -- an option missing from a
    list reads as unsupported, while a dimmed one with a note tells the operator
    what to fix. ollama in particular stays *selectable* while down, because the
    flow has always let an unreachable provider be configured anyway.
    """

    if not probe.reachable:
        local = f"not running at {probe.base_url} — configure it anyway"
    elif not probe.models:
        local = "running, but no models pulled yet"
    else:
        count = len(probe.models)
        local = f"{count} model{'s' if count != 1 else ''} on this machine"

    choices = [
        Choice("ollama", "On this machine — ollama", local),
        Choice(
            "openrouter",
            "In the cloud — OpenRouter",
            "one key, hundreds of models · evidence leaves this machine",
        ),
    ]
    for provider, label in _AGENT_CLI_LABELS.items():
        status = llm.cli_status(provider)
        choices.append(
            Choice(provider, label, status.note, enabled=status.usable)
        )
    ready = bool(probe.reachable and probe.models)
    return str(
        prompt.select(
            "Where should the 6 judgment jobs run?",
            choices,
            default=0 if ready else 1,
        )
    )


#: The two agent CLIs, in the order they are offered. Labels name the
#: subscription rather than the binary, because that is what the operator is
#: choosing to spend.
_AGENT_CLI_LABELS = {
    "claude-code": "On your Claude subscription — claude",
    "codex": "On your ChatGPT subscription — codex",
}


def _ask_agent_cli(
    provider: str,
    branches: dict[str, tuple[PrivacyMode, FilingMode]],
) -> ModelChoice:
    """Confirm what this costs in behaviour, then take a model or the default.

    The two things worth saying out loud are said here rather than left to be
    discovered: this is *not* local inference however local a CLI on your own
    machine feels, and Claude Code cannot be given an output schema, so
    structured jobs are asked rather than constrained.
    """

    private = [
        n for n, (privacy, _) in branches.items() if privacy is PrivacyMode.NEVER_INGEST
    ]
    status = llm.cli_status(provider)
    lines = [
        f"{status.executable} · {status.note}",
        f"{', '.join(private) or 'Nothing'} stays on this machine. The process is "
        "local; the model is not.",
    ]
    if provider == "claude-code":
        lines.append(
            "This CLI takes no output schema, so structured jobs are asked for "
            "JSON rather than constrained to it — expect occasional retries."
        )
    for line in lines:
        print(console.indent(console.style(line, console.MUTED)))
    print()

    model = prompt.select(
        "Which model?",
        [
            Choice(
                llm.CLI_DEFAULT_MODEL,
                "Whatever the CLI is set to",
                "follows your own configuration; never goes stale",
            ),
            Choice("", "Name one", "an alias or model id this CLI accepts"),
        ],
    )
    if not model:
        model = prompt.text(f"Model id for {status.executable}", llm.CLI_DEFAULT_MODEL)
    return ModelChoice(
        provider=provider,
        model=str(model) or llm.CLI_DEFAULT_MODEL,
        # No endpoint exists to name. `provider_config` omits an empty base_url
        # rather than writing a URL nothing will ever fetch.
        base_url="",
    )


def _ask_ollama_model(probe: OllamaProbe) -> ModelChoice:
    """Choose from what is installed, or say plainly that nothing is."""

    model = ""
    if not probe.reachable:
        print(
            console.style(
                f"  ollama is not reachable at {probe.base_url} — {probe.error}",
                console.WARN,
            )
        )
        model = prompt.text(
            "Model to configure anyway (jobs stay idle until ollama runs)",
            "qwen2.5:3b",
        )
    elif not probe.models:
        print(
            console.style(
                "  ollama is running but has no models — pull one with "
                "`ollama pull qwen2.5:3b`",
                console.WARN,
            )
        )
        model = prompt.text("Model to configure anyway", "qwen2.5:3b")
    else:
        usable = sorted(probe.usable, key=lambda m: -m.billions)
        model = str(
            prompt.select(
                "Model for the 6 local jobs",
                [Choice(m.name, m.name, m.note) for m in usable],
                hint="↑↓ · Enter · only models found on this machine",
            )
        )
    return ModelChoice(
        provider="ollama",
        model=model or "qwen2.5:3b",
        base_url=probe.base_url,
    )


def _ask_openrouter(
    branches: dict[str, tuple[PrivacyMode, FilingMode]],
) -> ModelChoice:
    """Key first, then model — both verified against the provider as they are given.

    Order matters. Validating the key at the prompt that produced it is the same
    rule the rest of the wizard follows, and it is worth more here than
    anywhere: an unverified key does not fail during onboarding at all. It fails
    at the first digest, days later, as `Provider rejected the API key` from a
    command that was not about credentials.
    """

    private = [n for n, (privacy, _) in branches.items() if privacy is PrivacyMode.NEVER_INGEST]
    print(
        console.indent(
            console.style(
                f"{', '.join(private) or 'Nothing'} stays on this machine. "
                "Everything filed elsewhere is sent to OpenRouter.",
                console.MUTED,
            )
        )
    )
    print()
    key_env, key = _ask_openrouter_key()
    probe = probe_openrouter()
    return ModelChoice(
        provider="openrouter",
        model=_ask_openrouter_model(probe),
        base_url=OPENROUTER_URL,
        api_key_env=key_env,
        api_key=key,
    )


def _ask_openrouter_key() -> tuple[str, str]:
    """Return `(variable name, key to store)`; the key is empty when exported.

    The environment is consulted first and by *value*, not only under the
    canonical name. A machine that already exports `OPENROUTER_API_KEY_BK`
    should not be asked to retype the key it is holding, and pointing
    `api_key_env` at the variable that already exists is a better answer than
    storing a second copy under a different name -- one of them would go stale.
    """

    exported = _exported_openrouter_keys()
    for name, value in exported:
        check = check_openrouter_key(value)
        if not check.valid:
            continue
        print(
            console.indent(
                console.style(
                    f"{console.CHECK} {name} is set and works — {check.note}",
                    console.OK,
                )
            )
        )
        if prompt.confirm(f"Use the key in {name}?"):
            return name, ""
        break

    while True:
        typed = prompt.secret("OpenRouter API key (sk-or-…)")
        check = check_openrouter_key(typed)
        if check.valid:
            print(
                console.indent(
                    console.style(
                        f"{console.CHECK} key accepted — {check.note}", console.OK
                    )
                )
            )
            # Under the canonical name even when another variable holds a
            # different key: `api_key_env` is per vault, and a name derived from
            # whatever else the shell happens to export would be unguessable
            # later.
            return OPENROUTER_KEY_ENV, typed
        print(console.style(f"  {check.error}", console.WARN))
        if not prompt.confirm("Try another key?"):
            # Configured but unproven, and said so rather than pretended
            # otherwise. The vault is still valid; its jobs will refuse until
            # the variable holds something the provider accepts.
            return OPENROUTER_KEY_ENV, typed


def _exported_openrouter_keys() -> list[tuple[str, str]]:
    """Environment variables that plausibly already hold an OpenRouter key.

    The canonical name first, then anything else whose *value* looks like one.
    Matching on the value rather than only the name is what finds the key a
    machine keeps under a private alias.
    """

    found: list[tuple[str, str]] = []
    canonical = os.environ.get(OPENROUTER_KEY_ENV, "")
    if canonical.strip():
        found.append((OPENROUTER_KEY_ENV, canonical.strip()))
    for name, value in sorted(os.environ.items()):
        if name == OPENROUTER_KEY_ENV or not value.strip().startswith("sk-or-"):
            continue
        found.append((name, value.strip()))
    return found


def _ask_openrouter_model(probe: OpenRouterProbe) -> str:
    """A short screen for the common case, a search for the other 280.

    Listing every model would be honest and useless: the catalogue is ~400 rows,
    of which ~300 can run a job, and arrowing through them is not a choice
    anyone makes well. So the first screen is the free models plus the cheapest
    paid ones -- the two ends people actually pick between -- and the search
    covers everything else.
    """

    if not probe.reachable:
        print(
            console.style(
                f"  the OpenRouter catalogue is unreachable — {probe.error}",
                console.WARN,
            )
        )
        return prompt.text("Model id to configure anyway", "z-ai/glm-5.2")

    usable = probe.usable
    free = sorted([m for m in usable if m.free], key=lambda m: -m.context_length)
    paid = sorted(
        [m for m in usable if m.priced and not m.free],
        key=lambda m: m.prompt_price + m.completion_price,
    )[:_SHORTLIST]
    while True:
        choices: list[Choice[OpenRouterModel | None]] = [
            Choice(m, m.id, m.note) for m in (*free, *paid)
        ]
        choices.append(
            Choice(None, "Search…", f"all {len(usable)} models that can run a job")
        )
        picked = prompt.select(
            "Model for the 6 jobs",
            choices,
            hint="↑↓ · Enter · free models first, then cheapest",
        )
        if picked is not None:
            return picked.id
        found = _search_openrouter(usable)
        if found:
            return found


def _search_openrouter(models: Sequence[OpenRouterModel]) -> str:
    """Filter the catalogue by substring; empty return means "back to the list"."""

    term = prompt.text("Search model ids", "", normalize=None).strip()
    if not term:
        return ""
    # Matched case-insensitively, but offered back verbatim: the search is a
    # convenience, and lowercasing what the operator typed would silently
    # configure a model id they did not name.
    needle = term.lower()
    hits = [m for m in models if needle in m.id.lower() or needle in m.name.lower()]
    if not hits:
        print(console.style(f"  nothing in the catalogue matches {term!r}", console.WARN))
        # Offered rather than refused: the catalogue is what OpenRouter serves
        # *now*, and an operator naming a model it does not list is more likely
        # to know something this screen does not than to be typing nonsense.
        if prompt.confirm(f"Configure {term!r} as the model id anyway?", default=False):
            return term
        return ""
    shown = hits[: _SHORTLIST * 3]
    choices: list[Choice[OpenRouterModel | None]] = [
        Choice(m, m.id, m.note) for m in shown
    ]
    if len(hits) > len(shown):
        choices.append(
            Choice(None, "Narrow the search…", f"{len(hits) - len(shown)} more match")
        )
    else:
        choices.append(Choice(None, "Back", "return to the shortlist"))
    picked = prompt.select(f"{len(hits)} match {term!r}", choices)
    return picked.id if picked is not None else ""


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
    choice: ModelChoice,
    branches: dict[str, tuple[PrivacyMode, FilingMode]],
    extras: Sequence[str],
) -> dict[str, Any]:
    """Build a complete, schema-valid policy. Never a partial one.

    Every key `VaultConfig.from_dict` requires is written here, and the
    integration set is generated from `INTEGRATION_NAMES` rather than from
    whatever the operator happened to name -- which is what makes the
    "integration policy set is incomplete" rejection structurally unreachable
    instead of merely less likely.
    """

    return {
        "version": 3,
        "wiki_language": environment.language,
        "inbox_policy": {
            "privacy": _privacy_for(choice, PrivacyMode.LOCAL_ONLY).value,
            "filing": FilingMode.APPROVE_EACH.value,
        },
        "branches": {
            name: {"privacy": _privacy_for(choice, privacy).value, "filing": filing.value}
            for name, (privacy, filing) in branches.items()
        },
        # Only the chosen provider. Writing both would leave a credential-less
        # `openrouter` block that `bk doctor` has to report on and nothing uses,
        # and the second provider is one config edit away for anyone who wants
        # to route a single job elsewhere.
        "providers": {choice.provider: choice.provider_config},
        "job_models": {
            job: {"provider": choice.provider, "model": choice.model}
            for job in job_names()
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


def _privacy_for(choice: ModelChoice, privacy: PrivacyMode) -> PrivacyMode:
    """The branch's privacy, once the provider it will actually run on is known.

    The presets were written when Ollama was the only provider the wizard could
    configure, so their working branches say `local-only` — which then meant
    "the default is fine". Against a cloud provider it means something else
    entirely: `PolicyJudgmentRouter` refuses `local-only` evidence for every
    provider that is not Ollama, so the vault it produced could not run a single
    job. `bk ingest` on a freshly captured note answered *"Local-only content
    can only be routed to Ollama"* — a vault that is valid, configured, and
    inert.

    So the mode follows the choice. `never-ingest` is the one that never moves:
    it is the promise that some evidence is sent nowhere at all, and a provider
    choice is not permission to break it.
    """

    if privacy is PrivacyMode.NEVER_INGEST or choice.local:
        return privacy
    return PrivacyMode.CLOUD


def _summary(
    environment: Environment,
    branches: dict[str, tuple[PrivacyMode, FilingMode]],
    choice: ModelChoice,
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
        (
            "models",
            f"{choice.provider}/{choice.model} → all {len(job_names())} jobs",
        ),
    ]
    if not choice.local:
        # Stated, because choosing a provider quietly changed what these
        # branches permit. The row it would otherwise contradict -- "never sent
        # to an LLM" -- is directly above it.
        sent = [name for name in branches if name not in private]
        rows.append(("sent to the provider", ", ".join([*sent, "_inbox"])))
    if choice.api_key_env:
        rows.append(("api key", _credential_row(choice)))
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


def _credential_row(choice: ModelChoice) -> str:
    """Where the key comes from — never the key.

    The summary is printed to a terminal and read back in bug reports, so it
    says which variable answers and who stores it. Anyone reading a transcript
    can see the vault is configured without the transcript being a credential.
    """

    if choice.api_key:
        return f"{choice.api_key_env} · stored for this machine"
    return f"{choice.api_key_env} · read from the environment"


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
