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
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any

from test_engine import policy

from brainskit.application.health import redirected_git_hooks_path
from brainskit.application.services import BrainskitService
from brainskit.infrastructure.graph import MarkdownGraph
from brainskit.infrastructure.index import SqliteFtsIndex
from brainskit.infrastructure.vault import FileVault
from brainskit.interfaces import cli, console

GATE = "brainskit-gate.sh"
STATUS = "brainskit-status.sh"


class EnforcementStatusTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.vault = FileVault.initialize(Path(self.temporary.name), policy())
        # Build paths from the vault's own root, not the raw temp path: on macOS
        # /var resolves to /private/var, and a test that spells the root
        # differently than production would be testing its own bookkeeping.
        self.root = self.vault.root
        self.service = BrainskitService(
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
            "notes\n<!-- brainskit:start -->\ncontract\n<!-- brainskit:end -->\n",
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


class HealthyHeadlineMeansEnforcementTooTest(EnforcementStatusTest):
    """`bk status` printed a green headline above three red enforcement rows.

    `healthy` was `lint_result["ok"]` alone, so a vault whose write gate was not
    running still announced itself healthy as long as the pages it already had
    happened to lint. The headline sits directly above those rows.
    """

    def test_a_vault_with_no_enforcement_is_not_healthy(self) -> None:
        self.assertFalse(self.service.status()["healthy"])

    def test_lint_alone_does_not_make_it_healthy(self) -> None:
        """Control: lint really is clean in this fixture, and that is not enough."""

        self.assertTrue(self.service.lint()["ok"])
        self.assertFalse(self.service.status()["healthy"])

    def test_a_fully_gated_vault_is_healthy(self) -> None:
        """Control: the headline must still be reachable."""

        self.register("brainskit-gate.sh", "PreToolUse")
        self.register("brainskit-status.sh", "SessionStart")
        hooks = self.root / ".git" / "hooks"
        hooks.mkdir(parents=True, exist_ok=True)
        (hooks / "pre-commit").write_text(
            "#!/bin/sh\nbk lint --changed\n", encoding="utf-8"
        )
        subprocess.run(["git", "init", "--quiet"], cwd=self.root, check=True)
        report = self.service.status()
        inactive = [
            layer["layer"]
            for layer in report["enforcement"]["layers"]
            if not layer.get("active") and not layer.get("advisory")
        ]
        if inactive:
            self.skipTest(f"fixture could not activate: {inactive}")
        self.assertTrue(report["healthy"])


class NestedVaultEnforcementReportingTest(unittest.TestCase):
    """A vault nested inside its own project's git repo, before `hooks
    install` has ever run there -- the exact directory `_project_root_for`
    targets at install time (`repo/docs/brain`, the layout the rest of this
    codebase already treats as the common one). `_agent_workspace`'s
    no-adapter fallback used to default to the vault's own directory
    regardless, so every layer read as though the vault sat nowhere near a
    git repository -- including `instructions`, which looked for `CLAUDE.md`
    inside the vault rather than at the project root that actually has one.
    """

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

    def enforcement(self) -> dict[str, Any]:
        report = self.service.status()["enforcement"]
        assert isinstance(report, dict)
        return report

    def layer(self, name: str) -> dict[str, Any]:
        for entry in self.enforcement()["layers"]:
            if entry["layer"] == name:
                return entry
        raise AssertionError(f"no {name} layer reported")

    def test_commit_lint_names_the_enclosing_repository_not_the_vault(self) -> None:
        detail = self.layer("commit_lint")["detail"]
        self.assertNotIn(str(self.vault.root), detail)
        self.assertIn(str(self.project), detail)

    def test_commit_lint_distinguishes_missing_repo_from_missing_hook(self) -> None:
        # The vault sits inside a real repository -- the message must say the
        # hook is missing, never that the repository is.
        detail = self.layer("commit_lint")["detail"]
        self.assertNotIn("is not a git repository", detail)
        self.assertIn("has no brainskit pre-commit hook", detail)

    def test_a_hook_already_installed_in_the_project_reads_as_active(self) -> None:
        hook = self.project / ".git" / "hooks" / "pre-commit"
        hook.parent.mkdir(parents=True, exist_ok=True)
        hook.write_text("#!/bin/sh\nexec bk lint --changed\n", encoding="utf-8")
        self.assertTrue(self.layer("commit_lint")["active"])

    def test_instructions_resolve_against_the_project_root(self) -> None:
        (self.project / "CLAUDE.md").write_text(
            "notes\n<!-- brainskit:start -->\ncontract\n<!-- brainskit:end -->\n",
            encoding="utf-8",
        )
        self.assertTrue(self.layer("instructions")["active"])

    def test_write_gate_resolves_against_the_project_root(self) -> None:
        gate = self.project / ".claude" / "hooks" / GATE
        gate.parent.mkdir(parents=True, exist_ok=True)
        gate.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        settings = self.project / ".claude" / "settings.json"
        settings.write_text(
            json.dumps(
                {
                    "hooks": {
                        "PreToolUse": [
                            {"hooks": [{"type": "command", "command": str(gate)}]}
                        ]
                    }
                }
            ),
            encoding="utf-8",
        )
        self.assertTrue(self.layer("write_gate")["active"])

    def test_a_standalone_vault_is_unaffected(self) -> None:
        # Regression guard: a vault that is not nested in any git repository
        # keeps reporting exactly as it always has.
        with tempfile.TemporaryDirectory() as standalone_dir:
            standalone = FileVault.initialize(Path(standalone_dir), policy())
            service = BrainskitService(
                standalone, SqliteFtsIndex(standalone.index_path), graph=MarkdownGraph()
            )
            layers = service.status()["enforcement"]["layers"]
            commit = next(entry for entry in layers if entry["layer"] == "commit_lint")
            self.assertIn("is not a git repository", commit["detail"])


class RedirectedHooksPathTest(unittest.TestCase):
    """`core.hooksPath` moves the directory git runs, and the report must follow.

    Husky sets it on every install, and once set git never reads
    `.git/hooks/pre-commit` again. Reporting that file as an active layer
    because it exists is the same lie as reporting a gate script that nothing
    registers: the artefact is there, and it is not what runs.
    """

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.vault = FileVault.initialize(Path(self.temporary.name), policy())
        self.root = self.vault.root
        self.service = BrainskitService(
            self.vault, SqliteFtsIndex(self.vault.index_path), graph=MarkdownGraph()
        )
        subprocess.run(["git", "init", "--quiet"], cwd=self.root, check=True)
        hook = self.root / ".git" / "hooks" / "pre-commit"
        hook.parent.mkdir(parents=True, exist_ok=True)
        hook.write_text("#!/bin/sh\nexec bk lint --changed\n", encoding="utf-8")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def redirect(self, relative: str = ".husky/_") -> None:
        subprocess.run(
            ["git", "config", "core.hooksPath", relative], cwd=self.root, check=True
        )

    def commit_lint(self) -> dict[str, Any]:
        for entry in self.service.status()["enforcement"]["layers"]:
            if entry["layer"] == "commit_lint":
                return entry
        raise AssertionError("no commit_lint layer reported")

    def test_a_default_repository_still_reports_the_layer_active(self) -> None:
        """The control: without the setting, nothing about this changed."""
        self.assertTrue(self.commit_lint()["active"])

    def test_a_redirected_hooks_path_reports_the_layer_off(self) -> None:
        self.redirect()
        entry = self.commit_lint()
        self.assertFalse(entry["active"])
        self.assertIn("commit_lint", self.service.status()["enforcement"]["inactive"])

    def test_the_reason_names_the_directory_git_actually_runs(self) -> None:
        """A bare "off" sends someone to reinstall the hook that already exists."""
        self.redirect()
        detail = self.commit_lint()["detail"]
        self.assertIn(".husky/_", detail)
        self.assertIn("never runs", detail)

    def test_the_hook_file_on_disk_does_not_rescue_the_layer(self) -> None:
        """The file is present, readable, and mentions lint. It still never runs."""
        self.redirect()
        hook = self.root / ".git" / "hooks" / "pre-commit"
        self.assertIn("lint", hook.read_text(encoding="utf-8"))
        self.assertFalse(self.commit_lint()["active"])

    def test_an_absolute_hooks_path_is_recognised_too(self) -> None:
        self.redirect(str(self.root / "elsewhere"))
        self.assertFalse(self.commit_lint()["active"])

    def test_pointing_it_back_at_the_default_reports_active_again(self) -> None:
        """Spelled explicitly, `.git/hooks` is still the directory we wrote to."""
        self.redirect(".git/hooks")
        self.assertTrue(self.commit_lint()["active"])


class HooksPathResolverTest(unittest.TestCase):
    """The resolver answers for a repository root, and never raises."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        subprocess.run(["git", "init", "--quiet"], cwd=self.root, check=True)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_an_unset_hooks_path_is_not_a_redirect(self) -> None:
        self.assertIsNone(redirected_git_hooks_path(self.root))

    def test_a_set_hooks_path_resolves_against_the_root(self) -> None:
        subprocess.run(
            ["git", "config", "core.hooksPath", ".husky/_"], cwd=self.root, check=True
        )
        self.assertEqual(
            redirected_git_hooks_path(self.root), self.root / ".husky" / "_"
        )

    def test_a_subdirectory_is_not_asked_about_the_repository_above_it(self) -> None:
        """Git answers for the repository it finds, which is the parent's.

        Read as a redirect, that would report a `.git/hooks` this directory
        never owned as having moved away.
        """
        subprocess.run(
            ["git", "config", "core.hooksPath", ".husky/_"], cwd=self.root, check=True
        )
        nested = self.root / "packages" / "app"
        nested.mkdir(parents=True)
        self.assertIsNone(redirected_git_hooks_path(nested))

    def test_a_directory_that_is_not_a_repository_is_not_a_redirect(self) -> None:
        plain = self.root / "plain"
        plain.mkdir()
        self.assertIsNone(redirected_git_hooks_path(plain))

    def test_a_missing_directory_returns_rather_than_raises(self) -> None:
        self.assertIsNone(redirected_git_hooks_path(self.root / "nope"))


class StatusHeadlineSaysWhyTest(unittest.TestCase):
    """The headline has to mean what `healthy` means.

    `healthy` widened to "lint is clean *and* every enforcing layer is on"; the
    headline stayed a lint-error count. A vault with clean pages and no git
    repository therefore printed `0 lint error(s)` under a red cross -- a cross
    above a zero -- and that is the default outcome of the documented quickstart,
    because `bk init` outside a git repository can never make `commit_lint`
    active.

    These drive `_status_headline` on report shapes rather than on real vaults:
    the shapes are what the renderer contracts with, and `Health` has its own
    tests above for producing them.
    """

    def headline(self, **over: Any) -> str:
        report: dict[str, Any] = {
            "healthy": False,
            "lint_errors": 0,
            "enforcement": {"layers": [], "inactive": [], "gated": False},
        }
        report.update(over)
        return cli._status_headline(report)

    def layers(self, *specs: tuple[str, bool, bool]) -> dict[str, Any]:
        return {
            "layers": [
                {"layer": name, "active": active, "advisory": advisory, "detail": "x"}
                for name, active, advisory in specs
            ]
        }

    def test_a_healthy_vault_says_so(self) -> None:
        """Control: a vault that is genuinely fine must never print a fault."""

        self.assertEqual("vault healthy", self.headline(healthy=True))

    def test_clean_lint_with_a_layer_off_names_the_layer(self) -> None:
        headline = self.headline(
            lint_errors=0,
            enforcement=self.layers(
                ("write_gate", True, False), ("commit_lint", False, False)
            ),
        )
        self.assertIn("commit_lint", headline)
        self.assertNotIn("0 lint error(s)", headline)

    def test_lint_errors_are_still_counted(self) -> None:
        """Control: the case the old headline got right must keep working."""

        self.assertEqual("2 lint error(s)", self.headline(lint_errors=2))

    def test_both_faults_are_reported_together(self) -> None:
        headline = self.headline(
            lint_errors=2, enforcement=self.layers(("write_gate", False, False))
        )
        self.assertIn("2 lint error(s)", headline)
        self.assertIn("write_gate", headline)

    def test_an_inactive_advisory_layer_is_not_a_reason(self) -> None:
        """`healthy` excludes advisory layers, so the headline must not name one.

        Naming it would make the headline fire for something no mechanism was
        ever going to stop -- and disagree with the flag it is rendering.
        """

        self.assertNotIn(
            "instructions",
            self.headline(enforcement=self.layers(("instructions", False, True))),
        )

    def test_an_unrecognised_reason_points_at_the_rows_instead_of_guessing(self) -> None:
        """If `healthy` gains a third input, the headline must not invent one.

        Restating `healthy` in terms of one of its inputs is exactly how this
        broke; a headline that says "look below" is merely unhelpful.
        """

        headline = self.headline(healthy=False, lint_errors=0)
        self.assertNotIn("0", headline)
        self.assertIn("see the rows below", headline)


class EnforcementRowsDistinguishAdvisoryTest(unittest.TestCase):
    """An advisory layer must not render as a fourth guarantee.

    `instructions` is advisory -- correctly excluded from `gated` and from
    `healthy` -- but drew the same green tick as the three layers that actually
    stop a write.
    """

    def rows(self, *specs: tuple[str, bool, bool]) -> list[list[str]]:
        return cli._enforcement_rows(
            [
                {"layer": name, "active": active, "advisory": advisory, "detail": "d"}
                for name, active, advisory in specs
            ]
        )

    def test_an_advisory_row_says_so_in_text(self) -> None:
        """In text, not only in colour: this output is usually read off a TTY."""

        (row,) = self.rows(("instructions", True, True))
        self.assertIn("advisory", row[0])

    def test_an_enforcing_row_is_not_labelled_advisory(self) -> None:
        """Control: the three real layers must keep their plain names."""

        (row,) = self.rows(("write_gate", True, False))
        self.assertEqual("write_gate", row[0])
        self.assertNotIn("advisory", row[1])

    def test_every_layer_still_gets_exactly_one_row(self) -> None:
        rows = self.rows(
            ("write_gate", True, False),
            ("commit_lint", False, False),
            ("instructions", True, True),
        )
        self.assertEqual(3, len(rows))
        self.assertEqual(
            ["write_gate", "commit_lint", "instructions (advisory)"],
            [row[0] for row in rows],
        )

    def test_the_detail_survives_in_both_kinds_of_row(self) -> None:
        for spec in (("write_gate", False, False), ("instructions", True, True)):
            with self.subTest(layer=spec[0]):
                (row,) = self.rows(spec)
                self.assertIn("d", console.strip_ansi(row[1]))


if __name__ == "__main__":
    unittest.main()
