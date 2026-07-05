# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for honest build/version identity (``ems.build_info``).

These never depend on the real repository state: the local-git fallback is
stubbed so results are deterministic on any checkout or in a packaged image.
"""

import pytest

from ems import build_info
from ems.build_info import collect_build_info

pytestmark = pytest.mark.simulation

_EMPTY_GIT = {
    "git_commit": None,
    "git_commit_short": None,
    "git_branch": None,
    "git_describe": None,
    "git_dirty": None,
}


def _no_local_git(monkeypatch):
    monkeypatch.setattr(build_info, "_git_info", lambda base_dir: dict(_EMPTY_GIT))


def test_release_tag_becomes_ems_version(monkeypatch):
    _no_local_git(monkeypatch)
    info = collect_build_info(environ={"EMS_RELEASE_TAG": "v0.6.3"})
    assert info["ems_version"] == "v0.6.3"
    assert info["release_version"] == "v0.6.3"
    assert info["build_label"] == "v0.6.3"


@pytest.mark.parametrize("tag", ["v0.6.3-rc1", "v0.6.3-beta.1", "v1.2.3+build.5"])
def test_prerelease_tags_become_ems_version(monkeypatch, tag):
    _no_local_git(monkeypatch)
    info = collect_build_info(environ={"EMS_RELEASE_TAG": tag})
    assert info["ems_version"] == tag


@pytest.mark.parametrize(
    "value", ["latest", "main", "dev", "unknown", "null", "", "   ", "0.6.0"]
)
def test_channels_and_placeholders_never_become_ems_version(monkeypatch, value):
    _no_local_git(monkeypatch)
    info = collect_build_info(environ={"EMS_RELEASE_TAG": value})
    assert info["ems_version"] is None
    assert info["release_version"] is None


def test_env_git_describe_becomes_build_label(monkeypatch):
    _no_local_git(monkeypatch)
    info = collect_build_info(
        environ={"EMS_RELEASE_TAG": "", "EMS_GIT_DESCRIBE": "v0.6.3-12-gabcdef"}
    )
    assert info["ems_version"] is None
    assert info["git_describe"] == "v0.6.3-12-gabcdef"
    assert info["build_label"] == "v0.6.3-12-gabcdef"


def test_env_derives_short_commit_when_only_full_given(monkeypatch):
    _no_local_git(monkeypatch)
    info = collect_build_info(environ={"EMS_GIT_COMMIT": "abcdef1234567890abc"})
    assert info["git_commit"] == "abcdef1234567890abc"
    assert info["git_commit_short"] == "abcdef123456"
    # A commit is a usable build label when nothing better is available.
    assert info["build_label"] == "abcdef123456"


def test_full_build_metadata_passthrough(monkeypatch):
    _no_local_git(monkeypatch)
    info = collect_build_info(environ={
        "EMS_RELEASE_TAG": "v0.6.3",
        "EMS_GIT_COMMIT": "abcdef1234567890",
        "EMS_GIT_COMMIT_SHORT": "abcdef123456",
        "EMS_GIT_BRANCH": "main",
        "EMS_GIT_DESCRIBE": "v0.6.3",
        "EMS_GIT_DIRTY": "false",
        "EMS_BUILD_ID": "12345-1",
        "EMS_BUILD_SERIAL": "42",
        "EMS_CHANNEL": "stable",
    })
    assert info["channel"] == "stable"
    assert info["build_id"] == "12345-1"
    assert info["build_serial"] == "42"
    assert info["git_branch"] == "main"
    assert info["git_dirty"] is False


def test_falls_back_to_local_git_when_env_absent(monkeypatch):
    monkeypatch.setattr(build_info, "_git_info", lambda base_dir: {
        "git_commit": "abcdef1234567890abcdef",
        "git_commit_short": "abcdef123456",
        "git_branch": "feature/x",
        "git_describe": "v0.6.3-4-gabcdef-dirty",
        "git_dirty": True,
    })
    info = collect_build_info(environ={})
    assert info["ems_version"] is None
    assert info["build_label"] == "v0.6.3-4-gabcdef-dirty"
    assert info["git_commit_short"] == "abcdef123456"
    assert info["git_branch"] == "feature/x"
    assert info["git_dirty"] is True


def test_no_git_no_env_returns_none_not_unknown(monkeypatch, tmp_path):
    _no_local_git(monkeypatch)
    info = collect_build_info(base_dir=str(tmp_path), environ={})
    for key in ("ems_version", "release_version", "build_label", "git_commit",
                "git_commit_short", "git_branch", "git_describe", "git_dirty",
                "build_id", "build_serial", "channel"):
        assert info[key] is None, key
    assert "unknown" not in {str(v).lower() for v in info.values()}


@pytest.mark.parametrize(
    "raw,expected",
    [("true", True), ("1", True), ("yes", True), ("dirty", True),
     ("false", False), ("0", False), ("", None), ("unknown", None)],
)
def test_dirty_flag_parsing_from_env(monkeypatch, raw, expected):
    _no_local_git(monkeypatch)
    # Pin git_commit so the env is treated as the git source (no local fallback).
    info = collect_build_info(
        environ={"EMS_GIT_COMMIT": "abc", "EMS_GIT_DIRTY": raw}
    )
    assert info["git_dirty"] is expected


def test_local_git_helpers_via_run_git_stub(monkeypatch, tmp_path):
    (tmp_path / ".git").mkdir()
    responses = {
        ("rev-parse", "HEAD"): "abcdef1234567890abcdef",
        ("rev-parse", "--abbrev-ref", "HEAD"): "feature/x",
        ("describe", "--tags", "--always", "--dirty"): "v0.6.3-2-gabcdef-dirty",
        ("status", "--porcelain"): " M ems/build_info.py",
    }
    monkeypatch.setattr(
        build_info, "_run_git", lambda args, cwd: responses.get(tuple(args))
    )
    info = collect_build_info(base_dir=str(tmp_path), environ={})
    assert info["git_commit"] == "abcdef1234567890abcdef"
    assert info["git_commit_short"] == "abcdef123456"
    assert info["git_branch"] == "feature/x"
    assert info["git_describe"] == "v0.6.3-2-gabcdef-dirty"
    assert info["git_dirty"] is True
    assert info["build_label"] == "v0.6.3-2-gabcdef-dirty"


def test_missing_dot_git_dir_yields_none(monkeypatch, tmp_path):
    # _git_info short-circuits without a .git directory; _run_git is never called.
    def _fail(*args, **kwargs):  # pragma: no cover - must not run
        raise AssertionError("_run_git should not be called without .git")

    monkeypatch.setattr(build_info, "_run_git", _fail)
    info = collect_build_info(base_dir=str(tmp_path), environ={})
    assert info["git_commit"] is None
    assert info["git_describe"] is None
