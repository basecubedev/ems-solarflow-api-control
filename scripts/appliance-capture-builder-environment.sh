#!/bin/sh
# SPDX-License-Identifier: AGPL-3.0-or-later
# Record which machine is about to assemble a release image.
#
#   scripts/appliance-capture-builder-environment.sh --output FILE
#                                                    [--base-image-lock-id ID]
#                                                    [--base-image-sha512 HEX]
#                                                    [--depends FILE]
#                                                    [--captured-at ISO8601]
#
# Run inside the builder guest, before the build. Two identical source trees
# built on different machines are not the same supply chain: mmdebstrap decides
# what a root filesystem contains, podman runs the foreign-architecture stages,
# and the aarch64 binfmt handler decides whether they ran natively or emulated.
# A release that cannot name those cannot be diagnosed later.
#
# What is deliberately not captured: the process environment, cloud-init data,
# credentials and anything else that would turn build provenance into a place
# secrets leak from. Only tool identities and versions.
#
# Exit status: 0 the evidence was written, 1 a required identity could not be
# read, 2 the command line is wrong.
set -eu

OUTPUT=""
LOCK_ID=""
BASE_SHA512=""
DEPENDS=""
CAPTURED_AT=""

usage() { sed -n '3,20p' "$0"; }

fail() {
    echo "appliance-capture-builder-environment: $1" >&2
    echo "RESULT: FAIL ($2)" >&2
    exit 1
}

while [ $# -gt 0 ]; do
    case "$1" in
        --output) OUTPUT=${2:?--output needs a file}; shift 2 ;;
        --output=*) OUTPUT=${1#*=}; shift ;;
        --base-image-lock-id) LOCK_ID=${2:?--base-image-lock-id needs a value}; shift 2 ;;
        --base-image-lock-id=*) LOCK_ID=${1#*=}; shift ;;
        --base-image-sha512) BASE_SHA512=${2:?--base-image-sha512 needs a value}; shift 2 ;;
        --base-image-sha512=*) BASE_SHA512=${1#*=}; shift ;;
        --depends) DEPENDS=${2:?--depends needs a file}; shift 2 ;;
        --depends=*) DEPENDS=${1#*=}; shift ;;
        --captured-at) CAPTURED_AT=${2:?--captured-at needs a timestamp}; shift 2 ;;
        --captured-at=*) CAPTURED_AT=${1#*=}; shift ;;
        -h|--help) usage; exit 0 ;;
        *) echo "unknown argument: $1" >&2; usage >&2; exit 2 ;;
    esac
done

[ -n "$OUTPUT" ] || { echo "--output is required" >&2; usage >&2; exit 2; }

version_of() {
    command -v "$1" >/dev/null 2>&1 || { printf ''; return 0; }
    "$@" 2>/dev/null | head -n 1 | tr -d '\r'
}

OS_RELEASE=$( (. /etc/os-release 2>/dev/null && printf '%s %s' "${ID:-unknown}" "${VERSION_ID:-unknown}") || printf 'unknown' )
KERNEL=$(uname -sr 2>/dev/null || printf 'unknown')
ARCHITECTURE=$(uname -m 2>/dev/null || printf 'unknown')
PYTHON_VERSION=$(version_of python3 --version)
PODMAN_VERSION=$(version_of podman --version)
MMDEBSTRAP_VERSION=$(version_of mmdebstrap --version)
QEMU_VERSION=$(version_of qemu-aarch64-static --version)

# Which handler answers for an aarch64 binary decides whether the foreign
# stages ran under emulation at all. "registered" is not the same claim as
# "some qemu binary is installed".
BINFMT_HANDLER=none
for entry in /proc/sys/fs/binfmt_misc/qemu-aarch64 /proc/sys/fs/binfmt_misc/qemu-aarch64-static; do
    if [ -r "$entry" ]; then
        interpreter=$(sed -n 's/^interpreter //p' "$entry" | head -n 1)
        state=$(sed -n '1p' "$entry")
        BINFMT_HANDLER="$(basename "$entry") $state ${interpreter:-unknown}"
        break
    fi
done

[ -n "$CAPTURED_AT" ] || CAPTURED_AT=$(date -u +%Y-%m-%dT%H:%M:%SZ)

CRITICAL_PACKAGES=""
if command -v dpkg-query >/dev/null 2>&1; then
    CRITICAL_PACKAGES=$(dpkg-query -W -f='${Package} ${Version}\n' \
        mmdebstrap podman uidmap dctrl-tools python3-jsonschema btrfs-progs cryptsetup \
        dosfstools e2fsprogs fdisk flex pv qemu-user-static mtools zip rsync zstd \
        2>/dev/null | awk 'NF == 2' | sort || true)
fi

export EMS_OS_RELEASE="$OS_RELEASE" EMS_KERNEL="$KERNEL" EMS_ARCH="$ARCHITECTURE"
export EMS_PYTHON="$PYTHON_VERSION" EMS_PODMAN="$PODMAN_VERSION"
export EMS_MMDEBSTRAP="$MMDEBSTRAP_VERSION" EMS_QEMU="$QEMU_VERSION"
export EMS_BINFMT="$BINFMT_HANDLER" EMS_LOCK_ID="$LOCK_ID" EMS_BASE_SHA512="$BASE_SHA512"
export EMS_CAPTURED_AT="$CAPTURED_AT" EMS_DEPENDS="$DEPENDS"
export EMS_PACKAGES="$CRITICAL_PACKAGES" EMS_OUTPUT="$OUTPUT"

python3 <<'PY' || fail "the builder environment could not be written" builder_environment_unwritable
import hashlib
import json
import os
import pathlib

depends = os.environ.get("EMS_DEPENDS") or ""
manifest = ""
if depends:
    try:
        data = pathlib.Path(depends).read_bytes()
    except OSError as error:
        raise SystemExit(f"the dependency manifest could not be read: {error}")
    manifest = "sha256:" + hashlib.sha256(data).hexdigest()

packages = tuple(
    line.strip() for line in (os.environ.get("EMS_PACKAGES") or "").splitlines() if line.strip()
)

environment = {
    "schema_version": 1,
    "base_image_lock_id": os.environ.get("EMS_LOCK_ID", ""),
    "base_image_sha512": os.environ.get("EMS_BASE_SHA512", ""),
    "os_release": os.environ.get("EMS_OS_RELEASE", ""),
    "kernel": os.environ.get("EMS_KERNEL", ""),
    "architecture": os.environ.get("EMS_ARCH", ""),
    "python_version": os.environ.get("EMS_PYTHON", ""),
    "podman_version": os.environ.get("EMS_PODMAN", ""),
    "mmdebstrap_version": os.environ.get("EMS_MMDEBSTRAP", ""),
    "qemu_version": os.environ.get("EMS_QEMU", ""),
    "binfmt_handler": os.environ.get("EMS_BINFMT", ""),
    "dependency_manifest_sha256": manifest,
    "critical_packages": list(packages),
    "captured_at": os.environ.get("EMS_CAPTURED_AT", ""),
}

target = pathlib.Path(os.environ["EMS_OUTPUT"])
target.parent.mkdir(parents=True, exist_ok=True)
target.write_text(json.dumps(environment, indent=2, sort_keys=True) + "\n", encoding="utf-8")
PY

echo "builder environment: $OUTPUT"
echo "RESULT: PASS"
