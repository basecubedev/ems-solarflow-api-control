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
HASH_FILE=/usr/share/ems-appliance-manager/rescue-password.hash
HOME_DIR=/home/$ACCOUNT
SHELL_PATH=/bin/bash

note() {
    echo "ems-appliance: $1"
}

fail() {
    echo "ems-appliance: $1" >&2
    exit 1
}

if getent passwd "$ACCOUNT" >/dev/null 2>&1; then
    note "the rescue account $ACCOUNT already exists; leaving it untouched"
    exit 0
fi

[ -f "$HASH_FILE" ] || fail "$HASH_FILE is missing, so no rescue password could be set"
HASH=$(cat "$HASH_FILE")
case "$HASH" in
    \$*) ;;
    *) fail "$HASH_FILE does not hold a crypt hash" ;;
esac

[ -x "$SHELL_PATH" ] || SHELL_PATH=/bin/sh

adduser --disabled-password --gecos "EMS SolarFlow console rescue" \
        --home "$HOME_DIR" --shell "$SHELL_PATH" "$ACCOUNT" >/dev/null \
    || fail "the rescue account could not be created"

# Set through the hash rather than a plaintext password: the postinst has no
# tty, and a plaintext one would reach the process table.
printf '%s:%s\n' "$ACCOUNT" "$HASH" | chpasswd -e \
    || fail "the rescue password could not be set"

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
