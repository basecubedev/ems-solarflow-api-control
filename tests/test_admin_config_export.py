# SPDX-License-Identifier: AGPL-3.0-or-later
"""Validated config download serialization and safe-write tests."""

import json

import pytest

from admin.config_export import ConfigExportService, ConfigExportValidationError
from admin.config_preview import ConfigPreviewGenerator

pytestmark = pytest.mark.simulation


@pytest.fixture(autouse=True)
def _isolate_install_root(isolated_install_root):
    """Keep these tests off the developer's real repo-local config/data."""

    return isolated_install_root


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


def _service(tmp_path):
    preview = ConfigPreviewGenerator(_ReleaseManager())
    return ConfigExportService(preview, tmp_path)


def test_serialize_returns_pretty_valid_config_from_release_template(tmp_path):
    payload, preview = _service(tmp_path).serialize(_draft(), 1)
    config = json.loads(payload)

    assert payload.endswith(b"\n")
    assert preview["ready"] is True
    assert config["system"] == TEMPLATE["system"]
    assert config["devices"][0]["name"] == "inverter_1"
    assert config["grid_meter"]["ip"] == "192.168.1.20"


def test_serialize_blocks_invalid_preview(tmp_path):
    with pytest.raises(ConfigExportValidationError) as exc:
        _service(tmp_path).serialize([], 0)
    codes = {
        issue["code"] for issue in exc.value.preview["validation"]["errors"]
    }
    assert "grid_meter_missing" in codes


def test_write_creates_generated_parent_and_config(tmp_path):
    service = _service(tmp_path)
    result = service.write(_draft(), 1)

    target = tmp_path / "generated" / "config.json"
    assert result["ok"] is True
    assert result["path"] == str(target)
    assert result["written_at"].endswith("Z")
    assert json.loads(target.read_text(encoding="utf-8"))["system"] == TEMPLATE["system"]
    assert list(target.parent.glob(".config.*.tmp")) == []


def test_write_refuses_existing_target_without_confirmation(tmp_path):
    service = _service(tmp_path)
    service.write(_draft(), 1)
    target = tmp_path / "generated" / "config.json"
    original = target.read_bytes()

    result = service.write(_draft(), 1, overwrite=False)

    assert result["ok"] is False
    assert result["reason"] == "target_exists"
    assert target.read_bytes() == original


def test_write_replaces_existing_target_only_with_confirmation(tmp_path):
    service = _service(tmp_path)
    target = tmp_path / "generated" / "config.json"
    target.parent.mkdir(parents=True)
    target.write_text('{"old": true}\n', encoding="utf-8")

    result = service.write(_draft(), 1, overwrite=True)

    assert result["ok"] is True
    assert json.loads(target.read_text(encoding="utf-8"))["system"] == TEMPLATE["system"]


def test_export_ignores_local_repo_config_and_leaves_it_untouched(tmp_path, monkeypatch):
    # Regression for the developer-checkout scenario: a gitignored local
    # config/config.json sitting in the resolved repo root must never be adopted
    # as the export base, nor read, modified, or deleted. The export path stays
    # isolated to its own empty install root via an explicit context provider.
    from ems import paths
    from admin.install_context import detect_install_context

    repo_root = tmp_path / "repo"
    (repo_root / "config").mkdir(parents=True)
    (repo_root / "data" / "admin").mkdir(parents=True)
    local_config = repo_root / "config" / "config.json"
    local_config.write_bytes(b'{"operator_only": "do-not-touch"}')
    original = local_config.read_bytes()
    monkeypatch.setattr(paths, "BASE_DIR", str(repo_root))

    install_root = tmp_path / "install"
    preview = ConfigPreviewGenerator(
        _ReleaseManager(),
        install_context_provider=lambda: detect_install_context(
            base_dir=str(install_root)
        ),
    )
    service = ConfigExportService(preview, tmp_path / "admin")

    payload, result = service.serialize(_draft(), 1)
    config = json.loads(payload)

    assert result["base"] == {"source": "release_template"}
    assert "operator_only" not in config
    # The developer's local config is neither used as base, modified, nor removed.
    assert local_config.exists()
    assert local_config.read_bytes() == original
