#!/bin/sh
# SPDX-License-Identifier: AGPL-3.0-or-later
# Lifecycle of the package-owned SFTP backup account.
#
#   backup-account.sh ensure   create the account and record that we created it
#   backup-account.sh disable  remove its authentication, fail-closed
#   backup-account.sh purge    remove only what this package created
#
# A package may not adopt, alter and later delete an account an operator put on
# the host, so ownership is recorded when the account is created and every
# destructive step is gated on that record. An account that already exists
# without the record is a conflict, not something to take over.
set -eu

BACKUP_USER=${EMS_APPLIANCE_BACKUP_USER:-ems-backup}
STATE_DIR=${EMS_APPLIANCE_STATE_DIR:-/var/lib/ems-appliance-manager}
DEFAULT_HOME=${EMS_APPLIANCE_BACKUP_HOME:-/var/lib/ems-backup}
RECORD_DIR="$STATE_DIR/agent/package-state"
RECORD="$RECORD_DIR/backup-account.json"
DISABLED_SUFFIX=.disabled-by-appliance

fail() {
    echo "ems-appliance: $1" >&2
    exit 1
}

note() {
    echo "ems-appliance: $1"
}

passwd_field() {
    getent passwd "$BACKUP_USER" 2>/dev/null | cut -d: -f"$1"
}

account_exists() {
    getent passwd "$BACKUP_USER" >/dev/null 2>&1
}

record_value() {
    [ -r "$RECORD" ] || return 0
    sed -n "s/.*\"$1\"[[:space:]]*:[[:space:]]*\"\{0,1\}\([^\",}]*\)\"\{0,1\}.*/\1/p" "$RECORD" \
        | head -n 1
}

package_owns_account() {
    [ -r "$RECORD" ] || return 1
    [ "$(record_value account)" = "$BACKUP_USER" ] || return 1
    [ "$(record_value created_by_package)" = "true" ] || return 1
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

# --- ensure -----------------------------------------------------------------

write_record() {
    mkdir -p "$RECORD_DIR" || fail "cannot create $RECORD_DIR"
    staged="$RECORD.staged"
    printf '{\n' > "$staged"
    printf '  "account": "%s",\n' "$BACKUP_USER" >> "$staged"
    printf '  "created_by_package": %s,\n' "$1" >> "$staged"
    printf '  "home": "%s",\n' "$2" >> "$staged"
    printf '  "home_created_by_package": %s,\n' "$3" >> "$staged"
    printf '  "original_shell": "%s",\n' "$4" >> "$staged"
    printf '  "original_group": "%s",\n' "$5" >> "$staged"
    printf '  "original_expiry": "%s",\n' "$6" >> "$staged"
    printf '  "authorized_keys": "%s/.ssh/authorized_keys",\n' "$2" >> "$staged"
    printf '  "recorded_at": "%s"\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || echo unknown)" \
        >> "$staged"
    printf '}\n' >> "$staged"
    chmod 0600 "$staged" 2>/dev/null || true
    mv -f "$staged" "$RECORD" || fail "cannot write $RECORD"
}

ensure_account() {
    if account_exists; then
        if package_owns_account; then
            note "the backup account $BACKUP_USER is already owned by this package."
            return 0
        fi
        fail "the account $BACKUP_USER already exists and was not created by this package;
  this is a conflict the appliance will not resolve on its own. Either remove or
  rename that account, or restore the ownership record at $RECORD, then install again."
    fi

    home_existed=false
    [ -d "$DEFAULT_HOME" ] && home_existed=true
    adduser --system --group --home "$DEFAULT_HOME" \
            --shell /usr/sbin/nologin "$BACKUP_USER" \
        || fail "cannot create the $BACKUP_USER account"

    created_home=true
    [ "$home_existed" = true ] && created_home=false
    write_record true "$(home_directory)" "$created_home" "" "$BACKUP_USER" ""
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

# Authentication goes before confinement does. A key file next to an already
# preserved one is a conflict only an operator can resolve, so both are kept.
disable_account() {
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

    if command -v usermod >/dev/null 2>&1; then
        usermod --expiredate 1 "$BACKUP_USER" >/dev/null 2>&1 \
            || chage -E 1 "$BACKUP_USER" >/dev/null 2>&1 \
            || note "the backup account could not be expired; its key was removed."
    elif command -v chage >/dev/null 2>&1; then
        chage -E 1 "$BACKUP_USER" >/dev/null 2>&1 || true
    fi
    return 0
}

# --- purge ------------------------------------------------------------------

remove_managed_keys() {
    directory=$1
    [ -d "$directory" ] || return 0
    rm -f "$directory/authorized_keys" "$directory/authorized_keys$DISABLED_SUFFIX"
    for conflict in "$directory/authorized_keys$DISABLED_SUFFIX".conflict*; do
        [ -e "$conflict" ] && rm -f "$conflict"
    done
    return 0
}

purge_account() {
    home=$(home_directory)
    remove_managed_keys "$home/.ssh"

    if ! package_owns_account; then
        note "the backup account $BACKUP_USER was not created by this package; leaving it and its home untouched."
        rm -f "$RECORD"
        return 0
    fi

    created_home=$(record_value home_created_by_package)
    recorded_home=$(record_value home)

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

    # A home the package did not create belongs to the operator, whatever is
    # in it now.
    if [ "$created_home" = "true" ] && [ -n "$recorded_home" ] && [ -d "$recorded_home" ] \
       && [ "$recorded_home" != "/" ] && [ ! -L "$recorded_home" ]; then
        rm -rf "$recorded_home"
    fi
    rm -f "$RECORD"
    return 0
}

# --- describe ---------------------------------------------------------------

describe_account() {
    exists=false
    account_exists && exists=true
    owned=false
    package_owns_account && owned=true
    printf '{"account":"%s","exists":%s,"package_owned":%s,"home":"%s","shell":"%s"}\n' \
        "$BACKUP_USER" "$exists" "$owned" "$(home_directory)" "$(passwd_field 7)"
}

case "${1:-}" in
    ensure) ensure_account ;;
    disable) disable_account ;;
    purge) purge_account ;;
    describe) describe_account ;;
    *) echo "usage: $0 ensure|disable|purge|describe" >&2; exit 2 ;;
esac
exit 0
