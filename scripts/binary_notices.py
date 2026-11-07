#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 basecubedev
# SPDX-License-Identifier: AGPL-3.0-or-later
"""License notices for the Go modules compiled into the binaries the images ship.

    scripts/binary_notices.py generate [--component NAME ...] [--revision SHA]
    scripts/binary_notices.py check --component NAME --binary PATH --notices FILE

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

Exit status: 0 written or consistent, 1 something could not be fetched, read or
matched, 2 the command line is wrong.
"""

import argparse
import io
import json
import re
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
GO_LICENSE = ("golang/go", "go1.25.0")
ATTEMPTS = 4

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

MAGIC = b"\xff Go buildinf:"
_LICENSE_FILE = re.compile(r"^(?:LICEN[CS]E|COPYING|NOTICE|PATENTS)(?:[.-][\w.-]*)?$", re.IGNORECASE)
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
    main, deps, settings = None, [], {}
    for line in modinfo.splitlines():
        words = line.split("\t")
        if words[0] == "mod" and len(words) >= 3:
            main = (words[1], words[2])
        elif words[0] == "dep" and len(words) >= 3:
            deps.append((words[1], words[2]))
        elif words[0] == "=>" and len(words) >= 3 and deps:
            deps[-1] = (words[1], words[2])
        elif words[0] == "build" and len(words) >= 2:
            key, _, value = words[1].partition("=")
            settings[key] = _unquote(value)
    return {"go": go_version, "main": main, "deps": sorted(set(deps)), "settings": settings}


def binary_identity(info, rule):
    """The revision or version a component's binary says it was built from."""

    if rule == "vcs.revision":
        return info["settings"].get("vcs.revision", "")
    name = rule.split(":", 1)[1]
    match = re.search(rf"-X\s+\"?{re.escape(name)}=([^\s\"]+)", info["settings"].get("-ldflags", ""))
    return match.group(1) if match else ""


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

    info = read_buildinfo(Path(binary).read_bytes())
    header, modules = parse_notices(Path(notices).read_text(encoding="utf-8"))
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

    def get(self, url):
        for attempt in range(self.attempts):
            request = urllib.request.Request(url, headers={"User-Agent": "ems-binary-notices"})
            try:
                with urllib.request.urlopen(request, timeout=120) as response:
                    return response.read()
            except urllib.error.HTTPError as error:
                if error.code in (404, 410):
                    return None
                failure = error
            except (urllib.error.URLError, TimeoutError, ConnectionError) as error:
                failure = error
            time.sleep(self.pause * (attempt + 1))
        raise NoticeError(f"{url}: {failure}")

    def require(self, url):
        data = self.get(url)
        if data is None:
            raise NoticeError(f"{url}: not found")
        return data


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
            current = (*target, words[0]) if len(target) == 2 and not target[0].startswith((".", "/")) else None
        elif line.strip() and current:
            modules.add(current)
    return sorted(modules)


def _license_texts(names_and_data):
    found = {}
    for name, data in names_and_data:
        if _LICENSE_FILE.match(PurePosixPath(name).name):
            found[PurePosixPath(name).name] = data.decode("utf-8", "replace").replace("\r\n", "\n").strip() + "\n"
    return found


def module_licenses(path, version, fetcher):
    data = fetcher.require(f"{PROXY}/{escape_module(path)}/@v/{escape_module(version)}.zip")
    prefix = f"{path}@{version}/"
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        root = [
            (name[len(prefix):], archive.read(name))
            for name in archive.namelist()
            if name.startswith(prefix) and "/" not in name[len(prefix):]
        ]
    return _license_texts(root)


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
            members = {member.name.split("/", 1)[1]: member for member in archive.getmembers() if "/" in member.name and member.isfile()}
            vendored = parse_vendor_modules(archive.extractfile(members["vendor/modules.txt"]).read().decode())
            modules = sorted({(path, module_version) for path, module_version, _ in vendored})
            licenses = {}
            for path, module_version, imported in vendored:
                folder = f"vendor/{imported}/"
                root = [(name, archive.extractfile(member).read()) for name, member in members.items()
                        if name.startswith(folder) and "/" not in name[len(folder):]]
                licenses[(path, module_version)] = _license_texts(root)
        identity = version
    else:
        commit = ref if "version_arg" not in spec else tag_revision(repository, ref, fetcher)
        go_mod = fetcher.require(f"{RAW}/{repository}/{commit}/go.mod").decode()
        modules = parse_go_mod(go_mod)
        licenses = {module: module_licenses(*module, fetcher) for module in modules}
        identity = commit
    missing = [f"{path} {module_version}" for (path, module_version), texts in licenses.items() if not texts]
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
    groups = {}
    for module, texts in sorted(gathered["licenses"].items()):
        for name, text in sorted(texts.items()):
            groups.setdefault(text, {"names": set(), "modules": []})
            groups[text]["names"].add(name)
            if module not in groups[text]["modules"]:
                groups[text]["modules"].append(module)
    for text, group in sorted(groups.items(), key=lambda item: (item[1]["modules"][0], sorted(item[1]["names"]))):
        lines += ["", "=" * 78, f"{', '.join(sorted(group['names']))} of:"]
        lines += [f"  {path} {version}" for path, version in group["modules"]]
        lines += ["-" * 78, text.rstrip("\n")]
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
    args = parser.parse_args(argv)
    try:
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
