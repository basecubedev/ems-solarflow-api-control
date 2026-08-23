# SPDX-License-Identifier: AGPL-3.0-or-later
# Where the appliance build and gate scripts do their work.
#
# These scripts stage multi-gigabyte images. /tmp is a tmpfs on many hosts --
# including the maintainer's, at 12G -- so a default of /tmp fills RAM and takes
# the machine down with it. One resolver, so a host is configured once and every
# script follows.
#
# shellcheck shell=sh

EMS_WORK_DEFAULT_SUBDIR="ems-appliance/work"

ems_work_root() {
    if [ -n "${EMS_APPLIANCE_WORK_DIR:-}" ]; then
        printf '%s\n' "$EMS_APPLIANCE_WORK_DIR"
        return 0
    fi
    printf '%s/%s\n' "${XDG_CACHE_HOME:-${HOME:-/var/tmp}/.cache}" "$EMS_WORK_DEFAULT_SUBDIR"
}

# ems_require_space <directory> <needed-bytes> <label>
ems_require_space() {
    ems_space_dir=$1
    ems_space_needed=$2
    ems_space_label=$3

    ems_space_free=$(df -PB1 "$ems_space_dir" 2>/dev/null | awk 'NR==2 {print $4}')
    if [ -z "$ems_space_free" ]; then
        echo "cannot determine free space in $ems_space_dir" >&2
        return 1
    fi
    if [ "$ems_space_free" -lt "$ems_space_needed" ]; then
        echo "not enough room for $ems_space_label in $ems_space_dir:" >&2
        echo "  needs $((ems_space_needed / 1024 / 1024 / 1024))G, has $((ems_space_free / 1024 / 1024 / 1024))G" >&2
        echo "  point EMS_APPLIANCE_WORK_DIR at a filesystem with room and run again" >&2
        return 1
    fi
    return 0
}

# ems_work_dir <prefix> [needed-bytes] -- prints a fresh directory under the
# configured root. The caller traps its own cleanup; this only creates.
ems_work_dir() {
    ems_work_prefix=$1
    ems_work_needed=${2:-0}

    ems_work_base=$(ems_work_root)
    mkdir -p "$ems_work_base" || {
        echo "cannot create the work directory $ems_work_base" >&2
        echo "  set EMS_APPLIANCE_WORK_DIR to a writable path" >&2
        return 1
    }
    if [ "$ems_work_needed" -gt 0 ]; then
        ems_require_space "$ems_work_base" "$ems_work_needed" "$ems_work_prefix" || return 1
    fi
    mktemp -d "$ems_work_base/$ems_work_prefix.XXXXXX"
}
