"""`bk status` must describe the agent that was installed, not always `claude`.

`bk hooks install` is agent-aware: `--agent codex` writes `AGENTS.md`, records
its workspace in `.brain/agent-codex.json`, and installs no Claude Code hooks
because brainskit ships none for that agent. `Health` was not: it read the
workspace from `agent-claude.json`, looked for `CLAUDE.md`, and named two hook
scripts by hand.

So a codex install reported the whole enforcement picture for an agent that was
never installed -- a live `commit_lint` hook read as "not a git repository"
because the workspace resolution silently fell back to the vault, the managed
block read as missing because the reader opened the wrong file, and two layers
brainskit does not install for codex read as guards that had fallen off. Every
one of those is the writer and the reader disagreeing in silence about what
"installed" means, which is the divergence `ConstantsHaveOneOwnerTest` exists to
prevent and did not cover, because it guards sentinel strings rather than the
layer table.

These drive real installs through `cli._install_hooks` and then ask `bk status`,
so the two sides are compared rather than each being asserted against a fixture.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr
from io import StringIO
from pathlib import Path
from typing import Any

from test_engine import policy

from brainskit.application import install
from brainskit.application.services import BrainskitService
from brainskit.infrastructure.graph import MarkdownGraph
from brainskit.infrastructure.index import SqliteFtsIndex
from brainskit.infrastructure.vault import FileVault
from brainskit.interfaces import cli


class InstalledAgentCase(unittest.TestCase):
    """A vault nested in a git project, the layout `--root` exists for."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.project = Path(self.temporary.name).resolve()
        subprocess.run(["git", "init", "--quiet"], cwd=self.project, check=True)
        self.vault = FileVault.initialize(self.project / "docs" / "brain", policy())
        self.service = BrainskitService(
            self.vault, SqliteFtsIndex(self.vault.index_path), graph=MarkdownGraph()
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def install(self, agent: str) -> dict[str, Any]:
        with redirect_stderr(StringIO()):
            return cli._install_hooks(
                self.service, agent, root=str(self.project), skip_code_build=True
            )

    def enforcement(self) -> dict[str, Any]:
        report = self.service.status()["enforcement"]
        assert isinstance(report, dict)
        return report

    def layers(self) -> dict[str, dict[str, Any]]:
        return {entry["layer"]: entry for entry in self.enforcement()["layers"]}


class CodexInstallIsReportedAsCodexTest(InstalledAgentCase):
    def test_the_instructions_layer_names_the_file_that_was_written(self) -> None:
        written = self.install("codex")["instructions"]["path"]
        self.assertTrue(written.endswith("AGENTS.md"))
        entry = self.layers()[install.INSTRUCTIONS]
        self.assertEqual("AGENTS.md managed block", entry["mechanism"])
        self.assertTrue(entry["active"], entry["detail"])

    def test_the_pre_commit_hook_the_installer_wrote_reads_as_active(self) -> None:
        written = self.install("codex")["pre_commit"]
        self.assertEqual("created", written["state"])
        entry = self.layers()[install.COMMIT_LINT]
        self.assertTrue(entry["active"], entry["detail"])

    def test_no_layer_is_reported_that_brainskit_does_not_install(self) -> None:
        """Naming an unoffered layer reads as a guard that fell off."""

        self.install("codex")
        self.assertEqual(
            {install.COMMIT_LINT, install.INSTRUCTIONS}, set(self.layers())
        )

    def test_the_reader_reports_the_layers_the_installer_reported(self) -> None:
        """One table, iterated twice: the two sides must name the same layers."""

        written = self.install("codex")
        self.assertEqual(
            [entry["layer"] for entry in written["enforcement"]["layers"]],
            [entry["layer"] for entry in self.enforcement()["layers"]],
        )

    def test_the_two_sides_agree_on_every_mechanism(self) -> None:
        written = self.install("codex")
        self.assertEqual(
            {
                entry["layer"]: entry["mechanism"]
                for entry in written["enforcement"]["layers"]
            },
            {
                entry["layer"]: entry["mechanism"]
                for entry in self.enforcement()["layers"]
            },
        )


class WorkspaceComesFromTheAdapterTest(InstalledAgentCase):
    """`--root` is the answer; walking up from the vault is only a guess.

    The reader read the workspace out of `agent-claude.json`, so an install for
    any other agent fell through to the enclosing-repository guess -- silently,
    with no field saying the adapter was missing. The two agree wherever a vault
    happens to sit directly inside the workspace, which is why this fixture puts
    the recorded workspace somewhere the guess cannot reach: a second repository
    inside the first, named with `--root`.
    """

    def setUp(self) -> None:
        super().setUp()
        self.workspace = self.project / "app"
        self.workspace.mkdir()
        subprocess.run(["git", "init", "--quiet"], cwd=self.workspace, check=True)

    def install(self, agent: str) -> dict[str, Any]:
        with redirect_stderr(StringIO()):
            return cli._install_hooks(
                self.service, agent, root=str(self.workspace), skip_code_build=True
            )

    def test_the_recorded_workspace_is_where_the_layers_are_looked_for(self) -> None:
        written = self.install("codex")
        self.assertEqual(str(self.workspace), written["workspace"])
        self.assertFalse(
            (self.vault.root / install.adapter_path("claude")).exists(),
            "fixture guard: there must be no claude adapter to fall back to",
        )
        entry = self.layers()[install.COMMIT_LINT]
        self.assertTrue(entry["active"], entry["detail"])

    def test_the_guess_would_have_named_a_different_directory(self) -> None:
        """Control: the enclosing-repository fallback really is the wrong answer."""

        self.install("codex")
        (self.project / ".git" / "hooks" / "pre-commit").unlink(missing_ok=True)
        self.assertTrue(self.layers()[install.COMMIT_LINT]["active"])

    def test_claude_reads_the_same_recorded_workspace(self) -> None:
        """Control: the agent that always worked keeps working."""

        self.install("claude")
        self.assertTrue(self.enforcement()["gated"])


class ClaudeInstallIsUnchangedTest(InstalledAgentCase):
    """Control: the common case must report exactly as it always has."""

    def test_all_four_layers_are_reported(self) -> None:
        self.install("claude")
        self.assertEqual(
            [
                install.WRITE_GATE,
                install.SESSION_STATUS,
                install.COMMIT_LINT,
                install.INSTRUCTIONS,
            ],
            [entry["layer"] for entry in self.enforcement()["layers"]],
        )

    def test_a_fresh_install_is_gated_and_names_claude_md(self) -> None:
        self.install("claude")
        report = self.enforcement()
        self.assertTrue(report["gated"])
        layers = {entry["layer"]: entry for entry in report["layers"]}
        self.assertEqual(
            "CLAUDE.md managed block", layers[install.INSTRUCTIONS]["mechanism"]
        )

    def test_no_layer_carries_an_agent_key_when_only_one_is_installed(self) -> None:
        """The field exists to disambiguate; there is nothing to disambiguate."""

        self.install("claude")
        for entry in self.enforcement()["layers"]:
            self.assertNotIn("agent", entry)


class BothAgentsInstalledTest(InstalledAgentCase):
    """Two adapters means two installs, and the report has to say which is which."""

    def test_every_layer_of_both_agents_is_reported(self) -> None:
        self.install("claude")
        self.install("codex")
        reported = [
            (entry["agent"], entry["layer"]) for entry in self.enforcement()["layers"]
        ]
        self.assertEqual(
            [
                ("claude", install.WRITE_GATE),
                ("claude", install.SESSION_STATUS),
                ("claude", install.COMMIT_LINT),
                ("claude", install.INSTRUCTIONS),
                ("codex", install.COMMIT_LINT),
                ("codex", install.INSTRUCTIONS),
            ],
            reported,
        )

    def test_each_instructions_layer_names_its_own_file(self) -> None:
        self.install("claude")
        self.install("codex")
        named = {
            entry["agent"]: entry["mechanism"]
            for entry in self.enforcement()["layers"]
            if entry["layer"] == install.INSTRUCTIONS
        }
        self.assertEqual(
            {"claude": "CLAUDE.md managed block", "codex": "AGENTS.md managed block"},
            named,
        )

    def test_a_layer_name_is_listed_inactive_once(self) -> None:
        """Both agents share one repository, so both see the same missing hook."""

        self.install("claude")
        self.install("codex")
        (self.project / ".git" / "hooks" / "pre-commit").unlink()
        inactive = self.enforcement()["inactive"]
        self.assertEqual(1, inactive.count(install.COMMIT_LINT))


class BareVaultReportsTheDefaultAgentTest(unittest.TestCase):
    """No adapter is not an error; it is "nothing has been installed here yet".

    The reader has to say where the layers *would* land rather than invent an
    agent, and `bk hooks install --agent claude` is what the quickstart tells an
    operator to run.
    """

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.vault = FileVault.initialize(Path(self.temporary.name), policy())
        self.service = BrainskitService(
            self.vault, SqliteFtsIndex(self.vault.index_path), graph=MarkdownGraph()
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_every_layer_is_reported_off(self) -> None:
        report = self.service.status()["enforcement"]
        self.assertEqual(
            sorted(report["inactive"]),
            [
                install.COMMIT_LINT,
                install.INSTRUCTIONS,
                install.SESSION_STATUS,
                install.WRITE_GATE,
            ],
        )
        self.assertFalse(report["gated"])


class DoctorProbesTheGateThatExistsTest(InstalledAgentCase):
    """`bk doctor` reaches the write gate through the layer that names a script.

    With two agents installed the first `write_gate` row is not necessarily the
    one with a hook behind it, and a probe that stopped at the first would report
    `absent` for an installation whose gate is live.
    """

    def test_a_codex_only_install_has_no_gate_to_exercise(self) -> None:
        self.install("codex")
        probe = cli._probe_write_gate(self.service, self.enforcement()["layers"])
        self.assertEqual("absent", probe["state"])

    def test_a_claude_install_is_exercised_even_beside_another_agent(self) -> None:
        self.install("codex")
        self.install("claude")
        probe = cli._probe_write_gate(self.service, self.enforcement()["layers"])
        self.assertEqual("enforcing", probe["state"], probe["detail"])


class AdapterPathHasOneSpellingTest(unittest.TestCase):
    """The gate, the installer and the reader open the same file.

    Three modules built `.brain/agent-<agent>.json` by hand; this pins the one
    they now share, including the case the gate deliberately keeps working for.
    """

    def test_the_registry_and_the_install_shape_agree(self) -> None:
        for name, shape in install.AGENTS.items():
            with self.subTest(agent=name):
                self.assertEqual(install.adapter_path(name), shape.adapter)

    def test_an_unknown_agent_still_resolves_to_a_path(self) -> None:
        self.assertEqual(".brain/agent-whatever.json", install.adapter_path("whatever"))

    def test_the_gate_reads_the_adapter_the_installer_wrote(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".brain").mkdir()
            (root / install.adapter_path("codex")).write_text(
                json.dumps({"gate": {"deny_prefixes": ["views/"]}}), encoding="utf-8"
            )
            from brainskit.application.gate import load_gate_policy

            loaded = load_gate_policy(root, agent="codex")
            self.assertIsNone(loaded.degraded)
            self.assertEqual(("views/",), loaded.deny_prefixes)


if __name__ == "__main__":
    unittest.main()
