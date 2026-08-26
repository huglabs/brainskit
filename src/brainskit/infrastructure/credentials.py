"""Provider API keys, kept out of the vault and out of the repository.

`providers.<name>.api_key_env` names an environment variable, which is the
right contract for a server and an incomplete one for a laptop: a wizard can
ask for a key, but it cannot export one into the shell that will run the next
command. So onboarding used to end at *"now set OPENROUTER_API_KEY and try
again"* -- a step with no feedback, which fails silently for anyone who does
not already keep provider keys in their shell profile.

A key the operator types is written here instead: `$XDG_CONFIG_HOME/brainskit/
credentials.json`, mode 0600 inside a 0700 directory, read only when the
environment does not already answer.

**The environment always wins.** A stored file that could override an exported
variable would let a key typed once on a laptop silently change what a scripted
deployment authenticates as -- and the failure would be invisible, because both
paths produce a working request to the wrong account.

Never the vault. `.brain/config.json` sits inside the repository for most
vaults and is printed verbatim by `bk init --print-config`, `bk status` and
every `--json` caller; a secret there is a secret in git. That is why vault
config stores the *name* of a variable and never its value, and why this file
lives beside the vault registry rather than inside any vault.
"""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Mapping
from pathlib import Path

from brainskit.domain.model import ValidationError
from brainskit.infrastructure.vaults import config_home

CREDENTIALS_VERSION = 1
CREDENTIALS_DIRECTORY_MODE = 0o700
CREDENTIALS_FILE_MODE = 0o600


def default_credentials_path(environment: Mapping[str, str] | None = None) -> Path:
    """`$XDG_CONFIG_HOME/brainskit/credentials.json`.

    No legacy `brainkit/` fallback, unlike the vault registry beside it: this
    file did not exist under the old name, so there is nothing to migrate and a
    fallback would only widen where a secret might be found.
    """

    return config_home(environment) / "brainskit" / "credentials.json"


class CredentialStore:
    """Secrets this machine holds, addressed by the variable name they answer to.

    Keying on the environment-variable name rather than on the provider is what
    keeps the contract single: `api_key_env` is already the question every
    driver asks, so a stored credential answers it without any driver learning
    a second lookup rule.
    """

    def __init__(self, path: Path | None = None):
        self.path = (path or default_credentials_path()).expanduser()

    def lookup(self, name: str, environment: Mapping[str, str] | None = None) -> str:
        """The value for `name`: the environment first, then this file."""

        if not name:
            return ""
        values = os.environ if environment is None else environment
        exported = str(values.get(name, "") or "")
        return exported or self.stored(name)

    def stored(self, name: str) -> str:
        """What this file holds for `name`, ignoring the environment entirely."""

        return str(self._entries().get(name, "") or "")

    def names(self) -> list[str]:
        """The variable names held here. Never the values -- this is for reports."""

        return sorted(self._entries())

    def remember(self, name: str, value: str) -> Path:
        if not name.strip():
            raise ValidationError("A credential needs the name of its variable")
        if not value.strip():
            raise ValidationError(
                "A credential needs a value",
                details={"name": name, "hint": "Use forget() to remove one"},
            )
        entries = self._entries()
        entries[name.strip()] = value
        self._write(entries)
        return self.path

    def forget(self, name: str) -> bool:
        entries = self._entries()
        if name not in entries:
            return False
        del entries[name]
        self._write(entries)
        return True

    def _entries(self) -> dict[str, str]:
        if not self.path.is_file():
            return {}
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValidationError(
                "Credential file is unreadable",
                details={"path": str(self.path), "reason": str(exc)},
            ) from exc
        raw = payload.get("credentials") if isinstance(payload, dict) else None
        if not isinstance(raw, dict):
            return {}
        return {str(k): str(v) for k, v in raw.items() if isinstance(v, str)}

    def _write(self, entries: Mapping[str, str]) -> None:
        directory = self.path.parent
        directory.mkdir(parents=True, exist_ok=True)
        # Re-applied on every write rather than only at creation, exactly as the
        # vault registry does: a secret directory widened by an unrelated umask
        # is the state the mode exists to prevent.
        os.chmod(directory, CREDENTIALS_DIRECTORY_MODE)
        payload = (
            json.dumps(
                {
                    "version": CREDENTIALS_VERSION,
                    "credentials": dict(sorted(entries.items())),
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
            # Before the rename, never after: a window in which the finished
            # file is world-readable is the whole exposure this mode prevents.
            os.chmod(handle.name, CREDENTIALS_FILE_MODE)
            os.replace(handle.name, self.path)
        except Exception:
            Path(handle.name).unlink(missing_ok=True)
            raise


def lookup(name: str, environment: Mapping[str, str] | None = None) -> str:
    """Module-level convenience for drivers, which hold no store of their own."""

    return CredentialStore().lookup(name, environment)
