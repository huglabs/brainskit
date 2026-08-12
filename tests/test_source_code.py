"""Captured source code has to reach the index.

A vault that stores a file and cannot find it is worse than one that refuses it:
the capture reports success, the bytes are on disk, the hash is registered, and
`bk search` returns nothing. That is what happened to every programming language
before this module existed — the extractor keyed on a set of eight suffixes, none
of them code, and everything else fell through to a "no text converter available"
placeholder that was itself indexed in the file's place.

Two mechanisms, and both are needed:

- A suffix table, because `mimetypes` either has no answer (`.go`, `.rs`) or has
  a confidently wrong one (`.ts` is MPEG transport stream to it, not TypeScript).
  A media-type rule alone cannot fix either case.
- A `text/*` fallback, because no table enumerates every language anyone will
  capture, and a file that declares itself text should be read as text.

The tests below pin both, and pin the boundary: a real binary must still get the
placeholder, and `.env` must never be read as source.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from brainskit.infrastructure.converters import (
    PLAIN_TEXT_SUFFIXES,
    SOURCE_CODE_NAMES,
    SOURCE_CODE_TYPES,
    extract_document,
    guess_media_type,
    is_source_code,
)

PLACEHOLDER = "no text converter available"


class MediaTypeTest(unittest.TestCase):
    def test_corrects_the_types_mimetypes_gets_wrong(self) -> None:
        # `.ts` is the one that matters: mimetypes answers `video/mp2t`, so a
        # rule that trusted it would refuse every TypeScript file in the repo.
        self.assertEqual(guess_media_type("component.ts"), "text/x-typescript")
        self.assertEqual(guess_media_type("component.tsx"), "text/x-typescript")

    def test_names_the_types_mimetypes_has_none_for(self) -> None:
        self.assertEqual(guess_media_type("main.go"), "text/x-go")
        self.assertEqual(guess_media_type("lib.rs"), "text/x-rust")
        self.assertEqual(guess_media_type("schema.sql"), "text/x-sql")

    def test_reads_suffixless_build_files_by_name(self) -> None:
        self.assertEqual(guess_media_type("Dockerfile"), "text/x-dockerfile")
        self.assertEqual(guess_media_type("Makefile"), "text/x-makefile")

    def test_leaves_everything_else_to_mimetypes(self) -> None:
        self.assertEqual(guess_media_type("notes.md"), "text/markdown")
        self.assertEqual(guess_media_type("scan.pdf"), "application/pdf")

    def test_falls_back_to_the_caller_s_default(self) -> None:
        self.assertEqual(guess_media_type("mystery"), "application/octet-stream")
        self.assertEqual(
            guess_media_type("mystery", default="text/markdown"), "text/markdown"
        )

    def test_every_code_type_is_a_text_type(self) -> None:
        # The `text/*` fallback in `extract_document` is only coherent if this
        # holds: anything the table names is text by construction.
        for suffix, media_type in SOURCE_CODE_TYPES.items():
            self.assertTrue(
                media_type.startswith("text/"), f"{suffix} -> {media_type}"
            )
        for name, media_type in SOURCE_CODE_NAMES.items():
            self.assertTrue(media_type.startswith("text/"), f"{name} -> {media_type}")

    def test_never_claims_dotenv_is_source(self) -> None:
        # It is text, and reading it would put a secret in a searchable index.
        self.assertNotIn(".env", SOURCE_CODE_TYPES)
        self.assertFalse(is_source_code(".env"))
        self.assertFalse(is_source_code("production.env"))

    def test_tables_do_not_overlap_the_plain_text_set(self) -> None:
        self.assertEqual(SOURCE_CODE_TYPES.keys() & PLAIN_TEXT_SUFFIXES, set())


class ExtractionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def write(self, name: str, body: str | bytes) -> Path:
        path = self.root / name
        if isinstance(body, bytes):
            path.write_bytes(body)
        else:
            path.write_text(body, encoding="utf-8")
        return path

    def extract(self, name: str, body: str | bytes) -> tuple[str, bool]:
        path = self.write(name, body)
        return extract_document(path, guess_media_type(name))

    def test_reads_code_verbatim_and_does_not_call_it_derived(self) -> None:
        source = "export class MemoryDatabase {\n  connect() {}\n}\n"
        for name in ("db.ts", "db.py", "db.go", "db.rs", "db.sql", "Dockerfile"):
            text, derived = self.extract(name, source)
            self.assertEqual(text, source, name)
            self.assertFalse(derived, name)

    def test_reads_an_unlisted_language_through_the_text_fallback(self) -> None:
        # `.ahk` is in no table here and `mimetypes` calls it text/plain, so
        # this exercises the fallback rather than the suffix map.
        text, derived = extract_document(
            self.write("script.txtish", "uniqueTerm"), "text/plain"
        )
        self.assertEqual(text, "uniqueTerm")
        self.assertFalse(derived)

    def test_still_placeholders_a_real_binary(self) -> None:
        text, derived = self.extract("blob.bin", bytes(range(256)))
        self.assertIn(PLACEHOLDER, text)
        self.assertTrue(derived)

    def test_survives_code_that_is_not_valid_utf8(self) -> None:
        text, _ = self.extract("broken.py", b"x = '\xff\xfe'\n")
        self.assertIn("x =", text)

    def test_leaves_json_and_html_to_their_own_converters(self) -> None:
        text, derived = self.extract("data.json", '{"b":1,"a":2}')
        self.assertTrue(derived, "json is reformatted, so it is a conversion")
        self.assertIn('"b": 1', text)

        text, derived = self.extract("page.html", "<p>hello</p><script>x</script>")
        self.assertTrue(derived)
        self.assertNotIn("script", text)


class CaptureTest(unittest.TestCase):
    """The property that actually matters: capture, then find it."""

    def setUp(self) -> None:
        from test_projections import policy

        from brainskit.application.services import BrainskitService
        from brainskit.infrastructure.graph import MarkdownGraph
        from brainskit.infrastructure.index import SqliteFtsIndex
        from brainskit.infrastructure.vault import FileVault

        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        vault = FileVault.initialize(self.root / "vault", policy())
        self.service = BrainskitService(
            vault, SqliteFtsIndex(vault.index_path), graph=MarkdownGraph()
        )
        self.service.reindex()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def capture(self, name: str, body: str) -> None:
        path = self.workspace / name
        path.write_text(body, encoding="utf-8")
        self.service.capture(str(path))

    def hits(self, query: str) -> int:
        return int(self.service.search(query, consumer="local")["count"])

    def test_a_captured_symbol_is_findable(self) -> None:
        self.capture("db.ts", "export class MemoryDatabase { connect() {} }\n")
        self.capture("db.go", "package main\nfunc Connect() {}\n")
        self.capture("q.sql", "SELECT uniqueColumn FROM t;\n")

        # Every one of these returned 0 before the converter learned about code.
        self.assertEqual(self.hits("MemoryDatabase"), 1)
        self.assertEqual(self.hits("uniqueColumn"), 1)
        self.assertEqual(self.hits("Connect"), 2, "matches the .ts and the .go")

    def test_the_placeholder_is_not_what_gets_indexed(self) -> None:
        self.capture("db.py", "class MemoryDatabase:\n    pass\n")
        self.assertEqual(self.hits("converter"), 0)
