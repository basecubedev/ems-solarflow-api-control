#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Build the index an appliance reads to learn which manager packages exist.

    scripts/appliance-build-manager-index.py --base-url URL \
        [--previous manager-packages.json] [--keep N] [--output FILE] \
        MANIFEST [MANIFEST ...]

Same format and the same rule as the OS release index: a list of places to
look, every entry a suggestion, and what may be installed decided by the
signature over the manifest it points at.

It carries history for a sharper reason here than there. The manager has no
second slot: going back to an earlier package is the whole recovery, and an
index naming only the newest package takes that away from every appliance that
did not keep one locally.
"""

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from appliance import manager_releases, os_releases, release_index  # noqa: E402

DESCRIBED_FROM_MANIFEST = (("release_version", "version"), ("created_at", "created_at"),
                           ("build_id", "build_id"))


def entry_for(manifest_path, base_url):
    """One index entry, described entirely from the manifest it points at."""

    payload = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    stem = Path(manifest_path).name
    if not stem.endswith(".manifest.json"):
        raise SystemExit(f"{manifest_path}: expected a *.manifest.json file")
    release_id = stem[: -len(".manifest.json")]
    try:
        release = manager_releases.parse_manifest(payload, release_id=release_id)
    except (manager_releases.ManagerReleaseError, os_releases.ReleaseError) as exc:
        raise SystemExit(f"{manifest_path}: {getattr(exc, 'message', exc)}")

    prefix = base_url.rstrip("/")
    entry = {
        "release_id": release.release_id,
        "manifest_url": f"{prefix}/{release.release_id}.manifest.json",
        "signature_url": f"{prefix}/{release.release_id}.manifest.json.asc",
        "archive_url": f"{prefix}/{release.artifact_name}",
        "architecture": release.architecture,
    }
    for field, attribute in DESCRIBED_FROM_MANIFEST:
        entry[field] = str(getattr(release, attribute))
    return entry


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
        help="list at most this many packages, newest first (0 lists every one)",
    )
    parser.add_argument("--output", default="-", help="write here instead of stdout")
    args = parser.parse_args(argv)

    if not args.base_url.startswith("https://"):
        raise SystemExit("--base-url must be https: the appliance refuses any other scheme")

    index, dropped = build(
        args.manifests, base_url=args.base_url, previous=args.previous, keep=args.keep
    )
    # A package the index stops naming is one no appliance can reach any more,
    # and for this artefact that is the recovery path. Saying which ones, out
    # loud, is the difference between a retention policy and a silent one.
    for entry in dropped:
        print(
            f"no longer listed: {entry['release_id']} "
            f"({entry.get('release_version') or 'unknown version'})",
            file=sys.stderr,
        )

    try:
        release_index.verify(index)
    except release_index.ReleaseIndexError as exc:
        raise SystemExit(exc.message)

    text = json.dumps(index, indent=2, sort_keys=True) + "\n"
    if args.output == "-":
        sys.stdout.write(text)
    else:
        Path(args.output).write_text(text, encoding="utf-8")
        print(f"{args.output}: {len(index['releases'])} packages", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
