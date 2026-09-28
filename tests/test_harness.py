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
import re
import subprocess
import sys
import tempfile
import unittest
from collections.abc import Mapping
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


NON_VACUOUS = re.compile(r"#\s*non-vacuous:\s*\S")
_VIEWS = frozenset({"items", "keys", "values"})
_WRAPPERS = frozenset(
    {"enumerate", "zip", "sorted", "reversed", "list", "tuple", "set"}
)


def _asserted(node: ast.stmt) -> ast.expr | None:
    """What `assert x` or `self.assertX(...)` checks; None for any other statement."""

    if isinstance(node, ast.Assert):
        return node.test
    if (
        isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Attribute)
        and node.value.func.attr.startswith("assert")
    ):
        return node.value
    return None


def _is_literal(node: ast.expr, bound: Mapping[str, ast.expr]) -> bool:
    """A collection whose size the test source fixes, whatever production does."""

    if isinstance(node, ast.List | ast.Tuple | ast.Set):
        return not any(isinstance(element, ast.Starred) for element in node.elts)
    if isinstance(node, ast.Dict):
        return None not in node.keys
    if isinstance(node, ast.Call):
        func = node.func
        if isinstance(func, ast.Name) and func.id == "range":
            return all(isinstance(argument, ast.Constant) for argument in node.args)
        if isinstance(func, ast.Name) and func.id in _WRAPPERS:
            return bool(node.args) and all(_is_literal(a, bound) for a in node.args)
        if isinstance(func, ast.Attribute) and func.attr in _VIEWS:
            return _is_literal(func.value, bound)
        return False
    name = ast.unparse(node)
    if name not in bound:
        return False
    rest = {key: value for key, value in bound.items() if key != name}
    return _is_literal(bound[name], rest)


def _cores(node: ast.expr) -> set[str]:
    """The iterable as written, and the collections under `sorted(x.items())`."""

    found = {ast.unparse(node)}
    if isinstance(node, ast.Call):
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr in _VIEWS:
            found |= _cores(func.value)
        elif isinstance(func, ast.Name) and func.id in _WRAPPERS:
            for argument in node.args:
                found |= _cores(argument)
    return found


def _bindings(body: list[ast.stmt], *prefixes: str) -> dict[str, ast.expr]:
    bound: dict[str, ast.expr] = {}
    for statement in body:
        if isinstance(statement, ast.Assign) and len(statement.targets) == 1:
            target, value = statement.targets[0], statement.value
        elif isinstance(statement, ast.AnnAssign) and statement.value is not None:
            target, value = statement.target, statement.value
        else:
            continue
        if isinstance(target, ast.Name):
            for prefix in prefixes:
                bound[prefix + target.id] = value
    return bound


def vacuity_prone_loops(source: str, name: str) -> list[str]:
    """`name:line` of each `for` whose assertions may never run. See the test."""

    lines = source.splitlines()
    tree = ast.parse(source)
    module = _bindings(tree.body, "")
    scopes: list[tuple[ast.AST, dict[str, ast.expr]]] = [(tree, module)]
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            members = _bindings(node.body, "self.", f"{node.name}.")
            scopes.append((node, {**module, **members}))
    offenders: set[int] = set()
    for scope, bound in scopes:
        for function in ast.iter_child_nodes(scope):
            if not isinstance(function, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            statements = sorted(
                (node for node in ast.walk(function) if isinstance(node, ast.stmt)),
                key=lambda node: (node.lineno, node.col_offset),
            )
            local = dict(bound)
            checked: set[str] = set()
            for statement in statements:
                local.update(_bindings([statement], ""))
                asserted = _asserted(statement)
                if asserted is not None:
                    mentioned = ast.walk(asserted)
                    checked |= {
                        ast.unparse(n) for n in mentioned if isinstance(n, ast.expr)
                    }
                if not isinstance(statement, ast.For | ast.AsyncFor):
                    continue
                if not any(_asserted(inner) is not None for inner in statement.body):
                    continue
                end = max(statement.body[0].lineno - 1, statement.lineno)
                header = lines[statement.lineno - 1 : end]
                if (
                    _is_literal(statement.iter, local)
                    or _cores(statement.iter) & checked
                    or any(NON_VACUOUS.search(line) for line in header)
                ):
                    continue
                offenders.add(statement.lineno)
    return [f"{name}:{line}" for line in sorted(offenders)]


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


class NoAssertionHidesInAnEmptyLoopTest(unittest.TestCase):
    """A loop that never runs is a test that cannot fail (TQ2, issue #19).

    The scan flags a `for` with an assertion (`assert`, `self.assert*`)
    directly in its body, unless one of these shows the loop runs:

    - the iterable is a literal: a list/tuple/set/dict display, `range(<const>)`,
      a name the module, class (`self.X`) or function binds to one, or
      `enumerate`/`zip`/`sorted`/`.items()`/... of those;
    - an earlier assertion in the same function mentions the iterable, or the
      collection under `sorted(x.items())` -- `assertEqual(len(x), 3)`,
      `assertTrue(x)`, `assertEqual(set(X), {...})`;
    - the `for` line carries `# non-vacuous: <reason>`.

    The mention is not proof: `assertEqual(ys, [f(x) for x in xs])` mentions
    `xs` without pinning its size. The scan makes the question visible; the
    precondition still has to answer it. Assertions nested one block deeper
    (`with self.subTest():`, `if ...:`) are outside the rule.
    """

    maxDiff = None

    def test_no_test_module_asserts_in_a_loop_that_may_never_run(self) -> None:
        modules = sorted(TESTS.glob("test_*.py"))
        self.assertGreater(len(modules), 40, "the scan found the test modules")
        offenders = [
            site
            for module in modules
            for site in vacuity_prone_loops(
                module.read_text(encoding="utf-8"), module.name
            )
        ]
        self.assertEqual(
            offenders,
            [],
            "these loops assert over a collection nothing shows is non-empty: "
            "pin its size or contents first, or mark the `for` line "
            "`# non-vacuous: <reason>`",
        )

    def test_the_scan_tells_a_vacuous_loop_from_a_proven_one(self) -> None:
        """Control: the rule flags the shape it names and nothing it exempts."""

        flagged = (
            "def test(self):\n"
            "    for item in produce():\n"
            "        self.assertTrue(item)\n"
        )
        self.assertEqual(vacuity_prone_loops(flagged, "m"), ["m:2"])
        exempt = {
            "display": "def t(self):\n    for x in (a(), b()):\n        assert x\n",
            "range": "def t(self):\n    for i in range(3):\n        assert f(i)\n",
            "module constant": (
                "XS = [1, 2]\n"
                "def t(self):\n    for x in sorted(XS):\n        assert x\n"
            ),
            "class constant": (
                "class C:\n    XS = {'a': 1}\n    def t(self):\n"
                "        for k, v in self.XS.items():\n            assert v\n"
            ),
            "local literal": (
                "def t(self):\n    cases = [1]\n    for c in cases:\n        assert c\n"
            ),
            "precondition": (
                "def t(self):\n    xs = produce()\n    self.assertEqual(len(xs), 2)\n"
                "    for x in xs:\n        self.assertTrue(x)\n"
            ),
            "precondition on the collection": (
                "def t(self):\n    self.assertIn('a', TABLE)\n"
                "    for k, v in TABLE.items():\n        assert v\n"
            ),
            "opt-out": (
                "def t(self):\n"
                "    for x in produce():  # non-vacuous: fixture has two\n"
                "        assert x\n"
            ),
            "opt-out on a one-line loop": (
                "def t(self):\n    for x in produce(): assert x  # non-vacuous: two\n"
            ),
        }
        for shape, source in exempt.items():
            with self.subTest(shape=shape):
                self.assertEqual(vacuity_prone_loops(source, "m"), [])
        rebound = (
            "def t(self):\n    xs = [1]\n    xs = produce()\n"
            "    for x in xs:\n        assert x\n"
        )
        self.assertEqual(vacuity_prone_loops(rebound, "m"), ["m:4"])
        bare_opt_out = (
            "def t(self):\n    for x in produce():  # non-vacuous:\n        assert x\n"
        )
        self.assertEqual(vacuity_prone_loops(bare_opt_out, "m"), ["m:2"])


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
