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
# Build from a clean slate, because otherwise this gate can pass on state that
# a clean checkout does not have. `include-package-data = true` makes setuptools
# read an existing `.egg-info/SOURCES.txt` when it assembles the sdist, so a file
# recorded by an earlier build keeps shipping after the `package-data` glob that
# named it is gone -- verified: deleting the `templates/web/*.js` glob left
# `three.min.js` in the wheel, and it disappeared only once the egg-info went.
# CI checks out fresh and would have dropped it; the maintainer's machine would
# have said the release was fine.
rm -rf build
rm -rf src/*.egg-info
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

# Everything below runs against a throwaway machine configuration. The CLI
# contract section ends with `bk init "$WORK/vault"`, and `bk init` registers
# the vault it creates in the machine-level registry -- correct for an operator,
# wrong for a gate, because the vault it registers is inside the `mktemp -d`
# this script deletes on exit. Every run left one more
# `.../T/tmp.XXXXXXXXXX/vault` entry pointing at a directory that no longer
# exists; a maintainer's registry had accumulated eleven of them.
#
# `XDG_CONFIG_HOME` is the same seam `tests/conftest.py` uses for the suite and
# the one `infrastructure/vaults.py::default_registry_path` already reads, so
# nothing in the product has to know it is being verified, and any future
# machine-wide config file honouring the variable is covered too. Absolute, as
# the XDG basedir specification requires: a relative value is ignored and falls
# straight back to the real `~/.config`, which is the state this prevents.
#
# Set *after* the `uv` steps rather than beside `mktemp`, deliberately. `uv`
# reads its own `uv.toml` from `XDG_CONFIG_HOME`, so blanking it earlier would
# silently change which index and settings built and installed the wheel -- the
# gate would then be verifying an artifact the operator's real configuration
# never produces.
export XDG_CONFIG_HOME="$WORK/config"
mkdir -p "$XDG_CONFIG_HOME"

echo "==> packaged resources"
"$PY" - <<'PY'
import sys
from importlib.resources import files

# Everything the package needs at runtime and cannot import. The two holes this
# list used to have were the ones that mattered:
#
#   templates/web  — `bk web` serves three.min.js at /static/three.min.js. A
#     regressed glob leaves the renderer 404ing while this gate stays green.
#   infrastructure/codeanalysis — 43 vendored MIT/Apache source files shipped
#     for three releases without LICENSE-MIT or NOTICE beside them. MIT requires
#     its permission notice to accompany all copies; Apache-2.0 §4(c)/(d)
#     requires the NOTICE content to be conveyed. NOTICE is also read at runtime
#     for the AST cache namespace, checked below.
expected = {
    "jobs": {"digest.md", "file-proposal.md", "ingest.md", "lint-semantic.md",
             "query.md", "resurface.md"},
    "jobs/_output-schemas": {"digest.json", "file-proposal.json", "ingest.json",
                             "lint-semantic.json", "query.json", "resurface.json"},
    "templates/default": {"schema.json"},
    "templates/agents": {"claude-skill.md", "instructions.md",
                         "brainskit-gate.sh", "brainskit-status.sh"},
    "templates/web": {"three.min.js"},
    "infrastructure/codeanalysis": {"LICENSE-MIT", "NOTICE"},
}

root = files("brainskit")
missing = []
for subdir, names in expected.items():
    try:
        present = {entry.name for entry in (root / subdir).iterdir()}
    except (FileNotFoundError, NotADirectoryError):
        # A glob that stops matching takes the whole directory with it, so this
        # is the shape the failure actually has. Report it as missing resources
        # rather than letting `iterdir` raise a traceback at the reader.
        present = set()
    for name in sorted(names - present):
        missing.append(f"{subdir}/{name}")

if missing:
    print("    missing packaged resources:", ", ".join(missing))
    sys.exit(1)
print(f"    ok: {sum(len(v) for v in expected.values())} resources")
PY

echo "==> attribution"
# A licence that ships only in the repository does not accompany the copies that
# matter. Asserted against the installed artifact, and against the sdist, which
# is published alongside the wheel and carries the same 43 source files.
"$PY" - <<'PY'
import sys
from importlib.metadata import distribution
from importlib.resources import files

problems = []

vendored = files("brainskit") / "infrastructure" / "codeanalysis"
mit = (vendored / "LICENSE-MIT").read_text(encoding="utf-8")
if "shall be included in all" not in mit:
    problems.append("codeanalysis/LICENSE-MIT lost its MIT permission notice")

notice = (vendored / "NOTICE").read_text(encoding="utf-8")
for heading in ("MODIFIED", "REMOVED"):
    if heading not in notice:
        problems.append(f"codeanalysis/NOTICE lost its {heading} declaration")

# `license-files` puts the project's own LICENSE and NOTICE here. They are what
# states the copyright holder and conveys the third-party attribution, so an
# empty or dropped entry is a compliance regression, not cosmetics.
shipped = {
    path.name: path
    for path in (distribution("brainskit").files or [])
    if "licenses/" in path.as_posix()
}
for name in ("LICENSE", "NOTICE"):
    if name not in shipped:
        problems.append(f"dist-info/licenses/{name} is missing")
        continue
    text = shipped[name].read_text(encoding="utf-8")
    if "[name of copyright owner]" in text:
        problems.append(f"{name} still carries the unfilled Apache template line")
    if name == "NOTICE" and "src/brainkit/" in text:
        problems.append("NOTICE points at src/brainkit/, which does not exist")

if problems:
    for problem in problems:
        print(f"    {problem}")
    sys.exit(1)
print("    ok: vendored LICENSE-MIT + NOTICE, dist-info/licenses/{LICENSE,NOTICE}")
PY

# Listed once into a variable, and matched without a pipeline. `tar tzf "$SDIST"
# | grep -q` is the obvious spelling and it is a false negative under `pipefail`:
# `grep -q` exits at its first match -- entry 37 of 166 here -- which closes the
# pipe while tar is still writing the other 129. GNU tar dies of EPIPE, `pipefail`
# adopts that status for the whole pipeline, and the leading `!` inverts it into
# "missing". macOS ships bsdtar, which finishes writing before grep can leave and
# exits 0, so this passed on the maintainer's machine and failed every run on
# CI's GNU tar -- it refused the 0.6.1 release over two files the sdist
# demonstrably carried, seconds after the wheel built from that same sdist was
# found to contain them.
#
# So no early-exiting reader may sit downstream of a producer here. Note that
# `printf '%s\n' "$SDIST_ENTRIES" | grep -q` is the *same* bug and not a fix:
# measured, it survives only while the listing fits the 64 KiB pipe buffer, and
# returns 141 on a listing that does not. A here-string has no exposure at any
# size, because bash writes the whole string before `grep` is ever exec'd.
SDIST_ENTRIES="$(tar tzf "$SDIST")"
for path in \
    "src/brainskit/infrastructure/codeanalysis/LICENSE-MIT" \
    "src/brainskit/infrastructure/codeanalysis/NOTICE"; do
    if ! grep -q "/$path\$" <<<"$SDIST_ENTRIES"; then
        echo "    sdist is missing $path"
        exit 1
    fi
done
echo "    ok: the sdist carries the vendored licence files too"

echo "==> AST cache namespace"
# `_cache_format_marker` hashes the vendored tree, `NOTICE` included, and falls
# back to the constant "unversioned" when it cannot read them. Through 0.6.0
# every installed copy hit that fallback -- the namespace meant to stop a
# changed extractor reading a stale cache was inert in exactly the environments
# the cache runs in. It is only observable from an installed artifact, so it is
# asserted here rather than in the test suite.
MARKER="$("$PY" -c 'from brainskit.infrastructure import extractor; print(extractor._cache_format_marker())')"
if [ "$MARKER" = "unversioned" ]; then
    echo "    the AST cache namespace fell back to \"unversioned\": the vendored"
    echo "    NOTICE did not ship, so a re-vendor cannot invalidate the cache"
    exit 1
fi
echo "    ok: namespace is $MARKER, not the unreadable-tree fallback"

echo "==> version agreement"
# Asserted against the INSTALLED wheel, not the working tree: the drift that
# shipped as 0.5.0-with-bk-0.4.0 was invisible to every repo-level check,
# because `release.yml` compared the tag to `[project].version` and nothing
# ever asked the artifact what it thought it was.
INSTALLED="$("$PY" -c 'import brainskit; print(brainskit.__version__)')"
REPORTED="$("$BK" --version | awk '{print $NF}')"
METADATA="$("$PY" -c 'from importlib.metadata import version; print(version("brainskit"))')"
for pair in "__version__:$INSTALLED" "bk --version:$REPORTED" "dist metadata:$METADATA"; do
    label="${pair%%:*}"
    value="${pair#*:}"
    if [ "$value" != "$VERSION" ]; then
        echo "    $label reports $value, expected $VERSION"
        exit 1
    fi
done
echo "    ok: __version__, bk --version and metadata all report $VERSION"

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
