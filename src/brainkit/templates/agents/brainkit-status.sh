#!/bin/sh
# brainkit:generated — written by `bk hooks install --agent claude`.
#
# Claude Code SessionStart hook. Prints a compact vault summary on stdout —
# counts, pending proposals, open `wiki.outside_apply` findings and which
# enforcement layers are actually on — so a session starts knowing what it
# walked into instead of discovering it eleven pages later.
#
# Two rules this script exists to obey:
#
#   1. Emit the content, never a path to it. A hook that echoes a filename
#      delivers nothing to the model.
#   2. When the vault cannot be reached, SAY SO on stderr. Exiting 0 in silence
#      is how a dead hook stays dead for weeks without anyone noticing.
#
# It never blocks the session: every path exits 0.

set -u

VAULT={{vault}}

note() {
    printf 'brainkit-status: %s\n' "$*" >&2
}

if ! command -v bk >/dev/null 2>&1; then
    note 'bk is not on PATH; no vault summary for this session'
    exit 0
fi

if [ ! -d "$VAULT" ]; then
    note "vault $VAULT is unreachable; no vault summary for this session"
    exit 0
fi

if ! command -v python3 >/dev/null 2>&1; then
    note 'python3 is not available; no vault summary for this session'
    exit 0
fi

STATUS_JSON=$(bk --vault "$VAULT" status --json)
STATUS=$?
if [ "$STATUS" -ne 0 ] || [ -z "$STATUS_JSON" ]; then
    note "bk status exited $STATUS; no vault summary for this session"
    # In --json mode bk reports the cause on stdout, so it is sitting in the
    # captured output rather than on the terminal. Say what it was.
    CAUSE=$(printf '%s' "$STATUS_JSON" | python3 -c '
import json
import sys

try:
    payload = json.load(sys.stdin)
except Exception:
    raise SystemExit(0)
error = payload.get("error") if isinstance(payload, dict) else None
if isinstance(error, dict) and isinstance(error.get("message"), str):
    sys.stdout.write(error["message"])
' 2>/dev/null)
    if [ -n "$CAUSE" ]; then
        note "bk said: $CAUSE"
    fi
    exit 0
fi

# `lint` exits 1 when it finds errors, which is the interesting case, so its
# exit code is deliberately not checked. A missing document becomes `null` so
# the reader below keeps its positions.
PROPOSALS_JSON=$(bk --vault "$VAULT" proposals --status pending --json 2>/dev/null)
LINT_JSON=$(bk --vault "$VAULT" lint --json 2>/dev/null)
[ -n "$PROPOSALS_JSON" ] || PROPOSALS_JSON=null
[ -n "$LINT_JSON" ] || LINT_JSON=null

HOOK_DIR=$(dirname "$0")
if [ -x "$HOOK_DIR/brainkit-gate.sh" ]; then
    WRITE_GATE=active
else
    WRITE_GATE='OFF (brainkit-gate.sh is missing; re-run bk hooks install)'
fi

if [ ! -d "$VAULT/.git" ]; then
    COMMIT_LINT='OFF (the vault is not a git repository)'
elif [ ! -f "$VAULT/.git/hooks/pre-commit" ]; then
    COMMIT_LINT='OFF (no pre-commit hook is installed)'
else
    COMMIT_LINT=active
fi

printf '%s\n%s\n%s\n' "$STATUS_JSON" "$PROPOSALS_JSON" "$LINT_JSON" | python3 -c '
import json
import sys

write_gate, commit_lint = sys.argv[1], sys.argv[2]


def documents(raw):
    decoder = json.JSONDecoder()
    found, index = [], 0
    while index < len(raw):
        while index < len(raw) and raw[index].isspace():
            index += 1
        if index >= len(raw):
            break
        try:
            value, index = decoder.raw_decode(raw, index)
        except ValueError:
            break
        found.append(value)
    return found


def result(document):
    if isinstance(document, dict) and document.get("ok") and isinstance(
        document.get("result"), dict
    ):
        return document["result"]
    return {}


parsed = documents(sys.stdin.read())
parsed += [None] * (3 - len(parsed))
status, proposals, lint = (result(item) for item in parsed[:3])
if not status:
    print("brainkit: status returned no readable result", file=sys.stderr)
    raise SystemExit(0)

freshness = status.get("freshness") or {}
index = status.get("index") or {}
lines = ["brainkit vault {}".format(status.get("vault", "?"))]
lines.append(
    "  sources {} ({} awaiting filing) - wiki {} pages - index {} documents".format(
        status.get("sources", 0),
        status.get("pending", 0),
        status.get("wiki_pages", 0),
        index.get("documents", 0),
    )
)
lines.append(
    "  freshness fresh {} - review {} - stale {} - unknown {}".format(
        freshness.get("fresh", 0),
        freshness.get("review", 0),
        freshness.get("stale", 0),
        freshness.get("unknown", 0),
    )
)

# Present only once projection freshness lands; absence is not an error here.
projections = status.get("projections")
if isinstance(projections, dict) and projections:
    rendered = [
        "{}{}".format(name, " STALE" if (entry or {}).get("stale") else "")
        for name, entry in sorted(projections.items())
        if isinstance(entry, dict)
    ]
    if rendered:
        lines.append("  projections " + " - ".join(rendered))

lines.append("  proposals pending {}".format(proposals.get("count", 0)))

findings = lint.get("findings")
findings = findings if isinstance(findings, list) else []
errors = [item for item in findings if isinstance(item, dict) and item.get("severity") == "error"]
bypassed = [item for item in errors if item.get("code") == "wiki.outside_apply"]
lines.append(
    "  lint {} errors - {} written outside the apply gate".format(
        len(errors), len(bypassed)
    )
)
for item in bypassed[:5]:
    lines.append("    outside apply: {}".format(item.get("path") or "?"))
if len(bypassed) > 5:
    lines.append("    ... and {} more".format(len(bypassed) - 5))

lines.append("  enforcement write gate {} - commit lint {}".format(write_gate, commit_lint))
lines.append("  evidence: bk context \"QUERY\" --consumer local --json - writes: bk apply")
print("\n".join(lines))
' "$WRITE_GATE" "$COMMIT_LINT"

exit 0
