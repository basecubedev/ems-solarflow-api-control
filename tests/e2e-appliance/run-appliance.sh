#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Start the deterministic Appliance Manager test server for Playwright.
set -euo pipefail
cd "$(dirname "$0")/../.."
export EMS_APPLIANCE_E2E_PORT="${EMS_APPLIANCE_E2E_PORT:-8124}"
export EMS_APPLIANCE_TEST_MODE=1
# The package data an installed appliance reads from /usr/share. Pointing at
# the packaging tree keeps the browser driving the same shipped hash the
# postinst would have set, rather than a copy invented for the harness.
export EMS_APPLIANCE_DATADIR="$PWD/packaging/appliance/config"
exec python3 tests/e2e-appliance/serve_appliance_test.py
