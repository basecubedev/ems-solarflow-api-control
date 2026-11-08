#!/bin/sh
# SPDX-FileCopyrightText: 2026 basecubedev
# SPDX-License-Identifier: AGPL-3.0-or-later
# Decide whether the manager install that armed this deadline may stand.
#
# Run by ems-appliance-manager-verify.timer, from a snapshot the *outgoing*
# package left under the state directory. The packaged copy at
# /usr/lib/ems-appliance-manager is replaced by the install this judges, so a
# reverter read from there would be code the install brought with it.
#
# No Python: dpkg rewrites appliance/*.py underneath a running interpreter.
#
# The state directory is a parameter so the unit says which one it operates on.
set -eu

STATE=${1:-/var/lib/ems-appliance-manager/agent/packages}
DEADLINE="$STATE/verify-deadline.json"
VERDICT="$STATE/verify-verdict.json"
# install-manager.sh's result file; a revert this script drove is recorded there too.
RESULT="$STATE/install-result.json"
ATTEMPTS="$STATE/verify-revert-attempts"
TICKS="$STATE/verify-ticks"
STABLE="$STATE/verify-stable"
EXTENDED="$STATE/verify-extended-until"
PACKAGE=ems-appliance-manager
TIMER=ems-appliance-manager-verify.timer
INSTALL_UNIT=ems-appliance-manager-install.service
SERVICES="ems-appliance-agent.service ems-appliance-web.service"
# The web service's port, read the way the manager reads it; a missing file or
# key is the shipped default.
CONFIG=${2:-/etc/ems-appliance-manager/appliance.conf}

# The dpkg frontend lock is held for as long as an operator's apt run takes, and
# repairing the package manager is the console action a bad install invites. One
# attempt spends the only automatic way back on a condition that clears itself.
REVERT_ATTEMPTS=5

# The one deadline record layout this reverter can act on: the number
# manager_verify.DEADLINE_SCHEMA_VERSION writes, and a test holds the two
# together. Fields read out of a record with another number may not mean
# what they meant here.
DEADLINE_SCHEMA=1

# How far apart the ticks that run this are: ems-appliance-manager-verify.timer's
# OnUnitActiveSec, and a test holds the two together. The board has no
# real-time clock -- systemd restores a stale time at boot and only moves it
# forward -- so the window is counted in these ticks as well as on the clock,
# and whichever runs out first ends it. Ticks are at least this far apart, so
# the budget can only lengthen the real-time window, never shorten it.
TICK_SECONDS=60

text() {
    sed -n "s/.*\"$1\"[[:space:]]*:[[:space:]]*\"\([^\"]*\)\".*/\1/p" "$DEADLINE"
}

number() {
    sed -n "s/.*\"$1\"[[:space:]]*:[[:space:]]*\([0-9][0-9]*\).*/\1/p" "$DEADLINE"
}

record() {
    umask 077
    cat > "$VERDICT.part" <<EOF
{
  "verdict": "$1",
  "detail": "$2",
  "decided_at": "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
}
EOF
    mv "$VERDICT.part" "$VERDICT"
}

record_install_reverted() {
    umask 077
    cat > "$RESULT.part" <<EOF
{
  "outcome": "reverted",
  "detail": "$1",
  "finished_at": "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
}
EOF
    mv "$RESULT.part" "$RESULT"
}

disarm() {
    rm -f "$DEADLINE" "$ATTEMPTS" "$TICKS" "$STABLE" "$EXTENDED"
    systemctl disable --now "$TIMER" >/dev/null 2>&1 || true
}

if [ ! -f "$DEADLINE" ]; then
    disarm
    exit 0
fi

# The record is judged before any field in it is trusted. manager_verify.read()
# already refuses a version it does not know, and the console reports that
# record as unreadable -- a reverter that read the same file field by field
# would then install previous.deb behind a console saying nothing is in
# flight. A record with no deadline in it cannot have expired either; the old
# default of 0 was a deadline in 1970. Disarmed rather than left: nothing can
# act on this record, and an armed one would tick behind that console forever.
SCHEMA=$(number schema_version)
DEADLINE_EPOCH=$(number deadline_epoch)
WINDOW=$(number window_seconds)
if [ "$SCHEMA" != "$DEADLINE_SCHEMA" ] || [ -z "$DEADLINE_EPOCH" ] || [ -z "$WINDOW" ]; then
    record revert_unavailable \
        "the deadline record could not be read by this reverter (schema ${SCHEMA:-none}); nothing was judged and nothing was installed"
    disarm
    exit 0
fi

EXPECTED=$(text expected_version)
PREVIOUS=$(text previous_path)
PREVIOUS_SHA=$(text previous_sha256)
NOW=$(date -u +%s)
EXTENDED_UNTIL=$(cat "$EXTENDED" 2>/dev/null || echo 0)
case "$EXTENDED_UNTIL" in '' | *[!0-9]*) EXTENDED_UNTIL=0 ;; esac
[ "$EXTENDED_UNTIL" -gt "$DEADLINE_EPOCH" ] && DEADLINE_EPOCH=$EXTENDED_UNTIL

# dpkg is still working on the package this deadline judges. Nothing about it
# can be known yet, and a tick spent here is one the judgement does not get:
# a slow card used to spend most of the window waiting for its own install. The
# unit's TimeoutStartSec bounds the wait. The clock half of the window is moved
# on too, to a whole window from now, in a file the console reads beside the
# record: a lock wait plus a slow unpack otherwise left the judgement a minute,
# and reverted an install still binding its port.
case "$(systemctl is-active "$INSTALL_UNIT" 2>/dev/null || true)" in
    activating | active | deactivating | reloading)
        if [ "$((NOW + WINDOW))" -gt "$DEADLINE_EPOCH" ]; then
            umask 077
            printf '%s\n' "$((NOW + WINDOW))" > "$EXTENDED.part"
            mv "$EXTENDED.part" "$EXTENDED"
        fi
        echo "verify-manager: $INSTALL_UNIT is still running; nothing is judged yet" >&2
        exit 0
        ;;
esac

# ${Version} answers for a package dpkg unpacked and never configured, and for
# one it has only config files left for. Those are what the deadline exists to
# catch, so the state dpkg is in is read alongside the version.
KNOWN=$(dpkg-query -W -f '${db:Status-Status}|${Version}' "$PACKAGE" 2>/dev/null || true)
INSTALLED_STATE=${KNOWN%%|*}
case "$KNOWN" in
    *"|"*) INSTALLED=${KNOWN#*|} ;;
    *) INSTALLED= ;;
esac

healthy=yes
[ "$INSTALLED_STATE" = installed ] || healthy=no
[ -n "$EXPECTED" ] && [ "$INSTALLED" = "$EXPECTED" ] || healthy=no
for service in $SERVICES; do
    systemctl is-active --quiet "$service" || healthy=no
done

# A unit with Restart=on-failure that dies a second after it starts is "active"
# at almost any single look, so one healthy look confirmed a console that
# never answered. The processes are named by systemd's invocation id and
# restart count, and only a second healthy tick that finds the same ones
# confirms: they stayed up for a whole tick. The web service must also be
# listening; without ss to ask, that part is not known and not held against it.
SIGNATURE=
if [ "$healthy" = yes ]; then
    for service in $SERVICES; do
        SIGNATURE="$SIGNATURE $(systemctl show -p InvocationID -p NRestarts --value "$service" 2>/dev/null | tr '\n' ':')"
    done
    # configparser's own spellings: either separator, any case of the key, and
    # a comment after the value once whitespace separates it.
    PORT=$(sed -n 's/^[[:space:]]*[Ww][Ee][Bb]_[Pp][Oo][Rr][Tt][[:space:]]*[=:][[:space:]]*\([0-9][0-9]*\)\([[:space:]][[:space:]]*[#;].*\)\{0,1\}[[:space:]]*$/\1/p' "$CONFIG" 2>/dev/null | tail -n 1)
    if command -v ss >/dev/null 2>&1; then
        ss -Hltn "sport = :${PORT:-8088}" 2>/dev/null | grep -q . || healthy=no
    fi
fi

# What this proves is narrow and stated as such: the package dpkg reports is the
# one the install promised, and the two units that make the appliance reachable
# have been running for a tick, the web one listening. It is not a functional
# test of the manager.
if [ "$healthy" = yes ]; then
    if [ -f "$STABLE" ] && [ "$(cat "$STABLE")" = "$SIGNATURE" ]; then
        record confirmed "$PACKAGE $INSTALLED is installed and its services have stayed up"
        disarm
        exit 0
    fi
    umask 077
    printf '%s\n' "$SIGNATURE" > "$STABLE.part"
    mv "$STABLE.part" "$STABLE"
else
    rm -f "$STABLE"
fi

# One tick spent, healthy or not: a unit that keeps restarting looks healthy
# on every tick and must still run out of window. Read the way the revert
# attempts are.
TICKS_N=$(cat "$TICKS" 2>/dev/null || echo 0)
case "$TICKS_N" in '' | *[!0-9]*) TICKS_N=0 ;; esac
TICKS_N=$((TICKS_N + 1))
umask 077
printf '%s\n' "$TICKS_N" > "$TICKS.part"
mv "$TICKS.part" "$TICKS"

# A healthy look at the end of the window gets the ticks that can prove it
# stays up, rather than a revert of a working install.
GRACE=0
[ "$healthy" = yes ] && GRACE=$((2 * TICK_SECONDS))
if [ "$NOW" -lt "$((DEADLINE_EPOCH + GRACE))" ] \
    && [ "$((TICKS_N * TICK_SECONDS))" -lt "$((WINDOW + GRACE))" ]; then
    echo "verify-manager: not confirmed yet, $((DEADLINE_EPOCH + GRACE - NOW))s on the clock and" \
         "$((WINDOW + GRACE - TICKS_N * TICK_SECONDS))s of ticks left" >&2
    exit 0
fi

if [ -z "$PREVIOUS" ] || [ ! -f "$PREVIOUS" ]; then
    record revert_unavailable \
        "the deadline expired and this appliance has kept no earlier package to install"
    disarm
    exit 0
fi

# previous.deb is a slot, and retain() rewrites it on every install. An archive
# that is no longer the one this deadline kept is not a way back -- it may be
# the very package being undone -- so it is refused rather than installed.
if [ -n "$PREVIOUS_SHA" ]; then
    # "sha256:<hex>", the form artifact_trust.file_digest writes everywhere
    # else. A sha256sum that could not run leaves the prefix alone, which
    # matches nothing -- an archive that cannot be checked is not installed.
    ACTUAL="sha256:$(sha256sum "$PREVIOUS" 2>/dev/null | cut -d" " -f1 || true)"
    if [ "$ACTUAL" != "$PREVIOUS_SHA" ]; then
        record revert_unavailable \
            "the deadline expired and $PREVIOUS is no longer the package it kept"
        disarm
        exit 0
    fi
fi

# Counted before dpkg runs, not after: the unit's TimeoutStartSec can kill an
# old postinst that waits on its services, and an attempt counted only on
# return was then never counted, so the deadline tried for ever.
attempts=$(cat "$ATTEMPTS" 2>/dev/null || echo 0)
case "$attempts" in '' | *[!0-9]*) attempts=0 ;; esac
if [ "$attempts" -ge "$REVERT_ATTEMPTS" ]; then
    record revert_failed \
        "the deadline expired and $PREVIOUS could not be installed in $REVERT_ATTEMPTS attempts"
    disarm
    exit 0
fi
attempts=$((attempts + 1))
umask 077
printf '%s\n' "$attempts" > "$ATTEMPTS.part"
mv "$ATTEMPTS.part" "$ATTEMPTS"

echo "verify-manager: the deadline expired without a healthy $PACKAGE; reinstalling $PREVIOUS" >&2
if dpkg --force-confold --install "$PREVIOUS"; then
    record reverted "the deadline expired without a healthy $PACKAGE $EXPECTED; $PREVIOUS was put back"
    record_install_reverted "the verify deadline put $PREVIOUS back"
    disarm
    exit 0
fi

# Before the next attempt, not after the last one. The install unit's
# TimeoutStartSec and this window are both 900 s, so they run out together:
# systemd SIGTERMs the install cgroup with dpkg inside it and the database is
# left interrupted -- which is exactly what the refusal above is, and exactly
# what this clears. Running it only once the attempts were spent meant every
# retry failed for a reason already in hand. install-manager.sh does it in this
# order and says why.
dpkg --force-confold --configure -a || true

if [ "$attempts" -lt "$REVERT_ATTEMPTS" ]; then
    echo "verify-manager: $PREVIOUS was refused (attempt $attempts of $REVERT_ATTEMPTS); the next tick tries again" >&2
    exit 0
fi

record revert_failed \
    "the deadline expired and $PREVIOUS could not be installed in $REVERT_ATTEMPTS attempts"
disarm
exit 0
