#!/usr/bin/env bash
# Track 2 of the four-track review, as a release gate: install the BUILT wheel,
# wire a vault into Claude Code, then break each enforcement layer in turn --
# every break in its own throwaway vault -- and require `bk status --json` and
# `bk doctor --json` to report it. A reporting surface that has only ever been
# read against a healthy install has not been shown to report anything.
#
# Every expectation below is one the test suite already pins against the
# source tree (tests/test_hooks_install.py, tests/test_enforcement_status.py).
# What this adds is the artifact: the hook scripts, the installer's renderer
# and the probes as they ship in the wheel, run through a real `sh`, git and
# PATH rather than the suite's patches.
#
#   ./scripts/enforcement-harness.sh [WHEEL]    # default: dist/brainskit-<version>-*.whl
#
# Run ./scripts/verify-wheel.sh first; it builds the wheel this installs.
# BK_BIN_DIR=<dir> skips the install and drives the `bk` already in <dir>. That
# is for shaking out the script itself; it proves nothing about a wheel.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

WORK="$(cd "$(mktemp -d)" && pwd -P)"
trap 'rm -rf "$WORK"' EXIT

VERSION="$(python3 -c '
import tomllib
with open("pyproject.toml", "rb") as handle:
    print(tomllib.load(handle)["project"]["version"])
')"

if [ -n "${BK_BIN_DIR:-}" ]; then
    BIN="$(cd "$BK_BIN_DIR" && pwd -P)"
    echo "==> DRY RUN: driving $BIN/bk, not an installed wheel"
else
    WHEEL="${1:-}"
    if [ -z "$WHEEL" ]; then
        shopt -s nullglob
        wheels=(dist/brainskit-"$VERSION"-*.whl)
        shopt -u nullglob
        if [ "${#wheels[@]}" -ne 1 ]; then
            echo "expected one dist/brainskit-$VERSION-*.whl, found ${#wheels[@]};"
            echo "run ./scripts/verify-wheel.sh first, or pass the wheel's path"
            exit 1
        fi
        WHEEL="${wheels[0]}"
    fi
    [ -f "$WHEEL" ] || { echo "no such wheel: $WHEEL"; exit 1; }
    echo "==> uv tool install $WHEEL"
    BIN="$WORK/bin"
    UV_TOOL_DIR="$WORK/tools" UV_TOOL_BIN_DIR="$BIN" \
        uv tool install --quiet --python "$(cat .python-version)" "$WHEEL"
fi

export HOME="$WORK/home"
export XDG_CONFIG_HOME="$HOME/.config"
export PATH="$BIN:$PATH"
mkdir -p "$XDG_CONFIG_HOME"
BK="$BIN/bk"

if [ "$(command -v bk)" != "$BK" ]; then
    echo "bk resolves to $(command -v bk), not the one under test in $BIN"
    exit 1
fi

"$BK" init --print-config >"$WORK/policy.json"

# A standalone vault, its own git repository, wired into Claude Code. The code
# graph is skipped: this harness is about the enforcement layers, and a default
# install has no grammars to build one with.
VAULT=""
fresh_vault() {
    VAULT="$WORK/$1/vault"
    mkdir -p "$WORK/$1"
    "$BK" init "$VAULT" --config "$WORK/policy.json" --json >/dev/null
    git -c init.defaultBranch=main init --quiet "$VAULT"
    "$BK" --vault "$VAULT" hooks install --agent claude --skip-code-build --json \
        >"$WORK/$1/install.json" 2>"$WORK/$1/install.err"
}

REPORT=""
report() {
    local label="$1"
    shift
    local rc=0
    REPORT="$("$@")" || rc=$?
    # Unhealthy is a finding, not an error: both commands still answer, so
    # anything but a JSON envelope here is the harness breaking, not the layer.
    if [ "$rc" -gt 1 ]; then
        echo "    $label exited $rc: $REPORT"
        exit 1
    fi
    for marker in "Traceback" "Not a brainskit vault" "unhandled internal error"; do
        if grep -qF -- "$marker" <<<"$REPORT"; then
            echo "    $label: $marker"
            echo "$REPORT"
            exit 1
        fi
    done
}

# Asserts on one JSON document: each CHECK is `dotted.path=<json>` or
# `dotted.path~<substring>`. A path segment that meets a list of enforcement
# rows selects the row whose `layer` is that name.
expect() {
    local label="$1"
    shift
    python3 - "$label" "$REPORT" "$@" <<'PY'
import json
import sys

label, raw, checks = sys.argv[1], sys.argv[2], sys.argv[3:]
document = json.loads(raw)
failures = []
for check in checks:
    contains = "~" in check and ("=" not in check or check.index("~") < check.index("="))
    path, want = check.split("~" if contains else "=", 1)
    node = document
    try:
        for segment in path.split("."):
            if isinstance(node, list):
                node = next(row for row in node if row.get("layer") == segment)
            else:
                node = node[segment]
    except (KeyError, StopIteration, TypeError):
        failures.append(f"{path} is absent")
        continue
    if contains:
        if want not in str(node):
            failures.append(f"{path} is {node!r}, expected it to contain {want!r}")
    elif node != json.loads(want):
        failures.append(f"{path} is {json.dumps(node)}, expected {want}")
if failures:
    print(f"    FAIL {label}:")
    for failure in failures:
        print(f"      {failure}")
    print("      " + json.dumps(document, indent=2).replace("\n", "\n      "))
    sys.exit(1)
print(f"    ok: {label}")
PY
}

status_expect() {
    local label="$1"
    shift
    report "status" "$BK" --vault "$VAULT" status --json
    expect "status: $label" "$@"
}

doctor_expect() {
    local label="$1"
    shift
    report "doctor" "$BK" --vault "$VAULT" doctor --json
    expect "doctor: $label" "$@"
}

gate_script() { printf '%s' "$VAULT/.claude/hooks/brainskit-gate.sh"; }
status_script() { printf '%s' "$VAULT/.claude/hooks/brainskit-status.sh"; }
pre_commit() { printf '%s' "$VAULT/.git/hooks/pre-commit"; }

# The control. Without it every "reports the break" below could pass on an
# install that reports everything broken all the time.
echo "==> intact install"
fresh_vault intact
status_expect "every layer active, gated, healthy" \
    'result.healthy=true' 'result.enforcement.gated=true' \
    'result.enforcement.inactive=[]' 'result.enforcement.outdated=[]' \
    'result.enforcement.layers.write_gate.active=true' \
    'result.enforcement.layers.session_status.active=true' \
    'result.enforcement.layers.commit_lint.active=true'
doctor_expect "both probes enforce" \
    'result.healthy=true' 'result.enforcement.gated=true' \
    'result.enforcement.write_gate_probe.state="enforcing"' \
    'result.enforcement.write_gate_probe.exercised="registered_command"' \
    'result.enforcement.commit_lint_probe.state="enforcing"'

echo "==> gate script deleted, registration left in settings.json"
fresh_vault gate-deleted
rm "$(gate_script)"
status_expect "the write gate is not active" \
    'result.healthy=false' 'result.enforcement.gated=false' \
    'result.enforcement.layers.write_gate.active=false' \
    'result.enforcement.layers.write_gate.detail~every write goes through'
doctor_expect "the registered command lets writes through" \
    'result.healthy=false' 'result.enforcement.gated=false' \
    'result.enforcement.write_gate_probe.state="not_enforcing"' \
    'result.enforcement.write_gate_probe.denies_a_gated_write=false' \
    'result.enforcement.write_gate_probe.hook_said~No such file'

echo "==> gate unregistered, script left on disk"
fresh_vault gate-unregistered
python3 - "$VAULT/.claude/settings.json" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    settings = json.load(handle)
settings["hooks"]["PreToolUse"] = []
with open(sys.argv[1], "w", encoding="utf-8") as handle:
    json.dump(settings, handle)
PY
status_expect "nothing registers the gate" \
    'result.healthy=false' 'result.enforcement.gated=false' \
    'result.enforcement.layers.write_gate.active=false'
# The script itself still refuses; that is not the question -- Claude Code
# never runs it, so the install is ungated and unhealthy.
doctor_expect "the script refuses, but is exercised directly, not as registered" \
    'result.healthy=false' 'result.enforcement.gated=false' \
    'result.enforcement.write_gate_probe.exercised="script"' \
    'result.enforcement.write_gate_probe.state="enforcing"'

echo "==> gate script without its executable bit"
fresh_vault gate-not-executable
chmod 0644 "$(gate_script)"
doctor_expect "sh -c answers 126, which is not a denial" \
    'result.healthy=false' \
    'result.enforcement.write_gate_probe.state="not_enforcing"' \
    'result.enforcement.write_gate_probe.hook_said~ermission denied'

echo "==> bk off PATH, as the hooks will see it"
fresh_vault bk-off-path
status_expect "status cannot see this without running anything" \
    'result.enforcement.gated=true' \
    'result.enforcement.layers.write_gate.active=true'
report "doctor" env PATH=/usr/bin:/bin "$BK" --vault "$VAULT" doctor --json
expect "doctor: the gate fails open and the pre-commit hook cannot run" \
    'result.healthy=false' \
    'result.enforcement.write_gate_probe.state="not_enforcing"' \
    'result.enforcement.write_gate_probe.hook_said~bk is not on PATH' \
    'result.enforcement.commit_lint_probe.state="not_enforcing"' \
    'result.enforcement.commit_lint_probe.exit=127'

echo "==> pre-commit hook linting another vault"
fresh_vault pre-commit-elsewhere
OTHER="$WORK/pre-commit-elsewhere/other"
"$BK" init "$OTHER" --config "$WORK/policy.json" --json >/dev/null
python3 - "$(pre_commit)" "$VAULT" "$OTHER" <<'PY'
import sys
from pathlib import Path

hook, vault, other = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
text = hook.read_text(encoding="utf-8")
if vault not in text:
    sys.exit(f"{hook} does not name {vault}; cannot redirect it")
hook.write_text(text.replace(vault, other), encoding="utf-8")
PY
status_expect "commit_lint is off; the gate is unaffected" \
    'result.healthy=false' 'result.enforcement.gated=true' \
    'result.enforcement.inactive=["commit_lint"]' \
    'result.enforcement.layers.commit_lint.detail~not this vault'
doctor_expect "the hook is reported without being run" \
    'result.healthy=false' \
    'result.enforcement.commit_lint_probe.state="not_enforcing"' \
    'result.enforcement.commit_lint_probe.detail~not this vault'

echo "==> core.hooksPath redirected away from .git/hooks"
fresh_vault hooks-path
mkdir -p "$VAULT/.githooks"
git -C "$VAULT" config core.hooksPath .githooks
status_expect "commit_lint is off: git never runs the installed hook" \
    'result.healthy=false' 'result.enforcement.gated=true' \
    'result.enforcement.inactive=["commit_lint"]' \
    'result.enforcement.layers.commit_lint.active=false' \
    'result.enforcement.layers.commit_lint.detail~.githooks'
doctor_expect "the stranded hook is not enforcing" \
    'result.healthy=false' \
    'result.enforcement.commit_lint_probe.state="not_enforcing"' \
    'result.enforcement.commit_lint_probe.hint~lint --changed'

echo "==> session-status script outdated"
fresh_vault status-outdated
printf '# from an older brainskit\n' >>"$(status_script)"
status_expect "warned, still active, health unaffected" \
    'result.healthy=true' 'result.enforcement.gated=true' \
    'result.enforcement.outdated=["session_status"]' \
    'result.enforcement.layers.session_status.outdated=true' \
    'result.enforcement.layers.session_status.active=true' \
    'result.enforcement.layers.session_status.hint="bk hooks install --agent claude"'
doctor_expect "the same warning, still healthy" \
    'result.healthy=true' 'result.enforcement.outdated=["session_status"]'

echo "==> gate script outdated"
fresh_vault gate-outdated
printf '# from an older brainskit\n' >>"$(gate_script)"
status_expect "an outdated gate is not a live gate" \
    'result.healthy=false' 'result.enforcement.gated=false' \
    'result.enforcement.outdated=["write_gate"]' \
    'result.enforcement.layers.write_gate.outdated=true' \
    'result.enforcement.layers.write_gate.active=false'
doctor_expect "the old copy still refuses; the install is still unhealthy" \
    'result.healthy=false' \
    'result.enforcement.write_gate_probe.state="enforcing"'

echo "==> brainskit $VERSION reports every enforcement break it was shown"
