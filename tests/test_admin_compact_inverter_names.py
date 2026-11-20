# SPDX-FileCopyrightText: 2026 basecubedev
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Compact inverter-name contract shared by Setup and Maintenance."""

from pathlib import Path

import pytest

from admin.config_preview import ConfigPreviewGenerator
from tests.modern_frontend import js_call


pytestmark = [
    pytest.mark.admin,
    pytest.mark.setup,
    pytest.mark.contract,
    pytest.mark.simulation,
]

ROOT = Path(__file__).resolve().parents[1]
SETUP_DRAFT_JS = ROOT / "frontend" / "admin-modern" / "src" / "lib" / "setupDraft.js"


@pytest.mark.parametrize(
    ("names", "count", "expected"),
    [
        ([], 0, "INV_1"),
        (["WR1", "WR2"], 2, "INV_3"),
        (["WR1", "INV_4"], 2, "INV_5"),
        (["INV_1", "INV_3"], 2, "INV_4"),
        (["inv_1", "InV_2"], 2, "INV_3"),
        (["INV_2"], 1, "INV_3"),
    ],
)
def test_frontend_compact_allocator_contract(names, count, expected):
    assert js_call(SETUP_DRAFT_JS, "nextCompactName", names, count) == expected


@pytest.mark.parametrize(
    ("names", "count", "expected"),
    [
        ([], 0, "INV_1"),
        (["WR1", "WR2"], 2, "INV_3"),
        (["WR1", "INV_4"], 2, "INV_5"),
        (["INV_1", "INV_3"], 2, "INV_4"),
        (["inv_1", "InV_2"], 2, "INV_3"),
    ],
)
def test_backend_compact_allocator_matches_frontend(names, count, expected):
    from admin.inverter_names import next_compact_inverter_name

    assert next_compact_inverter_name(names, count) == expected


def _generator():
    return ConfigPreviewGenerator(
        _ReleaseManager(), install_context_provider=lambda: None
    )


def _local(serial, host, **extra):
    item = {
        "display_name": "SolarFlow 800 Pro 2",
        "role": "inverter",
        "enabled": True,
        "ip": host,
        "serial_number": serial,
    }
    item.update(extra)
    return item


def _meter():
    return {
        "config_name": "grid_meter",
        "display_name": "Shelly Pro 3EM",
        "role": "grid_meter",
        "enabled": True,
        "ip": "192.0.2.20",
        "api_family": "shelly_gen2",
    }


def _mqtt_proposal(config_name_marker=True):
    proposal = {
        "id": "mqtt:EOD1NLN9P010902",
        "target": "device",
        "display_name": "SolarFlow 800 Pro 2",
        "serial_number": "EOD1NLN9P010902",
        "device_id": "EOD1NLN9P010902",
        "config_fragment": {
            "type": "zendure_mqtt",
            "enabled": True,
            "name": "Zendure MQTT SolarFlow 800 Pro 2 EOD1NLN9P010902",
            "serial_number": "EOD1NLN9P010902",
            "mqtt": {
                "topic_family": "zensdk_ha_scalar",
                "base_topic": None,
                "device_id": "EOD1NLN9P010902",
            },
            "capabilities": {
                "read_power": True,
                "read_soc": True,
                "write_output_limit": False,
            },
        },
    }
    if config_name_marker is not False:
        proposal["config_name"] = config_name_marker
    return proposal



class _ReleaseManager:
    def config_template(self):
        return {
            "tag": "test",
            "template": {
                "system": {"max_total_power": 1600},
                "devices": [
                    {
                        "name": "WR1",
                        "ip": "192.0.2.10",
                        "sn": "YOUR_SN",
                        "max_power": 800,
                    }
                ],
                "grid_meter": {"type": "shelly", "ip": "192.0.2.20"},
            },
        }


def test_backend_falls_back_for_missing_names_but_rejects_explicit_empty():
    generator = _generator()
    fallback = generator.generate(
        [_local("LOCAL-1", "192.0.2.11"), _meter()],
        1,
        zendure_mqtt_proposals=[_mqtt_proposal(False)],
    )
    assert [device["name"] for device in fallback["config"]["devices"]] == [
        "INV_1",
        "INV_2",
    ]
    assert "EOD1NLN9P010902" not in fallback["config"]["devices"][1]["name"]

    explicit_empty = generator.generate(
        [_local("LOCAL-1", "192.0.2.11", config_name="INV_1"), _meter()],
        1,
        zendure_mqtt_proposals=[_mqtt_proposal("")],
    )
    codes = {item["code"] for item in explicit_empty["validation"]["errors"]}
    assert explicit_empty["ready"] is False
    assert "zendure_mqtt_invalid" in codes


def test_backend_uses_selected_mqtt_config_name_not_description_or_serial():
    result = _generator().generate(
        [_meter()],
        1,
        zendure_mqtt_proposals=[_mqtt_proposal("INV_1")],
    )
    mqtt = next(
        device for device in result["config"]["devices"]
        if device.get("type") == "zendure_mqtt"
    )
    assert mqtt["name"] == "INV_1"
    assert mqtt["serial_number"] == "EOD1NLN9P010902"


def test_backend_manual_mqtt_uses_compact_name_without_serial_fallback():
    manual = {
        "serial_number": "MANUAL-SERIAL",
        "generation": "solarflow_zensdk",
    }
    result = _generator().generate(
        [_local("LOCAL-1", "192.0.2.11", config_name="INV_1"), _meter()],
        1,
        zendure_mqtt_broker={
            "name": "local_mqtt",
            "host": "192.0.2.30",
            "port": 1883,
        },
        zendure_mqtt_manual_devices=[manual],
    )
    mqtt = next(
        device for device in result["config"]["devices"]
        if device.get("type") == "zendure_mqtt"
    )
    assert mqtt["name"] == "INV_2"
    assert mqtt["serial_number"] == "MANUAL-SERIAL"
    assert "MANUAL-SERIAL" not in mqtt["name"]

    explicit_empty = _generator().generate(
        [_local("LOCAL-1", "192.0.2.11", config_name="INV_1"), _meter()],
        1,
        zendure_mqtt_broker={
            "name": "local_mqtt",
            "host": "192.0.2.30",
            "port": 1883,
        },
        zendure_mqtt_manual_devices=[dict(manual, name="")],
    )
    assert explicit_empty["ready"] is False
    assert "zendure_mqtt_invalid" in {
        issue["code"] for issue in explicit_empty["validation"]["errors"]
    }


def test_maintenance_backend_preserves_existing_name_and_defaults_only_new_items():
    from admin.maintenance_config import _merge_devices

    merged = {
        "devices": [
            {
                "name": "Roof West",
                "ip": "192.0.2.10",
                "sn": "EXISTING",
                "max_power": 800,
            }
        ]
    }
    draft = [
        {
            "kind": "local_api",
            "original_name": "Roof West",
            "name": "Roof West",
            "ip": "192.0.2.10",
            "sn": "EXISTING",
        },
        {
            "kind": "local_api",
            "original_name": None,
            "ip": "192.0.2.11",
            "sn": "NEW",
        },
    ]
    _merge_devices(merged, draft, [])
    assert [device["name"] for device in merged["devices"]] == [
        "Roof West",
        "INV_2",
    ]

    cleared = {
        "devices": [
            {"name": "Roof West", "ip": "192.0.2.10", "sn": "EXISTING"}
        ]
    }
    _merge_devices(
        cleared,
        [
            {
                "kind": "local_api",
                "original_name": "Roof West",
                "name": "",
                "ip": "192.0.2.10",
                "sn": "EXISTING",
            }
        ],
        [],
    )
    assert cleared["devices"][0]["name"] == ""
