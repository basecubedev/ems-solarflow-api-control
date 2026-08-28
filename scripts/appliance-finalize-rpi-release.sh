#!/bin/sh
# SPDX-License-Identifier: AGPL-3.0-or-later
# Turn artefacts a builder qualified into a signed release, in a trusted place.
#
#   scripts/appliance-finalize-rpi-release.sh --sign-key KEYID --keyring FILE
#          --trusted-fingerprint FPR... [--dist DIR] [--profile rpi3|rpi4|rpi5]...
#          --source-bundle FILE [--source-authority FILE] [--runtime-gates FILE]
#          --package FILE [--builder-lock FILE] [--kit DIR] [--no-kit]
#
# The builder guest is disposable, runs as root, installs whatever the generator
# declares and is thrown away afterwards. A production signing key has no
# business in it: whoever can reach that guest can sign a release. So the
# builder proves what it can prove without a key -- that this source, on this
# builder, produces an image that inspects cleanly -- and the signature happens
# here, in an environment that holds the key and builds nothing.
#
# This script therefore never invokes rpi-image-gen. It verifies the build
# authority in front of it, runs the production release gate, signs the release
# attestation, verifies that signature against the trusted keyring, and
# assembles the hardware validation kit from that authority.
#
# Three artefacts arrive here and each one validates on its own, which is not
# the same as their describing one release. So before anything is signed:
#
#   the source bundle's project revision and tree hash must be the build's
#   the package must be the exact .deb whose digest the build recorded
#   the builder must have booted a base image release policy approves
#
# A build from commit A alongside a valid source bundle from commit B, a
# package digest nobody ever compared against a package, and a structurally
# perfect authority from an unapproved builder were all reachable before.
#
# The source bundle is compared against the tracked tree here, object by object,
# rather than trusted through its own authority document: a hash recorded in a
# JSON file beside an archive says only that someone hashed that archive once.
# The parity report that comparison produces is bound into the attestation, so a
# kit can tell later that the comparison happened at all.
#
# The private key is used through gpg's own keyring and is never read, copied,
# printed or written into any artefact, report or kit. The signature it makes is
# verified against --trusted-fingerprint, not merely against the keyring: a
# keyring can hold more keys than a release may be signed with.
#
# Exit status: 0 a signed release passed every production gate, 1 something did
# not, 2 the command line is wrong, 3 there is nothing to finalize.
set -eu

ROOT=$(cd "$(dirname "$0")/.." && pwd)
DIST="$ROOT/dist"
KIT="$ROOT/dist/hardware-validation"
PROFILES=""
SIGN_KEY=${EMS_APPLIANCE_OS_SIGN_KEY:-}
KEYRING=${EMS_APPLIANCE_OS_KEYRING:-}
FINGERPRINTS=${EMS_APPLIANCE_OS_TRUSTED_FINGERPRINTS:-}
SOURCE_BUNDLE=""
SOURCE_AUTHORITY=""
RUNTIME_GATES=""
PACKAGE=""
BUILDER_LOCK="$ROOT/packaging/appliance/vm/base-images.lock.json"
BUILD_KIT=yes
# The release index is only built when a base url says where these assets will
# be published. Without one there is nowhere for its urls to point, and an index
# of unreachable urls is worse than none: the appliance would refuse each entry
# one at a time instead of reporting that no index is configured.

usage() { sed -n '3,40p' "$0"; }

fail() {
    echo "appliance-finalize-rpi-release: $1" >&2
    echo "RESULT: FAIL ($2)" >&2
    exit 1
}

not_run() {
    echo "appliance-finalize-rpi-release: $1" >&2
    echo "RESULT: NOT RUN ($2)" >&2
    exit 3
}

while [ $# -gt 0 ]; do
    case "$1" in
        --sign-key) SIGN_KEY=${2:?--sign-key needs a key id}; shift 2 ;;
        --sign-key=*) SIGN_KEY=${1#*=}; shift ;;
        --keyring) KEYRING=${2:?--keyring needs a file}; shift 2 ;;
        --keyring=*) KEYRING=${1#*=}; shift ;;
        --trusted-fingerprint)
            FINGERPRINTS="$FINGERPRINTS ${2:?--trusted-fingerprint needs a fingerprint}"
            shift 2 ;;
        --trusted-fingerprint=*) FINGERPRINTS="$FINGERPRINTS ${1#*=}"; shift ;;
        --dist) DIST=${2:?--dist needs a directory}; shift 2 ;;
        --dist=*) DIST=${1#*=}; shift ;;
        --profile) PROFILES="$PROFILES ${2:?--profile needs rpi3, rpi4 or rpi5}"; shift 2 ;;
        --profile=*) PROFILES="$PROFILES ${1#*=}"; shift ;;
        --source-bundle) SOURCE_BUNDLE=${2:?--source-bundle needs a file}; shift 2 ;;
        --source-bundle=*) SOURCE_BUNDLE=${1#*=}; shift ;;
        --source-authority) SOURCE_AUTHORITY=${2:?--source-authority needs a file}; shift 2 ;;
        --source-authority=*) SOURCE_AUTHORITY=${1#*=}; shift ;;
        --runtime-gates) RUNTIME_GATES=${2:?--runtime-gates needs a file}; shift 2 ;;
        --runtime-gates=*) RUNTIME_GATES=${1#*=}; shift ;;
        --package) PACKAGE=${2:?--package needs a .deb}; shift 2 ;;
        --package=*) PACKAGE=${1#*=}; shift ;;
        --builder-lock) BUILDER_LOCK=${2:?--builder-lock needs a file}; shift 2 ;;
        --builder-lock=*) BUILDER_LOCK=${1#*=}; shift ;;
        --kit) KIT=${2:?--kit needs a directory}; shift 2 ;;
        --no-kit) BUILD_KIT=no; shift ;;
        -h|--help) usage; exit 0 ;;
        *) echo "unknown argument: $1" >&2; usage >&2; exit 2 ;;
    esac
done

[ -n "$SIGN_KEY" ] || { echo "--sign-key is required to finalize a release" >&2; exit 2; }
[ -n "$KEYRING" ] || { echo "--keyring is required to verify what was signed" >&2; exit 2; }
[ -f "$KEYRING" ] || fail "the keyring $KEYRING does not exist" keyring_missing
# A keyring names the keys a verifier can check against; a trust policy names
# the ones this release may be signed with. Without the second, any key in the
# keyring would be accepted, which is not a production release.
[ -n "$FINGERPRINTS" ] \
    || { echo "--trusted-fingerprint is required to finalize a release" >&2; exit 2; }
[ -d "$DIST" ] || not_run "no build output at $DIST" dist_unavailable
command -v gpg >/dev/null 2>&1 || not_run "gpg is not installed" required_tool_missing

# A release is what these three agree on. None of them is optional here: a
# finalization that skipped one would sign a set nobody proved was one set.
[ -n "$SOURCE_BUNDLE" ] \
    || { echo "--source-bundle is required to finalize a release" >&2; exit 2; }
[ -f "$SOURCE_BUNDLE" ] || fail "the source bundle $SOURCE_BUNDLE does not exist" \
    source_bundle_missing
[ -n "$PACKAGE" ] || { echo "--package is required to finalize a release" >&2; exit 2; }
[ -f "$PACKAGE" ] || fail "the package $PACKAGE does not exist" package_missing
[ -f "$BUILDER_LOCK" ] || fail "the builder lock $BUILDER_LOCK does not exist" \
    builder_lock_missing
if [ -z "$SOURCE_AUTHORITY" ]; then
    SOURCE_AUTHORITY="${SOURCE_BUNDLE%.tar.gz}"
    SOURCE_AUTHORITY="${SOURCE_AUTHORITY%.tar}.source-authority.json"
fi
[ -f "$SOURCE_AUTHORITY" ] \
    || fail "the source bundle carries no authority at $SOURCE_AUTHORITY" \
            source_authority_missing

# Read off the images being finalized rather than derived. This runs against an
# extracted source bundle, where git resolves nothing and a development version
# would name files that do not exist -- and it finalizes images that were built
# already, so their own names are the one answer that cannot disagree with them.
if [ -z "${VERSION:-}" ]; then
    FIRST_PROFILE=$(printf '%s' "$PROFILES" | tr ' ' '\n' | grep -v '^$' | head -1)
    VERSION=$(ls "$DIST"/ems-solarflow-appliance-*-"$FIRST_PROFILE"-arm64.img.xz 2>/dev/null \
        | head -1 \
        | sed -n "s#.*/ems-solarflow-appliance-\(.*\)-$FIRST_PROFILE-arm64\.img\.xz\$#\1#p")
fi
[ -n "$VERSION" ] || fail "no image in $DIST names a version to finalize" version_unreadable

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

echo "== the builds this release would be cut from =="
# Before anything is signed: one completed authority per profile, its builder
# environment recorded, the artefacts hashing to what that build produced, and
# the source, package and builder all naming the same release.
EMS_SOURCE_BUNDLE="$SOURCE_BUNDLE" EMS_SOURCE_AUTHORITY="$SOURCE_AUTHORITY" \
EMS_PACKAGE="$PACKAGE" EMS_BUILDER_LOCK="$BUILDER_LOCK" EMS_VERSION="$VERSION" \
PYTHONPATH="$ROOT" python3 - "$DIST" $PROFILES <<'PY' || fail "the build authority does not describe these artefacts" build_authority_rejected
import os
import sys
from pathlib import Path

from appliance import build_authority, release_inputs

dist = Path(sys.argv[1])
profiles = sys.argv[2:]
problems = []

try:
    source = release_inputs.read_source_bundle_authority(os.environ["EMS_SOURCE_AUTHORITY"])
except release_inputs.ReleaseInputError as error:
    sys.exit(f"source authority: {error.code}: {error.message}")
problems.extend(
    f"source bundle: {problem}"
    for problem in release_inputs.verify_source_bundle(source, os.environ["EMS_SOURCE_BUNDLE"])
)
print(
    f"source: project {source.revision[:12]} tree {source.tree_sha256[7:19]} "
    f"{source.tracked_objects} objects {source.symlinks} symlinks"
)

for profile in profiles:
    matches = sorted(dist.glob(f"*-{profile}-arm64.build-authority.json"))
    if len(matches) != 1:
        problems.append(f"{profile}: {len(matches)} build authorities in {dist}")
        continue
    try:
        authority = build_authority.read(matches[0])
    except build_authority.BuildAuthorityError as error:
        problems.append(f"{profile}: {error.code}: {error.message}")
        continue
    prefix = matches[0].name[: -len(".build-authority.json")]
    image = dist / f"{prefix}.img"
    if not image.is_file():
        problems.append(f"{profile}: {image.name} is missing")
        continue
    problems.extend(
        f"{profile}: {problem}"
        for problem in build_authority.verify_image(
            authority, image, profile=profile, require_environment=True
        )
    )
    # The three bindings a valid-on-its-own artefact does not give you.
    problems.extend(
        f"{profile}: {problem}"
        for problem in release_inputs.verify_source_matches_build(authority, source)
    )
    problems.extend(
        f"{profile}: {problem}"
        for problem in release_inputs.verify_package(
            authority,
            os.environ["EMS_PACKAGE"],
            name="ems-appliance-manager",
            version=os.environ["EMS_VERSION"],
            architecture="arm64",
        )
    )
    problems.extend(
        f"{profile}: {problem}"
        for problem in release_inputs.verify_builder_environment(
            authority, lock=os.environ["EMS_BUILDER_LOCK"]
        )
    )
    print(
        f"{profile}: build {authority.build_id} project {authority.project.revision[:12]} "
        f"builder {authority.builder_environment_sha256[7:19]}"
    )

if problems:
    sys.exit("; ".join(problems))
PY

echo
echo "== the bundle really is the tracked tree =="
# Not the authority document: that records a hash somebody computed once. This
# opens the archive and compares every tracked object — content, mode, symlink
# target — against git, and the report it writes is bound into the attestation.
mkdir -p "$DIST/reports"
PARITY="$DIST/reports/source-bundle-parity.json"
sh "$ROOT/scripts/appliance-check-source-bundle.sh" --json "$SOURCE_BUNDLE" >"$PARITY" \
    || fail "the source bundle is not the tracked tree (see $PARITY)" source_bundle_rejected
PYTHONPATH="$ROOT" python3 - "$PARITY" <<'PY' || fail "the source bundle parity report proves nothing" source_bundle_rejected
import json
import sys

report = json.loads(open(sys.argv[1], encoding="utf-8").read())
if not report.get("ok") or not report.get("compared"):
    sys.exit("the parity report is not a clean comparison of a non-empty tree")
print(f"bundle: {report['compared']} tracked objects, {report['symlinks']} symlinks, 0 findings")
PY

GATE_ARGS=""
for profile in $PROFILES; do GATE_ARGS="$GATE_ARGS --profile $profile"; done
for fingerprint in $FINGERPRINTS; do
    GATE_ARGS="$GATE_ARGS --trusted-fingerprint $fingerprint"
done
[ -n "$SOURCE_BUNDLE" ] && GATE_ARGS="$GATE_ARGS --source-bundle $SOURCE_BUNDLE"

echo
echo "== the production gates =="
REPORT="$DIST/release-gate-report.txt"
set +e
# shellcheck disable=SC2086
sh "$ROOT/scripts/appliance-release-gates.sh" --mode production --output "$DIST" \
    $GATE_ARGS >"$REPORT" 2>&1
gate_status=$?
set -e
cat "$REPORT"
# The verdict, not only the exit status: the report is what the kit carries and
# what a reviewer reads, so the two have to agree before anything is assembled.
if [ "$gate_status" -ne 0 ] || ! grep -q "^RESULT: PASS (production release" "$REPORT"; then
    echo
    echo "report: $REPORT"
    fail "the production release gate did not pass" release_gate_failed
fi

echo
echo "== the attestation the hardware process verifies against =="
# What the release is, as digests. A hardware kit that trusted a report file
# containing the words RESULT: PASS could not tell a stale report, a report for
# another profile and the real one apart.
ATTESTATION="$DIST/release-attestation.json"
EMS_SOURCE_AUTHORITY="$SOURCE_AUTHORITY" EMS_PACKAGE="$PACKAGE" \
EMS_SOURCE_BUNDLE="$SOURCE_BUNDLE" EMS_SOURCE_PARITY="$PARITY" \
EMS_RUNTIME_GATES="$RUNTIME_GATES" \
EMS_GATE_REPORT="$REPORT" EMS_VERSION="$VERSION" EMS_ATTESTATION="$ATTESTATION" \
PYTHONPATH="$ROOT" python3 - "$DIST" $PROFILES <<'ATTEST' \
    || fail "the release attestation could not be recorded" release_attestation_failed
import os
import sys
from pathlib import Path

from appliance import (
    build_authority,
    media_sizing,
    release_attestation,
    release_inputs,
    release_trust,
)

dist = Path(sys.argv[1])
profiles = sys.argv[2:]
version = os.environ["EMS_VERSION"]
gate_report = os.environ["EMS_GATE_REPORT"]

source = release_inputs.read_source_bundle_authority(os.environ["EMS_SOURCE_AUTHORITY"])
package = release_inputs.read_package(os.environ["EMS_PACKAGE"])
runtime_gate_evidence = os.environ.get("EMS_RUNTIME_GATES") or ""

entries, environment = [], None
for profile in profiles:
    prefix = f"ems-solarflow-appliance-{version}-{profile}-arm64"
    authority = build_authority.read(dist / f"{prefix}.build-authority.json")
    environment = authority.environment
    entries.append(
        release_attestation.describe_profile(
            profile,
            dist=dist,
            prefix=prefix,
            reports=dist / "reports",
            build_id=authority.build_id,
            gate_report=gate_report,
        )
    )

attestation = release_attestation.build(
    project={"revision": source.revision, "tree_sha256": source.tree_sha256},
    # Three digests, because a bundle, the document describing it and the
    # comparison that proved it is the tracked tree are three separate claims.
    source={
        "bundle_sha256": release_trust.file_sha256(os.environ["EMS_SOURCE_BUNDLE"]),
        "authority_sha256": release_trust.file_sha256(os.environ["EMS_SOURCE_AUTHORITY"]),
        "parity_sha256": release_trust.file_sha256(os.environ["EMS_SOURCE_PARITY"]),
        "tracked_objects": source.tracked_objects,
        "symlinks": source.symlinks,
    },
    package=package.to_dict(),
    builder={
        "base_image_lock_id": environment.base_image_lock_id,
        "base_image_sha512": environment.base_image_sha512,
        "environment_sha256": environment.canonical_hash,
    },
    profiles=entries,
    runtime_gates=(
        {"sha256": release_trust.file_sha256(runtime_gate_evidence)}
        if runtime_gate_evidence and Path(runtime_gate_evidence).is_file()
        else {}
    ),
    release_gate={"sha256": release_trust.file_sha256(gate_report)},
    minimum_media_bytes=media_sizing.requirements()["minimum_media_bytes"],
)
release_attestation.write(os.environ["EMS_ATTESTATION"], attestation)
if attestation.result != release_attestation.PASS:
    missing = [entry.profile for entry in entries if entry.result != release_attestation.PASS]
    sys.exit("the attestation is incomplete for: " + ", ".join(missing))
print(f"attestation: {attestation.canonical_hash}")
for entry in entries:
    print(
        f"  {entry.profile}: build {entry.build_id} {len(entry.artefacts)} artefacts "
        f"{len(entry.reports)} reports"
    )
ATTEST

# Signed with the same key as a manifest, and then verified the way a kit will
# verify it: against the keyring *and* the trusted fingerprint. "gpg said good"
# does not say which key it was good for.
gpg --batch --yes --local-user "$SIGN_KEY" --armor --detach-sign \
    --output "$ATTESTATION.asc" "$ATTESTATION" \
    || fail "the release attestation could not be signed" release_attestation_unsigned
# shellcheck disable=SC2086
EMS_ATTESTATION="$ATTESTATION" EMS_KEYRING="$KEYRING" \
PYTHONPATH="$ROOT" python3 - $FINGERPRINTS <<'PY' \
    || fail "the release attestation signature is not trusted" release_attestation_untrusted
import os
import sys

from appliance import release_trust

policy = release_trust.TrustPolicy.of(os.environ["EMS_KEYRING"], sys.argv[1:])
verdict = release_trust.verify_signature(os.environ["EMS_ATTESTATION"], policy)
if not verdict.ok:
    sys.exit(f"{verdict.code}: {verdict.detail}")
print(f"signer: {verdict.fingerprints[0]}")
PY
echo "signed:      $ATTESTATION.asc"

TRUST_ARGS="--keyring $KEYRING"
for fingerprint in $FINGERPRINTS; do
    TRUST_ARGS="$TRUST_ARGS --trusted-fingerprint $fingerprint"
done
EVIDENCE_ARGS="--source-authority $SOURCE_AUTHORITY --source-bundle $SOURCE_BUNDLE"
EVIDENCE_ARGS="$EVIDENCE_ARGS --source-parity $PARITY"
[ -n "$RUNTIME_GATES" ] && EVIDENCE_ARGS="$EVIDENCE_ARGS --runtime-gates $RUNTIME_GATES"

# A kit directory outlives the run that filled it, so a run that built no kit
# hands over no manifest: an earlier run's readiness is not this release's.
KIT_MANIFEST_ARG=""
if [ "$BUILD_KIT" = yes ]; then
    KIT_MANIFEST_ARG="--kit-manifest $KIT/kit-manifest.json"
    echo
    echo "== the kit an operator carries to the hardware =="
    KIT_ARGS=""
    for profile in $PROFILES; do KIT_ARGS="$KIT_ARGS --profile $profile"; done
    # shellcheck disable=SC2086
    sh "$ROOT/scripts/appliance-hardware-validation-kit.sh" --dist "$DIST" --output "$KIT" \
        --gate-report "$REPORT" --attestation "$ATTESTATION" $KIT_ARGS \
        $TRUST_ARGS $EVIDENCE_ARGS \
        || fail "the hardware validation kit is not authoritative" hardware_kit_incomplete

    echo
    echo "== the kit, verified again from the directory rather than from this run =="
    # shellcheck disable=SC2086
    python3 "$ROOT/scripts/appliance_verify_hardware_kit.py" --kit "$KIT" $TRUST_ARGS \
        --project-root "$ROOT" --json "$DIST/reports/kit-verification.json" \
        || fail "the assembled kit does not verify itself" hardware_kit_unverified
fi

echo
echo "== the summary a reviewer reads =="
RESULT_PROFILE_ARGS=""
for profile in $PROFILES; do RESULT_PROFILE_ARGS="$RESULT_PROFILE_ARGS --profile $profile"; done
# Generated from the reports, never copied out of them: a hand-written summary
# claiming "79 pass, 0 not run" once sat beside a report recording 79 pass,
# 2 fail and 1 not run.
# shellcheck disable=SC2086
PYTHONPATH="$ROOT" python3 "$ROOT/scripts/appliance_release_result.py" \
    --dist "$DIST" --output "$DIST/release-result.json" \
    --markdown "$DIST/release-result.md" \
    --attestation "$ATTESTATION" --gate-report "$REPORT" \
    --package "$PACKAGE" --project-root "$ROOT" --builder-lock "$BUILDER_LOCK" \
    $KIT_MANIFEST_ARG \
    $TRUST_ARGS $EVIDENCE_ARGS $RESULT_PROFILE_ARGS \
    || fail "the release result does not add up to a ready release" release_result_incomplete

echo
echo "release gate report: $REPORT"
echo "release attestation: $ATTESTATION"
echo "release result:      $DIST/release-result.json"
[ "$BUILD_KIT" = yes ] && echo "hardware kit:        $KIT"
echo "The signing key was used through gpg and never copied into any artefact."
echo "Nothing was published, tagged or uploaded."
echo "RESULT: PASS (signed production release)"
