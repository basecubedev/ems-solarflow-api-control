# SPDX-License-Identifier: AGPL-3.0-or-later
"""Choosing the package an image bakes in, and refusing the ones it must not.

An image should carry the bytes an operator is offered rather than a second
build of the same source that only happens to match. That makes the build a
consumer of the same index the fleet reads, which means the build has to make
the same decision the fleet makes -- and has to refuse for the same reasons.

Three refusals matter here and each is asserted against a real signature rather
than a stub: a candidate never wins, because an image that quietly baked one in
would ship it to every card; a manifest signed by a key the project does not
trust is not a package; and a download whose digest is not the signed one is
not the package the signature was about.
"""

import importlib.util
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

pytestmark = [pytest.mark.unit, pytest.mark.simulation, pytest.mark.appliance]

gpg_required = pytest.mark.skipif(
    shutil.which("gpg") is None or shutil.which("gpgv") is None,
    reason="the signature is what decides, so there is nothing to test without gpg",
)


def load(name):
    spec = importlib.util.spec_from_file_location(
        name.replace("-", "_"), ROOT / "scripts" / f"{name}.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


fetcher = load("appliance-fetch-manager-package")
manifests = load("appliance-build-manager-manifest")
indexes = load("appliance-build-manager-index")

BASE = "https://example.invalid/releases/download"
REVISION = "b" * 40


def write_package(directory, *, version):
    """A package and the build record beside it, as build-deb.sh leaves them."""

    package = directory / f"ems-appliance-manager_{version}_arm64.deb"
    package.write_bytes(f"package {version}".encode())
    (directory / f"ems-appliance-manager_{version}_arm64.build.json").write_text(
        json.dumps(
            {
                "artifact": package.name,
                "version": version,
                "architecture": "arm64",
                "source_date_epoch": 1787000000,
                "compression": "xz -6",
                "dpkg_deb": "test",
            }
        ),
        encoding="utf-8",
    )
    return package


@pytest.fixture(scope="module")
def signer(tmp_path_factory):
    """A throwaway key and the keyring that trusts it.

    The shipped keyring's private half is not in this repository, which is the
    point of it, so the trust anchor under test here is a stand-in. What is
    real is the verification: the same gpgv call against a keyring file.
    """

    home = tmp_path_factory.mktemp("gnupg")
    home.chmod(0o700)
    environment = {**os.environ, "GNUPGHOME": str(home)}
    subprocess.run(
        ["gpg", "--batch", "--quiet", "--passphrase", "", "--quick-generate-key",
         "Test Signer <t@example.invalid>", "ed25519", "sign", "never"],
        env=environment, check=True, capture_output=True, timeout=120,
    )
    listed = subprocess.run(
        ["gpg", "--batch", "--with-colons", "--list-keys"],
        env=environment, check=True, capture_output=True, text=True, timeout=60,
    )
    fingerprint = next(
        line.split(":")[9] for line in listed.stdout.splitlines() if line.startswith("fpr:")
    )
    keyring = home / "trusted.gpg"
    keyring.write_bytes(
        subprocess.run(
            ["gpg", "--batch", "--export", fingerprint],
            env=environment, check=True, capture_output=True, timeout=60,
        ).stdout
    )
    return {"env": environment, "fingerprint": fingerprint, "keyring": keyring}


def publish(directory, signer, *, version, sign=True):
    """One released version: package, manifest, and a detached signature."""

    package = write_package(directory, version=version)
    release_id, payload = manifests.manifest_for(
        package, revision=REVISION, created_at="2026-08-01T00:00:00Z", release_id=""
    )
    manifest = directory / f"{release_id}.manifest.json"
    manifest.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if sign:
        subprocess.run(
            ["gpg", "--batch", "--yes", "--armor", "--detach-sign",
             "--local-user", signer["fingerprint"],
             "--output", str(manifest) + ".asc", str(manifest)],
            env=signer["env"], check=True, capture_output=True, timeout=120,
        )
    return release_id, manifest, package


def serve(directory, mapping):
    """Stand in for the network: every URL resolves to a file already written."""

    def fetch(url, *, limit, label):
        name = mapping.get(url)
        if name is None:
            raise OSError(f"nothing is published at {url}")
        return (directory / name).read_bytes()

    return fetch


def index_for(directory, released):
    """The index the publisher would have written for these releases."""

    entries = [
        indexes.entry_for(str(directory / f"{release_id}.manifest.json"), f"{BASE}/v{version}")
        for version, release_id in released
    ]
    return {"format_version": 1, "releases": entries}


def urls_for(directory, released):
    mapping = {}
    for version, release_id in released:
        prefix = f"{BASE}/v{version}"
        mapping[f"{prefix}/{release_id}.manifest.json"] = f"{release_id}.manifest.json"
        mapping[f"{prefix}/{release_id}.manifest.json.asc"] = f"{release_id}.manifest.json.asc"
        mapping[f"{prefix}/ems-appliance-manager_{version}_arm64.deb"] = (
            f"ems-appliance-manager_{version}_arm64.deb"
        )
    return mapping


def run(monkeypatch, directory, index, mapping, signer, into, *, version=""):
    (directory / "index.json").write_text(json.dumps(index), encoding="utf-8")
    served = {**mapping, "https://example.invalid/index.json": "index.json"}
    monkeypatch.setattr(fetcher, "fetch", serve(directory, served))
    argv = [
        "--index", "https://example.invalid/index.json",
        "--into", str(into),
        "--keyring", str(signer["keyring"]),
    ]
    if version:
        argv += ["--version", version]
    return fetcher.main(argv)


@gpg_required
def test_the_newest_stable_release_is_the_one_taken(tmp_path, monkeypatch, signer):
    released = [(version, publish(tmp_path, signer, version=version)[0])
                for version in ("0.1.0", "0.2.0")]
    into = tmp_path / "into"

    assert run(monkeypatch, tmp_path, index_for(tmp_path, released),
               urls_for(tmp_path, released), signer, into) == 0
    assert (into / "ems-appliance-manager_0.2.0_arm64.deb").is_file()
    assert not (into / "ems-appliance-manager_0.1.0_arm64.deb").exists()


def candidate_entry(version):
    """An index row naming a candidate.

    Written by hand rather than produced, because this project's own publishing
    chain cannot make one: a prerelease is spelled with a tilde and
    ``artifact_trust.RELEASE_ID`` admits no tilde. The index is untrusted input
    all the same -- it is fetched over the network and nothing about it is
    signed -- so the guard has to hold against a row nobody here wrote.
    """

    release_id = f"ems-appliance-manager-{version.replace('~', '-')}-arm64"
    prefix = f"{BASE}/v{version}"
    return {
        "release_id": release_id,
        "release_version": version,
        "manifest_url": f"{prefix}/{release_id}.manifest.json",
        "signature_url": f"{prefix}/{release_id}.manifest.json.asc",
        "archive_url": f"{prefix}/ems-appliance-manager_{version}_arm64.deb",
    }


@gpg_required
def test_a_candidate_never_wins_even_when_it_is_the_newest_thing_there_is(
    tmp_path, monkeypatch, signer
):
    """An image bakes this in for every card. "Latest" meaning "newest
    candidate" is how a release candidate reaches people who never asked for
    one.

    The candidate's URLs are never served here, so the test also shows the
    choice is made before anything is fetched.
    """

    released = [("0.1.0", publish(tmp_path, signer, version="0.1.0")[0])]
    index = index_for(tmp_path, released)
    index["releases"].insert(0, candidate_entry("0.2.0~rc1"))
    into = tmp_path / "into"

    assert run(monkeypatch, tmp_path, index, urls_for(tmp_path, released), signer, into) == 0
    assert (into / "ems-appliance-manager_0.1.0_arm64.deb").is_file()


def test_an_index_with_no_stable_release_is_not_an_error(tmp_path, monkeypatch, signer):
    """Before the first Manager release there is nothing to fetch, and a build
    that falls back to its own source is doing the right thing. Three, not one,
    so a caller can tell that apart from a refusal."""

    index = {"format_version": 1, "releases": [candidate_entry("0.1.0~rc1")]}

    assert run(monkeypatch, tmp_path, index, {}, signer, tmp_path / "into") == 3


@gpg_required
def test_a_manifest_this_project_did_not_sign_is_not_a_package(tmp_path, monkeypatch, signer):
    released = [("0.1.0", publish(tmp_path, signer, version="0.1.0", sign=False)[0])]
    (tmp_path / f"{released[0][1]}.manifest.json.asc").write_text(
        "-----BEGIN PGP SIGNATURE-----\nnot a signature\n-----END PGP SIGNATURE-----\n",
        encoding="utf-8",
    )

    with pytest.raises(SystemExit) as refused:
        run(monkeypatch, tmp_path, index_for(tmp_path, released),
            urls_for(tmp_path, released), signer, tmp_path / "into")

    assert "does not trust" in str(refused.value) or "not one this project trusts" in str(
        refused.value
    )


@gpg_required
def test_a_download_that_is_not_what_was_signed_is_refused(tmp_path, monkeypatch, signer):
    """The signature covers the manifest, and the manifest names a digest. A
    package that hashes to something else is not the package that was signed,
    however sound the signature over the manifest is."""

    release_id, _, package = publish(tmp_path, signer, version="0.1.0")
    package.write_bytes(b"substituted after signing")

    with pytest.raises(SystemExit) as refused:
        run(monkeypatch, tmp_path, index_for(tmp_path, [("0.1.0", release_id)]),
            urls_for(tmp_path, [("0.1.0", release_id)]), signer, tmp_path / "into")

    assert "hashes to" in str(refused.value)


@gpg_required
def test_a_named_version_can_be_taken_instead_of_the_newest(tmp_path, monkeypatch, signer):
    """What makes rebuilding an older image possible at all."""

    released = [(version, publish(tmp_path, signer, version=version)[0])
                for version in ("0.1.0", "0.2.0")]
    into = tmp_path / "into"

    assert run(monkeypatch, tmp_path, index_for(tmp_path, released),
               urls_for(tmp_path, released), signer, into, version="0.1.0") == 0
    assert (into / "ems-appliance-manager_0.1.0_arm64.deb").is_file()


@gpg_required
def test_an_index_that_overstates_a_version_is_refused(tmp_path, monkeypatch, signer):
    """Which entry to fetch is decided on the index's own claim, because before
    a signature has been checked that is all there is. Once there is a signed
    answer the two have to agree -- otherwise an index naming 0.9.0 for a
    manifest that says 0.1.0 has the build install the old package while
    reporting the new one, with every signature valid."""

    release_id, manifest, _ = publish(tmp_path, signer, version="0.1.0")
    index = {"format_version": 1, "releases": [
        {
            "release_id": release_id,
            "release_version": "0.9.0",
            "manifest_url": f"{BASE}/v0.1.0/{release_id}.manifest.json",
            "signature_url": f"{BASE}/v0.1.0/{release_id}.manifest.json.asc",
            "archive_url": f"{BASE}/v0.1.0/ems-appliance-manager_0.1.0_arm64.deb",
        }
    ]}

    with pytest.raises(SystemExit) as refused:
        run(monkeypatch, tmp_path, index, urls_for(tmp_path, [("0.1.0", release_id)]),
            signer, tmp_path / "into")

    assert "signed manifest says" in str(refused.value)


def test_an_index_that_answers_with_rubbish_is_not_no_index(tmp_path, monkeypatch, signer):
    """Not reachable and not readable are different answers. Before the first
    release the index is simply absent and building one's own package is right;
    an index that answered and turned out to be rubbish is something wrong, and
    the fallback would hide it."""

    (tmp_path / "index.json").write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(fetcher, "fetch", serve(tmp_path, {
        "https://example.invalid/index.json": "index.json"
    }))

    with pytest.raises(SystemExit) as refused:
        fetcher.main([
            "--index", "https://example.invalid/index.json",
            "--into", str(tmp_path / "into"),
            "--keyring", str(signer["keyring"]),
        ])

    assert "not readable json" in str(refused.value)


def raising(error):
    def fetch(url, *, limit, label):
        raise error

    return fetch


def test_a_missing_index_is_the_only_failure_that_falls_back(tmp_path, monkeypatch, signer):
    """404 is the state this project is in before the first Manager release, and
    a build that then makes its own package is doing the right thing."""

    import urllib.error

    monkeypatch.setattr(fetcher, "fetch", raising(
        urllib.error.HTTPError("https://example.invalid/index.json", 404, "Not Found", {}, None)
    ))

    assert fetcher.main([
        "--index", "https://example.invalid/index.json",
        "--into", str(tmp_path / "into"),
        "--keyring", str(signer["keyring"]),
    ]) == 3


@pytest.mark.parametrize("unreachable", ["503", "429", "dns", "timeout"])
def test_an_index_that_could_not_be_reached_fails_the_build(
    tmp_path, monkeypatch, signer, unreachable
):
    """urllib raises every one of these as OSError, so catching the base class
    reported a 503 as "nothing published yet" and put an unsigned package built
    from the checkout into the image, behind one notice line in a three-hour log.
    Harmless while no release exists; permanently wrong afterwards."""

    import urllib.error

    url = "https://example.invalid/index.json"
    error = {
        "503": urllib.error.HTTPError(url, 503, "Service Unavailable", {}, None),
        "429": urllib.error.HTTPError(url, 429, "Too Many Requests", {}, None),
        "dns": urllib.error.URLError("name resolution failed"),
        "timeout": TimeoutError("timed out"),
    }[unreachable]
    monkeypatch.setattr(fetcher, "fetch", raising(error))

    with pytest.raises(SystemExit) as refused:
        fetcher.main([
            "--index", url,
            "--into", str(tmp_path / "into"),
            "--keyring", str(signer["keyring"]),
        ])

    assert "could not be re" in str(refused.value), str(refused.value)


def test_an_index_that_is_not_https_is_refused_before_anything_is_read():
    with pytest.raises(SystemExit) as refused:
        fetcher.main(["--index", "http://example.invalid/i.json", "--into", "/tmp/unused"])

    assert "https" in str(refused.value)
