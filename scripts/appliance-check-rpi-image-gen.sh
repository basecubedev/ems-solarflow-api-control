#!/bin/sh
# SPDX-License-Identifier: AGPL-3.0-or-later
# Check an rpi-image-gen checkout against the pinned contract. Read-only.
#
#   scripts/appliance-check-rpi-image-gen.sh [--json] [--rpi-image-gen DIR]
#
# Nothing is executed, downloaded, installed or built. The checkout is compared
# against packaging/appliance/image/rpi-image-gen.lock, which pins the revision
# whose image-rpios layer defines this appliance's partition table and the
# labels its root is mounted through.
#
# Exit status: 0 compatible and buildable, 1 incompatible or the source identity
# could not be proven, 2 the command line is wrong, 3 compatible but this host is
# missing build dependencies.
#
# Both supported source forms are accepted: a git checkout at the pinned commit,
# and a release tarball whose SHA-256 the fetch script verified and recorded.
# A tree that is neither is refused, never reported as NOT RUN.
set -eu

ROOT=$(cd "$(dirname "$0")/.." && pwd)
FORMAT=text
GENERATOR=${EMS_RPI_IMAGE_GEN:-}

usage() {
    sed -n '3,18p' "$0"
}

while [ $# -gt 0 ]; do
    case "$1" in
        --json) FORMAT=json; shift ;;
        --rpi-image-gen) GENERATOR=${2:?--rpi-image-gen needs a directory}; shift 2 ;;
        --rpi-image-gen=*) GENERATOR=${1#*=}; shift ;;
        -h|--help) usage; exit 0 ;;
        *) echo "unknown argument: $1" >&2; usage >&2; exit 2 ;;
    esac
done

if [ -z "$GENERATOR" ]; then
    for candidate in /usr/share/rpi-image-gen /opt/rpi-image-gen "$ROOT/../rpi-image-gen"; do
        [ -d "$candidate" ] && GENERATOR=$candidate && break
    done
fi
[ -n "$GENERATOR" ] && [ -d "$GENERATOR" ] || {
    echo "appliance-check-rpi-image-gen: no rpi-image-gen checkout at ${GENERATOR:-<unset>}" >&2
    echo "RESULT: NOT RUN (rpi_image_gen_unavailable)" >&2
    exit 3
}

PYTHONPATH="$ROOT" python3 - "$GENERATOR" "$FORMAT" <<'PY'
import json
import sys

from appliance import rpi_image_gen

directory, output_format = sys.argv[1:3]
lock = rpi_image_gen.read_lock()
report = rpi_image_gen.probe_checkout(directory, lock)
summary = report.to_dict()
summary["lock"] = lock.to_dict()
summary["build_host"] = rpi_image_gen.build_host_state(report.dependencies).to_dict()

if output_format == "json":
    print(json.dumps(summary, indent=2, sort_keys=True))
else:
    for finding in report.findings:
        print(f"{finding.result.upper():8} {finding.check:56} {finding.detail}")
    print()
    dependencies = report.dependencies
    if dependencies.missing_binaries:
        print("missing binaries:  " + ", ".join(dependencies.missing_binaries))
    if dependencies.missing_packages:
        print("missing packages:  " + ", ".join(dependencies.missing_packages))
    if dependencies.unverified_packages:
        print("unverifiable:      " + ", ".join(dependencies.unverified_packages))
    print(f"source identity:   {report.source_identity}")
    host = rpi_image_gen.build_host_state(dependencies)
    if host.missing_binfmt:
        print("missing binfmt:    " + ", ".join(host.missing_binfmt))
    if host.unsupported_architecture:
        print("architecture:      " + host.unsupported_architecture)
    if report.compatible and report.buildable:
        print(f"RESULT: PASS ({lock.release} {lock.commit[:12]})")
    elif report.compatible:
        print(f"RESULT: NOT RUN ({report.reason})")
    else:
        print(f"RESULT: FAIL ({report.reason})")

if not report.compatible:
    sys.exit(1)
if not report.buildable:
    sys.exit(3)
PY
