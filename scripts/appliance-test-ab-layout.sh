#!/bin/sh
# SPDX-License-Identifier: AGPL-3.0-or-later
# Run the A/B suites that need no hardware, and say what could not be run.
#
#   scripts/appliance-test-ab-layout.sh [--loop] [--image IMG]
#
# Without --loop this is the deterministic tier: the slot model, the layout
# authority, the selector parser, the release authority, the fake block-device
# failure matrix, the state machine and the boot-flow simulator. All of it runs
# unprivileged on a developer machine.
#
# --loop additionally runs the loop-device tier, which needs root and losetup.
# Where that is unavailable the tier reports NOT RUN. It is never skipped
# silently: a matrix that quietly dropped its destructive cases reads as
# "covered everything".
set -eu

ROOT=$(cd "$(dirname "$0")/.." && pwd)
. "$ROOT/scripts/lib/workdir.sh"
LOOP=0
IMAGE=""

SUITES="tests/test_appliance_ab_layout.py
tests/test_appliance_ab_persistence.py
tests/test_appliance_ab_selector.py
tests/test_appliance_ab_releases.py
tests/test_appliance_ab_update.py
tests/test_appliance_ab_blocks.py
tests/test_appliance_ab_health.py
tests/test_appliance_ab_boot_flow.py
tests/test_appliance_ab_image.py
tests/test_appliance_ab_api.py"

while [ $# -gt 0 ]; do
    case "$1" in
        --loop) LOOP=1; shift ;;
        --image) IMAGE=${2:?--image needs a file}; shift 2 ;;
        --image=*) IMAGE=${1#*=}; shift ;;
        -h|--help) sed -n '3,17p' "$0"; exit 0 ;;
        *) echo "unknown argument: $1" >&2; exit 2 ;;
    esac
done

cd "$ROOT"
TMPDIR=$(ems_work_dir ems-appliance-ab-layout) || exit 1
export TMPDIR
trap 'rm -rf "$TMPDIR"' EXIT

present=""
for suite in $SUITES; do
    [ -f "$suite" ] && present="$present $suite"
done
[ -n "$present" ] || { echo "no A/B suites found" >&2; exit 3; }

echo "== deterministic A/B tier =="
# shellcheck disable=SC2086
python3 -m pytest $present -q
status=$?

echo
if [ "$LOOP" -eq 1 ]; then
    if [ "$(id -u)" != "0" ] || ! command -v losetup >/dev/null 2>&1; then
        echo "loop-device tier: NOT RUN (needs root and losetup)"
    else
        echo "== loop-device tier =="
        EMS_APPLIANCE_AB_LOOP=1 python3 -m pytest \
            tests/test_appliance_ab_loop_devices.py -q || status=$?
    fi
else
    echo "loop-device tier: NOT RUN (pass --loop to include it)"
fi

if [ -n "$IMAGE" ]; then
    echo
    echo "== image inspection =="
    "$ROOT/scripts/appliance-inspect-rpi-ab-image.sh" "$IMAGE" || status=$?
else
    echo "image inspection: NOT RUN (pass --image to include it)"
fi

exit "$status"
