#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# Regenerate every user-documentation screenshot.
#
#   ./scripts/capture-docs-screenshots.sh            # Admin + Dashboard
#   ./scripts/capture-docs-screenshots.sh admin      # Admin Console only
#   ./scripts/capture-docs-screenshots.sh dashboard  # EMS Dashboard only
#
# The Admin capture scripts start their own loopback-only preview server from
# the deterministic fixtures in tests/fixtures/admin_docs/; the EMS Dashboard's
# are a Playwright spec (tests/e2e-dashboard/capture-docs.spec.ts) whose
# webServer is scripts/serve_dashboard_preview.py over
# scripts/dashboard_preview_data.py. Each shuts its server down again when it
# finishes. No Docker, no hardware, no discovery, no MQTT broker, no Zendure
# credentials, no config.json and no runtime state are involved, and nothing is
# pushed.
set -euo pipefail
cd "$(dirname "$0")/.."

target="${1:-all}"

case "$target" in
    all) tools="firefox convert npx" ;;
    admin) tools="firefox convert" ;;
    dashboard) tools="npx" ;;
    *)
        printf 'usage: %s {all|admin|dashboard}\n' "$0" >&2
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
    python3 scripts/capture_admin_docs.py
    printf '== Admin Console, Modern ==\n'
    node scripts/capture_admin_modern_docs.mjs
fi

if [ "$target" = all ] || [ "$target" = dashboard ]; then
    printf '== EMS Dashboard ==\n'
    EMS_DASHBOARD_CAPTURE_DOCS=1 npx playwright test \
        --config=playwright.dashboard.config.ts --project=chromium capture-docs
fi

printf '\nDone. Review the diff before committing:\n'
printf '  git status --short docs/assets\n'
