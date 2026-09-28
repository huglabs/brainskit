#!/usr/bin/env bash
# Track 1 of the four-track review, as a release gate: install the BUILT wheel
# the way the README says, then follow docs/getting-started.md literally as a
# fresh user would. The 0.6.0 review ran this by hand and found a documented
# quickstart whose default outcome was a red ✗ (D1) -- behaviour no amount of
# source reading produced, and no test caught, because the suite never follows
# the docs.
#
# The steps are read out of the doc's ```bash fences at run time rather than
# copied here, so a doc that drifts from what `bk` does fails this gate instead
# of going on describing a product that no longer exists.
#
#   ./scripts/fresh-install-smoke.sh [WHEEL]    # default: dist/brainskit-<version>-*.whl
#   ./scripts/fresh-install-smoke.sh --print-steps
#
# Run ./scripts/verify-wheel.sh first; it builds the wheel this installs.
# BK_BIN_DIR=<dir> skips the install and drives the `bk` already in <dir>. That
# is for shaking out the script itself; it proves nothing about a wheel.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

GUIDE="docs/getting-started.md"

# What the guide leaves to other pages, run after its own steps. Each entry must
# appear verbatim in README.md or docs/ -- tests/test_version.py enforces that,
# so this list cannot drift into commands no reader was ever told to run.
FOLLOW_ON=(
    "bk --vault ./my-vault hooks install --agent claude"
)

doc_steps() {
    python3 - "$GUIDE" <<'PY'
import re
import sys

text = open(sys.argv[1], encoding="utf-8").read()
steps = [
    line.strip()
    for block in re.findall(r"^```bash\n(.*?)^```", text, flags=re.M | re.S)
    for line in block.splitlines()
    if line.strip().startswith("bk ")
]
if not steps:
    sys.exit(f"{sys.argv[1]} has no `bk` command in a ```bash fence to follow")
print("\n".join(steps))
PY
}

if [ "${1:-}" = "--print-steps" ]; then
    doc_steps
    printf '%s\n' "${FOLLOW_ON[@]}"
    exit 0
fi

STEPS="$(doc_steps)"
for needed in "bk init " "capture " "search "; do
    if ! grep -q -- "$needed" <<<"$STEPS"; then
        echo "$GUIDE no longer shows a \`$needed\` step; this gate follows the guide,"
        echo "so update the guide or this script's assertions together"
        exit 1
    fi
done

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

    # The README's own install line, `uv tool install <wheel>`, into a tool
    # directory nobody else uses. `uv` runs before HOME and XDG_CONFIG_HOME are
    # replaced, for the reason verify-wheel.sh gives: it reads its own uv.toml
    # from there, and the gate must install the way the operator's uv would.
    echo "==> uv tool install $WHEEL"
    BIN="$WORK/bin"
    UV_TOOL_DIR="$WORK/tools" UV_TOOL_BIN_DIR="$BIN" \
        uv tool install --quiet --python "$(cat .python-version)" "$WHEEL"
fi

# A fresh user: no brainskit config, no vault registry, no global git config,
# nothing from the runner's real home. `bk init` registers every vault it
# creates, and that registry must be this throwaway one.
export HOME="$WORK/home"
export XDG_CONFIG_HOME="$HOME/.config"
export PATH="$BIN:$PATH"
mkdir -p "$XDG_CONFIG_HOME"
USER_DIR="$WORK/user"
mkdir -p "$USER_DIR"

if [ "$(command -v bk)" != "$BIN/bk" ]; then
    echo "bk resolves to $(command -v bk), not the one under test in $BIN"
    exit 1
fi

# Markers of a failure that still exits cleanly, or of a command that never
# reached the vault it was pointed at. The same list `_harness.run_cli` refuses.
MARKERS=("Traceback" "Not a brainskit vault" "No brainskit vault found" "unhandled internal error")

STEP_OUT=""
run_step() {
    local line="$1" rc
    set +e
    (cd "$USER_DIR" && bash -c "$line") >"$WORK/out" 2>"$WORK/err"
    rc=$?
    set -e
    STEP_OUT="$(<"$WORK/out")"
    local err
    err="$(<"$WORK/err")"
    if [ "$rc" -ne 0 ]; then
        echo "    FAIL (exit $rc): $line"
        printf '%s\n%s\n' "$STEP_OUT" "$err" | sed 's/^/      /'
        exit 1
    fi
    local marker
    for marker in "${MARKERS[@]}"; do
        if grep -qF -- "$marker" <<<"$STEP_OUT$err"; then
            echo "    FAIL ($marker): $line"
            printf '%s\n%s\n' "$STEP_OUT" "$err" | sed 's/^/      /'
            exit 1
        fi
    done
    if [[ "$line" == *--json* && "$line" != *">"* ]]; then
        if ! python3 -c 'import json,sys; sys.exit(0 if json.loads(sys.argv[1]).get("ok") is True else 1)' "$STEP_OUT"; then
            echo "    FAIL (not {\"ok\": true}): $line"
            printf '%s\n' "$STEP_OUT" | sed 's/^/      /'
            exit 1
        fi
    fi
    echo "    ok: $line"
}

# Asserts on one JSON document: each CHECK is `dotted.path=<json>` or
# `dotted.path~<substring>`. A path segment that meets a list of enforcement
# rows selects the row whose `layer` is that name.
expect() {
    local label="$1"
    shift
    python3 - "$label" "$STEP_OUT" "$@" <<'PY'
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
    print("      " + raw.replace("\n", "\n      "))
    sys.exit(1)
print(f"    ok: {label}")
PY
}

echo "==> the README's first command"
run_step "bk --help"

# The guide's steps name a notes.md the reader already has. Its words are the
# guide's own search query, so the search step has something to find.
printf '# Notes\nretrieval memory, compiled once and cited forever.\n' >"$USER_DIR/notes.md"

echo "==> $GUIDE, followed literally"
while IFS= read -r line; do
    run_step "$line"
done <<<"$STEPS"

echo "==> onward, from the pages the guide links to"
# docs/agents.md: the pre-commit layer is installed "when the workspace is a git
# repository". A standalone vault is its own workspace, so it is one.
git -c init.defaultBranch=main init --quiet "$USER_DIR/my-vault"
for line in "${FOLLOW_ON[@]}"; do
    run_step "$line"
    for gap in "ENFORCEMENT GAP" "WORKSPACE"; do
        if grep -qF "$gap" "$WORK/err"; then
            echo "    FAIL: \`$line\` warned $gap on the documented path"
            sed 's/^/      /' "$WORK/err"
            exit 1
        fi
    done
done

echo "==> what the reader is told to expect"
run_step "bk --vault ./my-vault search \"retrieval memory\" --consumer local --json"
expect "search finds the captured note" \
    'result.count=1'

run_step "bk --vault ./my-vault lint --json"
expect "lint is clean, in both envelopes" \
    'ok=true' 'result.ok=true'

run_step "bk --vault ./my-vault status --json"
expect "status: healthy, gated, every layer active" \
    'result.healthy=true' 'result.lint_errors=0' \
    'result.enforcement.gated=true' 'result.enforcement.inactive=[]' \
    'result.enforcement.outdated=[]'

run_step "bk --vault ./my-vault doctor --json"
expect "doctor: both probes enforce, install healthy" \
    'result.healthy=true' \
    'result.enforcement.write_gate_probe.state="enforcing"' \
    'result.enforcement.write_gate_probe.exercised="registered_command"' \
    'result.enforcement.commit_lint_probe.state="enforcing"'

# The README shows this headline for a healthy vault. D1 was the documented
# quickstart printing `✗ 0 lint error(s)` here instead.
run_step "bk --vault ./my-vault status"
headline="${STEP_OUT%%$'\n'*}"
if [[ "$headline" != *"vault healthy"* ]]; then
    echo "    FAIL: status headline is \"$headline\", the README promises \"✓ vault healthy\""
    exit 1
fi
echo "    ok: headline \"$headline\""

echo "==> a fresh install of brainskit $VERSION follows its own getting-started guide"
