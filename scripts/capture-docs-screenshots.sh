#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# Regenerate every user-documentation screenshot.
#
#   ./scripts/capture-docs-screenshots.sh            # Admin + Dashboard + Appliance
#   ./scripts/capture-docs-screenshots.sh admin      # Admin Console only
#   ./scripts/capture-docs-screenshots.sh dashboard  # EMS Dashboard only
#   ./scripts/capture-docs-screenshots.sh appliance  # Appliance Manager only
#
# All three take their screenshots from the Modern design with Playwright Chromium.
# The Admin Console's script (scripts/capture_admin_docs.mjs) starts its own
# loopback-only preview server, scripts/serve_admin_docs_preview.py over the
# deterministic fixtures in tests/fixtures/admin_docs/; the EMS Dashboard's is
# a Playwright spec (tests/e2e-dashboard/capture-docs.spec.ts) whose webServer
# is scripts/serve_dashboard_preview.py over scripts/dashboard_preview_data.py;
# the Appliance Manager's is a Playwright spec too
# (tests/e2e-appliance/capture-docs.spec.ts) against the browser suite's
# scripted EMS_APPLIANCE_TEST_MODE host (tests/e2e-appliance/serve_appliance_test.py).
# Each shuts its server down again when it finishes. No Docker, no hardware,
# no discovery, no MQTT broker, no Zendure credentials, no config.json and no
# runtime state are involved, and nothing is pushed.
set -euo pipefail
cd "$(dirname "$0")/.."

target="${1:-all}"

case "$target" in
    all) tools="node npx" ;;
    admin) tools="node" ;;
    dashboard|appliance) tools="npx" ;;
    *)
        printf 'usage: %s {all|admin|dashboard|appliance}\n' "$0" >&2
        exit 2
        ;;
esac

for tool in $tools; do
    command -v "$tool" >/dev/null 2>&1 || {
        printf 'required executable not found: %s\n' "$tool" >&2
        exit 1
    }
done

if [ "$target" = all ] || [ "$target" = admin ]; then
    printf '== Admin Console ==\n'
    node scripts/capture_admin_docs.mjs
fi

if [ "$target" = all ] || [ "$target" = dashboard ]; then
    printf '== EMS Dashboard ==\n'
    EMS_DASHBOARD_CAPTURE_DOCS=1 npx playwright test \
        --config=playwright.dashboard.config.ts --project=chromium capture-docs
fi

if [ "$target" = all ] || [ "$target" = appliance ]; then
    printf '== Appliance Manager ==\n'
    EMS_APPLIANCE_CAPTURE_DOCS=1 npx playwright test \
        --config=playwright.appliance.config.ts --project=chromium capture-docs
fi

printf '\nDone. Review the diff before committing:\n'
printf '  git status --short docs/assets\n'
