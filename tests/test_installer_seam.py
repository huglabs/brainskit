"""The installer's decisions, driven without an install.

Every one of these was already covered -- by `tests/test_hooks_install.py`,
through a real `FileVault`, a real index, a real `BrainskitService` and a real
`bk hooks install`, because while the installer lived in `interfaces/cli.py` the
CLI was its only entry point. That end-to-end net is the right net for "does an
install work"; it is the wrong one for "is a hook command stale", which is a
question about two strings.

So these drive `application/installer.py` and `application/doctor.py` directly:
a temporary directory, a vault stub carrying the two members the installer asks
of a vault, and no service at all. They are the seam the move opened, and they
fail in one line rather than in a fixture.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from brainskit.application import doctor, installer
from brainskit.application.gate import (
    HOOK_SENTINEL,
    INSTRUCTION_END,
    INSTRUCTION_START,
)
from brainskit.application.install import (
    BRAND,
    COMMIT_LINT,
    INSTRUCTIONS,
    WRITE_GATE,
    agent_install,
)
from brainskit.domain.model import ValidationError

LEGACY = installer.LEGACY_BRANDS[0]


class _VaultStub:
    """Exactly what `install_agent` asks of a vault: a root and one writer.

    The point of the stub is the shortfall. `FileVault.initialize` needs a
    complete policy, an index and a wiki before it will hand back an object;
    the installer reads `root` and calls `write_generated` once, and nothing
    else. Anything this stub has to grow is the installer reaching further into
    the vault than it says it does.
    """

    def __init__(self, root: Path) -> None:
        self.root = root
        self.generated: dict[str, str] = {}

    def write_generated(self, relative_path: str, content: str) -> None:
        self.generated[relative_path] = content
        target = self.root / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")


class SeamCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def legacy_hook(self, template: str, *, owned: bool = True) -> Path:
        """A hook script on disk exactly as the former brand wrote it."""

        path = self.root / ".claude" / "hooks" / f"{template.replace(BRAND, LEGACY)}.sh"
        path.parent.mkdir(parents=True, exist_ok=True)
        mark = HOOK_SENTINEL.replace(BRAND, LEGACY) if owned else "# hand written"
        path.write_text(f"#!/bin/sh\n{mark}\nexit 0\n", encoding="utf-8")
        return path


class StaleHookCommandTest(SeamCase):
    """Which registered commands a reinstall supersedes, as a string question."""

    def command(self, root: Path, template: str) -> str:
        return str(root / ".claude" / "hooks" / f"{template}.sh")

    def test_the_same_script_at_another_path_is_stale(self) -> None:
        current = self.command(self.root, f"{BRAND}-gate")
        elsewhere = self.command(Path("/somewhere/else"), f"{BRAND}-gate")
        self.assertTrue(
            installer._is_stale_hook_command(elsewhere, f"{BRAND}-gate", current)
        )

    def test_the_command_being_installed_is_never_stale(self) -> None:
        current = self.command(self.root, f"{BRAND}-gate")
        self.assertFalse(
            installer._is_stale_hook_command(current, f"{BRAND}-gate", current)
        )

    def test_a_former_brands_filename_is_the_same_hook(self) -> None:
        """The rename case: `<former>-gate.sh` is this template, spelled before."""

        current = self.command(self.root, f"{BRAND}-gate")
        former = self.command(Path("/old/project"), f"{LEGACY}-gate")
        self.assertTrue(
            installer._is_stale_hook_command(former, f"{BRAND}-gate", current)
        )

    def test_unrelated_tooling_on_the_same_event_is_left_alone(self) -> None:
        current = self.command(self.root, f"{BRAND}-gate")
        for foreign in ("/usr/local/bin/prettier", "npx lint-staged", 17, None):
            with self.subTest(command=foreign):
                self.assertFalse(
                    installer._is_stale_hook_command(foreign, f"{BRAND}-gate", current)
                )

    def test_an_entry_that_loses_every_command_is_dropped_whole(self) -> None:
        current = self.command(self.root, f"{BRAND}-gate")
        stale = self.command(Path("/old/project"), f"{BRAND}-gate")
        group: list[Any] = [
            {"matcher": "Write", "hooks": [{"type": "command", "command": stale}]},
            {"matcher": "Write", "hooks": [{"type": "command", "command": "make fmt"}]},
        ]
        pruned, removed = installer._prune_stale_hook_entries(
            group, f"{BRAND}-gate", current
        )
        self.assertEqual(removed, [stale])
        self.assertEqual(
            pruned,
            [
                {
                    "matcher": "Write",
                    "hooks": [{"type": "command", "command": "make fmt"}],
                }
            ],
        )


class ManagedBlockTest(SeamCase):
    """The instruction file is the operator's; the block inside it is not."""

    def instructions(self, agent: str = "claude") -> Path:
        return self.root / agent_install(agent).instructions

    def test_a_second_install_changes_nothing(self) -> None:
        first = installer._install_instructions(self.root, self.root, "claude")
        self.assertEqual(first["state"], "created")
        written = self.instructions().read_text(encoding="utf-8")
        second = installer._install_instructions(self.root, self.root, "claude")
        self.assertEqual(second["state"], "current")
        self.assertEqual(self.instructions().read_text(encoding="utf-8"), written)
        self.assertEqual(written.count(INSTRUCTION_START), 1)

    def test_the_block_is_replaced_in_place(self) -> None:
        self.instructions().write_text(
            f"# Top\n\n{INSTRUCTION_START}\nstale\n{INSTRUCTION_END}\n\n## Bottom\n",
            encoding="utf-8",
        )
        outcome = installer._install_instructions(self.root, self.root, "claude")
        self.assertEqual(outcome["state"], "updated")
        text = self.instructions().read_text(encoding="utf-8")
        self.assertEqual(text.count(INSTRUCTION_START), 1)
        self.assertNotIn("stale", text)
        self.assertLess(text.index("# Top"), text.index(INSTRUCTION_START))
        self.assertLess(text.index(INSTRUCTION_END), text.index("## Bottom"))

    def test_a_former_brands_block_is_migrated_not_duplicated(self) -> None:
        self.instructions().write_text(
            f"{INSTRUCTION_START.replace(BRAND, LEGACY)}\nold\n"
            f"{INSTRUCTION_END.replace(BRAND, LEGACY)}\n\n# after\n",
            encoding="utf-8",
        )
        outcome = installer._install_instructions(self.root, self.root, "claude")
        self.assertEqual(outcome["state"], "migrated")
        self.assertEqual(
            outcome["migrated"], [INSTRUCTION_START.replace(BRAND, LEGACY)]
        )
        text = self.instructions().read_text(encoding="utf-8")
        self.assertEqual(text.count(INSTRUCTION_START), 1)
        self.assertNotIn(INSTRUCTION_START.replace(BRAND, LEGACY), text)
        self.assertLess(text.index(INSTRUCTION_START), text.index("# after"))

    def test_each_agent_writes_its_own_instruction_file(self) -> None:
        for agent in ("claude", "codex", "gemini"):
            with self.subTest(agent=agent):
                outcome = installer._install_instructions(self.root, self.root, agent)
                self.assertEqual(outcome["path"], str(self.instructions(agent)))
                self.assertTrue(self.instructions(agent).is_file())


class WorkspaceResolutionTest(SeamCase):
    """Where the agent's configuration goes, which is not always the vault."""

    def test_no_root_leaves_the_workspace_at_the_vault(self) -> None:
        vault = self.root / "vault"
        vault.mkdir()
        self.assertEqual(installer._resolve_workspace(vault, None), vault)

    def test_a_named_root_is_resolved(self) -> None:
        project = self.root / "project"
        project.mkdir()
        resolved = installer._resolve_workspace(self.root / "vault", str(project))
        self.assertEqual(resolved, project.resolve())

    def test_a_root_that_is_not_a_directory_is_refused(self) -> None:
        with self.assertRaises(ValidationError):
            installer._resolve_workspace(self.root, str(self.root / "nope"))

    def test_a_vault_inside_a_project_is_advised_about(self) -> None:
        (self.root / ".git").mkdir()
        vault = self.root / "docs" / "brain"
        vault.mkdir(parents=True)
        advisory = installer._workspace_advisory(vault, vault)
        assert advisory is not None
        self.assertIn(str(self.root), advisory["reason"])
        self.assertEqual(advisory["hint"], f"Reinstall with --root {self.root}")

    def test_a_standalone_vault_and_an_answered_question_stay_quiet(self) -> None:
        vault = self.root / "vault"
        (vault / ".claude").mkdir(parents=True)
        self.assertIsNone(installer._workspace_advisory(vault, vault))
        bare = self.root / "elsewhere"
        bare.mkdir()
        self.assertIsNone(installer._workspace_advisory(bare, self.root))


class LegacyArtefactTest(SeamCase):
    """What a former brand left, and which half of it this tool may delete."""

    def test_a_generated_script_is_removed(self) -> None:
        paths = [self.legacy_hook(hook.template) for hook in installer.CLAUDE_HOOKS]
        retired = installer._retire_legacy_hook_scripts(
            self.root, installer.CLAUDE_HOOKS
        )
        self.assertEqual([item["state"] for item in retired], ["removed"] * len(paths))
        for path in paths:
            self.assertFalse(path.is_file())

    def test_an_edited_script_is_reported_and_kept(self) -> None:
        path = self.legacy_hook(installer.CLAUDE_HOOKS[0].template, owned=False)
        retired = installer._retire_legacy_hook_scripts(
            self.root, [installer.CLAUDE_HOOKS[0]]
        )
        self.assertEqual(retired[0]["state"], "kept")
        self.assertIn("edited since it was generated", retired[0]["reason"])
        self.assertTrue(path.is_file())

    def test_a_former_brands_skill_is_reported_never_deleted(self) -> None:
        skill = self.root / ".claude" / "skills" / LEGACY
        skill.mkdir(parents=True)
        self.assertEqual(installer._legacy_skill_dirs(self.root), [str(skill)])
        self.assertTrue(skill.is_dir())


class InstallAgentWithoutAServiceTest(SeamCase):
    """The whole installer, against a stub that is two members wide."""

    def test_an_install_writes_its_adapter_and_reports_its_layers(self) -> None:
        vault = _VaultStub(self.root)
        result = installer.install_agent(vault, "claude")  # type: ignore[arg-type]

        adapter = json.loads(vault.generated[agent_install("claude").adapter])
        self.assertEqual(adapter["agent"], "claude")
        self.assertEqual(adapter["workspace"], str(self.root))

        names = [layer["layer"] for layer in result["enforcement"]["layers"]]
        self.assertEqual(
            names,
            [hook.layer for hook in agent_install("claude").hooks]
            + [COMMIT_LINT, INSTRUCTIONS],
        )
        # No git repository in a bare temporary directory, so exactly one layer
        # is off -- and it is named rather than left out of the report.
        self.assertEqual(result["enforcement"]["inactive"], [COMMIT_LINT])

    def test_the_installer_says_nothing_and_builds_no_graph(self) -> None:
        """The two steps that stayed in `interfaces/cli.py` really did stay.

        A `code_graph` key or a line on stderr from here would mean the split
        leaked back: the code-graph bootstrap needs the running interpreter and
        the banners need a terminal, and neither is this layer's.
        """

        vault = _VaultStub(self.root)
        result = installer.install_agent(vault, "codex")  # type: ignore[arg-type]
        self.assertNotIn("code_graph", result)


class WriteGateProbeTest(SeamCase):
    """Exercising the gate, with no CLI and no installed agent."""

    def script(self, exit_gated: int, exit_ordinary: int) -> Path:
        """A stand-in hook: one status for a wiki path, another for anything else."""

        path = self.root / "fake-gate.sh"
        path.write_text(
            "#!/bin/sh\n"
            "payload=$(cat)\n"
            'case "$payload" in\n'
            f'  *"/wiki/"*) exit {exit_gated} ;;\n'
            f"  *) exit {exit_ordinary} ;;\n"
            "esac\n",
            encoding="utf-8",
        )
        path.chmod(0o755)
        return path

    def layers(self, script: Path | None) -> list[dict[str, Any]]:
        layer: dict[str, Any] = {"layer": WRITE_GATE, "active": True}
        if script is not None:
            layer["script"] = str(script)
        return [layer]

    def probe(self, script: Path | None) -> dict[str, Any]:
        vault = _VaultStub(self.root / "vault")
        return doctor.probe_write_gate(vault, self.layers(script))  # type: ignore[arg-type]

    def test_a_gate_that_denies_wiki_and_allows_the_rest_is_enforcing(self) -> None:
        report = self.probe(self.script(exit_gated=2, exit_ordinary=0))
        self.assertEqual(report["state"], "enforcing")
        self.assertTrue(report["denies_a_gated_write"])
        self.assertTrue(report["allows_an_ordinary_write"])

    def test_a_gate_that_lets_a_wiki_write_through_is_not_enforcing(self) -> None:
        report = self.probe(self.script(exit_gated=0, exit_ordinary=0))
        self.assertEqual(report["state"], "not_enforcing")
        self.assertIn("installed but not guarding", report["detail"])

    def test_a_gate_that_refuses_everything_is_over_blocking(self) -> None:
        report = self.probe(self.script(exit_gated=2, exit_ordinary=2))
        self.assertEqual(report["state"], "over_blocking")

    def test_a_layer_with_no_script_is_absent_not_broken(self) -> None:
        self.assertEqual(self.probe(None)["state"], "absent")

    def test_a_script_that_cannot_be_executed_is_unknown(self) -> None:
        """The failure the probe exists for: present, registered, unrunnable."""

        script = self.script(exit_gated=2, exit_ordinary=0)
        script.chmod(0o644)
        report = self.probe(script)
        self.assertEqual(report["state"], "unknown")
        self.assertIn("could not be run", report["detail"])
        # ...and the probe never creates the paths it asks about.
        self.assertEqual(list(self.root.rglob("*brainskit-doctor-probe*")), [])


if __name__ == "__main__":
    unittest.main()
