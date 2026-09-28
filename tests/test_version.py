"""The package version has exactly one source of truth.

`bk --version` reported 0.4.0 while the distribution, the git tag and the
published artifact said 0.5.0. `release.yml` asserted tag == [project].version
but never looked at `__version__`, and `verify-wheel.sh` did not compare them
either -- so the drift shipped straight through the gate built to catch it.

These tests are the guard that survives after CI changes: they fail if a second
literal ever reappears. The same goes for the release workflow's other version
assertions -- that the version is publishable, and that it landed on PyPI --
which are only ever exercised on a real release, where a wrong answer is
permanent.
"""

from __future__ import annotations

try:
    from . import _harness
except ImportError:
    import _harness  # noqa: F401

import os
import re
import shutil
import subprocess
import tempfile
import textwrap
import tomllib
import unittest
from pathlib import Path

import brainskit

REPO_ROOT = Path(__file__).resolve().parents[1]
PYPROJECT = REPO_ROOT / "pyproject.toml"
RELEASE = REPO_ROOT / ".github" / "workflows" / "release.yml"
VISIBILITY = REPO_ROOT / "scripts" / "check-pypi-visibility.sh"


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


def _simple_index(*filenames: str) -> str:
    """A PyPI simple-index page: every filename is the anchor text of a link."""

    links = "\n".join(
        f'<a href="https://files.pythonhosted.org/packages/x/{name}">{name}</a><br />'
        for name in filenames
    )
    return f"<!DOCTYPE html><html><body><h1>Links for brainskit</h1>\n{links}\n</body></html>"


#: What PyPI actually lists for this project before 0.8.0 ships.
PUBLISHED = (
    "brainskit-0.6.0-py3-none-any.whl",
    "brainskit-0.6.0.tar.gz",
    "brainskit-0.7.0-py3-none-any.whl",
    "brainskit-0.7.0.tar.gz",
)

#: The check `release.yml` shipped with: one substring, anywhere in the body.
_OLD_CHECK = '''case "$body" in *"brainskit-$version"*) exit 0 ;; esac; exit 1'''


def _release_step_run(name: str) -> tuple[str, str]:
    """The text of the `release.yml` step called `name`, and its `run:` script.

    Read as text rather than parsed, so the suite needs no YAML dependency;
    the step's shell is executed as written, which is the point -- a copy of it
    here would be tested instead of the thing CI runs.
    """

    lines = RELEASE.read_text().splitlines()
    header = next(
        i
        for i, line in enumerate(lines)
        if line.strip() in (f"- name: {name}", f'- name: "{name}"')
    )
    indent = len(lines[header]) - len(lines[header].lstrip())
    step = [lines[header]]
    for line in lines[header + 1 :]:
        if line.strip() and len(line) - len(line.lstrip()) <= indent:
            break
        step.append(line)
    run_at = next(i for i, line in enumerate(step) if line.strip() == "run: |")
    run_indent = len(step[run_at]) - len(step[run_at].lstrip())
    body = []
    for line in step[run_at + 1 :]:
        if line.strip() and len(line) - len(line.lstrip()) <= run_indent:
            break
        body.append(line)
    return "\n".join(step), textwrap.dedent("\n".join(body))


@unittest.skipUnless(shutil.which("bash") and shutil.which("curl"), "needs bash and curl")
class PublishedVersionIsVisibleTest(unittest.TestCase):
    """`scripts/check-pypi-visibility.sh`, fed fixture indexes over `file://`.

    The old gate matched `brainskit-$version` as a substring, so a `v0.6` tag
    passed on 0.6.0's files and one uploaded artifact of two was enough.
    """

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def _index(self, *filenames: str) -> Path:
        page = self.tmp / "index.html"
        page.write_text(_simple_index(*filenames), encoding="utf-8")
        return page

    def check(self, version: str, *filenames: str) -> subprocess.CompletedProcess[str]:
        env = {
            **os.environ,
            "BRAINSKIT_INDEX_URL": self._index(*filenames).as_uri(),
            "ATTEMPTS": "2",
            "DELAY": "0",
        }
        return subprocess.run(
            [str(VISIBILITY), version],
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )

    def old_check(self, version: str, *filenames: str) -> int:
        return subprocess.run(
            ["bash", "-c", _OLD_CHECK],
            env={**os.environ, "body": _simple_index(*filenames), "version": version},
            timeout=30,
            check=False,
        ).returncode

    def test_both_artifacts_of_the_exact_version_pass(self) -> None:
        result = self.check("0.7.0", *PUBLISHED)
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertIn("brainskit 0.7.0 is on PyPI", result.stdout)

    def test_a_version_prefix_is_not_the_version(self) -> None:
        """`v0.6` is not satisfied by `brainskit-0.6.0-py3-none-any.whl`."""

        for version in ("0.6", "0.7", "0.8.0"):
            with self.subTest(version=version):
                result = self.check(version, *PUBLISHED)
                self.assertEqual(result.returncode, 1)
                self.assertIn(f"brainskit-{version}-py3-none-any.whl", result.stderr)
                self.assertIn(f"brainskit-{version}.tar.gz", result.stderr)

    def test_a_wheel_without_its_sdist_fails(self) -> None:
        result = self.check("0.8.0", *PUBLISHED, "brainskit-0.8.0-py3-none-any.whl")
        self.assertEqual(result.returncode, 1)
        self.assertIn("missing: brainskit-0.8.0.tar.gz", result.stderr)

    def test_an_sdist_without_its_wheel_fails(self) -> None:
        result = self.check("0.8.0", *PUBLISHED, "brainskit-0.8.0.tar.gz")
        self.assertEqual(result.returncode, 1)
        self.assertIn("missing: brainskit-0.8.0-py3-none-any.whl", result.stderr)

    def test_an_unreachable_index_fails(self) -> None:
        env = {
            **os.environ,
            "BRAINSKIT_INDEX_URL": (self.tmp / "absent.html").as_uri(),
            "ATTEMPTS": "1",
        }
        result = subprocess.run(
            [str(VISIBILITY), "0.7.0"],
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        self.assertEqual(result.returncode, 1)

    def test_the_old_substring_check_passed_exactly_these_cases(self) -> None:
        """The negative control: the fixtures above are the ones that got through.

        Without this, the new tests could pass on inputs the old check also
        rejected, and prove nothing about the defect they were written for.
        """

        cases = {
            "prefix tag": ("0.6", PUBLISHED),
            "wheel only": ("0.8.0", (*PUBLISHED, "brainskit-0.8.0-py3-none-any.whl")),
            "sdist only": ("0.8.0", (*PUBLISHED, "brainskit-0.8.0.tar.gz")),
        }
        for label, (version, files) in cases.items():
            with self.subTest(label):
                self.assertEqual(self.old_check(version, *files), 0)
                self.assertEqual(self.check(version, *files).returncode, 1)

    def test_the_release_workflow_runs_this_script(self) -> None:
        text = RELEASE.read_text()
        self.assertIn("./scripts/check-pypi-visibility.sh", text)
        self.assertNotIn('*"brainskit-$version"*', text)


class LocalVersionIsRefusedTest(unittest.TestCase):
    """`0.8.0+huglabs.1` names a private build and must never reach PyPI."""

    STEP = "[project].version is publishable"

    def run_step(self, version: str) -> subprocess.CompletedProcess[str]:
        _, script = _release_step_run(self.STEP)
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "pyproject.toml").write_text(
                f'[project]\nname = "brainskit"\nversion = "{version}"\n'
            )
            return subprocess.run(
                ["bash", "-e", "-c", script],
                cwd=tmp,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )

    def test_a_local_version_is_refused(self) -> None:
        result = self.run_step("0.8.0+huglabs.1")
        self.assertEqual(result.returncode, 1)
        self.assertIn("0.8.0+huglabs.1 is a local version", result.stderr)

    def test_a_public_version_passes(self) -> None:
        result = self.run_step("0.8.0")
        self.assertEqual(result.returncode, 0, msg=result.stderr)

    def test_the_declared_version_is_publishable(self) -> None:
        self.assertNotIn("+", declared_version())

    def test_the_refusal_runs_on_every_trigger_before_anything_installs(self) -> None:
        """Unconditional, unlike the tag step, and ahead of `uv sync`."""

        step, _ = _release_step_run(self.STEP)
        self.assertNotRegex(step, r"(?m)^\s*if:", msg="must run on workflow_dispatch too")
        text = RELEASE.read_text()
        self.assertLess(text.index(self.STEP), text.index("uv sync"))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
