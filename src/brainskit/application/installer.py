"""Writing an install: every file `bk hooks install` puts on disk.

`install.py` says what an install *consists of* -- the per-agent table both the
writer and the readers iterate, per ADR 0004. This is the writer: it resolves
the workspace, renders the adapter, the managed instruction block, the skill,
the git pre-commit hook and the Claude Code hook scripts, registers those hooks
in `.claude/settings.json` without clobbering it, retires the artefacts a former
brand left, and reports every enforcement layer it did or did not manage to
create.

None of that is a CLI concern. It was in `interfaces/cli.py` only because the
CLI was its one entry point, which is also why it could be tested only through
an end-to-end install -- and it is the half of the writer/reader pair whose
reader (`Health.enforcement`) has always lived here. What stays on the other
side of the boundary is everything that talks to a person: the stderr banners
naming an inactive layer, a pruned hook or a former brand's debris. This
function decides; `interfaces/cli.py` says it out loud.

A module of its own rather than more of `install.py` for a mechanical reason as
well as a conceptual one. `gate.py` imports `adapter_path` from `install.py` on
the path of every Write an agent attempts, and this writer needs `gate.py`'s
managed-block sentinels and deny prefixes -- so putting it in `install.py` would
close an import cycle `tests/test_layering.py` forbids. The registry stays
stdlib-only and gate-free; the writer is free to depend on both.
"""

from __future__ import annotations

import json
import re
import shlex
from collections.abc import Sequence
from importlib.resources import files
from pathlib import Path
from typing import Any

from brainskit.application.gate import (
    DEFAULT_DENY_PREFIXES,
    HOOK_SENTINEL,
    INSTRUCTION_END,
    INSTRUCTION_START,
)
from brainskit.application.health import redirected_git_hooks_path
from brainskit.application.install import (
    BRAND,
    COMMIT_LINT,
    COMMIT_LINT_MECHANISM,
    DEFAULT_AGENT,
    INSTRUCTIONS,
    AgentHook,
    agent_install,
)
from brainskit.application.ports import VaultPort
from brainskit.domain.model import NotConfiguredError, ValidationError

#: Names this tool shipped under before. A rename has to *migrate* an install,
#: not duplicate it.
#
# Every lookup in this module keys on the current name, so without this list a
# pre-rename install survives the upgrade intact: its gate stays registered
# beside the new one and keeps firing on every write against whatever vault it
# was baked with, its scripts stay on disk beside the current ones, and the
# instruction file ends up carrying two managed blocks that disagree about which
# vault this workspace has. That is the state every pre-rename install lands in,
# and this repository was the reference installation demonstrating it on itself.
#
# Hard-coded and finite deliberately. Deriving the set would mean guessing which
# `*-gate.sh` in an operator's settings.json was once ours, and a wrong guess
# deletes somebody else's hook -- the one failure worse than leaving debris.
#
# RENAMING AGAIN IS ONE ENTRY HERE. Append the name being retired and every
# artefact class migrates, because they all spell the brand the same way.
LEGACY_BRANDS: tuple[str, ...] = ("brainkit",)


def _legacy_spellings(current: str) -> tuple[str, ...]:
    """`current` as each former brand spelled it, for artefacts to migrate."""

    return tuple(
        current.replace(BRAND, legacy) for legacy in LEGACY_BRANDS if BRAND in current
    )


def _managed_block_re(start: str, end: str) -> re.Pattern[str]:
    return re.compile(rf"{re.escape(start)}.*?{re.escape(end)}\n?", re.DOTALL)


_MANAGED_BLOCK_RE = _managed_block_re(INSTRUCTION_START, INSTRUCTION_END)

#: Each former brand's managed block: the start marker to look for, and the
#: pattern that spans it. Built from the current markers so a brand added above
#: needs nothing here.
_LEGACY_MANAGED_BLOCKS: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (
        INSTRUCTION_START.replace(BRAND, legacy),
        _managed_block_re(
            INSTRUCTION_START.replace(BRAND, legacy),
            INSTRUCTION_END.replace(BRAND, legacy),
        ),
    )
    for legacy in LEGACY_BRANDS
)

#: Stands in for the new block while former blocks are being removed around it.
#
# Inserting the real block first and deleting afterwards would be correct only
# while no former brand name is a substring of the current one; a future rename
# where it is would have the deletion pass eat the block just written. A token
# carrying NUL cannot occur in a file this module writes, so it is safe to plant
# and is always swapped back out below.
_BLOCK_ANCHOR = "\x00brainskit:managed-block\x00"


#: The hooks this install writes, read from the registry both sides iterate.
#: Named here because every function below installs, registers or retires one.
CLAUDE_HOOKS: tuple[AgentHook, ...] = agent_install(DEFAULT_AGENT).hooks

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
    """Install the Claude Code skill that teaches the vault contract.

    A former brand's skill directory is reported alongside — an already-current
    install is exactly where that debris hides, so the report is attached to
    every outcome rather than only to the one that writes.
    """
    skill = root / ".claude" / "skills" / BRAND / "SKILL.md"
    content = _agent_template("claude-skill", vault)
    legacy = _legacy_skill_dirs(root)
    if skill.is_file() and not force:
        if skill.read_text(encoding="utf-8") == content:
            return {"path": str(skill), "state": "current", **_legacy(legacy)}
        raise ValidationError(
            "A brainskit skill already exists; re-run with --force to replace it",
            details={"path": str(skill)},
        )
    updated = skill.is_file()
    skill.parent.mkdir(parents=True, exist_ok=True)
    skill.write_text(content, encoding="utf-8")
    return {
        "path": str(skill),
        "state": "updated" if updated else "created",
        **_legacy(legacy),
    }


def _legacy(found: list[str]) -> dict[str, Any]:
    """`legacy` only when there is something to report, so callers can `in` it."""

    return {"legacy": found} if found else {}


def _install_instructions(root: Path, vault: Path, agent: str) -> dict[str, Any]:
    """Append the graph contract, replacing any block a previous run wrote.

    The block is fenced by HTML comments so re-running never duplicates it and
    never disturbs instructions the operator wrote around it.

    A block left by a former brand is retired here rather than appended beside.
    Two managed blocks is a worse state than one stale block: they contradict
    each other about which vault this workspace has, both are addressed to the
    same agent, and nothing downstream can tell which one the operator meant.
    The first one retired inherits the new block's position, so the contract
    stays where it was last read instead of moving to the end of the file.
    """
    target = root / agent_install(agent).instructions
    block = (
        f"{INSTRUCTION_START}\n"
        f"{_agent_template('instructions', vault).strip()}\n"
        f"{INSTRUCTION_END}\n"
    )
    existing = target.read_text(encoding="utf-8") if target.is_file() else ""
    content = existing
    migrated: list[str] = []
    # Only when there is no current block: an install that already has one keeps
    # it where it is, and the former block is then simply debris to drop.
    adopt = INSTRUCTION_START not in content
    for start, pattern in _LEGACY_MANAGED_BLOCKS:
        if start not in content:
            continue
        migrated.append(start)
        if adopt:
            content = pattern.sub(lambda _: _BLOCK_ANCHOR, content, count=1)
            adopt = False
        content = pattern.sub("", content)

    if _BLOCK_ANCHOR in content:
        content = content.replace(_BLOCK_ANCHOR, block, 1)
        state = "migrated"
    elif INSTRUCTION_START in content:
        # Replace in place so instructions written after the block keep their
        # position; a lambda avoids re.sub interpreting escapes in the block.
        content = _MANAGED_BLOCK_RE.sub(lambda _: block, content, count=1)
        state = "current" if existing == content else "updated"
    else:
        stripped = content.strip()
        content = f"{stripped}\n\n{block}" if stripped else block
        state = "appended" if stripped else "created"
    if content != existing:
        target.write_text(content, encoding="utf-8")
    result: dict[str, Any] = {"path": str(target), "state": state}
    if migrated:
        result["migrated"] = migrated
    return result


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
    root: Path, vault: Path, hook: AgentHook, *, force: bool
) -> dict[str, Any]:
    """Write one hook script, refusing to clobber a file brainskit did not write.

    The sentinel comment is what makes a rewrite safe: a script carrying it is
    ours to replace, and a script without it belongs to the operator.
    """
    target = root / ".claude" / "hooks" / hook.script
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


def _hook_script_names(template: str) -> frozenset[str]:
    """Every filename this hook has ever been installed as, current and former.

    A former brand's filename belongs here because it identifies the same hook
    on the same event: keying only on the current name is what let a pre-rename
    gate stay registered beside the new one through a `--force` reinstall.
    """

    return frozenset(
        f"{name}.sh" for name in (template, *_legacy_spellings(template))
    )


def _is_stale_hook_command(command: Any, template: str, current: str) -> bool:
    """Whether `command` is a brainskit `template` hook at some path other than `current`.

    Every install writes to `<root>/.claude/hooks/<template>.sh`, so the command
    this workspace should be running for a given template never has more than
    one live value at a time. A registered command matching that name — under
    this brand or one it used to have — but not equal to the one being installed
    now is not "unrelated tooling on the same event", the case this file
    otherwise takes care never to touch. It is a previous install this one
    supersedes: a settings.json carried over from a different `--root`, copied
    wholesale from another project's `.claude/`, or simply older than a rename.
    Left alone, it keeps firing every session against a vault or workspace this
    one no longer is.
    """
    if not isinstance(command, str) or command == current:
        return False
    return Path(command).name in _hook_script_names(template)


def _retire_legacy_hook_scripts(
    root: Path, installed: Sequence[AgentHook]
) -> list[dict[str, Any]]:
    """Remove the hook scripts a former brand left, when they are still ours.

    Unregistering alone leaves `<former>-gate.sh` sitting beside the current one:
    inert, but indistinguishable at a glance from the hook that actually runs,
    which is how a workspace gets debugged against the wrong file.

    Ownership is decided exactly the way `_write_hook_script` decides it — by the
    generated sentinel, spelled as that brand spelled it. A script still carrying
    it is untouched output of an earlier install and safe to delete. One without
    it has been edited, and deleting an edited file is a different act from
    dropping a settings entry, so it is reported and left where it is.

    `--force` is not consulted. Force decides whether to clobber an install that
    is currently the right one; finishing a rename this tool itself performed is
    not that question, and gating it would leave the default upgrade path — the
    documented `bk hooks install` with no flags — silently running two gates.

    Only hooks this run actually installed are retired. A hook whose current
    script was skipped keeps its old registration, and deleting the file that
    registration points at would turn debris into a broken command.
    """

    retired: list[dict[str, Any]] = []
    hooks_dir = root / ".claude" / "hooks"
    for legacy in LEGACY_BRANDS:
        sentinel = HOOK_SENTINEL.replace(BRAND, legacy)
        for hook in installed:
            path = hooks_dir / f"{hook.template.replace(BRAND, legacy)}.sh"
            if not path.is_file():
                continue
            try:
                owned = sentinel in path.read_text(encoding="utf-8")
                if owned:
                    path.unlink()
            except OSError as exc:
                retired.append(
                    {"path": str(path), "state": "kept", "reason": str(exc)}
                )
                continue
            retired.append(
                {"path": str(path), "state": "removed"}
                if owned
                else {
                    "path": str(path),
                    "state": "kept",
                    "reason": (
                        "edited since it was generated, so it is yours; "
                        "it is no longer registered and can be deleted"
                    ),
                }
            )
    return retired


def _legacy_skill_dirs(root: Path) -> list[str]:
    """Skill directories a former brand installed and an agent still loads.

    Reported, never removed. A hook script proves its provenance with the
    generated sentinel; a skill is plain markdown carrying no such mark, so
    nothing here distinguishes an earlier install's output from something the
    operator wrote. Deleting on a guess is worse than the debris — and unlike a
    stale hook, a stale skill is only read, never executed.
    """

    skills = root / ".claude" / "skills"
    return [str(skills / legacy) for legacy in LEGACY_BRANDS if (skills / legacy).is_dir()]


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
    root: Path, entries: Sequence[tuple[AgentHook, str]]
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
    registrable: list[tuple[AgentHook, str]] = []
    for hook in CLAUDE_HOOKS:
        outcome = _write_hook_script(root, vault, hook, force=force)
        scripts[hook.template] = outcome
        if outcome["state"] != "skipped":
            registrable.append((hook, str(outcome["path"])))
    # Registration first, so a former brand's command is gone from settings.json
    # before its script leaves the disk. The reverse order has a window — and, if
    # the unlink fails, a lasting state — where a registered hook points at a
    # file that is not there.
    settings = _register_claude_hooks(root, registrable)
    retired = _retire_legacy_hook_scripts(root, [hook for hook, _ in registrable])
    result: dict[str, Any] = {"scripts": scripts, "settings": settings}
    if retired:
        result["legacy"] = retired
    return result


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

    The layers come from `application.install`, which is also what `bk status`
    reads back. Restating them here is how the two came to disagree: this side
    knew all four agents and the reader knew one.

    A report, not a rendering: it decides which layers exist and which are on,
    and says nothing about how any of that reaches a person. `interfaces/cli.py`
    turns the `inactive` half of it into the stderr banner.
    """
    install = agent_install(result["agent"])
    layers: list[dict[str, Any]] = []
    hook = result.get("claude_hook")
    if isinstance(hook, dict):
        settings = hook["settings"]
        registered = {
            str(item["command"]) for item in settings.get("registered", [])
        }
        for entry in install.hooks:
            script = hook["scripts"][entry.template]
            active = script["state"] != "skipped" and script["path"] in registered
            outcome = script if script["state"] == "skipped" else settings
            layers.append(
                _enforcement_layer(
                    entry.layer, entry.mechanism, outcome, active=active
                )
            )
    pre_commit = result["pre_commit"]
    layers.append(
        _enforcement_layer(
            COMMIT_LINT,
            COMMIT_LINT_MECHANISM,
            pre_commit,
            active=pre_commit["state"] != "skipped",
        )
    )
    layers.append(
        {
            "layer": INSTRUCTIONS,
            "mechanism": install.instructions_mechanism,
            "active": True,
            "advisory": True,
        }
    )
    return {
        "layers": layers,
        "inactive": [layer["layer"] for layer in layers if not layer["active"]],
    }


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


def install_agent(
    vault: VaultPort,
    agent: str,
    *,
    root: str | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """Write every artefact `bk hooks install --agent <name>` puts on disk.

    Everything a run of the installer *decides* and *writes*, and nothing it
    says. The two steps that used to sit at the end of this function are on the
    other side of the boundary now: the first `bk code build` (which needs the
    running interpreter to turn a missing extra into an install command) and the
    four stderr banners naming an inactive layer, a pruned hook, a former
    brand's debris and an unbuilt code graph. A caller that wants those runs
    them against this result -- see `interfaces/cli.py`'s `_install_hooks`.
    """
    vault_root = vault.root
    workspace = _resolve_workspace(vault_root, root)
    # Decided before anything is written: the installer creates `.claude/` in
    # the workspace, and reading that back afterwards would be this check
    # observing its own side effect and concluding all is well.
    advisory = _workspace_advisory(vault_root, workspace)
    install = agent_install(agent)
    vault.write_generated(
        install.adapter, json.dumps(_agent_policy(agent, workspace), indent=2) + "\n"
    )
    result: dict[str, Any] = {
        "agent": agent,
        "adapter": install.adapter,
        "workspace": str(workspace),
        INSTRUCTIONS: _install_instructions(workspace, vault_root, agent),
        "pre_commit": _install_pre_commit(workspace, vault_root, force=force),
    }
    # What an agent gets is registry data, not a test on its name: `bk status`
    # reads the same two fields back to decide which layers to report.
    if install.skill:
        result["skill"] = _install_skill(workspace, vault_root, force=force)
    if install.hooks:
        result["claude_hook"] = _install_claude_hook(workspace, vault_root, force=force)
    if advisory is not None:
        result["workspace_advisory"] = advisory
    result["enforcement"] = _enforcement_summary(result)
    return result
