#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 basecubedev
# SPDX-License-Identifier: AGPL-3.0-or-later
"""License notices for the Go modules compiled into the binaries the images ship.

    scripts/binary_notices.py generate [--component NAME ...] [--revision SHA]
    scripts/binary_notices.py check --component NAME --binary PATH --notices FILE
    scripts/binary_notices.py appliance --request FILE [--request FILE ...] --output DIR

The EMS image copies the influx CLI out of influxdb, the Admin image downloads
the Docker CLI and the Compose plugin. Each is a static Go program with dozens
of modules compiled in, most under MIT, BSD or Apache-2.0, and those licenses
travel with every copy of the binary. ``third_party/<name>/LICENSE`` covers the
program itself; ``THIRD_PARTY_NOTICES`` beside it covers what is inside.

``generate`` writes that file from the upstream source of the pinned version:
the module list from ``go.mod`` (or ``vendor/modules.txt`` for a vendored
build), each module's license files from the Go module proxy (or the vendor
tree), and the Go standard library's own license. Identical texts are written
once with every module that uses them.

``check`` runs inside the image build. It reads the binary's Go build
information and refuses the image when the binary is another revision than the
notices were written for, or holds a module they do not name, so a moved
upstream tag cannot ship a binary with someone else's notices.

``appliance`` writes the notices of the Docker programs an appliance image
carries, beside the image in its release rather than inside it. Docker's Debian
packages ship no notices for what their programs are built from, so the texts
come from what each program says it is, as
``scripts/appliance_image_sources.py list`` read it off the image:
the modules of its Go build information from the module proxy, its own license
from the module or the tagged GitHub tree it was built from, and for a C
program the notices know (tini, as ``docker-init``) its tree and the C library
linked into it. A tree is refused unless it is the commit the program names,
and a program no rule describes, a build that reports modified sources, a Go
experiment or a sanitizer, C code linked in statically as the build
information shows it, and a glibc of a Debian release whose texts are not
pinned stop the run.

Exit status: 0 written or consistent, 1 something could not be fetched, read or
matched, 2 the command line is wrong.
"""

import argparse
import base64
import concurrent.futures
import hashlib
import http.client
import io
import json
import re
import struct
import sys
import tarfile
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
ADMIN_DOCKERFILE = ROOT / "deploy" / "admin" / "Dockerfile"
NOTICES = "THIRD_PARTY_NOTICES"
MODULES_HEADER = "Modules of the build (its go.mod or vendor/modules.txt, test-only modules included):"
PROXY = "https://proxy.golang.org"
RAW = "https://raw.githubusercontent.com"
CODELOAD = "https://codeload.github.com"
API = "https://api.github.com"
SUMDB = "https://sum.golang.org"
GO_LICENSE = ("golang/go", "go1.25.0")
ATTEMPTS = 4
BLOCK = 64 * 1024
WORKERS = 8

COMPONENTS = {
    "influx-cli": {
        "repository": "influxdata/influx-cli",
        "identity": "vcs.revision",
        "modules": "go.mod",
    },
    "docker-cli": {
        "repository": "docker/cli",
        "identity": "ldflag:github.com/docker/cli/cli/version.Version",
        "modules": "vendor",
        "version_arg": "DOCKER_CLI_VERSION",
    },
    "docker-compose": {
        "repository": "docker/compose",
        "identity": "vcs.revision",
        "modules": "go.mod",
        "version_arg": "DOCKER_COMPOSE_VERSION",
    },
}

SOURCE_TREES = (
    {
        "package": "github.com/moby/moby/v2/",
        "repository": "moby/moby",
        "tag": "docker-v{version}",
        "version": "github.com/moby/moby/v2/dockerversion.Version",
        "commit": "github.com/moby/moby/v2/dockerversion.GitCommit",
    },
    {
        "package": "github.com/docker/cli/",
        "repository": "docker/cli",
        "tag": "v{version}",
        "version": "github.com/docker/cli/cli/version.Version",
        "commit": "github.com/docker/cli/cli/version.GitCommit",
        "vendored": True,
    },
)
TINI_REPOSITORY = "krallin/tini"
GLIBC_URL = "https://sourceware.org/git/?p=glibc.git;a=blob_plain;f={name};hb=refs/tags/glibc-{version}"
GLIBC = {
    "13": (
        "2.41",
        {
            "COPYING.LIB": "dc626520dcd53a22f727af3ee42c770e56c97a64fe3adb063799d8ab032fe551",
            "LICENSES": "b22a69aa3f80a5201818c66cb0df0f25f9fa13cf5861b0093a058dbd12d50dce",
        },
    ),
}
APPLIANCE_NOTICES = "ems-solarflow-appliance-{version}-docker-notices.txt"
APPLIANCE_SOURCES = "ems-solarflow-appliance-{version}-docker-module-sources.tar"
GLIBC_KEY = "GNU C Library"

MAGIC = b"\xff Go buildinf:"
_TINI = re.compile(rb"tini version (\d+\.\d+\.\d+) - git\.([0-9a-f]{7,40})([^\x00\s]*)")
_COMMIT = re.compile(r"[0-9a-f]{7,40}")
PT_LOAD, PT_DYNAMIC, PT_INTERP = 1, 2, 3
DT_NULL, DT_NEEDED, DT_STRTAB = 0, 1, 5
CGO_BINDINGS = {"github.com/seccomp/libseccomp-golang": "libseccomp.so"}
_STATIC_GLIBC = b"Fatal glibc error"
_DEBIAN_RELEASE = re.compile(r"~debian\.(\d+)~")
_STATIC_LINK = re.compile(r"-static\b|-Bstatic\b|-l:|--whole-archive|\.a(?=$|[\s\"',])")
_SANITIZERS = ("-race", "-msan", "-asan")
_COPYLEFT = re.compile(
    r"Mozilla\s+Public\s+Licen[cs]e|GNU\s+(?:Lesser\s+|Library\s+|Affero\s+)?General\s+Public\s+Licen[cs]e"
    r"|Eclipse\s+Public\s+Licen[cs]e|Common\s+Development\s+and\s+Distribution\s+Licen[cs]e"
    r"|European\s+Union\s+Public\s+Licen[cs]e|Common\s+Public\s+Licen[cs]e|Open\s+Software\s+Licen[cs]e"
    r"|Microsoft\s+Reciprocal\s+Licen[cs]e|\b(?:MPL|L?GPL|AGPL|EPL|CDDL|EUPL|CPL|OSL)-\d|\bMS-RL\b",
    re.IGNORECASE,
)
_LICENSE_FILE = re.compile(r"licen[cs]e|copying|copyright|copyleft|notice|patents|legal", re.IGNORECASE)
_SOURCE_FILE = re.compile(
    r"\.(?:go|s|c|h|cc|cpp|hpp|m|sh|bash|py|js|mjs|ts|java|kt|rs|rb|pl|php|swift|proto|tmpl|tpl|mod|sum"
    r"|json|ya?ml|xml|toml|golden|pem|crt|key|png|jpe?g|gif|svg|ico|gz|zip|tar|bin|exe|so|a)$",
    re.IGNORECASE,
)
_HEADER = re.compile(r"^(\w[\w-]*): (.+)$")
_MODULE_LINE = re.compile(r"^  (\S+) (\S+)$")


class NoticeError(Exception):
    """A notice that cannot be written, or a binary that does not match its notices."""


def _varint(data, position):
    value = shift = 0
    while True:
        byte = data[position]
        position += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, position
        shift += 7


def _unquote(value):
    """A build setting Go wrote with ``strconv.Quote`` because it holds spaces or quotes."""

    if value.startswith('"') and value.endswith('"'):
        try:
            return json.loads(value)
        except ValueError:
            return value[1:-1]
    return value


def read_buildinfo(data):
    """``{"go", "main", "deps", "settings"}`` from a Go 1.18+ binary's build information."""

    at = data.find(MAGIC)
    if at < 0:
        raise NoticeError("the binary carries no Go build information")
    if not data[at + 15] & 2:
        raise NoticeError("the build information predates Go 1.18 and is not supported")
    position = at + 32
    fields = []
    for _ in range(2):
        length, position = _varint(data, position)
        fields.append(bytes(data[position:position + length]).decode("utf-8", "replace"))
        position += length
    go_version, modinfo = fields
    if len(modinfo) >= 33 and modinfo[-17] == "\n":
        modinfo = modinfo[16:-16]
    path, main, deps, settings = "", None, [], {}
    for line in modinfo.splitlines():
        words = line.split("\t")
        if words[0] == "path" and len(words) >= 2:
            path = words[1]
        elif words[0] == "mod" and len(words) >= 3:
            main = (words[1], words[2])
        elif words[0] == "dep" and len(words) >= 3:
            deps.append((words[1], words[2]))
        elif words[0] == "=>" and len(words) >= 3 and deps:
            deps[-1] = (words[1], words[2])
        elif words[0] == "build" and len(words) >= 2:
            key, _, value = words[1].partition("=")
            settings[key] = _unquote(value)
    return {"go": go_version, "path": path, "main": main, "deps": sorted(set(deps)), "settings": settings}


def _segments(data):
    """``(type, offset, address, size)`` of each ELF program header, and the byte order and width to read with."""

    order, wide = "<" if data[5] == 1 else ">", data[4] == 2
    if wide:
        (offset,) = struct.unpack_from(f"{order}Q", data, 0x20)
        size, count = struct.unpack_from(f"{order}HH", data, 0x36)
    else:
        (offset,) = struct.unpack_from(f"{order}I", data, 0x1C)
        size, count = struct.unpack_from(f"{order}HH", data, 0x2A)
    segments = []
    for index in range(count):
        at = offset + index * size
        if wide:
            kind, _, start, address, _, length = struct.unpack_from(f"{order}IIQQQQ", data, at)
        else:
            kind, start, address, _, length = struct.unpack_from(f"{order}IIIII", data, at)
        segments.append((kind, start, address, length))
    return segments, order, wide


def has_interpreter(data):
    """Whether an ELF program names a dynamic loader; one that does not carries its C library inside."""

    try:
        return any(kind == PT_INTERP for kind, *_ in _segments(data)[0])
    except (IndexError, struct.error):
        return False


def needed_libraries(data):
    """The shared libraries an ELF program asks the loader for (its DT_NEEDED entries)."""

    try:
        segments, order, wide = _segments(data)
        dynamic = next(((start, length) for kind, start, _, length in segments if kind == PT_DYNAMIC), None)
        if dynamic is None:
            return []
        entry = f"{order}qQ" if wide else f"{order}iI"
        width = struct.calcsize(entry)
        names, table = [], None
        for at in range(dynamic[0], dynamic[0] + dynamic[1] - width + 1, width):
            tag, value = struct.unpack_from(entry, data, at)
            if tag == DT_NULL:
                break
            if tag == DT_NEEDED:
                names.append(value)
            elif tag == DT_STRTAB:
                table = value
        base = next((start + table - address for kind, start, address, length in segments
                     if kind == PT_LOAD and table is not None and address <= table < address + length), None)
        if base is None:
            return []
        return [data[base + value:data.index(b"\0", base + value)].decode("utf-8", "replace") for value in names]
    except (IndexError, ValueError, struct.error):
        return []


def describe_program(data):
    """What a program says it is: its Go build information, or the banner of a C program these notices know."""

    linked = {"interpreter": has_interpreter(data), "needed": needed_libraries(data)}
    if MAGIC in data:
        try:
            info = read_buildinfo(data)
        except (NoticeError, IndexError) as error:
            return {"kind": "unknown", "reason": f"unreadable Go build information: {error}", **linked}
        return {
            "kind": "go",
            "go": info["go"],
            "package_path": info["path"],
            "main": list(info["main"]) if info["main"] else None,
            "deps": [list(dep) for dep in info["deps"]],
            "settings": info["settings"],
            **linked,
        }
    match = _TINI.search(data)
    if match:
        return {
            "kind": "tini",
            "tini_version": match.group(1).decode(),
            "commit": match.group(2).decode(),
            "modified": match.group(3).decode("utf-8", "replace"),
            "static_glibc": _STATIC_GLIBC in data,
            **linked,
        }
    return {"kind": "unknown", "reason": "no Go build information and no banner these notices know", **linked}


def binary_identity(info, rule):
    """The revision or version a component's binary says it was built from."""

    if rule == "vcs.revision":
        return info["settings"].get("vcs.revision", "")
    name = rule.split(":", 1)[1]
    values = re.findall(rf"-X\s+\"?{re.escape(name)}=([^\s\"]+)", info["settings"].get("-ldflags", ""))
    return values[-1] if values else ""


def parse_notices(text):
    """The header fields and the module list a notices file was written for."""

    header, modules, in_modules = {}, set(), False
    for line in text.splitlines():
        if line.startswith(MODULES_HEADER):
            in_modules = True
            continue
        if in_modules:
            match = _MODULE_LINE.match(line)
            if match:
                modules.add((match.group(1), match.group(2)))
                continue
            if line.strip():
                break
            continue
        match = _HEADER.match(line)
        if match:
            header[match.group(1)] = match.group(2)
    return header, modules


def check(component, binary, notices):
    """Problems that keep this binary from shipping with these notices."""

    return check_data(component, Path(binary).read_bytes(), Path(notices).read_text(encoding="utf-8"), binary, notices)


def check_data(component, data, text, binary="the binary", notices="its notices"):
    """``check`` on a binary's bytes and a notices file's text, as an image holds them."""

    info = read_buildinfo(data)
    header, modules = parse_notices(text)
    identity = binary_identity(info, COMPONENTS[component]["identity"])
    problems = []
    if header.get("component") != component:
        problems.append(f"{notices} is written for {header.get('component')!r}, not {component!r}")
    if not identity or identity != header.get("identity"):
        problems.append(
            f"{binary} is {component} {identity or 'of unknown identity'}, the notices are for "
            f"{header.get('identity')}; regenerate them: scripts/binary_notices.py generate --component {component}"
            + (f" --revision {identity}" if COMPONENTS[component]["identity"] == "vcs.revision" and identity else "")
        )
    unnamed = sorted(set(info["deps"]) - modules)
    if unnamed:
        problems.append(f"{binary} holds modules its notices do not name: " + ", ".join(f"{p} {v}" for p, v in unnamed))
    return problems


class Fetcher:
    """HTTP with retries; ``get`` answers None for a missing file."""

    def __init__(self, attempts=ATTEMPTS, pause=2.0):
        self.attempts = attempts
        self.pause = pause

    def _fetch(self, url, headers=None):
        for attempt in range(self.attempts):
            request = urllib.request.Request(url, headers={"User-Agent": "ems-binary-notices", **(headers or {})})
            try:
                with urllib.request.urlopen(request, timeout=120) as response:
                    return response.status, response.headers, response.read()
            except urllib.error.HTTPError as error:
                if error.code in (404, 410):
                    return None
                failure = error
            except (http.client.HTTPException, OSError) as error:
                failure = error
            time.sleep(self.pause * (attempt + 1))
        raise NoticeError(f"{url}: {failure}")

    def get(self, url):
        answer = self._fetch(url)
        return None if answer is None else answer[2]

    def get_range(self, url, first, last=None):
        """Bytes ``first`` to ``last`` of ``url`` (its last ``-first`` bytes when negative) and its whole size."""

        wanted = f"bytes={first}" if first < 0 else f"bytes={first}-{'' if last is None else last}"
        answer = self._fetch(url, {"Range": wanted})
        if answer is None:
            raise NoticeError(f"{url}: not found")
        status, headers, data = answer
        if status == 206:
            total = (headers.get("Content-Range") or "").rpartition("/")[2]
            if not total.isdigit():
                raise NoticeError(f"{url}: a partial answer without its size")
            return data, int(total)
        start = max(len(data) + first, 0) if first < 0 else first
        end = len(data) if first < 0 or last is None else last + 1
        return data[start:end], len(data)

    def require(self, url):
        data = self.get(url)
        if data is None:
            raise NoticeError(f"{url}: not found")
        return data


class RemoteFile(io.RawIOBase):
    """A file read over HTTP ranges a block at a time, enough for ``zipfile`` to take a few members out of it."""

    def __init__(self, url, fetcher, block=BLOCK):
        super().__init__()
        self.url, self.fetcher, self.block = url, fetcher, block
        tail, self.size = fetcher.get_range(url, -block)
        self.pieces = [(self.size - len(tail), tail)]
        self.position = 0

    def readable(self):
        return True

    def seekable(self):
        return True

    def tell(self):
        return self.position

    def seek(self, offset, whence=io.SEEK_SET):
        base = {io.SEEK_SET: 0, io.SEEK_CUR: self.position, io.SEEK_END: self.size}[whence]
        self.position = max(0, base + offset)
        return self.position

    def readinto(self, buffer):
        length = min(len(buffer), max(0, self.size - self.position))
        if length:
            buffer[:length] = self._bytes(self.position, length)
            self.position += length
        return length

    def _bytes(self, start, length):
        for first, piece in self.pieces:
            if first <= start and start + length <= first + len(piece):
                return piece[start - first:start - first + length]
        end = min(self.size, start + max(length, self.block))
        piece, _ = self.fetcher.get_range(self.url, start, end - 1)
        if len(piece) != end - start:
            raise NoticeError(f"{self.url}: {len(piece)} bytes at {start}, asked for {end - start}")
        self.pieces.append((start, piece))
        return piece[:length]


def escape_module(path):
    """The module proxy's case encoding: an upper-case letter becomes ``!`` and its lower case."""

    return re.sub(r"[A-Z]", lambda match: "!" + match.group(0).lower(), path)


def parse_go_mod(text):
    """Every required module, with ``replace`` applied; local replacements are the main module's."""

    required, replaced, block = [], {}, None
    for raw in text.splitlines():
        line = raw.split("//", 1)[0].strip()
        if not line:
            continue
        if line.endswith("(") and line[:-1].strip() in ("require", "replace"):
            block = line[:-1].strip()
            continue
        if line == ")":
            block = None
            continue
        words = line.split()
        kind = block
        if words[0] in ("require", "replace") and block is None:
            kind, words = words[0], words[1:]
        if kind == "require" and len(words) >= 2:
            required.append((words[0], words[1]))
        elif kind == "replace" and "=>" in words:
            left, right = words[:words.index("=>")], words[words.index("=>") + 1:]
            replaced[(left[0], left[1] if len(left) > 1 else None)] = tuple(right)
    modules = []
    for path, version in required:
        target = replaced.get((path, version)) or replaced.get((path, None))
        if target is None:
            modules.append((path, version))
        elif len(target) == 2 and not target[0].startswith((".", "/")):
            modules.append(target)
    return sorted(set(modules))


def parse_vendor_modules(text):
    """``(path, version, folder)`` of each vendored module with a package in the build.

    A replaced module is named by its replacement but vendored under its own
    import path, which is where ``go mod vendor`` puts its license files.
    """

    modules, current = set(), None
    for line in text.splitlines():
        if line.startswith("## "):
            continue
        if line.startswith("# "):
            words = line[2:].split()
            target = words[words.index("=>") + 1:] if "=>" in words else words[:2]
            if target and target[0].startswith((".", "/")):
                raise NoticeError(f"vendor/modules.txt replaces {words[0]} with the local directory {target[0]}, whose license these notices cannot fetch")
            current = (*target, words[0]) if len(target) == 2 else None
        elif line.strip() and current:
            modules.add(current)
    return sorted(modules)


def is_license_file(name):
    """A file whose name says license, copying, copyright, notice, patents or legal, and that is not code or data."""

    return bool(_LICENSE_FILE.search(name)) and not _SOURCE_FILE.search(name)


def has_root_license(texts):
    """Whether a module's own license is among its texts, not only those of code it carries below its root."""

    return any("/" not in name for name in texts or {})


def in_build(relative):
    """Whether a file can belong to a package Go builds: not under testdata, vendor or a directory Go ignores."""

    return not any(part in ("testdata", "vendor") or part.startswith((".", "_")) for part in relative.split("/")[:-1])


def _wanted(relative):
    return in_build(relative) and is_license_file(PurePosixPath(relative).name)


def _license_texts(names_and_data):
    """``{path: text}`` of every license file, by its path below the module or tree it came from."""

    found = {}
    for name, data in names_and_data:
        if _wanted(name):
            found[name] = data.decode("utf-8", "replace").replace("\r\n", "\n").strip() + "\n"
    return found


def module_licenses(path, version, fetcher):
    """Every license file of a module's proxy zip, its root and its packages', read without downloading the rest."""

    prefix = f"{path}@{version}/"
    with zipfile.ZipFile(RemoteFile(f"{PROXY}/{escape_module(path)}/@v/{escape_module(version)}.zip", fetcher)) as archive:
        found = [
            (name[len(prefix):], archive.read(name))
            for name in archive.namelist()
            if name.startswith(prefix) and not name.endswith("/") and _wanted(name[len(prefix):])
        ]
    return _license_texts(found)


def module_origin(path, version, fetcher):
    """The commit the module proxy says a version was made from, or nothing when it does not say."""

    data = fetcher.require(f"{PROXY}/{escape_module(path)}/@v/{escape_module(version)}.info")
    return str((json.loads(data).get("Origin") or {}).get("Hash", ""))


def zip_hash(data):
    """The ``h1:`` hash Go records for a module zip: SHA-256 over each file's SHA-256 and name, sorted by name."""

    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        summary = "".join(f"{hashlib.sha256(archive.read(name)).hexdigest()}  {name}\n" for name in sorted(archive.namelist()))
    return "h1:" + base64.b64encode(hashlib.sha256(summary.encode()).digest()).decode()


def module_source(path, version, fetcher):
    """A module's zip as the proxy serves it, refused unless it hashes to what sum.golang.org records for it."""

    data = fetcher.require(f"{PROXY}/{escape_module(path)}/@v/{escape_module(version)}.zip")
    lookup = fetcher.require(f"{SUMDB}/lookup/{escape_module(path)}@{escape_module(version)}").decode("utf-8", "replace")
    recorded = next((words[2] for words in map(str.split, lookup.splitlines()) if len(words) == 3 and words[:2] == [path, version]), "")
    if not recorded or zip_hash(data) != recorded:
        raise NoticeError(f"{path} {version}: the module proxy's zip is not the one sum.golang.org records ({recorded or 'nothing'})")
    return data


def copyleft(texts):
    """Whether any of these texts is a license that asks for the source of what it covers."""

    return any(_COPYLEFT.search(text) for text in (texts or {}).values())


def modules_licenses(modules, fetcher, workers=WORKERS):
    """``{module: texts}`` of every module, a few fetched at a time."""

    with concurrent.futures.ThreadPoolExecutor(workers) as pool:
        return dict(zip(modules, pool.map(lambda module: module_licenses(*module, fetcher), modules)))


def _members(archive):
    return {member.name.split("/", 1)[1]: member for member in archive.getmembers() if "/" in member.name and member.isfile()}


def _subtree(archive, members, folder, skip=()):
    """License files below ``folder``, by their path inside it, leaving out the folders in ``skip``."""

    return [
        (name[len(folder):], archive.extractfile(member).read())
        for name, member in members.items()
        if name.startswith(folder) and not name.startswith(tuple(skip)) and _wanted(name[len(folder):])
    ]


def vendored_licenses(archive, members):
    """The modules ``vendor/modules.txt`` names, with the license files ``go mod vendor`` copied for each."""

    vendored = parse_vendor_modules(archive.extractfile(members["vendor/modules.txt"]).read().decode())
    folders = sorted({f"vendor/{imported}/" for _, _, imported in vendored})
    licenses = {}
    for path, version, imported in vendored:
        folder = f"vendor/{imported}/"
        nested = [other for other in folders if other != folder and other.startswith(folder)]
        licenses[(path, version)] = _license_texts(_subtree(archive, members, folder, nested))
    return sorted(licenses), licenses


def admin_pin(name):
    match = re.search(rf"^ARG {name}=(\S+)$", ADMIN_DOCKERFILE.read_text(encoding="utf-8"), re.MULTILINE)
    if not match:
        raise NoticeError(f"{ADMIN_DOCKERFILE} pins no {name}")
    return match.group(1)


def existing_identity(component):
    path = ROOT / "third_party" / component / NOTICES
    return parse_notices(path.read_text(encoding="utf-8"))[0].get("identity", "") if path.is_file() else ""


def tag_revision(repository, tag, fetcher):
    data = fetcher.require(f"{API}/repos/{repository}/commits/{tag}")
    return json.loads(data)["sha"]


def gather(component, fetcher, revision=None):
    """The header, module list and license texts for one component."""

    spec = COMPONENTS[component]
    repository = spec["repository"]
    if "version_arg" in spec:
        version = admin_pin(spec["version_arg"])
        ref = f"v{version}"
    else:
        ref = revision or existing_identity(component)
        if not ref:
            raise NoticeError(f"{component} has no pinned version; pass --revision")
        version = ref
    if spec["modules"] == "vendor":
        tarball = fetcher.require(f"{CODELOAD}/{repository}/tar.gz/refs/tags/{ref}")
        with tarfile.open(fileobj=io.BytesIO(tarball), mode="r:gz") as archive:
            modules, licenses = vendored_licenses(archive, _members(archive))
        identity = version
    else:
        commit = ref if "version_arg" not in spec else tag_revision(repository, ref, fetcher)
        go_mod = fetcher.require(f"{RAW}/{repository}/{commit}/go.mod").decode()
        modules = parse_go_mod(go_mod)
        licenses = modules_licenses(modules, fetcher)
        identity = commit
    missing = [f"{path} {module_version}" for (path, module_version), texts in licenses.items() if not has_root_license(texts)]
    if missing:
        raise NoticeError(f"{component}: no license file in " + ", ".join(missing))
    go_license = fetcher.require(f"{RAW}/{GO_LICENSE[0]}/{GO_LICENSE[1]}/LICENSE").decode()
    licenses[("std", "Go standard library")] = {"LICENSE": go_license.strip() + "\n"}
    return {
        "component": component,
        "version": version,
        "identity": identity,
        "source": f"https://github.com/{repository} at {ref}",
        "modules": modules,
        "licenses": licenses,
    }


def _license_sections(licenses):
    """Each distinct text once, under every module, or package of a module, that carries it."""

    groups = {}
    for (path, version), texts in sorted(licenses.items()):
        for name, text in sorted(texts.items()):
            folder, _, file = name.rpartition("/")
            module = (f"{path}/{folder}" if folder else path, version)
            groups.setdefault(text, {"names": set(), "modules": []})
            groups[text]["names"].add(file)
            if module not in groups[text]["modules"]:
                groups[text]["modules"].append(module)
    lines = []
    for text, group in sorted(groups.items(), key=lambda item: (item[1]["modules"][0], sorted(item[1]["names"]))):
        lines += ["", "=" * 78, f"{', '.join(sorted(group['names']))} of:"]
        lines += [f"  {path} {version}" for path, version in group["modules"]]
        lines += ["-" * 78, text.rstrip("\n")]
    return lines


def render(gathered):
    """The notices file: a header the build check reads, then each distinct text once."""

    lines = [
        f"Third-party notices for {gathered['component']} {gathered['version']}",
        "",
        f"component: {gathered['component']}",
        f"version: {gathered['version']}",
        f"identity: {gathered['identity']}",
        f"source: {gathered['source']}",
        "generator: scripts/binary_notices.py",
        "",
        MODULES_HEADER,
    ]
    lines += [f"  {path} {version}" for path, version in gathered["modules"]]
    lines += _license_sections(gathered["licenses"])
    return "\n".join(lines) + "\n"


def _tree(repository, tag, commit, fetcher, trees):
    """A tagged GitHub tree, refused unless it is the commit the program names; its own license files."""

    if (repository, tag) not in trees:
        tarball = fetcher.require(f"{CODELOAD}/{repository}/tar.gz/refs/tags/{tag}")
        with tarfile.open(fileobj=io.BytesIO(tarball), mode="r:gz") as archive:
            members = _members(archive)
            modules = [name[: -len("go.mod")] for name in members if name.endswith("/go.mod")]
            trees[(repository, tag)] = {
                "commit": archive.pax_headers.get("comment", ""),
                "root": _license_texts(_subtree(archive, members, "", modules)),
                "vendored": vendored_licenses(archive, members) if "vendor/modules.txt" in members else None,
            }
    tree = trees[(repository, tag)]
    if not _COMMIT.fullmatch(commit or "") or not tree["commit"].startswith(commit):
        raise NoticeError(f"{repository} {tag} is commit {tree['commit'] or 'unknown'}, the program says {commit or 'nothing'}")
    if not has_root_license(tree["root"]):
        raise NoticeError(f"{repository} {tag} has no license file at its root")
    return tree


def glibc_texts(release, fetcher):
    """The glibc of the Debian release a package was built for, and its license texts as pinned."""

    version, pinned = GLIBC[release]
    texts = {}
    for file, digest in pinned.items():
        url = GLIBC_URL.format(name=file, version=version)
        data = fetcher.require(url)
        if hashlib.sha256(data).hexdigest() != digest:
            raise NoticeError(f"{url}: not the {file} of glibc {version}")
        texts[file] = data.decode().strip() + "\n"
    return version, texts


def program_notices(program, fetcher, trees):
    """Where one program's source is, its own license texts, and the modules compiled into it."""

    where = f"{program['path']} ({program['package']} {program['version']})"
    if program["kind"] == "tini":
        if program["modified"]:
            raise NoticeError(f"{where}: built from a modified tree ({program['modified']}), not from upstream's")
        if not program["interpreter"] and not program["static_glibc"]:
            raise NoticeError(f"{where}: statically linked with a C library these notices do not know")
        tag = f"v{program['tini_version']}"
        tree = _tree(TINI_REPOSITORY, tag, program["commit"], fetcher, trees)
        own = {(f"github.com/{TINI_REPOSITORY}", tag): tree["root"]}
        linked = ""
        if program["static_glibc"]:
            release = _DEBIAN_RELEASE.search(program["version"])
            if not release or release.group(1) not in GLIBC:
                raise NoticeError(f"{where}: statically linked with the glibc of a Debian release these notices do not pin")
            version, texts = glibc_texts(release.group(1), fetcher)
            own[(GLIBC_KEY, f"glibc {version}")] = texts
            linked = f", statically linked with the GNU C Library of Debian {release.group(1)} (glibc {version})"
        return {"source": f"https://github.com/{TINI_REPOSITORY} at {tag} ({tree['commit']}){linked}", "own": own, "modules": []}
    if program["kind"] != "go":
        raise NoticeError(f"{where}: {program.get('reason', 'not a program these notices know')}")
    settings = program["settings"]
    if not program["go"].strip():
        raise NoticeError(f"{where}: names no Go version")
    if settings.get("vcs.modified") == "true":
        raise NoticeError(f"{where}: built from a modified tree, not from upstream's")
    experiment = " ".join(program["go"].split()[1:]) or settings.get("GOEXPERIMENT", "")
    if experiment:
        raise NoticeError(f"{where}: built with the Go experiment {experiment}, which no rule names")
    sanitizers = [flag for flag in _SANITIZERS if settings.get(flag) == "true"]
    if sanitizers:
        raise NoticeError(f"{where}: built with {' '.join(sanitizers)}, whose runtime no rule names")
    linking = f"{settings.get('-ldflags', '')} {settings.get('CGO_LDFLAGS', '')}"
    if settings.get("CGO_ENABLED") == "1" and (not program["interpreter"] or _STATIC_LINK.search(linking)):
        raise NoticeError(f"{where}: links C code in statically, and no rule names the libraries")
    for path, _ in program["deps"]:
        library = CGO_BINDINGS.get(path)
        if library and settings.get("CGO_ENABLED") == "1" and not any(name.startswith(library) for name in program["needed"]):
            raise NoticeError(f"{where}: compiles {path} in but does not load {library}, so it links the library in statically")
    main = tuple(program["main"]) if program["main"] else None
    modules = [tuple(dep) for dep in program["deps"]]
    local = [path for path, _ in modules if path.startswith((".", "/"))]
    if local:
        raise NoticeError(f"{where}: replaces {', '.join(local)} with a local directory, whose license these notices cannot fetch")
    if main and main[1] != "(devel)":
        revision = settings.get("vcs.revision", "")
        if revision:
            published = module_origin(*main, fetcher)
            if published != revision:
                raise NoticeError(f"{where}: built from {revision}, but {main[0]} {main[1]} is {published or 'of no recorded commit'}")
        return {"source": f"module {main[0]} {main[1]}", "own": {}, "modules": [main, *modules]}
    rule = next((rule for rule in SOURCE_TREES if program["package_path"].startswith(rule["package"])), None)
    if rule is None:
        raise NoticeError(f"{where}: built from {' '.join(main) if main else 'no module'}, and no rule says where its source is")
    if not rule.get("vendored") and (main is None or not modules):
        raise NoticeError(f"{where}: records no modules, and its rule reads none from a vendor tree")
    version = binary_identity(program, f"ldflag:{rule['version']}")
    if not version:
        raise NoticeError(f"{where}: names no version in {rule['version']}")
    tag = rule["tag"].format(version=version)
    tree = _tree(rule["repository"], tag, binary_identity(program, f"ldflag:{rule['commit']}"), fetcher, trees)
    revision = settings.get("vcs.revision", "")
    if revision and revision != tree["commit"]:
        raise NoticeError(f"{where}: built from {revision}, but {rule['repository']} {tag} is {tree['commit']}")
    found = {"source": f"https://github.com/{rule['repository']} at {tag} ({tree['commit']})",
             "own": {(rule["package"].rstrip("/"), tag): tree["root"]}, "modules": modules}
    if rule.get("vendored"):
        if tree["vendored"] is None:
            raise NoticeError(f"{rule['repository']} {tag} has no vendor/modules.txt")
        vendored, found["vendored"] = tree["vendored"]
        found["modules"] = sorted({*vendored, *modules})
    return found


def appliance_programs(requests):
    """The release version and every program of every board, once per file, with the boards that carry it."""

    versions = {request.get("release_version", "") for request in requests}
    if len(versions) != 1 or not next(iter(versions)):
        raise NoticeError(f"the boards do not name one release version: {sorted(versions)}")
    programs = {}
    for request in requests:
        if "programs" not in request:
            raise NoticeError(f"{request.get('board')}: the request names no programs; it predates them")
        for program in request["programs"]:
            key = (program["path"], program["sha256"], program["package"], program["version"])
            entry = programs.setdefault(key, {**program, "boards": []})
            entry["boards"] = sorted({*entry["boards"], request["board"]})
    if not programs:
        raise NoticeError("the images carry no Docker program, so there is nothing to write notices for")
    return versions.pop(), [programs[key] for key in sorted(programs)]


def gather_appliance(requests, fetcher):
    release, programs = appliance_programs(requests)
    trees, licenses, modules, entries = {}, {}, set(), []
    vendored, proxied = {}, set()
    for program in programs:
        found = program_notices(program, fetcher, trees)
        for (path, version), texts in found["own"].items():
            if path != GLIBC_KEY and copyleft(texts):
                raise NoticeError(f"{program['path']}: {path} {version} carries a copyleft license no rule offers the source of")
        licenses.update(found["own"])
        vendored.update(found.get("vendored", {}))
        proxied.update(module for module in found["modules"] if module not in found.get("vendored", {}))
        modules.update(found["modules"])
        go = program["go"].split()[0] if program["kind"] == "go" else ""
        if go:
            licenses.setdefault(("std", go), None)
        entries.append({**program, "source": found["source"], "go": go})
    fetched = modules_licenses(sorted(proxied), fetcher)
    for module in modules:
        licenses[module] = {**vendored.get(module, {}), **fetched.get(module, {})}
    for module, texts in list(licenses.items()):
        if module[0] == "std":
            licenses[module] = {"LICENSE": fetcher.require(f"{RAW}/golang/go/{module[1]}/LICENSE").decode().strip() + "\n"}
    missing = [f"{path} {version}" for (path, version), texts in sorted(licenses.items()) if not has_root_license(texts)]
    if missing:
        raise NoticeError("no license file at the root of " + ", ".join(missing))
    owed = sorted(module for module in modules if copyleft(licenses[module]))
    with concurrent.futures.ThreadPoolExecutor(WORKERS) as pool:
        sources = dict(zip(owed, pool.map(lambda module: module_source(*module, fetcher), owed)))
    boards = sorted({board for program in programs for board in program["boards"]})
    return {"release": release, "boards": boards, "programs": entries, "modules": sorted(modules), "licenses": licenses,
            "sources": sources}


def write_sources(sources, target):
    """The zips of the modules whose license asks for their source, in a tar that carries no builder identity."""

    with tarfile.open(target, "w", format=tarfile.GNU_FORMAT) as archive:
        for (path, version), data in sorted(sources.items()):
            info = tarfile.TarInfo(f"{path}@{version}.zip")
            info.size, info.mtime, info.mode = len(data), 0, 0o644
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            archive.addfile(info, io.BytesIO(data))


def render_appliance(gathered):
    """The notices file of an appliance release: what each program is, every module in them, each text once."""

    lines = [
        f"Third-party notices for the Docker programs of the EMS SolarFlow appliance image {gathered['release']}",
        "",
        f"release: {gathered['release']}",
        f"boards: {', '.join(gathered['boards'])}",
        "generator: scripts/binary_notices.py appliance",
        "",
        "Docker's Debian packages ship no notices for what their programs are built from.",
        "These are the license texts of their programs, of every Go module compiled into",
        "them and of what is linked in, as each program's build information or version",
        "banner names them.",
        "",
        "Programs:",
    ]
    for program in gathered["programs"]:
        lines.append(f"  {program['path']}")
        lines.append(f"    package: {program['package']} {program['version']}")
        lines.append(f"    sha256: {program['sha256']}")
        if program["boards"] != gathered["boards"]:
            lines.append(f"    boards: {', '.join(program['boards'])}")
        lines.append(f"    source: {program['source']}")
        if program["go"]:
            lines.append(f"    built with: {program['go']}")
    lines += ["", "Modules compiled into them:"]
    lines += [f"  {path} {version}" for path, version in gathered["modules"]]
    if gathered["sources"]:
        lines += [
            "",
            "Modules that carry a copyleft license text (such as MPL-2.0), for all or part of",
            "their code or as one of the licenses to choose from. Their source is beside this",
            f"file, in {APPLIANCE_SOURCES.format(version=gathered['release'])}, as the Go module",
            "proxy serves each zip and sum.golang.org records its hash:",
        ]
        lines += [f"  {path} {version}" for path, version in sorted(gathered["sources"])]
    lines += _license_sections(gathered["licenses"])
    return "\n".join(lines) + "\n"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    generating = commands.add_parser("generate", help="Write third_party/<name>/THIRD_PARTY_NOTICES.")
    generating.add_argument("--component", action="append", choices=sorted(COMPONENTS))
    generating.add_argument("--revision", help="The upstream revision of a component no Dockerfile pins.")
    checking = commands.add_parser("check", help="Refuse a binary its notices were not written for.")
    checking.add_argument("--component", required=True, choices=sorted(COMPONENTS))
    checking.add_argument("--binary", required=True)
    checking.add_argument("--notices", required=True)
    releasing = commands.add_parser("appliance", help="Write the notices of the Docker programs an appliance release carries.")
    releasing.add_argument("--request", action="append", required=True)
    releasing.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "appliance":
            requests = [json.loads(Path(path).read_text(encoding="utf-8")) for path in args.request]
            gathered = gather_appliance(requests, Fetcher())
            output = Path(args.output)
            output.mkdir(parents=True, exist_ok=True)
            if gathered["sources"]:
                write_sources(gathered["sources"], output / APPLIANCE_SOURCES.format(version=gathered["release"]))
            target = output / APPLIANCE_NOTICES.format(version=gathered["release"])
            target.write_text(render_appliance(gathered), encoding="utf-8")
            print(
                f"{target}: {len(gathered['programs'])} programs, {len(gathered['modules'])} modules, {target.stat().st_size} bytes; "
                f"the source of {len(gathered['sources'])} copyleft modules beside it"
            )
            return 0
        if args.command == "check":
            problems = check(args.component, args.binary, args.notices)
            for problem in problems:
                print(f"binary-notices: {problem}", file=sys.stderr)
            if not problems:
                print(f"{args.binary}: its notices name every module it holds")
            return 1 if problems else 0
        fetcher = Fetcher()
        for component in args.component or sorted(COMPONENTS):
            gathered = gather(component, fetcher, args.revision if component == "influx-cli" else None)
            target = ROOT / "third_party" / component / NOTICES
            target.write_text(render(gathered), encoding="utf-8")
            print(f"{target.relative_to(ROOT)}: {len(gathered['modules'])} modules, {target.stat().st_size} bytes")
    except NoticeError as error:
        print(f"binary-notices: {error}", file=sys.stderr)
        return 1
    except (OSError, ValueError, KeyError) as error:
        print(f"binary-notices: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
