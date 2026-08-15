"""The write gate an agent cannot talk its way past.

`bk hooks install --agent claude` used to ship prose: a managed CLAUDE.md block
and a skill that *ask* the model to route wiki writes through `bk apply`. These
tests cover the part that does not depend on the model agreeing — a Claude Code
PreToolUse hook that refuses the write while it is being attempted — and the two
ways that feature can fail worse than not existing at all: clobbering an
operator's `settings.json`, and blocking a session when the gate itself breaks.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from typing import Any
from unittest.mock import patch

from test_code_graph import _HAS_CODE_EXTRA

from brainskit.application import installer
from brainskit.application.gate import (
    HOOK_SENTINEL,
    INSTRUCTION_END,
    INSTRUCTION_START,
)
from brainskit.application.install import BRAND
from brainskit.application.services import BrainskitService
from brainskit.domain.model import ValidationError
from brainskit.infrastructure.extractor import GraphifyExtractor
from brainskit.infrastructure.graph import MarkdownGraph
from brainskit.infrastructure.index import SqliteFtsIndex
from brainskit.infrastructure.vault import FileVault
from brainskit.interfaces import cli

REPO_ROOT = Path(__file__).resolve().parents[1]

DENY_DECISION: dict[str, Any] = {
    "allowed": False,
    "path": "wiki/concepts/foo.md",
    "reason": "This page is compiled from evidence, not written by hand.",
    "remediation": "Wiki pages are written only by the apply gate. Use: bk apply",
    "rule": "wiki/",
}
ALLOW_DECISION: dict[str, Any] = {
    "allowed": True,
    "path": "notes/scratch.md",
    "reason": None,
    "remediation": None,
    "rule": None,
}


def policy() -> dict[str, Any]:
    return {
        "version": 3,
        "wiki_language": "Portuguese (Brazil)",
        "inbox_policy": {"privacy": "local-only", "filing": "approve-each"},
        "branches": {
            "10-work": {"privacy": "never-ingest", "filing": "approve-each"},
            "20-research": {"privacy": "local-only", "filing": "auto+digest-review"},
        },
        "providers": {"ollama": {"base_url": "http://127.0.0.1:11434"}},
        "job_models": {
            job: {"provider": "ollama", "model": "test"}
            for job in (
                "ingest",
                "query",
                "digest",
                "lint-semantic",
                "file-proposal",
                "resurface",
            )
        },
        "sources": [],
        "schedule": {"digest": "0 8 * * *"},
        "taxonomy_seed": ["work", "research"],
        "novelty": {
            "duplicate_similarity_threshold": 0.9,
            "min_new_token_ratio": 0.15,
            "stale_after_days": 30,
        },
        "integrations": {
            name: {"enabled": False, "managed": False, "options": {}}
            for name in ("obsidian", "neo4j", "postgres", "web")
        },
    }


class VaultCase(unittest.TestCase):
    """A real vault, because the installer bakes its resolved path into scripts."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.vault = FileVault.initialize(self.root, policy())
        self.service = BrainskitService(
            self.vault,
            SqliteFtsIndex(self.vault.index_path),
            graph=MarkdownGraph(),
        )
        self.root = self.vault.root

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def install(
        self,
        agent: str = "claude",
        *,
        force: bool = False,
        skip_code_build: bool = False,
    ) -> dict[str, Any]:
        # The installer writes an enforcement banner to stderr; the tests below
        # assert on the returned structure, so keep the test output readable.
        with redirect_stderr(StringIO()):
            return cli._install_hooks(
                self.service, agent, force=force, skip_code_build=skip_code_build
            )

    def settings_path(self) -> Path:
        return self.root / ".claude" / "settings.json"

    def settings(self) -> dict[str, Any]:
        loaded = json.loads(self.settings_path().read_text(encoding="utf-8"))
        assert isinstance(loaded, dict)
        return loaded

    def script(self, name: str) -> Path:
        return self.root / ".claude" / "hooks" / f"{name}.sh"

    def commands(self, event: str) -> list[str]:
        found: list[str] = []
        for entry in self.settings()["hooks"].get(event, []):
            for item in entry.get("hooks", []):
                found.append(item["command"])
        return found


class HookScriptTest(VaultCase):
    """The shipped scripts land executable, self-contained and syntactically valid."""

    def test_both_hooks_are_written_and_executable(self) -> None:
        result = self.install()
        for name in ("brainskit-gate", "brainskit-status"):
            with self.subTest(script=name):
                self.assertEqual(result["claude_hook"]["scripts"][name]["state"], "created")
                path = self.script(name)
                self.assertTrue(path.is_file())
                self.assertTrue(os.stat(path).st_mode & stat.S_IXUSR)

    def test_the_vault_path_is_baked_in_shell_quoted(self) -> None:
        self.install()
        for name in ("brainskit-gate", "brainskit-status"):
            with self.subTest(script=name):
                content = self.script(name).read_text(encoding="utf-8")
                self.assertNotIn("{{vault}}", content)
                self.assertIn(str(self.root), content)
                self.assertIn(HOOK_SENTINEL, content)

    def test_a_vault_path_with_spaces_and_quotes_stays_parsable(self) -> None:
        # macOS stores accented components decomposed, and real project paths
        # carry spaces. A hook that cannot parse its own argument fails open on
        # every write it exists to govern.
        awkward = self.root / "Protótipos e 'coisas'"
        awkward.mkdir()
        rendered = installer._hook_script("brainskit-gate", awkward)
        line = next(
            item for item in rendered.splitlines() if item.startswith("VAULT=")
        )
        echoed = subprocess.run(
            ["sh", "-c", f'{line}\nprintf "%s" "$VAULT"'],
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertEqual(echoed.stdout, str(awkward))

    def test_the_scripts_are_valid_posix_shell(self) -> None:
        self.install()
        for name in ("brainskit-gate", "brainskit-status"):
            with self.subTest(script=name):
                for shell in ("sh", "bash"):
                    checked = subprocess.run(
                        [shell, "-n", str(self.script(name))],
                        capture_output=True,
                        text=True,
                    )
                    self.assertEqual(checked.returncode, 0, checked.stderr)

    def test_an_unmanaged_script_is_never_clobbered(self) -> None:
        target = self.script("brainskit-gate")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("#!/bin/sh\necho mine\n", encoding="utf-8")
        outcome = self.install()["claude_hook"]["scripts"]["brainskit-gate"]
        self.assertEqual(outcome["state"], "skipped")
        self.assertIn("unmanaged", outcome["reason"])
        self.assertEqual(target.read_text(encoding="utf-8"), "#!/bin/sh\necho mine\n")
        # ...and it is not registered either, so settings never points at a
        # script brainskit refused to manage.
        self.assertEqual(self.commands("PreToolUse"), [])

    def test_force_replaces_an_unmanaged_script(self) -> None:
        target = self.script("brainskit-gate")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("#!/bin/sh\necho mine\n", encoding="utf-8")
        outcome = self.install(force=True)["claude_hook"]["scripts"]["brainskit-gate"]
        self.assertEqual(outcome["state"], "updated")
        self.assertIn(HOOK_SENTINEL, target.read_text(encoding="utf-8"))

    def test_a_generated_script_is_repaired_in_place(self) -> None:
        self.install()
        target = self.script("brainskit-gate")
        target.write_text(f"{HOOK_SENTINEL}\nrotted\n", encoding="utf-8")
        target.chmod(0o644)
        outcome = self.install()["claude_hook"]["scripts"]["brainskit-gate"]
        self.assertEqual(outcome["state"], "updated")
        self.assertTrue(os.stat(target).st_mode & stat.S_IXUSR)

    def test_a_lost_execute_bit_is_restored(self) -> None:
        self.install()
        target = self.script("brainskit-status")
        target.chmod(0o644)
        outcome = self.install()["claude_hook"]["scripts"]["brainskit-status"]
        self.assertEqual(outcome["state"], "current")
        self.assertTrue(os.stat(target).st_mode & stat.S_IXUSR)

    def test_a_missing_script_template_is_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            installer._hook_script("does-not-exist", self.root)


class SettingsRegistrationTest(VaultCase):
    """`.claude/settings.json` belongs to the operator; the installer is a guest."""

    def test_a_missing_settings_file_is_created(self) -> None:
        self.assertFalse(self.settings_path().is_file())
        outcome = self.install()["claude_hook"]["settings"]
        self.assertEqual(outcome["state"], "created")
        self.assertEqual(
            self.commands("PreToolUse"), [str(self.script("brainskit-gate"))]
        )
        self.assertEqual(
            self.commands("SessionStart"), [str(self.script("brainskit-status"))]
        )

    def test_the_pre_tool_use_entry_matches_the_write_tools(self) -> None:
        self.install()
        entry = next(
            item
            for item in self.settings()["hooks"]["PreToolUse"]
            if any(
                hook["command"].endswith("brainskit-gate.sh") for hook in item["hooks"]
            )
        )
        self.assertEqual(entry["matcher"], "Write|Edit|MultiEdit")
        self.assertEqual(entry["hooks"][0]["type"], "command")
        self.assertEqual(entry["hooks"][0]["timeout"], 10)

    def _seed_unrelated_settings(self) -> dict[str, Any]:
        """A settings file shaped like a real one: other tooling, same events."""
        existing = {
            "model": "opus",
            "statusLine": {"type": "command", "command": "mine.sh"},
            "permissions": {"allow": ["Bash(git status:*)"]},
            "hooks": {
                "PreToolUse": [
                    {
                        "matcher": "Bash",
                        "hooks": [{"type": "command", "command": "/other/rtk.sh"}],
                    }
                ],
                "SessionStart": [
                    {"hooks": [{"type": "command", "command": "/other/session.sh"}]}
                ],
                "Stop": [{"hooks": [{"type": "command", "command": "/other/stop.sh"}]}],
            },
        }
        self.settings_path().parent.mkdir(parents=True, exist_ok=True)
        self.settings_path().write_text(
            json.dumps(existing, indent=2), encoding="utf-8"
        )
        return existing

    def test_unrelated_hooks_and_keys_survive_verbatim(self) -> None:
        original = self._seed_unrelated_settings()
        self.install()
        merged = self.settings()
        for key in ("model", "statusLine", "permissions"):
            with self.subTest(key=key):
                self.assertEqual(merged[key], original[key])
        self.assertEqual(merged["hooks"]["Stop"], original["hooks"]["Stop"])
        self.assertIn(original["hooks"]["PreToolUse"][0], merged["hooks"]["PreToolUse"])
        self.assertIn(
            original["hooks"]["SessionStart"][0], merged["hooks"]["SessionStart"]
        )
        self.assertIn("/other/rtk.sh", self.commands("PreToolUse"))
        self.assertIn("/other/session.sh", self.commands("SessionStart"))

    def test_a_second_install_leaves_the_file_byte_identical(self) -> None:
        self._seed_unrelated_settings()
        self.install()
        first = self.settings_path().read_bytes()
        copied = self.root / "settings-copy.json"
        shutil.copyfile(self.settings_path(), copied)

        outcome = self.install()["claude_hook"]["settings"]

        self.assertEqual(outcome["state"], "current")
        self.assertEqual(self.settings_path().read_bytes(), first)
        self.assertEqual(copied.read_bytes(), self.settings_path().read_bytes())
        self.assertEqual(
            [item["state"] for item in outcome["registered"]], ["current", "current"]
        )

    def test_reinstalling_never_registers_a_second_entry(self) -> None:
        for _ in range(3):
            self.install()
        gate = str(self.script("brainskit-gate"))
        status = str(self.script("brainskit-status"))
        self.assertEqual(self.commands("PreToolUse").count(gate), 1)
        self.assertEqual(self.commands("SessionStart").count(status), 1)

    def test_an_entry_registered_by_hand_counts_as_present(self) -> None:
        # The idempotency key is the command path, not the shape of the entry
        # brainskit would have written.
        self.install()
        self.settings_path().write_text(
            json.dumps(
                {
                    "hooks": {
                        "PreToolUse": [
                            {
                                "matcher": "Write",
                                "hooks": [
                                    {
                                        "type": "command",
                                        "command": str(self.script("brainskit-gate")),
                                    }
                                ],
                            }
                        ],
                        "SessionStart": [
                            {
                                "hooks": [
                                    {
                                        "type": "command",
                                        "command": str(self.script("brainskit-status")),
                                    }
                                ]
                            }
                        ],
                    }
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        before = self.settings_path().read_bytes()
        outcome = self.install()["claude_hook"]["settings"]
        self.assertEqual(outcome["state"], "current")
        self.assertEqual(self.settings_path().read_bytes(), before)

    def test_malformed_settings_are_reported_and_left_alone(self) -> None:
        self.settings_path().parent.mkdir(parents=True, exist_ok=True)
        broken = '{ "hooks": [ this is not json\n'
        self.settings_path().write_text(broken, encoding="utf-8")
        outcome = self.install()["claude_hook"]["settings"]
        self.assertEqual(outcome["state"], "skipped")
        self.assertIn("not valid JSON", outcome["reason"])
        self.assertEqual(self.settings_path().read_text(encoding="utf-8"), broken)

    def test_a_settings_file_that_is_not_an_object_is_left_alone(self) -> None:
        self.settings_path().parent.mkdir(parents=True, exist_ok=True)
        self.settings_path().write_text("[1, 2, 3]", encoding="utf-8")
        outcome = self.install()["claude_hook"]["settings"]
        self.assertEqual(outcome["state"], "skipped")
        self.assertEqual(self.settings_path().read_text(encoding="utf-8"), "[1, 2, 3]")

    def test_an_unexpected_hooks_shape_is_left_alone(self) -> None:
        for content in ('{"hooks": []}', '{"hooks": {"PreToolUse": {}}}'):
            with self.subTest(content=content):
                self.settings_path().parent.mkdir(parents=True, exist_ok=True)
                self.settings_path().write_text(content, encoding="utf-8")
                outcome = self.install()["claude_hook"]["settings"]
                self.assertEqual(outcome["state"], "skipped")
                self.assertEqual(
                    self.settings_path().read_text(encoding="utf-8"), content
                )


class StaleHookReplacementTest(VaultCase):
    """A settings.json carried over from a different `--root` or vault must not
    end up running alongside the correct install rather than being replaced by it.

    Reproduces onboarding `eigent`: a settings.json copied in from another
    project's `.claude/` (or written by an earlier install at a different
    `--root`) registers `brainskit-gate.sh`/`brainskit-status.sh` at a path this
    install will never write to. The old idempotency key (the literal command
    string) never recognised that as the *same* hook, so both stayed
    registered forever.
    """

    STALE_GATE = "/some/other/repo/.claude/hooks/brainskit-gate.sh"
    STALE_STATUS = "/some/other/repo/.claude/hooks/brainskit-status.sh"

    def seed_stale_hooks(self, *, alongside_unrelated: bool = False) -> None:
        pre_tool_use = [
            {
                "matcher": "Write|Edit|MultiEdit",
                "hooks": [{"type": "command", "command": self.STALE_GATE, "timeout": 10}],
            }
        ]
        session_start = [
            {"hooks": [{"type": "command", "command": self.STALE_STATUS, "timeout": 15}]}
        ]
        if alongside_unrelated:
            pre_tool_use.append(
                {"matcher": "Bash", "hooks": [{"type": "command", "command": "/other/rtk.sh"}]}
            )
            session_start.append(
                {"hooks": [{"type": "command", "command": "/other/session.sh"}]}
            )
        self.settings_path().parent.mkdir(parents=True, exist_ok=True)
        self.settings_path().write_text(
            json.dumps(
                {"hooks": {"PreToolUse": pre_tool_use, "SessionStart": session_start}},
                indent=2,
            ),
            encoding="utf-8",
        )

    def test_a_stale_path_is_replaced_not_duplicated(self) -> None:
        self.seed_stale_hooks()
        gate = str(self.script("brainskit-gate"))
        status = str(self.script("brainskit-status"))
        self.install()
        self.assertEqual(self.commands("PreToolUse"), [gate])
        self.assertEqual(self.commands("SessionStart"), [status])
        self.assertNotIn(self.STALE_GATE, self.commands("PreToolUse"))
        self.assertNotIn(self.STALE_STATUS, self.commands("SessionStart"))

    def test_the_replacement_is_reported(self) -> None:
        self.seed_stale_hooks()
        outcome = self.install()["claude_hook"]["settings"]
        self.assertEqual(
            outcome["pruned"],
            [
                {"event": "PreToolUse", "command": self.STALE_GATE},
                {"event": "SessionStart", "command": self.STALE_STATUS},
            ],
        )

    def test_the_replacement_warns_on_stderr(self) -> None:
        self.seed_stale_hooks()
        buffer = StringIO()
        with redirect_stderr(buffer):
            cli._install_hooks(self.service, "claude")
        warning = buffer.getvalue()
        self.assertIn("STALE HOOKS replaced", warning)
        self.assertIn(self.STALE_GATE, warning)
        self.assertIn(self.STALE_STATUS, warning)

    def test_unrelated_tooling_on_the_same_event_survives_the_prune(self) -> None:
        self.seed_stale_hooks(alongside_unrelated=True)
        self.install()
        self.assertIn("/other/rtk.sh", self.commands("PreToolUse"))
        self.assertIn("/other/session.sh", self.commands("SessionStart"))
        self.assertNotIn(self.STALE_GATE, self.commands("PreToolUse"))
        self.assertNotIn(self.STALE_STATUS, self.commands("SessionStart"))

    def test_an_entry_left_empty_by_pruning_is_dropped_not_left_as_debris(self) -> None:
        self.seed_stale_hooks()
        self.install()
        for event in ("PreToolUse", "SessionStart"):
            for entry in self.settings()["hooks"][event]:
                self.assertTrue(entry.get("hooks"), f"{event} kept an emptied entry")

    def test_reinstalling_at_the_same_path_prunes_nothing_and_stays_idempotent(
        self,
    ) -> None:
        self.install()
        before = self.settings_path().read_bytes()
        outcome = self.install()["claude_hook"]["settings"]
        self.assertNotIn("pruned", outcome)
        self.assertEqual(self.settings_path().read_bytes(), before)


class PreRenameInstallMigrationTest(VaultCase):
    """A rename has to migrate an install, not duplicate it.

    `_prune_stale_hook_entries` and the instruction-block writer both keyed on
    the *current* name, so artefacts of a former one were invisible to them.
    Reproduced on a workspace carrying only pre-rename artefacts: `--force`
    pruned nothing, the old gate stayed registered beside the new one, both
    scripts stayed on disk, and the instruction file ended with two managed
    blocks. That is the state every install predating the rename upgrades into.
    """

    LEGACY = installer.LEGACY_BRANDS[0]

    def legacy_name(self, template: str) -> str:
        return template.replace(BRAND, self.LEGACY)

    def legacy_script(self, template: str) -> Path:
        return self.root / ".claude" / "hooks" / f"{self.legacy_name(template)}.sh"

    def seed_pre_rename_install(self, *, sentinel: bool = True) -> None:
        """A workspace as an install under the former name left it.

        The scripts carry that brand's generated sentinel, because that is what
        an unedited pre-rename install actually has on disk and it is the only
        thing distinguishing its files from the operator's.
        """

        hooks = self.root / ".claude" / "hooks"
        hooks.mkdir(parents=True, exist_ok=True)
        mark = HOOK_SENTINEL.replace(BRAND, self.LEGACY)
        entries: dict[str, list[Any]] = {"PreToolUse": [], "SessionStart": []}
        for hook in installer.CLAUDE_HOOKS:
            path = self.legacy_script(hook.template)
            body = f"#!/bin/sh\n{mark} - old\nexit 0\n" if sentinel else "#!/bin/sh\nexit 0\n"
            path.write_text(body, encoding="utf-8")
            path.chmod(0o755)
            entry: dict[str, Any] = {}
            if hook.matcher:
                entry["matcher"] = hook.matcher
            entry["hooks"] = [{"type": "command", "command": str(path), "timeout": 10}]
            entries[hook.event].append(entry)
        self.settings_path().write_text(
            json.dumps({"hooks": entries}, indent=2), encoding="utf-8"
        )
        (self.root / "CLAUDE.md").write_text(
            "keep above\n\n"
            f"{INSTRUCTION_START.replace(BRAND, self.LEGACY)}\n"
            "old contract\n"
            f"{INSTRUCTION_END.replace(BRAND, self.LEGACY)}\n\n"
            "keep below\n",
            encoding="utf-8",
        )

    def instructions(self) -> str:
        return (self.root / "CLAUDE.md").read_text(encoding="utf-8")

    # settings.json -----------------------------------------------------------

    def test_a_former_brands_hook_is_replaced_not_run_alongside(self) -> None:
        self.seed_pre_rename_install()
        self.install()
        self.assertEqual(self.commands("PreToolUse"), [str(self.script("brainskit-gate"))])
        self.assertEqual(
            self.commands("SessionStart"), [str(self.script("brainskit-status"))]
        )

    def test_the_migration_needs_no_force(self) -> None:
        """`--force` decides whether to clobber an install that is *currently*
        the right one. Finishing a rename this tool performed is not that
        question, and gating it would leave the documented no-flag upgrade
        silently running two gates."""

        self.seed_pre_rename_install()
        self.install(force=False)
        self.assertEqual(self.commands("PreToolUse"), [str(self.script("brainskit-gate"))])
        self.assertFalse(self.legacy_script("brainskit-gate").exists())
        self.assertEqual(1, self.instructions().count(INSTRUCTION_START))

    # the instruction file ----------------------------------------------------

    def test_the_instruction_file_ends_with_exactly_one_managed_block(self) -> None:
        self.seed_pre_rename_install()
        self.install()
        text = self.instructions()
        self.assertEqual(1, text.count(INSTRUCTION_START))
        self.assertNotIn(INSTRUCTION_START.replace(BRAND, self.LEGACY), text)

    def test_the_block_keeps_the_position_the_former_one_held(self) -> None:
        """Appending would move the contract below whatever the operator wrote
        after it, which is a change they never asked for."""

        self.seed_pre_rename_install()
        self.install()
        text = self.instructions()
        self.assertLess(text.index("keep above"), text.index(INSTRUCTION_START))
        self.assertLess(text.index(INSTRUCTION_START), text.index("keep below"))

    def test_a_current_block_beside_a_former_one_keeps_only_the_current(self) -> None:
        """This repository's own state: both blocks present at once."""

        self.seed_pre_rename_install()
        (self.root / "CLAUDE.md").write_text(
            self.instructions()
            + f"\n{INSTRUCTION_START}\ncurrent\n{INSTRUCTION_END}\n",
            encoding="utf-8",
        )
        before = self.instructions()
        self.assertEqual(1, before.count(INSTRUCTION_START))
        self.assertEqual(1, before.count(INSTRUCTION_START.replace(BRAND, self.LEGACY)))
        self.install()
        text = self.instructions()
        self.assertEqual(1, text.count(INSTRUCTION_START))
        self.assertNotIn(INSTRUCTION_START.replace(BRAND, self.LEGACY), text)

    # scripts on disk ---------------------------------------------------------

    def test_an_unedited_former_script_is_removed(self) -> None:
        self.seed_pre_rename_install()
        outcome = self.install()["claude_hook"]["legacy"]
        for hook in installer.CLAUDE_HOOKS:
            with self.subTest(hook=hook.template):
                self.assertFalse(self.legacy_script(hook.template).exists())
        self.assertEqual({"removed"}, {item["state"] for item in outcome})

    def test_an_edited_former_script_is_unregistered_but_never_deleted(self) -> None:
        """Dropping a settings entry is reversible by reinstalling. Deleting a
        file the operator edited is not, and nothing here proves it was ours."""

        self.seed_pre_rename_install(sentinel=False)
        outcome = self.install()["claude_hook"]["legacy"]
        for hook in installer.CLAUDE_HOOKS:
            with self.subTest(hook=hook.template):
                self.assertTrue(self.legacy_script(hook.template).is_file())
        self.assertEqual({"kept"}, {item["state"] for item in outcome})
        self.assertTrue(all(item["reason"] for item in outcome))
        self.assertEqual(self.commands("PreToolUse"), [str(self.script("brainskit-gate"))])

    def test_a_former_script_survives_when_its_replacement_was_not_installed(
        self,
    ) -> None:
        """Its registration survives too, so deleting the file would turn inert
        debris into a command pointing at nothing."""

        self.seed_pre_rename_install()
        target = self.script("brainskit-gate")
        target.write_text("#!/bin/sh\necho mine\n", encoding="utf-8")
        self.install()
        self.assertTrue(self.legacy_script("brainskit-gate").is_file())
        self.assertFalse(self.legacy_script("brainskit-status").exists())

    # the skill ---------------------------------------------------------------

    def test_a_former_skill_directory_is_reported_and_left_alone(self) -> None:
        """A hook proves it is ours with the generated sentinel; a skill is plain
        markdown with no such mark, and it is read rather than executed."""

        legacy = self.root / ".claude" / "skills" / self.LEGACY
        legacy.mkdir(parents=True)
        (legacy / "SKILL.md").write_text("mine\n", encoding="utf-8")
        outcome = self.install()["skill"]
        self.assertEqual([str(legacy)], outcome["legacy"])
        self.assertTrue((legacy / "SKILL.md").is_file())

    # reporting ---------------------------------------------------------------

    def test_the_whole_migration_is_announced_on_stderr(self) -> None:
        self.seed_pre_rename_install()
        (self.root / ".claude" / "skills" / self.LEGACY).mkdir(parents=True)
        buffer = StringIO()
        with redirect_stderr(buffer):
            cli._install_hooks(self.service, "claude", skip_code_build=True)
        warning = buffer.getvalue()
        self.assertIn("RENAME", warning)
        self.assertIn(str(self.legacy_script("brainskit-gate")), warning)
        self.assertIn(INSTRUCTION_START.replace(BRAND, self.LEGACY), warning)
        self.assertIn(str(self.root / ".claude" / "skills" / self.LEGACY), warning)

    # controls ----------------------------------------------------------------

    def test_a_clean_install_reports_no_migration_at_all(self) -> None:
        """Control: nothing here may fire on a workspace with no former brand."""

        buffer = StringIO()
        with redirect_stderr(buffer):
            result = cli._install_hooks(self.service, "claude", skip_code_build=True)
        self.assertNotIn("RENAME", buffer.getvalue())
        self.assertNotIn("legacy", result["claude_hook"])
        self.assertNotIn("legacy", result["skill"])
        self.assertNotIn("migrated", result["instructions"])

    def test_the_brand_the_migration_keys_on_is_the_one_in_use(self) -> None:
        """One list of former names covers hooks, sentinels, blocks and skills
        only while every artefact spells the brand identically. If a rename ever
        breaks that, this fails instead of the migration silently doing nothing.
        """

        self.assertIn(BRAND, INSTRUCTION_START)
        self.assertIn(BRAND, INSTRUCTION_END)
        self.assertIn(BRAND, HOOK_SENTINEL)
        for hook in installer.CLAUDE_HOOKS:
            with self.subTest(hook=hook.template):
                self.assertTrue(hook.template.startswith(f"{BRAND}-"))
        self.assertNotIn(BRAND, installer.LEGACY_BRANDS)


class EnforcementReportTest(VaultCase):
    """Every layer that is off has to say so; a quiet skip is how one disappears."""

    def test_a_non_git_vault_reports_the_missing_layer_loudly(self) -> None:
        buffer = StringIO()
        with redirect_stderr(buffer):
            result = cli._install_hooks(self.service, "claude")
        pre_commit = result["pre_commit"]
        self.assertEqual(pre_commit["state"], "skipped")
        self.assertEqual(pre_commit["enforcement"], "off")
        self.assertIn("bk lint --changed", pre_commit["consequence"])
        self.assertIn("commit_lint", result["enforcement"]["inactive"])
        warning = buffer.getvalue()
        self.assertIn("ENFORCEMENT GAP", warning)
        self.assertIn("commit_lint", warning)
        # The message names the directory it looked in, because with --root
        # that is no longer necessarily the vault.
        self.assertIn(f"{self.root} is not a git repository", warning)
        self.assertIn("--root", warning)

    def test_an_existing_pre_commit_hook_reports_the_gap_too(self) -> None:
        hooks = self.root / ".git" / "hooks"
        hooks.mkdir(parents=True)
        (hooks / "pre-commit").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        result = self.install()
        self.assertEqual(result["pre_commit"]["enforcement"], "off")
        self.assertIn("commit_lint", result["enforcement"]["inactive"])

    def test_a_git_vault_reports_every_layer_active(self) -> None:
        (self.root / ".git" / "hooks").mkdir(parents=True)
        buffer = StringIO()
        with redirect_stderr(buffer):
            result = cli._install_hooks(self.service, "claude")
        self.assertEqual(result["enforcement"]["inactive"], [])
        self.assertNotIn("ENFORCEMENT GAP", buffer.getvalue())
        layers = {item["layer"]: item for item in result["enforcement"]["layers"]}
        self.assertTrue(layers["write_gate"]["active"])
        self.assertTrue(layers["session_status"]["active"])
        self.assertTrue(layers["commit_lint"]["active"])
        # Instructions are named as advisory rather than counted as enforcement.
        self.assertTrue(layers["instructions"]["advisory"])

    def test_a_skipped_script_makes_the_write_gate_inactive(self) -> None:
        target = self.script("brainskit-gate")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("#!/bin/sh\n", encoding="utf-8")
        result = self.install()
        layers = {item["layer"]: item for item in result["enforcement"]["layers"]}
        self.assertFalse(layers["write_gate"]["active"])
        self.assertTrue(layers["session_status"]["active"])
        self.assertIn("write_gate", result["enforcement"]["inactive"])

    def test_the_adapter_carries_the_policy_the_gate_reads(self) -> None:
        result = self.install()
        stored = json.loads(
            (self.root / result["adapter"]).read_text(encoding="utf-8")
        )
        self.assertEqual(stored["version"], 2)
        self.assertEqual(stored["workspace"], str(self.root))
        self.assertEqual(stored["gate"]["deny_prefixes"], ["wiki/", "raw/"])
        self.assertIn("bk apply", stored["gate"]["remediation"]["wiki/"])
        self.assertIn("bk capture", stored["gate"]["remediation"]["raw/"])
        self.assertEqual(len(stored["rules"]), 3)

    def test_agents_other_than_claude_get_no_claude_hook(self) -> None:
        result = self.install("codex")
        self.assertNotIn("claude_hook", result)
        self.assertFalse(self.settings_path().exists())
        layers = {item["layer"] for item in result["enforcement"]["layers"]}
        self.assertEqual(layers, {"commit_lint", "instructions"})


class CodeGraphBootstrapTest(VaultCase):
    """`hooks install` runs the first `bk code build` itself.

    Without this, `bk code status` reports `missing` until an agent notices
    the `code build` row in the skill's own command table and runs it
    unprompted -- which nothing here ever asked it to do.
    """

    def test_no_extractor_configured_reports_skipped_not_raised(self) -> None:
        # `self.service` (VaultCase) is built the same way every other test in
        # this file uses it -- no extractor -- so this is also what every
        # other `install()` call in this suite has been exercising all along.
        result = self.install()
        code_graph = result["code_graph"]
        self.assertEqual(code_graph["state"], "skipped")
        self.assertIn("No code extractor is configured", code_graph["reason"])
        self.assertIn("bk code import", code_graph["hint"])

    def test_the_skip_flag_is_reported_without_touching_the_extractor(self) -> None:
        result = self.install(skip_code_build=True)
        self.assertEqual(
            result["code_graph"],
            {"state": "skipped", "reason": "--skip-code-build was passed"},
        )

    def test_a_skip_warns_on_stderr_by_name(self) -> None:
        buffer = StringIO()
        with redirect_stderr(buffer):
            cli._install_hooks(self.service, "claude", skip_code_build=True)
        warning = buffer.getvalue()
        self.assertIn("CODE GRAPH not built during onboarding", warning)
        self.assertIn("--skip-code-build was passed", warning)
        self.assertIn("bk code build", warning)

    def test_an_unavailable_extractor_is_reported_not_raised(self) -> None:
        service = BrainskitService(
            self.vault,
            SqliteFtsIndex(self.vault.index_path),
            graph=MarkdownGraph(),
            extractor=_UnavailableExtractor(),
        )
        buffer = StringIO()
        with redirect_stderr(buffer):
            result = cli._install_hooks(service, "claude")
        self.assertEqual(result["code_graph"]["state"], "skipped")
        self.assertIn("brainskit[code]", result["code_graph"]["hint"])
        self.assertIn("brainskit[code]", buffer.getvalue())


class _UnavailableExtractor:
    """Stands in for `GraphifyExtractor` on a machine without the `code` extra.

    Mirrors `test_code_graph._UnavailableExtractor`; kept local rather than
    imported because that one asserts `extract()` is never called, which is
    an assumption about `CodeGraph.build` this file has no reason to depend
    on to make its own, narrower point about `_install_hooks`.
    """

    def available(self) -> bool:
        return False

    def extract(self, root: Path, paths: list[Path] | None = None, **_: Any) -> dict[str, Any]:
        return {"nodes": [], "edges": []}


@unittest.skipUnless(_HAS_CODE_EXTRA, "requires the `code` extra (tree-sitter + grammars)")
class CodeGraphBootstrapBuildTest(unittest.TestCase):
    """The one place `hooks install` meets a real extractor end to end."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.repo = Path(self.temporary.name) / "repo"
        (self.repo / "src").mkdir(parents=True)
        (self.repo / ".git").mkdir()
        (self.repo / "src" / "app.py").write_text(
            "def main():\n    return 1\n", encoding="utf-8"
        )
        self.vault = FileVault.initialize(self.repo / "docs" / "brain", policy())
        self.service = BrainskitService(
            self.vault,
            SqliteFtsIndex(self.vault.index_path),
            graph=MarkdownGraph(),
            extractor=GraphifyExtractor(),
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def graph_file(self) -> Path:
        return self.vault.root / "graph" / "code.json"

    def test_the_graph_is_built_and_the_run_stays_quiet_about_it(self) -> None:
        buffer = StringIO()
        with redirect_stderr(buffer):
            result = cli._install_hooks(self.service, "claude", root=str(self.repo))
        code_graph = result["code_graph"]
        self.assertEqual(code_graph["state"], "built")
        self.assertEqual(code_graph["written"], "graph/code.json")
        self.assertGreater(code_graph["nodes"], 0)
        self.assertTrue(self.graph_file().is_file())
        self.assertNotIn("CODE GRAPH not built", buffer.getvalue())

    def test_status_reports_fresh_immediately_after_install(self) -> None:
        cli._install_hooks(self.service, "claude", root=str(self.repo))
        self.assertEqual(self.service.code_status()["state"], "fresh")

    def test_skip_code_build_leaves_no_graph_file_even_with_a_real_extractor(self) -> None:
        result = cli._install_hooks(
            self.service, "claude", root=str(self.repo), skip_code_build=True
        )
        self.assertEqual(result["code_graph"]["state"], "skipped")
        self.assertFalse(self.graph_file().exists())
        self.assertEqual(self.service.code_status()["state"], "missing")


class GateCommandTest(VaultCase):
    """`bk gate check-write` answers with an exit code a shell can branch on.

    `gate_check_write` is owned by the decision engine, so it is stubbed here:
    these tests cover the CLI contract around it, not the policy inside it.
    """

    def run_cli(self, argv: list[str], decision: dict[str, Any]) -> tuple[int, str, str]:
        out, err = StringIO(), StringIO()
        with patch.object(
            BrainskitService,
            "gate_check_write",
            create=True,
            return_value=decision,
        ) as stub:
            with redirect_stdout(out), redirect_stderr(err):
                code = cli.main(["--vault", str(self.root), *argv])
        self.stub = stub
        return code, out.getvalue(), err.getvalue()

    def test_a_denied_write_exits_two_and_explains_itself_on_stderr(self) -> None:
        code, out, err = self.run_cli(
            ["gate", "check-write", "wiki/concepts/foo.md"], DENY_DECISION
        )
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn(DENY_DECISION["reason"], err)
        self.assertIn("bk apply", err)
        self.assertEqual(len(err.strip().splitlines()), 1)

    def test_an_allowed_write_exits_zero(self) -> None:
        code, out, err = self.run_cli(
            ["gate", "check-write", "notes/scratch.md"], ALLOW_DECISION
        )
        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        self.assertIn("notes/scratch.md", out)

    def test_json_mode_prints_the_decision_and_still_exits_two(self) -> None:
        code, out, err = self.run_cli(
            ["gate", "check-write", "wiki/concepts/foo.md", "--json"], DENY_DECISION
        )
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(out), DENY_DECISION)
        self.assertEqual(err, "")

    def test_json_mode_prints_an_allowed_decision_too(self) -> None:
        code, out, _ = self.run_cli(
            ["gate", "check-write", "notes/scratch.md", "--json"], ALLOW_DECISION
        )
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out), ALLOW_DECISION)

    def test_the_global_json_flag_is_honoured_before_the_subcommand(self) -> None:
        code, out, _ = self.run_cli(
            ["--json", "gate", "check-write", "wiki/concepts/foo.md"], DENY_DECISION
        )
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(out), DENY_DECISION)

    def test_the_agent_defaults_to_claude_and_is_overridable(self) -> None:
        self.run_cli(["gate", "check-write", "x.md"], ALLOW_DECISION)
        self.assertEqual(self.stub.call_args.kwargs["agent"], "claude")
        self.run_cli(
            ["gate", "check-write", "x.md", "--agent", "codex"], ALLOW_DECISION
        )
        self.assertEqual(self.stub.call_args.kwargs["agent"], "codex")
        # A relative target is absolutised against the caller's directory before
        # it reaches the service: `check_write` resolves relative paths against
        # the *vault root*, so `bk gate check-write wiki/x.md` from anywhere
        # else answered about a different file than the one named -- with
        # exit 0. The installed hook always passes absolute paths and is
        # unaffected. See `GateCliPathBaseTest`.
        self.assertEqual(
            self.stub.call_args.args, (str(Path.cwd() / "x.md"),)
        )

    def test_a_decision_without_a_verdict_is_an_internal_error_not_a_denial(
        self,
    ) -> None:
        # Exit 2 has to mean "denied" and nothing else, so a malformed decision
        # must not arrive at the hook wearing a denial's exit code by accident.
        out, err = StringIO(), StringIO()
        with patch.object(
            BrainskitService, "gate_check_write", create=True, return_value={}
        ):
            with redirect_stdout(out), redirect_stderr(err):
                code = cli.main(
                    ["--vault", str(self.root), "--json", "gate", "check-write", "x.md"]
                )
        self.assertEqual(code, 2)
        payload = json.loads(out.getvalue())
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"]["code"], "internal_error")


class ShellHookCase(VaultCase):
    """Drives the installed shell scripts as Claude Code would."""

    #: `bk` stand-in. FAKE_BK_MODE selects which failure is under test.
    FAKE_BK = """#!/bin/sh
MODE=${FAKE_BK_MODE:-decide}
TARGET=
for arg in "$@"; do
    case "$arg" in
    --*|gate|check-write|status|proposals|lint) ;;
    *) TARGET=$arg ;;
    esac
done
case "$MODE" in
decide)
    case "$TARGET" in
    */wiki/*|wiki/*)
        printf '%s\\n' '{"allowed": false, "path": "wiki/concepts/foo.md", \
"reason": "This page is compiled from evidence.", \
"remediation": "Wiki pages are written only by the apply gate. Use: bk apply", \
"rule": "wiki/"}'
        exit 2
        ;;
    *)
        printf '%s\\n' '{"allowed": true, "path": "notes/scratch.md", \
"reason": null, "remediation": null, "rule": null}'
        exit 0
        ;;
    esac
    ;;
error_envelope)
    printf '%s\\n' '{"ok": false, "error": {"code": "not_found", \
"message": "Not a brainskit vault"}}'
    exit 2
    ;;
usage_error)
    printf 'bk: error: invalid choice\\n' >&2
    exit 2
    ;;
silent_two)
    exit 2
    ;;
crash)
    exit 137
    ;;
esac
"""

    def setUp(self) -> None:
        super().setUp()
        self.install()
        self.bin = self.root / "fake-bin"
        self.bin.mkdir()
        fake = self.bin / "bk"
        fake.write_text(self.FAKE_BK, encoding="utf-8")
        fake.chmod(0o755)

    def link(self, directory: Path, *tools: str) -> None:
        """Symlink real binaries into a restricted PATH directory."""
        for tool in tools:
            found = shutil.which(tool)
            if found:
                os.symlink(found, directory / tool)

    def drive(
        self,
        script: str,
        payload: str = "",
        *,
        path: str | None = None,
        env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        environment = dict(os.environ)
        environment["PATH"] = (
            path if path is not None else f"{self.bin}{os.pathsep}{os.environ['PATH']}"
        )
        environment.update(env or {})
        return subprocess.run(
            [str(self.script(script))],
            input=payload,
            capture_output=True,
            text=True,
            env=environment,
            timeout=60,
        )

    @staticmethod
    def hook_payload(target: str, *, key: str = "file_path") -> str:
        return json.dumps({"tool_name": "Write", "tool_input": {key: target}})


class GateScriptTest(ShellHookCase):
    """The PreToolUse hook: exit 2 only for a real denial, exit 0 for everything else."""

    def test_a_write_under_wiki_is_refused_with_the_remediation(self) -> None:
        done = self.drive(
            "brainskit-gate", self.hook_payload(f"{self.root}/wiki/concepts/foo.md")
        )
        self.assertEqual(done.returncode, 2)
        self.assertIn("compiled from evidence", done.stderr)
        self.assertIn("bk apply", done.stderr)
        self.assertEqual(done.stdout, "")

    def test_a_write_elsewhere_passes_silently(self) -> None:
        done = self.drive(
            "brainskit-gate", self.hook_payload(f"{self.root}/notes/scratch.md")
        )
        self.assertEqual(done.returncode, 0)
        self.assertEqual(done.stdout, "")
        self.assertEqual(done.stderr, "")

    def test_the_camel_case_field_name_is_understood(self) -> None:
        done = self.drive(
            "brainskit-gate",
            self.hook_payload(f"{self.root}/wiki/concepts/foo.md", key="filePath"),
        )
        self.assertEqual(done.returncode, 2)

    def test_jq_reaches_the_same_verdicts_as_python(self) -> None:
        if not shutil.which("jq"):
            self.skipTest("jq is not installed")
        restricted = self.root / "jq-bin"
        restricted.mkdir()
        os.symlink(self.bin / "bk", restricted / "bk")
        self.link(restricted, "jq", "cat", "printf", "dirname", "sed")
        for target, expected in (
            (f"{self.root}/wiki/concepts/foo.md", 2),
            (f"{self.root}/notes/scratch.md", 0),
        ):
            with self.subTest(target=target):
                done = self.drive(
                    "brainskit-gate",
                    self.hook_payload(target),
                    path=str(restricted),
                )
                self.assertEqual(done.returncode, expected)
        denied = self.drive(
            "brainskit-gate",
            self.hook_payload(f"{self.root}/wiki/concepts/foo.md"),
            path=str(restricted),
        )
        self.assertIn("bk apply", denied.stderr)


class GateScriptFailOpenTest(ShellHookCase):
    """Every way this hook can break has to end in exit 0, with a note on stderr.

    A gate that blocks the session on its own breakage is worse than no gate:
    `bk lint` still reports the bypass as `wiki.outside_apply`, but nothing
    recovers a session the hook wedged shut.
    """

    DENY_TARGET = "wiki/concepts/foo.md"

    def assert_fails_open(
        self, done: subprocess.CompletedProcess[str], expected: str
    ) -> None:
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("brainskit-gate:", done.stderr)
        self.assertIn(expected, done.stderr)

    def test_empty_stdin(self) -> None:
        self.assert_fails_open(self.drive("brainskit-gate", ""), "empty hook payload")

    def test_malformed_stdin(self) -> None:
        self.assert_fails_open(
            self.drive("brainskit-gate", "{not json at all"), "no file path"
        )

    def test_stdin_without_a_tool_input(self) -> None:
        self.assert_fails_open(
            self.drive("brainskit-gate", json.dumps({"tool_name": "Bash"})), "no file path"
        )

    def test_a_tool_input_that_is_not_an_object(self) -> None:
        self.assert_fails_open(
            self.drive("brainskit-gate", json.dumps({"tool_input": "nope"})),
            "no file path",
        )

    def test_a_tool_without_a_file_path(self) -> None:
        self.assert_fails_open(
            self.drive(
                "brainskit-gate", json.dumps({"tool_input": {"command": "ls"}})
            ),
            "no file path",
        )

    def test_bk_missing_from_path(self) -> None:
        bare = self.root / "bare-bin"
        bare.mkdir()
        self.link(bare, "python3", "cat", "printf", "dirname", "sed")
        self.assert_fails_open(
            self.drive(
                "brainskit-gate",
                self.hook_payload(self.DENY_TARGET),
                path=str(bare),
            ),
            "bk is not on PATH",
        )

    def test_neither_python3_nor_jq(self) -> None:
        bare = self.root / "no-parser-bin"
        bare.mkdir()
        os.symlink(self.bin / "bk", bare / "bk")
        self.link(bare, "cat", "printf", "sed")
        self.assert_fails_open(
            self.drive(
                "brainskit-gate",
                self.hook_payload(self.DENY_TARGET),
                path=str(bare),
            ),
            "neither python3 nor jq",
        )

    def test_a_vault_that_has_been_moved_away(self) -> None:
        script = self.script("brainskit-gate")
        stranded = Path(self.temporary.name).parent / f"{self.root.name}-stranded.sh"
        shutil.copyfile(script, stranded)
        stranded.chmod(0o755)
        moved = self.root.parent / f"{self.root.name}-moved"
        self.root.rename(moved)
        try:
            done = subprocess.run(
                [str(stranded)],
                input=self.hook_payload(self.DENY_TARGET),
                capture_output=True,
                text=True,
                env={**os.environ, "PATH": f"{moved / 'fake-bin'}{os.pathsep}{os.environ['PATH']}"},
                timeout=60,
            )
        finally:
            moved.rename(self.root)
            stranded.unlink()
        self.assert_fails_open(done, "is unreachable")

    def test_an_error_envelope_carrying_exit_two_is_not_a_denial(self) -> None:
        # `bk` returns 2 for any modelled error, and argparse returns 2 for an
        # unknown subcommand. The exit code alone is never proof of a denial.
        self.assert_fails_open(
            self.drive(
                "brainskit-gate",
                self.hook_payload(self.DENY_TARGET),
                env={"FAKE_BK_MODE": "error_envelope"},
            ),
            "without a deny decision",
        )

    def test_a_usage_error_carrying_exit_two_is_not_a_denial(self) -> None:
        self.assert_fails_open(
            self.drive(
                "brainskit-gate",
                self.hook_payload(self.DENY_TARGET),
                env={"FAKE_BK_MODE": "usage_error"},
            ),
            "exited 2",
        )

    def test_exit_two_with_no_output_is_not_a_denial(self) -> None:
        self.assert_fails_open(
            self.drive(
                "brainskit-gate",
                self.hook_payload(self.DENY_TARGET),
                env={"FAKE_BK_MODE": "silent_two"},
            ),
            "exited 2",
        )

    def test_an_unexpected_exit_code(self) -> None:
        self.assert_fails_open(
            self.drive(
                "brainskit-gate",
                self.hook_payload(self.DENY_TARGET),
                env={"FAKE_BK_MODE": "crash"},
            ),
            "exited 137",
        )


class DoctorGateProbeTest(ShellHookCase):
    """`bk doctor` must run the gate, not confirm that its file exists.

    Every other enforcement answer is "installed and registered". The hook
    fails open by design in half a dozen ways, so those two claims come apart
    silently: `bk status` keeps reporting the layer active while a write to
    `wiki/` sails straight through. These pin the difference.
    """

    def doctor(self, *, path: str | None = None) -> dict[str, Any]:
        environment = dict(os.environ)
        environment["PATH"] = (
            path if path is not None else f"{self.bin}{os.pathsep}{os.environ['PATH']}"
        )
        # Pin the grammars so `healthy` reflects the probe and nothing else.
        with patch.dict(os.environ, environment, clear=True), patch.object(
            cli, "grammar_inventory", return_value={"python": True}
        ):
            return cli._doctor(self.service)

    def probe(self, **kwargs: Any) -> dict[str, Any]:
        report = self.doctor(**kwargs)["enforcement"]["write_gate_probe"]
        assert isinstance(report, dict)
        return report

    def test_a_working_gate_is_reported_as_enforcing(self) -> None:
        report = self.probe()
        self.assertEqual(report["state"], "enforcing")
        self.assertTrue(report["denies_a_gated_write"])
        self.assertTrue(report["allows_an_ordinary_write"])
        self.assertTrue(self.doctor()["healthy"])

    def test_a_gate_that_cannot_reach_bk_is_caught(self) -> None:
        """The field failure: registered, active in `status`, guarding nothing."""
        layers = {
            layer["layer"]: layer
            for layer in self.service.status()["enforcement"]["layers"]
        }
        self.assertTrue(layers["write_gate"]["active"], "status should still say active")

        report = self.probe(path="/usr/bin:/bin")
        self.assertEqual(report["state"], "not_enforcing")
        self.assertFalse(report["denies_a_gated_write"])
        self.assertIn("not guarding", report["detail"])

    def test_the_failure_repeats_the_hook_s_own_explanation(self) -> None:
        """The script names the missing piece better than an exit code can."""
        self.assertIn("bk is not on PATH", self.probe(path="/usr/bin:/bin")["hook_said"])

    def test_a_gate_that_cannot_reach_bk_makes_the_install_unhealthy(self) -> None:
        self.assertFalse(self.doctor(path="/usr/bin:/bin")["healthy"])

    def test_a_gate_that_denies_everything_is_reported_too(self) -> None:
        """Broken in the direction that stops work is still broken."""
        script = self.script("brainskit-gate")
        script.write_text("#!/bin/sh\nexit 2\n", encoding="utf-8")
        script.chmod(0o755)
        report = self.probe()
        self.assertEqual(report["state"], "over_blocking")
        self.assertFalse(report["allows_an_ordinary_write"])
        self.assertFalse(self.doctor()["healthy"])

    def test_a_hook_that_cannot_be_executed_is_not_trusted(self) -> None:
        """A lost executable bit disables a hook without changing a byte of it."""
        self.script("brainskit-gate").chmod(0o644)
        self.assertEqual(self.probe()["state"], "unknown")
        self.assertFalse(self.doctor()["healthy"])

    def test_no_installed_gate_is_reported_without_failing_the_install(self) -> None:
        """A vault with no agent is a choice, not a fault."""
        self.script("brainskit-gate").unlink()
        self.assertEqual(self.probe()["state"], "absent")
        self.assertTrue(self.doctor()["healthy"])

    def test_the_probe_writes_nothing(self) -> None:
        """Both probes are decisions. Neither may leave a file behind."""
        self.doctor()
        stray = [
            str(path)
            for path in self.root.parent.rglob("*brainskit-doctor-probe*")
        ]
        self.assertEqual(stray, [])


class DoctorHeadlineSaysWhyTest(unittest.TestCase):
    """`bk doctor`'s headline has to mean what its `healthy` means.

    Twin of the `bk status` headline: `healthy` is "every grammar present *and*
    the gate probe landed on a state an installation can have", but the headline
    restated only the grammar half. A machine with every grammar installed and a
    gate failing open printed `0 language(s) cannot be parsed` under a red cross
    -- on the very run whose purpose is to exercise that gate.
    """

    def headline(self, **over: Any) -> str:
        value: dict[str, Any] = {
            "vault": "/tmp/v",
            "environment": {"label": "uv tool"},
            "code": {
                "grammars_known": 5,
                "grammars_installed": 5,
                "grammars_missing": [],
            },
            "enforcement": {"write_gate_probe": {"state": "enforcing"}},
            "healthy": False,
        }
        value.update(over)
        return cli._doctor_headline(value)

    def probe(self, state: str) -> dict[str, Any]:
        return {"enforcement": {"write_gate_probe": {"state": state}}}

    def test_a_complete_installation_says_so(self) -> None:
        """Control: the headline must still be reachable."""

        self.assertEqual("installation complete", self.headline(healthy=True))

    def test_a_gate_that_fails_open_is_named_instead_of_a_zero(self) -> None:
        headline = self.headline(**self.probe("not_enforcing"))
        self.assertIn("not_enforcing", headline)
        self.assertNotIn("0 language(s)", headline)

    def test_a_gate_that_blocks_everything_is_named_too(self) -> None:
        """Over-blocking is a fault in the other direction, and just as quiet."""

        self.assertIn("over_blocking", self.headline(**self.probe("over_blocking")))

    def test_missing_grammars_are_still_counted(self) -> None:
        """Control: the case the old headline got right must keep working."""

        self.assertEqual(
            "2 language(s) cannot be parsed",
            self.headline(
                code={
                    "grammars_missing": ["go", "rust"],
                    "grammars_known": 5,
                    "grammars_installed": 3,
                }
            ),
        )

    def test_an_absent_gate_is_not_a_fault(self) -> None:
        """A vault with no agent installed is a choice, not a broken machine --
        the same allowlist `_doctor` builds `healthy` from."""

        self.assertNotIn("write gate", self.headline(**self.probe("absent")))

    def test_an_unrecognised_reason_points_at_the_sections(self) -> None:
        self.assertIn("see the sections below", self.headline())


class StatusScriptTest(ShellHookCase):
    """The SessionStart hook says what the session walked into, and never blocks."""

    def real_bk(self) -> Path:
        """A `bk` that runs this checkout, so the summary is built from real output."""
        directory = self.root / "real-bin"
        directory.mkdir()
        shim = directory / "bk"
        shim.write_text(
            f'#!/bin/sh\nexec {sys.executable} -m brainskit "$@"\n', encoding="utf-8"
        )
        shim.chmod(0o755)
        return directory

    def test_it_emits_the_summary_itself_not_a_path_to_it(self) -> None:
        done = self.drive("brainskit-status", path=f"{self.real_bk()}{os.pathsep}{os.environ['PATH']}")
        self.assertEqual(done.returncode, 0)
        self.assertIn("brainskit vault", done.stdout)
        for expected in ("sources", "wiki", "freshness", "proposals pending", "lint"):
            with self.subTest(row=expected):
                self.assertIn(expected, done.stdout)
        # Counts and rows, not page bodies, and never a bare filename.
        self.assertLess(len(done.stdout.splitlines()), 20)

    def test_it_names_the_enforcement_layers_that_are_off(self) -> None:
        done = self.drive("brainskit-status", path=f"{self.real_bk()}{os.pathsep}{os.environ['PATH']}")
        self.assertIn("write gate active", done.stdout)
        self.assertIn("commit lint OFF", done.stdout)
        self.assertIn("not a git repository", done.stdout)

    def test_a_vault_it_cannot_reach_is_announced_rather_than_ignored(self) -> None:
        # The failure this guards against: a hook that exits 0 in silence stays
        # dead for weeks because nothing ever reports that it did nothing.
        script = self.script("brainskit-status")
        stranded = Path(self.temporary.name).parent / f"{self.root.name}-status.sh"
        shutil.copyfile(script, stranded)
        stranded.chmod(0o755)
        moved = self.root.parent / f"{self.root.name}-moved"
        self.root.rename(moved)
        try:
            done = subprocess.run(
                [str(stranded)], capture_output=True, text=True, timeout=60
            )
        finally:
            moved.rename(self.root)
            stranded.unlink()
        self.assertEqual(done.returncode, 0)
        self.assertEqual(done.stdout, "")
        self.assertIn("is unreachable", done.stderr)

    def test_a_vault_bk_refuses_to_open_is_announced_with_the_reason(self) -> None:
        (self.root / ".brain" / "config.json").unlink()
        done = self.drive(
            "brainskit-status", path=f"{self.real_bk()}{os.pathsep}{os.environ['PATH']}"
        )
        self.assertEqual(done.returncode, 0)
        self.assertIn("bk status exited", done.stderr)
        self.assertIn("Not a brainskit vault", done.stderr)

    def test_a_missing_bk_is_announced(self) -> None:
        bare = self.root / "status-bare-bin"
        bare.mkdir()
        self.link(bare, "python3", "cat", "printf", "dirname", "sed")
        done = self.drive("brainskit-status", path=str(bare))
        self.assertEqual(done.returncode, 0)
        self.assertIn("bk is not on PATH", done.stderr)

    def test_a_missing_python3_is_announced(self) -> None:
        bare = self.root / "status-nopy-bin"
        bare.mkdir()
        os.symlink(self.bin / "bk", bare / "bk")
        self.link(bare, "cat", "printf", "dirname", "sed")
        done = self.drive("brainskit-status", path=str(bare))
        self.assertEqual(done.returncode, 0)
        self.assertIn("python3 is not available", done.stderr)


class PackagingTest(unittest.TestCase):
    """A template missing from package-data works from source and breaks in the wheel."""

    def test_the_hook_scripts_resolve_through_importlib_resources(self) -> None:
        for name in ("brainskit-gate", "brainskit-status"):
            with self.subTest(script=name):
                self.assertIn(HOOK_SENTINEL, installer._hook_script(name, Path("/tmp")))

    def test_shell_templates_are_declared_as_package_data(self) -> None:
        import tomllib

        with (REPO_ROOT / "pyproject.toml").open("rb") as handle:
            declared = tomllib.load(handle)["tool"]["setuptools"]["package-data"]
        self.assertIn("templates/agents/*.sh", declared["brainskit"])

    def test_every_shipped_hook_template_has_a_declaration(self) -> None:
        templates = REPO_ROOT / "src" / "brainskit" / "templates" / "agents"
        shipped = {path.name for path in templates.glob("*.sh")}
        expected = {f"{hook.template}.sh" for hook in installer.CLAUDE_HOOKS}
        self.assertEqual(shipped, expected)


class NestedVaultWorkspaceTest(unittest.TestCase):
    """The vault an agent guards is usually not the project the agent opened.

    Installing agent configuration into a nested vault is the worst failure
    this installer has: every file lands, the summary reads like success, and
    Claude Code loads none of it because it reads `.claude/` from the project
    root. These cover the separation and, just as importantly, that a vault
    which really is its own workspace keeps working untouched.
    """

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.project = Path(self.temporary.name).resolve()
        (self.project / ".claude").mkdir()
        self.vault = FileVault.initialize(self.project / "docs" / "brain", policy())
        self.service = BrainskitService(
            self.vault,
            SqliteFtsIndex(self.vault.index_path),
            graph=MarkdownGraph(),
        )
        self.root = self.vault.root

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def install(self, **kwargs: Any) -> dict[str, Any]:
        with redirect_stderr(StringIO()):
            return cli._install_hooks(self.service, "claude", **kwargs)

    def test_root_puts_agent_configuration_in_the_project(self) -> None:
        result = self.install(root=str(self.project))
        self.assertEqual(result["workspace"], str(self.project))
        for path in (
            self.project / ".claude" / "settings.json",
            self.project / ".claude" / "hooks" / "brainskit-gate.sh",
            self.project / ".claude" / "skills" / "brainskit" / "SKILL.md",
            self.project / "CLAUDE.md",
        ):
            with self.subTest(path=path.name):
                self.assertTrue(path.is_file(), f"{path} was not written")

    def test_nothing_agent_facing_leaks_into_the_vault(self) -> None:
        self.install(root=str(self.project))
        self.assertFalse((self.root / ".claude").exists())
        self.assertFalse((self.root / "CLAUDE.md").exists())
        # The adapter is vault policy and belongs to the vault, not the project.
        self.assertTrue((self.root / ".brain" / "agent-claude.json").is_file())
        self.assertFalse((self.project / ".brain").exists())

    def test_the_hook_still_governs_the_vault_not_the_workspace(self) -> None:
        self.install(root=str(self.project))
        script = (
            self.project / ".claude" / "hooks" / "brainskit-gate.sh"
        ).read_text(encoding="utf-8")
        # Compare the whole assignment line: the vault path is *under* the
        # project path, so a substring check passes for the wrong value too.
        assigned = [
            line for line in script.splitlines() if line.startswith("VAULT=")
        ]
        self.assertEqual(assigned, [f"VAULT={shlex.quote(str(self.root))}"])

    def test_the_status_hook_looks_for_git_in_the_workspace(self) -> None:
        """The commit-lint layer lives in the workspace's repository.

        This shipped broken: the script checked `$VAULT/.git`, so a vault
        nested in a git project reported commit lint OFF while the pre-commit
        hook was installed and running. An enforcement report that says OFF
        for a live layer is the same failure as one that says ON for a dead
        one — it is trusted, and it is wrong.
        """
        self.install(root=str(self.project))
        script = (
            self.project / ".claude" / "hooks" / "brainskit-status.sh"
        ).read_text(encoding="utf-8")
        assigned = {
            line.split("=", 1)[0]: line.split("=", 1)[1]
            for line in script.splitlines()
            if line.startswith(("VAULT=", "WORKSPACE="))
        }
        self.assertEqual(assigned["VAULT"], shlex.quote(str(self.root)))
        # The hook no longer locates the workspace or inspects git itself. It
        # used to, and answered "commit lint active" for a Husky repository and
        # for hooks that nothing registers -- the two cases `bk status` had
        # already learned to catch. The workspace question is now asked once, of
        # `bk status`, and `NestedVaultEnforcementReportingTest` pins that it
        # names the enclosing repository rather than the vault.
        self.assertNotIn("WORKSPACE", assigned)
        self.assertNotIn(".git/hooks/pre-commit", script)
        self.assertNotIn('"$VAULT/.git"', script)

    def test_no_template_placeholder_survives_rendering(self) -> None:
        """An unsubstituted {{token}} is a silent hole in a shipped script."""
        self.install(root=str(self.project))
        for name in ("brainskit-gate", "brainskit-status"):
            with self.subTest(script=name):
                body = (
                    self.project / ".claude" / "hooks" / f"{name}.sh"
                ).read_text(encoding="utf-8")
                self.assertNotIn("{{", body)

    def test_status_finds_the_layers_where_they_were_installed(self) -> None:
        """The half of the bug that survives a correct install.

        `bk status` used to look beside the vault unconditionally, so a
        correctly installed gate still reported every layer off.
        """
        self.install(root=str(self.project))
        layers = {
            layer["layer"]: layer
            for layer in self.service.status()["enforcement"]["layers"]
        }
        self.assertTrue(layers["write_gate"]["active"], layers["write_gate"])
        self.assertTrue(layers["session_status"]["active"])
        self.assertTrue(layers["instructions"]["active"])

    def test_a_nested_vault_without_root_is_reported_not_silently_wrong(self) -> None:
        buffer = StringIO()
        with redirect_stderr(buffer):
            result = cli._install_hooks(self.service, "claude")
        advisory = result["workspace_advisory"]
        self.assertIn(str(self.project), advisory["reason"])
        self.assertIn(f"--root {self.project}", advisory["hint"])
        self.assertIn("WORKSPACE", buffer.getvalue())

    def test_an_explicit_root_never_triggers_the_advisory(self) -> None:
        result = self.install(root=str(self.project))
        self.assertNotIn("workspace_advisory", result)

    def test_a_root_that_is_not_a_directory_is_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            self.install(root=str(self.project / "nope"))

    def test_pre_commit_lands_in_the_workspace_repository(self) -> None:
        subprocess.run(
            ["git", "init", "--quiet"], cwd=self.project, check=True
        )
        self.install(root=str(self.project))
        hook = self.project / ".git" / "hooks" / "pre-commit"
        self.assertTrue(hook.is_file())
        # It must lint the vault, which is not the repository it lives in.
        self.assertIn(json.dumps(str(self.root)), hook.read_text(encoding="utf-8"))


class RedirectedHooksPathInstallTest(VaultCase):
    """A repository whose `core.hooksPath` moved must not be told a hook landed.

    Husky sets `core.hooksPath` on every install, and git then never reads
    `.git/hooks/pre-commit` again. Writing there would leave a file that looks
    installed, reports active, and never runs -- the artefact-versus-execution
    gap this installer's own enforcement summary exists to close.
    """

    def setUp(self) -> None:
        super().setUp()
        subprocess.run(["git", "init", "--quiet"], cwd=self.root, check=True)

    def redirect(self, relative: str = ".husky/_") -> None:
        subprocess.run(
            ["git", "config", "core.hooksPath", relative], cwd=self.root, check=True
        )

    def hook(self) -> Path:
        return self.root / ".git" / "hooks" / "pre-commit"

    def commit_layer(self, result: dict[str, Any]) -> dict[str, Any]:
        for entry in result["enforcement"]["layers"]:
            if entry["layer"] == "commit_lint":
                return entry
        raise AssertionError("no commit_lint layer reported")

    def test_a_default_repository_still_installs_the_hook(self) -> None:
        """The control: without the setting, this path is untouched."""
        result = self.install()
        self.assertEqual(result["pre_commit"]["state"], "created")
        self.assertTrue(self.hook().is_file())
        self.assertTrue(self.commit_layer(result)["active"])

    def test_a_redirected_hooks_path_writes_no_hook_at_all(self) -> None:
        self.redirect()
        result = self.install()
        self.assertEqual(result["pre_commit"]["state"], "skipped")
        self.assertFalse(
            self.hook().exists(),
            "wrote a hook into a directory git does not read",
        )

    def test_the_refusal_names_the_directory_and_how_to_wire_it(self) -> None:
        """"Skipped" alone leaves the operator with no way to restore the layer."""
        self.redirect()
        pre_commit = self.install()["pre_commit"]
        self.assertIn("core.hooksPath", pre_commit["reason"])
        self.assertIn(".husky/_", pre_commit["reason"])
        self.assertIn(str(self.root / ".husky" / "_" / "pre-commit"), pre_commit["hint"])
        self.assertIn("lint --changed", pre_commit["hint"])
        self.assertEqual(pre_commit["enforcement"], "off")

    def test_the_enforcement_summary_reports_the_layer_off(self) -> None:
        self.redirect()
        result = self.install()
        entry = self.commit_layer(result)
        self.assertFalse(entry["active"])
        self.assertIn("core.hooksPath", entry["reason"])

    def test_force_cannot_make_git_read_a_directory_it_does_not(self) -> None:
        """`--force` decides whether to clobber a hook, not which directory runs."""
        self.redirect()
        result = self.install(force=True)
        self.assertEqual(result["pre_commit"]["state"], "skipped")
        self.assertFalse(self.hook().exists())

    def test_the_other_layers_still_install(self) -> None:
        """One dead layer must not take the write gate down with it."""
        self.redirect()
        result = self.install()
        active = {
            entry["layer"]
            for entry in result["enforcement"]["layers"]
            if entry["active"]
        }
        self.assertIn("write_gate", active)
        self.assertIn("session_status", active)


class StandaloneVaultWorkspaceTest(VaultCase):
    """A vault that is its own project must behave exactly as it always did."""

    def test_the_default_workspace_is_still_the_vault(self) -> None:
        result = self.install()
        self.assertEqual(result["workspace"], str(self.root))
        self.assertTrue((self.root / ".claude" / "settings.json").is_file())

    def test_a_git_vault_raises_no_workspace_advisory(self) -> None:
        subprocess.run(["git", "init", "--quiet"], cwd=self.root, check=True)
        self.assertNotIn("workspace_advisory", self.install())

    def test_an_adapter_without_a_workspace_falls_back_to_the_vault(self) -> None:
        """Vaults installed before the field existed keep reporting correctly."""
        self.install()
        adapter = self.root / ".brain" / "agent-claude.json"
        stored = json.loads(adapter.read_text(encoding="utf-8"))
        del stored["workspace"]
        adapter.write_text(json.dumps(stored), encoding="utf-8")
        layers = {
            layer["layer"]: layer
            for layer in self.service.status()["enforcement"]["layers"]
        }
        self.assertTrue(layers["write_gate"]["active"])


if __name__ == "__main__":
    unittest.main()
