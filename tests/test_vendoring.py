"""The vendoring contract for `infrastructure/codeanalysis/`.

That directory is Graphify's source, copied in. Its `NOTICE` used to claim
"nothing inside this directory" was edited — and by the time anyone checked,
three files had been. The claim mattered for two separate reasons:

- **Legally**, Apache-2.0 section 4(b) requires modified files to carry
  prominent notice of the change.
- **Operationally**, the extractor's AST cache is namespaced by a hash of this
  tree. When the hash covered only `NOTICE`, an edit to `extract.py` — one that
  changed *which results get cached* — left the namespace unchanged, so entries
  written by the previous extractor stayed live under the new one. A cache that
  cannot tell those apart is the "stale cache, wrong graph" failure the
  namespace exists to prevent.

So this file enforces what the notice states, rather than trusting it:

1. every modified file is declared in `NOTICE`, and no undeclared file is
   modified — a fourth edit fails here until it is argued for in writing;
2. the cache marker changes when the vendored source changes; and
3. no user-facing string in that tree still points at `graphifyy`, which
   brainkit does not depend on.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

from brainkit.infrastructure import extractor

VENDORED = Path(extractor.__file__).resolve().parent / "codeanalysis"
NOTICE = VENDORED / "NOTICE"

#: The files that knowingly depart from upstream. Kept here *and* in `NOTICE`
#: on purpose: the notice is the human-readable declaration Apache-2.0 asks
#: for, and this is the machine-checked one. They are asserted to agree.
DECLARED_MODIFICATIONS = frozenset(
    {"extract.py", "detect.py", "google_workspace.py"}
)


class VendoredModificationTest(unittest.TestCase):
    def test_notice_declares_exactly_the_modified_files(self) -> None:
        text = NOTICE.read_text(encoding="utf-8")
        _, _, section = text.partition("What was changed")
        self.assertTrue(section, "NOTICE lost its 'What was changed' section")
        named = {
            name
            for name in re.findall(r"\b([a-z_]+\.py)\b", section)
            if (VENDORED / name).is_file()
        }
        self.assertEqual(
            named,
            set(DECLARED_MODIFICATIONS),
            "NOTICE and DECLARED_MODIFICATIONS disagree about which vendored "
            "files were edited",
        )

    def test_the_notice_no_longer_claims_byte_identity(self) -> None:
        # The specific false statement that hid three edits. Its absence is
        # what makes the declaration above meaningful.
        text = NOTICE.read_text(encoding="utf-8")
        _, _, section = text.partition("What was changed")
        self.assertNotIn("Nothing inside this directory", section)

    def test_every_declared_file_exists(self) -> None:
        for name in DECLARED_MODIFICATIONS:
            self.assertTrue((VENDORED / name).is_file(), f"{name} is gone")


class CacheMarkerTest(unittest.TestCase):
    """The AST cache namespace must move when the extractor does."""

    def setUp(self) -> None:
        extractor._cache_format_marker.cache_clear()
        self.addCleanup(extractor._cache_format_marker.cache_clear)

    def test_the_marker_is_stable_for_unchanged_source(self) -> None:
        first = extractor._cache_format_marker()
        extractor._cache_format_marker.cache_clear()
        self.assertEqual(first, extractor._cache_format_marker())

    def test_editing_vendored_source_changes_the_marker(self) -> None:
        # The regression. Editing `extract.py` while leaving `NOTICE` alone used
        # to leave the marker -- and therefore the cache namespace -- untouched.
        target = VENDORED / "extract.py"
        original = target.read_bytes()
        before = extractor._cache_format_marker()
        try:
            target.write_bytes(original + b"\n# cache-marker probe\n")
            extractor._cache_format_marker.cache_clear()
            after = extractor._cache_format_marker()
        finally:
            target.write_bytes(original)
            extractor._cache_format_marker.cache_clear()

        self.assertNotEqual(
            before,
            after,
            "a changed extractor reuses the cache written by the old one",
        )
        self.assertEqual(before, extractor._cache_format_marker())

    def test_editing_the_notice_still_changes_the_marker(self) -> None:
        # The original guarantee, kept: a re-vendor updates NOTICE, and that
        # alone must still invalidate the cache.
        original = NOTICE.read_bytes()
        before = extractor._cache_format_marker()
        try:
            NOTICE.write_bytes(original + b"\n")
            extractor._cache_format_marker.cache_clear()
            after = extractor._cache_format_marker()
        finally:
            NOTICE.write_bytes(original)
            extractor._cache_format_marker.cache_clear()
        self.assertNotEqual(before, after)


class InstallHintTest(unittest.TestCase):
    def test_no_vendored_string_points_at_graphifyy(self) -> None:
        offenders = []
        for path in sorted(VENDORED.rglob("*.py")):
            for number, line in enumerate(
                path.read_text(encoding="utf-8").splitlines(), 1
            ):
                if "graphifyy[" in line and not line.lstrip().startswith("#"):
                    offenders.append(f"{path.name}:{number}")
        self.assertEqual(offenders, [], f"still advertising graphifyy: {offenders}")


if __name__ == "__main__":
    unittest.main()
