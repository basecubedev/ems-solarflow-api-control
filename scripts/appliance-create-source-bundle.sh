#!/bin/sh
# SPDX-License-Identifier: AGPL-3.0-or-later
# Create a source/review bundle of this repository, and prove it round-trips.
#
#   scripts/appliance-create-source-bundle.sh [--output FILE] [--ref REF]
#                                             [--prefix NAME] [--keep-invalid]
#
# A delivery path that rewrites a file mode drops the executable bit off a build
# hook; one that flattens a symlink turns it into a regular file that still
# parses. Either produces a tree that still builds and is not this project --
# silently, and only at the far end. Both archives produced for previous
# independent reviews arrived that way.
#
# So the bundle is written from the git object tree rather than the working
# directory, and it is then verified against that same tree object by object
# before it is handed over. An archive that does not round-trip is deleted
# rather than delivered.
#
# Alongside the bundle it writes a source authority: the bundle's digest, the
# full project revision and the canonical tree hash the build authority uses.
# Production finalization compares the two, because a build from one commit and
# a source bundle from another are both individually valid artefacts.
#
# Read-only with respect to the repository. Nothing is published or uploaded.
#
# Exit status: 0 the bundle was written and verified, 1 it did not round-trip,
# 2 the command line is wrong, 3 a prerequisite is missing.
set -eu

ROOT=$(cd "$(dirname "$0")/.." && pwd)
REF=HEAD
PREFIX=""
OUTPUT=""
KEEP=no

usage() {
    sed -n '3,22p' "$0"
}

not_run() {
    echo "appliance-create-source-bundle: $1" >&2
    echo "RESULT: NOT RUN ($2)" >&2
    exit 3
}

fail() {
    echo "appliance-create-source-bundle: $1" >&2
    echo "RESULT: FAIL ($2)" >&2
    exit 1
}

while [ $# -gt 0 ]; do
    case "$1" in
        --output) OUTPUT=${2:?--output needs a file}; shift 2 ;;
        --output=*) OUTPUT=${1#*=}; shift ;;
        --ref) REF=${2:?--ref needs a revision}; shift 2 ;;
        --ref=*) REF=${1#*=}; shift ;;
        --prefix) PREFIX=${2:?--prefix needs a directory name}; shift 2 ;;
        --prefix=*) PREFIX=${1#*=}; shift ;;
        --keep-invalid) KEEP=yes; shift ;;
        -h|--help) usage; exit 0 ;;
        *) echo "unknown argument: $1" >&2; usage >&2; exit 2 ;;
    esac
done

command -v git >/dev/null 2>&1 || not_run "git is not installed" required_tool_missing
command -v python3 >/dev/null 2>&1 || not_run "python3 is not installed" required_tool_missing
git -C "$ROOT" rev-parse --git-dir >/dev/null 2>&1 \
    || not_run "$ROOT is not a git repository" not_a_repository
REVISION=$(git -C "$ROOT" rev-parse "$REF" 2>/dev/null) \
    || not_run "$REF does not name a revision" unknown_revision

NAME=$(basename "$ROOT")
[ -n "$PREFIX" ] || PREFIX="$NAME"
[ -n "$OUTPUT" ] || OUTPUT="$ROOT/dist/$NAME-$(echo "$REVISION" | cut -c1-12).tar.gz"
mkdir -p "$(dirname "$OUTPUT")" || fail "cannot create $(dirname "$OUTPUT")" output_unusable

# git archive reads the object tree, so it emits modes and symlinks as git
# recorded them. It never follows a link and never picks up an untracked file,
# which is the whole reason the bundle is not built from the working directory.
git -C "$ROOT" archive --format=tar --prefix="$PREFIX/" "$REVISION" \
    | gzip -n > "$OUTPUT" \
    || fail "the archive could not be written" archive_failed

MANIFEST="${OUTPUT%.tar.gz}"
MANIFEST="${MANIFEST%.tar}.manifest.json"
AUTHORITY="${OUTPUT%.tar.gz}"
AUTHORITY="${AUTHORITY%.tar}.source-authority.json"

set +e
PYTHONPATH="$ROOT" python3 - "$OUTPUT" "$ROOT" "$REVISION" "$PREFIX" "$MANIFEST" <<'PY'
import json
import sys

from appliance import source_bundle

archive, root, revision, prefix, manifest = sys.argv[1:6]

try:
    report = source_bundle.verify(archive, root=root, ref=revision, prefix=prefix)
except source_bundle.SourceBundleError as exc:
    print(f"appliance-create-source-bundle: {exc.message}", file=sys.stderr)
    sys.exit(3)

payload = report.to_dict()
payload["ref"] = revision
payload["prefix"] = prefix
with open(manifest, "w", encoding="utf-8") as handle:
    handle.write(json.dumps(payload, indent=2, sort_keys=True) + "\n")

for path in report.missing:
    print(f"MISSING    {path}", file=sys.stderr)
for path, reason in report.mismatched:
    print(f"CHANGED    {path}: {reason}", file=sys.stderr)
for path in report.unexpected:
    print(f"UNDECLARED {path}", file=sys.stderr)
for path, reason in report.unsafe:
    print(f"UNSAFE     {path}: {reason}", file=sys.stderr)
for path in report.duplicate:
    print(f"DUPLICATE  {path}", file=sys.stderr)

print(f"compared: {report.compared} tracked object(s)")
print(f"symlinks: {report.symlinks} preserved")
sys.exit(0 if report.ok else 1)
PY
verified=$?
set -e

if [ "$verified" -ne 0 ]; then
    if [ "$KEEP" = no ]; then
        rm -f "$OUTPUT" "$MANIFEST" "$AUTHORITY"
        echo "appliance-create-source-bundle: the bundle did not round-trip and was deleted" >&2
    fi
    [ "$verified" -eq 3 ] && exit 3
    fail "the bundle is not the tracked tree" bundle_not_faithful
fi

# Only for a bundle that already round-tripped: an authority for an archive
# that is not the tracked tree would name a project this bundle is not.
PYTHONPATH="$ROOT" python3 - "$OUTPUT" "$ROOT" "$REVISION" "$AUTHORITY" <<'PY' \
    || fail "the source authority could not be recorded" source_authority_failed
import sys

from appliance import release_inputs

archive, root, revision, target = sys.argv[1:5]
try:
    authority = release_inputs.describe_source_bundle(archive, root=root, ref=revision)
except Exception as error:  # noqa: BLE001 - reported, never swallowed
    sys.exit(f"appliance-create-source-bundle: {error}")
release_inputs.write_source_bundle_authority(target, authority)
print(f"project:  {authority.revision}")
print(f"tree:     {authority.tree_sha256}")
print(f"objects:  {authority.tracked_objects} tracked, {authority.symlinks} symlink(s)")
PY

echo
echo "bundle:   $OUTPUT"
echo "manifest: $MANIFEST"
echo "authority: $AUTHORITY"
echo "ref:      $REVISION"
echo "Nothing was published or uploaded."
echo "RESULT: PASS ($(basename "$OUTPUT"))"
