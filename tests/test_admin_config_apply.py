# SPDX-License-Identifier: AGPL-3.0-or-later
"""Apply a validated Admin setup config to the resolved real EMS config path."""

import json

import pytest

from admin.config_apply import ConfigApplyService
from admin.config_export import ConfigExportService, ConfigExportValidationError
from admin.config_preview import ConfigPreviewGenerator
from admin.install_context import detect_install_context

pytestmark = pytest.mark.simulation


TEMPLATE = {
    "system": {"max_total_power": 800},
    "devices": [
        {
            "name": "WR1",
            "ip": "192.0.2.10",
            "sn": "YOUR_SN",
            "max_power": 800,
        }
    ],
    "grid_meter": {"type": "shelly", "ip": "192.0.2.20"},
}


class _ReleaseManager:
    def config_template(self):
        return {"tag": "v0.6.0", "template": TEMPLATE}


def _draft():
    return [
        {
            "config_name": "inverter_1",
            "display_name": "SolarFlow",
            "role": "inverter",
            "enabled": True,
            "ip": "192.168.1.10",
            "serial_number": "SN123",
        },
        {
            "config_name": "grid_meter",
            "display_name": "Shelly Pro 3EM",
            "role": "grid_meter",
            "enabled": True,
            "ip": "192.168.1.20",
            "api_family": "shelly_gen2",
        },
    ]


def _service(admin_data_dir, install_root):
    provider = lambda: detect_install_context(base_dir=str(install_root))
    preview = ConfigPreviewGenerator(_ReleaseManager(), install_context_provider=provider)
    export = ConfigExportService(preview, admin_data_dir)
    return ConfigApplyService(export, admin_data_dir, install_context_provider=provider)


def test_apply_targets_ems_install_dir_config_path(tmp_path, monkeypatch):
    install_root = tmp_path / "ems"
    admin_data_dir = tmp_path / "admin"
    monkeypatch.setenv("EMS_INSTALL_DIR", str(install_root))
    provider = detect_install_context
    preview = ConfigPreviewGenerator(_ReleaseManager(), install_context_provider=provider)
    export = ConfigExportService(preview, admin_data_dir)
    service = ConfigApplyService(export, admin_data_dir, install_context_provider=provider)

    result = service.apply(_draft(), 1)

    target = install_root / "config" / "config.json"
    assert result["ok"] is True
    assert result["path"] == str(target)
    assert target.is_file()
    assert not (tmp_path / "app" / "config" / "config.json").exists()


def test_apply_creates_fresh_config_without_backup(tmp_path):
    install_root = tmp_path / "ems"
    service = _service(tmp_path / "admin", install_root)

    result = service.apply(_draft(), 1)

    target = install_root / "config" / "config.json"
    assert result["created"] is True
    assert result["backup_path"] is None
    assert result["path"] == str(target)
    config = json.loads(target.read_text(encoding="utf-8"))
    assert config["devices"][0]["name"] == "inverter_1"
    assert list(target.parent.glob(".config.*.tmp")) == []


def test_apply_backs_up_existing_config_before_overwrite(tmp_path):
    install_root = tmp_path / "ems"
    target = install_root / "config" / "config.json"
    target.parent.mkdir(parents=True)
    old_bytes = b'{"system": {"max_total_power": 111}, "devices": []}\n'
    target.write_bytes(old_bytes)

    service = _service(tmp_path / "admin", install_root)
    result = service.apply(_draft(), 1)

    assert result["created"] is False
    assert result["backup_path"] is not None
    backup = tmp_path / "admin" / "backups" / "config"
    backup_file = backup / result["backup_path"].rsplit("/", 1)[-1]
    assert backup_file.read_bytes() == old_bytes
    # The live config was replaced with the newly generated one.
    assert target.read_bytes() != old_bytes
    assert json.loads(target.read_text(encoding="utf-8"))["devices"][0]["name"] == "inverter_1"


def test_apply_rejects_invalid_config_and_keeps_existing(tmp_path):
    install_root = tmp_path / "ems"
    target = install_root / "config" / "config.json"
    target.parent.mkdir(parents=True)
    original = b'{"keep": true}\n'
    target.write_bytes(original)

    service = _service(tmp_path / "admin", install_root)
    with pytest.raises(ConfigExportValidationError):
        service.apply([], 0)

    assert target.read_bytes() == original


def test_apply_failure_leaves_existing_config_unchanged(tmp_path):
    install_root = tmp_path / "ems"
    target = install_root / "config" / "config.json"
    target.parent.mkdir(parents=True)
    original = b'{"keep": true}\n'
    target.write_bytes(original)

    admin_data_dir = tmp_path / "admin"
    # A file where the backup directory tree must be created forces the backup
    # (and therefore the whole apply) to fail before the target is touched.
    (admin_data_dir).mkdir()
    (admin_data_dir / "backups").write_text("not a directory", encoding="utf-8")

    service = _service(admin_data_dir, install_root)
    with pytest.raises(OSError):
        service.apply(_draft(), 1)

    assert target.read_bytes() == original
