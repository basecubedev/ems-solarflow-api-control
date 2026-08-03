# SPDX-License-Identifier: AGPL-3.0-or-later
# shellcheck shell=bash
#
# Shared helpers for the scripts/test-*.sh tiers. Source it from the repository
# root. Nothing here installs dependencies or writes outside the repository.

PYTHON="${PYTHON:-python3}"

announce() {
    printf '\n=== %s\n' "$1"
    shift
    printf '    %s\n\n' "$*"
}

run_pytest_tier() {
    tier="$1"
    shift
    announce "tier: ${tier}" "pytest $*"
    status=0
    "$PYTHON" -m pytest "$@" || status=$?
    # Exit code 5 means the selection matched nothing. A marker typo or a
    # renamed module must fail the tier instead of reporting "nothing to do".
    if [ "$status" -eq 5 ]; then
        printf '\nerror: tier "%s" collected no tests; check the marker expression\n' \
            "$tier" >&2
    fi
    return "$status"
}

run_stage() {
    stage="$1"
    shift
    announce "stage: ${stage}" "$*"
    "$@"
}

require_playwright() {
    if [ ! -x node_modules/.bin/playwright ]; then
        printf 'error: Playwright is not installed. Run: npm ci\n' >&2
        return 1
    fi
}

require_docker() {
    if ! command -v docker >/dev/null 2>&1 || ! docker info >/dev/null 2>&1; then
        printf 'error: a reachable Docker daemon is required for this tier\n' >&2
        return 1
    fi
}
