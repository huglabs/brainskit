"""Import-alias shim for the vendored Graphify subset.

Everything else in this directory is vendored source, kept byte-identical to
upstream Graphify — see `NOTICE`. This file is not vendored; it is Brainkit's
own, and it is the only file here that may ever change.

The problem
-----------

The vendored modules import each other by Graphify's own absolute paths —
`from graphify.extractors.models import ...`, `from graphify.ids import ...` —
because that is how they import inside Graphify's package. Renaming those
imports to `brainkit.infrastructure.codeanalysis....` would be an edit, and
the NOTICE is explicit that nothing in this directory is edited in place: a
future re-vendor has to be a copy, not a merge.

The fix: make `graphify` resolve, don't make the vendored files stop asking
for it. This module registers a synthetic top-level package named `graphify`
in `sys.modules`, whose search path (`__path__`) points at this directory.
Python's import machinery then finds `graphify.extract`, `graphify.ids`,
`graphify.extractors.base`, and every other vendored module right here — the
same as if upstream Graphify were actually installed.

Why a new module object rather than aliasing this package itself
------------------------------------------------------------------

The tempting shortcut is `sys.modules["graphify"] = sys.modules[__name__]` —
alias the name to this very package. That reuses this package's real dotted
identity (`brainkit.infrastructure.codeanalysis`) as `graphify` too, which
causes two problems: every vendored module's `__name__`/`__package__` would
lie about where it lives, and — the one that actually bites — anything that
later imported a vendored module by this package's real path (e.g.
`brainkit.infrastructure.codeanalysis.extract`) instead of through the alias
would get a *second*, independently-imported copy of the same file: its own
module object, its own module-level caches (`extract.py` has several),
possibly its own classes. Two copies of "the same" module is exactly the
ghost-duplication bug class `graphify.ids`'s own docstring warns about, just
moved from node ids to Python module identity.

A brand-new `ModuleType` sidesteps that: it is unambiguously `graphify`, and
as long as every caller reaches these modules through `graphify.*` — never
through `brainkit.infrastructure.codeanalysis.<vendored module>` — each
vendored file is imported exactly once, under one name. That discipline is
this module's job to establish and everyone else's job to keep: nothing
outside this file should import a sibling module here by its real dotted
path.

Cost and safety
----------------

Installing the alias only touches `sys.modules`; it imports nothing and costs
nothing. It is safe to run whether or not the `code` extra is installed,
because it never imports a vendored module itself — it only makes them
importable. Whoever imports `graphify.extract` next (`infrastructure/
extractor.py`) is the one on the hook for making that lazy and for turning a
missing `tree-sitter` into a clear Brainkit error; see that module for how.

Registration is idempotent: importing this package twice — which Python's own
module cache already prevents, but the check is cheap insurance against
anything that re-execs this file directly — leaves the existing alias alone
rather than replacing it with a second, disconnected `graphify` module.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

_GRAPHIFY = "graphify"
_VENDOR_DIR = Path(__file__).resolve().parent


def _install_graphify_alias() -> types.ModuleType:
    installed = sys.modules.get(_GRAPHIFY)
    if installed is not None:
        return installed
    alias = types.ModuleType(_GRAPHIFY)
    alias.__path__ = [str(_VENDOR_DIR)]  # type: ignore[attr-defined]
    alias.__package__ = _GRAPHIFY
    sys.modules[_GRAPHIFY] = alias
    return alias


_install_graphify_alias()

# `graphify.ids` is pure stdlib (`re` + `unicodedata`, see that file) — trivial
# to import even without the `code` extra, so re-exporting it here costs
# nothing. `application/codegraph.py` imports it from here rather than
# copying `normalize_id`: the recipe has to stay byte-identical to the
# extractor's own, and the only way to guarantee that without a second,
# hand-maintained copy is to import the one that exists.
from graphify.ids import normalize_id as normalize_id  # noqa: E402
