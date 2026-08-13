"""The generated SessionStart hook must report what `bk status` reports.

`bk status` was fixed to detect both failure modes -- a `core.hooksPath`
redirection that means git never reads `.git/hooks`, and a script sitting on
disk that nothing registers. The generated shell then threw that answer away
and recomputed it as `[ -x gate.sh ]` and `[ -f .git/hooks/pre-commit ]`, so it
announced `write gate active - commit lint active` in exactly the two cases the
Python had learned to catch.

This is the block an agent reads at the start of every session, which makes it
the highest-leverage place in the product for a false claim to live.

The hook already fetches the full status document, and that document already
carries `enforcement.layers[]` with `active`, `detail` and `script`. The fix is
deletion, so these tests pin the direction: the shell must render the field it
is holding, never re-derive it.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

TEMPLATE = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "brainskit"
    / "templates"
    / "agents"
    / "brainskit-status.sh"
)

BASE_STATUS: dict = {
    "vault": "/vault",
    "sources": 3,
    "pending": 0,
    "wiki_pages": 2,
    "index": {"documents": 5},
    "freshness": {"fresh": 2, "review": 0, "stale": 0, "unknown": 0},
}


def layers(**active: bool) -> list[dict]:
    """`enforcement.layers[]` as `bk status --json` emits it."""

    details = {
        "write_gate": "brainskit-gate.sh is not registered under PreToolUse",
        "session_status": "brainskit-status.sh is not registered under SessionStart",
        "commit_lint": "git runs hooks from .husky/_, not .git/hooks",
    }
    return [
        {
            "layer": name,
            "mechanism": name,
            "active": active[name],
            **({} if active[name] else {"detail": details[name]}),
        }
        for name in ("write_gate", "session_status", "commit_lint")
    ] + [{"layer": "instructions", "mechanism": "CLAUDE.md", "active": True,
          "advisory": True}]


class SessionStatusHookTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.vault = self.root / "vault"
        (self.vault / ".brain").mkdir(parents=True)
        self.hooks = self.root / ".claude" / "hooks"
        self.hooks.mkdir(parents=True)
        # The workspace looks fully equipped: a sibling gate script that is
        # executable, and a git pre-commit hook on disk. The shipped shell reads
        # exactly these two facts, so this is the fixture that separates
        # "the file exists" from "the layer runs".
        (self.hooks / "brainskit-gate.sh").write_text("#!/bin/sh\nexit 0\n")
        (self.hooks / "brainskit-gate.sh").chmod(0o755)
        git_hooks = self.root / ".git" / "hooks"
        git_hooks.mkdir(parents=True)
        (git_hooks / "pre-commit").write_text("#!/bin/sh\nexit 0\n")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def install_fake_bk(self, status: dict) -> Path:
        """A `bk` that answers only what the hook asks, from a canned document."""

        bindir = self.root / "bin"
        bindir.mkdir(exist_ok=True)
        shim = bindir / "bk"
        shim.write_text(
            "#!/usr/bin/env python3\n"
            "import json, sys\n"
            "args = sys.argv[1:]\n"
            f"status = {json.dumps(status)!r}\n"
            "if 'code' in args:\n"
            "    out = {'ok': True, 'result': {'state': 'fresh', 'files': 1}}\n"
            "elif 'proposals' in args:\n"
            "    out = {'ok': True, 'result': {'count': 0}}\n"
            "elif 'lint' in args:\n"
            "    out = {'ok': True, 'result': {'findings': []}}\n"
            "else:\n"
            "    out = {'ok': True, 'result': json.loads(status)}\n"
            "print(json.dumps(out))\n"
        )
        shim.chmod(0o755)
        return bindir

    def run_hook(self, status: dict) -> str:
        script = self.hooks / "brainskit-status.sh"
        rendered = (
            TEMPLATE.read_text(encoding="utf-8")
            .replace("{{vault}}", f'"{self.vault}"')
            .replace("{{workspace}}", f'"{self.root}"')
        )
        script.write_text(rendered, encoding="utf-8")
        script.chmod(0o755)
        bindir = self.install_fake_bk(status)
        completed = subprocess.run(
            [str(script)],
            capture_output=True,
            text=True,
            env={
                **os.environ,
                "PATH": f"{bindir}:{os.environ['PATH']}",
            },
            check=False,
        )
        self.assertEqual(completed.returncode, 0, msg=completed.stderr)
        return completed.stdout

    def enforcement_line(self, status: dict) -> str:
        for line in self.run_hook(status).splitlines():
            if "enforcement" in line:
                return line
        raise AssertionError("the hook printed no enforcement line")

    # ------------------------------------------------------------------ cases

    def test_all_layers_active_is_reported_as_active(self) -> None:
        """Control. Without it, 'never say active' would pass everything else."""

        line = self.enforcement_line(
            {**BASE_STATUS, "enforcement": {"layers": layers(
                write_gate=True, session_status=True, commit_lint=True)}}
        )
        self.assertIn("write gate active", line)
        self.assertIn("commit lint active", line)
        self.assertNotIn("OFF", line)

    def test_a_redirected_hooks_path_is_not_reported_as_active(self) -> None:
        """Case A: Husky. `.git/hooks/pre-commit` exists and git never reads it."""

        line = self.enforcement_line(
            {**BASE_STATUS, "enforcement": {"layers": layers(
                write_gate=True, session_status=True, commit_lint=False)}}
        )
        self.assertNotIn("commit lint active", line)

    def test_an_unregistered_gate_is_not_reported_as_active(self) -> None:
        """Case B: scripts on disk, nothing registers them in settings.json."""

        line = self.enforcement_line(
            {**BASE_STATUS, "enforcement": {"layers": layers(
                write_gate=False, session_status=False, commit_lint=True)}}
        )
        self.assertNotIn("write gate active", line)

    def test_an_inactive_layer_repeats_the_reason_status_gave(self) -> None:
        """The Python already phrased it correctly; do not re-word it."""

        line = self.enforcement_line(
            {**BASE_STATUS, "enforcement": {"layers": layers(
                write_gate=True, session_status=True, commit_lint=False)}}
        )
        self.assertIn(".husky/_", line)

    def test_the_shell_does_not_recompute_enforcement(self) -> None:
        """Structural: no file test may decide an enforcement claim."""

        source = TEMPLATE.read_text(encoding="utf-8")
        for probe in ('[ -x "$HOOK_DIR', '.git/hooks/pre-commit'):
            self.assertNotIn(
                probe,
                source,
                msg="the hook re-derives a layer it already has in $STATUS_JSON",
            )

    def test_a_status_document_without_enforcement_says_so(self) -> None:
        """An older bk that reports no layers must not claim they are on."""

        line = self.enforcement_line({**BASE_STATUS})
        self.assertNotIn("active", line)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
