#!/bin/sh
# SPDX-License-Identifier: AGPL-3.0-or-later
# Build the read-only SFTP export root for the EMS backup account.
#
# The account is chrooted into the export root, so what it can see is exactly
# what this script puts there: three read-only bind mounts of the live EMS
# directories, and nothing else. Paths come from the root-owned host
# configuration only.
#
# Idempotent, and safe to run at boot, from the postinst and whenever the EMS
# installation changes. See docs/appliance/security-model.md.
set -eu

HOST_PATHS_FILE="${EMS_APPLIANCE_HOST_PATHS:-/etc/ems-appliance-manager/host-paths.env}"

# Read one key without sourcing the file: a generated configuration must never
# be able to execute anything in this script's shell.
host_path() {
    [ -r "$HOST_PATHS_FILE" ] || return 0
    sed -n "s/^$1=//p" "$HOST_PATHS_FILE" | tail -n 1
}

BACKUP_USER=${EMS_APPLIANCE_BACKUP_USER:-$(host_path EMS_APPLIANCE_BACKUP_USER)}
BACKUP_USER=${BACKUP_USER:-ems-backup}
INSTALL_ROOT=${EMS_APPLIANCE_INSTALL_ROOT:-$(host_path EMS_APPLIANCE_INSTALL_ROOT)}
INSTALL_ROOT=${INSTALL_ROOT:-/opt/ems-solarflow}
EXPORT_ROOT=${EMS_APPLIANCE_EXPORT_ROOT:-$(host_path EMS_APPLIANCE_EXPORT_ROOT)}
EXPORT_ROOT=${EXPORT_ROOT:-/srv/ems-appliance-export}
STATUS_FILE=${EMS_APPLIANCE_EXPORT_STATUS_FILE:-$(host_path EMS_APPLIANCE_EXPORT_STATUS_FILE)}
STATUS_FILE=${STATUS_FILE:-/var/lib/ems-appliance-manager/agent/export-access.json}
EXPORTS="config backups data"

status="configured"
detail=""
entries=""
missing=""

teardown() {
    for name in $EXPORTS; do
        target="$EXPORT_ROOT/$name"
        while mountpoint -q "$target" 2>/dev/null; do
            umount "$target" || break
        done
    done
}

record() {
    directory=$(dirname "$STATUS_FILE")
    [ -d "$directory" ] || return 0
    staged="$directory/.$(basename "$STATUS_FILE").staged"
    printf '{"status":"%s","user":"%s","root":"%s","export_root":"%s","chroot":true,"paths":[%s],"detail":"%s"}\n' \
        "$status" "$BACKUP_USER" "$INSTALL_ROOT" "$EXPORT_ROOT" "$entries" "$detail" > "$staged" \
        || { rm -f "$staged"; return 0; }
    chmod 0600 "$staged" 2>/dev/null || true
    mv -f "$staged" "$STATUS_FILE" 2>/dev/null || rm -f "$staged"
}

add_entry() {
    entry=$(printf '{"name":"%s","source":"%s","target":"%s","state":"%s","read_only":%s}' \
        "$1" "$2" "$3" "$4" "$5")
    if [ -z "$entries" ]; then entries="$entry"; else entries="$entries,$entry"; fi
}

# Refusals name the export and the configured path only. Where a rejected
# symlink actually pointed is not repeated into a status file or the journal.
fail() {
    status="failed"
    detail="$1"
    record
    echo "ems-appliance: $1" >&2
    exit 1
}

unavailable() {
    status="unavailable"
    detail="$1"
    record
    echo "ems-appliance: $1" >&2
    exit 1
}

# --- path policy ------------------------------------------------------------

# The same policy the Python services apply: absolute, no trailing slash, no
# empty or dotted segment, and no character that would need quoting in a mount
# command or escaping in the status file.
validate_configured_path() {
    label=$1
    value=$2
    case "$value" in
        /*) ;;
        *) fail "$label must be an absolute path" ;;
    esac
    case "$value" in
        */) fail "$label must not end in a slash" ;;
        *//*) fail "$label must not contain an empty path segment" ;;
        *[!A-Za-z0-9/._-]*) fail "$label contains characters that a host path may not use" ;;
    esac
    case "$value/" in
        */./*|*/../*) fail "$label must not contain a . or .. path segment" ;;
    esac
    [ "$value" = "/" ] && fail "$label must not be the filesystem root"
    return 0
}

# Every component that already exists must be a real directory. A path below a
# symlinked parent is refused before anything is created, so a redirected
# parent can never receive a root-owned mkdir, chown, chmod or ACL.
require_real_chain() {
    label=$1
    rest=${2#/}
    prefix=""
    while [ -n "$rest" ]; do
        segment=${rest%%/*}
        case "$rest" in
            */*) rest=${rest#*/} ;;
            *) rest="" ;;
        esac
        prefix="$prefix/$segment"
        if [ -L "$prefix" ]; then
            fail "$label is reached through a symbolic link; a separate partition must be a mount"
        fi
        [ -e "$prefix" ] || return 0
        [ -d "$prefix" ] || fail "$label passes through something that is not a directory"
    done
    return 0
}

# Device and inode identify a directory across every path that reaches it, so
# comparing them proves the kernel really mounted the subtree that was
# validated.
identity_of() {
    stat -Lc '%d:%i' "$1" 2>/dev/null || true
}

canonical() {
    readlink -f "$1" 2>/dev/null || true
}

require_export_source() {
    name=$1
    source_dir="$INSTALL_ROOT/$name"
    if [ -L "$source_dir" ]; then
        fail "$name is a symlink; an export source must be a real directory in $INSTALL_ROOT"
    fi
    if [ -e "$source_dir" ] && [ ! -d "$source_dir" ]; then
        fail "$name is not a directory in $INSTALL_ROOT"
    fi
    if [ -d "$source_dir" ] && [ "$(canonical "$source_dir")" != "$source_dir" ]; then
        fail "$name does not resolve to $source_dir; a redirected export source is refused"
    fi
    return 0
}

require_export_target() {
    name=$1
    target_dir="$EXPORT_ROOT/$name"
    if [ -L "$target_dir" ]; then
        fail "the export target for $name is a symlink; refusing to operate through it"
    fi
    if [ -e "$target_dir" ] && [ ! -d "$target_dir" ]; then
        fail "the export target for $name is not a directory"
    fi
    if [ -e "$target_dir" ] && [ "$(canonical "$target_dir")" != "$target_dir" ]; then
        fail "the export target for $name does not resolve inside $EXPORT_ROOT"
    fi
    return 0
}

# The chroot root is what the account sees. Anything in it that this feature
# does not manage would be visible to the account, so it is refused rather than
# removed — operator content is not this script's to delete.
require_exclusive_export_root() {
    [ -d "$EXPORT_ROOT" ] || return 0
    unexpected=""
    for entry in "$EXPORT_ROOT"/* "$EXPORT_ROOT"/.[!.]* "$EXPORT_ROOT"/..?*; do
        [ -e "$entry" ] || [ -L "$entry" ] || continue
        name=${entry##*/}
        managed=0
        for expected in $EXPORTS; do
            [ "$name" = "$expected" ] && managed=1
        done
        if [ "$managed" = 1 ] && [ ! -L "$entry" ] && [ -d "$entry" ]; then
            continue
        fi
        unexpected="$unexpected $name"
    done
    if [ -n "$unexpected" ]; then
        fail "the export root contains entries this appliance does not manage:${unexpected}; move them out of $EXPORT_ROOT"
    fi
    return 0
}

# --- mounting ---------------------------------------------------------------

is_read_only() {
    options=$(findmnt -no OPTIONS --mountpoint "$1" 2>/dev/null || echo "")
    case ",$options," in
        *,ro,*) return 0 ;;
        *) return 1 ;;
    esac
}

# A mount point is not evidence. What is exported is only what the kernel says
# is mounted there: the same filesystem object the source was validated as, and
# read-only in effect.
mount_proves() {
    target_dir=$1
    identity=$2
    mountpoint -q "$target_dir" 2>/dev/null || return 1
    is_read_only "$target_dir" || return 1
    [ "$(identity_of "$target_dir")" = "$identity" ] || return 1
    return 0
}

unmount_all() {
    target_dir=$1
    while mountpoint -q "$target_dir" 2>/dev/null; do
        umount "$target_dir" || return 1
    done
    return 0
}

# Remove whatever is mounted at the target, then judge the object that becomes
# visible: a mount can hide a symlink, and that symlink must never become the
# target of the next root-owned operation.
prepare_target() {
    name=$1
    target_dir="$EXPORT_ROOT/$name"
    if mountpoint -q "$target_dir" 2>/dev/null; then
        unmount_all "$target_dir" || fail "cannot unmount the export target for $name"
    fi
    require_export_target "$name"
    mkdir -p "$target_dir" || fail "cannot create the export target for $name"
    [ -L "$target_dir" ] && fail "the export target for $name became a symlink"
    chown root:root "$target_dir" || fail "cannot own the export target for $name"
    chmod 0755 "$target_dir" || fail "cannot set the mode of the export target for $name"
    mode=$(stat -c '%a' "$target_dir" 2>/dev/null || echo "")
    [ "$mode" = "755" ] || fail "the export target for $name is $mode, expected 755"
    if [ "$(id -u)" = "0" ]; then
        owner=$(stat -c '%U:%G' "$target_dir" 2>/dev/null || echo "")
        [ "$owner" = "root:root" ] || fail "the export target for $name is $owner, expected root:root"
    fi
    return 0
}

bind_read_only() {
    source_dir=$1
    target_dir=$2
    identity=$3
    mount --bind "$source_dir" "$target_dir" || return 1
    # A read-write bind that cannot be made read-only must not stay mounted:
    # an exported path is read-only or it is not exported.
    if ! mount -o remount,bind,ro "$target_dir"; then
        unmount_all "$target_dir" || true
        return 1
    fi
    if ! mount_proves "$target_dir" "$identity"; then
        unmount_all "$target_dir" || true
        return 1
    fi
    return 0
}

# --- entry point ------------------------------------------------------------

case "${1:-}" in
    --teardown)
        teardown
        exit 0
        ;;
    "") ;;
    *) echo "usage: $0 [--teardown]" >&2; exit 2 ;;
esac

validate_configured_path "the EMS installation root" "$INSTALL_ROOT"
validate_configured_path "the export root" "$EXPORT_ROOT"
[ "$INSTALL_ROOT" = "$EXPORT_ROOT" ] \
    && fail "the EMS installation root and the export root must not be the same directory"
case "$EXPORT_ROOT/" in
    "$INSTALL_ROOT"/*) fail "the export root must not live inside the EMS installation root" ;;
esac
case "$INSTALL_ROOT/" in
    "$EXPORT_ROOT"/*) fail "the EMS installation root must not live inside the export root" ;;
esac

for tool in setfacl mountpoint findmnt mount umount readlink stat; do
    command -v "$tool" >/dev/null 2>&1 || unavailable "$tool is not installed"
done
# ACLs are applied through an open directory handle, which needs /proc.
[ -d /proc/self/fd ] || unavailable "/proc is not mounted"

if ! getent passwd "$BACKUP_USER" >/dev/null 2>&1; then
    unavailable "the backup account $BACKUP_USER does not exist"
fi

# --- validation pass: nothing is changed until every path checks out --------

require_real_chain "the export root" "$EXPORT_ROOT"
require_real_chain "the EMS installation root" "$INSTALL_ROOT"
require_exclusive_export_root

if [ ! -d "$INSTALL_ROOT" ]; then
    teardown
    status="pending"
    detail="no EMS installation found yet"
    for name in $EXPORTS; do
        add_entry "$name" "$INSTALL_ROOT/$name" "$EXPORT_ROOT/$name" "missing" "true"
    done
    record
    echo "ems-appliance: $INSTALL_ROOT does not exist yet; the export root is empty."
    exit 0
fi

for name in $EXPORTS; do
    require_export_source "$name"
done

# --- mutation pass ----------------------------------------------------------

mkdir -p "$EXPORT_ROOT" || fail "cannot create the export root"
chown root:root "$EXPORT_ROOT" || fail "cannot own the export root"
chmod 0755 "$EXPORT_ROOT" || fail "cannot set the mode of the export root"

# Traverse-only on the install root: the account must reach the exports without
# being able to list unrelated siblings if it ever escaped the chroot.
setfacl -m "u:${BACKUP_USER}:x" "$INSTALL_ROOT" || fail "cannot set the traversal ACL on $INSTALL_ROOT"

for name in $EXPORTS; do
    source_dir="$INSTALL_ROOT/$name"
    target_dir="$EXPORT_ROOT/$name"
    # A source that is not there yet is pending; a source that is there and is
    # not a real directory is a refusal, not a missing export.
    require_export_source "$name"
    if [ ! -d "$source_dir" ]; then
        prepare_target "$name"
        missing="$missing $name"
        add_entry "$name" "$source_dir" "$target_dir" "missing" "true"
        continue
    fi

    # The ACL walk and the identity checks act on this open handle, so they can
    # never be redirected by a source that is swapped while they run.
    exec 9<"$source_dir" || fail "$name cannot be opened in $INSTALL_ROOT"
    handle=/proc/self/fd/9
    [ "$(readlink "$handle" 2>/dev/null)" = "$source_dir" ] \
        || fail "$name is not the directory it claims to be; refusing to export it"
    identity=$(identity_of "$handle")
    [ -n "$identity" ] || fail "$name cannot be identified in $INSTALL_ROOT"

    setfacl -R -m "u:${BACKUP_USER}:rX" "$handle" || fail "cannot set the read ACL on $name"
    setfacl -R -d -m "u:${BACKUP_USER}:rX" "$handle" || fail "cannot set the default ACL on $name"

    [ "$(readlink "$handle" 2>/dev/null)" = "$source_dir" ] \
        || fail "$name changed while it was being prepared; refusing to export it"
    [ "$(identity_of "$handle")" = "$identity" ] \
        || fail "$name changed while it was being prepared; refusing to export it"

    if mount_proves "$target_dir" "$identity"; then
        add_entry "$name" "$source_dir" "$target_dir" "mounted" "true"
        exec 9<&-
        continue
    fi

    prepare_target "$name"
    require_export_source "$name"
    if bind_read_only "$source_dir" "$target_dir" "$identity"; then
        add_entry "$name" "$source_dir" "$target_dir" "mounted" "true"
    else
        status="degraded"
        detail="cannot mount $name read-only at $target_dir"
        add_entry "$name" "$source_dir" "$target_dir" "failed" "false"
    fi
    exec 9<&-
done

missing=$(echo "$missing" | sed 's/^ *//')
if [ "$status" = "configured" ] && [ -n "$missing" ]; then
    detail="not present: $missing"
fi
record

if [ "$status" = "degraded" ]; then
    echo "ems-appliance: $detail" >&2
    exit 1
fi
echo "ems-appliance: read-only SFTP export root configured for $BACKUP_USER at $EXPORT_ROOT."
exit 0
