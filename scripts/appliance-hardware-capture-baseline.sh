#!/bin/sh
# SPDX-License-Identifier: AGPL-3.0-or-later
# Capture what this appliance looks like before a destructive hardware case.
#
#   scripts/appliance-hardware-capture-baseline.sh [--output DIR]
#
# Read-only. Runs on the Raspberry Pi under test, writes one directory of
# evidence, and touches nothing else: no block device is written, no selector
# is changed, no service is restarted, nothing reboots, and no SSH key is
# created, read or modified. It takes no device argument — every device it
# reports is discovered from the running system, so there is no path by which
# a mistyped argument reaches a block device.
#
# The pair to this is appliance-hardware-collect-evidence.sh, run after the
# case. Compare the two directories; the difference is the result.
#
# Exit status: 0 the baseline was captured, 3 this host cannot produce one.
set -eu

OUTPUT=""
while [ $# -gt 0 ]; do
    case "$1" in
        --output) OUTPUT=${2:?--output needs a directory}; shift 2 ;;
        --output=*) OUTPUT=${1#*=}; shift ;;
        -h|--help) sed -n '3,17p' "$0"; exit 0 ;;
        *) echo "unknown argument: $1" >&2; exit 2 ;;
    esac
done

not_run() {
    echo "appliance-hardware-capture-baseline: $1" >&2
    echo "RESULT: NOT RUN ($2)" >&2
    exit 3
}

command -v ems-appliance >/dev/null 2>&1 \
    || not_run "the appliance CLI is not installed" appliance_cli_unavailable

STAMP=$(date -u +%Y%m%dT%H%M%SZ)
[ -n "$OUTPUT" ] || OUTPUT="/var/log/ems-appliance-manager/hardware/baseline-$STAMP"
mkdir -p "$OUTPUT"

capture() {
    name=$1
    shift
    if "$@" >"$OUTPUT/$name" 2>"$OUTPUT/$name.err"; then
        [ -s "$OUTPUT/$name.err" ] || rm -f "$OUTPUT/$name.err"
    else
        printf 'NOT RUN: %s exited %s\n' "$1" "$?" >>"$OUTPUT/$name.err"
    fi
}

# The runtime's own view first: it is the authority the operator is testing.
capture ab-status.json ems-appliance ab status --json
capture verify-persistence.json ems-appliance ab verify-persistence --json
capture verify-install.json ems-appliance verify-install --json
capture host-identity.json ems-appliance host-identity --json

# Then the block and mount reality the runtime derived it from.
capture lsblk.json lsblk --json --output NAME,PATH,SIZE,TYPE,FSTYPE,LABEL,PARTLABEL,PARTUUID,UUID,MOUNTPOINTS
capture findmnt.json findmnt --json --all
capture by-slot.txt ls -l /dev/disk/by-slot
capture cmdline.txt cat /proc/cmdline
capture machine-id.txt cat /etc/machine-id
capture os-release.txt cat /etc/os-release

# The selector itself, byte for byte. A power-cut case is an argument about
# what reached this one file before the power went, and ab status reports the
# parsed result rather than the bytes a torn write left behind.
for mountpoint in /bootfs /boot/firmware /boot; do
    [ -f "$mountpoint/autoboot.txt" ] || continue
    capture autoboot.txt cat "$mountpoint/autoboot.txt"
    echo "$mountpoint/autoboot.txt" >"$OUTPUT/autoboot.source.txt"
    break
done

# Firmware's account of how this boot was selected. Absent off a Raspberry Pi.
for property in tryboot partition boot-mode; do
    path="/proc/device-tree/chosen/bootloader/$property"
    [ -e "$path" ] && capture "bootloader-$property.bin" cat "$path"
done

capture units.txt systemctl list-units --type=service --all --no-pager \
    --no-legend ems-appliance-\*
capture failed-units.txt systemctl --failed --no-pager --no-legend

# Public fingerprints only. The private keys are never read.
if [ -d /var/lib/ems-appliance-manager/ssh ]; then
    for key in /var/lib/ems-appliance-manager/ssh/*.pub; do
        [ -f "$key" ] && ssh-keygen -lf "$key"
    done >"$OUTPUT/ssh-fingerprints.txt" 2>/dev/null || true
fi

if command -v docker >/dev/null 2>&1; then
    capture docker-digests.txt docker ps --all \
        --format '{{.Names}} {{.Image}} {{.Status}}'
    capture docker-images.txt docker images --digests \
        --format '{{.Repository}}:{{.Tag}} {{.Digest}}'
fi

( cd "$OUTPUT" && find . -type f ! -name SHA256SUMS -print0 \
    | sort -z | xargs -0 sha256sum >SHA256SUMS )

echo "baseline: $OUTPUT"
echo "RESULT: PASS"
