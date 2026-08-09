#!/bin/sh
# SPDX-License-Identifier: AGPL-3.0-or-later
# Check a built A/B appliance image against the declared layout, without booting.
#
#   scripts/appliance-inspect-rpi-ab-image.sh [--json] [--compare other.img] <image.img>
#
# The inspector reads the GPT and the filesystem superblocks straight out of the
# image file: no loop device, no mount, no root. It checks the image-rota
# contract the runtime depends on: the slot labels, the filesystem types and the
# per-build partition identities. What it cannot check without mounting — that
# the package is installed, that the units are enabled — is reported as NOT RUN
# rather than as a pass, because a partition-table check is not image validation.
#
# Exit status: 0 every check passed, 1 a check failed, 2 the command line is
# wrong, 3 the host cannot inspect.
set -eu

ROOT=$(cd "$(dirname "$0")/.." && pwd)
FORMAT=text
IMAGE=""
COMPARE=""

usage() {
    sed -n '3,15p' "$0"
}

while [ $# -gt 0 ]; do
    case "$1" in
        --json) FORMAT=json; shift ;;
        --compare) COMPARE=${2:?--compare needs a second image}; shift 2 ;;
        --compare=*) COMPARE=${1#*=}; shift ;;
        -h|--help) usage; exit 0 ;;
        -*) echo "unknown argument: $1" >&2; usage >&2; exit 2 ;;
        *) [ -z "$IMAGE" ] || { echo "one image at a time" >&2; exit 2; }; IMAGE=$1; shift ;;
    esac
done

[ -n "$IMAGE" ] || { usage >&2; exit 2; }
command -v python3 >/dev/null 2>&1 || {
    echo "appliance-inspect-rpi-ab-image: python3 is not installed" >&2
    echo "RESULT: NOT RUN (required_tool_missing)" >&2
    exit 3
}
[ -f "$IMAGE" ] || {
    echo "appliance-inspect-rpi-ab-image: $IMAGE does not exist" >&2
    echo "RESULT: NOT RUN (image_unavailable)" >&2
    exit 3
}

PYTHONPATH="$ROOT" python3 - "$IMAGE" "$FORMAT" "$COMPARE" <<'PY'
import json
import sys

from appliance import ab_image

image_path, output_format, compare = sys.argv[1:4]
findings = ab_image.inspect(image_path)
if compare:
    # Two independently built images must not share a partition identity, or
    # two appliance media on one bus would be indistinguishable.
    findings.append(ab_image.compare_identities(image_path, compare))
summary = ab_image.summarise(findings)

if output_format == "json":
    print(json.dumps(summary, indent=2, sort_keys=True))
else:
    for finding in findings:
        print(f"{finding.result.upper():8} {finding.check:38} {finding.detail}")
    counts = summary["counts"]
    print()
    print(
        f"pass {counts['pass']}  fail {counts['fail']}  not run {counts['not_run']}"
    )
    print(f"RESULT: {summary['result'].upper().replace('_', ' ')}")

sys.exit(1 if summary["counts"]["fail"] else 0)
PY
