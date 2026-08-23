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
STATE_DIR=${EMS_APPLIANCE_STATE_DIR:-/var/lib/ems-appliance-manager}
ACL_MANIFEST=${EMS_APPLIANCE_ACL_MANIFEST:-$STATE_DIR/agent/package-state/acl-manifest.tsv}
ACL_OWNERSHIP_RECORD=${EMS_APPLIANCE_OWNERSHIP_RECORD:-$STATE_DIR/agent/package-state/backup-account.json}
EXPORTS="config backups data"
ACL_STAGED=""
ACL_BEFORE=""
ACL_PREVIOUS=""
ACL_AFTER=""
ACL_SLICE=""
ACL_ROOTS=""
ACL_RECOVERY=""
ACL_STATE_FILE=""
ACL_PRIOR=""
ACL_PRIOR_PRESENT=no
ACL_PRIOR_HASH=""
ACL_PRIOR_MODE=""
ACL_PRIOR_OWNER=""
ACL_PRIOR_STATE=""
ACL_PRIOR_STATE_PRESENT=no
ACL_PRIOR_STATE_MODE=""
ACL_PRIOR_STATE_OWNER=""
ACL_STEP=none
ACL_MANIFEST_RENAMED=no
ACL_ROLLBACK_CLEAN=no
ACL_CONFLICTS=""
ACL_SCHEMA=3
TAB=$(printf '\t')
# One descriptor per export source, held open for the whole transaction. The
# rollback has to reach the object it changed, and only a descriptor still
# refers to it after the configured path stopped naming it.
ACL_FIRST_FD=5
ACL_LAST_FD=9
ACL_NEXT_FD=$ACL_FIRST_FD
ACL_OPEN_FDS=""
ACL_HANDLE=""

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
    # The status file is JSON that other components parse. A detail that wrapped
    # onto a second line would make it unreadable, and an unreadable status is
    # indistinguishable from no status at all.
    flat_detail=$(printf '%s' "$detail" | tr -c 'A-Za-z0-9 ._/:;,()=+-' ' ')
    printf '{"status":"%s","user":"%s","root":"%s","export_root":"%s","chroot":true,"paths":[%s],"detail":"%s"}\n' \
        "$status" "$BACKUP_USER" "$INSTALL_ROOT" "$EXPORT_ROOT" "$entries" "$flat_detail" > "$staged" \
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

# --- ACL manifest -----------------------------------------------------------

# Removal may only withdraw the exact ACL entries this package granted, so the
# manifest names one object and scope per line, with the permissions that were
# there before and the permissions this run left behind. A recursive grant is
# expanded: the subtree root alone cannot say which descendants were changed.
#
# Everything below is one transaction. An ACL this package applied without a
# committed manifest entry can never be attributed again — purge would either
# leave it behind forever or delete an entry it cannot prove is its own — so a
# staging, capture or commit failure aborts the run, and a failure after the
# first setfacl restores the captured pre-state before it does.
# The installation this package recorded when it created the backup account, so
# the ACL grant, the ownership record and the home marker all name one
# installation. Only a host without that record mints an identifier here.
acl_installation_id() {
    recorded=$(sed -n 's/.*"installation_id"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' \
        "$ACL_OWNERSHIP_RECORD" 2>/dev/null | head -n 1)
    if [ -n "$recorded" ]; then
        printf '%s' "$recorded"
        return 0
    fi
    if [ -r /proc/sys/kernel/random/uuid ]; then
        cat /proc/sys/kernel/random/uuid
        return 0
    fi
    printf '%s-%s' "$(date -u +%Y%m%d%H%M%S 2>/dev/null || echo unknown)" "$$"
}

fsync_path() {
    sync "$1" 2>/dev/null || return 1
    return 0
}

# Sets ACL_HANDLE rather than printing it: a command substitution would open the
# descriptor in a subshell that exits before anything can act through it.
acl_open_root() {
    ACL_HANDLE=""
    [ "$ACL_NEXT_FD" -le "$ACL_LAST_FD" ] || return 1
    eval "exec $ACL_NEXT_FD<\"\$1\"" || return 1
    ACL_OPEN_FDS="$ACL_OPEN_FDS $ACL_NEXT_FD"
    ACL_HANDLE=/proc/self/fd/$ACL_NEXT_FD
    ACL_NEXT_FD=$((ACL_NEXT_FD + 1))
    return 0
}

acl_close_roots() {
    for closing_fd in $ACL_OPEN_FDS; do
        eval "exec $closing_fd<&-" || true
    done
    ACL_OPEN_FDS=""
    ACL_NEXT_FD=$ACL_FIRST_FD
    return 0
}

# --- BEGIN package-object identity (byte-identical in setup-export-root.sh and postrm)
# The identity splits in two, and the split is the contract.
#
# Mandatory: device, inode, file type, owner, group and a generation — birth time
# where the filesystem keeps one, status-change time otherwise. Every field of it
# comes from stat(1), so it is readable wherever this package runs. Device and
# inode alone are not enough: they are handed straight back out when an object is
# unlinked, so the generation is what makes a newly created object at the same
# path a different object, and type, owner and group are part of it because an
# ACL entry means something different on an object whose ownership changed.
#
# Optional: the inode generation number ext4 and its relatives keep, which sees a
# reuse that two creations in the same clock tick hid. It needs lsattr, which
# this package does not depend on and which no filesystem is obliged to answer.
# So it only ever strengthens or refuses a match, never carries one: an optional
# signal that was recorded and cannot be read now — or the other way round —
# leaves the mandatory comparison in charge, because whether lsattr is installed
# is a property of the host and not of the object.
object_identity_core() {
    identity_target=$1
    if [ -L "$identity_target" ] && [ -d "$identity_target" ]; then
        identity_target="$identity_target/."
    fi
    stat -c '%d|%i|%F|%u|%g|%w|%z' "$identity_target" 2>/dev/null | awk -F'|' '
        NR == 1 {
            generation = $6
            if (generation == "-" || generation == "?" || generation == "") generation = $7
            gsub(/[ \t]/, "_", $3)
            gsub(/[ \t]/, "_", generation)
            printf "%s:%s:%s:%s:%s:%s", $1, $2, $3, $4, $5, generation
        }'
}

object_identity_optional() {
    identity_target=$1
    if [ -L "$identity_target" ] && [ -d "$identity_target" ]; then
        identity_target="$identity_target/."
    fi
    identity_version=$(lsattr -d -v "$identity_target" 2>/dev/null | awk 'NR == 1 { print $1 }')
    case "$identity_version" in
        ''|*[!0-9]*) return 0 ;;
        *) printf 'v%s' "$identity_version" ;;
    esac
}

object_identity() {
    identity_core=$(object_identity_core "$1")
    [ -n "$identity_core" ] || return 0
    identity_extra=$(object_identity_optional "$1")
    if [ -n "$identity_extra" ]; then
        printf '%s:%s' "$identity_core" "$identity_extra"
    else
        printf '%s' "$identity_core"
    fi
}

identity_core_of() {
    case "$1" in
        *:v[0-9]*) printf '%s' "${1%:*}" ;;
        *) printf '%s' "$1" ;;
    esac
}

identity_optional_of() {
    case "$1" in
        *:v[0-9]*) printf '%s' "${1##*:}" ;;
        *) return 0 ;;
    esac
}

# Does an object still carry the identity a manifest recorded for it? The
# mandatory half must agree exactly. The optional half refuses when both sides
# have one and they differ, and is silent when either side does not.
identity_agrees() {
    [ -n "$1" ] && [ -n "$2" ] || return 1
    [ "$(identity_core_of "$1")" = "$(identity_core_of "$2")" ] || return 1
    recorded_optional=$(identity_optional_of "$1")
    observed_optional=$(identity_optional_of "$2")
    if [ -n "$recorded_optional" ] && [ -n "$observed_optional" ] \
       && [ "$recorded_optional" != "$observed_optional" ]; then
        return 1
    fi
    return 0
}
# --- END package-object identity

# The transaction has states, not file presence. A manifest is authoritative only
# while the state says the run that wrote it committed; anything else names work
# an operator or the next run still has to finish.
acl_state_set() {
    ACL_STEP=$1
    [ -n "$ACL_STATE_FILE" ] || return 0
    printf '%s\n' "$1" > "$ACL_STATE_FILE.staged" 2>/dev/null || return 1
    chmod 0600 "$ACL_STATE_FILE.staged" 2>/dev/null || true
    mv -f "$ACL_STATE_FILE.staged" "$ACL_STATE_FILE" 2>/dev/null || {
        rm -f "$ACL_STATE_FILE.staged"
        return 1
    }
    fsync_path "$(dirname "$ACL_STATE_FILE")" || return 1
    return 0
}

# Exactly what the authoritative name holds right now, so a commit that cannot be
# made durable can put it back byte for byte instead of guessing.
#
# The manifest and its transaction state are one authority, never two: a manifest
# whose state says a run was still working on it is not a manifest a later purge
# may act through. They are snapshotted together here and put back together in
# acl_restore_prior_pair, or neither of them is claimed to be restored.
acl_manifest_snapshot() {
    ACL_PRIOR_PRESENT=no
    ACL_PRIOR_HASH=""
    ACL_PRIOR_MODE=""
    ACL_PRIOR_OWNER=""
    ACL_PRIOR_STATE=""
    ACL_PRIOR_STATE_PRESENT=no
    ACL_PRIOR_STATE_MODE=""
    ACL_PRIOR_STATE_OWNER=""
    rm -f "$ACL_PRIOR"
    if [ -n "$ACL_STATE_FILE" ] && [ -f "$ACL_STATE_FILE" ]; then
        ACL_PRIOR_STATE=$(cat "$ACL_STATE_FILE" 2>/dev/null) \
            || fail "the ACL transaction state at $ACL_STATE_FILE could not be read; refusing to replace it"
        ACL_PRIOR_STATE_MODE=$(stat -c '%a' "$ACL_STATE_FILE" 2>/dev/null || echo "")
        ACL_PRIOR_STATE_OWNER=$(stat -c '%u:%g' "$ACL_STATE_FILE" 2>/dev/null || echo "")
        ACL_PRIOR_STATE_PRESENT=yes
    fi
    [ -f "$ACL_MANIFEST" ] || return 0
    cp -f "$ACL_MANIFEST" "$ACL_PRIOR" \
        || fail "the ACL manifest at $ACL_MANIFEST could not be read; refusing to replace it"
    ACL_PRIOR_HASH=$(sha256sum "$ACL_PRIOR" 2>/dev/null | cut -d' ' -f1)
    [ -n "$ACL_PRIOR_HASH" ] \
        || fail "the ACL manifest at $ACL_MANIFEST could not be hashed; refusing to replace it"
    ACL_PRIOR_MODE=$(stat -c '%a' "$ACL_MANIFEST" 2>/dev/null || echo "")
    ACL_PRIOR_OWNER=$(stat -c '%u:%g' "$ACL_MANIFEST" 2>/dev/null || echo "")
    ACL_PRIOR_PRESENT=yes
    return 0
}

# Restoring a file means restoring the object, not only its bytes: a manifest
# that came back world-readable, owned by another account or as something other
# than a regular file is not the file the next purge may act through. Every step
# is checked, because a restore nobody verified is a restore nobody can trust.
acl_restore_file() {
    restore_target=$1
    restore_hash=$2
    restore_mode=$3
    restore_owner=$4
    restore_staged="$restore_target.restoring"
    rm -f "$restore_staged"
    cp -f "$ACL_PRIOR" "$restore_staged" 2>/dev/null || { rm -f "$restore_staged"; return 1; }
    if [ -n "$restore_mode" ]; then
        chmod "$restore_mode" "$restore_staged" 2>/dev/null \
            || { rm -f "$restore_staged"; return 1; }
    fi
    if [ -n "$restore_owner" ] && [ "$(id -u)" = "0" ]; then
        chown "$restore_owner" "$restore_staged" 2>/dev/null \
            || { rm -f "$restore_staged"; return 1; }
    fi
    fsync_path "$restore_staged" || { rm -f "$restore_staged"; return 1; }
    mv -f "$restore_staged" "$restore_target" 2>/dev/null \
        || { rm -f "$restore_staged"; return 1; }
    acl_verify_restored "$restore_target" "$restore_hash" "$restore_mode" "$restore_owner"
}

acl_verify_restored() {
    verify_target=$1
    verify_hash=$2
    verify_mode=$3
    verify_owner=$4
    [ -f "$verify_target" ] && [ ! -L "$verify_target" ] || return 1
    [ "$(sha256sum "$verify_target" 2>/dev/null | cut -d' ' -f1)" = "$verify_hash" ] || return 1
    if [ -n "$verify_mode" ]; then
        [ "$(stat -c '%a' "$verify_target" 2>/dev/null)" = "$verify_mode" ] || return 1
    fi
    if [ -n "$verify_owner" ] && [ "$(id -u)" = "0" ]; then
        [ "$(stat -c '%u:%g' "$verify_target" 2>/dev/null)" = "$verify_owner" ] || return 1
    fi
    return 0
}

# The transaction state that belonged to the restored manifest, written the same
# way acl_state_set writes it and read back before it is believed.
acl_restore_prior_state() {
    [ -n "$ACL_STATE_FILE" ] || return 1
    if [ "$ACL_PRIOR_STATE_PRESENT" != yes ]; then
        rm -f "$ACL_STATE_FILE" 2>/dev/null || return 1
        [ ! -e "$ACL_STATE_FILE" ] || return 1
        ACL_STEP=none
        return 0
    fi
    staged_state="$ACL_STATE_FILE.restoring"
    rm -f "$staged_state"
    printf '%s\n' "$ACL_PRIOR_STATE" > "$staged_state" 2>/dev/null \
        || { rm -f "$staged_state"; return 1; }
    if [ -n "$ACL_PRIOR_STATE_MODE" ]; then
        chmod "$ACL_PRIOR_STATE_MODE" "$staged_state" 2>/dev/null \
            || { rm -f "$staged_state"; return 1; }
    fi
    if [ -n "$ACL_PRIOR_STATE_OWNER" ] && [ "$(id -u)" = "0" ]; then
        chown "$ACL_PRIOR_STATE_OWNER" "$staged_state" 2>/dev/null \
            || { rm -f "$staged_state"; return 1; }
    fi
    fsync_path "$staged_state" || { rm -f "$staged_state"; return 1; }
    mv -f "$staged_state" "$ACL_STATE_FILE" 2>/dev/null \
        || { rm -f "$staged_state"; return 1; }
    [ -f "$ACL_STATE_FILE" ] && [ ! -L "$ACL_STATE_FILE" ] || return 1
    [ "$(cat "$ACL_STATE_FILE" 2>/dev/null)" = "$ACL_PRIOR_STATE" ] || return 1
    ACL_STEP=$ACL_PRIOR_STATE
    return 0
}

# Both halves or neither. A run that put the previous manifest back but left the
# transaction state at rollback_complete has produced a pair no later purge can
# use, which is indistinguishable from having lost the manifest.
#
# $1 says whether the previous transaction state may be claimed again. Only a
# rollback that put every ACL back may claim it: after an incomplete rollback the
# host no longer matches the manifest, so recovery_required has to stand. $2 says
# whether the manifest bytes still have to be put back, or are already in place.
acl_restore_prior_pair() {
    [ "$ACL_PRIOR_PRESENT" = yes ] || return 1
    if [ "$2" = yes ]; then
        [ -f "$ACL_PRIOR" ] || return 1
        acl_restore_file "$ACL_MANIFEST" "$ACL_PRIOR_HASH" "$ACL_PRIOR_MODE" "$ACL_PRIOR_OWNER" \
            || return 1
    fi
    if [ "$1" = yes ]; then
        acl_restore_prior_state || return 1
    fi
    fsync_path "$(dirname "$ACL_MANIFEST")" || return 1
    acl_verify_restored "$ACL_MANIFEST" "$ACL_PRIOR_HASH" "$ACL_PRIOR_MODE" "$ACL_PRIOR_OWNER" \
        || return 1
    if [ "$1" = yes ] && [ "$ACL_PRIOR_STATE_PRESENT" = yes ]; then
        [ "$(cat "$ACL_STATE_FILE" 2>/dev/null)" = "$ACL_PRIOR_STATE" ] || return 1
    fi
    return 0
}

acl_manifest_begin() {
    directory=$(dirname "$ACL_MANIFEST")
    mkdir -p "$directory" || fail "cannot create the ACL manifest directory $directory"
    base="$directory/.$(basename "$ACL_MANIFEST")"
    ACL_STAGED="$base.staged"
    ACL_BEFORE="$base.before"
    ACL_AFTER="$base.after"
    ACL_SLICE="$base.slice"
    ACL_ROOTS="$base.roots"
    ACL_PREVIOUS="$base.previous"
    ACL_PRIOR="$base.prior"
    ACL_RECOVERY=${EMS_APPLIANCE_ACL_RECOVERY:-$directory/acl-recovery.tsv}
    ACL_STATE_FILE="$directory/acl-transaction.state"
    rm -f "$ACL_STAGED" "$ACL_BEFORE" "$ACL_AFTER" "$ACL_SLICE" "$ACL_ROOTS" "$ACL_PREVIOUS"
    acl_manifest_snapshot
    acl_state_set staging || fail "the ACL transaction state could not be recorded"
    {
        echo "# ems-appliance ACL manifest v$ACL_SCHEMA"
        echo "schema=$ACL_SCHEMA"
        echo "user=$BACKUP_USER"
        echo "install_root=$INSTALL_ROOT"
        echo "installation_id=$(acl_installation_id)"
        echo "recorded_at=$(date -u +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || echo unknown)"
        # Which optional signals this host could read, so a reader knows whether
        # an entry without one was recorded without it or lost it since.
        if [ -n "$(object_identity_optional "$INSTALL_ROOT")" ]; then
            echo "optional_identity=inode_generation"
        else
            echo "optional_identity=none"
        fi
    } > "$ACL_STAGED" || fail "the ACL manifest cannot be staged at $ACL_STAGED"
    chmod 0600 "$ACL_STAGED" 2>/dev/null || true
    fsync_path "$ACL_STAGED" || fail "the staged ACL manifest could not be flushed to disk"
    : > "$ACL_BEFORE" || fail "the ACL pre-state cannot be staged at $ACL_BEFORE"
    : > "$ACL_ROOTS" || fail "the ACL root list cannot be staged at $ACL_ROOTS"
    : > "$ACL_PREVIOUS" || fail "the previous ACL manifest cannot be staged at $ACL_PREVIOUS"
    # A reinstall must not record its own previous grant as pre-existing state.
    if [ -r "$ACL_MANIFEST" ] \
       && [ "$(sed -n 's/^schema=//p' "$ACL_MANIFEST" | head -n 1)" = "$ACL_SCHEMA" ]; then
        cp -f "$ACL_MANIFEST" "$ACL_PREVIOUS" \
            || fail "the previous ACL manifest could not be read; refusing to grant again"
    fi
    return 0
}

# The walk, the mutation and both captures act on the same open directory
# handle, so a source swapped while this runs cannot make the manifest describe
# an object this package never changed. What is recorded is the canonical path
# an operator recognises, with the identity read back through the handle.
acl_capture() {
    acl_handle=$1
    acl_mode=$2
    acl_shown=$3
    acl_output=$4
    if [ "$acl_mode" = "recursive" ]; then
        getfacl -R -p --absolute-names "$acl_handle" > "$acl_output.raw" 2>/dev/null || return 1
    else
        getfacl -p --absolute-names "$acl_handle" > "$acl_output.raw" 2>/dev/null || return 1
    fi
    awk -v user="$BACKUP_USER" -v prefix="$acl_handle" -v shown="$acl_shown" -v OFS='\t' '
        function visible(path) {
            if (path == prefix) return shown
            if (index(path, prefix "/") == 1) return shown substr(path, length(prefix) + 1)
            return path
        }
        /^# file:/ { file = substr($0, 9); next }
        index($0, "user:" user ":") == 1 {
            split($0, parts, ":"); sub(/[ \t].*$/, "", parts[3])
            print visible(file), file, "access", parts[3]
        }
        index($0, "default:user:" user ":") == 1 {
            split($0, parts, ":"); sub(/[ \t].*$/, "", parts[4])
            print visible(file), file, "default", parts[4]
        }
    ' "$acl_output.raw" > "$acl_output" || { rm -f "$acl_output.raw"; return 1; }
    rm -f "$acl_output.raw"
    return 0
}

# The mutation authority of every root: the descriptor it was opened on, the
# mode it was granted with, and the identity read back through that descriptor.
# The canonical path travels with it for diagnostics only.
acl_capture_before() {
    printf 'root\t%s\t%s\t%s\t%s\n' "$3" "$1" "$2" "$(identity_of "$1")" >> "$ACL_ROOTS" \
        || fail "the ACL root list for $3 could not be written"
    acl_capture "$1" "$2" "$3" "$ACL_SLICE" \
        || fail "the ACL state of $3 could not be captured; nothing was changed"
    cat "$ACL_SLICE" >> "$ACL_BEFORE" \
        || fail "the captured ACL state of $3 could not be recorded; nothing was changed"
    return 0
}

# Append the identity of every captured object, read through the same handle the
# capture used. Rollback compares against it immediately before it writes, so an
# object swapped underneath the walk is reported instead of silently changed.
acl_identify_entries() {
    : > "$1.identified" || return 1
    while IFS="$TAB" read -r object handle_path scope perms; do
        [ -n "$object" ] && [ -n "$handle_path" ] && [ -n "$scope" ] || continue
        entry_identity=$(object_identity "$handle_path")
        [ -n "$entry_identity" ] || return 1
        printf '%s\t%s\t%s\t%s\t%s\n' \
            "$object" "$handle_path" "$scope" "$perms" "$entry_identity" >> "$1.identified" \
            || return 1
    done < "$1"
    mv -f "$1.identified" "$1" || return 1
    return 0
}

acl_capture_root() {
    acl_capture "$2" "$3" "$1" "$ACL_SLICE" || return 1
    acl_identify_entries "$ACL_SLICE" || return 1
    return 0
}

# Put back exactly what the pre-state described, through the descriptor the
# change was made on. What cannot be put back is written to a root-only recovery
# manifest: an untracked grant that nobody records is worse than a reported one
# nobody has resolved yet.
acl_rollback() {
    unresolved=""
    : > "$ACL_AFTER.all" || return 1
    while IFS="$TAB" read -r kind shown handle mode identity; do
        [ "$kind" = "root" ] || continue
        # A descriptor cannot be redirected, so this only fails when the root
        # was never identified; either way it is not safe to write through.
        if [ -z "$identity" ] || [ "$(identity_of "$handle")" != "$identity" ]; then
            unresolved="$unresolved $shown"
            continue
        fi
        if acl_capture_root "$shown" "$handle" "$mode"; then
            cat "$ACL_SLICE" >> "$ACL_AFTER.all" || true
        else
            unresolved="$unresolved $shown"
        fi
    done < "$ACL_ROOTS"

    while IFS="$TAB" read -r object handle_path scope perms identity; do
        [ -n "$object" ] && [ -n "$handle_path" ] && [ -n "$scope" ] || continue
        previous=$(awk -F"$TAB" -v o="$object" -v s="$scope" \
            '$1 == o && $3 == s { print $4; exit }' "$ACL_BEFORE" 2>/dev/null || true)
        if [ "$previous" = "$perms" ]; then
            continue
        fi
        if ! identity_agrees "$identity" "$(object_identity "$handle_path")"; then
            unresolved="$unresolved $object"
            continue
        fi
        if [ -n "$previous" ]; then
            if [ "$scope" = "default" ]; then
                setfacl -d -m "u:${BACKUP_USER}:$previous" "$handle_path" 2>/dev/null \
                    || unresolved="$unresolved $object"
            else
                setfacl -m "u:${BACKUP_USER}:$previous" "$handle_path" 2>/dev/null \
                    || unresolved="$unresolved $object"
            fi
        elif [ "$scope" = "default" ]; then
            setfacl -d -x "u:${BACKUP_USER}" "$handle_path" 2>/dev/null \
                || unresolved="$unresolved $object"
        else
            setfacl -x "u:${BACKUP_USER}" "$handle_path" 2>/dev/null \
                || unresolved="$unresolved $object"
        fi
    done < "$ACL_AFTER.all"

    : > "$ACL_AFTER.verify" || return 1
    while IFS="$TAB" read -r kind shown handle mode identity; do
        [ "$kind" = "root" ] || continue
        [ -n "$identity" ] && [ "$(identity_of "$handle")" = "$identity" ] || continue
        acl_capture "$handle" "$mode" "$shown" "$ACL_SLICE" || continue
        cat "$ACL_SLICE" >> "$ACL_AFTER.verify" || true
    done < "$ACL_ROOTS"
    if ! cut -f1,3,4 "$ACL_AFTER.verify" | sort > "$ACL_AFTER.verify.sorted" \
       || ! cut -f1,3,4 "$ACL_BEFORE" | sort > "$ACL_BEFORE.sorted" \
       || ! cmp -s "$ACL_AFTER.verify.sorted" "$ACL_BEFORE.sorted"; then
        unresolved="$unresolved incomplete-restore"
    fi
    cp -f "$ACL_AFTER.verify" "$ACL_AFTER.observed" 2>/dev/null || true
    rm -f "$ACL_AFTER.all" "$ACL_AFTER.verify" "$ACL_AFTER.verify.sorted" "$ACL_BEFORE.sorted"
    if [ -z "$unresolved" ]; then
        ACL_ROLLBACK_CLEAN=yes
        acl_state_set rollback_complete || true
        rm -f "$ACL_AFTER.observed"
        return 0
    fi
    acl_state_set recovery_required || true
    if ! acl_write_recovery "$unresolved" "$1"; then
        rm -f "$ACL_AFTER.observed"
        return 1
    fi
    rm -f "$ACL_AFTER.observed"
    return 0
}

# What a rollback could not put back, and everything needed to finish it by hand.
# Every step is checked: a cleanup that lost both the ACL state and the record of
# it is the one outcome nobody can recover from, so it is never reported as done.
acl_write_recovery() {
    {
        echo "# ems-appliance ACL recovery manifest v$ACL_SCHEMA"
        echo "schema=$ACL_SCHEMA"
        echo "user=$BACKUP_USER"
        echo "install_root=$INSTALL_ROOT"
        echo "installation_id=$(sed -n 's/^installation_id=//p' "$ACL_STAGED" 2>/dev/null | head -n 1)"
        echo "operation_id=$$"
        echo "recorded_at=$(date -u +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || echo unknown)"
        echo "state=recovery_required"
        echo "last_step=$ACL_STEP"
        echo "error=$2"
        echo "unresolved=$1"
        [ -f "$ACL_ROOTS" ] && sed 's/^/opened\t/' "$ACL_ROOTS"
        [ -f "$ACL_BEFORE" ] && sed 's/^/before\t/' "$ACL_BEFORE"
        [ -f "$ACL_AFTER.observed" ] && sed 's/^/observed\t/' "$ACL_AFTER.observed"
        [ -f "$ACL_STAGED" ] && cat "$ACL_STAGED"
    } > "$ACL_RECOVERY.staged" 2>/dev/null \
        || return 1
    chmod 0600 "$ACL_RECOVERY.staged" 2>/dev/null || true
    fsync_path "$ACL_RECOVERY.staged" || { rm -f "$ACL_RECOVERY.staged"; return 1; }
    mv -f "$ACL_RECOVERY.staged" "$ACL_RECOVERY" 2>/dev/null \
        || { rm -f "$ACL_RECOVERY.staged"; return 1; }
    fsync_path "$(dirname "$ACL_RECOVERY")" || return 1
    return 0
}

acl_abort() {
    acl_state_set rollback_required || true
    if ! acl_rollback "$1"; then
        acl_close_roots
        acl_manifest_uncommit || true
        rm -f "$ACL_STAGED" "$ACL_BEFORE" "$ACL_AFTER" "$ACL_SLICE" "$ACL_ROOTS" "$ACL_PREVIOUS"
        ACL_STAGED=""
        fail "$1; the ACL state that could not be put back could not be written to the recovery manifest at $ACL_RECOVERY either. Resolve the ACL entries for $BACKUP_USER under $INSTALL_ROOT by hand."
    fi
    acl_close_roots
    restored=yes
    acl_manifest_uncommit || restored=no
    rm -f "$ACL_STAGED" "$ACL_BEFORE" "$ACL_AFTER" "$ACL_SLICE" "$ACL_ROOTS" "$ACL_PREVIOUS"
    ACL_STAGED=""
    if [ "$restored" = no ]; then
        fail "$1; the previous ACL manifest at $ACL_MANIFEST and its transaction state could not both be put back. Resolve the ACL entries for $BACKUP_USER under $INSTALL_ROOT by hand."
    fi
    fail "$1"
}

record_granted_acl() {
    granted_handle=$1
    granted_mode=$2
    granted_shown=$3
    granted_identity=$(object_identity "$granted_handle")
    [ -n "$granted_identity" ] \
        || acl_abort "$granted_shown could not be identified after its ACL was set"
    printf 'root\t%s\t%s\t%s\n' "$granted_shown" "$granted_identity" "$granted_mode" \
        >> "$ACL_STAGED" \
        || acl_abort "the ACL manifest entry for $granted_shown could not be written"
    acl_capture "$granted_handle" "$granted_mode" "$granted_shown" "$ACL_AFTER" \
        || acl_abort "the ACL state of $granted_shown could not be read back"
    while IFS="$TAB" read -r object handle_path scope perms; do
        [ -n "$object" ] && [ -n "$handle_path" ] && [ -n "$scope" ] && [ -n "$perms" ] || continue
        entry_identity=$(object_identity "$handle_path")
        [ -n "$entry_identity" ] \
            || acl_abort "$object could not be identified after its ACL was set"
        carried=$(awk -F"$TAB" -v o="$object" -v i="$entry_identity" -v s="$scope" \
            '$1 == "entry" && $2 == o && $3 == i && $4 == s { print $5 "\t" $6 "\t" $7; exit }' \
            "$ACL_PREVIOUS" 2>/dev/null || true)
        if [ -n "$carried" ]; then
            preexisting=${carried%%"$TAB"*}
            carried_rest=${carried#*"$TAB"}
            previous=${carried_rest%%"$TAB"*}
            carried_granted=${carried_rest#*"$TAB"}
            # What this package last left there is what it expects to find. An
            # entry that says something else was changed by an operator, and
            # re-granting it silently would take that decision away from them.
            observed_before=$(awk -F"$TAB" -v o="$object" -v s="$scope" \
                '$1 == o && $3 == s { print $4; exit }' "$ACL_BEFORE" 2>/dev/null || true)
            if [ -n "$carried_granted" ] && [ "$observed_before" != "$carried_granted" ]; then
                ACL_CONFLICTS="$ACL_CONFLICTS $object"
            fi
        else
            previous=$(awk -F"$TAB" -v o="$object" -v s="$scope" \
                '$1 == o && $3 == s { print $4; exit }' "$ACL_BEFORE" 2>/dev/null || true)
            if [ -n "$previous" ]; then preexisting=yes; else preexisting=no; fi
        fi
        # Tab is IFS whitespace, so an empty field would collapse and shift
        # every field after it when the manifest is read back.
        [ -n "$previous" ] || previous="-"
        printf 'entry\t%s\t%s\t%s\t%s\t%s\t%s\n' \
            "$object" "$entry_identity" "$scope" "$preexisting" "$previous" "$perms" \
            >> "$ACL_STAGED" \
            || acl_abort "the ACL manifest entry for $object could not be written"
    done < "$ACL_AFTER"
    return 0
}

# The authoritative pair either holds what it held before this run, or nothing.
# What it must never hold is a manifest describing grants the rollback withdrew,
# and never a previously committed manifest under a transaction state that says
# some run is still working on it: a purge reading either would act on grants
# nobody holds any more, or refuse to act on grants it does hold.
acl_manifest_uncommit() {
    if [ "$ACL_MANIFEST_RENAMED" != yes ]; then
        # The authoritative manifest was never replaced. Its transaction state
        # was, when this run opened, so the pair is broken until that is undone.
        [ "$ACL_ROLLBACK_CLEAN" = yes ] && [ "$ACL_PRIOR_PRESENT" = yes ] || return 0
        acl_restore_prior_pair yes no && return 0
        echo "ems-appliance: the ACL transaction state at $ACL_STATE_FILE could not be" \
             "restored to '$ACL_PRIOR_STATE'; the manifest at $ACL_MANIFEST describes" \
             "grants this host still holds but no run may act on until it is resolved." >&2
        return 1
    fi
    ACL_MANIFEST_RENAMED=no
    if [ "$ACL_PRIOR_PRESENT" = yes ] && [ -f "$ACL_PRIOR" ]; then
        if acl_restore_prior_pair "$ACL_ROLLBACK_CLEAN" yes; then
            return 0
        fi
        rm -f "$ACL_MANIFEST.restoring"
    elif rm -f "$ACL_MANIFEST" 2>/dev/null && [ ! -f "$ACL_MANIFEST" ]; then
        fsync_path "$(dirname "$ACL_MANIFEST")" || true
        return 0
    fi
    # Neither the previous manifest nor an empty slot could be restored. The new
    # one leaves the authoritative name under a name nothing reads, so no reader
    # can mistake it for the state of the host.
    if mv -f "$ACL_MANIFEST" "$ACL_MANIFEST.uncommitted" 2>/dev/null; then
        fsync_path "$(dirname "$ACL_MANIFEST")" || true
        echo "ems-appliance: the ACL manifest could not be committed and was moved to" \
             "$ACL_MANIFEST.uncommitted; it describes grants this run withdrew." >&2
        return 0
    fi
    rm -f "$ACL_MANIFEST" 2>/dev/null || true
    echo "ems-appliance: the ACL manifest at $ACL_MANIFEST could not be withdrawn after a" \
         "failed commit; it may describe grants this run withdrew." >&2
    return 1
}

acl_manifest_commit() {
    [ -n "$ACL_STAGED" ] || return 0
    ACL_STEP=flush_staged
    fsync_path "$ACL_STAGED" || acl_abort "the ACL manifest could not be flushed to disk"
    ACL_STEP=rename
    mv -f "$ACL_STAGED" "$ACL_MANIFEST" \
        || acl_abort "the ACL manifest could not be committed to $ACL_MANIFEST"
    ACL_MANIFEST_RENAMED=yes
    ACL_STEP=flush_parent
    fsync_path "$(dirname "$ACL_MANIFEST")" \
        || acl_abort "the committed ACL manifest could not be flushed to disk"
    ACL_STEP=commit_state
    acl_state_set committed \
        || acl_abort "the committed ACL transaction state could not be recorded"
    ACL_MANIFEST_RENAMED=no
    rm -f "$ACL_BEFORE" "$ACL_AFTER" "$ACL_SLICE" "$ACL_ROOTS" "$ACL_PREVIOUS" "$ACL_PRIOR"
    ACL_STAGED=""
    return 0
}

# chown(2) and chmod(2) fail with EROFS on a read-only filesystem whether or not
# they would change anything. The slot root is read-only on a booted appliance
# and these directories are created by the image build, so the wanted state is
# checked first and only a difference is written.
ensure_root_owned_directory() {
    ensure_target=$1
    ensure_label=$2
    [ -d "$ensure_target" ] || mkdir -p "$ensure_target" \
        || fail "cannot create $ensure_label"
    if [ "$(id -u)" = "0" ] \
       && [ "$(stat -c '%U:%G' "$ensure_target" 2>/dev/null)" != "root:root" ]; then
        chown root:root "$ensure_target" || fail "cannot own $ensure_label"
    fi
    if [ "$(stat -c '%a' "$ensure_target" 2>/dev/null)" != "755" ]; then
        chmod 0755 "$ensure_target" || fail "cannot set the mode of $ensure_label"
    fi
    [ "$(stat -c '%a' "$ensure_target" 2>/dev/null)" = "755" ] \
        || fail "$ensure_label is $(stat -c '%a' "$ensure_target" 2>/dev/null), expected 755"
    if [ "$(id -u)" = "0" ]; then
        [ "$(stat -c '%U:%G' "$ensure_target" 2>/dev/null)" = "root:root" ] \
            || fail "$ensure_label is $(stat -c '%U:%G' "$ensure_target" 2>/dev/null), expected root:root"
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
    ensure_root_owned_directory "$target_dir" "the export target for $name"
    [ -L "$target_dir" ] && fail "the export target for $name became a symlink"
    return 0
}

bind_read_only() {
    source_dir=$1
    target_dir=$2
    identity=$3
    if ! mount --bind "$source_dir" "$target_dir"; then
        # Another run may have completed the same bind in the meantime; that is
        # the wanted state, not a failure.
        mount_proves "$target_dir" "$identity" && return 0
        return 1
    fi
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
    --teardown|"") ;;
    *) echo "usage: $0 [--teardown]" >&2; exit 2 ;;
esac

# The path watcher, the postinst and an operator can all start a run at the
# same moment. Two runs interleaving would unmount what the other just bound,
# so only one may hold the export root at a time. The lock is taken before the
# dispatch, not after it: a teardown unmounts exactly what a concurrent setup
# is binding, so it needs the lock at least as much as a setup does.
LOCK_FILE=${EMS_APPLIANCE_EXPORT_LOCK:-/run/ems-appliance-export.lock}
if [ -z "${EMS_APPLIANCE_EXPORT_LOCKED:-}" ] && command -v flock >/dev/null 2>&1 \
   && ( : >> "$LOCK_FILE" ) 2>/dev/null; then
    EMS_APPLIANCE_EXPORT_LOCKED=1 exec flock -w 300 "$LOCK_FILE" "$0" "$@"
fi

if [ "${1:-}" = "--teardown" ]; then
    teardown
    exit 0
fi

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

for tool in setfacl getfacl mountpoint findmnt mount umount readlink stat; do
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

ensure_root_owned_directory "$EXPORT_ROOT" "the export root"

acl_manifest_begin

# Traverse-only on the install root: the account must reach the exports without
# being able to list unrelated siblings if it ever escaped the chroot. Capture,
# mutation and read-back all go through one open handle.
acl_open_root "$INSTALL_ROOT" || fail "the EMS installation root cannot be opened"
install_handle=$ACL_HANDLE
[ "$(readlink "$install_handle" 2>/dev/null)" = "$INSTALL_ROOT" ] \
    || fail "the EMS installation root is not the directory it claims to be"
install_identity=$(identity_of "$install_handle")
[ -n "$install_identity" ] || fail "the EMS installation root cannot be identified"

acl_capture_before "$install_handle" single "$INSTALL_ROOT"
setfacl -m "u:${BACKUP_USER}:x" "$install_handle" \
    || acl_abort "cannot set the traversal ACL on $INSTALL_ROOT"
[ "$(identity_of "$install_handle")" = "$install_identity" ] \
    || acl_abort "the EMS installation root changed while it was being prepared"
record_granted_acl "$install_handle" single "$INSTALL_ROOT"

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

    # The ACL walk, the identity checks and any rollback act on this open
    # handle, so none of them can be redirected by a source that is swapped
    # while they run. It stays open until the transaction ends.
    acl_open_root "$source_dir" || fail "$name cannot be opened in $INSTALL_ROOT"
    handle=$ACL_HANDLE
    [ "$(readlink "$handle" 2>/dev/null)" = "$source_dir" ] \
        || fail "$name is not the directory it claims to be; refusing to export it"
    identity=$(identity_of "$handle")
    [ -n "$identity" ] || fail "$name cannot be identified in $INSTALL_ROOT"

    acl_capture_before "$handle" recursive "$source_dir"
    # The mask is set with the entry, not left to be derived. A named-user ACL
    # is capped by the mask, and on a 0600 file or a 0700 directory -- which is
    # what the EMS writes its secrets as -- the derived mask is empty, so the
    # grant would read as present in getfacl and be effective for nothing.
    setfacl -R -m "u:${BACKUP_USER}:rX,m::rX" "$handle" \
        || acl_abort "cannot set the read ACL on $name"
    setfacl -R -d -m "u:${BACKUP_USER}:rX,m::rX" "$handle" \
        || acl_abort "cannot set the default ACL on $name"

    [ "$(readlink "$handle" 2>/dev/null)" = "$source_dir" ] \
        || acl_abort "$name changed while it was being prepared; refusing to export it"
    [ "$(identity_of "$handle")" = "$identity" ] \
        || acl_abort "$name changed while it was being prepared; refusing to export it"

    record_granted_acl "$handle" recursive "$source_dir"

    if mount_proves "$target_dir" "$identity"; then
        add_entry "$name" "$source_dir" "$target_dir" "mounted" "true"
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
done

acl_manifest_commit
acl_close_roots

missing=$(echo "$missing" | sed 's/^ *//')
if [ "$status" = "configured" ] && [ -n "$missing" ]; then
    detail="not present: $missing"
fi
if [ -n "$ACL_CONFLICTS" ]; then
    echo "ems-appliance: an ACL entry for $BACKUP_USER was changed after the last" \
         "installation and has been granted again:$ACL_CONFLICTS" >&2
    if [ "$status" = "configured" ]; then
        if [ -n "$detail" ]; then detail="$detail; "; fi
        detail="${detail}re-granted a changed ACL on:$ACL_CONFLICTS"
    fi
fi
record

if [ "$status" = "degraded" ]; then
    echo "ems-appliance: $detail" >&2
    exit 1
fi
echo "ems-appliance: read-only SFTP export root configured for $BACKUP_USER at $EXPORT_ROOT."
exit 0
