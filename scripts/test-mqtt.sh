#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# MQTT tier: local MQTT, Zendure MQTT and broker workflows.
# Optional first argument narrows to one execution level:
#   all (default) unit contract integration e2e
# Real-broker Mosquitto suites are Docker-only; run them with
# scripts/test-pr.sh docker. See docs/developer/testing.md.
set -euo pipefail
cd "$(dirname "$0")/.."
# shellcheck source=scripts/lib/pytest-tier.sh
. scripts/lib/pytest-tier.sh

level="all"
if [ $# -gt 0 ] && [ "${1#-}" = "$1" ]; then
    level="$1"
    shift
fi

case "$level" in
    all)
        selection="mqtt and not docker and not slow"
        ;;
    unit|contract|integration|e2e)
        selection="mqtt and ${level} and not docker and not slow"
        ;;
    *)
        printf 'usage: %s [all|unit|contract|integration|e2e] [pytest args]\n' "$0" >&2
        exit 2
        ;;
esac

run_pytest_tier "mqtt/${level}" -q -m "$selection" "$@"
