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
set -eu

MOUNT=$(/usr/bin/ems-appliance ab persistent-device >/dev/null 2>&1 && echo /persistent || echo "")
MARKER=/persistent/.grown

[ -f /etc/ems-appliance-manager/ab-layout.json ] || {
    echo "grow-persistent: this is not an A/B appliance image"
    exit 0
}
[ -f "$MARKER" ] && exit 0

DEVICE=$(/usr/bin/ems-appliance ab persistent-device) || {
    echo "grow-persistent: the persistent partition could not be identified" >&2
    exit 1
}
DISK=$(/usr/bin/ems-appliance ab persistent-disk) || DISK=""
NUMBER=$(/usr/bin/ems-appliance ab persistent-partition-number) || NUMBER=""

[ -b "$DEVICE" ] || {
    echo "grow-persistent: $DEVICE is not a block device" >&2
    exit 1
}

# Both are best-effort: a medium that is already the built size simply has
# nothing to grow, and that is not a boot failure.
if [ -n "$DISK" ] && [ -n "$NUMBER" ] && command -v growpart >/dev/null 2>&1; then
    growpart "$DISK" "$NUMBER" >/dev/null 2>&1 || true
fi
command -v resize2fs >/dev/null 2>&1 && resize2fs "$DEVICE" >/dev/null 2>&1 || true

mountpoint -q "${MOUNT:-/persistent}" || {
    echo "grow-persistent: /persistent is not mounted; nothing was marked" >&2
    exit 1
}
sync
printf 'grown_at=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || echo unknown)" > "$MARKER"
sync
echo "grow-persistent: $DEVICE now fills the medium"
