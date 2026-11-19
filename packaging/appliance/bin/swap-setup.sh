#!/bin/sh
# SPDX-FileCopyrightText: 2026 basecubedev
# SPDX-License-Identifier: AGPL-3.0-or-later
# Swap for the appliance: compressed swap in memory first, a swap file on the
# root behind it as the reserve.
#
# A Raspberry Pi 3 has 1 GB of memory for Docker, the Admin console, EMS and
# InfluxDB. Without swap the kernel's only answer to a peak -- an InfluxDB
# start that loads years of history, its upgrade -- is to drop every cached
# page it can, program code included, and in the end to kill a process. zram
# keeps swapped pages compressed in memory: no card writes, read back in
# microseconds. The swap file has the lower priority, so it is written only
# once zram is full. On ext4 a swap file is written block by block like a
# partition, and unlike one it is sized for the card it is on.
#
# Swappiness 100 weighs swapping a page to zram the same as dropping a cached
# file page, which comes back from the card slowly. Measured three times each
# in a VM shaped like a Pi 3B+, an InfluxDB upgrade and its way back ran about
# a tenth faster at 100 than at 5, with the same EMS cycle times, up to 317 MB
# of zram and none of the file.
#
# The units run this only on a medium written from an appliance image. A swap
# the owner set up stays theirs: no file is added beside it.
#
# usage: swap-setup.sh zram|file
# Exit status 0 also when nothing is added; 1 when adding failed.
set -eu

# The test harness substitutes /proc and the swap directory; systemd supplies
# neither, so a booted appliance uses the paths below.
PROC=${EMS_SWAP_PROC:-/proc}
SWAP_DIR=${EMS_SWAP_DIR:-/var/lib/ems-appliance-swap}
SWAP_FILE="$SWAP_DIR/swapfile"
ZRAM_LABEL=ems-zram
ZRAM_PRIORITY=100
FILE_PRIORITY=10
SWAPPINESS=100
FILE_SIZES_MB="2048 1024 512"
# Room Docker images, backups and OS updates keep beside the file.
KEEP_FREE_MB=2048

say() { echo "swap-setup: $1"; }

active() { sed 1d "$PROC/swaps" | cut -d' ' -f1; }

mem_total_mb() {
    kb=$(sed -n 's/^MemTotal:[[:space:]]*\([0-9][0-9]*\) kB$/\1/p' "$PROC/meminfo")
    [ -n "$kb" ] && echo $((kb / 1024))
}

zram() {
    if active | grep -q '^/dev/zram'; then
        say "compressed swap is on already"
        return 0
    fi
    total=$(mem_total_mb) || total=""
    if [ -z "$total" ]; then
        say "the memory size cannot be read; no compressed swap"
        return 1
    fi
    size=$((total / 2))
    echo "$SWAPPINESS" > "$PROC/sys/vm/swappiness"
    if ! modprobe zram; then
        say "this kernel has no zram; the swap file is the only swap"
        return 1
    fi
    device=$(zramctl --find --size "${size}M" --algorithm zstd 2>/dev/null) \
        || device=$(zramctl --find --size "${size}M")
    mkswap --label "$ZRAM_LABEL" "$device" >/dev/null
    swapon --priority "$ZRAM_PRIORITY" "$device"
    say "compressed swap of $size MB on $device"
}

file() {
    if active | grep -qx "$SWAP_FILE"; then
        say "the swap file is on already"
        return 0
    fi
    if active | grep -qv '^/dev/zram'; then
        say "a swap the owner set up is on; no swap file is added"
        return 0
    fi
    echo "$SWAPPINESS" > "$PROC/sys/vm/swappiness"
    if [ ! -f "$SWAP_FILE" ]; then
        free=$(df --output=avail -BM "$(dirname "$SWAP_DIR")" | sed -n '2s/[^0-9]//gp')
        size=""
        for candidate in $FILE_SIZES_MB; do
            if [ $((free - candidate)) -ge "$KEEP_FREE_MB" ]; then
                size=$candidate
                break
            fi
        done
        if [ -z "$size" ]; then
            say "only $free MB are free on the root; no swap file is made"
            return 0
        fi
        install -d -m 0700 "$SWAP_DIR"
        partial="$SWAP_FILE.partial"
        rm -f "$partial"
        fallocate --length "${size}M" "$partial" \
            || dd if=/dev/zero of="$partial" bs=1M count="$size" status=none
        chmod 0600 "$partial"
        mkswap --label ems-swapfile "$partial" >/dev/null
        mv "$partial" "$SWAP_FILE"
        say "a swap file of $size MB was made"
    fi
    swapon --priority "$FILE_PRIORITY" "$SWAP_FILE"
    say "the swap file is on behind compressed swap"
}

case "${1:-}" in
    zram) zram ;;
    file) file ;;
    *) echo "usage: swap-setup.sh zram|file" >&2; exit 2 ;;
esac
