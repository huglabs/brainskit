"""`bk update`: check PyPI, then upgrade this installation the right way.

The command is brainskit's one deliberate network call and its one
self-mutation. Everything testable here is tested without either: the fetch is
stubbed at `urlopen`, the upgrade at `subprocess.run`, so the suite stays
stdlib-only and hermetic while still asserting the whole decision tree.
"""

from __future__ import annotations

import contextlib
import io
import json
import unittest
import unittest.mock

from brainskit.infrastructure import pyenv
from brainskit.interfaces import cli


def _pypi_body(version: str) -> bytes:
    return json.dumps({"info": {"version": version}}).encode()


class FakeResponse:
    def __init__(self, body: bytes, status: int = 200):
        self.body = body
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self.body


class LatestVersionTest(unittest.TestCase):
    def test_the_pypi_payload_is_parsed(self) -> None:
        urlopen = unittest.mock.patch.object(
            cli.urllib.request,
            "urlopen",
            return_value=FakeResponse(_pypi_body("9.9.9")),
        )
        with urlopen:
            self.assertEqual(cli._latest_pypi_version(), "9.9.9")

    def test_an_unreachable_pypi_is_unavailable_not_fatal(self) -> None:
        def refuse(*a, **kw):
            raise OSError("no route to host")

        with unittest.mock.patch.object(cli.urllib.request, "urlopen", refuse):
            self.assertIsNone(cli._latest_pypi_version())

    def test_a_malformed_payload_is_unavailable_too(self) -> None:
        with unittest.mock.patch.object(
            cli.urllib.request,
            "urlopen",
            return_value=FakeResponse(b"not json"),
        ):
            self.assertIsNone(cli._latest_pypi_version())

    def test_only_https_is_fetched(self) -> None:
        # urlopen honours file://; the scheme check must hold even if someone
        # later edits the constant to something local.
        with unittest.mock.patch.object(cli, "_PYPI_JSON_URL", "file:///etc/hostname"):
            self.assertIsNone(cli._latest_pypi_version())


class VersionComparisonTest(unittest.TestCase):
    def test_newer_is_outdated_and_equal_is_not(self) -> None:
        self.assertTrue(cli._outdated("0.7.0", "0.8.0"))
        self.assertFalse(cli._outdated("0.8.0", "0.8.0"))
        self.assertFalse(cli._outdated("1.0", "0.9.9"))
        # Padded comparison: 0.7 == 0.7.0.
        self.assertFalse(cli._outdated("0.7", "0.7.0"))


class UpgradeCommandTest(unittest.TestCase):
    """One verb per installer, derived from how bk was actually installed."""

    def test_a_uv_tool_uses_the_tool_upgrade_verb(self) -> None:
        environment = pyenv.Environment(
            kind="uv-tool", executable="/tools/bk/bin/python", prefix="/tools/bk"
        )
        with unittest.mock.patch.object(pyenv.shutil, "which", lambda _: "/bin/uv"):
            self.assertEqual(
                environment.upgrade_command(),
                ["/bin/uv", "tool", "upgrade", "brainskit"],
            )

    def test_pipx_upgrades_the_package(self) -> None:
        environment = pyenv.Environment(
            kind="pipx", executable="/pipx/venvs/bk/bin/python", prefix="/pipx"
        )
        with unittest.mock.patch.object(pyenv.shutil, "which", lambda _: "/bin/pipx"):
            self.assertEqual(
                environment.upgrade_command(), ["/bin/pipx", "upgrade", "brainskit"]
            )

    def test_a_plain_environment_upgrades_in_place(self) -> None:
        environment = pyenv.Environment(
            kind="venv", executable="/venv/bin/python", prefix="/venv"
        )
        with unittest.mock.patch.object(pyenv.shutil, "which", lambda _: "/bin/uv"):
            command = environment.upgrade_command()
        self.assertIn("--upgrade", command)
        self.assertIn("brainskit", command)

    def test_an_unupgradeable_environment_says_so(self) -> None:
        environment = pyenv.Environment(
            kind="system", executable="/usr/bin/python3", prefix="/usr",
            installable=False,
        )
        self.assertIsNone(environment.upgrade_command())


class UpdateFlowTest(unittest.TestCase):
    """The decision tree, end to end through `main` with the edges stubbed."""

    def run_cli(self, argv: list[str]) -> tuple[int, dict, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = cli.main(argv)
            except SystemExit as exit_:  # argparse's own failures
                code = int(exit_.code or 0)
        try:
            payload = json.loads(out.getvalue())
        except Exception:
            payload = {}
        return code, payload, out.getvalue() + err.getvalue()

    def setUp(self) -> None:
        self.runs: list[list[str]] = []

    def _record_run(self, command, **kwargs):
        self.runs.append(list(command))

        class Done:
            returncode = 0
            stdout = ""
            stderr = ""

        return Done()

    def _patched(self, latest: str | None, kind: str = "uv-tool"):
        fetch = unittest.mock.patch.object(
            cli, "_latest_pypi_version", return_value=latest
        )
        runner = unittest.mock.patch.object(cli.subprocess, "run", self._record_run)
        env = unittest.mock.patch.object(
            cli.pyenv,
            "describe_environment",
            return_value=pyenv.Environment(
                kind=kind, executable="/x/python", prefix="/x"
            ),
        )
        which = unittest.mock.patch.object(pyenv.shutil, "which", lambda _: "/bin/uv")
        return fetch, runner, env, which

    def test_check_reports_without_running_anything(self) -> None:
        fetch, runner, env, which = self._patched("9.9.9")
        with fetch, runner, env, which:
            code, payload, _ = self.run_cli(["update", "--check", "--json"])
        self.assertEqual(code, 0)
        self.assertEqual(payload["result"]["state"], "available")
        self.assertEqual(payload["result"]["latest"], "9.9.9")
        self.assertEqual(self.runs, [])

    def test_up_to_date_never_runs_an_upgrade(self) -> None:
        fetch, runner, env, which = self._patched("0.7.0")
        with fetch, runner, env, which:
            _, payload, _ = self.run_cli(["update", "--check", "--json"])
        self.assertEqual(payload["result"]["state"], "up-to-date")
        self.assertEqual(self.runs, [])

    def test_yes_runs_the_upgrade_command(self) -> None:
        fetch, runner, env, which = self._patched("9.9.9")
        with fetch, runner, env, which:
            _, payload, _ = self.run_cli(["update", "--yes", "--json"])
        self.assertEqual(payload["result"]["state"], "updated")
        self.assertEqual(self.runs, [["/bin/uv", "tool", "upgrade", "brainskit"]])

    def test_json_without_yes_returns_the_plan_and_touches_nothing(self) -> None:
        fetch, runner, env, which = self._patched("9.9.9")
        with fetch, runner, env, which:
            _, payload, _ = self.run_cli(["update", "--json"])
        self.assertEqual(payload["result"]["state"], "outdated")
        self.assertIn("--yes", payload["result"].get("hint", ""))
        self.assertEqual(self.runs, [])

    def test_an_unreachable_pypi_answers_unavailable(self) -> None:
        fetch, runner, env, which = self._patched(None)
        with fetch, runner, env, which:
            _, payload, _ = self.run_cli(["update", "--json"])
        self.assertEqual(payload["result"]["state"], "unavailable")
        self.assertEqual(self.runs, [])

    def test_a_failed_upgrade_names_the_cause(self) -> None:
        fetch = unittest.mock.patch.object(
            cli, "_latest_pypi_version", return_value="9.9.9"
        )

        def boom(command, **kwargs):
            class Failed:
                returncode = 1
                stdout = ""
                stderr = "resolution failed"

            return Failed()

        with fetch, unittest.mock.patch.object(cli.subprocess, "run", boom):
            _, payload, _ = self.run_cli(["update", "--yes", "--json"])
        self.assertEqual(payload["result"]["state"], "failed")
        self.assertIn("resolution failed", payload["result"]["stderr_tail"])


class RegistrationTest(unittest.TestCase):
    def test_update_is_listed_exactly_once_in_the_grouped_help(self) -> None:
        listed = [
            name
            for _, names in cli.HELP_CATEGORIES
            for name in names
            if name == "update"
        ]
        self.assertEqual(listed, ["update"])


if __name__ == "__main__":
    unittest.main()
