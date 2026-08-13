"""Terminal rendering primitives for the `bk` CLI.

A hand-rolled ANSI layer, not a library: brainskit's core stays at one
dependency (`jsonschema`) by design, and colored text, simple tables and a
banner do not need `rich` to look right. Every function here is a pure
string builder -- nothing writes to stdout -- so callers stay easy to test
and free to compose these however a command's shape demands.

Color is gated on `supports_color()`, which is false whenever the target
stream is not a real terminal (a pipe, a file, or the `io.StringIO` every
test captures stdout/stderr into). That single gate is what keeps `--json`
and every existing plain-text assertion untouched: pretty structure still
applies off a TTY, but escape codes never do.
"""

from __future__ import annotations

import os
import re
import shutil
import sys
from collections.abc import Sequence
from typing import IO

RESET = "\x1b[0m"
BOLD = "\x1b[1m"
DIM = "\x1b[2m"

# The HugLabs accent, matching `--accent` on huglabs.ai and the README badges.
# The muted tone is not a brand token -- it is a dim neutral for secondary text.
ACCENT = "\x1b[38;2;238;80;44m"  # #ee502c -- brand orange
MUTED = "\x1b[38;2;183;181;169m"  # #b7b5a9 -- muted tan

OK = "\x1b[32m"
WARN = "\x1b[33m"
ERR = "\x1b[31m"

CHECK = "✓"
CROSS = "✗"
ARROW = "→"
BULLET = "•"
DASH = "—"

_FALLBACK_WIDTH = 100
_MIN_COLUMN_WIDTH = 8
# Both escape shapes this module emits: SGR color, and the OSC 8 hyperlink
# wrapper. Neither occupies a column, so both have to be invisible to
# `_visible_len` -- a link inside a table cell would otherwise be measured as
# its URL plus its text, and `truncate()` would cut a row that already fits.
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m|\x1b\]8;[^\x07\x1b]*(?:\x07|\x1b\\)")

# The product name as a masthead, in the ANSI Shadow figlet face the other
# HugLabs CLIs use. Generated once and inlined: rendering it at runtime would
# buy a font dependency for six constant strings, and the core keeps one.
_WORDMARK = (
    "██████╗ ██████╗  █████╗ ██╗███╗   ██╗███████╗██╗  ██╗██╗████████╗",
    "██╔══██╗██╔══██╗██╔══██╗██║████╗  ██║██╔════╝██║ ██╔╝██║╚══██╔══╝",
    "██████╔╝██████╔╝███████║██║██╔██╗ ██║███████╗█████╔╝ ██║   ██║",
    "██╔══██╗██╔══██╗██╔══██║██║██║╚██╗██║╚════██║██╔═██╗ ██║   ██║",
    "██████╔╝██║  ██║██║  ██║██║██║ ╚████║███████║██║  ██╗██║   ██║",
    "╚═════╝ ╚═╝  ╚═╝╚═╝  ╚═╝╚═╝╚═╝  ╚═══╝╚══════╝╚═╝  ╚═╝╚═╝   ╚═╝",
)

# Who made it, what they do, and where to find them. Kept narrower than the
# wordmark on purpose: the mark is what decides whether the masthead fits, and
# a credit line that pushed that threshold up would cost terminals the whole
# banner just to carry a URL.
_CREDIT_INDENT = " " * 8
_SITE = "https://www.huglabs.ai"
_SITE_LABEL = "www.huglabs.ai"
_TAGLINE = "Enterprise AI that ships"
_LICENSE = "Apache-2.0"
_LICENSE_URL = "https://www.apache.org/licenses/LICENSE-2.0"
# The plain-text projection of what `banner()` draws. It exists to size the
# masthead, and a test pins it against the rendered lines so the two cannot
# drift into disagreeing about how wide the credit is.
_CREDIT = (
    f"{_CREDIT_INDENT}by HugLabs {BULLET} {_TAGLINE}",
    f"{_CREDIT_INDENT}{_SITE_LABEL} {BULLET} open source, {_LICENSE}",
)
_WORDMARK_WIDTH = max(len(line) for line in (*_WORDMARK, *_CREDIT))


def _visible_len(text: str) -> int:
    """The length a cell occupies on screen, ignoring its ANSI codes.

    A cell built from `status_line`/`state_tag`/`style` carries escape bytes
    that `len()` would count as columns -- using raw length here would both
    misjudge how much room a colored cell needs and, worse, let `table()`'s
    truncation slice into the middle of a cell and cut off its trailing
    reset code, leaking color into everything printed after it.
    """

    return len(_ANSI_RE.sub("", text))


def truncate(text: str, width: int) -> str:
    """Cut `text` to `width` visible columns, never mid-escape-sequence.

    Slicing a string that carries ANSI codes could land inside one and drop its
    trailing reset, leaking that colour into everything printed afterwards, so
    anything that actually needs cutting is flattened to plain text first.

    Shared rather than private because two callers need the same guarantee for
    different reasons: `table()` wants columns that line up, and the interactive
    prompts need it for correctness -- a row wider than the terminal wraps onto
    a second physical line, and every redraw there counts logical lines while
    the terminal counts physical ones.
    """

    if width <= 0:
        return ""
    if _visible_len(text) <= width:
        return text
    plain = _ANSI_RE.sub("", text)
    if width <= 1:
        return plain[:width]
    return plain[: width - 1].rstrip() + "…"



def strip_ansi(text: str) -> str:
    """`text` without colour codes, for callers measuring their own layout."""

    return _ANSI_RE.sub("", text)


def terminal_width() -> int:
    """The usable width, shared so callers do not each guess their own."""

    return _terminal_width()

def supports_color(*, stream: IO[str] | None = None) -> bool:
    """Whether `stream` (default stdout) is a real terminal worth coloring.

    Checked per-call rather than cached: a test capturing stdout into a
    fresh `io.StringIO` for every case must see a fresh (and always false)
    answer, not one memoized from an earlier, differently-configured call.
    """

    if os.environ.get("NO_COLOR") is not None:
        return False
    if os.environ.get("TERM") == "dumb":
        return False
    target = stream if stream is not None else sys.stdout
    try:
        return bool(target.isatty())
    except (AttributeError, ValueError):
        return False


def style(text: str, *codes: str, stream: IO[str] | None = None) -> str:
    """Wrap `text` in `codes`, or return it unchanged off a terminal."""

    if not codes or not supports_color(stream=stream):
        return text
    return f"{''.join(codes)}{text}{RESET}"


def link(text: str, url: str, *, stream: IO[str] | None = None) -> str:
    """`text` as an OSC 8 hyperlink, or unchanged where that would be noise.

    A terminal that understands the sequence renders `text` clickable and one
    that does not drops it, so this is safe to emit anywhere -- but it is
    gated on the same check as color, because an escape sequence written into
    a pipe, a log or a test capture is noise no reader asked for. The URL
    never occupies a column: `_ANSI_RE` strips this shape too, so a linked
    cell still measures as the text a reader sees.
    """

    if not supports_color(stream=stream):
        return text
    return f"\x1b]8;;{url}\x1b\\{text}\x1b]8;;\x1b\\"


#: Every projection/freshness state, mapped to what it means for the reader.
#
# `missing` is the one quiet state and it is quiet on purpose: a vault that has
# not generated an artefact yet is healthy, and nothing needs doing. Every other
# named state is a fault of some degree -- `stale` and `partial` say regenerate,
# `malformed` says the file cannot be what it claims to be.
#
# The default is WARN, never MUTED. `malformed` and `partial` both spent their
# first release rendering in the same grey as `missing` because the map was a
# denylist with a calm fallback, so a state nobody had thought about read as the
# healthy one. An unrecognised state is at minimum "this renderer does not know
# what that is", which is not a thing to draw quietly.
_STATE_COLORS = {
    "fresh": OK,
    "stale": WARN,
    "partial": WARN,
    "malformed": ERR,
    "missing": MUTED,
}


def state_tag(state: str, *, stream: IO[str] | None = None) -> str:
    """Color a projection/freshness state word consistently everywhere."""

    return style(state, _STATE_COLORS.get(state, WARN), stream=stream)


def status_line(ok: bool, message: str, *, stream: IO[str] | None = None) -> str:
    symbol = CHECK if ok else CROSS
    color = OK if ok else ERR
    return style(f"{symbol} {message}", color, stream=stream)


def banner(*, stream: IO[str] | None = None) -> str:
    """The product masthead: the wordmark on a terminal, one line anywhere else.

    Figlet art cannot be truncated the way `table()` truncates a cell -- each
    line is one row of the same glyphs, so cutting a column mid-letter mangles
    every letter at once, and wrapping is what makes this kind of art look
    broken. So it is drawn only when the target is a real terminal wide enough
    to hold it, and a redirected banner stays the compact one-liner that
    `bk --help | less`, a CI log and the tests already read.
    """

    def linked(text: str, url: str, *codes: str) -> str:
        return link(style(text, *codes, stream=stream), url, stream=stream)

    name = style("brainskit", BOLD, ACCENT, stream=stream)
    if not supports_color(stream=stream) or _terminal_width() < _WORDMARK_WIDTH:
        return " ".join(
            [
                name,
                style(f"{BULLET} by", MUTED, stream=stream),
                linked("HugLabs", _SITE, MUTED),
                style(BULLET, MUTED, stream=stream),
                linked(_LICENSE, _LICENSE_URL, MUTED),
            ]
        )

    lines = [style(line, ACCENT, stream=stream) for line in _WORDMARK]
    lines.append(
        style(f"{_CREDIT_INDENT}by ", MUTED, stream=stream)
        + linked("HugLabs", _SITE, BOLD, ACCENT)
        + style(f" {BULLET} {_TAGLINE}", MUTED, stream=stream)
    )
    lines.append(
        style(_CREDIT_INDENT, MUTED, stream=stream)
        + linked(_SITE_LABEL, _SITE, MUTED)
        + style(f" {BULLET} open source, ", MUTED, stream=stream)
        + linked(_LICENSE, _LICENSE_URL, MUTED)
    )
    lines.append(rule(stream=stream))
    return "\n".join(lines)


def step_header(
    n: int, total: int, title: str, *, stream: IO[str] | None = None
) -> str:
    label = style(f"Step {n}/{total}", BOLD, ACCENT, stream=stream)
    return f"{label} {ARROW} {title}"


def rule(
    title: str | None = None,
    *,
    width: int | None = None,
    stream: IO[str] | None = None,
) -> str:
    total = max(width or _terminal_width(), 10)
    if not title:
        return style(DASH * total, MUTED, stream=stream)
    label = f" {title} "
    left = max((total - len(label)) // 2, 2)
    right = max(total - left - len(label), 2)
    return style(DASH * left, MUTED, stream=stream) + label + style(
        DASH * right, MUTED, stream=stream
    )


def kv_panel(pairs: Sequence[tuple[str, str]], *, stream: IO[str] | None = None) -> str:
    """An aligned `key : value` block, for summaries and confirmations."""

    if not pairs:
        return ""
    label_width = max(_visible_len(key) for key, _ in pairs)
    lines = []
    for key, value in pairs:
        padded = key + " " * max(label_width - _visible_len(key), 0)
        label = style(padded, MUTED, stream=stream)
        lines.append(f"{label}  {value}")
    return "\n".join(lines)


def table(
    headers: Sequence[str],
    rows: Sequence[Sequence[str]],
    *,
    width: int | None = None,
    stream: IO[str] | None = None,
) -> str:
    """A column-aligned table, its widths bounded by the terminal width.

    Columns are sized to their content first; if that overflows the
    available width, the widest columns give back space (down to a floor of
    `_MIN_COLUMN_WIDTH`) and their cells are ellipsized rather than wrapped
    -- a table that scrolls off-screen is harder to scan than one that
    truncates and says so.
    """

    if not rows:
        return style("(none)", MUTED, stream=stream)
    total_width = width or _terminal_width()
    str_rows = [[str(cell) for cell in row] for row in rows]
    columns = len(headers)
    widths = [_visible_len(str(header)) for header in headers]
    for row in str_rows:
        for i in range(columns):
            cell = row[i] if i < len(row) else ""
            widths[i] = max(widths[i], _visible_len(cell))

    gap = 2
    budget = total_width - gap * (columns - 1)
    overflow = sum(widths) - budget
    if overflow > 0:
        for i in sorted(range(columns), key=lambda i: -widths[i]):
            if overflow <= 0:
                break
            give = min(overflow, max(widths[i] - _MIN_COLUMN_WIDTH, 0))
            widths[i] -= give
            overflow -= give

    def fit(cell: str, col_width: int, *, pad: bool) -> str:
        cell = truncate(cell, col_width)
        if not pad:
            return cell
        return cell + " " * max(col_width - _visible_len(cell), 0)

    def render_row(cells: Sequence[str]) -> str:
        return "  ".join(
            fit(cells[i], widths[i], pad=i < columns - 1) for i in range(columns)
        )

    lines = [
        "  ".join(
            style(
                fit(str(header), widths[i], pad=i < columns - 1),
                BOLD,
                ACCENT,
                stream=stream,
            )
            for i, header in enumerate(headers)
        )
    ]
    rule_width = min(total_width, sum(widths) + gap * (columns - 1))
    lines.append(style(DASH * rule_width, MUTED, stream=stream))
    for row in str_rows:
        lines.append(render_row([row[i] if i < len(row) else "" for i in range(columns)]))
    return "\n".join(lines)


def indent(text: str, prefix: str = "  ") -> str:
    return "\n".join(f"{prefix}{line}" if line else line for line in text.splitlines())


def _terminal_width() -> int:
    return shutil.get_terminal_size(fallback=(_FALLBACK_WIDTH, 24)).columns
