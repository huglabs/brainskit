"""The serving docs must list the endpoints the server actually registers.

`docs/serving.md` and `docs/architecture.md` both called the web API read-only.
`do_POST` handles four endpoints, one of which routes through the apply gate and
writes `wiki/`. The documented GET list also omitted `/api/code-graph`, so the
real read surface was eleven, not ten.

Compared against the source rather than restated, the same way
`docs/commands.md` is checked -- the audit found zero wrong command names across
32 commands there, which is what a generated-or-tested list buys.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "src" / "brainskit" / "interfaces" / "web.py"
SERVING = ROOT / "docs" / "serving.md"


def _handled(method: str) -> set[str]:
    """Endpoints compared inside `do_GET` / `do_POST`."""

    source = WEB.read_text(encoding="utf-8")
    start = source.index(f"def {method}(")
    end = len(source)
    for other in ("do_GET", "do_POST", "do_HEAD", "def log_message"):
        marker = f"def {other}("
        position = source.find(marker, start + 1)
        if position != -1:
            end = min(end, position)
    return set(re.findall(r'parsed\.path == "(/api/[a-z/-]+)"', source[start:end]))


class ServingDocsTruthTest(unittest.TestCase):
    def documented(self) -> set[str]:
        # Anywhere in the prose, not only where a code span starts with the
        # path: the write table spells them `POST /api/capture`.
        return set(re.findall(r"(/api/[a-z/-]+)", SERVING.read_text(encoding="utf-8")))

    def test_every_get_endpoint_is_documented(self) -> None:
        missing = sorted(_handled("do_GET") - self.documented())
        self.assertEqual(missing, [], msg="undocumented read endpoints")

    def test_every_write_endpoint_is_documented(self) -> None:
        missing = sorted(_handled("do_POST") - self.documented())
        self.assertEqual(missing, [], msg="undocumented write endpoints")

    def test_the_source_is_actually_scanned(self) -> None:
        """Control: an empty scan would make both assertions vacuous."""

        self.assertGreaterEqual(len(_handled("do_GET")), 10)
        self.assertGreaterEqual(len(_handled("do_POST")), 4)

    def test_the_docs_do_not_call_the_api_read_only(self) -> None:
        text = SERVING.read_text(encoding="utf-8")
        for line in text.splitlines():
            if "read-only" in line:
                # The one permitted mention is the sentence saying it is *not*.
                self.assertIn("not a read-only", line)

    def test_the_write_guard_is_documented_as_the_consumer(self) -> None:
        """What protects the writes is `--consumer human`, not the method."""

        text = SERVING.read_text(encoding="utf-8")
        self.assertIn("writes_refused", text)
        self.assertIn("--consumer human", text)

    def test_the_optional_token_is_documented(self) -> None:
        """`_authorised` returns True when no token is configured."""

        self.assertIn("optional", SERVING.read_text(encoding="utf-8"))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
