#!/bin/sh
# SPDX-License-Identifier: AGPL-3.0-or-later
# Fetch the pinned rpi-image-gen source tree. Source only — nothing is installed.
#
#   scripts/appliance-fetch-rpi-image-gen.sh [--into DIR] [--form git|tarball]
#
# Both supported source forms end in the same place: a tree whose identity can
# be proven later without trusting this script's output. A git clone is checked
# out at the pinned commit; a release tarball is verified against the SHA-256 in
# packaging/appliance/image/rpi-image-gen.lock *before* it is extracted, and the
# result is recorded in .rpi-image-gen-source.json beside the tree.
#
# No URL, revision or digest is accepted from the caller: all three come from
# the lock. No host package is installed. Nothing is built.
#
# Exit status: 0 fetched and verified, 1 verification failed, 2 the command line
# is wrong, 3 the host lacks a tool needed to fetch.
set -eu

ROOT=$(cd "$(dirname "$0")/.." && pwd)
LOCK="$ROOT/packaging/appliance/image/rpi-image-gen.lock"
INTO="$ROOT/../rpi-image-gen"
FORM=auto

usage() {
    sed -n '3,17p' "$0"
}

not_run() {
    echo "appliance-fetch-rpi-image-gen: $1" >&2
    echo "RESULT: NOT RUN ($2)" >&2
    exit 3
}

fail() {
    echo "appliance-fetch-rpi-image-gen: $1" >&2
    echo "RESULT: FAIL ($2)" >&2
    exit 1
}

while [ $# -gt 0 ]; do
    case "$1" in
        --into) INTO=${2:?--into needs a directory}; shift 2 ;;
        --into=*) INTO=${1#*=}; shift ;;
        --form) FORM=${2:?--form needs git or tarball}; shift 2 ;;
        --form=*) FORM=${1#*=}; shift ;;
        -h|--help) usage; exit 0 ;;
        *) echo "unknown argument: $1" >&2; usage >&2; exit 2 ;;
    esac
done

[ -f "$LOCK" ] || fail "$LOCK is missing" lock_missing
command -v python3 >/dev/null 2>&1 || not_run "python3 is not installed" required_tool_missing

PINNED=$(PYTHONPATH="$ROOT" python3 - "$LOCK" <<'PY'
import sys

from appliance import rpi_image_gen

lock = rpi_image_gen.read_lock(sys.argv[1])
for key, value in (
    ("REPOSITORY", lock.repository),
    ("RELEASE", lock.release),
    ("COMMIT", lock.commit),
    ("URL", lock.tarball["url"]),
    ("DIGEST", lock.tarball["sha256"]),
    ("TOP_LEVEL", lock.tarball["top_level_directory"]),
):
    print(f"{key}={value}")
PY
) || fail "the lock could not be read" lock_invalid

REPOSITORY=$(echo "$PINNED" | sed -n 's/^REPOSITORY=//p')
RELEASE=$(echo "$PINNED" | sed -n 's/^RELEASE=//p')
COMMIT=$(echo "$PINNED" | sed -n 's/^COMMIT=//p')
URL=$(echo "$PINNED" | sed -n 's/^URL=//p')
DIGEST=$(echo "$PINNED" | sed -n 's/^DIGEST=//p')
TOP_LEVEL=$(echo "$PINNED" | sed -n 's/^TOP_LEVEL=//p')

case "$FORM" in
    auto) command -v git >/dev/null 2>&1 && FORM=git || FORM=tarball ;;
    git|tarball) ;;
    *) echo "unknown source form: $FORM" >&2; exit 2 ;;
esac

[ -e "$INTO" ] && fail "$INTO already exists; remove it or pass --into" destination_exists

if [ "$FORM" = git ]; then
    command -v git >/dev/null 2>&1 || not_run "git is not installed" required_tool_missing
    echo "== cloning $REPOSITORY at $COMMIT =="
    git clone --quiet "$REPOSITORY" "$INTO" || fail "the clone failed" fetch_failed
    git -C "$INTO" checkout --quiet "$COMMIT" \
        || fail "$COMMIT is not in the clone" source_unverified
    HEAD=$(git -C "$INTO" rev-parse HEAD)
    [ "$HEAD" = "$COMMIT" ] || fail "the checkout is at $HEAD, not $COMMIT" source_unverified
    echo "RESULT: PASS (git $HEAD)"
    exit 0
fi

command -v curl >/dev/null 2>&1 || not_run "curl is not installed" required_tool_missing
command -v tar >/dev/null 2>&1 || not_run "tar is not installed" required_tool_missing

STAGE=$(mktemp -d) || fail "cannot create a staging directory" output_unusable
trap 'rm -rf "$STAGE"' EXIT
ARCHIVE="$STAGE/rpi-image-gen.tar.gz"

echo "== downloading $URL =="
curl -sSL --fail --proto '=https' --tlsv1.2 -o "$ARCHIVE" "$URL" \
    || fail "the download failed" fetch_failed

# Verified before it is opened. An archive that is extracted first and checked
# afterwards has already written whatever it wanted to.
OBSERVED=$(PYTHONPATH="$ROOT" python3 -c \
    "from appliance import rpi_image_gen; print(rpi_image_gen.file_sha256('$ARCHIVE'))")
[ "$OBSERVED" = "$DIGEST" ] \
    || fail "the download hashes to $OBSERVED, the lock pins $DIGEST" source_unverified

echo "== extracting =="
mkdir -p "$STAGE/tree"
# No absolute paths, no traversal, no symlinks followed out of the tree.
tar -xzf "$ARCHIVE" -C "$STAGE/tree" --no-same-owner --no-same-permissions \
    || fail "the archive could not be extracted" fetch_failed
[ -d "$STAGE/tree/$TOP_LEVEL" ] \
    || fail "the archive does not extract to $TOP_LEVEL" source_unverified
[ -f "$STAGE/tree/$TOP_LEVEL/LICENSE" ] \
    || fail "the extracted tree is not an rpi-image-gen source tree" source_unverified

PYTHONPATH="$ROOT" python3 - "$STAGE/tree/$TOP_LEVEL" "$OBSERVED" "$URL" \
    "$RELEASE" "$COMMIT" "$TOP_LEVEL" <<'PY' || fail "the source record could not be written" output_unusable
import json
import sys
from pathlib import Path

from appliance.rpi_image_gen import SOURCE_IDENTITY_NAME

tree, digest, url, release, commit, top_level = sys.argv[1:7]
Path(tree, SOURCE_IDENTITY_NAME).write_text(
    json.dumps(
        {
            "form": "tarball",
            "release": release,
            "commit": commit,
            "url": url,
            "sha256": digest,
            "top_level_directory": top_level,
        },
        indent=2,
        sort_keys=True,
    )
    + "\n",
    encoding="utf-8",
)
PY

mkdir -p "$(dirname "$INTO")" || fail "cannot create $(dirname "$INTO")" output_unusable
mv "$STAGE/tree/$TOP_LEVEL" "$INTO" || fail "the tree could not be moved into place" output_unusable

echo "tree:   $INTO"
echo "digest: $OBSERVED"
echo "RESULT: PASS (tarball $RELEASE)"
