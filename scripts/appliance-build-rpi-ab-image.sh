#!/bin/sh
# SPDX-License-Identifier: AGPL-3.0-or-later
# Build the fail-safe A/B appliance image with Raspberry Pi's rpi-image-gen.
#
#   scripts/appliance-build-rpi-ab-image.sh --profile rpi4|rpi5 [--output DIR]
#                                           [--build-id ID] [--rpi-image-gen DIR]
#
# One artefact per board. The device layer selects the kernel and firmware, so
# a Pi 5 image is not a Pi 4 image and neither may claim to be the other.
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
PROFILE=rpi5
OUTPUT="$ROOT/dist"
BUILD_ID=""
GENERATOR=${EMS_RPI_IMAGE_GEN:-}

usage() {
    sed -n '3,21p' "$0"
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
        --profile) PROFILE=${2:?--profile needs rpi4 or rpi5}; shift 2 ;;
        --profile=*) PROFILE=${1#*=}; shift ;;
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
CONFIG="$IMAGE_DIR/profiles/${PROFILE}-ab.yaml"
[ -f "$CONFIG" ] || fail "there is no build profile for $PROFILE" hardware_profile_unknown
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

# An image whose shared binds are generated but never activated loses every
# write at the next slot switch, silently. That has to fail the build.
set +e
"$ROOT/scripts/appliance-verify-slot-mounts.sh" --rpi-image-gen "$GENERATOR" >&2
mounts=$?
set -e
case $mounts in
    0) ;;
    1) fail "the generated persistence units are incomplete" persistence_units_incomplete ;;
    *) not_run "the slot-shared generator could not be verified on this host" \
               persistence_units_unverified ;;
esac

VERSION=$(sed -n 's/^APPLIANCE_VERSION = "\(.*\)"$/\1/p' "$ROOT/appliance/version.py")
[ -n "$VERSION" ] || fail "cannot read APPLIANCE_VERSION" version_unreadable
[ -n "$BUILD_ID" ] || BUILD_ID=$(date -u +%Y%m%d%H%M%S 2>/dev/null || echo unknown)
NAME="ems-solarflow-appliance-${VERSION}-${PROFILE}-arm64-ab"

# The project's own tree, to the same standard as the generator's. A revision
# alone says nothing about the files this build is about to package.
PROJECT=$(PYTHONPATH="$ROOT" python3 - "$ROOT" <<'PY'
import sys

from appliance import project_source

try:
    identity = project_source.assert_clean(sys.argv[1])
except project_source.ProjectSourceError as exc:
    sys.exit(f"{exc.code}: {exc.message}")
print(f"REVISION={identity.revision}")
print(f"TREE={identity.tree_sha256}")
PY
) || fail "the project source tree is not a revision this build may claim" \
        project_source_unprovable
PROJECT_REVISION=$(echo "$PROJECT" | sed -n 's/^REVISION=//p')
PROJECT_TREE=$(echo "$PROJECT" | sed -n 's/^TREE=//p')
DEVICE_LAYER=$(PYTHONPATH="$ROOT" python3 -c \
    "from appliance import rpi_image_gen as m; print(m.read_profile('$CONFIG').device_layer)") \
    || fail "$CONFIG does not resolve to a known hardware profile" hardware_profile_unknown
BOARD_CLASSES=$(PYTHONPATH="$ROOT" python3 -c \
    "import json
from appliance import rpi_image_gen as m
print(json.dumps(list(m.read_profile('$CONFIG').compatible_board_classes)))")

mkdir -p "$OUTPUT" || fail "cannot create $OUTPUT" output_unusable
# One build, one fresh directory. A reused one is how yesterday's update.tar.zst
# ends up beside today's metadata and gets signed as if this build produced it.
WORK=$(PYTHONPATH="$ROOT" python3 -c \
    "from appliance import build_authority; print(build_authority.prepare_output('$OUTPUT', build_id='$BUILD_ID'))") \
    || fail "the build output directory could not be claimed for $BUILD_ID" output_unusable

echo "== building the appliance package =="
"$ROOT/packaging/appliance/build-deb.sh" --output "$OUTPUT" --arch arm64 >/dev/null \
    || fail "the arm64 package build failed" package_build_failed
PACKAGE=""
for candidate in "$OUTPUT"/ems-appliance-manager_*_arm64.deb; do
    [ -f "$candidate" ] && PACKAGE=$candidate
done
[ -n "$PACKAGE" ] || fail "the package build produced no .deb" package_build_failed
PACKAGE_SHA256=$(sha256sum "$PACKAGE" | cut -d' ' -f1)

echo "== proving the source tree one last time =="
# The compatibility probe above ran before the package build. This is the check
# that matters: the tree ./rpi-image-gen build is about to read, proven at the
# moment it is read, so an edit made in between cannot reach the artefact.
SOURCE=$(PYTHONPATH="$ROOT" python3 - "$GENERATOR" <<'PY'
import sys

from appliance import rpi_image_gen

try:
    report = rpi_image_gen.assert_buildable(sys.argv[1])
except rpi_image_gen.ImageGenError as exc:
    sys.exit(f"{exc.code}: {exc.message}")
print(f"FORM={report.source_identity}")
print(f"REVISION={report.revision}")
print(f"TREE={report.tree_digest or report.revision}")
PY
) || fail "$GENERATOR changed after it was checked" rpi_image_gen_source_modified
SOURCE_FORM=$(echo "$SOURCE" | sed -n 's/^FORM=//p')
GENERATOR_REVISION=$(echo "$SOURCE" | sed -n 's/^REVISION=//p')
SOURCE_TREE=$(echo "$SOURCE" | sed -n 's/^TREE=//p')

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

echo "== proving both source trees are still the ones that were read =="
# A build takes long enough that either tree can be edited while it runs. The
# pre-build proof closes ordinary TOCTOU; this closes the build window itself,
# and completed authority is only issued for trees that did not move.
PYTHONPATH="$ROOT" python3 - "$ROOT" "$PROJECT_REVISION" "$PROJECT_TREE" <<'PY' \
    || fail "the project source tree changed while the build was running" \
            build_source_changed_during_build
import sys

from appliance import project_source

root, revision, tree = sys.argv[1:4]
identity = project_source.ProjectSource(revision=revision, tree_sha256=tree)
try:
    project_source.assert_unchanged(root, identity)
except project_source.ProjectSourceError as exc:
    sys.exit(f"{exc.code}: {exc.message}")
PY

PYTHONPATH="$ROOT" python3 - "$GENERATOR" "$SOURCE_TREE" <<'PY' \
    || fail "$GENERATOR changed while the build was running" \
            build_source_changed_during_build
import sys

from appliance import rpi_image_gen

root, tree = sys.argv[1:3]
try:
    report = rpi_image_gen.assert_buildable(root)
except rpi_image_gen.ImageGenError as exc:
    sys.exit(f"{exc.code}: {exc.message}")
observed = report.tree_digest or report.revision
if observed != tree:
    sys.exit(f"build_source_changed_during_build: {root} hashes to {observed}, not {tree}")
PY

# What this completed run produced, hashed. Production release signing verifies
# the artefact in front of it against exactly this, so an artefact nobody built
# with the pinned generator cannot inherit its provenance.
AUTHORITY=$(PYTHONPATH="$ROOT" python3 - "$WORK" "$SOURCE_FORM" "$GENERATOR_REVISION" \
    "$SOURCE_TREE" "$PROFILE" "$PROJECT_REVISION" "$PROJECT_TREE" "$BUILD_ID" \
    "$PACKAGE_SHA256" \
    "$OUTPUT/$NAME.img" "$([ -n "$UPDATE" ] && echo "$OUTPUT/$NAME.update.tar.zst" || echo "")" \
    <<'PY'
import sys

from appliance import build_authority

(work, form, revision, tree, profile, project_revision, project_tree, build_id,
 package, image, update) = sys.argv[1:12]
authority = build_authority.BuildAuthority(
    builder=build_authority.Builder(
        source_form=form, revision=revision, source_tree_sha256=tree
    ),
    project=build_authority.Project(
        revision=project_revision, tree_sha256=project_tree
    ),
    profile=profile,
    build_id=build_id,
    image=build_authority.Artefact(
        path=image, sha256=build_authority.file_sha256(image)
    ),
    update=build_authority.Artefact(
        path=update, sha256=build_authority.file_sha256(update) if update else ""
    ),
    package_sha256=package,
    completed=True,
)
print(build_authority.write(work, authority))
PY
) || fail "the build authority could not be written" build_authority_unwritable
cp "$AUTHORITY" "$OUTPUT/build-authority.json"

cat > "$OUTPUT/$NAME.build.json" <<JSON
{
  "format_version": 2,
  "release_version": "$VERSION",
  "build_id": "$BUILD_ID",
  "architecture": "arm64",
  "device_layer": "$DEVICE_LAYER",
  "compatible_board_classes": $BOARD_CLASSES,
  "hardware_profile": "$PROFILE",
  "image_layer": "image-rota",
  "rpi_image_gen_revision": "$GENERATOR_REVISION",
  "rpi_image_gen_source_form": "$SOURCE_FORM",
  "rpi_image_gen_source_tree": "$SOURCE_TREE",
  "build_authority": "build-authority.json",
  "project_revision": "$PROJECT_REVISION",
  "project_tree_sha256": "$PROJECT_TREE",
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
echo "authority: $OUTPUT/build-authority.json"
[ -n "$UPDATE" ] && echo "update:   $OUTPUT/$NAME.update.tar.zst"
echo
echo "Nothing was published. Sign and publish through the release pipeline; see"
echo "packaging/appliance/image/README.md."
echo "RESULT: PASS (built $NAME.img)"
