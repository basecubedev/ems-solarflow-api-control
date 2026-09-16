# SPDX-License-Identifier: AGPL-3.0-or-later
"""What the Admin does not know, it does not touch.

There is no Admin flow for an external MQTT inverter: it is maintained in
config.json. That is a reason to leave it exactly as it is, not a reason to
treat it as the nearest thing the Admin does know.

Before this was pinned, the maintenance draft classified such an entry as a
local-API device and the preview wrote it back as one: the type gone, the
topics gone, an empty ip and a full set of Zendure control defaults invented
for hardware that has none. Saving anything on the maintenance page would have
destroyed the configuration of a device the page never showed.
"""

import copy
import json

import pytest

from admin.maintenance_config import build_maintenance_draft, preview_maintenance_config

pytestmark = [
    pytest.mark.admin,
    pytest.mark.contract,
    pytest.mark.simulation,
]


EXTERNAL = {
    "name": "Kostal Piko",
    "type": "external_mqtt",
    "mqtt": {
        "broker_ref": "house",
        "device_id": "EXAMPLE0000001",
        "topics": {"outputHomePower": "KostlPico/EXAMPLE0000001/solarPower"},
    },
}


def _config():
    return {
        "devices": [
            {"name": "WR1", "ip": "192.0.2.10", "sn": "SN1"},
            copy.deepcopy(EXTERNAL),
        ],
        "zendure_mqtt": {
            "brokers": {
                "house": {"enabled": True, "source": "local_mqtt", "host": "10.0.0.5"}
            }
        },
        "grid_meter": {"type": "shelly", "ip": "192.0.2.50"},
    }


def _install(base_dir, config):
    config_dir = base_dir / "config"
    config_dir.mkdir(exist_ok=True)
    (config_dir / "config.json").write_text(json.dumps(config), encoding="utf-8")


def _preview(tmp_path):
    """The preview merges the draft with the installed config, not the draft alone."""
    config = _config()
    _install(tmp_path, config)
    draft = build_maintenance_draft(copy.deepcopy(config))
    result = preview_maintenance_config(draft, base_dir=str(tmp_path))
    assert result.get("status") == "ok", result
    return result["preview"]


def test_the_maintenance_preview_returns_the_entry_unchanged(tmp_path):
    devices = _preview(tmp_path).get("devices", [])

    external = [device for device in devices if device.get("name") == "Kostal Piko"]
    assert external == [EXTERNAL], external


def test_the_other_devices_are_still_editable_beside_it(tmp_path):
    """Leaving one entry alone must not freeze the page."""
    devices = _preview(tmp_path)["devices"]
    wr1 = next(device for device in devices if device["name"] == "WR1")

    assert wr1["ip"] == "192.0.2.10"
    assert wr1["sn"] == "SN1"


def test_a_broker_used_only_by_an_external_device_is_not_pruned():
    """Same question as the credential scan: a profile used only by an external
    device must not look unreferenced and be dropped."""
    from admin.config_preview import _prune_unreferenced_new_brokers

    preview = _config()
    _prune_unreferenced_new_brokers(preview, set())

    assert "house" in preview["zendure_mqtt"]["brokers"]
