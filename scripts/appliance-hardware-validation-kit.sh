#!/bin/sh
# SPDX-License-Identifier: AGPL-3.0-or-later
# Assemble the one directory an operator carries to the Raspberry Pi.
#
#   scripts/appliance-hardware-validation-kit.sh [--dist DIR] [--output DIR]
#                                                [--gate-report FILE]
#                                                [--attestation FILE]
#                                                [--keyring FILE]
#                                                [--trusted-fingerprint FPR]...
#                                                [--runtime-gates FILE]
#                                                [--source-authority FILE]
#                                                [--source-bundle FILE]
#                                                [--source-parity FILE]
#                                                [--profile rpi4|rpi5]...
#                                                [--development-kit]
#
# The kit is assembled from build authority, not from filename globs: each
# profile has exactly one completed build, and that record decides which image,
# update, manifest, signature and inspection report the kit is allowed to
# carry. Mixed builds, a missing signature and a missing release-gate report are
# failures rather than a smaller kit. scripts/appliance_hardware_kit.py does
# that work; this adds the expected slot layout and the checklist an operator
# reads beside it.
#
# --development-kit assembles whatever exists for a bench and reports
# INCOMPLETE with physical_ready=false. It can never report READY.
#
# No private signing key is ever copied. The kit carries public material only.
#
# Exit status: 0 the kit is authoritative and complete, 1 it is not, 2 the
# command line is wrong, 3 there is nothing to assemble.
set -eu

ROOT=$(cd "$(dirname "$0")/.." && pwd)
DIST="$ROOT/dist"
OUTPUT="$ROOT/dist/hardware-validation"
CHECKLIST="$ROOT/docs/appliance/ab-hardware-validation.md"
ARGS=""

usage() { sed -n '3,31p' "$0"; }

while [ $# -gt 0 ]; do
    case "$1" in
        --dist) DIST=${2:?--dist needs a directory}; shift 2 ;;
        --output) OUTPUT=${2:?--output needs a directory}; shift 2 ;;
        --gate-report) ARGS="$ARGS --gate-report $2"; shift 2 ;;
        --attestation) ARGS="$ARGS --attestation $2"; shift 2 ;;
        --keyring) ARGS="$ARGS --keyring $2"; shift 2 ;;
        --trusted-fingerprint) ARGS="$ARGS --trusted-fingerprint $2"; shift 2 ;;
        --runtime-gates) ARGS="$ARGS --runtime-gates $2"; shift 2 ;;
        --source-authority) ARGS="$ARGS --source-authority $2"; shift 2 ;;
        --source-bundle) ARGS="$ARGS --source-bundle $2"; shift 2 ;;
        --source-parity) ARGS="$ARGS --source-parity $2"; shift 2 ;;
        --profile) ARGS="$ARGS --profile $2"; shift 2 ;;
        --development-kit) ARGS="$ARGS --development-kit"; shift ;;
        -h|--help) usage; exit 0 ;;
        *) echo "unknown argument: $1" >&2; usage >&2; exit 2 ;;
    esac
done

set +e
# shellcheck disable=SC2086
python3 "$ROOT/scripts/appliance_hardware_kit.py" --dist "$DIST" --output "$OUTPUT" \
    --checklist "$CHECKLIST" $ARGS
status=$?
set -e
[ -d "$OUTPUT" ] || exit "$status"

PYTHONPATH="$ROOT" python3 - "$OUTPUT/expected-slot-layout.txt" <<'PY'
import sys

from appliance import ab_persistence
from appliance.rpi_image_gen import read_lock

lock = read_lock()
with open(sys.argv[1], "w", encoding="utf-8") as handle:
    handle.write("# What a flashed appliance must look like.\n")
    handle.write("# Generated from the pinned lock and the persistence contract.\n\n")
    handle.write("partitions (GPT labels, identities are generated per build):\n")
    handle.write(f"  1 {lock.partition_labels['bootconfig']}   vfat  {lock.bootconfig_mountpoint}\n")
    for index, label in enumerate(lock.partition_labels["boot"], start=2):
        handle.write(f"  {index} {label}       vfat  {lock.boot_mountpoint} when that slot is active\n")
    for index, label in enumerate(lock.partition_labels["system"], start=4):
        handle.write(f"  {index} {label}     ext4  / when that slot is active, read-only\n")
    handle.write(f"  6 {lock.partition_labels['persistent']}   ext4  {lock.persistent_mountpoint}\n\n")
    handle.write(f"slot aliases:      {lock.slot_device_prefix}/\n")
    handle.write(f"shared root:       {lock.shared_root}\n")
    handle.write(f"machine identity:  {lock.machine_id_source}\n")
    handle.write(f"update archive:    {lock.update_archive} members {', '.join(lock.update_members)}\n")
    handle.write(f"member encoding:   {lock.update_member_format}\n\n")
    handle.write(
        f"shared paths, all {len(ab_persistence.SHARED_PATHS)} of which must be "
        "bound from the persistent partition:\n"
    )
    for shared in ab_persistence.SHARED_PATHS:
        handle.write(f"  {shared.target}\n")
    handle.write("\nactivation links the image ships (upstream links only the last):\n")
    for link, target in sorted(ab_persistence.activation_links().items()):
        handle.write(f"  {link} -> {target}\n")
PY

# The layout file is written after the kit hashed itself, so the checksum list
# is completed here rather than left describing a directory that has changed.
( cd "$OUTPUT" && find . -type f ! -name KIT-SHA256SUMS -print0 \
    | sort -z | xargs -0 sha256sum >KIT-SHA256SUMS )

exit "$status"
