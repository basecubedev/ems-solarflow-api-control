#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Build the release index an appliance reads to learn what it may install.

    scripts/appliance-build-os-release-index.py --base-url URL \
        [--previous os-releases.json] [--keep N] [--output os-releases.json] \
        MANIFEST [MANIFEST ...]

The appliance has been configured to fetch this file since the first image was
built, and nothing has ever produced one. Without it the update path cannot run
at all — not for an older release, not for the newest.

What it is: a list of places to look. The appliance treats every entry as a
suggestion, verifies the signed manifest it points at, and decides from that.
The descriptive fields exist so an operator choosing between several published
releases has something to choose by; they are read from each manifest here
rather than typed, so they cannot disagree with what the appliance will verify.

Why it carries history: a release that turns out to be bad is only recoverable
if the one before it is still listed. An index naming a single release makes
every published release a one-way step.
"""

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from appliance import image_variants, os_releases, release_index  # noqa: E402

DESCRIBED_FROM_MANIFEST = ("release_version", "created_at", "build_id")


def entry_for(manifest_path, base_url):
    """One index entry, described entirely from the manifest it points at."""

    payload = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    release = os_releases.parse_manifest(payload)
    stem = Path(manifest_path).name
    if not stem.endswith(".manifest.json"):
        raise SystemExit(f"{manifest_path}: expected a *.manifest.json file")
    release_id = stem[: -len(".manifest.json")]
    os_releases.validate_release_id(release_id)

    prefix = base_url.rstrip("/")
    entry = {
        "release_id": release_id,
        "manifest_url": f"{prefix}/{release_id}.manifest.json",
        "signature_url": f"{prefix}/{release_id}.manifest.json.asc",
        "archive_url": f"{prefix}/{release.archive_name}",
    }
    for field in DESCRIBED_FROM_MANIFEST:
        entry[field] = str(getattr(release, field))
    if release.compatible_hardware:
        entry["board"] = str(release.compatible_hardware[0])
    entry["variant"] = variant_of(release_id)
    return entry


def variant_of(release_id):
    """Which image variant this release updates, stated by the identifier.

    A single-slot image has no update archive at all — it is patched by apt and
    has no second slot to write. Listing one would offer an update no appliance
    could ever apply, so it is refused here rather than published and refused
    later on a device.
    """

    for slug in sorted(image_variants.VARIANTS):
        if release_id.endswith(f"-{slug}"):
            if not image_variants.variant(slug).has_update_archive:
                raise SystemExit(
                    f"{release_id}: the {slug} variant has no update archive to offer"
                )
            return slug
    raise SystemExit(f"{release_id}: names no image variant this project builds")


def build(manifests, *, base_url, previous=None, keep=0):
    entries = [entry_for(manifest, base_url) for manifest in manifests]
    try:
        return release_index.assemble(entries, previous=previous or "", keep=keep)
    except release_index.ReleaseIndexError as exc:
        raise SystemExit(exc.message)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("manifests", nargs="+", metavar="MANIFEST")
    parser.add_argument("--base-url", required=True, help="where the assets are published")
    parser.add_argument("--previous", default="", help="an existing index to carry history from")
    parser.add_argument(
        "--keep",
        type=int,
        default=0,
        help="list at most this many releases, newest first (0 lists every one)",
    )
    parser.add_argument("--output", default="-", help="write here instead of stdout")
    args = parser.parse_args(argv)

    if not args.base_url.startswith("https://"):
        raise SystemExit("--base-url must be https: the appliance refuses any other scheme")

    index, dropped = build(
        args.manifests, base_url=args.base_url, previous=args.previous, keep=args.keep
    )
    # A release the index stops naming is one no appliance can reach any more.
    # Saying which ones, out loud, is the difference between a retention policy
    # and a silent truncation.
    for entry in dropped:
        print(
            f"no longer listed: {entry['release_id']} "
            f"({entry.get('release_version') or 'unknown version'})",
            file=sys.stderr,
        )

    # Parsing it back is the check: an index the appliance would refuse, or one
    # whose entries it would silently skip, must not leave this script.
    try:
        release_index.verify(index)
    except release_index.ReleaseIndexError as exc:
        raise SystemExit(exc.message)

    text = json.dumps(index, indent=2, sort_keys=True) + "\n"
    if args.output == "-":
        sys.stdout.write(text)
    else:
        Path(args.output).write_text(text, encoding="utf-8")
        print(f"{args.output}: {len(index['releases'])} releases", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
