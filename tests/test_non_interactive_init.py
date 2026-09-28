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

try:
    from . import _harness
except ImportError:
    import _harness

import json
import re
import shlex
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from brainskit.domain.model import NotConfiguredError, ValidationError, VaultConfig
from brainskit.interfaces import onboarding


class PrintConfigTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()

    def print_config(self, *extra: str) -> str:
        run = _harness.run_cli(["init", str(self.root / "v"), "--print-config", *extra])
        self.assertEqual(run.code, 0)
        return run.stdout

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
        run = _harness.run_cli(
            [
                "init",
                str(self.root / "v"),
                "--config",
                str(policy_path),
                "--skip-code-build",
                "--json",
            ]
        )
        self.assertEqual(run.code, 0, run.output)
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


class NextStepsRunVerbatimTest(unittest.TestCase):
    """Every command `bk init` ends on must work from where it leaves you.

    They were printed bare. `bk init ./my-vault` leaves you one level above the
    vault, where discovery does not look, so all three failed with "No
    brainskit vault found" -- the wizard's closing instructions were the one
    part of the quickstart that did not work. `run_cli` refuses exactly that
    output, so each run below fails loudly if a suggestion regresses.
    """

    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.cwd = Path(self.temporary.name).resolve()
        (self.cwd / "note.md").write_text("# A note\n\nSomething worth keeping.\n", "utf-8")

    def init(self, path: str) -> list[str]:
        policy = self.cwd / "policy.json"
        run = _harness.run_cli(["init", path, "--print-config"], cwd=self.cwd)
        policy.write_text(run.stdout, encoding="utf-8")
        run = _harness.run_cli(
            ["init", path, "--config", str(policy), "--skip-code-build"], cwd=self.cwd
        )
        self.assertEqual(run.code, 0, run.output)
        lines = run.stdout.split("Next", 1)[1].splitlines()
        steps = [
            match.group(1)
            for line in lines
            if (match := re.match(r"\s*→ (.+?)\s{2,}\S", line))
        ]
        self.assertEqual(len(steps), 4, run.stdout)
        return steps

    def run_step(self, command: str) -> _harness.CliRun:
        argv = shlex.split(command)
        self.assertEqual(argv[0], "bk")
        stand_ins = {"<file>": "note.md", "...": "what is in here?", "<branch>": "10-work"}
        argv = [stand_ins.get(arg, arg) for arg in argv[1:]]
        # `ask` needs a model; which one answers is not what this pins. The stub
        # stands where the provider would, so reaching it proves the command
        # found the vault.
        unreachable = NotConfiguredError("Provider is unreachable")
        with patch("brainskit.infrastructure.llm._post_json", side_effect=unreachable):
            return _harness.run_cli(argv, cwd=self.cwd)

    def assert_every_step_runs(self, steps: list[str]) -> None:
        for command in steps:
            with self.subTest(command=command):
                run = self.run_step(command)
                if command.startswith("bk ask"):
                    self.assertIn("Provider is unreachable", run.output)
                else:
                    self.assertEqual(run.code, 0, run.output)
                if command.startswith("bk capture"):
                    # The capture's own `next` line is the same trap one step on.
                    match = re.search(r"next  (bk file .+)", run.stdout)
                    self.assertIsNotNone(match, run.stdout)
                    assert match is not None
                    filed = self.run_step(match.group(1))
                    self.assertEqual(filed.code, 0, filed.output)

    def test_a_vault_below_the_current_directory(self) -> None:
        steps = self.init("./my-vault")
        self.assertTrue(all("--vault my-vault" in step for step in steps), steps)
        self.assert_every_step_runs(steps)
        self.assertTrue((self.cwd / "my-vault" / "CLAUDE.md").is_file())

    def test_a_vault_discovery_finds_needs_no_flag(self) -> None:
        """Control: the flag is added where it is needed, not everywhere."""

        subprocess.run(["git", "init", "-q"], cwd=self.cwd, check=True)
        steps = self.init(".brainskit")
        self.assertEqual(
            steps[0], "bk hooks install --agent claude --root .", steps
        )
        self.assertTrue(all("--vault" not in step for step in steps), steps)
        self.assert_every_step_runs(steps)
        # `--root .`: without it `hooks install` defaults to the vault, and an
        # agent opened on the repository would load none of what it wrote.
        self.assertTrue((self.cwd / "CLAUDE.md").is_file())
        self.assertFalse((self.cwd / ".brainskit" / "CLAUDE.md").exists())


class SmallIngestModelWarningTest(unittest.TestCase):
    """`bk init` names an ingest model too small to hold the citation contract.

    A 3B model answers ingest on a short note and fails `citation_mismatch` on
    every repair attempt once the source is long, so the first sign of the
    problem was a refused ingest days later. The provider URL is a closed port,
    so a probe here is a refusal on loopback, never the operator's ollama.
    """

    CLOSED = "http://127.0.0.1:1"

    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()

    def init(self, model: str, *extra: str) -> _harness.CliRun:
        environment = onboarding.Environment(
            vault=self.root / "v",
            workspace=self.root,
            is_git_repo=False,
            has_agent_dir=False,
            language="English",
        )
        choice = onboarding.ModelChoice(provider="ollama", model=model, base_url=self.CLOSED)
        policy = onboarding._assemble(
            environment, choice, dict(onboarding.PRESETS[0].branches), []
        )
        path = self.root / "policy.json"
        path.write_text(json.dumps(policy), encoding="utf-8")
        run = _harness.run_cli(
            ["init", str(self.root / "v"), "--config", str(path), "--skip-code-build", *extra]
        )
        self.assertEqual(run.code, 0, run.output)
        return run

    def test_a_small_tagged_model_warns_in_json(self) -> None:
        warnings = self.init("qwen2.5:3b", "--json").json()["result"]["warnings"]
        self.assertEqual(len(warnings), 1)
        self.assertEqual(warnings[0]["model"], "qwen2.5:3b")
        self.assertEqual(warnings[0]["parameters_billions"], 3.0)
        self.assertIn("job_models.ingest", warnings[0]["message"])

    def test_a_small_model_warns_on_the_human_screen(self) -> None:
        stdout = self.init("qwen2.5:3b").stdout
        self.assertIn("ingest may fail on longer sources", stdout)
        self.assertIn("route job_models.ingest to a larger model", stdout)

    def test_a_large_model_does_not_warn(self) -> None:
        self.assertEqual(self.init("qwen2.5:14b", "--json").json()["result"]["warnings"], [])

    def test_what_ollama_reports_sizes_an_untagged_name(self) -> None:
        reported = onboarding.OllamaProbe(
            base_url=self.CLOSED,
            reachable=True,
            models=(onboarding.OllamaModel("mystery:latest", "3.2B", 0, True),),
        )
        with patch.object(onboarding, "probe_ollama", return_value=reported):
            warnings = self.init("mystery", "--json").json()["result"]["warnings"]
        self.assertEqual([w["parameters_billions"] for w in warnings], [3.2])

    def test_an_unknown_size_does_not_warn(self) -> None:
        """No tag and nothing reported (ollama down): unknown is not small."""

        run = self.init("mystery", "--json")
        self.assertEqual(run.json()["result"]["warnings"], [])

    def test_sizes_come_from_the_report_then_the_tag(self) -> None:
        self.assertEqual(onboarding.ollama_model_billions("llama3.2:1b-instruct-q4_K_M"), 1.0)
        self.assertEqual(onboarding.ollama_model_billions("deepseek-r1:1.5b"), 1.5)
        self.assertIsNone(onboarding.ollama_model_billions("mixtral:8x7b"))
        self.assertIsNone(onboarding.ollama_model_billions("qwen3:latest"))
        probe = onboarding.OllamaProbe(
            base_url=self.CLOSED,
            reachable=True,
            models=(onboarding.OllamaModel("smollm2:latest", "135M", 0, True),),
        )
        self.assertEqual(onboarding.ollama_model_billions("smollm2", probe), 0.135)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
