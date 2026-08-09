"""How `bk` was installed, and therefore how to add a package to it.

Every "install the optional X" message brainkit printed named `pip`, and on the
machine this was written for that instruction cannot work: `bk` is a `uv tool`,
and a uv tool environment ships no `pip` at all. The advice was not merely
terse — following it either failed outright or, worse, succeeded against some
*other* interpreter on `PATH` and installed the grammar somewhere `bk` will
never look. Either way the operator does as they are told, retries, and sees
the identical message.

So the hint is derived from the running interpreter rather than assumed. There
is exactly one question — "which command adds a package to *this* environment"
— and four answers, distinguished by artefacts the installers themselves leave
behind:

    uv tool     uv-receipt.toml beside the environment
    pipx        pipx_metadata.json beside the environment
    venv        sys.prefix differs from sys.base_prefix
    system      none of the above

`uv pip install --python <interpreter>` is preferred wherever `uv` is on
`PATH`, because it targets an interpreter explicitly and does not require the
target environment to contain `pip`. That is the whole failure this module
exists to prevent, so it is not an optimisation.

Nothing here imports the package manager or shells out on import; a
`Environment` is a description, and running the command is the caller's
decision (`bk code build` asks first).
"""

from __future__ import annotations

import importlib.util
import os
import shlex
import shutil
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

#: The distribution name, used when a manager installs *into* a package
#: (`pipx inject`) rather than into an environment.
_PACKAGE = "brainkit"


@dataclass(frozen=True)
class Environment:
    """The interpreter running `bk`, and how to add packages to it."""

    kind: str
    executable: str
    prefix: str
    #: False when nothing here can produce a working command — a system
    #: interpreter with no `uv` and no `pip` module, typically a distro python
    #: whose packages are managed elsewhere. Saying so is better than emitting
    #: a command that will fail.
    installable: bool = True

    @property
    def label(self) -> str:
        return {
            "uv-tool": "uv tool",
            "pipx": "pipx",
            "venv": "virtualenv",
            "system": "system interpreter",
        }.get(self.kind, self.kind)

    def install_command(self, packages: Sequence[str]) -> list[str]:
        """Argv that adds `packages` to this environment."""

        wanted = list(packages)
        uv = shutil.which("uv")
        if self.kind == "pipx" and shutil.which("pipx"):
            return ["pipx", "inject", _PACKAGE, *wanted]
        if uv:
            return [uv, "pip", "install", "--python", self.executable, *wanted]
        command = [self.executable, "-m", "pip", "install"]
        if self.kind == "system":
            command.append("--user")
        return [*command, *wanted]

    def install_hint(self, packages: Sequence[str]) -> str:
        """The same command, shaped for a human to copy out of a message."""

        if not self.installable:
            return (
                "This interpreter has neither uv nor pip available, so "
                f"{', '.join(packages)} must be installed however "
                f"{self.executable} is managed"
            )
        return shlex.join(self.install_command(packages))


def describe_environment() -> Environment:
    """Classify the interpreter running this process."""

    prefix = Path(sys.prefix)
    executable = sys.executable or "python3"
    if (prefix / "uv-receipt.toml").is_file():
        kind = "uv-tool"
    elif (prefix / "pipx_metadata.json").is_file() or _under_pipx(prefix):
        kind = "pipx"
    elif sys.prefix != sys.base_prefix:
        kind = "venv"
    else:
        kind = "system"
    installable = bool(shutil.which("uv")) or _has_pip()
    return Environment(
        kind=kind,
        executable=executable,
        prefix=str(prefix),
        installable=installable,
    )


def _under_pipx(prefix: Path) -> bool:
    home = os.environ.get("PIPX_HOME")
    roots = [Path(home)] if home else [Path.home() / ".local" / "pipx"]
    return any(prefix.is_relative_to(root) for root in roots if root.exists())


def _has_pip() -> bool:
    return importlib.util.find_spec("pip") is not None


def install_hint(packages: Sequence[str]) -> str:
    """One-shot convenience for the many call sites that only want the string."""

    return describe_environment().install_hint(packages)


def grammar_install_hint(distributions: Sequence[str] | None = None) -> str:
    """The hint for missing tree-sitter grammars.

    With no argument this is the whole-extra form, which is what a caller that
    only knows "extraction is unavailable" can say. Given the specific
    distributions a scan needs, it names those instead — installing three
    grammars is a much smaller ask than the ~70 MB `code-all` set, and an
    accurate hint is the difference between the operator running it and not.
    """

    environment = describe_environment()
    if not distributions:
        return environment.install_hint(["brainkit[code]"])
    return environment.install_hint(list(distributions))
