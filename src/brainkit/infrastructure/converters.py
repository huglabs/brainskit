from __future__ import annotations

import json
import shutil
import subprocess
import zipfile
from html.parser import HTMLParser
from pathlib import Path
from xml.etree import ElementTree


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


def extract_document(path: Path, media_type: str) -> tuple[str, bool]:
    """Return searchable text and whether it is a derived conversion."""

    suffix = path.suffix.lower()
    if suffix in PLAIN_TEXT_SUFFIXES:
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
            document = archive.read("word/document.xml")
    except (OSError, KeyError, zipfile.BadZipFile):
        return None
    try:
        root = ElementTree.fromstring(document)
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


def _pdftotext(path: Path) -> str | None:
    executable = shutil.which("pdftotext")
    if not executable:
        return None
    try:
        result = subprocess.run(
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

    def handle_starttag(self, tag: str, attrs) -> None:
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

