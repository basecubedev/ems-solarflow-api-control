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
PACKAGE=ems-appliance-manager
TIMER=ems-appliance-manager-verify.timer
SERVICES="ems-appliance-agent.service ems-appliance-web.service"

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
    rm -f "$DEADLINE"
    systemctl disable --now "$TIMER" >/dev/null 2>&1 || true
}

if [ ! -f "$DEADLINE" ]; then
    disarm
    exit 0
fi

EXPECTED=$(text expected_version)
PREVIOUS=$(text previous_path)
DEADLINE_EPOCH=$(number deadline_epoch)
[ -n "$DEADLINE_EPOCH" ] || DEADLINE_EPOCH=0
NOW=$(date -u +%s)

INSTALLED=$(dpkg-query -W -f '${Version}' "$PACKAGE" 2>/dev/null || true)

healthy=yes
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

echo "verify-manager: the deadline expired without a healthy $PACKAGE; reinstalling $PREVIOUS" >&2
if dpkg --force-confold --install "$PREVIOUS"; then
    record reverted "the deadline expired without a healthy $PACKAGE $EXPECTED; $PREVIOUS was put back"
    disarm
    exit 0
fi

dpkg --configure -a || true
record revert_failed "the deadline expired and $PREVIOUS could not be installed either"
disarm
exit 0
