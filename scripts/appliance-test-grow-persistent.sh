#!/bin/sh
# SPDX-License-Identifier: AGPL-3.0-or-later
# Exercise the first-boot growth against real growpart, resize2fs and a real
# kernel partition table.
#
#   scripts/appliance-test-grow-persistent.sh [--work DIR]
#
# The unit tier substitutes the partitioning tools, because repartitioning a
# medium is not something a unit test may do. That leaves the two things only a
# real run can answer: whether the geometry this project reads out of sysfs and
# the GPT agrees with what growpart actually does to a table, and whether the
# kernel has re-read that table by the time resize2fs runs.
#
# Everything happens inside a disposable image file this script creates and a
# loop device it attaches to that file. It refuses to touch a block device it
# did not create. Never point it at a medium that matters.
#
# The failure cases shadow one tool with a failing stub: real growpart cannot be
# made to fail on demand, and "the marker survives a tool that failed" is the
# property under test, not the tool. Every success path runs the real tools.
#
# Exit status: 0 every case behaved, 1 one did not, 3 the environment cannot run
# this tier (not root, or a required tool is missing).
set -eu

ROOT=$(cd "$(dirname "$0")/.." && pwd)
SCRIPT="$ROOT/packaging/appliance/bin/grow-persistent.sh"
WORK=""
LOOP=""
MOUNTPOINT=""
FAILURES=0
CASES=0

MIB=$((1024 * 1024))
IMAGE_MIB=512
PERSIST_START_MIB=209
PERSIST_SIZE_MIB=64

usage() { sed -n '3,24p' "$0"; }

not_run() {
    echo "appliance-test-grow-persistent: $1" >&2
    echo "RESULT: NOT RUN ($2)" >&2
    exit 3
}

while [ $# -gt 0 ]; do
    case "$1" in
        --work) WORK=${2:?--work needs a directory}; shift 2 ;;
        --work=*) WORK=${1#*=}; shift ;;
        -h|--help) usage; exit 0 ;;
        *) echo "unknown argument: $1" >&2; usage >&2; exit 2 ;;
    esac
done

[ "$(id -u)" = "0" ] || not_run "loop devices and mounts need root" not_root
for tool in losetup sfdisk mkfs.ext4 resize2fs dumpe2fs growpart python3 mountpoint; do
    command -v "$tool" >/dev/null 2>&1 || not_run "$tool is not installed" "${tool}_unavailable"
done
[ -e /dev/loop-control ] || not_run "no /dev/loop-control" loop_unavailable

[ -n "$WORK" ] || WORK=$(mktemp -d "${TMPDIR:-/tmp}/ems-grow-real.XXXXXX")
mkdir -p "$WORK"
IMAGE="$WORK/medium.img"

detach() {
    if [ -n "$MOUNTPOINT" ] && mountpoint -q "$MOUNTPOINT" 2>/dev/null; then
        umount "$MOUNTPOINT" || true
    fi
    MOUNTPOINT=""
    if [ -n "$LOOP" ]; then
        losetup -d "$LOOP" 2>/dev/null || true
        LOOP=""
    fi
}

cleanup() {
    detach
    rm -rf "$WORK"
}
trap cleanup EXIT

say() { echo "-- $1"; }

check() {
    CASES=$((CASES + 1))
    if [ "$1" = ok ]; then
        printf '   %-58s PASS\n' "$2"
    else
        printf '   %-58s FAIL  %s\n' "$2" "$3"
        FAILURES=$((FAILURES + 1))
    fi
}

# --- the disposable medium ---------------------------------------------------

# Six partitions, the last one persistent, exactly the shape image-rota writes:
# the occupied prefix in front of the persistent partition is what made
# `disk_bytes - partition_bytes` the wrong question.
make_medium() {
    persist_size=$1
    detach
    rm -f "$IMAGE"
    truncate -s "$((IMAGE_MIB * MIB))" "$IMAGE"
    sfdisk --quiet --label gpt "$IMAGE" >/dev/null <<EOF
1MiB,16MiB,,,
,32MiB,,,
,32MiB,,,
,64MiB,,,
,64MiB,,,
${PERSIST_START_MIB}MiB,${persist_size},,,
EOF
    LOOP=$(losetup --show --find --partscan "$IMAGE")
    ensure_partition_nodes
    [ -b "${LOOP}p6" ] || { echo "the kernel did not present ${LOOP}p6" >&2; exit 1; }
    mkfs.ext4 -q -F "${LOOP}p6" >/dev/null 2>&1
}

# The kernel creates the partitions; udev creates their device nodes. A
# disposable container has no udev, so the nodes are made from what the block
# layer already reports rather than from a guessed numbering.
ensure_partition_nodes() {
    name=${LOOP#/dev/}
    for entry in "/sys/class/block/$name"p*; do
        [ -d "$entry" ] || continue
        node="/dev/$(basename "$entry")"
        [ -b "$node" ] && continue
        devnum=$(cat "$entry/dev" 2>/dev/null || echo "")
        [ -n "$devnum" ] || continue
        mknod "$node" b "${devnum%%:*}" "${devnum##*:}" 2>/dev/null || true
    done
}

mount_persistent() {
    MOUNTPOINT="$WORK/persistent"
    mkdir -p "$MOUNTPOINT"
    mount "${LOOP}p6" "$MOUNTPOINT"
    rm -f "$MOUNTPOINT/.grown"
}

# The geometry the helper acts on is read by the production module against the
# real kernel and the real GPT on the loop device. Only layout *discovery* is
# short-circuited here; the numbers are not.
make_appliance_stub() {
    mkdir -p "$WORK/bin"
    cat > "$WORK/bin/ems-appliance" <<EOF
#!/bin/sh
case "\$2" in
  persistent-device) echo "${LOOP}p6" ;;
  persistent-geometry)
    PYTHONPATH="$ROOT" python3 -c '
import sys
from appliance import ab_geometry
try:
    geometry = ab_geometry.read_geometry(sys.argv[1])
except ab_geometry.GeometryError as error:
    sys.exit(error.code)
print(chr(10).join(geometry.to_lines()))
' "${LOOP}p6" ;;
  *) exit 1 ;;
esac
EOF
    chmod 0755 "$WORK/bin/ems-appliance"
    printf '{}' > "$WORK/ab-layout.json"
}

shadow_tool() {
    cat > "$WORK/bin/$1" <<EOF
#!/bin/sh
exit ${2:-1}
EOF
    chmod 0755 "$WORK/bin/$1"
}

unshadow_tool() { rm -f "$WORK/bin/$1"; }

run_helper() {
    set +e
    EMS_GROW_APPLIANCE_BIN="$WORK/bin/ems-appliance" \
    EMS_GROW_PERSISTENT_ROOT="$MOUNTPOINT" \
    EMS_GROW_LAYOUT="$WORK/ab-layout.json" \
    PATH="$WORK/bin:$PATH" \
        sh "$SCRIPT" > "$WORK/out.log" 2> "$WORK/err.log"
    HELPER_STATUS=$?
    set -e
    return 0
}

partition_bytes() { blockdev --getsize64 "${LOOP}p6"; }

filesystem_bytes() {
    dumpe2fs -h "${LOOP}p6" 2>/dev/null | awk '
        /^Block count:/ { count = $3 }
        /^Block size:/  { size = $3 }
        END { if (count && size) print count * size; else print 0 }'
}

marker_field() { sed -n "s/^$1=//p" "$MOUNTPOINT/.grown" 2>/dev/null | tail -n 1; }

# --- the cases ---------------------------------------------------------------

echo "== a freshly imaged medium with an unused tail =="
make_medium "${PERSIST_SIZE_MIB}MiB"
make_appliance_stub
mount_persistent
before_partition=$(partition_bytes)
before_filesystem=$(filesystem_bytes)
run_helper
after_partition=$(partition_bytes)
after_filesystem=$(filesystem_bytes)
say "partition ${before_partition} -> ${after_partition}, filesystem ${before_filesystem} -> ${after_filesystem}"

[ "$HELPER_STATUS" -eq 0 ] \
    && check ok "real growpart and resize2fs grow the medium" \
    || check no "real growpart and resize2fs grow the medium" "exit $HELPER_STATUS: $(cat "$WORK/err.log")"
[ "$after_partition" -gt "$before_partition" ] \
    && check ok "the kernel re-read the grown partition table" \
    || check no "the kernel re-read the grown partition table" "still ${after_partition}"
[ "$after_filesystem" -gt "$before_filesystem" ] \
    && check ok "resize2fs grew the real filesystem" \
    || check no "resize2fs grew the real filesystem" "still ${after_filesystem}"
[ $((after_partition - after_filesystem)) -le "$MIB" ] \
    && check ok "the filesystem fills the grown partition" \
    || check no "the filesystem fills the grown partition" "$((after_partition - after_filesystem)) bytes short"
[ "$(marker_field outcome)" = grown ] \
    && check ok "the marker records a completed growth" \
    || check no "the marker records a completed growth" "outcome=$(marker_field outcome)"
GROWN_PARTITION=$after_partition

echo
echo "== the same medium on a later boot =="
rm -f "$MOUNTPOINT/.grown"
run_helper
[ "$HELPER_STATUS" -eq 0 ] \
    && check ok "an already grown medium is recognised, not repartitioned" \
    || check no "an already grown medium is recognised, not repartitioned" \
              "exit $HELPER_STATUS: $(cat "$WORK/err.log")"
[ "$(marker_field outcome)" = already_filled ] \
    && check ok "it is marked already_filled" \
    || check no "it is marked already_filled" "outcome=$(marker_field outcome)"
[ "$(partition_bytes)" = "$GROWN_PARTITION" ] \
    && check ok "the partition was not touched again" \
    || check no "the partition was not touched again" "$(partition_bytes)"
tail_bytes=$(marker_field tail_bytes)
[ -n "$tail_bytes" ] && [ "$tail_bytes" -lt $((2 * MIB)) ] \
    && check ok "the recorded tail is alignment, not the occupied prefix" \
    || check no "the recorded tail is alignment, not the occupied prefix" "tail_bytes=$tail_bytes"

echo
echo "== a medium built already full =="
detach
make_medium "$((IMAGE_MIB - PERSIST_START_MIB - 1))MiB"
make_appliance_stub
mount_persistent
run_helper
[ "$HELPER_STATUS" -eq 0 ] \
    && check ok "a medium imaged at full size needs no growth" \
    || check no "a medium imaged at full size needs no growth" \
              "exit $HELPER_STATUS: $(cat "$WORK/err.log")"
[ "$(marker_field outcome)" = already_filled ] \
    && check ok "and is marked already_filled" \
    || check no "and is marked already_filled" "outcome=$(marker_field outcome)"

echo
echo "== growpart fails =="
detach
make_medium "${PERSIST_SIZE_MIB}MiB"
make_appliance_stub
mount_persistent
shadow_tool growpart 1
run_helper
unshadow_tool growpart
grep -q growpart_failed "$WORK/err.log" \
    && check ok "a failed growpart is reported" \
    || check no "a failed growpart is reported" "$(cat "$WORK/err.log")"
[ ! -f "$MOUNTPOINT/.grown" ] \
    && check ok "and leaves no marker" \
    || check no "and leaves no marker" "the marker exists"

echo
echo "== growpart refuses a tail it will not take =="
shadow_tool growpart 2
run_helper
unshadow_tool growpart
grep -q growpart_refused "$WORK/err.log" \
    && check ok "NOCHANGE on an ungrown medium is a refusal, not a pass" \
    || check no "NOCHANGE on an ungrown medium is a refusal, not a pass" "$(cat "$WORK/err.log")"
[ ! -f "$MOUNTPOINT/.grown" ] \
    && check ok "and leaves no marker" \
    || check no "and leaves no marker" "the marker exists"

echo
echo "== power loss after growpart, before resize2fs =="
shadow_tool resize2fs 1
run_helper
unshadow_tool resize2fs
partial_partition=$(partition_bytes)
partial_filesystem=$(filesystem_bytes)
say "partition ${partial_partition}, filesystem ${partial_filesystem}"
grep -q resize2fs_failed "$WORK/err.log" \
    && check ok "a failed resize2fs is reported" \
    || check no "a failed resize2fs is reported" "$(cat "$WORK/err.log")"
[ ! -f "$MOUNTPOINT/.grown" ] \
    && check ok "a half-grown medium is never marked" \
    || check no "a half-grown medium is never marked" "the marker exists"
[ "$partial_partition" -gt "$partial_filesystem" ] \
    && check ok "the partition really is ahead of the filesystem" \
    || check no "the partition really is ahead of the filesystem" \
              "$partial_partition vs $partial_filesystem"

echo
echo "== the next boot retries the partial growth =="
run_helper
[ "$HELPER_STATUS" -eq 0 ] \
    && check ok "the retry completes with real resize2fs" \
    || check no "the retry completes with real resize2fs" \
              "exit $HELPER_STATUS: $(cat "$WORK/err.log")"
[ $(($(partition_bytes) - $(filesystem_bytes))) -le "$MIB" ] \
    && check ok "and the filesystem now fills the partition" \
    || check no "and the filesystem now fills the partition" \
              "$(($(partition_bytes) - $(filesystem_bytes))) bytes short"
[ -f "$MOUNTPOINT/.grown" ] \
    && check ok "only now is the marker written" \
    || check no "only now is the marker written" "no marker"

echo
echo "== power loss after resize2fs, before the marker =="
rm -f "$MOUNTPOINT/.grown"
run_helper
[ "$HELPER_STATUS" -eq 0 ] \
    && check ok "the boot after the cut completes without repartitioning" \
    || check no "the boot after the cut completes without repartitioning" \
              "exit $HELPER_STATUS: $(cat "$WORK/err.log")"
[ "$(marker_field outcome)" = already_filled ] \
    && check ok "and records what it actually found" \
    || check no "and records what it actually found" "outcome=$(marker_field outcome)"

echo
echo "== the marker cannot be written =="
rm -f "$MOUNTPOINT/.grown"
mount -o remount,ro "$MOUNTPOINT"
run_helper
mount -o remount,rw "$MOUNTPOINT"
[ "$HELPER_STATUS" -ne 0 ] \
    && check ok "a marker that cannot be staged fails the run" \
    || check no "a marker that cannot be staged fails the run" "exit 0"
grep -q marker_unwritable "$WORK/err.log" \
    && check ok "with the reason named" \
    || check no "with the reason named" "$(cat "$WORK/err.log")"
[ ! -f "$MOUNTPOINT/.grown" ] && [ ! -f "$MOUNTPOINT/.grown.staged" ] \
    && check ok "and nothing is left staged" \
    || check no "and nothing is left staged" "a marker file remains"

echo
echo "== the marker is published by rename and flushed =="
run_helper
[ -f "$MOUNTPOINT/.grown" ] \
    && check ok "the marker is back after the medium became writable" \
    || check no "the marker is back after the medium became writable" "no marker"
# Whether the bytes reached the medium, asked of the medium rather than of the
# page cache the writer just used.
umount "$MOUNTPOINT"
mount "${LOOP}p6" "$MOUNTPOINT"
[ -f "$MOUNTPOINT/.grown" ] && [ -n "$(marker_field outcome)" ] \
    && check ok "and survives a remount, so it reached the medium" \
    || check no "and survives a remount, so it reached the medium" "the marker did not survive"

echo
echo "cases:   $CASES"
echo "failed:  $FAILURES"
echo "medium:  a disposable image file on $LOOP; no host storage was touched."
[ "$FAILURES" -eq 0 ] || { echo "RESULT: FAIL ($FAILURES case(s))"; exit 1; }
echo "RESULT: PASS (real growpart, resize2fs and partition table)"
