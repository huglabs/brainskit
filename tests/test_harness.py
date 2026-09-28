"""The harness every test module stands on, pinned so it cannot quietly lapse.

Two properties: no runner reaches the operator's own `~/.config` (the isolation
used to live in `conftest.py`, which `python -m unittest` never imports), and
`run_cli` refuses a run that never reached the command under test.
"""

from __future__ import annotations

try:
    from . import _harness
except ImportError:
    import _harness

import ast
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

TESTS = Path(__file__).resolve().parent
REPO_ROOT = TESTS.parent

#: Runs `bk init`, which registers the vault it creates -- so an unisolated run
#: writes the registry, which is what makes it a fair probe.
PROBE = "test_non_interactive_init"


def _is_harness_import(node: ast.stmt) -> bool:
    """`try: from . import _harness` / `except ImportError: import _harness`."""

    if not isinstance(node, ast.Try) or len(node.handlers) != 1:
        return False
    body, handler = node.body, node.handlers[0]
    relative = (
        len(body) == 1
        and isinstance(body[0], ast.ImportFrom)
        and body[0].level == 1
        and [alias.name for alias in body[0].names] == ["_harness"]
    )
    plain = (
        len(handler.body) == 1
        and isinstance(handler.body[0], ast.Import)
        and [alias.name for alias in handler.body[0].names] == ["_harness"]
    )
    caught = isinstance(handler.type, ast.Name) and handler.type.id == "ImportError"
    return relative and plain and caught


class EveryModuleImportsTheHarnessFirstTest(unittest.TestCase):
    def test_no_test_module_can_run_without_the_isolation(self) -> None:
        modules = sorted(TESTS.glob("test_*.py"))
        self.assertGreater(len(modules), 40, "the scan found the test modules")
        offenders = []
        for module in modules:
            tree = ast.parse(module.read_text(encoding="utf-8"))
            statements = [
                node
                for node in tree.body
                if not (
                    isinstance(node, ast.Expr)
                    and isinstance(node.value, ast.Constant)
                    and isinstance(node.value.value, str)
                )
                and not (
                    isinstance(node, ast.ImportFrom) and node.module == "__future__"
                )
            ]
            if not statements or not _is_harness_import(statements[0]):
                offenders.append(module.name)
        self.assertEqual(
            offenders,
            [],
            "these modules do not import `_harness` before anything else, so "
            "`python -m unittest` runs them against the operator's ~/.config",
        )

    def test_no_test_module_drives_the_cli_around_the_vacuity_guard(self) -> None:
        offenders = []
        for module in sorted(TESTS.glob("test_*.py")):
            for node in ast.walk(ast.parse(module.read_text(encoding="utf-8"))):
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "main"
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id == "cli"
                ):
                    offenders.append(f"{module.name}:{node.lineno}")
        self.assertEqual(
            offenders, [], "call `_harness.run_cli`, which refuses a vacuous run"
        )

    def test_the_scan_recognises_a_module_that_skips_it(self) -> None:
        """Control: the predicate is not satisfied by any leading `try`."""

        skipped = ast.parse("import os\ntry:\n    import x\nexcept ImportError:\n    pass\n")
        self.assertFalse(_is_harness_import(skipped.body[0]))
        self.assertFalse(_is_harness_import(skipped.body[1]))


class StandaloneRunnerIsolationTest(unittest.TestCase):
    """A test file run outside pytest must not touch the operator's registry.

    The child gets a fake `HOME` and the `XDG_CONFIG_HOME` an operator's shell
    would resolve to, with this run's isolation stripped -- the environment of
    someone typing the command by hand.
    """

    def run_probe(self, command: list[str], *, preisolated: bool) -> Path:
        home = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        config = home / ".config"
        environment = {
            key: value
            for key, value in os.environ.items()
            if key not in {"XDG_CONFIG_HOME", _harness.ISOLATION_MARKER}
        }
        environment.update(HOME=str(home), XDG_CONFIG_HOME=str(config))
        if preisolated:
            environment[_harness.ISOLATION_MARKER] = str(config)
        completed = subprocess.run(
            command,
            cwd=REPO_ROOT,
            env=environment,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertRegex(completed.stderr, r"Ran [1-9]\d* tests?")
        return config / "brainskit" / "vaults.json"

    def test_python_dash_m_unittest_on_the_dotted_module(self) -> None:
        registry = self.run_probe(
            [sys.executable, "-m", "unittest", f"tests.{PROBE}"], preisolated=False
        )
        self.assertFalse(registry.exists(), f"wrote the operator's {registry}")

    def test_running_the_file_as_a_script(self) -> None:
        registry = self.run_probe(
            [sys.executable, str(TESTS / f"{PROBE}.py")], preisolated=False
        )
        self.assertFalse(registry.exists(), f"wrote the operator's {registry}")

    def test_the_probe_really_registers_when_its_isolation_is_that_directory(
        self,
    ) -> None:
        """Control: without it the two cases above would pass on a probe that
        never registers anything. Marking the operator-looking directory as
        already isolated makes the harness keep it, so the write lands there."""

        registry = self.run_probe(
            [sys.executable, "-m", "unittest", f"tests.{PROBE}"], preisolated=True
        )
        self.assertTrue(registry.exists(), "the probe no longer registers a vault")


class IsolationIsIdempotentTest(unittest.TestCase):
    def test_an_isolation_already_in_place_is_kept(self) -> None:
        with mock.patch.dict(
            os.environ,
            {"XDG_CONFIG_HOME": "/iso", _harness.ISOLATION_MARKER: "/iso"},
        ):
            self.assertEqual(_harness._isolate(), "/iso")
            self.assertEqual(os.environ["XDG_CONFIG_HOME"], "/iso")

    def test_an_operator_s_own_config_home_is_replaced(self) -> None:
        with mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": "/home/op/.config"}):
            os.environ.pop(_harness.ISOLATION_MARKER, None)
            isolated = _harness._isolate()
            self.assertNotEqual(isolated, "/home/op/.config")
            self.assertEqual(os.environ["XDG_CONFIG_HOME"], isolated)
            self.assertTrue(Path(isolated).is_absolute())
            Path(isolated).rmdir()

    def test_this_run_is_isolated(self) -> None:
        self.assertEqual(os.environ["XDG_CONFIG_HOME"], _harness.CONFIG_HOME)
        self.assertFalse(
            Path(_harness.CONFIG_HOME).resolve().is_relative_to(
                (Path.home() / ".config").resolve()
            )
        )


class RunCliVacuityGuardTest(unittest.TestCase):
    def test_every_marker_is_refused(self) -> None:
        for marker in _harness.VACUITY_MARKERS:
            with self.subTest(marker=marker), self.assertRaises(AssertionError):
                _harness.refuse_vacuous(f"bk: {marker}\n")

    def test_a_named_marker_is_allowed_only_when_it_occurs(self) -> None:
        _harness.refuse_vacuous("Traceback ...", expect=("Traceback",))
        with self.assertRaisesRegex(AssertionError, "stale"):
            _harness.refuse_vacuous("all fine", expect=("Traceback",))

    def test_an_opt_out_covers_only_the_marker_it_names(self) -> None:
        with self.assertRaises(AssertionError):
            _harness.refuse_vacuous(
                "Traceback\nNot a brainskit vault", expect=("Traceback",)
            )

    def test_an_unknown_marker_is_a_typo_not_an_opt_out(self) -> None:
        with self.assertRaises(ValueError):
            _harness.refuse_vacuous("", expect=("Not a vault",))

    def test_a_command_on_a_directory_that_is_not_a_vault_is_refused(self) -> None:
        """End to end: exit 2 here is the same status a gate denial returns."""

        with tempfile.TemporaryDirectory() as empty:
            with self.assertRaisesRegex(AssertionError, "vacuous"):
                _harness.run_cli(["--vault", empty, "status"])
            run = _harness.run_cli(
                ["--vault", empty, "status"], expect=("Not a brainskit vault",)
            )
        self.assertEqual(run.code, 2)

    def test_argparse_exits_are_returned_not_raised(self) -> None:
        run = _harness.run_cli(["--help"])
        self.assertFalse(run.exited)
        run = _harness.run_cli(["status", "--no-such-flag"])
        self.assertTrue(run.exited)
        self.assertEqual(run.code, 2)


if __name__ == "__main__":
    unittest.main()
