#!/bin/sh
# SPDX-License-Identifier: AGPL-3.0-or-later
# Compare a source bundle against the tracked tree. Read-only.
#
#   scripts/appliance-check-source-bundle.sh ARCHIVE [--ref REF] [--prefix PATH]
#                                            [--exclude PATH]... [--json]
#
# A delivery path that flattens a symlink into a regular file produces a tree
# that still builds and never activates its persistence mounts. So every tracked
# object is compared: content, file mode, symlink mode and symlink target.
#
# Paths a bundle deliberately omits must be named with --exclude. A silent
# omission and a dropped file are indistinguishable from the far end.
#
# Exit status: 0 the bundle is the tracked tree, 1 it is not, 2 the command line
# is wrong, 3 a prerequisite is missing.
set -eu

ROOT=$(cd "$(dirname "$0")/.." && pwd)
ARCHIVE=""
REF=HEAD
PREFIX=""
FORMAT=text
EXCLUDES=""

usage() {
    sed -n '3,16p' "$0"
}

not_run() {
    echo "appliance-check-source-bundle: $1" >&2
    echo "RESULT: NOT RUN ($2)" >&2
    exit 3
}

while [ $# -gt 0 ]; do
    case "$1" in
        --ref) REF=${2:?--ref needs a revision}; shift 2 ;;
        --ref=*) REF=${1#*=}; shift ;;
        --prefix) PREFIX=${2:?--prefix needs a path}; shift 2 ;;
        --prefix=*) PREFIX=${1#*=}; shift ;;
        --exclude) EXCLUDES="$EXCLUDES ${2:?--exclude needs a path}"; shift 2 ;;
        --exclude=*) EXCLUDES="$EXCLUDES ${1#*=}"; shift ;;
        --json) FORMAT=json; shift ;;
        -h|--help) usage; exit 0 ;;
        -*) echo "unknown argument: $1" >&2; usage >&2; exit 2 ;;
        *) ARCHIVE=$1; shift ;;
    esac
done

[ -n "$ARCHIVE" ] || { usage >&2; exit 2; }
command -v python3 >/dev/null 2>&1 || not_run "python3 is not installed" required_tool_missing
command -v git >/dev/null 2>&1 || not_run "git is not installed" required_tool_missing
[ -f "$ARCHIVE" ] || not_run "$ARCHIVE is not a file" bundle_unavailable

# shellcheck disable=SC2086
PYTHONPATH="$ROOT" python3 - "$ARCHIVE" "$ROOT" "$REF" "$PREFIX" "$FORMAT" $EXCLUDES <<'PY'
import json
import sys

from appliance import source_bundle

archive, root, ref, prefix, output_format = sys.argv[1:6]
exclude = tuple(sys.argv[6:])

try:
    # A bundle usually wraps its tree in one top-level directory. Detecting it
    # is all-or-nothing, so a reviewer does not have to know which name was
    # used and a bundle with no single root is still compared as-is.
    if not prefix:
        prefix = source_bundle.detect_prefix(archive)
        if prefix and output_format != "json":
            print(f"prefix:   {prefix} (detected)")
    report = source_bundle.verify(
        archive, root=root, ref=ref, prefix=prefix, exclude=exclude
    )
except source_bundle.SourceBundleError as exc:
    print(f"appliance-check-source-bundle: {exc.message}", file=sys.stderr)
    print(f"RESULT: NOT RUN ({exc.code})", file=sys.stderr)
    sys.exit(3)

if output_format == "json":
    print(json.dumps(report.to_dict(), indent=2, sort_keys=True))
else:
    for path in report.missing:
        print(f"MISSING    {path}")
    for path, reason in report.mismatched:
        print(f"CHANGED    {path}: {reason}")
    for path in report.unexpected:
        print(f"UNDECLARED {path}")
    for path, reason in report.unsafe:
        print(f"UNSAFE     {path}: {reason}")
    for path in report.duplicate:
        print(f"DUPLICATE  {path}")
    if report.excluded:
        print(f"excluded: {len(report.excluded)} declared path(s)")
    print(f"compared: {report.compared} tracked object(s)")
    print(f"symlinks: {report.symlinks} preserved")
    if report.ok:
        print(f"RESULT: PASS ({ref})")
    else:
        print(
            f"RESULT: FAIL ({len(report.missing)} missing, "
            f"{len(report.mismatched)} changed, "
            f"{len(report.unexpected)} undeclared, "
            f"{len(report.unsafe)} unsafe, "
            f"{len(report.duplicate)} duplicate)"
        )

sys.exit(0 if report.ok else 1)
PY
