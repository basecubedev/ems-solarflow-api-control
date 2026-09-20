#!/bin/sh
# SPDX-License-Identifier: AGPL-3.0-or-later
# Install an Appliance Manager package, from a cgroup the install survives.
#
# Run by ems-appliance-manager-install.service and by nothing else. Two
# properties this file exists for:
#
#   Not the agent's cgroup. postinst restarts the agent, and systemd SIGTERMs
#   that whole cgroup -- which used to kill dpkg mid-configure.
#
#   No Python. dpkg replaces appliance/*.py while this runs.
#
set -eu

# What appliance/paths.py resolves packages_dir to, pinned to it by a test. The
# pre-split path one level up is the one migration.py moves away from.
STATE=/var/lib/ems-appliance-manager/agent/packages
REQUEST="$STATE/install-request.json"
RESULT="$STATE/install-result.json"
PREVIOUS="$STATE/previous.deb"

# dpkg runs the installed package's postinst, which tightens agent state and
# leaves the armed reverter unable to run. A revert installs an older retained
# package, and an older package carries no fix for that, so the restore belongs
# here in the manager that drives the install and covers whatever dpkg put on.
# A failure to restore is reported, never fatal: the package is already on, and
# refusing to record that would lose more than the deadline it costs.
#
# On a trap rather than after each dpkg. A revert that itself fails is the one
# moment the deadline is the last way out, and it leaves by a path no
# hand-placed call covers; a trap cannot be forgotten by the next branch added
# here. Restoring on a path that installed nothing is harmless -- the mode is
# what the arming already chose.
#
# Signals as well as EXIT: the unit's TimeoutStartSec makes SIGTERM an ordinary
# way for this to end, and dash does not run an EXIT trap for one. Each signal
# handler re-raises after restoring, so systemd still sees how the unit died.
ARMED_REVERTER="$STATE/verify-manager.armed.sh"

restore_armed_reverter() {
    [ -f "$ARMED_REVERTER" ] || return 0
    chmod 0700 "$ARMED_REVERTER" 2>/dev/null \
        || echo "install-manager: could not restore $ARMED_REVERTER" >&2
}

trap restore_armed_reverter EXIT
trap 'restore_armed_reverter; trap - INT;  kill -s INT  $$' INT
trap 'restore_armed_reverter; trap - TERM; kill -s TERM $$' TERM
trap 'restore_armed_reverter; trap - HUP;  kill -s HUP  $$' HUP

record() {
    # outcome, detail
    umask 077
    cat > "$RESULT.part" <<EOF
{
  "outcome": "$1",
  "detail": "$2",
  "finished_at": "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
}
EOF
    mv "$RESULT.part" "$RESULT"
}

fail() {
    echo "install-manager: $2" >&2
    record "$1" "$2"
    exit 1
}

[ -f "$REQUEST" ] || fail no_request "no install request is waiting at $REQUEST"

# The request names a file the agent already verified and retained. Reading it
# with sed rather than a JSON parser keeps this script free of interpreters that
# the install itself is replacing.
ARCHIVE=$(sed -n 's/.*"archive"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' "$REQUEST")
[ -n "$ARCHIVE" ] || fail request_invalid "the request names no archive"
[ -f "$ARCHIVE" ] || fail request_invalid "$ARCHIVE is not a file"

case "$ARCHIVE" in
    "$STATE"/*) ;;
    *) fail request_invalid "$ARCHIVE is outside $STATE" ;;
esac

echo "install-manager: installing $ARCHIVE"

# --force-confold: an operator's edited conffile is not collateral of an update.
# A tty-less run cannot answer a conffile prompt, and the alternative to
# answering is dpkg picking for us.
if dpkg --force-confold --install "$ARCHIVE"; then
    record installed "$ARCHIVE"
    echo "install-manager: installed"
    exit 0
fi

echo "install-manager: the install failed; attempting to complete it" >&2
# A half-configured package is the state everything else here is trying to
# avoid, so try the ordinary cure once before reaching for the previous package.
dpkg --configure -a || true

if [ -f "$PREVIOUS" ]; then
    echo "install-manager: reinstalling $PREVIOUS" >&2
    if dpkg --force-confold --install "$PREVIOUS"; then
        record reverted "the install failed and $PREVIOUS was put back"
        exit 1
    fi
    fail revert_failed "the install failed and $PREVIOUS could not be put back either"
fi

fail install_failed "the install failed and no earlier package is retained"
