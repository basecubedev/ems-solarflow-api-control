#!/bin/sh
# SPDX-License-Identifier: AGPL-3.0-or-later
# The gates a real image release has to pass, in order. Nothing is published.
#
#   scripts/appliance-release-gates.sh [--mode builder|production]
#                                      [--rpi-image-gen DIR] [--output DIR]
#                                      [--profile rpi3|rpi4|rpi5]... [--fetch]
#                                      [--source-bundle FILE]
#                                      [--builder-environment FILE]
#
# There are two different questions here, and one verdict used to answer both.
#
#   --mode builder (default)   Can this source, on this builder, produce an
#                              image that inspects cleanly? It builds. Its best
#                              verdict is PASS (builder qualification), which is
#                              not a release.
#
#   --mode production          Are the artefacts already built here a release?
#                              It builds nothing. It requires full image content
#                              inspection and the source bundle. Its verdict is
#                              PASS (production release).
#
# The sequence:
#
#   1  source authority       the pinned tree is the pinned tree, right now
#   2  host dependencies      this host can run the generator at all
#   3  build <profile>        one image per board                  (builder)
#   4  inspect image          the artefact's structure and its contents
#   5  source bundle          the delivered tree is the tracked tree
#
# Strict is the default: a required gate that did not run is not a pass, because
# "no image was built" and "the image is good" are not the same answer.
# --allow-not-run is for exploring on a host without the prerequisites; it never
# prints PASS.
#
# No credential is read from this file, nothing is pushed, tagged or uploaded,
# and no release is created.
#
# Exit status: 0 every required gate passed, 1 a gate failed, 2 the command line
# is wrong, 3 a required gate did not run.
set -eu

ROOT=$(cd "$(dirname "$0")/.." && pwd)
OUTPUT="$ROOT/dist"
GENERATOR=${EMS_RPI_IMAGE_GEN:-}
PROFILES=""
SOURCE_BUNDLE=""
BUILDER_ENVIRONMENT=""
FETCH=no
MODE=builder
ALLOW_NOT_RUN=no
FAILURES=0
SKIPPED=0
REQUIRED_SKIPPED=0
NOT_RUN_NAMES=""

usage() {
    sed -n '3,42p' "$0"
}

# Everything the running mode claims.
required_gate() {
    case "$MODE" in
        builder)
            case "$1" in
                source-authority|source-bundle) return 0 ;;
                build-*|inspect-image-*) return 0 ;;
                *) return 1 ;;
            esac
            ;;
        production)
            # source-authority asks about a generator checkout. A finalizer
            # builds nothing and has no checkout: what binds the upstream tree
            # here is the build authority, which is verified before anything is
            # attested.
            case "$1" in
                source-bundle) return 0 ;;
                artefacts-*|inspect-image-*) return 0 ;;
                *) return 1 ;;
            esac
            ;;
    esac
    return 1
}

report() {
    printf '%-28s %s\n' "$1" "$2"
    case "$2" in
        FAIL*) FAILURES=$((FAILURES + 1)) ;;
        "NOT RUN"*)
            SKIPPED=$((SKIPPED + 1))
            if required_gate "$1"; then
                REQUIRED_SKIPPED=$((REQUIRED_SKIPPED + 1))
                NOT_RUN_NAMES="$NOT_RUN_NAMES $1"
            fi
            ;;
    esac
}

gate() {
    name=$1
    shift
    set +e
    "$@" >"$OUTPUT/gates/$name.log" 2>&1
    status=$?
    set -e
    case $status in
        0) report "$name" "PASS" ;;
        3) report "$name" "NOT RUN (see $OUTPUT/gates/$name.log)" ;;
        *) report "$name" "FAIL (see $OUTPUT/gates/$name.log)" ;;
    esac
    return 0
}

# The evidence a hardware kit is assembled from, as JSON rather than as a log.
gate_json() {
    name=$1
    target=$2
    shift 2
    set +e
    "$@" >"$OUTPUT/reports/$target" 2>"$OUTPUT/gates/$name.log"
    status=$?
    set -e
    case $status in
        0) report "$name" "PASS" ;;
        3) report "$name" "NOT RUN (see $OUTPUT/gates/$name.log)" ;;
        *) report "$name" "FAIL (see $OUTPUT/gates/$name.log)" ;;
    esac
    return 0
}

while [ $# -gt 0 ]; do
    case "$1" in
        --mode) MODE=${2:?--mode needs builder or production}; shift 2 ;;
        --mode=*) MODE=${1#*=}; shift ;;
        --rpi-image-gen) GENERATOR=${2:?--rpi-image-gen needs a directory}; shift 2 ;;
        --rpi-image-gen=*) GENERATOR=${1#*=}; shift ;;
        --output) OUTPUT=${2:?--output needs a directory}; shift 2 ;;
        --output=*) OUTPUT=${1#*=}; shift ;;
        --profile) PROFILES="$PROFILES ${2:?--profile needs rpi3, rpi4 or rpi5}"; shift 2 ;;
        --profile=*) PROFILES="$PROFILES ${1#*=}"; shift ;;
        --source-bundle) SOURCE_BUNDLE=${2:?--source-bundle needs a file}; shift 2 ;;
        --source-bundle=*) SOURCE_BUNDLE=${1#*=}; shift ;;
        --builder-environment) BUILDER_ENVIRONMENT=${2:?--builder-environment needs a file}; shift 2 ;;
        --builder-environment=*) BUILDER_ENVIRONMENT=${1#*=}; shift ;;
        --fetch) FETCH=yes; shift ;;
        --allow-not-run) ALLOW_NOT_RUN=yes; shift ;;
        -h|--help) usage; exit 0 ;;
        *) echo "unknown argument: $1" >&2; usage >&2; exit 2 ;;
    esac
done

case "$MODE" in
    builder|production) ;;
    *) echo "--mode is builder or production, not $MODE" >&2; exit 2 ;;
esac

# Which boards build an image, from the one table that knows. Listing them here
# would let a release publish an incomplete matrix the moment a profile is added
# or removed.
default_profiles() {
    PYTHONPATH="$ROOT" python3 - <<'PY'
from appliance import rpi_image_gen

print(" ".join(sorted(rpi_image_gen.HARDWARE_PROFILES)))
PY
}

[ -n "$PROFILES" ] || PROFILES=$(default_profiles) \
    || fail "the profile list could not be resolved" hardware_profile_unknown
mkdir -p "$OUTPUT/gates" "$OUTPUT/reports"

for profile in $PROFILES; do
    PYTHONPATH="$ROOT" python3 - "$profile" <<'PY' || exit 2
import sys

from appliance import build_authority

try:
    build_authority.validate_profile(sys.argv[1])
except build_authority.BuildAuthorityError as error:
    sys.exit(f"appliance-release-gates: {error.code}: {error.message}")
PY
done

if [ "$FETCH" = yes ]; then
    gate fetch-upstream sh "$ROOT/scripts/appliance-fetch-rpi-image-gen.sh"
    [ -n "$GENERATOR" ] || GENERATOR="$ROOT/../rpi-image-gen"
fi

GENERATOR_ARGS=""
[ -n "$GENERATOR" ] && GENERATOR_ARGS="--rpi-image-gen $GENERATOR"
BUILDER_ENVIRONMENT_ARG=""
[ -n "$BUILDER_ENVIRONMENT" ] && \
    BUILDER_ENVIRONMENT_ARG="--builder-environment $BUILDER_ENVIRONMENT"

# shellcheck disable=SC2086
gate source-authority sh "$ROOT/scripts/appliance-check-rpi-image-gen.sh" $GENERATOR_ARGS
. "$ROOT/scripts/lib/appliance-version.sh"
VERSION=$(appliance_version "${VERSION:-}")

for profile in $PROFILES; do
    NAME="ems-solarflow-appliance-${VERSION}-${profile}-arm64"
    AUTHORITY="$OUTPUT/$NAME.build-authority.json"

    if [ "$MODE" = builder ]; then
        # shellcheck disable=SC2086
        # The builder environment belongs to the artefact, not to the run that
        # happened to produce it. Without it the build authority records an
        # empty environment, which is refused at signing time by
        # require_environment -- so the gate would pass, the build would report
        # completed, and the release would fail hours later on an artefact that
        # can never be signed. Committed evidence shows exactly that shape:
        # reports/appliance/2026-08-11-head/build-authority-rpi5-1.json.
        gate "build-$profile" sh "$ROOT/scripts/appliance-build-rpi-image.sh" \
            --profile "$profile" --output "$OUTPUT" \
            $BUILDER_ENVIRONMENT_ARG $GENERATOR_ARGS
    else
        # A production run consumes what a builder run proved. It never builds:
        # a release that rebuilt its own artefacts could not be the one the
        # builder qualification passed.
        if [ -f "$OUTPUT/$NAME.img" ] && [ -f "$AUTHORITY" ]; then
            report "artefacts-$profile" "PASS"
        else
            report "artefacts-$profile" "NOT RUN (no built image and build authority in $OUTPUT)"
            continue
        fi
    fi

    BUILD_ID=""
    if [ -f "$AUTHORITY" ]; then
        BUILD_ID=$(PYTHONPATH="$ROOT" python3 - "$AUTHORITY" <<'PY' 2>/dev/null || true
import sys

from appliance import build_authority

print(build_authority.read(sys.argv[1]).build_id)
PY
)
    fi

    if [ -f "$OUTPUT/$NAME.img" ]; then
        # The version the image carries, not the version this checkout is at.
        # Since --manager-package the image bakes in the newest *published*
        # stable Manager, chosen independently of this tree -- so comparing
        # against appliance/version.py fails every build made while the next
        # release waits for its signing approval, and reports it as a broken
        # image.
        BAKED=$(sed -n 's/.*"appliance_package_version": "\([^"]*\)".*/\1/p' \
            "$OUTPUT/$NAME.build.json" 2>/dev/null | head -1)
        [ -n "$BAKED" ] || BAKED=$VERSION
        gate_json "inspect-image-$profile" "image-inspection-$profile.json" \
            sh "$ROOT/scripts/appliance-inspect-rpi-image.sh" --json \
            --appliance-version "$BAKED" --build-id "$BUILD_ID" \
            "$OUTPUT/$NAME.img"
    else
        report "inspect-image-$profile" "NOT RUN (no image was built)"
    fi

done

if [ -n "$SOURCE_BUNDLE" ]; then
    gate source-bundle sh "$ROOT/scripts/appliance-check-source-bundle.sh" "$SOURCE_BUNDLE"
else
    report "source-bundle" "NOT RUN (no --source-bundle)"
fi

echo
echo "mode:     $MODE"
echo "logs:     $OUTPUT/gates"
echo "reports:  $OUTPUT/reports"
echo "failed:   $FAILURES"
echo "not run:  $SKIPPED ($REQUIRED_SKIPPED required)"
[ -n "$NOT_RUN_NAMES" ] && echo "required gates that did not run:$NOT_RUN_NAMES"
echo "Nothing was published, tagged or uploaded."

# A failure outranks a skip: something was actually proven wrong.
[ "$FAILURES" -eq 0 ] || { echo "RESULT: FAIL ($FAILURES gate(s))"; exit 1; }

if [ "$REQUIRED_SKIPPED" -gt 0 ]; then
    if [ "$ALLOW_NOT_RUN" = yes ]; then
        echo "RESULT: INCOMPLETE ($REQUIRED_SKIPPED required gate(s) NOT RUN)"
        exit 0
    fi
    echo "RESULT: NOT RUN ($REQUIRED_SKIPPED required gate(s) never executed)"
    exit 3
fi

if [ "$MODE" = production ]; then
    echo "RESULT: PASS (production release, $SKIPPED optional gate(s) NOT RUN)"
else
    echo "RESULT: PASS (builder qualification, $SKIPPED optional gate(s) NOT RUN)"
    echo "This is not a release: run --mode production against a signing environment."
fi
