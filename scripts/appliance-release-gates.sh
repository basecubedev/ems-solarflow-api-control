#!/bin/sh
# SPDX-License-Identifier: AGPL-3.0-or-later
# The gates a real A/B OS release has to pass, in order. Nothing is published.
#
#   scripts/appliance-release-gates.sh [--rpi-image-gen DIR] [--output DIR]
#                                      [--profile rpi4|rpi5]... [--fetch]
#                                      [--sign-key KEYID] [--source-bundle FILE]
#
# Each gate reports PASS, FAIL or NOT RUN with the exact prerequisite it needs.
# The sequence is:
#
#   1  source authority       the pinned tree is the pinned tree, right now
#   2  host dependencies      this host can run the generator at all
#   3  build <profile>        one image and one update per board
#   4  inspect image          what the artefact claims about itself
#   5  inspect update         the members a slot will actually be written from
#   6  sign                   only with a build authority, only with a keyring
#   7  source bundle          the delivered tree is the tracked tree
#
# Strict is the default and is what a release must use: a required gate that
# did not run is not a pass, because "no image was built" and "the image is
# good" are not the same answer. --allow-not-run is for exploring on a host
# without the builder prerequisites; it never prints PASS.
#
# No credential is read from this file, nothing is pushed, tagged or uploaded,
# and no release is created. Signing happens only when --sign-key names a key
# already in the caller's keyring.
#
# Exit status: 0 every required gate passed, 1 a gate failed, 2 the command line
# is wrong, 3 a required gate did not run.
set -eu

ROOT=$(cd "$(dirname "$0")/.." && pwd)
OUTPUT="$ROOT/dist"
GENERATOR=${EMS_RPI_IMAGE_GEN:-}
PROFILES=""
SIGN_KEY=${EMS_APPLIANCE_OS_SIGN_KEY:-}
SOURCE_BUNDLE=""
FETCH=no
ALLOW_NOT_RUN=no
FAILURES=0
SKIPPED=0
REQUIRED_SKIPPED=0
NOT_RUN_NAMES=""

# Everything a release claims. `sign-<profile>` is deliberately not here: an
# unsigned describe run is a legitimate rehearsal, and the signature gate is
# required by the release workflow that passes --sign-key.
required_gate() {
    case "$1" in
        source-authority|slot-mounts|source-bundle) return 0 ;;
        build-*|inspect-image-*|inspect-update-*|describe-*) return 0 ;;
        *) return 1 ;;
    esac
}

usage() {
    sed -n '3,30p' "$0"
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

while [ $# -gt 0 ]; do
    case "$1" in
        --rpi-image-gen) GENERATOR=${2:?--rpi-image-gen needs a directory}; shift 2 ;;
        --rpi-image-gen=*) GENERATOR=${1#*=}; shift ;;
        --output) OUTPUT=${2:?--output needs a directory}; shift 2 ;;
        --output=*) OUTPUT=${1#*=}; shift ;;
        --profile) PROFILES="$PROFILES ${2:?--profile needs rpi4 or rpi5}"; shift 2 ;;
        --profile=*) PROFILES="$PROFILES ${1#*=}"; shift ;;
        --sign-key) SIGN_KEY=${2:?--sign-key needs a key id}; shift 2 ;;
        --sign-key=*) SIGN_KEY=${1#*=}; shift ;;
        --source-bundle) SOURCE_BUNDLE=${2:?--source-bundle needs a file}; shift 2 ;;
        --source-bundle=*) SOURCE_BUNDLE=${1#*=}; shift ;;
        --fetch) FETCH=yes; shift ;;
        --allow-not-run) ALLOW_NOT_RUN=yes; shift ;;
        -h|--help) usage; exit 0 ;;
        *) echo "unknown argument: $1" >&2; usage >&2; exit 2 ;;
    esac
done

[ -n "$PROFILES" ] || PROFILES="rpi4 rpi5"
mkdir -p "$OUTPUT/gates"

if [ "$FETCH" = yes ]; then
    gate fetch-upstream sh "$ROOT/scripts/appliance-fetch-rpi-image-gen.sh"
    [ -n "$GENERATOR" ] || GENERATOR="$ROOT/../rpi-image-gen"
fi

GENERATOR_ARGS=""
[ -n "$GENERATOR" ] && GENERATOR_ARGS="--rpi-image-gen $GENERATOR"

# shellcheck disable=SC2086
gate source-authority sh "$ROOT/scripts/appliance-check-rpi-image-gen.sh" $GENERATOR_ARGS
# shellcheck disable=SC2086
gate slot-mounts sh "$ROOT/scripts/appliance-verify-slot-mounts.sh" $GENERATOR_ARGS

for profile in $PROFILES; do
    # shellcheck disable=SC2086
    gate "build-$profile" sh "$ROOT/scripts/appliance-build-rpi-ab-image.sh" \
        --profile "$profile" --output "$OUTPUT" $GENERATOR_ARGS

    VERSION=$(sed -n 's/^APPLIANCE_VERSION = "\(.*\)"$/\1/p' "$ROOT/appliance/version.py")
    NAME="ems-solarflow-appliance-${VERSION}-${profile}-arm64-ab"

    if [ -f "$OUTPUT/$NAME.img" ]; then
        gate "inspect-image-$profile" sh "$ROOT/scripts/appliance-inspect-rpi-ab-image.sh" \
            "$OUTPUT/$NAME.img"
    else
        report "inspect-image-$profile" "NOT RUN (no image was built)"
    fi

    if [ -f "$OUTPUT/$NAME.update.tar.zst" ] && [ -f "$OUTPUT/build-authority.json" ]; then
        if [ -n "$SIGN_KEY" ]; then
            gate "sign-$profile" sh "$ROOT/scripts/appliance-build-rpi-ab-update.sh" \
                --profile "$profile" --output "$OUTPUT" \
                --update "$OUTPUT/$NAME.update.tar.zst" \
                --build-authority "$OUTPUT/build-authority.json" \
                --sign-key "$SIGN_KEY"
        else
            gate "describe-$profile" sh "$ROOT/scripts/appliance-build-rpi-ab-update.sh" \
                --profile "$profile" --output "$OUTPUT" \
                --update "$OUTPUT/$NAME.update.tar.zst" \
                --build-authority "$OUTPUT/build-authority.json"
            report "sign-$profile" "NOT RUN (no --sign-key)"
        fi
        if [ -f "$OUTPUT/$NAME.manifest.json" ]; then
            gate "inspect-update-$profile" sh \
                "$ROOT/scripts/appliance-inspect-rpi-ab-update.sh" \
                "$OUTPUT/$NAME.manifest.json"
        fi
    else
        report "describe-$profile" "NOT RUN (no build authority and update artefact)"
    fi
done

if [ -n "$SOURCE_BUNDLE" ]; then
    gate source-bundle sh "$ROOT/scripts/appliance-check-source-bundle.sh" "$SOURCE_BUNDLE"
else
    report "source-bundle" "NOT RUN (no --source-bundle)"
fi

echo
echo "logs:     $OUTPUT/gates"
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
echo "RESULT: PASS ($SKIPPED optional gate(s) NOT RUN)"
