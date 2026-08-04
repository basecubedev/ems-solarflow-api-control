#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# Fast developer tier: deterministic unit and contract tests.
# No Docker daemon, no browser and no slow tests. See docs/developer/testing.md.
set -euo pipefail
cd "$(dirname "$0")/.."
# shellcheck source=scripts/lib/pytest-tier.sh
. scripts/lib/pytest-tier.sh

run_pytest_tier "fast" -q \
    -m "(unit or contract) and not docker and not browser and not slow" \
    --maxfail=1 \
    "$@"
