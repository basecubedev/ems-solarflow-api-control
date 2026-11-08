# SPDX-FileCopyrightText: 2026 basecubedev
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pure-logic contract for pruning old development image versions.

Guards the decision of which GHCR versions get deleted: keep the newest N of a
feature prefix, and never delete a version that carries a protected tag or a tag
outside the prefix.
"""

import importlib.util
from pathlib import Path

import pytest

pytestmark = [
    pytest.mark.system_build,
    pytest.mark.contract,
]

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "prune_development_builds", ROOT / "scripts" / "prune_development_builds.py"
)
_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_module)
select_deletions = _module.select_deletions

PREFIX = "dev-feature-x-abc1234567"


def _version(vid, created_at, tags):
    return {"id": vid, "created_at": created_at, "tags": tags}


def _build(vid, created_at, short, extra_tags=()):
    # An immutable dev build carries its canonical tag plus the commit tag.
    return _version(
        vid,
        created_at,
        [f"{PREFIX}-{short}-{vid}-1", f"{PREFIX}-{short}", *extra_tags],
    )


def test_keeps_newest_two_and_deletes_older():
    versions = [
        _build(101, "2026-07-01T10:00:00Z", "1111111"),
        _build(102, "2026-07-02T10:00:00Z", "2222222"),
        _build(103, "2026-07-03T10:00:00Z", "3333333"),
        _build(104, "2026-07-04T10:00:00Z", "4444444", extra_tags=[PREFIX]),
    ]
    assert sorted(select_deletions(versions, PREFIX, keep=2)) == [101, 102]


def test_current_newest_build_is_never_deleted():
    versions = [_build(200 + i, f"2026-07-0{i}T10:00:00Z", f"{i}{i}{i}{i}{i}{i}{i}")
                for i in range(1, 5)]
    deletions = select_deletions(versions, PREFIX, keep=2)
    assert 204 not in deletions and 203 not in deletions  # two newest kept


def test_foreign_tag_protects_an_old_version():
    versions = [
        _version(101, "2026-07-01T10:00:00Z", [f"{PREFIX}-1111111-101-1", "latest"]),
        _build(102, "2026-07-02T10:00:00Z", "2222222"),
        _build(103, "2026-07-03T10:00:00Z", "3333333"),
        _build(104, "2026-07-04T10:00:00Z", "4444444"),
    ]
    # 101 is the oldest but shares a release tag -> must survive.
    assert 101 not in select_deletions(versions, PREFIX, keep=2)


def test_protect_tag_is_never_deleted():
    versions = [_build(100 + i, f"2026-07-0{i}T10:00:00Z", f"{i}{i}{i}{i}{i}{i}{i}")
                for i in range(1, 5)]
    protected = f"{PREFIX}-1111111-101-1"
    assert 101 not in select_deletions(
        versions, PREFIX, keep=2, protect_tags=[protected]
    )


def test_ignores_untagged_and_other_prefixes():
    versions = [
        _build(101, "2026-07-01T10:00:00Z", "1111111"),
        _build(102, "2026-07-02T10:00:00Z", "2222222"),
        _build(103, "2026-07-03T10:00:00Z", "3333333"),
        _version(900, "2026-06-01T00:00:00Z", []),  # untagged child manifest
        _version(901, "2026-06-01T00:00:00Z", ["dev-other-9999999999-9-1"]),
    ]
    deletions = select_deletions(versions, PREFIX, keep=2)
    assert deletions == [101]  # only the oldest matching build
    assert 900 not in deletions and 901 not in deletions


def test_no_deletion_when_within_keep():
    versions = [
        _build(101, "2026-07-01T10:00:00Z", "1111111"),
        _build(102, "2026-07-02T10:00:00Z", "2222222"),
    ]
    assert select_deletions(versions, PREFIX, keep=2) == []


def build(version_id, run, created, moving=()):
    return {"id": version_id, "name": f"sha256:{version_id:064d}", "created_at": created,
            "tags": [f"{PREFIX}-{run}", *moving]}


def test_the_images_and_their_source_of_one_build_are_kept_or_deleted_together():
    """Run B tagged its source and then failed to tag the images; run C's pruning must not drop source A."""

    ems = [build(1, "a", "1"), build(3, "c", "3", moving=(PREFIX,))]
    sources = [build(11, "a", "1"), build(12, "b", "2"), build(13, "c", "3", moving=(PREFIX,))]
    versions = {"ems": ems, "sources": sources}
    plain = {package: select_deletions(found, PREFIX, 2) for package, found in versions.items()}
    assert plain == {"ems": [], "sources": [11]}
    assert _module.keep_builds_together(versions, plain) == {"ems": [], "sources": []}
    older = {"ems": ems + [build(0, "z", "0")], "sources": sources + [build(10, "z", "0")]}
    pruned = _module.keep_builds_together(older, {package: select_deletions(found, PREFIX, 2) for package, found in older.items()})
    assert pruned == {"ems": [0], "sources": [10]}


def test_a_failed_run_s_digests_are_deleted_but_never_a_version_with_a_foreign_tag():
    versions = [build(1, "a", "1"), {"id": 2, "name": "sha256:" + "f" * 64, "tags": []}, build(3, "c", "3")]
    assert _module.select_digest_deletions(versions, ["sha256:" + "f" * 64, versions[0]["name"]], PREFIX, [f"{PREFIX}-a"]) == [1, 2]
    moved = build(5, "e", "5", moving=(PREFIX,))
    assert _module.select_digest_deletions([moved], [moved["name"]], PREFIX, [f"{PREFIX}-e"]) == []
    versions.append({"id": 4, "name": "sha256:" + "e" * 64, "tags": ["v0.9.0"]})
    with pytest.raises(SystemExit, match="carries v0.9.0"):
        _module.select_digest_deletions(versions, ["sha256:" + "e" * 64], PREFIX)


def run_main(monkeypatch, versions, *arguments):
    deleted = []
    monkeypatch.setattr(_module, "_list_versions", lambda owner_path, package: versions[package])
    monkeypatch.setattr(_module, "_delete_version", lambda owner_path, package, version_id: deleted.append((package, version_id)))
    packages = [item for package in versions for item in ("--package", package)]
    _module.main(["--owner", "o", *packages, "--prefix", PREFIX, *arguments])
    return deleted


def test_the_command_keeps_the_images_and_their_source_of_one_build_together(monkeypatch):
    versions = {"ems": [build(1, "a", "1"), build(3, "c", "3", moving=(PREFIX,))],
                "sources": [build(11, "a", "1"), build(12, "b", "2"), build(13, "c", "3", moving=(PREFIX,))]}
    assert run_main(monkeypatch, versions, "--keep", "2") == []


def test_the_command_deletes_a_failed_run_s_own_versions_only(monkeypatch):
    own = build(21, "f", "9")
    moved = build(22, "f", "9", moving=(PREFIX,))
    versions = {"ems": [own], "sources": [moved]}
    assert run_main(monkeypatch, versions, "--digest", own["name"], "--digest", moved["name"], "--own-tag", f"{PREFIX}-f") == [("ems", 21)]


def test_ghcr_s_last_tagged_version_is_kept_rather_than_failing_the_run(monkeypatch, capsys):
    class Refused:
        returncode, stderr = 1, "gh: You cannot delete the last tagged version of a package. You must delete the package instead. (HTTP 400)"

    monkeypatch.setattr(_module, "_gh_json", lambda args: Refused())
    _module._delete_version("/users/o", "ems-solarflow-sources", 7)
    assert "keeping version 7" in capsys.readouterr().out

    class Broken:
        returncode, stderr = 1, "gh: Server Error (HTTP 500)"

    monkeypatch.setattr(_module, "_gh_json", lambda args: Broken())
    with pytest.raises(SystemExit, match="Failed to delete version 7"):
        _module._delete_version("/users/o", "ems-solarflow-sources", 7)
