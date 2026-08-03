from __future__ import annotations

import json
import mimetypes
import shutil
import subprocess
import zipfile
from html.parser import HTMLParser
from pathlib import Path, PurePosixPath
from xml.etree import ElementTree

#: Upper bound on the decompressed `word/document.xml` of a captured .docx.
#: Far above any real document, far below what an expansion bomb aims for.
MAX_DOCX_DOCUMENT_BYTES = 64 * 1024 * 1024

PLAIN_TEXT_SUFFIXES = {
    ".md",
    ".markdown",
    ".txt",
    ".csv",
    ".tsv",
    ".xml",
    ".yaml",
    ".yml",
}

#: Source-code suffixes, mapped to the media type to record for them.
#:
#: This table earns its place by fixing two separate failures at once. For most
#: of these `mimetypes` has no entry at all, so a capture recorded
#: `application/octet-stream` and the extractor fell through to the "no text
#: converter available" placeholder — a `.go` file went into the vault and out
#: of the index, silently. And for `.ts` `mimetypes` has an entry that is
#: actively wrong (`video/mp2t`, MPEG transport stream), so testing the media
#: type alone would still refuse TypeScript.
#:
#: Every value starts with `text/`, which is what makes the media-type fallback
#: below coherent: anything this table names is text by construction, and
#: anything else claiming to be text is taken at its word.
#:
#: `.env` is deliberately absent. It is text, and capturing one would index a
#: secret into a searchable database.
SOURCE_CODE_TYPES: dict[str, str] = {
    ".c": "text/x-c",
    ".cc": "text/x-c++",
    ".cfg": "text/x-config",
    ".clj": "text/x-clojure",
    ".cjs": "text/javascript",
    ".cmake": "text/x-cmake",
    ".cpp": "text/x-c++",
    ".cs": "text/x-csharp",
    ".css": "text/css",
    ".dart": "text/x-dart",
    ".erl": "text/x-erlang",
    ".ex": "text/x-elixir",
    ".exs": "text/x-elixir",
    ".fish": "text/x-shellscript",
    ".go": "text/x-go",
    ".gql": "text/x-graphql",
    ".gradle": "text/x-groovy",
    ".graphql": "text/x-graphql",
    ".h": "text/x-c",
    ".hcl": "text/x-hcl",
    ".hpp": "text/x-c++",
    ".hs": "text/x-haskell",
    ".ini": "text/x-config",
    ".java": "text/x-java",
    ".jl": "text/x-julia",
    ".js": "text/javascript",
    ".jsx": "text/javascript",
    ".kt": "text/x-kotlin",
    ".kts": "text/x-kotlin",
    ".lua": "text/x-lua",
    ".mjs": "text/javascript",
    ".ml": "text/x-ocaml",
    ".nim": "text/x-nim",
    ".php": "text/x-php",
    ".pl": "text/x-perl",
    ".proto": "text/x-protobuf",
    ".py": "text/x-python",
    ".r": "text/x-r",
    ".rb": "text/x-ruby",
    ".rs": "text/x-rust",
    ".scala": "text/x-scala",
    ".sh": "text/x-shellscript",
    ".sql": "text/x-sql",
    ".svelte": "text/x-svelte",
    ".swift": "text/x-swift",
    ".tf": "text/x-terraform",
    ".toml": "text/x-toml",
    ".ts": "text/x-typescript",
    ".tsx": "text/x-typescript",
    ".vue": "text/x-vue",
    ".zig": "text/x-zig",
    ".zsh": "text/x-shellscript",
    ".bash": "text/x-shellscript",
}

#: Build and config files that carry no suffix at all, so no table keyed on one
#: can reach them. Matched case-sensitively, which is how these names are spelled.
SOURCE_CODE_NAMES: dict[str, str] = {
    "Dockerfile": "text/x-dockerfile",
    "Makefile": "text/x-makefile",
    "Rakefile": "text/x-ruby",
    "Gemfile": "text/x-ruby",
    "Brewfile": "text/x-ruby",
    "Justfile": "text/x-justfile",
    "Procfile": "text/x-procfile",
    "CMakeLists.txt": "text/x-cmake",
}


def guess_media_type(name: str, default: str = "application/octet-stream") -> str:
    """Media type for a captured file, correcting `mimetypes` where it is wrong.

    Consulted before `mimetypes` rather than after, because the cases worth
    correcting are the ones where it answers confidently and wrongly.
    """

    if name in SOURCE_CODE_NAMES:
        return SOURCE_CODE_NAMES[name]
    suffix = PurePosixPath(name).suffix.lower()
    if suffix in SOURCE_CODE_TYPES:
        return SOURCE_CODE_TYPES[suffix]
    return mimetypes.guess_type(name)[0] or default


def is_source_code(name: str) -> bool:
    """Whether a captured file is source code this vault can read as text."""

    return (
        name in SOURCE_CODE_NAMES
        or PurePosixPath(name).suffix.lower() in SOURCE_CODE_TYPES
    )


def extract_document(path: Path, media_type: str) -> tuple[str, bool]:
    """Return searchable text and whether it is a derived conversion."""

    suffix = path.suffix.lower()
    if suffix in PLAIN_TEXT_SUFFIXES or is_source_code(path.name):
        return path.read_text(encoding="utf-8", errors="replace"), False
    if suffix == ".json":
        raw = path.read_text(encoding="utf-8", errors="replace")
        try:
            return json.dumps(json.loads(raw), ensure_ascii=False, indent=2), True
        except json.JSONDecodeError:
            return raw, False
    if suffix in {".html", ".htm"}:
        parser = _ReadableHtml()
        parser.feed(path.read_text(encoding="utf-8", errors="replace"))
        return parser.text(), True
    # Anything still unclaimed that calls itself text is read as itself. The
    # tables above cannot enumerate every language anyone will ever capture, and
    # a converter mangling plain text is worse than reading it verbatim — so
    # this runs before the converters rather than after them.
    if media_type.startswith("text/"):
        return path.read_text(encoding="utf-8", errors="replace"), False
    converted = _markitdown(path)
    if converted:
        return converted, True
    if suffix == ".docx":
        converted = _docx_text(path)
        if converted:
            return converted, True
    if suffix == ".pdf":
        converted = _pdftotext(path)
        if converted:
            return converted, True
    return (
        f"[Binary source: {path.name}; media type: {media_type}; "
        "no text converter available]",
        True,
    )


def _markitdown(path: Path) -> str | None:
    try:
        from markitdown import MarkItDown  # type: ignore[import-not-found]
    except ImportError:
        return None
    try:
        result = MarkItDown().convert(str(path))
    except Exception:
        return None
    text = getattr(result, "text_content", None)
    return str(text).strip() if text else None


def _docx_text(path: Path) -> str | None:
    try:
        with zipfile.ZipFile(path) as archive:
            entry = archive.getinfo("word/document.xml")
            # A .docx is a zip, so its declared size is attacker-controlled and
            # its real size is not known until it is decompressed. Refusing an
            # implausible one keeps a crafted capture from expanding into
            # memory during `capture` or a full `reindex`.
            if entry.file_size > MAX_DOCX_DOCUMENT_BYTES:
                return None
            document = archive.read("word/document.xml")
    except (OSError, KeyError, zipfile.BadZipFile):
        return None
    if _declares_entities(document):
        # ElementTree does not resolve external entities, but it does expand
        # internal ones, which is all "billion laughs" needs. A real .docx has
        # no doctype at all, so refusing one costs nothing.
        return None
    try:
        root = ElementTree.fromstring(document)  # noqa: S314 - guarded above
    except ElementTree.ParseError:
        return None
    paragraphs: list[str] = []
    for paragraph in root.iter("{http://schemas.openxmlformats.org/wordprocessingml/2006/main}p"):
        text = "".join(
            node.text or ""
            for node in paragraph.iter(
                "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}t"
            )
        ).strip()
        if text:
            paragraphs.append(text)
    return "\n\n".join(paragraphs) or None


def _declares_entities(document: bytes) -> bool:
    """True when the XML prolog declares a doctype or an entity.

    Only the prolog is inspected: a doctype must precede the root element, so
    scanning the whole body would mean rejecting a document that merely
    mentions the string in its text.
    """

    prolog = document[:4096].lstrip()
    return b"<!DOCTYPE" in prolog or b"<!ENTITY" in prolog


def _pdftotext(path: Path) -> str | None:
    executable = shutil.which("pdftotext")
    if not executable:
        return None
    try:
        # Resolved through `which`, fixed argument vector, no shell: the only
        # caller-supplied value is the path, and it is passed as one argv entry.
        result = subprocess.run(  # noqa: S603
            [executable, "-layout", str(path), "-"],
            check=False,
            capture_output=True,
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    text = result.stdout.decode("utf-8", errors="replace").strip()
    return text or None


class _ReadableHtml(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._skip_depth = 0
        self._chunks: list[str] = []

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        if tag in {"script", "style", "noscript"}:
            self._skip_depth += 1
        elif tag in {"p", "div", "article", "section", "br", "li", "h1", "h2", "h3"}:
            self._chunks.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "noscript"} and self._skip_depth:
            self._skip_depth -= 1
        elif tag in {"p", "div", "article", "section", "li"}:
            self._chunks.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip_depth:
            self._chunks.append(data)

    def text(self) -> str:
        lines = (" ".join(chunk.split()) for chunk in "".join(self._chunks).splitlines())
        return "\n".join(line for line in lines if line)

