#!/bin/sh
# SPDX-License-Identifier: AGPL-3.0-or-later
# Check a built appliance image against its declared layout, without booting.
#
#   scripts/appliance-inspect-rpi-image.sh [--json] [--appliance-version V]
#                                          [--build-id ID] [--architecture ARCH]
#                                          [--no-contents] <image.img>
#
# The inspector reads the partition table, the filesystem structures and the
# files inside them straight out of the image file: no loop device, no mount,
# no root.
#
# Content is read rather than mounted for a reason that does not go away. The
# Pi 5 root filesystem uses 16 KiB ext4 blocks, and a kernel mounts ext4 only up
# to its page size, so on a 4 KiB-page build host a mount-based path cannot run
# at all -- and a gate that reports the checks it needed as NOT RUN while
# passing is a gate that proves nothing.
#
# Every finding declares whether a release may be cut without it. A mandatory
# check that did not run leaves the inspection incomplete rather than passing:
# "the image is good" and "nobody looked" were the same verdict before, which
# is how an inspection whose checks were never run reached a release gate as
# PASS.
#
# Exit status: 0 every mandatory check passed, 1 a check failed, 2 the command
# line is wrong, 3 the host cannot inspect or a mandatory check did not run.
set -eu

ROOT=$(cd "$(dirname "$0")/.." && pwd)
FORMAT=text
IMAGE=""
CONTENTS=1
APPLIANCE_VERSION=""
BUILD_ID=""
# Every profile builds one architecture; a release that changes that has to say so.
ARCHITECTURE=arm64

usage() {
    sed -n '3,26p' "$0"
}

while [ $# -gt 0 ]; do
    case "$1" in
        --json) FORMAT=json; shift ;;
        --no-contents) CONTENTS=0; shift ;;
        --appliance-version) APPLIANCE_VERSION=${2:?--appliance-version needs a value}; shift 2 ;;
        --appliance-version=*) APPLIANCE_VERSION=${1#*=}; shift ;;
        --build-id) BUILD_ID=${2:?--build-id needs a value}; shift 2 ;;
        --build-id=*) BUILD_ID=${1#*=}; shift ;;
        --architecture) ARCHITECTURE=${2:?--architecture needs a value}; shift 2 ;;
        --architecture=*) ARCHITECTURE=${1#*=}; shift ;;
        -h|--help) usage; exit 0 ;;
        -*) echo "unknown argument: $1" >&2; usage >&2; exit 2 ;;
        *) [ -z "$IMAGE" ] || { echo "one image at a time" >&2; exit 2; }; IMAGE=$1; shift ;;
    esac
done

[ -n "$IMAGE" ] || { usage >&2; exit 2; }
command -v python3 >/dev/null 2>&1 || {
    echo "appliance-inspect-rpi-image: python3 is not installed" >&2
    echo "RESULT: NOT RUN (required_tool_missing)" >&2
    exit 3
}
[ -f "$IMAGE" ] || {
    echo "appliance-inspect-rpi-image: $IMAGE does not exist" >&2
    echo "RESULT: NOT RUN (image_unavailable)" >&2
    exit 3
}

EMS_APPLIANCE_VERSION="$APPLIANCE_VERSION" EMS_BUILD_ID="$BUILD_ID" EMS_CONTENTS="$CONTENTS" \
EMS_ARCHITECTURE="$ARCHITECTURE" \
PYTHONPATH="$ROOT" python3 - "$IMAGE" "$FORMAT" <<'PY'
import json
import os
import sys

from appliance import image_inspect

image_path, output_format = sys.argv[1:3]
findings = image_inspect.inspect(
    image_path,
    appliance_version=os.environ.get("EMS_APPLIANCE_VERSION") or "",
    build_id=os.environ.get("EMS_BUILD_ID") or "",
    architecture=os.environ.get("EMS_ARCHITECTURE") or "",
    contents=os.environ.get("EMS_CONTENTS") != "0",
)
summary = image_inspect.summarise(findings)

if output_format == "json":
    print(json.dumps(summary, indent=2, sort_keys=True))
else:
    for finding in findings:
        scope = "" if finding.mandatory else " (optional)"
        print(f"{finding.result.upper():8} {finding.check:38} {finding.detail}{scope}")
    counts = summary["counts"]
    print()
    print(
        f"pass {counts['pass']}  fail {counts['fail']}  not run {counts['not_run']}"
        f"  ({summary['optional']} optional)"
    )
    if summary["mandatory_not_run"]:
        print("mandatory checks that did not run: " + ", ".join(summary["mandatory_not_run"]))
    print(f"RESULT: {summary['result'].upper().replace('_', ' ')}")

# A mandatory check that never ran is not a pass, and the exit status has to say
# so where a release gate can see it.
sys.exit({"pass": 0, "fail": 1, "not_run": 3}[summary["result"]])
PY
