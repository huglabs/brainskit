"""The guided `bk init`, and the two ways its predecessor failed silently.

The wizard this replaces asked twenty questions to arrive at its own defaults,
and two of its failures were not matters of taste:

- it offered `qwen3:8b` for all six jobs without asking ollama what was
  installed, so accepting every default on a machine without that model
  produced a vault whose every LLM job would fail at first use;
- it validated nothing until `VaultConfig.from_dict` assembled the finished
  policy, so one late rejection -- pasting a partial integrations object, the
  natural way to enable a single integration -- discarded all twenty answers
  and created no vault.

So the tests here are mostly about *structure*: that an assembled policy is
always complete, that the job list is derived rather than restated, and that a
provider that is down is reported rather than assumed. The pty cases cover the
raw-mode input layer, which cannot be exercised any other way -- and one of
them is a regression test for a bug that made every arrow key read as "cancel".
"""

from __future__ import annotations

import fcntl
import os
import pty
import re
import select
import struct
import subprocess
import sys
import tempfile
import termios
import time
import unittest
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from brainskit.domain.model import INTEGRATION_NAMES, PrivacyMode
from brainskit.interfaces import onboarding, prompt
from brainskit.interfaces.onboarding import OllamaModel, OllamaProbe
from brainskit.interfaces.prompt import Choice

REPO_ROOT = Path(__file__).resolve().parents[1]

#: The last row of the browser's first screen: seeing it proves every row was
#: drawn, which can only happen once raw mode is in effect.
_GROUPS_DRAWN = "Show every command"


class PolicyAssemblyTest(unittest.TestCase):
    """A policy the wizard builds must be accepted by the domain, always.

    `_assemble` generates the integration set from `INTEGRATION_NAMES` rather
    than from whatever the operator named, which is what turns the old
    "integration policy set is incomplete" failure from *unlikely* into
    *unreachable*.
    """

    def environment(self, tmp: Path) -> onboarding.Environment:
        return onboarding.Environment(
            vault=tmp,
            workspace=tmp,
            is_git_repo=False,
            has_agent_dir=False,
            language="English",
        )

    def assemble(self, tmp: Path, extras: list[str], model: str | None = "m") -> dict:
        return onboarding._assemble(
            self.environment(tmp),
            OllamaProbe(base_url="http://127.0.0.1:11434", reachable=True),
            dict(onboarding.PRESETS[0].branches),
            model,
            extras,
        )

    def test_every_extras_combination_yields_a_config_the_domain_accepts(self) -> None:
        from itertools import chain, combinations

        from brainskit.domain.model import VaultConfig

        options = ["agent", "obsidian", "web"]
        every = chain.from_iterable(
            combinations(options, n) for n in range(len(options) + 1)
        )
        with tempfile.TemporaryDirectory() as name:
            for extras in every:
                policy = self.assemble(Path(name), list(extras))
                config = VaultConfig.from_dict(policy)
                self.assertEqual(
                    sorted(config.integrations), sorted(INTEGRATION_NAMES), extras
                )

    def test_job_models_covers_exactly_the_shipped_jobs(self) -> None:
        """Derived from `jobs/*.md`, so adding a job cannot leave it unconfigured."""

        shipped = {
            entry.name[:-3]
            for entry in (REPO_ROOT / "src" / "brainskit" / "jobs").iterdir()
            if entry.name.endswith(".md") and not entry.name.startswith("_")
        }
        self.assertEqual(set(onboarding.job_names()), shipped)
        with tempfile.TemporaryDirectory() as name:
            policy = self.assemble(Path(name), [])
        self.assertEqual(set(policy["job_models"]), shipped)

    def test_the_private_branch_is_never_ingest_in_every_preset(self) -> None:
        """The one choice that cannot be undone by editing config later.

        Anything already sent to a provider has been sent, so a preset that
        got this wrong would be unfixable after the fact.
        """

        for preset in onboarding.PRESETS:
            private = [
                name
                for name, (privacy, _) in preset.branches.items()
                if privacy is PrivacyMode.NEVER_INGEST
            ]
            self.assertTrue(private, f"{preset.key} has no never-ingest branch")

    def test_a_model_is_configured_even_when_none_was_chosen(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            policy = self.assemble(Path(name), [], model=None)
        self.assertTrue(all(spec["model"] for spec in policy["job_models"].values()))


class ProbeTest(unittest.TestCase):
    """Probing is advisory: it reports, it never raises."""

    def test_an_unreachable_provider_is_reported_not_raised(self) -> None:
        probe = onboarding.probe_ollama("http://127.0.0.1:1")
        self.assertFalse(probe.reachable)
        self.assertTrue(probe.error)

    def test_a_non_http_url_is_refused_before_any_request(self) -> None:
        """`urlopen` would happily read a `file:` path and call it a model list."""

        probe = onboarding.probe_ollama("file:///etc/passwd")
        self.assertFalse(probe.reachable)
        self.assertIn("http", probe.error)

    def test_models_without_tool_support_are_the_fallback_not_the_offer(self) -> None:
        probe = OllamaProbe(
            base_url="x",
            reachable=True,
            models=(
                OllamaModel("tools-yes", "7B", 32768, True),
                OllamaModel("tools-no", "3B", 8192, False),
            ),
        )
        self.assertEqual([m.name for m in probe.usable], ["tools-yes"])

    def test_when_nothing_supports_tools_every_model_is_still_offered(self) -> None:
        """Offering nothing would strand the operator; offering all is honest."""

        probe = OllamaProbe(
            base_url="x",
            reachable=True,
            models=(OllamaModel("only", "3B", 8192, False),),
        )
        self.assertEqual([m.name for m in probe.usable], ["only"])


class DetectionTest(unittest.TestCase):
    def test_the_language_comes_from_the_environment_not_a_hardcoded_default(
        self,
    ) -> None:
        """The old wizard defaulted every machine on earth to one language."""

        cases = {
            "en_US.UTF-8": "English",
            "pt_BR.UTF-8": "Portuguese (Brazil)",
            "es_ES.UTF-8": "Spanish",
            "zz_ZZ.UTF-8": "English",
        }
        for value, expected in cases.items():
            with patch.dict(os.environ, {"LANG": value, "LC_ALL": ""}, clear=False):
                self.assertEqual(onboarding._language(), expected, value)

    def test_the_workspace_is_the_repository_when_the_vault_is_nested(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            (root / ".git").mkdir()
            vault = root / "docs" / "brain"
            vault.mkdir(parents=True)
            environment = onboarding.detect(vault)
        self.assertEqual(environment.workspace.resolve(), root.resolve())
        self.assertTrue(environment.is_git_repo)


class BranchValidationTest(unittest.TestCase):
    """Validated at the prompt that produced it, not when the policy assembles."""

    def test_a_reserved_or_duplicate_or_empty_name_is_rejected(self) -> None:
        self.assertIsNotNone(onboarding._valid_branches(""))
        self.assertIsNotNone(onboarding._valid_branches("a,a"))
        self.assertIsNotNone(onboarding._valid_branches("_inbox"))

    def test_a_reasonable_list_is_accepted(self) -> None:
        self.assertIsNone(onboarding._valid_branches("10-work, 90-private"))


class FallbackTest(unittest.TestCase):
    """Off a terminal every prompt degrades to numbered input rather than failing.

    This is the path tests and here-docs take, and it is why nothing in the
    wizard needs a pty to be exercised.
    """

    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)

    def test_select_falls_back_to_the_default_on_empty_input(self) -> None:
        with patch("sys.stdin", StringIO("\n")), patch("sys.stdout", StringIO()):
            picked = prompt.select(
                "pick", [Choice("a", "A"), Choice("b", "B")], default=1
            )
        self.assertEqual(picked, "b")

    def test_select_honours_a_typed_number(self) -> None:
        with patch("builtins.input", return_value="1"), patch("sys.stdout", StringIO()):
            picked = prompt.select("pick", [Choice("a", "A"), Choice("b", "B")])
        self.assertEqual(picked, "a")

    def test_multiselect_parses_a_comma_separated_answer(self) -> None:
        with patch("builtins.input", return_value="1,3"), patch(
            "sys.stdout", StringIO()
        ):
            picked = prompt.multiselect(
                "pick",
                [Choice("a", "A"), Choice("b", "B"), Choice("c", "C")],
            )
        self.assertEqual(picked, ["a", "c"])

    def test_text_reasks_until_validation_passes(self) -> None:
        answers = iter(["", "ok"])
        problems = iter([None])

        def validate(value: str) -> str | None:
            return None if value == "ok" else "no"

        with patch("builtins.input", lambda _: next(answers)), patch(
            "sys.stdout", StringIO()
        ):
            self.assertEqual(prompt.text("q", "", validate=validate), "ok")
        self.assertIsNone(next(problems))

    def test_text_gives_up_when_the_input_stream_ends(self) -> None:
        """EOF cannot become a valid answer, so re-asking loops forever.

        `bk search` with no argument asked for a query, took Ctrl-D as the
        empty string, failed the required-value check and asked again --
        printing "This one is required." until the process was killed. The
        fix is not to accept a rejected default but to stop asking.
        """

        def eof(_: str) -> str:
            raise EOFError

        with patch("builtins.input", eof), patch("sys.stdout", StringIO()):
            with self.assertRaises(prompt.Cancelled):
                prompt.text("q", "", validate=lambda value: value or "required")

    def test_text_still_accepts_a_default_that_validates_at_eof(self) -> None:
        # What a here-doc pressing Enter through a wizard relies on: the stream
        # ending is only fatal when the answer it leaves behind is unusable.
        with patch("builtins.input", self._raise_eof), patch("sys.stdout", StringIO()):
            self.assertEqual(
                prompt.text("q", "fallback", validate=lambda _: None), "fallback"
            )

    def test_text_normalises_before_it_validates(self) -> None:
        """The ordering is the bug, not the normalisation.

        Reported live: a PDF dragged into the terminal arrived quoted, and
        `capture`'s validator ran on the raw string. It rejected a path that
        exists -- "No file at '…'", quotes included -- and re-asked, with no
        input that could ever satisfy it, because unquoting was applied to the
        *return* value of a call that never returned.
        """

        target = Path(self._temp.name) / "Vant · relatório de negócio.pdf"
        target.write_text("x")
        seen: list[str] = []

        def validate(value: str) -> str | None:
            seen.append(value)
            return None if Path(value).is_file() else "no such file"

        with patch("builtins.input", lambda _: f"'{target}'"), patch(
            "sys.stdout", StringIO()
        ):
            self.assertEqual(prompt.text("value", "", validate=validate), str(target))
        self.assertEqual(seen, [str(target)])

    @staticmethod
    def _raise_eof(_: str) -> str:
        raise EOFError

    def test_select_falls_back_to_an_enabled_row(self) -> None:
        # Off a terminal the fallback took `default` verbatim, so an index that
        # is out of range or disabled answered with the wrong choice -- or
        # raised IndexError inside a prompt.
        choices = [Choice("a", "A", enabled=False), Choice("b", "B")]
        with patch("builtins.input", self._raise_eof), patch("sys.stdout", StringIO()):
            self.assertEqual(prompt.select("q", choices, stream=StringIO()), "b")

    def test_interactive_is_refused_when_stdout_is_not_a_terminal(self) -> None:
        self.assertFalse(prompt.supports_interactive(stream=StringIO()))


def _drive(
    child: str,
    keys: list[bytes],
    *,
    want: str = "",
    size: tuple[int, int] | None = None,
    leave_running: bool = False,
) -> str:
    """Run `child` under a pty, sending `keys` one screen at a time.

    Each key waits for the child to stop producing output before being sent,
    rather than for a fixed delay or a single marker. Both of the simpler
    approaches are races against the same thing: a prompt prints its question
    line *before* `_RawTerminal` is entered, so a key delivered in that window
    lands on a still-canonical tty, where Esc is buffered awaiting a newline
    that never comes and the child hangs until the test times out. Going quiet
    is the only signal that the child is actually blocked reading a key -- and
    unlike a marker, it re-synchronises on every screen, which is what a
    multi-level browser needs.
    """

    master, slave = pty.openpty()
    if size is not None:
        # Set the pty's own window size, and drop COLUMNS/LINES so the child's
        # `shutil.get_terminal_size` reads the ioctl rather than the env --
        # otherwise a test cannot vary the terminal it is rendering for.
        fcntl.ioctl(
            slave, termios.TIOCSWINSZ, struct.pack("HHHH", size[0], size[1], 0, 0)
        )
    env = dict(os.environ, TERM="xterm-256color")
    if size is None:
        env.update(COLUMNS="80", LINES="24")
    else:
        env.pop("COLUMNS", None)
        env.pop("LINES", None)
    process = subprocess.Popen(
        [sys.executable, "-c", child],
        stdin=slave,
        stdout=slave,
        stderr=slave,
        close_fds=True,
        env=env,
        cwd=str(REPO_ROOT),
    )
    os.close(slave)
    chunks: list[str] = []
    deadline = time.time() + 30

    def pump(timeout: float) -> bool:
        if select.select([master], [], [], timeout)[0]:
            try:
                data = os.read(master, 8192)
            except OSError:
                return False
            if data:
                chunks.append(data.decode("utf-8", "replace"))
                return True
        return False

    def settled(idle: float = 0.4) -> None:
        """Wait until nothing has been written for `idle`, and something has.

        The "and something has" clause matters: the child is also silent while
        it imports, which is long enough to look like a settled prompt.
        """

        last = time.time()
        while time.time() < deadline:
            if pump(0.1):
                last = time.time()
                continue
            if chunks and (want in "".join(chunks) if want else True):
                if time.time() - last >= idle:
                    return

    for key in keys:
        settled()
        os.write(master, key)
    if leave_running:
        # Stop while the prompt is still painted. Letting it exit would run the
        # cleanup that erases exactly the rows under inspection, so a corrupt
        # screen would tidy itself up before it could be asserted on.
        settled()
        process.kill()
        process.wait(timeout=10)
        os.close(master)
        return "".join(chunks)
    while time.time() < deadline and process.poll() is None:
        pump(0.3)
    pump(0.3)
    process.wait(timeout=10)
    os.close(master)
    return "".join(chunks)


_RAW_PRELUDE = """
import sys, termios
sys.path.insert(0, "src")
from brainskit.interfaces import prompt
from brainskit.interfaces.prompt import Choice
"""


@unittest.skipUnless(hasattr(os, "openpty"), "needs a pty")
class RawModeTest(unittest.TestCase):
    """The parts that only exist when a real terminal is attached."""

    def test_arrow_keys_move_the_cursor_rather_than_cancelling(self) -> None:
        """Regression: every arrow press used to read as Esc, i.e. "cancel".

        `sys.stdin.read(1)` pulls the rest of `\\x1b[B` into `TextIOWrapper`'s
        buffer, so a `select()` on the file descriptor -- which only sees the
        OS buffer -- reported nothing pending and the sequence was resolved as
        a bare Esc. Reading the fd directly is what makes this pass.
        """

        child = _RAW_PRELUDE + """
picked = prompt.select("Pick", [Choice("a", "first"), Choice("b", "second")])
print("PICKED:" + str(picked))
"""
        output = _drive(child, [b"\x1b[B", b"\r"], want="first")
        self.assertIn("PICKED:b", output)

    def test_ctrl_c_cancels_and_restores_the_line_discipline(self) -> None:
        """A prompt that leaks raw mode leaves the operator without an echo.

        `PENDIN` is excluded from the comparison deliberately: it is a
        transient kernel status bit meaning "input is pending", which is true
        precisely because a key was just sent, and is not a setting anyone
        restores.
        """

        child = _RAW_PRELUDE + """
fd = sys.stdin.fileno()
before = termios.tcgetattr(fd)
try:
    prompt.select("Pick", [Choice("a", "first"), Choice("b", "second")])
    print("OUTCOME:returned")
except prompt.Cancelled:
    print("OUTCOME:cancelled")
after = termios.tcgetattr(fd)
mask = ~termios.PENDIN
same = (
    before[:3] == after[:3]
    and before[3] & mask == after[3] & mask
    and before[4:] == after[4:]
)
print("RESTORED:" + str(same))
print("ECHO:" + str(bool(after[3] & termios.ECHO)))
"""
        output = _drive(child, [b"\x03"], want="first")
        self.assertIn("OUTCOME:cancelled", output)
        self.assertIn("RESTORED:True", output)
        self.assertIn("ECHO:True", output)

    def test_space_toggles_a_checkbox_without_submitting(self) -> None:
        child = _RAW_PRELUDE + """
picked = prompt.multiselect(
    "Pick", [Choice("a", "first"), Choice("b", "second")], selected=[]
)
print("PICKED:" + ",".join(str(p) for p in picked))
"""
        output = _drive(child, [b" ", b"\x1b[B", b" ", b"\r"], want="first")
        self.assertIn("PICKED:a,b", output)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()


class TopLevelHelpTest(unittest.TestCase):
    """A bare `bk`, and a mistyped command, are the two first impressions.

    Argparse answers both by reprinting all thirty command names -- once as a
    brace-delimited usage blob, and again in `invalid choice: (choose from ...)`
    -- which is the least useful reply available at exactly the moment someone
    is trying to get their bearings.
    """

    def run_cli(self, argv: list[str]) -> tuple[int, str, str]:
        from contextlib import redirect_stderr, redirect_stdout

        from brainskit.interfaces import cli

        out, err = StringIO(), StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            try:
                code = cli.main(argv)
            except SystemExit as exit_:  # argparse's own failures
                code = int(exit_.code or 0)
        return code, out.getvalue(), err.getvalue()

    def test_a_bare_bk_prints_the_grouped_help_and_succeeds(self) -> None:
        code, out, _ = self.run_cli([])
        self.assertEqual(code, 0)
        self.assertIn("Vault & capture", out)
        self.assertIn("init", out)

    def test_the_usage_line_does_not_enumerate_every_command(self) -> None:
        """The grouped listing *is* the enumeration; the usage line shows shape."""

        _, out, _ = self.run_cli([])
        usage = next(line for line in out.splitlines() if line.startswith("usage:"))
        self.assertIn("<command>", usage)
        self.assertNotIn("reconcile", usage)

    def test_version_still_reaches_argparse(self) -> None:
        """`--version` exhausts the token scan without naming a command.

        It must not be mistaken for "no command given" and answered with help.
        """

        code, out, _ = self.run_cli(["--version"])
        self.assertEqual(code, 0)
        self.assertIn("bk", out)
        self.assertNotIn("Vault & capture", out)

    def test_a_mistyped_command_suggests_the_close_one(self) -> None:
        code, _, err = self.run_cli(["stat"])
        self.assertEqual(code, 2)
        self.assertIn("is not a bk command", err)
        self.assertIn("status", err)

    def test_a_prefix_is_suggested_even_when_the_ratio_is_poor(self) -> None:
        """`bk co` is a truncation, which `get_close_matches` alone scores badly."""

        _, _, err = self.run_cli(["co"])
        self.assertIn("code", err)
        self.assertIn("context", err)

    def test_an_unrecognisable_command_still_points_somewhere(self) -> None:
        code, _, err = self.run_cli(["zzzzz"])
        self.assertEqual(code, 2)
        self.assertIn("Run `bk`", err)

    def test_argparse_keeps_ownership_of_every_other_argument_error(self) -> None:
        """A valid command with a bad flag is the subparser's message to write."""

        _, _, err = self.run_cli(["status", "--bogusflag"])
        self.assertIn("unrecognized arguments", err)


class DashLeadingQueryTest(unittest.TestCase):
    """`bk search -retrieval` used to die in "arguments are required: query".

    argparse refuses any positional beginning with `-`, so a negation-shaped
    term -- a thing FTS5 users legitimately type -- could not be passed at all,
    and the only spelling argparse accepts (`--` after every flag) is one
    nobody knows. The CLI now rewrites exactly that shape before parsing.
    """

    def setUp(self) -> None:
        from brainskit.interfaces.cli import build_parser

        self.parser = build_parser()

    def hoist(self, argv: list[str]) -> list[str]:
        from brainskit.interfaces.cli import _hoist_dash_leading_query

        return _hoist_dash_leading_query(argv, self.parser)

    def test_a_dash_leading_query_is_moved_behind_the_separator(self) -> None:
        self.assertEqual(
            self.hoist(["search", "-retrieval"]), ["search", "--", "-retrieval"]
        )

    def test_flags_between_the_command_and_the_query_keep_their_order(
        self,
    ) -> None:
        self.assertEqual(
            self.hoist(["search", "-retrieval", "--consumer", "local"]),
            ["search", "--consumer", "local", "--", "-retrieval"],
        )

    def test_a_value_taking_flag_consumes_a_dash_leading_value(self) -> None:
        # `--limit -1` means a limit of minus one (and errors later); it must
        # not be read as a query plus an unknown flag.
        argv = ["search", "--limit", "-1", "-alpha"]
        self.assertEqual(self.hoist(argv), ["search", "--limit", "-1", "--", "-alpha"])

    def test_commands_outside_the_free_text_set_are_untouched(self) -> None:
        # `capture -` is the stdin marker; its argv is argparse's business.
        argv = ["capture", "-", "--json"]
        self.assertEqual(self.hoist(argv), argv)

    def test_a_real_positional_present_means_no_rewrite(self) -> None:
        # A mistyped flag beside a real query must stay argparse's error, not
        # silently become part of the query.
        argv = ["search", "alpha", "--conusmer", "local"]
        self.assertEqual(self.hoist(argv), argv)

    def test_no_query_and_no_dash_token_changes_nothing(self) -> None:
        self.assertEqual(self.hoist(["search", "--consumer", "local"]),
                         ["search", "--consumer", "local"])

    def test_an_explicit_separator_is_left_alone(self) -> None:
        # Someone who already spelled `--` knows what they are doing; the
        # rewrite must not reorder around their separator.
        argv = ["search", "--", "-retrieval"]
        self.assertEqual(self.hoist(argv), argv)

    def test_every_rewritten_argv_parses_to_the_intended_query(self) -> None:
        for original, dest, query in (
            (["search", "-retrieval"], "query", "-retrieval"),
            (["context", "-x", "--consumer", "cloud"], "query", "-x"),
            (["ask", "-why", "--save"], "question", "-why"),
        ):
            with self.subTest(argv=original):
                rewritten = self.hoist(original)
                args = self.parser.parse_args(rewritten)
                self.assertEqual(getattr(args, dest), query)

    def test_the_transform_is_the_whole_fix_end_to_end(self) -> None:
        """Negative control: without the rewrite, argparse rejects the query."""

        self.parser.exit = lambda status=0, message="": (_ for _ in ()).throw(
            SystemExit(2)
        )  # type: ignore[method-assign]
        with self.assertRaises(SystemExit):
            self.parser.parse_args(["search", "-retrieval"])


@unittest.skipUnless(hasattr(os, "openpty"), "needs a pty")
class CommandBrowserTest(unittest.TestCase):
    """Bare `bk` at a terminal discloses progressively instead of dumping 30 rows.

    The flat listing is still what a pipe, a redirect and `bk -h` get, because
    those are read, grepped and scripted against -- so every case here is about
    the *terminal* path being different without the text path changing.
    """

    #: A *rendered row*, not the question line. The question is printed before
    #: `_RawTerminal` is entered, so keying off it races the terminal into raw
    #: mode: an Esc arriving while the tty is still canonical is buffered
    #: waiting for a newline that never comes, and the child hangs.
    CHILD = """
import sys
sys.path.insert(0, "src")
from brainskit.interfaces import cli
raise SystemExit(cli.main([]))
"""

    def test_the_first_screen_offers_groups_not_every_command(self) -> None:
        output = _drive(self.CHILD, [b"\x1b"], want=_GROUPS_DRAWN)
        self.assertIn("Vault & capture", output)
        self.assertIn("Governance", output)
        # A command that only appears once you drill in must not be on screen one.
        self.assertNotIn("reconcile", output)

    def test_choosing_a_group_reveals_its_commands(self) -> None:
        output = _drive(
            self.CHILD, [b"\r", b"\x1b", b"\x1b"], want=_GROUPS_DRAWN
        )
        self.assertIn("reconcile", output)
        self.assertIn("Esc goes back", output)

    def test_escape_at_the_top_level_leaves_without_an_error(self) -> None:
        output = _drive(self.CHILD, [b"\x1b"], want=_GROUPS_DRAWN)
        self.assertNotIn("Traceback", output)
        self.assertNotIn("error", output.lower())

    def test_selecting_a_command_shows_it_rather_than_running_it(self) -> None:
        """`bk` is typed by people who do not yet know what these do.

        Selecting `capture` prints its help and then *asks* what to do — it
        must never capture anything on the strength of having been highlighted.
        The browser closing its own loop (`Run it now`) is a second, explicit
        choice; what this pins is that arriving at the command is not one.
        """

        # Enter opens the first group, Down+Enter picks `capture`, then two
        # Escapes back out of the follow-up prompt and the browser itself --
        # which is the point: every screen has a way out.
        output = _drive(
            self.CHILD,
            [b"\r", b"\x1b[B", b"\r", b"\x1b", b"\x1b"],
            want=_GROUPS_DRAWN,
        )
        self.assertIn("usage: bk capture", output)
        # The follow-up prompt, not an executed command.
        self.assertIn("What next?", output)
        self.assertNotIn("created", output.lower())

    def test_the_run_option_is_withheld_from_destructive_commands(self) -> None:
        """`forget` is shown as a command line to run deliberately, never run.

        A keystroke reached by wandering through a menu is not the right way to
        drop a source, which is the same standard `--force` already holds these
        commands to.
        """

        from brainskit.interfaces import cli

        self.assertIn("forget", cli._DESTRUCTIVE)
        for command in cli._DESTRUCTIVE:
            self.assertIn(
                command,
                {name for _, names in cli.HELP_CATEGORIES for name in names},
                "a destructive command must still be reachable to be shown",
            )


class SubcommandHelpTest(unittest.TestCase):
    def test_it_delegates_to_argparse_rather_than_restating_flags(self) -> None:
        """One source of truth: what the browser shows is `bk <cmd> --help`."""

        from brainskit.interfaces import cli

        parser = cli.build_parser()
        rendered = cli._subcommand_help(parser, "hooks")
        self.assertIn("usage: bk hooks", rendered)
        self.assertEqual(cli._subcommand_help(parser, "nosuchcommand"), "")


_SGR = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]")


class Screen:
    """Just enough ANSI to catch redraw-arithmetic bugs.

    Two behaviours are the whole point, and a raw byte-stream assertion sees
    neither: the cursor **clamps** at row 0, so an up-move that overshoots
    silently does less than it was told, and text **wraps**, so an over-wide
    row occupies physical lines the redraw never counted. A prompt can emit a
    perfectly balanced stream of bytes and still paint a mangled screen.
    """

    def __init__(self, rows: int, cols: int) -> None:
        self.rows, self.cols = rows, cols
        self.buf = [[" "] * cols for _ in range(rows)]
        self.r = self.c = 0

    def _scroll(self) -> None:
        self.buf.pop(0)
        self.buf.append([" "] * self.cols)

    def _newline(self) -> None:
        self.r += 1
        if self.r >= self.rows:
            self._scroll()
            self.r = self.rows - 1

    def feed(self, data: str) -> Screen:
        i = 0
        while i < len(data):
            ch = data[i]
            if ch == "\x1b":
                match = _SGR.match(data, i)
                if not match:
                    i += 1
                    continue
                seq = match.group(0)
                i = match.end()
                body, kind = seq[2:-1], seq[-1]
                if kind == "A":
                    self.r = max(0, self.r - int(body or 1))
                elif kind == "K" and body == "2":
                    self.buf[self.r] = [" "] * self.cols
                continue
            i += 1
            if ch == "\r":
                self.c = 0
            elif ch == "\n":
                self._newline()
            else:
                if self.c >= self.cols:
                    self.c = 0
                    self._newline()
                self.buf[self.r][self.c] = ch
                self.c += 1
        return self

    def text(self) -> str:
        return "\n".join("".join(row).rstrip() for row in self.buf)


class RowFittingTest(unittest.TestCase):
    """Every rendered row must occupy exactly one physical terminal line.

    `_redraw` moves the cursor up by the number of lines it wrote. A row wider
    than the terminal wraps onto a second physical line that count knows
    nothing about, so from then on every redraw paints over the wrong rows.
    `bk forget`'s help is 140 characters, which made the single group
    containing it corrupt on any normal-width terminal.
    """

    def rows_for_every_group(self, width: int) -> list[str]:
        from brainskit.interfaces import cli

        parser = cli.build_parser()
        index = cli._command_help_index(parser)
        rendered: list[str] = []
        for _, names in cli.HELP_CATEGORIES:
            choices = [Choice(n, n, index.get(n, "")) for n in names]
            rendered += prompt._render_rows(
                choices, 0, ["◆"] * len(choices), width=width
            )
        return rendered

    def test_no_real_command_row_can_overflow_any_plausible_terminal(self) -> None:
        from brainskit.interfaces import console

        for width in (40, 60, 80, 100, 120):
            for row in self.rows_for_every_group(width):
                self.assertLess(
                    console._visible_len(row),
                    width,
                    f"row overflows a {width}-column terminal: {row!r}",
                )

    def test_the_longest_help_string_is_the_one_that_used_to_break_it(self) -> None:
        """A guard on the premise, so this stays honest if the help text changes."""

        from brainskit.interfaces import cli

        index = cli._command_help_index(cli.build_parser())
        self.assertGreater(len(index["forget"]), 100)

    def test_truncation_keeps_colour_from_leaking_past_the_cut(self) -> None:
        from brainskit.interfaces import console

        coloured = console.style("x" * 50, console.ACCENT, stream=None)
        self.assertNotIn("\x1b", console.truncate(coloured, 10))


class ViewportTest(unittest.TestCase):
    """A list taller than the screen cannot be redrawn by cursor arithmetic."""

    def test_a_short_list_is_shown_whole(self) -> None:
        self.assertEqual(prompt._viewport(5, 0, 20), (0, 5))

    def test_a_long_list_is_windowed_to_the_height(self) -> None:
        start, end = prompt._viewport(40, 20, 10)
        self.assertEqual(end - start, 10)

    def test_the_cursor_is_always_inside_the_window(self) -> None:
        for cursor in range(40):
            start, end = prompt._viewport(40, cursor, 10)
            self.assertTrue(start <= cursor < end, cursor)

    def test_the_window_never_runs_past_either_end(self) -> None:
        for cursor in range(40):
            start, end = prompt._viewport(40, cursor, 10)
            self.assertGreaterEqual(start, 0)
            self.assertLessEqual(end, 40)


@unittest.skipUnless(hasattr(os, "openpty"), "needs a pty")
class ScreenIntegrityTest(unittest.TestCase):
    """What the operator actually sees, on a terminal the size they actually use."""

    def screen_after_browsing(self, rows: int, cols: int) -> str:
        child = """
import sys
sys.path.insert(0, "src")
from brainskit.interfaces import cli
raise SystemExit(cli.main([]))
"""
        # Enter the first group -- the one holding the 140-character `forget`
        # help -- and move within it, stopping while the prompt is still on
        # screen. Letting it exit would erase the very rows under test.
        output = _drive(
            child,
            [b"\r", b"\x1b[B", b"\x1b[B", b"\x1b[B"],
            size=(rows, cols),
            leave_running=True,
        )
        return Screen(rows, cols).feed(output).text()

    def test_the_screen_holds_exactly_one_row_per_command(self) -> None:
        """The count is the assertion that survives both shapes of corruption.

        A desynchronised redraw shows up as duplicated rows or as a staircase
        of rows painted at growing columns, depending on where the terminal
        happened to be scrolled. Either way the screen ends up with more rows
        than the group has commands, so counting them catches what an
        alignment check alone can miss.
        """

        from brainskit.interfaces import cli

        expected = len(cli.HELP_CATEGORIES[0][1])
        screen = self.screen_after_browsing(24, 80)
        drawn = [
            line for line in screen.splitlines() if "◆" in line or "◇" in line
        ]
        self.assertEqual(
            len(drawn),
            expected,
            f"{len(drawn)} rows on screen for a {expected}-command group: "
            "a redraw painted over the wrong lines",
        )

    def test_each_command_appears_exactly_once(self) -> None:
        screen = self.screen_after_browsing(24, 80)
        for command in ("capture", "status", "reconcile", "watch", "lint"):
            self.assertLessEqual(
                screen.count(f"◇ {command}") + screen.count(f"◆ {command}"),
                1,
                f"{command} was painted more than once",
            )
