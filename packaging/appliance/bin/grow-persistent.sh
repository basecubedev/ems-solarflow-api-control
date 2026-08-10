#!/bin/sh
# SPDX-License-Identifier: AGPL-3.0-or-later
# Grow the persistent partition to the medium, once, on a freshly imaged card.
#
# image-rota sizes the persistent partition at build time; the medium an
# operator imaged it onto is whatever they had. This is the only partition
# change the project makes.
#
# It lives here rather than in the appliance modules on purpose: no partitioning
# tool is reachable from the Python command runner, so no code path that handles
# a request can repartition anything. The marker keeps this to the first boot of
# a freshly imaged appliance; a running installation is never repartitioned.
#
# The marker means "this medium was grown and the result was verified". It used
# to mean "growth was attempted": growpart and resize2fs were both run with
# `|| true`, and the marker was written either way, so a card whose filesystem
# never grew was marked as finished and never tried again. Growth is therefore
# a transaction now — measure, mutate, verify, and only then record — and a
# failure or a power cut leaves no marker, so the next boot retries.
#
# "Already grown" is decided from real geometry, never from `disk - partition`:
# the persistent partition is the *last* of six, so subtracting its size from
# the disk's size counts the whole occupied prefix as free tail. A card grown to
# the end of a 32 GB medium looked several gigabytes short that way, and a power
# cut between growpart and the marker left a medium that retried on every boot,
# got NOCHANGE from growpart, and failed the boot path forever.
#
# Exit status: 0 the medium is grown (or already filled), 1 it is not and the
# next boot has to try again.
set -eu

# The test harness substitutes the tools and the persistent root; systemd
# supplies neither, so a booted appliance uses the paths below.
APPLIANCE=${EMS_GROW_APPLIANCE_BIN:-/usr/bin/ems-appliance}
PERSISTENT=${EMS_GROW_PERSISTENT_ROOT:-/persistent}
LAYOUT=${EMS_GROW_LAYOUT:-/etc/ems-appliance-manager/ab-layout.json}
MARKER="$PERSISTENT/.grown"

# resize2fs can only use whole blocks, so a filled filesystem is short by less
# than one block group.
FILESYSTEM_SLACK=$((1024 * 1024))

say() { echo "grow-persistent: $1"; }
fail() { echo "grow-persistent: $1" >&2; echo "RESULT: FAIL ($2)" >&2; exit 1; }

[ -f "$LAYOUT" ] || {
    say "this is not an A/B appliance image"
    exit 0
}
[ -f "$MARKER" ] && exit 0

GEOMETRY=""
GEOMETRY_FILE="${TMPDIR:-/tmp}/ems-grow-geometry.$$"
trap 'rm -f "$GEOMETRY_FILE"' EXIT

# One reader for every geometry field, so no caller re-derives a number from
# another number. Refreshed after growpart, because the point of the second read
# is to see whether the kernel adopted the new table at all.
read_geometry() {
    "$APPLIANCE" ab persistent-geometry > "$GEOMETRY_FILE" 2>/dev/null \
        || fail "the persistent partition geometry could not be measured" \
                persistent_geometry_unknown
    GEOMETRY=$GEOMETRY_FILE
}

geometry_field() {
    value=$(sed -n "s/^$1=//p" "$GEOMETRY" | tail -n 1)
    [ -n "$value" ] || fail "the geometry reports no $1" persistent_geometry_unknown
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

[ -b "$DEVICE" ] || fail "$DEVICE is not a block device" persistent_device_unknown

filesystem_bytes() {
    dumpe2fs -h "$1" 2>/dev/null | awk '
        /^Block count:/ { count = $3 }
        /^Block size:/  { size = $3 }
        END { if (count && size) print count * size; else print 0 }'
}

FILESYSTEM_BYTES=$(filesystem_bytes "$DEVICE")
[ "$FILESYSTEM_BYTES" -gt 0 ] \
    || fail "the filesystem on $DEVICE could not be measured" filesystem_size_unknown

say "disk=${DISK_BYTES} partition=${PARTITION_BYTES} tail=${TAIL_BYTES} filesystem=${FILESYSTEM_BYTES}"

filesystem_fills_partition() {
    [ $((PARTITION_BYTES - FILESYSTEM_BYTES)) -le "$FILESYSTEM_SLACK" ]
}

write_marker() {
    staged="$MARKER.staged"
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
    sync "$PERSISTENT" 2>/dev/null || sync
}

mountpoint -q "$PERSISTENT" \
    || fail "$PERSISTENT is not mounted; nothing was grown or marked" persistent_not_mounted

if [ "$FILLS_DISK" = yes ] && filesystem_fills_partition; then
    write_marker "$DISK_BYTES" "$PARTITION_BYTES" "$FILESYSTEM_BYTES" "$TAIL_BYTES" already_filled
    say "the medium is already filled; nothing to grow"
    exit 0
fi

if [ "$FILLS_DISK" != yes ]; then
    command -v growpart >/dev/null 2>&1 \
        || fail "growpart is not installed" growpart_unavailable
    set +e
    growpart "$DISK" "$NUMBER" >/dev/null 2>&1
    grow_status=$?
    set -e
    # growpart exits 2 for "no change necessary". Reached from here it is a
    # disagreement, not agreement: the geometry above already established that
    # the tail is larger than alignment slack. Marking would hand the operator a
    # persistent partition the size of the image on a medium several times
    # larger, silently, which is the whole defect class this helper exists for.
    [ "$grow_status" -eq 2 ] \
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
    resize2fs "$DEVICE" >/dev/null 2>&1 \
        || fail "resize2fs could not grow the filesystem on $DEVICE" resize2fs_failed

    FILESYSTEM_BYTES=$(filesystem_bytes "$DEVICE")
    [ "$FILESYSTEM_BYTES" -gt 0 ] \
        || fail "the grown filesystem could not be measured" filesystem_size_unknown
    filesystem_fills_partition \
        || fail "the filesystem is ${FILESYSTEM_BYTES} bytes of a ${PARTITION_BYTES}-byte partition" \
                filesystem_not_grown
    say "filesystem grown to ${FILESYSTEM_BYTES} bytes"
fi

write_marker "$DISK_BYTES" "$PARTITION_BYTES" "$FILESYSTEM_BYTES" "$TAIL_BYTES" grown
say "$DEVICE now fills the medium"
echo "RESULT: PASS"
