# SPDX-License-Identifier: AGPL-3.0-or-later
"""Maintenance offers a scanned E3/DC as grid meter and as read-only device.

The scan reports one E3/DC candidate. Maintenance gives it two review rows,
because the two roles are separate entries in the config: the grid meter row
sets the meter to the E3/DC's Modbus endpoint, the read-only device row adds a
device card for it. Neither row offers anything the EMS could control. The real
admin.js review builder, action and card renderer run against the DOM shim in
tests/js/maintenance_card_runner.js.
"""

import json
import os
import re
import shutil
import subprocess

import pytest

from admin import discovery
from admin.config_preview import _GRID_TYPES
from ems import config as cfg

pytestmark = [
    pytest.mark.admin,
    pytest.mark.maintenance,
    pytest.mark.setup,
    pytest.mark.contract,
]

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RUNNER = os.path.join(ROOT, "tests", "js", "maintenance_card_runner.js")
ADMIN_JS = os.path.join(ROOT, "admin", "static", "admin.js")

E3DC = {
    "id": "e3dc_modbus:S10-TEST",
    "observation_id": "obs-e3dc",
    "ip": "192.168.1.40",
    "port": 502,
    "protocol": "modbus_tcp",
    "api_family": "e3dc_modbus",
    "device_type": "e3dc_modbus",
    "role_suggestion": "grid_meter",
    "display_name": "E3/DC S10 E AIO",
    "model": "S10 E AIO",
    "serial_number": "S10-TEST",
    "config_ready": True,
    "usable_for_config": True,
}
SHELLY_METER = {"present": True, "type": "shelly", "ip": "192.168.1.3"}
SAVED_E3DC = {
    "kind": "e3dc_modbus",
    "original_name": "Keller",
    "name": "Keller",
    "ip": "192.168.1.40",
    "port": 502,
    "unit_id": 1,
    "enabled": True,
}


def _review(draft, discovered=(E3DC,), accept=None):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for the maintenance discovery review tests")
    request = {"card": "discovery_review", "draft": draft, "discovered": list(discovered)}
    if accept is not None:
        request["accept"] = accept
    result = subprocess.run(
        [node, RUNNER], input=json.dumps(request), text=True, capture_output=True, check=False
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def _rows(review, role):
    """The rows the E3/DC candidate produced for ``role``."""

    return [row for row in review["rows"] if row["role"] == role and row["discovered"] == E3DC["id"]]


def _index(review, role):
    return next(
        index
        for index, row in enumerate(review["rows"])
        if row["role"] == role and row["discovered"] == E3DC["id"]
    )


def test_a_scanned_e3dc_gets_a_grid_meter_row_and_a_read_only_device_row():
    review = _review({"devices": [], "grid_meter": SHELLY_METER})

    [meter] = _rows(review, "grid_meter")
    [device] = _rows(review, "read_only_device")
    assert meter["state"] == "new"
    assert meter["card"]["action"]["text"] == "Add as grid meter"
    assert device["state"] == "new"
    assert device["card"]["action"]["text"] == "Add as read-only device"
    assert device["card"]["action"]["disabled"] is False


def test_the_read_only_row_is_its_own_card():
    review = _review({"devices": [], "grid_meter": SHELLY_METER})

    meter, device = _rows(review, "grid_meter")[0], _rows(review, "read_only_device")[0]
    assert device["card"]["dataset"]["role"] == "read_only_device"
    assert device["card"]["dataset"]["sourceId"] != meter["card"]["dataset"]["sourceId"]
    assert device["card"]["transportPill"]["connection"] == "modbus_tcp"
    assert "Read-only device candidate" in device["card"]["text"]
    assert "never controls" in device["card"]["text"]


def test_adding_the_read_only_device_prefills_its_modbus_endpoint():
    draft = {"devices": [], "grid_meter": SHELLY_METER}
    review = _review(draft, accept=_index(_review(draft), "read_only_device"))

    assert review["accepted"] is True
    assert review["draft"]["devices"] == [
        {
            "kind": "e3dc_modbus",
            "original_name": None,
            "name": "E3DC",
            "ip": "192.168.1.40",
            "port": 502,
            "unit_id": 1,
            "enabled": True,
            "has_enabled_key": True,
        }
    ]
    assert review["draft"]["grid_meter"] == SHELLY_METER
    [device] = _rows({"rows": review["after"]}, "read_only_device")
    assert device["card"]["action"] == {
        "text": "Added to draft",
        "disabled": True,
        "classes": ["primary-button", "compact", "mconfig-discovery-add-button", "is-added"],
    }


def test_adding_the_grid_meter_takes_the_e3dc_type_and_port():
    draft = {"devices": [], "grid_meter": SHELLY_METER}
    review = _review(draft, accept=_index(_review(draft), "grid_meter"))

    assert review["accepted"] is True
    assert review["draft"]["grid_meter"] == {
        "present": True,
        "type": "e3dc_modbus",
        "ip": "192.168.1.40",
        "port": 502,
    }
    assert review["draft"]["devices"] == []


def test_a_saved_e3dc_device_on_that_endpoint_is_reported_in_config():
    review = _review({"devices": [dict(SAVED_E3DC)], "grid_meter": SHELLY_METER})

    [device] = _rows(review, "read_only_device")
    assert device["state"] == "found"
    assert device["configured"] == "Keller"
    assert device["card"]["action"]["text"] == "In config"


def test_a_saved_e3dc_device_the_scan_did_not_find_is_reported_not_found():
    review = _review({"devices": [dict(SAVED_E3DC)], "grid_meter": SHELLY_METER}, discovered=[])

    [row] = [row for row in review["rows"] if row["role"] == "read_only_device"]
    assert row["state"] == "missing"
    assert row["configured"] == "Keller"
    assert row["card"]["action"]["text"] == "Configured"
    assert row["card"]["action"]["disabled"] is True


@pytest.mark.parametrize(
    "change",
    [{"port": 5020}, {"unit_id": 2}, {"unit_id": 0}, {"ip": "e3dc.fritz.box"}, {"ip": ""}],
)
def test_a_saved_device_the_scan_cannot_reach_is_not_reported_not_found(change):
    """The scan probes port 502 at unit ID 1 on the /24 around an address only;
    another endpoint is not evidence of a missing system."""

    review = _review({"devices": [{**SAVED_E3DC, **change}], "grid_meter": SHELLY_METER}, discovered=[])

    assert [row for row in review["rows"] if row["role"] == "read_only_device"] == []


def test_two_not_found_devices_get_their_own_cards():
    second = {**SAVED_E3DC, "original_name": "Garage", "name": "Garage", "ip": "192.168.1.41"}
    review = _review({"devices": [dict(SAVED_E3DC), second], "grid_meter": SHELLY_METER}, discovered=[])

    ids = [row["card"]["dataset"]["sourceId"] for row in review["rows"] if row["role"] == "read_only_device"]
    assert len(ids) == 2
    assert len(set(ids)) == 2


def test_a_device_added_in_this_draft_is_never_reported_not_found():
    draft_only = {**SAVED_E3DC, "original_name": None}
    review = _review({"devices": [draft_only], "grid_meter": SHELLY_METER}, discovered=[])

    assert [row for row in review["rows"] if row["role"] == "read_only_device"] == []


@pytest.mark.parametrize("change", [{"ip": "192.168.1.41"}, {"port": 5020}])
def test_a_saved_e3dc_device_on_another_endpoint_leaves_the_candidate_new(change):
    review = _review({"devices": [{**SAVED_E3DC, **change}], "grid_meter": SHELLY_METER})

    [device] = _rows(review, "read_only_device")
    assert device["state"] == "new"
    saved = [row for row in review["rows"] if row["role"] == "read_only_device" and row["discovered"] is None]
    assert [row["state"] for row in saved] == (["missing"] if "ip" in change else [])


@pytest.mark.parametrize("change", [{"port": "", "unit_id": None}, {"port": "502", "unit_id": "1"}])
def test_a_saved_device_on_the_default_endpoint_is_reported_not_found(change):
    review = _review({"devices": [{**SAVED_E3DC, **change}], "grid_meter": SHELLY_METER}, discovered=[])

    assert [row["state"] for row in review["rows"] if row["role"] == "read_only_device"] == ["missing"]


def test_a_saved_device_with_a_typed_port_matches_the_candidate():
    review = _review({"devices": [{**SAVED_E3DC, "port": "502"}], "grid_meter": SHELLY_METER})

    assert _rows(review, "read_only_device")[0]["state"] == "found"


def test_an_e3dc_grid_meter_on_that_endpoint_is_in_config():
    meter = {"present": True, "type": "e3dc_modbus", "ip": "192.168.1.40", "port": 502}
    review = _review({"devices": [], "grid_meter": meter})

    assert _rows(review, "grid_meter")[0]["state"] == "found"
    assert _rows(review, "read_only_device")[0]["state"] == "new"


def test_another_meter_gets_no_read_only_device_row():
    shelly = {
        **E3DC,
        "id": "shelly_gen2:AABB",
        "observation_id": "obs-shelly",
        "port": 80,
        "api_family": "shelly_gen2",
        "device_type": "shelly_pro_3em",
    }
    review = _review({"devices": [], "grid_meter": {"present": False}}, discovered=[shelly])

    assert _rows(review, "read_only_device") == []


def test_the_browser_knows_the_endpoint_the_scan_probes():
    """Maintenance decides from these values whether a saved E3/DC the scan did
    not report is missing; they must be the ones the probe uses."""

    with open(ADMIN_JS, encoding="utf-8") as handle:
        source = handle.read()
    block = re.search(r"const E3DC_MODBUS_DEFAULTS = \{(.*?)\};", source, re.S)
    assert block, "E3DC_MODBUS_DEFAULTS not found in admin.js"
    mirrored = {key: int(value) for key, value in re.findall(r"([a-z_]+): (\d+)", block.group(1))}
    assert mirrored == {"port": discovery.E3DC_SCAN_PORT, "unit_id": cfg.E3DC_MODBUS_DEFAULT_UNIT_ID}
    assert discovery.E3DC_SCAN_PORT == cfg.E3DC_MODBUS_DEFAULT_PORT


def test_the_browser_family_map_is_the_preview_family_map():
    """Setup infers the meter type in the browser and in the preview; both must
    know every family the scan reports, the E3/DC included."""

    with open(ADMIN_JS, encoding="utf-8") as handle:
        source = handle.read()
    block = re.search(r"const GRID_METER_FAMILY_TYPES = \{(.*?)\};", source, re.S)
    assert block, "GRID_METER_FAMILY_TYPES not found in admin.js"
    mirrored = dict(re.findall(r'([a-z0-9_]+): "([a-z0-9_]+)"', block.group(1)))
    assert mirrored == _GRID_TYPES
    assert _GRID_TYPES["e3dc_modbus"] == "e3dc_modbus"
