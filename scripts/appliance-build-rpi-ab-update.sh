#!/bin/sh
# SPDX-License-Identifier: AGPL-3.0-or-later
# Wrap rpi-image-gen's A/B update artifact in this project's release metadata.
#
#   scripts/appliance-build-rpi-ab-update.sh --profile rpi4|rpi5 [--output DIR]
#          [--update FILE] [--build-authority FILE] [--build-id ID]
#          [--sign-key KEYID]
#
# The hardware the manifest declares comes from the build profile, so an
# artefact can only claim the board its device layer was built for.
#
# The archive itself is upstream's: image-rota's post-image.sh produces
# update.tar.zst holding exactly two android-sparse members, boot and system.
# Nothing is repacked here — repacking would mean this project, not the
# generator, decided what an update is. What is added is the signed manifest
# binding that exact archive to a layout, a hardware set and a minimum
# Appliance Manager version.
#
# Provenance is not assumed. A production release names the rpi-image-gen
# revision that built it, so it requires the build authority a completed builder
# run wrote: the update's SHA-256, the profile, the generator revision and the
# source tree hash all have to match before anything is signed. An artefact
# supplied with --update and no build authority remains supported as a
# development artefact — it is described, marked provenance_verified=false, and
# refused for signing.
#
# The manifest never contains private signing material. This script signs only
# when --sign-key names a key that is already in the caller's keyring; it never
# generates, imports or embeds one.
#
# Exit status: 0 built, 1 the build failed, 2 the command line is wrong, 3 the
# host cannot build. A host that cannot build reports NOT RUN, never a pass.
set -eu

ROOT=$(cd "$(dirname "$0")/.." && pwd)
OUTPUT="$ROOT/dist"
PROFILE=rpi5
UPDATE=""
BUILD_AUTHORITY=""
BUILD_ID=""
SIGN_KEY=${EMS_APPLIANCE_OS_SIGN_KEY:-}
REQUIRED_TOOLS="python3 tar zstd sha256sum"

usage() {
    sed -n '3,33p' "$0"
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
        --profile) PROFILE=${2:?--profile needs rpi4 or rpi5}; shift 2 ;;
        --profile=*) PROFILE=${1#*=}; shift ;;
        --output) OUTPUT=${2:?--output needs a directory}; shift 2 ;;
        --output=*) OUTPUT=${1#*=}; shift ;;
        --update) UPDATE=${2:?--update needs a file}; shift 2 ;;
        --update=*) UPDATE=${1#*=}; shift ;;
        --build-authority) BUILD_AUTHORITY=${2:?--build-authority needs a file}; shift 2 ;;
        --build-authority=*) BUILD_AUTHORITY=${1#*=}; shift ;;
        --build-id) BUILD_ID=${2:?--build-id needs a value}; shift 2 ;;
        --build-id=*) BUILD_ID=${1#*=}; shift ;;
        --sign-key) SIGN_KEY=${2:?--sign-key needs a key id}; shift 2 ;;
        --sign-key=*) SIGN_KEY=${1#*=}; shift ;;
        -h|--help) usage; exit 0 ;;
        *) echo "unknown argument: $1" >&2; usage >&2; exit 2 ;;
    esac
done

missing=""
for tool in $REQUIRED_TOOLS; do
    command -v "$tool" >/dev/null 2>&1 || missing="$missing $tool"
done
[ -z "$missing" ] || not_run "not installed:$missing" required_tool_missing

VERSION=$(sed -n 's/^APPLIANCE_VERSION = "\(.*\)"$/\1/p' "$ROOT/appliance/version.py")
[ -n "$VERSION" ] || fail "cannot read APPLIANCE_VERSION" version_unreadable
[ -n "$BUILD_ID" ] || BUILD_ID=$(date -u +%Y%m%d%H%M%S 2>/dev/null || echo unknown)
PROFILE_CONFIG="$ROOT/packaging/appliance/image/profiles/${PROFILE}-ab.yaml"
[ -f "$PROFILE_CONFIG" ] || fail "there is no build profile for $PROFILE" hardware_profile_unknown
NAME="ems-solarflow-appliance-${VERSION}-${PROFILE}-arm64-ab"

if [ -z "$UPDATE" ]; then
    for candidate in "$OUTPUT/$NAME.update.tar.zst" "$OUTPUT/update.tar.zst"; do
        [ -f "$candidate" ] && UPDATE=$candidate && break
    done
fi
[ -n "$UPDATE" ] && [ -f "$UPDATE" ] \
    || not_run "no rpi-image-gen update artifact; build an image first or pass --update" \
               update_artifact_unavailable

if [ -z "$BUILD_AUTHORITY" ]; then
    for candidate in "$(dirname "$UPDATE")/build-authority.json" \
                     "$OUTPUT/build-authority.json"; do
        [ -f "$candidate" ] && BUILD_AUTHORITY=$candidate && break
    done
fi

# Verified against the artefact before anything is described, so an update
# edited after its build cannot be signed with that build's provenance.
if [ -n "$BUILD_AUTHORITY" ]; then
    [ -f "$BUILD_AUTHORITY" ] || fail "$BUILD_AUTHORITY is not a file" build_authority_unreadable
    set +e
    PROVENANCE=$(PYTHONPATH="$ROOT" python3 - "$BUILD_AUTHORITY" "$UPDATE" "$PROFILE" \
        "$BUILD_ID" <<'PY'
import sys

from appliance import build_authority, rpi_image_gen

path, update, profile, build_id = sys.argv[1:5]
lock = rpi_image_gen.read_lock()
try:
    authority = build_authority.read(path)
except build_authority.BuildAuthorityError as exc:
    sys.exit(f"{exc.code}: {exc.message}")
problems = build_authority.verify_update(
    authority, update, profile=profile, revision=lock.commit, build_id=build_id
)
if problems:
    sys.exit("; ".join(problems))
print(f"REVISION={authority.builder.revision}")
print(f"SOURCE_FORM={authority.builder.source_form}")
print(f"SOURCE_TREE={authority.builder.source_tree_sha256}")
print(f"PROJECT_REVISION={authority.project.revision}")
print(f"PROJECT_TREE={authority.project.tree_sha256}")
print(f"AUTHORITY_SHA256={authority.canonical_hash}")
PY
    )
    verified=$?
    set -e
    [ $verified -eq 0 ] || fail "the build authority does not describe this artifact: $PROVENANCE" \
                                build_authority_rejected
    REVISION=$(echo "$PROVENANCE" | sed -n 's/^REVISION=//p')
    SOURCE_FORM=$(echo "$PROVENANCE" | sed -n 's/^SOURCE_FORM=//p')
    SOURCE_TREE=$(echo "$PROVENANCE" | sed -n 's/^SOURCE_TREE=//p')
    # From the verified authority, never from this host's git: asking git again
    # would answer for whatever tree happens to be checked out now.
    PROJECT_REVISION=$(echo "$PROVENANCE" | sed -n 's/^PROJECT_REVISION=//p')
    PROJECT_TREE=$(echo "$PROVENANCE" | sed -n 's/^PROJECT_TREE=//p')
    AUTHORITY_SHA256=$(echo "$PROVENANCE" | sed -n 's/^AUTHORITY_SHA256=//p')
    PROVENANCE_VERIFIED=true
else
    # Development only. The manifest must not claim a builder nobody ran.
    REVISION=unverified
    SOURCE_FORM=unverified
    SOURCE_TREE=""
    PROJECT_REVISION=unverified
    PROJECT_TREE=""
    AUTHORITY_SHA256=""
    PROVENANCE_VERIFIED=false
    echo "appliance-build-rpi-ab-update: no build authority; this is a development" \
         "artifact and cannot be signed. Build with" \
         "scripts/appliance-build-rpi-ab-image.sh to produce one." >&2
fi

if [ "$PROVENANCE_VERIFIED" != true ] && [ -n "$SIGN_KEY" ]; then
    fail "a development artifact never receives production release provenance" \
         provenance_unverified
fi

mkdir -p "$OUTPUT" || fail "cannot create $OUTPUT" output_unusable
ARCHIVE="$OUTPUT/$NAME.tar.zst"
[ "$UPDATE" = "$ARCHIVE" ] || cp "$UPDATE" "$ARCHIVE"

echo "== describing the upstream artifact =="
EMS_PROVENANCE_VERIFIED=$PROVENANCE_VERIFIED \
EMS_RPI_IMAGE_GEN_REVISION=$REVISION \
EMS_BUILD_SOURCE_FORM=$SOURCE_FORM \
EMS_BUILD_SOURCE_TREE=$SOURCE_TREE \
EMS_PROJECT_REVISION=$PROJECT_REVISION \
EMS_PROJECT_TREE=$PROJECT_TREE \
EMS_BUILD_AUTHORITY_SHA256=$AUTHORITY_SHA256 \
PYTHONPATH="$ROOT" python3 - "$ARCHIVE" "$OUTPUT/$NAME.manifest.json" \
    "$VERSION" "$BUILD_ID" "$NAME" "$PROFILE_CONFIG" <<'PY' || fail "the artifact could not be described" describe_failed
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile

from appliance import os_artifacts, os_releases, rpi_image_gen, sparse, version

archive, manifest_path, release_version, build_id, name, profile_config = sys.argv[1:7]
lock = rpi_image_gen.read_lock()
profile = rpi_image_gen.read_profile(profile_config)

members = {}
staging = tempfile.mkdtemp(prefix="ems-ab-describe-")
# The same reader the appliance uses, so an archive the runtime cannot open
# cannot be described here as if it were fine. Each member is written out
# once so its container can be read a second time: the encoded digest is
# what the archive carries, the expanded digest is what a partition will
# hold, and a manifest that conflated them is how a sparse container ends up
# on a slot.
try:
    with os_artifacts.open_archive(archive) as handle:
        while True:
            member = handle.next()
            if member is None:
                break
            if not member.isfile():
                sys.exit(f"{member.name} is not a regular file")
            staged = os.path.join(staging, os.path.basename(member.name))
            stream = handle.extractfile(member)
            digest = hashlib.sha256()
            with open(staged, "wb") as target:
                while True:
                    block = stream.read(1024 * 1024)
                    if not block:
                        break
                    digest.update(block)
                    target.write(block)
            entry = {
                "encoded_sha256": f"sha256:{digest.hexdigest()}",
                "encoding": sparse.ENCODING_RAW,
            }
            if sparse.is_sparse(staged):
                summary = sparse.summarize(staged)
                entry.update(
                    {
                        "encoding": sparse.ENCODING_ANDROID_SPARSE,
                        "expanded_sha256": summary.digest,
                        "expanded_size": summary.bytes_written,
                    }
                )
            else:
                entry.update(
                    {
                        "expanded_sha256": entry["encoded_sha256"],
                        "expanded_size": os.path.getsize(staged),
                    }
                )
            members[member.name] = entry
finally:
    shutil.rmtree(staging, ignore_errors=True)

if set(members) != set(lock.update_members):
    sys.exit(
        f"the archive holds {sorted(members)}, image-rota produces {sorted(lock.update_members)}"
    )

archive_digest = hashlib.sha256()
with open(archive, "rb") as handle:
    for block in iter(lambda: handle.read(1024 * 1024), b""):
        archive_digest.update(block)

verified = os.environ.get("EMS_PROVENANCE_VERIFIED") == "true"
revision = os.environ.get("EMS_RPI_IMAGE_GEN_REVISION") or "unverified"
created = subprocess.run(
    ["date", "-u", "+%Y-%m-%dT%H:%M:%SZ"], capture_output=True, text=True
).stdout.strip() or "unknown"
project = os.environ.get("EMS_PROJECT_REVISION") or "unverified"

payload = {
    "format_version": os_releases.MANIFEST_FORMAT_VERSION,
    "release_version": release_version,
    "build_id": build_id,
    "created_at": created,
    "architecture": "arm64",
    "device_layer": profile.device_layer,
    "compatible_hardware": list(profile.compatible_board_classes),
    "os_release": "Raspberry Pi OS Trixie arm64",
    "image_layer": lock.image_layer,
    "image_layer_version": lock.image_layer_version,
    "rpi_image_gen_revision": revision,
    # Where the revision above comes from. An artefact nobody built with the
    # pinned generator says so here rather than inheriting the lock's answer.
    "provenance": {
        "verified": verified,
        "source_form": os.environ.get("EMS_BUILD_SOURCE_FORM") or "unverified",
        "source_tree_sha256": os.environ.get("EMS_BUILD_SOURCE_TREE") or "",
        "project_tree_sha256": os.environ.get("EMS_PROJECT_TREE") or "",
        "build_authority_sha256": os.environ.get("EMS_BUILD_AUTHORITY_SHA256") or "",
    },
    "project_revision": project,
    "appliance_manager_version": version.APPLIANCE_VERSION,
    "minimum_appliance_manager_version": version.APPLIANCE_VERSION,
    "layout_id": "ems-appliance-rota-v1",
    "slot_schema_version": 2,
    "persistent_schema_version": 2,
    "archive": {
        "name": f"{name}.tar.zst",
        "digest": f"sha256:{archive_digest.hexdigest()}",
        "size_bytes": os.path.getsize(archive),
        "compression": "zstd",
    },
    "members": {
        "boot": dict(members["boot"], role="boot", filesystem="vfat"),
        "system": dict(members["system"], role="root", filesystem="ext4"),
    },
}
# Parsing it back is the check: an artifact the runtime would refuse must not
# leave the build host looking like a release.
os_releases.parse_manifest(payload)
with open(manifest_path, "w", encoding="utf-8") as handle:
    handle.write(json.dumps(payload, indent=2, sort_keys=True) + "\n")
for name in sorted(members):
    entry = members[name]
    print(f"{name}: {entry['encoding']} {entry['encoded_sha256'][:19]}... "
          f"-> {entry['expanded_size']} bytes {entry['expanded_sha256'][:19]}...")
PY

( cd "$OUTPUT" && sha256sum "$NAME.tar.zst" "$NAME.manifest.json" > "$NAME.sha256" )

if [ -n "$SIGN_KEY" ]; then
    [ "$PROVENANCE_VERIFIED" = true ] \
        || fail "a development artifact is never signed" provenance_unverified
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
echo "artifact: $ARCHIVE"
echo "manifest: $OUTPUT/$NAME.manifest.json"
echo "checksum: $OUTPUT/$NAME.sha256"
echo
if [ "$PROVENANCE_VERIFIED" = true ]; then
    echo "provenance: verified against $BUILD_AUTHORITY"
else
    echo "provenance: development (provenance_verified=false, signing_forbidden=true)"
fi
echo "Nothing was published, tagged or uploaded."
echo "RESULT: PASS (described $NAME.tar.zst)"
