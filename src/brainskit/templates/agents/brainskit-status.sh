#!/bin/sh
# brainskit:generated — written by `bk hooks install --agent claude`.
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
# There is deliberately no WORKSPACE here any more. The agent's configuration
# lives outside the vault -- hooks, the instruction file, the git repository
# that tracks it -- and this script used to locate that itself in order to
# decide whether the commit hook was live. `bk status` already resolves the
# enclosing repository, and knows the two things a `[ -f ]` cannot: that
# `core.hooksPath` sends git elsewhere, and that a script on disk may be
# registered nowhere. Asking it once beats re-deriving it badly here.

note() {
    printf 'brainskit-status: %s\n' "$*" >&2
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
# the reader below keeps its positions. `code status` only ever reads the
# stored graph -- no tree-sitter grammar required -- so it is safe to call
# even on a vault that never installed the `code` extra; it just reports
# `missing` the same way `bk code status` would from a terminal.
PROPOSALS_JSON=$(bk --vault "$VAULT" proposals --status pending --json 2>/dev/null)
LINT_JSON=$(bk --vault "$VAULT" lint --json 2>/dev/null)
CODE_JSON=$(bk --vault "$VAULT" code status --json 2>/dev/null)
[ -n "$PROPOSALS_JSON" ] || PROPOSALS_JSON=null
[ -n "$LINT_JSON" ] || LINT_JSON=null
[ -n "$CODE_JSON" ] || CODE_JSON=null

# Enforcement is NOT recomputed here. `$STATUS_JSON` already carries
# `enforcement.layers[]` with `active`, `detail` and `script`, and `bk status`
# knows things this shell cannot cheaply learn: that `core.hooksPath` sends git
# somewhere other than `.git/hooks`, and that a script on disk is registered
# nowhere. Re-deriving it as `[ -x gate.sh ]` and `[ -f pre-commit ]` announced
# both of those as active -- in the block an agent reads to decide what it
# walked into.

printf '%s\n%s\n%s\n%s\n' "$STATUS_JSON" "$PROPOSALS_JSON" "$LINT_JSON" "$CODE_JSON" | python3 -c '
import json
import sys


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
parsed += [None] * (4 - len(parsed))
status, proposals, lint, code = (result(item) for item in parsed[:4])
if not status:
    print("brainskit: status returned no readable result", file=sys.stderr)
    raise SystemExit(0)

freshness = status.get("freshness") or {}
index = status.get("index") or {}
lines = ["brainskit vault {}".format(status.get("vault", "?"))]
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

# `code` is {} rather than a real result whenever `bk code status` itself
# could not be read (bk missing the subcommand, a vault old enough to
# predate it) -- silence here, same tolerance as a missing lint/proposals
# document above, not a claim that no code graph exists.
code_state = code.get("state") if isinstance(code, dict) else None
if code_state == "missing":
    lines.append("  code graph missing - build it with: bk code build")
elif code_state == "stale":
    lines.append(
        "  code graph stale ({} changed, {} removed) - refresh with: bk code build".format(
            code.get("changed_total", 0), code.get("removed_total", 0)
        )
    )
elif code_state == "fresh":
    lines.append("  code graph fresh ({} files indexed)".format(code.get("files", 0)))

# Rendered from the document, not re-derived. An advisory layer (CLAUDE.md) is
# not enforcement, so it is not claimed as such; a layer that is off repeats the
# reason `bk status` gave rather than inventing a second wording for it.
LABELS = {
    "write_gate": "write gate",
    "session_status": "session status",
    "commit_lint": "commit lint",
}
enforcement = status.get("enforcement")
reported = enforcement.get("layers") if isinstance(enforcement, dict) else None
parts = []
for entry in reported if isinstance(reported, list) else []:
    if not isinstance(entry, dict) or entry.get("advisory"):
        continue
    label = LABELS.get(entry.get("layer"))
    if label is None:
        continue
    if entry.get("active"):
        parts.append("{} active".format(label))
    else:
        parts.append("{} OFF ({})".format(label, entry.get("detail") or "not installed"))
if parts:
    lines.append("  enforcement " + " - ".join(parts))
else:
    lines.append("  enforcement not reported by this bk")
lines.append("  evidence: bk context \"QUERY\" --consumer local --json - writes: bk apply")
print("\n".join(lines))
'

exit 0
