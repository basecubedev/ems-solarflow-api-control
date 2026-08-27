#!/bin/sh
# SPDX-License-Identifier: AGPL-3.0-or-later
# Lifecycle of the package-owned SFTP backup account.
#
#   backup-account.sh ensure             create the account and record what was created
#   backup-account.sh disable            remove its authentication, fail-closed
#   backup-account.sh purge              remove only what this package created
#   backup-account.sh migrate-ownership  adopt a schema-2 record on request
#   backup-account.sh ownership-state    what the record can and cannot prove
#
# A name is not an identity: an account, a home directory and a key file can all
# be replaced by something the operator owns while keeping the same name. Every
# destructive step is therefore gated on the recorded uid, gid, home path and the
# ownership marker inside the home, and refuses when any of them no longer match.
#
# A record from an older schema carries no marker, so it cannot prove any of
# that on its own. Nothing here upgrades one by itself: the fields it does carry
# — created_by_package, the account name, the home path, a device and an inode —
# are all reproducible by whatever wrote them, and an inode is reproducible by
# the filesystem the moment it is handed out again. Such a record is reported,
# and an administrator adopts it with migrate-ownership, which proves the rest of
# the identity by hand.
set -eu

BACKUP_USER=${EMS_APPLIANCE_BACKUP_USER:-ems-backup}
STATE_DIR=${EMS_APPLIANCE_STATE_DIR:-/var/lib/ems-appliance-manager}
DEFAULT_HOME=${EMS_APPLIANCE_BACKUP_HOME:-/var/lib/ems-backup}
RECORD_DIR="$STATE_DIR/agent/package-state"
RECORD="$RECORD_DIR/backup-account.json"
MANAGED_KEYS="$RECORD_DIR/managed-keys.list"
# Preserved key material must outlive the state directory purge deletes.
QUARANTINE_DIR=${EMS_APPLIANCE_QUARANTINE_DIR:-/var/backups/ems-appliance-manager}
DISABLED_SUFFIX=.disabled-by-appliance
RECORD_SCHEMA=3
LEGACY_RECORD_SCHEMA=2
HOME_MARKER_NAME=.ems-appliance-backup-home
HOME_MARKER_SCHEMA=1
ORIGIN_DIR=${EMS_APPLIANCE_ORIGIN_DIR:-/usr/lib/ems-appliance-manager}
ORIGIN="$ORIGIN_DIR/backup-account-origin"
ORIGIN_SCHEMA=1

warnings=""

fail() {
    echo "ems-appliance: $1" >&2
    exit 1
}

note() {
    echo "ems-appliance: $1"
}

warn() {
    warnings="$warnings $1"
    echo "ems-appliance: $1" >&2
}

# A condition the operator must know about that is not a failure of this run.
caution() {
    echo "ems-appliance: $1" >&2
}

passwd_field() {
    getent passwd "$BACKUP_USER" 2>/dev/null | cut -d: -f"$1"
}

account_exists() {
    getent passwd "$BACKUP_USER" >/dev/null 2>&1
}

# --- BEGIN package-home authority (byte-identical in backup-account.sh and postrm)
# Device and inode are not a durable identity: a filesystem may hand a
# replacement directory the inode the original just released, so the pair a
# record was written with can describe a directory this package never created.
# The marker is the half that survives that — a root-owned file inside a
# root-owned home the backup account cannot write, absent from any replacement.
record_value() {
    [ -r "$RECORD" ] || return 0
    sed -n "s/.*\"$1\"[[:space:]]*:[[:space:]]*\"\{0,1\}\([^\",}]*\)\"\{0,1\}.*/\1/p" "$RECORD" \
        | head -n 1
}

home_identity() {
    [ -n "${1:-}" ] || return 0
    stat -c '%d:%i' "$1" 2>/dev/null || true
}

marker_value() {
    [ -r "$2" ] || return 0
    sed -n "s/^$1=//p" "$2" | head -n 1
}

home_marker_path() {
    marker=$(record_value home_marker)
    if [ -n "$marker" ]; then
        printf '%s' "$marker"
        return 0
    fi
    marker_home=$(record_value home)
    [ -n "$marker_home" ] || return 0
    printf '%s/%s' "$marker_home" "$HOME_MARKER_NAME"
}

home_marker_is_recorded() {
    marker=$(home_marker_path)
    [ -n "$marker" ] || return 1
    [ "$marker" = "$(record_value home)/$HOME_MARKER_NAME" ] || return 1
    if [ -L "$marker" ]; then
        return 1
    fi
    [ -f "$marker" ] || return 1
    [ "$(stat -c '%h' "$marker" 2>/dev/null || echo 0)" = "1" ] || return 1
    [ -z "$(find "$marker" -maxdepth 0 -perm /022 2>/dev/null)" ] || return 1
    if [ "$(id -u)" = "0" ]; then
        [ "$(stat -c '%u' "$marker" 2>/dev/null || echo 1)" = "0" ] || return 1
    fi
    recorded_nonce=$(record_value home_marker_nonce)
    [ -n "$recorded_nonce" ] || return 1
    [ "$(marker_value schema_version "$marker")" = "$HOME_MARKER_SCHEMA" ] || return 1
    [ "$(marker_value account "$marker")" = "$BACKUP_USER" ] || return 1
    [ "$(marker_value uid "$marker")" = "$(record_value uid)" ] || return 1
    [ "$(marker_value primary_gid "$marker")" = "$(record_value primary_gid)" ] || return 1
    [ "$(marker_value home "$marker")" = "$(record_value home)" ] || return 1
    [ "$(marker_value nonce "$marker")" = "$recorded_nonce" ] || return 1
    return 0
}

record_says_created() {
    [ -r "$RECORD" ] || return 1
    [ "$(record_value schema_version)" = "$RECORD_SCHEMA" ] || return 1
    [ "$(record_value account)" = "$BACKUP_USER" ] || return 1
    [ "$(record_value created_by_package)" = "true" ] || return 1
    return 0
}

identity_matches() {
    account_exists || return 1
    [ "$(passwd_field 3)" = "$(record_value uid)" ] || return 1
    [ "$(passwd_field 4)" = "$(record_value primary_gid)" ] || return 1
    [ "$(passwd_field 6)" = "$(record_value home)" ] || return 1
    return 0
}

# The recorded home must still be that exact directory. A home that is gone,
# unstatable, replaced, or reached through a symbolic link is somebody else's
# state, whatever the passwd entry still says.
home_is_recorded() {
    recorded_home=$(record_value home)
    [ -n "$recorded_home" ] || return 1
    if [ -L "$recorded_home" ]; then
        return 1
    fi
    [ -d "$recorded_home" ] || return 1
    recorded_identity="$(record_value home_device):$(record_value home_inode)"
    [ "$recorded_identity" != ":" ] || return 1
    [ "$(home_identity "$recorded_home")" = "$recorded_identity" ] || return 1
    home_marker_is_recorded || return 1
    return 0
}
# --- END package-home authority

package_owns_account() {
    record_says_created || return 1
    identity_matches || return 1
    home_is_recorded || return 1
    return 0
}

# --- what the record can prove ----------------------------------------------

record_schema() {
    record_value schema_version
}

home_marker_exists() {
    marker=$(home_marker_path)
    [ -n "$marker" ] || return 1
    [ -e "$marker" ] || [ -L "$marker" ]
}

home_matches_recorded_identity() {
    recorded_home=$(record_value home)
    [ -n "$recorded_home" ] || return 1
    [ -L "$recorded_home" ] && return 1
    [ -d "$recorded_home" ] || return 1
    recorded_identity="$(record_value home_device):$(record_value home_inode)"
    [ "$recorded_identity" != ":" ] || return 1
    [ "$(home_identity "$recorded_home")" = "$recorded_identity" ] || return 1
    return 0
}

# One closed set of states, so nothing has to infer ownership from a message.
# Unresolved legacy ownership is never reported as package-owned.
ownership_state() {
    if [ ! -r "$RECORD" ]; then
        printf 'no_ownership_record\n'
        return 0
    fi
    if [ -z "$(record_value account)" ] || [ -z "$(record_value home)" ]; then
        printf 'record_corrupt\n'
        return 0
    fi
    case "$(record_schema)" in
        "$RECORD_SCHEMA") ;;
        ""|"$LEGACY_RECORD_SCHEMA")
            printf 'legacy_manual_migration_required\n'
            return 0
            ;;
        *)
            printf 'record_corrupt\n'
            return 0
            ;;
    esac
    if [ "$(record_value account)" != "$BACKUP_USER" ] \
       || [ "$(record_value created_by_package)" != "true" ] \
       || ! identity_matches \
       || ! home_matches_recorded_identity; then
        printf 'ownership_conflict\n'
        return 0
    fi
    if ! home_marker_exists; then
        printf 'marker_missing\n'
        return 0
    fi
    if ! home_marker_is_recorded; then
        printf 'marker_mismatch\n'
        return 0
    fi
    printf 'current\n'
    return 0
}

home_directory() {
    home=$(passwd_field 6)
    [ -n "$home" ] || home=$(record_value home)
    printf '%s' "${home:-$DEFAULT_HOME}"
}

keys_directory() {
    printf '%s/.ssh' "$(home_directory)"
}

account_group() {
    group=$(passwd_field 4)
    [ -n "$group" ] || group=$(record_value primary_gid)
    printf '%s' "$group"
}

# --- ensure -----------------------------------------------------------------

installation_id() {
    if [ -r /proc/sys/kernel/random/uuid ]; then
        cat /proc/sys/kernel/random/uuid
        return 0
    fi
    printf '%s-%s' "$(date -u +%Y%m%d%H%M%S 2>/dev/null || echo unknown)" "$$"
}

new_marker_nonce() {
    value=$(od -An -tx1 -N32 /dev/urandom 2>/dev/null | tr -d ' \n' || true)
    if [ -n "$value" ]; then
        printf '%s' "$value"
        return 0
    fi
    printf '%s-%s' "$(installation_id)" "$$" | sha256sum | cut -d' ' -f1
}

# The account only ever reads its keys, so nothing in the home needs to be
# writable by it. A root-owned home is what makes the marker below unremovable
# by the account whose ownership it proves.
#
# sshd opens authorized_keys as the account, so the key directory has to be
# searchable and the file readable by the account's own group. Root ownership
# with no group write is what keeps that from becoming self-authorisation.
harden_home() {
    directory=$1
    [ -d "$directory" ] || return 0
    if [ -L "$directory" ]; then
        fail "$directory is a symbolic link; refusing to prepare the backup home"
    fi
    if [ "$(id -u)" = "0" ]; then
        chown root:root "$directory" || fail "cannot own the backup home $directory"
    fi
    chmod 0755 "$directory" || fail "cannot set the mode of the backup home $directory"
    if [ -d "$directory/.ssh" ] && [ ! -L "$directory/.ssh" ]; then
        if [ "$(id -u)" = "0" ]; then
            chown "root:$(account_group)" "$directory/.ssh" \
                || fail "cannot own $directory/.ssh"
        fi
        chmod 0750 "$directory/.ssh" || fail "cannot set the mode of $directory/.ssh"
        for keyfile in "$directory/.ssh/authorized_keys" \
                       "$directory/.ssh/authorized_keys$DISABLED_SUFFIX"; do
            [ -f "$keyfile" ] && [ ! -L "$keyfile" ] || continue
            if [ "$(id -u)" = "0" ]; then
                chown "root:$(account_group)" "$keyfile" || fail "cannot own $keyfile"
            fi
            chmod 0640 "$keyfile" || fail "cannot set the mode of $keyfile"
        done
    fi
    return 0
}

write_home_marker() {
    directory=$1
    nonce=$2
    installation=$3
    marker="$directory/$HOME_MARKER_NAME"
    if [ -L "$marker" ]; then
        fail "$marker is a symbolic link; refusing to write the ownership marker through it"
    fi
    staged="$marker.staged"
    rm -f "$staged"
    {
        printf '# ems-appliance backup home marker. Written by the package; do not edit.\n'
        printf 'schema_version=%s\n' "$HOME_MARKER_SCHEMA"
        printf 'account=%s\n' "$BACKUP_USER"
        printf 'uid=%s\n' "$(passwd_field 3)"
        printf 'primary_gid=%s\n' "$(passwd_field 4)"
        printf 'home=%s\n' "$directory"
        printf 'installation_id=%s\n' "$installation"
        printf 'nonce=%s\n' "$nonce"
    } > "$staged" || fail "cannot write the ownership marker in $directory"
    if [ "$(id -u)" = "0" ]; then
        chown root:root "$staged" || fail "cannot own the ownership marker in $directory"
    fi
    chmod 0400 "$staged" 2>/dev/null || true
    mv -f "$staged" "$marker" || fail "cannot install the ownership marker in $directory"
    return 0
}

# A marker without the record that names its secret proves nothing and would
# block the next adoption, so a record that cannot be committed takes the marker
# this run wrote with it.
abandon_home_marker() {
    rm -f "$1/$HOME_MARKER_NAME" 2>/dev/null || true
    fail "$2"
}

write_record() {
    created_home=$1
    origin_nonce=${2:-}
    mkdir -p "$RECORD_DIR" || fail "cannot create $RECORD_DIR"
    chmod 0700 "$RECORD_DIR" 2>/dev/null || true
    home=$(passwd_field 6)
    [ -n "$home" ] || fail "the $BACKUP_USER account has no home directory"
    harden_home "$home"
    nonce=$(new_marker_nonce)
    [ -n "$nonce" ] || fail "cannot generate an ownership marker for $home"
    # One installation is one identifier. The marker and the record are two
    # halves of the same ownership proof, so a marker whose installation does
    # not match the record it belongs to is not this package's marker.
    installation=$(installation_id)
    [ -n "$installation" ] || fail "cannot generate an installation identifier for $home"
    write_home_marker "$home" "$nonce" "$installation"
    identity=$(home_identity "$home")
    staged="$RECORD.staged"
    {
        printf '{\n'
        printf '  "schema_version": %s,\n' "$RECORD_SCHEMA"
        printf '  "account": "%s",\n' "$BACKUP_USER"
        printf '  "created_by_package": true,\n'
        printf '  "uid": %s,\n' "$(passwd_field 3)"
        printf '  "primary_gid": %s,\n' "$(passwd_field 4)"
        printf '  "home": "%s",\n' "$home"
        printf '  "home_device": "%s",\n' "${identity%%:*}"
        printf '  "home_inode": "%s",\n' "${identity##*:}"
        printf '  "home_marker": "%s",\n' "$home/$HOME_MARKER_NAME"
        printf '  "home_marker_nonce": "%s",\n' "$nonce"
        printf '  "home_created_by_package": %s,\n' "$created_home"
        printf '  "installation_id": "%s",\n' "$installation"
        printf '  "origin_nonce": "%s",\n' "$origin_nonce"
        printf '  "managed_keys_file": "%s",\n' "$MANAGED_KEYS"
        printf '  "authorized_keys": "%s/.ssh/authorized_keys",\n' "$home"
        printf '  "recorded_at": "%s"\n' \
            "$(date -u +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || echo unknown)"
        printf '}\n'
    } > "$staged" || abandon_home_marker "$home" "cannot stage $RECORD"
    chmod 0600 "$staged" 2>/dev/null || true
    mv -f "$staged" "$RECORD" || abandon_home_marker "$home" "cannot write $RECORD"
    [ -f "$MANAGED_KEYS" ] || : > "$MANAGED_KEYS"
    chmod 0600 "$MANAGED_KEYS" 2>/dev/null || true
}

# --- the account the image carries ------------------------------------------
# An imaged appliance carries the account from the build, and the record and
# marker proving it are created on the first boot. This declaration settles that
# one question -- whether the passwd entry is the one the build wrote -- and no
# other; the home is still judged by inspect_pre_existing_home. See
# docs/appliance/security-model.md.

origin_value() {
    marker_value "$1" "$ORIGIN"
}

origin_file_usable() {
    [ -e "$ORIGIN" ] || [ -L "$ORIGIN" ] || return 1
    require_real_chain "$ORIGIN_DIR"
    if [ -L "$ORIGIN" ]; then
        return 1
    fi
    [ -f "$ORIGIN" ] || return 1
    [ "$(stat -c '%h' "$ORIGIN" 2>/dev/null || echo 0)" = "1" ] || return 1
    [ -z "$(find "$ORIGIN" -maxdepth 0 -perm /022 2>/dev/null)" ] || return 1
    if [ "$(id -u)" = "0" ]; then
        [ "$(stat -c '%u' "$ORIGIN" 2>/dev/null || echo 1)" = "0" ] || return 1
    fi
    return 0
}

origin_describes_account() {
    origin_file_usable || return 1
    [ "$(origin_value schema_version)" = "$ORIGIN_SCHEMA" ] || return 1
    [ "$(origin_value account)" = "$BACKUP_USER" ] || return 1
    [ -n "$(origin_value nonce)" ] || return 1
    [ "$(origin_value home)" = "$DEFAULT_HOME" ] || return 1
    [ "$(origin_value uid)" = "$(passwd_field 3)" ] || return 1
    [ "$(origin_value primary_gid)" = "$(passwd_field 4)" ] || return 1
    [ "$(origin_value home)" = "$(passwd_field 6)" ] || return 1
    [ "$(origin_value shell)" = "$(passwd_field 7)" ] || return 1
    return 0
}

adopt_from_origin() {
    inspect_pre_existing_home
    write_record "$home_created_by_package" "$(origin_value nonce)"
    note "adopted the backup account $BACKUP_USER described by the origin declaration at $ORIGIN."
    return 0
}

# Not fatal on purpose: a failure here costs the next boot its adoption, not
# this run its account. The image inspection is what refuses an image without it.
write_origin_declaration() {
    mkdir -p "$ORIGIN_DIR" 2>/dev/null || true
    if [ ! -d "$ORIGIN_DIR" ] || [ -L "$ORIGIN" ]; then
        caution "the origin declaration could not be placed at $ORIGIN; a flashed image
  built from this root will need the backup account established by hand."
        return 0
    fi
    nonce=$(new_marker_nonce)
    staged="$ORIGIN.staged"
    rm -f "$staged" 2>/dev/null || true
    if ! {
        printf '# ems-appliance backup account origin. Written by the package; do not edit.\n'
        printf 'schema_version=%s\n' "$ORIGIN_SCHEMA"
        printf 'account=%s\n' "$BACKUP_USER"
        printf 'uid=%s\n' "$(passwd_field 3)"
        printf 'primary_gid=%s\n' "$(passwd_field 4)"
        printf 'home=%s\n' "$(passwd_field 6)"
        printf 'shell=%s\n' "$(passwd_field 7)"
        printf 'nonce=%s\n' "$nonce"
    } > "$staged" 2>/dev/null; then
        rm -f "$staged" 2>/dev/null || true
        caution "the origin declaration could not be written at $ORIGIN; a flashed image
  built from this root will need the backup account established by hand."
        return 0
    fi
    if [ "$(id -u)" = "0" ]; then
        chown root:root "$staged" 2>/dev/null || true
    fi
    chmod 0444 "$staged" 2>/dev/null || true
    mv -f "$staged" "$ORIGIN" 2>/dev/null || {
        rm -f "$staged" 2>/dev/null || true
        caution "the origin declaration could not be installed at $ORIGIN; a flashed image
  built from this root will need the backup account established by hand."
    }
    return 0
}

# Every key in the home has to be one this package wrote. A key it cannot
# attribute means somebody else has been writing into that home, which is the
# one thing an ownership upgrade may not assume away.
keys_are_attributed() {
    directory=$1
    [ -d "$directory" ] || return 0
    for conflict in "$directory/authorized_keys$DISABLED_SUFFIX".conflict*; do
        if [ -e "$conflict" ]; then
            return 1
        fi
    done
    for file in "$directory/authorized_keys" "$directory/authorized_keys$DISABLED_SUFFIX"; do
        [ -f "$file" ] || continue
        while IFS= read -r line || [ -n "$line" ]; do
            case "$line" in ''|\#*) continue ;; esac
            blob=$(printf '%s\n' "$line" | awk '{print $2}')
            [ -n "$blob" ] || return 1
            hash=$(printf '%s' "$blob" | sha256sum 2>/dev/null | cut -d' ' -f1)
            [ -n "$hash" ] || return 1
            grep -Fxq "$hash" "$MANAGED_KEYS" 2>/dev/null || return 1
        done < "$file"
    done
    return 0
}

# A home this package created holds nothing but the key directory and its own
# marker, and the key directory holds nothing but the key files this package
# writes. Anything else means somebody has been writing into that home, which is
# the one thing an ownership adoption may not assume away.
home_content_is_package_only() {
    directory=$1
    [ -d "$directory" ] || return 1
    for entry in "$directory"/* "$directory"/.[!.]* "$directory"/..?*; do
        [ -e "$entry" ] || [ -L "$entry" ] || continue
        case "${entry##*/}" in
            .ssh|"$HOME_MARKER_NAME") continue ;;
        esac
        return 1
    done
    keys_content_is_package_only "$directory/.ssh" || return 1
    return 0
}

keys_content_is_package_only() {
    directory=$1
    [ -e "$directory" ] || [ -L "$directory" ] || return 0
    [ -L "$directory" ] && return 1
    [ -d "$directory" ] || return 1
    for entry in "$directory"/* "$directory"/.[!.]* "$directory"/..?*; do
        [ -e "$entry" ] || [ -L "$entry" ] || continue
        case "${entry##*/}" in
            authorized_keys|"authorized_keys$DISABLED_SUFFIX") continue ;;
        esac
        return 1
    done
    return 0
}

# The home this package creates is root-owned and writable by nobody else, which
# is what makes the marker inside it unforgeable by the account it identifies. A
# home that never had that contract cannot acquire it retroactively.
home_is_confined() {
    directory=$1
    [ -L "$directory" ] && return 1
    [ -d "$directory" ] || return 1
    [ -z "$(find "$directory" -maxdepth 0 -perm /022 2>/dev/null)" ] || return 1
    if [ "$(id -u)" = "0" ]; then
        [ "$(stat -c '%u' "$directory" 2>/dev/null || echo 1)" = "0" ] || return 1
    fi
    return 0
}

# The explicit adoption of a schema-2 record. Device and inode equality is a
# necessary condition and never a sufficient one, so every other part of the
# identity is proven independently before a marker is written.
migrate_ownership_record() {
    [ "$(record_schema)" = "$LEGACY_RECORD_SCHEMA" ] \
        || fail "the ownership record for $BACKUP_USER at $RECORD has no schema this
  adoption can interpret. Nothing was changed."
    [ "$(record_value created_by_package)" = "true" ] \
        || fail "the ownership record at $RECORD does not say this package created
  $BACKUP_USER. Nothing was changed."
    account_exists || fail "the account $BACKUP_USER does not exist. Nothing was changed."
    identity_matches \
        || fail "the account $BACKUP_USER is no longer the account the record at $RECORD
  describes: its uid, group or home directory changed. Nothing was changed."

    recorded_home=$(record_value home)
    home_matches_recorded_identity \
        || fail "$recorded_home is not the directory the record at $RECORD describes.
  Nothing was changed."
    if home_marker_exists; then
        fail "$recorded_home already holds a $HOME_MARKER_NAME that this record does not
  describe. Resolve that by hand; nothing was changed."
    fi
    home_is_confined "$recorded_home" \
        || fail "$recorded_home is not a root-owned directory closed to other writers, so a
  marker in it would prove nothing. Nothing was changed."
    home_content_is_package_only "$recorded_home" \
        || fail "$recorded_home holds files this package did not put there, so it cannot be
  proven to be the home this package created. Review it by hand; nothing was changed."
    keys_are_attributed "$recorded_home/.ssh" \
        || fail "$recorded_home/.ssh holds key material this package cannot attribute to
  itself. Review it by hand; nothing was changed."

    note "adopting the backup account $BACKUP_USER (uid $(passwd_field 3), gid $(passwd_field 4))
  and its home $recorded_home; the key material in it is already attributed to this package."
    created_home=$(record_value home_created_by_package)
    [ "$created_home" = "true" ] || created_home=false
    write_record "$created_home"
    package_owns_account \
        || fail "the ownership record was rewritten but $recorded_home still cannot be proven
  to be the home this package created. Review $RECORD and that directory by hand."
    note "bound the ownership record for $BACKUP_USER to a home ownership marker in $recorded_home."
    return 0
}

migrate_ownership() {
    state=$(ownership_state)
    case "$state" in
        current)
            note "the ownership record for $BACKUP_USER is already bound to a home marker."
            return 0
            ;;
        legacy_manual_migration_required) migrate_ownership_record ;;
        *)
            fail "the ownership record for $BACKUP_USER is '$state', which no adoption can
  resolve. Review $RECORD by hand; nothing was changed."
            ;;
    esac
}

require_real_chain() {
    rest=${1#/}
    prefix=""
    while [ -n "$rest" ]; do
        segment=${rest%%/*}
        case "$rest" in
            */*) rest=${rest#*/} ;;
            *) rest="" ;;
        esac
        prefix="$prefix/$segment"
        [ -L "$prefix" ] && fail "$1 is reached through a symbolic link at $prefix"
        [ -e "$prefix" ] || return 0
        [ -d "$prefix" ] || fail "$1 passes through $prefix, which is not a directory"
    done
    return 0
}

# A home that is already there belongs to whoever put it there. Only an empty
# directory with a safe mode is adopted, and the record then says it was not
# created here so purge never removes it.
home_created_by_package=true

inspect_pre_existing_home() {
    home_created_by_package=true
    require_real_chain "$DEFAULT_HOME"
    if [ ! -e "$DEFAULT_HOME" ] && [ ! -L "$DEFAULT_HOME" ]; then
        return 0
    fi
    if [ -L "$DEFAULT_HOME" ]; then
        fail "$DEFAULT_HOME is a symbolic link; refusing to create the backup account through it"
    fi
    [ -d "$DEFAULT_HOME" ] || fail "$DEFAULT_HOME exists and is not a directory"
    if [ -n "$(ls -A "$DEFAULT_HOME" 2>/dev/null)" ]; then
        fail "the home directory $DEFAULT_HOME already exists and is not empty;
  this package will not adopt it or the key material in it. Move it aside, then
  install again."
    fi
    if [ -n "$(find "$DEFAULT_HOME" -maxdepth 0 -perm /022 2>/dev/null)" ]; then
        fail "$DEFAULT_HOME is writable by group or others; refusing to adopt it"
    fi
    if [ "$(id -u)" = "0" ] && [ "$(stat -c '%u' "$DEFAULT_HOME" 2>/dev/null || echo 0)" != "0" ]; then
        fail "$DEFAULT_HOME is not owned by root; refusing to adopt it"
    fi
    home_created_by_package=false
    return 0
}

ensure_account() {
    if account_exists; then
        if package_owns_account; then
            note "the backup account $BACKUP_USER is already owned by this package."
            # An upgrade from a release that left the key directory unreadable
            # by the account has to correct it; only a home this package is
            # proven to own is touched.
            harden_home "$(home_directory)"
            return 0
        fi
        if [ "$(ownership_state)" = "legacy_manual_migration_required" ]; then
            if [ "$(record_schema)" = "$LEGACY_RECORD_SCHEMA" ]; then
                fail "the ownership record for $BACKUP_USER at $RECORD predates the home ownership
  marker, so it cannot prove that $(record_value home) is the home this package created.
  Nothing was changed. Review that directory, then adopt it explicitly with
  'ems-appliance backup-account migrate-ownership'."
            fi
            fail "the ownership record for $BACKUP_USER at $RECORD has no schema version. The
  fields it carries cannot establish that $(record_value home), the account or the key
  material in it is the state this package left behind, and no adoption can make them.
  Nothing was changed. Review that record and that directory by hand; remove or rename
  the account, or restore a current ownership record, then install again."
        fi
        if [ "$(ownership_state)" = "no_ownership_record" ] && origin_describes_account; then
            adopt_from_origin
            return 0
        fi
        if record_says_created; then
            fail "the account $BACKUP_USER is no longer the account this package created;
  its uid, group or home directory changed. The appliance will not take it over.
  Remove or rename that account, or restore the ownership record at $RECORD."
        fi
        fail "the account $BACKUP_USER already exists and was not created by this package;
  this is a conflict the appliance will not resolve on its own. Either remove or
  rename that account, or restore the ownership record at $RECORD, then install again."
    fi

    inspect_pre_existing_home
    adduser --system --group --home "$DEFAULT_HOME" \
            --shell /usr/sbin/nologin "$BACKUP_USER" \
        || fail "cannot create the $BACKUP_USER account"

    write_record "$home_created_by_package"
    write_origin_declaration
    note "created the package-owned backup account $BACKUP_USER."
    return 0
}

# --- disable ----------------------------------------------------------------

free_conflict_path() {
    base=$1
    index=1
    while [ "$index" -le 20 ]; do
        if [ "$index" = 1 ]; then
            candidate="$base.conflict"
        else
            candidate="$base.conflict.$index"
        fi
        [ -e "$candidate" ] || { printf '%s' "$candidate"; return 0; }
        index=$((index + 1))
    done
    return 1
}

expire_account() {
    if command -v usermod >/dev/null 2>&1 \
            && usermod --expiredate 1 "$BACKUP_USER" >/dev/null 2>&1; then
        return 0
    fi
    if command -v chage >/dev/null 2>&1 && chage -E 1 "$BACKUP_USER" >/dev/null 2>&1; then
        return 0
    fi
    return 1
}

# Authentication goes before confinement does. A key file next to an already
# preserved one is a conflict only an operator can resolve, so both are kept.
disable_account() {
    if ! account_exists; then
        return 0
    fi
    if ! record_says_created || ! identity_matches; then
        note "the backup account $BACKUP_USER is not the account this package created; leaving its key material untouched."
        return 0
    fi
    # The key file at a replaced home belongs to whoever put it there. Expiring
    # the account withdraws authentication without touching that file.
    if ! home_is_recorded; then
        expire_account || fail "$(record_value home) is no longer the home directory this
  package created and $BACKUP_USER could not be expired; its authentication is still live."
        caution "$(record_value home) is no longer the home directory this package created;
  its key material was left untouched and $BACKUP_USER was expired instead. Review that
  directory by hand."
        return 0
    fi
    directory=$(keys_directory)
    keys="$directory/authorized_keys"
    preserved="$keys$DISABLED_SUFFIX"
    if [ -f "$keys" ]; then
        if [ -e "$preserved" ]; then
            target=$(free_conflict_path "$preserved") \
                || fail "$directory holds too many unresolved key files"
        else
            target=$preserved
        fi
        mv -f "$keys" "$target" || fail "cannot move $keys out of sshd's reach"
    fi
    [ -f "$keys" ] && fail "$keys is still readable by sshd"

    expire_account || note "the backup account could not be expired; its key was removed."
    return 0
}

# --- purge ------------------------------------------------------------------

quarantine_keys() {
    source_file=$1
    origin=$2
    mkdir -p "$QUARANTINE_DIR" 2>/dev/null || return 1
    chmod 0700 "$QUARANTINE_DIR" 2>/dev/null || true
    stamp=$(date -u +%Y%m%d%H%M%S 2>/dev/null || echo unknown)
    target="$QUARANTINE_DIR/$(basename "$origin").$BACKUP_USER.$stamp"
    mv -f "$source_file" "$target" 2>/dev/null || return 1
    chmod 0600 "$target" 2>/dev/null || true
    note "preserved unattributed key material from $origin at $target."
    return 0
}

# Only a key this package wrote is this package's to remove. Attribution is the
# recorded hash of the key body; anything else is preserved, never deleted.
filter_key_file() {
    file=$1
    [ -f "$file" ] || return 0
    kept="$file.kept.$$"
    : > "$kept"
    while IFS= read -r line || [ -n "$line" ]; do
        case "$line" in ''|\#*) continue ;; esac
        blob=$(printf '%s\n' "$line" | awk '{print $2}')
        if [ -n "$blob" ]; then
            hash=$(printf '%s' "$blob" | sha256sum 2>/dev/null | cut -d' ' -f1)
            if [ -n "$hash" ] && grep -Fxq "$hash" "$MANAGED_KEYS" 2>/dev/null; then
                continue
            fi
        fi
        printf '%s\n' "$line" >> "$kept"
    done < "$file"

    if [ -s "$kept" ]; then
        if quarantine_keys "$kept" "$file"; then
            rm -f "$file"
        else
            rm -f "$kept"
            warn "key material in $file could not be attributed to this package and was left in place"
            return 0
        fi
    else
        rm -f "$kept" "$file"
    fi
    return 0
}

withdraw_managed_keys() {
    directory=$1
    [ -d "$directory" ] || return 0
    filter_key_file "$directory/authorized_keys"
    filter_key_file "$directory/authorized_keys$DISABLED_SUFFIX"
    for conflict in "$directory/authorized_keys$DISABLED_SUFFIX".conflict*; do
        [ -e "$conflict" ] && filter_key_file "$conflict"
    done
    return 0
}

purge_account() {
    if ! record_says_created; then
        note "the backup account $BACKUP_USER was not created by this package; leaving it, its home and its key material untouched."
        rm -f "$RECORD"
        return 0
    fi

    if account_exists && ! identity_matches; then
        fail "the account $BACKUP_USER is not the account this package created;
  its uid, group or home directory changed. It, its home and its key material are
  left untouched. Remove the ownership record at $RECORD by hand once the account
  has been reviewed."
    fi

    # Nothing below may run against a home this package cannot prove is the one
    # it created: the key files in it, the account behind it and the ACL entries
    # naming it would all be somebody else's state.
    if ! home_is_recorded; then
        warn "$(record_value home) is no longer the home directory this package created;
  it, its key material and the account were left untouched. Review that directory and
  remove the ownership record at $RECORD by hand."
        return 0
    fi

    recorded_home=$(record_value home)
    created_home=$(record_value home_created_by_package)
    withdraw_managed_keys "$recorded_home/.ssh"

    if account_exists; then
        deluser --quiet "$BACKUP_USER" >/dev/null 2>&1 || true
        if account_exists; then
            usermod --expiredate 1 --lock "$BACKUP_USER" >/dev/null 2>&1 || true
            fail "the account $BACKUP_USER could not be removed; it was locked and expired instead"
        fi
    fi
    if getent group "$BACKUP_USER" >/dev/null 2>&1 && command -v delgroup >/dev/null 2>&1; then
        delgroup --quiet "$BACKUP_USER" >/dev/null 2>&1 || true
    fi

    # A home the package did not create belongs to the operator, and a home that
    # is no longer the recorded directory is somebody else's.
    if [ "$created_home" = "true" ] && [ -n "$recorded_home" ] && [ -d "$recorded_home" ] \
       && [ "$recorded_home" != "/" ] && [ ! -L "$recorded_home" ]; then
        if home_is_recorded; then
            rm -rf "$recorded_home"
        else
            warn "$recorded_home is no longer the home directory this package created; it was left in place"
        fi
    fi
    rm -f "$RECORD" "$MANAGED_KEYS"
    return 0
}

# --- describe ---------------------------------------------------------------

describe_account() {
    exists=false
    account_exists && exists=true
    owned=false
    package_owns_account && owned=true
    recorded=false
    record_says_created && recorded=true
    printf '{"account":"%s","exists":%s,"package_owned":%s,"record_present":%s,' \
        "$BACKUP_USER" "$exists" "$owned" "$recorded"
    printf '"home":"%s","shell":"%s","uid":"%s","primary_gid":"%s"}\n' \
        "$(home_directory)" "$(passwd_field 7)" "$(passwd_field 3)" "$(passwd_field 4)"
}

case "${1:-}" in
    ensure) ensure_account ;;
    disable) disable_account ;;
    purge) purge_account ;;
    describe) describe_account ;;
    migrate-ownership) migrate_ownership ;;
    ownership-state) ownership_state ;;
    *)
        echo "usage: $0 ensure|disable|purge|describe|migrate-ownership|ownership-state" >&2
        exit 2
        ;;
esac

if [ -n "$warnings" ]; then
    exit 1
fi
exit 0
