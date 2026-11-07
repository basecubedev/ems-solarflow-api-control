# SPDX-FileCopyrightText: 2026 basecubedev
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The notices of the Go modules compiled into the binaries the images ship.

The image builds run ``check`` on every binary they ship, so a binary built from
another revision, or holding a module its notices do not name, must be refused.
``generate`` must name every module of the pinned source with its license text,
each distinct text once. Build information is assembled here byte for byte in
the format Go writes, and the upstream is a fake fetcher, so nothing touches the
network.
"""

import base64
import hashlib
import importlib.util
import io
import json
import random
import struct
import tarfile
import zipfile
from pathlib import Path

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.documentation]

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("binary_notices", ROOT / "scripts" / "binary_notices.py")
notices = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(notices)

REVISION = "8cdf40161d7662e942582086853fca948fc1a842"
REAL_GLIBC = notices.GLIBC
MIT = "MIT License\n\nCopyright (c) 2021 Example\n"
APACHE = "Apache License\nVersion 2.0, January 2004\n"


def varint(value):
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        out.append(byte | (0x80 if value else 0))
        if not value:
            return bytes(out)


def buildinfo(lines, go="go1.25.9"):
    modinfo = ("x" * 16 + "\n".join(lines) + "\n" + "y" * 16).encode()
    header = notices.MAGIC + bytes([8, 2]) + b"\0" * 16
    return b"\0" * 64 + header + varint(len(go)) + go.encode() + varint(len(modinfo)) + modinfo + b"\0" * 64


def influx_binary(tmp_path, revision=REVISION, extra=()):
    lines = [
        "path\tgithub.com/influxdata/influx-cli/v2/cmd/influx",
        "mod\tgithub.com/influxdata/influx-cli/v2\tv2.8.0\t",
        "dep\tgithub.com/a/one\tv1.0.0\th1:x",
        "dep\tgithub.com/b/two\tv0.1.0\th1:y",
        "=>\tgithub.com/b/fork\tv0.2.0\th1:z",
        *extra,
        "build\t-ldflags=\" -X main.commit=" + revision + "\"",
        "build\tvcs.revision=" + revision,
    ]
    path = tmp_path / "influx"
    path.write_bytes(buildinfo(lines))
    return path


def notices_file(tmp_path, identity=REVISION, modules=(("github.com/a/one", "v1.0.0"), ("github.com/b/fork", "v0.2.0"))):
    gathered = {
        "component": "influx-cli",
        "version": identity,
        "identity": identity,
        "source": "https://github.com/influxdata/influx-cli at " + identity,
        "modules": sorted(modules),
        "licenses": {module: {"LICENSE": MIT} for module in modules},
    }
    path = tmp_path / "THIRD_PARTY_NOTICES"
    path.write_text(notices.render(gathered), encoding="utf-8")
    return path


class FakeFetcher:
    def __init__(self, files):
        self.files = files
        self.asked = []

    def get(self, url):
        self.asked.append(url)
        return self.files.get(url)

    def require(self, url):
        data = self.get(url)
        if data is None:
            raise notices.NoticeError(f"{url}: not found")
        return data

    def get_range(self, url, first, last=None):
        self.asked.append((url, first, last))
        data = self.files.get(url)
        if data is None:
            raise notices.NoticeError(f"{url}: not found")
        if first < 0:
            return data[max(len(data) + first, 0):], len(data)
        return data[first:len(data) if last is None else last + 1], len(data)


class CountingFetcher(FakeFetcher):
    served = 0

    def get_range(self, url, first, last=None):
        data, size = super().get_range(url, first, last)
        self.served += len(data)
        return data, size


def module_zip(path, version, files):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, text in files.items():
            archive.writestr(f"{path}@{version}/{name}", text)
    return buffer.getvalue()


def source_tarball(top, files, commit):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz", format=tarfile.PAX_FORMAT, pax_headers={"comment": commit}) as archive:
        for name, text in files.items():
            info = tarfile.TarInfo(f"{top}/{name}")
            info.size = len(text.encode())
            archive.addfile(info, io.BytesIO(text.encode()))
    return buffer.getvalue()


# --- build information ---------------------------------------------------------


def test_the_build_information_is_read_as_go_writes_it(tmp_path):
    info = notices.read_buildinfo(influx_binary(tmp_path).read_bytes())
    assert info["go"] == "go1.25.9"
    assert info["main"] == ("github.com/influxdata/influx-cli/v2", "v2.8.0")
    assert info["deps"] == [("github.com/a/one", "v1.0.0"), ("github.com/b/fork", "v0.2.0")]
    assert info["settings"]["-ldflags"] == " -X main.commit=" + REVISION
    assert notices.binary_identity(info, "vcs.revision") == REVISION


def test_a_quoted_ldflag_names_the_version_of_a_build_without_modules():
    data = buildinfo([
        "path\tgithub.com/docker/cli/cmd/docker",
        'build\t-ldflags=" -X \\"github.com/docker/cli/cli/version.Version=27.5.1\\" -extldflags -static"',
    ])
    info = notices.read_buildinfo(data)
    assert notices.binary_identity(info, notices.COMPONENTS["docker-cli"]["identity"]) == "27.5.1"


def test_a_file_that_is_not_a_go_binary_is_refused():
    with pytest.raises(notices.NoticeError, match="no Go build information"):
        notices.read_buildinfo(b"#!/bin/sh\necho hello\n")


# --- the check the image builds run ---------------------------------------------


def test_a_binary_its_notices_were_written_for_passes(tmp_path):
    assert notices.check("influx-cli", influx_binary(tmp_path), notices_file(tmp_path)) == []


def test_a_binary_of_another_revision_is_refused_with_the_command_that_fixes_it(tmp_path):
    other = "f" * 40
    problems = notices.check("influx-cli", influx_binary(tmp_path, revision=other), notices_file(tmp_path))
    assert len(problems) == 1
    assert f"generate --component influx-cli --revision {other}" in problems[0]


def test_a_binary_holding_a_module_its_notices_do_not_name_is_refused(tmp_path):
    binary = influx_binary(tmp_path, extra=("dep\tgithub.com/c/three\tv3.0.0\th1:w",))
    problems = notices.check("influx-cli", binary, notices_file(tmp_path))
    assert problems == [f"{binary} holds modules its notices do not name: github.com/c/three v3.0.0"]


def test_notices_written_for_another_component_are_refused(tmp_path):
    path = notices_file(tmp_path)
    path.write_text(path.read_text().replace("component: influx-cli", "component: docker-cli"), encoding="utf-8")
    assert any("written for 'docker-cli'" in problem for problem in notices.check("influx-cli", influx_binary(tmp_path), path))


def test_the_command_line_exits_non_zero_on_a_mismatch(tmp_path, capsys):
    binary = influx_binary(tmp_path, revision="e" * 40)
    status = notices.main(["check", "--component", "influx-cli", "--binary", str(binary), "--notices", str(notices_file(tmp_path))])
    assert status == 1
    assert "regenerate" in capsys.readouterr().err


# --- module lists ----------------------------------------------------------------


def test_go_mod_requires_are_read_with_their_replacements():
    text = """module github.com/influxdata/influx-cli/v2

go 1.22

require (
\tgithub.com/a/one v1.0.0
\tgithub.com/b/two v0.1.0 // indirect
\tgithub.com/local/part v0.0.0
)

require github.com/c/three v3.0.0

require(
	github.com/d/four v4.0.0
)

replace github.com/b/two => github.com/b/fork v0.2.0

replace github.com/local/part => ./internal/part
"""
    assert notices.parse_go_mod(text) == [
        ("github.com/a/one", "v1.0.0"),
        ("github.com/b/fork", "v0.2.0"),
        ("github.com/c/three", "v3.0.0"),
        ("github.com/d/four", "v4.0.0"),
    ]


def test_only_vendored_modules_with_a_package_count():
    text = (
        "# github.com/a/one v1.0.0\n## explicit; go 1.20\ngithub.com/a/one\n"
        "# github.com/b/unused v0.1.0\n## explicit\n"
        "# github.com/c/three v3.0.0 => github.com/c/fork v3.0.1\ngithub.com/c/fork/pkg\n"
    )
    assert notices.parse_vendor_modules(text) == [
        ("github.com/a/one", "v1.0.0", "github.com/a/one"),
        ("github.com/c/fork", "v3.0.1", "github.com/c/three"),
    ]


def test_the_module_proxy_path_encodes_upper_case():
    assert notices.escape_module("github.com/BurntSushi/toml") == "github.com/!burnt!sushi/toml"


# --- generating --------------------------------------------------------------------


def go_mod_upstream(missing_license=False):
    go_mod = "module x\n\nrequire (\n\tgithub.com/a/one v1.0.0\n\tgithub.com/b/two v0.1.0\n)\n"
    return {
        f"{notices.RAW}/influxdata/influx-cli/{REVISION}/go.mod": go_mod.encode(),
        f"{notices.PROXY}/github.com/a/one/@v/v1.0.0.zip": module_zip("github.com/a/one", "v1.0.0", {"LICENSE": MIT, "main.go": "package one"}),
        f"{notices.PROXY}/github.com/b/two/@v/v0.1.0.zip": module_zip(
            "github.com/b/two", "v0.1.0", {"README": "no license"} if missing_license else {"LICENSE.txt": MIT, "NOTICE": "Two\n", "sub/LICENSE": "Sub notice", "testdata/LICENSE": "test only", "_old/LICENSE": "ignored dir"}
        ),
        f"{notices.RAW}/golang/go/{notices.GO_LICENSE[1]}/LICENSE": b"Copyright 2009 The Go Authors.\n",
    }


def test_the_notices_name_every_module_and_each_text_once():
    fetcher = FakeFetcher(go_mod_upstream())
    gathered = notices.gather("influx-cli", fetcher, REVISION)
    text = notices.render(gathered)
    header, modules = notices.parse_notices(text)
    assert header["identity"] == REVISION and header["component"] == "influx-cli"
    assert modules == {("github.com/a/one", "v1.0.0"), ("github.com/b/two", "v0.1.0")}
    assert text.count("Copyright (c) 2021 Example") == 1
    assert "LICENSE, LICENSE.txt of:\n  github.com/a/one v1.0.0\n  github.com/b/two v0.1.0" in text
    assert "The Go Authors" in text
    assert "LICENSE of:\n  github.com/b/two/sub v0.1.0\n" + "-" * 78 + "\nSub notice" in text
    assert "test only" not in text and "ignored dir" not in text
    assert notices.render(notices.gather("influx-cli", FakeFetcher(go_mod_upstream()), REVISION)) == text


def test_a_module_without_a_license_file_stops_the_generator():
    with pytest.raises(notices.NoticeError, match="no license file in github.com/b/two v0.1.0"):
        notices.gather("influx-cli", FakeFetcher(go_mod_upstream(missing_license=True)), REVISION)


def test_a_vendored_build_takes_its_licenses_from_the_vendor_tree(monkeypatch):
    monkeypatch.setattr(notices, "admin_pin", lambda name: "27.5.1")
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for name, data in {
            "cli-27.5.1/vendor/modules.txt": "# github.com/a/one v1.0.0\ngithub.com/a/one\n",
            "cli-27.5.1/vendor/github.com/a/one/LICENSE": MIT,
            "cli-27.5.1/vendor/github.com/a/one/one.go": "package one",
        }.items():
            info = tarfile.TarInfo(name)
            info.size = len(data.encode())
            archive.addfile(info, io.BytesIO(data.encode()))
    fetcher = FakeFetcher({
        f"{notices.CODELOAD}/docker/cli/tar.gz/refs/tags/v27.5.1": buffer.getvalue(),
        f"{notices.RAW}/golang/go/{notices.GO_LICENSE[1]}/LICENSE": b"Copyright 2009 The Go Authors.\n",
    })
    gathered = notices.gather("docker-cli", fetcher)
    assert (gathered["version"], gathered["identity"]) == ("27.5.1", "27.5.1")
    assert gathered["modules"] == [("github.com/a/one", "v1.0.0")]
    assert gathered["licenses"][("github.com/a/one", "v1.0.0")] == {"LICENSE": MIT}


def test_a_pinned_go_mod_build_records_the_commit_its_tag_names(monkeypatch):
    monkeypatch.setattr(notices, "admin_pin", lambda name: "2.32.4")
    files = go_mod_upstream()
    files[f"{notices.RAW}/docker/compose/{REVISION}/go.mod"] = files.pop(f"{notices.RAW}/influxdata/influx-cli/{REVISION}/go.mod")
    files[f"{notices.API}/repos/docker/compose/commits/v2.32.4"] = json.dumps({"sha": REVISION}).encode()
    gathered = notices.gather("docker-compose", FakeFetcher(files))
    assert (gathered["version"], gathered["identity"]) == ("2.32.4", REVISION)


def test_the_shipped_notices_parse_and_name_their_component():
    for name in notices.COMPONENTS:
        header, modules = notices.parse_notices((ROOT / "third_party" / name / notices.NOTICES).read_text(encoding="utf-8"))
        assert header["component"] == name and header["identity"] and modules


def test_a_replaced_vendored_module_takes_its_license_from_where_go_put_it(monkeypatch):
    monkeypatch.setattr(notices, "admin_pin", lambda name: "27.5.1")
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for name, data in {
            "cli-27.5.1/vendor/modules.txt": "# github.com/c/three v3.0.0 => github.com/c/fork v3.0.1\ngithub.com/c/three\n",
            "cli-27.5.1/vendor/github.com/c/three/LICENSE": MIT,
        }.items():
            info = tarfile.TarInfo(name)
            info.size = len(data.encode())
            archive.addfile(info, io.BytesIO(data.encode()))
    fetcher = FakeFetcher({
        f"{notices.CODELOAD}/docker/cli/tar.gz/refs/tags/v27.5.1": buffer.getvalue(),
        f"{notices.RAW}/golang/go/{notices.GO_LICENSE[1]}/LICENSE": b"Copyright 2009 The Go Authors.\n",
    })
    gathered = notices.gather("docker-cli", fetcher)
    assert gathered["modules"] == [("github.com/c/fork", "v3.0.1")]
    assert gathered["licenses"][("github.com/c/fork", "v3.0.1")] == {"LICENSE": MIT}


@pytest.fixture(autouse=True)
def fake_glibc_pins(monkeypatch):
    version, pinned = notices.GLIBC["13"]
    monkeypatch.setattr(notices, "GLIBC", {"13": (version, {file: hashlib.sha256(f"glibc {file}\n".encode()).hexdigest() for file in pinned})})


def test_the_real_glibc_pins_are_sha256_digests_of_both_texts():
    assert REAL_GLIBC is not notices.GLIBC
    for version, pinned in REAL_GLIBC.values():
        assert version and set(pinned) == {"COPYING.LIB", "LICENSES"}
        assert all(len(digest) == 64 and int(digest, 16) >= 0 for digest in pinned.values())


def elf(interpreter, wide=True):
    """An ELF header with one program header, PT_INTERP or PT_LOAD."""

    kind = notices.PT_INTERP if interpreter else 1
    if wide:
        header = b"\x7fELF\x02\x01" + b"\0" * 26 + (64).to_bytes(8, "little") + b"\0" * 14 + (56).to_bytes(2, "little") + (1).to_bytes(2, "little")
        return header.ljust(64, b"\0") + kind.to_bytes(4, "little") + b"\0" * 52
    header = b"\x7fELF\x01\x02" + b"\0" * 22 + (52).to_bytes(4, "big") + b"\0" * 10 + (32).to_bytes(2, "big") + (1).to_bytes(2, "big")
    return header.ljust(52, b"\0") + kind.to_bytes(4, "big") + b"\0" * 28


def test_a_module_zip_is_read_for_its_license_files_only():
    noise = random.Random(0).randbytes(1 << 20)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr("github.com/a/one@v1.0.0/LICENSE", MIT)
        archive.writestr("github.com/a/one@v1.0.0/data.bin", noise)
        archive.writestr("github.com/a/one@v1.0.0/sub/LICENSE", "nested")
        archive.writestr("github.com/a/one@v1.0.0/testdata/LICENSE", "test only")
    data = buffer.getvalue()
    fetcher = CountingFetcher({f"{notices.PROXY}/github.com/a/one/@v/v1.0.0.zip": data})
    assert notices.module_licenses("github.com/a/one", "v1.0.0", fetcher) == {"LICENSE": MIT, "sub/LICENSE": "nested\n"}
    assert all(isinstance(asked, tuple) for asked in fetcher.asked)
    assert fetcher.served < len(data) // 4


def test_a_server_that_ignores_the_range_still_answers_the_bytes_asked_for(monkeypatch):
    fetcher = notices.Fetcher(attempts=1, pause=0)
    monkeypatch.setattr(fetcher, "_fetch", lambda url, headers=None: (200, {}, b"0123456789"))
    assert fetcher.get_range("https://example.org/x", -3) == (b"789", 10)
    assert fetcher.get_range("https://example.org/x", 2, 4) == (b"234", 10)
    monkeypatch.setattr(fetcher, "_fetch", lambda url, headers=None: (206, {"Content-Range": "bytes 7-9/10"}, b"789"))
    assert fetcher.get_range("https://example.org/x", -3) == (b"789", 10)
    monkeypatch.setattr(fetcher, "_fetch", lambda url, headers=None: (206, {}, b"789"))
    with pytest.raises(notices.NoticeError, match="without its size"):
        fetcher.get_range("https://example.org/x", -3)


def test_a_broken_answer_is_tried_again(monkeypatch):
    answers = [notices.http.client.IncompleteRead(b"01"), OSError("connection reset"), b"0123"]

    class Response:
        status, headers = 200, {}

        def __init__(self, body):
            self.body = body

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return self.body

    def urlopen(request, timeout):
        answer = answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return Response(answer)

    monkeypatch.setattr(notices.urllib.request, "urlopen", urlopen)
    assert notices.Fetcher(attempts=3, pause=0).get("https://example.org/x") == b"0123"
    assert answers == []


# --- the Docker programs of an appliance image -------------------------------------


MOBY = "0367cbeb49b9d98cb0b4f51e95f7b54b4803e76c"
CLI = "a82e528901260c8df6073311379eac65f92db204"
TINI = "de40ad007797e0dcd8b7126f27bb87401d224240"
CONTAINERD = "b7a25261e029d68978bbf290df0a62fc819b3c51"
BSD = "BSD 3-Clause License\n\nCopyright (c) 2019 Three\n"
GO_TEXT = "Copyright 2009 The Go Authors.\n"


def program(path, package, **fields):
    return {"package": package, "version": "5:29.9.2-1~debian.13~trixie", "path": path, "size": 8, "sha256": path.encode().hex()[:64].ljust(64, "0"), **fields}


def docker_programs():
    return [
        program("/usr/bin/containerd", "containerd.io", kind="go", go="go1.27.3", package_path="github.com/containerd/containerd/v2/cmd/containerd",
                main=["github.com/containerd/containerd/v2", "v2.4.2"], deps=[["github.com/a/one", "v1.0.0"]],
                settings={"CGO_ENABLED": "1", "vcs.revision": CONTAINERD}, interpreter=True),
        program("/usr/bin/dockerd", "docker-ce", kind="go", go="go1.26.10", package_path="github.com/moby/moby/v2/cmd/dockerd",
                main=["github.com/moby/moby/v2", "(devel)"], deps=[["github.com/a/one", "v1.0.0"], ["github.com/b/two", "v0.1.0"]],
                settings={"-ldflags": '-w -X "github.com/moby/moby/v2/dockerversion.Version=29.9.2" -X "github.com/moby/moby/v2/dockerversion.GitCommit=0367cbe"',
                          "CGO_ENABLED": "1"}, interpreter=True),
        program("/usr/bin/docker", "docker-ce-cli", kind="go", go="go1.26.10", package_path="github.com/docker/cli/cmd/docker", main=None, deps=[],
                settings={"-ldflags": ' -X "github.com/docker/cli/cli/version.GitCommit=a82e528" -X "github.com/docker/cli/cli/version.Version=29.9.2"',
                          "CGO_ENABLED": "0"}, interpreter=False),
        program("/usr/libexec/docker/docker-init", "docker-ce", kind="tini", tini_version="0.19.0", commit="de40ad0", modified="", static_glibc=True,
                interpreter=False),
    ]


def docker_request(board="rpi4", programs=None, release_version="0.5.0"):
    return {"board": board, "release_version": release_version, "programs": docker_programs() if programs is None else programs}


def docker_upstream():
    version, files = notices.GLIBC["13"]
    served = {
        f"{notices.PROXY}/github.com/containerd/containerd/v2/@v/v2.4.2.zip": module_zip(
            "github.com/containerd/containerd/v2", "v2.4.2", {"LICENSE": APACHE, "NOTICE": "containerd notice\n", "main.go": "package main"}),
        f"{notices.PROXY}/github.com/containerd/containerd/v2/@v/v2.4.2.info": json.dumps(
            {"Version": "v2.4.2", "Origin": {"VCS": "git", "Hash": CONTAINERD, "Ref": "refs/tags/v2.4.2"}}).encode(),
        f"{notices.PROXY}/github.com/a/one/@v/v1.0.0.zip": module_zip("github.com/a/one", "v1.0.0", {"LICENSE": MIT}),
        f"{notices.PROXY}/github.com/b/two/@v/v0.1.0.zip": module_zip("github.com/b/two", "v0.1.0", {"LICENSE": MIT}),
        f"{notices.CODELOAD}/moby/moby/tar.gz/refs/tags/docker-v29.9.2": source_tarball(
            "moby-docker-v29.9.2", {"LICENSE": APACHE, "NOTICE": "Docker notice\n", "daemon/daemon.go": "package daemon"}, MOBY),
        f"{notices.CODELOAD}/docker/cli/tar.gz/refs/tags/v29.9.2": source_tarball("cli-29.9.2", {
            "LICENSE": APACHE, "NOTICE": "Docker CLI notice\n",
            "vendor/modules.txt": "# github.com/c/three v3.0.0\ngithub.com/c/three\n",
            "vendor/github.com/c/three/LICENSE": BSD,
        }, CLI),
        f"{notices.CODELOAD}/{notices.TINI_REPOSITORY}/tar.gz/refs/tags/v0.19.0": source_tarball("tini-0.19.0", {"LICENSE": "tini MIT\n"}, TINI),
        f"{notices.RAW}/golang/go/go1.26.10/LICENSE": GO_TEXT.encode(),
        f"{notices.RAW}/golang/go/go1.27.3/LICENSE": GO_TEXT.encode(),
    }
    served.update({notices.GLIBC_URL.format(name=file, version=version): f"glibc {file}\n".encode() for file in files})
    return served


def test_the_appliance_notices_name_each_docker_program_and_every_module_in_it():
    fetcher = FakeFetcher(docker_upstream())
    text = notices.render_appliance(notices.gather_appliance([docker_request("rpi4"), docker_request("rpi5")], fetcher))
    modules = text.split("Modules compiled into them:\n", 1)[1].split("\n\n", 1)[0].splitlines()
    assert modules == [
        "  github.com/a/one v1.0.0", "  github.com/b/two v0.1.0", "  github.com/c/three v3.0.0", "  github.com/containerd/containerd/v2 v2.4.2",
    ]
    assert "boards: rpi4, rpi5" in text and "\n    boards:" not in text
    assert f"source: https://github.com/moby/moby at docker-v29.9.2 ({MOBY})" in text
    assert f"source: https://github.com/docker/cli at v29.9.2 ({CLI})" in text
    assert "source: module github.com/containerd/containerd/v2 v2.4.2" in text
    assert f"source: https://github.com/krallin/tini at v0.19.0 ({TINI}), statically linked with the GNU C Library of Debian 13 (glibc 2.41)" in text
    for own in ("Docker notice", "Docker CLI notice", "containerd notice", "tini MIT", "glibc COPYING.LIB", "glibc LICENSES", BSD.strip()):
        assert own in text
    assert text.count("Apache License") == 1 and text.count("Copyright (c) 2021 Example") == 1
    assert "  std go1.26.10\n  std go1.27.3\n" in text and text.count("The Go Authors") == 1
    assert not any("github.com/c/three" in str(asked) for asked in fetcher.asked)
    assert not [asked for asked in fetcher.asked if isinstance(asked, str) and asked.endswith(".zip")]
    assert f"{notices.PROXY}/github.com/containerd/containerd/v2/@v/v2.4.2.info" in fetcher.asked


def test_a_program_on_one_board_only_says_which():
    rpi5 = docker_programs() + [program("/usr/bin/runc", "containerd.io", kind="go", go="go1.27.3", package_path="github.com/opencontainers/runc",
                                        main=["github.com/a/one", "v1.0.0"], deps=[], settings={}, interpreter=True)]
    text = notices.render_appliance(notices.gather_appliance([docker_request("rpi4"), docker_request("rpi5", rpi5)], FakeFetcher(docker_upstream())))
    assert "  /usr/bin/runc\n    package: containerd.io 5:29.9.2-1~debian.13~trixie\n" in text
    assert text.split("  /usr/bin/runc\n", 1)[1].splitlines()[2] == "    boards: rpi5"


@pytest.mark.parametrize("change, message", [
    (lambda programs: programs[3].update(commit="deadbee"), f"krallin/tini v0.19.0 is commit {TINI}, the program says deadbee"),
    (lambda programs: programs[1]["settings"].update({"-ldflags": '-X "github.com/moby/moby/v2/dockerversion.Version=29.9.2"'}), "the program says nothing"),
    (lambda programs: programs[2]["settings"].update({"-ldflags": ""}), "names no version"),
    (lambda programs: programs.append(program("/usr/bin/x", "docker-ce", kind="unknown", reason="no Go build information")), r"/usr/bin/x \(docker-ce 5:29.9.2-1~debian.13~trixie\): no Go build information"),
    (lambda programs: programs.append(program("/usr/bin/y", "docker-ce", kind="go", go="go1.26.10", package_path="example.com/y", main=["example.com/y", "(devel)"], deps=[], settings={}, interpreter=False)), "no rule says where its source is"),
    (lambda programs: programs[3].update(modified="-dirty"), r"built from a modified tree \(-dirty\)"),
    (lambda programs: programs[3].update(static_glibc=False), "statically linked with a C library these notices do not know"),
    (lambda programs: programs[0].update(interpreter=False), "links C code in statically"),
    (lambda programs: programs[1]["settings"].update({"-ldflags": programs[1]["settings"]["-ldflags"] + ' -X "github.com/moby/moby/v2/dockerversion.GitCommit=deadbee"'}), "the program says deadbee"),
    (lambda programs: programs[1]["settings"].update({"-ldflags": programs[1]["settings"]["-ldflags"].replace("GitCommit=0367cbe", "GitCommit=0")}), "the program says 0$"),
    (lambda programs: programs[0]["deps"].append(["github.com/d/none", "v1.0.0"]), "github.com/d/none/@v/v1.0.0.zip: not found"),
    (lambda programs: programs[1].update(main=None, deps=[]), "records no modules, and its rule reads none from a vendor tree"),
    (lambda programs: programs[1].update(deps=[]), "records no modules"),
    (lambda programs: programs[1]["settings"].update({"vcs.modified": "true"}), "built from a modified tree"),
    (lambda programs: programs[0]["settings"].update({"vcs.modified": "true"}), "built from a modified tree"),
    (lambda programs: programs[1]["settings"].update({"vcs.revision": "1" * 40}), f"built from {'1' * 40}, but moby/moby docker-v29.9.2 is {MOBY}"),
    (lambda programs: programs[0].update(go="go1.27.3 X:boringcrypto"), "Go experiment X:boringcrypto"),
    (lambda programs: programs[0]["settings"].update({"GOEXPERIMENT": "boringcrypto"}), "Go experiment boringcrypto"),
    (lambda programs: programs[0]["settings"].update({"-ldflags": '-extldflags "-Wl,-Bstatic -lseccomp -Wl,-Bdynamic"'}), "links C code in statically"),
    (lambda programs: programs[0]["settings"].update({"CGO_LDFLAGS": "-static"}), "links C code in statically"),
    (lambda programs: programs[3].update(version="5:30.0.0-1~debian.14~forky"), "glibc of a Debian release these notices do not pin"),
    (lambda programs: programs[3].update(version="5:29.9.2-1"), "glibc of a Debian release these notices do not pin"),
    (lambda programs: programs[0]["settings"].update({"vcs.revision": "1" * 40}), f"built from {'1' * 40}, but github.com/containerd/containerd/v2 v2.4.2 is {CONTAINERD}"),
    (lambda programs: programs[0]["settings"].update({"-race": "true"}), "built with -race"),
    (lambda programs: programs[0]["settings"].update({"CGO_LDFLAGS": "-l:libseccomp.a"}), "links C code in statically"),
    (lambda programs: programs[0]["settings"].update({"-ldflags": '-extldflags "-Wl,--whole-archive -lseccomp"'}), "links C code in statically"),
    (lambda programs: programs[0]["settings"].update({"-ldflags": '-extldflags "/usr/lib/libseccomp.a"'}), "links C code in statically"),
    (lambda programs: programs[0].update(go=""), "names no Go version"),
    (lambda programs: programs[1]["deps"].append(["./api", ""]), "replaces ./api with a local directory"),
])
def test_a_docker_program_the_notices_cannot_vouch_for_stops_the_run(change, message):
    programs = docker_programs()
    change(programs)
    with pytest.raises(notices.NoticeError, match=message):
        notices.gather_appliance([docker_request(programs=programs)], FakeFetcher(docker_upstream()))


def test_dynamically_linked_tini_needs_no_c_library_texts():
    programs = docker_programs()
    programs[3].update(static_glibc=False, interpreter=True)
    text = notices.render_appliance(notices.gather_appliance([docker_request(programs=programs)], FakeFetcher(docker_upstream())))
    assert f"source: https://github.com/krallin/tini at v0.19.0 ({TINI})\n" in text and "glibc" not in text


def test_glibc_texts_that_are_not_the_pinned_bytes_stop_the_run():
    files = docker_upstream() | {notices.GLIBC_URL.format(name="LICENSES", version="2.41"): b"<html>Too Many Requests</html>"}
    with pytest.raises(notices.NoticeError, match="not the LICENSES of glibc 2.41"):
        notices.gather_appliance([docker_request()], FakeFetcher(files))


def test_a_main_module_the_proxy_names_no_commit_for_is_refused():
    files = docker_upstream()
    files[f"{notices.PROXY}/github.com/containerd/containerd/v2/@v/v2.4.2.info"] = json.dumps({"Version": "v2.4.2"}).encode()
    with pytest.raises(notices.NoticeError, match="is of no recorded commit"):
        notices.gather_appliance([docker_request()], FakeFetcher(files))


def test_a_main_module_without_a_recorded_revision_is_not_asked_about():
    programs = docker_programs()
    del programs[0]["settings"]["vcs.revision"]
    fetcher = FakeFetcher(docker_upstream())
    notices.gather_appliance([docker_request(programs=programs)], fetcher)
    assert not any(str(asked).endswith(".info") for asked in fetcher.asked)


def test_source_files_named_like_a_license_are_not_license_texts():
    for name in ("LICENSE", "License", "LICENSE.md", "LICENSE-APACHE-2.0.txt", "LICENSE.libyaml", "COPYING.LIB", "NOTICE.txt", "PATENTS"):
        assert notices.is_license_file(name), name
    for name in ("license.go", "License.java", "NOTICE.sh", "copying.c", "patents.go"):
        assert not notices.is_license_file(name), name
    files = docker_upstream()
    files[f"{notices.PROXY}/github.com/b/two/@v/v0.1.0.zip"] = module_zip("github.com/b/two", "v0.1.0", {"license.go": "package two"})
    with pytest.raises(notices.NoticeError, match="no license file at the root of github.com/b/two v0.1.0"):
        notices.gather_appliance([docker_request()], FakeFetcher(files))


def test_a_failing_appliance_run_exits_non_zero_and_writes_nothing(tmp_path, monkeypatch, capsys):
    request = tmp_path / "os-sources-rpi4.json"
    request.write_text(json.dumps(docker_request(programs=[program("/usr/bin/x", "docker-ce", kind="unknown", reason="no banner")])), encoding="utf-8")
    monkeypatch.setattr(notices, "Fetcher", lambda: FakeFetcher(docker_upstream()))
    assert notices.main(["appliance", "--request", str(request), "--output", str(tmp_path / "release")]) == 1
    assert "binary-notices: /usr/bin/x" in capsys.readouterr().err
    assert not (tmp_path / "release").exists()


def test_a_statically_linked_go_program_without_c_code_needs_no_rule():
    programs = docker_programs()
    programs[0].update(interpreter=False)
    programs[0]["settings"].update({"CGO_ENABLED": "0", "-ldflags": '-extldflags "-static" -s -w'})
    text = notices.render_appliance(notices.gather_appliance([docker_request(programs=programs)], FakeFetcher(docker_upstream())))
    assert "source: module github.com/containerd/containerd/v2 v2.4.2" in text


def test_a_vendored_rule_whose_tree_has_no_vendor_list_is_refused():
    files = docker_upstream()
    files[f"{notices.CODELOAD}/docker/cli/tar.gz/refs/tags/v29.9.2"] = source_tarball("cli-29.9.2", {"LICENSE": APACHE}, CLI)
    with pytest.raises(notices.NoticeError, match="docker/cli v29.9.2 has no vendor/modules.txt"):
        notices.gather_appliance([docker_request()], FakeFetcher(files))


def test_a_vendored_build_that_also_records_modules_keeps_them():
    programs = docker_programs()
    programs[2]["deps"] = [["github.com/b/two", "v0.1.0"]]
    text = notices.render_appliance(notices.gather_appliance([docker_request(programs=programs[2:3])], FakeFetcher(docker_upstream())))
    assert text.split("Modules compiled into them:\n", 1)[1].split("\n\n", 1)[0].splitlines() == ["  github.com/b/two v0.1.0", "  github.com/c/three v3.0.0"]


def test_the_same_bytes_in_two_package_revisions_name_both():
    rpi5 = docker_programs()
    rpi5[0] = {**rpi5[0], "version": "2.4.2-4~debian.13~trixie"}
    text = notices.render_appliance(notices.gather_appliance([docker_request("rpi4"), docker_request("rpi5", rpi5)], FakeFetcher(docker_upstream())))
    assert "    package: containerd.io 5:29.9.2-1~debian.13~trixie\n" in text and "    package: containerd.io 2.4.2-4~debian.13~trixie\n" in text


def test_the_last_ldflag_for_a_variable_is_the_one_the_binary_carries():
    info = {"settings": {"-ldflags": '-X "a/version.Version=1.0" -X a/version.Version=2.0'}}
    assert notices.binary_identity(info, "ldflag:a/version.Version") == "2.0"


def test_a_module_without_a_license_stops_the_appliance_notices():
    files = docker_upstream()
    files[f"{notices.PROXY}/github.com/b/two/@v/v0.1.0.zip"] = module_zip("github.com/b/two", "v0.1.0", {"README": "none"})
    with pytest.raises(notices.NoticeError, match="no license file at the root of github.com/b/two v0.1.0"):
        notices.gather_appliance([docker_request()], FakeFetcher(files))


def test_requests_that_disagree_or_predate_the_programs_are_refused():
    with pytest.raises(notices.NoticeError, match="one release version"):
        notices.gather_appliance([docker_request("rpi4"), docker_request("rpi5", release_version="0.5.1")], FakeFetcher(docker_upstream()))
    with pytest.raises(notices.NoticeError, match="predates them"):
        notices.gather_appliance([{"board": "rpi4", "release_version": "0.5.0"}], FakeFetcher(docker_upstream()))
    with pytest.raises(notices.NoticeError, match="no Docker program"):
        notices.gather_appliance([docker_request(programs=[])], FakeFetcher(docker_upstream()))


def test_the_appliance_command_writes_the_release_file(tmp_path, monkeypatch, capsys):
    request = tmp_path / "os-sources-rpi4.json"
    request.write_text(json.dumps(docker_request()), encoding="utf-8")
    monkeypatch.setattr(notices, "Fetcher", lambda: FakeFetcher(docker_upstream()))
    assert notices.main(["appliance", "--request", str(request), "--output", str(tmp_path / "release")]) == 0
    written = tmp_path / "release" / "ems-solarflow-appliance-0.5.0-docker-notices.txt"
    assert written.read_text(encoding="utf-8").startswith("Third-party notices for the Docker programs of the EMS SolarFlow appliance image 0.5.0\n")
    assert "4 programs, 4 modules" in capsys.readouterr().out


def test_a_program_says_what_it_is():
    go = notices.describe_program(b"\x7fELF" + buildinfo([
        "path\tgithub.com/docker/buildx/cmd/buildx", "mod\tgithub.com/docker/buildx\tv0.38.1\t", "dep\tgithub.com/a/one\tv1.0.0\t", "build\tvcs.revision=abc",
    ]))
    assert go == {"kind": "go", "go": "go1.25.9", "package_path": "github.com/docker/buildx/cmd/buildx", "main": ["github.com/docker/buildx", "v0.38.1"],
                  "deps": [["github.com/a/one", "v1.0.0"]], "settings": {"vcs.revision": "abc"}, "interpreter": False, "needed": []}
    tini = notices.describe_program(elf(False) + b"tini version 0.19.0 - git.de40ad0\0Fatal glibc error: x\0")
    assert tini == {"kind": "tini", "tini_version": "0.19.0", "commit": "de40ad0", "modified": "", "static_glibc": True, "interpreter": False, "needed": []}
    assert notices.describe_program(elf(True) + b"tini version 0.19.0 - git.de40ad0\0")["static_glibc"] is False
    assert notices.describe_program(b"tini version 0.19.0 - git.de40ad0-dirty\0")["modified"] == "-dirty"
    assert notices.describe_program(b"\x7fELF\0\0")["kind"] == "unknown"
    assert notices.describe_program(b"\x7fELF" + notices.MAGIC + b"\0" * 40)["kind"] == "unknown"
    assert notices.describe_program(b"\x7fELF" + notices.MAGIC + b"\0\x02")["kind"] == "unknown"


def test_a_program_that_names_a_loader_is_told_from_one_that_carries_its_libc():
    assert notices.has_interpreter(elf(True)) and notices.has_interpreter(elf(True, wide=False))
    assert not notices.has_interpreter(elf(False)) and not notices.has_interpreter(elf(False, wide=False))
    assert not notices.has_interpreter(b"\x7fELF\x02\x01")


def test_the_boards_of_a_program_do_not_depend_on_the_order_of_the_requests():
    text = notices.render_appliance(notices.gather_appliance([docker_request("rpi5"), docker_request("rpi4")], FakeFetcher(docker_upstream())))
    assert "boards: rpi4, rpi5" in text and "\n    boards:" not in text


MPL = "Mozilla Public License, version 2.0\n"


def sumdb_line(path, version, data):
    return f"123\n{path} {version} {notices.zip_hash(data)}\n{path} {version}/go.mod h1:x\n\ngo.sum database tree\n".encode()


def copyleft_upstream(served_hash=None):
    files = docker_upstream()
    data = module_zip("github.com/h/mpl", "v1.0.0", {"LICENSE": MPL, "mpl.go": "package mpl"})
    files[f"{notices.PROXY}/github.com/h/mpl/@v/v1.0.0.zip"] = data
    files[f"{notices.SUMDB}/lookup/github.com/h/mpl@v1.0.0"] = served_hash if served_hash is not None else sumdb_line("github.com/h/mpl", "v1.0.0", data)
    return files, data


def with_copyleft_dep():
    programs = docker_programs()
    programs[1]["deps"].append(["github.com/h/mpl", "v1.0.0"])
    return programs


def test_the_go_module_hash_is_sha256_over_the_sorted_file_lines():
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("m@v1/xyz", "data for xyz")
        archive.writestr("m@v1/abc", "data for abc")
    lines = "".join(f"{hashlib.sha256(f'data for {name}'.encode()).hexdigest()}  m@v1/{name}\n" for name in ("abc", "xyz"))
    assert notices.zip_hash(buffer.getvalue()) == "h1:" + base64.b64encode(hashlib.sha256(lines.encode()).digest()).decode()


def test_a_copyleft_module_has_its_verified_source_beside_the_notices(tmp_path, monkeypatch):
    files, data = copyleft_upstream()
    request = tmp_path / "os-sources-rpi4.json"
    request.write_text(json.dumps(docker_request(programs=with_copyleft_dep())), encoding="utf-8")
    monkeypatch.setattr(notices, "Fetcher", lambda: FakeFetcher(files))
    assert notices.main(["appliance", "--request", str(request), "--output", str(tmp_path / "release")]) == 0
    text = (tmp_path / "release" / "ems-solarflow-appliance-0.5.0-docker-notices.txt").read_text(encoding="utf-8")
    assert "in ems-solarflow-appliance-0.5.0-docker-module-sources.tar, as the Go module" in text
    assert text.split("records its hash:\n", 1)[1].split("\n\n", 1)[0] == "  github.com/h/mpl v1.0.0"
    with tarfile.open(tmp_path / "release" / "ems-solarflow-appliance-0.5.0-docker-module-sources.tar") as archive:
        members = archive.getmembers()
        assert [member.name for member in members] == ["github.com/h/mpl@v1.0.0.zip"]
        assert archive.extractfile(members[0]).read() == data
        assert (members[0].mtime, members[0].uid, members[0].uname, members[0].mode) == (0, 0, "", 0o644)


def test_without_a_copyleft_module_no_source_archive_is_written(tmp_path, monkeypatch):
    request = tmp_path / "os-sources-rpi4.json"
    request.write_text(json.dumps(docker_request()), encoding="utf-8")
    monkeypatch.setattr(notices, "Fetcher", lambda: FakeFetcher(docker_upstream()))
    assert notices.main(["appliance", "--request", str(request), "--output", str(tmp_path / "release")]) == 0
    assert sorted(path.name for path in (tmp_path / "release").iterdir()) == ["ems-solarflow-appliance-0.5.0-docker-notices.txt"]
    assert "records its hash" not in (tmp_path / "release" / "ems-solarflow-appliance-0.5.0-docker-notices.txt").read_text(encoding="utf-8")


@pytest.mark.parametrize("served, message", [
    (b"123\ngithub.com/h/mpl v1.0.0 h1:AAAA\n", r"not the one sum.golang.org records \(h1:AAAA\)"),
    (b"123\ngithub.com/h/other v1.0.0 h1:AAAA\n", r"records \(nothing\)"),
])
def test_a_copyleft_module_whose_zip_sum_golang_org_does_not_vouch_for_stops_the_run(served, message):
    files, _ = copyleft_upstream(served)
    with pytest.raises(notices.NoticeError, match=message):
        notices.gather_appliance([docker_request(programs=with_copyleft_dep())], FakeFetcher(files))


def test_a_copyleft_text_in_a_program_s_own_tree_stops_the_run():
    files = docker_upstream()
    files[f"{notices.CODELOAD}/moby/moby/tar.gz/refs/tags/docker-v29.9.2"] = source_tarball(
        "moby-docker-v29.9.2", {"LICENSE": APACHE, "pkg/forked/LICENSE": MPL}, MOBY)
    with pytest.raises(notices.NoticeError, match="github.com/moby/moby/v2 docker-v29.9.2 carries a copyleft license"):
        notices.gather_appliance([docker_request()], FakeFetcher(files))


def test_nested_license_files_are_named_by_the_package_they_belong_to():
    files = docker_upstream()
    files[f"{notices.CODELOAD}/docker/cli/tar.gz/refs/tags/v29.9.2"] = source_tarball("cli-29.9.2", {
        "LICENSE": APACHE, "NOTICE": "Docker CLI notice\n",
        "cli/connhelper/internal/syntax/LICENSE": "BSD syntax\n",
        "e2e/testdata/LICENSE": "test only\n",
        "vendor/modules.txt": "# github.com/c/three v3.0.0\ngithub.com/c/three\ngithub.com/c/three/inner\n# github.com/c/three/inner v1.0.0\ngithub.com/c/three/inner\n",
        "vendor/github.com/c/three/LICENSE": BSD,
        "vendor/github.com/c/three/dotenv/LICENSE": "dotenv MIT\n",
        "vendor/github.com/c/three/inner/LICENSE": "inner MIT\n",
    }, CLI)
    files[f"{notices.PROXY}/github.com/a/one/@v/v1.0.0.zip"] = module_zip("github.com/a/one", "v1.0.0", {"LICENSE": MIT, "third_party/uritemplates/LICENSE": "uritemplates\n"})
    text = notices.render_appliance(notices.gather_appliance([docker_request()], FakeFetcher(files)))
    for package, body in [
        ("github.com/docker/cli/cli/connhelper/internal/syntax v29.9.2", "BSD syntax"),
        ("github.com/c/three/dotenv v3.0.0", "dotenv MIT"),
        ("github.com/c/three/inner v1.0.0", "inner MIT"),
        ("github.com/a/one/third_party/uritemplates v1.0.0", "uritemplates"),
    ]:
        assert f"LICENSE of:\n  {package}\n" + "-" * 78 + f"\n{body}" in text, package
    assert "test only" not in text and text.count("inner MIT") == 1 and "github.com/c/three/inner v3.0.0" not in text


def dynamic_elf(libraries):
    """A 64-bit ELF with a loadable segment holding a dynamic section and its string table."""

    strings = b"\0" + b"".join(name.encode() + b"\0" for name in libraries)
    entries = [(notices.DT_NEEDED, 1 + sum(len(name) + 1 for name in libraries[:index])) for index in range(len(libraries))]
    table_at, dynamic_at = 0x200, 0x100
    entries += [(notices.DT_STRTAB, 0x400000 + table_at), (notices.DT_NULL, 0)]
    header = b"\x7fELF\x02\x01" + b"\0" * 26 + (64).to_bytes(8, "little") + b"\0" * 14 + (56).to_bytes(2, "little") + (3).to_bytes(2, "little")
    data = bytearray(header.ljust(64, b"\0"))
    for kind, offset, address, size in [(notices.PT_INTERP, 0x80, 0x400080, 16), (notices.PT_LOAD, 0, 0x400000, 0x300), (notices.PT_DYNAMIC, dynamic_at, 0x400000 + dynamic_at, 16 * len(entries))]:
        data += struct.pack("<IIQQQQQQ", kind, 0, offset, address, address, size, size, 0)
    data = data.ljust(dynamic_at, b"\0") + b"".join(struct.pack("<qQ", tag, value) for tag, value in entries)
    return bytes(data.ljust(table_at, b"\0") + strings).ljust(0x300, b"\0")


def test_the_libraries_a_program_loads_are_read_from_its_dynamic_section():
    assert notices.needed_libraries(dynamic_elf(["libseccomp.so.2", "libc.so.6"])) == ["libseccomp.so.2", "libc.so.6"]
    assert notices.needed_libraries(elf(False)) == [] and notices.needed_libraries(b"\x7fELF") == []


def runc(needed):
    return program("/usr/bin/runc", "containerd.io", kind="go", go="go1.27.3", package_path="github.com/opencontainers/runc",
                   main=["github.com/a/one", "v1.0.0"], deps=[["github.com/seccomp/libseccomp-golang", "v0.11.0"]],
                   settings={"CGO_ENABLED": "1", "-trimpath": "true"}, interpreter=True, needed=needed)


def test_the_libseccomp_binding_must_load_libseccomp_or_it_was_linked_in_statically():
    files = docker_upstream()
    files[f"{notices.PROXY}/github.com/seccomp/libseccomp-golang/@v/v0.11.0.zip"] = module_zip("github.com/seccomp/libseccomp-golang", "v0.11.0", {"LICENSE": BSD})
    with pytest.raises(notices.NoticeError, match="compiles github.com/seccomp/libseccomp-golang in but does not load libseccomp.so"):
        notices.gather_appliance([docker_request(programs=[runc(["libc.so.6"])])], FakeFetcher(files))
    text = notices.render_appliance(notices.gather_appliance([docker_request(programs=[runc(["libseccomp.so.2", "libc.so.6"])])], FakeFetcher(files)))
    assert "  github.com/seccomp/libseccomp-golang v0.11.0" in text


def test_license_files_are_known_by_every_name_upstreams_give_them():
    for name in ("GO_LICENSE", "GO-LICENSE", "LICENSE_list", "COPYRIGHT", "MIT-LICENSE", "UNLICENSE", "LEGAL", "ThirdPartyNotices.txt"):
        assert notices.is_license_file(name), name
    for name in ("license.json", "NOTICE.yaml", "copyright_test.go", "legal.png"):
        assert not notices.is_license_file(name), name


def test_a_module_whose_only_license_is_below_its_root_is_refused():
    files = docker_upstream()
    files[f"{notices.PROXY}/github.com/b/two/@v/v0.1.0.zip"] = module_zip("github.com/b/two", "v0.1.0", {"internal/third_party/x/LICENSE": MIT})
    with pytest.raises(notices.NoticeError, match="no license file at the root of github.com/b/two v0.1.0"):
        notices.gather_appliance([docker_request()], FakeFetcher(files))
    upstream = go_mod_upstream()
    upstream[f"{notices.PROXY}/github.com/b/two/@v/v0.1.0.zip"] = module_zip("github.com/b/two", "v0.1.0", {"sub/LICENSE": MIT})
    with pytest.raises(notices.NoticeError, match="no license file in github.com/b/two v0.1.0"):
        notices.gather("influx-cli", FakeFetcher(upstream), REVISION)


def test_a_module_the_cli_vendors_and_another_program_compiles_takes_the_texts_of_both():
    programs = docker_programs()
    programs[1]["deps"].append(["github.com/c/three", "v3.0.0"])
    files = docker_upstream()
    files[f"{notices.PROXY}/github.com/c/three/@v/v3.0.0.zip"] = module_zip("github.com/c/three", "v3.0.0", {"LICENSE": BSD, "s2/LICENSE": "s2 only in the proxy zip\n"})
    text = notices.render_appliance(notices.gather_appliance([docker_request(programs=programs)], FakeFetcher(files)))
    assert "LICENSE of:\n  github.com/c/three/s2 v3.0.0\n" in text and BSD.strip() in text


# REUSE-IgnoreStart
@pytest.mark.parametrize("text", [
    "This Source Code Form is subject to the terms of the Mozilla Public\n  License, v. 2.0.",
    "SPDX-License-Identifier: MPL-2.0",
    "GNU LESSER\nGENERAL PUBLIC LICENSE",
    "SPDX-License-Identifier: GPL-2.0-or-later",
    "Common Public License Version 1.0",
    "Licensed under the Microsoft Reciprocal License (MS-RL)",
])
# REUSE-IgnoreEnd
def test_a_copyleft_license_is_known_however_it_is_written(text):
    assert notices.copyleft({"LICENSE": text})


def test_permissive_texts_are_not_copyleft():
    assert not notices.copyleft({"LICENSE": APACHE + MIT + BSD})


def test_a_tree_s_texts_belong_to_its_module_path_and_stop_at_its_nested_modules():
    files = docker_upstream()
    files[f"{notices.CODELOAD}/moby/moby/tar.gz/refs/tags/docker-v29.9.2"] = source_tarball("moby-docker-v29.9.2", {
        "LICENSE": APACHE, "NOTICE": "Docker notice\n",
        "daemon/logger/srslog/LICENSE": "srslog BSD\n",
        "api/go.mod": "module github.com/moby/moby/api\n", "api/LICENSE": "api of another version\n",
    }, MOBY)
    text = notices.render_appliance(notices.gather_appliance([docker_request()], FakeFetcher(files)))
    assert "LICENSE of:\n  github.com/moby/moby/v2/daemon/logger/srslog docker-v29.9.2\n" in text
    assert "api of another version" not in text and "github.com/moby/moby docker-v29.9.2" not in text


def test_a_vendor_tree_that_replaces_a_module_with_a_local_directory_is_refused():
    with pytest.raises(notices.NoticeError, match="replaces github.com/c/three with the local directory ./three"):
        notices.parse_vendor_modules("# github.com/c/three v3.0.0 => ./three\ngithub.com/c/three\n")
