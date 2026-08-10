#!/bin/sh
# SPDX-License-Identifier: AGPL-3.0-or-later
# Resolve one pinned, digest-verified guest base image and print its path.
#
#   scripts/appliance-guest-base-image.sh --role builder|smoke-amd64|guest-arm64
#                                         [--cache DIR] [--lock FILE] [--offline]
#
# Every disposable guest in this project boots through here. The builder guest
# is part of the release supply chain — it runs as root, receives the project
# source and produces the images a release is cut from — so "whatever
# cloud.debian.org published under latest today" is not an acceptable answer to
# what it booted.
#
# The digest in packaging/appliance/vm/base-images.lock.json is the authority.
# A download lands in .part, is verified, and only then renamed into place, so
# the cache never holds a file that was not proven. A cached image is re-hashed
# before every boot: the check that matters is the one taken immediately before
# use, not the one taken when the file was first written.
#
# A role with no lock entry, or an entry with no digest, is a failure. There is
# no path through this script that boots an unverified image.
#
# Exit status: 0 verified, the absolute path is on stdout. 1 the image is not
# what the lock names. 2 the command line is wrong. 3 the image could not be
# obtained on this host.
set -eu

ROOT=$(cd "$(dirname "$0")/.." && pwd)
LOCK=${EMS_APPLIANCE_VM_BASE_IMAGE_LOCK:-$ROOT/packaging/appliance/vm/base-images.lock.json}
CACHE=${EMS_APPLIANCE_VM_CACHE:-${XDG_CACHE_HOME:-$HOME/.cache}/ems-appliance-vm}
ROLE=""
OFFLINE=no

usage() { sed -n '3,22p' "$0"; }

fail() {
    echo "appliance-guest-base-image: $1" >&2
    echo "RESULT: FAIL ($2)" >&2
    exit 1
}

not_run() {
    echo "appliance-guest-base-image: $1" >&2
    echo "RESULT: NOT RUN ($2)" >&2
    exit 3
}

while [ $# -gt 0 ]; do
    case "$1" in
        --role) ROLE=${2:?--role needs a role name}; shift 2 ;;
        --role=*) ROLE=${1#*=}; shift ;;
        --cache) CACHE=${2:?--cache needs a directory}; shift 2 ;;
        --cache=*) CACHE=${1#*=}; shift ;;
        --lock) LOCK=${2:?--lock needs a file}; shift 2 ;;
        --lock=*) LOCK=${1#*=}; shift ;;
        --offline) OFFLINE=yes; shift ;;
        -h|--help) usage; exit 0 ;;
        *) echo "unknown argument: $1" >&2; usage >&2; exit 2 ;;
    esac
done

[ -n "$ROLE" ] || { echo "--role is required" >&2; usage >&2; exit 2; }
[ -f "$LOCK" ] || fail "the base image lock $LOCK is missing" base_image_lock_missing

command -v python3 >/dev/null 2>&1 || not_run "python3 is missing" python3_unavailable
command -v sha512sum >/dev/null 2>&1 || not_run "sha512sum is missing" sha512sum_unavailable

ENTRY=$(python3 - "$LOCK" "$ROLE" <<'PY' || exit 1
import json
import re
import sys

lock_path, role = sys.argv[1], sys.argv[2]
try:
    with open(lock_path, encoding="utf-8") as handle:
        lock = json.load(handle)
except (OSError, ValueError) as error:
    print(f"the base image lock could not be read: {error}", file=sys.stderr)
    raise SystemExit(1)

entry = (lock.get("images") or {}).get(role)
if not entry:
    print(f"no base image is locked for the role {role!r}", file=sys.stderr)
    raise SystemExit(1)

digest = str(entry.get("sha512") or "")
if not re.fullmatch(r"[0-9a-f]{128}", digest):
    print(f"the {role!r} entry carries no usable sha512 digest", file=sys.stderr)
    raise SystemExit(1)

filename = str(entry.get("filename") or "")
if not filename or "/" in filename or filename.startswith("."):
    print(f"the {role!r} entry names no plain filename", file=sys.stderr)
    raise SystemExit(1)

url = str(entry.get("url") or "")
if not url.startswith("https://"):
    print(f"the {role!r} entry names no https url", file=sys.stderr)
    raise SystemExit(1)

print("\t".join([filename, url, digest, str(entry.get("build_id") or "")]))
PY
) || fail "the lock does not describe a usable image for $ROLE" base_image_not_locked

FILENAME=$(printf '%s' "$ENTRY" | cut -f1)
URL=$(printf '%s' "$ENTRY" | cut -f2)
EXPECTED=$(printf '%s' "$ENTRY" | cut -f3)
BUILD_ID=$(printf '%s' "$ENTRY" | cut -f4)

mkdir -p "$CACHE"
TARGET="$CACHE/$FILENAME"

digest_of() { sha512sum "$1" | cut -d' ' -f1; }

if [ -f "$TARGET" ]; then
    actual=$(digest_of "$TARGET")
    if [ "$actual" != "$EXPECTED" ]; then
        fail "the cached $FILENAME is not the locked image; remove $TARGET to refetch" \
            base_image_digest_mismatch
    fi
    echo "appliance-guest-base-image: $ROLE $FILENAME ($BUILD_ID) verified from cache" >&2
    printf '%s\n' "$TARGET"
    exit 0
fi

[ "$OFFLINE" = no ] || not_run "$FILENAME is not cached and --offline was given" base_image_uncached
command -v curl >/dev/null 2>&1 || not_run "curl is missing" curl_unavailable

echo "appliance-guest-base-image: fetching $FILENAME ($BUILD_ID)" >&2
PART="$TARGET.part"
rm -f "$PART"
curl -fsSL --retry 3 -o "$PART" "$URL" \
    || { rm -f "$PART"; not_run "$URL could not be downloaded" base_image_unavailable; }

actual=$(digest_of "$PART")
if [ "$actual" != "$EXPECTED" ]; then
    rm -f "$PART"
    fail "the downloaded $FILENAME is not the locked image" base_image_digest_mismatch
fi
mv "$PART" "$TARGET"

echo "appliance-guest-base-image: $ROLE $FILENAME ($BUILD_ID) verified after download" >&2
printf '%s\n' "$TARGET"
