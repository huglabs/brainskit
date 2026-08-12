"""Write gate: decide whether a direct file write inside a vault is allowed.

The gate is consulted before an agent's Write/Edit reaches the filesystem, so it
must be cheap, free of side effects and — above all — must never raise. A gate
that fails closed on its own breakage blocks every write in a session, which is
strictly worse than having no gate at all. Every failure mode below therefore
degrades to :data:`DEFAULT_DENY_PREFIXES` instead of propagating.

The gate governs the vault, not the filesystem: a target that lands outside the
vault is allowed. Inside the vault, matching is done on path segments so that
``wiki/`` covers ``wiki/a.md`` but never ``wikipedia/a.md``.

This module depends on nothing but the standard library, which keeps the
application layer free of infrastructure and lets the hook path stay fast.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

DEFAULT_DENY_PREFIXES: tuple[str, ...] = ("wiki/", "raw/")

# Built-in remediation for the built-in prefixes. A policy file may override
# either entry; these fill the gap when it defines prefixes but no guidance.
DEFAULT_REMEDIATION: Mapping[str, str] = {
    "wiki/": "Wiki pages are written only by the apply gate. Use: bk apply",
    "raw/": "Sources are immutable and hash-identified. Use: bk capture",
}

# The agent name becomes part of a filename. Excluding separators is what keeps
# `--agent ../../etc/shadow` from pointing the loader at an arbitrary file.
_AGENT_TOKEN = re.compile(r"[A-Za-z0-9._-]+")


@dataclass(frozen=True, slots=True)
class GateDecision:
    """The gate's verdict for one write target."""

    allowed: bool
    path: str
    reason: str | None = None
    remediation: str | None = None
    rule: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "path": self.path,
            "reason": self.reason,
            "remediation": self.remediation,
            "rule": self.rule,
        }


@dataclass(frozen=True, slots=True)
class GatePolicy:
    """Deny rules the gate enforces, loaded from the agent adapter or built in.

    ``source`` is the adapter file the rules were read from, and ``degraded``
    records why the built-in defaults were used instead. Both exist so a
    surprised operator can tell "the policy said so" from "the policy could not
    be read"; neither participates in a decision.
    """

    agent: str
    deny_prefixes: tuple[str, ...] = DEFAULT_DENY_PREFIXES
    remediation: Mapping[str, str] = field(default_factory=dict)
    source: Path | None = None
    degraded: str | None = None

    def match(self, relative_path: str) -> str | None:
        """Return the deny prefix covering ``relative_path``, if any.

        ``relative_path`` is vault-relative and POSIX-separated. Matching is on
        whole segments, so ``wiki/`` covers ``wiki`` and ``wiki/a/b.md`` but not
        ``wikipedia/a.md``.
        """

        segments = _fold_segments(relative_path)
        for prefix in self.deny_prefixes:
            needle = _fold_segments(prefix)
            if needle and segments[: len(needle)] == needle:
                return prefix
        return None

    def remediation_for(self, rule: str) -> str | None:
        """Return the guidance for ``rule``, tolerating a missing trailing slash."""

        bare = rule.rstrip("/")
        for table in (self.remediation, DEFAULT_REMEDIATION):
            for key in (rule, bare, f"{bare}/"):
                text = table.get(key)
                if text:
                    return text
        return None


def load_gate_policy(vault_root: Path, agent: str = "claude") -> GatePolicy:
    """Read ``.brain/agent-<agent>.json`` and return the gate rules it declares.

    Never raises. A missing file, unreadable bytes, invalid JSON, a non-object
    document, an adapter written before the gate existed (no ``gate`` key) or a
    ``gate`` section holding the wrong types all degrade to
    :data:`DEFAULT_DENY_PREFIXES`.
    """

    if not _AGENT_TOKEN.fullmatch(agent or ""):
        return _defaults(agent, None, "agent name is not a safe filename token")
    base = _absolute(_as_text(vault_root))
    if base is None:
        return _defaults(agent, None, "vault root could not be normalised")
    source = Path(base) / ".brain" / f"agent-{agent}.json"
    try:
        raw = source.read_text(encoding="utf-8")
    except (OSError, ValueError, UnicodeDecodeError):
        return _defaults(agent, source, "policy file is missing or unreadable")
    try:
        document = json.loads(raw)
    except (ValueError, RecursionError):
        return _defaults(agent, source, "policy file is not valid JSON")
    if not isinstance(document, dict):
        return _defaults(agent, source, "policy file is not a JSON object")
    gate = document.get("gate")
    if not isinstance(gate, dict):
        return _defaults(agent, source, "policy file declares no gate section")
    prefixes = _read_prefixes(gate.get("deny_prefixes"))
    if prefixes is None:
        return _defaults(agent, source, "gate.deny_prefixes is not a list of prefixes")
    return GatePolicy(
        agent=agent,
        deny_prefixes=prefixes,
        remediation=_read_remediation(gate.get("remediation")),
        source=source,
    )


def check_write(
    vault_root: Path, target: str | Path, *, agent: str = "claude"
) -> GateDecision:
    """Decide whether writing ``target`` directly is allowed.

    ``target`` may be absolute or relative to ``vault_root``; anything landing
    outside the vault is allowed, because the gate governs the vault rather than
    the filesystem.

    Two forms of the target are checked, and either one matching denies:

    * the **resolved** path, so a symlink or ``..`` cannot smuggle a write into
      a gated directory from outside it;
    * the **lexical** path with ``.``/``..`` folded but symlinks left alone, so
      a symlink *inside* a gated directory cannot be used to slip a write past
      the gate by pointing somewhere harmless.

    Resolution covers the whole target rather than just its parent: the file
    being written usually does not exist yet, and ``realpath`` already resolves
    every existing component and appends the rest, while resolving only the
    parent would miss the case where the final component is itself a symlink
    into a gated directory.
    """

    policy = load_gate_policy(vault_root, agent=agent)
    raw = _as_text(target)
    # brainskit is POSIX-only (it uses fcntl), so a backslash is far likelier to
    # be a Windows-style separator arriving from a hook payload than a literal
    # filename character. Reading it as a separator can only over-deny.
    joined_base = os.path.expanduser(_as_text(vault_root))
    joined = os.path.join(joined_base, raw.replace("\\", "/"))

    candidates: list[str] = []
    for base_value, target_value in (
        (_realpath(joined_base), _realpath(joined)),
        (_absolute(joined_base), _absolute(joined)),
    ):
        if base_value is None or target_value is None:
            # A path Python cannot even normalise (an embedded NUL, say) cannot
            # be opened either, so there is nothing to gate.
            continue
        relative = _vault_relative(base_value, target_value)
        if relative is not None and relative not in candidates:
            candidates.append(relative)

    for relative in candidates:
        rule = policy.match(relative)
        if rule is not None:
            return GateDecision(
                allowed=False,
                path=relative,
                # Ends in a period: the hook prints reason and remediation as
                # one stderr line, and that line is what reaches the model.
                reason=f"{relative} is gated: direct writes under {rule} are not allowed.",
                remediation=policy.remediation_for(rule),
                rule=rule,
            )
    return GateDecision(allowed=True, path=candidates[0] if candidates else raw)


def _defaults(agent: str, source: Path | None, reason: str) -> GatePolicy:
    return GatePolicy(
        agent=agent,
        deny_prefixes=DEFAULT_DENY_PREFIXES,
        remediation=dict(DEFAULT_REMEDIATION),
        source=source,
        degraded=reason,
    )


def _read_prefixes(value: Any) -> tuple[str, ...] | None:
    """Return the usable prefixes in ``value``, or None when it is malformed.

    An explicitly empty list is honoured as a deliberate "deny nothing"; a
    non-empty list whose entries are all unusable is malformed and falls back.
    """

    if not isinstance(value, list):
        return None
    stripped = (entry.strip() for entry in value if isinstance(entry, str))
    # Strip before testing: " " survives _fold_segments as a segment but is an
    # empty prefix once trimmed, and an empty prefix would match every path.
    kept = tuple(entry for entry in stripped if _fold_segments(entry))
    if value and not kept:
        return None
    return kept


def _read_remediation(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    return {
        key: text
        for key, text in value.items()
        if isinstance(key, str) and isinstance(text, str)
    }


def _fold_segments(value: str) -> tuple[str, ...]:
    """Split a POSIX-ish path into comparable segments.

    Comparison is case-insensitive. macOS — the primary deployment target — is
    case-insensitive by default, so ``WIKI/x.md`` names the same file as
    ``wiki/x.md`` and a case-sensitive comparison would be a one-keystroke
    bypass. On a case-sensitive filesystem this can over-deny a genuinely
    distinct ``WIKI/`` directory, which is the safe direction and does not arise
    in a brainskit vault: the scaffold only ever creates lowercase ``wiki/`` and
    ``raw/``.
    """

    return tuple(
        segment.casefold()
        for segment in value.replace("\\", "/").split("/")
        if segment not in ("", ".")
    )


def _vault_relative(base: str, candidate: str) -> str | None:
    """Return ``candidate`` relative to ``base``, or None when it is outside."""

    if _same_path(candidate, base):
        return "."
    prefix = base if base.endswith(os.sep) else base + os.sep
    if len(candidate) <= len(prefix):
        return None
    if not _same_path(candidate[: len(prefix)], prefix):
        return None
    return candidate[len(prefix) :].replace(os.sep, "/")


def _same_path(left: str, right: str) -> bool:
    # Compared as equal-length slices so a case fold that changes length cannot
    # desynchronise the offsets the caller slices with.
    return left == right or left.lower() == right.lower()


def _absolute(value: str) -> str | None:
    """Absolute path with ``.``/``..`` folded lexically, symlinks left alone."""

    try:
        return os.path.abspath(value)
    except (OSError, ValueError):
        return None


def _realpath(value: str) -> str | None:
    """Absolute path with every resolvable symlink followed (non-strict)."""

    try:
        return os.path.realpath(value)
    except (OSError, ValueError):
        return None


def _as_text(value: str | Path) -> str:
    try:
        return os.fspath(value)
    except TypeError:
        return str(value)
