# SPDX-License-Identifier: AGPL-3.0-or-later
"""Admin release-cache data path and Docker mount contracts."""

from pathlib import Path

import pytest

from admin.releases import ReleaseError, ReleaseManager, default_admin_data_dir

pytestmark = pytest.mark.simulation

ROOT = Path(__file__).resolve().parent.parent


def test_configured_admin_data_dir_controls_cache_and_state(monkeypatch, tmp_path):
    data_dir = tmp_path / "admin-data"
    monkeypatch.setenv("EMS_ADMIN_DATA_DIR", str(data_dir))

    manager = ReleaseManager()

    assert default_admin_data_dir() == data_dir
    assert manager.releases_dir == data_dir / "releases"
    assert manager.state_dir == data_dir / "state"


def test_local_default_never_uses_app_data(monkeypatch):
    monkeypatch.delenv("EMS_ADMIN_DATA_DIR", raising=False)

    data_dir = default_admin_data_dir()

    assert data_dir == ROOT / "data" / "admin"
    assert data_dir != Path("/app/data")


def test_prepare_creates_missing_data_release_and_state_directories(
    tmp_path, monkeypatch
):
    data_dir = tmp_path / "missing" / "admin"
    manager = ReleaseManager(data_dir=data_dir)
    monkeypatch.setattr(
        manager,
        "_prepare_locked",
        lambda tag: {"status": "ready", "tag": tag},
    )

    result = manager.prepare("v0.6.0")

    assert result["status"] == "ready"
    assert manager.releases_dir.is_dir()
    assert manager.state_dir.is_dir()


def test_non_writable_data_path_returns_actionable_error(tmp_path):
    data_path = tmp_path / "not-a-directory"
    data_path.write_text("blocks directory creation", encoding="utf-8")
    manager = ReleaseManager(data_dir=data_path)

    with pytest.raises(ReleaseError) as raised:
        manager.prepare("v0.6.0")

    assert raised.value.status == 500
    assert str(raised.value) == (
        f"Admin data directory is not writable: {data_path}. "
        "Check the Docker volume mount for ./data/admin:/data."
    )


@pytest.mark.parametrize(
    "compose_name",
    (
        "docker-compose.yml",
        "docker-compose.discovery-only.yml",
        "docker-compose.hostnet.yml",
    ),
)
def test_admin_compose_mounts_writable_data_directory(compose_name):
    compose = (ROOT / "deploy" / "admin" / compose_name).read_text(encoding="utf-8")

    if compose_name == "docker-compose.hostnet.yml":
        assert "EMS_ADMIN_DATA_DIR" not in compose
        assert "../../data/admin" not in compose
    else:
        # Same-path mounting: the Admin data dir is bound at its real host path.
        assert 'EMS_ADMIN_DATA_DIR: "${EMS_ADMIN_DATA_DIR}"' in compose
        assert '"${EMS_ADMIN_DATA_DIR}:${EMS_ADMIN_DATA_DIR}"' in compose


def test_admin_preview_keeps_read_only_root_filesystem():
    compose = (
        ROOT / "deploy" / "admin" / "docker-compose.discovery-only.yml"
    ).read_text(encoding="utf-8")
    dockerfile = (ROOT / "deploy" / "admin" / "Dockerfile").read_text(
        encoding="utf-8"
    )

    assert "read_only: true" in compose
    assert 'user: "${PUID:-1000}:${PGID:-1000}"' in compose
    assert "ENV EMS_ADMIN_DATA_DIR=/data" in dockerfile
    assert "/app/data" not in dockerfile


def test_admin_host_data_directory_is_tracked_but_runtime_contents_are_ignored():
    gitignore = (ROOT / ".gitignore").read_text(encoding="utf-8")

    assert (ROOT / "data" / "admin" / ".gitkeep").is_file()
    assert "!/data/admin/.gitkeep" in gitignore
    assert "/data/admin/*" in gitignore
