"""Every code-graph traversal must carry a staleness signal.

`staleness()` is honest and correct, and nothing consulted it. `hubs`,
`affected`, `path`, `communities`, `cycles`, `diff` and `data` all go through
`_read()`, which checks the privacy boundary and then returns whatever is on
disk. Reproduced on this project's own vault: `code status` reported `stale`
with 41 removed files while `code hubs` returned `src/brainkit/domain/model.py`
with an exact line number -- a path that ceased to exist at the rename.

An agent asking "what is load-bearing here" was answered authoritatively about
a tree that is gone.

The answer is disclosure, not refusal: a rebuild in progress is a legitimate
state, and refusing would break the workflow the signal exists to inform.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from brainskit.application.services import BrainskitService
from brainskit.infrastructure.index import SqliteFtsIndex
from brainskit.infrastructure.vault import FileVault

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_engine import policy


def _node(node_id: str, label: str, path: str, line: int = 1) -> dict:
    return {
        "id": node_id,
        "label": label,
        "source_file": path,
        "source_location": f"L{line}",
        "file_type": "code",
    }


TRAVERSALS = ("affected", "path", "hubs", "communities", "cycles", "diff", "data")


class CodeGraphStalenessTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.vault = FileVault.initialize(self.root / "vault", policy())
        self.service = BrainskitService(
            self.vault, SqliteFtsIndex(self.vault.index_path)
        )
        # A graph describing a file that no longer exists: exactly the state the
        # rename left this project's own vault in.
        self.service.code_import(
            {
                "nodes": [
                    _node("n1", "Gone", "src/gone/model.py"),
                    _node("n2", "AlsoGone", "src/gone/service.py"),
                ],
                "links": [
                    {
                        "source": "n1",
                        "target": "n2",
                        "relation": "imports_from",
                        "source_file": "src/gone/model.py",
                        "source_location": "L2",
                    }
                ],
            }
        )

    def results(self) -> dict[str, dict]:
        return {
            "affected": self.service.code_affected("Gone"),
            "path": self.service.code_path("n1", "n2"),
            "hubs": self.service.code_hubs(top=5),
            "communities": self.service.code_communities(),
            "cycles": self.service.code_cycles(),
            # `diff` is deliberately absent: it re-extracts in order to compare,
            # so it needs a configured extractor and cannot answer from the
            # stored artifact alone. It is the one traversal that cannot be
            # stale by construction.
            "data": self.service.code_graph_data(),
        }

    def test_status_agrees_the_graph_is_stale(self) -> None:
        """Control: the fixture genuinely is in the state under test."""

        self.assertNotEqual(self.service.code_status()["state"], "fresh")

    def test_every_traversal_reports_staleness(self) -> None:
        for name, payload in self.results().items():
            with self.subTest(traversal=name):
                self.assertIn(
                    "staleness",
                    payload,
                    msg=f"{name} answered with no staleness signal",
                )
                self.assertNotEqual(payload["staleness"].get("state"), "fresh")

    def test_data_reports_a_state_rather_than_none(self) -> None:
        """`data()["state"]` read a key `_write` never stores, so it was always None."""

        self.assertIsNotNone(self.service.code_graph_data().get("state"))

    def test_the_signal_is_serialisable(self) -> None:
        """These payloads go out over --json and the web API."""

        json.dumps(self.results())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
