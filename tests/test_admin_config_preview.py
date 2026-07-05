# SPDX-License-Identifier: AGPL-3.0-or-later
"""Config preview generation tests."""

import copy
import json

import pytest

from admin.config_preview import ConfigPreviewGenerator
from admin.install_context import AdminInstallContext
from admin.releases import ReleaseError

pytestmark = pytest.mark.simulation


TEMPLATE = {
    "system": {"max_total_power": 1600, "dry_run": False},
    "devices": [
        {"name": "WR1", "ip": "192.0.2.1", "sn": "YOUR_SN", "max_power": 800},
        {"name": "WR2", "ip": "192.0.2.2", "sn": "YOUR_SN", "max_power": 600},
    ],
    "grid_meter": {"type": "shelly", "ip": "192.0.2.3"},
    "future_release_default": {"preserved": True},
}


class _ReleaseManager:
    def __init__(self, template=TEMPLATE):
        self.template = template

    def config_template(self):
        return {"tag": "v0.6.0", "template": self.template}


def _device(index=1, **values):
    item = {
        "config_name": f"inverter_{index}",
        "display_name": f"SolarFlow {index}",
        "role": "inverter",
        "enabled": True,
        "ip": f"192.168.1.{index}",
        "serial_number": f"SN{index}",
        "device_type": "zendure_solarflow_800_pro",
        "api_family": "zendure_local_http",
    }
    item.update(values)
    return item


def _meter(**values):
    item = {
        "config_name": "grid_meter",
        "display_name": "Shelly Pro 3EM",
        "role": "grid_meter",
        "enabled": True,
        "ip": "shelly-meter.local",
        "api_family": "shelly_gen2",
        "device_type": "shelly_pro_3em",
    }
    item.update(values)
    return item


def test_preview_preserves_template_defaults_and_does_not_mutate_template():
    source = copy.deepcopy(TEMPLATE)
    result = ConfigPreviewGenerator(_ReleaseManager(source)).generate(
        [_device(1), _device(2), _meter()], 1
    )

    assert result["ready"] is True
    assert result["release"] == "v0.6.0"
    assert result["config"]["system"] == TEMPLATE["system"]
    assert result["config"]["future_release_default"] == {"preserved": True}
    assert result["config"]["devices"][0] == {
        "name": "inverter_1",
        "ip": "192.168.1.1",
        "sn": "SN1",
        "max_power": 800,
    }
    assert result["config"]["devices"][1]["max_power"] == 600
    assert result["config"]["grid_meter"]["type"] == "shelly"
    assert result["config"]["grid_meter"]["ip"] == "shelly-meter.local"
    assert source == TEMPLATE


def test_preview_preserves_template_top_level_key_order():
    result = ConfigPreviewGenerator(_ReleaseManager()).generate(
        [_device(1), _device(2), _meter()], 1
    )
    assert list(result["config"].keys()) == list(TEMPLATE.keys())


def test_preview_preserves_nested_subkey_order_for_template_sections():
    result = ConfigPreviewGenerator(_ReleaseManager()).generate(
        [_device(1), _meter()], 1
    )
    device = result["config"]["devices"][0]
    # Existing template subkeys keep their order; new keys append at the end.
    assert list(device.keys()) == ["name", "ip", "sn", "max_power"]
    grid = result["config"]["grid_meter"]
    assert list(grid.keys()) == list(TEMPLATE["grid_meter"].keys())


def test_extra_inverters_reuse_template_defaults():
    result = ConfigPreviewGenerator(_ReleaseManager()).generate(
        [_device(1), _device(2), _device(3), _meter()]
    )
    assert result["config"]["devices"][2]["max_power"] == 800
    assert result["config"]["devices"][2]["name"] == "inverter_3"


def test_grid_meter_type_comes_from_discovery_metadata():
    result = ConfigPreviewGenerator(_ReleaseManager()).generate(
        [_device(), _meter(api_family="shelly_3em_gen1")], 1
    )
    assert result["config"]["grid_meter"]["type"] == "shelly_3em_gen1"


def test_validation_reports_ambiguous_meter_and_bad_device_values():
    result = ConfigPreviewGenerator(_ReleaseManager()).generate(
        [
            _device(1, display_name="", ip="not valid", serial_number=""),
            _device(2, config_name="inverter_1"),
        ],
        supported_grid_meter_count=2,
    )
    codes = {issue["code"] for issue in result["validation"]["errors"]}
    assert result["ready"] is False
    assert {
        "display_name_empty",
        "device_host_invalid",
        "device_serial_missing",
        "config_name_duplicate",
        "grid_meter_ambiguous",
    } <= codes
    assert "grid_meter" not in result["config"]


def test_validation_includes_grid_meter_name_in_uniqueness_check():
    result = ConfigPreviewGenerator(_ReleaseManager()).generate(
        [_device(config_name="grid_meter"), _meter()], 1
    )
    codes = {issue["code"] for issue in result["validation"]["errors"]}
    assert "config_name_duplicate" in codes


def test_ipv6_is_not_accepted_for_ipv4_only_ems_hosts():
    result = ConfigPreviewGenerator(_ReleaseManager()).generate(
        [_device(ip="2001:db8::1"), _meter()], 1
    )
    codes = {issue["code"] for issue in result["validation"]["errors"]}
    assert "device_host_invalid" in codes


def test_missing_prepared_release_returns_preview_validation():
    class _Missing:
        def config_template(self):
            raise ReleaseError("No release resources prepared yet.", 404)

    result = ConfigPreviewGenerator(_Missing()).generate([])
    assert result["ready"] is False
    assert result["template_loaded"] is False
    assert result["config"] is None
    assert result["validation"]["errors"][0]["code"] == "release_resources_not_prepared"


EXISTING_CONFIG = {
    "system": {"max_total_power": 2000, "operator_note": "hand-tuned"},
    "devices": [
        {"name": "WR1", "ip": "10.0.0.1", "sn": "REAL1", "max_power": 800, "custom": "keep"},
        {"name": "WR2", "ip": "10.0.0.2", "sn": "REAL2", "max_power": 600},
    ],
    "grid_meter": {"type": "shelly", "ip": "10.0.0.9", "operator_field": 1},
    "operator_only": {"never": "touch"},
}


def _context(config_path, config_exists=True):
    return AdminInstallContext(
        config_path=config_path,
        config_exists=config_exists,
        config_source="canonical",
        template_path="/app/config.template.json",
        template_exists=True,
        template_source="legacy",
        data_dir="/data",
        data_dir_exists=True,
        compose_path="/app/docker-compose.yml",
        compose_exists=True,
    )


def _existing_generator(config_path, config_exists=True, template=TEMPLATE):
    return ConfigPreviewGenerator(
        _ReleaseManager(template),
        install_context_provider=lambda: _context(config_path, config_exists),
    )


def _write_config(tmp_path, data):
    path = tmp_path / "config.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def test_existing_config_is_used_as_base_and_metadata_reports_source(tmp_path):
    path = _write_config(tmp_path, EXISTING_CONFIG)
    result = _existing_generator(path).generate([_device(1, config_name="WR1")])

    assert result["ready"] is True
    assert result["base"]["source"] == "existing_config"
    assert result["base"]["config_path"] == str(path)
    assert result["base"]["config_source"] == "canonical"
    assert result["base"]["template_source"] == "legacy"
    codes = {issue["code"] for issue in result["validation"]["info"]}
    assert "existing_config_base" in codes


def test_existing_config_preserves_unrelated_keys_and_untouched_devices(tmp_path):
    path = _write_config(tmp_path, EXISTING_CONFIG)
    result = _existing_generator(path).generate(
        [_device(1, config_name="WR1", ip="10.0.0.5", serial_number="NEW1")]
    )
    config = result["config"]

    assert config["operator_only"] == {"never": "touch"}
    assert config["system"]["operator_note"] == "hand-tuned"
    wr1 = next(d for d in config["devices"] if d["name"] == "WR1")
    assert wr1["ip"] == "10.0.0.5"
    assert wr1["sn"] == "NEW1"
    assert wr1["custom"] == "keep"
    assert wr1["max_power"] == 800
    # WR2 is not in the draft and must remain untouched.
    wr2 = next(d for d in config["devices"] if d["name"] == "WR2")
    assert wr2 == EXISTING_CONFIG["devices"][1]


def test_existing_config_appends_new_draft_device_from_template_prototype(tmp_path):
    path = _write_config(tmp_path, EXISTING_CONFIG)
    result = _existing_generator(path).generate(
        [_device(3, config_name="WR3", ip="10.0.0.3", serial_number="NEW3")]
    )
    names = [d["name"] for d in result["config"]["devices"]]

    assert names == ["WR1", "WR2", "WR3"]
    wr3 = result["config"]["devices"][2]
    assert wr3["ip"] == "10.0.0.3"
    assert wr3["sn"] == "NEW3"
    assert wr3["max_power"] == 800  # from template prototype shape


def test_existing_grid_meter_preserved_when_no_draft_meter(tmp_path):
    path = _write_config(tmp_path, EXISTING_CONFIG)
    result = _existing_generator(path).generate([_device(1, config_name="WR1")])

    assert result["config"]["grid_meter"] == EXISTING_CONFIG["grid_meter"]
    assert result["ready"] is True


def test_draft_grid_meter_updates_existing_grid_meter(tmp_path):
    path = _write_config(tmp_path, EXISTING_CONFIG)
    result = _existing_generator(path).generate(
        [_device(1, config_name="WR1"), _meter(ip="10.0.0.20")], 1
    )
    grid = result["config"]["grid_meter"]

    assert grid["ip"] == "10.0.0.20"
    assert grid["operator_field"] == 1  # existing custom field preserved


def test_existing_config_serial_preserved_when_draft_serial_empty(tmp_path):
    path = _write_config(tmp_path, EXISTING_CONFIG)
    result = _existing_generator(path).generate(
        [_device(1, config_name="WR1", serial_number="")]
    )
    wr1 = next(d for d in result["config"]["devices"] if d["name"] == "WR1")

    assert wr1["sn"] == "REAL1"
    codes = {issue["code"] for issue in result["validation"]["errors"]}
    assert "device_serial_missing" not in codes


def test_existing_config_invalid_json_is_reported(tmp_path):
    path = tmp_path / "config.json"
    path.write_text("{not json", encoding="utf-8")
    result = _existing_generator(path).generate([_device(1)])

    assert result["ready"] is False
    assert result["config"] is None
    assert result["base"]["source"] == "existing_config"
    codes = {issue["code"] for issue in result["validation"]["errors"]}
    assert "existing_config_invalid_json" in codes


def test_existing_config_not_object_is_reported(tmp_path):
    path = tmp_path / "config.json"
    path.write_text("[1, 2, 3]", encoding="utf-8")
    result = _existing_generator(path).generate([_device(1)])

    codes = {issue["code"] for issue in result["validation"]["errors"]}
    assert "existing_config_not_object" in codes
    assert result["config"] is None


def test_existing_config_unreadable_is_reported(tmp_path):
    missing = tmp_path / "does-not-exist" / "config.json"
    result = _existing_generator(missing).generate([_device(1)])

    codes = {issue["code"] for issue in result["validation"]["errors"]}
    assert "existing_config_unreadable" in codes
    assert result["config"] is None


def test_fresh_setup_uses_release_template_base_when_no_config(tmp_path):
    path = tmp_path / "config.json"
    result = _existing_generator(path, config_exists=False).generate(
        [_device(1), _meter()], 1
    )

    assert result["base"] == {"source": "release_template"}
    assert result["ready"] is True
    assert [d["name"] for d in result["config"]["devices"]] == ["inverter_1"]
