#!/bin/sh
# SPDX-License-Identifier: AGPL-3.0-or-later
# Prove the A/B unit ordering and failure propagation under a real systemd.
#
#   appliance-guest-ab-systemd.sh <rpi-image-gen-dir> <overlay> [appliance-disk]
#
# Runs *inside* a disposable guest that has already booted systemd and has the
# appliance package installed. It turns that guest into an A/B appliance the
# only honest way: upstream's own slot-perst and slot-shared generators, the
# project's own slot-shared declaration and activation links, and a real ext4
# filesystem behind /dev/disk/by-slot/persistent.
#
# Nothing here re-implements the mount mechanism. A generator change upstream,
# a declaration change here, or a dropped activation link shows up as a failing
# scenario rather than as a fixture that was updated to match.
#
# Exit status: 0 every scenario held, 1 one did not, 3 the guest cannot run it.
set -eu

GENERATOR_DIR=${1:?usage: appliance-guest-ab-systemd.sh <rpi-image-gen-dir> <overlay>}
OVERLAY=${2:?usage: appliance-guest-ab-systemd.sh <rpi-image-gen-dir> <overlay>}
AB_DISK=${3:-/dev/vdb}

PERSIST_MOUNT=/persistent
BY_SLOT=/dev/disk/by-slot
STATE_PATH=/var/lib/ems-appliance-manager
UPSTREAM_OVERLAY="$GENERATOR_DIR/image/gpt/ab_userdata/device/rootfs-overlay"
GENERATORS="$UPSTREAM_OVERLAY/usr/lib/systemd/system-generators"
IMAGE_RULES="$UPSTREAM_OVERLAY/etc/udev/rules.d/99-rpi-05-image.rules"
SLOT_SHARED_CONF="$OVERLAY/etc/rpi-image-gen/slot-shared.d/50-ems-appliance.conf"
WANTS_DIR="$OVERLAY/etc/systemd/system/local-fs.target.wants"
SSH_DROP_IN="$OVERLAY/etc/systemd/system/ssh.service.d/50-ems-appliance-host-identity.conf"

failures=0
pass() { printf '  PASS  %s\n' "$1"; }
fail() { printf '  FAIL  %s\n' "$1" >&2; failures=$((failures + 1)); }
step() { printf '\n== %s ==\n' "$1"; }
not_run() { printf 'appliance-guest-ab-systemd: %s\nRESULT: NOT RUN (%s)\n' "$1" "$2" >&2; exit 3; }

[ "$(id -u)" -eq 0 ] || not_run "this scenario needs root in the guest" not_root
[ -d "$GENERATORS" ] || not_run "no upstream generators at $GENERATORS" generators_unavailable
[ -f "$SLOT_SHARED_CONF" ] || not_run "no slot-shared declaration at $SLOT_SHARED_CONF" overlay_unavailable
[ -d "$WANTS_DIR" ] || not_run "no activation links at $WANTS_DIR" overlay_unavailable
[ -f "$IMAGE_RULES" ] || not_run "no image-rota udev rules at $IMAGE_RULES" generators_unavailable
for tool in sfdisk mkfs.ext4 mkfs.vfat udevadm findmnt; do
    command -v "$tool" >/dev/null 2>&1 || not_run "$tool is missing" "${tool}_unavailable"
done

# The scenarios below deliberately withhold sshd. Leaving a disposable guest
# with an unstartable sshd is how an investigation loses the guest it was
# investigating, so the drop-in comes back out whatever happens.
restore_ssh() {
    rm -f /etc/systemd/system/ssh.service.d/50-ems-appliance-host-identity.conf
    rmdir /etc/systemd/system/ssh.service.d 2>/dev/null || true
    systemctl daemon-reload 2>/dev/null || true
    systemctl start ssh.service 2>/dev/null || true
}
trap restore_ssh EXIT INT TERM

diagnose() {
    printf '  ---- %s ----\n' "$1"
    systemctl status "$1" --no-pager -l 2>&1 | sed -n '1,6p;/ExecStart/,$p' | head -20
    journalctl -u "$1" -n 20 --no-pager --output=cat 2>&1 | tail -15
    # The units run --quiet, which is right for a boot log and useless here.
    case "$1" in
        ems-appliance-host-identity.service)
            ems-appliance host-identity --json 2>&1 | tail -40 ;;
        ems-appliance-persistence.service)
            ems-appliance ab verify-persistence --json 2>&1 | tail -40 ;;
    esac
}

expect_active() {
    if systemctl is-active --quiet "$1"; then
        pass "$2"
    else
        fail "$2 (is-active: $(systemctl is-active "$1" 2>&1))"
        diagnose "$1"
    fi
}

expect_inactive() {
    if systemctl is-active --quiet "$1"; then fail "$2 (unexpectedly active)"; else pass "$2"; fi
}

shared_paths() {
    while IFS= read -r line; do
        case "$line" in Path=*) printf '%s\n' "${line#Path=}" ;; esac
    done < "$SLOT_SHARED_CONF"
}

step "an A/B appliance assembled from the real generators"

install -m 0755 "$GENERATORS/slot-perst-generator" /usr/lib/systemd/system-generators/
install -m 0755 "$GENERATORS/slot-shared-generator" /usr/lib/systemd/system-generators/
install -d /etc/rpi-image-gen/slot-shared.d
install -m 0644 "$SLOT_SHARED_CONF" /etc/rpi-image-gen/slot-shared.d/50-ems-appliance.conf

[ -b "$AB_DISK" ] || not_run "no appliance disk at $AB_DISK" ab_disk_unavailable

if ! blkid -o value -s PARTLABEL "${AB_DISK}1" >/dev/null 2>&1; then
    sfdisk --quiet --label gpt "$AB_DISK" <<'EOF'
name=bootconfig, size=32M, type=uefi
name=boot_a, size=128M, type=uefi
name=boot_b, size=128M, type=uefi
name=system_a, size=512M
name=system_b, size=512M
name=persistent
EOF
    udevadm settle
    mkfs.vfat -n bootconfig "${AB_DISK}1" >/dev/null
    mkfs.vfat -n boot_a "${AB_DISK}2" >/dev/null
    mkfs.vfat -n boot_b "${AB_DISK}3" >/dev/null
    mkfs.ext4 -q -L system_a "${AB_DISK}4"
    mkfs.ext4 -q -L system_b "${AB_DISK}5"
    mkfs.ext4 -q -L persistent "${AB_DISK}6"
fi

# Which disk the appliance booted from is a firmware question: upstream's
# storage-binder answers it from the Raspberry Pi bootloader's device tree,
# which a generic guest does not have. Only that answer is substituted. The
# rule that turns the answer into /dev/disk/by-slot/persistent is upstream's
# own, copied out of the image-rota layer unmodified.
printf 'SUBSYSTEM=="block", KERNEL=="%s*", ENV{RPI_ONBOOTDEV}="1"\n' \
    "$(basename "$AB_DISK")" >/etc/udev/rules.d/60-ems-appliance-guest-bootdev.rules
install -m 0644 "$IMAGE_RULES" /etc/udev/rules.d/99-rpi-05-image.rules
udevadm control --reload
udevadm trigger --subsystem-match=block
udevadm settle
[ -e "$BY_SLOT/persistent" ] || not_run "udev did not create the by-slot alias" by_slot_unavailable

install -d "$PERSIST_MOUNT"
mount "$BY_SLOT/persistent" "$PERSIST_MOUNT"
for path in $(shared_paths); do
    install -d "$PERSIST_MOUNT/shared${path}"
    [ -d "$path" ] && cp -a "$path/." "$PERSIST_MOUNT/shared${path}/" 2>/dev/null || true
done
install -d "$PERSIST_MOUNT/slots/system_a/var" "$PERSIST_MOUNT/common/etc"
umount "$PERSIST_MOUNT"

install -d /etc/systemd/system/local-fs.target.wants
cp -a "$WANTS_DIR/." /etc/systemd/system/local-fs.target.wants/

systemctl daemon-reload
systemctl start persistent.mount
for path in $(shared_paths); do
    systemctl start "$(systemd-escape --path "$path").mount"
done

# The same call the image layer makes at build time. A descriptor written by
# hand here would only prove that this script and the verifier agree.
/usr/bin/ems-appliance ab write-layout \
    --output /etc/ems-appliance-manager/ab-layout.json >/dev/null

declared=0
bound=0
for path in $(shared_paths); do
    declared=$((declared + 1))
    findmnt -no TARGET "$path" >/dev/null 2>&1 && bound=$((bound + 1))
done
# Counted from the declaration rather than compared against a literal: the
# declared set has grown before, and a hard-coded number turns that growth into
# a failing check instead of a passing one.
if [ "$declared" -gt 0 ] && [ "$bound" -eq "$declared" ]; then
    pass "all $declared shared paths are bound through upstream's generator"
else
    fail "only $bound of $declared shared paths are bound"
fi

step "what a generic guest can and cannot answer"

# A healthy A/B verdict is not one of them. appliance/ab_layout.py anchors
# discovery on /proc/device-tree/chosen/bootloader/partition — the partition
# the Raspberry Pi firmware says it booted. Without it there is no booted
# medium, so no slot and no persistent partition resolve, and the runtime
# correctly refuses to call this an A/B appliance. Faking that property would
# make the verdict a statement about the fake.
#
# So the healthy verdict belongs to the hardware gate. What is provable here is
# every mechanism underneath it: the binds, the fail-open catch, the ordering,
# the propagation and the identity.
printf '  NOT RUN  a healthy A/B verdict (firmware_boot_partition_not_emulatable)\n'

step "healthy boot"

# The image seeds this on first boot; the identity is stable across slots only
# because it is read from the persistent partition rather than from this root.
install -d "$PERSIST_MOUNT/common/etc"
[ -s "$PERSIST_MOUNT/common/etc/machine-id" ] || cp /etc/machine-id "$PERSIST_MOUNT/common/etc/machine-id"
install -d /etc/ssh/sshd_config.d
install -m 0644 "$OVERLAY/etc/ssh/sshd_config.d/50-ems-appliance-hostkeys.conf" \
    /etc/ssh/sshd_config.d/

rm -rf "$STATE_PATH/ssh"
systemctl reset-failed ems-appliance-host-identity.service 2>/dev/null || true
systemctl restart ems-appliance-host-identity.service || true
expect_active ems-appliance-host-identity.service "host identity is established on first boot"
for key in ed25519 rsa ecdsa; do
    if [ -s "$STATE_PATH/ssh/ssh_host_${key}_key" ] \
       && [ -s "$STATE_PATH/ssh/ssh_host_${key}_key.pub" ]; then
        pass "the $key host key pair was generated onto the persistent partition"
    else
        fail "no $key host key pair was generated"
    fi
done

expect_dependency() {
    if systemctl show "$1" -p "$2" --value | tr ' ' '\n' | grep -qx "$3"; then
        pass "$1 declares $2=$3"
    else
        fail "$1 does not declare $2=$3"
    fi
}

expect_dependency ems-appliance-agent.service After ems-appliance-persistence.service
expect_dependency ems-appliance-agent.service Requires ems-appliance-persistence.service
expect_dependency ems-appliance-persistence.service Requires persistent.mount
expect_dependency ems-appliance-persistence.service After ems-appliance-host-identity.service

step "the verifier catches upstream's fail-open bind"

# Upstream guards each bind with ConditionPathIsDirectory, so a missing source
# makes the mount skip itself and the path silently falls back to the read-only
# root. That is the failure the persistence unit exists to catch, and it is
# checked per path so the verdict is attributable to this bind and not to the
# medium this guest could not identify.
skipped_paths() {
    ems-appliance ab verify-persistence --json 2>/dev/null \
        | python3 -c 'import json,sys; print(" ".join(e["target"] for e in json.load(sys.stdin).get("paths", []) if "skipped it" in (e.get("problem") or "")))'
}

before=$(skipped_paths)
if [ -z "$before" ]; then
    pass "with all six binds up, no path reports a skipped bind"
else
    fail "a bind was already reported as skipped: $before"
fi

systemctl stop ems-appliance-agent.service ems-appliance-web.service || true
state_unit=$(systemd-escape --path "$STATE_PATH").mount
systemctl stop "$state_unit"
mount "$BY_SLOT/persistent" /mnt
mv "/mnt/shared$STATE_PATH" "/mnt/shared${STATE_PATH}.moved"
umount /mnt
systemctl daemon-reload
systemctl start "$state_unit" 2>/dev/null || true
expect_inactive "$state_unit" "the shared bind skips itself rather than failing"

after=$(skipped_paths)
if [ "$after" = "$STATE_PATH" ]; then
    pass "the verifier names exactly the path whose bind was skipped"
else
    fail "the verifier reported skipped paths [$after], expected [$STATE_PATH]"
fi

step "a failed persistence unit stops every writer"

systemctl reset-failed ems-appliance-persistence.service 2>/dev/null || true
systemctl restart ems-appliance-persistence.service 2>/dev/null || true
expect_inactive ems-appliance-persistence.service "persistence refuses to report success"
for unit in ems-appliance-agent ems-appliance-web ems-appliance-slot-bootstrap ems-appliance-ab-health; do
    systemctl start "$unit.service" 2>/dev/null || true
    expect_inactive "$unit.service" "$unit does not start"
done

step "recovery"

mount "$BY_SLOT/persistent" /mnt
mv "/mnt/shared${STATE_PATH}.moved" "/mnt/shared$STATE_PATH"
umount /mnt
systemctl daemon-reload
systemctl start "$state_unit"
expect_active "$state_unit" "the shared bind comes back once its source is back"
recovered=$(skipped_paths)
if [ -z "$recovered" ]; then
    pass "the verifier stops reporting a skipped bind"
else
    fail "the verifier still reports skipped paths: $recovered"
fi

step "an unprovable host identity withholds SSH"

if [ -f "$SSH_DROP_IN" ]; then
    install -d /etc/systemd/system/ssh.service.d
    install -m 0644 "$SSH_DROP_IN" /etc/systemd/system/ssh.service.d/
    systemctl daemon-reload
    pass "the image's ssh drop-in is in place"
else
    fail "the image ships no ssh drop-in"
fi

systemctl stop ssh.service 2>/dev/null || true
identity_dir="$STATE_PATH/ssh"
rm -rf "$identity_dir"
: >"$identity_dir"
systemctl reset-failed ems-appliance-host-identity.service 2>/dev/null || true
systemctl restart ems-appliance-host-identity.service 2>/dev/null || true
expect_inactive ems-appliance-host-identity.service "host identity fails on an unusable key directory"
systemctl start ssh.service 2>/dev/null || true
expect_inactive ssh.service "sshd does not start behind a failed identity"

rm -f "$identity_dir"
systemctl reset-failed ems-appliance-host-identity.service 2>/dev/null || true
systemctl restart ems-appliance-host-identity.service || true
expect_active ems-appliance-host-identity.service "host identity recovers"
systemctl start ssh.service || true
expect_active ssh.service "sshd starts again behind a proven identity"

printf '\n'
if [ "$failures" -eq 0 ]; then
    echo "RESULT: PASS"
    exit 0
fi
echo "RESULT: FAIL ($failures)"
exit 1
