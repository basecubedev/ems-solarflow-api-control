#!/bin/sh
# SPDX-License-Identifier: AGPL-3.0-or-later
# Run the pinned slot-shared generator and prove every shared mount activates.
#
#   scripts/appliance-verify-slot-mounts.sh [--rpi-image-gen DIR]
#
# The generator is upstream's, unmodified, executed against this project's own
# slot-shared configuration in a disposable mount namespace. What it writes is
# compared against what appliance/ab_persistence.py declares:
#
#   every declared path produces a .mount unit
#   every one of those units is activated, by the generator or by the image
#
# A build that would ship an image where a shared bind is never activated has to
# fail here. At runtime ems-appliance-persistence.service verifies the same
# thing again, independently, and fails closed.
#
# Exit status: 0 complete, 1 incomplete, 2 the command line is wrong, 3 the
# check could not run here.
set -eu

ROOT=$(cd "$(dirname "$0")/.." && pwd)
GENERATOR=${EMS_RPI_IMAGE_GEN:-}
OVERLAY="$ROOT/packaging/appliance/image/layer/ems-appliance.rootfs-overlay"

usage() {
    sed -n '3,18p' "$0"
}

not_run() {
    echo "appliance-verify-slot-mounts: $1" >&2
    echo "RESULT: NOT RUN ($2)" >&2
    exit 3
}

fail() {
    echo "appliance-verify-slot-mounts: $1" >&2
    echo "RESULT: FAIL ($2)" >&2
    exit 1
}

while [ $# -gt 0 ]; do
    case "$1" in
        --rpi-image-gen) GENERATOR=${2:?--rpi-image-gen needs a directory}; shift 2 ;;
        --rpi-image-gen=*) GENERATOR=${1#*=}; shift ;;
        -h|--help) usage; exit 0 ;;
        *) echo "unknown argument: $1" >&2; usage >&2; exit 2 ;;
    esac
done

[ -n "$GENERATOR" ] && [ -d "$GENERATOR" ] \
    || not_run "rpi-image-gen was not found; pass --rpi-image-gen DIR" \
               rpi_image_gen_unavailable
command -v unshare >/dev/null 2>&1 || not_run "unshare is not installed" required_tool_missing
command -v python3 >/dev/null 2>&1 || not_run "python3 is not installed" required_tool_missing

SCRIPT="$GENERATOR/image/gpt/ab_userdata/device/rootfs-overlay/usr/lib/systemd/system-generators/slot-shared-generator"
[ -f "$SCRIPT" ] || fail "$SCRIPT is missing" rpi_image_gen_incompatible

WORK=$(mktemp -d) || not_run "cannot create a working directory" output_unusable
trap 'rm -rf "$WORK"' EXIT
mkdir -p "$WORK/conf" "$WORK/out"

PYTHONPATH="$ROOT" python3 -c \
    "from appliance import ab_persistence
import pathlib
pathlib.Path('$WORK/conf', ab_persistence.SLOT_SHARED_CONF_NAME).write_text(
    ab_persistence.slot_shared_conf(), encoding='utf-8')" \
    || fail "the slot-shared configuration could not be generated" persistence_contract_unreadable

unshare -rm sh -c "
set -e
mount -t tmpfs tmpfs /etc
mkdir -p /etc/rpi-image-gen/slot-shared.d
cp $WORK/conf/*.conf /etc/rpi-image-gen/slot-shared.d/
mount -t tmpfs tmpfs /run/systemd
mkdir -p /run/systemd/generator
$SCRIPT
cp -r /run/systemd/generator/. $WORK/out/
" || not_run "a private mount namespace is not available here" namespace_unavailable

PYTHONPATH="$ROOT" python3 - "$WORK/out" "$OVERLAY" <<'PY'
import sys
from pathlib import Path

from appliance import ab_persistence

generated_dir, overlay = (Path(item) for item in sys.argv[1:3])
expected = set(ab_persistence.shared_mount_units())

units = {item.name for item in generated_dir.iterdir() if item.suffix == ".mount"}
wants_dir = generated_dir / "local-fs.target.wants"
generated_wants = {item.name for item in wants_dir.iterdir()} if wants_dir.is_dir() else set()
shipped = {
    item.name
    for item in (overlay / "etc/systemd/system/local-fs.target.wants").iterdir()
} if (overlay / "etc/systemd/system/local-fs.target.wants").is_dir() else set()

missing_units = expected - units
activated = generated_wants | shipped
missing_activation = expected - activated

print(f"declared paths:      {len(expected)}")
print(f"generated units:     {len(units & expected)}")
print(f"upstream activation: {len(generated_wants & expected)}")
print(f"image activation:    {len(shipped & expected)}")

if missing_units:
    print("no mount unit generated for: " + ", ".join(sorted(missing_units)), file=sys.stderr)
    sys.exit(1)
if missing_activation:
    print(
        "generated but never activated: " + ", ".join(sorted(missing_activation)),
        file=sys.stderr,
    )
    sys.exit(1)
PY
status=$?
[ "$status" -eq 0 ] || fail "the generated persistence units are incomplete" persistence_units_incomplete

echo "RESULT: PASS (every declared shared path is generated and activated)"
