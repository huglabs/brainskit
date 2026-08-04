"""The terminal rendering primitives `cli.py` builds every human-mode view on.

Color is gated on `supports_color()`, which is why every test below either
passes an explicit non-TTY/TTY fake stream or patches the environment
variables the gate reads -- the ambient `sys.stdout` a test runs under is not
a stable signal (a real terminal one moment, a captured pipe the next), and
that instability is exactly what these tests exist to pin down.
"""

from __future__ import annotations

import re
import unittest
from unittest.mock import patch

from brainkit.interfaces import console


class FakeStream:
    def __init__(self, *, tty: bool) -> None:
        self._tty = tty

    def isatty(self) -> bool:
        return self._tty


TTY = FakeStream(tty=True)
PIPE = FakeStream(tty=False)


class SupportsColorTests(unittest.TestCase):
    def test_false_off_a_non_tty_stream(self) -> None:
        self.assertFalse(console.supports_color(stream=PIPE))

    def test_true_on_a_tty_stream(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            self.assertTrue(console.supports_color(stream=TTY))

    def test_no_color_env_wins_even_on_a_tty(self) -> None:
        with patch.dict("os.environ", {"NO_COLOR": "1"}, clear=True):
            self.assertFalse(console.supports_color(stream=TTY))

    def test_dumb_term_wins_even_on_a_tty(self) -> None:
        with patch.dict("os.environ", {"TERM": "dumb"}, clear=True):
            self.assertFalse(console.supports_color(stream=TTY))

    def test_a_stream_with_no_isatty_method_is_never_colored(self) -> None:
        self.assertFalse(console.supports_color(stream=object()))  # type: ignore[arg-type]


class StyleTests(unittest.TestCase):
    def test_no_op_off_a_non_tty_stream(self) -> None:
        self.assertEqual("plain", console.style("plain", console.ACCENT, stream=PIPE))

    def test_no_op_with_no_codes_even_on_a_tty(self) -> None:
        self.assertEqual("plain", console.style("plain", stream=TTY))

    def test_wraps_and_resets_on_a_tty(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            result = console.style("hi", console.ACCENT, stream=TTY)
        self.assertTrue(result.startswith(console.ACCENT))
        self.assertTrue(result.endswith(console.RESET))
        self.assertIn("hi", result)


class StatusLineTests(unittest.TestCase):
    def test_ok_uses_the_check_mark(self) -> None:
        self.assertEqual(
            f"{console.CHECK} vault is healthy",
            console.status_line(True, "vault is healthy", stream=PIPE),
        )

    def test_failure_uses_the_cross_mark(self) -> None:
        self.assertEqual(
            f"{console.CROSS} 2 lint errors",
            console.status_line(False, "2 lint errors", stream=PIPE),
        )


class BannerAndHeadersTests(unittest.TestCase):
    def test_banner_names_the_product_and_the_company(self) -> None:
        text = console.banner(stream=PIPE)
        self.assertIn("brainkit", text)
        self.assertIn("HugLabs", text)

    def test_step_header_reports_position_and_title(self) -> None:
        text = console.step_header(2, 4, "Privacy & filing policy", stream=PIPE)
        self.assertIn("Step 2/4", text)
        self.assertIn("Privacy & filing policy", text)

    def test_rule_without_a_title_is_a_plain_divider(self) -> None:
        line = console.rule(width=20, stream=PIPE)
        self.assertEqual(20, len(line))
        self.assertEqual(console.DASH * 20, line)

    def test_rule_with_a_title_insets_it(self) -> None:
        line = console.rule("Section", width=30, stream=PIPE)
        self.assertIn(" Section ", line)
        self.assertEqual(30, len(line))


class KvPanelTests(unittest.TestCase):
    def test_empty_pairs_render_nothing(self) -> None:
        self.assertEqual("", console.kv_panel([], stream=PIPE))

    def test_keys_align_to_the_longest_label(self) -> None:
        text = console.kv_panel(
            [("vault", "/tmp/x"), ("sources", "12")], stream=PIPE
        )
        lines = text.splitlines()
        self.assertEqual(2, len(lines))
        # Both value columns start at the same offset once padded.
        self.assertEqual(lines[0].index("/tmp/x"), lines[1].index("12"))

    def test_a_colored_key_still_aligns_by_visible_length(self) -> None:
        colored_key = console.style("vault", console.ACCENT, stream=TTY)
        with patch.dict("os.environ", {}, clear=True):
            text = console.kv_panel(
                [(colored_key, "/tmp/x"), ("sources", "12")], stream=TTY
            )
        stripped_lines = [re.sub(r"\x1b\[[0-9;]*m", "", line) for line in text.splitlines()]
        self.assertEqual(stripped_lines[0].index("/tmp/x"), stripped_lines[1].index("12"))


class VisibleLenTests(unittest.TestCase):
    def test_ignores_ansi_codes(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            colored = console.style("hi", console.ACCENT, stream=TTY)
        self.assertEqual(2, console._visible_len(colored))

    def test_matches_plain_len_with_no_codes(self) -> None:
        self.assertEqual(len("plain text"), console._visible_len("plain text"))


class TableTests(unittest.TestCase):
    def test_empty_rows_render_a_placeholder(self) -> None:
        self.assertEqual("(none)", console.table(["a"], [], stream=PIPE))

    def test_header_and_one_rule_and_one_row_per_line(self) -> None:
        text = console.table(
            ["title", "score"],
            [["Alpha", "0.9"], ["Beta", "0.5"]],
            width=40,
            stream=PIPE,
        )
        lines = text.splitlines()
        self.assertEqual(4, len(lines))  # header, rule, 2 rows
        self.assertTrue(lines[0].startswith("title"))
        self.assertEqual(set(lines[1]), {console.DASH})

    def test_long_cells_are_ellipsized_to_the_column_width(self) -> None:
        text = console.table(
            ["title", "score"],
            [["A" * 60, "0.5"]],
            width=30,
            stream=PIPE,
        )
        title_cell = text.splitlines()[2].split("  ")[0]
        self.assertTrue(title_cell.endswith("…"))
        self.assertLess(len(title_cell), 60)

    def test_no_line_exceeds_the_requested_width_at_several_sizes(self) -> None:
        rows = [["short", "1"], ["a moderately long title here", "22"]]
        for width in (20, 40, 80, 120):
            text = console.table(["title", "n"], rows, width=width, stream=PIPE)
            for line in text.splitlines():
                self.assertLessEqual(len(line), width)

    def test_the_final_column_is_not_right_padded(self) -> None:
        text = console.table(["k"], [["v"]], width=40, stream=PIPE)
        rows = text.splitlines()
        self.assertEqual("v", rows[2])

    def test_colored_cells_do_not_inflate_column_width(self) -> None:
        """A cell built from `style()`/`status_line()` carries escape bytes
        `len()` would count as columns -- if width math used raw length, a
        colored cell in one row would misalign every later column against
        an uncolored cell in another row of the same table.
        """

        colored = console.style("stale", console.WARN, stream=TTY)
        with patch.dict("os.environ", {}, clear=True):
            text = console.table(
                ["state", "note"],
                [[colored, "x"], ["fresh", "y"]],
                width=40,
                stream=TTY,
            )
        stripped_lines = [re.sub(r"\x1b\[[0-9;]*m", "", line) for line in text.splitlines()]
        self.assertEqual(stripped_lines[2].index("x"), stripped_lines[3].index("y"))

    def test_an_overflowing_colored_cell_truncates_without_a_dangling_escape(self) -> None:
        """Slicing a colored cell's raw string could cut inside an escape
        sequence and drop its trailing reset code, leaking that cell's color
        into every line printed after it -- so an overflowing colored cell
        is truncated on its plain text instead, never on the raw string.
        """

        colored = console.style("A" * 60, console.WARN, stream=TTY)
        with patch.dict("os.environ", {}, clear=True):
            text = console.table(["title"], [[colored]], width=20, stream=TTY)
        row = text.splitlines()[2]
        self.assertNotIn("\x1b", row)
        self.assertTrue(row.endswith("…"))


class IndentTests(unittest.TestCase):
    def test_prefixes_every_nonblank_line(self) -> None:
        self.assertEqual("  a\n  b", console.indent("a\nb"))

    def test_leaves_blank_lines_untouched(self) -> None:
        self.assertEqual("  a\n\n  b", console.indent("a\n\nb"))


if __name__ == "__main__":
    unittest.main()
