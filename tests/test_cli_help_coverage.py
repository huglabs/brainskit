"""Every flag explains itself, and every subcommand names its global options.

26 flags carried no help text at all, and none of the 32 subcommands mentioned
`--json` or `--vault` -- the two options an agent needs before it can use any of
them. `bk <cmd> --help` is the only contract a caller reads.

Written as properties over the parser rather than as a list, so a flag added
later is covered without anyone remembering to extend a fixture.
"""

from __future__ import annotations

import argparse
import unittest

from brainskit.interfaces.cli import build_parser


def _subparsers(parser: argparse.ArgumentParser) -> dict[str, argparse.ArgumentParser]:
    found: dict[str, argparse.ArgumentParser] = {}
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            for name, sub in action.choices.items():
                found[name] = sub
    return found


def _walk(
    parser: argparse.ArgumentParser, prefix: str = ""
) -> list[tuple[str, argparse.ArgumentParser]]:
    out = [(prefix.strip(), parser)] if prefix else []
    for name, sub in _subparsers(parser).items():
        out.extend(_walk(sub, f"{prefix} {name}"))
    return out


class HelpCoverageTest(unittest.TestCase):
    def test_the_parser_tree_is_actually_walked(self) -> None:
        """Control: an empty walk makes every assertion below vacuous."""

        commands = _walk(build_parser())
        self.assertGreater(len(commands), 30)

    def test_every_option_has_help_text(self) -> None:
        bare: list[str] = []
        for command, parser in _walk(build_parser()):
            for action in parser._actions:
                if not action.option_strings:
                    continue
                if action.help in (None, ""):
                    bare.append(f"{command} {action.option_strings[0]}")
        self.assertEqual(sorted(bare), [], msg="flags with no help text")

    def test_every_positional_has_help_text(self) -> None:
        bare: list[str] = []
        for command, parser in _walk(build_parser()):
            for action in parser._actions:
                if action.option_strings or isinstance(
                    action, argparse._SubParsersAction
                ):
                    continue
                if action.help in (None, ""):
                    bare.append(f"{command} {action.dest}")
        self.assertEqual(sorted(bare), [], msg="positionals with no help text")

    def test_leaf_commands_document_the_global_options(self) -> None:
        """`--json` and `--vault` are stripped before parsing, so they never

        appear in a subcommand's own help unless the epilog says so. An agent
        reading `bk search --help` has no other way to learn they exist.
        """

        silent: list[str] = []
        for command, parser in _walk(build_parser()):
            if _subparsers(parser):
                continue  # a group, not a leaf
            text = (parser.epilog or "") + (parser.description or "")
            if "--json" not in text or "--vault" not in text:
                silent.append(command)
        self.assertEqual(sorted(silent), [], msg="leaf commands not naming --json/--vault")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
