#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# Pull-request tiers. Each group runs independently and mirrors one CI job.
#
#   core admin mqtt power-control docker chromium-critical firefox-smoke
#
# The four Python groups partition the non-Docker suite: `core` is the
# complement of the functional groups, so no test can fall out of PR coverage
# by staying unclassified. See docs/developer/testing.md.
set -euo pipefail
cd "$(dirname "$0")/.."
# shellcheck source=scripts/lib/pytest-tier.sh
. scripts/lib/pytest-tier.sh

group="${1:-}"
[ $# -gt 0 ] && shift

case "$group" in
    core)
        run_pytest_tier "pr/core" -q \
            -m "not docker and not admin and not mqtt and not power_control" "$@"
        ;;
    admin)
        run_pytest_tier "pr/admin" -q -m "admin and not docker" "$@"
        ;;
    mqtt)
        run_pytest_tier "pr/mqtt" -q -m "mqtt and not docker" "$@"
        ;;
    power-control)
        run_pytest_tier "pr/power-control" -q -m "power_control and not docker" "$@"
        ;;
    docker)
        require_docker
        run_pytest_tier "pr/docker" -q -rs -m "docker" "$@"
        ;;
    chromium-critical)
        require_playwright
        run_stage "pr/chromium-critical" \
            npx playwright test --project=chromium --grep "@smoke|@authority" "$@"
        ;;
    firefox-smoke)
        require_playwright
        run_stage "pr/firefox-smoke" \
            npx playwright test --project=firefox --grep "@smoke" "$@"
        ;;
    *)
        printf 'usage: %s {core|admin|mqtt|power-control|docker|chromium-critical|firefox-smoke}\n' \
            "$0" >&2
        exit 2
        ;;
esac
