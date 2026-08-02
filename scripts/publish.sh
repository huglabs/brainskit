#!/usr/bin/env bash
# Publish brainkit to a GitLab PyPI package registry.
#
# A published version is permanent: the registry rejects a re-upload of the same
# filename, so a wheel that was never proven deliverable becomes a version that
# cannot be fixed in place. This script therefore refuses to upload anything it
# has not first built, installed in a throwaway environment and driven through
# the real CLI contract, and it refuses to publish code that is not committed.
#
# No credential is stored or read from a file. The token is taken from the
# environment, passed to uv through UV_PUBLISH_PASSWORD so it never appears in
# the process arguments, and never printed.
#
#   BRAINKIT_GITLAB_PROJECT_ID   numeric project id (Settings → General)
#   BRAINKIT_GITLAB_TOKEN        token with write_package_registry
#   BRAINKIT_GITLAB_USER         token username (default: the token name is
#                                irrelevant for a PAT; use any value)
#   BRAINKIT_GITLAB_HOST         default gitlab.dev.hugyourcustomer.ai
#
# Usage: scripts/publish.sh [--dry-run] [--allow-dirty] [--skip-verify]
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

HOST="${BRAINKIT_GITLAB_HOST:-gitlab.dev.hugyourcustomer.ai}"
PROJECT_ID="${BRAINKIT_GITLAB_PROJECT_ID:-129}"
TOKEN="${BRAINKIT_GITLAB_TOKEN:-}"
USERNAME="${BRAINKIT_GITLAB_USER:-brainkit-publisher}"

DRY_RUN=0
ALLOW_DIRTY=0
SKIP_VERIFY=0
for arg in "$@"; do
    case "$arg" in
        --dry-run) DRY_RUN=1 ;;
        --allow-dirty) ALLOW_DIRTY=1 ;;
        --skip-verify) SKIP_VERIFY=1 ;;
        *) echo "unknown argument: $arg" >&2; exit 2 ;;
    esac
done

fail() { echo "error: $*" >&2; exit 1; }

[ -n "$PROJECT_ID" ] || fail "BRAINKIT_GITLAB_PROJECT_ID is unset"
case "$PROJECT_ID" in (*[!0-9]*|"") fail "BRAINKIT_GITLAB_PROJECT_ID must be the numeric id, got '$PROJECT_ID'" ;; esac
[ -n "$TOKEN" ] || [ "$DRY_RUN" -eq 1 ] || fail "BRAINKIT_GITLAB_TOKEN is unset"

VERSION="$(python3 -c '
import tomllib
with open("pyproject.toml", "rb") as handle:
    print(tomllib.load(handle)["project"]["version"])
')"

PUBLISH_URL="https://$HOST/api/v4/projects/$PROJECT_ID/packages/pypi"
INDEX_URL="$PUBLISH_URL/simple"

echo "==> brainkit $VERSION -> $PUBLISH_URL"

# A published artifact must be reproducible from a commit, otherwise the version
# in the registry corresponds to a working tree that no longer exists anywhere.
if [ "$ALLOW_DIRTY" -eq 0 ]; then
    [ -z "$(git status --porcelain)" ] || fail "working tree is dirty; commit first or pass --allow-dirty"
    echo "    commit $(git rev-parse --short HEAD) (clean)"
fi

# The gate builds dist/ and proves that exact wheel; publishing anything else
# would ship an artifact nothing verified.
if [ "$SKIP_VERIFY" -eq 0 ]; then
    ./scripts/verify-wheel.sh
else
    echo "==> skipping the delivery gate (--skip-verify)"
    rm -f "dist/brainkit-$VERSION.tar.gz" "dist/brainkit-$VERSION-"*.whl
    uv build --out-dir dist
fi

FILES=()
for path in "dist/brainkit-$VERSION.tar.gz" "dist/brainkit-$VERSION-"*.whl; do
    [ -f "$path" ] || fail "expected artifact missing: $path"
    FILES+=("$path")
done
printf '    %s\n' "${FILES[@]}"

if [ "$DRY_RUN" -eq 1 ]; then
    echo "==> dry run: nothing uploaded"
    exit 0
fi

echo "==> uv publish"
UV_PUBLISH_USERNAME="$USERNAME" UV_PUBLISH_PASSWORD="$TOKEN" \
    uv publish --publish-url "$PUBLISH_URL" --check-url "$INDEX_URL" "${FILES[@]}"

cat <<EOF

==> published brainkit $VERSION

Consumers install it by name, with the registry as a named index:

    uv tool install brainkit --index brainkit=$INDEX_URL

Bump [project].version before the next publish; the registry will reject a
re-upload of these filenames.
EOF
