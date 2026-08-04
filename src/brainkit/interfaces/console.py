"""Terminal rendering primitives for the `bk` CLI.

A hand-rolled ANSI layer, not a library: brainkit's core stays at one
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

# The HugLabs palette already established in README.md's badges.
ACCENT = "\x1b[38;2;217;119;87m"  # #d97757 -- brand orange
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
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _visible_len(text: str) -> int:
    """The length a cell occupies on screen, ignoring its ANSI codes.

    A cell built from `status_line`/`state_tag`/`style` carries escape bytes
    that `len()` would count as columns -- using raw length here would both
    misjudge how much room a colored cell needs and, worse, let `table()`'s
    truncation slice into the middle of a cell and cut off its trailing
    reset code, leaking color into everything printed after it.
    """

    return len(_ANSI_RE.sub("", text))


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


def state_tag(state: str, *, stream: IO[str] | None = None) -> str:
    """Color a projection/freshness state word consistently everywhere."""

    color = {"fresh": OK, "stale": WARN, "missing": MUTED}.get(state, MUTED)
    return style(state, color, stream=stream)


def status_line(ok: bool, message: str, *, stream: IO[str] | None = None) -> str:
    symbol = CHECK if ok else CROSS
    color = OK if ok else ERR
    return style(f"{symbol} {message}", color, stream=stream)


def banner(*, stream: IO[str] | None = None) -> str:
    name = style("brainkit", BOLD, ACCENT, stream=stream)
    tagline = style(f"{BULLET} by HugLabs", MUTED, stream=stream)
    return f"{name} {tagline}"


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

    def truncate(cell: str, col_width: int) -> str:
        if _visible_len(cell) <= col_width:
            return cell
        # A cell that still overflows once it is stripped of color is cut on
        # the plain text -- slicing the raw (possibly ANSI-bearing) string
        # instead could land mid-escape-sequence and drop the trailing
        # reset, leaking that cell's color into everything printed after it.
        plain = _ANSI_RE.sub("", cell)
        if col_width <= 1:
            return plain[:col_width]
        return plain[: col_width - 1].rstrip() + "…"

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
