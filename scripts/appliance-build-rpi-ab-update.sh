#!/bin/sh
# SPDX-License-Identifier: AGPL-3.0-or-later
# Build the A/B update artifact and its release metadata.
#
#   scripts/appliance-build-rpi-ab-update.sh [--output DIR] [--image IMG]
#                                            [--build-id ID] [--sign-key KEYID]
#
# The artifact is the rpi-image-gen A/B update model wrapped in project release
# metadata: a .tar.zst holding the boot and root filesystem images, a manifest
# describing what it is compatible with, a checksum and a detached signature.
#
# The manifest never contains private signing material. This script signs only
# when --sign-key names a key that is already in the caller's keyring; it never
# generates, imports or embeds one.
#
# Exit status: 0 built, 1 the build failed, 2 the command line is wrong, 3 the
# host cannot build. A host that cannot build reports NOT RUN, never a pass.
set -eu

ROOT=$(cd "$(dirname "$0")/.." && pwd)
IMAGE_DIR="$ROOT/packaging/appliance/image"
LAYOUT="$IMAGE_DIR/manifests/layout.json"
OUTPUT="$ROOT/dist"
SOURCE_IMAGE=""
BUILD_ID=""
SIGN_KEY=${EMS_APPLIANCE_OS_SIGN_KEY:-}
REQUIRED_TOOLS="python3 tar zstd sha256sum partx dd"

usage() {
    sed -n '3,19p' "$0"
}

not_run() {
    echo "appliance-build-rpi-ab-update: $1" >&2
    echo "RESULT: NOT RUN ($2)" >&2
    exit 3
}

fail() {
    echo "appliance-build-rpi-ab-update: $1" >&2
    echo "RESULT: FAIL ($2)" >&2
    exit 1
}

while [ $# -gt 0 ]; do
    case "$1" in
        --output) OUTPUT=${2:?--output needs a directory}; shift 2 ;;
        --output=*) OUTPUT=${1#*=}; shift ;;
        --image) SOURCE_IMAGE=${2:?--image needs a file}; shift 2 ;;
        --image=*) SOURCE_IMAGE=${1#*=}; shift ;;
        --build-id) BUILD_ID=${2:?--build-id needs a value}; shift 2 ;;
        --build-id=*) BUILD_ID=${1#*=}; shift ;;
        --sign-key) SIGN_KEY=${2:?--sign-key needs a key id}; shift 2 ;;
        --sign-key=*) SIGN_KEY=${1#*=}; shift ;;
        -h|--help) usage; exit 0 ;;
        *) echo "unknown argument: $1" >&2; usage >&2; exit 2 ;;
    esac
done

[ -f "$LAYOUT" ] || fail "$LAYOUT is missing" layout_missing

missing=""
for tool in $REQUIRED_TOOLS; do
    command -v "$tool" >/dev/null 2>&1 || missing="$missing $tool"
done
[ -z "$missing" ] || not_run "not installed:$missing" required_tool_missing

VERSION=$(sed -n 's/^APPLIANCE_VERSION = "\(.*\)"$/\1/p' "$ROOT/appliance/version.py")
[ -n "$VERSION" ] || fail "cannot read APPLIANCE_VERSION" version_unreadable
[ -n "$BUILD_ID" ] || BUILD_ID=$(date -u +%Y%m%d%H%M%S 2>/dev/null || echo unknown)
NAME="ems-solarflow-appliance-${VERSION}-arm64-ab"

if [ -z "$SOURCE_IMAGE" ]; then
    SOURCE_IMAGE=$(ls "$OUTPUT/$NAME.img" 2>/dev/null || true)
fi
[ -n "$SOURCE_IMAGE" ] && [ -f "$SOURCE_IMAGE" ] \
    || not_run "no A/B image to extract from; build one first or pass --image" \
               source_image_unavailable

mkdir -p "$OUTPUT" || fail "cannot create $OUTPUT" output_unusable
WORK=$(mktemp -d "${TMPDIR:-/tmp}/ems-ab-update.XXXXXX") \
    || not_run "a working directory could not be created" working_directory_unusable
trap 'rm -rf "$WORK"' EXIT

partition_number() {
    PYTHONPATH="$ROOT" python3 -c '
import sys
from appliance import ab_image
layout = ab_image.read_layout(sys.argv[1])
print(next(int(entry["partition"]) for entry in layout["partitions"]
           if entry["role"] == sys.argv[2]))
' "$LAYOUT" "$1"
}

extract() {
    role=$1
    target=$2
    number=$(partition_number "$role")
    start=$(partx --show --output START --noheadings --nr "$number" "$SOURCE_IMAGE" | tr -d ' ')
    sectors=$(partx --show --output SECTORS --noheadings --nr "$number" "$SOURCE_IMAGE" | tr -d ' ')
    [ -n "$start" ] && [ -n "$sectors" ] \
        || fail "partition $number ($role) could not be located in $SOURCE_IMAGE" partition_missing
    dd if="$SOURCE_IMAGE" of="$target" bs=512 skip="$start" count="$sectors" status=none \
        || fail "partition $number ($role) could not be extracted" extract_failed
}

echo "== extracting the slot images =="
extract boot_a "$WORK/boot-a.img"
extract boot_b "$WORK/boot-b.img"
extract root_a "$WORK/root.img"

digest_of() {
    printf 'sha256:%s' "$(sha256sum "$1" | cut -d' ' -f1)"
}

BOOT_A_DIGEST=$(digest_of "$WORK/boot-a.img")
BOOT_B_DIGEST=$(digest_of "$WORK/boot-b.img")
ROOT_DIGEST=$(digest_of "$WORK/root.img")

echo "== packing the update artifact =="
( cd "$WORK" && tar --sort=name --owner=0 --group=0 --numeric-owner \
    --mtime='@0' -cf - boot-a.img boot-b.img root.img ) \
    | zstd -19 -T0 -q -o "$OUTPUT/$NAME.tar.zst" \
    || fail "the update archive could not be packed" pack_failed

ARCHIVE_DIGEST=$(digest_of "$OUTPUT/$NAME.tar.zst")
ARCHIVE_SIZE=$(stat -c '%s' "$OUTPUT/$NAME.tar.zst")

PYTHONPATH="$ROOT" python3 - "$LAYOUT" "$OUTPUT/$NAME.manifest.json" <<PY
import json
import sys

from appliance import ab_image, version

layout = ab_image.read_layout(sys.argv[1])
manifest = {
    "format_version": 1,
    "release_version": "$VERSION",
    "build_id": "$BUILD_ID",
    "created_at": "$(date -u +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || echo unknown)",
    "architecture": "arm64",
    "compatible_hardware": ["raspberrypi,4-model-b", "raspberrypi,5-model-b"],
    "os_release": "Raspberry Pi OS Trixie arm64",
    "rpi_image_gen_revision": "${EMS_RPI_IMAGE_GEN_REVISION:-unknown}",
    "project_revision": "$(git -C "$ROOT" rev-parse --short HEAD 2>/dev/null || echo unknown)",
    "appliance_manager_version": version.APPLIANCE_VERSION,
    "minimum_appliance_manager_version": version.APPLIANCE_VERSION,
    "layout_id": layout["layout_id"],
    "slot_schema_version": layout["slot_schema_version"],
    "persistent_schema_version": layout["persistent_schema_version"],
    "archive": {
        "name": "$NAME.tar.zst",
        "digest": "$ARCHIVE_DIGEST",
        "size_bytes": $ARCHIVE_SIZE,
        "compression": "zstd",
    },
    "members": {
        "boot-a.img": {"digest": "$BOOT_A_DIGEST", "slot": "A", "role": "boot"},
        "boot-b.img": {"digest": "$BOOT_B_DIGEST", "slot": "B", "role": "boot"},
        "root.img": {"digest": "$ROOT_DIGEST", "role": "root"},
    },
}
with open(sys.argv[2], "w", encoding="utf-8") as handle:
    handle.write(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
PY

( cd "$OUTPUT" && sha256sum "$NAME.tar.zst" "$NAME.manifest.json" > "$NAME.sha256" )

if [ -n "$SIGN_KEY" ]; then
    command -v gpg >/dev/null 2>&1 \
        || not_run "gpg is not installed, so the manifest cannot be signed" \
                   required_tool_missing
    gpg --batch --yes --local-user "$SIGN_KEY" --detach-sign --armor \
        --output "$OUTPUT/$NAME.manifest.json.asc" "$OUTPUT/$NAME.manifest.json" \
        || fail "the manifest could not be signed with $SIGN_KEY" signing_failed
    echo "signature: $OUTPUT/$NAME.manifest.json.asc"
else
    echo "appliance-build-rpi-ab-update: unsigned; the runtime refuses this artifact" \
         "in production. Pass --sign-key to produce a release artifact." >&2
fi

echo
echo "artifact: $OUTPUT/$NAME.tar.zst"
echo "manifest: $OUTPUT/$NAME.manifest.json"
echo "checksum: $OUTPUT/$NAME.sha256"
echo
echo "Nothing was published, tagged or uploaded."
echo "RESULT: PASS (built $NAME.tar.zst)"
