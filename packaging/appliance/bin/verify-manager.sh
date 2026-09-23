#!/bin/sh
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
ATTEMPTS="$STATE/verify-revert-attempts"
PACKAGE=ems-appliance-manager
TIMER=ems-appliance-manager-verify.timer
SERVICES="ems-appliance-agent.service ems-appliance-web.service"

# The dpkg frontend lock is held for as long as an operator's apt run takes, and
# repairing the package manager is the console action a bad install invites. One
# attempt spends the only automatic way back on a condition that clears itself.
REVERT_ATTEMPTS=5

# The one deadline record layout this reverter can act on: the number
# manager_verify.DEADLINE_SCHEMA_VERSION writes, and a test holds the two
# together. Fields read out of a record with another number may not mean
# what they meant here.
DEADLINE_SCHEMA=1

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

disarm() {
    rm -f "$DEADLINE" "$ATTEMPTS"
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
if [ "$SCHEMA" != "$DEADLINE_SCHEMA" ] || [ -z "$DEADLINE_EPOCH" ]; then
    record revert_unavailable \
        "the deadline record could not be read by this reverter (schema ${SCHEMA:-none}); nothing was judged and nothing was installed"
    disarm
    exit 0
fi

EXPECTED=$(text expected_version)
PREVIOUS=$(text previous_path)
PREVIOUS_SHA=$(text previous_sha256)
NOW=$(date -u +%s)

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

# What this proves is narrow and stated as such: the package dpkg reports is the
# one the install promised, and the two units that make the appliance reachable
# are running. It is not a functional test of the manager.
if [ "$healthy" = yes ]; then
    record confirmed "$PACKAGE $INSTALLED is installed and its services are running"
    disarm
    exit 0
fi

if [ "$NOW" -lt "$DEADLINE_EPOCH" ]; then
    echo "verify-manager: not healthy yet, $((DEADLINE_EPOCH - NOW))s left" >&2
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

echo "verify-manager: the deadline expired without a healthy $PACKAGE; reinstalling $PREVIOUS" >&2
if dpkg --force-confold --install "$PREVIOUS"; then
    record reverted "the deadline expired without a healthy $PACKAGE $EXPECTED; $PREVIOUS was put back"
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
dpkg --configure -a || true

attempts=$(cat "$ATTEMPTS" 2>/dev/null || echo 0)
case "$attempts" in '' | *[!0-9]*) attempts=0 ;; esac
attempts=$((attempts + 1))
if [ "$attempts" -lt "$REVERT_ATTEMPTS" ]; then
    umask 077
    printf '%s\n' "$attempts" > "$ATTEMPTS.part"
    mv "$ATTEMPTS.part" "$ATTEMPTS"
    echo "verify-manager: $PREVIOUS was refused (attempt $attempts of $REVERT_ATTEMPTS); the next tick tries again" >&2
    exit 0
fi

record revert_failed \
    "the deadline expired and $PREVIOUS could not be installed in $REVERT_ATTEMPTS attempts"
disarm
exit 0
