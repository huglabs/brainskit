"""The package version has exactly one source of truth.

`bk --version` reported 0.4.0 while the distribution, the git tag and the
published artifact said 0.5.0. `release.yml` asserted tag == [project].version
but never looked at `__version__`, and `verify-wheel.sh` did not compare them
either -- so the drift shipped straight through the gate built to catch it.

These tests are the guard that survives after CI changes: they fail if a second
literal ever reappears.
"""

from __future__ import annotations

import re
import tomllib
import unittest
from pathlib import Path

import brainskit

REPO_ROOT = Path(__file__).resolve().parents[1]
PYPROJECT = REPO_ROOT / "pyproject.toml"


def declared_version() -> str:
    with PYPROJECT.open("rb") as handle:
        return str(tomllib.load(handle)["project"]["version"])


class VersionIsSingleSourcedTest(unittest.TestCase):
    def test_package_version_matches_the_distribution(self) -> None:
        """The one assertion `release.yml` was missing."""

        self.assertEqual(brainskit.__version__, declared_version())

    def test_the_cli_reports_the_distribution_version(self) -> None:
        """`bk --version` is what a bug reporter pastes into SECURITY.md."""

        from brainskit.interfaces import cli

        self.assertEqual(cli.__version__, declared_version())

    def test_mcp_server_info_reports_the_distribution_version(self) -> None:
        """MCP `serverInfo.version` drifted with the same literal."""

        from brainskit.interfaces import mcp

        self.assertEqual(mcp.__version__, declared_version())

    def test_the_version_is_not_a_second_literal_in_the_package(self) -> None:
        """The structural guard: `__init__.py` must not carry its own string.

        Equality alone would pass again the moment someone hand-syncs two
        literals, which is precisely how 0.4.0 and 0.5.0 stayed 'in agreement'
        until they didn't.
        """

        source = (REPO_ROOT / "src" / "brainskit" / "__init__.py").read_text()
        # Any version-shaped literal, not just today's value: asserting the
        # absence of the *correct* string would pass while a stale one sat
        # there, which is exactly the state this test was written against.
        literals = re.findall(r"""["']\d+\.\d+\.\d+[^"']*["']""", source)
        self.assertEqual(
            literals,
            [],
            msg="__init__.py carries a version literal; derive it from "
            "distribution metadata so the two cannot diverge",
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
