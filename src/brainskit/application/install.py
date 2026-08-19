"""What an install is made of, for the writer that creates one and the readers
that check it.

`bk hooks install` writes an install; `bk status`, `bk doctor` and `/api/status`
read one back and report whether it is still guarding anything. Those are two
sides of the same table -- which instruction file an agent reads, which state
file records where its configuration went, which hook scripts this brand
installs, and what each enforcement layer is called -- and the table is here so
that neither side can restate it.

The alternative is the shape this module replaces. The installer knew all four
agents; the reader knew only `claude`, in six separate hardcoded strings, so an
install for any other agent was reported against a workspace that was never
recorded, an instruction file that was never written, and two hook scripts that
brainskit does not install for it. Per `ConstantsHaveOneOwnerTest`, this
repository has already shipped that divergence twice; the layer descriptions
were the third instance, and the sentinels that test guards are exactly the
copies it did catch.

Owned by `application` rather than by `interfaces/cli.py`, which writes the
install, for the reason `gate.py` states for the managed-block sentinels: the
layering rule is application must not import interfaces, and the response to it
must not be a second copy. It is a module of its own rather than more of
`gate.py` because `gate.py` answers one question -- may this write land -- on
the path of every Write the agent attempts, and nothing here participates in
that decision.

Stdlib only, like the gate: the write gate imports `adapter_path` from here.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

#: The name this tool installs its artefacts under.
#
# Every artefact spells it identically -- `<brand>-gate.sh`, `# <brand>:
# generated`, `<!-- <brand>:start -->`, `.claude/skills/<brand>/` -- which is
# what lets `interfaces/cli.py`'s one list of former names cover all four
# classes at once, and what makes a script name below derived rather than
# written down. A module that spells `brainskit-gate.sh` has copied it.
BRAND = "brainskit"

#: The enforcement layers, named. Both the installer's summary and `bk status`
#: report under these names, and `enforcement_ok` and the web viewer read them.
WRITE_GATE = "write_gate"
SESSION_STATUS = "session_status"
COMMIT_LINT = "commit_lint"
INSTRUCTIONS = "instructions"

#: `commit_lint` is the same mechanism whatever agent is installed: git runs it,
#: not the agent.
COMMIT_LINT_MECHANISM = ".git/hooks/pre-commit running bk lint --changed"

#: The agent this tool installs for when nothing says otherwise -- the documented
#: quickstart, and what a vault with no adapter at all is reported as, because a
#: reader has to say where the layers *would* land rather than invent an agent.
DEFAULT_AGENT = "claude"


def adapter_path(agent: str) -> str:
    """Where `agent`'s adapter lives, relative to the vault root.

    The one file that records which workspace an install went to, and the file
    the write gate reads its deny rules from. Takes any name rather than only a
    known agent: `load_gate_policy` is handed whatever the hook payload carried
    and validates it itself, and a gate that refused to look for an unknown
    agent's policy would fail open on exactly the input it should be strictest
    about.
    """

    return f".brain/agent-{agent}.json"


@dataclass(frozen=True, slots=True)
class AgentHook:
    """One hook script this brand installs, and the layer it provides.

    `template` is the basename without its extension because that is the unit
    everything else keys on: the shipped resource, the file on disk, the name a
    former brand spelled differently, and the key in the installer's report.
    """

    layer: str
    template: str
    event: str
    matcher: str | None
    timeout: int
    mechanism: str

    @property
    def script(self) -> str:
        """The filename on disk, which is the template plus `.sh`."""

        return f"{self.template}.sh"


@dataclass(frozen=True, slots=True)
class AgentInstall:
    """Everything `bk hooks install --agent <name>` writes, as data.

    `hooks` is empty for every agent but `claude`, and that is a fact about this
    tool rather than about the agent: brainskit ships PreToolUse and SessionStart
    scripts for Claude Code and nothing equivalent for the others. The reader
    must therefore not report a write gate for them -- naming a layer that was
    never offered as "not installed" reads as a guard that fell off, which is a
    different and more alarming claim than the true one.
    """

    agent: str
    instructions: str
    hooks: tuple[AgentHook, ...] = ()
    skill: bool = False

    @property
    def adapter(self) -> str:
        return adapter_path(self.agent)

    @property
    def instructions_mechanism(self) -> str:
        return f"{self.instructions} managed block"

    def hook(self, layer: str) -> AgentHook | None:
        """The hook providing `layer`, or None when this agent has none."""

        return next((hook for hook in self.hooks if hook.layer == layer), None)


CLAUDE_HOOKS: tuple[AgentHook, ...] = (
    AgentHook(
        WRITE_GATE,
        f"{BRAND}-gate",
        "PreToolUse",
        "Write|Edit|MultiEdit",
        10,
        "Claude Code PreToolUse hook on Write|Edit|MultiEdit",
    ),
    # SessionStart carries no matcher so every session source is covered; the
    # settings schema treats an absent matcher as "all", which is how the other
    # session-scoped hooks in a real settings file are written.
    AgentHook(
        SESSION_STATUS,
        f"{BRAND}-status",
        "SessionStart",
        None,
        15,
        "Claude Code SessionStart hook reporting vault state",
    ),
)

#: Insertion order is report order: every surface that lists agents lists them
#: like this, so a two-agent install reads the same way twice running.
AGENTS: Mapping[str, AgentInstall] = {
    "claude": AgentInstall("claude", "CLAUDE.md", CLAUDE_HOOKS, skill=True),
    "codex": AgentInstall("codex", "AGENTS.md"),
    "gemini": AgentInstall("gemini", "GEMINI.md"),
    "opencode": AgentInstall("opencode", "AGENTS.md"),
}


def agent_install(agent: str) -> AgentInstall:
    """The install shape for `agent`, or the default one for an unknown name.

    Never raises. The argparse choices already reject an unknown agent on the
    way in, and the one caller that cannot rely on that is a reader describing a
    vault someone else installed -- for which a wrong description is better than
    a traceback out of `bk status`.
    """

    return AGENTS.get(agent, AGENTS[DEFAULT_AGENT])


def installed_agents(vault_root: Path) -> tuple[str, ...]:
    """Which agents this vault has an adapter for, in registry order.

    The adapter is the only thing on disk that records an install, so this is
    the whole answer to "who is this vault installed for". Empty means nobody
    has run `bk hooks install` here yet, which is a state a reader has to report
    rather than resolve -- see `Health._enforcement_state` for what it reports
    instead.

    Only known agents are looked for. An `agent-<something>.json` this build has
    never heard of cannot be described -- there is no instruction file and no
    hook set to check -- so glob-and-parse would turn an unreadable name into an
    unreadable report.
    """

    return tuple(
        agent for agent in AGENTS if (vault_root / adapter_path(agent)).is_file()
    )
