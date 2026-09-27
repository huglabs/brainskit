"""Whether an installation can do what it advertises, checked by running it.

`installer.py` writes an install and `Health` reads one back; both then say a
layer is *installed and registered*. This module answers the different question
`bk doctor` exists for -- whether the thing installed actually refuses a write
-- by executing the gate hook on one path it must deny and one it must allow.
That is the "exercised, not believed" idea ADR 0004 keeps separate from the
registry, and it is separate here for the same reason: the installer's remit
ends when the files are on disk, and this begins by distrusting them.

Two facts in the report come from the running interpreter rather than from the
vault -- which environment `bk` was installed into, and which tree-sitter
grammars that environment can reach -- and `application` may not import
`infrastructure` (`tests/test_layering.py`). They arrive as parameters instead,
the same shape `IntegrationPort.sync` uses for its boundary: a capability the
caller holds and this layer questions, never one it reaches for. The verdict
stays here, because deciding what counts as healthy is not a rendering choice.
"""

from __future__ import annotations

import importlib.metadata
import json
import re
import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from brainskit.application.codegraph import CODE_PROJECTION
from brainskit.application.install import WRITE_GATE
from brainskit.application.ports import EnvironmentPort, VaultPort

#: The probe payload is the shape Claude Code sends a PreToolUse hook.
_GATE_PROBE_NAME = "brainskit-doctor-probe.md"

#: The distribution whose dependency metadata names the grammar pins.
_SELF_DISTRIBUTION = "brainskit"


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


#: The extra whose grammars pip installs as one unit, and the one the docs
#: recommend. `code-all` adds grammars `bk code build` also offers to install
#: one at a time, so a subset of those is a choice rather than a broken install.
_CODE_EXTRA = "code"
_ALL_EXTRA = "code-all"


def _declared_requirements() -> list[tuple[str, str, str, str | None]]:
    """This distribution's requirements as (name, bracketed extras, specifiers, extra).

    `tree-sitter-python>=0.23,<0.26; extra == "code"` is a name, any version
    specifiers, and the extra that pulls it in; `brainskit[code]; extra ==
    "code-all"` is how one extra includes another. An unresolvable
    self-distribution (an exotic install without dist-info) yields nothing, and
    every reader below degrades to "cannot judge" rather than to a fault.
    """

    try:
        requires = importlib.metadata.requires(_SELF_DISTRIBUTION) or []
    except Exception:
        return []
    parsed: list[tuple[str, str, str, str | None]] = []
    for requirement in requires:
        head, _, marker = requirement.partition(";")
        match = re.match(
            r"^([A-Za-z0-9][A-Za-z0-9._-]*)\s*(?:\[([^\]]*)\])?\s*(.*)$", head.strip()
        )
        if match is None:
            continue
        extra = re.search(r"extra\s*==\s*[\"']([^\"']+)[\"']", marker)
        parsed.append(
            (
                match.group(1).lower(),
                match.group(2) or "",
                match.group(3).strip(),
                extra.group(1) if extra else None,
            )
        )
    return parsed


def _grammar_requirements() -> dict[str, str]:
    """The version pins brainskit declares for each grammar, from its metadata.

    Read from this distribution's own dependency metadata rather than from a
    second table here: the extras in `pyproject.toml` are the one source of
    what a working grammar install means, and a hand-kept copy would drift the
    first time a pin moved. `importlib.metadata` is stdlib, so the layering
    rule (no `infrastructure` imports below `application`) is untouched.

    An unresolvable self-distribution yields an empty mapping: the update check
    degrades to absent, never to a false "outdated".
    """

    pins: dict[str, list[str]] = {}
    for name, _, specifiers, _ in _declared_requirements():
        if not name.startswith("tree-sitter") or not specifiers:
            continue
        pins.setdefault(name, []).append(specifiers)
    return {name: ",".join(parts) for name, parts in sorted(pins.items())}


def _grammar_extras() -> dict[str, frozenset[str]]:
    """Each extra's grammar distributions, with included extras folded in.

    `code-all` declares `brainskit[code]` rather than repeating its grammars,
    so the closure is taken here; otherwise "is `code-all` complete" would be
    answered over the sixteen grammars it names and not the twenty-nine it
    installs.
    """

    direct: dict[str, set[str]] = {}
    includes: dict[str, set[str]] = {}
    for name, bracketed, _, extra in _declared_requirements():
        if extra is None:
            continue
        if name == _SELF_DISTRIBUTION:
            includes.setdefault(extra, set()).update(
                part.strip() for part in bracketed.split(",") if part.strip()
            )
        elif name.startswith("tree-sitter-"):
            direct.setdefault(extra, set()).add(name)

    def closure(extra: str, seen: frozenset[str] = frozenset()) -> set[str]:
        members = set(direct.get(extra, set()))
        for included in includes.get(extra, set()) - seen - {extra}:
            members |= closure(included, seen | {extra})
        return members

    extras = set(direct) | set(includes)
    return {
        extra: frozenset(members)
        for extra in sorted(extras)
        if (members := closure(extra))
    }


def grammar_install_state(grammars: Mapping[str, bool]) -> dict[str, Any]:
    """Whether the grammars present are a choice or a broken install.

    `code` is an optional extra, so no grammar at all is `absent`: an operator
    who never asked for the code graph has nothing wrong with their machine,
    and folding that into `healthy` made the field a constant on every default
    install (#13). The `code-all` grammars beyond `code` are individually
    optional too -- `bk code build` itself offers to install just the ones a
    scan needs -- so `[code]` complete with none of them is `complete` (#24).

    What *is* a fault is `partial`: some of the `code` extra's grammars present
    and some not. pip installs that extra as one unit, so a subset is an
    interrupted or hand-assembled install, and `bk code build` over it succeeds
    while a language contributes nothing. `broken` names exactly the grammars
    whose absence is that fault, and nothing that is merely optional.
    """

    known = {str(name).lower() for name in grammars}
    installed = {str(name).lower() for name, present in grammars.items() if present}
    extras = _grammar_extras()
    unit = extras.get(_CODE_EXTRA, frozenset()) & known
    if not installed:
        state, broken = "absent", []
    else:
        broken = sorted(unit - installed)
        state = "partial" if broken else "complete"
    complete = [
        extra
        for extra, members in extras.items()
        if (members & known) and (members & known) <= installed
    ]
    return {"state": state, "broken": broken, "extras_complete": complete}


def _version_key(version: str) -> tuple[int, ...]:
    """A grammar version as a tuple of ints (`0.24.4` -> `(0, 24, 4)`).

    Grammar wheels number releases with plain dotted integers, so this covers
    every real case. A part that is not numeric — pre-release suffixes like
    `1.0a1` — stops the tuple there rather than raising; the comparison then
    answers on the prefix it could read, which for an advisory check beats
    reporting every such install as broken.
    """

    parts: list[int] = []
    for chunk in version.split("+")[0].split("."):
        if chunk.isdigit():
            parts.append(int(chunk))
        else:
            digits = "".join(ch for ch in chunk if ch.isdigit())
            if digits:
                parts.append(int(digits))
            break
    return tuple(parts)


def _satisfies(version: str, specifiers: str) -> bool:
    """Whether `version` meets a comma-separated specifier set.

    A clause that cannot be parsed (an operator's own constraint shape, a
    wildcard) is skipped rather than failed: flagging an install nobody proved
    broken is the false alarm that teaches operators to ignore the check.
    """

    got = _version_key(version)
    for clause in specifiers.split(","):
        clause = clause.strip()
        matched = re.match(r"^(>=|<=|==|!=|~=|>|<)\s*v?(\d+(?:\.\d+)*)$", clause)
        if matched is None:
            continue
        op, raw = matched.groups()
        want = _version_key(raw)
        # Pad to one width so `0.23` compares equal to `0.23.0`.
        width = max(len(got), len(want))
        g = got + (0,) * (width - len(got))
        w = want + (0,) * (width - len(want))
        # `~=X.Y` (compatible release) is `>= X.Y, == X.*`; the prefix is cut
        # from the specifier as written, not from its padded form.
        release_prefix = max(len(want) - 1, 1)
        if op == "~=":
            if not (g >= w and g[:release_prefix] == want[:release_prefix]):
                return False
            continue
        outcome = {
            ">=": g >= w,
            "<=": g <= w,
            "==": g == w,
            "!=": g != w,
            ">": g > w,
            "<": g < w,
        }.get(op)
        if outcome is False:
            return False
    return True


def grammar_update_check(
    versions: Mapping[str, Any] | None,
    *,
    environment: EnvironmentPort,
) -> dict[str, Any]:
    """Whether installed grammars are missing *or* older than brainskit's pins.

    The health half (which distributions are absent) has always lived with the
    caller; this adds the update half (which present ones violate the pinned
    range). A mismatched grammar usually still imports — it fails per file at
    extraction time, naming a parse error nothing traces back to the wheel —
    so it belongs in the same report as a missing one, not in a crash log.

    Returns `grammars_outdated` entries plus, when anything needs changing,
    one command covering both the missing and the outdated sets.
    """

    pins = _grammar_requirements()
    if not versions or not pins:
        return {}
    outdated: list[dict[str, Any]] = []
    needing_install: list[str] = []
    for distribution, info in sorted(versions.items()):
        entry = info if isinstance(info, dict) else {}
        installed = bool(entry.get("installed"))
        version = entry.get("version")
        specifiers = pins.get(str(distribution).lower())
        if not installed:
            needing_install.append(str(distribution))
            continue
        if specifiers is None or not isinstance(version, str):
            continue
        if _satisfies(version, specifiers) is False:
            outdated.append(
                {
                    "distribution": str(distribution),
                    "installed": version,
                    "required": specifiers,
                }
            )
    report: dict[str, Any] = {}
    if outdated:
        report["grammars_outdated"] = outdated
    all_needing = sorted(set(needing_install) | {e["distribution"] for e in outdated})
    if all_needing and environment.installable:
        report["upgrade"] = environment.install_hint(all_needing)
    return report


def probe_write_gate(vault: VaultPort, layers: list[dict[str, Any]]) -> dict[str, Any]:
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

    The layer taken is the first one naming a script, not simply the first one:
    with two agents installed the report carries a `write_gate` row per agent,
    and only the agent brainskit ships hooks for has a file behind it. Stopping
    at the first would report `absent` for an installation whose gate is live.
    """

    entry = next(
        (
            layer
            for layer in layers
            if layer["layer"] == WRITE_GATE and layer.get("script")
        ),
        None,
    )
    script = Path(entry["script"]) if entry and entry.get("script") else None
    if script is None or not script.is_file():
        return {
            "state": "absent",
            "detail": "no write-gate hook is installed; nothing to exercise",
        }

    vault_root = vault.root
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


def doctor_report(
    vault: VaultPort,
    enforcement: dict[str, Any],
    *,
    environment: EnvironmentPort,
    grammars: Mapping[str, bool],
    grammar_versions: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
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

    `environment` and `grammars` are supplied rather than read: both describe
    the interpreter `bk` is running in, which is `infrastructure`'s to know and
    a layer that must not import it cannot ask. What this decides is the
    verdict, and that is not a fact about the interpreter.
    """

    root, reason = vault.code_root_reason()
    missing = [name for name, installed in grammars.items() if not installed]
    verdict = grammar_install_state(grammars)
    broken = verdict["broken"]
    probe = probe_write_gate(vault, enforcement["layers"])
    enforcement["write_gate_probe"] = probe
    # The update half of the grammar check: a distribution may be present and
    # still violate the pin brainskit declares, which fails later, per file,
    # at extraction time. Same report as a broken install, because it is the
    # same "this language will let you down" fact. Optional grammars that are
    # simply absent are left out, or a default install would carry an upgrade
    # command for twenty-nine packages it never asked for.
    updates = grammar_update_check(
        {
            name: info
            for name, info in (grammar_versions or {}).items()
            if grammars.get(name) or str(name).lower() in broken
        },
        environment=environment,
    )
    outdated = [
        str(entry.get("distribution", ""))
        for entry in (updates.get("grammars_outdated") or [])
    ]
    code: dict[str, Any] = {
        "root": str(root),
        "why_this_root": reason,
        "scan_limit": vault.config().code_scan_limit,
        "grammars_installed": sum(grammars.values()),
        "grammars_known": len(grammars),
        "grammars_missing": missing,
        "grammars_state": verdict["state"],
        "grammars_broken": broken,
        "extras_complete": verdict["extras_complete"],
        "grammars_outdated": outdated,
        **updates,
    }
    if missing and environment.installable:
        # The smallest command that moves this install forward: the grammars a
        # partial `code` lacks, else the extra itself, else the optional rest.
        if broken:
            packages = broken
        elif verdict["state"] == "absent":
            packages = [f"{_SELF_DISTRIBUTION}[{_CODE_EXTRA}]"]
        else:
            packages = [f"{_SELF_DISTRIBUTION}[{_ALL_EXTRA}]"]
        code["install"] = environment.install_hint(packages)
    graph_note = _code_graph_without_grammars(vault, verdict["state"])
    if graph_note is not None:
        code["graph_without_grammars"] = graph_note
    # The probe ran the script directly, which proves the script refuses a
    # write; `gated` is what says the agent will actually run it -- registered
    # under its event, and the script this version installs rather than an
    # older copy that may enforce older rules. Both, or nobody has shown that a
    # write to wiki/ is refused.
    gate_live = probe["state"] == "absent" or bool(enforcement.get("gated"))
    return {
        "vault": str(vault.root),
        "environment": {
            "kind": environment.kind,
            "label": environment.label,
            "executable": environment.executable,
            "installable": environment.installable,
        },
        "code": code,
        "enforcement": enforcement,
        # An allowlist, not a denylist: only two states are compatible with a
        # healthy installation -- the gate refused what it must ("enforcing"),
        # or there is no gate to judge ("absent", which an operator may have
        # chosen). Everything else, including a hook that could not be run at
        # all, means nobody has confirmed that a write to wiki/ is refused, and
        # an installed gate that does not guard is worse than none: every other
        # layer keeps reporting success while writes go around it.
        #
        # Grammars count only when they are broken, never when they are absent:
        # the extra is optional, and a field that is False on every default
        # install is a constant a CI gate cannot use -- it hid the gate fault it
        # was meant to report (#13). A partial `code` extra and an out-of-range
        # grammar do count, because both are ways a build that reports success
        # silently loses a language. A code graph this machine cannot rebuild
        # is reported beside them but is not one: `bk code build` refuses loudly
        # with the install command, and the stored graph still answers.
        "healthy": (
            not broken
            and not outdated
            and probe["state"] in {"enforcing", "absent"}
            and gate_live
        ),
    }


def _code_graph_without_grammars(vault: VaultPort, state: str) -> dict[str, Any] | None:
    """A code graph this vault uses, on a machine with no grammar to rebuild it.

    Built (`graph/code.json` exists) or configured (`code_root` is set) says the
    operator chose the code graph, so here the absent extra stops being a
    choice made on their behalf. Reported, not counted against `healthy`: see
    the verdict in `doctor_report`.
    """

    if state != "absent":
        return None
    built = (vault.root / CODE_PROJECTION).is_file()
    configured = vault.config().code_root is not None
    if not (built or configured):
        return None
    return {
        "built": built,
        "configured": configured,
        "detail": (
            "this vault uses a code graph, but no tree-sitter grammar is "
            "installed, so `bk code build` cannot refresh it here"
        ),
    }
