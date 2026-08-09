#!/bin/sh
# SPDX-License-Identifier: AGPL-3.0-or-later
# Build the fail-safe A/B appliance image with Raspberry Pi's rpi-image-gen.
#
#   scripts/appliance-build-rpi-ab-image.sh [--output DIR] [--build-id ID]
#                                           [--rpi-image-gen DIR]
#
# rpi-image-gen is supplied by the build host and is not vendored here. Its
# revision is pinned in packaging/appliance/image/rpi-image-gen.lock and the
# checkout is verified against that lock before anything runs: image-rota owns
# the partition table, the slot labels and the shared-slot mechanism, so a
# different generator produces an image the runtime does not agree with.
#
# Exit status: 0 the image was built, 1 the build failed, 2 the command line is
# wrong, 3 the host cannot run the build. A host that cannot build reports
# NOT RUN and never a pass — an image nobody built is not an image that works.
set -eu

ROOT=$(cd "$(dirname "$0")/.." && pwd)
IMAGE_DIR="$ROOT/packaging/appliance/image"
LOCK="$IMAGE_DIR/rpi-image-gen.lock"
CONFIG="ems-appliance-ab.yaml"
OUTPUT="$ROOT/dist"
BUILD_ID=""
GENERATOR=${EMS_RPI_IMAGE_GEN:-}

usage() {
    sed -n '3,17p' "$0"
}

not_run() {
    echo "appliance-build-rpi-ab-image: $1" >&2
    echo "RESULT: NOT RUN ($2)" >&2
    exit 3
}

fail() {
    echo "appliance-build-rpi-ab-image: $1" >&2
    echo "RESULT: FAIL ($2)" >&2
    exit 1
}

while [ $# -gt 0 ]; do
    case "$1" in
        --output) OUTPUT=${2:?--output needs a directory}; shift 2 ;;
        --output=*) OUTPUT=${1#*=}; shift ;;
        --build-id) BUILD_ID=${2:?--build-id needs a value}; shift 2 ;;
        --build-id=*) BUILD_ID=${1#*=}; shift ;;
        --rpi-image-gen) GENERATOR=${2:?--rpi-image-gen needs a directory}; shift 2 ;;
        --rpi-image-gen=*) GENERATOR=${1#*=}; shift ;;
        -h|--help) usage; exit 0 ;;
        *) echo "unknown argument: $1" >&2; usage >&2; exit 2 ;;
    esac
done

[ -f "$LOCK" ] || fail "$LOCK is missing" lock_missing
[ -f "$IMAGE_DIR/config/$CONFIG" ] || fail "$IMAGE_DIR/config/$CONFIG is missing" config_missing
command -v python3 >/dev/null 2>&1 || not_run "python3 is not installed" required_tool_missing

if [ -z "$GENERATOR" ]; then
    for candidate in /usr/share/rpi-image-gen /opt/rpi-image-gen "$ROOT/../rpi-image-gen"; do
        [ -d "$candidate" ] && GENERATOR=$candidate && break
    done
fi
[ -n "$GENERATOR" ] && [ -d "$GENERATOR" ] \
    || not_run "rpi-image-gen was not found; pass --rpi-image-gen DIR or set EMS_RPI_IMAGE_GEN" \
               rpi_image_gen_unavailable

# The compatibility probe is the gate. It reports incompatible (exit 1) and
# missing host dependencies (exit 3) separately, and neither is a pass.
set +e
"$ROOT/scripts/appliance-check-rpi-image-gen.sh" --rpi-image-gen "$GENERATOR" >&2
compatibility=$?
set -e
case $compatibility in
    0) ;;
    1) fail "$GENERATOR is not the pinned rpi-image-gen contract" rpi_image_gen_incompatible ;;
    *) not_run "$GENERATOR is compatible but this host cannot build with it" \
               rpi_image_gen_dependencies_missing ;;
esac

VERSION=$(sed -n 's/^APPLIANCE_VERSION = "\(.*\)"$/\1/p' "$ROOT/appliance/version.py")
[ -n "$VERSION" ] || fail "cannot read APPLIANCE_VERSION" version_unreadable
[ -n "$BUILD_ID" ] || BUILD_ID=$(date -u +%Y%m%d%H%M%S 2>/dev/null || echo unknown)
GENERATOR_REVISION=$(git -C "$GENERATOR" rev-parse HEAD 2>/dev/null || echo unknown)
PROJECT_REVISION=$(git -C "$ROOT" rev-parse --short HEAD 2>/dev/null || echo unknown)
NAME="ems-solarflow-appliance-${VERSION}-arm64-ab"

mkdir -p "$OUTPUT" || fail "cannot create $OUTPUT" output_unusable
WORK="$OUTPUT/build"
mkdir -p "$WORK" || fail "cannot create $WORK" output_unusable

echo "== building the appliance package =="
"$ROOT/packaging/appliance/build-deb.sh" --output "$OUTPUT" --arch arm64 >/dev/null \
    || fail "the arm64 package build failed" package_build_failed
PACKAGE=""
for candidate in "$OUTPUT"/ems-appliance-manager_*_arm64.deb; do
    [ -f "$candidate" ] && PACKAGE=$candidate
done
[ -n "$PACKAGE" ] || fail "the package build produced no .deb" package_build_failed
PACKAGE_SHA256=$(sha256sum "$PACKAGE" | cut -d' ' -f1)

echo "== building the A/B image =="
# -S makes packaging/appliance/image the source root, so the project's config
# and layer are found beside upstream's. Overrides are passed after --, which
# is how rpi-image-gen takes build-time variables.
LOG="$OUTPUT/$NAME.build.log"
(
    cd "$GENERATOR" || exit 1
    ./rpi-image-gen build \
        -c "$CONFIG" \
        -S "$IMAGE_DIR" \
        -B "$WORK" \
        -- \
        "IGconf_emsappliance_package=$PACKAGE" \
        "IGconf_emsappliance_release_version=$VERSION" \
        "IGconf_emsappliance_build_id=$BUILD_ID"
) >"$LOG" 2>&1 || fail "rpi-image-gen could not build the image; see $LOG" image_build_failed

BUILT=$(find "$WORK" -name '*.img' -type f 2>/dev/null | head -n 1)
[ -n "$BUILT" ] || fail "the build produced no image; see $LOG" image_build_failed
cp "$BUILT" "$OUTPUT/$NAME.img" || fail "the built image could not be collected" output_unusable
( cd "$OUTPUT" && sha256sum "$NAME.img" > "$NAME.img.sha256" )

UPDATE=$(find "$WORK" -name 'update.tar.zst' -type f 2>/dev/null | head -n 1)
[ -n "$UPDATE" ] && cp "$UPDATE" "$OUTPUT/$NAME.update.tar.zst"

cat > "$OUTPUT/$NAME.build.json" <<JSON
{
  "format_version": 2,
  "release_version": "$VERSION",
  "build_id": "$BUILD_ID",
  "architecture": "arm64",
  "image_layer": "image-rota",
  "rpi_image_gen_revision": "$GENERATOR_REVISION",
  "project_revision": "$PROJECT_REVISION",
  "appliance_package": "$(basename "$PACKAGE")",
  "appliance_package_sha256": "$PACKAGE_SHA256",
  "image_sha256": "$(cut -d' ' -f1 < "$OUTPUT/$NAME.img.sha256")",
  "update_artifact": "$([ -n "$UPDATE" ] && echo "$NAME.update.tar.zst" || echo "")"
}
JSON

echo
echo "image:    $OUTPUT/$NAME.img"
echo "checksum: $OUTPUT/$NAME.img.sha256"
echo "metadata: $OUTPUT/$NAME.build.json"
[ -n "$UPDATE" ] && echo "update:   $OUTPUT/$NAME.update.tar.zst"
echo
echo "Nothing was published. Sign and publish through the release pipeline; see"
echo "packaging/appliance/image/README.md."
echo "RESULT: PASS (built $NAME.img)"
