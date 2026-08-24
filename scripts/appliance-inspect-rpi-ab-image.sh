#!/bin/sh
# SPDX-License-Identifier: AGPL-3.0-or-later
# Check a built appliance image against its declared layout, without booting.
#
#   scripts/appliance-inspect-rpi-ab-image.sh [--json] [--variant ab|single]
#                                             [--compare other.img]
#                                             [--appliance-version V] [--build-id ID]
#                                             [--architecture ARCH]
#                                             [--no-contents] [--mount] <image.img>
#
# The variant is stated, never sniffed. Deciding it from what the file happens
# to contain would let an image be judged by the contract it already satisfies
# rather than the one it was built to.
#
# The inspector reads the GPT, the filesystem structures and the files inside
# them straight out of the image file: no loop device, no mount, no root.
#
# Content is read rather than mounted for a reason that does not go away. The
# Pi 5 root filesystem uses 16 KiB ext4 blocks, and a kernel mounts ext4 only up
# to its page size, so on a 4 KiB-page build host the mount-based path cannot
# run at all — and the gate used to report the five checks it needed as NOT RUN
# while passing. Both slot roots, both boot partitions and the bootconfig
# partition are inspected: an update writes the *other* slot, so content present
# in only one of them is an appliance that stops being one at the first switch.
#
# --mount is still accepted and answers the same questions over a read-only loop
# device where the host kernel can mount the filesystem. It needs root.
#
# Every finding declares whether a release may be cut without it. A mandatory
# check that did not run leaves the inspection incomplete rather than passing:
# "the image is good" and "nobody looked" were the same verdict before, which
# is how an inspection whose independent GPT oracle was never installed reached
# a release gate as PASS.
#
# Exit status: 0 every mandatory check passed, 1 a check failed, 2 the command
# line is wrong, 3 the host cannot inspect or a mandatory check did not run.
set -eu

ROOT=$(cd "$(dirname "$0")/.." && pwd)
FORMAT=text
IMAGE=""
COMPARE=""
MOUNT=0
MOUNT_A=""
MOUNT_B=""
MOUNT_BOOT=""
CONTENTS=1
VARIANT=ab
APPLIANCE_VERSION=""
BUILD_ID=""
# Both profiles build one architecture; a release that changes that has to say so.
ARCHITECTURE=arm64

usage() {
    sed -n '3,22p' "$0"
}

while [ $# -gt 0 ]; do
    case "$1" in
        --json) FORMAT=json; shift ;;
        --mount) MOUNT=1; shift ;;
        --no-contents) CONTENTS=0; shift ;;
        --variant) VARIANT=${2:?--variant needs ab or single}; shift 2 ;;
        --variant=*) VARIANT=${1#*=}; shift ;;
        --compare) COMPARE=${2:?--compare needs a second image}; shift 2 ;;
        --compare=*) COMPARE=${1#*=}; shift ;;
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
case "$VARIANT" in
    ab|single) ;;
    *) echo "unknown variant: $VARIANT" >&2; usage >&2; exit 2 ;;
esac
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

if [ "$MOUNT" -eq 1 ]; then
    [ "$(id -u)" = "0" ] || {
        echo "appliance-inspect-rpi-ab-image: --mount needs root" >&2
        echo "RESULT: NOT RUN (mount_requires_root)" >&2
        exit 3
    }
    for tool in losetup mount umount; do
        command -v "$tool" >/dev/null 2>&1 || {
            echo "appliance-inspect-rpi-ab-image: $tool is not installed" >&2
            echo "RESULT: NOT RUN (required_tool_missing)" >&2
            exit 3
        }
    done

    MNT=$(mktemp -d "${TMPDIR:-/tmp}/ems-appliance-inspect.XXXXXX")
    LOOP=""
    cleanup() {
        for point in "$MNT/boot" "$MNT/system_b" "$MNT/system_a"; do
            [ -d "$point" ] && umount "$point" 2>/dev/null || true
        done
        [ -n "$LOOP" ] && losetup -d "$LOOP" 2>/dev/null || true
        rm -rf "$MNT"
    }
    trap cleanup EXIT

    # Read-only, so inspecting an image can never be what corrupted it.
    LOOP=$(losetup --find --show --partscan --read-only "$IMAGE") || {
        echo "appliance-inspect-rpi-ab-image: the image could not be attached" >&2
        echo "RESULT: NOT RUN (loop_device_unavailable)" >&2
        exit 3
    }

    # The partition a label lives on is read from the GPT rather than assumed,
    # so a layout change is a failed check and not a wrong mount.
    for entry in $(PYTHONPATH="$ROOT" python3 - "$IMAGE" <<'PY'
import sys

from appliance import ab_image

for partition in ab_image.read_partitions(sys.argv[1]):
    if partition.label in ("boot_a", "system_a", "system_b"):
        print(f"{partition.label}={partition.number}")
PY
    ); do
        label=${entry%%=*}
        number=${entry#*=}
        case "$label" in
            system_a) point="$MNT/system_a"; MOUNT_A=$point ;;
            system_b) point="$MNT/system_b"; MOUNT_B=$point ;;
            boot_a) point="$MNT/boot"; MOUNT_BOOT=$point ;;
        esac
        mkdir -p "$point"
        mount -o ro "${LOOP}p${number}" "$point" 2>/dev/null || {
            echo "appliance-inspect-rpi-ab-image: ${LOOP}p${number} ($label) could not be mounted" >&2
            echo "RESULT: NOT RUN (partition_not_mountable)" >&2
            exit 3
        }
    done
fi

EMS_APPLIANCE_VERSION="$APPLIANCE_VERSION" EMS_BUILD_ID="$BUILD_ID" EMS_CONTENTS="$CONTENTS" \
EMS_ARCHITECTURE="$ARCHITECTURE" \
PYTHONPATH="$ROOT" python3 - "$IMAGE" "$FORMAT" "$COMPARE" "$MOUNT_A" "$MOUNT_B" "$MOUNT_BOOT" \
    "$VARIANT" <<'PY'
import json
import os
import shutil
import subprocess
import sys

from appliance import ab_image

image_path, output_format, compare, mount_a, mount_b, mount_boot, variant = sys.argv[1:8]
findings = ab_image.inspect(
    image_path,
    variant=variant,
    appliance_version=os.environ.get("EMS_APPLIANCE_VERSION") or "",
    build_id=os.environ.get("EMS_BUILD_ID") or "",
    architecture=os.environ.get("EMS_ARCHITECTURE") or "",
    contents=os.environ.get("EMS_CONTENTS") != "0",
)


def independent_gpt_oracle(path):
    """A second opinion on the partition table, from a tool nobody here wrote.

    The internal parser and the checks that read it share an author and a set
    of assumptions. sgdisk does not, so its verdict is the cross-check — and it
    is mandatory: a release cut on one parser's opinion of its own output is
    exactly the evidence this gate exists to refuse. A builder or finalizer
    without gdisk installed leaves the inspection incomplete, not passing.
    """

    sgdisk = shutil.which("sgdisk")
    if not sgdisk:
        return ab_image.Finding(
            "gpt_independent_oracle", ab_image.NOT_RUN, "sgdisk (gdisk) is not installed"
        )
    result = subprocess.run(
        [sgdisk, "--verify", str(path)], capture_output=True, text=True, check=False
    )
    output = f"{result.stdout}\n{result.stderr}"
    if result.returncode != 0:
        return ab_image.Finding(
            "gpt_independent_oracle", ab_image.FAIL, output.strip().splitlines()[-1][:160]
        )
    problems = [
        line.strip()
        for line in output.splitlines()
        if "Problem" in line or "problem" in line or "corrupt" in line.lower()
    ]
    if problems and not any("No problems found" in line for line in problems):
        return ab_image.Finding("gpt_independent_oracle", ab_image.FAIL, problems[0][:160])
    return ab_image.Finding("gpt_independent_oracle", ab_image.PASS, "sgdisk found no problems")


if variant == "ab":
    findings.append(independent_gpt_oracle(image_path))
else:
    # sgdisk verifies a GPT. Pointing it at an MBR image would answer a
    # question this image does not pose, and its answer would be about the
    # protective entry a GPT tool expects to find rather than about anything
    # this build produced.
    findings.append(
        ab_image.Finding(
            "gpt_independent_oracle",
            ab_image.NOT_RUN,
            "a single-slot image carries no GPT to cross-check",
            mandatory=False,
        )
    )

if variant == "ab" and mount_a and mount_b:
    findings = [f for f in findings if f.check not in ab_image.UNMOUNTED_CHECKS]
    findings.extend(
        ab_image.inspect_mounted(
            system_a=mount_a, system_b=mount_b, boot=mount_boot or None
        )
    )
if compare:
    # Two independently built images must not share a partition identity, or
    # two appliance media on one bus would be indistinguishable.
    findings.append(ab_image.compare_identities(image_path, compare))
summary = ab_image.summarise(findings)

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
