"""Interactive input primitives for the `bk` CLI.

The counterpart to `console.py`. That module grew rich *output* primitives --
tables, panels, rules, status lines -- while every question the CLI asked
stayed a bare `input()` with the suggestion spelled into the prompt string.
The asymmetry is why onboarding felt like an interrogation: choosing between
three privacy modes meant *typing* `local-only` and being re-asked on a typo,
and choosing an integration meant hand-authoring JSON at a terminal prompt.

Everything here is stdlib (`termios`/`tty`/`select`). brainskit's core stays at
one dependency by design, and arrow-key selection is a few hundred bytes of
ANSI -- not a reason to take on a TUI framework.

Two rules hold throughout:

- **The terminal is always restored.** Every raw-mode section is wrapped in
  `try/finally` around `termios.tcsetattr`, so a `KeyboardInterrupt`, an
  exception, or a normal return all leave the tty exactly as found. A prompt
  library that leaks raw mode leaves the operator with a shell that does not
  echo, which is a far worse failure than anything it was asking about.
- **Off a terminal, degrade rather than refuse.** `supports_interactive()` is
  false for a pipe, a file, `TERM=dumb`, or a platform without `termios`, and
  every function falls back to a numbered `input()` that a human or a here-doc
  can both drive. Tests capture stdout into `io.StringIO`, so they take this
  path automatically and never need a pty.
"""

from __future__ import annotations

import getpass
import os
import select as _select
import shlex
import shutil
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import IO, Any, Generic, TypeVar

from . import console

try:  # pragma: no cover - platform probe, exercised by whichever branch runs
    import termios
    import tty

    _HAS_TERMIOS = True
except ImportError:  # pragma: no cover - Windows and other non-POSIX
    _HAS_TERMIOS = False

T = TypeVar("T")

# Cursor control. `2K` clears the whole line rather than to-end-of-line so a
# redraw cannot leave the tail of a longer previous line on screen.
_UP = "\x1b[A"
_CLEAR_LINE = "\x1b[2K\r"
_HIDE_CURSOR = "\x1b[?25l"
_SHOW_CURSOR = "\x1b[?25h"

RADIO_ON = "◆"
RADIO_OFF = "◇"
CHECK_ON = "◼"
CHECK_OFF = "◻"
POINTER = "❯"  # noqa: RUF001 -- a cursor ornament, not a greater-than sign


class Cancelled(Exception):
    """The operator dismissed a prompt with Ctrl-C, Esc or `q`.

    Distinct from `KeyboardInterrupt` so a caller can tell "cancel this
    wizard" apart from "kill this process", and distinct from any domain
    error so it never reads as a validation failure.
    """


@dataclass(frozen=True)
class Choice(Generic[T]):
    """One selectable row, carrying whatever value the caller wants back.

    `note` is the dim trailing text that makes a choice self-explanatory --
    which branches a preset creates, which model is fastest. A choice list
    whose labels need a paragraph above them to be understood is a list with
    the explanation in the wrong place.

    Generic in the value so `select()` hands back the caller's own type rather
    than `object`: a wizard that has to re-narrow every answer it just chose
    from a fixed list has lost type information it never needed to lose.
    """

    value: T
    label: str
    note: str = ""
    enabled: bool = True


def supports_interactive(*, stream: IO[str] | None = None) -> bool:
    """Whether raw-mode selection will work on this stream and platform.

    Requires a real tty on **both** ends: stdin, because raw mode is
    meaningless on a pipe, and the output stream, because the redraw writes
    cursor-movement codes that would otherwise land in a file. `NO_COLOR` is
    deliberately *not* consulted -- it asks for no color, not for no
    interaction, and the fallback is answered by keyboard either way.
    """

    if not _HAS_TERMIOS:
        return False
    if os.environ.get("TERM") == "dumb":
        return False
    if os.environ.get("BK_NO_INTERACTIVE"):
        return False
    target = stream if stream is not None else sys.stdout
    try:
        return bool(sys.stdin.isatty() and target.isatty())
    except (AttributeError, ValueError):
        return False


def _read_key() -> str:
    """Read one keypress in raw mode, resolving escape sequences to names.

    Reads the **file descriptor** rather than `sys.stdin`. That is not a style
    preference: `sys.stdin.read(1)` pulls a whole chunk into `TextIOWrapper`'s
    internal buffer, so the remaining bytes of an arrow key's `\\x1b[A` sit in
    Python's buffer while `select()` -- which can only see the OS buffer --
    reports nothing pending. Every arrow press then reads as a bare Esc, which
    this module treats as "cancel". Going through `os.read` keeps the only
    buffer in play the one `select` actually measures.

    A terminal delivers an escape sequence in a single write, so the common
    case needs one read. The timed follow-up covers a sequence split across
    reads (a slow link), and distinguishes it from a genuine lone Esc.
    """

    fd = sys.stdin.fileno()
    data = os.read(fd, 8)
    if not data:
        return ""
    if data == b"\x1b" and _select.select([fd], [], [], 0.05)[0]:
        data += os.read(fd, 8)
    text = data.decode("utf-8", "replace")
    if text == "\x1b":
        return "escape"
    if text.startswith("\x1b["):
        return {"A": "up", "B": "down", "C": "right", "D": "left"}.get(
            text[2:3], "unknown"
        )
    return text[0]


class _RawTerminal:
    """Raw mode plus a hidden cursor, restored no matter how the block exits."""

    def __enter__(self) -> _RawTerminal:
        self._fd = sys.stdin.fileno()
        self._saved = termios.tcgetattr(self._fd)
        tty.setraw(self._fd)
        sys.stdout.write(_HIDE_CURSOR)
        sys.stdout.flush()
        return self

    def __exit__(self, *exc: object) -> None:
        # Ordering matters: restore the mode first, so that even if writing
        # the show-cursor code fails on a closed stream the tty is already
        # usable again.
        termios.tcsetattr(self._fd, termios.TCSADRAIN, self._saved)
        sys.stdout.write(_SHOW_CURSOR)
        sys.stdout.flush()


def _question(title: str, hint: str, *, stream: IO[str] | None = None) -> str:
    mark = console.style("?", console.BOLD, console.ACCENT, stream=stream)
    text = console.style(title, console.BOLD, stream=stream)
    if not hint:
        return f"{mark} {text}"
    return f"{mark} {text}  {console.style(hint, console.MUTED, stream=stream)}"


def _answered(title: str, answer: str, *, stream: IO[str] | None = None) -> str:
    """The one-line residue a finished prompt leaves behind.

    Collapsing to `✓ question  answer` is what keeps a multi-screen wizard
    readable: the transcript above the current question stays a summary of
    decisions rather than a wall of every option that was ever offered.
    """

    mark = console.style(console.CHECK, console.OK, stream=stream)
    return f"{mark} {title}  {console.style(answer, console.ACCENT, stream=stream)}"


def _rows_available() -> int:
    """How many rows a prompt may draw and still redraw itself correctly.

    Two are reserved: the question line printed above the block, and the row
    the cursor rests on beneath it. The `… N more` footer the viewport adds
    takes a third when it is present, so the budget subtracts it up front
    rather than discovering the overflow after the fact.
    """

    return max(shutil.get_terminal_size(fallback=(80, 24)).lines - 3, 3)


def _viewport(count: int, cursor: int, height: int) -> tuple[int, int]:
    """The slice of `count` rows to draw, keeping `cursor` inside it.

    A list taller than the screen cannot be redrawn by cursor arithmetic at
    all: once its top has scrolled past row zero, `\\x1b[A` stops moving --
    the cursor clamps -- so the up-count silently under-shoots and every
    redraw writes over the wrong lines. Showing a window instead keeps the
    block strictly smaller than the terminal, which is the only condition
    under which the arithmetic is sound.
    """

    if height <= 0 or count <= height:
        return 0, count
    half = height // 2
    start = min(max(cursor - half, 0), count - height)
    return start, start + height


def _render_rows(
    choices: Sequence[Choice[Any]],
    cursor: int,
    marks: Sequence[str],
    *,
    window: tuple[int, int] | None = None,
    width: int | None = None,
    stream: IO[str] | None = None,
) -> list[str]:
    """Render the visible rows, each fitted to one physical terminal line.

    The fitting is not cosmetic. `_redraw` moves the cursor up by the number of
    lines it wrote, so a row wider than the terminal -- which wraps onto a
    second physical line the count knows nothing about -- desynchronises every
    subsequent redraw and duplicates rows down the screen. `bk`'s own `forget`
    help is 140 characters, which made the one group containing it corrupt on
    any normal-width terminal.
    """

    total = width or console._terminal_width()
    start, end = window or (0, len(choices))
    label_width = max(
        (console._visible_len(c.label) for c in choices[start:end]), default=0
    )
    rows = []
    for i in range(start, end):
        choice = choices[i]
        pointer = POINTER if i == cursor else " "
        pad = " " * max(label_width - console._visible_len(choice.label), 0)
        label = choice.label + pad
        if not choice.enabled:
            body = console.style(f"{marks[i]} {label}", console.DIM, stream=stream)
        elif i == cursor:
            body = console.style(f"{marks[i]} {label}", console.BOLD, stream=stream)
        else:
            body = f"{marks[i]} {label}"
        note = (
            f"  {console.style(choice.note, console.MUTED, stream=stream)}"
            if choice.note
            else ""
        )
        arrow = console.style(pointer, console.ACCENT, stream=stream)
        # One column spare: writing into the last cell of a line makes some
        # terminals wrap immediately, which is the very thing being avoided.
        rows.append(console.truncate(f"  {arrow} {body}{note}", total - 1))
    if (start, end) != (0, len(choices)):
        more = len(choices) - end + start
        rows.append(
            console.style(f"    … {more} more", console.MUTED, stream=stream)
        )
    return rows


def _redraw(previous: int, lines: Sequence[str]) -> int:
    """Overwrite `previous` rendered lines with `lines`; return the new count.

    Sound only while every line occupies exactly one physical row and the whole
    block fits on screen -- `_render_rows` and `_viewport` are what guarantee
    both.
    """

    if previous:
        sys.stdout.write((_UP + _CLEAR_LINE) * previous)
    for line in lines:
        sys.stdout.write(_CLEAR_LINE + line + "\r\n")
    sys.stdout.flush()
    return len(lines)


def select(
    title: str,
    choices: Sequence[Choice[T]],
    *,
    default: int = 0,
    hint: str = "↑↓ · Enter",
    quiet: bool = False,
    stream: IO[str] | None = None,
) -> T:
    """Pick exactly one choice with the arrow keys.

    Disabled choices are shown rather than hidden -- an option missing from a
    list reads as unsupported, while a dimmed one with a note ("ollama not
    running") tells the operator what to fix.

    `quiet` suppresses the collapsed `✓ question  answer` line. A linear wizard
    wants that residue, because it is the transcript of what was decided; a
    browser the operator can back out of does not, because backing out would
    leave a checkmark next to a path they abandoned.
    """

    if not choices:
        raise ValueError("select() needs at least one choice")
    selectable = [i for i, c in enumerate(choices) if c.enabled]
    if not selectable:
        raise ValueError("select() needs at least one enabled choice")
    # Resolved before the branch so both paths start from the same row: the
    # fallback used to take `default` verbatim, which off a terminal would
    # index out of range or answer with a disabled choice.
    cursor = default if default in selectable else selectable[0]
    if not supports_interactive(stream=stream):
        return _select_fallback(title, choices, default=cursor)

    out = sys.stdout
    out.write(_question(title, hint, stream=stream) + "\n")
    drawn = 0
    try:
        with _RawTerminal():
            while True:
                marks = [
                    RADIO_ON if i == cursor else RADIO_OFF for i in range(len(choices))
                ]
                window = _viewport(len(choices), cursor, _rows_available())
                drawn = _redraw(
                    drawn,
                    _render_rows(
                        choices, cursor, marks, window=window, stream=stream
                    ),
                )
                key = _read_key()
                if key in ("\r", "\n"):
                    break
                if key in ("\x03", "escape", "q"):
                    raise Cancelled()
                if key in ("up", "k", "left"):
                    cursor = _step(selectable, cursor, -1)
                elif key in ("down", "j", "right"):
                    cursor = _step(selectable, cursor, +1)
    finally:
        if drawn:
            sys.stdout.write((_UP + _CLEAR_LINE) * drawn)
        # The question line itself is rewritten as the collapsed answer, so it
        # is cleared here too rather than left as a second copy of the title.
        sys.stdout.write(_UP + _CLEAR_LINE)
        sys.stdout.flush()
    if not quiet:
        out.write(_answered(title, choices[cursor].label, stream=stream) + "\n")
    return choices[cursor].value


def multiselect(
    title: str,
    choices: Sequence[Choice[T]],
    *,
    selected: Sequence[int] = (),
    hint: str = "↑↓ · space · Enter",
    stream: IO[str] | None = None,
) -> list[T]:
    """Toggle any number of choices with space, confirm with Enter.

    This is what replaces asking for an integrations object as pasted JSON.
    Beyond being typable, it is *structurally* safer: a checkbox list can only
    produce a complete set of known keys, so the "integration policy set is
    incomplete" rejection -- which used to discard an entire wizard's answers
    at the very last prompt -- becomes unreachable rather than merely rarer.
    """

    if not choices:
        return []
    chosen = {i for i in selected if 0 <= i < len(choices) and choices[i].enabled}
    if not supports_interactive(stream=stream):
        return _multiselect_fallback(title, choices, chosen)

    selectable = [i for i, c in enumerate(choices) if c.enabled]
    if not selectable:
        return []
    cursor = selectable[0]
    out = sys.stdout
    out.write(_question(title, hint, stream=stream) + "\n")
    drawn = 0
    try:
        with _RawTerminal():
            while True:
                marks = [
                    CHECK_ON if i in chosen else CHECK_OFF for i in range(len(choices))
                ]
                window = _viewport(len(choices), cursor, _rows_available())
                drawn = _redraw(
                    drawn,
                    _render_rows(
                        choices, cursor, marks, window=window, stream=stream
                    ),
                )
                key = _read_key()
                if key in ("\r", "\n"):
                    break
                if key in ("\x03", "escape"):
                    raise Cancelled()
                if key in ("up", "k"):
                    cursor = _step(selectable, cursor, -1)
                elif key in ("down", "j"):
                    cursor = _step(selectable, cursor, +1)
                elif key == " ":
                    chosen.symmetric_difference_update({cursor})
    finally:
        if drawn:
            sys.stdout.write((_UP + _CLEAR_LINE) * drawn)
        sys.stdout.write(_UP + _CLEAR_LINE)
        sys.stdout.flush()
    picked = [i for i in range(len(choices)) if i in chosen]
    answer = ", ".join(choices[i].label for i in picked) or "none"
    out.write(_answered(title, answer, stream=stream) + "\n")
    return [choices[i].value for i in picked]


def shell_value(raw: str) -> str:
    """What the operator *meant*, after the shell conventions they pasted.

    A path reaches a prompt the way the operator obtained it, and the two
    common ways both carry shell syntax: dragging a file into a terminal
    escapes its spaces (`/a/b\\ c.pdf`), and tab-completion or a copied command
    wraps it in quotes (`'/a/b c.pdf'`). Passing either through verbatim
    produces a path that cannot exist -- brainskit answered a real drag-and-drop
    with "No file at …" and echoed the filename with the quotes still in it,
    which reads as brainskit failing rather than as one layer of quoting nobody
    removed.

    `shlex` already knows both conventions, so the rule is: if the value parses
    to exactly **one** token, that token is what was meant. Anything that parses
    to several is ordinary prose -- a search query, a title -- and is returned
    untouched. Unbalanced quotes raise, and are likewise left alone rather than
    guessed at.
    """

    value = raw.strip()
    if not value:
        return value
    try:
        tokens = shlex.split(value)
    except ValueError:
        return value
    if len(tokens) == 1:
        value = tokens[0]
    return os.path.expanduser(value) if value.startswith("~") else value


def text(
    title: str,
    default: str = "",
    *,
    validate: object = None,
    normalize: Callable[[str], str] | None = shell_value,
    stream: IO[str] | None = None,
) -> str:
    """Ask for a line of text, re-asking until `validate` accepts it.

    `normalize` runs **before** `validate`, which is the whole reason it is a
    parameter here rather than something the caller applies to the result: a
    quoted path validated raw fails the check that was meant to accept it, and
    the operator is re-asked with no way to satisfy the prompt. Pass `None` for
    a prompt that genuinely wants the bytes as typed.

    `validate` returns an error message for a bad value and `None` for a good
    one. Validating *here* rather than when the finished policy is assembled
    is the whole point: the old wizard checked its answers only in
    `VaultConfig.from_dict`, so a value rejected there took every other answer
    down with it.

    An exhausted input stream ends the loop rather than re-entering it. EOF
    means no further answer is coming, so a default the validator rejects can
    never become acceptable: `bk search` with no argument asked for a query,
    took Ctrl-D as the empty string, and printed "This one is required."
    forever. Accepting the default is still right when it *does* validate,
    which is what lets a here-doc drive a wizard by pressing Enter through it.
    """

    label = console.style(title, console.BOLD, stream=stream)
    suffix = (
        console.style(f" [{default}]", console.MUTED, stream=stream) if default else ""
    )
    while True:
        exhausted = False
        try:
            raw = input(f"{label}{suffix}: ").strip()
        except EOFError:
            raw, exhausted = "", True
        except KeyboardInterrupt:
            raise Cancelled() from None
        value = raw or default
        if normalize is not None:
            value = normalize(value)
        if validate is None:
            return value
        problem = validate(value)  # type: ignore[operator]
        if not problem:
            return value
        if exhausted:
            raise Cancelled() from None
        print(console.style(str(problem), console.WARN, stream=stream))


def secret(
    title: str,
    *,
    validate: object = None,
    stream: IO[str] | None = None,
) -> str:
    """Ask for a value that must not be echoed, re-asking until `validate` accepts.

    Three differences from `text`, each of them the reason this is not a flag on
    it:

    - **No echo and no default.** `getpass` reads from `/dev/tty` where it can,
      so the key is not painted onto a terminal that someone else may be
      watching and does not survive in the scrollback. There is no default to
      offer, because a secret nobody typed is not one.
    - **No `shell_value` normalisation.** That helper unquotes a single token,
      which is right for a dragged-in path and wrong here: a key is bytes, and
      one whose quoting was "corrected" fails authentication for a reason no
      error message would name.
    - **The answer is never echoed back.** Every other prompt collapses to
      `✓ question  answer`; this one collapses to a fixed placeholder, so a
      wizard transcript never contains the credential.

    Off a terminal it falls through to `input`, which is what a here-doc driving
    the wizard needs -- and `getpass` warns about the missing echo itself.
    """

    label = console.style(title, console.BOLD, stream=stream)
    out = stream or sys.stdout
    while True:
        try:
            value = getpass.getpass(f"{label}: ").strip()
        except EOFError:
            raise Cancelled() from None
        except KeyboardInterrupt:
            raise Cancelled() from None
        problem = validate(value) if validate is not None else None  # type: ignore[operator]
        if not problem:
            out.write(_answered(title, "•" * 12, stream=stream) + "\n")
            return value
        print(console.style(str(problem), console.WARN, stream=stream))


def confirm(
    title: str, *, default: bool = True, stream: IO[str] | None = None
) -> bool:
    suffix = "Y/n" if default else "y/N"
    while True:
        try:
            raw = input(
                f"{console.style(title, console.BOLD, stream=stream)} "
                f"{console.style(f'[{suffix}]', console.MUTED, stream=stream)}: "
            ).strip().lower()
        except EOFError:
            return default
        except KeyboardInterrupt:
            raise Cancelled() from None
        if not raw:
            return default
        if raw in ("y", "yes"):
            return True
        if raw in ("n", "no"):
            return False


def _step(selectable: Sequence[int], cursor: int, delta: int) -> int:
    """Move to the next *enabled* row, wrapping at either end."""

    order = list(selectable)
    if cursor not in order:
        return order[0]
    return order[(order.index(cursor) + delta) % len(order)]


def _select_fallback(
    title: str, choices: Sequence[Choice[T]], *, default: int
) -> T:
    print(f"{title}")
    for i, choice in enumerate(choices, start=1):
        marker = " " if choice.enabled else "-"
        note = f"  ({choice.note})" if choice.note else ""
        print(f"  {marker}{i}) {choice.label}{note}")
    while True:
        try:
            raw = input(f"Choose 1-{len(choices)} [{default + 1}]: ").strip()
        except (EOFError, KeyboardInterrupt):
            raw = ""
        if not raw:
            return choices[default].value
        if raw.isdigit() and 1 <= int(raw) <= len(choices):
            picked = choices[int(raw) - 1]
            if picked.enabled:
                return picked.value
        print(f"Enter a number between 1 and {len(choices)}.")


def _multiselect_fallback(
    title: str, choices: Sequence[Choice[T]], chosen: set[int]
) -> list[T]:
    print(f"{title}")
    for i, choice in enumerate(choices, start=1):
        mark = "x" if i - 1 in chosen else " "
        note = f"  ({choice.note})" if choice.note else ""
        print(f"  [{mark}] {i}) {choice.label}{note}")
    preset = ",".join(str(i + 1) for i in sorted(chosen))
    try:
        raw = input(f"Comma-separated numbers, blank for [{preset or 'none'}]: ").strip()
    except (EOFError, KeyboardInterrupt):
        raw = ""
    if raw:
        chosen = {
            int(part) - 1
            for part in raw.split(",")
            if part.strip().isdigit() and 0 < int(part) <= len(choices)
        }
    return [choices[i].value for i in sorted(chosen) if choices[i].enabled]
