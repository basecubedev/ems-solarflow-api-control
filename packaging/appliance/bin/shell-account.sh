#!/bin/sh
# SPDX-License-Identifier: AGPL-3.0-or-later
# Create the SSH shell account, once, and never touch it again.
#
# Run from the package's postinst. This is the account an operator reaches over
# SSH when an update stops half way and the console cannot say why: it has a
# real shell and it reaches root through sudo. ems-backup cannot (SFTP, chroot,
# forced command) and ems-rescue must not (its password is published, and the
# shipped sshd policy refuses it over the network).
#
# Creating it is not enabling it. sshd refuses this account every
# authentication method until 'ems-appliance shell-access --enable' is run, and
# an account with no key is no login either. Both gates are off here.
#
# No password at all, ever: --disabled-password, and the sudoers drop-in below
# is NOPASSWD because a key holder has no password to type. A password prompt
# nobody can answer is not a safety property, it is an account that cannot do
# the one thing it exists for.
#
# An account that already exists is left exactly as it is, so an upgrade never
# resets a shell, a group membership or a home an operator changed.
#
# No Python: this runs from a postinst that is replacing appliance/*.py.
set -eu

ACCOUNT=${EMS_APPLIANCE_SHELL_ACCOUNT:-ems-shell}
HOME_DIR=${EMS_APPLIANCE_SHELL_HOME:-/home/$ACCOUNT}
SHELL_PATH=${EMS_APPLIANCE_SHELL_PATH:-/bin/bash}
SUDOERS=${EMS_APPLIANCE_SHELL_SUDOERS:-/etc/sudoers.d/ems-shell}

note() {
    echo "ems-appliance: $1"
}

fail() {
    echo "ems-appliance: $1" >&2
    exit 1
}

# Never fatal. This package installs on hosts without sudo, and an appliance
# that refuses to install because one account cannot reach root is worse than
# one where that account cannot reach root -- it has no console at all. Every
# degraded path says so instead of passing quietly; the account has no password,
# so without this drop-in it simply cannot sudo.
install_sudoers() {
    sudoers_dir=$(dirname "$SUDOERS")
    if [ ! -d "$sudoers_dir" ]; then
        note "$sudoers_dir does not exist, so $ACCOUNT will not be able to reach root"
        return 0
    fi
    # Through visudo, into a staging file, and only then into place. A sudoers
    # file that does not parse takes sudo away from every account on the host,
    # including the rescue account an operator would use to repair it.
    staging=$(mktemp "${SUDOERS}.XXXXXX" 2>/dev/null) || {
        note "no staging file could be created in $sudoers_dir; $SUDOERS was not installed"
        return 0
    }
    printf '%s ALL=(ALL:ALL) NOPASSWD: ALL\n' "$ACCOUNT" > "$staging"
    chmod 0440 "$staging"
    if command -v visudo >/dev/null 2>&1; then
        if ! visudo -cf "$staging" >/dev/null 2>&1; then
            rm -f "$staging"
            note "the sudoers drop-in for $ACCOUNT does not parse; nothing was installed"
            return 0
        fi
    else
        note "visudo is not available, so $SUDOERS was installed unchecked"
    fi
    mv "$staging" "$SUDOERS" || {
        rm -f "$staging"
        note "$SUDOERS could not be installed, so $ACCOUNT cannot reach root"
    }
    return 0
}

if getent passwd "$ACCOUNT" >/dev/null 2>&1; then
    note "the shell account $ACCOUNT already exists; leaving it untouched"
    [ -f "$SUDOERS" ] || install_sudoers
    exit 0
fi

[ -x "$SHELL_PATH" ] || SHELL_PATH=/bin/sh

adduser --disabled-password --gecos "EMS SolarFlow SSH shell" \
        --home "$HOME_DIR" --shell "$SHELL_PATH" "$ACCOUNT" >/dev/null \
    || fail "the shell account could not be created"

# Reaching root is the whole point; an account that cannot is not worth the
# exposure it costs. The group may be absent on a host without sudo, which is
# reported rather than fixed -- installing a package is not this script's
# business.
if getent group sudo >/dev/null 2>&1; then
    usermod -a -G sudo "$ACCOUNT" >/dev/null 2>&1 \
        || note "$ACCOUNT could not be added to the sudo group"
else
    note "there is no sudo group on this host, so $ACCOUNT cannot reach root"
fi

install_sudoers

note "created $ACCOUNT; it has no key and sshd refuses it until shell-access is enabled"
