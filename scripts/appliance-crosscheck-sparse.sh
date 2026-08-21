#!/bin/sh
# SPDX-License-Identifier: AGPL-3.0-or-later
# Expand a real update artefact twice, with two unrelated decoders, and compare.
#
#   scripts/appliance-crosscheck-sparse.sh <update.tar.zst> [--image IMAGE]
#                                          [--platform linux/amd64] [--report FILE]
#
# The appliance expands Android Sparse containers with its own in-process
# decoder, because writing a slot from a subprocess it cannot account for is
# not something a fail-safe update may do. That decoder is therefore the one
# thing in the update path with no second opinion: if it agreed with itself
# about a corrupt chunk, every test would still pass.
#
# So each member is expanded by appliance/sparse.py and by simg2img, and the
# expanded size and SHA-256 are compared. simg2img is a test oracle only — it
# runs in a throwaway container so the developer host stays untouched, and the
# runtime keeps using its own decoder.
#
# The members are staged through the production allowlist extractor, never with
# a raw tar. An update archive is untrusted input, and a verifier that unpacks
# it with `tar -x` before the safe parser sees it is exactly as exposed to a
# traversal, an absolute path, a symlink or an unexpected member as the code it
# was written to check.
#
# Any disagreement is FAIL. Read-only with respect to the artefact.
#
# --report writes the comparison as JSON, so a release attestation can bind
# what the two decoders actually said rather than that a log said PASS.
#
# Exit status: 0 both decoders agree, 1 they do not, 2 the command line is
# wrong, 3 the cross-check could not run.
set -eu

ARCHIVE=""
IMAGE=debian:trixie-slim
PLATFORM=linux/amd64
REPORT=""

usage() { sed -n '3,20p' "$0"; }

not_run() {
    echo "appliance-crosscheck-sparse: $1" >&2
    echo "RESULT: NOT RUN ($2)" >&2
    exit 3
}

while [ $# -gt 0 ]; do
    case "$1" in
        --image) IMAGE=${2:?--image needs a container image}; shift 2 ;;
        --platform) PLATFORM=${2:?--platform needs a docker platform}; shift 2 ;;
        --report) REPORT=${2:?--report needs a file}; shift 2 ;;
        --report=*) REPORT=${1#*=}; shift ;;
        -h|--help) usage; exit 0 ;;
        -*) echo "unknown argument: $1" >&2; usage >&2; exit 2 ;;
        *) [ -z "$ARCHIVE" ] || { echo "one archive at a time" >&2; exit 2; }
           ARCHIVE=$1; shift ;;
    esac
done
[ -n "$ARCHIVE" ] || { usage >&2; exit 2; }
[ -f "$ARCHIVE" ] || not_run "no artefact at $ARCHIVE" artefact_unavailable

ROOT=$(cd "$(dirname "$0")/.." && pwd)
. "$ROOT/scripts/lib/workdir.sh"
# the crosscheck expands roughly 8.5G of sparse members.
CROSSCHECK_WORK_BYTES=$((12 * 1024 * 1024 * 1024))
command -v zstd >/dev/null 2>&1 || not_run "zstd is missing" zstd_unavailable
command -v docker >/dev/null 2>&1 || not_run "docker is missing, so there is no second decoder" \
    external_decoder_unavailable
docker info >/dev/null 2>&1 || not_run "the Docker daemon is not reachable" \
    external_decoder_unavailable

WORK=$(ems_work_dir ems-appliance-crosscheck "$CROSSCHECK_WORK_BYTES") || exit 1
trap 'rm -rf "$WORK"' EXIT

mkdir -p "$WORK/ours" "$WORK/theirs"
PYTHONPATH="$ROOT" python3 - "$ARCHIVE" "$WORK/members" <<'PY' || \
    not_run "the artefact could not be staged safely" artefact_unreadable
import sys

from appliance import os_artifacts, rpi_image_gen

archive, staging = sys.argv[1:3]
lock = rpi_image_gen.read_lock()
try:
    staged = os_artifacts.stage_members(archive, staging, lock.update_members)
except os_artifacts.ArtifactError as error:
    sys.exit(f"{error.code}: {error.message}")
print("\n".join(sorted(staged.members)))
PY

members=$(find "$WORK/members" -maxdepth 1 -type f -printf '%f\n' | sort)
[ -n "$members" ] || not_run "the artefact carries no members" artefact_empty
# The staging directory is 0700 and its members 0600; the oracle runs as a
# different uid inside the container and has to be able to read them.
chmod 0755 "$WORK/members" && chmod 0644 "$WORK/members"/*

echo "== this project's decoder =="
for member in $members; do
    PYTHONPATH="$ROOT" python3 - "$WORK/members/$member" "$WORK/ours/$member" <<'PY'
import hashlib
import sys
from pathlib import Path

from appliance import sparse

source, target = Path(sys.argv[1]), Path(sys.argv[2])
header = sparse.read_header(source)
sparse.expand(source, target)
digest = hashlib.sha256()
with target.open("rb") as handle:
    for block in iter(lambda: handle.read(1024 * 1024), b""):
        digest.update(block)
print(f"{source.name} declared={header.expanded_size} written={target.stat().st_size} "
      f"sha256={digest.hexdigest()}")
PY
done

echo
echo "== simg2img =="
# The oracle has to run, not merely be pulled: a cached image of another
# architecture starts and dies with "exec format error", which is a fact about
# this host and not about the decoder.
docker run --rm --platform "$PLATFORM" -e OWNER="$(id -u):$(id -g)" \
    -v "$WORK:/work" "$IMAGE" sh -c '
        set -e
        export DEBIAN_FRONTEND=noninteractive
        apt-get update -qq >/dev/null 2>&1
        apt-get install -y -qq --no-install-recommends android-sdk-libsparse-utils \
            >/dev/null 2>&1
        for member in $(ls /work/members); do
            simg2img "/work/members/$member" "/work/theirs/$member" >&2
            printf "%s %s %s\n" "$member" \
                "$(stat -c %s "/work/theirs/$member")" \
                "$(sha256sum "/work/theirs/$member" | cut -d" " -f1)"
            rm -f "/work/theirs/$member"
        done
        chown -R "$OWNER" /work/theirs' >"$WORK/external.txt" 2>"$WORK/external.err" || {
    tail -n 20 "$WORK/external.err" >&2
    not_run "the external decoder could not run" external_decoder_unavailable
}
cat "$WORK/external.txt"

echo
echo "== comparison =="
failures=0
COMPARISON="$WORK/comparison.txt"
: > "$COMPARISON"
for member in $members; do
    ours_size=$(stat -c %s "$WORK/ours/$member")
    ours_digest=$(sha256sum "$WORK/ours/$member" | cut -d' ' -f1)
    theirs_size=$(awk -v m="$member" '$1 == m {print $2}' "$WORK/external.txt")
    theirs_digest=$(awk -v m="$member" '$1 == m {print $3}' "$WORK/external.txt")
    if [ -z "$theirs_size" ]; then
        echo "  FAIL  $member: the external decoder produced nothing"
        printf '%s\tfail\t%s\t%s\t\t\n' "$member" "$ours_size" "$ours_digest" \
            >> "$COMPARISON"
        failures=$((failures + 1))
        continue
    fi
    if [ "$ours_size" = "$theirs_size" ] && [ "$ours_digest" = "$theirs_digest" ]; then
        echo "  PASS  $member: $ours_size bytes, sha256:$ours_digest"
        outcome=pass
    else
        echo "  FAIL  $member: ours $ours_size/$ours_digest, simg2img $theirs_size/$theirs_digest"
        outcome=fail
        failures=$((failures + 1))
    fi
    printf '%s\t%s\t%s\t%s\t%s\t%s\n' \
        "$member" "$outcome" "$ours_size" "$ours_digest" "$theirs_size" "$theirs_digest" \
        >> "$COMPARISON"
done

# The comparison as data, so a release attestation binds what the two decoders
# said rather than that a log contained the word PASS.
if [ -n "$REPORT" ]; then
    ARCHIVE="$ARCHIVE" FAILURES="$failures" python3 - "$COMPARISON" "$REPORT" <<'PY' \
        || not_run "the comparison could not be recorded" report_unwritable
import hashlib
import json
import os
import sys

comparison, report = sys.argv[1:3]
archive = os.environ["ARCHIVE"]
digest = hashlib.sha256()
with open(archive, "rb") as handle:
    for block in iter(lambda: handle.read(1024 * 1024), b""):
        digest.update(block)

members = []
with open(comparison, encoding="utf-8") as handle:
    for line in handle:
        if not line.strip():
            continue
        name, outcome, ours_size, ours_digest, theirs_size, theirs_digest = (
            line.rstrip("\n").split("\t")
        )
        members.append(
            {
                "member": name,
                "result": outcome,
                "project": {
                    "expanded_size": int(ours_size or 0),
                    "expanded_sha256": f"sha256:{ours_digest}" if ours_digest else "",
                },
                "external": {
                    "expanded_size": int(theirs_size or 0),
                    "expanded_sha256": f"sha256:{theirs_digest}" if theirs_digest else "",
                },
            }
        )

failures = int(os.environ["FAILURES"])
with open(report, "w", encoding="utf-8") as handle:
    handle.write(
        json.dumps(
            {
                "schema_version": 1,
                "artefact_sha256": f"sha256:{digest.hexdigest()}",
                "external_decoder": "simg2img",
                "members": members,
                "result": "fail" if failures or not members else "pass",
            },
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )
PY
    echo "report:   $REPORT"
fi

echo
if [ "$failures" -eq 0 ]; then
    echo "RESULT: PASS"
    exit 0
fi
echo "RESULT: FAIL ($failures)"
exit 1
