#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Build the arm64 package and smoke-test it in a booted Debian 13 ARM64 VM.
#
#   scripts/appliance-smoke-arm64.sh [--keep] [--image /path/to/debian-13-arm64.qcow2]
#                                    [--image-sha256 HEX] [--image-checksum-file FILE]
#                                    [--allow-unverified-image]
#
# This boots a real aarch64 guest under qemu-system-aarch64 with EFI firmware
# and drives the same guest smoke test the amd64 run uses, so the two results
# are comparable. The Appliance Manager ships arm64 only, and an emulated
# container is not a booted system — nothing here is claimed unless the VM
# actually came up.
#
# Reproducibility rules:
#   * Every input is printed with its checksum, so a result can be repeated.
#   * A downloaded image is verified against the checksum manifest from the
#     same directory, and that manifest against its detached signature. An
#     image that could not be verified is never used unless
#     --allow-unverified-image says so explicitly.
#   * A supplied --image is only called verified when --image-sha256 or
#     --image-checksum-file confirms it. Otherwise the run is labelled
#     UNVERIFIED INPUT and never reports an unqualified release-style pass.
#
# Prerequisites (Debian/Ubuntu host):
#   sudo apt install qemu-system-arm qemu-utils qemu-efi-aarch64 cloud-image-utils \
#                    xorriso curl gpgv debian-keyring debian-archive-keyring
#
# Exit status: 0 every check passed, 1 a check failed, 3 the environment cannot
# run the test. A skipped run is never reported as a pass.
set -euo pipefail

ROOT=$(cd "$(dirname "$0")/.." && pwd)
IMAGE_BASE=${EMS_ARM64_IMAGE_BASE:-https://cloud.debian.org/images/cloud/trixie/latest}
IMAGE_NAME=${EMS_ARM64_IMAGE_NAME:-debian-13-genericcloud-arm64.qcow2}
CHECKSUM_FILE=${EMS_ARM64_CHECKSUM_FILE:-SHA512SUMS}
KEYRINGS=${EMS_ARM64_KEYRINGS:-/usr/share/keyrings/debian-archive-keyring.gpg:/usr/share/keyrings/debian-keyring.gpg:/usr/share/keyrings/debian-role-keys.gpg}
GUEST_ARCH=arm64
REQUIRED_FORMAT=qcow2
# Both pflash drives must present the same slot size to the firmware.
PFLASH_SIZE=67108864

BASE_IMAGE=""
IMAGE_SHA256=""
IMAGE_CHECKSUM_FILE=""
KEEP=0
ALLOW_UNVERIFIED=0
VERIFIED=0
MEMORY=${EMS_ARM64_MEMORY:-2048}
CPUS=${EMS_ARM64_CPUS:-2}
BOOT_TIMEOUT=${EMS_ARM64_BOOT_TIMEOUT:-1800}

while [ $# -gt 0 ]; do
    case "$1" in
        --keep) KEEP=1; shift ;;
        --image) BASE_IMAGE=$2; shift 2 ;;
        --image-sha256) IMAGE_SHA256=$2; shift 2 ;;
        --image-checksum-file) IMAGE_CHECKSUM_FILE=$2; shift 2 ;;
        --allow-unverified-image) ALLOW_UNVERIFIED=1; shift ;;
        -h|--help) sed -n '2,31p' "$0"; exit 0 ;;
        *) echo "unknown argument: $1" >&2; exit 2 ;;
    esac
done

fail_environment() {
    echo "appliance-smoke-arm64: $1" >&2
    echo "RESULT: NOT RUN" >&2
    exit 3
}

require() {
    command -v "$1" >/dev/null 2>&1 || fail_environment "$1 is not installed ($2)"
}

require qemu-system-aarch64 "apt install qemu-system-arm"
require qemu-img "apt install qemu-utils"
require cloud-localds "apt install cloud-image-utils"
require xorriso "apt install xorriso"
require sha256sum "apt install coreutils"

# The firmware code image and the variable-store template belong together: a
# blank variable store does not boot the AAVMF build Debian ships.
FIRMWARE=${EMS_ARM64_FIRMWARE:-}
if [ -z "$FIRMWARE" ]; then
    for candidate in /usr/share/AAVMF/AAVMF_CODE.fd \
                     /usr/share/qemu-efi-aarch64/QEMU_EFI.fd \
                     /usr/share/edk2/aarch64/QEMU_EFI.fd; do
        [ -f "$candidate" ] && FIRMWARE=$candidate && break
    done
fi
[ -n "$FIRMWARE" ] && [ -f "$FIRMWARE" ] \
    || fail_environment "no aarch64 EFI firmware found (apt install qemu-efi-aarch64)"

FIRMWARE_VARS=${EMS_ARM64_FIRMWARE_VARS:-}
if [ -z "$FIRMWARE_VARS" ]; then
    for candidate in "$(dirname "$FIRMWARE")/AAVMF_VARS.fd" \
                     /usr/share/AAVMF/AAVMF_VARS.fd \
                     /usr/share/qemu-efi-aarch64/QEMU_VARS.fd \
                     /usr/share/edk2/aarch64/vars-template-pflash.raw; do
        [ -f "$candidate" ] && FIRMWARE_VARS=$candidate && break
    done
fi
[ -n "$FIRMWARE_VARS" ] && [ -f "$FIRMWARE_VARS" ] \
    || fail_environment "no formatted UEFI variable-store template found for $FIRMWARE (apt install qemu-efi-aarch64)"

FIRMWARE_SIZE=$(stat -c '%s' "$FIRMWARE")
FIRMWARE_VARS_SIZE=$(stat -c '%s' "$FIRMWARE_VARS")
[ "$FIRMWARE_SIZE" -le "$PFLASH_SIZE" ] \
    || fail_environment "the firmware code image is $FIRMWARE_SIZE bytes, larger than the ${PFLASH_SIZE}-byte pflash slot"
[ "$FIRMWARE_VARS_SIZE" -le "$PFLASH_SIZE" ] \
    || fail_environment "the firmware variable store is $FIRMWARE_VARS_SIZE bytes, larger than the ${PFLASH_SIZE}-byte pflash slot"

WORK=$(mktemp -d "${TMPDIR:-/tmp}/ems-appliance-arm64.XXXXXX")
cleanup() {
    if [ "$KEEP" -eq 1 ]; then
        echo "kept: $WORK"
        return
    fi
    rm -rf "$WORK"
}
trap cleanup EXIT

echo "== building the arm64 package =="
"$ROOT/packaging/appliance/build-deb.sh" --output "$WORK" --arch "$GUEST_ARCH" >/dev/null
PACKAGE=$(ls "$WORK"/*.deb)
( cd "$WORK" && sha256sum -c "$(basename "$PACKAGE").sha256" )

checksum_of() {
    sha256sum "$1" | cut -d' ' -f1
}

# The manifest comes from the same directory as the image, so it is only
# evidence once its detached signature checks out.
verify_downloaded_image() {
    local manifest="$WORK/$CHECKSUM_FILE"
    curl -fsSL -o "$manifest" "$IMAGE_BASE/$CHECKSUM_FILE" || return 1
    if command -v gpgv >/dev/null 2>&1 && curl -fsSL -o "$manifest.sign" \
            "$IMAGE_BASE/$CHECKSUM_FILE.sign" 2>/dev/null; then
        local verified=0
        local keyring
        while IFS= read -r keyring; do
            [ -n "$keyring" ] && [ -f "$keyring" ] || continue
            if gpgv --keyring "$keyring" "$manifest.sign" "$manifest" >/dev/null 2>&1; then
                verified=1
                break
            fi
        done <<< "$(printf '%s\n' "$KEYRINGS" | tr ':' '\n')"
        if [ "$verified" -eq 0 ]; then
            echo "appliance-smoke-arm64: the $CHECKSUM_FILE signature could not be verified" >&2
            return 1
        fi
    else
        echo "appliance-smoke-arm64: no signed $CHECKSUM_FILE (install gpgv and a Debian keyring)" >&2
        return 1
    fi

    local tool=sha512sum
    case "$CHECKSUM_FILE" in SHA256SUMS) tool=sha256sum ;; esac
    local entry
    entry=$(grep " \*\?$IMAGE_NAME\$" "$manifest" || true)
    if [ -z "$entry" ]; then
        echo "appliance-smoke-arm64: $CHECKSUM_FILE has no entry for $IMAGE_NAME" >&2
        return 1
    fi
    ( cd "$WORK" && printf '%s\n' "$entry" | "$tool" -c - ) >/dev/null 2>&1
}

if [ -z "$BASE_IMAGE" ]; then
    require curl "apt install curl"
    BASE_IMAGE="$WORK/$IMAGE_NAME"
    echo "== downloading $IMAGE_BASE/$IMAGE_NAME =="
    curl -fsSL -o "$BASE_IMAGE" "$IMAGE_BASE/$IMAGE_NAME" \
        || fail_environment "cannot download the Debian 13 arm64 cloud image"
    if verify_downloaded_image; then
        VERIFIED=1
    else
        [ "$ALLOW_UNVERIFIED" -eq 1 ] \
            || fail_environment "the downloaded image is unverified; pass --image or --allow-unverified-image"
        echo "appliance-smoke-arm64: continuing with an unverified image on request" >&2
    fi
fi
[ -f "$BASE_IMAGE" ] || fail_environment "the base image $BASE_IMAGE does not exist"

BASE_IMAGE_SHA256=$(checksum_of "$BASE_IMAGE")

# A checksum the operator supplied is the only thing that can turn a local file
# into a verified input.
if [ -n "$IMAGE_CHECKSUM_FILE" ]; then
    [ -f "$IMAGE_CHECKSUM_FILE" ] \
        || fail_environment "the checksum file $IMAGE_CHECKSUM_FILE does not exist"
    expected=$(awk -v name="$(basename "$BASE_IMAGE")" \
        '{ sub(/^\*/, "", $2); n=$2; sub(/^.*\//, "", n); if (n == name) print $1 }' \
        "$IMAGE_CHECKSUM_FILE" | head -n 1)
    [ -n "$expected" ] \
        || fail_environment "$IMAGE_CHECKSUM_FILE has no entry for $(basename "$BASE_IMAGE")"
    [ -z "$IMAGE_SHA256" ] && IMAGE_SHA256=$expected
fi
if [ -n "$IMAGE_SHA256" ]; then
    [ "$IMAGE_SHA256" = "$BASE_IMAGE_SHA256" ] \
        || fail_environment "the base image checksum is $BASE_IMAGE_SHA256, expected $IMAGE_SHA256"
    VERIFIED=1
fi
[ "$ALLOW_UNVERIFIED" -eq 1 ] && VERIFIED=0

IMAGE_INFO=$(qemu-img info --output=json "$BASE_IMAGE" 2>/dev/null) \
    || fail_environment "qemu-img cannot read $BASE_IMAGE"
IMAGE_FORMAT=$(printf '%s' "$IMAGE_INFO" | sed -n 's/.*"format"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' | head -n 1)
[ "$IMAGE_FORMAT" = "$REQUIRED_FORMAT" ] \
    || fail_environment "the base image is $IMAGE_FORMAT, but a $REQUIRED_FORMAT backing image is required"

PACKAGE_SHA256=$(checksum_of "$PACKAGE")
QEMU_VERSION=$(qemu-system-aarch64 --version | head -n 1)

echo "== inputs =="
echo "  qemu:                $QEMU_VERSION"
echo "  firmware code:       $FIRMWARE ($FIRMWARE_SIZE bytes)"
echo "  firmware vars:       $FIRMWARE_VARS ($FIRMWARE_VARS_SIZE bytes)"
echo "  base image:          $BASE_IMAGE ($IMAGE_FORMAT)"
echo "  base image sha256:   $BASE_IMAGE_SHA256"
echo "  package:             $PACKAGE"
echo "  package sha256:      $PACKAGE_SHA256"
if [ "$VERIFIED" -eq 1 ]; then
    echo "  image verification:  verified against a supplied or signed checksum"
else
    echo "  image verification:  UNVERIFIED INPUT — the base image checksum was not confirmed"
fi

echo "== preparing the guest disks =="
qemu-img create -f qcow2 -F qcow2 -b "$(readlink -f "$BASE_IMAGE")" "$WORK/guest.qcow2" 12G >/dev/null
cp "$FIRMWARE" "$WORK/efi-code.fd"
truncate -s "$PFLASH_SIZE" "$WORK/efi-code.fd"
cp "$FIRMWARE_VARS" "$WORK/efi-vars.fd"
truncate -s "$PFLASH_SIZE" "$WORK/efi-vars.fd"

# The package and the shared guest script travel on their own ISO: no network
# access to the developer host and no base64 blob in the cloud-init payload.
mkdir -p "$WORK/payload"
cp "$PACKAGE" "$WORK/payload/appliance.deb"
cp "$ROOT/scripts/appliance-guest-smoke.sh" "$WORK/payload/guest-smoke.sh"
xorriso -as mkisofs -quiet -V EMSDEB -o "$WORK/payload.iso" "$WORK/payload"

cat > "$WORK/user-data" <<'CLOUDINIT'
#cloud-config
runcmd:
  - [ sh, -c, "mkdir -p /mnt/payload && mount -L EMSDEB /mnt/payload" ]
  - [ sh, -c, "sh /mnt/payload/guest-smoke.sh /mnt/payload/appliance.deb arm64 > /dev/ttyAMA0 2>&1; echo \"APPLIANCE_SMOKE_EXIT: $?\" > /dev/ttyAMA0" ]
  - [ sh, -c, "sync; poweroff" ]
CLOUDINIT
printf 'instance-id: ems-appliance-smoke\nlocal-hostname: ems-smoke\n' > "$WORK/meta-data"
cloud-localds "$WORK/seed.iso" "$WORK/user-data" "$WORK/meta-data"

ACCEL=()
if [ "$(uname -m)" = "aarch64" ] && [ -w /dev/kvm ]; then
    ACCEL=(-enable-kvm -cpu host)
    echo "== booting the ARM64 guest with KVM =="
else
    ACCEL=(-cpu cortex-a72)
    echo "== booting the ARM64 guest under full emulation (slow) =="
fi

CONSOLE="$WORK/console.log"
set +e
timeout "$BOOT_TIMEOUT" qemu-system-aarch64 \
    -machine virt -m "$MEMORY" -smp "$CPUS" "${ACCEL[@]}" \
    -drive if=pflash,format=raw,readonly=on,file="$WORK/efi-code.fd" \
    -drive if=pflash,format=raw,file="$WORK/efi-vars.fd" \
    -drive if=virtio,format=qcow2,file="$WORK/guest.qcow2" \
    -drive if=virtio,format=raw,file="$WORK/seed.iso" \
    -drive if=virtio,format=raw,file="$WORK/payload.iso" \
    -nographic -serial "file:$CONSOLE" -monitor none -display none \
    -netdev user,id=net0 -device virtio-net-pci,netdev=net0
qemu_status=$?
set -e

if [ ! -s "$CONSOLE" ]; then
    fail_environment "the ARM64 guest produced no console output (qemu exit $qemu_status)"
fi

echo "== guest output =="
sed -n '/^== architecture ==/,$p' "$CONSOLE" || cat "$CONSOLE"

if grep -q '^RESULT: PASS' "$CONSOLE"; then
    if ! grep -q 'aarch64' "$CONSOLE"; then
        fail_environment "the guest passed but never reported aarch64; the run proves nothing"
    fi
    echo
    echo "inputs: base image sha256 $BASE_IMAGE_SHA256, package sha256 $PACKAGE_SHA256"
    if [ "$VERIFIED" -eq 1 ]; then
        echo "RESULT: PASS (booted aarch64 guest)"
    else
        echo "UNVERIFIED INPUT: the base image checksum was never confirmed."
        echo "RESULT: PASS (unverified input, booted aarch64 guest)"
    fi
    exit 0
fi
if grep -q '^RESULT: FAIL' "$CONSOLE"; then
    echo
    echo "RESULT: FAIL (booted aarch64 guest)" >&2
    exit 1
fi

echo "console log: $CONSOLE" >&2
fail_environment "the ARM64 guest did not reach the smoke test (qemu exit $qemu_status)"
