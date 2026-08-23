#!/bin/sh
# SPDX-License-Identifier: AGPL-3.0-or-later
# Run a guest tier and deliver its whole record on a channel nothing else writes.
#
#   appliance-guest-evidence.sh --channel-name NAME [--fallback DEV] \
#                               [--log FILE] -- <guest-script> [argument...]
#
# Runs *inside* a disposable guest. The tier's own output never goes to a
# terminal: a login console is claimed by agetty, which calls vhangup() and
# revokes every descriptor already open on it, so a tier that logged there
# loses the rest of its output and dies on its next write. The record is
# written to a file, and copied once — on a fresh open, after the tier has
# finished — to a virtio-serial port the host reads back.
#
# Stage markers are mirrored to the boot console while the tier runs. They are a
# heartbeat for a guest that never finishes, not a record: the console is shared
# and nothing parses it.
#
# Exit status: the tier's own, or 3 when the tier could not be started.
set -u

CHANNEL_NAME=""
FALLBACK=/dev/console
LOG=/var/log/ems-appliance-guest-evidence.log
MARKER=APPLIANCE_EVIDENCE

fatal() {
    echo "appliance-guest-evidence: $1" >&2
    exit 3
}

while [ $# -gt 0 ]; do
    case "$1" in
        --channel-name) CHANNEL_NAME=${2:-}; shift 2 ;;
        --fallback) FALLBACK=${2:-}; shift 2 ;;
        --log) LOG=${2:-}; shift 2 ;;
        --) shift; break ;;
        *) fatal "unknown argument: $1" ;;
    esac
done

[ $# -ge 1 ] || fatal "no guest script was given"
TIER=$1
shift
[ -f "$TIER" ] || fatal "the guest script $TIER does not exist"

DEVICE_ROOT=${EMS_APPLIANCE_EVIDENCE_ROOT:-/dev}
CHANNEL=""
if [ -n "$CHANNEL_NAME" ]; then
    for candidate in "$DEVICE_ROOT/virtio-ports/$CHANNEL_NAME" "$DEVICE_ROOT/$CHANNEL_NAME"; do
        [ -w "$candidate" ] && CHANNEL=$candidate && break
    done
fi
CHANNEL_KIND=dedicated
if [ -z "$CHANNEL" ]; then
    CHANNEL=$FALLBACK
    CHANNEL_KIND=fallback
fi

: > "$LOG" 2>/dev/null || fatal "the record file $LOG is not writable"

# Appending, not truncating: the fallback is a character device in the guest
# but a plain file wherever this is exercised, and a truncating heartbeat would
# erase the record it was meant to accompany.
heartbeat() {
    printf '%s %s\n' "$MARKER" "$1" >> "$FALLBACK" 2>/dev/null || true
}

heartbeat "stage=boot channel=$CHANNEL_KIND path=$CHANNEL"

# The console mirror only ever carries the stage markers, so a guest that stops
# responding still says which stage it stopped in. It is one shell holding one
# descriptor rather than a `tail -f` pipeline, because this run has to be able
# to end it: killing a pipeline leaves the follower behind, and a follower on a
# quiet file never notices the pipe it writes to has gone.
MIRROR_FLAG="$LOG.mirror"
mirror_markers() {
    exec 9< "$LOG" || return 0
    while :; do
        while IFS= read -r line <&9; do
            case "$line" in
                "$MARKER "*) printf '%s\n' "$line" >> "$FALLBACK" 2>/dev/null || true ;;
            esac
        done
        # Checked after draining, so a tier that finished before the first poll
        # still has its stages mirrored rather than cut off by the shutdown.
        [ -e "$MIRROR_FLAG" ] || break
        sleep 1
    done
}
: > "$MIRROR_FLAG"
mirror_markers &
mirror=$!

sh "$TIER" "$@" >> "$LOG" 2>&1
status=$?

{
    printf '%s stage=record\n' "$MARKER"
    printf '%s channel=%s\n' "$MARKER" "$CHANNEL_KIND"
    printf '%s result=%s\n' "$MARKER" "$([ "$status" -eq 0 ] && echo PASS || echo FAIL)"
    printf 'APPLIANCE_SMOKE_EXIT: %s\n' "$status"
} >> "$LOG"

rm -f "$MIRROR_FLAG"
wait "$mirror" 2>/dev/null

heartbeat "stage=delivery"
if ! cat "$LOG" >> "$CHANNEL" 2>/dev/null; then
    heartbeat "stage=delivery result=FAIL"
    echo "appliance-guest-evidence: the record could not be written to $CHANNEL" >&2
    exit 3
fi
heartbeat "stage=delivery result=PASS exit=$status"
exit "$status"
