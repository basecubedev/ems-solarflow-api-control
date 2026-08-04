#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# Regenerate every user-documentation screenshot.
#
#   ./scripts/capture-docs-screenshots.sh            # Admin + Dashboard
#   ./scripts/capture-docs-screenshots.sh admin      # Admin Console only
#   ./scripts/capture-docs-screenshots.sh dashboard  # EMS Dashboard only
#
# Both capture scripts start their own loopback-only preview server from the
# deterministic fixtures in tests/fixtures/admin_docs/ and
# scripts/dashboard_preview_data.py, and shut it down again when they finish.
# No Docker, no hardware, no discovery, no MQTT broker, no Zendure credentials,
# no config.json and no runtime state are involved, and nothing is pushed.
set -euo pipefail
cd "$(dirname "$0")/.."

target="${1:-all}"

for tool in firefox convert; do
    command -v "$tool" >/dev/null 2>&1 || {
        printf 'required executable not found: %s\n' "$tool" >&2
        exit 1
    }
done

case "$target" in
    all|admin|dashboard) ;;
    *)
        printf 'usage: %s {all|admin|dashboard}\n' "$0" >&2
        exit 2
        ;;
esac

if [ "$target" = all ] || [ "$target" = admin ]; then
    printf '== Admin Console ==\n'
    python3 scripts/capture_admin_docs.py
fi

if [ "$target" = all ] || [ "$target" = dashboard ]; then
    printf '== EMS Dashboard ==\n'
    python3 scripts/capture_dashboard_docs.py
fi

printf '\nDone. Review the diff before committing:\n'
printf '  git status --short docs/assets/screenshots\n'
