# SPDX-License-Identifier: AGPL-3.0-or-later
"""Publishing a manager package: the manifest, and the index that names it.

The package alone says nothing an appliance may act on — dpkg's metadata is
inside the archive being judged — so the manifest is what gets signed, and it
is read out of the artefact rather than typed.

The index carries history for a sharper reason than the OS one does. The
manager has no second slot: an earlier package is the whole recovery, and an
index naming only the newest one takes that away from every appliance that did
not keep a copy locally.
"""

import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

from appliance import manager_releases, os_fetch, persistent_state

ROOT = Path(__file__).resolve().parents[1]
BASE = "https://example.invalid/releases/download/v1"
REVISION = "a" * 40

pytestmark = [pytest.mark.unit, pytest.mark.simulation, pytest.mark.appliance]


def load(name):
    spec = importlib.util.spec_from_file_location(
        name.replace("-", "_"), ROOT / "scripts" / f"{name}.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


manifests = load("appliance-build-manager-manifest")
indexes = load("appliance-build-manager-index")


def write_package(directory, *, version="0.2.0", architecture="arm64", body=b"a package",
                  epoch=1787000000, record=None):
    package = directory / f"ems-appliance-manager_{version}_{architecture}.deb"
    package.write_bytes(body)
    payload = {
        "artifact": package.name,
        "version": version,
        "architecture": architecture,
        "source_date_epoch": epoch,
        "dpkg_deb": "1.22.11",
        "compression": "xz -6",
    }
    if record is not None:
        payload.update(record)
    Path(str(package)[:-4] + ".build.json").write_text(json.dumps(payload), encoding="utf-8")
    return package


# --- the manifest ------------------------------------------------------------


def test_the_manifest_describes_the_package_it_points_at(tmp_path):
    package = write_package(tmp_path, body=b"a real package")

    release_id, payload = manifests.manifest_for(
        package, revision=REVISION, created_at="2026-08-26T01:00:00Z"
    )

    assert release_id == "ems-appliance-manager-0.2.0-arm64"
    assert payload["artifact"]["digest"] == "sha256:" + hashlib.sha256(b"a real package").hexdigest()
    assert payload["artifact"]["size_bytes"] == len(b"a real package")


def test_the_manifest_declares_what_this_tree_can_read(tmp_path):
    """The schemas belong to the manager inside the package, not to the file."""

    _, payload = manifests.manifest_for(
        write_package(tmp_path), revision=REVISION, created_at="2026-08-26T01:00:00Z"
    )

    assert payload["state_schemas"]["implements"] == persistent_state.implemented_schemas()
    assert payload["state_schemas"]["reads"] == persistent_state.readable_floors()


def test_the_manifest_is_parsed_back_before_it_is_published(tmp_path):
    """A manifest this project's own reader refuses must not reach a device."""

    package = write_package(tmp_path, version="0.2.0-rc1", record={"version": "0.2.0-rc1"})

    with pytest.raises(manager_releases.ManagerReleaseError) as refusal:
        manifests.manifest_for(package, revision=REVISION, created_at="2026-08-26T01:00:00Z")

    assert "hyphen" in refusal.value.message


def test_a_package_with_no_build_record_cannot_be_described(tmp_path):
    package = tmp_path / "ems-appliance-manager_0.2.0_arm64.deb"
    package.write_bytes(b"x")

    with pytest.raises(SystemExit) as refusal:
        manifests.manifest_for(package, revision=REVISION, created_at="2026-08-26T01:00:00Z")

    assert "build.json" in str(refusal.value)


def test_a_build_record_describing_another_artefact_is_refused(tmp_path):
    package = write_package(tmp_path, record={"artifact": "something-else.deb"})

    with pytest.raises(SystemExit) as refusal:
        manifests.manifest_for(package, revision=REVISION, created_at="2026-08-26T01:00:00Z")

    assert "something-else.deb" in str(refusal.value)


def test_a_revision_that_is_not_a_commit_is_refused(tmp_path):
    with pytest.raises(SystemExit) as refusal:
        manifests.main(["--revision", "HEAD", str(write_package(tmp_path))])

    assert "40-character" in str(refusal.value)


def test_two_builds_of_one_tag_share_a_build_id(tmp_path):
    """The build id is the timestamp the package is re-derivable from."""

    first = tmp_path / "one"
    second = tmp_path / "two"
    first.mkdir()
    second.mkdir()

    _, one = manifests.manifest_for(
        write_package(first), revision=REVISION, created_at="2026-08-26T01:00:00Z"
    )
    _, two = manifests.manifest_for(
        write_package(second), revision=REVISION, created_at="2026-08-26T01:00:00Z"
    )

    assert one["build_id"] == two["build_id"]


# --- the index ---------------------------------------------------------------


def write_manifest(directory, *, version="0.2.0", epoch=1787000000):
    target_dir = directory / version
    target_dir.mkdir(parents=True, exist_ok=True)
    package = write_package(target_dir, version=version, epoch=epoch, body=version.encode())
    release_id, payload = manifests.manifest_for(
        package, revision=REVISION, created_at="2026-08-26T01:00:00Z"
    )
    path = directory / f"{release_id}.manifest.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return str(path)


def test_an_entry_describes_itself_from_the_manifest_it_points_at(tmp_path):
    index, _ = indexes.build([write_manifest(tmp_path)], base_url=BASE)

    entry = index["releases"][0]
    assert entry["release_id"] == "ems-appliance-manager-0.2.0-arm64"
    assert entry["manifest_url"].startswith(BASE)
    assert entry["archive_url"].endswith("ems-appliance-manager_0.2.0_arm64.deb")
    assert entry["release_version"] == "0.2.0"


def test_the_appliance_reads_back_exactly_what_was_written(tmp_path):
    index, _ = indexes.build(
        [write_manifest(tmp_path, version="0.1.0"), write_manifest(tmp_path, version="0.2.0")],
        base_url=BASE,
    )

    parsed = os_fetch.parse_index(index)

    assert [entry["release_id"] for entry in parsed] == [
        "ems-appliance-manager-0.2.0-arm64",
        "ems-appliance-manager-0.1.0-arm64",
    ]


def test_history_is_carried_so_an_earlier_package_stays_reachable(tmp_path):
    """It is the recovery, not a nicety: the manager has no second slot."""

    previous = tmp_path / "old.json"
    first, _ = indexes.build([write_manifest(tmp_path, version="0.1.0")], base_url=BASE)
    previous.write_text(json.dumps(first), encoding="utf-8")

    index, _ = indexes.build(
        [write_manifest(tmp_path, version="0.2.0")], base_url=BASE, previous=str(previous)
    )

    assert [entry["release_id"] for entry in index["releases"]] == [
        "ems-appliance-manager-0.2.0-arm64",
        "ems-appliance-manager-0.1.0-arm64",
    ]


def test_a_rebuild_replaces_its_row_rather_than_joining_it(tmp_path):
    previous = tmp_path / "old.json"
    first, _ = indexes.build([write_manifest(tmp_path, version="0.2.0")], base_url=BASE)
    previous.write_text(json.dumps(first), encoding="utf-8")

    index, _ = indexes.build(
        [write_manifest(tmp_path, version="0.2.0", epoch=1787000999)],
        base_url=BASE,
        previous=str(previous),
    )

    assert len(index["releases"]) == 1


def test_retention_never_drops_the_package_this_run_just_published(tmp_path):
    previous = tmp_path / "old.json"
    existing, _ = indexes.build(
        [write_manifest(tmp_path, version=f"0.{n}.0") for n in (3, 4, 5)], base_url=BASE
    )
    previous.write_text(json.dumps(existing), encoding="utf-8")

    with pytest.raises(SystemExit) as refusal:
        indexes.build(
            [write_manifest(tmp_path, version="0.1.0")],
            base_url=BASE,
            previous=str(previous),
            keep=2,
        )

    assert "ems-appliance-manager-0.1.0-arm64" in str(refusal.value)


def test_a_plain_http_base_url_is_refused(tmp_path):
    with pytest.raises(SystemExit) as refusal:
        indexes.main(["--base-url", "http://example.invalid/x", write_manifest(tmp_path)])

    assert "https" in str(refusal.value)
