from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from brainkit.domain.model import NotFoundError, ValidationError
from brainkit.infrastructure.integrations import vault_id

REGISTRY_VERSION = 1
# The registry names every vault on the machine, which is a map of what the
# operator works on. It carries no secret -- vault configuration stores only
# the *names* of environment variables, and this file is held to the same rule
# -- but the paths alone are worth keeping to one account on a shared host.
REGISTRY_DIRECTORY_MODE = 0o700
REGISTRY_FILE_MODE = 0o600
VAULT_MARKER = Path(".brain") / "config.json"


@dataclass(frozen=True)
class RegisteredVault:
    """One entry: an absolute vault root and the name the operator calls it."""

    path: Path
    label: str

    def to_dict(self) -> dict[str, str]:
        return {"path": str(self.path), "label": self.label}


class VaultRegistry:
    """The machine-level list of vaults `bk vaults` operates on.

    Vaults belong to unrelated projects, so there is nothing to discover them
    from. A filesystem scan would be slow, would miss every vault outside the
    trees it was pointed at, and would find checkouts, copies and backups that
    must never be synced into a shared store under their own identity. So the
    operator names each vault once and the list is durable.
    """

    def __init__(self, path: Path | None = None):
        self.path = (path or default_registry_path()).expanduser()

    def entries(self) -> list[RegisteredVault]:
        if not self.path.is_file():
            return []
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValidationError(
                "The vault registry is not valid JSON",
                details={"registry": str(self.path), "reason": str(exc)},
            ) from exc
        listed = raw.get("vaults") if isinstance(raw, dict) else None
        if not isinstance(listed, list):
            raise ValidationError(
                "The vault registry has no vaults list",
                details={"registry": str(self.path)},
            )
        entries: list[RegisteredVault] = []
        for item in listed:
            path = str(item.get("path", "")).strip() if isinstance(item, dict) else ""
            if not path:
                raise ValidationError(
                    "A vault registry entry has no path",
                    details={"registry": str(self.path), "entry": item},
                )
            label = str(item.get("label", "")) if isinstance(item, dict) else ""
            entries.append(RegisteredVault(path=Path(path), label=label or Path(path).name))
        return entries

    def describe(self) -> dict[str, Any]:
        """Every entry with the two facts a stale registry is diagnosed from.

        `exists` answers whether the vault is still there, so an entry left
        behind by a deleted or moved project reports rather than raising --
        listing is how an operator finds out what to forget.
        """
        entries = self.entries()
        return {
            "registry": str(self.path),
            "count": len(entries),
            "vaults": [
                {
                    "label": entry.label,
                    "path": str(entry.path),
                    "exists": (entry.path / VAULT_MARKER).is_file(),
                    "vault_id": vault_id(entry.path),
                }
                for entry in entries
            ],
        }

    def register(self, root: Path, *, label: str = "") -> dict[str, Any]:
        resolved = _vault_root(root)
        chosen = label.strip() or resolved.name
        entries = self.entries()
        for index, entry in enumerate(entries):
            if entry.path != resolved:
                continue
            # Registering the same vault again is a no-op, never a second row:
            # a duplicate would sync one vault twice into the same shared store
            # and report a count the operator cannot reconcile.
            if label.strip() and entry.label != chosen:
                entries[index] = RegisteredVault(path=resolved, label=chosen)
                self._write(entries)
                return _registration(resolved, chosen, registered=False, relabelled=True)
            return _registration(resolved, entry.label, registered=False, relabelled=False)
        entries.append(RegisteredVault(path=resolved, label=chosen))
        entries.sort(key=lambda item: str(item.path))
        self._write(entries)
        return _registration(resolved, chosen, registered=True, relabelled=False)

    def forget(self, selector: str) -> dict[str, Any]:
        """Drop one entry. The vault's own files are never read or written."""
        wanted = selector.strip()
        if not wanted:
            raise ValidationError("Name the path or the label of a registered vault")
        entries = self.entries()
        matches = [entry for entry in entries if entry.label == wanted]
        if not matches:
            candidate = Path(wanted).expanduser()
            targets = {candidate, candidate.resolve()}
            matches = [entry for entry in entries if entry.path in targets]
        if not matches:
            raise NotFoundError(
                "No registered vault matches that path or label",
                details={"selector": selector, "registry": str(self.path)},
            )
        if len(matches) > 1:
            raise ValidationError(
                "That label belongs to more than one registered vault",
                details={
                    "label": wanted,
                    "paths": [str(entry.path) for entry in matches],
                    "hint": "Name the path instead",
                },
            )
        removed = matches[0]
        self._write([entry for entry in entries if entry != removed])
        return {
            "vault": str(removed.path),
            "label": removed.label,
            "forgotten": True,
            "count": len(entries) - 1,
        }

    def _write(self, entries: Sequence[RegisteredVault]) -> None:
        directory = self.path.parent
        directory.mkdir(parents=True, exist_ok=True)
        # Applied on every write rather than only at creation: a registry whose
        # directory was widened by an unrelated umask is exactly the state the
        # mode exists to prevent, and this is brainkit's own directory.
        os.chmod(directory, REGISTRY_DIRECTORY_MODE)
        payload = (
            json.dumps(
                {
                    "version": REGISTRY_VERSION,
                    "vaults": [entry.to_dict() for entry in entries],
                },
                indent=2,
                ensure_ascii=False,
            )
            + "\n"
        )
        handle = tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=directory,
            prefix=f".{self.path.name}.",
            delete=False,
        )
        try:
            with handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(handle.name, REGISTRY_FILE_MODE)
            os.replace(handle.name, self.path)
        except Exception:
            Path(handle.name).unlink(missing_ok=True)
            raise


def default_registry_path(environment: Mapping[str, str] | None = None) -> Path:
    """`$XDG_CONFIG_HOME/brainkit/vaults.json`, falling back to `~/.config`.

    The XDG basedir specification requires an absolute path and says to ignore
    anything else, so a relative value falls back rather than resolving against
    whatever directory the command happened to be run from.
    """
    values = os.environ if environment is None else environment
    configured = str(values.get("XDG_CONFIG_HOME", "")).strip()
    base = Path(configured)
    if not configured or not base.is_absolute():
        base = Path.home() / ".config"
    return base / "brainkit" / "vaults.json"


def _vault_root(root: Path) -> Path:
    """Normalise exactly as `FileVault` does, then prove it is a vault.

    The shared-store identity is a hash of this string, so an entry that
    differed by a symlink or a trailing `.` would report a `vault_id` that no
    row in Postgres carries and no operator could match up.
    """
    resolved = root.expanduser().resolve()
    if not (resolved / VAULT_MARKER).is_file():
        raise ValidationError(
            "Not a brainkit vault",
            details={
                "path": str(resolved),
                "expected": str(resolved / VAULT_MARKER),
                "hint": "Run bk init there first",
            },
        )
    return resolved


def _registration(
    root: Path, label: str, *, registered: bool, relabelled: bool
) -> dict[str, Any]:
    return {
        "vault": str(root),
        "label": label,
        "vault_id": vault_id(root),
        "registered": registered,
        "relabelled": relabelled,
    }
