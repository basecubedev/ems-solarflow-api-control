#!/bin/sh
# SPDX-License-Identifier: AGPL-3.0-or-later
# Grow the root filesystem to the medium, once, on a freshly imaged card.
#
# The image sizes its root at build time; the medium an operator flashed it
# onto is whatever they had. The order is deliberate: measure, mutate, verify,
# and only then record. A failure or a power cut leaves no marker, so the next
# boot retries.
#
# It runs on media this project imaged and on nothing else. A .deb installation
# on somebody else's Raspberry Pi OS carries no appliance build marker, and
# docs/appliance/installation.md promises that this appliance never resizes,
# moves or repartitions a running installation's storage. That promise is kept
# here, by refusing to act without a positive statement that this medium was
# written from an appliance image.
#
# It lives here rather than in the appliance modules on purpose: no partitioning
# tool is reachable from the Python command runner, so no code path that handles
# a request can repartition anything.
#
# Exit status: 0 the medium is grown, already filled, or not ours to touch;
# 1 it is ours, it is not grown, and the next boot has to try again.
set -eu

# The test harness substitutes the tools and the marker root; systemd supplies
# neither, so a booted appliance uses the paths below.
APPLIANCE=${EMS_GROW_APPLIANCE_BIN:-/usr/bin/ems-appliance}
STATE=${EMS_GROW_ROOT_STATE:-/var/lib/ems-appliance-manager}
MARKER="$STATE/.root-grown"

# `ems-appliance image-check` answers 3 for "not written from an appliance
# image"; any other refusal means the check itself could not run.
IMAGE_CHECK_NOT_OURS=3

say() { echo "grow-root: $1"; }
fail() { echo "grow-root: $1" >&2; echo "RESULT: FAIL ($2)" >&2; exit 1; }

[ -f "$MARKER" ] && exit 0

# The one gate that keeps this off media this project did not write. Only a
# positive answer counts: no marker, an unreadable one, or one naming any other
# image all leave the medium alone.
set +e
"$APPLIANCE" image-check >/dev/null 2>&1
image_check_status=$?
set -e
if [ "$image_check_status" -eq "$IMAGE_CHECK_NOT_OURS" ]; then
    say "this medium was not written from an appliance image"
    exit 0
fi
[ "$image_check_status" -eq 0 ] \
    || fail "ems-appliance image-check could not run (exit $image_check_status)" image_check_failed

GEOMETRY=""
GEOMETRY_FILE="${TMPDIR:-/tmp}/ems-grow-root-geometry.$$"
trap 'rm -f "$GEOMETRY_FILE"' EXIT

# One reader for every geometry field, so no caller re-derives a number from
# another number. Refreshed after growpart, because the point of the second read
# is to see whether the kernel adopted the new table at all.
read_geometry() {
    "$APPLIANCE" root-geometry > "$GEOMETRY_FILE" 2>/dev/null \
        || fail "the root partition geometry could not be measured" root_geometry_unknown
    GEOMETRY=$GEOMETRY_FILE
}

geometry_field() {
    value=$(sed -n "s/^$1=//p" "$GEOMETRY" | tail -n 1)
    [ -n "$value" ] || fail "the geometry reports no $1" root_geometry_unknown
    printf '%s' "$value"
}

read_geometry
DEVICE=$(geometry_field device)
DISK=$(geometry_field disk)
NUMBER=$(geometry_field number)
PARTITION_BYTES=$(geometry_field size_bytes)
DISK_BYTES=$(geometry_field disk_bytes)
TAIL_BYTES=$(geometry_field tail_bytes)
FILLS_DISK=$(geometry_field fills_disk)

[ -b "$DEVICE" ] || fail "$DEVICE is not a block device" root_device_unknown

# The filesystem's size, and how far short of its partition a filled one may
# still end. resize2fs drops a trailing piece of a group too small to carry the
# group's own metadata (resize2fs's adjust_fs_info: less than its overhead plus
# 50 blocks), so a grown filesystem can end a few mebibytes short, and more
# with the Pi 5's 16 KiB blocks. Without the fields that bound it, only the
# alignment grain counts.
measure_filesystem() {
    dumpe2fs -h "$1" 2>/dev/null | awk '
        /^Block count:/            { count = $3 }
        /^Block size:/             { size = $3 }
        /^Blocks per group:/       { per_group = $4 }
        /^Inode blocks per group:/ { inode_blocks = $5 }
        /^Reserved GDT blocks:/    { reserved = $4 }
        /^Group descriptor size:/  { descriptor = $4 }
        END {
            if (!(count && size)) { print "0 0"; exit }
            slack = 1024 * 1024
            if (per_group && inode_blocks) {
                if (!descriptor) descriptor = 32
                groups = int((count + per_group - 1) / per_group)
                descriptor_blocks = int((groups * descriptor + size - 1) / size)
                overhead = 3 + inode_blocks + descriptor_blocks + reserved + 50
                if ((overhead + 1) * size > slack) slack = (overhead + 1) * size
            }
            printf "%.0f %.0f\n", count * size, slack
        }'
}

read_filesystem() {
    measured=$(measure_filesystem "$DEVICE")
    FILESYSTEM_BYTES=${measured% *}
    FILESYSTEM_SLACK=${measured#* }
    [ "$FILESYSTEM_BYTES" -gt 0 ] \
        || fail "the filesystem on $DEVICE could not be measured" filesystem_size_unknown
}

read_filesystem

say "disk=${DISK_BYTES} partition=${PARTITION_BYTES} tail=${TAIL_BYTES} filesystem=${FILESYSTEM_BYTES}"

filesystem_fills_partition() {
    [ $((PARTITION_BYTES - FILESYSTEM_BYTES)) -le "$FILESYSTEM_SLACK" ]
}

write_marker() {
    staged="$MARKER.staged"
    mkdir -p "$STATE" || fail "$STATE could not be created" marker_unwritable
    {
        printf 'grown_at=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || echo unknown)"
        printf 'disk_bytes=%s\n' "$1"
        printf 'partition_bytes=%s\n' "$2"
        printf 'filesystem_bytes=%s\n' "$3"
        printf 'tail_bytes=%s\n' "$4"
        printf 'outcome=%s\n' "$5"
    } > "$staged" || fail "the marker could not be staged" marker_unwritable
    # The marker is the record that this medium will never be grown again, so
    # it has to reach the medium before the rename that publishes it, and the
    # rename has to reach the medium before this boot continues.
    sync "$staged" 2>/dev/null || sync
    mv "$staged" "$MARKER" || fail "the marker could not be published" marker_unwritable
    sync "$STATE" 2>/dev/null || sync
}

if [ "$FILLS_DISK" = yes ] && filesystem_fills_partition; then
    write_marker "$DISK_BYTES" "$PARTITION_BYTES" "$FILESYSTEM_BYTES" "$TAIL_BYTES" already_filled
    say "the medium is already filled; nothing to grow"
    exit 0
fi

if [ "$FILLS_DISK" != yes ]; then
    command -v growpart >/dev/null 2>&1 \
        || fail "growpart is not installed" growpart_unavailable
    set +e
    grow_output=$(growpart "$DISK" "$NUMBER" 2>&1)
    grow_status=$?
    set -e
    # growpart's own NOCHANGE/FAILED line is the only account of why.
    printf '%s\n' "$grow_output" | sed '/^$/d; s/^/grow-root: growpart: /'
    # growpart exits 1 for NOCHANGE and 2 for FAILED. NOCHANGE reached from
    # here is a disagreement, not agreement: the geometry above already
    # established that the tail is larger than alignment slack. Marking would
    # hand the operator a root partition the size of the image on a medium
    # several times larger, silently, which is the whole defect class this
    # helper exists for.
    [ "$grow_status" -eq 1 ] \
        && fail "growpart refuses to grow partition $NUMBER of $DISK into ${TAIL_BYTES} unused bytes" \
                growpart_refused
    [ "$grow_status" -eq 0 ] \
        || fail "growpart could not grow partition $NUMBER of $DISK" growpart_failed

    PREVIOUS_PARTITION_BYTES=$PARTITION_BYTES
    read_geometry
    PARTITION_BYTES=$(geometry_field size_bytes)
    TAIL_BYTES=$(geometry_field tail_bytes)
    FILLS_DISK=$(geometry_field fills_disk)
    # growpart rewrites the table; until the kernel has re-read it, resize2fs
    # would grow the filesystem to the size it can still see.
    [ "$PARTITION_BYTES" -gt "$PREVIOUS_PARTITION_BYTES" ] \
        || fail "the kernel still reports $DEVICE as ${PARTITION_BYTES} bytes" \
                partition_table_not_reread
    [ "$FILLS_DISK" = yes ] \
        || fail "$DEVICE still leaves ${TAIL_BYTES} bytes of the medium unused" \
                partition_not_grown
    say "partition grown to ${PARTITION_BYTES} bytes"
fi

if ! filesystem_fills_partition; then
    command -v resize2fs >/dev/null 2>&1 \
        || fail "resize2fs is not installed" resize2fs_unavailable
    # The root is mounted and stays mounted: ext4 grows online, and unmounting
    # the filesystem this script is running from is not an option.
    resize2fs "$DEVICE" >/dev/null 2>&1 \
        || fail "resize2fs could not grow the filesystem on $DEVICE" resize2fs_failed

    read_filesystem
    filesystem_fills_partition \
        || fail "the filesystem is ${FILESYSTEM_BYTES} bytes of a ${PARTITION_BYTES}-byte partition" \
                filesystem_not_grown
    say "filesystem grown to ${FILESYSTEM_BYTES} bytes"
fi

write_marker "$DISK_BYTES" "$PARTITION_BYTES" "$FILESYSTEM_BYTES" "$TAIL_BYTES" grown
say "$DEVICE now fills the medium"
echo "RESULT: PASS"
