#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# Release-candidate tier: every gate an RC must pass, in order, with no
# deselection of known failures. Requires a reachable Docker daemon and
# installed Playwright browsers (npm ci && npx playwright install chromium
# firefox). Nothing is installed implicitly.
#
#   ./scripts/test-rc.sh            run every gate
#   ./scripts/test-rc.sh --list     print the gates without running them
#
# See docs/developer/testing.md.
set -euo pipefail
cd "$(dirname "$0")/.."
# shellcheck source=scripts/lib/pytest-tier.sh
. scripts/lib/pytest-tier.sh

GATES="static python-full simulation-power-control authority security
system-build docker-first chromium-full firefox-full admin-replacement
generated-files clean-tree"

if [ "${1:-}" = "--list" ]; then
    printf '%s\n' $GATES
    exit 0
fi

require_docker
require_playwright

gate_static() {
    run_stage "static: ruff" ruff check .
    run_stage "static: compileall" "$PYTHON" -m compileall -q \
        admin ems dashboard scripts tests emsctl.py ems-solarflow-api-control.py
    run_stage "static: admin.js" node --check admin/static/admin.js
    run_stage "static: whitespace" git diff --check
}

gate_python_full() {
    run_pytest_tier "rc/python-full" -q -m "not docker"
}

gate_simulation_power_control() {
    run_pytest_tier "rc/simulation-power-control" -q -m "simulation and power_control"
}

gate_authority() {
    run_pytest_tier "rc/authority" -q -m "authority"
}

gate_security() {
    run_pytest_tier "rc/security" -q -m "not docker" \
        -k "auth or secret or csrf or xss or privilege or redaction or hardening"
}

gate_system_build() {
    run_pytest_tier "rc/system-build" -q -m "system_build"
}

gate_docker_first() {
    run_pytest_tier "rc/docker-first" -q -rs -m "docker"
}

gate_chromium_full() {
    run_stage "rc/chromium-full" npx playwright test --project=chromium
}

gate_firefox_full() {
    run_stage "rc/firefox-full" npx playwright test --project=firefox
}

gate_admin_replacement() {
    run_stage "rc/admin-replacement" \
        npx playwright test --config=playwright.admin-replacement.config.ts
}

gate_generated_files() {
    run_stage "rc/generated-config-template" \
        "$PYTHON" tools/build_config_template.py --check
}

gate_clean_tree() {
    run_stage "rc/clean-tree" git status --porcelain
    if [ -n "$(git status --porcelain)" ]; then
        printf 'error: working tree is not clean after the RC run\n' >&2
        return 1
    fi
}

for gate in $GATES; do
    "gate_$(printf '%s' "$gate" | tr '-' '_')"
done

printf '\n=== RC tier complete: %s\n' "$(printf '%s' "$GATES" | tr '\n' ' ')"
