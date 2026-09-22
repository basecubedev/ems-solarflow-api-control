#!/bin/sh
# SPDX-License-Identifier: AGPL-3.0-or-later
# Create the console rescue account, once, and never touch it again.
#
# Run from the package's postinst. Without this an imaged appliance has no
# login at all: no human account, root locked, sulogin at rescue.target. An
# appliance nobody can reach and nobody can log into was a re-flash.
#
# The password it sets is documented, and that is the trade: those credentials
# are public knowledge. Changing them is the operator's choice, so an upgrade
# must never reset one they chose -- an account that already exists is left
# exactly as it is.
#
# No Python: this runs from a postinst that is replacing appliance/*.py.
set -eu

ACCOUNT=ems-rescue
HASH_FILE=${EMS_APPLIANCE_RESCUE_HASH_FILE:-/usr/share/ems-appliance-manager/rescue-password.hash}
HOME_DIR=/home/$ACCOUNT
SHELL_PATH=/bin/bash

note() {
    echo "ems-appliance: $1"
}

fail() {
    echo "ems-appliance: $1" >&2
    exit 1
}

# Read where it is used, not at the top: a host that has the account already and
# a password an operator chose needs nothing from this file, and failing there
# would turn a missing data file into a refused upgrade.
set_password() {
    [ -f "$HASH_FILE" ] || fail "$HASH_FILE is missing, so no rescue password could be set"
    hash=$(cat "$HASH_FILE")
    case "$hash" in
        \$*) ;;
        *) fail "$HASH_FILE does not hold a crypt hash" ;;
    esac
    # Through the hash rather than a plaintext password: the postinst has no
    # tty, and a plaintext one would reach the process table.
    printf '%s:%s\n' "$ACCOUNT" "$hash" | chpasswd -e \
        || fail "the rescue password could not be set"
}

# Creating the account and giving it a password are two steps, and anything
# between them -- a locked /etc/shadow, a full filesystem, an interrupted
# install -- used to leave an account with no password that this script then
# reported as finished on every later run. The account an operator logs in with
# at a keyboard could never log in, and reinstalling does not help because
# postrm never deletes it.
#
# Only the placeholder adduser --disabled-password writes is filled in: "*" or
# "!" with no hash after it. A password an operator chose is never reset, and
# `passwd -l` leaves "!" in front of a hash, which is a decision rather than a
# gap.
if getent passwd "$ACCOUNT" >/dev/null 2>&1; then
    stored=$(getent shadow "$ACCOUNT" 2>/dev/null | cut -d: -f2)
    case "$stored" in
        '' | '*' | '!' | '!!')
            note "the rescue account $ACCOUNT has no password; setting the shipped one"
            set_password
            ;;
        *)
            note "the rescue account $ACCOUNT already exists; leaving it untouched"
            ;;
    esac
    exit 0
fi

[ -x "$SHELL_PATH" ] || SHELL_PATH=/bin/sh

adduser --disabled-password --gecos "EMS SolarFlow console rescue" \
        --home "$HOME_DIR" --shell "$SHELL_PATH" "$ACCOUNT" >/dev/null \
    || fail "the rescue account could not be created"

set_password

# Reaching root is the whole point; an account that cannot is not a rescue.
# The group may not exist on a host without sudo, and that is reported rather
# than fixed here -- installing a package is not this script's business.
if getent group sudo >/dev/null 2>&1; then
    usermod -a -G sudo "$ACCOUNT" >/dev/null 2>&1 \
        || note "$ACCOUNT could not be added to the sudo group"
else
    note "there is no sudo group on this host, so $ACCOUNT cannot become root"
fi

note "created the rescue account $ACCOUNT with the documented default password"
note "see /usr/share/doc/ems-appliance-manager/console-recovery.md"
