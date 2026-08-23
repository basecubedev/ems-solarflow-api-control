#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Provision a disposable builder VM and build the real A/B images in it.
#
#   scripts/appliance-builder-vm.sh --profile rpi5 [--profile rpi4]...
#                                   [--output DIR] [--cache DIR] [--keep]
#                                   [--memory MB] [--disk SIZE] [--build-id ID]
#                                   [--release-gate]
#
# rpi-image-gen needs mmdebstrap, podman, loop devices, a qemu-aarch64 binfmt
# handler and root. Installing that on a developer's workstation to build one
# image is not a reasonable trade, so it goes into a throwaway Debian Trixie
# guest instead: the packages land in the guest, the guest is deleted, and the
# only thing that comes back out is the dist directory.
#
# The build itself is the project's own scripts/appliance-build-rpi-ab-image.sh,
# run against the pinned generator, so the source-authority and post-build
# re-verification are exactly the ones a release uses.
#
# --release-gate runs scripts/appliance-release-gates.sh in the guest instead,
# and it is the only place that gate can reach PASS: the gate builds the images
# itself, so it needs the prerequisites that are deliberately not on a
# developer's workstation. Its verdict and its per-gate logs are what come back.
#
# Exit status: 0 every requested profile built (or every required gate passed),
# 1 a build or a gate failed, 2 the command line is wrong, 3 the environment
# cannot run the builder, or a required gate never executed.
set -euo pipefail

ROOT=$(cd "$(dirname "$0")/.." && pwd)
. "$ROOT/scripts/lib/workdir.sh"
# the builder VM grows to 60G and collects multi-GB artefacts beside it.
BUILDER_WORK_BYTES=$((70 * 1024 * 1024 * 1024))
CACHE=${EMS_APPLIANCE_VM_CACHE:-${XDG_CACHE_HOME:-$HOME/.cache}/ems-appliance-vm}
SSH_PORT=${EMS_APPLIANCE_BUILDER_SSH_PORT:-2322}
OUTPUT="$ROOT/dist"
MEMORY=3072
DISK=60G
KEEP=0
BUILD_ID=""
RELEASE_GATE=0
PROFILES=()

usage() { sed -n '3,20p' "$0"; }

not_run() {
    echo "appliance-builder-vm: $1" >&2
    echo "RESULT: NOT RUN ($2)" >&2
    exit 3
}

while [ $# -gt 0 ]; do
    case "$1" in
        --profile) PROFILES+=("${2:?--profile needs rpi4 or rpi5}"); shift 2 ;;
        --output) OUTPUT=${2:?--output needs a directory}; shift 2 ;;
        --cache) CACHE=${2:?--cache needs a directory}; shift 2 ;;
        --memory) MEMORY=${2:?--memory needs a size in MB}; shift 2 ;;
        --disk) DISK=${2:?--disk needs a size}; shift 2 ;;
        --ssh-port) SSH_PORT=${2:?--ssh-port needs a port}; shift 2 ;;
        --build-id) BUILD_ID=${2:?--build-id needs an identifier}; shift 2 ;;
        --release-gate) RELEASE_GATE=1; shift ;;
        --keep) KEEP=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) echo "unknown argument: $1" >&2; usage >&2; exit 2 ;;
    esac
done
[ "${#PROFILES[@]}" -gt 0 ] || { echo "at least one --profile is required" >&2; exit 2; }

for tool in qemu-system-x86_64 qemu-img ssh scp ssh-keygen curl git; do
    command -v "$tool" >/dev/null 2>&1 || not_run "$tool is missing" "${tool}_unavailable"
done
command -v genisoimage >/dev/null 2>&1 || command -v xorriso >/dev/null 2>&1 \
    || not_run "no ISO writer (genisoimage or xorriso)" iso_writer_unavailable
[ -w /dev/kvm ] || not_run "/dev/kvm is not writable by this user" kvm_unavailable

git -C "$ROOT" diff --quiet && git -C "$ROOT" diff --cached --quiet \
    || not_run "the project source tree has uncommitted changes" project_source_dirty

WORK=$(ems_work_dir ems-appliance-builder "$BUILDER_WORK_BYTES") || exit 1
GUEST_PID=""
cleanup() {
    if [ -n "$GUEST_PID" ] && kill -0 "$GUEST_PID" 2>/dev/null; then
        kill "$GUEST_PID" 2>/dev/null || true
        wait "$GUEST_PID" 2>/dev/null || true
    fi
    if [ "$KEEP" -eq 1 ]; then echo "kept: $WORK"; else rm -rf "$WORK"; fi
}
trap cleanup EXIT

mkdir -p "$CACHE" "$OUTPUT"
# The guest that builds a release may only be a guest this project pinned and
# measured. scripts/appliance-guest-base-image.sh is the single place that
# decides that, for the builder, the smoke guest and the ARM64 guest alike.
set +e
BASE_IMAGE_PATH=$(sh "$ROOT/scripts/appliance-guest-base-image.sh" --role builder --cache "$CACHE")
base_status=$?
set -e
case "$base_status" in
    0) ;;
    3) not_run "the pinned builder base image could not be obtained" base_image_unavailable ;;
    *) echo "RESULT: FAIL (base_image_unverified)" >&2; exit 1 ;;
esac

echo "== the source the builder will build =="
# A git bundle, not a tarball: appliance/project_source.py refuses to attribute
# a build to a revision it cannot verify the tree against, and an unpacked
# archive has no objects to verify against. The clone carries the same commit,
# so the builder computes the same revision and tree hash this host would.
"$ROOT/scripts/appliance-create-source-bundle.sh" --output "$WORK/source.tar.gz" >/dev/null \
    || { echo "the source bundle did not round-trip" >&2; exit 1; }
REVISION=$(git -C "$ROOT" rev-parse HEAD)
git -C "$ROOT" bundle create "$WORK/source.bundle" HEAD >/dev/null 2>&1 \
    || { echo "the project git bundle could not be created" >&2; exit 1; }
echo "project: $REVISION"

echo "== the generator the builder will use =="
"$ROOT/scripts/appliance-fetch-rpi-image-gen.sh" --into "$WORK/rpi-image-gen" --form tarball >/dev/null \
    || { echo "the pinned generator could not be fetched" >&2; exit 1; }
tar -C "$WORK" -czf "$WORK/rpi-image-gen.tar.gz" rpi-image-gen

echo "== booting the builder =="
ssh-keygen -q -t ed25519 -N '' -C ems-appliance-builder -f "$WORK/key"
mkdir -p "$WORK/seed"
cat >"$WORK/seed/meta-data" <<EOF
instance-id: ems-appliance-builder-$$
local-hostname: ems-appliance-builder
EOF
cat >"$WORK/seed/user-data" <<EOF
#cloud-config
disable_root: false
ssh_pwauth: false
users:
  - name: root
    ssh_authorized_keys:
      - $(cat "$WORK/key.pub")
  - name: builder
    shell: /bin/bash
    sudo: "ALL=(ALL) NOPASSWD:ALL"
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

qemu-img create -f qcow2 -b "$BASE_IMAGE_PATH" -F qcow2 "$WORK/builder.qcow2" "$DISK" >/dev/null
# setsid, so --keep actually keeps it: without its own session the guest is in
# this script's process group and dies with the script that started it.
setsid qemu-system-x86_64 \
    -name ems-appliance-builder \
    -enable-kvm -cpu host -smp "$(nproc)" -m "$MEMORY" \
    -drive file="$WORK/builder.qcow2",if=virtio,format=qcow2 \
    -drive file="$WORK/seed.iso",media=cdrom,format=raw \
    -nic user,model=virtio-net-pci,hostfwd=tcp:127.0.0.1:"$SSH_PORT"-:22 \
    -display none -serial file:"$WORK/console.log" &
GUEST_PID=$!

b() {
    ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR \
        -o ConnectTimeout=5 -o ServerAliveInterval=30 -o ServerAliveCountMax=240 \
        -i "$WORK/key" -p "$SSH_PORT" root@127.0.0.1 "$@"
}
# The generator is run unprivileged, over its own login session. Its bin/ns
# helper is #!/bin/sh but evals a bash function definition when it is already
# root, so the root path dies on Debian with "[[: not found" before the first
# layer runs. The supported path is the rootless one — which is what uidmap and
# dbus-user-session are in upstream's dependency list for — and a login session
# is what gives rootless podman its XDG_RUNTIME_DIR.
u() {
    ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR \
        -o ConnectTimeout=5 -o ServerAliveInterval=30 -o ServerAliveCountMax=240 \
        -i "$WORK/key" -p "$SSH_PORT" builder@127.0.0.1 "$@"
}
bcp() {
    scp -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR \
        -i "$WORK/key" -P "$SSH_PORT" "$@"
}

booted=0
for _ in $(seq 1 60); do
    if b true 2>/dev/null; then booted=1; break; fi
    sleep 5
done
[ "$booted" -eq 1 ] || not_run "the builder never became reachable over ssh" guest_boot_failed

echo "== installing the generator's declared dependencies =="
bcp "$WORK/source.bundle" "$WORK/rpi-image-gen.tar.gz" root@127.0.0.1:/root/ >/dev/null
b "set -eu
   export DEBIAN_FRONTEND=noninteractive
   apt-get update -qq
   apt-get install -y -qq git ca-certificates >/dev/null
   mkdir -p /build && tar -xzf /root/rpi-image-gen.tar.gz -C /build
   git clone --quiet /root/source.bundle /build/source
   git -C /build/source checkout --quiet $REVISION
   /build/rpi-image-gen/install_deps.sh
   apt-get install -y -qq qemu-user-static binfmt-support zstd xz-utils gpgv gdisk \
       android-sdk-libsparse-utils >/dev/null
   systemctl restart systemd-binfmt.service || true
   grep -q '^builder:' /etc/subuid || echo 'builder:100000:65536' >>/etc/subuid
   grep -q '^builder:' /etc/subgid || echo 'builder:100000:65536' >>/etc/subgid
   chown -R builder:builder /build
   loginctl enable-linger builder" \
    || not_run "the builder dependencies could not be installed" builder_deps_failed

echo "== builder environment =="
# Captured in the guest, before the build, and bound into the build authority:
# the machine that assembled an image is part of what a release claims.
BASE_IMAGE_SHA512=$(sha512sum "$BASE_IMAGE_PATH" | cut -d' ' -f1)
BASE_IMAGE_LOCK_ID="builder:$(basename "$BASE_IMAGE_PATH")"
bcp "$ROOT/scripts/appliance-capture-builder-environment.sh" root@127.0.0.1:/root/ >/dev/null
# shellcheck disable=SC2029
b "sh /root/appliance-capture-builder-environment.sh \
       --output /build/builder-environment.json \
       --base-image-lock-id '$BASE_IMAGE_LOCK_ID' \
       --base-image-sha512 '$BASE_IMAGE_SHA512' \
       --depends /build/rpi-image-gen/depends" \
    || not_run "the builder environment could not be captured" builder_environment_unavailable
b 'cat /build/builder-environment.json'
b 'chown builder:builder /build/builder-environment.json'

SOURCE_DIR=/build/source
u 'podman system migrate >/dev/null 2>&1 || true'
BUILT_REVISION=$(u "git -C $SOURCE_DIR rev-parse HEAD" | tr -d '\r')
[ "$BUILT_REVISION" = "$REVISION" ] \
    || not_run "the builder checked out $BUILT_REVISION, not $REVISION" source_revision_mismatch
echo "source: $SOURCE_DIR at $BUILT_REVISION"

GUEST_PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

status=0
if [ "$RELEASE_GATE" -eq 1 ]; then
    echo
    echo "== running the release gates =="
    gate_args=""
    for profile in "${PROFILES[@]}"; do gate_args="$gate_args --profile $profile"; done
    # The source-bundle gate checks a bundle against the git tree it came from,
    # so it is made in the guest from the guest's own clone.
    # shellcheck disable=SC2029
    u "export PATH=$GUEST_PATH
       cd $SOURCE_DIR
       ./scripts/appliance-create-source-bundle.sh --output /build/source-bundle.tar.gz >/dev/null
       time ./scripts/appliance-release-gates.sh --rpi-image-gen /build/rpi-image-gen \
            --output /build/dist --source-bundle /build/source-bundle.tar.gz \
            --crosscheck $gate_args" \
        || status=$?
else
for profile in "${PROFILES[@]}"; do
    echo
    echo "== building $profile =="
    build_args="--profile $profile --rpi-image-gen /build/rpi-image-gen --output /build/dist"
    build_args="$build_args --builder-environment /build/builder-environment.json"
    [ -n "$BUILD_ID" ] && build_args="$build_args --build-id ${BUILD_ID}-${profile}"
    # Five of upstream's declared binaries — mkfs.btrfs, veritysetup, mkdosfs,
    # mke2fs, fdisk — live in /usr/sbin, which Debian keeps off a non-root
    # PATH. Without them the dependency probe reports the generator as
    # unusable, which is true of the shell and not of the machine.
    # shellcheck disable=SC2029
    u "export PATH=$GUEST_PATH
       cd $SOURCE_DIR && time ./scripts/appliance-build-rpi-ab-image.sh $build_args" || status=$?
done
fi

echo
echo "== collecting artefacts =="
# The guest describes its own output before anything is copied, and the host
# re-hashes what arrived against that description. A dropped 16 GiB image, a
# full host disk and a complete build used to produce the same verdict.
#
# The work root is deliberately not collected: /build/dist also holds a chroot
# and the sparse copies of the image, and moving those over a slirp link takes
# longer than the build did.
# shellcheck disable=SC2029
b "python3 $SOURCE_DIR/scripts/appliance_builder_output.py describe \
       --dist /build/dist --output /build/output-manifest.json" \
    || { echo "RESULT: FAIL (artifact_copy_failed: the guest could not describe its output)" >&2
         exit 1; }

STAGING="$WORK/collected"
mkdir -p "$STAGING"
bcp "builder@127.0.0.1:/build/output-manifest.json" "$STAGING/" >/dev/null \
    || { echo "RESULT: FAIL (artifact_copy_failed: no output manifest)" >&2; exit 1; }

copy_failed=0
for artefact in $(b 'find /build/dist -maxdepth 1 -type f -printf "%f\n" 2>/dev/null'); do
    bcp "builder@127.0.0.1:/build/dist/$artefact" "$STAGING/" >/dev/null \
        || { echo "could not collect $artefact" >&2; copy_failed=1; }
done
for directory in gates reports; do
    # shellcheck disable=SC2029
    if b "test -d /build/dist/$directory" 2>/dev/null; then
        bcp -r "builder@127.0.0.1:/build/dist/$directory" "$STAGING/" >/dev/null \
            || { echo "could not collect /build/dist/$directory" >&2; copy_failed=1; }
    fi
done

# The hashes the guest recorded, against the bytes that reached this host.
if ! python3 "$ROOT/scripts/appliance_builder_output.py" verify \
        --manifest "$STAGING/output-manifest.json" --directory "$STAGING"; then
    echo "RESULT: FAIL (artifact_copy_failed)" >&2
    exit 1
fi
[ "$copy_failed" -eq 0 ] || { echo "RESULT: FAIL (artifact_copy_failed)" >&2; exit 1; }

# Only a verified staging directory is published into the operator's output.
mkdir -p "$OUTPUT"
for item in "$STAGING"/*; do
    [ -e "$item" ] || continue
    cp -r "$item" "$OUTPUT/" || { echo "RESULT: FAIL (artifact_copy_failed)" >&2; exit 1; }
done
sync
echo "collected: $OUTPUT"
[ -d "$OUTPUT/gates" ] && echo "gates: $OUTPUT/gates"

# A gate that never executed exits 3, and that is not a pass. Only the build
# path collapses its statuses into PASS/FAIL; the gate prints its own verdict.
if [ "$RELEASE_GATE" -eq 1 ]; then
    [ "$status" -eq 0 ] || echo "the release gates did not pass (exit $status)"
elif [ "$status" -eq 0 ]; then
    echo "RESULT: PASS"
else
    echo "RESULT: FAIL"
fi
exit "$status"
