# SPDX-FileCopyrightText: 2026 basecubedev
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Admin Maintenance marks a PV input empty on every device kind that reads its inputs.

A Zendure device takes ``unused_pv_inputs`` through the catalogue's common
device fields; an external MQTT device, an inverter an OpenDTU reads and an
E3/DC, whose inverter reports its two strings, take it beside the few keys their
editors own. The open hints of the running EMS reach the page with the
maintenance view, minus those the saved config has already settled.
"""

import copy
import json
from pathlib import Path

import pytest

from admin.config_runtime_overlap import resolve_runtime_state_path
from admin.ems_device_states import open_pv_input_hints, pv_input_hints
from admin.maintenance_config import _READ_ONLY_KINDS, load_maintenance_config, prepare_maintenance_config_apply
from admin.test_support import write_started_ems_snapshot
from ems.paths import resolve_control_status_path
from ems.read_only_devices import READ_ONLY_DEVICE_TYPES

pytestmark = [
    pytest.mark.admin,
    pytest.mark.maintenance,
    pytest.mark.config,
    pytest.mark.contract,
]

FRONTEND_DEVICES = Path(__file__).resolve().parents[1] / "frontend" / "admin-modern" / "src" / "lib" / "devices.js"


@pytest.fixture(autouse=True)
def _isolate(isolated_install_root):
    return isolated_install_root


_ZENDURE = {"name": "WR1", "ip": "192.168.1.100", "sn": "AAA", "max_power": 800}
_EXTERNAL = {
    "name": "Garage",
    "type": "external_mqtt",
    "mqtt": {"broker_ref": "fhem", "topic_family": "ems_solarflow", "device_id": "garage"},
}
_OPENDTU = {"name": "DTU", "type": "opendtu_http", "ip": "192.168.1.70"}
_ZENDURE_MQTT = {
    "type": "zendure_mqtt",
    "name": "Telemetry only",
    "enabled": True,
    "serial_number": "MQTT1",
    "mqtt": {"broker_ref": "fhem", "topic_family": "zensdk_ha_scalar", "device_id": "MQTT1"},
    "capabilities": {"read_power": True, "read_soc": True, "write_output_limit": False},
}
_E3DC = {"name": "E3DC", "type": "e3dc_modbus", "ip": "192.168.1.40"}


def _every_kind():
    return [dict(_ZENDURE), dict(_EXTERNAL), dict(_OPENDTU), copy.deepcopy(_ZENDURE_MQTT), dict(_E3DC)]


def _write_devices(base_dir, devices):
    config_dir = base_dir / "config"
    config_dir.mkdir(exist_ok=True)
    (config_dir / "config.json").write_text(
        json.dumps(
            {
                "system": {"max_total_power": 800},
                "devices": devices,
                "grid_meter": {"type": "shelly", "ip": "192.168.1.3"},
                "zendure_mqtt": {"brokers": {"fhem": {"enabled": True, "source": "local_mqtt", "host": "192.168.1.71", "port": 1883}}},
            }
        ),
        encoding="utf-8",
    )


IDENTITY_KEY = b"pv-inputs-identity-key-32-bytes!"


def _load(tmp_path):
    return load_maintenance_config(base_dir=str(tmp_path), identity_token_key=IDENTITY_KEY)


def _apply(tmp_path, change):
    loaded = _load(tmp_path)
    draft = loaded["draft"]
    change(draft["devices"])
    return prepare_maintenance_config_apply(draft, loaded["revision"], base_dir=str(tmp_path), identity_token_key=IDENTITY_KEY)


def _written(prepared):
    assert prepared["status"] == "ok", prepared
    return json.loads(prepared["payload"])["devices"]


@pytest.mark.parametrize("index", [0, 1, 2, 3, 4])
def test_each_kind_that_reads_its_inputs_takes_an_empty_one(tmp_path, index):
    _write_devices(tmp_path, _every_kind())

    written = _written(_apply(tmp_path, lambda devices: devices[index].update({"unused_pv_inputs": [3, 1]})))

    assert sorted(written[index]["unused_pv_inputs"]) == [1, 3]


@pytest.mark.parametrize("index", [0, 1, 2, 3, 4])
def test_a_stored_null_stays_on_a_no_op_apply(tmp_path, index):
    devices = _every_kind()
    devices[index]["unused_pv_inputs"] = None
    _write_devices(tmp_path, devices)

    assert _written(_apply(tmp_path, lambda devices: None))[index] == devices[index]


@pytest.mark.parametrize("index", [0, 1, 2, 3, 4])
def test_the_draft_shows_what_the_entry_names_and_an_untouched_entry_stays_as_stored(tmp_path, index):
    devices = _every_kind()
    devices[index]["unused_pv_inputs"] = [4, 1]
    _write_devices(tmp_path, devices)

    assert _load(tmp_path)["draft"]["devices"][index]["unused_pv_inputs"] == [4, 1]
    assert _written(_apply(tmp_path, lambda devices: None))[index] == devices[index]


@pytest.mark.parametrize("index", [1, 2, 4])
def test_no_empty_input_left_removes_the_key_and_a_stored_empty_list_stays(tmp_path, index):
    devices = _every_kind()
    devices[index]["unused_pv_inputs"] = [1]
    _write_devices(tmp_path, devices)
    assert "unused_pv_inputs" not in _written(_apply(tmp_path, lambda devices: devices[index].update({"unused_pv_inputs": []})))[index]

    devices[index]["unused_pv_inputs"] = []
    _write_devices(tmp_path, devices)
    assert _written(_apply(tmp_path, lambda devices: None))[index]["unused_pv_inputs"] == []


@pytest.mark.parametrize("index", [0, 1, 2, 3, 4])
@pytest.mark.parametrize("value", [[5], [0, 1], ["x"]])
def test_an_input_number_outside_one_to_four_is_refused(tmp_path, index, value):
    _write_devices(tmp_path, _every_kind())

    prepared = _apply(tmp_path, lambda devices: devices[index].update({"unused_pv_inputs": value}))

    assert prepared["status"] != "ok"
    assert "unused_pv_inputs" in json.dumps(prepared)


def test_the_editor_offers_the_inputs_on_exactly_the_kinds_the_server_takes_them_for():
    source = FRONTEND_DEVICES.read_text(encoding="utf-8")
    offered = source.split("export const PV_INPUT_KINDS = Object.freeze([", 1)[1].split("]", 1)[0]
    kinds = {part.strip().strip('"') for part in offered.split(",") if part.strip()}

    read_only = {kind for kind in READ_ONLY_DEVICE_TYPES if _READ_ONLY_KINDS[kind].pv_inputs}
    assert kinds == {"local_api", "zendure_mqtt", "external_mqtt", *read_only}


def test_a_hint_is_open_until_the_saved_config_settles_it():
    config = {"devices": [{"name": "WR1", "unused_pv_inputs": [2]}, {"name": "DTU"}]}
    hints = [
        {"device": "WR1", "input": 1, "kind": "dark", "days": 3},
        {"device": "WR1", "input": 2, "kind": "dark", "days": 4},
        {"device": "WR1", "input": 2, "kind": "delivers", "peak_w": 180},
        {"device": "WR1", "input": 3, "kind": "delivers", "peak_w": 90},
        {"device": "DTU", "input": 1, "kind": "dark", "days": 3},
        {"device": "GONE", "input": 1, "kind": "dark", "days": 3},
        {"device": "WR1", "input": 9, "kind": "dark", "days": 3},
        {"device": "WR1", "input": 1, "kind": "other"},
        "junk",
    ]

    assert open_pv_input_hints(hints, config) == [hints[0], hints[2], hints[4]]
    assert open_pv_input_hints(None, config) == []


def test_the_open_hints_of_a_running_ems_reach_the_maintenance_view(tmp_path, isolated_install_root):
    from admin.install_context import detect_install_context

    _write_devices(isolated_install_root, [dict(_ZENDURE)])
    context = detect_install_context()
    write_started_ems_snapshot(context, pv_input_hints=[{"device": "WR1", "input": 1, "kind": "dark", "days": 3}])

    assert pv_input_hints(context) == [{"device": "WR1", "input": 1, "kind": "dark", "days": 3}]


def test_a_stopped_ems_offers_no_hint(isolated_install_root):
    from admin.install_context import detect_install_context

    _write_devices(isolated_install_root, [dict(_ZENDURE)])
    context = detect_install_context()
    write_started_ems_snapshot(context, pv_input_hints=[{"device": "WR1", "input": 1, "kind": "dark", "days": 3}])
    status = Path(resolve_control_status_path(resolve_runtime_state_path(context, json.loads(context.config_path.read_text()))))
    snapshot = json.loads(status.read_text())
    snapshot["written_at"] = "2026-01-01T00:00:00+00:00"
    status.write_text(json.dumps(snapshot))

    assert pv_input_hints(context) == []


def test_a_device_without_the_key_is_not_called_incomplete_for_it(tmp_path):
    _write_devices(tmp_path, [dict(_ZENDURE)])

    prepared = _apply(tmp_path, lambda devices: None)

    assert "unused_pv_inputs" not in json.dumps(prepared.get("validation", {}).get("warnings", []))
    assert any(issue["code"] == "device_common_values_missing" for issue in prepared["validation"]["warnings"])


@pytest.mark.parametrize("index", [0, 1, 2, 3, 4])
@pytest.mark.parametrize("stored,read", [("1,3", []), ([1.0, 2], [2]), ([5, 1], [1])])
def test_a_value_the_ems_cannot_read_in_full_is_shown_and_saved_as_what_it_reads(tmp_path, index, stored, read):
    from ems.pv_inputs import unused_pv_inputs

    devices = _every_kind()
    devices[index]["unused_pv_inputs"] = stored
    _write_devices(tmp_path, devices)

    assert _load(tmp_path)["draft"]["devices"][index]["unused_pv_inputs"] == read
    written = _written(_apply(tmp_path, lambda devices: None))[index]
    assert sorted(unused_pv_inputs(written)) == read
    assert written.get("unused_pv_inputs", []) == read


def test_a_hint_of_a_shape_this_cannot_read_is_dropped_and_each_is_offered_once():
    config = {"devices": [{"name": "WR1"}]}
    good = {"device": "WR1", "input": 1, "kind": "dark", "days": 3}
    hints = [
        {"device": ["WR1"], "input": 1, "kind": "dark", "days": 3},
        {"device": "WR1", "input": 2, "kind": "dark"},
        {"device": "WR1", "input": 2, "kind": "dark", "days": "3"},
        {"device": "WR1", "input": True, "kind": "dark", "days": 3},
        good,
        dict(good),
    ]

    assert open_pv_input_hints(hints, config) == [good]


def test_the_maintenance_view_carries_the_open_hints(isolated_install_root):
    from admin.install_context import detect_install_context
    from tests.test_admin_maintenance_config import _get, _server

    _write_devices(isolated_install_root, [dict(_ZENDURE, unused_pv_inputs=[2])])
    write_started_ems_snapshot(
        detect_install_context(),
        pv_input_hints=[{"device": "WR1", "input": 1, "kind": "dark", "days": 3}, {"device": "WR1", "input": 2, "kind": "dark", "days": 5}],
    )
    srv, base = _server()
    try:
        status, view = _get(f"{base}/api/admin/maintenance/config")
    finally:
        srv.shutdown()
        srv.server_close()

    assert status == 200
    assert view["pv_input_hints"] == [{"device": "WR1", "input": 1, "kind": "dark", "days": 3}]
