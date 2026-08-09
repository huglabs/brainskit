"""The coverage gate: a language whose grammar is present must produce nodes.

This is the regression test the incident wanted. `bk code build` on a
four-file repository wrote a two-node graph, exited 0, and `bk code status`
called the result `fresh` — because every check in the system asked "does the
graph match the files it recorded" and none asked "did every file it could
have parsed get parsed".

The assertion is deliberately *relative* to what is installed. Demanding all
26 languages would fail on any environment without the `code-all` extra, which
is most of them, and a test that fails for the wrong reason gets muted. What
must hold everywhere is the invariant: **no file whose grammar is installed may
contribute nothing.** A missing grammar is a reported gap; a present grammar
that yields nothing is a defect.

`benchmarks/run.py` is imported rather than shelled out to, so the fixture and
the measurement here are the same code the benchmark uses — a fixture that
drifted from the benchmark would test nothing.

This file also runs the extractor's **parallel** path under pytest, which is
the environment that exposed the spawned-worker bug: `__main__` is pytest, so
a child inherits nothing that would let it resolve the vendored `graphify`
alias. It passing here is the proof that the shim works rather than that the
harness stepped around it.
"""

from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

BENCHMARKS = Path(__file__).resolve().parent.parent / "benchmarks"
sys.path.insert(0, str(BENCHMARKS))

# Registers the `graphify` alias in this process (side effect only).
import brainkit.infrastructure.codeanalysis  # noqa: E402,F401


def _code_extra_installed() -> bool:
    try:
        import tree_sitter  # noqa: F401
        import tree_sitter_python  # noqa: F401
    except ImportError:
        return False
    return True


_HAS_CODE_EXTRA = _code_extra_installed()


@unittest.skipUnless(_HAS_CODE_EXTRA, "needs the `code` extra's grammars")
class PolyglotCoverageTest(unittest.TestCase):
    def setUp(self) -> None:
        import run as benchmark

        self.benchmark = benchmark
        self._temp = TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.fixture = Path(self._temp.name) / "polyglot"
        benchmark.build_fixture(self.fixture)

    def test_every_installed_grammar_actually_contributes(self) -> None:
        from brainkit.infrastructure.extractor import _extension_grammars

        known = _extension_grammars()
        installed_extensions = {
            extension
            for extension, modules in known.items()
            if all(importlib.util.find_spec(m) is not None for m in modules)
        }
        expected = sum(
            1
            for path in self.fixture.iterdir()
            if path.suffix.lower() in installed_extensions
        )

        result = self.benchmark.measure("fixture:polyglot", self.fixture)

        self.assertEqual(result.error, "", result.error)
        self.assertGreaterEqual(
            result.files_covered,
            expected,
            "a language whose grammar is installed produced no nodes; "
            f"covered {result.files_covered} of an expected {expected}",
        )

    def test_a_missing_grammar_is_reported_rather_than_hidden(self) -> None:
        # The other half. Whatever is absent has to be *named*, because the
        # original failure was not that files were skipped -- it was that
        # nothing said so in a form any later command could read.
        result = self.benchmark.measure("fixture:polyglot", self.fixture)
        uncovered = result.files_supported - result.files_covered
        if uncovered:
            self.assertTrue(
                result.missing_grammars,
                f"{uncovered} file(s) contributed nothing and no grammar was named",
            )

    def test_files_with_no_extractor_do_not_count_against_coverage(self) -> None:
        # `notes.txt` has no extractor at all. Counting it as uncovered would
        # make the metric punish a repository for containing prose.
        result = self.benchmark.measure("fixture:polyglot", self.fixture)
        self.assertLess(result.files_supported, result.files_seen)


if __name__ == "__main__":
    unittest.main()


@unittest.skipUnless(_HAS_CODE_EXTRA, "needs the `code` extra's grammars")
class SpawnedWorkerTest(unittest.TestCase):
    """A worker pool must resolve the extractor, or say so — never go quiet.

    The pool pickles work by qualified name (`graphify.extract.extract_python`)
    and `graphify` is a synthetic alias registered at runtime. Under `spawn`
    the child inherits `sys.path` but not `sys.modules`, so without a real
    package on that path every worker died and its files produced nothing —
    counted as coverage, never as an error.
    """

    def test_a_fresh_interpreter_can_resolve_the_vendored_extractor(self) -> None:
        # A subprocess with none of this process's imports is exactly what a
        # spawned worker is. `-I` isolates it further: no site customisations,
        # no inherited PYTHONPATH beyond what we pass.
        import subprocess

        from brainkit.infrastructure.extractor import _enable_parallel_workers

        self.assertTrue(_enable_parallel_workers(), "no import shim was installed")
        probe = (
            "import sys; sys.path[:] = " + repr(sys.path) + "\n"
            "from graphify.extract import extract_python\n"
            "print(extract_python.__module__)\n"
        )
        result = subprocess.run(
            [sys.executable, "-I", "-c", probe], capture_output=True, text=True
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "graphify.extract")

    def test_the_parallel_path_covers_what_the_sequential_path_covers(self) -> None:
        # The regression, stated as an equality. Anything that breaks workers
        # again shows up as a difference between these two numbers rather than
        # as a plausible-looking coverage percentage.
        import os
        from tempfile import TemporaryDirectory

        import run as benchmark

        with TemporaryDirectory() as scratch:
            fixture = Path(scratch) / "polyglot"
            benchmark.build_fixture(fixture)
            parallel = benchmark.measure("parallel", fixture)
            os.environ["BK_NO_PARALLEL"] = "1"
            try:
                sequential = benchmark.measure("sequential", fixture)
            finally:
                del os.environ["BK_NO_PARALLEL"]

        self.assertEqual(parallel.error, "", parallel.error)
        self.assertEqual(
            parallel.files_covered,
            sequential.files_covered,
            "the worker pool lost files the sequential path found",
        )
        self.assertEqual(parallel.nodes, sequential.nodes)
