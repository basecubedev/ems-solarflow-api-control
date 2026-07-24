# SPDX-License-Identifier: AGPL-3.0-or-later
"""Admin install-state detection, routing and legacy-config migration."""

import json
from pathlib import Path

import pytest

from admin import install_state as istate

pytestmark = pytest.mark.simulation


def _standard(root):
    path = root / "config" / "config.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))
    return path


def _marker(root):
    marker = root / "data" / "admin" / "state" / ".admin-deployment.json"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("{}")
    return marker


# --- detection --------------------------------------------------------------


def test_fresh_install_recommends_setup(tmp_path):
    state = istate.detect_install_state(base_dir=tmp_path)
    assert state.state == istate.STATE_NONE
    assert state.recommended_path == istate.PATH_SETUP_NEW
    assert state.setup_requires_confirmation is False
    assert state.legacy_migration_available is False


def test_standard_config_only_recommends_setup_but_still_confirms(tmp_path):
    # The wizard writes config/config.json before anything is deployed; a lone
    # config is still a fresh install, not an existing system to maintain.
    _write_json(_standard(tmp_path), {"a": 1})
    state = istate.detect_install_state(base_dir=tmp_path)
    assert state.state == istate.STATE_STANDARD_CONFIG_ONLY
    assert state.recommended_path == istate.PATH_SETUP_NEW
    assert state.setup_requires_confirmation is True


def _probe(*, available=True, container_exists=False):
    evidence = (
        {"available": True, "container_exists": container_exists}
        if available
        else {"available": False}
    )
    return lambda: evidence


def test_running_ems_container_recommends_maintenance(tmp_path):
    _write_json(_standard(tmp_path), {"a": 1})
    state = istate.detect_install_state(
        base_dir=tmp_path, ems_container_probe=_probe(container_exists=True)
    )
    assert state.recommended_path == istate.PATH_MANAGE_EXISTING


def test_prepared_but_no_ems_container_recommends_setup(tmp_path):
    _write_json(_standard(tmp_path), {"a": 1})
    (tmp_path / "docker-compose.yml").write_text("services: {}")
    state = istate.detect_install_state(
        base_dir=tmp_path, ems_container_probe=_probe(container_exists=False)
    )
    assert state.state == istate.STATE_STANDARD_INSTALL
    assert state.recommended_path == istate.PATH_SETUP_NEW


def test_docker_unavailable_falls_back_to_filesystem(tmp_path):
    _write_json(_standard(tmp_path), {"a": 1})
    (tmp_path / "docker-compose.yml").write_text("services: {}")
    state = istate.detect_install_state(
        base_dir=tmp_path, ems_container_probe=_probe(available=False)
    )
    assert state.recommended_path == istate.PATH_MANAGE_EXISTING


def test_compose_only_recommends_manage(tmp_path):
    (tmp_path / "docker-compose.yml").write_text("services: {}")
    state = istate.detect_install_state(base_dir=tmp_path)
    assert state.state == istate.STATE_COMPOSE_ONLY
    assert state.recommended_path == istate.PATH_MANAGE_EXISTING


def test_standard_install_recommends_manage(tmp_path):
    _write_json(_standard(tmp_path), {"a": 1})
    (tmp_path / "docker-compose.yml").write_text("services: {}")
    state = istate.detect_install_state(base_dir=tmp_path)
    assert state.state == istate.STATE_STANDARD_INSTALL
    assert state.recommended_path == istate.PATH_MANAGE_EXISTING


def test_admin_marker_promotes_to_admin_prepared_install(tmp_path):
    _write_json(_standard(tmp_path), {"a": 1})
    (tmp_path / "docker-compose.yml").write_text("services: {}")
    _marker(tmp_path)
    state = istate.detect_install_state(base_dir=tmp_path)
    assert state.state == istate.STATE_ADMIN_PREPARED_INSTALL
    assert state.recommended_path == istate.PATH_MANAGE_EXISTING


def test_legacy_root_config_recommends_manage_with_migration(tmp_path):
    _write_json(tmp_path / "config.json", {"a": 1})
    state = istate.detect_install_state(base_dir=tmp_path)
    assert state.state == istate.STATE_LEGACY_ROOT_CONFIG
    assert state.recommended_path == istate.PATH_MANAGE_EXISTING
    assert state.legacy_migration_available is True
    assert any("legacy root config.json" in r for r in state.reasons)


def test_damaged_standard_config_is_partial_install(tmp_path):
    path = _standard(tmp_path)
    path.write_text("this is not json")
    state = istate.detect_install_state(base_dir=tmp_path)
    assert state.state == istate.STATE_PARTIAL_INSTALL
    assert state.recommended_path == istate.PATH_MANAGE_EXISTING


def test_both_configs_same_standard_active_no_warning(tmp_path):
    _write_json(_standard(tmp_path), {"a": 1})
    _write_json(tmp_path / "config.json", {"a": 1})
    (tmp_path / "docker-compose.yml").write_text("services: {}")
    state = istate.detect_install_state(base_dir=tmp_path)
    assert state.state == istate.STATE_STANDARD_INSTALL
    assert state.config_layout_state == "both_same"
    assert state.warnings == []


def test_both_configs_different_surfaces_warning(tmp_path):
    _write_json(_standard(tmp_path), {"a": 1})
    _write_json(tmp_path / "config.json", {"a": 2})
    (tmp_path / "docker-compose.yml").write_text("services: {}")
    state = istate.detect_install_state(base_dir=tmp_path)
    assert state.state == istate.STATE_STANDARD_INSTALL
    assert state.config_layout_state == "both_different"
    assert state.warnings
    # The standard config is already active; divergence is a warning, not an
    # auto-offered migration.
    assert state.legacy_migration_available is False


# --- routing ----------------------------------------------------------------


def test_setup_new_on_fresh_install_ok(tmp_path):
    result = istate.select_start_path(istate.PATH_SETUP_NEW, base_dir=tmp_path)
    assert result["ok"] is True
    assert result["route"] == "setup"
    assert result["requires_confirmation"] is False


def test_setup_new_on_existing_requires_confirmation(tmp_path):
    _write_json(_standard(tmp_path), {"a": 1})
    result = istate.select_start_path(istate.PATH_SETUP_NEW, base_dir=tmp_path)
    assert result["ok"] is False
    assert result["requires_confirmation"] is True

    confirmed = istate.select_start_path(
        istate.PATH_SETUP_NEW, base_dir=tmp_path, confirm=True
    )
    assert confirmed["ok"] is True
    assert confirmed["requires_confirmation"] is False


def test_manage_existing_routes_to_maintenance(tmp_path):
    _write_json(_standard(tmp_path), {"a": 1})
    result = istate.select_start_path(istate.PATH_MANAGE_EXISTING, base_dir=tmp_path)
    assert result["route"] == "maintenance"
    assert result["migrate_legacy_config"] is False


def test_manage_existing_flags_legacy_migration(tmp_path):
    _write_json(tmp_path / "config.json", {"a": 1})
    result = istate.select_start_path(istate.PATH_MANAGE_EXISTING, base_dir=tmp_path)
    assert result["route"] == "maintenance"
    assert result["migrate_legacy_config"] is True


def test_unknown_choice_raises(tmp_path):
    with pytest.raises(ValueError):
        istate.select_start_path("docker_bootstrap", base_dir=tmp_path)


# --- legacy migration -------------------------------------------------------


def test_migrate_legacy_creates_standard_with_backup(tmp_path):
    legacy = _write_json(tmp_path / "config.json", {"a": 1})
    (tmp_path / "data").mkdir()

    result = istate.migrate_legacy_root_config(base_dir=tmp_path)

    standard = tmp_path / "config" / "config.json"
    assert result["ok"] is True and result["migrated"] is True
    assert standard.read_bytes() == legacy.read_bytes()
    assert legacy.exists(), "legacy source must be preserved"
    assert (tmp_path / "data").is_dir(), "runtime data must be preserved"
    assert "root-config-legacy-" in result["backup_path"]
    assert result["backup_path"].endswith(".json")
    assert Path(result["backup_path"]).read_bytes() == legacy.read_bytes()


def test_migrate_legacy_missing_source_raises_404(tmp_path):
    with pytest.raises(istate.LegacyMigrationError) as exc:
        istate.migrate_legacy_root_config(base_dir=tmp_path)
    assert exc.value.reason == "legacy_config_missing"
    assert exc.value.status == 404


def test_migrate_legacy_invalid_json_raises(tmp_path):
    (tmp_path / "config.json").write_text("[]")
    with pytest.raises(istate.LegacyMigrationError) as exc:
        istate.migrate_legacy_root_config(base_dir=tmp_path)
    assert exc.value.reason == "invalid_legacy_config"


def test_migrate_legacy_does_not_overwrite_standard_without_confirm(tmp_path):
    _write_json(tmp_path / "config.json", {"a": 1})
    standard = _write_json(_standard(tmp_path), {"a": 999})

    with pytest.raises(istate.LegacyMigrationError) as exc:
        istate.migrate_legacy_root_config(base_dir=tmp_path)
    assert exc.value.reason == "target_exists"
    assert exc.value.status == 409
    assert json.loads(standard.read_text()) == {"a": 999}


def test_migrate_legacy_overwrite_backs_up_existing_standard(tmp_path):
    legacy = _write_json(tmp_path / "config.json", {"a": 1})
    _write_json(_standard(tmp_path), {"a": 999})

    result = istate.migrate_legacy_root_config(base_dir=tmp_path, overwrite=True)

    standard = tmp_path / "config" / "config.json"
    assert result["migrated"] is True
    assert standard.read_bytes() == legacy.read_bytes()
    assert len(result["backups"]) == 2


def test_migrate_legacy_idempotent_when_identical(tmp_path):
    _write_json(tmp_path / "config.json", {"a": 1})
    _write_json(_standard(tmp_path), {"a": 1})

    result = istate.migrate_legacy_root_config(base_dir=tmp_path)
    assert result["ok"] is True
    assert result["migrated"] is False
    assert result["reason"] == "already_migrated"


# --- EMS_INSTALL_DIR resolution (container/runtime path) --------------------
#
# In the Admin container ``base_dir`` is None and ``ems.paths.BASE_DIR`` points
# at ``/app``; the real install root only arrives via ``EMS_INSTALL_DIR``. These
# tests pin ``BASE_DIR`` to an unrelated directory so a regression that ignored
# the env would resolve there and fail loudly.


@pytest.fixture()
def _decoy_base_dir(tmp_path, monkeypatch):
    decoy = tmp_path / "app"
    decoy.mkdir()
    monkeypatch.setattr(istate.paths, "BASE_DIR", str(decoy))
    monkeypatch.delenv("EMS_CONFIG_FILE", raising=False)
    monkeypatch.delenv("EMS_TEMPLATE_FILE", raising=False)
    return decoy


def test_env_install_dir_standard_install(tmp_path, monkeypatch, _decoy_base_dir):
    root = tmp_path / "install"
    _write_json(_standard(root), {"a": 1})
    (root / "docker-compose.yml").write_text("services: {}")
    monkeypatch.setenv("EMS_INSTALL_DIR", str(root))

    state = istate.detect_install_state()
    assert state.state == istate.STATE_STANDARD_INSTALL
    assert state.recommended_path == istate.PATH_MANAGE_EXISTING
    assert state.paths["standard_config"] == str(root / "config" / "config.json")
    assert state.paths["install_root"] == str(root)


def test_env_install_dir_legacy_root_config(tmp_path, monkeypatch, _decoy_base_dir):
    root = tmp_path / "install"
    _write_json(root / "config.json", {"a": 1})
    monkeypatch.setenv("EMS_INSTALL_DIR", str(root))

    state = istate.detect_install_state()
    assert state.state == istate.STATE_LEGACY_ROOT_CONFIG
    assert state.legacy_migration_available is True
    assert state.paths["legacy_config"] == str(root / "config.json")


def test_env_install_dir_standard_config_only(tmp_path, monkeypatch, _decoy_base_dir):
    root = tmp_path / "install"
    _write_json(_standard(root), {"a": 1})
    monkeypatch.setenv("EMS_INSTALL_DIR", str(root))

    state = istate.detect_install_state()
    assert state.state == istate.STATE_STANDARD_CONFIG_ONLY


def test_env_install_dir_compose_only(tmp_path, monkeypatch, _decoy_base_dir):
    root = tmp_path / "install"
    root.mkdir()
    (root / "docker-compose.yml").write_text("services: {}")
    monkeypatch.setenv("EMS_INSTALL_DIR", str(root))

    state = istate.detect_install_state()
    assert state.state == istate.STATE_COMPOSE_ONLY


def test_env_install_dir_migrates_within_resolved_root(
    tmp_path, monkeypatch, _decoy_base_dir
):
    root = tmp_path / "install"
    legacy = _write_json(root / "config.json", {"a": 1})
    (root / "data").mkdir()
    monkeypatch.setenv("EMS_INSTALL_DIR", str(root))

    result = istate.migrate_legacy_root_config()

    standard = root / "config" / "config.json"
    assert result["target"] == str(standard)
    assert standard.read_bytes() == legacy.read_bytes()
    assert legacy.exists(), "legacy source must be preserved"
    assert (root / "data").is_dir(), "runtime data must be preserved"
    backup = Path(result["backup_path"])
    assert backup.parent == root / "data" / "admin" / "backups" / "config"
    assert backup.read_bytes() == legacy.read_bytes()


def test_env_install_dir_migrate_target_exists_conflicts(
    tmp_path, monkeypatch, _decoy_base_dir
):
    root = tmp_path / "install"
    _write_json(root / "config.json", {"a": 1})
    standard = _write_json(_standard(root), {"a": 999})
    monkeypatch.setenv("EMS_INSTALL_DIR", str(root))

    with pytest.raises(istate.LegacyMigrationError) as exc:
        istate.migrate_legacy_root_config()
    assert exc.value.status == 409
    assert json.loads(standard.read_text()) == {"a": 999}

    result = istate.migrate_legacy_root_config(overwrite=True)
    assert result["migrated"] is True
