# SPDX-License-Identifier: AGPL-3.0-or-later
"""CLI setup assistant grid-meter coverage for the Zendure 3CT HTTP type."""

from ems import config_init


def test_supported_grid_meter_types_include_zendure_3ct_http():
    assert (
        "zendure_smartmeter_3ct_http"
        in config_init.SUPPORTED_GRID_METER_TYPES
    )


def test_ask_grid_meter_zendure_3ct_http_keeps_only_ip():
    existing = {
        "type": "zendure_smartmeter_3ct_http",
        "ip": "192.0.2.80",
        # stale keys from a previous meter type must be dropped
        "url": "http://192.168.1.50/cm?cmnd=Status%2010",
        "power_path": "StatusSNS.SML.Power_curr",
        "mqtt": {"host": "mqtt.local", "topic": "meter/grid"},
    }

    result = config_init.ask_grid_meter(existing, noninteractive=True)

    assert result["type"] == "zendure_smartmeter_3ct_http"
    assert result["ip"] == "192.0.2.80"
    assert "url" not in result
    assert "power_path" not in result
    assert "mqtt" not in result


def test_ask_grid_meter_zendure_3ct_http_requires_ip():
    existing = {"type": "zendure_smartmeter_3ct_http"}

    try:
        config_init.ask_grid_meter(existing, noninteractive=True)
    except config_init.ConfigInitError as exc:
        assert "IP" in str(exc) or "ip" in str(exc)
    else:  # pragma: no cover - defensive
        raise AssertionError("expected ConfigInitError for missing IP")
