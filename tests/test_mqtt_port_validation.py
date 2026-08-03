# SPDX-License-Identifier: AGPL-3.0-or-later
"""Strict shared MQTT port validation (defect 6).

Covers the lowest shared layer (``ems.config.parse_mqtt_port``) plus the Admin
preview integration, so an explicit invalid port is rejected everywhere instead
of being silently clamped or replaced.
"""

import pytest

from ems import config as cfg

pytestmark = [
    pytest.mark.mqtt,
    pytest.mark.unit,
    pytest.mark.simulation,
]


@pytest.mark.parametrize("value", [1, 1883, 8883, 65535, "1883", "8883"])
def test_valid_ports_accepted(value):
    assert cfg.parse_mqtt_port(value) == int(value)


@pytest.mark.parametrize(
    "value", [0, -1, 65536, 70000, "broken", "1883x", "", True, False, 12.5, "0"]
)
def test_invalid_explicit_ports_rejected(value):
    with pytest.raises(ValueError):
        cfg.parse_mqtt_port(value)


def test_absent_port_uses_default_only_when_allowed():
    assert cfg.parse_mqtt_port(None, default=1883) == 1883
    assert cfg.parse_mqtt_port("", default=8883) == 8883
    with pytest.raises(ValueError):
        cfg.parse_mqtt_port(None)


def test_explicit_invalid_never_replaced_by_default():
    # A default is offered, but an explicit invalid value must still be rejected.
    with pytest.raises(ValueError):
        cfg.parse_mqtt_port(0, default=1883)
    with pytest.raises(ValueError):
        cfg.parse_mqtt_port(70000, default=1883)


def test_protocol_default_follows_tls():
    assert cfg.default_mqtt_port(False) == 1883
    assert cfg.default_mqtt_port(True) == 8883


def test_broker_profile_resolution_rejects_invalid_port():
    config = {
        "grid_meter": {
            "type": "zendure_smartmeter_d0",
            "mqtt": {"broker_ref": "home", "topic": "Zendure/sensor/D0SN/totalPower"},
        },
        "zendure_mqtt": {
            "enabled": True,
            "brokers": {
                "home": {
                    "enabled": True,
                    "source": "local_mqtt",
                    "host": "192.168.1.10",
                    "port": 70000,
                }
            },
        },
    }
    with pytest.raises(ValueError):
        cfg.resolve_grid_meter_mqtt_settings(config)


def test_admin_preview_rejects_invalid_manual_broker_port():
    from admin.config_preview import _apply_manual_zendure_mqtt_broker

    validation = {"errors": [], "warnings": [], "info": []}
    preview = {}
    _apply_manual_zendure_mqtt_broker(
        preview, {"name": "home", "host": "192.168.1.10", "port": "70000"}, validation
    )
    codes = [issue["code"] for issue in validation["errors"]]
    assert "zendure_mqtt_broker_port_invalid" in codes
    # No broker profile is provisioned from an invalid explicit port.
    assert "home" not in preview.get("zendure_mqtt", {}).get("brokers", {})
