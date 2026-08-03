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

# The replacement canary replaces one published Admin container with another,
# both pinned by digest. Those identities come from the Development catalogue, so
# the gate cannot derive them locally — name them instead of failing deep inside
# the Playwright web server. scripts/resolve_canary_builds.py prints exactly
# these values for a catalogue.
require_replacement_canary_env() {
    missing=""
    for name in ADMIN_REPLACEMENT_RUNTIME ADMIN_REPLACEMENT_EVENTS \
        CANARY_SOURCE_TAG CANARY_SOURCE_REVISION CANARY_SOURCE_BUILD_ID \
        CANARY_SOURCE_ADMIN_DIGEST CANARY_TAG CANARY_REVISION CANARY_BUILD_ID \
        CANARY_ADMIN_DIGEST CANARY_EMS_DIGEST; do
        eval "value=\${$name:-}"
        [ -n "$value" ] || missing="${missing} ${name}"
    done
    if [ -n "$missing" ]; then
        printf 'error: the Admin replacement canary needs published image digests.\n' >&2
        printf '       missing:%s\n' "$missing" >&2
        printf '       See .github/workflows/admin-replacement-canary.yml for how CI supplies them.\n' >&2
        return 1
    fi
}
