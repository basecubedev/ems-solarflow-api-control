#!/bin/sh
# SPDX-License-Identifier: AGPL-3.0-or-later
# The gates a real A/B OS release has to pass, in order. Nothing is published.
#
#   scripts/appliance-release-gates.sh [--mode builder|production]
#                                      [--rpi-image-gen DIR] [--output DIR]
#                                      [--profile rpi4|rpi5]... [--fetch]
#                                      [--sign-key KEYID] [--source-bundle FILE]
#                                      [--keyring FILE] [--trusted-fingerprint FPR]...
#                                      [--crosscheck]
#
# There are two different questions here, and one verdict used to answer both.
#
#   --mode builder (default)   Can this source, on this builder, produce an
#                              image and an update that inspect cleanly? It
#                              builds. It may be unsigned: a rehearsal is a
#                              legitimate thing to run, and signing keys have no
#                              business inside a disposable builder guest. Its
#                              best verdict is PASS (builder qualification),
#                              which is not a release.
#
#   --mode production          Are the artefacts already built here a release?
#                              It builds nothing. It requires a signed manifest,
#                              a signature that verifies against a named keyring
#                              and trust policy, full image content inspection,
#                              the update inspection, the external sparse
#                              cross-check and the source bundle. Its verdict is
#                              PASS (production release), and it cannot be
#                              reached without signatures.
#
# The sequence:
#
#   1  source authority       the pinned tree is the pinned tree, right now
#   2  host dependencies      this host can run the generator at all
#   3  build <profile>        one image and one update per board   (builder)
#   4  inspect image          the artefact's structure and its contents
#   5  inspect update         the members a slot will actually be written from
#   6  sign / verify          a real signature by a trusted key    (production)
#   7  sparse crosscheck      a second decoder agrees              (production)
#   8  source bundle          the delivered tree is the tracked tree
#
# Strict is the default: a required gate that did not run is not a pass, because
# "no image was built" and "the image is good" are not the same answer.
# --allow-not-run is for exploring on a host without the prerequisites; it never
# prints PASS.
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
KEYRING=${EMS_APPLIANCE_OS_KEYRING:-}
FINGERPRINTS=${EMS_APPLIANCE_OS_TRUSTED_FINGERPRINTS:-}
FETCH=no
MODE=builder
CROSSCHECK=no
ALLOW_NOT_RUN=no
FAILURES=0
SKIPPED=0
REQUIRED_SKIPPED=0
NOT_RUN_NAMES=""

usage() {
    sed -n '3,52p' "$0"
}

# Everything the running mode claims. In builder mode `sign-<profile>` is
# deliberately absent: an unsigned rehearsal is legitimate, and the builder is
# the wrong place for a production key. In production mode the signature gates
# are exactly what the verdict is about.
required_gate() {
    case "$MODE" in
        builder)
            case "$1" in
                source-authority|slot-mounts|source-bundle) return 0 ;;
                build-*|inspect-image-*|inspect-update-*|describe-*) return 0 ;;
                *) return 1 ;;
            esac
            ;;
        production)
            # source-authority and slot-mounts ask about a generator checkout.
            # A finalizer builds nothing and has no checkout: what binds the
            # upstream tree here is the build authority, which is verified
            # before anything is signed.
            case "$1" in
                source-bundle) return 0 ;;
                artefacts-*|inspect-image-*|inspect-update-*) return 0 ;;
                sign-*|verify-signature-*|crosscheck-*) return 0 ;;
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
        --profile) PROFILES="$PROFILES ${2:?--profile needs rpi4 or rpi5}"; shift 2 ;;
        --profile=*) PROFILES="$PROFILES ${1#*=}"; shift ;;
        --sign-key) SIGN_KEY=${2:?--sign-key needs a key id}; shift 2 ;;
        --sign-key=*) SIGN_KEY=${1#*=}; shift ;;
        --source-bundle) SOURCE_BUNDLE=${2:?--source-bundle needs a file}; shift 2 ;;
        --source-bundle=*) SOURCE_BUNDLE=${1#*=}; shift ;;
        --keyring) KEYRING=${2:?--keyring needs a file}; shift 2 ;;
        --keyring=*) KEYRING=${1#*=}; shift ;;
        --trusted-fingerprint)
            FINGERPRINTS="$FINGERPRINTS ${2:?--trusted-fingerprint needs a fingerprint}"
            shift 2 ;;
        --trusted-fingerprint=*) FINGERPRINTS="$FINGERPRINTS ${1#*=}"; shift ;;
        --crosscheck) CROSSCHECK=yes; shift ;;
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

[ -n "$PROFILES" ] || PROFILES="rpi4 rpi5"
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

# shellcheck disable=SC2086
gate source-authority sh "$ROOT/scripts/appliance-check-rpi-image-gen.sh" $GENERATOR_ARGS
# shellcheck disable=SC2086
gate slot-mounts sh "$ROOT/scripts/appliance-verify-slot-mounts.sh" $GENERATOR_ARGS

VERSION=$(sed -n 's/^APPLIANCE_VERSION = "\(.*\)"$/\1/p' "$ROOT/appliance/version.py")

for profile in $PROFILES; do
    NAME="ems-solarflow-appliance-${VERSION}-${profile}-arm64-ab"
    AUTHORITY="$OUTPUT/$NAME.build-authority.json"

    if [ "$MODE" = builder ]; then
        # shellcheck disable=SC2086
        gate "build-$profile" sh "$ROOT/scripts/appliance-build-rpi-ab-image.sh" \
            --profile "$profile" --output "$OUTPUT" $GENERATOR_ARGS
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
        gate_json "inspect-image-$profile" "image-inspection-$profile.json" \
            sh "$ROOT/scripts/appliance-inspect-rpi-ab-image.sh" --json \
            --appliance-version "$VERSION" --build-id "$BUILD_ID" \
            "$OUTPUT/$NAME.img"
    else
        report "inspect-image-$profile" "NOT RUN (no image was built)"
    fi

    if [ -f "$OUTPUT/$NAME.update.tar.zst" ] && [ -f "$AUTHORITY" ]; then
        if [ -n "$SIGN_KEY" ]; then
            gate "sign-$profile" sh "$ROOT/scripts/appliance-build-rpi-ab-update.sh" \
                --profile "$profile" --output "$OUTPUT" \
                --update "$OUTPUT/$NAME.update.tar.zst" \
                --build-authority "$AUTHORITY" \
                --sign-key "$SIGN_KEY"
        else
            gate "describe-$profile" sh "$ROOT/scripts/appliance-build-rpi-ab-update.sh" \
                --profile "$profile" --output "$OUTPUT" \
                --update "$OUTPUT/$NAME.update.tar.zst" \
                --build-authority "$AUTHORITY"
            report "sign-$profile" "NOT RUN (no --sign-key)"
        fi
        if [ -f "$OUTPUT/$NAME.manifest.json" ]; then
            INSPECT_ARGS=""
            [ -n "$KEYRING" ] && INSPECT_ARGS="--keyring $KEYRING"
            for fingerprint in $FINGERPRINTS; do
                INSPECT_ARGS="$INSPECT_ARGS --trusted-fingerprint $fingerprint"
            done
            [ "$MODE" = production ] && INSPECT_ARGS="$INSPECT_ARGS --require-signature"
            # shellcheck disable=SC2086
            gate_json "inspect-update-$profile" "update-inspection-$profile.json" \
                sh "$ROOT/scripts/appliance-inspect-rpi-ab-update.sh" --json $INSPECT_ARGS \
                "$OUTPUT/$NAME.manifest.json"
            if [ "$MODE" = production ]; then
                if [ -n "$KEYRING" ] && [ -f "$OUTPUT/$NAME.manifest.json.asc" ]; then
                    # shellcheck disable=SC2086
                    gate "verify-signature-$profile" sh \
                        "$ROOT/scripts/appliance-inspect-rpi-ab-update.sh" \
                        --require-signature $INSPECT_ARGS "$OUTPUT/$NAME.manifest.json"
                else
                    report "verify-signature-$profile" \
                        "NOT RUN (no --keyring, or the manifest is unsigned)"
                fi
            fi
        fi
    else
        report "describe-$profile" "NOT RUN (no build authority and update artefact)"
    fi

    if [ "$CROSSCHECK" = yes ] || [ "$MODE" = production ]; then
        if [ -f "$OUTPUT/$NAME.update.tar.zst" ]; then
            gate "crosscheck-$profile" sh "$ROOT/scripts/appliance-crosscheck-sparse.sh" \
                --report "$OUTPUT/reports/sparse-crosscheck-$profile.json" \
                "$OUTPUT/$NAME.update.tar.zst"
        else
            report "crosscheck-$profile" "NOT RUN (no update artefact)"
        fi
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
