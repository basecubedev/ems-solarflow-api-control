#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 basecubedev
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The source code of everything an appliance image is built from, beside it.

    scripts/appliance_image_sources.py list --image IMG --board BOARD --output FILE
    scripts/appliance_image_sources.py collect --request FILE [--request FILE ...]
                                               --revision SHA --repository-url URL
                                               --output DIR [--part-limit BYTES]

An image carries the Linux kernel and some three hundred Debian and Raspberry Pi
packages, many under the GPL or LGPL. Whoever offers that image for download
has to offer the source of exactly those versions, and offering it from the
same place as the image is what the GPL counts as distributing it. So the
workflow that publishes an image publishes its source in the same release.

``list`` reads the image itself, never the build's intentions: the dpkg
database names every installed package with its source package and version,
and the apt sources say which archives they came from. ``collect`` joins the
lists of every board, fetches each source package in exactly that version,
verifies every file, and packs them into tar archives that stay under GitHub's
per-file limit, with a manifest that says what is where.

A source is taken from the archive's current index when it is still listed,
from its ``.dsc`` in the archive's pool when the index has moved on, and for
Debian from snapshot.debian.org. Every file is checked against the index, the
``.dsc`` that lists it or snapshot.debian.org's SHA-1; a ``.dsc`` taken from a
pool is trusted as the archive serves it over HTTPS. The sources a package was
built with (``Built-Using``, ``Static-Built-Using``) are collected like its
own. Two kinds of package have no source to fetch: this project's own (its
source is this repository at the Manager's release tag), and Docker's, whose
repository publishes none and whose own license (Apache-2.0) asks for none;
the copyleft Go modules and the GNU C Library in their programs are the
exceptions, which THIRD_PARTY_LICENSES.md describes.
Docker's packages ship no notices for what their programs are built from
either, so ``list`` also records what
each of their programs says it is (its Go build information, or the banner of
``docker-init``), and ``scripts/binary_notices.py appliance`` writes their
license notices beside the image from that.
Anything else that cannot be fetched and verified, a source without a ``.dsc``
and a tarball, and a package dpkg holds in a state that is neither installed nor
removed fail the run.

Exit status: 0 the request or the source set was written, 1 something could not
be read, fetched or verified, 2 the command line is wrong.
"""

import argparse
import gzip
import hashlib
import http.client
import json
import lzma
import re
import shutil
import sys
import tarfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path, PurePosixPath

SCHEMA_VERSION = 3
ROOT = Path(__file__).resolve().parents[1]

DPKG_STATUS = "/var/lib/dpkg/status"
DPKG_INFO = "/var/lib/dpkg/info"
ELF = b"\x7fELF"
APT_SOURCES_LIST = "/etc/apt/sources.list"
APT_SOURCES_DIR = "/etc/apt/sources.list.d"
BUILD_MARKER = "/etc/ems-appliance-os-build"
GONE = {"not-installed", "config-files"}

PROJECT_PACKAGES = {"ems-appliance-manager"}
PROJECT_TAG_PREFIX = "appliance-manager-v"
NO_SOURCE_DUTY = {
    "docker-ce": ("Apache-2.0", "https://github.com/moby/moby", "@docker.com"),
    "docker-buildx-plugin": ("Apache-2.0", "https://github.com/docker/buildx", "@docker.com"),
    "docker-compose-plugin": ("Apache-2.0", "https://github.com/docker/compose", "@docker.com"),
    "containerd.io": ("Apache-2.0", "https://github.com/containerd/containerd", "@containerd.io"),
}
SNAPSHOT = "https://snapshot.debian.org"
DEBIAN_HOSTS = {"deb.debian.org", "security.debian.org", "ftp.debian.org"}
INDEX_NAMES = ("Sources.xz", "Sources.gz", "Sources")
PART_LIMIT = 1_900_000_000
TAR_BLOCK = 512
TAR_TRAILER = 2 * TAR_BLOCK + 20 * TAR_BLOCK
ATTEMPTS = 4

_SOURCE_FIELD = re.compile(r"^(\S+)(?:\s+\((\S+)\))?$")
_BUILT_USING = re.compile(r"([a-z0-9][a-z0-9+.-]*)\s*\(\s*=\s*([^)\s]+)\s*\)")
_SIGNED = "-----BEGIN PGP SIGNED MESSAGE-----"
_SIGNATURE = "-----BEGIN PGP SIGNATURE-----"
_BINNMU = re.compile(r"\+b\d+$")


class SourceError(Exception):
    """A source that cannot be read, fetched or verified."""


class MissingFile(SourceError):
    """A file an index or a ``.dsc`` lists that the archive no longer serves."""


def stanzas(text):
    """deb822 paragraphs as dicts; a field's continuation lines stay in its value."""

    found = []
    for block in re.split(r"\n\s*\n", text):
        fields, key = {}, None
        for line in block.splitlines():
            if line.startswith("#"):
                continue
            if line[:1] in (" ", "\t") and key:
                fields[key] += "\n" + line.strip()
                continue
            key, separator, value = line.partition(":")
            if not separator:
                key = None
                continue
            key = key.strip()
            fields[key] = value.strip()
        if fields:
            found.append(fields)
    return found


def source_of(fields):
    """dpkg's ``${source:Package}`` and ``${source:Version}`` for one status stanza."""

    match = _SOURCE_FIELD.match(fields.get("Source", "").strip() or fields["Package"])
    if not match:
        raise SourceError(f"{fields['Package']}: unreadable Source field {fields.get('Source')!r}")
    return match.group(1), match.group(2) or fields["Version"]


def built_using(fields):
    """The ``(source, version)`` pairs a binary was built with, from both Built-Using fields."""

    found = set()
    for name in ("Built-Using", "Static-Built-Using"):
        found.update(_BUILT_USING.findall(fields.get(name, "")))
    return sorted(found)


def installed_packages(status_text):
    packages = []
    for fields in stanzas(status_text):
        if "Package" not in fields:
            continue
        status = fields.get("Status", "").split()
        if len(status) == 3 and status[2] in GONE:
            continue
        if len(status) != 3 or status[1] != "ok" or status[2] != "installed":
            raise SourceError(f"{fields['Package']} is '{fields.get('Status', '')}' in the image, not installed")
        source, source_version = source_of(fields)
        packages.append({
            "package": fields["Package"],
            "version": fields["Version"],
            "architecture": fields.get("Architecture", ""),
            "maintainer": fields.get("Maintainer", ""),
            "source": source,
            "source_version": source_version,
            "built_using": [list(pair) for pair in built_using(fields)],
        })
    return sorted(packages, key=lambda item: (item["package"], item["architecture"]))


def apt_archives(files):
    """``(uri, suite, component)`` of every binary archive in ``{name: text}`` apt files."""

    archives = set()
    for name, text in sorted(files.items()):
        if name.endswith(".sources"):
            for fields in stanzas(text):
                if fields.get("Enabled", "yes").lower() == "no" or "deb" not in fields.get("Types", "").split():
                    continue
                for uri in fields.get("URIs", "").split():
                    for suite in fields.get("Suites", "").split():
                        for component in fields.get("Components", "").split():
                            archives.add((uri.rstrip("/"), suite, component))
        else:
            for line in text.splitlines():
                words = re.sub(r"\[[^\]]*\]", " ", line.split("#", 1)[0]).split()
                if len(words) >= 4 and words[0] == "deb":
                    for component in words[3:]:
                        archives.add((words[1].rstrip("/"), words[2], component))
    return sorted(archives)


def programs_of(reader, packages):
    """Every ELF program of the packages whose source nobody publishes, with what it says it is."""

    sys.path.insert(0, str(ROOT))
    from scripts import binary_notices

    found = []
    for item in packages:
        if item["source"] not in NO_SOURCE_DUTY:
            continue
        names = (f"{DPKG_INFO}/{item['package']}:{item['architecture']}.list", f"{DPKG_INFO}/{item['package']}.list")
        listing = next((name for name in names if reader.exists(name)), None)
        if listing is None:
            raise SourceError(f"{item['package']}: the image's dpkg database lists none of its files")
        for path in reader.read_text(listing).splitlines():
            if not path.strip() or not reader.is_file(path):
                continue
            data = reader.read_bytes(path)
            if not data.startswith(ELF):
                continue
            found.append({
                "package": item["package"],
                "version": item["version"],
                "path": path,
                "size": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
                **binary_notices.describe_program(data),
            })
    return sorted(found, key=lambda program: program["path"])


def request_from_root(reader, board):
    """The source request of one board, read from its root filesystem."""

    files = {}
    if reader.exists(APT_SOURCES_LIST):
        files["sources.list"] = reader.read_text(APT_SOURCES_LIST)
    if reader.exists(APT_SOURCES_DIR):
        for name in reader.listdir(APT_SOURCES_DIR):
            if name.endswith((".sources", ".list")):
                files[name] = reader.read_text(f"{APT_SOURCES_DIR}/{name}")
    marker = json.loads(reader.read_text(BUILD_MARKER)) if reader.exists(BUILD_MARKER) else {}
    packages = installed_packages(reader.read_text(DPKG_STATUS))
    if not packages:
        raise SourceError(f"{board}: the image's dpkg database lists no installed package")
    archives = apt_archives(files)
    if not archives:
        raise SourceError(f"{board}: the image names no apt archive")
    return {
        "schema_version": SCHEMA_VERSION,
        "board": board,
        "release_version": marker.get("release_version", ""),
        "build_id": marker.get("build_id", ""),
        "archives": [{"uri": uri, "suite": suite, "component": component} for uri, suite, component in archives],
        "packages": packages,
        "programs": programs_of(reader, packages),
    }


def open_root(image_path):
    sys.path.insert(0, str(ROOT))
    from appliance import image_filesystems, image_inspect

    partitions = {item.label: item for item in image_inspect.read_mbr_partitions(image_path)}
    if "root" not in partitions:
        raise SourceError(f"{image_path}: the image has no root partition")
    return image_filesystems.open_partition(image_path, partitions["root"])


def https(uri):
    return "https://" + uri[len("http://"):] if uri.startswith("http://") else uri


class Fetcher:
    """HTTP with retries; ``get`` answers None for a missing file."""

    def __init__(self, attempts=ATTEMPTS, pause=2.0):
        self.attempts = attempts
        self.pause = pause

    def _open(self, url):
        request = urllib.request.Request(url, headers={"User-Agent": "ems-appliance-image-sources"})
        return urllib.request.urlopen(request, timeout=120)

    def get(self, url):
        for attempt in range(self.attempts):
            try:
                with self._open(url) as response:
                    return response.read()
            except urllib.error.HTTPError as error:
                if error.code == 404:
                    return None
                failure = error
            except (http.client.HTTPException, OSError) as error:
                failure = error
            time.sleep(self.pause * (attempt + 1))
        raise SourceError(f"{url}: {failure}")

    def download(self, url, target):
        for attempt in range(self.attempts):
            try:
                with self._open(url) as response, target.open("wb") as handle:
                    shutil.copyfileobj(response, handle, 1024 * 1024)
                return
            except urllib.error.HTTPError as error:
                if error.code == 404:
                    raise MissingFile(f"{url}: not found") from error
                failure = error
            except (http.client.HTTPException, OSError) as error:
                failure = error
            time.sleep(self.pause * (attempt + 1))
        raise SourceError(f"{url}: {failure}")


def _checksums(field):
    files = []
    for line in field.splitlines():
        words = line.split()
        if len(words) == 3:
            files.append({"name": words[2], "size": int(words[1]), "sha256": words[0]})
    return files


def _decompress(name, data):
    if name.endswith(".xz"):
        return lzma.decompress(data)
    if name.endswith(".gz"):
        return gzip.decompress(data)
    return data


def load_indexes(archives, fetcher):
    """``{(source, version): candidate}`` and ``{source: [(base, directory)]}`` of every archive."""

    exact, directories = {}, {}
    for archive in archives:
        base = https(archive["uri"])
        for name in INDEX_NAMES:
            data = fetcher.get(f"{base}/dists/{archive['suite']}/{archive['component']}/source/{name}")
            if data is not None:
                break
        else:
            continue
        for fields in stanzas(_decompress(name, data).decode("utf-8", "replace")):
            if "Package" not in fields or "Directory" not in fields:
                continue
            candidate = {"base": base, "directory": fields["Directory"], "files": _checksums(fields.get("Checksums-Sha256", ""))}
            exact.setdefault((fields["Package"], fields["Version"]), candidate)
            locations = directories.setdefault(fields["Package"], [])
            if (base, fields["Directory"]) not in locations:
                locations.append((base, fields["Directory"]))
    return exact, directories


def _upstream_version(version):
    return version.split(":", 1)[-1]


def dsc_fields(text):
    """The control fields of a ``.dsc``, with the armour of a clear-signed one taken off."""

    if _SIGNED in text:
        body = text.split(_SIGNED, 1)[1].split("\n\n", 1)[-1].split(_SIGNATURE, 1)[0]
        text = "\n".join(line[2:] if line.startswith("- ") else line for line in body.splitlines())
    return next((fields for fields in stanzas(text) if "Checksums-Sha256" in fields), {})


def _from_pool(source, version, directories, fetcher):
    name = f"{source}_{_upstream_version(version)}.dsc"
    for base, directory in directories.get(source, []):
        data = fetcher.get(f"{base}/{directory}/{name}")
        if data is None:
            continue
        listed = _checksums(dsc_fields(data.decode("utf-8", "replace")).get("Checksums-Sha256", ""))
        if not listed:
            raise SourceError(f"{base}/{directory}/{name} lists no file")
        files = [{"name": name, "size": len(data), "sha256": hashlib.sha256(data).hexdigest()}] + listed
        return {"base": base, "directory": directory, "files": files, "verified_by": "dsc"}
    return None


def _from_snapshot(source, version, fetcher):
    quoted = "/".join(urllib.parse.quote(part, safe="") for part in (source, version))
    data = fetcher.get(f"{SNAPSHOT}/mr/package/{quoted}/srcfiles?fileinfo=1")
    if data is None:
        return None
    answer = json.loads(data)
    files, names = [], set()
    for digest, seen in sorted(answer.get("fileinfo", {}).items()):
        entry = next((item for item in seen if item.get("archive_name") == "debian"), seen[0])
        if entry["name"] in names:
            raise SourceError(f"snapshot.debian.org names two files {entry['name']} for {source} {version}")
        names.add(entry["name"])
        files.append({"name": entry["name"], "size": entry["size"], "sha1": digest, "url": f"{SNAPSHOT}/file/{digest}"})
    return {"base": SNAPSHOT, "directory": "", "files": files, "verified_by": "snapshot"} if files else None


def complete(record):
    """A source package is its ``.dsc`` and at least one tarball; anything less is not its source."""

    names = [entry["name"] for entry in record["files"]]
    if len(names) != len(set(names)):
        raise SourceError(f"{record['source']} {record['version']} lists one file name twice")
    if not any(name.endswith(".dsc") for name in names) or not any(".tar." in name or name.endswith(".tar") for name in names):
        raise SourceError(f"{record['source']} {record['version']}: no .dsc and tarball among {names}")
    return record


def resolve(needed, exact, directories, fetcher, archive_hosts):
    """Where each needed source comes from, or why none is owed; refuses the rest."""

    found, exempt, project, missing = [], [], [], []
    for (source, version), use in sorted(needed.items()):
        record = {"source": source, "version": version, "boards": sorted(use["boards"]), "binaries": sorted(use["binaries"])}
        if use["built_into"]:
            record["built_into"] = sorted(use["built_into"])
        candidate = exact.get((source, version)) or exact.get((source, _BINNMU.sub("", version)))
        if candidate:
            found.append(complete({**record, **candidate, "verified_by": "index"}))
        elif source in PROJECT_PACKAGES:
            project.append(record)
        elif source in NO_SOURCE_DUTY:
            license_id, upstream, maintainer = NO_SOURCE_DUTY[source]
            if not use["maintainers"] or not all(maintainer in item for item in use["maintainers"]):
                missing.append(f"{source} {version}: not maintained by {maintainer[1:]}")
                continue
            exempt.append({**record, "license": license_id, "upstream": upstream})
        else:
            candidate = _from_pool(source, version, directories, fetcher)
            if candidate is None and archive_hosts & DEBIAN_HOSTS:
                candidate = _from_snapshot(source, version, fetcher)
            if candidate is None:
                missing.append(f"{source} {version}")
            else:
                found.append(complete({**record, **candidate}))
    if missing:
        raise SourceError("no source could be found for: " + ", ".join(missing))
    return found, exempt, project


def origin_label(base):
    without = base.split("://", 1)[-1]
    return re.sub(r"[^A-Za-z0-9.-]+", "_", without).strip("_")


def fetch_files(found, cache, fetcher):
    """Download every file into ``cache`` and verify it; returns the verified records.

    A Debian archive drops a superseded version from its pool while a mirror
    may still serve the index or ``.dsc`` that listed it, so a file that has
    gone takes the whole source from snapshot.debian.org instead, and every
    file the index or ``.dsc`` listed has to be among snapshot's with the same
    size and SHA-256.
    """

    for record in found:
        listed = {entry["name"]: dict(entry) for entry in record["files"]}
        try:
            _fetch_record(record, cache, fetcher)
        except MissingFile:
            if record["verified_by"] == "snapshot" or record["base"].split("/")[2] not in DEBIAN_HOSTS:
                raise
            candidate = _from_snapshot(record["source"], record["version"], fetcher)
            if candidate is None:
                raise
            record.update(complete({**record, **candidate}))
            _fetch_record(record, cache, fetcher)
            fetched = {entry["name"]: entry for entry in record["files"]}
            for name, entry in listed.items():
                other = fetched.get(name)
                if other is None or other["size"] != entry["size"] or entry.get("sha256", other["sha256"]) != other["sha256"]:
                    raise SourceError(f"{record['source']} {record['version']}: snapshot.debian.org does not hold the {name} the archive listed")
    return found


def _fetch_record(record, cache, fetcher):
    folder = cache / origin_label(record["base"]) / f"{record['source']}_{_upstream_version(record['version'])}"
    folder.mkdir(parents=True, exist_ok=True)
    record["folder"] = folder
    for entry in record["files"]:
        target = folder / PurePosixPath(entry["name"]).name
        url = entry.pop("url", None) or f"{record['base']}/{record['directory']}/{entry['name']}"
        fetcher.download(url, target)
        data_size = target.stat().st_size
        sha256 = file_digest(target, "sha256")
        if data_size != entry["size"]:
            raise SourceError(f"{url}: {data_size} bytes, the {record['verified_by']} says {entry['size']}")
        expected = entry.get("sha256")
        if expected and sha256 != expected:
            raise SourceError(f"{url}: SHA-256 {sha256} does not match {expected}")
        if "sha1" in entry and file_digest(target, "sha1") != entry.pop("sha1"):
            raise SourceError(f"{url}: SHA-1 does not match snapshot.debian.org")
        entry["sha256"] = sha256
        entry["url"] = url


def file_digest(path, algorithm, chunk=1024 * 1024):
    digest = hashlib.new(algorithm)
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(chunk), b""):
            digest.update(block)
    return digest.hexdigest()


def _tar_size(size):
    """What one member costs in a GNU tar: a long-name header, its own header, padded data."""

    return 3 * TAR_BLOCK + -(-size // TAR_BLOCK) * TAR_BLOCK


def plan_parts(found, limit):
    """Sources in order, a new part whenever the next whole source would pass ``limit``."""

    parts, current, size = [], [], TAR_TRAILER
    for record in sorted(found, key=lambda item: (origin_label(item["base"]), item["source"], item["version"])):
        weight = sum(_tar_size(entry["size"]) for entry in record["files"])
        if weight + TAR_TRAILER > limit:
            raise SourceError(f"{record['source']} {record['version']} alone is larger than a part may be")
        if current and size + weight > limit:
            parts.append(current)
            current, size = [], TAR_TRAILER
        current.append(record)
        size += weight
    if current:
        parts.append(current)
    return parts


def _tar_entry(name, size):
    info = tarfile.TarInfo(name)
    info.size = size
    info.mtime = 0
    info.mode = 0o644
    info.uid = info.gid = 0
    info.uname = info.gname = ""
    return info


def write_parts(parts, output, prefix):
    written = []
    for index, records in enumerate(parts, start=1):
        name = f"{prefix}-sources-{index}-of-{len(parts)}.tar"
        path = output / name
        with tarfile.open(path, "w", format=tarfile.GNU_FORMAT) as archive:
            for record in records:
                folder = f"{origin_label(record['base'])}/{record['source']}_{_upstream_version(record['version'])}"
                for entry in sorted(record["files"], key=lambda item: item["name"]):
                    source = record["folder"] / PurePosixPath(entry["name"]).name
                    with source.open("rb") as handle:
                        archive.addfile(_tar_entry(f"{folder}/{PurePosixPath(entry['name']).name}", entry["size"]), handle)
                record["part"] = name
        written.append({"name": name, "size": path.stat().st_size, "sha256": file_digest(path, "sha256")})
    return written


def _use():
    return {"boards": set(), "binaries": set(), "maintainers": set(), "built_into": set()}


def project_where(record, repository_url, revision, tag_prefix, fetcher):
    """The Manager's release tag when that release is published, else the commit that built this image."""

    tag = f"{tag_prefix}{record['version']}"
    if fetcher.get(f"{repository_url}/releases/tag/{tag}") is not None:
        return record | {"where": f"{repository_url}/tree/{tag}"}
    return record | {"where": f"{repository_url}/tree/{revision}"}


def collect(requests, *, revision, repository_url, output, fetcher, part_limit=PART_LIMIT, cache=None,
            project_tag_prefix=PROJECT_TAG_PREFIX, name="ems-solarflow-appliance"):
    """Fetch, verify and pack the source of every request; returns the manifest.

    ``name`` and the requests' release version name the files, so the container
    images can collect into files of their own with the same machinery.
    """

    versions = {request.get("release_version", "") for request in requests}
    if len(versions) != 1 or not next(iter(versions)):
        raise SourceError(f"the boards do not name one release version: {sorted(versions)}")
    release_version = versions.pop()
    needed, archives = {}, {}
    for request in requests:
        if request.get("schema_version") != SCHEMA_VERSION:
            raise SourceError(f"{request.get('board')}: request schema {request.get('schema_version')!r}")
        for archive in request["archives"]:
            archives[(archive["uri"], archive["suite"], archive["component"])] = archive
        for package in request["packages"]:
            use = needed.setdefault((package["source"], package["source_version"]), _use())
            use["boards"].add(request["board"])
            use["binaries"].add(package["package"])
            use["maintainers"].add(package["maintainer"])
            for source, version in package.get("built_using", []):
                built = needed.setdefault((source, version), _use())
                built["boards"].add(request["board"])
                built["built_into"].add(package["package"])
    ordered = [archives[key] for key in sorted(archives)]
    exact, directories = load_indexes(ordered, fetcher)
    hosts = {https(archive["uri"]).split("/")[2] for archive in ordered}
    found, exempt, project = resolve(needed, exact, directories, fetcher, hosts)
    project = [project_where(record, repository_url, revision, project_tag_prefix, fetcher) for record in project]
    output.mkdir(parents=True, exist_ok=True)
    cache = cache or output / ".sources-cache"
    try:
        fetch_files(found, cache, fetcher)
        prefix = f"{name}-{release_version}"
        parts = write_parts(plan_parts(found, part_limit), output, prefix)
    finally:
        shutil.rmtree(cache, ignore_errors=True)
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "release_version": release_version,
        "revision": revision,
        "boards": sorted(request["board"] for request in requests),
        "parts": parts,
        "sources": [
            {
                key: record[key]
                for key in ("source", "version", "boards", "binaries", "built_into", "verified_by", "part")
                if key in record
            }
            | {"origin": record["base"], "files": record["files"]}
            for record in sorted(found, key=lambda item: (item["source"], item["version"]))
        ],
        "without_source_duty": exempt,
        "project": project,
    }
    (output / f"{prefix}-sources.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return manifest


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    listing = commands.add_parser("list", help="Write the source request of one image.")
    listing.add_argument("--image", required=True)
    listing.add_argument("--board", required=True)
    listing.add_argument("--output", required=True)
    collecting = commands.add_parser("collect", help="Fetch, verify and pack the source of every request.")
    collecting.add_argument("--request", action="append", required=True)
    collecting.add_argument("--revision", required=True)
    collecting.add_argument("--repository-url", required=True)
    collecting.add_argument("--output", required=True)
    collecting.add_argument("--part-limit", type=int, default=PART_LIMIT)
    collecting.add_argument("--project-tag-prefix", default=PROJECT_TAG_PREFIX)
    args = parser.parse_args(argv)
    try:
        if args.command == "list":
            request = request_from_root(open_root(args.image), args.board)
            Path(args.output).write_text(json.dumps(request, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            print(
                f"{args.board}: {len(request['packages'])} packages from {len(request['archives'])} archive(s), "
                f"{len(request['programs'])} Docker programs"
            )
        else:
            requests = [json.loads(Path(path).read_text(encoding="utf-8")) for path in args.request]
            manifest = collect(
                requests,
                revision=args.revision,
                repository_url=args.repository_url.rstrip("/"),
                output=Path(args.output),
                fetcher=Fetcher(),
                part_limit=args.part_limit,
                project_tag_prefix=args.project_tag_prefix,
            )
            total = sum(part["size"] for part in manifest["parts"])
            print(
                f"{len(manifest['sources'])} source packages in {len(manifest['parts'])} part(s), "
                f"{total / 1e9:.2f} GB; {len(manifest['without_source_duty'])} without a source duty, "
                f"{len(manifest['project'])} from this repository"
            )
    except SourceError as error:
        print(f"appliance-image-sources: {error}", file=sys.stderr)
        return 1
    except (OSError, ValueError, KeyError) as error:
        print(f"appliance-image-sources: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
