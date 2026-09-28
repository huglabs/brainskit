"""The suite's harness: a throwaway machine configuration, and the one CLI runner.

Every test module imports this first, before anything from `brainskit`::

    try:
        from . import _harness
    except ImportError:
        import _harness

The two spellings are the two ways a test file gets loaded: as `tests.test_x`
(`python -m unittest tests.test_x`, where only the package-relative form
resolves) and as a top-level `test_x` (pytest, `python tests/test_x.py`,
`unittest discover -s tests`, where only the plain form does).
`test_harness.py` fails the suite if a test module stops doing this.

Machine isolation
-----------------

`bk init` registers the vault it creates in this machine's vault registry
(`interfaces/cli.py::_register_new_vault`). That is the right behaviour for an
operator -- it was added because `bk init` was the only way to create a vault
and the only path that never registered one -- and the wrong one for a test:
the suite creates vaults under `TemporaryDirectory` and never unregisters them,
so the operator's `~/.config/brainskit/vaults.json` accumulated dozens of
`/var/folders/.../T/tmp*/v` entries pointing at directories deleted the moment
the test that made them finished.

Isolating the environment rather than adding a test-only opt-out is deliberate.
The registration is a product decision made in the CLI, not a `FileVault`
behaviour, so an `initialize(..., register=False)` affordance would guard
nothing; and a `--no-register` flag would put a test-only escape hatch in the
operator's command surface while still relying on every caller to remember it.
The call sites that reach the registry are not the ones a test author expects:
`bk web` on an empty directory offers to run `init` and does, from
`test_fix_interfaces.py`, which never mentions the registry at all.

`XDG_CONFIG_HOME` is the seam `infrastructure/vaults.py::config_home` already
reads, so nothing in the product has to know it is under test, and the
isolation covers every machine-wide config file that honours the same
variable. `RegistryIsolationTest` in `test_vault_registry.py` pins the
invariant.

It lives here rather than in `conftest.py` because only pytest imports
conftest: `python tests/test_x.py` and `python -m unittest` used to run
against the operator's real `~/.config`. It is set at import rather than in a
fixture so a module that touches the registry at import time is covered too.

The CLI runner
--------------

`run_cli` is the only way the suite drives `bk` in-process. It refuses a run
whose output shows the CLI failed before reaching the code under test -- no
vault, an unhandled error, a traceback -- because every assertion on such a run
compares two identical failures rather than two behaviours. `bk` exits 2 for
"not a vault" and 2 for "denied", so a test asserting a denial passes on a
broken fixture. A test that provokes one of these on purpose names it in
`expect=`, where it is visible and must actually occur.
"""

from __future__ import annotations

import atexit
import io
import json
import os
import shutil
import tempfile
from collections.abc import Iterable, Sequence
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass
from pathlib import Path
from typing import Any

#: Carries the throwaway directory to child processes and to a second copy of
#: this module (loaded once as `tests._harness`, once as `_harness`), so both
#: reuse it. An operator's own `XDG_CONFIG_HOME` has no marker and is replaced.
ISOLATION_MARKER = "BRAINSKIT_TEST_CONFIG_HOME"


def _isolate() -> str:
    inherited = os.environ.get(ISOLATION_MARKER, "")
    if inherited and os.environ.get("XDG_CONFIG_HOME") == inherited:
        return inherited
    # Absolute, as the XDG basedir specification requires -- a relative value
    # is ignored by `config_home` and falls straight back to the real
    # `~/.config`, which is the state this exists to prevent.
    home = tempfile.mkdtemp(prefix="brainskit-test-config-")
    atexit.register(shutil.rmtree, home, ignore_errors=True)
    os.environ["XDG_CONFIG_HOME"] = home
    os.environ[ISOLATION_MARKER] = home
    return home


CONFIG_HOME = _isolate()

VACUITY_MARKERS = (
    "Not a brainskit vault",
    "No brainskit vault found",
    "unhandled internal error",
    "Traceback",
)


@dataclass(frozen=True)
class CliRun:
    code: int
    stdout: str
    stderr: str
    #: The CLI raised `SystemExit` (argparse, `--help`) instead of returning.
    exited: bool

    @property
    def output(self) -> str:
        return self.stdout + self.stderr

    def json(self) -> Any:
        return json.loads(self.stdout)


def refuse_vacuous(output: str, *, expect: Iterable[str] = ()) -> None:
    """Raise unless `output` came from a CLI that reached the code under test."""

    expected = set(expect)
    unknown = expected - set(VACUITY_MARKERS)
    if unknown:
        raise ValueError(f"not a vacuity marker: {sorted(unknown)}")
    for marker in VACUITY_MARKERS:
        if marker in expected and marker not in output:
            raise AssertionError(
                f"expect= names {marker!r} but the run never produced it; "
                f"the opt-out is stale.\n{output}"
            )
        if marker not in expected and marker in output:
            raise AssertionError(
                f"the CLI never reached the code under test ({marker!r}), so any "
                f"assertion on this run is vacuous. If the test provokes it on "
                f"purpose, pass expect=({marker!r},).\n{output}"
            )


def run_cli(
    argv: Sequence[str],
    *,
    cwd: Path | str | None = None,
    expect: Iterable[str] = (),
) -> CliRun:
    """Run `bk` in-process with its output captured, refusing a vacuous run."""

    from brainskit.interfaces import cli

    out, err = io.StringIO(), io.StringIO()
    previous = os.getcwd()
    if cwd is not None:
        os.chdir(cwd)
    try:
        with redirect_stdout(out), redirect_stderr(err):
            try:
                code, exited = cli.main(list(argv)), False
            except SystemExit as exit_:
                code, exited = int(exit_.code or 0), True
    finally:
        os.chdir(previous)
    run = CliRun(code, out.getvalue(), err.getvalue(), exited)
    refuse_vacuous(run.output, expect=expect)
    return run
