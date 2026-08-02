# SPDX-License-Identifier: AGPL-3.0-or-later
"""Config preview generation tests."""

import copy
import json

import pytest

from admin.config_preview import ConfigPreviewGenerator
from admin.install_context import AdminInstallContext
from admin.releases import ReleaseError

pytestmark = pytest.mark.simulation


@pytest.fixture(autouse=True)
def _isolate_install_root(isolated_install_root):
    """Keep these tests off the developer's real repo-local config/data."""

    return isolated_install_root


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


def test_device_config_values_apply_to_generated_device():
    result = ConfigPreviewGenerator(_ReleaseManager()).generate(
        [
            _device(
                1,
                config_values={"max_power": "600", "min_soc": "20", "max_soc": "95"},
            ),
            _meter(),
        ],
        1,
    )
    device = result["config"]["devices"][0]

    assert device["max_power"] == 600
    assert device["min_soc"] == 20
    assert device["max_soc"] == 95


def test_device_config_values_ignore_unknown_and_identity_keys():
    result = ConfigPreviewGenerator(_ReleaseManager()).generate(
        [
            _device(
                1,
                config_values={
                    "name": "hijack",
                    "sn": "hijack",
                    "bogus": 1,
                    "system.max_total_power": 5,
                },
            ),
            _meter(),
        ],
        1,
    )
    device = result["config"]["devices"][0]

    # Identity fields come from the mapped draft properties, never config_values.
    assert device["name"] == "inverter_1"
    assert device["sn"] == "SN1"
    assert "bogus" not in device
    # Device values can never write outside their own device object.
    assert result["config"]["system"]["max_total_power"] == 1600


def test_device_config_values_merge_into_existing_device(tmp_path):
    path = _write_config(tmp_path, EXISTING_CONFIG)
    result = _existing_generator(path).generate(
        [
            _device(
                1,
                config_name="WR1",
                config_values={"max_power": "500"},
            )
        ]
    )
    wr1 = next(d for d in result["config"]["devices"] if d["name"] == "WR1")

    assert wr1["max_power"] == 500
    # Unrelated existing device keys are preserved through the merge.
    assert wr1["custom"] == "keep"


def test_grid_meter_type_comes_from_discovery_metadata():
    result = ConfigPreviewGenerator(_ReleaseManager()).generate(
        [_device(), _meter(api_family="shelly_3em_gen1")], 1
    )
    assert result["config"]["grid_meter"]["type"] == "shelly_3em_gen1"


def test_manual_grid_meter_type_is_used_over_inference():
    # A manually added meter has no discovery metadata, so the explicitly chosen
    # type must drive the generated grid_meter.type.
    meter = _meter(
        api_family="",
        device_type="",
        grid_meter_type="shelly_3em_gen1",
    )
    result = ConfigPreviewGenerator(_ReleaseManager()).generate([_device(), meter], 1)
    assert result["config"]["grid_meter"]["type"] == "shelly_3em_gen1"


def test_manual_local_api_connection_uses_current_device_config_shape():
    result = ConfigPreviewGenerator(_ReleaseManager()).generate(
        [_device(connection_type="zendure_local_api"), _meter()], 1
    )
    device = result["config"]["devices"][0]

    assert result["ready"] is True
    assert device["ip"] == "192.168.1.1"
    assert device["sn"] == "SN1"
    assert "connection_type" not in device


def test_manual_grid_meter_without_type_falls_back_to_shelly():
    meter = _meter(api_family="", device_type="", grid_meter_type="")
    result = ConfigPreviewGenerator(_ReleaseManager()).generate([_device(), meter], 1)
    assert result["config"]["grid_meter"]["type"] == "shelly"


def test_explicit_grid_meter_type_wins_over_conflicting_discovery():
    meter = _meter(api_family="shelly_gen2", grid_meter_type="ecotracker")
    result = ConfigPreviewGenerator(_ReleaseManager()).generate([_device(), meter], 1)
    assert result["config"]["grid_meter"]["type"] == "ecotracker"


def test_grid_meter_type_from_zendure_http_discovery_metadata():
    # A discovered Zendure HTTP grid meter (D0 or 3CT) normalizes to the generic
    # local-HTTP type and is config-ready on its IP alone.
    meter = _meter(
        api_family="zendure_grid_meter_http",
        device_type="zendure_smartmeter_3ct",
        display_name="Zendure Grid Meter via local HTTP (Smart Meter 3CT)",
        ip="192.0.2.80",
    )
    result = ConfigPreviewGenerator(_ReleaseManager()).generate([_device(), meter], 1)
    grid = result["config"]["grid_meter"]
    assert grid["type"] == "zendure_grid_meter_http"
    assert grid["ip"] == "192.0.2.80"


def test_grid_meter_preserves_discovered_http_port():
    meter = _meter(
        api_family="zendure_grid_meter_http",
        device_type="zendure_grid_meter_http",
        display_name="Zendure Grid Meter via local HTTP",
        ip="192.0.2.80",
        port=8080,
    )
    result = ConfigPreviewGenerator(_ReleaseManager()).generate([_device(), meter], 1)
    grid = result["config"]["grid_meter"]
    assert grid["type"] == "zendure_grid_meter_http"
    assert grid["port"] == 8080


def test_manual_zendure_3ct_grid_meter_type_is_accepted():
    meter = _meter(
        api_family="",
        device_type="",
        grid_meter_type="zendure_smartmeter_3ct_http",
        ip="192.0.2.81",
    )
    result = ConfigPreviewGenerator(_ReleaseManager()).generate([_device(), meter], 1)
    grid = result["config"]["grid_meter"]
    assert grid["type"] == "zendure_smartmeter_3ct_http"
    assert grid["ip"] == "192.0.2.81"


def test_manual_zendure_d0_http_grid_meter_type_is_accepted():
    # A manually added D0 local-API meter produces the exact IP-only HTTP shape
    # and is never rewritten to the generic type or the 3CT type.
    meter = _meter(
        api_family="",
        device_type="",
        grid_meter_type="zendure_smartmeter_d0_http",
        ip="192.168.1.60",
    )
    result = ConfigPreviewGenerator(_ReleaseManager()).generate([_device(), meter], 1)
    grid = result["config"]["grid_meter"]
    assert grid["type"] == "zendure_smartmeter_d0_http"
    assert grid["ip"] == "192.168.1.60"
    # A D0 local-API meter carries no MQTT block and no serial-derived topic.
    assert "mqtt" not in grid


def test_d0_http_preview_has_no_stale_mqtt_or_tasmota_fields():
    # Switching a Tasmota/MQTT-shaped draft to the D0 local API drops every field
    # that does not belong to a flat HTTP meter.
    meter = _meter(
        api_family="",
        device_type="",
        grid_meter_type="zendure_smartmeter_d0_http",
        ip="192.168.1.60",
        url="http://192.168.1.50/cm?cmnd=Status%2010",
        power_path="StatusSNS.SML.Power_curr",
    )
    result = ConfigPreviewGenerator(_ReleaseManager()).generate([_device(), meter], 1)
    grid = result["config"]["grid_meter"]
    assert grid["type"] == "zendure_smartmeter_d0_http"
    assert set(grid) <= {"type", "ip", "port"}
    assert "mqtt" not in grid
    assert "url" not in grid
    assert "power_path" not in grid


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
        config_layout_state="standard_only",
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


def test_default_install_context_is_isolated_from_local_repo_config():
    # Regression: with the isolation fixture active, a preview built with the
    # default install-context provider must fall back to the release template
    # instead of a developer's real gitignored repo-local config/config.json.
    # Without isolation this resolves against paths.BASE_DIR and, in a used
    # developer checkout, silently adopts the local config as the base.
    result = ConfigPreviewGenerator(_ReleaseManager()).generate(
        [_device(1), _meter()], 1
    )

    assert result["base"] == {"source": "release_template"}


# --- catalog-driven feature settings integration -------------------------

FEATURE_TEMPLATE = {
    "system": {"max_total_power": 1600, "dry_run": False},
    "devices": [{"name": "WR1", "ip": "192.0.2.1", "sn": "YOUR_SN", "max_power": 800}],
    "grid_meter": {"type": "shelly", "ip": "192.0.2.3"},
    "winter": {"enabled": False, "summer_min_soc": 15, "winter_min_soc": 40},
    "dashboard": {"enabled": True, "port": 8080},
    "influxdb": {"enabled": False, "mode": "bundled"},
}


def _feature_generator():
    return ConfigPreviewGenerator(_ReleaseManager(copy.deepcopy(FEATURE_TEMPLATE)))


def test_winter_feature_values_flow_into_preview():
    result = _feature_generator().generate(
        [_device(1), _meter()],
        1,
        features={
            "winter.enabled": True,
            "winter.summer_min_soc": 20,
            "winter.winter_min_soc": 55,
        },
    )

    winter = result["config"]["winter"]
    assert winter["enabled"] is True
    assert winter["summer_min_soc"] == 20
    assert winter["winter_min_soc"] == 55
    codes = {issue["code"] for issue in result["validation"]["info"]}
    assert "setup_features_applied" in codes


def test_dashboard_port_feature_changes_preview():
    result = _feature_generator().generate(
        [_device(1), _meter()], 1, features={"dashboard.port": "9099"}
    )

    assert result["config"]["dashboard"]["port"] == 9099


def test_influxdb_enable_and_mode_feature_changes_preview():
    result = _feature_generator().generate(
        [_device(1), _meter()],
        1,
        features={"influxdb.enabled": True, "influxdb.mode": "external"},
    )

    influx = result["config"]["influxdb"]
    assert influx["enabled"] is True
    assert influx["mode"] == "external"


INFLUX_TEMPLATE = {
    "system": {"max_total_power": 1600, "dry_run": False},
    "devices": [{"name": "WR1", "ip": "192.0.2.1", "sn": "YOUR_SN", "max_power": 800}],
    "grid_meter": {"type": "shelly", "ip": "192.0.2.3"},
    "influxdb": {
        "enabled": False,
        "mode": "bundled",
        "auto_init": True,
        "auto_sync": True,
        "secret_file": "deploy/docker/influxdb.env",
    },
}


def _influx_generator():
    return ConfigPreviewGenerator(_ReleaseManager(copy.deepcopy(INFLUX_TEMPLATE)))


def test_bundled_influx_normalizes_secret_file_to_config_volume():
    # Docker-first Admin deployments only mount config/ and data/ as writable, so
    # bundled secrets must live in config/, never the read-only deploy/docker tree.
    result = _influx_generator().generate(
        [_device(1), _meter()],
        1,
        features={"influxdb.enabled": True, "influxdb.mode": "bundled"},
    )

    influx = result["config"]["influxdb"]
    assert influx["secret_file"] == "config/influxdb.env"
    assert influx["enabled"] is True
    assert influx["mode"] == "bundled"
    assert influx["auto_init"] is True
    assert influx["auto_sync"] is True


def test_external_influx_secret_file_is_not_normalized():
    result = _influx_generator().generate(
        [_device(1), _meter()],
        1,
        features={"influxdb.enabled": True, "influxdb.mode": "external"},
    )

    influx = result["config"]["influxdb"]
    assert influx["mode"] == "external"
    assert influx["secret_file"] == "deploy/docker/influxdb.env"


def test_disabled_bundled_influx_secret_file_is_not_normalized():
    result = _influx_generator().generate([_device(1), _meter()], 1)

    influx = result["config"]["influxdb"]
    assert influx["enabled"] is False
    assert influx["secret_file"] == "deploy/docker/influxdb.env"


def test_grid_meter_variant_feature_overrides_draft_type():
    result = _feature_generator().generate(
        [_device(1), _meter()],
        1,
        features={
            "grid_meter.type": "mqtt",
            "grid_meter.mqtt.host": "192.0.2.50",
            "grid_meter.mqtt.port": "1883",
        },
    )

    grid = result["config"]["grid_meter"]
    assert grid["type"] == "mqtt"
    assert grid["mqtt"]["host"] == "192.0.2.50"
    assert grid["mqtt"]["port"] == 1883


def test_features_are_optional_and_do_not_change_preview_by_default():
    baseline = _feature_generator().generate([_device(1), _meter()], 1)
    with_empty = _feature_generator().generate([_device(1), _meter()], 1, features={})

    assert baseline["config"] == with_empty["config"]
    assert with_empty["config"]["winter"]["enabled"] is False


def test_device_setup_still_works_alongside_features():
    result = _feature_generator().generate(
        [_device(1, config_name="WR1", ip="192.168.9.9", serial_number="SN9"), _meter()],
        1,
        features={"winter.enabled": True},
    )

    assert result["ready"] is True
    device = result["config"]["devices"][0]
    assert device["name"] == "WR1"
    assert device["ip"] == "192.168.9.9"
    assert device["sn"] == "SN9"


def _mqtt_proposal(**fragment_overrides):
    fragment = {
        "type": "zendure_mqtt",
        "enabled": True,
        "name": "Zendure MQTT SolarFlow 800",
        "mqtt": {
            "topic_family": "zensdk_ha_scalar",
            "base_topic": None,
            "device_id": "ABC123",
            "product_key": "PK1",
            "app_key": None,
        },
        "capabilities": {
            "read_power": True,
            "read_soc": True,
            "write_output_limit": False,
        },
    }
    fragment.update(fragment_overrides)
    return {"id": "zendure-mqtt:ABC123", "config_fragment": fragment}


def test_zendure_mqtt_proposal_is_added_to_config_devices():
    result = ConfigPreviewGenerator(_ReleaseManager()).generate(
        [_device(1), _meter()], 1, zendure_mqtt_proposals=[_mqtt_proposal()]
    )

    assert result["ready"] is True
    devices = result["config"]["devices"]
    mqtt_entries = [d for d in devices if d.get("type") == "zendure_mqtt"]
    assert len(mqtt_entries) == 1
    entry = mqtt_entries[0]
    assert "preview_only" not in entry
    assert entry["mqtt"]["device_id"] == "ABC123"
    assert entry["capabilities"]["write_output_limit"] is False
    assert result["summary"]["zendure_mqtt_devices"] == 1


def test_zendure_mqtt_proposal_adds_telemetry_only_warning():
    result = ConfigPreviewGenerator(_ReleaseManager()).generate(
        [_device(1), _meter()], 1, zendure_mqtt_proposals=[_mqtt_proposal()]
    )

    codes = [issue["code"] for issue in result["validation"]["warnings"]]
    assert "zendure_mqtt_telemetry_only" in codes


def _control_mqtt_proposal():
    return _mqtt_proposal(
        name="Zendure MQTT Hyper",
        hardware_profile="hyper_2000",
        power_write_profile="legacy_object_device_automation",
        mqtt={
            "source": "local_mqtt",
            "topic_family": "legacy_zendure_json",
            "base_topic": "iot",
            "device_id": "CTL123",
            "product_key": "PKCTL",
            "app_key": None,
        },
        capabilities={
            "read_power": True,
            "read_soc": True,
            "write_output_limit": True,
        },
    )


def test_zendure_mqtt_control_proposal_warning_reflects_enabled_control():
    # A selected control proposal becomes a control device; the preview must say
    # so instead of claiming output write/control is disabled for it.
    result = ConfigPreviewGenerator(_ReleaseManager()).generate(
        [_device(1), _meter()], 1, zendure_mqtt_proposals=[_control_mqtt_proposal()]
    )

    entry = [
        d for d in result["config"]["devices"] if d.get("type") == "zendure_mqtt"
    ][0]
    assert entry["capabilities"]["write_output_limit"] is True
    warnings = result["validation"]["warnings"]
    codes = [issue["code"] for issue in warnings]
    assert "zendure_mqtt_control_enabled" in codes
    assert "zendure_mqtt_telemetry_only" not in codes
    blob = json.dumps(warnings).lower()
    assert "write/control is disabled" not in blob


def test_zendure_mqtt_proposal_failing_ems_validation_blocks_export():
    proposal = _mqtt_proposal()
    proposal["config_fragment"]["mqtt"].pop("device_id")
    proposal["config_fragment"].pop("name")

    result = ConfigPreviewGenerator(_ReleaseManager()).generate(
        [_device(1), _meter()], 1, zendure_mqtt_proposals=[proposal]
    )

    assert result["ready"] is False
    codes = [issue["code"] for issue in result["validation"]["errors"]]
    assert "zendure_mqtt_invalid" in codes
    mqtt_entries = [
        d for d in result["config"]["devices"] if d.get("type") == "zendure_mqtt"
    ]
    assert mqtt_entries == []


def test_zendure_mqtt_proposal_strips_secrets_and_forces_no_write():
    proposal = _mqtt_proposal()
    proposal["config_fragment"]["mqtt"]["app_key"] = "cloud-app-key"
    proposal["config_fragment"]["mqtt"]["token"] = "sekret-token"
    proposal["config_fragment"]["password"] = "hunter2"
    proposal["config_fragment"]["username"] = "admin"
    proposal["config_fragment"]["capabilities"]["write_output_limit"] = True

    result = ConfigPreviewGenerator(_ReleaseManager()).generate(
        [_device(1), _meter()], 1, zendure_mqtt_proposals=[proposal]
    )

    blob = json.dumps(result["config"]).lower()
    for secret in ("cloud-app-key", "sekret-token", "hunter2", "app_key", "token", "password"):
        assert secret not in blob
    entry = [d for d in result["config"]["devices"] if d.get("type") == "zendure_mqtt"][0]
    assert entry["capabilities"]["write_output_limit"] is False


def test_zendure_mqtt_proposals_skip_non_dicts_and_bad_fragments():
    proposals = [
        "junk",
        {"id": "x"},  # no config_fragment
        {"id": "y", "config_fragment": "not-a-dict"},
        _mqtt_proposal(),  # one good one survives
    ]
    result = ConfigPreviewGenerator(_ReleaseManager()).generate(
        [_device(1), _meter()], 1, zendure_mqtt_proposals=proposals
    )
    assert result["summary"]["zendure_mqtt_devices"] == 1


def test_existing_sn_and_mqtt_proposal_same_serial_blocks_preview(tmp_path):
    config = copy.deepcopy(EXISTING_CONFIG)
    path = _write_config(tmp_path, config)
    proposal = _mqtt_proposal()
    proposal["config_fragment"]["serial_number"] = "real1"

    result = _existing_generator(path).generate(
        [], zendure_mqtt_proposals=[proposal]
    )

    assert result["ready"] is False
    codes = [issue["code"] for issue in result["validation"]["errors"]]
    assert "zendure_device_identity_duplicate" in codes


def test_two_mqtt_proposals_same_device_id_block_preview():
    first = _mqtt_proposal()
    second = _mqtt_proposal(name="Second SolarFlow")

    result = ConfigPreviewGenerator(_ReleaseManager()).generate(
        [_device(1), _meter()], 1, zendure_mqtt_proposals=[first, second]
    )

    assert result["ready"] is False
    codes = [issue["code"] for issue in result["validation"]["errors"]]
    assert "zendure_device_identity_duplicate" in codes


def test_two_mqtt_proposals_unique_device_id_are_accepted():
    first = _mqtt_proposal()
    second = _mqtt_proposal(name="Second SolarFlow")
    second["config_fragment"]["mqtt"] = dict(second["config_fragment"]["mqtt"])
    second["config_fragment"]["mqtt"]["device_id"] = "XYZ789"

    result = ConfigPreviewGenerator(_ReleaseManager()).generate(
        [_device(1), _meter()], 1, zendure_mqtt_proposals=[first, second]
    )

    assert result["ready"] is True
    codes = [issue["code"] for issue in result["validation"]["errors"]]
    assert "zendure_device_identity_duplicate" not in codes


def test_masked_product_key_is_not_written_to_config():
    proposal = _mqtt_proposal()
    proposal["config_fragment"]["mqtt"]["product_key"] = "…abcd"

    result = ConfigPreviewGenerator(_ReleaseManager()).generate(
        [_device(1), _meter()], 1, zendure_mqtt_proposals=[proposal]
    )

    entry = [d for d in result["config"]["devices"] if d.get("type") == "zendure_mqtt"][0]
    assert "product_key" not in entry["mqtt"]
    assert "•" not in json.dumps(result["config"])
    assert "…" not in json.dumps(result["config"])


def test_masked_device_id_is_dropped_but_serial_keeps_entry_valid():
    proposal = _mqtt_proposal()
    proposal["config_fragment"]["mqtt"]["device_id"] = "••••"
    proposal["config_fragment"]["serial_number"] = "SNCLEAN"

    result = ConfigPreviewGenerator(_ReleaseManager()).generate(
        [_device(1), _meter()], 1, zendure_mqtt_proposals=[proposal]
    )

    assert result["ready"] is True
    entry = [d for d in result["config"]["devices"] if d.get("type") == "zendure_mqtt"][0]
    assert "device_id" not in entry["mqtt"]
    assert entry["serial_number"] == "SNCLEAN"


def test_preview_without_proposals_is_unchanged():
    baseline = ConfigPreviewGenerator(_ReleaseManager()).generate([_device(1), _meter()], 1)
    with_none = ConfigPreviewGenerator(_ReleaseManager()).generate(
        [_device(1), _meter()], 1, zendure_mqtt_proposals=None
    )
    assert baseline["config"] == with_none["config"]
    assert with_none["summary"]["zendure_mqtt_devices"] == 0


def _broker_proposal(broker_ref, **overrides):
    proposal = _mqtt_proposal(**overrides)
    proposal["config_fragment"]["mqtt"]["broker_ref"] = broker_ref
    proposal["config_fragment"]["mqtt"]["source"] = (
        "zendure_cloud_mqtt" if broker_ref == "zendure_cloud" else "local_mqtt"
    )
    return proposal


def test_unknown_broker_ref_blocks_preview():
    proposal = _broker_proposal("ghost", name="Ghost broker device")
    result = ConfigPreviewGenerator(_ReleaseManager()).generate(
        [_device(1), _meter()], 1, zendure_mqtt_proposals=[proposal]
    )
    assert result["ready"] is False
    codes = [issue["code"] for issue in result["validation"]["errors"]]
    assert "zendure_mqtt_broker_unresolved" in codes
    mqtt_entries = [
        d for d in result["config"]["devices"] if d.get("type") == "zendure_mqtt"
    ]
    assert mqtt_entries == []


def _cloud_ready_generator():
    # A cloud proposal is only usable when the external Zendure account auth
    # exists; inject that condition so provisioning is exercised deterministically.
    return ConfigPreviewGenerator(
        _ReleaseManager(), zendure_cloud_auth_available=lambda: True
    )


def test_selected_cloud_proposal_provisions_secret_free_broker_profile():
    proposal = _broker_proposal("zendure_cloud", name="Cloud SolarFlow")
    result = _cloud_ready_generator().generate(
        [_device(1), _meter()], 1, zendure_mqtt_proposals=[proposal]
    )
    assert result["ready"] is True
    brokers = result["config"]["zendure_mqtt"]["brokers"]
    assert "zendure_cloud" in brokers
    profile = brokers["zendure_cloud"]
    assert profile["source"] == "zendure_cloud_mqtt"
    assert profile["enabled"] is True
    assert "host" in profile
    # No broker credential is ever written into the provisioned profile.
    blob = json.dumps(profile).lower()
    for secret in ("password", "username", "app_key", "token"):
        assert secret not in blob


def test_cloud_proposal_blocks_when_account_auth_is_unavailable():
    proposal = _broker_proposal("zendure_cloud", name="Cloud SolarFlow")
    result = ConfigPreviewGenerator(
        _ReleaseManager(), zendure_cloud_auth_available=lambda: False
    ).generate([_device(1), _meter()], 1, zendure_mqtt_proposals=[proposal])
    assert result["ready"] is False
    codes = [issue["code"] for issue in result["validation"]["errors"]]
    assert "zendure_mqtt_broker_auth_missing" in codes
    mqtt_entries = [
        d for d in result["config"]["devices"] if d.get("type") == "zendure_mqtt"
    ]
    assert mqtt_entries == []


def test_local_proposal_blocks_when_broker_host_is_unavailable():
    proposal = _broker_proposal("local_mqtt", name="Local SolarFlow")
    result = ConfigPreviewGenerator(_ReleaseManager()).generate(
        [_device(1), _meter()], 1, zendure_mqtt_proposals=[proposal]
    )
    assert result["ready"] is False
    codes = [issue["code"] for issue in result["validation"]["errors"]]
    assert "zendure_mqtt_broker_incomplete" in codes
    mqtt_entries = [
        d for d in result["config"]["devices"] if d.get("type") == "zendure_mqtt"
    ]
    assert mqtt_entries == []


def test_local_proposal_with_known_host_provisions_enabled_broker():
    proposal = _broker_proposal("local_mqtt", name="Local SolarFlow")
    proposal["broker_host"] = "10.0.0.5"
    proposal["broker_port"] = 1883
    result = ConfigPreviewGenerator(_ReleaseManager()).generate(
        [_device(1), _meter()], 1, zendure_mqtt_proposals=[proposal]
    )
    assert result["ready"] is True
    profile = result["config"]["zendure_mqtt"]["brokers"]["local_mqtt"]
    assert profile["enabled"] is True
    assert profile["host"] == "10.0.0.5"
    assert profile["port"] == 1883
    # The feature is always on; the removed top-level toggle is never written.
    assert "enabled" not in result["config"]["zendure_mqtt"]


# --- manual Zendure MQTT broker + generation-profile devices -------------

_LOCAL_BROKER = {"name": "local_mqtt", "host": "192.168.1.20", "port": 1883}


def _manual_mqtt(**overrides):
    device = {
        "name": "SolarFlow 800 Pro 2",
        "serial_number": "DEVSN1",
        "generation": "solarflow_zensdk",
    }
    device.update(overrides)
    return device


def _manual_entry(result):
    return [d for d in result["config"]["devices"] if d.get("type") == "zendure_mqtt"][0]


def test_zensdk_generation_maps_to_internal_topic_family():
    result = ConfigPreviewGenerator(_ReleaseManager()).generate(
        [_device(1), _meter()],
        1,
        zendure_mqtt_broker=dict(_LOCAL_BROKER),
        zendure_mqtt_manual_devices=[_manual_mqtt()],
    )

    assert result["ready"] is True
    entry = _manual_entry(result)
    assert entry["mqtt"]["topic_family"] == "zensdk_ha_scalar"
    assert entry["mqtt"]["base_topic"] == "Zendure"
    assert entry["serial_number"] == "DEVSN1"


def test_hub_hyper_generation_maps_to_legacy_topic_family_with_product_key():
    result = ConfigPreviewGenerator(_ReleaseManager()).generate(
        [_device(1), _meter()],
        1,
        zendure_mqtt_broker=dict(_LOCAL_BROKER),
        zendure_mqtt_manual_devices=[
            _manual_mqtt(generation="hub_hyper_legacy", product_key="PK9")
        ],
    )

    assert result["ready"] is True
    entry = _manual_entry(result)
    assert entry["mqtt"]["topic_family"] == "legacy_zendure_json"
    assert entry["mqtt"]["base_topic"] == "iot"
    assert entry["mqtt"]["product_key"] == "PK9"


def test_cloud_generation_maps_to_cloud_topic_family():
    result = ConfigPreviewGenerator(_ReleaseManager()).generate(
        [_device(1), _meter()],
        1,
        zendure_mqtt_broker=dict(_LOCAL_BROKER),
        zendure_mqtt_manual_devices=[_manual_mqtt(generation="zendure_cloud")],
    )

    assert result["ready"] is True
    entry = _manual_entry(result)
    assert entry["mqtt"]["topic_family"] == "zendure_cloud_scalar"
    # The cloud generation exposes no product key field, so none is written.
    assert "product_key" not in entry["mqtt"]


def test_manual_mqtt_device_is_always_telemetry_only():
    result = ConfigPreviewGenerator(_ReleaseManager()).generate(
        [_device(1), _meter()],
        1,
        zendure_mqtt_broker=dict(_LOCAL_BROKER),
        zendure_mqtt_manual_devices=[_manual_mqtt()],
    )

    entry = _manual_entry(result)
    assert entry["capabilities"]["write_output_limit"] is False


def test_manual_mqtt_missing_identifier_blocks_preview_with_friendly_error():
    result = ConfigPreviewGenerator(_ReleaseManager()).generate(
        [_device(1), _meter()],
        1,
        zendure_mqtt_broker=dict(_LOCAL_BROKER),
        zendure_mqtt_manual_devices=[_manual_mqtt(serial_number="")],
    )

    assert result["ready"] is False
    codes = {issue["code"] for issue in result["validation"]["errors"]}
    assert "zendure_mqtt_device_identifier_missing" in codes
    mqtt_entries = [
        d for d in result["config"]["devices"] if d.get("type") == "zendure_mqtt"
    ]
    assert mqtt_entries == []


def test_manual_mqtt_missing_broker_host_blocks_with_friendly_error():
    result = ConfigPreviewGenerator(_ReleaseManager()).generate(
        [_device(1), _meter()],
        1,
        zendure_mqtt_broker={"name": "local_mqtt", "host": ""},
        zendure_mqtt_manual_devices=[_manual_mqtt()],
    )

    assert result["ready"] is False
    codes = {issue["code"] for issue in result["validation"]["errors"]}
    assert "zendure_mqtt_broker_host_missing" in codes


def test_manual_mqtt_broker_profile_is_provisioned_and_secret_free_in_devices():
    result = ConfigPreviewGenerator(_ReleaseManager()).generate(
        [_device(1), _meter()],
        1,
        zendure_mqtt_broker=dict(_LOCAL_BROKER, security="tls", username="u", password="p"),
        zendure_mqtt_manual_devices=[_manual_mqtt()],
    )

    assert result["ready"] is True
    profile = result["config"]["zendure_mqtt"]["brokers"]["local_mqtt"]
    assert profile["host"] == "192.168.1.20"
    assert profile["tls"] is True
    # The device entry never carries broker credentials.
    entry = _manual_entry(result)
    assert "password" not in json.dumps(entry).lower()


def test_discovery_proposal_still_accepts_legacy_alt_family_internally():
    # The advanced legacy_zendure_json_alt family is never a manual choice, but a
    # discovered proposal that already carries it must keep working unchanged.
    proposal = _mqtt_proposal()
    proposal["config_fragment"]["mqtt"]["topic_family"] = "legacy_zendure_json_alt"

    result = ConfigPreviewGenerator(_ReleaseManager()).generate(
        [_device(1), _meter()], 1, zendure_mqtt_proposals=[proposal]
    )

    assert result["ready"] is True
    entry = _manual_entry(result)
    assert entry["mqtt"]["topic_family"] == "legacy_zendure_json_alt"


def test_two_devices_with_two_broker_refs_are_accepted():
    first = _broker_proposal("local_mqtt", name="Local SolarFlow")
    first["config_fragment"]["mqtt"]["device_id"] = "LOCALDEV"
    first["broker_host"] = "10.0.0.5"
    first["broker_port"] = 1883
    second = _broker_proposal("zendure_cloud", name="Cloud SolarFlow")
    second["config_fragment"]["mqtt"]["device_id"] = "CLOUDDEV"
    result = _cloud_ready_generator().generate(
        [_device(1), _meter()], 1, zendure_mqtt_proposals=[first, second]
    )
    assert result["ready"] is True
    assert result["summary"]["zendure_mqtt_devices"] == 2
    brokers = result["config"]["zendure_mqtt"]["brokers"]
    assert {"local_mqtt", "zendure_cloud"} <= set(brokers)
    # Generated config never carries a broker credential.
    blob = json.dumps(result["config"]).lower()
    for secret in ("password", "username", "app_key", "api_key", "token", "secret"):
        assert secret not in blob


# --- shared Zendure MQTT hardware profile mapping -----------------------


def test_manual_generation_maps_to_internal_topic_family():
    from admin.zendure_mqtt_config_draft import build_manual_zendure_mqtt_fragment

    cases = {
        "solarflow_zensdk": ("zensdk_ha_scalar", "Zendure"),
        "hub_hyper_legacy": ("legacy_zendure_json", "iot"),
        "zendure_cloud": ("zendure_cloud_scalar", None),
    }
    for generation, (topic_family, base_topic) in cases.items():
        fragment, issues = build_manual_zendure_mqtt_fragment(
            {"name": "SF", "serial_number": "DEV1", "generation": generation},
            "local_mqtt",
        )
        assert issues == []
        assert fragment["type"] == "zendure_mqtt"
        assert fragment["mqtt"]["topic_family"] == topic_family
        assert fragment["mqtt"]["base_topic"] == base_topic
        assert fragment["capabilities"]["write_output_limit"] is False


def test_manual_generation_unknown_is_actionable_error():
    from admin.zendure_mqtt_config_draft import build_manual_zendure_mqtt_fragment

    fragment, issues = build_manual_zendure_mqtt_fragment(
        {"name": "SF", "serial_number": "DEV1", "generation": "nope"}, "local_mqtt"
    )
    assert fragment is None
    assert issues and issues[0]["code"] == "zendure_mqtt_generation_unknown"


def test_topic_family_reverse_maps_to_generation():
    from admin.zendure_mqtt_config_draft import hardware_profile_for_topic_family

    assert hardware_profile_for_topic_family("zensdk_ha_scalar") == (
        "solarflow_zensdk",
        False,
    )
    # The leading-slash JSON layout is used by new ZenSDK devices on the cloud
    # broker as well as by older hardware, so the topic family alone never
    # resolves to a hardware generation; it only flags the alternative layout.
    assert hardware_profile_for_topic_family("legacy_zendure_json_alt") == (
        None,
        True,
    )
    # The local ``iot/...`` tree remains legacy-generation evidence.
    assert hardware_profile_for_topic_family("legacy_zendure_json") == (
        "hub_hyper_legacy",
        False,
    )


def test_hardware_generation_resolves_from_model_before_topic_layout():
    # Model/generation and telemetry schema are separate derivations: the
    # product model wins whenever it is known; the topic family stays a
    # schema-only fallback.
    from admin.zendure_mqtt_config_draft import resolve_hardware_generation

    assert resolve_hardware_generation(
        "legacy_zendure_json_alt", model_hint="SolarFlow 800 Pro2"
    ) == ("solarflow_zensdk", True)
    assert resolve_hardware_generation(
        "legacy_zendure_json_alt", model_hint="Hyper 2000"
    ) == ("hub_hyper_legacy", True)
    # The SolarFlow brand also prefixes legacy hub names; the hub token wins.
    assert resolve_hardware_generation(
        "legacy_zendure_json", model_hint="SolarFlow Hub 2000"
    ) == ("hub_hyper_legacy", False)
    assert resolve_hardware_generation(
        "legacy_zendure_json", model_hint="SolarFlow 800 Pro 2"
    ) == ("solarflow_zensdk", False)
    # No model evidence: fall back to the topic-family mapping (ambiguous for
    # the leading-slash layout).
    assert resolve_hardware_generation("legacy_zendure_json_alt") == (None, True)
    assert resolve_hardware_generation("legacy_zendure_json") == (
        "hub_hyper_legacy",
        False,
    )
    assert resolve_hardware_generation("zensdk_ha_scalar", model_hint="") == (
        "solarflow_zensdk",
        False,
    )


def test_neutral_schema_names_keep_stored_legacy_values_valid():
    # The neutral schema names describe the format without implying a hardware
    # generation; the stored config values stay the legacy_* strings so existing
    # configs never become invalid.
    from ems.zendure_mqtt.topics import (
        FAMILY_LEGACY_JSON,
        FAMILY_LEGACY_JSON_ALT,
        FAMILY_ZENDURE_JSON_REPORT,
        FAMILY_ZENDURE_JSON_REPORT_LEADING_SLASH,
        JSON_FAMILIES,
    )

    assert FAMILY_ZENDURE_JSON_REPORT == FAMILY_LEGACY_JSON
    assert FAMILY_ZENDURE_JSON_REPORT_LEADING_SLASH == FAMILY_LEGACY_JSON_ALT
    assert FAMILY_ZENDURE_JSON_REPORT in JSON_FAMILIES
    assert FAMILY_ZENDURE_JSON_REPORT_LEADING_SLASH in JSON_FAMILIES


# --- Zendure SmartMeter D0 guided setup ----------------------------------

def _ambiguous_zendure_meter(**values):
    item = {
        "config_name": "grid_meter",
        "display_name": "Zendure grid meter — model not identified",
        "role": "grid_meter",
        "enabled": True,
        "ip": "192.168.1.80",
        "api_family": "zendure_grid_meter_http",
        "device_type": "zendure_grid_meter_unknown",
    }
    item.update(values)
    return item


def _d0_features(**extra):
    features = {
        "grid_meter.type": "zendure_smartmeter_d0",
        "grid_meter.mqtt.host": "192.0.2.50",
        "grid_meter.mqtt.port": "1883",
    }
    features.update(extra)
    return features


def test_d0_selection_generates_topic_from_discovery_serial():
    result = _feature_generator().generate(
        [_device(1), _ambiguous_zendure_meter(serial_number="D0SN")],
        1,
        features=_d0_features(),
    )
    grid = result["config"]["grid_meter"]
    assert grid["type"] == "zendure_smartmeter_d0"
    assert grid["mqtt"]["topic"] == "Zendure/sensor/D0SN/totalPower"
    assert result["ready"] is True


def test_d0_always_uses_number_payload_format():
    result = _feature_generator().generate(
        [_device(1), _ambiguous_zendure_meter(serial_number="D0SN")],
        1,
        # Even if a JSON payload_format/value_path leaks in from a previous type.
        features=_d0_features(
            **{
                "grid_meter.mqtt.payload_format": "json",
                "grid_meter.mqtt.value_path": "power.total",
            }
        ),
    )
    mqtt = result["config"]["grid_meter"]["mqtt"]
    assert mqtt["payload_format"] == "number"
    assert "value_path" not in mqtt


def test_d0_preview_has_no_stale_http_fields():
    result = _feature_generator().generate(
        [_device(1), _ambiguous_zendure_meter(serial_number="D0SN")],
        1,
        features=_d0_features(),
    )
    grid = result["config"]["grid_meter"]
    for stale in ("ip", "port", "channels", "url", "power_path"):
        assert stale not in grid


def test_3ct_http_preview_has_no_stale_d0_mqtt_values():
    result = _feature_generator().generate(
        [_device(1), _meter(ip="192.0.2.80")],
        1,
        features={
            "grid_meter.type": "zendure_smartmeter_3ct_http",
            # Leftovers from a prior D0/MQTT selection must not survive.
            "grid_meter.mqtt.host": "192.0.2.50",
            "grid_meter.mqtt.topic": "Zendure/sensor/OLD/totalPower",
        },
    )
    grid = result["config"]["grid_meter"]
    assert grid["type"] == "zendure_smartmeter_3ct_http"
    assert grid["ip"] == "192.0.2.80"
    assert "mqtt" not in grid


def test_ambiguous_candidate_can_be_manually_mapped_to_d0():
    result = _feature_generator().generate(
        [_device(1), _ambiguous_zendure_meter(serial_number="D0SN")],
        1,
        features=_d0_features(),
    )
    assert result["config"]["grid_meter"]["type"] == "zendure_smartmeter_d0"


def test_zendure_http_candidate_without_type_is_config_ready_generic():
    # A discovered Zendure HTTP grid meter is config-ready on numeric total_power
    # alone: with no explicit type selection it resolves to the generic local-HTTP
    # type (never a silent Shelly fallback) and a grid_meter block is generated.
    result = _feature_generator().generate(
        [_device(1), _ambiguous_zendure_meter(serial_number="D0SN", ip="192.0.2.80")], 1
    )
    grid = result["config"]["grid_meter"]
    assert grid["type"] == "zendure_grid_meter_http"
    assert grid["ip"] == "192.0.2.80"
    assert "mqtt" not in grid


def test_neutral_candidate_can_be_manually_mapped_to_3ct_http():
    result = _feature_generator().generate(
        [_device(1), _ambiguous_zendure_meter(serial_number="D0SN", ip="192.0.2.80")],
        1,
        features={"grid_meter.type": "zendure_smartmeter_3ct_http"},
    )
    grid = result["config"]["grid_meter"]
    assert grid["type"] == "zendure_smartmeter_3ct_http"
    assert grid["ip"] == "192.0.2.80"
    assert "mqtt" not in grid


def test_d0_missing_serial_blocks_config_ready_with_validation():
    result = _feature_generator().generate(
        [_device(1), _ambiguous_zendure_meter()],
        1,
        features=_d0_features(),
    )
    assert result["ready"] is False
    codes = {issue["code"] for issue in result["validation"]["errors"]}
    assert "grid_meter_d0_serial_missing" in codes
    assert "topic" not in result["config"]["grid_meter"].get("mqtt", {})


def test_d0_missing_serial_with_custom_topic_is_still_rejected():
    # A custom topic must not bypass the required D0 serial number.
    result = _feature_generator().generate(
        [_device(1), _ambiguous_zendure_meter()],
        1,
        features=_d0_features(**{"grid_meter.mqtt.topic": "custom/grid/power"}),
    )
    assert result["ready"] is False
    codes = {issue["code"] for issue in result["validation"]["errors"]}
    assert "grid_meter_d0_serial_missing" in codes


def test_d0_whitespace_serial_is_rejected():
    result = _feature_generator().generate(
        [_device(1), _ambiguous_zendure_meter(serial_number="   ")],
        1,
        features=_d0_features(),
    )
    assert result["ready"] is False
    codes = {issue["code"] for issue in result["validation"]["errors"]}
    assert "grid_meter_d0_serial_missing" in codes


def test_d0_canonical_looking_custom_topic_is_not_overwritten_by_serial():
    # A supplied topic whose shape looks canonical but names a different serial is
    # preserved exactly; the backend never regenerates a non-empty topic.
    result = _feature_generator().generate(
        [_device(1), _ambiguous_zendure_meter(serial_number="D0-B")],
        1,
        features=_d0_features(
            **{"grid_meter.mqtt.topic": "Zendure/sensor/MANUAL/totalPower"}
        ),
    )
    grid = result["config"]["grid_meter"]
    assert grid["mqtt"]["topic"] == "Zendure/sensor/MANUAL/totalPower"


def test_d0_serial_parsed_from_canonical_topic_when_no_explicit_serial():
    # Migration convenience: an existing canonical topic supplies the serial when
    # neither a manual nor a discovered serial is available.
    result = _feature_generator().generate(
        [_device(1), _ambiguous_zendure_meter()],
        1,
        features=_d0_features(
            **{"grid_meter.mqtt.topic": "Zendure/sensor/FROMTOPIC/totalPower"}
        ),
    )
    assert result["ready"] is True
    assert result["config"]["grid_meter"]["mqtt"]["topic"] == (
        "Zendure/sensor/FROMTOPIC/totalPower"
    )


# --- grid-meter variant cleanup ------------------------------------------

def _grid_after_switch(grid_type, meter_fields, features):
    feats = {"grid_meter.type": grid_type}
    feats.update(features)
    result = _feature_generator().generate(
        [_device(1), _meter(**meter_fields)],
        1,
        features=feats,
    )
    return result["config"]["grid_meter"]


def test_tasmota_to_3ct_drops_tasmota_and_shelly_fields():
    grid = _grid_after_switch(
        "zendure_smartmeter_3ct_http",
        {"ip": "192.168.1.80"},
        {
            "grid_meter.url": "http://old/status",
            "grid_meter.power_path": "StatusSNS.Energy.Power",
            "grid_meter.channels": ["a"],
        },
    )
    assert grid["type"] == "zendure_smartmeter_3ct_http"
    assert grid["ip"] == "192.168.1.80"
    for stale in ("url", "power_path", "channels", "mqtt"):
        assert stale not in grid


def test_shelly_to_3ct_keeps_ip_drops_channels():
    grid = _grid_after_switch(
        "zendure_smartmeter_3ct_http",
        {"ip": "192.0.2.80"},
        {"grid_meter.channels": ["a", "b", "c"]},
    )
    assert grid["type"] == "zendure_smartmeter_3ct_http"
    assert grid["ip"] == "192.0.2.80"
    assert "channels" not in grid


def test_3ct_to_tasmota_allows_tasmota_fields():
    grid = _grid_after_switch(
        "tasmota_http",
        {"ip": "192.0.2.80"},
        {
            "grid_meter.url": "http://meter/status",
            "grid_meter.power_path": "StatusSNS.Energy.Power",
        },
    )
    assert grid["type"] == "tasmota_http"
    assert grid["url"] == "http://meter/status"
    assert grid["power_path"] == "StatusSNS.Energy.Power"
    assert "mqtt" not in grid


def test_d0_to_shelly_drops_mqtt_block():
    grid = _grid_after_switch(
        "shelly",
        {"ip": "192.0.2.50"},
        {
            "grid_meter.mqtt.host": "broker",
            "grid_meter.mqtt.topic": "Zendure/sensor/OLD/totalPower",
        },
    )
    assert grid["type"] == "shelly"
    assert grid["ip"] == "192.0.2.50"
    assert "mqtt" not in grid


def test_generic_mqtt_to_d0_drops_value_path():
    grid = _grid_after_switch(
        "zendure_smartmeter_d0",
        {"serial_number": "D0SN"},
        {
            "grid_meter.mqtt.host": "broker",
            "grid_meter.mqtt.value_path": "power.total",
        },
    )
    assert grid["type"] == "zendure_smartmeter_d0"
    assert grid["mqtt"]["host"] == "broker"
    assert "value_path" not in grid["mqtt"]
    assert grid["mqtt"]["payload_format"] == "number"


def test_d0_to_generic_mqtt_keeps_value_path():
    grid = _grid_after_switch(
        "mqtt",
        {"serial_number": "D0SN"},
        {
            "grid_meter.mqtt.host": "broker",
            "grid_meter.mqtt.topic": "custom/topic",
            "grid_meter.mqtt.value_path": "power.total",
        },
    )
    assert grid["type"] == "mqtt"
    assert grid["mqtt"]["value_path"] == "power.total"


def test_d0_manual_serial_generates_topic_and_ip_is_not_used():
    result = _feature_generator().generate(
        [_device(1), _ambiguous_zendure_meter(serial_number="MANUAL1", ip="192.168.1.99")],
        1,
        features=_d0_features(),
    )
    grid = result["config"]["grid_meter"]
    assert grid["mqtt"]["topic"] == "Zendure/sensor/MANUAL1/totalPower"
    assert "ip" not in grid


def test_d0_customized_topic_is_preserved():
    result = _feature_generator().generate(
        [_device(1), _ambiguous_zendure_meter(serial_number="D0SN")],
        1,
        features=_d0_features(**{"grid_meter.mqtt.topic": "custom/grid/power"}),
    )
    assert result["config"]["grid_meter"]["mqtt"]["topic"] == "custom/grid/power"


# --- D0 MQTT grid-meter proposal (target=grid_meter) ------------------------


def _d0_grid_proposal(**overrides):
    proposal = {
        "id": "zendure-mqtt:D0SN",
        "target": "grid_meter",
        "role_hint": "grid_meter_candidate",
        "connection_source": "local_mqtt",
        "topic_family": "zensdk_ha_scalar",
        "broker_ref": "local_mqtt",
        "broker_host": "10.0.0.9",
        "broker_port": 1883,
        "broker_tls": False,
        "serial_number": "D0SN",
        "device_id": "D0SN",
        "seen_topics": ["Zendure/sensor/D0SN/totalPower"],
        "grid_meter_fragment": {
            "type": "zendure_smartmeter_d0",
            "mqtt": {
                "broker_ref": "local_mqtt",
                "topic": "Zendure/sensor/D0SN/totalPower",
                "payload_format": "number",
                "max_age_seconds": 15,
            },
        },
    }
    proposal.update(overrides)
    return proposal


def test_d0_grid_proposal_becomes_central_grid_meter_not_device():
    result = ConfigPreviewGenerator(_ReleaseManager()).generate(
        [_device(1)], 1, zendure_mqtt_proposals=[_d0_grid_proposal()]
    )
    grid = result["config"]["grid_meter"]
    assert grid["type"] == "zendure_smartmeter_d0"
    assert grid["mqtt"]["broker_ref"] == "local_mqtt"
    assert grid["mqtt"]["topic"] == "Zendure/sensor/D0SN/totalPower"
    assert grid["mqtt"]["payload_format"] == "number"
    # It is never appended to devices[].
    assert not any(
        d.get("type") in ("zendure_mqtt", "zendure_smartmeter_d0")
        for d in result["config"]["devices"]
    )
    # A local broker profile is provisioned without secrets.
    broker = result["config"]["zendure_mqtt"]["brokers"]["local_mqtt"]
    assert broker["host"] == "10.0.0.9"
    assert "password" not in broker


def test_d0_grid_proposal_without_topic_is_rejected():
    proposal = _d0_grid_proposal(seen_topics=[], grid_meter_fragment=None)
    result = ConfigPreviewGenerator(_ReleaseManager()).generate(
        [_device(1)], 1, zendure_mqtt_proposals=[proposal]
    )
    codes = {i["code"] for i in result["validation"]["errors"]}
    assert "grid_meter_topic_missing" in codes


def test_d0_grid_proposal_cloud_source_is_rejected():
    proposal = _d0_grid_proposal(connection_source="zendure_cloud_mqtt")
    result = ConfigPreviewGenerator(_ReleaseManager()).generate(
        [_device(1)], 1, zendure_mqtt_proposals=[proposal]
    )
    codes = {i["code"] for i in result["validation"]["errors"]}
    assert "grid_meter_cloud_unsupported" in codes


def test_two_d0_grid_proposals_are_rejected():
    p1 = _d0_grid_proposal()
    p2 = _d0_grid_proposal(id="zendure-mqtt:D0SN2")
    result = ConfigPreviewGenerator(_ReleaseManager()).generate(
        [_device(1)], 1, zendure_mqtt_proposals=[p1, p2]
    )
    codes = {i["code"] for i in result["validation"]["errors"]}
    assert "grid_meter_duplicate" in codes


def test_d0_grid_proposal_does_not_silently_replace_selected_http_meter():
    result = ConfigPreviewGenerator(_ReleaseManager()).generate(
        [_device(1), _meter()], 1, zendure_mqtt_proposals=[_d0_grid_proposal()]
    )
    codes = {i["code"] for i in result["validation"]["errors"]}
    assert "grid_meter_conflict" in codes
    # The HTTP meter stays selected; the D0 did not overwrite it.
    assert result["config"]["grid_meter"]["type"] != "zendure_smartmeter_d0"


def test_d0_grid_proposal_replaces_http_meter_only_on_explicit_request():
    proposal = _d0_grid_proposal(replace_grid_meter=True)
    result = ConfigPreviewGenerator(_ReleaseManager()).generate(
        [_device(1), _meter()], 1, zendure_mqtt_proposals=[proposal]
    )
    assert result["config"]["grid_meter"]["type"] == "zendure_smartmeter_d0"


def test_d0_grid_proposal_ignores_untrusted_fragment_topic():
    # A frontend-supplied non-canonical topic is rejected; only the exact
    # observed totalPower topic is used.
    proposal = _d0_grid_proposal(
        seen_topics=["Zendure/sensor/D0SN/totalPower"],
        grid_meter_fragment={
            "type": "zendure_smartmeter_d0",
            "mqtt": {"broker_ref": "local_mqtt", "topic": "attacker/evil/topic"},
        },
    )
    result = ConfigPreviewGenerator(_ReleaseManager()).generate(
        [_device(1)], 1, zendure_mqtt_proposals=[proposal]
    )
    grid = result["config"]["grid_meter"]
    assert grid["mqtt"]["topic"] == "Zendure/sensor/D0SN/totalPower"


# --- Defect 2: D0 topic is bound to the trusted proposal identity ------------
def test_d0_fake_serial_topic_injection_is_rejected():
    # Serial REAL was discovered; a browser-injected FAKE totalPower topic in the
    # observation set is rejected rather than mapped.
    proposal = _d0_grid_proposal(
        serial_number="REAL",
        device_id="REAL",
        seen_topics=[
            "Zendure/sensor/REAL/totalPower",
            "Zendure/sensor/FAKE/totalPower",
        ],
    )
    result = ConfigPreviewGenerator(_ReleaseManager()).generate(
        [_device(1)], 1, zendure_mqtt_proposals=[proposal]
    )
    codes = {i["code"] for i in result["validation"]["errors"]}
    assert "grid_meter_topic_identity_mismatch" in codes
    assert "grid_meter" not in result["config"]
    assert "FAKE" not in json.dumps(result["config"])


def test_d0_totalpower_topic_must_have_been_observed():
    # Only another metric was observed, never totalPower for the serial.
    proposal = _d0_grid_proposal(
        serial_number="REAL",
        device_id="REAL",
        seen_topics=[
            "Zendure/sensor/REAL/electricLevel",
            "Zendure/sensor/REAL/outputHomePower",
        ],
    )
    result = ConfigPreviewGenerator(_ReleaseManager()).generate(
        [_device(1)], 1, zendure_mqtt_proposals=[proposal]
    )
    codes = {i["code"] for i in result["validation"]["errors"]}
    assert "grid_meter_topic_missing" in codes
    assert "grid_meter" not in result["config"]


def test_d0_empty_seen_topics_is_rejected():
    proposal = _d0_grid_proposal(seen_topics=[])
    result = ConfigPreviewGenerator(_ReleaseManager()).generate(
        [_device(1)], 1, zendure_mqtt_proposals=[proposal]
    )
    codes = {i["code"] for i in result["validation"]["errors"]}
    assert "grid_meter_topic_missing" in codes


def test_d0_wildcard_seen_topic_is_rejected():
    proposal = _d0_grid_proposal(
        seen_topics=["Zendure/sensor/D0SN/totalPower", "Zendure/#"],
    )
    result = ConfigPreviewGenerator(_ReleaseManager()).generate(
        [_device(1)], 1, zendure_mqtt_proposals=[proposal]
    )
    codes = {i["code"] for i in result["validation"]["errors"]}
    assert "grid_meter_topic_untrusted" in codes


def test_d0_inconsistent_serial_and_device_id_is_rejected():
    proposal = _d0_grid_proposal(serial_number="REAL", device_id="FAKE")
    result = ConfigPreviewGenerator(_ReleaseManager()).generate(
        [_device(1)], 1, zendure_mqtt_proposals=[proposal]
    )
    codes = {i["code"] for i in result["validation"]["errors"]}
    assert "grid_meter_identity_mismatch" in codes


def test_d0_topic_is_rebuilt_server_side_from_serial():
    # Exact canonical topic observed, broker matches: the server rebuilds the
    # topic from the serial rather than trusting the browser fragment.
    proposal = _d0_grid_proposal(
        serial_number="D0SN",
        device_id="D0SN",
        grid_meter_fragment={
            "type": "zendure_smartmeter_d0",
            "mqtt": {"broker_ref": "local_mqtt", "topic": "Zendure/sensor/EVIL/totalPower"},
        },
    )
    result = ConfigPreviewGenerator(_ReleaseManager()).generate(
        [_device(1)], 1, zendure_mqtt_proposals=[proposal]
    )
    grid = result["config"]["grid_meter"]
    assert grid["mqtt"]["topic"] == "Zendure/sensor/D0SN/totalPower"
    assert "EVIL" not in json.dumps(result["config"])


# --- Defect 1: existing broker refs are reused only on endpoint match --------
def _config_with_broker(ref, host, *, source="local_mqtt", port=1883, with_device=True):
    brokers = {
        ref: {
            "enabled": True, "source": source, "host": host, "port": port, "tls": False,
        }
    }
    # These tests exercise broker-ref resolution, not the EMS no-device guard.
    # Keep one real API control inverter in every fixture so a D0 or telemetry
    # proposal is never the only object presented as a bootable control config.
    devices = [
        {"name": "WR1", "ip": "10.0.0.5", "sn": "API1", "max_power": 800}
    ]
    if with_device:
        devices.insert(
            0,
            {
                "type": "zendure_mqtt", "name": "Legacy", "enabled": True,
                "serial_number": "LEG1",
                "hardware_profile": "hyper_2000",
                "power_write_profile": "legacy_object_device_automation",
                "mqtt": {
                    "broker_ref": ref, "source": source,
                    "topic_family": "legacy_zendure_json", "base_topic": "iot",
                    "device_id": "DEVLEG", "product_key": "PK1",
                },
                "capabilities": {"write_output_limit": True},
            }
        )
    return {
        "system": {"max_total_power": 2000},
        "zendure_mqtt": {"enabled": True, "brokers": brokers},
        "devices": devices,
        "grid_meter": {"type": "shelly", "ip": "10.0.0.9"},
    }


def test_new_d0_on_different_endpoint_gets_distinct_ref(tmp_path):
    # Regression scenario 1: existing local_mqtt -> 10.0.0.10; a D0 discovered on
    # 10.0.0.20 must not inherit the existing endpoint.
    path = _write_config(tmp_path, _config_with_broker("local_mqtt", "10.0.0.10"))
    proposal = _d0_grid_proposal(
        broker_ref="local_mqtt", broker_host="10.0.0.20", broker_port=1883,
        replace_grid_meter=True,
    )
    result = _existing_generator(path).generate([], zendure_mqtt_proposals=[proposal])
    assert result["ready"] is True
    brokers = result["config"]["zendure_mqtt"]["brokers"]
    # The existing broker keeps its endpoint; a second, distinct broker is added.
    assert brokers["local_mqtt"]["host"] == "10.0.0.10"
    grid_ref = result["config"]["grid_meter"]["mqtt"]["broker_ref"]
    assert grid_ref != "local_mqtt"
    assert brokers[grid_ref]["host"] == "10.0.0.20"
    # The legacy inverter stays on the original broker.
    legacy = result["config"]["devices"][0]
    assert legacy["mqtt"]["broker_ref"] == "local_mqtt"


def test_matching_existing_broker_endpoint_is_reused(tmp_path):
    # Regression scenario 2: an existing broker whose endpoint matches the
    # discovered D0 is reused, never duplicated, even under a different ref name.
    path = _write_config(
        tmp_path, _config_with_broker("home_broker", "10.0.0.10", with_device=False)
    )
    proposal = _d0_grid_proposal(
        broker_ref="local_mqtt", broker_host="10.0.0.10", broker_port=1883,
        replace_grid_meter=True,
    )
    result = _existing_generator(path).generate([], zendure_mqtt_proposals=[proposal])
    assert result["ready"] is True
    brokers = result["config"]["zendure_mqtt"]["brokers"]
    assert set(brokers) == {"home_broker"}
    assert result["config"]["grid_meter"]["mqtt"]["broker_ref"] == "home_broker"


def test_device_proposal_matching_endpoint_reuses_existing_broker(tmp_path):
    path = _write_config(
        tmp_path, _config_with_broker("home_broker", "10.0.0.10", with_device=False)
    )
    proposal = _broker_proposal("local_mqtt", name="New SolarFlow")
    proposal["config_fragment"]["mqtt"]["device_id"] = "NEWDEV"
    proposal["broker_host"] = "10.0.0.10"
    proposal["broker_port"] = 1883
    result = _existing_generator(path).generate([], zendure_mqtt_proposals=[proposal])
    assert result["ready"] is True
    brokers = result["config"]["zendure_mqtt"]["brokers"]
    assert set(brokers) == {"home_broker"}
    entry = [d for d in result["config"]["devices"] if d.get("name") == "INV_2"][0]
    assert entry["mqtt"]["broker_ref"] == "home_broker"


def test_device_proposal_conflicting_endpoint_gets_distinct_ref(tmp_path):
    path = _write_config(tmp_path, _config_with_broker("local_mqtt", "10.0.0.10"))
    proposal = _broker_proposal("local_mqtt", name="Second SolarFlow")
    proposal["config_fragment"]["mqtt"]["device_id"] = "SECONDDEV"
    proposal["broker_host"] = "10.0.0.20"
    proposal["broker_port"] = 1883
    result = _existing_generator(path).generate([], zendure_mqtt_proposals=[proposal])
    assert result["ready"] is True
    brokers = result["config"]["zendure_mqtt"]["brokers"]
    assert brokers["local_mqtt"]["host"] == "10.0.0.10"
    entry = [d for d in result["config"]["devices"] if d.get("name") == "INV_3"][0]
    new_ref = entry["mqtt"]["broker_ref"]
    assert new_ref != "local_mqtt"
    assert brokers[new_ref]["host"] == "10.0.0.20"
    # No endpoint was overwritten and no credential was crossed between profiles.
    blob = json.dumps(result["config"]).lower()
    for secret in ("password", "username", "token"):
        assert secret not in blob


def _cloud_generator(auth=True, template=TEMPLATE):
    return ConfigPreviewGenerator(
        _ReleaseManager(template), zendure_cloud_auth_available=lambda: auth
    )


def _warning_codes(result):
    return {issue["code"] for issue in result["validation"]["warnings"]}


def test_cloud_auth_without_mqtt_device_warns_about_http_only():
    result = _cloud_generator(auth=True).generate([_device(1)])
    assert "zendure_mqtt_cloud_devices_not_selected" in _warning_codes(result)


def test_no_cloud_auth_does_not_warn_about_http_only():
    result = _cloud_generator(auth=False).generate([_device(1)])
    assert "zendure_mqtt_cloud_devices_not_selected" not in _warning_codes(result)


def test_cloud_auth_without_inverters_does_not_warn():
    result = _cloud_generator(auth=True).generate([])
    assert "zendure_mqtt_cloud_devices_not_selected" not in _warning_codes(result)


def test_cloud_auth_with_mqtt_device_present_does_not_warn(tmp_path):
    base = copy.deepcopy(EXISTING_CONFIG)
    base["devices"].append(
        {"name": "MQTT1", "type": "zendure_mqtt", "sn": "MQ1",
         "mqtt": {"broker_ref": "default"}}
    )
    path = _write_config(tmp_path, base)
    generator = ConfigPreviewGenerator(
        _ReleaseManager(),
        install_context_provider=lambda: _context(path),
        zendure_cloud_auth_available=lambda: True,
    )
    result = generator.generate([_device(1, config_name="WR1")])
    assert "zendure_mqtt_cloud_devices_not_selected" not in _warning_codes(result)


# --- TLS mode is read exactly as EMS Core reads it --------------------------


@pytest.mark.parametrize("security", ["tls", "mqtts", "ssl", "system_ca", "secure"])
def test_every_core_tls_alias_provisions_a_tls_broker(security):
    """Admin must not read a mode as plain that Core reads as TLS.

    Both sides accepting the same vocabulary is what keeps a broker from being
    provisioned without transport security while the operator asked for it.
    """

    result = ConfigPreviewGenerator(_ReleaseManager()).generate(
        [_device(1), _meter()],
        1,
        zendure_mqtt_broker={
            "name": "local_mqtt",
            "host": "192.168.1.20",
            "security": security,
        },
        zendure_mqtt_manual_devices=[_manual_mqtt()],
    )

    assert result["ready"] is True, result["validation"]
    profile = result["config"]["zendure_mqtt"]["brokers"]["local_mqtt"]
    assert profile["tls"] is True
    assert profile["port"] == 8883, "a TLS broker defaults to the TLS port"


def test_the_insecure_tls_mode_keeps_its_verification_opt_out():
    """Silently upgrading to verified TLS would break the connection instead."""

    result = ConfigPreviewGenerator(_ReleaseManager()).generate(
        [_device(1), _meter()],
        1,
        zendure_mqtt_broker={
            "name": "local_mqtt",
            "host": "192.168.1.20",
            "security": "insecure_no_verify",
        },
        zendure_mqtt_manual_devices=[_manual_mqtt()],
    )

    assert result["ready"] is True, result["validation"]
    profile = result["config"]["zendure_mqtt"]["brokers"]["local_mqtt"]
    assert profile["tls"] is True
    assert profile["tls_insecure"] is True


def test_an_unknown_security_mode_is_refused_not_silently_plain():
    """Core raises on an unknown mode; Admin must not answer it with plaintext."""

    result = ConfigPreviewGenerator(_ReleaseManager()).generate(
        [_device(1), _meter()],
        1,
        zendure_mqtt_broker={
            "name": "local_mqtt",
            "host": "192.168.1.20",
            "security": "sort-of-secure",
        },
        zendure_mqtt_manual_devices=[_manual_mqtt()],
    )

    assert result["ready"] is False
    codes = {issue["code"] for issue in result["validation"]["errors"]}
    assert "zendure_mqtt_broker_security_invalid" in codes


def test_plain_stays_plain():
    result = ConfigPreviewGenerator(_ReleaseManager()).generate(
        [_device(1), _meter()],
        1,
        zendure_mqtt_broker=dict(_LOCAL_BROKER, security="plain"),
        zendure_mqtt_manual_devices=[_manual_mqtt()],
    )

    profile = result["config"]["zendure_mqtt"]["brokers"]["local_mqtt"]
    assert profile["tls"] is False
    assert profile.get("tls_insecure") in (None, False)
