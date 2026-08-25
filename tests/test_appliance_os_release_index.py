# SPDX-License-Identifier: AGPL-3.0-or-later
"""The index an appliance reads to learn what it may install.

The appliance has been configured to fetch this file since the first image was
built, and nothing produced one. Without it the update path cannot run at all —
not for an older release, not for the newest.

It carries history for one reason: a release that turns out to be bad is only
recoverable if the one before it is still listed. An index naming a single
release makes every publication a one-way step.
"""

import importlib.util
import json
from pathlib import Path

import pytest

from appliance import os_fetch
from tests.helpers.appliance_ab_artifacts import DEFAULT_MEMBERS, build_manifest, digest_of

ROOT = Path(__file__).resolve().parents[1]
BASE = "https://example.invalid/releases/download/v1"

pytestmark = [pytest.mark.unit, pytest.mark.simulation, pytest.mark.appliance]


def load_producer():
    spec = importlib.util.spec_from_file_location(
        "os_release_index", ROOT / "scripts" / "appliance-build-os-release-index.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


producer = load_producer()


def write_manifest(directory, release_id, **overrides):
    payload = build_manifest(
        archive_digest=digest_of(release_id.encode()),
        archive_size=len(release_id) + 1,
        archive_name=f"{release_id}.tar.zst",
        members={name: dict(entry) for name, entry in DEFAULT_MEMBERS.items()},
        **overrides,
    )
    path = directory / f"{release_id}.manifest.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return str(path)


def test_an_entry_describes_itself_from_the_manifest_it_points_at(tmp_path):
    """Read, never typed: the label cannot disagree with what gets verified."""

    manifest = write_manifest(
        tmp_path, "app-1.5.0-ab", release_version="1.5.0", build_id="20260807-1"
    )

    index, _ = producer.build([manifest], base_url=BASE)
    entry = index["releases"][0]

    assert entry["release_id"] == "app-1.5.0-ab"
    assert entry["release_version"] == "1.5.0"
    assert entry["build_id"] == "20260807-1"
    assert entry["manifest_url"] == f"{BASE}/app-1.5.0-ab.manifest.json"
    assert entry["signature_url"] == f"{BASE}/app-1.5.0-ab.manifest.json.asc"
    assert entry["archive_url"] == f"{BASE}/app-1.5.0-ab.tar.zst"


def test_publishing_a_release_does_not_delist_the_one_before_it(tmp_path):
    """The whole point: a bad release stays recoverable."""

    first = write_manifest(tmp_path, "app-1.0.0-ab", release_version="1.0.0")
    old_index = tmp_path / "old.json"
    index, _ = producer.build([first], base_url=BASE)
    old_index.write_text(json.dumps(index), encoding="utf-8")

    second = write_manifest(tmp_path, "app-1.1.0-ab", release_version="1.1.0")
    merged, _ = producer.build([second], base_url=BASE, previous=str(old_index))

    assert [entry["release_id"] for entry in merged["releases"]] == [
        "app-1.1.0-ab",
        "app-1.0.0-ab",
    ]


def test_a_rebuild_replaces_its_entry_rather_than_joining_it(tmp_path):
    first = write_manifest(tmp_path, "app-1.0.0-ab", release_version="1.0.0", build_id="a")
    old_index = tmp_path / "old.json"
    index, _ = producer.build([first], base_url=BASE)
    old_index.write_text(json.dumps(index), encoding="utf-8")

    again = write_manifest(tmp_path, "app-1.0.0-ab", release_version="1.0.0", build_id="b")
    merged, _ = producer.build([again], base_url=BASE, previous=str(old_index))

    assert len(merged["releases"]) == 1
    assert merged["releases"][0]["build_id"] == "b"


def test_releases_are_listed_newest_first(tmp_path):
    manifests = [
        write_manifest(tmp_path, "app-0.9.0-ab", release_version="0.9.0"),
        write_manifest(tmp_path, "app-0.10.0-ab", release_version="0.10.0"),
        write_manifest(tmp_path, "app-0.10.0rc-ab", release_version="0.10.0-rc1"),
    ]

    index, _ = producer.build(manifests, base_url=BASE)

    assert [entry["release_version"] for entry in index["releases"]] == [
        "0.10.0",
        "0.10.0-rc1",
        "0.9.0",
    ]


def test_a_release_that_stops_being_listed_is_named_rather_than_dropped_quietly(tmp_path):
    """A delisted release is one no appliance can reach any more.

    Saying which, out loud, is the difference between a retention policy and a
    silent truncation. Retention applies to carried-forward history; what this
    run published is never eligible, which the next test covers.
    """

    history = [
        write_manifest(tmp_path, f"app-1.{n}.0-ab", release_version=f"1.{n}.0") for n in range(3)
    ]
    old_index = tmp_path / "old.json"
    index, _ = producer.build(history, base_url=BASE)
    old_index.write_text(json.dumps(index), encoding="utf-8")

    newest = write_manifest(tmp_path, "app-1.4.0-ab", release_version="1.4.0")
    index, dropped = producer.build([newest], base_url=BASE, previous=str(old_index), keep=2)

    assert [entry["release_version"] for entry in index["releases"]] == ["1.4.0", "1.2.0"]
    assert [entry["release_version"] for entry in dropped] == ["1.1.0", "1.0.0"]


def test_publishing_more_releases_than_retention_lists_is_refused(tmp_path):
    """Publishing four and listing two hides two of the four just published."""

    manifests = [
        write_manifest(tmp_path, f"app-5.{n}.0-ab", release_version=f"5.{n}.0") for n in range(4)
    ]

    with pytest.raises(SystemExit):
        producer.build(manifests, base_url=BASE, keep=2)


def test_the_appliance_reads_back_exactly_what_was_written(tmp_path):
    manifests = [
        write_manifest(tmp_path, "app-1.0.0-ab", release_version="1.0.0"),
        write_manifest(tmp_path, "app-1.1.0-ab", release_version="1.1.0"),
    ]

    index, _ = producer.build(manifests, base_url=BASE)
    parsed = os_fetch.parse_index(index)

    assert [entry["release_id"] for entry in parsed] == [
        "app-1.1.0-ab",
        "app-1.0.0-ab",
    ]
    assert parsed[0]["described"]["release_version"] == "1.1.0"


def test_a_plain_http_base_url_is_refused(tmp_path, capsys):
    manifest = write_manifest(tmp_path, "app-1.0.0-ab")

    with pytest.raises(SystemExit) as caught:
        producer.main(["--base-url", "http://example.invalid/x", manifest])

    assert "https" in str(caught.value)


# --- what the appliance does with it -----------------------------------------


def index_payload(*entries):
    return {"format_version": os_fetch.INDEX_FORMAT_VERSION, "releases": list(entries)}


def candidate(release_id, **described):
    return {
        "release_id": release_id,
        "manifest_url": f"{BASE}/{release_id}.manifest.json",
        "signature_url": f"{BASE}/{release_id}.manifest.json.asc",
        "archive_url": f"{BASE}/{release_id}.tar.zst",
        **described,
    }


def test_an_index_that_says_nothing_still_sorts_deterministically():
    parsed = os_fetch.parse_index(
        index_payload(candidate("app-b"), candidate("app-a"), candidate("app-c"))
    )

    assert [entry["release_id"] for entry in parsed] == ["app-c", "app-b", "app-a"]


def test_what_an_entry_claims_about_itself_is_kept_apart_from_where_to_fetch_it():
    """The index is a suggestion. Its claims label a choice; they decide nothing."""

    parsed = os_fetch.parse_index(
        index_payload(candidate("app-a", release_version="1.0.0", board="pi5"))
    )

    assert parsed[0]["described"] == {"release_version": "1.0.0", "board": "pi5"}
    assert "release_version" not in set(parsed[0]) - {"described"}


@pytest.mark.parametrize("value", [None, "", "   ", True, {"nested": 1}, ["list"]])
def test_a_claim_that_is_not_a_word_is_left_out(value):
    parsed = os_fetch.parse_index(index_payload(candidate("app-a", release_version=value)))

    assert "release_version" not in parsed[0]["described"]


def test_a_claim_cannot_grow_without_bound():
    parsed = os_fetch.parse_index(
        index_payload(candidate("app-a", release_version="9" * 500))
    )

    assert len(parsed[0]["described"]["release_version"]) == os_fetch.MAX_DESCRIPTION


# --- when the release act builds one -----------------------------------------


def finalizer():
    return (ROOT / "scripts" / "appliance-finalize-rpi-release.sh").read_text(encoding="utf-8")


def test_the_finalizer_builds_an_index_only_when_told_where_it_will_be_published():
    """An index of unreachable urls is worse than none.

    The appliance would refuse each entry one at a time instead of reporting
    that no index is configured, which reads to an operator as a broken release
    rather than an unconfigured one.
    """

    script = finalizer()

    assert 'INDEX_BASE_URL=""' in script
    assert 'if [ -n "$INDEX_BASE_URL" ]; then' in script
    assert "appliance-build-os-release-index.py" in script


def test_the_index_is_built_after_the_gate_and_after_the_signature():
    """It names releases appliances will fetch, so it must name proven ones."""

    script = finalizer()
    signing = script.index("--detach-sign")
    gate = script.index("release-gate-report.txt")
    index = script.index("appliance-build-os-release-index.py")

    assert index > signing
    assert index > gate


def test_the_indexed_variant_is_the_requested_one_not_a_written_out_suffix():
    """A hardcoded suffix would index the wrong variant of a two-variant dist."""

    script = finalizer()

    assert '"$DIST"/*-arm64-"$VARIANT".manifest.json' in script
    assert "has_update_archive" in script


def test_a_single_slot_release_is_refused_rather_than_offered(tmp_path):
    """It is patched by apt and has no second slot: no update could ever apply."""

    manifest = write_manifest(tmp_path, "app-1.0.0-single")

    with pytest.raises(SystemExit) as caught:
        producer.entry_for(manifest, BASE)

    assert "no update archive" in str(caught.value)


def test_an_identifier_naming_no_known_variant_is_refused(tmp_path):
    manifest = write_manifest(tmp_path, "app-1.0.0-something")

    with pytest.raises(SystemExit) as caught:
        producer.entry_for(manifest, BASE)

    assert "names no image variant" in str(caught.value)


def test_retention_never_drops_the_release_this_run_just_published(tmp_path):
    """Sorting is by version, and the newest release is not always the highest.

    A patch to an older line, or a rebuild, sorts below what is already listed.
    Dropping it would publish an assets bundle no appliance can see, which reads
    as a release that silently did not happen.
    """

    old_index = tmp_path / "old.json"
    existing = [
        write_manifest(tmp_path, f"app-2.{n}.0-ab", release_version=f"2.{n}.0") for n in range(3)
    ]
    index, _ = producer.build(existing, base_url=BASE)
    old_index.write_text(json.dumps(index), encoding="utf-8")

    patch = write_manifest(tmp_path, "app-1.9.1-ab", release_version="1.9.1")

    with pytest.raises(SystemExit) as caught:
        producer.build([patch], base_url=BASE, previous=str(old_index), keep=2)

    assert "app-1.9.1-ab" in str(caught.value)


def test_retention_still_drops_older_releases_that_this_run_did_not_publish(tmp_path):
    manifests = [
        write_manifest(tmp_path, f"app-3.{n}.0-ab", release_version=f"3.{n}.0") for n in range(3)
    ]
    old_index = tmp_path / "old.json"
    index, _ = producer.build(manifests, base_url=BASE)
    old_index.write_text(json.dumps(index), encoding="utf-8")

    newest = write_manifest(tmp_path, "app-3.9.0-ab", release_version="3.9.0")
    kept, dropped = producer.build([newest], base_url=BASE, previous=str(old_index), keep=2)

    assert [e["release_version"] for e in kept["releases"]] == ["3.9.0", "3.2.0"]
    assert [e["release_version"] for e in dropped] == ["3.1.0", "3.0.0"]
