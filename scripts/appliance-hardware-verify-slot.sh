#!/bin/sh
# SPDX-License-Identifier: AGPL-3.0-or-later
# Report which slot this Raspberry Pi actually booted, and from what.
#
#   scripts/appliance-hardware-verify-slot.sh [--json]
#
# Read-only. Nothing is written to a block device, the selector is not touched,
# no service is started or stopped and nothing reboots. Changing the slot is a
# separate, explicitly destructive command; this only reports.
#
# It takes no device argument. The active slot, its partitions and the selector
# are discovered through the runtime's own authority, so a hardware run cannot
# be pointed at the wrong disk by a typo.
#
# Exit status: 0 the slot is consistent, 1 it is not, 3 this host cannot answer.
set -eu

FORMAT=text
while [ $# -gt 0 ]; do
    case "$1" in
        --json) FORMAT=json; shift ;;
        -h|--help) sed -n '3,15p' "$0"; exit 0 ;;
        *) echo "unknown argument: $1" >&2; exit 2 ;;
    esac
done

not_run() {
    echo "appliance-hardware-verify-slot: $1" >&2
    echo "RESULT: NOT RUN ($2)" >&2
    exit 3
}

command -v ems-appliance >/dev/null 2>&1 \
    || not_run "the appliance CLI is not installed" appliance_cli_unavailable

STATUS=$(ems-appliance ab status --json 2>/dev/null) \
    || not_run "the runtime could not report its A/B status" ab_status_unavailable

if [ "$FORMAT" = json ]; then
    printf '%s\n' "$STATUS"
    exit 0
fi

python3 - "$STATUS" <<'PY'
import json
import sys

status = json.loads(sys.argv[1])
problems = []

mode = status.get("mode")
active = status.get("active_slot")
inactive = status.get("inactive_slot")

print(f"mode:          {mode}")
print(f"ab supported:  {status.get('ab_supported')}  ({status.get('reason') or 'ok'})")
print(f"active slot:   {active}")
print(f"inactive slot: {inactive}")
print(f"tryboot:       {status.get('tryboot')}")

selector = status.get("selector") or {}
if selector:
    print("selector:")
    for key in sorted(selector):
        print(f"  {key}: {selector[key]}")

state = status.get("ab_state") or {}
if state:
    print("operation state:")
    for key in ("operation", "phase", "pending_slot", "known_good_slot"):
        if key in state:
            print(f"  {key}: {state[key]}")

if mode != "single_slot":
    if not active:
        problems.append("the runtime reports no active slot")
    if active and active == inactive:
        problems.append(f"active and inactive slot are both {active}")
    if status.get("ab_supported") is False:
        problems.append(f"A/B is unsupported here: {status.get('reason')}")

for problem in problems:
    print(f"problem: {problem}", file=sys.stderr)
print("RESULT: FAIL" if problems else "RESULT: PASS")
sys.exit(1 if problems else 0)
PY
