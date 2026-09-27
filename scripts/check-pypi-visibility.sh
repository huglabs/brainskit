#!/usr/bin/env bash
# Assert that a published version is actually installable from PyPI: both the
# wheel and the sdist for exactly VERSION are listed on the simple index.
#
# The release gate used to match `*"brainskit-$version"*` anywhere in the index
# body. That is a substring test, so a `v0.6` tag was satisfied by the existing
# `brainskit-0.6.0-py3-none-any.whl`, and one matching file was enough -- a
# wheel-only or sdist-only upload went green. The simple index renders each
# filename as the anchor text of its link, so `>` and `<` pin the version at
# both ends, and each artifact is required on its own.
#
# BRAINSKIT_INDEX_URL overrides the index (tests point it at a `file://`
# fixture); ATTEMPTS and DELAY tune the retry, because the index is eventually
# consistent and a fresh upload takes a few seconds to appear.
set -euo pipefail

VERSION="${1:?usage: check-pypi-visibility.sh VERSION}"
INDEX="${BRAINSKIT_INDEX_URL:-https://pypi.org/simple/brainskit/}"
ATTEMPTS="${ATTEMPTS:-6}"
DELAY="${DELAY:-10}"

WHEEL="brainskit-$VERSION-py3-none-any.whl"
SDIST="brainskit-$VERSION.tar.gz"

for attempt in $(seq 1 "$ATTEMPTS"); do
    body="$(curl -fsSL "$INDEX" || true)"
    missing=()
    case "$body" in *">$WHEEL<"*) ;; *) missing+=("$WHEEL") ;; esac
    case "$body" in *">$SDIST<"*) ;; *) missing+=("$SDIST") ;; esac
    if [ "${#missing[@]}" -eq 0 ]; then
        echo "brainskit $VERSION is on PyPI: $WHEEL, $SDIST"
        exit 0
    fi
    echo "attempt $attempt: not visible yet: ${missing[*]}"
    if [ "$attempt" -lt "$ATTEMPTS" ]; then
        sleep "$DELAY"
    fi
done
echo "brainskit $VERSION is not on PyPI after publishing; missing: ${missing[*]}" >&2
echo "the upload step reported no error, which is how a silent" >&2
echo "no-op ships as a green release" >&2
exit 1
