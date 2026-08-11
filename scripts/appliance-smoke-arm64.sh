#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Build the arm64 package and smoke-test it in a booted Debian 13 ARM64 VM.
#
#   scripts/appliance-smoke-arm64.sh [--keep] [--image /path/to/debian-13-arm64.qcow2]
#                                    [--image-sha256 HEX] [--image-checksum-file FILE]
#                                    [--allow-unverified-image] [--output DIR]
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
# A "RESULT: PASS" substring in the serial log is evidence, never the verdict.
# The pass authority is listed at evaluate_guest_result() below; every element
# of it must hold before this driver exits zero.
#
# Prerequisites (Debian/Ubuntu host):
#   sudo apt install qemu-system-arm qemu-utils qemu-efi-aarch64 cloud-image-utils \
#                    xorriso curl gpgv debian-keyring debian-archive-keyring
#
# Exit status: 0 every check passed, 1 a check failed, 2 the command line is
# wrong, 3 the environment cannot run the test. A skipped run is never reported
# as a pass.
set -euo pipefail

ROOT=$(cd "$(dirname "$0")/.." && pwd)
IMAGE_BASE=${EMS_ARM64_IMAGE_BASE:-https://cloud.debian.org/images/cloud/trixie/latest}
IMAGE_NAME=${EMS_ARM64_IMAGE_NAME:-debian-13-genericcloud-arm64.qcow2}
CHECKSUM_FILE=${EMS_ARM64_CHECKSUM_FILE:-SHA512SUMS}
KEYRINGS=${EMS_ARM64_KEYRINGS:-/usr/share/keyrings/debian-archive-keyring.gpg:/usr/share/keyrings/debian-keyring.gpg:/usr/share/keyrings/debian-role-keys.gpg}
BUILD_SCRIPT=${EMS_ARM64_BUILD_SCRIPT:-$ROOT/packaging/appliance/build-deb.sh}
GUEST_ARCH=arm64
REQUIRED_FORMAT=qcow2
# Both pflash drives must present the same slot size to the firmware.
PFLASH_SIZE=67108864
TIMEOUT_STATUS=124
TIMEOUT_KILL_STATUS=137

BASE_IMAGE=""
IMAGE_SHA256=""
IMAGE_CHECKSUM_FILE=""
OUTPUT_DIR=""
EVIDENCE_DIR=""
# Where the artefacts of a terminal result are written. The evidence directory
# takes over as soon as --output has been accepted, so every outcome after that
# point — including one that never gets a working directory — is recorded.
STAGE_DIR=""
KEEP=0
ALLOW_UNVERIFIED=0
VERIFIED=0
WORK=""
PACKAGE_SHA256=""
BASE_IMAGE_SHA256=""
TIMEOUT_CLASS=none
STARTED_AT=""
RUN_ID=""
DRIVER_REVISION=""
REASON_CODE=""
QEMU_STARTED=false
MISSING_REQUIREMENTS=""
# Every file a result has to be reproducible from. Evidence that was asked for
# and could not be written is a failed run, not a quiet omission. The base set
# is owed by every terminal result; the emulator's own artefacts are only owed
# once the emulator ran, because a run that stopped before it has none.
EVIDENCE_FILES="inputs.txt result.txt run.txt environment.txt missing-requirements.txt"
EVIDENCE_FILES_AFTER_BOOT="console.log evidence.log qemu-command.txt qemu-status.txt"
# The guest's record travels on a virtio-serial port of its own. The boot
# console is shared with the kernel, systemd and agetty, and agetty calls
# vhangup() on it: a tier that logged there loses the rest of its output and
# dies on its next write. Nothing but the guest writes to this port.
EVIDENCE_PORT_NAME=${EMS_ARM64_EVIDENCE_PORT:-org.ems.appliance.evidence}
RECORD=""
RECORD_CHANNEL=none
MEMORY=${EMS_ARM64_MEMORY:-2048}
CPUS=${EMS_ARM64_CPUS:-2}
BOOT_TIMEOUT=${EMS_ARM64_BOOT_TIMEOUT:-1800}
DISK_GB=${EMS_ARM64_DISK_GB:-12}

usage() {
    sed -n '3,36p' "$0"
}

usage_error() {
    echo "appliance-smoke-arm64: $1" >&2
    usage >&2
    REASON_CODE=${2:-usage_error}
    record_result "USAGE ERROR" "$1"
    exit 2
}

fail_environment() {
    echo "appliance-smoke-arm64: $1" >&2
    REASON_CODE=${2:-$REASON_CODE}
    [ -n "$REASON_CODE" ] || REASON_CODE=environment_unusable
    record_result "NOT RUN" "$1"
    echo "RESULT: NOT RUN" >&2
    exit 3
}

smoke_failure() {
    echo "appliance-smoke-arm64: $1" >&2
    REASON_CODE=${2:-guest_smoke_failed}
    record_result "FAIL" "$1"
    echo "RESULT: FAIL ($1)" >&2
    exit 1
}

# A PASS on an input nobody verified is a functional result, never a release
# gate. Both are written as separate machine-readable fields so a consumer
# cannot read one as the other.
record_result() {
    [ -n "$STAGE_DIR" ] && [ -d "$STAGE_DIR" ] || return 0
    verification=unverified
    gate=no
    exit_code=1
    [ "$VERIFIED" -eq 1 ] && verification=verified
    [ "$1" = "PASS" ] && [ "$VERIFIED" -eq 1 ] && gate=pass
    case "$1" in
        PASS) exit_code=0 ;;
        FAIL) exit_code=1 ;;
        "USAGE ERROR") exit_code=2 ;;
        *) exit_code=3 ;;
    esac
    {
        printf 'result: %s\n' "$1"
        printf 'exit_code: %s\n' "$exit_code"
        printf 'reason: %s\n' "$2"
        printf 'reason_code: %s\n' "${REASON_CODE:-none}"
        printf 'verification: %s\n' "$verification"
        printf 'verified: %s\n' "$([ "$VERIFIED" -eq 1 ] && echo true || echo false)"
        printf 'release_gate: %s\n' "$gate"
        printf 'qemu_started: %s\n' "$QEMU_STARTED"
        printf 'timeout: %s\n' "$TIMEOUT_CLASS"
        printf 'record_channel: %s\n' "$RECORD_CHANNEL"
        printf 'run_id: %s\n' "$RUN_ID"
        printf 'started_at: %s\n' "$STARTED_AT"
        printf 'ended_at: %s\n' "$(timestamp)"
        printf 'driver_revision: %s\n' "$DRIVER_REVISION"
        printf 'package_sha256: %s\n' "$PACKAGE_SHA256"
        printf 'base_image_sha256: %s\n' "$BASE_IMAGE_SHA256"
    } > "$STAGE_DIR/result.txt"
}

# What this run is, independently of what it concluded. Written before the
# evidence is copied so a run that ended early is still identifiable.
write_run_metadata() {
    [ -n "$STAGE_DIR" ] && [ -d "$STAGE_DIR" ] || return 0
    {
        printf 'run_id: %s\n' "$RUN_ID"
        printf 'started_at: %s\n' "$STARTED_AT"
        printf 'ended_at: %s\n' "$(timestamp)"
        printf 'driver: %s\n' "$0"
        printf 'driver_revision: %s\n' "$DRIVER_REVISION"
        printf 'guest_architecture: %s\n' "$GUEST_ARCH"
        printf 'package_sha256: %s\n' "$PACKAGE_SHA256"
        printf 'base_image_sha256: %s\n' "$BASE_IMAGE_SHA256"
        printf 'boot_timeout_seconds: %s\n' "$BOOT_TIMEOUT"
        printf 'timeout: %s\n' "$TIMEOUT_CLASS"
    } > "$STAGE_DIR/run.txt"
    return 0
}

# Evidence is copied out before the working directory goes away, so a failed run
# stays diagnosable without --keep. A copy that fails is not a detail to ignore:
# a run that says it preserved evidence and did not is a result nobody can check.
preserve_evidence() {
    [ -n "$EVIDENCE_DIR" ] && [ -d "$EVIDENCE_DIR" ] || return 0
    write_run_metadata
    local artifact
    local missing=""
    local required="$EVIDENCE_FILES"
    # A run that never started the emulator owes no console log; one that did
    # owes all three, and a missing one there is a result nobody can check.
    [ "$QEMU_STARTED" = true ] && required="$required $EVIDENCE_FILES_AFTER_BOOT"
    # The emulator writes into the working directory; everything else is already
    # staged in the evidence directory, because a run may end before it has one.
    if [ -n "$WORK" ] && [ -d "$WORK" ]; then
        for artifact in $EVIDENCE_FILES $EVIDENCE_FILES_AFTER_BOOT; do
            [ -f "$WORK/$artifact" ] || continue
            [ "$WORK/$artifact" = "$EVIDENCE_DIR/$artifact" ] && continue
            cp -f "$WORK/$artifact" "$EVIDENCE_DIR/$artifact" || return 1
        done
    fi
    for artifact in $required; do
        [ -f "$EVIDENCE_DIR/$artifact" ] || missing="$missing $artifact"
    done
    if [ -n "$missing" ]; then
        REASON_CODE=evidence_write_failed
        record_result "NOT RUN" "the requested evidence could not be completed"
        printf 'missing:%s\n' "$missing" > "$EVIDENCE_DIR/evidence-incomplete.txt" || return 1
        return 1
    fi
    # The result file is written last and says so, so a truncated evidence
    # directory can never be read as a complete record of anything.
    printf 'evidence_complete: true\n' >> "$EVIDENCE_DIR/result.txt" || return 1
    printf '%s\n' "${EVIDENCE_DIR##*/}" > "$OUTPUT_DIR/.latest.txt" || return 1
    mv -f "$OUTPUT_DIR/.latest.txt" "$OUTPUT_DIR/latest.txt" || return 1
    return 0
}

cleanup() {
    local status=$?
    if ! preserve_evidence; then
        echo "appliance-smoke-arm64: the requested evidence is incomplete in $EVIDENCE_DIR" >&2
        echo "RESULT: EVIDENCE INCOMPLETE" >&2
        [ "$status" -eq 0 ] && status=1
    fi
    if [ -n "$WORK" ]; then
        if [ "$KEEP" -eq 1 ]; then
            echo "kept: $WORK"
        else
            rm -rf "$WORK"
        fi
    fi
    exit "$status"
}

# Take the evidence directory as soon as --output has been accepted, before the
# working directory exists. Everything that can end the run from here on — a
# usage error in a later argument, a temporary directory that cannot be created
# — is a terminal result that owes the operator a complete record.
evidence_begin() {
    if [ ! -d "$OUTPUT_DIR" ]; then
        mkdir "$OUTPUT_DIR" 2>/dev/null \
            || fail_environment "the output directory $OUTPUT_DIR does not exist and its parent does not either" \
                                output_directory_unusable
    fi
    [ -w "$OUTPUT_DIR" ] \
        || fail_environment "the output directory $OUTPUT_DIR is not writable" \
                            output_directory_unusable
    OUTPUT_DIR=$(cd "$OUTPUT_DIR" && pwd)
    EVIDENCE_DIR="$OUTPUT_DIR/run-$RUN_ID"
    mkdir "$EVIDENCE_DIR" \
        || fail_environment "the evidence directory $EVIDENCE_DIR could not be created" \
                            output_directory_unusable
    STAGE_DIR="$EVIDENCE_DIR"
    trap cleanup EXIT
    seed_evidence
}
# Placeholders for every artefact a terminal result owes, written before the
# first check that can end the run. A result file that says nothing is still a
# statement; an evidence directory that says nothing is not.
seed_evidence() {
    [ -n "$STAGE_DIR" ] && [ -d "$STAGE_DIR" ] || return 0
    : > "$STAGE_DIR/inputs.txt"
    : > "$STAGE_DIR/missing-requirements.txt"
    {
        printf 'uname: %s\n' "$(uname -srm 2>/dev/null || echo unknown)"
        printf 'kvm: %s\n' "$([ -w /dev/kvm ] && echo writable || echo unavailable)"
        printf 'tmpdir: %s\n' "${TMPDIR:-/tmp}"
        printf 'memory_mib: %s\n' "$MEMORY"
        printf 'cpus: %s\n' "$CPUS"
        printf 'disk_gib: %s\n' "$DISK_GB"
        printf 'boot_timeout_seconds: %s\n' "$BOOT_TIMEOUT"
        for tool in qemu-system-aarch64 qemu-img cloud-localds xorriso curl gpgv \
                    sha256sum timeout; do
            printf '%s: %s\n' "$tool" "$(command -v "$tool" 2>/dev/null || echo absent)"
        done
    } > "$STAGE_DIR/environment.txt"
    REASON_CODE=incomplete_run
    record_result "NOT RUN" "the run ended before it reached a verdict"
    write_run_metadata
    return 0
}

note_missing_requirement() {
    MISSING_REQUIREMENTS="$MISSING_REQUIREMENTS $1"
    [ -n "$STAGE_DIR" ] && [ -d "$STAGE_DIR" ] || return 0
    printf '%s\n' "$1" >> "$STAGE_DIR/missing-requirements.txt"
    return 0
}

timestamp() {
    date -u +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || echo unknown
}

bounded_integer() {
    case "$2" in
        ''|*[!0-9]*) fail_environment "$1 must be a positive integer, got '$2'" ;;
    esac
    [ "$2" -ge 1 ] || fail_environment "$1 must be at least 1, got '$2'"
    [ "$2" -le "$3" ] || fail_environment "$1 must not exceed $3, got '$2'"
}

# An option that takes a value may not swallow the next option as one. The
# value of --image is a path; "--keep" is a request, and reading it as a path
# turns a typo into a run that reports something about the wrong inputs. A
# value that legitimately begins with a dash has the explicit --option=value
# form. Neither check may run inside a command substitution: an exit there
# would only leave the subshell.
require_value() {
    case "${2-}" in
        "") usage_error "$1 requires a value" ;;
        -*) usage_error "$1 requires a value, but the next argument is the option '$2'" ;;
    esac
}

require_inline_value() {
    [ -n "${1#*=}" ] || usage_error "${1%%=*} requires a value"
}

STARTED_AT=$(timestamp)
RUN_ID="${STARTED_AT}-$$"
DRIVER_REVISION=$(git -C "$ROOT" rev-parse --short HEAD 2>/dev/null || echo unknown)

# Only the final component of --output is created: an evidence path whose parent
# does not exist is a typo, not a request to build a directory tree. Each run
# then gets its own directory inside it, because a second run dropping files next
# to the first is how a stale console log gets read as this run's evidence.
#
# Where the output path itself is the fault there is nowhere to write a result,
# and that is the only terminal outcome that owes none.
accept_output_directory() {
    [ -z "$EVIDENCE_DIR" ] || usage_error "--output may only be given once"
    OUTPUT_DIR=$1
    evidence_begin
}

while [ $# -gt 0 ]; do
    case "$1" in
        --keep) KEEP=1; shift ;;
        --allow-unverified-image) ALLOW_UNVERIFIED=1; shift ;;
        --image=*) require_inline_value "$1"; BASE_IMAGE=${1#*=}; shift ;;
        --image) require_value "$1" "${2-}"; BASE_IMAGE=$2; shift 2 ;;
        --image-sha256=*) require_inline_value "$1"; IMAGE_SHA256=${1#*=}; shift ;;
        --image-sha256) require_value "$1" "${2-}"; IMAGE_SHA256=$2; shift 2 ;;
        --image-checksum-file=*)
            require_inline_value "$1"; IMAGE_CHECKSUM_FILE=${1#*=}; shift ;;
        --image-checksum-file)
            require_value "$1" "${2-}"; IMAGE_CHECKSUM_FILE=$2; shift 2 ;;
        --output=*) require_inline_value "$1"; accept_output_directory "${1#*=}"; shift ;;
        --output) require_value "$1" "${2-}"; accept_output_directory "$2"; shift 2 ;;
        -h|--help) usage; exit 0 ;;
        *) usage_error "unknown argument: $1" ;;
    esac
done

# The working directory holds the build and the emulator's artefacts. The
# evidence transaction is already open when --output was given, so a temporary
# directory that cannot be created is a recorded terminal result, not a silent
# one, and never leaves an empty run directory behind.
WORK=$(mktemp -d "${TMPDIR:-/tmp}/ems-appliance-arm64.XXXXXX") \
    || fail_environment "a working directory could not be created" working_directory_unusable
trap cleanup EXIT
if [ -z "$STAGE_DIR" ]; then
    STAGE_DIR="$WORK"
    seed_evidence
fi

if [ "$ALLOW_UNVERIFIED" -eq 1 ] \
        && { [ -n "$IMAGE_SHA256" ] || [ -n "$IMAGE_CHECKSUM_FILE" ]; }; then
    usage_error "--allow-unverified-image cannot be combined with a supplied checksum" \
                conflicting_verification_options
fi

bounded_integer "the guest memory in MiB (EMS_ARM64_MEMORY)" "$MEMORY" 1048576
bounded_integer "the guest cpu count (EMS_ARM64_CPUS)" "$CPUS" 256
bounded_integer "the boot timeout in seconds (EMS_ARM64_BOOT_TIMEOUT)" "$BOOT_TIMEOUT" 86400
bounded_integer "the guest disk size in GiB (EMS_ARM64_DISK_GB)" "$DISK_GB" 1024

for tool in qemu-system-aarch64 qemu-img cloud-localds xorriso sha256sum timeout python3; do
    command -v "$tool" >/dev/null 2>&1 || note_missing_requirement "$tool"
done
if [ -n "$MISSING_REQUIREMENTS" ]; then
    fail_environment "not installed:$MISSING_REQUIREMENTS (apt install qemu-system-arm qemu-utils cloud-image-utils xorriso coreutils python3)" \
                     required_tool_missing
fi

if [ ! -x "$BUILD_SCRIPT" ]; then
    note_missing_requirement "$BUILD_SCRIPT"
    fail_environment "the package build script $BUILD_SCRIPT is not executable" \
                     build_script_unavailable
fi

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
[ -n "$FIRMWARE" ] && [ -r "$FIRMWARE" ] \
    || fail_environment "no readable aarch64 EFI firmware found (apt install qemu-efi-aarch64)" \
                        firmware_unavailable

FIRMWARE_VARS=${EMS_ARM64_FIRMWARE_VARS:-}
if [ -z "$FIRMWARE_VARS" ]; then
    for candidate in "$(dirname "$FIRMWARE")/AAVMF_VARS.fd" \
                     /usr/share/AAVMF/AAVMF_VARS.fd \
                     /usr/share/qemu-efi-aarch64/QEMU_VARS.fd \
                     /usr/share/edk2/aarch64/vars-template-pflash.raw; do
        [ -f "$candidate" ] && FIRMWARE_VARS=$candidate && break
    done
fi
[ -n "$FIRMWARE_VARS" ] && [ -r "$FIRMWARE_VARS" ] \
    || fail_environment "no readable UEFI variable-store template found for $FIRMWARE (apt install qemu-efi-aarch64)" \
                        firmware_unavailable

# -L on purpose: the packaged AAVMF_CODE.fd is a symlink, and stat without
# it measures the link rather than the firmware, so the pflash bound below
# would be checked against 24 bytes and pass whatever it points at.
FIRMWARE_SIZE=$(stat -Lc '%s' "$FIRMWARE")
FIRMWARE_VARS_SIZE=$(stat -Lc '%s' "$FIRMWARE_VARS")
[ "$FIRMWARE_SIZE" -le "$PFLASH_SIZE" ] \
    || fail_environment "the firmware code image is $FIRMWARE_SIZE bytes, larger than the ${PFLASH_SIZE}-byte pflash slot" \
                        firmware_incompatible
[ "$FIRMWARE_VARS_SIZE" -le "$PFLASH_SIZE" ] \
    || fail_environment "the firmware variable store is $FIRMWARE_VARS_SIZE bytes, larger than the ${PFLASH_SIZE}-byte pflash slot" \
                        firmware_incompatible

echo "== building the arm64 package =="
"$BUILD_SCRIPT" --output "$WORK" --arch "$GUEST_ARCH" >/dev/null \
    || fail_environment "the arm64 package build failed"
PACKAGE=$(ls "$WORK"/*.deb 2>/dev/null | head -n 1 || true)
[ -n "$PACKAGE" ] && [ -f "$PACKAGE" ] || fail_environment "the build produced no package"
( cd "$WORK" && sha256sum -c "$(basename "$PACKAGE").sha256" ) \
    || fail_environment "the built package does not match its recorded checksum"

# Smoke-testing a foreign-architecture package in an ARM64 guest proves nothing
# about the arm64 build, so the package is refused before the VM is prepared.
package_architecture() {
    local field
    if command -v dpkg-deb >/dev/null 2>&1; then
        field=$(dpkg-deb --field "$1" Architecture 2>/dev/null || true)
        if [ -n "$field" ]; then
            printf '%s\n' "$field"
            return 0
        fi
    fi
    basename "$1" | sed -n 's/^.*_\([^_]*\)\.deb$/\1/p'
}
PACKAGE_ARCH=$(package_architecture "$PACKAGE")
[ "$PACKAGE_ARCH" = "$GUEST_ARCH" ] \
    || fail_environment "the package is built for '$PACKAGE_ARCH', but this run requires $GUEST_ARCH"

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
    # One acquisition policy for every disposable guest in this project: the
    # digest packaging/appliance/vm/base-images.lock.json pins, re-checked
    # immediately before the boot that uses it. A floating "latest" is not a
    # base image a release tier may boot.
    echo "== resolving the pinned arm64 base image =="
    set +e
    BASE_IMAGE=$(sh "$ROOT/scripts/appliance-guest-base-image.sh" --role guest-arm64)
    base_status=$?
    set -e
    case "$base_status" in
        0) VERIFIED=1 ;;
        3) fail_environment "the pinned arm64 base image could not be obtained" \
               base_image_unavailable ;;
        *) fail_environment "the arm64 base image is not the one the lock pins" \
               base_image_unverified ;;
    esac
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
if [ "$ALLOW_UNVERIFIED" -eq 1 ]; then
    VERIFIED=0
fi

IMAGE_INFO=$(qemu-img info --output=json "$BASE_IMAGE" 2>/dev/null) \
    || fail_environment "qemu-img cannot read $BASE_IMAGE"
# The top-level format, not the first one in the document. qemu-img 9 and later
# describe the protocol node first, and its format is "file", so taking the
# first match reads every qcow2 as unusable and refuses a perfectly good image.
IMAGE_FORMAT=$(printf '%s' "$IMAGE_INFO" \
    | python3 -c 'import json,sys; print(json.load(sys.stdin).get("format") or "")' 2>/dev/null)
[ "$IMAGE_FORMAT" = "$REQUIRED_FORMAT" ] \
    || fail_environment "the base image is $IMAGE_FORMAT, but a $REQUIRED_FORMAT backing image is required"

PACKAGE_SHA256=$(checksum_of "$PACKAGE")
QEMU_VERSION=$(qemu-system-aarch64 --version | head -n 1)

{
    echo "== inputs =="
    echo "  qemu:                $QEMU_VERSION"
    echo "  firmware code:       $FIRMWARE ($FIRMWARE_SIZE bytes)"
    echo "  firmware vars:       $FIRMWARE_VARS ($FIRMWARE_VARS_SIZE bytes)"
    echo "  base image:          $BASE_IMAGE ($IMAGE_FORMAT)"
    echo "  base image sha256:   $BASE_IMAGE_SHA256"
    echo "  package:             $PACKAGE"
    echo "  package architecture: $PACKAGE_ARCH"
    echo "  package sha256:      $PACKAGE_SHA256"
    if [ "$VERIFIED" -eq 1 ]; then
        echo "  image verification:  verified against a supplied or signed checksum"
    else
        echo "  image verification:  UNVERIFIED INPUT — the base image checksum was not confirmed"
    fi
} | tee "$STAGE_DIR/inputs.txt"

echo "== preparing the guest disks =="
qemu-img create -f qcow2 -F qcow2 -b "$(readlink -f "$BASE_IMAGE")" "$WORK/guest.qcow2" "${DISK_GB}G" >/dev/null
cp "$FIRMWARE" "$WORK/efi-code.fd"
truncate -s "$PFLASH_SIZE" "$WORK/efi-code.fd"
cp "$FIRMWARE_VARS" "$WORK/efi-vars.fd"
truncate -s "$PFLASH_SIZE" "$WORK/efi-vars.fd"

# The package and the shared guest script travel on their own ISO: no network
# access to the developer host and no base64 blob in the cloud-init payload.
mkdir -p "$WORK/payload"
cp "$PACKAGE" "$WORK/payload/appliance.deb"
cp "$ROOT/scripts/appliance-guest-smoke.sh" "$WORK/payload/guest-smoke.sh"
cp "$ROOT/scripts/appliance-guest-evidence.sh" "$WORK/payload/guest-evidence.sh"
xorriso -as mkisofs -quiet -V EMSDEB -o "$WORK/payload.iso" "$WORK/payload"

# The tier is never pointed at a terminal. Its record goes to the dedicated
# port; the console keeps the kernel, systemd and agetty it already had, plus
# the stage heartbeat, which nothing reads as a result.
cat > "$WORK/user-data" <<CLOUDINIT
#cloud-config
runcmd:
  - [ sh, -c, "mkdir -p /mnt/payload && mount -L EMSDEB /mnt/payload" ]
  - [ sh, -c, "sh /mnt/payload/guest-evidence.sh --channel-name $EVIDENCE_PORT_NAME --fallback /dev/ttyAMA0 -- /mnt/payload/guest-smoke.sh /mnt/payload/appliance.deb arm64" ]
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
EVIDENCE_LOG="$WORK/evidence.log"
# Created before the emulator so a run that never opened the port still leaves
# the artefact its own evidence contract promises.
: > "$EVIDENCE_LOG"
QEMU_COMMAND=(
    qemu-system-aarch64
    -machine virt -m "$MEMORY" -smp "$CPUS" "${ACCEL[@]}"
    -drive "if=pflash,format=raw,readonly=on,file=$WORK/efi-code.fd"
    -drive "if=pflash,format=raw,file=$WORK/efi-vars.fd"
    -drive "if=virtio,format=qcow2,file=$WORK/guest.qcow2"
    -drive "if=virtio,format=raw,file=$WORK/seed.iso"
    -drive "if=virtio,format=raw,file=$WORK/payload.iso"
    -nographic -serial "file:$CONSOLE" -monitor none -display none
    -device "virtio-serial-pci,id=evidence-bus"
    -chardev "file,id=evidence,path=$EVIDENCE_LOG"
    -device "virtserialport,bus=evidence-bus.0,chardev=evidence,name=$EVIDENCE_PORT_NAME"
    -netdev "user,id=net0" -device "virtio-net-pci,netdev=net0"
)
printf '%q ' timeout "$BOOT_TIMEOUT" "${QEMU_COMMAND[@]}" > "$WORK/qemu-command.txt"
printf '\n' >> "$WORK/qemu-command.txt"

QEMU_STARTED=true
set +e
timeout "$BOOT_TIMEOUT" "${QEMU_COMMAND[@]}"
qemu_status=$?
set -e
printf '%s\n' "$qemu_status" > "$WORK/qemu-status.txt"

if [ ! -s "$CONSOLE" ]; then
    fail_environment "the ARM64 guest produced no console output (qemu exit $qemu_status)" \
                     guest_completion_missing
fi

# The dedicated port is the record. The console is only read when the guest
# could not reach that port and said so by delivering the record there instead;
# that is a degraded run, and it is labelled rather than silently accepted.
if [ -s "$EVIDENCE_LOG" ]; then
    RECORD="$EVIDENCE_LOG"
    RECORD_CHANNEL=dedicated
else
    RECORD="$CONSOLE"
    RECORD_CHANNEL=console
    echo "appliance-smoke-arm64: the guest delivered no record on $EVIDENCE_PORT_NAME; reading the shared console" >&2
fi

echo "== guest output ($RECORD_CHANNEL channel) =="
if grep -q '^== architecture ==' "$RECORD"; then
    sed -n '/^== architecture ==/,$p' "$RECORD"
else
    tail -n 80 "$RECORD"
fi

# How the emulator ended decides whether the serial log is a complete record at
# all. A guest that was killed mid-run can still have printed "RESULT: PASS".
case "$qemu_status" in
    0)
        ;;
    "$TIMEOUT_STATUS")
        TIMEOUT_CLASS=expired
        smoke_failure "the guest timed out after ${BOOT_TIMEOUT}s (qemu exit $qemu_status)" \
                      guest_timeout
        ;;
    "$TIMEOUT_KILL_STATUS")
        TIMEOUT_CLASS=killed
        smoke_failure "the guest timed out after ${BOOT_TIMEOUT}s and was killed (qemu exit $qemu_status)" \
                      guest_timeout
        ;;
    *)
        if [ "$qemu_status" -gt 128 ]; then
            smoke_failure "qemu was terminated by signal $((qemu_status - 128))" qemu_failed
        fi
        smoke_failure "qemu ended abnormally with exit status $qemu_status" qemu_failed
        ;;
esac

count_lines() {
    grep -c "$1" "$RECORD" 2>/dev/null || true
}

# Which boundary the guest last reached. The stage heartbeat is on the console
# even when the record never arrived, so a guest that stopped mid-run still
# names the stage it stopped in.
last_stage_note() {
    local stage
    stage=$(sed -n 's/.*APPLIANCE_EVIDENCE stage=\([A-Za-z0-9-]*\).*/\1/p' \
        "$RECORD" "$CONSOLE" 2>/dev/null | tail -n 1)
    [ -n "$stage" ] && printf ', last stage %s' "$stage"
    return 0
}

# Guest-side evidence of failure outranks any earlier optimistic marker. A
# kernel panic is the kernel's own statement and belongs to the console
# whichever channel carried the tier's record.
if grep -qi 'kernel panic' "$CONSOLE"; then
    smoke_failure "the guest hit a kernel panic" guest_kernel_panic
fi
if grep -qi 'cloud-init.*fatal' "$CONSOLE"; then
    smoke_failure "cloud-init reported a fatal error in the guest" guest_smoke_failed
fi
if grep -q '^RESULT: FAIL' "$RECORD"; then
    smoke_failure "the guest smoke test reported RESULT: FAIL$(last_stage_note)" guest_smoke_failed
fi
GUEST_EXITS=$(sed -n 's/^APPLIANCE_SMOKE_EXIT:[[:space:]]*\([0-9][0-9]*\).*$/\1/p' "$RECORD")
if printf '%s\n' "$GUEST_EXITS" | grep -q '^[1-9]'; then
    smoke_failure "the guest smoke test exited non-zero (APPLIANCE_SMOKE_EXIT: $(printf '%s' "$GUEST_EXITS" | tr '\n' ' '))" \
                  guest_smoke_failed
fi

# The pass authority. Every element must hold; anything ambiguous is NOT RUN
# rather than a pass, because an incomplete log cannot prove the run happened.
evaluate_guest_result() {
    local arch_lines exit_lines result_lines guest_uname
    arch_lines=$(count_lines '^guest: ')
    [ "$arch_lines" -eq 1 ] \
        || fail_environment "the guest reported $arch_lines architecture markers, expected exactly one; the run proves nothing" \
                            guest_architecture_mismatch
    guest_uname=$(sed -n 's/^guest: \([^ ][^ ]*\).*$/\1/p' "$RECORD" | head -n 1)
    case "$guest_uname" in
        aarch64|arm64) ;;
        *) fail_environment "the guest reported '$guest_uname', not aarch64; the run proves nothing" \
                            guest_architecture_mismatch ;;
    esac

    exit_lines=$(count_lines '^APPLIANCE_SMOKE_EXIT:')
    [ "$exit_lines" -eq 1 ] \
        || fail_environment "the guest wrote $exit_lines completion markers, expected exactly one; the serial log is truncated or duplicated" \
                            guest_completion_missing
    [ "$GUEST_EXITS" = "0" ] \
        || fail_environment "the guest completion marker is '$GUEST_EXITS', not 0" \
                            guest_completion_missing

    result_lines=$(count_lines '^RESULT:')
    [ "$result_lines" -eq 1 ] \
        || fail_environment "the guest wrote $result_lines RESULT lines, expected exactly one" \
                            guest_completion_missing
    grep -q '^RESULT: PASS' "$RECORD" \
        || fail_environment "the guest never reported RESULT: PASS" guest_completion_missing
}
evaluate_guest_result

echo
echo "inputs: base image sha256 $BASE_IMAGE_SHA256, package sha256 $PACKAGE_SHA256"
[ -n "$EVIDENCE_DIR" ] && echo "evidence: $EVIDENCE_DIR"
REASON_CODE=guest_smoke_passed
if [ "$VERIFIED" -eq 1 ]; then
    record_result "PASS" "booted aarch64 guest, verified input"
    echo "RESULT: PASS (booted aarch64 guest)"
else
    record_result "PASS" "booted aarch64 guest, unverified input"
    echo "UNVERIFIED INPUT: the base image checksum was never confirmed."
    echo "RESULT: PASS (unverified input, booted aarch64 guest)"
fi
exit 0
