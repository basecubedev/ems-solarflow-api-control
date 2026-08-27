#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Build the amd64 package and prove it in a Debian guest that really booted.
#
#   scripts/appliance-smoke-vm-amd64.sh [--keep] [--cache DIR] [--ssh-port N]
#                                       [--memory MB]
#                                       [--evidence DIR]
#
# The container tier this replaces could not finish a systemd boot, so every
# unit-ordering and failure-propagation claim it made was a claim about a
# process namespace rather than about an appliance. This boots a disposable
# Debian Trixie cloud image under QEMU/KVM, installs the package the operator
# would install, and runs the same guest smoke script the ARM64 driver runs.
#
# --evidence writes one log per runtime gate into a directory, so a release can
# bind what the guest proved rather than a paragraph somebody wrote afterwards.
#
# Nothing on the developer host is modified: the base image is cached, the
# overlay, the key pair and the seed live in a temporary directory, and the
# guest is destroyed on exit.
#
# Exit status: 0 every check passed, 1 a check failed, 2 the command line is
# wrong, 3 the environment cannot run the test. A skipped run is never a pass.
set -euo pipefail

ROOT=$(cd "$(dirname "$0")/.." && pwd)
CACHE=${EMS_APPLIANCE_VM_CACHE:-${XDG_CACHE_HOME:-$HOME/.cache}/ems-appliance-vm}
SSH_PORT=${EMS_APPLIANCE_VM_SSH_PORT:-2222}
MEMORY=2560
KEEP=0
EVIDENCE=

usage() { sed -n '3,26p' "$0"; }

not_run() {
    echo "appliance-smoke-vm-amd64: $1" >&2
    echo "RESULT: NOT RUN ($2)" >&2
    exit 3
}

while [ $# -gt 0 ]; do
    case "$1" in
        --keep) KEEP=1; shift ;;
        --cache) CACHE=${2:?--cache needs a directory}; shift 2 ;;
        --ssh-port) SSH_PORT=${2:?--ssh-port needs a port}; shift 2 ;;
        --memory) MEMORY=${2:?--memory needs a size in MB}; shift 2 ;;
        --evidence) EVIDENCE=${2:?--evidence needs a directory}; shift 2 ;;
        -h|--help) usage; exit 0 ;;
        *) echo "unknown argument: $1" >&2; usage >&2; exit 2 ;;
    esac
done

for tool in qemu-system-x86_64 qemu-img ssh scp ssh-keygen curl; do
    command -v "$tool" >/dev/null 2>&1 || not_run "$tool is missing" "${tool}_unavailable"
done
command -v genisoimage >/dev/null 2>&1 || command -v xorriso >/dev/null 2>&1 \
    || not_run "no ISO writer (genisoimage or xorriso)" iso_writer_unavailable
[ -w /dev/kvm ] || not_run "/dev/kvm is not writable by this user" kvm_unavailable

WORK=$(mktemp -d "${TMPDIR:-/tmp}/ems-appliance-vm.XXXXXX")
GUEST_PID=""
cleanup() {
    if [ "$KEEP" -eq 1 ]; then
        echo "kept: $WORK (guest pid ${GUEST_PID:-none})"
        echo "  ssh -i $WORK/key -p $SSH_PORT root@127.0.0.1"
        return
    fi
    if [ -n "$GUEST_PID" ] && kill -0 "$GUEST_PID" 2>/dev/null; then
        kill "$GUEST_PID" 2>/dev/null || true
        wait "$GUEST_PID" 2>/dev/null || true
    fi
    rm -rf "$WORK"
}
trap cleanup EXIT

mkdir -p "$CACHE"
# One acquisition policy for every disposable guest: the digest this project
# pinned, re-checked immediately before the boot that uses it. A checksum file
# that happens to be absent used to mean the cached image was accepted.
set +e
BASE_IMAGE_PATH=$(sh "$ROOT/scripts/appliance-guest-base-image.sh" \
    --role smoke-amd64 --cache "$CACHE")
base_status=$?
set -e
case "$base_status" in
    0) ;;
    3) not_run "the pinned guest base image could not be obtained" base_image_unavailable ;;
    *) echo "RESULT: FAIL (base_image_unverified)" >&2; exit 1 ;;
esac

echo "== building the amd64 package =="
"$ROOT/packaging/appliance/build-deb.sh" --output "$WORK" --arch amd64 >/dev/null
PACKAGE=$(ls "$WORK"/ems-appliance-manager_*_amd64.deb)

echo "== preparing the guest =="
ssh-keygen -q -t ed25519 -N '' -C ems-appliance-vm -f "$WORK/key"
mkdir -p "$WORK/seed"
cat >"$WORK/seed/meta-data" <<EOF
instance-id: ems-appliance-vm-$$
local-hostname: ems-appliance-vm
EOF
cat >"$WORK/seed/user-data" <<EOF
#cloud-config
disable_root: false
ssh_pwauth: false
users:
  - name: root
    ssh_authorized_keys:
      - $(cat "$WORK/key.pub")
growpart:
  mode: auto
  devices: ['/']
resize_rootfs: true
EOF
if command -v genisoimage >/dev/null 2>&1; then
    genisoimage -quiet -output "$WORK/seed.iso" -volid CIDATA -joliet -rock \
        "$WORK/seed/user-data" "$WORK/seed/meta-data"
else
    xorriso -as mkisofs -quiet -output "$WORK/seed.iso" -volid CIDATA -joliet -rock \
        "$WORK/seed/user-data" "$WORK/seed/meta-data"
fi

qemu-img create -f qcow2 -b "$BASE_IMAGE_PATH" -F qcow2 "$WORK/guest.qcow2" 40G >/dev/null
qemu-system-x86_64 \
    -name ems-appliance-vm \
    -enable-kvm -cpu host -smp "$(nproc)" -m "$MEMORY" \
    -drive file="$WORK/guest.qcow2",if=virtio,format=qcow2 \
    -drive file="$WORK/seed.iso",media=cdrom,format=raw \
    -nic user,model=virtio-net-pci,hostfwd=tcp:127.0.0.1:"$SSH_PORT"-:22 \
    -display none -serial file:"$WORK/console.log" &
GUEST_PID=$!

g() {
    ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR \
        -o ConnectTimeout=5 -i "$WORK/key" -p "$SSH_PORT" root@127.0.0.1 "$@"
}
gcp() {
    scp -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR \
        -i "$WORK/key" -P "$SSH_PORT" "$@"
}

echo "== waiting for the guest to boot =="
booted=0
for _ in $(seq 1 60); do
    if g true 2>/dev/null; then booted=1; break; fi
    sleep 5
done
[ "$booted" -eq 1 ] || not_run "the guest never became reachable over ssh" guest_boot_failed
g 'systemctl is-system-running --wait >/dev/null 2>&1 || true'
echo "guest: $(g '. /etc/os-release && echo "$PRETTY_NAME"') kernel $(g uname -r)"

echo "== installing the guest baseline =="
g 'export DEBIAN_FRONTEND=noninteractive
   apt-get update -qq
   apt-get install -y -qq openssh-sftp-server acl docker.io docker-cli gpgv \
       jq curl e2fsprogs >/dev/null' \
    || not_run "the guest baseline could not be installed" guest_apt_failed

gcp "$PACKAGE" "$ROOT/scripts/appliance-guest-smoke.sh" \
    "$ROOT/scripts/appliance-guest-sftp-lifecycle.sh" \
    "$ROOT/scripts/appliance-guest-sftp-session.sh" \
    "$ROOT/scripts/appliance-guest-issue-backup-key.sh" root@127.0.0.1:/root/ >/dev/null

[ -n "$EVIDENCE" ] && mkdir -p "$EVIDENCE"
# One log per gate, kept whatever the verdict was: a gate that failed is
# evidence too, and a release that recorded only the passes would be a summary.
gate_log() {
    [ -n "$EVIDENCE" ] || { cat; return; }
    tee "$EVIDENCE/$1.log"
}

status=0
echo "== packaged smoke =="
g "bash /root/appliance-guest-smoke.sh /root/$(basename "$PACKAGE") amd64" || status=$?

echo
echo "== the backup account's confinement, asked of a real sshd =="
# sshd -T -C is sshd's own answer for a connection by that account. A claim
# about the drop-in's text is not a claim about what sshd does with it.
lifecycle=0
# pipefail would make a NOT RUN tier end the whole run; the verdict is
# read from PIPESTATUS and classified below instead.
set +e
g "bash /root/appliance-guest-sftp-lifecycle.sh" 2>&1 | gate_log sftp-effective-policy
lifecycle=${PIPESTATUS[0]}
set -e
if [ "$lifecycle" -eq 3 ]; then
    echo "appliance-smoke-vm-amd64: the SFTP lifecycle tier did not run" >&2
elif [ "$lifecycle" -ne 0 ]; then
    status=$lifecycle
fi

echo
echo "== a real SFTP session, with a key the appliance issued =="
# sshd -T answers what sshd would do. Only a session answers whether an
# operator can actually fetch a backup, and whether the chroot is a boundary.
# pipefail would make a NOT RUN tier end the whole run; the verdict is
# read from PIPESTATUS and classified below instead.
set +e
g "bash /root/appliance-guest-sftp-session.sh" 2>&1 | gate_log sftp-session
session=${PIPESTATUS[0]}
set -e
if [ "$session" -eq 3 ]; then
    echo "appliance-smoke-vm-amd64: the SFTP session tier did not run" >&2
elif [ "$session" -ne 0 ]; then
    status=$session
fi

echo
if [ "$status" -eq 0 ]; then
    echo "RESULT: PASS"
else
    echo "RESULT: FAIL"
fi
exit "$status"
