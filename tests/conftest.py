"""pytest's entry into the suite's harness.

The machine isolation this file used to own lives in `_harness.py`, because
only pytest imports conftest and `python -m unittest` bypassed it. Importing
the harness here still runs it before pytest imports any test module.
"""

from __future__ import annotations

import _harness  # noqa: F401
