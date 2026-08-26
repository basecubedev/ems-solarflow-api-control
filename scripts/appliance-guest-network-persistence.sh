#!/bin/bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Whether NetworkManager really refuses to start against a fallback directory.
#
#   scripts/appliance-guest-network-persistence.sh <rootfs-overlay> [evidence]
#
# Run as root in a disposable Debian guest with a running systemd and the
# package installed.
#
# /etc/NetworkManager/system-connections is one of the shared paths bound
# from the persistent partition, so it holds the profiles and credentials that
# make the appliance reachable. Upstream's slot-shared generator guards each
# bind with a condition and therefore fails open: with the persistent source
# missing the bind is skipped, NetworkManager comes up against the empty
# slot-local directory the image shipped, reports itself healthy, and writes new
# profiles somewhere the next slot switch discards.
#
# The image ships a drop-in making NetworkManager Require= the verification
# service, which is the one unit that fails closed. That is a claim about what
# systemd does with two unit files, and until now it was only ever checked by
# reading them. This asks systemd.
#
# Both halves are run against the same guest: a healthy slot where the bind is
# there and NetworkManager starts, and a broken one where the persistent source
# is gone and it must not.
#
# Exit status: 0 every check passed, 1 a check failed, 3 a prerequisite is
# missing.
set -uo pipefail

PERSISTENT=/persistent
SHARED=/etc/NetworkManager/system-connections
LAYOUT=/etc/ems-appliance-manager/ab-layout.json
OVERLAY=${1:?usage: appliance-guest-network-persistence.sh <rootfs-overlay> [evidence]}
EVIDENCE=${2:-/root/network-persistence-evidence.txt}
DROP_IN=etc/systemd/system/NetworkManager.service.d/50-ems-appliance-persistence.conf

failures=0
skipped=0
step() { printf '\n== %s ==\n' "$1"; }
pass() { printf '  PASS  %s\n' "$1"; }
fail() { printf '  FAIL  %s\n' "$1"; failures=$((failures + 1)); }

not_run() {
    echo "appliance-guest-network-persistence: $1" >&2
    echo "RESULT: NOT RUN ($2)" >&2
    exit 3
}

for tool in systemctl findmnt mount umount; do
    command -v "$tool" >/dev/null 2>&1 || not_run "$tool is missing" "${tool}_unavailable"
done
[ -d /run/systemd/system ] || not_run "systemd is not the init of this guest" systemd_unavailable
command -v ems-appliance >/dev/null 2>&1 || not_run "the package is not installed" package_missing
systemctl list-unit-files NetworkManager.service >/dev/null 2>&1 \
    || not_run "NetworkManager is not installed" networkmanager_unavailable
[ -f "$LAYOUT" ] \
    || not_run "this guest carries no A/B layout manifest, so the verification unit is \
conditioned out" ab_layout_missing
[ -f "$OVERLAY/$DROP_IN" ] || not_run "the image overlay carries no $DROP_IN" drop_in_missing

: >"$EVIDENCE"
record() { printf '\n--- %s ---\n' "$1" >>"$EVIDENCE"; shift; "$@" >>"$EVIDENCE" 2>&1; }

step "NetworkManager can start in this guest at all"
# The control for everything below. Without it, "NetworkManager is refused"
# would be equally true of a guest where NetworkManager simply does not work,
# and the refusal would prove nothing about the drop-in.
rm -f "/$DROP_IN"
systemctl daemon-reload
systemctl stop NetworkManager.service 2>/dev/null
if systemctl start NetworkManager.service 2>>"$EVIDENCE"; then
    pass "NetworkManager starts here with no appliance drop-in"
else
    fail "NetworkManager does not start in this guest even unconstrained"
    record "unconstrained NetworkManager" journalctl -u NetworkManager.service -n 40
fi
systemctl stop NetworkManager.service 2>/dev/null

step "the image drop-in, installed the way the image ships it"
# The drop-in is an image layer, not a package file: a guest that installed only
# the .deb has no reason to carry it, and asking systemd about a unit file that
# is not there would answer a question about this guest rather than about the
# appliance.
install -D -m 0644 "$OVERLAY/$DROP_IN" "/$DROP_IN"
systemctl daemon-reload
pass "the image's NetworkManager drop-in is installed"

step "the dependency systemd actually loaded"
DEPENDENCY=$(systemctl show -p Requires -p After --value NetworkManager.service | tr ' ' '\n')
record "systemctl show NetworkManager.service" systemctl show \
    -p Requires -p After -p Wants NetworkManager.service
if systemctl show -p Requires --value NetworkManager.service \
        | tr ' ' '\n' | grep -qx ems-appliance-persistence.service; then
    pass "systemd loaded Requires=ems-appliance-persistence.service"
else
    fail "NetworkManager does not Require the persistence unit: $DEPENDENCY"
fi
if systemctl show -p After --value NetworkManager.service \
        | tr ' ' '\n' | grep -qx ems-appliance-persistence.service; then
    pass "systemd loaded After=ems-appliance-persistence.service"
else
    fail "NetworkManager is not ordered after the persistence unit"
fi
record "systemctl list-dependencies" systemctl list-dependencies NetworkManager.service

step "a healthy slot, if this guest can present one"
# Taken as found. A guest whose A/B scenarios have already run their recovery
# cases may not have a provable persistent partition left, and re-mounting one
# here would be this script proving something about itself. When it cannot be
# had, the case says so: the control above already shows the refusal below is
# not vacuous.
healthy=0
systemctl reset-failed ems-appliance-persistence.service 2>/dev/null
if systemctl start ems-appliance-persistence.service 2>>"$EVIDENCE"; then
    healthy=1
    pass "the persistence verification passes on this guest as found"
    systemctl stop NetworkManager.service 2>/dev/null
    if systemctl start NetworkManager.service 2>>"$EVIDENCE"; then
        pass "NetworkManager starts once persistence is proven"
    else
        fail "NetworkManager would not start with persistence proven"
        record "healthy NetworkManager journal" journalctl -u NetworkManager.service -n 40
    fi
else
    skipped=$((skipped + 1))
    printf '  NOT RUN  a healthy slot: this guest cannot prove its persistence\n'
    ems-appliance ab verify-persistence 2>&1 | sed 's/^/    /' | head -12
    echo "  prerequisite: a guest whose persistent partition and shared binds"
    echo "  are still the ones the A/B scenarios established"
fi

step "a broken slot: the persistent source is unavailable"
systemctl stop NetworkManager.service 2>/dev/null
systemctl stop ems-appliance-persistence.service 2>/dev/null
systemctl reset-failed ems-appliance-persistence.service 2>/dev/null
umount "$SHARED" 2>/dev/null
mv "$PERSISTENT/shared$SHARED" "$PERSISTENT/shared$SHARED.away" 2>/dev/null
record "findmnt (broken)" findmnt -T "$SHARED"

if systemctl start ems-appliance-persistence.service 2>>"$EVIDENCE"; then
    fail "the persistence verification passed with no persistent source"
else
    pass "the persistence verification fails closed"
fi

# The point of Requires=: systemd must refuse the dependent job, not merely
# order it after a unit that failed.
if systemctl start NetworkManager.service 2>>"$EVIDENCE"; then
    fail "NetworkManager started against the slot-local fallback directory"
else
    pass "NetworkManager is refused while the persistent source is unavailable"
fi
record "NetworkManager state (broken)" systemctl show \
    -p ActiveState -p SubState -p Result NetworkManager.service
if [ "$(systemctl show -p ActiveState --value NetworkManager.service)" = active ]; then
    fail "NetworkManager is active after a failed persistence verification"
else
    pass "NetworkManager is not active after a failed persistence verification"
fi
if [ -n "$(ls -A "$SHARED" 2>/dev/null)" ]; then
    fail "the slot-local fallback directory carries profiles"
else
    pass "nothing was consumed from a slot-local fallback"
fi

step "restoring the guest"
if [ -d "$PERSISTENT/shared$SHARED.away" ]; then
    mv "$PERSISTENT/shared$SHARED.away" "$PERSISTENT/shared$SHARED"
fi
systemctl reset-failed ems-appliance-persistence.service 2>/dev/null
if [ "$healthy" -eq 1 ]; then
    if systemctl start ems-appliance-persistence.service >/dev/null 2>&1; then
        pass "the healthy slot recovers once the source is back"
    else
        fail "the healthy slot did not recover"
        ems-appliance ab verify-persistence 2>&1 | sed 's/^/    /' | head -12
    fi
fi

printf '\nevidence: %s\n' "$EVIDENCE"
if [ "$failures" -ne 0 ]; then
    echo "RESULT: FAIL ($failures)"
    exit 1
fi
if [ "$skipped" -ne 0 ]; then
    echo "RESULT: PASS ($skipped case(s) NOT RUN; the refusal was proven, the control held)"
else
    echo "RESULT: PASS"
fi
