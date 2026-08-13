"""The suite runs against a throwaway machine configuration, never the real one.

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

`XDG_CONFIG_HOME` is the seam `infrastructure/vaults.py::default_registry_path`
already reads, so nothing in the product has to know it is under test, and the
isolation covers every future machine-wide config file that honours the same
variable. The invariant this buys is pinned by `RegistryIsolationTest` in
`test_vault_registry.py`, so deleting this file fails the suite instead of
quietly resuming the pollution.

Set at import rather than in a fixture: conftest is imported before any test
module, so a module that touches the registry at import time is covered too.
"""

from __future__ import annotations

import os
import tempfile

_CONFIG_HOME = tempfile.TemporaryDirectory(prefix="brainskit-test-config-")

# Absolute, as the XDG basedir specification requires -- a relative value is
# ignored by `default_registry_path` and would fall straight back to the real
# `~/.config`, which is the state this exists to prevent.
os.environ["XDG_CONFIG_HOME"] = _CONFIG_HOME.name


def pytest_unconfigure() -> None:
    """A pytest hook may take a subset of its arguments; this one needs none."""

    _CONFIG_HOME.cleanup()
