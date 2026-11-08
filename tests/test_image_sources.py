# SPDX-FileCopyrightText: 2026 basecubedev
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The source of the third-party components in the EMS and Admin images, read off the images as pushed.

The registry is a fake serving manifests and layer tars from memory, so what is
tested is the reading (platforms, layers laid over each other with their
whiteouts, the files that matter) and the decision (which sources, verified
how), and that an image of another revision, a copyleft package without a
source or a source image nobody can pull stops the run.
"""

import gzip
import hashlib
import importlib.util
import io
import json
import tarfile
import urllib.error
import zipfile
from pathlib import Path

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.documentation]

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("image_sources", ROOT / "scripts" / "image_sources.py")
images = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(images)
notices = images.notices

REVISION = "0123456789abcdef0123456789abcdef01234567"
STATUS = (
    "Package: bash\nStatus: install ok installed\nVersion: 5.2.37-2+b5\nSource: bash (5.2.37-2)\nArchitecture: arm64\n"
    "Maintainer: Debian <debian@lists.debian.org>\n"
)
APT = "Types: deb\nURIs: http://deb.debian.org/debian\nSuites: trixie\nComponents: main\n"
SITE = "usr/local/lib/python3.14/site-packages"
MPL = "Mozilla Public License Version 2.0\n"
MIT = "MIT License\n\nCopyright (c) 2021 Example\n"
CPYTHON_TARBALL = b"Python-3.14.10 source"
CPYTHON_SHA256 = hashlib.sha256(CPYTHON_TARBALL).hexdigest()
CPYTHON_ENV = ["PATH=/usr/local/bin", "PYTHON_VERSION=3.14.10", f"PYTHON_SHA256={CPYTHON_SHA256}"]


def digest(data):
    return "sha256:" + hashlib.sha256(data).hexdigest()


def layer(files, links=()):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            data = data.encode() if isinstance(data, str) else data
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
        for name, target in links:
            info = tarfile.TarInfo(name)
            info.type, info.linkname = tarfile.SYMTYPE, target
            archive.addfile(info)
    return buffer.getvalue()


class FakeRegistry:
    """One repository: ``{reference: manifest}`` and ``{digest: blob}`` in memory."""

    served = {}

    def __init__(self, registry, repository):
        self.registry, self.repository = registry, repository
        self.manifests, self.blobs = self.served[repository]

    def authenticate(self):
        return self

    def manifest(self, reference):
        if reference not in self.manifests:
            raise images.ImageError(f"{reference}: 404")
        return self.manifests[reference]

    def manifest_bytes(self, reference):
        return json.dumps(self.manifest(reference)).encode()

    def blob(self, digest_):
        return self.blobs[digest_]


def publish(repository, platforms, revision=REVISION, media_type="application/vnd.oci.image.layer.v1.tar+gzip", env=CPYTHON_ENV):
    """Serve an index of one image per platform, each from its list of layer dicts."""

    manifests, blobs, entries = {}, {}, []
    for (os_, architecture), layers in platforms.items():
        config = json.dumps({"os": os_, "architecture": architecture,
                             "config": {"Labels": {images.REVISION_LABEL: revision}, "Env": env}}).encode()
        blobs[digest(config)] = config
        tars = [layer(*item) if isinstance(item, tuple) else layer(item) for item in layers]
        blobs.update({digest(tar): tar for tar in tars})
        manifest = {"mediaType": "application/vnd.oci.image.manifest.v1+json", "config": {"digest": digest(config)},
                    "layers": [{"mediaType": media_type, "digest": digest(tar)} for tar in tars]}
        manifest_digest = digest(json.dumps(manifest).encode())
        manifests[manifest_digest] = manifest
        entries.append({"digest": manifest_digest, "platform": {"os": os_, "architecture": architecture}})
    entries.append({"digest": "sha256:" + "a" * 64, "platform": {"os": "unknown", "architecture": "unknown"},
                    "annotations": {"vnd.docker.reference.type": "attestation-manifest"}})
    manifests["v1.0.0"] = {"mediaType": "application/vnd.oci.image.index.v1+json", "manifests": entries}
    FakeRegistry.served[repository] = (manifests, blobs)


def base_layer(**extra):
    return {
        images.DPKG_STATUS: STATUS,
        "etc/apt/sources.list.d/debian.sources": APT,
        f"{SITE}/zeroconf-0.151.5.dist-info/METADATA": "Metadata-Version: 2.4\nName: zeroconf\nVersion: 0.151.5\nLicense-Expression: LGPL-2.1-or-later\n\nbody",
        f"{SITE}/requests-2.34.2.dist-info/METADATA": "Metadata-Version: 2.1\nName: requests\nVersion: 2.34.2\nLicense: Apache-2.0\n",
        f"{SITE}/certifi-2026.10.23.dist-info/METADATA": "Metadata-Version: 2.1\nName: certifi\nVersion: 2026.10.23\n",
        f"{SITE}/certifi-2026.10.23.dist-info/licenses/LICENSE": MPL,
        **extra,
    }


# --- reading an image -------------------------------------------------------


def test_a_reference_names_its_registry_repository_and_tag_or_digest():
    assert images.parse_reference("ghcr.io/basecubedev/ems-solarflow-admin:v0.9.0") == ("ghcr.io", "basecubedev/ems-solarflow-admin", "v0.9.0")
    pinned = "ghcr.io/basecubedev/ems-solarflow-admin@sha256:" + "b" * 64
    assert images.parse_reference(pinned)[2] == "sha256:" + "b" * 64
    with pytest.raises(images.ImageError, match="not a registry/repository"):
        images.parse_reference("ems-solarflow-admin")


def test_each_platform_of_an_image_is_read_with_its_layers_laid_over_each_other():
    publish("o/ems", {
        ("linux", "amd64"): [base_layer(), {f"{SITE}/.wh.requests-2.34.2.dist-info": ""}],
        ("linux", "arm64"): [base_layer(), {f"{SITE}/.wh..wh..opq": "", f"{SITE}/ifaddr-0.2.0.dist-info/METADATA": "Name: ifaddr\nVersion: 0.2.0\nLicense: MIT\n"}],
    })
    described = images.describe("ghcr.io/o/ems:v1.0.0", fetcher_registry=FakeRegistry)
    assert [item["platform"] for item in described] == ["linux/amd64", "linux/arm64"]
    amd64, arm64 = described
    assert [(item["name"], item["copyleft"]) for item in amd64["python"]] == [("certifi", True), ("zeroconf", True)]
    assert [item["name"] for item in arm64["python"]] == ["ifaddr"]
    assert amd64["packages"][0]["source"] == "bash" and amd64["packages"][0]["source_version"] == "5.2.37-2"
    assert amd64["archives"] == [{"uri": "http://deb.debian.org/debian", "suite": "trixie", "component": "main"}]
    assert amd64["revision"] == REVISION and amd64["image"] == "ghcr.io/o/ems"


def test_a_file_replaced_by_a_link_in_a_later_layer_is_no_longer_read():
    files = images.apply_layer({}, layer(base_layer()))
    images.apply_layer(files, layer({}, links=[(images.DPKG_STATUS, "/elsewhere")]))
    assert images.DPKG_STATUS not in files and "etc/apt/sources.list.d/debian.sources" in files


def test_the_notices_of_the_go_programs_are_read_from_the_image():
    text = (ROOT / "third_party" / "docker-compose" / notices.NOTICES).read_text(encoding="utf-8")
    publish("o/admin", {("linux", "amd64"): [base_layer(**{f"usr/share/doc/docker-compose/{notices.NOTICES}": text})]})
    described = images.describe("ghcr.io/o/admin:v1.0.0", fetcher_registry=FakeRegistry)
    assert described[0]["go"] == {"docker-compose": text}


def test_an_image_without_a_dpkg_database_or_with_a_layer_it_cannot_read_is_refused():
    publish("o/bare", {("linux", "amd64"): [{"etc/hostname": "x"}]})
    with pytest.raises(images.ImageError, match="has no dpkg database"):
        images.describe("ghcr.io/o/bare:v1.0.0", fetcher_registry=FakeRegistry)
    publish("o/zstd", {("linux", "amd64"): [base_layer()]}, media_type="application/vnd.oci.image.layer.v1.tar+zstd")
    with pytest.raises(images.ImageError, match="which this script cannot read"):
        images.describe("ghcr.io/o/zstd:v1.0.0", fetcher_registry=FakeRegistry)


def test_a_single_platform_image_must_be_named_by_its_digest():
    publish("o/single", {("linux", "amd64"): [base_layer()]})
    manifests, _ = FakeRegistry.served["o/single"]
    only = next(key for key in manifests if key.startswith("sha256:"))
    manifests["plain"] = manifests[only]
    registry = FakeRegistry("ghcr.io", "o/single")
    assert images.platforms(registry, only) == [("linux/amd64", only)]
    with pytest.raises(images.ImageError, match="must be named by its digest"):
        images.platforms(registry, "plain")


def test_a_blob_that_does_not_hash_to_its_digest_is_refused(monkeypatch):
    registry = images.Registry("ghcr.io", "o/ems", attempts=1, pause=0)
    monkeypatch.setattr(registry, "_open", lambda url, accept=None: b"tampered")
    with pytest.raises(images.ImageError, match="does not hash to its digest"):
        registry.blob("sha256:" + "c" * 64)


def test_only_ghcr_is_read():
    with pytest.raises(images.ImageError, match="only ghcr.io is read"):
        images.Registry("docker.io", "library/python")


# --- what is owed ---------------------------------------------------------------


def notices_listing(component, modules):
    return notices.render({"component": component, "version": "1", "identity": "1", "source": "s", "modules": sorted(modules), "licenses": {}})


def test_the_copyleft_modules_are_those_whose_own_license_files_are_copyleft():
    files = {**go_module("github.com/h/mpl", "v1.0.0", {"LICENSE": MPL}), **go_module("github.com/a/mit", "v1.0.0", {"LICENSE": MIT}),
             **go_module("github.com/a/mit/sub", "v1.0.0", {"LICENSE": MIT, "fork/LICENSE": MPL})}
    text = notices_listing("docker-compose", [("github.com/h/mpl", "v1.0.0"), ("github.com/a/mit", "v1.0.0"), ("github.com/a/mit/sub", "v1.0.0")])
    assert images.copyleft_modules("docker-compose", text, FakeFetcher(files)) == [("github.com/a/mit/sub", "v1.0.0"), ("github.com/h/mpl", "v1.0.0")]


def test_a_notices_file_that_lists_no_module_is_refused():
    with pytest.raises(images.ImageError, match="its notices list no module"):
        images.copyleft_modules("x", "nothing here\n", FakeFetcher({}))


# --- collecting -------------------------------------------------------------------


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
        data = self.require(url)
        if first < 0:
            return data[max(len(data) + first, 0):], len(data)
        return data[first:len(data) if last is None else last + 1], len(data)


def pypi(name, version, data, sdist=True, sha256=None):
    filename = f"{name}-{version}.tar.gz"
    url = f"https://files.example/{filename}"
    entries = [{"packagetype": "bdist_wheel", "filename": f"{name}-{version}-py3-none-any.whl", "url": url + ".whl", "digests": {"sha256": "0"}}]
    if sdist:
        entries.append({"packagetype": "sdist", "filename": filename, "url": url, "digests": {"sha256": sha256 or hashlib.sha256(data).hexdigest()}})
    return {f"{images.PYPI}/{name}/{version}/json": json.dumps({"urls": entries}).encode(), url: data}


def go_module(path, version, texts):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, text in texts.items():
            archive.writestr(f"{path}@{version}/{name}", text)
    data = buffer.getvalue()
    return {
        f"{notices.PROXY}/{path}/@v/{version}.zip": data,
        f"{notices.SUMDB}/lookup/{path}@{version}": f"1\n{path} {version} {notices.zip_hash(data)}\n".encode(),
    }


def described(revision=REVISION, python=None, go=None):
    return [{
        "image": "ghcr.io/o/ems", "reference": "sha256:" + "d" * 64, "platform": "linux/amd64", "digest": "sha256:" + "e" * 64,
        "revision": revision,
        "packages": [{"package": "bash", "version": "5.2.37-2+b5", "architecture": "arm64", "maintainer": "Debian", "source": "bash",
                      "source_version": "5.2.37-2", "built_using": []}],
        "archives": [{"uri": "http://deb.debian.org/debian", "suite": "trixie", "component": "main"}],
        "python": python if python is not None else [{"name": "zeroconf", "version": "0.151.5", "copyleft": True},
                                                     {"name": "requests", "version": "2.34.2", "copyleft": False}],
        "go": go or {},
        "interpreter": {"version": "3.14.10", "sha256": CPYTHON_SHA256},
    }]


@pytest.fixture
def debian_collect(monkeypatch):
    seen = {}

    def fake(requests, *, revision, repository_url, output, fetcher, part_limit, name):
        seen.update(requests=requests, name=name)
        output.mkdir(parents=True, exist_ok=True)
        prefix = f"{name}-{requests[0]['release_version']}"
        (output / f"{prefix}-sources-1-of-1.tar").write_bytes(b"debian")
        return {"schema_version": 3, "parts": [{"name": f"{prefix}-sources-1-of-1.tar", "size": 6}], "sources": [], "boards": []}

    monkeypatch.setattr(images.debian, "collect", fake)
    return seen


def upstream():
    compose = notices_listing("docker-compose", [("github.com/h/mpl", "v1.0.0"), ("github.com/a/mit", "v1.0.0")])
    files = {**pypi("zeroconf", "0.151.5", b"zeroconf sdist"), **pypi("requests", "2.34.2", b"requests sdist"),
             **go_module("github.com/h/mpl", "v1.0.0", {"LICENSE": MPL, "a.go": "package a"}),
             **go_module("github.com/a/mit", "v1.0.0", {"LICENSE": MIT}),
             images.CPYTHON.format(folder="3.14.10", version="3.14.10"): CPYTHON_TARBALL}
    return files, {"docker-compose": compose}


def run(tmp_path, images_, files):
    output = tmp_path / "out"
    return images.collect(images_, name="v1.0.0-abc", revision=REVISION, repository_url="https://github.com/o/r", output=output,
                          fetcher=FakeFetcher(files), debian_fetcher=None), output


def test_every_source_is_fetched_verified_and_written_with_a_manifest_naming_each_image(tmp_path, debian_collect):
    files, go = upstream()
    manifest, output = run(tmp_path, described(go=go), files)
    assert debian_collect["name"] == images.NAME
    assert debian_collect["requests"][0]["board"] == "ghcr.io/o/ems linux/amd64"
    assert debian_collect["requests"][0]["release_version"] == "v1.0.0-abc"
    assert (output / "python" / "zeroconf-0.151.5.tar.gz").read_bytes() == b"zeroconf sdist"
    assert (output / "python" / "requests-2.34.2.tar.gz").read_bytes() == b"requests sdist"
    assert (output / "go" / "github.com" / "h" / "mpl@v1.0.0.zip").exists()
    assert not (output / "go" / "github.com" / "a").exists()
    written = json.loads((output / "ems-solarflow-images-v1.0.0-abc-sources.json").read_text(encoding="utf-8"))
    assert written == manifest
    assert manifest["images"][0]["digest"] == "sha256:" + "e" * 64
    assert [item["name"] for item in manifest["python"]] == ["requests", "zeroconf"]
    assert manifest["go_modules"][0]["module"] == "github.com/h/mpl"
    assert manifest["project"] == [{"source": "this repository", "where": f"https://github.com/o/r/tree/{REVISION}"}]


def test_an_image_of_another_revision_is_refused(tmp_path, debian_collect):
    files, _ = upstream()
    with pytest.raises(images.ImageError, match="not all of"):
        run(tmp_path, described(revision="f" * 40), files)


def test_a_distribution_without_an_sdist_is_refused_unless_the_script_says_why_none_is_owed(tmp_path, debian_collect, monkeypatch):
    files, _ = upstream()
    files.update(pypi("requests", "2.34.2", b"", sdist=False))
    with pytest.raises(images.ImageError, match="requests 2.34.2: PyPI publishes no sdist of it"):
        run(tmp_path, described(), files)
    monkeypatch.setattr(images, "WITHOUT_SDIST", {"requests": "Apache-2.0 asks for no source"})
    manifest, _ = run(tmp_path / "allowed", described(), files)
    record = next(item for item in manifest["python"] if item["name"] == "requests")
    assert (record["file"], record["without_sdist"]) == (None, "Apache-2.0 asks for no source")
    files.update(pypi("zeroconf", "0.151.5", b"", sdist=False))
    monkeypatch.setattr(images, "WITHOUT_SDIST", {"requests": "x", "zeroconf": "y"})
    with pytest.raises(images.ImageError, match="zeroconf 0.151.5: PyPI publishes no sdist of it"):
        run(tmp_path / "again", described(), files)


def test_an_sdist_that_does_not_match_pypi_s_hash_is_refused(tmp_path, debian_collect):
    files, _ = upstream()
    files.update(pypi("zeroconf", "0.151.5", b"zeroconf sdist", sha256="0" * 64))
    with pytest.raises(images.ImageError, match="SHA-256 does not match what PyPI publishes"):
        run(tmp_path, described(), files)


def test_a_go_module_sum_golang_org_does_not_vouch_for_is_refused(tmp_path, debian_collect):
    files, go = upstream()
    files[f"{notices.SUMDB}/lookup/github.com/h/mpl@v1.0.0"] = b"1\ngithub.com/h/mpl v1.0.0 h1:AAAA\n"
    with pytest.raises(notices.NoticeError, match="not the one sum.golang.org records"):
        run(tmp_path, described(go=go), files)


def test_a_source_image_nobody_can_pull_fails_with_what_to_do(monkeypatch):
    class Private(FakeRegistry):
        def __init__(self, registry, repository):
            self.registry, self.repository = registry, repository

        def authenticate(self):
            raise images.ImageError("https://ghcr.io/token: 401 Unauthorized")

    with pytest.raises(images.ImageError, match="cannot be pulled without credentials .* make the package public"):
        images.verify("ghcr.io/o/ems-solarflow-sources:v1.0.0", fetcher_registry=Private)
    publish("o/ems-solarflow-sources", {("linux", "amd64"): [base_layer()]})
    images.verify("ghcr.io/o/ems-solarflow-sources:v1.0.0", fetcher_registry=FakeRegistry)


def test_the_command_line_refuses_an_output_directory_that_is_not_empty(tmp_path, capsys):
    (tmp_path / "left-over").write_text("x")
    status = images.main(["collect", "--image", "ghcr.io/o/ems:v1", "--name", "n", "--revision", REVISION,
                          "--repository-url", "https://github.com/o/r", "--output", str(tmp_path)])
    assert status == 1 and "is not empty" in capsys.readouterr().err


def test_a_layer_is_read_whether_it_is_compressed_or_not():
    plain = io.BytesIO()
    with tarfile.open(fileobj=plain, mode="w") as archive:
        info = tarfile.TarInfo(images.DPKG_STATUS)
        info.size = len(STATUS)
        archive.addfile(info, io.BytesIO(STATUS.encode()))
    assert images.DPKG_STATUS in images.apply_layer({}, plain.getvalue())
    assert images.DPKG_STATUS in images.apply_layer({}, gzip.compress(plain.getvalue()))


# --- what a review found ------------------------------------------------------------


def test_an_index_entry_that_names_no_platform_and_is_no_attestation_is_refused():
    publish("o/odd", {("linux", "amd64"): [base_layer()]})
    manifests, _ = FakeRegistry.served["o/odd"]
    manifests["v1.0.0"]["manifests"].append({"digest": "sha256:" + "9" * 64, "platform": {}})
    with pytest.raises(images.ImageError, match="names no platform"):
        images.describe("ghcr.io/o/odd:v1.0.0", fetcher_registry=FakeRegistry)


def test_a_whiteout_at_the_root_removes_what_lies_below_it():
    files = images.apply_layer({}, layer(base_layer()))
    images.apply_layer(files, layer({"./.wh.etc": ""}))
    assert not any(name.startswith("etc/") for name in files) and images.DPKG_STATUS in files


def test_a_license_field_continued_on_the_next_line_is_read_whole():
    metadata = "Name: odd\nVersion: 1.0\nLicense: Copyright 2020 Foo\n        Licensed under the GNU Lesser General Public License\n\nbody"
    files = {f"{SITE}/odd-1.0.dist-info/METADATA": metadata.encode()}
    assert images.python_distributions(files) == [{"name": "odd", "version": "1.0", "copyleft": True}]


@pytest.mark.parametrize("path", [
    "opt/venv/lib/python3.14/site-packages/zeroconf-0.151.5.dist-info/METADATA",
    "root/.local/lib/python3.14/site-packages/x-1.dist-info/METADATA",
    f"{SITE}/legacy-1.0.egg-info/PKG-INFO",
])
def test_a_python_distribution_in_a_place_this_script_does_not_read_stops_it(path):
    assert images.wanted(path)
    with pytest.raises(images.ImageError, match="outside /usr/local's site-packages"):
        images.python_distributions({path: b"Name: x\nVersion: 1\n"})


def test_debian_s_own_python_packages_are_left_to_the_dpkg_database():
    path = "usr/lib/python3/dist-packages/requests-2.32.3.dist-info/METADATA"
    assert not images.wanted(path) and images.python_distributions({path: b"Name: requests\nVersion: 2.32.3+dfsg\n"}) == []


def test_a_wheel_lying_in_the_image_is_a_distribution_read_from_its_own_metadata():
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("pip-26.2.2.dist-info/METADATA", "Name: pip\nVersion: 26.2.2\nLicense-Expression: MIT\n")
        archive.writestr("pip-26.2.2.dist-info/licenses/src/pip/_vendor/certifi/LICENSE", MPL)
    path = "usr/local/lib/python3.14/ensurepip/_bundled/pip-26.2.2-py3-none-any.whl"
    assert images.wanted(path)
    assert images.python_distributions({path: buffer.getvalue()}) == [{"name": "pip", "version": "26.2.2", "copyleft": True}]


def test_a_manifest_named_by_digest_must_hash_to_it(monkeypatch):
    registry = images.Registry("ghcr.io", "o/ems", attempts=1, pause=0)
    monkeypatch.setattr(registry, "_open", lambda url, accept=None, missing_is_final=True: b'{"layers": []}')
    with pytest.raises(images.ImageError, match="does not hash to its digest"):
        registry.manifest("sha256:" + "c" * 64)
    assert registry.manifest("v1.0.0") == {"layers": []}


def test_a_manifest_not_yet_served_after_a_push_is_asked_for_again_and_a_missing_blob_is_not(monkeypatch):
    answers = []

    class Response(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def urlopen(request, timeout):
        answers.append(request.full_url)
        if len(answers) == 1:
            raise urllib.error.HTTPError(request.full_url, 404, "manifest unknown", {}, None)
        if "/blobs/" in request.full_url:
            raise urllib.error.HTTPError(request.full_url, 404, "blob unknown", {}, None)
        return Response(b'{"manifests": []}')

    monkeypatch.setattr(images.urllib.request, "urlopen", urlopen)
    registry = images.Registry("ghcr.io", "o/ems", attempts=3, pause=0)
    assert registry.manifest("latest") == {"manifests": []} and len(answers) == 2
    with pytest.raises(images.ImageError, match="404"):
        registry.blob("sha256:" + "c" * 64)
    assert len(answers) == 3


def test_the_manifest_names_where_each_python_and_go_file_came_from(tmp_path, debian_collect):
    files, go = upstream()
    manifest, _ = run(tmp_path, described(go=go), files)
    assert next(item for item in manifest["python"] if item["name"] == "zeroconf")["url"] == "https://files.example/zeroconf-0.151.5.tar.gz"
    assert manifest["go_modules"][0]["url"] == f"{notices.PROXY}/github.com/h/mpl/@v/v1.0.0.zip"


def test_a_tag_must_point_at_the_digest_the_run_pushed():
    publish("o/tagged", {("linux", "amd64"): [base_layer()]})
    served = "sha256:" + hashlib.sha256(json.dumps(FakeRegistry.served["o/tagged"][0]["v1.0.0"]).encode()).hexdigest()
    images.verify("ghcr.io/o/tagged:v1.0.0", served, fetcher_registry=FakeRegistry)
    with pytest.raises(images.ImageError, match="is sha256:.*, not sha256:0000"):
        images.verify("ghcr.io/o/tagged:v1.0.0", "sha256:" + "0" * 64, fetcher_registry=FakeRegistry)


def test_a_tag_read_stale_right_after_it_moved_is_read_again():
    publish("o/moving", {("linux", "amd64"): [base_layer()]})
    current = FakeRegistry.served["o/moving"][0]["v1.0.0"]
    stale = {"mediaType": "application/vnd.oci.image.index.v1+json", "manifests": []}
    reads = [stale, current]

    class Moving(FakeRegistry):
        attempts, pause = 3, 0

        def manifest_bytes(self, reference):
            return json.dumps(reads.pop(0) if reads else current).encode()

    digest = "sha256:" + hashlib.sha256(json.dumps(current).encode()).hexdigest()
    images.verify("ghcr.io/o/moving:v1.0.0", digest, fetcher_registry=Moving)
    assert reads == []


# --- the interpreter --------------------------------------------------------------


def test_the_cpython_release_the_interpreter_was_built_from_is_collected(tmp_path, debian_collect):
    files, _ = upstream()
    manifest, output = run(tmp_path, described(), files)
    assert (output / "python" / "Python-3.14.10.tar.xz").read_bytes() == CPYTHON_TARBALL
    assert manifest["interpreter"] == [{"version": "3.14.10", "sha256": CPYTHON_SHA256, "file": "python/Python-3.14.10.tar.xz",
                                        "url": "https://www.python.org/ftp/python/3.14.10/Python-3.14.10.tar.xz"}]


def test_a_cpython_tarball_that_is_not_the_one_the_image_was_built_from_is_refused(tmp_path, debian_collect):
    files, _ = upstream()
    files[images.CPYTHON.format(folder="3.14.10", version="3.14.10")] = b"another tarball"
    with pytest.raises(images.ImageError, match="does not match the .* the image was built from"):
        run(tmp_path, described(), files)
    other = described() + [dict(described()[0], platform="linux/arm64", interpreter={"version": "3.14.10", "sha256": "1" * 64})]
    with pytest.raises(images.ImageError, match="CPython 3.14.10 is named with two SHA-256 values"):
        run(tmp_path / "two", other, upstream()[0])


def test_a_prerelease_is_fetched_from_its_release_folder():
    url = images.CPYTHON.format(folder="3.15.0", version="3.15.0rc1")
    fetcher = FakeFetcher({url: b"rc"})
    assert images.cpython("3.15.0rc1", hashlib.sha256(b"rc").hexdigest(), fetcher)[2] == url


@pytest.mark.parametrize("env", [[], ["PYTHON_VERSION=3.14.10"], ["PYTHON_VERSION=latest", f"PYTHON_SHA256={CPYTHON_SHA256}"]])
def test_an_image_that_names_no_cpython_build_is_refused(env):
    publish("o/nopython", {("linux", "amd64"): [base_layer()]}, env=env)
    with pytest.raises(images.ImageError, match="names no CPython build"):
        images.describe("ghcr.io/o/nopython:v1.0.0", fetcher_registry=FakeRegistry)


# --- every program accounted for ----------------------------------------------------


def varint(value):
    out = bytearray()
    while True:
        out.append((value & 0x7F) | (0x80 if value >> 7 else 0))
        value >>= 7
        if not value:
            return bytes(out)


GO_REVISION = "8cdf40161d7662e942582086853fca948fc1a842"


def go_program(revision=GO_REVISION, deps=(("github.com/a/one", "v1.0.0"),)):
    lines = ["path\tgithub.com/influxdata/influx-cli/v2/cmd/influx", "mod\tgithub.com/influxdata/influx-cli/v2\tv2.8.0\t",
             *(f"dep\t{path}\t{version}\th1:x" for path, version in deps), f"build\tvcs.revision={revision}"]
    modinfo = ("x" * 16 + "\n".join(lines) + "\n" + "y" * 16).encode()
    go = b"go1.25.9"
    return images.ELF + b"\x02\x01" + b"\0" * 58 + notices.MAGIC + bytes([8, 2]) + b"\0" * 16 + varint(len(go)) + go + varint(len(modinfo)) + modinfo


INFLUX_NOTICES = notices.render({"component": "influx-cli", "version": GO_REVISION, "identity": GO_REVISION, "source": "s",
                                 "modules": [("github.com/a/one", "v1.0.0")], "licenses": {}})


def described_image(repository, **extra):
    publish(repository, {("linux", "amd64"): [base_layer(**extra)]})
    return images.describe(f"ghcr.io/{repository}:v1.0.0", fetcher_registry=FakeRegistry)


def md5(data):
    return hashlib.md5(data).hexdigest()


DPKG_FILES = {
    "usr/bin/bash": images.ELF + b"bash", "usr/lib/libc.so.6": images.ELF + b"libc",
    "var/lib/dpkg/info/bash.list": "/.\n/usr\n/usr/bin/bash\n", "var/lib/dpkg/info/libc6.list": "/lib/libc.so.6\n",
    "var/lib/dpkg/info/bash.md5sums": f"{md5(images.ELF + b'bash')}  usr/bin/bash\n",
    "var/lib/dpkg/info/libc6.md5sums": f"{md5(images.ELF + b'libc')}  lib/libc.so.6\n",
}


def test_a_program_dpkg_owns_with_its_checksum_needs_nothing_else_also_under_a_merged_usr():
    described_image("o/owned", **DPKG_FILES)


def test_a_program_a_later_layer_put_over_a_dpkg_file_is_not_dpkg_s():
    publish("o/overwritten", {("linux", "amd64"): [base_layer(**DPKG_FILES), {"usr/bin/bash": images.ELF + b"something else"}]})
    with pytest.raises(images.ImageError, match="usr/bin/bash is a program that neither dpkg"):
        images.describe("ghcr.io/o/overwritten:v1.0.0", fetcher_registry=FakeRegistry)


def test_a_go_program_its_notices_match_is_accounted_for():
    described_image("o/influx", **{"usr/local/bin/influx": go_program(), f"usr/share/doc/influx-cli/{notices.NOTICES}": INFLUX_NOTICES})


@pytest.mark.parametrize("extra, message", [
    ({"usr/local/bin/tool": images.ELF + b"c program"}, "usr/local/bin/tool is a program that neither dpkg"),
    ({"usr/local/bin/influx": go_program()}, "usr/local/bin/influx is a program that neither dpkg"),
    ({"usr/local/bin/influx": go_program(revision="f" * 40), f"usr/share/doc/influx-cli/{notices.NOTICES}": INFLUX_NOTICES}, "usr/local/bin/influx is a program"),
    ({"usr/local/bin/influx": go_program(deps=(("github.com/a/one", "v1.0.0"), ("github.com/b/new", "v2.0.0"))),
      f"usr/share/doc/influx-cli/{notices.NOTICES}": INFLUX_NOTICES}, "usr/local/bin/influx is a program"),
    ({f"{SITE}/numpy.libs/libquadmath-1.so": images.ELF + b"lgpl"}, "numpy.libs/libquadmath-1.so is a program or library a wheel carries"),
    ({f"{SITE}/numpy/.libs/libopenblas.so": images.ELF + b"PyInit_x"}, "numpy/.libs/libopenblas.so is a program or library a wheel carries"),
    ({f"{SITE}/imageio_ffmpeg/binaries/ffmpeg-linux-x86_64-v9.0.2": images.ELF + b"gpl ffmpeg"}, "ffmpeg-linux-x86_64-v9.0.2 is a program or library a wheel carries"),
    ({f"{SITE}/somepkg/lib/libfoo.so": images.ELF + b"plain library"}, "somepkg/lib/libfoo.so is a program or library a wheel carries"),
])
def test_a_program_or_bundled_library_nothing_accounts_for_stops_the_run(extra, message):
    with pytest.raises(images.ImageError, match=message):
        described_image("o/foreign", **extra)


def test_cpython_s_own_programs_need_no_notices():
    described_image("o/cpython", **{"usr/local/bin/python3.14": images.ELF + b"python", "usr/local/lib/python3.14/lib-dynload/readline.so": images.ELF + b"so",
                                    f"{SITE}/cryptography/_rust.abi3.so": images.ELF + b"rust PyInit__rust"})


def test_a_hardlinked_file_is_read_as_the_file_it_links():
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        info = tarfile.TarInfo("var/lib/dpkg/status.real")
        info.size = len(STATUS)
        archive.addfile(info, io.BytesIO(STATUS.encode()))
        link = tarfile.TarInfo(images.DPKG_STATUS)
        link.type, link.linkname = tarfile.LNKTYPE, "var/lib/dpkg/status.real"
        archive.addfile(link)
    assert images.apply_layer({}, buffer.getvalue())[images.DPKG_STATUS] == STATUS.encode()


def test_the_registry_token_is_never_sent_on_past_a_redirect(monkeypatch):
    seen = []

    class Response(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def urlopen(request, timeout):
        seen.append(request)
        return Response(b"{}")

    monkeypatch.setattr(images.urllib.request, "urlopen", urlopen)
    registry = images.Registry("ghcr.io", "o/ems", attempts=1, pause=0)
    registry.token = "secret-token"
    registry.manifest("latest")
    assert seen[0].unredirected_hdrs.get("Authorization") == "Bearer secret-token"
    assert "Authorization" not in seen[0].headers


def test_an_index_entry_for_an_unknown_platform_that_is_no_attestation_is_refused():
    publish("o/unknown", {("linux", "amd64"): [base_layer()]})
    manifests, _ = FakeRegistry.served["o/unknown"]
    manifests["v1.0.0"]["manifests"].append({"digest": "sha256:" + "8" * 64, "platform": {"os": "unknown", "architecture": "unknown"}})
    with pytest.raises(images.ImageError, match="names no platform"):
        images.describe("ghcr.io/o/unknown:v1.0.0", fetcher_registry=FakeRegistry)



def go_program_with(settings, marker=b""):
    lines = ["path\tgithub.com/influxdata/influx-cli/v2/cmd/influx", "mod\tgithub.com/influxdata/influx-cli/v2\tv2.8.0\t",
             "dep\tgithub.com/a/one\tv1.0.0\th1:x", *(f"build\t{key}={value}" for key, value in settings.items()), f"build\tvcs.revision={GO_REVISION}"]
    modinfo = ("x" * 16 + "\n".join(lines) + "\n" + "y" * 16).encode()
    go = b"go1.25.9"
    return images.ELF + b"\x02\x01" + b"\0" * 58 + notices.MAGIC + bytes([8, 2]) + b"\0" * 16 + varint(len(go)) + go + varint(len(modinfo)) + modinfo + marker


INFLUX = {f"usr/share/doc/influx-cli/{notices.NOTICES}": INFLUX_NOTICES}


def test_a_go_program_with_musl_linked_in_needs_musl_s_notice_beside_its_own():
    program = go_program_with({"CGO_ENABLED": "1"}, b"No error information")
    described_image("o/musl", **{"usr/local/bin/influx": program, "usr/share/doc/influx-cli/MUSL.COPYRIGHT": "musl", **INFLUX})
    with pytest.raises(images.ImageError, match="links musl in statically, and usr/share/doc/influx-cli/MUSL.COPYRIGHT is missing"):
        described_image("o/musl-bare", **{"usr/local/bin/influx": program, **INFLUX})


@pytest.mark.parametrize("marker", [b"Fatal glibc error", b"", b"No error information Fatal glibc error"])
def test_a_go_program_with_another_c_library_linked_in_stops_the_run(marker):
    program = go_program_with({"CGO_ENABLED": "1"}, marker)
    with pytest.raises(images.ImageError, match="links a C library in statically that the script has no rule for"):
        described_image("o/glibc", **{"usr/local/bin/influx": program, "usr/share/doc/influx-cli/MUSL.COPYRIGHT": "musl", **INFLUX})


def test_an_image_with_docker_s_debian_packages_is_refused(tmp_path, debian_collect):
    files, _ = upstream()
    image = described()
    image[0]["packages"].append({"package": "docker-compose-plugin", "version": "5.6.1-1", "architecture": "amd64", "maintainer": "Docker <support@docker.com>",
                                 "source": "docker-compose-plugin", "source_version": "5.6.1-1", "built_using": []})
    with pytest.raises(images.ImageError, match="install docker-compose-plugin, whose programs' module sources this script does not collect"):
        run(tmp_path, image, files)



def test_a_wheel_binary_the_script_names_is_accounted_for(monkeypatch):
    monkeypatch.setattr(images, "WHEEL_BINARIES", {"imageio_ffmpeg/binaries/": "named with the source of its ffmpeg build"})
    described_image("o/named", **{f"{SITE}/imageio_ffmpeg/binaries/ffmpeg-linux-x86_64-v9.0.2": images.ELF + b"ffmpeg"})


def test_a_file_another_package_took_over_is_dpkg_s_by_its_new_owner_s_checksum():
    new, old = images.ELF + b"libx new", images.ELF + b"libx old"
    described_image("o/replaced", **{
        "usr/lib/libx.so.1": new,
        "var/lib/dpkg/info/liba.list": "/usr/lib/libx.so.1\n", "var/lib/dpkg/info/liba.md5sums": f"{md5(new)}  usr/lib/libx.so.1\n",
        "var/lib/dpkg/info/libz.list": "/usr/share/doc/libz\n", "var/lib/dpkg/info/libz.md5sums": f"{md5(old)}  usr/lib/libx.so.1\n",
    })


def test_a_diverted_file_is_dpkg_s_under_the_name_it_was_diverted_to():
    program = images.ELF + b"original tool"
    described_image("o/diverted", **{
        "usr/bin/tool.distrib": program,
        "var/lib/dpkg/info/tool.list": "/usr/bin/tool\n", "var/lib/dpkg/info/tool.md5sums": f"{md5(program)}  usr/bin/tool\n",
        images.DPKG_DIVERSIONS: "/usr/bin/tool\n/usr/bin/tool.distrib\nlocal-wrapper\n",
    })
