"""Import-alias shim for the vendored Graphify subset.

Everything else in this directory is vendored source, kept byte-identical to
upstream Graphify — see `NOTICE`. This file is not vendored; it is Brainskit's
own, and it is the only file here that may ever change.

The problem
-----------

The vendored modules import each other by Graphify's own absolute paths —
`from graphify.extractors.models import ...`, `from graphify.ids import ...` —
because that is how they import inside Graphify's package. Renaming those
imports to `brainskit.infrastructure.codeanalysis....` would be an edit, and
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
identity (`brainskit.infrastructure.codeanalysis`) as `graphify` too, which
causes two problems: every vendored module's `__name__`/`__package__` would
lie about where it lives, and — the one that actually bites — anything that
later imported a vendored module by this package's real path (e.g.
`brainskit.infrastructure.codeanalysis.extract`) instead of through the alias
would get a *second*, independently-imported copy of the same file: its own
module object, its own module-level caches (`extract.py` has several),
possibly its own classes. Two copies of "the same" module is exactly the
ghost-duplication bug class `graphify.ids`'s own docstring warns about, just
moved from node ids to Python module identity.

A brand-new `ModuleType` sidesteps that: it is unambiguously `graphify`, and
as long as every caller reaches these modules through `graphify.*` — never
through `brainskit.infrastructure.codeanalysis.<vendored module>` — each
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
missing `tree-sitter` into a clear Brainskit error; see that module for how.

Registration is idempotent: importing this package twice — which Python's own
module cache already prevents, but the check is cheap insurance against
anything that re-execs this file directly — leaves the existing alias alone
rather than replacing it with a second, disconnected `graphify` module.

Idempotency is not "the name is taken"
--------------------------------------

That check used to accept *any* `sys.modules["graphify"]`, which is a wider
claim than it meant to make: upstream Graphify is a real, installable
distribution, so the name can already be bound to a genuinely foreign package
rather than to a previous run of this file. Yielding to it is not degradation,
it is a fork of the extractor — and it fails in both available directions.

Loudly, when the foreign package lacks a module this tree has: the very next
line here imports `graphify.ids`, so `import graphify` before this package
turned the whole import into `ModuleNotFoundError: No module named
'graphify.ids'`, which names neither the conflict nor its cause.

Quietly, when it does not: `normalize_id` below is imported rather than copied
precisely because the recipe has to stay byte-identical to the extractor's own.
A foreign `graphify.ids` satisfies that import and silently supplies a
*different* recipe, so node ids stop matching the ones the vendored extractors
build. Same bug class as the double-import above, sourced from another
distribution instead of another path.

Refusing rather than overriding is deliberate. One process binds a top-level
name to exactly one package, and by the time this runs the foreign package's
submodules may already be in `sys.modules`; replacing only the parent would
leave `graphify.extract` resolving to their file and ours to this directory,
which is the fork rather than a repair of it. Evicting them instead would break
whatever imported them, mid-flight, to take a name we do not own. The conflict
is environmental and the operator is the one who can resolve it, so this says
so and stops.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

from brainskit.domain.model import NotConfiguredError

_GRAPHIFY = "graphify"
_VENDOR_DIR = Path(__file__).resolve().parent


def _is_our_alias(module: types.ModuleType) -> bool:
    """True when this module already resolves submodules to *this* directory.

    The identity that matters is the search path, not a marker attribute: two
    Brainskit installs in one process would both set any marker we invented
    while pointing at different vendored trees, and the wrong tree is the same
    fork as a foreign package.
    """

    return str(_VENDOR_DIR) in getattr(module, "__path__", ())


def _install_graphify_alias() -> types.ModuleType:
    installed = sys.modules.get(_GRAPHIFY)
    if installed is not None:
        if _is_our_alias(installed):
            return installed
        raise NotConfiguredError(
            "Another 'graphify' package already owns the import name this "
            "vault's vendored extractors resolve through",
            details={
                "vendored": str(_VENDOR_DIR),
                "occupied_by": _describe(installed),
                "remedy": (
                    "Uninstall the standalone graphify distribution from this "
                    "environment, or keep it out of any process that uses "
                    "`bk code`. One process binds the top-level name "
                    "'graphify' to exactly one package, and overriding it "
                    "here would fork the extractor rather than repair it."
                ),
            },
        )
    alias = types.ModuleType(_GRAPHIFY)
    alias.__path__ = [str(_VENDOR_DIR)]  # type: ignore[attr-defined]
    alias.__package__ = _GRAPHIFY
    sys.modules[_GRAPHIFY] = alias
    return alias


def _describe(module: types.ModuleType) -> str:
    """Where the occupying package lives, for an operator who has to find it.

    A namespace package has no `__file__` and a synthetic one has neither, so
    fall back through `__path__` to the name rather than reporting `None`.
    """

    file = getattr(module, "__file__", None)
    if file:
        return str(file)
    path = list(getattr(module, "__path__", ()))
    return str(path[0]) if path else repr(module)


_install_graphify_alias()

# `graphify.ids` is pure stdlib (`re` + `unicodedata`, see that file) — trivial
# to import even without the `code` extra, so re-exporting it here costs
# nothing. `application/codegraph.py` imports it from here rather than
# copying `normalize_id`: the recipe has to stay byte-identical to the
# extractor's own, and the only way to guarantee that without a second,
# hand-maintained copy is to import the one that exists.
from graphify.ids import normalize_id as normalize_id  # noqa: E402
