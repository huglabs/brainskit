"""`bk status` must report which enforcement layers are live, read from disk.

The installer reports what it wrote at the moment it ran. That answer goes stale
the first time a hook is edited away, a settings file is rewritten by other
tooling, or a vault is copied without its git directory. These tests pin the
divergence cases, because a vault whose write gate is silently off while every
command still reports success is the exact failure this feature exists to
prevent.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from test_engine import policy

from brainkit.application.services import BrainkitService
from brainkit.infrastructure.graph import MarkdownGraph
from brainkit.infrastructure.index import SqliteFtsIndex
from brainkit.infrastructure.vault import FileVault

GATE = "brainkit-gate.sh"
STATUS = "brainkit-status.sh"


class EnforcementStatusTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.vault = FileVault.initialize(Path(self.temporary.name), policy())
        # Build paths from the vault's own root, not the raw temp path: on macOS
        # /var resolves to /private/var, and a test that spells the root
        # differently than production would be testing its own bookkeeping.
        self.root = self.vault.root
        self.service = BrainkitService(
            self.vault, SqliteFtsIndex(self.vault.index_path), graph=MarkdownGraph()
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    # helpers -----------------------------------------------------------------

    def enforcement(self) -> dict[str, Any]:
        report = self.service.status()["enforcement"]
        assert isinstance(report, dict)
        return report

    def layer(self, name: str) -> dict[str, Any]:
        for entry in self.enforcement()["layers"]:
            if entry["layer"] == name:
                return entry
        raise AssertionError(f"no {name} layer reported")

    def install_script(self, name: str) -> Path:
        path = self.root / ".claude" / "hooks" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        return path

    def write_settings(self, hooks: dict[str, Any]) -> None:
        settings = self.root / ".claude" / "settings.json"
        settings.parent.mkdir(parents=True, exist_ok=True)
        settings.write_text(json.dumps({"hooks": hooks}, indent=2), encoding="utf-8")

    def register(self, name: str, event: str) -> None:
        path = self.install_script(name)
        settings = self.root / ".claude" / "settings.json"
        current = (
            json.loads(settings.read_text(encoding="utf-8"))
            if settings.is_file()
            else {"hooks": {}}
        )
        current.setdefault("hooks", {}).setdefault(event, []).append(
            {"hooks": [{"type": "command", "command": str(path)}]}
        )
        settings.parent.mkdir(parents=True, exist_ok=True)
        settings.write_text(json.dumps(current, indent=2), encoding="utf-8")

    # a bare vault ------------------------------------------------------------

    def test_a_vault_with_nothing_installed_reports_every_layer_off(self) -> None:
        report = self.enforcement()
        self.assertFalse(report["gated"])
        self.assertEqual(
            sorted(report["inactive"]),
            ["commit_lint", "instructions", "session_status", "write_gate"],
        )

    def test_every_inactive_layer_explains_itself(self) -> None:
        for entry in self.enforcement()["layers"]:
            self.assertNotEqual(
                entry["detail"],
                "active",
                f"{entry['layer']} is off but offers no reason",
            )

    # the divergence cases ----------------------------------------------------

    def test_a_registered_gate_whose_script_is_gone_is_not_active(self) -> None:
        self.register(GATE, "PreToolUse")
        self.assertTrue(self.layer("write_gate")["active"])
        (self.root / ".claude" / "hooks" / GATE).unlink()
        entry = self.layer("write_gate")
        self.assertFalse(entry["active"])
        self.assertIn("not installed", entry["detail"])

    def test_a_script_on_disk_that_nothing_registers_is_not_active(self) -> None:
        self.install_script(GATE)
        entry = self.layer("write_gate")
        self.assertFalse(entry["active"])
        self.assertIn("not registered", entry["detail"])

    def test_a_gate_registered_under_the_wrong_event_is_not_active(self) -> None:
        self.register(GATE, "PostToolUse")
        self.assertFalse(self.layer("write_gate")["active"])

    def test_an_unresolved_path_spelling_still_reads_as_registered(self) -> None:
        """/var and /private/var name the same file on macOS.

        Reporting the gate off because the settings file spelled the root the
        other way would send someone reinstalling a hook that already works.
        """
        path = self.install_script(GATE)
        unresolved = str(path).replace("/private/var/", "/var/", 1)
        self.write_settings(
            {"PreToolUse": [{"hooks": [{"type": "command", "command": unresolved}]}]}
        )
        self.assertTrue(self.layer("write_gate")["active"])

    def test_a_script_wrapped_in_a_shell_guard_still_reads_as_registered(self) -> None:
        """Other tooling registers hooks as guarded snippets, not bare paths."""
        path = self.install_script(GATE)
        self.write_settings(
            {
                "PreToolUse": [
                    {
                        "hooks": [
                            {
                                "type": "command",
                                "command": f"if [ -x '{path}' ]; then '{path}'; fi",
                            }
                        ]
                    }
                ]
            }
        )
        self.assertTrue(self.layer("write_gate")["active"])

    # `gated` means the write gate specifically -------------------------------

    def test_session_status_alone_does_not_report_a_gated_vault(self) -> None:
        """Observability is not enforcement.

        A vault reporting `gated` while a Write tool can still reach `wiki/` is
        worse than one reporting nothing at all.
        """
        self.register(STATUS, "SessionStart")
        self.assertTrue(self.layer("session_status")["active"])
        self.assertFalse(self.enforcement()["gated"])

    def test_commit_lint_alone_does_not_report_a_gated_vault(self) -> None:
        hook = self.root / ".git" / "hooks" / "pre-commit"
        hook.parent.mkdir(parents=True, exist_ok=True)
        hook.write_text("#!/bin/sh\nexec bk lint --changed\n", encoding="utf-8")
        self.assertTrue(self.layer("commit_lint")["active"])
        self.assertFalse(self.enforcement()["gated"])

    def test_the_write_gate_alone_reports_a_gated_vault(self) -> None:
        self.register(GATE, "PreToolUse")
        self.assertTrue(self.enforcement()["gated"])

    def test_instructions_are_marked_advisory(self) -> None:
        (self.root / "CLAUDE.md").write_text(
            "notes\n<!-- brainkit:start -->\ncontract\n<!-- brainkit:end -->\n",
            encoding="utf-8",
        )
        entry = self.layer("instructions")
        self.assertTrue(entry["active"])
        self.assertTrue(entry["advisory"])
        self.assertFalse(self.enforcement()["gated"])

    # never raise -------------------------------------------------------------

    def test_a_malformed_settings_file_reports_off_rather_than_raising(self) -> None:
        self.install_script(GATE)
        (self.root / ".claude" / "settings.json").write_text("{not json", "utf-8")
        self.assertFalse(self.layer("write_gate")["active"])

    def test_a_settings_file_of_the_wrong_shape_reports_off(self) -> None:
        self.install_script(GATE)
        for payload in ('{"hooks": []}', '{"hooks": {"PreToolUse": "nope"}}', "[]"):
            with self.subTest(payload=payload):
                (self.root / ".claude" / "settings.json").write_text(payload, "utf-8")
                self.assertFalse(self.layer("write_gate")["active"])

    def test_status_still_reports_its_other_blocks(self) -> None:
        """The enforcement block must be additive, never a replacement."""
        report = self.service.status()
        for key in ("vault", "sources", "wiki_pages", "freshness", "projections"):
            self.assertIn(key, report)


if __name__ == "__main__":
    unittest.main()
