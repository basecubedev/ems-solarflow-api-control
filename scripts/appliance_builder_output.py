#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""What a builder guest produced, and whether the host really received it.

    scripts/appliance_builder_output.py describe --dist DIR --output FILE
    scripts/appliance_builder_output.py verify --manifest FILE --directory DIR

The builder copied its output back with ``scp ... || true`` and then reported
PASS. A slirp link that dropped the 16 GiB image, a full host disk or a typo in
a path all produced the same verdict as a complete build, and the evidence a
release is signed from was the thing most likely to be missing.

So the guest describes its own output first: every file, its size, its SHA-256,
and whether a release needs it. The host copies into a staging directory,
re-hashes what arrived, and only then moves it into place. A required file that
is missing or does not hash to what the guest recorded is a failure, never a
pass with a gap in it.

Logs are the one optional class. They are useful and they are not evidence.

Exit status: 0 the output is complete, 1 it is not, 2 the command line is wrong.
"""

import argparse
import hashlib
import json
import sys
from pathlib import Path

SCHEMA_VERSION = 1

# A log helps a human read a failure; nothing is proven from one.
OPTIONAL_SUFFIXES = (".log",)

AUTHORITY_SUFFIX = ".build-authority.json"


def file_sha256(path, chunk=1024 * 1024):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(chunk), b""):
            digest.update(block)
    return f"sha256:{digest.hexdigest()}"


def is_required(relative):
    return not relative.endswith(OPTIONAL_SUFFIXES)


# The build's work root lives under the dist directory too — a chroot, the
# 16 GiB image and its sparse copies — and none of it is an artefact. What a
# release consists of is the top level plus the two report directories, which
# is exactly what the host collects.
COLLECTED_DIRECTORIES = ("gates", "reports")


def collectable(dist):
    dist = Path(dist)
    for path in sorted(dist.iterdir()):
        if path.is_file() and not path.is_symlink():
            yield path
    for name in COLLECTED_DIRECTORIES:
        directory = dist / name
        if not directory.is_dir():
            continue
        for path in sorted(directory.rglob("*")):
            if path.is_file() and not path.is_symlink():
                yield path


def collect_files(dist):
    dist = Path(dist)
    entries = []
    for path in collectable(dist):
        relative = path.relative_to(dist).as_posix()
        entries.append(
            {
                "path": relative,
                "size_bytes": path.stat().st_size,
                "sha256": file_sha256(path),
                "required": is_required(relative),
            }
        )
    return entries


def collect_builds(dist, entries):
    """One record per completed build, read from the authority it wrote."""

    by_path = {entry["path"]: entry for entry in entries}
    builds = []
    for entry in entries:
        if not entry["path"].endswith(AUTHORITY_SUFFIX):
            continue
        try:
            authority = json.loads((Path(dist) / entry["path"]).read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise SystemExit(f"{entry['path']} is not a readable build authority: {error}")
        prefix = entry["path"][: -len(AUTHORITY_SUFFIX)]
        image = f"{prefix}.img"
        update = f"{prefix}.update.tar.zst"
        builds.append(
            {
                "profile": authority.get("profile", ""),
                "build_id": authority.get("build_id", ""),
                "completed": bool(authority.get("completed")),
                "authority_file": entry["path"],
                "authority_sha256": entry["sha256"],
                "builder_environment_sha256": authority.get("builder_environment_sha256", ""),
                "image_file": image if image in by_path else "",
                "image_sha256": by_path.get(image, {}).get("sha256", ""),
                "update_file": update if update in by_path else "",
                "update_sha256": by_path.get(update, {}).get("sha256", ""),
            }
        )
    return sorted(builds, key=lambda build: (build["profile"], build["build_id"]))


def describe(args):
    dist = Path(args.dist)
    if not dist.is_dir():
        print(f"appliance-builder-output: {dist} is not a directory", file=sys.stderr)
        return 1
    entries = collect_files(dist)
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "dist": str(dist),
        "files": entries,
        "builds": collect_builds(dist, entries),
    }
    target = Path(args.output)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    required = sum(1 for entry in entries if entry["required"])
    print(f"described {len(entries)} file(s), {required} required, {len(manifest['builds'])} build(s)")
    return 0


def verify(args):
    try:
        manifest = json.loads(Path(args.manifest).read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        print(f"appliance-builder-output: the manifest is unreadable: {error}", file=sys.stderr)
        return 1
    if manifest.get("schema_version") != SCHEMA_VERSION:
        print(
            f"appliance-builder-output: manifest schema {manifest.get('schema_version')!r} "
            f"is not schema {SCHEMA_VERSION}",
            file=sys.stderr,
        )
        return 1

    directory = Path(args.directory)
    problems = []
    checked = 0
    for entry in manifest.get("files", []):
        required = bool(entry.get("required"))
        local = directory / entry["path"]
        if not local.is_file():
            if required:
                problems.append(f"missing: {entry['path']}")
            continue
        if local.stat().st_size != entry["size_bytes"]:
            problems.append(
                f"truncated: {entry['path']} is {local.stat().st_size} bytes, "
                f"the builder wrote {entry['size_bytes']}"
            )
            continue
        if file_sha256(local) != entry["sha256"]:
            problems.append(f"corrupt: {entry['path']} does not hash to what the builder wrote")
            continue
        checked += 1

    for build in manifest.get("builds", []):
        label = f"{build.get('profile', '?')}/{build.get('build_id', '?')}"
        if not build.get("completed"):
            problems.append(f"incomplete: the build authority for {label} is not completed")
        for role in ("authority_file", "image_file"):
            name = build.get(role) or ""
            if not name:
                problems.append(f"missing: {label} has no {role.replace('_', ' ')}")
            elif not (directory / name).is_file():
                problems.append(f"missing: {name} for {label}")

    for problem in problems:
        print(f"REJECTED  {problem}")
    print()
    print(f"verified: {checked} file(s)")
    print(f"builds:   {len(manifest.get('builds', []))}")
    if problems:
        print(f"RESULT: FAIL (artifact_copy_failed, {len(problems)} problem(s))")
        return 1
    print("RESULT: PASS")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    described = sub.add_parser("describe", help="Write the guest output manifest.")
    described.add_argument("--dist", required=True)
    described.add_argument("--output", required=True)
    described.set_defaults(handler=describe)

    verified = sub.add_parser("verify", help="Check a copied directory against a manifest.")
    verified.add_argument("--manifest", required=True)
    verified.add_argument("--directory", required=True)
    verified.set_defaults(handler=verify)

    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    return args.handler(args)


if __name__ == "__main__":
    sys.exit(main())
