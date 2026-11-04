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
# dpkg's own words are matched below; a translated locale would hide the lock.
LC_ALL=C
export LC_ALL

# What appliance/paths.py resolves packages_dir to, pinned to it by a test. The
# pre-split path one level up is the one migration.py moves away from.
STATE=/var/lib/ems-appliance-manager/agent/packages
REQUEST="$STATE/install-request.json"
RESULT="$STATE/install-result.json"
PREVIOUS="$STATE/previous.deb"
PACKAGE=ems-appliance-manager
# The deadline the agent armed for this install, and what disarming it removes:
# the files verify-manager.sh's disarm() removes, held together by a test.
DEADLINE="$STATE/verify-deadline.json"
VERDICT="$STATE/verify-verdict.json"
DEADLINE_FILES="$DEADLINE $STATE/verify-revert-attempts $STATE/verify-ticks $STATE/verify-stable $STATE/verify-extended-until"
TIMER=ems-appliance-manager-verify.timer
# dpkg refuses at once while apt or another dpkg holds its lock, and this unit
# is the only run a request gets. The lock is waited out for this long, well
# inside the unit's TimeoutStartSec; the deadline spends no tick while it waits.
LOCK_WAIT_SECONDS=300
LOCK_RETRY_SECONDS=10
ERRORS="$STATE/dpkg-errors"
LOCKED=no

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

# A revert this script has already made is the outcome the deadline exists to
# reach. Left armed, it waited out the window expecting the refused version,
# reinstalled previous.deb a second time and restarted both services again,
# while the console refused Update and Revert as "not judged yet".
disarm_deadline() {
    # Word splitting is the point: one variable names every file disarm removes.
    # shellcheck disable=SC2086
    rm -f $DEADLINE_FILES
    systemctl disable --now "$TIMER" >/dev/null 2>&1 \
        || echo "install-manager: could not stop $TIMER" >&2
}

retire_deadline() {
    [ -f "$DEADLINE" ] || return 0
    umask 077
    cat > "$VERDICT.part" <<VERDICT_EOF
{
  "verdict": "$1",
  "detail": "$2",
  "decided_at": "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
}
VERDICT_EOF
    mv "$VERDICT.part" "$VERDICT"
    disarm_deadline
}

# Install one archive, waiting out a lock another apt or dpkg run holds.
# LOCKED=yes only when the wait ran out and dpkg never got the lock: a pass
# that got it and then failed -- a postinst, say -- has changed the system and
# goes on to the cure and the fallback like any other failure.
dpkg_install() {
    waited=0
    LOCKED=no
    while :; do
        if dpkg --force-confold --install "$1" 2>"$ERRORS"; then
            cat "$ERRORS" >&2
            return 0
        fi
        cat "$ERRORS" >&2
        grep -q "locked by another process" "$ERRORS" || return 1
        if [ "$waited" -ge "$LOCK_WAIT_SECONDS" ]; then
            LOCKED=yes
            return 1
        fi
        echo "install-manager: the package manager is busy; waiting" >&2
        sleep "$LOCK_RETRY_SECONDS"
        waited=$((waited + LOCK_RETRY_SECONDS))
    done
}

# Refused before dpkg runs: nothing is installed, so there is nothing for the
# deadline to judge, and left armed it reinstalled the running package.
refuse() {
    disarm_deadline
    fail "$1" "$2"
}

[ -f "$REQUEST" ] || refuse no_request "no install request is waiting at $REQUEST"

# The request names a file the agent already verified and retained. Reading it
# with sed rather than a JSON parser keeps this script free of interpreters that
# the install itself is replacing.
ARCHIVE=$(sed -n 's/.*"archive"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' "$REQUEST")
WANTED=$(sed -n 's/.*"version"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' "$REQUEST")
[ -n "$ARCHIVE" ] || refuse request_invalid "the request names no archive"
[ -f "$ARCHIVE" ] || refuse request_invalid "$ARCHIVE is not a file"

case "$ARCHIVE" in
    "$STATE"/*) ;;
    *) refuse request_invalid "$ARCHIVE is outside $STATE" ;;
esac

# What to put on if dpkg refuses the archive. A request that names it says so,
# an empty name included: a reinstall of the running package has no fallback,
# and previous.deb is then an older release than the one that ran. A request
# that predates the field falls back to previous.deb, as it always did.
if grep -q '"fallback"' "$REQUEST"; then
    PREVIOUS=$(sed -n 's/.*"fallback"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' "$REQUEST")
    case "$PREVIOUS" in
        "" | "$STATE"/*) ;;
        *) refuse request_invalid "$PREVIOUS is outside $STATE" ;;
    esac
fi

echo "install-manager: installing $ARCHIVE"

# --force-confold: an operator's edited conffile is not collateral of an update.
# A tty-less run cannot answer a conffile prompt, and the alternative to
# answering is dpkg picking for us.
if dpkg_install "$ARCHIVE"; then
    record installed "$ARCHIVE"
    echo "install-manager: installed"
    exit 0
fi

# Nothing was unpacked: the lock never came free. The running package stays,
# so there is nothing for the deadline to judge and nothing to put back.
if [ "$LOCKED" = yes ]; then
    disarm_deadline
    fail not_started "another apt or dpkg run held the package lock for ${LOCK_WAIT_SECONDS}s; nothing was installed"
fi

echo "install-manager: the install failed; attempting to complete it" >&2
# A half-configured package is the state everything else here is trying to
# avoid, so try the ordinary cure once before reaching for the previous package.
dpkg --configure -a || true

# Whether the cure worked is a question about dpkg's database, not about an
# exit code: an install that failed before unpacking leaves nothing to
# configure, so `--configure -a` succeeds having done nothing. Reverting from
# here would throw away an update that is now complete and healthy, restart
# both services twice more, and file the run as `reverted`.
KNOWN=$(dpkg-query -W -f '${db:Status-Status}|${Version}' "$PACKAGE" 2>/dev/null || true)
if [ -n "$WANTED" ] && [ "$KNOWN" = "installed|$WANTED" ]; then
    record installed "$ARCHIVE was left installed by dpkg --configure -a"
    echo "install-manager: installed"
    exit 0
fi

if [ -n "$PREVIOUS" ] && [ -f "$PREVIOUS" ]; then
    echo "install-manager: reinstalling $PREVIOUS" >&2
    if dpkg_install "$PREVIOUS"; then
        record reverted "the install failed and $PREVIOUS was put back"
        retire_deadline reverted "the install failed and the installer put $PREVIOUS back"
        exit 1
    fi
    fail revert_failed "the install failed and $PREVIOUS could not be put back either"
fi

[ -n "$PREVIOUS" ] \
    || fail install_failed "the install failed; it reinstalled the package that was running, so there is no other package to put back"
fail install_failed "the install failed and no earlier package is retained"
