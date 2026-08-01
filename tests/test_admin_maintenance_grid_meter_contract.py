# SPDX-License-Identifier: AGPL-3.0-or-later
"""Maintenance grid-meter preservation and Core validation-parity contract.

A no-op Maintenance apply must preserve a Core-supported *legacy flat* MQTT
grid-meter configuration byte-for-byte: loading it into a draft and applying it
unchanged must not strip the flat connection fields (host/port/topic/…). The
resulting config must still be accepted by the EMS Core grid-meter resolver.

Separately, Maintenance validation must reach parity with Core: an MQTT
grid-meter draft that the Core resolver rejects must be rejected by Maintenance
*before* a config payload is prepared, not silently written and then rejected at
EMS startup.
"""

import copy
import json

import pytest

from admin.maintenance_config import (
    load_maintenance_config,
    prepare_maintenance_config_apply,
    preview_maintenance_config,
)
from ems.config import (
    normalize_mqtt_grid_meter_settings,
    resolve_grid_meter_mqtt_settings,
)

pytestmark = pytest.mark.simulation


@pytest.fixture(autouse=True)
def _isolate(isolated_install_root):
    return isolated_install_root


def _write_config(base_dir, data):
    config_dir = base_dir / "config"
    config_dir.mkdir(exist_ok=True)
    (config_dir / "config.json").write_text(json.dumps(data), encoding="utf-8")


def _base_config():
    return {
        "system": {"max_total_power": 1600},
        "devices": [
            {"name": "WR1", "ip": "192.168.1.100", "sn": "AAA", "max_power": 800},
        ],
    }


def _core_accepts_grid_meter(config):
    """Return True when the EMS Core resolver accepts the MQTT grid meter."""

    grid_type = str(config.get("grid_meter", {}).get("type") or "").strip().lower()
    resolved = resolve_grid_meter_mqtt_settings(config)
    normalize_mqtt_grid_meter_settings(
        {"type": grid_type, "mqtt": resolved}, meter_type=grid_type
    )
    return True


# --- Contract A: legacy flat MQTT grid meter survives a no-op --------------


def test_legacy_flat_mqtt_grid_meter_survives_noop(tmp_path):
    config = _base_config()
    config["grid_meter"] = {
        "type": "mqtt",
        "host": "192.168.1.20",
        "port": 1883,
        "username": "meter",
        "password": "meter-secret",
        "topic": "meter/power",
        "payload_format": "json",
        "value_path": "power",
        "max_age_seconds": 30,
    }
    original = copy.deepcopy(config)
    _write_config(tmp_path, config)

    loaded = load_maintenance_config(base_dir=str(tmp_path))
    assert loaded["status"] == "ok"
    draft = loaded["draft"]

    preview = preview_maintenance_config(draft, base_dir=str(tmp_path))
    assert preview["status"] == "ok"
    assert preview["changed"] is False, preview["diff"]
    # The stored secret must never reach the browser-facing preview.
    assert "meter-secret" not in json.dumps(preview)

    prepared = prepare_maintenance_config_apply(
        draft, loaded["revision"], base_dir=str(tmp_path)
    )
    assert prepared["status"] == "ok", prepared
    merged = json.loads(prepared["payload"])
    assert merged == original, "flat MQTT grid meter fields must all survive a no-op"
    # And the untouched config is still accepted by the EMS Core resolver.
    assert _core_accepts_grid_meter(merged)


# --- Contract B: Maintenance/Core validation parity -----------------------


def test_invalid_mqtt_grid_meter_rejected_before_write(tmp_path):
    # A flat MQTT grid meter missing the required host is accepted by Maintenance
    # today but rejected by Core at startup. Maintenance must reject it first.
    config = _base_config()
    config["grid_meter"] = {
        "type": "mqtt",
        "topic": "meter/power",
        "payload_format": "json",
    }
    _write_config(tmp_path, config)

    loaded = load_maintenance_config(base_dir=str(tmp_path))
    draft = loaded["draft"]

    preview = preview_maintenance_config(draft, base_dir=str(tmp_path))
    assert preview["validation"]["ok"] is False
    codes = {e["code"] for e in preview["validation"]["errors"]}
    assert "grid_meter_mqtt_invalid" in codes

    prepared = prepare_maintenance_config_apply(
        draft, loaded["revision"], base_dir=str(tmp_path)
    )
    assert prepared["status"] == "invalid"
    assert "payload" not in prepared


# --- Contract C: MQTT/variant grid-meter fields are editable ----------------
# The Maintenance editor exposes the same variant-specific fields as the setup
# flow (Tasmota url/power_path, grid_meter.mqtt.*), while an untouched draft
# stays a byte-level no-op and stored secrets never reach the browser.


def _nested_mqtt_config():
    config = _base_config()
    config["grid_meter"] = {
        "type": "mqtt",
        "mqtt": {
            "host": "192.168.1.20",
            "port": 1883,
            "username": "meter",
            "password": "meter-secret",
            "topic": "meter/power",
            "payload_format": "json",
            "value_path": "power",
            "max_age_seconds": 30,
        },
    }
    return config


def test_draft_exposes_mqtt_settings_without_password(tmp_path):
    _write_config(tmp_path, _nested_mqtt_config())
    draft = load_maintenance_config(base_dir=str(tmp_path))["draft"]
    mqtt = draft["grid_meter"]["mqtt"]
    assert mqtt["host"] == "192.168.1.20"
    assert mqtt["topic"] == "meter/power"
    assert mqtt["has_password"] is True
    assert "password" not in mqtt


def test_editing_nested_mqtt_topic_keeps_nested_representation(tmp_path):
    _write_config(tmp_path, _nested_mqtt_config())
    loaded = load_maintenance_config(base_dir=str(tmp_path))
    draft = loaded["draft"]
    draft["grid_meter"]["mqtt"]["topic"] = "meter/total"

    prepared = prepare_maintenance_config_apply(
        draft, loaded["revision"], base_dir=str(tmp_path)
    )
    assert prepared["status"] == "ok", prepared
    merged = json.loads(prepared["payload"])
    assert merged["grid_meter"]["mqtt"]["topic"] == "meter/total"
    assert "topic" not in merged["grid_meter"], "representation must stay nested"
    assert merged["grid_meter"]["mqtt"]["password"] == "meter-secret"
    assert _core_accepts_grid_meter(merged)


def test_editing_flat_mqtt_topic_keeps_flat_representation(tmp_path):
    config = _base_config()
    config["grid_meter"] = {
        "type": "mqtt",
        "host": "192.168.1.20",
        "password": "meter-secret",
        "topic": "meter/power",
        "payload_format": "number",
    }
    _write_config(tmp_path, config)
    loaded = load_maintenance_config(base_dir=str(tmp_path))
    draft = loaded["draft"]
    draft["grid_meter"]["mqtt"]["topic"] = "meter/total"

    prepared = prepare_maintenance_config_apply(
        draft, loaded["revision"], base_dir=str(tmp_path)
    )
    assert prepared["status"] == "ok", prepared
    merged = json.loads(prepared["payload"])
    assert merged["grid_meter"]["topic"] == "meter/total"
    assert "mqtt" not in merged["grid_meter"], "representation must stay flat"
    assert merged["grid_meter"]["password"] == "meter-secret"
    assert _core_accepts_grid_meter(merged)


def test_blank_mqtt_password_keeps_stored_secret(tmp_path):
    _write_config(tmp_path, _nested_mqtt_config())
    loaded = load_maintenance_config(base_dir=str(tmp_path))
    draft = loaded["draft"]
    draft["grid_meter"]["mqtt"]["password"] = ""
    draft["grid_meter"]["mqtt"]["topic"] = "meter/total"

    prepared = prepare_maintenance_config_apply(
        draft, loaded["revision"], base_dir=str(tmp_path)
    )
    merged = json.loads(prepared["payload"])
    assert merged["grid_meter"]["mqtt"]["password"] == "meter-secret"


def test_clear_mqtt_password_removes_it(tmp_path):
    _write_config(tmp_path, _nested_mqtt_config())
    loaded = load_maintenance_config(base_dir=str(tmp_path))
    draft = loaded["draft"]
    draft["grid_meter"]["mqtt"]["clear_password"] = True

    prepared = prepare_maintenance_config_apply(
        draft, loaded["revision"], base_dir=str(tmp_path)
    )
    merged = json.loads(prepared["payload"])
    assert "password" not in merged["grid_meter"]["mqtt"]


def test_new_mqtt_password_is_written_but_never_previewed(tmp_path):
    _write_config(tmp_path, _nested_mqtt_config())
    loaded = load_maintenance_config(base_dir=str(tmp_path))
    draft = loaded["draft"]
    draft["grid_meter"]["mqtt"]["password"] = "NEW_SECRET"

    preview = preview_maintenance_config(draft, base_dir=str(tmp_path))
    blob = json.dumps(preview)
    assert "NEW_SECRET" not in blob
    assert "meter-secret" not in blob

    prepared = prepare_maintenance_config_apply(
        draft, loaded["revision"], base_dir=str(tmp_path)
    )
    merged = json.loads(prepared["payload"])
    assert merged["grid_meter"]["mqtt"]["password"] == "NEW_SECRET"


def test_nested_mqtt_noop_stays_byte_identical(tmp_path):
    config = _nested_mqtt_config()
    original = copy.deepcopy(config)
    _write_config(tmp_path, config)
    loaded = load_maintenance_config(base_dir=str(tmp_path))

    preview = preview_maintenance_config(loaded["draft"], base_dir=str(tmp_path))
    assert preview["changed"] is False, preview["diff"]
    prepared = prepare_maintenance_config_apply(
        loaded["draft"], loaded["revision"], base_dir=str(tmp_path)
    )
    assert json.loads(prepared["payload"]) == original


def test_editing_tasmota_url_and_power_path(tmp_path):
    config = _base_config()
    config["grid_meter"] = {
        "type": "tasmota_http",
        "ip": "192.168.1.60",
        "url": "http://192.168.1.60/cm?cmnd=Status%208",
        "power_path": "StatusSNS.Power",
    }
    _write_config(tmp_path, config)
    loaded = load_maintenance_config(base_dir=str(tmp_path))
    draft = loaded["draft"]
    assert draft["grid_meter"]["url"].startswith("http://192.168.1.60")
    draft["grid_meter"]["power_path"] = "StatusSNS.ENERGY.Power"

    prepared = prepare_maintenance_config_apply(
        draft, loaded["revision"], base_dir=str(tmp_path)
    )
    merged = json.loads(prepared["payload"])
    assert merged["grid_meter"]["power_path"] == "StatusSNS.ENERGY.Power"


def test_switch_http_to_mqtt_writes_nested_settings(tmp_path):
    config = _base_config()
    config["grid_meter"] = {"type": "shelly", "ip": "192.168.1.50"}
    _write_config(tmp_path, config)
    loaded = load_maintenance_config(base_dir=str(tmp_path))
    draft = loaded["draft"]
    draft["grid_meter"]["type"] = "mqtt"
    draft["grid_meter"]["mqtt"] = {
        "host": "192.168.1.20",
        "topic": "meter/power",
        "payload_format": "number",
    }

    prepared = prepare_maintenance_config_apply(
        draft, loaded["revision"], base_dir=str(tmp_path)
    )
    assert prepared["status"] == "ok", prepared
    merged = json.loads(prepared["payload"])
    assert merged["grid_meter"]["mqtt"]["host"] == "192.168.1.20"
    assert merged["grid_meter"]["mqtt"]["topic"] == "meter/power"
    assert "ip" not in merged["grid_meter"], "stale HTTP fields must be stripped"
    assert _core_accepts_grid_meter(merged)


# --- Contract C: a variant switch obeys the EMS-owned catalog ---------------


def test_switch_between_mqtt_variants_drops_fields_the_variant_cannot_carry(tmp_path):
    """A D0 meter must not inherit the generic MQTT meter's value_path.

    The catalog decides which keys each variant may carry, and the fresh-install
    preview already strips by exactly that. Maintenance used its own coarse
    MQTT/non-MQTT split, so a switch inside the MQTT family left keys behind
    that the target variant does not know.
    """

    from ems.config_catalog import grid_meter_variant_field_spec

    config = _base_config()
    config["grid_meter"] = {
        "type": "mqtt",
        "mqtt": {
            "host": "192.168.1.20",
            "port": 1883,
            "topic": "meter/power",
            "payload_format": "json",
            "value_path": "total.power",
            "max_age_seconds": 30,
        },
    }
    _write_config(tmp_path, config)
    loaded = load_maintenance_config(base_dir=str(tmp_path))
    draft = loaded["draft"]
    draft["grid_meter"]["type"] = "zendure_smartmeter_d0"
    # D0 only reads a plain number, so the operator switches this along with
    # the type; value_path is the key that has no D0 meaning at all.
    draft["grid_meter"]["mqtt"]["payload_format"] = "number"

    prepared = prepare_maintenance_config_apply(
        draft, loaded["revision"], base_dir=str(tmp_path)
    )
    assert prepared["status"] == "ok", prepared
    merged = json.loads(prepared["payload"])

    allowed = grid_meter_variant_field_spec("zendure_smartmeter_d0")["mqtt_keys"]
    assert set(merged["grid_meter"]["mqtt"]) <= set(allowed)
    assert "value_path" not in merged["grid_meter"]["mqtt"]
    assert merged["grid_meter"]["mqtt"]["topic"] == "meter/power"


def test_editable_mqtt_grid_meter_keys_come_from_the_catalog(tmp_path):
    """The editable set is derived, so a new catalog field is editable at once."""

    from admin.maintenance_config import _MQTT_GRID_METER_EDIT_KEYS
    from ems.config_catalog import GRID_METER_KNOWN_MQTT_KEYS

    editable = set(_MQTT_GRID_METER_EDIT_KEYS)
    assert editable == (set(GRID_METER_KNOWN_MQTT_KEYS) - {"password"}) | {
        "broker_ref"
    }
