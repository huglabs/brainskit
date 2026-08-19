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

import json
import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from brainskit.application.install import WRITE_GATE
from brainskit.application.ports import EnvironmentPort, VaultPort

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
    probe = probe_write_gate(vault, enforcement["layers"])
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
