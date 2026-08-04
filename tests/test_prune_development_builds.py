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
