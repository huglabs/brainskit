#!/usr/bin/env bash
# Build the wheel and prove it is deliverable: install it in an isolated
# environment, assert that every packaged resource shipped, and drive the real
# CLI contract end to end. A wheel that imports but cannot init a vault is a
# broken delivery, so this script fails on behaviour, not only on import.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

VERSION="$(python3 -c '
import tomllib
with open("pyproject.toml", "rb") as handle:
    print(tomllib.load(handle)["project"]["version"])
')"

# Build the full pipeline, not just the wheel: `uv build` produces the sdist and
# then builds the wheel *from that sdist*, which is what publishing uploads. A
# `--wheel` build reads the working tree instead, so it can pass while the
# shipped wheel is missing whatever the sdist dropped.
echo "==> uv build (sdist, then wheel from sdist)"
rm -f "dist/brainskit-$VERSION.tar.gz" "dist/brainskit-$VERSION-"*.whl
uv build --out-dir dist
SDIST="dist/brainskit-$VERSION.tar.gz"
WHEEL="$(echo "dist/brainskit-$VERSION-"*.whl)"
[ -f "$SDIST" ] || { echo "    missing sdist: $SDIST"; exit 1; }
[ -f "$WHEEL" ] || { echo "    missing wheel: $WHEEL"; exit 1; }
echo "    $SDIST"
echo "    $WHEEL"

echo "==> isolated install"
uv venv --python "$(cat .python-version)" "$WORK/venv" >/dev/null
uv pip install --quiet --python "$WORK/venv/bin/python" "$WHEEL"
PY="$WORK/venv/bin/python"
BK="$WORK/venv/bin/bk"

echo "==> packaged resources"
"$PY" - <<'PY'
import sys
from importlib.resources import files

expected = {
    "jobs": {"digest.md", "file-proposal.md", "ingest.md", "lint-semantic.md",
             "query.md", "resurface.md"},
    "jobs/_output-schemas": {"digest.json", "file-proposal.json", "ingest.json",
                             "lint-semantic.json", "query.json", "resurface.json"},
    "templates/default": {"schema.json"},
    "templates/agents": {"claude-skill.md", "instructions.md",
                         "brainskit-gate.sh", "brainskit-status.sh"},
}

root = files("brainskit")
missing = []
for subdir, names in expected.items():
    present = {entry.name for entry in (root / subdir).iterdir()}
    for name in sorted(names - present):
        missing.append(f"{subdir}/{name}")

if missing:
    print("    missing packaged resources:", ", ".join(missing))
    sys.exit(1)
print(f"    ok: {sum(len(v) for v in expected.values())} resources")
PY

echo "==> CLI contract"
"$PY" - "$ROOT/tests" "$WORK/policy.json" <<'PY'
import json
import sys

sys.path.insert(0, sys.argv[1])
from test_engine import policy  # the vault policy contract has one source

with open(sys.argv[2], "w", encoding="utf-8") as handle:
    json.dump(policy(), handle)
PY

printf '# Wheel smoke\nEvidence for the packaged delivery check.\n' > "$WORK/note.md"

run() {
    local label="$1"
    shift
    local out
    out="$("$@")"
    if ! "$PY" -c 'import json,sys; sys.exit(0 if json.loads(sys.argv[1])["ok"] else 1)' "$out"; then
        echo "    $label failed: $out"
        exit 1
    fi
    echo "    $label ok"
}

run init "$BK" init "$WORK/vault" --config "$WORK/policy.json" --json
run capture "$BK" --vault "$WORK/vault" capture "$WORK/note.md" --json
run status "$BK" --vault "$WORK/vault" status --json
run lint "$BK" --vault "$WORK/vault" lint --json

echo "==> $WHEEL is deliverable (built from $SDIST)"
