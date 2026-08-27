#!/bin/sh
# SPDX-License-Identifier: AGPL-3.0-or-later
# Collect the evidence a hardware case produced, and diff it against a baseline.
#
#   scripts/appliance-hardware-collect-evidence.sh [--baseline DIR] [--output DIR]
#
# Read-only, and the counterpart of appliance-hardware-capture-baseline.sh: it
# captures the same set of facts after a case and prints what changed. For a
# power-cut case that difference is the result -- whether the root came up,
# whether the growth marker survived, and whether the services started.
#
# Nothing is written to a block device, no selector is changed, nothing reboots
# and no SSH key is created, read or modified. Only public fingerprints and
# digests are recorded. No device argument is accepted.
#
# Exit status: 0 evidence collected, 3 this host cannot produce it. A difference
# against the baseline is reported, not judged: whether a change was the
# expected outcome depends on the case being run.
set -eu

BASELINE=""
OUTPUT=""
while [ $# -gt 0 ]; do
    case "$1" in
        --baseline) BASELINE=${2:?--baseline needs a directory}; shift 2 ;;
        --baseline=*) BASELINE=${1#*=}; shift ;;
        --output) OUTPUT=${2:?--output needs a directory}; shift 2 ;;
        --output=*) OUTPUT=${1#*=}; shift ;;
        -h|--help) sed -n '3,20p' "$0"; exit 0 ;;
        *) echo "unknown argument: $1" >&2; exit 2 ;;
    esac
done

HERE=$(cd "$(dirname "$0")" && pwd)
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
[ -n "$OUTPUT" ] || OUTPUT="/var/log/ems-appliance-manager/hardware/evidence-$STAMP"

"$HERE/appliance-hardware-capture-baseline.sh" --output "$OUTPUT" >/dev/null

echo "evidence: $OUTPUT"

if [ -z "$BASELINE" ]; then
    parent=$(dirname "$OUTPUT")
    BASELINE=$(ls -d "$parent"/baseline-* 2>/dev/null | tail -1 || true)
fi

if [ -z "$BASELINE" ] || [ ! -d "$BASELINE" ]; then
    echo "no baseline to compare against; this run is the record"
    echo "RESULT: PASS"
    exit 0
fi

echo "baseline: $BASELINE"
echo
echo "== what this case changed =="
changed=0
for file in verify-install.json root-geometry.txt lsblk.json findmnt.json \
            cmdline.txt machine-id.txt ssh-fingerprints.txt \
            docker-digests.txt units.txt failed-units.txt; do
    before="$BASELINE/$file"
    after="$OUTPUT/$file"
    [ -f "$before" ] || [ -f "$after" ] || continue
    if ! diff -q "$before" "$after" >/dev/null 2>&1; then
        changed=$((changed + 1))
        echo "--- $file"
        diff -u "$before" "$after" 2>&1 | sed -n '3,40p' || true
    fi
done
[ "$changed" -eq 0 ] && echo "nothing changed"

echo
echo "RESULT: PASS"
