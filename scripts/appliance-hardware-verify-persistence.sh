#!/bin/sh
# SPDX-License-Identifier: AGPL-3.0-or-later
# Report whether every shared path on this Pi is really shared.
#
#   scripts/appliance-hardware-verify-persistence.sh [--json]
#
# Read-only. This is the check that catches upstream's fail-open bind: a shared
# path whose generator skipped it looks healthy until the next slot switch
# discards every write made to it. Run it before and after each slot switch.
#
# Nothing is mounted, remounted, repaired or written. No device argument is
# accepted; the paths and the partition come from the layout descriptor the
# image wrote.
#
# Exit status: 0 the persistence contract holds, 1 it does not, 3 this host
# cannot answer.
set -eu

FORMAT=text
while [ $# -gt 0 ]; do
    case "$1" in
        --json) FORMAT=json; shift ;;
        -h|--help) sed -n '3,16p' "$0"; exit 0 ;;
        *) echo "unknown argument: $1" >&2; exit 2 ;;
    esac
done

not_run() {
    echo "appliance-hardware-verify-persistence: $1" >&2
    echo "RESULT: NOT RUN ($2)" >&2
    exit 3
}

command -v ems-appliance >/dev/null 2>&1 \
    || not_run "the appliance CLI is not installed" appliance_cli_unavailable

if [ "$FORMAT" = json ]; then
    ems-appliance ab verify-persistence --json
    exit $?
fi

REPORT=$(ems-appliance ab verify-persistence --json 2>/dev/null) \
    || not_run "the runtime could not report its persistence state" verify_unavailable

python3 - "$REPORT" <<'PY'
import json
import sys

report = json.loads(sys.argv[1])
print(f"state:      {report.get('state')}")
print(f"mountpoint: {report.get('mountpoint')}")
print(f"source:     {report.get('source')}  (expected {report.get('expected_source')})")
print(f"schema:     {report.get('schema_version')}")

paths = report.get("paths") or []
print(f"shared paths: {sum(1 for entry in paths if entry.get('shared'))}/{len(paths)}")
for entry in paths:
    mark = "ok  " if entry.get("shared") else "FAIL"
    print(f"  {mark} {entry.get('target')}")
    if entry.get("problem"):
        print(f"       {entry['problem']}")

problems = report.get("problems") or []
for problem in problems:
    print(f"problem: {problem}", file=sys.stderr)

ok = report.get("state") == "ok" and not problems
print("RESULT: PASS" if ok else "RESULT: FAIL")
sys.exit(0 if ok else 1)
PY
