#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 basecubedev
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The source code of the third-party components in the EMS and Admin images, beside them on GHCR.

    scripts/image_sources.py collect --image REF [--image REF ...] --name NAME
                                     --revision SHA --repository-url URL --output DIR
    scripts/image_sources.py verify --image REF [--digest SHA256]

Every image this project pushes to GHCR is a copy of the Debian userland of
``python:3.14-slim`` (GPL and LGPL programs among it), of Python packages such
as ``zeroconf`` (LGPL) and ``certifi`` (MPL-2.0), and of Go programs with MPL
modules compiled in. Whoever offers those images has to offer their source
too, and offering it from the same place is what the GPL counts as
distributing it. So each workflow that pushes the images pushes their source
to the same registry, as ``ghcr.io/basecubedev/ems-solarflow-sources`` under
the same tags.

``collect`` reads the images as they were pushed, never the build's
intentions: for every platform of every image it walks the layers and takes
the dpkg database, the apt sources, the metadata of each installed Python
distribution and the notices of each Go program. It then fetches, verifies and
writes:

- every Debian source package in exactly the installed version, with the
  sources they were built with, through the same code as the appliance image
  (``appliance_image_sources.collect``);
- the sdist of every Python distribution in exactly the installed version,
  checked against the SHA-256 PyPI publishes for it; a distribution without
  an sdist stops the run unless ``WITHOUT_SDIST`` names it with the reason its
  license asks for no source;
- the zip of every Go module a program's notices list whose license files at
  the module proxy are copyleft, as the proxy serves it and checked against
  the hash sum.golang.org records; every ELF program dpkg does not own and that
  is not CPython's has to be a Go program one of the image's notices files
  passes ``binary_notices.check`` for; in site-packages only a wheel's Python
  extension modules pass, and any other program or library a wheel carries
  has to be named in ``WHEEL_BINARIES``;
- the CPython release the interpreter in ``/usr/local`` was built from, as the
  base image's ``PYTHON_VERSION`` and ``PYTHON_SHA256`` name it, since its
  ``readline`` and ``gdbm`` modules link GPL libraries; an image that names no
  CPython build stops the run;

and a manifest naming every image, platform and digest the set covers, and
this repository at the images' revision for the project's own code. An image
of another revision than ``--revision``, a layer in a compression this script
cannot read, and anything that cannot be fetched or verified stop the run.

``verify`` asks the registry for an image without credentials, so a source
image nobody else can pull fails the workflow rather than passing it, and
with ``--digest`` refuses a tag that does not point at that digest. The
workflows push all three images by digest, untagged, and tag them only once
the source is pushed and verified, so no public tag names an image whose
source is not beside it.

Exit status: 0 written or reachable, 1 something could not be read, fetched or
verified, 2 the command line is wrong.
"""

import argparse
import concurrent.futures
import gzip
import hashlib
import http.client
import io
import json
import re
import sys
import tarfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts import appliance_image_sources as debian
from scripts import binary_notices as notices

REGISTRY = "ghcr.io"
PYPI = "https://pypi.org/pypi"
CPYTHON = "https://www.python.org/ftp/python/{folder}/Python-{version}.tar.xz"
NAME = "ems-solarflow-images"
WITHOUT_SDIST = {}
WHEEL_BINARIES = {}
STATIC_C_LIBRARIES = {"musl": (b"No error information", "MUSL.COPYRIGHT")}
ATTEMPTS = 4
WORKERS = 8
ACCEPT = ", ".join((
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
    "application/vnd.docker.distribution.manifest.v2+json",
))
INDEXES = {"application/vnd.oci.image.index.v1+json", "application/vnd.docker.distribution.manifest.list.v2+json"}
READABLE_LAYERS = {
    "application/vnd.oci.image.layer.v1.tar",
    "application/vnd.oci.image.layer.v1.tar+gzip",
    "application/vnd.docker.image.rootfs.diff.tar",
    "application/vnd.docker.image.rootfs.diff.tar.gzip",
}
DPKG_STATUS = "var/lib/dpkg/status"
APT_SOURCES = ("etc/apt/sources.list", "etc/apt/sources.list.d/")
REVISION_LABEL = "org.opencontainers.image.revision"

_REFERENCE = re.compile(r"^(?P<registry>[^/]+)/(?P<repository>[a-z0-9._/-]+?)(?::(?P<tag>[\w][\w.-]{0,127}))?(?:@(?P<digest>sha256:[0-9a-f]{64}))?$")
_SITE_PACKAGES = re.compile(r"^usr/local/lib/python3[^/]*/site-packages/(?P<dist>[^/]+\.dist-info)/(?P<file>.+)$")
_DEBIAN_PACKAGES = re.compile(r"^usr/lib/python3[^/]*/dist-packages/")
_DISTRIBUTION = re.compile(r"\.(?:dist|egg)-info(?:/|$)|\.egg(?:/|$)")
_WHEEL = re.compile(r"(?:^|/)(?P<name>[^/-]+)-(?P<version>[^/-]+)(?:-\d[^/-]*)?-[^/-]+-[^/-]+-[^/-]+\.whl$")
_NOTICES = re.compile(r"^usr/share/doc/(?P<component>[^/]+)/" + re.escape(notices.NOTICES) + "$")
_DPKG_LIST = re.compile(r"^var/lib/dpkg/info/[^/]+\.list$")
_DPKG_SUMS = re.compile(r"^var/lib/dpkg/info/[^/]+\.md5sums$")
_STATIC_NOTICE = re.compile(r"^usr/share/doc/[^/]+/MUSL\.COPYRIGHT$")
_CPYTHON_FILES = re.compile(r"^usr/local/(?:lib/python3[^/]*/(?!site-packages/)|lib/libpython3|bin/python3)")
_SITE_FILE = re.compile(r"^usr/local/lib/python3[^/]*/site-packages/(?P<inside>.+)$")
_BUNDLED = re.compile(r"(?:^|/)[^/]*\.libs/")
DPKG_DIVERSIONS = "var/lib/dpkg/diversions"
_MERGED_USR = ("bin/", "sbin/", "lib/", "lib32/", "lib64/")
ELF = b"\x7fELF"
_FIELD = re.compile(r"^([A-Za-z][\w-]*): ?(.*)$")


class ImageError(Exception):
    """An image that cannot be read, or a source that cannot be fetched or verified."""


def parse_reference(reference):
    """``(registry, repository, tag or digest)`` of an image reference; a digest wins over a tag."""

    match = _REFERENCE.match(reference)
    if not match or not (match.group("tag") or match.group("digest")):
        raise ImageError(f"{reference}: not a registry/repository:tag or registry/repository@sha256 reference")
    return match.group("registry"), match.group("repository"), match.group("digest") or match.group("tag")


class Registry:
    """Anonymous reads from one repository of a registry, retried, the token never sent past a redirect."""

    def __init__(self, registry, repository, attempts=ATTEMPTS, pause=2.0):
        if registry != REGISTRY:
            raise ImageError(f"{registry}: only {REGISTRY} is read")
        self.registry, self.repository = registry, repository
        self.attempts, self.pause = attempts, pause
        self.token = None

    def _open(self, url, accept=None, missing_is_final=True):
        failure = None
        for attempt in range(self.attempts):
            request = urllib.request.Request(url, headers={"User-Agent": "ems-image-sources", **({"Accept": accept} if accept else {})})
            if self.token:
                request.add_unredirected_header("Authorization", f"Bearer {self.token}")
            try:
                with urllib.request.urlopen(request, timeout=120) as response:
                    return response.read()
            except urllib.error.HTTPError as error:
                if error.code in (401, 403) or (error.code == 404 and missing_is_final):
                    raise ImageError(f"{url}: {error.code} {error.reason}") from error
                failure = error
            except (http.client.HTTPException, OSError) as error:
                failure = error
            time.sleep(self.pause * (attempt + 1))
        raise ImageError(f"{url}: {failure}")

    def authenticate(self):
        scope = urllib.parse.quote(f"repository:{self.repository}:pull", safe=":/")
        answer = json.loads(self._open(f"https://{self.registry}/token?scope={scope}"))
        self.token = answer.get("token") or answer.get("access_token")
        if not self.token:
            raise ImageError(f"{self.registry}/{self.repository}: the registry issued no token for anonymous pulls")
        return self

    def manifest_bytes(self, reference):
        """A manifest or index as served, asked for again while a just-pushed one is not yet; checked against a digest."""

        data = self._open(f"https://{self.registry}/v2/{self.repository}/manifests/{reference}", ACCEPT, missing_is_final=False)
        if reference.startswith("sha256:") and "sha256:" + hashlib.sha256(data).hexdigest() != reference:
            raise ImageError(f"{self.repository}: manifest {reference} does not hash to its digest")
        return data

    def manifest(self, reference):
        return json.loads(self.manifest_bytes(reference))

    def blob(self, digest):
        data = self._open(f"https://{self.registry}/v2/{self.repository}/blobs/{digest}")
        if "sha256:" + hashlib.sha256(data).hexdigest() != digest:
            raise ImageError(f"{self.repository}: blob {digest} does not hash to its digest")
        return data


def platforms(registry, reference):
    """``[(platform, manifest digest)]`` of an image, leaving out attestation manifests."""

    manifest = registry.manifest(reference)
    if manifest.get("mediaType") in INDEXES or "manifests" in manifest:
        found = []
        for entry in manifest.get("manifests", []):
            platform = entry.get("platform", {})
            if (entry.get("annotations") or {}).get("vnd.docker.reference.type") == "attestation-manifest":
                continue
            if not platform.get("os") or not platform.get("architecture") or "unknown" in (platform["os"], platform["architecture"]):
                raise ImageError(f"{registry.repository}@{reference}: an index entry {entry.get('digest')} names no platform")
            name = f"{platform['os']}/{platform['architecture']}" + (f"/{platform['variant']}" if platform.get("variant") else "")
            found.append((name, entry["digest"]))
        if not found:
            raise ImageError(f"{registry.repository}@{reference}: the index names no platform image")
        return found
    if not reference.startswith("sha256:"):
        raise ImageError(f"{registry.repository}:{reference}: a single-platform image must be named by its digest")
    config = json.loads(registry.blob(manifest["config"]["digest"]))
    return [(f"{config['os']}/{config['architecture']}", reference)]


def wanted(path):
    """Whether a file in an image is one ``collect`` reads."""

    return (
        path == DPKG_STATUS
        or path == APT_SOURCES[0]
        or (path.startswith(APT_SOURCES[1]) and path.endswith((".list", ".sources")))
        or bool(_SITE_PACKAGES.match(path))
        or bool(_NOTICES.match(path))
        or bool(_DPKG_LIST.match(path))
        or bool(_DPKG_SUMS.match(path))
        or bool(_STATIC_NOTICE.match(path))
        or path == DPKG_DIVERSIONS
        or bool(_WHEEL.search(path))
        or (bool(_DISTRIBUTION.search(path)) and not _DEBIAN_PACKAGES.match(path))
    )


def _member_path(name):
    return name[2:] if name.startswith("./") else name.lstrip("/")


def apply_layer(files, layer, programs=None):
    """Lay one layer tar over the files read so far, whiteouts first, as an overlay filesystem does.

    ``programs`` collects every ELF file outside CPython's tree, by path, so
    that ``foreign_programs`` can account for each one.
    """

    raw = gzip.decompress(layer) if layer[:2] == b"\x1f\x8b" else layer
    tracked = [files] + ([programs] if programs is not None else [])
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:") as archive:
        members = archive.getmembers()
        for member in members:
            path = _member_path(member.name)
            folder, _, base = path.rpartition("/")
            prefix = f"{folder}/" if folder else ""
            for found in tracked:
                if base == ".wh..wh..opq":
                    for name in [name for name in found if name.startswith(prefix)]:
                        del found[name]
                elif base.startswith(".wh."):
                    gone = prefix + base[len(".wh."):]
                    for name in [name for name in found if name == gone or name.startswith(gone + "/")]:
                        del found[name]
        for member in members:
            path = _member_path(member.name)
            if path.rpartition("/")[2].startswith(".wh."):
                continue
            regular = member.isfile() or member.islnk()
            if regular and wanted(path):
                files[path] = archive.extractfile(member).read()
            else:
                files.pop(path, None)
            if programs is not None:
                if regular and not _CPYTHON_FILES.match(path) and archive.extractfile(member).read(len(ELF)) == ELF:
                    programs[path] = archive.extractfile(member).read()
                else:
                    programs.pop(path, None)
    return files


def image_files(registry, digest):
    """The image config and the files ``collect`` reads, from every layer of one platform image."""

    manifest = registry.manifest(digest)
    for layer in manifest.get("layers", []):
        if layer.get("mediaType") not in READABLE_LAYERS:
            raise ImageError(f"{registry.repository}@{digest}: layer {layer['digest']} is {layer.get('mediaType')}, which this script cannot read")
    config = json.loads(registry.blob(manifest["config"]["digest"]))
    with concurrent.futures.ThreadPoolExecutor(WORKERS) as pool:
        layers = list(pool.map(lambda layer: registry.blob(layer["digest"]), manifest.get("layers", [])))
    files, programs = {}, {}
    for layer in layers:
        apply_layer(files, layer, programs)
    return config, files, programs


def _spellings(name):
    yield name
    for old in _MERGED_USR:
        if name.startswith(old):
            yield "usr/" + name


def dpkg_owned(files):
    """``{path: {md5}}``: the checksums dpkg recorded for a file by the packages that list it, diversions followed.

    A package's checksum counts only where its own ``.list`` names the path:
    a file another package took over stays in the old owner's ``.md5sums``.
    """

    lists, sums = {}, {}
    for path, data in files.items():
        if _DPKG_LIST.match(path):
            lists[path[: -len(".list")]] = data.decode("utf-8", "replace")
        elif _DPKG_SUMS.match(path):
            sums[path[: -len(".md5sums")]] = data.decode("utf-8", "replace")
    owned = {}
    for stem, text in lists.items():
        listed = {spelling for line in text.splitlines() if line.strip().lstrip("/") for spelling in _spellings(line.strip().lstrip("/"))}
        for line in sums.get(stem, "").splitlines():
            digest, _, name = line.strip().partition("  ")
            for spelling in _spellings(name.lstrip("/")):
                if spelling in listed:
                    owned.setdefault(spelling, set()).add(digest)
    lines = files.get(DPKG_DIVERSIONS, b"").decode("utf-8", "replace").splitlines()
    for source, target in zip(lines[0::3], lines[1::3]):
        recorded = set().union(*(owned.get(spelling, set()) for spelling in _spellings(source.strip().lstrip("/"))))
        for spelling in _spellings(target.strip().lstrip("/")):
            owned.setdefault(spelling, set()).update(recorded)
    return owned


def foreign_programs(where, files, programs, go):
    """Refuse a program or library no one accounts for.

    It is accounted for when dpkg lists it with the checksum it has; when it is
    one of a wheel's Python extension modules (a ``.so`` defining ``PyInit_``
    outside a ``*.libs`` folder) or ``WHEEL_BINARIES`` names it; or when it is
    a Go program one of the image's notices files passes ``binary_notices.check``
    for. A Go program that links a C library in statically needs that library
    known and its notice beside the program's.
    """

    owned = dpkg_owned(files)
    for path, data in sorted(programs.items()):
        if hashlib.md5(data).hexdigest() in owned.get(path, ()):
            continue
        site = _SITE_FILE.match(path)
        if site:
            inside = site.group("inside")
            if any(inside == name or inside.startswith(name.rstrip("/") + "/") for name in WHEEL_BINARIES):
                continue
            if inside.endswith(".so") and not _BUNDLED.search(inside) and b"PyInit_" in data:
                continue
            raise ImageError(f"{where}: {path} is a program or library a wheel carries beside its own code, which WHEEL_BINARIES does not name")
        described = notices.describe_program(data)
        matched = [component for component, text in sorted(go.items())
                   if component in notices.COMPONENTS and described["kind"] == "go" and not notices.check_data(component, data, text, path)]
        if not matched:
            raise ImageError(f"{where}: {path} is a program that neither dpkg, CPython nor a Go notices file in the image accounts for")
        if described["settings"].get("CGO_ENABLED") == "1" and not described["interpreter"]:
            library = next((name for name, (marker, _) in STATIC_C_LIBRARIES.items() if marker in data), None)
            if library is None or notices._STATIC_GLIBC in data:
                raise ImageError(f"{where}: {path} links a C library in statically that the script has no rule for")
            notice = STATIC_C_LIBRARIES[library][1]
            if f"usr/share/doc/{matched[0]}/{notice}" not in files:
                raise ImageError(f"{where}: {path} links {library} in statically, and usr/share/doc/{matched[0]}/{notice} is missing")


def metadata_fields(text):
    """The header fields of a distribution's METADATA, a repeated field as a list."""

    fields, last = {}, None
    for line in text.split("\n\n", 1)[0].splitlines():
        match = _FIELD.match(line)
        if match:
            last = match.group(1).lower()
            fields.setdefault(last, []).append(match.group(2).strip())
        elif line[:1] in (" ", "\t") and last:
            fields[last][-1] += "\n" + line.strip()
    return fields


def _distribution(where, metadata, texts):
    fields = metadata_fields(metadata)
    if not fields.get("name") or not fields.get("version"):
        raise ImageError(f"{where}: METADATA names no Name and Version")
    declared = fields.get("license", []) + fields.get("license-expression", []) + fields.get("classifier", [])
    return {
        "name": fields["name"][0],
        "version": fields["version"][0],
        "copyleft": notices.copyleft({"METADATA": "\n".join(declared), **texts}),
    }


def wheel_distribution(path, data):
    """The distribution a wheel lying in an image holds, read from the wheel's own metadata."""

    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        names = archive.namelist()
        metadata = [name for name in names if re.fullmatch(r"[^/]+\.dist-info/METADATA", name)]
        if len(metadata) != 1:
            raise ImageError(f"{path}: a wheel with {len(metadata)} METADATA files")
        folder = metadata[0].rsplit("/", 1)[0] + "/"
        texts = {name: archive.read(name).decode("utf-8", "replace") for name in names
                 if name.startswith(folder) and notices.is_license_file(PurePosixPath(name).name)}
        return _distribution(path, archive.read(metadata[0]).decode("utf-8", "replace"), texts)


def python_distributions(files):
    """``[{name, version, copyleft}]`` of every Python distribution in an image, installed or lying in it as a wheel.

    Debian's own Python packages (``/usr/lib/python3/dist-packages``) are the
    dpkg database's; any other distribution outside ``/usr/local``'s
    site-packages is one this script does not know how to read, and stops it.
    """

    found, distributions = {}, []
    for path, data in sorted(files.items()):
        match = _SITE_PACKAGES.match(path)
        if match:
            entry = found.setdefault(match.group("dist"), {"texts": {}})
            if match.group("file") == "METADATA":
                entry["metadata"] = data.decode("utf-8", "replace")
            elif notices.is_license_file(PurePosixPath(match.group("file")).name):
                entry["texts"][match.group("file")] = data.decode("utf-8", "replace")
        elif _WHEEL.search(path):
            distributions.append(wheel_distribution(path, data))
        elif _DISTRIBUTION.search(path) and not _DEBIAN_PACKAGES.match(path):
            raise ImageError(f"{path}: a Python distribution outside /usr/local's site-packages, which this script does not read")
    for dist, entry in sorted(found.items()):
        if "metadata" not in entry:
            raise ImageError(f"{dist}: an installed distribution without METADATA")
        distributions.append(_distribution(dist, entry["metadata"], entry["texts"]))
    return distributions


def copyleft_modules(component, text, fetcher):
    """The modules a notices file lists whose own license files, as the module proxy serves them, are copyleft."""

    _, modules = notices.parse_notices(text)
    if not modules:
        raise ImageError(f"{component}: its notices list no module")
    licenses = notices.modules_licenses(sorted(modules), fetcher)
    return sorted(module for module, texts in licenses.items() if notices.copyleft(texts))


def describe(reference, fetcher_registry=Registry):
    """What one image holds, platform by platform, as ``collect`` needs it."""

    registry_name, repository, ref = parse_reference(reference)
    registry = fetcher_registry(registry_name, repository).authenticate()
    described = []
    for platform, digest in platforms(registry, ref):
        config, files, programs = image_files(registry, digest)
        if DPKG_STATUS not in files:
            raise ImageError(f"{reference} {platform}: the image has no dpkg database")
        go = {match.group("component"): data.decode("utf-8", "replace") for path, data in files.items() if (match := _NOTICES.match(path))}
        foreign_programs(f"{reference} {platform}", files, programs, go)
        environment = dict(item.partition("=")[::2] for item in (config.get("config") or {}).get("Env") or [])
        if not re.fullmatch(r"\d+\.\d+\.\d+\w*", environment.get("PYTHON_VERSION", "")) or not re.fullmatch(r"[0-9a-f]{64}", environment.get("PYTHON_SHA256", "")):
            raise ImageError(f"{reference} {platform}: the image names no CPython build (PYTHON_VERSION, PYTHON_SHA256)")
        apt = {path.rpartition("/")[2]: data.decode("utf-8", "replace") for path, data in files.items() if path.startswith(APT_SOURCES[0])}
        described.append({
            "image": f"{registry_name}/{repository}",
            "reference": ref,
            "platform": platform,
            "digest": digest,
            "revision": (config.get("config") or {}).get("Labels", {}).get(REVISION_LABEL, ""),
            "packages": debian.installed_packages(files[DPKG_STATUS].decode("utf-8", "replace")),
            "archives": [{"uri": uri, "suite": suite, "component": component} for uri, suite, component in debian.apt_archives(apt)],
            "python": python_distributions(files),
            "interpreter": {"version": environment["PYTHON_VERSION"], "sha256": environment["PYTHON_SHA256"]},
            "go": go,
        })
    return described


def sdist(name, version, fetcher):
    """``(filename, data, url)`` of a distribution's sdist on PyPI, verified against its SHA-256, or None."""

    answer = fetcher.get(f"{PYPI}/{urllib.parse.quote(name)}/{urllib.parse.quote(version)}/json")
    if answer is None:
        raise ImageError(f"{name} {version}: PyPI does not know this release")
    files = [entry for entry in json.loads(answer).get("urls", []) if entry.get("packagetype") == "sdist"]
    if not files:
        return None
    entry = sorted(files, key=lambda item: item["filename"])[0]
    data = fetcher.require(entry["url"])
    if hashlib.sha256(data).hexdigest() != entry["digests"]["sha256"]:
        raise ImageError(f"{entry['url']}: SHA-256 does not match what PyPI publishes")
    return PurePosixPath(entry["filename"]).name, data, entry["url"]


def cpython(version, sha256, fetcher):
    """``(filename, data, url)`` of a CPython release tarball, refused unless it is the SHA-256 the image names."""

    url = CPYTHON.format(folder=re.match(r"\d+\.\d+\.\d+", version).group(0), version=version)
    data = fetcher.require(url)
    if hashlib.sha256(data).hexdigest() != sha256:
        raise ImageError(f"{url}: SHA-256 does not match the {sha256} the image was built from")
    return PurePosixPath(url).name, data, url


def collect(images, *, name, revision, repository_url, output, fetcher, debian_fetcher, part_limit=debian.PART_LIMIT):
    """Fetch, verify and write the source of every image; returns the manifest."""

    if not images:
        raise ImageError("no image to collect the source of")
    others = sorted({f"{image['image']} {image['platform']} is {image['revision'] or 'of no revision'}"
                     for image in images if image["revision"] != revision})
    if others:
        raise ImageError(f"the images are not all of {revision}: " + "; ".join(others))
    docker = sorted({package["package"] for image in images for package in image["packages"] if package["source"] in debian.NO_SOURCE_DUTY})
    if docker:
        raise ImageError(f"the images install {', '.join(docker)}, whose programs' module sources this script does not collect")
    requests = [{
        "schema_version": debian.SCHEMA_VERSION,
        "board": f"{image['image']} {image['platform']}",
        "release_version": name,
        "archives": image["archives"],
        "packages": image["packages"],
        "programs": [],
    } for image in images]
    distributions = {}
    for image in images:
        for distribution in image["python"]:
            key = (distribution["name"], distribution["version"])
            distributions[key] = distributions.get(key, False) or distribution["copyleft"]
    notices_files = {(component, text) for image in images for component, text in image["go"].items()}
    modules = sorted({module for component, text in sorted(notices_files) for module in copyleft_modules(component, text, fetcher)})
    builds = {}
    for image in images:
        version, digest = image["interpreter"]["version"], image["interpreter"]["sha256"]
        if builds.setdefault(version, digest) != digest:
            raise ImageError(f"CPython {version} is named with two SHA-256 values: {builds[version]} and {digest}")
    interpreters = [cpython(version, digest, fetcher) + (version, digest) for version, digest in sorted(builds.items())]
    sdists, python = {}, []
    for (distribution, version), owed in sorted(distributions.items()):
        found = sdist(distribution, version, fetcher)
        if found is None and (owed or distribution not in WITHOUT_SDIST):
            raise ImageError(f"{distribution} {version}: PyPI publishes no sdist of it, and WITHOUT_SDIST does not say why none is owed")
        record = {"name": distribution, "version": version, "copyleft": owed}
        if found:
            sdists[found[0]] = found[1]
            record |= {"file": f"python/{found[0]}", "sha256": hashlib.sha256(found[1]).hexdigest(), "url": found[2]}
        else:
            record |= {"file": None, "without_sdist": WITHOUT_SDIST[distribution]}
        python.append(record)
    with concurrent.futures.ThreadPoolExecutor(WORKERS) as pool:
        zips = dict(zip(modules, pool.map(lambda module: notices.module_source(*module, fetcher), modules)))
    manifest = debian.collect(requests, revision=revision, repository_url=repository_url, output=output,
                              fetcher=debian_fetcher, part_limit=part_limit, name=NAME)
    (output / "python").mkdir(exist_ok=True)
    for filename, data in sdists.items():
        (output / "python" / filename).write_bytes(data)
    interpreter = []
    for filename, data, url, version, digest in interpreters:
        (output / "python" / filename).write_bytes(data)
        interpreter.append({"version": version, "sha256": digest, "file": f"python/{filename}", "url": url})
    (output / "go").mkdir(exist_ok=True)
    go = []
    for (path, version), data in zips.items():
        target = output / "go" / f"{path}@{version}.zip"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        go.append({"module": path, "version": version, "file": f"go/{path}@{version}.zip", "h1": notices.zip_hash(data),
                   "url": f"{notices.PROXY}/{notices.escape_module(path)}/@v/{notices.escape_module(version)}.zip"})
    manifest |= {
        "images": [{key: image[key] for key in ("image", "reference", "platform", "digest", "revision")} for image in images],
        "python": python,
        "interpreter": interpreter,
        "go_modules": go,
        "project": [{"source": "this repository", "where": f"{repository_url}/tree/{revision}"}],
    }
    (output / f"{NAME}-{name}-sources.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return manifest


def verify(reference, digest=None, fetcher_registry=Registry):
    """Refuse unless the registry serves this image to someone without credentials, and as ``digest`` when given."""

    registry_name, repository, ref = parse_reference(reference)
    registry = fetcher_registry(registry_name, repository)
    attempts, pause = getattr(registry, "attempts", 1), getattr(registry, "pause", 0)
    for attempt in range(attempts):
        try:
            data = registry.authenticate().manifest_bytes(ref)
        except ImageError as error:
            raise ImageError(f"{reference} cannot be pulled without credentials ({error}); make the package public") from error
        served = "sha256:" + hashlib.sha256(data).hexdigest()
        if not digest or served == digest:
            return
        time.sleep(pause * (attempt + 1))
    raise ImageError(f"{reference} is {served}, not {digest}")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    collecting = commands.add_parser("collect", help="Write the source of every third-party component of the images.")
    collecting.add_argument("--image", action="append", required=True)
    collecting.add_argument("--name", required=True)
    collecting.add_argument("--revision", required=True)
    collecting.add_argument("--repository-url", required=True)
    collecting.add_argument("--output", required=True)
    verifying = commands.add_parser("verify", help="Refuse an image nobody can pull without credentials.")
    verifying.add_argument("--image", required=True)
    verifying.add_argument("--digest", help="The digest the image must be served as.")
    args = parser.parse_args(argv)
    try:
        if args.command == "verify":
            verify(args.image, args.digest)
            print(f"{args.image}: anyone can pull it" + (f", as {args.digest}" if args.digest else ""))
            return 0
        output = Path(args.output)
        if output.exists() and any(output.iterdir()):
            raise ImageError(f"{output} is not empty")
        output.mkdir(parents=True, exist_ok=True)
        images = [image for reference in args.image for image in describe(reference)]
        manifest = collect(images, name=args.name, revision=args.revision, repository_url=args.repository_url.rstrip("/"),
                           output=output, fetcher=notices.Fetcher(), debian_fetcher=debian.Fetcher())
        total = sum(part["size"] for part in manifest["parts"])
        print(
            f"{len(manifest['images'])} image platforms: {len(manifest['sources'])} Debian sources in "
            f"{len(manifest['parts'])} part(s), {total / 1e6:.0f} MB; {sum(1 for item in manifest['python'] if item['file'])} "
            f"Python sdists; CPython {', '.join(item['version'] for item in manifest['interpreter'])}; "
            f"{len(manifest['go_modules'])} copyleft Go modules"
        )
    except (ImageError, debian.SourceError, notices.NoticeError) as error:
        print(f"image-sources: {error}", file=sys.stderr)
        return 1
    except (OSError, ValueError, KeyError) as error:
        print(f"image-sources: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
