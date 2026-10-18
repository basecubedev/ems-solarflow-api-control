# SPDX-License-Identifier: AGPL-3.0-or-later
"""The scheduler inside the running EMS, the dashboard and emsctl.

A due switch point is applied before the cycle reads its runtime values, so
the cycle it falls in already controls with it; a failing scheduler never
stops a cycle; the dashboard's routes need a session to read and a session
and CSRF token to change; emsctl schedule writes beside the runtime state.
"""

import json
import logging
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

import emsctl
from dashboard.auth import write_password_file
from ems.control_status import ControlStatusWriter
from ems.config import device_identities
from ems.controller import EMSController
from ems.paths import resolve_control_status_path, resolve_schedules_path
from ems.runtime_commands import build_validation_context
from ems.runtime_state import RuntimeState, build_runtime_defaults
from ems.schedule_store import ScheduleStore
from ems.scheduler import Scheduler
from tests._emsctl_test_helpers import run_emsctl
from tests.test_control_status_snapshot import MeteredGrid
from tests.test_dashboard_server import StoreStub, json_response, with_server
from tests.test_write_gates import device, state

pytestmark = [pytest.mark.contract, pytest.mark.power_control, pytest.mark.simulation]

CONFIG = {"system": {"max_total_power": 800}, "devices": [{"name": "WR1", "sn": "SN-TEST-0001", "max_power": 800}]}


def _controller(tmp_path, scheduler_for):
    dev = device("WR1")
    with patch("ems.runtime_state.cfg.CONFIG", {"devices": [{"name": "WR1"}]}):
        runtime_state = RuntimeState(str(tmp_path / "runtime-state.json"), build_runtime_defaults([dev]))
        runtime_state.load_or_create()
    controller = EMSController(
        devices=[dev],
        shelly=MeteredGrid(240),
        sleep_enabled=False,
        runtime_state=runtime_state,
        scheduler=scheduler_for(runtime_state),
    )
    controller.set_output_limit = Mock()
    return controller, dev


def _cycle(controller):
    with patch(
        "ems.controller.fetch_all_devices",
        return_value=[state(output=100, output_limit=100, pack_in=100)],
    ), patch("ems.controller.cfg.SYSTEM_ENABLED", True), patch(
        "ems.controller.cfg.MAX_TOTAL_POWER", 800
    ), patch("ems.controller.cfg.SOC_RECONCILE_INTERVAL", 0), patch(
        "ems.controller.cfg.MIN_OUTPUT_LIMIT", 0
    ):
        controller.run_once()


def test_the_cycle_a_switch_point_falls_in_already_controls_with_its_value(tmp_path):
    now = datetime.now(timezone.utc)
    store = ScheduleStore(resolve_schedules_path(tmp_path / "runtime-state.json"),
                          clock=lambda: now - timedelta(minutes=10))
    store.create({
        "name": "Midday",
        "target": {"scope": "device", "device": "WR1"},
        "action": {"id": "device_output_limit", "value": 300},
        "when": {"start": (now - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M")},
    }, by="dashboard")
    controller, dev = _controller(
        tmp_path,
        lambda runtime_state: Scheduler(store, runtime_state, zone=timezone.utc,
                                        validation_context=build_validation_context(CONFIG)),
    )

    _cycle(controller)

    assert dev.max_power == 300
    assert json.loads((tmp_path / "runtime-state.json").read_text())["devices"]["WR1"]["max_power"] == 300
    assert store.load().applied["devices.WR1.max_power"]["value"] == 300


def test_a_failing_scheduler_is_logged_and_the_cycle_still_runs(tmp_path, caplog):
    failing = SimpleNamespace(tick=Mock(side_effect=RuntimeError("store on fire")))
    controller, dev = _controller(tmp_path, lambda runtime_state: failing)

    with caplog.at_level(logging.WARNING):
        _cycle(controller)

    assert failing.tick.called
    assert controller.last_control_explanation is not None
    assert any("event=schedule_tick_failed" in record.getMessage() for record in caplog.records)


def test_without_a_scheduler_the_cycle_is_unchanged(tmp_path):
    controller, dev = _controller(tmp_path, lambda runtime_state: None)

    _cycle(controller)

    assert dev.max_power == 800


def test_the_control_snapshot_carries_what_the_scheduler_holds(tmp_path):
    status = {"loaded": True, "next": [], "applied": {}}
    controller, _ = _controller(tmp_path, lambda runtime_state: SimpleNamespace(
        tick=Mock(), status=Mock(return_value=status)))
    _cycle(controller)
    writer = ControlStatusWriter(resolve_control_status_path(tmp_path / "runtime-state.json"))

    assert writer.write(controller)

    assert json.loads(writer.path.read_text())["schedule"] == status


class _Api:
    def __init__(self):
        self.calls = []

    def handle(self, method, path, body=None, query=""):
        self.calls.append((method, path, body, query))
        return 201, {"answered": method}


def test_the_schedule_routes_need_a_session_to_read_and_csrf_to_change(tmp_path):
    auth_file = tmp_path / "dashboard-auth.json"
    write_password_file(auth_file, "secret-password")
    api = _Api()
    server, base_url = with_server(StoreStub(), auth_file=str(auth_file), schedule_api=api)
    try:
        assert json_response(f"{base_url}/api/schedule")[0] == 401
        _, headers, login = json_response(f"{base_url}/api/auth/login", method="POST",
                                          payload={"password": "secret-password"})
        cookie = {"Cookie": headers["Set-Cookie"]}

        assert json_response(f"{base_url}/api/schedule", headers=cookie)[2] == {"answered": "GET"}
        status, _, payload = json_response(f"{base_url}/api/schedule/rules", method="POST",
                                           payload={"rule": {}}, headers=cookie)
        assert (status, payload["error"]) == (403, "csrf_failed")
        with_csrf = {**cookie, "X-CSRF-Token": login["csrf_token"]}
        for method, path in (("POST", "/api/schedule/rules"), ("PUT", "/api/schedule/rules/r_00000001"),
                             ("PATCH", "/api/schedule"), ("DELETE", "/api/schedule/rules/r_00000001?revision=2")):
            status, _, payload = json_response(f"{base_url}{path}", method=method, payload={"x": 1},
                                               headers=with_csrf)
            assert (status, payload) == (201, {"answered": method})
        assert api.calls[-1] == ("DELETE", "/api/schedule/rules/r_00000001", {"x": 1}, "revision=2")
    finally:
        server.shutdown()
        server.server_close()


def test_without_a_scheduler_the_routes_say_so_after_authentication(tmp_path):
    auth_file = tmp_path / "dashboard-auth.json"
    write_password_file(auth_file, "secret-password")
    server, base_url = with_server(StoreStub(), auth_file=str(auth_file))
    try:
        assert json_response(f"{base_url}/api/schedule")[0] == 401
        _, headers, _ = json_response(f"{base_url}/api/auth/login", method="POST",
                                      payload={"password": "secret-password"})
        status, _, payload = json_response(f"{base_url}/api/schedule", headers={"Cookie": headers["Set-Cookie"]})
        assert (status, payload) == (404, {"error": "schedule_unavailable"})
    finally:
        server.shutdown()
        server.server_close()


def test_emsctl_schedule_writes_beside_the_runtime_state_as_its_owner(tmp_path):
    added = run_emsctl(tmp_path, "schedule", "add", "--name", "Evening socket", "--device", "WR1",
                       "--action", "offgrid_socket", "--value", "standard", "--from", "18:00", "--to", "23:00",
                       "--end-value", "off", "--repeat", "weekdays")
    listed = run_emsctl(tmp_path, "schedule", "list")
    refused = run_emsctl(tmp_path, "schedule", "add", "--name", "x", "--device", "WR9", "--action",
                         "offgrid_socket", "--value", "eco", "--at", "23:59")

    assert added.returncode == 0, added.stderr
    assert json.loads((tmp_path / "schedules.json").read_text())["rules"][0]["name"] == "Evening socket"
    assert "Evening socket" in listed.stdout
    assert refused.returncode != 0
    assert "schedule add: unknown device WR9; known devices: WR1" in refused.stdout + refused.stderr
    assert "schedule" in emsctl.RUNTIME_EDIT_COMMANDS


def _mqtt_control(name, **identity):
    return {
        "type": "zendure_mqtt",
        "enabled": True,
        "name": name,
        "mqtt": {"topic_family": "zensdk_ha_scalar", "base_topic": "Zendure", "product_key": "PK1",
                 "device_id": identity.pop("device_id")},
        "capabilities": {"read_power": True, "read_soc": True, "write_output_limit": True},
        **identity,
    }


MIXED_DEVICES = [
    {"name": "WR1", "ip": "192.0.2.20", "sn": " SN-TEST-0001 "},
    _mqtt_control("WR2", device_id="DEV2", serial_number="SN-TEST-0002"),
    _mqtt_control("WR3", device_id="DEV3", sn="SN-TEST-0003", serial_number="SN-OTHER-0003"),
    _mqtt_control("WR4", device_id="DEV4"),
]


def test_emsctl_and_the_ems_name_devices_to_the_scheduler_alike(tmp_path):
    """A rule follows its device through a rename by the serial it was saved
    with. The EMS builds one map for its scheduler and its dashboard with
    ems.config.device_identities; emsctl must read the same serials."""

    config = {"system": {"max_total_power": 800}, "devices": MIXED_DEVICES}
    (tmp_path / "config.json").write_text(json.dumps(config))
    args = emsctl.build_parser().parse_args(["--config", str(tmp_path / "config.json"), "--runtime-state",
                                             str(tmp_path / "runtime-state.json"), "schedule", "list"])
    with patch("emsctl.schedule_cli.handle_schedule_command", return_value=0) as handled:
        emsctl.handle_schedule_command(args, config)

    ems_map = device_identities([item["name"] for item in MIXED_DEVICES], MIXED_DEVICES)
    expected = {"WR1": "SN-TEST-0001", "WR2": "SN-TEST-0002", "WR3": "SN-TEST-0003", "WR4": "DEV4"}
    assert handled.call_args.kwargs["devices"] == ems_map == expected


def test_the_scheduler_names_devices_by_the_map_it_was_built_with(tmp_path):
    store = ScheduleStore(tmp_path / "schedules.json")
    renamed = store.create({
        "name": "Socket",
        "target": {"scope": "device", "device": "Garage", "identity": "SN-TEST-0001"},
        "action": {"id": "offgrid_socket", "value": "standard"},
        "when": {"start": "2026-10-19T18:00"},
    }, by="dashboard")
    runtime_state = RuntimeState(str(tmp_path / "runtime-state.json"), {"devices": {}})
    scheduler = Scheduler(store, runtime_state, zone=timezone.utc, validation_context={},
                          devices={"WR1": "SN-TEST-0001"})

    scheduler.tick(datetime(2026, 10, 19, 8, 0, tzinfo=timezone.utc))

    assert store.load().rule(renamed.id).device == "WR1"


def test_a_scheduler_that_keeps_failing_is_logged_once_per_failure(tmp_path, caplog):
    failing = SimpleNamespace(tick=Mock(side_effect=RuntimeError("store on fire")))
    controller, _ = _controller(tmp_path, lambda runtime_state: failing)

    with caplog.at_level(logging.WARNING):
        for _ in range(3):
            controller.tick_scheduler()
        failing.tick.side_effect = None
        controller.tick_scheduler()
        failing.tick.side_effect = RuntimeError("store on fire")
        controller.tick_scheduler()

    assert len([r for r in caplog.records if "event=schedule_tick_failed" in r.getMessage()]) == 2


def test_emsctl_schedule_status_does_not_pass_off_a_stale_snapshot_as_the_running_ems(tmp_path):
    from ems.control_status import CONTROL_STATUS_SCHEMA_VERSION

    run_emsctl(tmp_path, "status")
    resolve_control_status_path(tmp_path / "runtime-state.json").write_text(json.dumps({
        "schema_version": CONTROL_STATUS_SCHEMA_VERSION,
        "written_at": "2026-01-01T00:00:00Z",
        "loop_interval_s": 5,
        "schedule": {"loaded": True, "zone": "Europe/Berlin", "next": [], "applied": {}},
    }))

    result = run_emsctl(tmp_path, "schedule", "status")

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "no running EMS has reported its schedule recently"
