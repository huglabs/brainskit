"""A claim about code, pinned to the bytes it was true of.

Citing code by path and line number is a promise that decays silently: the next
commit moves the line, and the page keeps asserting it with no way to notice.
Citing it by content hash makes the decay observable — `lint` re-reads the file,
compares, and says so.

That is the entire design, and it is deliberately not a new mechanism. The hash
is the same SHA-256 `capture` gives a raw source, the failure lands in the same
`review` queue a superseded source produces, and the declare-then-cite rule is
the one `sources`/`[^source:]` already follows. What is new is only that the
bytes stay in the repository instead of being copied into `raw/` — a repository
is a moving thing to point at, not evidence to preserve.
"""

from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path
from typing import Any

from test_projections import policy

from brainkit.application.services import BrainkitService
from brainkit.domain.model import (
    CODE_CITATION_RE,
    CodeSource,
    PageOperation,
    ValidationError,
)
from brainkit.infrastructure.graph import MarkdownGraph
from brainkit.infrastructure.index import SqliteFtsIndex
from brainkit.infrastructure.vault import FileVault


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class CitationSyntaxTest(unittest.TestCase):
    def test_matches_a_bare_citation(self) -> None:
        digest = sha256("x")
        self.assertEqual(CODE_CITATION_RE.findall(f"claim [^code:{digest}]"), [digest])

    def test_line_ranges_do_not_change_which_source_is_cited(self) -> None:
        # Two claims about different lines of one file are two claims about the
        # same source, so the range sits outside the captured group.
        digest = sha256("x")
        for suffix in ("#L17", "#L17-L42", "#L17-42"):
            self.assertEqual(
                CODE_CITATION_RE.findall(f"a [^code:{digest}{suffix}] b"),
                [digest],
                suffix,
            )

    def test_ignores_a_source_citation(self) -> None:
        self.assertEqual(CODE_CITATION_RE.findall(f"[^source:{sha256('x')}]"), [])

    def test_ignores_a_malformed_hash(self) -> None:
        self.assertEqual(CODE_CITATION_RE.findall("[^code:not-a-hash]"), [])
        self.assertEqual(CODE_CITATION_RE.findall("[^code:abc123]"), [])


class CodeSourceTest(unittest.TestCase):
    def test_rejects_a_path_that_escapes_the_code_root(self) -> None:
        for path in ("../secrets.env", "a/../../b.py", "/etc/passwd"):
            with self.assertRaises(ValidationError, msg=path):
                CodeSource(path=path, content_hash=sha256("x"))

    def test_rejects_a_hash_that_is_not_sha256(self) -> None:
        with self.assertRaises(ValidationError):
            CodeSource(path="a.py", content_hash="deadbeef")

    def test_round_trips_through_a_dict(self) -> None:
        source = CodeSource(path="src/db.ts", content_hash=sha256("x"))
        self.assertEqual(CodeSource.from_dict(source.to_dict()), source)

    def test_names_what_a_malformed_entry_is_missing(self) -> None:
        with self.assertRaises(ValidationError) as caught:
            CodeSource.from_dict({"path": "a.py"})
        self.assertEqual(caught.exception.details["missing"], ["hash"])


class OperationValidationTest(unittest.TestCase):
    def operation(self, body: str, code_sources: list[dict[str, str]]) -> PageOperation:
        return PageOperation.from_dict(
            {
                "action": "upsert",
                "kind": "concept",
                "slug": "db",
                "title": "Db",
                "body": body,
                "source_hashes": [],
                "links": [],
                "metadata": {},
                "base_hash": None,
                "code_sources": code_sources,
            }
        )

    def test_accepts_a_declared_citation(self) -> None:
        digest = sha256("code")
        operation = self.operation(
            f"Pools connections [^code:{digest}#L17-L42].",
            [{"path": "src/db.ts", "hash": digest}],
        )
        self.assertEqual(operation.code_sources[0].path, "src/db.ts")

    def test_refuses_a_citation_nothing_declares(self) -> None:
        # Undeclared means unverifiable: nothing records where the file lives,
        # so lint could never re-read it.
        with self.assertRaises(ValidationError) as caught:
            self.operation(f"Claim [^code:{sha256('code')}].", [])
        self.assertIn("undeclared", caught.exception.details)

    def test_refuses_two_entries_for_one_path(self) -> None:
        digest, other = sha256("a"), sha256("b")
        with self.assertRaises(ValidationError):
            self.operation(
                f"[^code:{digest}] [^code:{other}]",
                [
                    {"path": "src/db.ts", "hash": digest},
                    {"path": "src/db.ts", "hash": other},
                ],
            )

    def test_code_sources_cannot_be_smuggled_through_metadata(self) -> None:
        with self.assertRaises(ValidationError):
            PageOperation.from_dict(
                {
                    "action": "upsert",
                    "kind": "concept",
                    "slug": "db",
                    "title": "Db",
                    "body": "Body.",
                    "source_hashes": [],
                    "links": [],
                    "metadata": {"code_sources": [{"path": "x", "hash": sha256("x")}]},
                    "base_hash": None,
                }
            )

    def test_a_page_with_no_code_sources_is_unchanged(self) -> None:
        operation = self.operation("Plain body.", [])
        self.assertEqual(operation.code_sources, ())


class LifecycleTest(unittest.TestCase):
    """Capture nothing, cite a repository file, then change it."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.repo = self.root / "repo"
        (self.repo / "src").mkdir(parents=True)
        (self.repo / ".git").mkdir()
        self.code = self.repo / "src" / "db.ts"
        self.code.write_text("export class Db {\n  connect() {}\n}\n", encoding="utf-8")

        # The vault sits inside the repository it documents, which is the layout
        # `code_root` discovery is built for.
        vault = FileVault.initialize(self.repo / "docs" / "brain", policy())
        self.vault = vault
        self.service = BrainkitService(
            vault, SqliteFtsIndex(vault.index_path), graph=MarkdownGraph()
        )
        self.service.reindex()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def digest(self) -> str:
        return sha256(self.code.read_text(encoding="utf-8"))

    def apply_page(self) -> str:
        digest = self.digest()
        self.service.apply(
            {
                "operations": [
                    {
                        "action": "upsert",
                        "kind": "concept",
                        "slug": "db",
                        "title": "Db",
                        "body": f"Db owns pooling [^code:{digest}#L1-L3].",
                        "source_hashes": [],
                        "links": [],
                        "metadata": {},
                        "base_hash": None,
                        "code_sources": [{"path": "src/db.ts", "hash": digest}],
                    }
                ]
            }
        )
        return "wiki/concepts/db.md"

    def codes(self) -> list[str]:
        return [item["code"] for item in self.service.lint()["findings"]]

    def test_discovers_the_repository_as_the_code_root(self) -> None:
        # Resolved on both sides: macOS hands out `/var/...` temporary paths
        # that are symlinks to `/private/var/...`.
        self.assertEqual(self.vault.code_root().resolve(), self.repo.resolve())

    def test_a_current_citation_is_clean(self) -> None:
        self.apply_page()
        self.assertNotIn("wiki.stale_code_citation", self.codes())
        self.assertNotIn("wiki.missing_code_source", self.codes())

    def test_the_citation_survives_a_round_trip_through_frontmatter(self) -> None:
        path = self.apply_page()
        written = self.vault.read_text(path)
        self.assertIn('"path": "src/db.ts"', written)
        self.assertIn(self.digest(), written)

    def test_changing_the_file_ages_the_page(self) -> None:
        path = self.apply_page()
        self.code.write_text("export class Db {\n  // rewritten\n}\n", encoding="utf-8")

        self.assertIn("wiki.stale_code_citation", self.codes())
        entry = self.vault.read_state("freshness")["pages"][path]
        self.assertEqual(entry["status"], "review")
        self.assertIn("src/db.ts", entry["review_reason"])

    def test_drift_is_a_warning_and_not_a_broken_vault(self) -> None:
        self.apply_page()
        self.code.write_text("changed\n", encoding="utf-8")
        result = self.service.lint()
        self.assertTrue(result["ok"], "drift is news, not corruption")

    def test_deleting_the_file_is_reported_as_missing_not_stale(self) -> None:
        self.apply_page()
        self.code.unlink()
        codes = self.codes()
        self.assertIn("wiki.missing_code_source", codes)
        self.assertNotIn("wiki.stale_code_citation", codes)

    def test_a_hand_written_citation_with_no_declaration_is_caught(self) -> None:
        # The apply gate refuses this, so reaching lint means someone edited the
        # file directly — which is exactly when lint has to be the backstop.
        path = self.apply_page()
        page = self.vault.root / path
        # Written straight to disk, because that is what "someone edited it by
        # hand" means — going through the gate is the case that already passes.
        page.write_text(
            page.read_text(encoding="utf-8") + f"\n\nMore [^code:{sha256('other')}].\n",
            encoding="utf-8",
        )
        self.assertIn("wiki.undeclared_code_citation", self.codes())

    def test_the_apply_gate_refuses_a_citation_nothing_declares(self) -> None:
        digest = sha256("unrelated")
        with self.assertRaises(ValidationError):
            self.service.apply(
                {
                    "operations": [
                        {
                            "action": "upsert",
                            "kind": "concept",
                            "slug": "other",
                            "title": "Other",
                            "body": f"Claim [^code:{digest}].",
                            "source_hashes": [],
                            "links": [],
                            "metadata": {},
                            "base_hash": None,
                            "code_sources": [],
                        }
                    ]
                }
            )

    def test_a_declared_source_nobody_cites_is_refused(self) -> None:
        failures = self._apply_expecting_failure(
            body="No citation here.",
            code_sources=[{"path": "src/db.ts", "hash": self.digest()}],
        )
        self.assertIn("code_citation_mismatch", failures)

    def _apply_expecting_failure(
        self, *, body: str, code_sources: list[dict[str, str]]
    ) -> list[str]:
        try:
            result: Any = self.service.apply(
                {
                    "operations": [
                        {
                            "action": "upsert",
                            "kind": "concept",
                            "slug": "other",
                            "title": "Other",
                            "body": body,
                            "source_hashes": [],
                            "links": [],
                            "metadata": {},
                            "base_hash": None,
                            "code_sources": code_sources,
                        }
                    ]
                }
            )
        except ValidationError as exc:
            failures = exc.details.get("failures", [])
            return [str(item.get("code")) for item in failures]
        return [str(item.get("code")) for item in result.get("failures", [])]
