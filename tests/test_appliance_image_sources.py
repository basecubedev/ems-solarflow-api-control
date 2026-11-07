# SPDX-FileCopyrightText: 2026 basecubedev
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The source code of an appliance image, read off the image and packed beside it.

Every case runs offline: a fake fetcher serves source indexes, pool files and
snapshot.debian.org answers from memory, so what is tested is the decision --
which source, from where, verified how, packed into which part -- and a missing
or tampered source has to fail the run rather than leave a gap in a release.
"""

import gzip
import hashlib
import importlib.util
import io
import json
import lzma
import tarfile
from pathlib import Path

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.appliance]

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("appliance_image_sources", ROOT / "scripts" / "appliance_image_sources.py")
sources = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(sources)

DEBIAN = "https://deb.debian.org/debian"
RPI = "https://archive.raspberrypi.com/debian"
REPOSITORY = "https://github.com/example/ems"
REVISION = "0123456789abcdef0123456789abcdef01234567"


def digest(data):
    return hashlib.sha256(data).hexdigest()


def sha1(data):
    return hashlib.sha1(data).hexdigest()


def source_stanza(name, version, directory, files):
    lines = [f"Package: {name}", f"Version: {version}", f"Directory: {directory}", "Checksums-Sha256:"]
    lines += [f" {digest(data)} {len(data)} {file}" for file, data in files.items()]
    return "\n".join(lines)


class FakeFetcher:
    def __init__(self, files):
        self.files = dict(files)
        self.asked = []

    def get(self, url):
        self.asked.append(url)
        return self.files.get(url)

    def download(self, url, target):
        self.asked.append(url)
        if url not in self.files:
            raise sources.SourceError(f"{url}: not found")
        target.write_bytes(self.files[url])


class FakeRoot:
    def __init__(self, files):
        self.files = files

    def exists(self, path):
        return path in self.files or any(name.startswith(path + "/") for name in self.files)

    def listdir(self, path):
        return sorted({name[len(path) + 1:].split("/")[0] for name in self.files if name.startswith(path + "/")})

    def is_file(self, path):
        return path in self.files

    def read_bytes(self, path):
        data = self.files[path]
        return data if isinstance(data, bytes) else data.encode()

    def read_text(self, path):
        return self.files[path]


ACL = {"acl_2.3.2-2.dsc": b"acl dsc", "acl_2.3.2.orig.tar.xz": b"acl upstream", "acl_2.3.2-2.debian.tar.xz": b"acl debian"}
EEPROM_OLD = {"rpi-eeprom_28.34.orig.tar.xz": b"eeprom upstream", "rpi-eeprom_28.34-1.debian.tar.xz": b"eeprom debian"}
EEPROM_DSC = (
    "-----BEGIN PGP SIGNED MESSAGE-----\nHash: SHA512\n\n"
    "Format: 3.0 (quilt)\nSource: rpi-eeprom\nVersion: 28.34-1\nChecksums-Sha256:\n"
    + "\n".join(f" {digest(data)} {len(data)} {name}" for name, data in EEPROM_OLD.items())
    + "\n\n-----BEGIN PGP SIGNATURE-----\n\niQIzBAEBCgAdFiEE\n-----END PGP SIGNATURE-----\n"
).encode()
GLIBC = {"glibc_2.41-12.dsc": b"glibc dsc", "glibc_2.41.orig.tar.xz": b"glibc upstream"}
OLD = {"oldpkg_1.0-1.dsc": b"old dsc", "oldpkg_1.0.orig.tar.gz": b"old upstream"}


def archive_files():
    debian = source_stanza("acl", "2.3.2-2", "pool/main/a/acl", ACL) + "\n\n" + source_stanza("glibc", "2.41-12", "pool/main/g/glibc", GLIBC)
    rpi = source_stanza("rpi-eeprom", "28.36-1", "pool/main/r/rpi-eeprom", {"rpi-eeprom_28.36-1.dsc": b"newer"})
    served = {
        f"{DEBIAN}/dists/trixie/main/source/Sources.xz": lzma.compress(debian.encode()),
        f"{RPI}/dists/trixie/main/source/Sources.gz": gzip.compress(rpi.encode()),
        f"{RPI}/pool/main/r/rpi-eeprom/rpi-eeprom_28.34-1.dsc": EEPROM_DSC,
        f"{sources.SNAPSHOT}/mr/package/oldpkg/1.0-1/srcfiles?fileinfo=1": json.dumps({
            "fileinfo": {sha1(data): [{"archive_name": "debian", "name": name, "size": len(data)}] for name, data in OLD.items()}
        }).encode(),
    }
    served.update({f"{DEBIAN}/pool/main/a/acl/{name}": data for name, data in ACL.items()})
    served.update({f"{DEBIAN}/pool/main/g/glibc/{name}": data for name, data in GLIBC.items()})
    served.update({f"{RPI}/pool/main/r/rpi-eeprom/{name}": data for name, data in EEPROM_OLD.items()})
    served.update({f"{sources.SNAPSHOT}/file/{sha1(data)}": data for data in OLD.values()})
    return served


def package(name, source, version, maintainer="Debian <debian@lists.debian.org>"):
    return {"package": name, "version": version, "architecture": "arm64", "maintainer": maintainer,
            "source": source, "source_version": version}


def request(board="rpi4", release_version="0.5.0", packages=None):
    return {
        "schema_version": sources.SCHEMA_VERSION,
        "board": board,
        "release_version": release_version,
        "build_id": "20261107",
        "archives": [
            {"uri": "http://deb.debian.org/debian", "suite": "trixie", "component": "main"},
            {"uri": "http://archive.raspberrypi.com/debian", "suite": "trixie", "component": "main"},
            {"uri": "https://download.docker.com/linux/debian", "suite": "trixie", "component": "stable"},
        ],
        "packages": packages if packages is not None else [
            package("acl", "acl", "2.3.2-2"),
            package("libacl1", "acl", "2.3.2-2"),
            package("rpi-eeprom", "rpi-eeprom", "28.34-1", "Raspberry Pi <serge@raspberrypi.com>"),
            package("oldpkg", "oldpkg", "1.0-1"),
            package("docker-ce", "docker-ce", "5:29.9.2-1~debian.13~trixie", "Docker <support@docker.com>"),
            package("ems-appliance-manager", "ems-appliance-manager", "0.5.0", "EMS SolarFlow <noreply@example.org>"),
        ],
    }


def run(tmp_path, requests, *, files=None, limit=sources.PART_LIMIT):
    fetcher = FakeFetcher(archive_files() if files is None else files)
    output = tmp_path / "release"
    manifest = sources.collect(requests, revision=REVISION, repository_url=REPOSITORY, output=output,
                               fetcher=fetcher, part_limit=limit)
    return manifest, output, fetcher


def members(path):
    with tarfile.open(path) as archive:
        return {member.name: archive.extractfile(member).read() for member in archive.getmembers()}


# --- what the image says -----------------------------------------------------


def test_the_source_of_a_package_is_what_dpkg_says_it_is():
    assert sources.source_of({"Package": "libacl1", "Version": "2.3.2-2+b1", "Source": "acl (2.3.2-2)"}) == ("acl", "2.3.2-2")
    assert sources.source_of({"Package": "libfoo1", "Version": "1.0-1", "Source": "foo"}) == ("foo", "1.0-1")
    assert sources.source_of({"Package": "bar", "Version": "2.0-1"}) == ("bar", "2.0-1")


def test_only_installed_packages_need_a_source():
    status = (
        "Package: kept\nStatus: install ok installed\nVersion: 1\n\n"
        "Package: held\nStatus: hold ok installed\nVersion: 2\n\n"
        "Package: removed\nStatus: deinstall ok config-files\nVersion: 3\n"
    )
    assert [item["package"] for item in sources.installed_packages(status)] == ["held", "kept"]


def test_the_archives_are_read_from_both_apt_formats():
    files = {
        "debian.sources": "Types: deb\nURIs: http://deb.debian.org/debian\nSuites: trixie trixie-updates\nComponents: main\n\n"
                          "Types: deb-src\nURIs: http://deb.debian.org/debian\nSuites: trixie\nComponents: main\n\n"
                          "Enabled: no\nTypes: deb\nURIs: http://example.org\nSuites: x\nComponents: y\n",
        "sources.list": "# a comment\ndeb [arch=arm64 signed-by=/k.gpg] http://archive.raspberrypi.com/debian/ trixie main\n",
    }
    assert sources.apt_archives(files) == [
        ("http://archive.raspberrypi.com/debian", "trixie", "main"),
        ("http://deb.debian.org/debian", "trixie", "main"),
        ("http://deb.debian.org/debian", "trixie-updates", "main"),
    ]


def test_the_request_is_read_off_the_image_root():
    root = FakeRoot({
        sources.DPKG_STATUS: "Package: acl\nStatus: install ok installed\nVersion: 2.3.2-2\nArchitecture: arm64\n",
        f"{sources.APT_SOURCES_DIR}/debian.sources": "Types: deb\nURIs: http://deb.debian.org/debian\nSuites: trixie\nComponents: main\n",
        sources.BUILD_MARKER: json.dumps({"release_version": "0.5.0", "build_id": "20261107"}),
    })
    made = sources.request_from_root(root, "rpi5")
    assert (made["board"], made["release_version"], made["build_id"]) == ("rpi5", "0.5.0", "20261107")
    assert made["archives"] == [{"uri": "http://deb.debian.org/debian", "suite": "trixie", "component": "main"}]
    assert [(item["source"], item["source_version"]) for item in made["packages"]] == [("acl", "2.3.2-2")]
    assert made["programs"] == []


TINI_BINARY = b"\x7fELF\0tini version 0.19.0 - git.de40ad0\0Fatal glibc error\0"


def docker_root(**extra):
    return FakeRoot({
        sources.DPKG_STATUS: (
            "Package: acl\nStatus: install ok installed\nVersion: 2.3.2-2\nArchitecture: arm64\n\n"
            "Package: docker-ce\nStatus: install ok installed\nVersion: 5:29.9.2-1\nArchitecture: arm64\nMaintainer: Docker <support@docker.com>\n"
        ),
        f"{sources.APT_SOURCES_DIR}/debian.sources": "Types: deb\nURIs: http://deb.debian.org/debian\nSuites: trixie\nComponents: main\n",
        f"{sources.DPKG_INFO}/acl.list": "/usr/bin/getfacl\n",
        "/usr/bin/getfacl": b"\x7fELF acl",
        f"{sources.DPKG_INFO}/docker-ce.list": "/.\n/usr\n/usr/libexec/docker/docker-init\n/usr/share/doc/docker-ce/README.md\n/usr/bin/unread\n",
        "/usr/libexec/docker/docker-init": TINI_BINARY,
        "/usr/share/doc/docker-ce/README.md": "not a program",
        **extra,
    })


def test_the_request_records_what_each_docker_program_says_it_is():
    programs = sources.request_from_root(docker_root(), "rpi4")["programs"]
    assert programs == [{
        "package": "docker-ce", "version": "5:29.9.2-1", "path": "/usr/libexec/docker/docker-init",
        "size": len(TINI_BINARY), "sha256": digest(TINI_BINARY),
        "kind": "tini", "tini_version": "0.19.0", "commit": "de40ad0", "modified": "", "static_glibc": True, "interpreter": False, "needed": [],
    }]


def varint(value):
    out = bytearray()
    while True:
        out.append((value & 0x7F) | (0x80 if value >> 7 else 0))
        value >>= 7
        if not value:
            return bytes(out)


def go_binary(lines):
    modinfo = ("x" * 16 + "\n".join(lines) + "\n" + "y" * 16).encode()
    go = b"go1.26.10"
    return b"\x7fELF\x02\x01" + b"\0" * 58 + b"\xff Go buildinf:" + bytes([8, 2]) + b"\0" * 16 + varint(len(go)) + go + varint(len(modinfo)) + modinfo


def test_a_go_program_of_a_docker_package_is_recorded_with_its_modules():
    binary = go_binary(["path\tgithub.com/moby/moby/v2/cmd/dockerd", "mod\tgithub.com/moby/moby/v2\t(devel)\t", "dep\tgithub.com/a/one\tv1.0.0\t", "build\tCGO_ENABLED=1"])
    root = docker_root(**{"/usr/bin/dockerd": binary})
    root.files[f"{sources.DPKG_INFO}/docker-ce.list"] += "/usr/bin/dockerd\n"
    dockerd = next(item for item in sources.request_from_root(root, "rpi4")["programs"] if item["path"] == "/usr/bin/dockerd")
    assert (dockerd["kind"], dockerd["go"], dockerd["package_path"]) == ("go", "go1.26.10", "github.com/moby/moby/v2/cmd/dockerd")
    assert dockerd["main"] == ["github.com/moby/moby/v2", "(devel)"] and dockerd["deps"] == [["github.com/a/one", "v1.0.0"]]
    assert dockerd["settings"] == {"CGO_ENABLED": "1"} and dockerd["sha256"] == digest(binary)


def test_a_docker_package_whose_files_dpkg_does_not_list_is_refused():
    root = docker_root()
    del root.files[f"{sources.DPKG_INFO}/docker-ce.list"]
    with pytest.raises(sources.SourceError, match="docker-ce: the image's dpkg database lists none of its files"):
        sources.request_from_root(root, "rpi4")


def test_a_multi_arch_list_file_is_read_too():
    root = docker_root()
    root.files[f"{sources.DPKG_INFO}/docker-ce:arm64.list"] = root.files.pop(f"{sources.DPKG_INFO}/docker-ce.list")
    assert [item["path"] for item in sources.request_from_root(root, "rpi4")["programs"]] == ["/usr/libexec/docker/docker-init"]


def test_an_image_without_an_archive_or_a_package_is_refused():
    with pytest.raises(sources.SourceError, match="no apt archive"):
        sources.request_from_root(FakeRoot({sources.DPKG_STATUS: "Package: a\nStatus: install ok installed\nVersion: 1\n"}), "rpi3")
    with pytest.raises(sources.SourceError, match="no installed package"):
        sources.request_from_root(FakeRoot({sources.DPKG_STATUS: ""}), "rpi3")


# --- collecting --------------------------------------------------------------


def test_every_source_is_fetched_verified_and_packed_beside_the_image(tmp_path):
    manifest, output, fetcher = run(tmp_path, [request("rpi4"), request("rpi5")])

    by_source = {item["source"]: item for item in manifest["sources"]}
    assert set(by_source) == {"acl", "rpi-eeprom", "oldpkg"}
    assert by_source["acl"]["verified_by"] == "index"
    assert by_source["acl"]["binaries"] == ["acl", "libacl1"]
    assert by_source["acl"]["boards"] == ["rpi4", "rpi5"]
    assert by_source["rpi-eeprom"]["verified_by"] == "dsc"
    assert by_source["oldpkg"]["verified_by"] == "snapshot"
    assert [item["source"] for item in manifest["without_source_duty"]] == ["docker-ce"]
    assert manifest["without_source_duty"][0]["license"] == "Apache-2.0"
    assert manifest["project"][0]["where"] == f"{REPOSITORY}/tree/{REVISION}"
    assert all(url.startswith("https://") for url in fetcher.asked)

    assert sorted(path.name for path in output.iterdir()) == [
        "ems-solarflow-appliance-0.5.0-sources-1-of-1.tar",
        "ems-solarflow-appliance-0.5.0-sources.json",
    ]
    packed = members(output / "ems-solarflow-appliance-0.5.0-sources-1-of-1.tar")
    assert packed["deb.debian.org_debian/acl_2.3.2-2/acl_2.3.2.orig.tar.xz"] == ACL["acl_2.3.2.orig.tar.xz"]
    assert packed["archive.raspberrypi.com_debian/rpi-eeprom_28.34-1/rpi-eeprom_28.34-1.dsc"] == EEPROM_DSC
    assert packed["snapshot.debian.org/oldpkg_1.0-1/oldpkg_1.0.orig.tar.gz"] == OLD["oldpkg_1.0.orig.tar.gz"]
    for record in manifest["sources"]:
        folder = f"{sources.origin_label(record['origin'])}/{record['source']}_{record['version']}"
        for entry in record["files"]:
            assert digest(packed[f"{folder}/{entry['name']}"]) == entry["sha256"]
    part = manifest["parts"][0]
    assert part["sha256"] == digest((output / part["name"]).read_bytes())
    assert json.loads((output / "ems-solarflow-appliance-0.5.0-sources.json").read_text()) == manifest


def test_the_same_request_packs_the_same_bytes(tmp_path):
    first, _, _ = run(tmp_path / "a", [request()])
    second, _, _ = run(tmp_path / "b", [request()])
    assert first["parts"] == second["parts"]


def test_a_file_that_does_not_match_its_checksum_is_refused(tmp_path):
    files = archive_files()
    files[f"{DEBIAN}/pool/main/a/acl/acl_2.3.2.orig.tar.xz"] = b"acl tampered"
    with pytest.raises(sources.SourceError, match="acl_2.3.2.orig.tar.xz"):
        run(tmp_path, [request()], files=files)


def test_a_snapshot_file_that_does_not_match_its_sha1_is_refused(tmp_path):
    files = archive_files()
    files[f"{sources.SNAPSHOT}/file/{sha1(OLD['oldpkg_1.0.orig.tar.gz'])}"] = b"old tamper.."
    with pytest.raises(sources.SourceError, match="SHA-1"):
        run(tmp_path, [request()], files=files)


def test_a_source_found_nowhere_is_refused_by_name(tmp_path):
    packages = request()["packages"] + [package("ghost", "ghost", "9.9-1")]
    with pytest.raises(sources.SourceError, match="ghost 9.9-1"):
        run(tmp_path, [request(packages=packages)])


def test_a_package_named_like_dockers_but_maintained_by_someone_else_is_refused(tmp_path):
    packages = [package("docker-ce", "docker-ce", "1.0", "Someone <a@example.org>")]
    with pytest.raises(sources.SourceError, match="docker-ce 1.0: not maintained by docker.com"):
        run(tmp_path, [request(packages=packages)])


def test_boards_that_name_different_versions_are_refused(tmp_path):
    with pytest.raises(sources.SourceError, match="one release version"):
        run(tmp_path, [request("rpi4", "0.5.0"), request("rpi5", "0.5.1")])


def test_the_sources_are_split_into_parts_that_stay_under_the_limit(tmp_path):
    limit = 3 * sources.TAR_BLOCK * 4 + sources.TAR_TRAILER
    manifest, output, _ = run(tmp_path, [request()], limit=limit)
    names = [part["name"] for part in manifest["parts"]]
    assert len(names) == 3
    assert names == [f"ems-solarflow-appliance-0.5.0-sources-{index}-of-3.tar" for index in (1, 2, 3)]
    assert all((output / name).stat().st_size <= limit for name in names)
    assert {record["part"] for record in manifest["sources"]} == set(names)


def test_a_source_larger_than_a_part_is_refused(tmp_path):
    with pytest.raises(sources.SourceError, match="larger than a part"):
        run(tmp_path, [request()], limit=sources.TAR_TRAILER + sources.TAR_BLOCK)


def test_nothing_but_the_parts_and_the_manifest_is_left_behind(tmp_path):
    _, output, _ = run(tmp_path, [request()])
    assert not (output / ".sources-cache").exists()


def test_the_command_line_fails_closed(tmp_path, capsys):
    path = tmp_path / "request.json"
    path.write_text(json.dumps(request(packages=[package("ghost", "ghost", "1")]) | {"archives": []}), encoding="utf-8")
    status = sources.main(["collect", "--request", str(path), "--revision", REVISION,
                           "--repository-url", REPOSITORY, "--output", str(tmp_path / "out")])
    assert status == 1
    assert "ghost 1" in capsys.readouterr().err


def test_the_tar_members_carry_no_builder_identity(tmp_path):
    manifest, output, _ = run(tmp_path, [request()])
    with tarfile.open(output / manifest["parts"][0]["name"]) as archive:
        for member in archive.getmembers():
            assert (member.mtime, member.uid, member.gid, member.uname, member.gname, member.mode) == (0, 0, 0, "", "", 0o644)


def test_a_tar_member_costs_what_the_plan_says():
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w", format=tarfile.GNU_FORMAT) as archive:
        name = "d" * 120 + "/f"
        archive.addfile(sources._tar_entry(name, 700), io.BytesIO(b"x" * 700))
    assert len(buffer.getvalue()) <= sources._tar_size(700) + sources.TAR_TRAILER


# --- what a review found -------------------------------------------------------


def test_a_clear_signed_dsc_names_its_tarballs():
    fields = sources.dsc_fields(EEPROM_DSC.decode())
    assert [entry["name"] for entry in sources._checksums(fields["Checksums-Sha256"])] == list(EEPROM_OLD)
    escaped = "-----BEGIN PGP SIGNED MESSAGE-----\nHash: SHA256\n\nSource: x\nChecksums-Sha256:\n abc 1 x.tar.xz\n- -escaped\n-----BEGIN PGP SIGNATURE-----\n"
    assert sources.dsc_fields(escaped)["Source"] == "x"


def test_the_pool_fallback_packs_the_tarballs_a_signed_dsc_lists(tmp_path):
    manifest, output, _ = run(tmp_path, [request()])
    eeprom = next(record for record in manifest["sources"] if record["source"] == "rpi-eeprom")
    assert sorted(entry["name"] for entry in eeprom["files"]) == sorted(["rpi-eeprom_28.34-1.dsc", *EEPROM_OLD])


def test_a_source_without_a_tarball_is_refused(tmp_path):
    files = archive_files()
    lonely = source_stanza("acl", "2.3.2-2", "pool/main/a/acl", {"acl_2.3.2-2.dsc": ACL["acl_2.3.2-2.dsc"]})
    files[f"{DEBIAN}/dists/trixie/main/source/Sources.xz"] = lzma.compress(lonely.encode())
    with pytest.raises(sources.SourceError, match="no .dsc and tarball"):
        run(tmp_path, [request()], files=files)


def test_the_sources_a_package_was_built_with_are_collected_too(tmp_path):
    packages = request()["packages"]
    packages[0] = packages[0] | {"built_using": [["glibc", "2.41-12"]]}
    manifest, _, _ = run(tmp_path, [request(packages=packages)])
    glibc = next(record for record in manifest["sources"] if record["source"] == "glibc")
    assert glibc["built_into"] == ["acl"] and glibc["binaries"] == []


def test_both_built_using_fields_are_read():
    fields = {"Built-Using": "gcc-14 (= 14.2.0-19), glibc (= 2.41-12)", "Static-Built-Using": "golang-x-sys (= 0.31.0-1)"}
    assert sources.built_using(fields) == [("gcc-14", "14.2.0-19"), ("glibc", "2.41-12"), ("golang-x-sys", "0.31.0-1")]


def test_a_package_dpkg_does_not_report_as_installed_stops_the_request():
    status = "Package: broken\nStatus: install ok half-configured\nVersion: 1\n"
    with pytest.raises(sources.SourceError, match="broken is 'install ok half-configured'"):
        sources.installed_packages(status)


def test_the_manager_points_at_its_release_tag_when_that_release_is_published(tmp_path):
    files = archive_files() | {f"{REPOSITORY}/releases/tag/appliance-manager-v0.5.0": b"<html>"}
    manifest, _, _ = run(tmp_path, [request()], files=files)
    assert manifest["project"][0]["where"] == f"{REPOSITORY}/tree/appliance-manager-v0.5.0"


def test_snapshot_naming_one_file_twice_is_refused(tmp_path):
    files = archive_files()
    files[f"{sources.SNAPSHOT}/mr/package/oldpkg/1.0-1/srcfiles?fileinfo=1"] = json.dumps({
        "fileinfo": {
            sha1(b"one"): [{"archive_name": "debian", "name": "oldpkg_1.0.orig.tar.gz", "size": 3}],
            sha1(b"two"): [{"archive_name": "debian", "name": "oldpkg_1.0.orig.tar.gz", "size": 3}],
        }
    }).encode()
    with pytest.raises(sources.SourceError, match="names two files"):
        run(tmp_path, [request()], files=files)


def test_a_docker_named_source_only_built_into_others_is_not_exempt(tmp_path):
    packages = request()["packages"]
    packages[0] = packages[0] | {"built_using": [["docker-ce", "1.0"]]}
    with pytest.raises(sources.SourceError, match="docker-ce 1.0: not maintained by docker.com"):
        run(tmp_path, [request(packages=packages)])


def test_the_manager_tag_is_asked_for_before_anything_is_downloaded(tmp_path):
    _, _, fetcher = run(tmp_path, [request()])
    tag = next(index for index, url in enumerate(fetcher.asked) if "/releases/tag/" in url)
    first_tarball = next(index for index, url in enumerate(fetcher.asked) if ".orig.tar" in url)
    assert tag < first_tarball


def test_a_download_broken_off_midway_is_fetched_again(tmp_path, monkeypatch):
    answers = [sources.http.client.IncompleteRead(b"par"), b"whole file"]

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            answer = answers.pop(0)
            if isinstance(answer, Exception):
                raise answer
            return answer

    fetcher = sources.Fetcher(attempts=2, pause=0)
    monkeypatch.setattr(fetcher, "_open", lambda url: Response())
    assert fetcher.get("https://example.org/x") == b"whole file"
    assert answers == []
