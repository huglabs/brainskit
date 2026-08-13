"""brainskit public package."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _distribution_version
from pathlib import Path

__all__ = ["__version__"]


def _version() -> str:
    """The one version, read rather than restated.

    A second literal here is how `bk --version` came to report 0.4.0 against a
    0.5.0 distribution and a v0.5.0 tag: `release.yml` asserted the tag matched
    `[project].version` and never looked at this file, so the drift shipped
    through the gate built to catch it.

    Installed -- including editable installs and the built wheel -- the answer
    comes from distribution metadata, which is the same string the artifact was
    published under. From a source tree that was never installed there is no
    metadata, so fall back to `pyproject.toml`: still the single source, just
    read directly.
    """

    try:
        return _distribution_version("brainskit")
    except PackageNotFoundError:
        pyproject = Path(__file__).resolve().parents[2] / "pyproject.toml"
        try:
            import tomllib

            with pyproject.open("rb") as handle:
                return str(tomllib.load(handle)["project"]["version"])
        except (OSError, KeyError, ValueError):
            # Neither installed nor in a checkout. Report the uncertainty rather
            # than inventing a number a bug reporter would then quote at us.
            return "0+unknown"


__version__ = _version()
