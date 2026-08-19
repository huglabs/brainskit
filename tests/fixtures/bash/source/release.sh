#!/usr/bin/env bash
set -euo pipefail

ARTIFACT_DIR="dist"

log() {
    printf '%s\n' "$1" >&2
}

clean() {
    log "removing ${ARTIFACT_DIR}"
    rm -rf "${ARTIFACT_DIR}"
}

package() {
    clean
    mkdir -p "${ARTIFACT_DIR}"
    log "packaged into ${ARTIFACT_DIR}"
}

main() {
    package
    log "done"
}

main "$@"
