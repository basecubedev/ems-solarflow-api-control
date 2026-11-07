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

import importlib.util
import io
import json
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


def module_zip(path, version, files):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, text in files.items():
            archive.writestr(f"{path}@{version}/{name}", text)
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
            "github.com/b/two", "v0.1.0", {"README": "no license"} if missing_license else {"LICENSE.txt": MIT, "NOTICE": "Two\n", "sub/LICENSE": "ignored"}
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
    assert "The Go Authors" in text and "ignored" not in text
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
