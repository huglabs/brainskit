#!/bin/sh
# brainskit:generated — written by `bk hooks install --agent claude`.
#
# Claude Code PreToolUse hook for Write|Edit|MultiEdit. It reads the hook
# payload on stdin and asks the brainskit CLI whether a direct write to that
# path is permitted, so the engine's central claim — only `bk apply` writes the
# wiki — is enforced in the write path instead of detected afterwards.
#
#   exit 2  the write is denied; stderr is what reaches the model
#   exit 0  everything else, including every way this script can break
#
# Failing open is deliberate. A knowledge gate that blocks the session on its
# own breakage is worse than no gate, and every write this script lets through
# is still reported afterwards: one under `wiki/` as `wiki.outside_apply` -- for
# every page, including the two `bk init` seeds, which lint used to skip -- and
# one under `raw/` as `registry.untracked_file` or `raw.content_modified`. That
# backstop is the whole reason failing open is tolerable, so it has to hold for
# everything the gate covers rather than for most of it.
# Only a decision that says `"allowed": false` may exit 2:
# `bk` uses exit 2 for its ordinary errors too, and argparse uses it for an
# unknown subcommand, so the exit code alone is never proof of a denial.

set -u

VAULT={{vault}}

note() {
    printf 'brainskit-gate: %s\n' "$*" >&2
}

if command -v python3 >/dev/null 2>&1; then
    PARSER=python3
elif command -v jq >/dev/null 2>&1; then
    PARSER=jq
else
    note 'neither python3 nor jq is available; allowing the write (gate not enforced)'
    exit 0
fi

# Reads the hook payload on stdin, writes `tool_input.file_path` on stdout.
# Non-zero means there is no usable path, whatever the reason.
hook_file_path() {
    case "$PARSER" in
    python3)
        python3 -c '
import json
import sys

try:
    payload = json.load(sys.stdin)
except Exception:
    raise SystemExit(3)
if not isinstance(payload, dict):
    raise SystemExit(3)
tool_input = payload.get("tool_input")
if not isinstance(tool_input, dict):
    raise SystemExit(4)
target = tool_input.get("file_path") or tool_input.get("filePath")
if not isinstance(target, str) or not target:
    raise SystemExit(4)
sys.stdout.write(target)
' 2>/dev/null
        ;;
    jq)
        jq -er '.tool_input.file_path // .tool_input.filePath
                | select(type == "string" and length > 0)' 2>/dev/null
        ;;
    esac
}

# True only when the decision on stdin explicitly denies the write.
decision_denied() {
    case "$PARSER" in
    python3)
        python3 -c '
import json
import sys

try:
    decision = json.load(sys.stdin)
except Exception:
    raise SystemExit(1)
denied = isinstance(decision, dict) and decision.get("allowed") is False
raise SystemExit(0 if denied else 1)
' 2>/dev/null
        ;;
    jq)
        jq -e '.allowed == false' >/dev/null 2>&1
        ;;
    esac
}

# Prints one string-valued field of the decision on stdin, or nothing.
decision_field() {
    case "$PARSER" in
    python3)
        python3 -c '
import json
import sys

try:
    decision = json.load(sys.stdin)
except Exception:
    raise SystemExit(0)
value = decision.get(sys.argv[1]) if isinstance(decision, dict) else None
if isinstance(value, str):
    sys.stdout.write(value)
' "$1" 2>/dev/null
        ;;
    jq)
        jq -r --arg key "$1" '
            if (.[$key] | type) == "string" then .[$key] else "" end' 2>/dev/null
        ;;
    esac
}

PAYLOAD=$(cat 2>/dev/null)
if [ -z "$PAYLOAD" ]; then
    note 'empty hook payload; allowing the write'
    exit 0
fi

TARGET=$(printf '%s' "$PAYLOAD" | hook_file_path)
STATUS=$?
if [ "$STATUS" -ne 0 ] || [ -z "$TARGET" ]; then
    note "no file path in the hook payload (parser exit $STATUS); allowing the write"
    exit 0
fi

if ! command -v bk >/dev/null 2>&1; then
    note 'bk is not on PATH; allowing the write (gate not enforced)'
    exit 0
fi

if [ ! -d "$VAULT" ]; then
    note "vault $VAULT is unreachable; allowing the write (gate not enforced)"
    exit 0
fi

DECISION=$(bk --vault "$VAULT" gate check-write "$TARGET" --json 2>/dev/null)
STATUS=$?

if [ "$STATUS" -eq 0 ]; then
    exit 0
fi

if [ "$STATUS" -ne 2 ] || [ -z "$DECISION" ]; then
    note "bk gate check-write exited $STATUS; allowing the write (gate not enforced)"
    exit 0
fi

if ! printf '%s' "$DECISION" | decision_denied; then
    note 'bk gate check-write exited 2 without a deny decision; allowing the write'
    exit 0
fi

REASON=$(printf '%s' "$DECISION" | decision_field reason)
REMEDIATION=$(printf '%s' "$DECISION" | decision_field remediation)
if [ -z "$REASON" ]; then
    REASON='This path is written by the brainskit apply gate, not by a file-write tool.'
fi

printf 'brainskit: %s\n' "$REASON" >&2
if [ -n "$REMEDIATION" ]; then
    printf '%s\n' "$REMEDIATION" >&2
fi
exit 2
