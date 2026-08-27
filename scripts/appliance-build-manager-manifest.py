#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Describe a built Appliance Manager package so an appliance can verify it.

    scripts/appliance-build-manager-manifest.py --revision SHA \
        [--release-id ID] [--output DIR] PACKAGE.deb

The package alone says nothing an appliance may act on: dpkg's own metadata is
inside the archive being judged. This writes the manifest that is signed
instead — the file whose detached signature decides whether the package may be
believed at all.

Everything descriptive is read from the package and its build record rather
than typed, so the manifest cannot disagree with the artefact it points at.
The state schemas are read from *this* tree, because they are what the manager
inside that package implements.

Sign the result before publishing:

    gpg --armor --detach-sign ID.manifest.json
"""

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from appliance import manager_releases, artifact_trust  # noqa: E402

REVISION = re.compile(r"^[0-9a-f]{40}$")


def build_record(package):
    """What build-deb.sh recorded beside the package it produced."""

    record = Path(str(package)[: -len(".deb")] + ".build.json")
    if not record.is_file():
        raise SystemExit(
            f"{record} is missing; the manifest cannot state what the package was built from"
        )
    payload = json.loads(record.read_text(encoding="utf-8"))
    if payload.get("artifact") != Path(package).name:
        raise SystemExit(
            f"{record} describes {payload.get('artifact')!r}, not {Path(package).name}"
        )
    return payload


def release_id_for(record):
    return f"{manager_releases.PACKAGE_NAME}-{record['version']}-{record['architecture']}"


def manifest_for(package, *, revision, created_at, release_id=""):
    package = Path(package)
    if package.suffix != ".deb":
        raise SystemExit(f"{package}: expected a .deb, so that an appliance can install it")
    record = build_record(package)
    payload = {
        "format_version": manager_releases.MANIFEST_FORMAT_VERSION,
        "package": manager_releases.PACKAGE_NAME,
        "version": record["version"],
        "architecture": record["architecture"],
        # The build id is the timestamp the package was made re-derivable from,
        # not a counter: two builds of one tag share it and hash identically.
        "build_id": str(record["source_date_epoch"]),
        "created_at": created_at,
        "project_revision": revision,
        "artifact": {
            "name": package.name,
            "digest": artifact_trust.file_digest(package),
            "size_bytes": package.stat().st_size,
        },
        "reproducibility": {
            "source_date_epoch": record["source_date_epoch"],
            "dpkg_deb": record.get("dpkg_deb", ""),
            "compression": record.get("compression", ""),
        },
        "state_schemas": manager_releases.implemented_state_schemas(),
    }
    # Parsing it back is the check. A manifest this project's own reader would
    # refuse must not be published and refused later on a device.
    parsed = manager_releases.parse_manifest(
        payload, release_id=release_id or release_id_for(record)
    )
    return parsed.release_id, payload


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("package", metavar="PACKAGE.deb")
    parser.add_argument("--revision", required=True, help="the commit this package was built at")
    parser.add_argument(
        "--created-at", default="", help="ISO 8601 UTC; defaults to the build's own timestamp"
    )
    parser.add_argument("--release-id", default="", help="override the derived identifier")
    parser.add_argument("--output", default="-", help="a directory to write into, or - for stdout")
    args = parser.parse_args(argv)

    if not REVISION.match(args.revision):
        raise SystemExit("--revision must be a full 40-character commit hash")

    created_at = args.created_at
    if not created_at:
        import datetime

        epoch = int(build_record(Path(args.package))["source_date_epoch"])
        created_at = (
            datetime.datetime.fromtimestamp(epoch, datetime.timezone.utc)
            .strftime("%Y-%m-%dT%H:%M:%SZ")
        )

    try:
        release_id, payload = manifest_for(
            args.package,
            revision=args.revision,
            created_at=created_at,
            release_id=args.release_id,
        )
    except manager_releases.ManagerReleaseError as exc:
        raise SystemExit(f"the manifest this run produced would be refused: {exc.message}")

    text = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    if args.output == "-":
        sys.stdout.write(text)
        return 0
    target = Path(args.output) / f"{release_id}.manifest.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    print(f"wrote {target}", file=sys.stderr)
    print("sign it before publishing:", file=sys.stderr)
    print(f"  gpg --armor --detach-sign {target}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
