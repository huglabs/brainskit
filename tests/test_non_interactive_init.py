"""A vault must be creatable without a terminal, from the docs alone.

Off a TTY, `bk init ./v` refused; the here-doc `docs/getting-started.md`
promised refused identically; and `--config` with `{}` listed nine missing keys
with no shapes, no template, no example and no next command. The only complete
specimen in the repository was the project's own vault, which a user never
receives.

That blocked CI, containers, agent-driven setup and any non-interactive shell --
precisely the audiences a local-first agent tool has.

The wizard could already assemble a valid policy; there was no way to get it out.
"""

from __future__ import annotations

import contextlib
import io
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from brainskit.domain.model import ValidationError, VaultConfig
from brainskit.interfaces import cli, onboarding


class PrintConfigTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()

    def print_config(self, *extra: str) -> str:
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = cli.main(["init", str(self.root / "v"), "--print-config", *extra])
        self.assertEqual(code, 0)
        return buffer.getvalue()

    def test_the_printed_policy_is_raw_json_not_the_result_envelope(self) -> None:
        """`--config` reads a policy, so `--print-config` must emit a policy."""

        payload = json.loads(self.print_config())
        self.assertNotIn("ok", payload)
        self.assertNotIn("result", payload)
        self.assertIn("branches", payload)

    def test_the_printed_policy_satisfies_the_schema(self) -> None:
        """The nine-missing-keys refusal must be unreachable from this output."""

        VaultConfig.from_dict(json.loads(self.print_config()))

    def test_the_round_trip_creates_a_usable_vault(self) -> None:
        """The documented flow, end to end, with no terminal anywhere."""

        policy_path = self.root / "policy.json"
        policy_path.write_text(self.print_config(), encoding="utf-8")
        code = cli.main(
            [
                "init",
                str(self.root / "v"),
                "--config",
                str(policy_path),
                "--skip-code-build",
                "--json",
            ]
        )
        self.assertEqual(code, 0)
        self.assertTrue((self.root / "v" / ".brain" / "config.json").is_file())

    def test_printing_creates_nothing(self) -> None:
        """It prints a policy; it must not be a disguised `init`."""

        self.print_config()
        self.assertFalse((self.root / "v").exists())

    def test_each_preset_is_valid(self) -> None:
        for preset in onboarding.PRESET_KEYS:
            with self.subTest(preset=preset):
                VaultConfig.from_dict(
                    json.loads(self.print_config("--preset", preset))
                )

    def test_an_unknown_preset_is_refused_by_name(self) -> None:
        """Control: the preset must actually select something."""

        with self.assertRaises(ValidationError) as refused:
            onboarding.default_policy(self.root, "nonsense")
        self.assertEqual(refused.exception.details["preset"], "nonsense")

    def test_presets_differ_from_one_another(self) -> None:
        """Control: if every preset were identical, the flag would be theatre."""

        layouts = {
            preset: sorted(json.loads(self.print_config("--preset", preset))["branches"])
            for preset in onboarding.PRESET_KEYS
        }
        self.assertGreater(len({tuple(v) for v in layouts.values()}), 1)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
