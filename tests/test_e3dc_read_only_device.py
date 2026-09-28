# SPDX-License-Identifier: AGPL-3.0-or-later
"""An E3/DC in ``devices[]`` is shown, never controlled.

The one property that must never slip: an ``e3dc_modbus`` entry reaches no
control, write or reconciliation path. Before this type existed, every entry
that was not ``zendure_mqtt`` counted as a local-API inverter, so an unknown
type would have been handed a ``ZendureClient`` and an ``outputLimit`` write.
The tests below pin that every such decision asks the read-only predicate, and
that the device's PV, battery, inverter and grid values reach the dashboard
through the same side door as telemetry-only MQTT devices, read in the same
control cycle and over the same connection as an E3/DC grid meter.
"""

import importlib.util
import os
import shutil
import subprocess
import textwrap
from pathlib import Path
from types import SimpleNamespace

import pytest

from dashboard.telemetry import build_dashboard_snapshot
from ems import config as cfg
from ems import e3dc_modbus as modbus
from ems.clients import create_grid_meter_client
from ems.controller import EMSController
from ems.e3dc_runtime import E3dcModbusSession, E3dcSessions, build_e3dc_device_runtime
from ems.read_only_devices import is_read_only_device_config
from ems.zendure_mqtt.config_entries import has_runtime_control_device
from tests.test_write_gates import RuntimeStateStub, ShellyStub

pytestmark = [
    pytest.mark.power_control,
    pytest.mark.unit,
]

ROOT = Path(__file__).resolve().parents[1]

_PROBE_PATH = ROOT / "scripts" / "e3dc_modbus_probe.py"
_spec = importlib.util.spec_from_file_location("e3dc_modbus_probe_for_device", _PROBE_PATH)
probe = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(probe)

ZENDURE = {"name": "WR1", "ip": "192.0.2.1", "sn": "SN1", "max_power": 800}


def _e3dc(**values):
    return {"name": "E3DC", "type": "e3dc_modbus", "ip": "192.0.2.60", **values}


@pytest.fixture
def device():
    with probe._LoopbackE3dcServer(probe.build_self_test_words()) as server:
        yield server


# --- never controlled -----------------------------------------------------------


def test_an_e3dc_entry_never_becomes_an_http_control_device():
    assert cfg.http_control_device_configs([ZENDURE, _e3dc()]) == [ZENDURE]


def test_an_e3dc_entry_has_no_write_gate():
    grouped = cfg.config_control_devices_by_gate({"devices": [ZENDURE, _e3dc()]})
    names = [item["name"] for items in grouped.values() for item in items]
    assert names == ["WR1"]


def test_an_e3dc_alone_does_not_make_a_config_bootable():
    assert has_runtime_control_device({"devices": [_e3dc()]}) is False
    assert has_runtime_control_device({"devices": [_e3dc(), ZENDURE]}) is True


def test_the_predicate_ignores_case_and_rejects_non_entries():
    assert is_read_only_device_config({"type": " E3DC_Modbus "}) is True
    assert is_read_only_device_config({"type": "zendure_mqtt"}) is False
    assert is_read_only_device_config({}) is False
    assert is_read_only_device_config("e3dc_modbus") is False


def test_an_e3dc_device_needs_an_address_but_no_serial():
    config = {"devices": [_e3dc()], "grid_meter": {"type": "shelly", "ip": "192.0.2.5"}}
    assert cfg.template_placeholder_paths(config) == []
    config["devices"][0]["ip"] = "192.168.1.50"
    assert cfg.template_placeholder_paths(config) == ["devices[0].ip"]


def test_the_config_upgrade_invents_no_zendure_values_for_an_e3dc():
    upgraded = cfg._merge_template_upgrade_view(
        {"devices": []},
        {"devices": [_e3dc(), dict(ZENDURE)]},
        {"smart_mode": 1, "min_soc": 15, "max_power": 800},
    )
    assert upgraded["devices"][0] == _e3dc()
    assert upgraded["devices"][1]["smart_mode"] == 1


# --- one connection, one request per cycle --------------------------------------


class _RecordingClient(modbus.ReadOnlyModbusTcpClient):
    reads = []

    def read_registers(self, address, count, function_code=3, unit_id=None):
        _RecordingClient.reads.append((count, function_code))
        return super().read_registers(address, count, function_code, unit_id)


class _FrozenClock:
    now = 1000.0

    def __call__(self):
        return self.now


def _recording_sessions(clock=None):
    clock = clock or _FrozenClock()

    def factory(host, *, port, unit_id):
        return E3dcModbusSession(
            host,
            port=port,
            unit_id=unit_id,
            modbus_client_factory=_RecordingClient,
            clock=clock,
        )

    return E3dcSessions(session_factory=factory)


def test_meter_and_device_for_one_e3dc_share_one_request_per_cycle(device):
    _RecordingClient.reads = []
    sessions = _recording_sessions()
    endpoint = {"ip": "127.0.0.1", "port": device.port}
    try:
        meter = create_grid_meter_client(
            {"type": "e3dc_modbus", **endpoint}, session=None, e3dc_sessions=sessions
        )
        runtime = build_e3dc_device_runtime([_e3dc(**endpoint)], sessions)
        assert meter.get_power() == -600.0
        runtime.refresh()
    finally:
        sessions.close()
    power_reads = [read for read in _RecordingClient.reads if read[0] == modbus.POWER_COUNT]
    inverter_reads = [read for read in _RecordingClient.reads if read[0] == modbus.INVERTER_COUNT]
    assert len(power_reads) == 1
    assert len(inverter_reads) == 1
    assert {code for _, code in _RecordingClient.reads} <= modbus.READ_ONLY_FUNCTION_CODES


def test_a_disabled_or_foreign_entry_builds_no_runtime():
    sessions = E3dcSessions()
    try:
        assert build_e3dc_device_runtime([ZENDURE], sessions) is None
        assert build_e3dc_device_runtime([_e3dc(enabled=False)], sessions) is None
    finally:
        sessions.close()


def test_an_invalid_e3dc_entry_is_refused_at_startup():
    with pytest.raises(ValueError, match="unit_id must be in 0..255"):
        build_e3dc_device_runtime([_e3dc(unit_id=999)], E3dcSessions())


# --- the tile -------------------------------------------------------------------


def _refreshed_runtime(server):
    sessions = E3dcSessions()
    runtime = build_e3dc_device_runtime(
        [_e3dc(ip="127.0.0.1", port=server.port)], sessions
    )
    runtime.refresh()
    return runtime, sessions


def test_the_tile_carries_pv_battery_inverter_and_grid(device):
    runtime, sessions = _refreshed_runtime(device)
    try:
        [tile] = runtime.tiles()
    finally:
        sessions.close()
    state = tile["state"]
    assert tile["name"] == "E3DC"
    assert tile["online"] is True
    assert state.solar == 4321.0
    assert state.soc == 87.0
    assert state.output == 1234 + 1200 + 1190
    assert (state.pack_in, state.pack_out) == (1500.0, 0.0)
    assert tile["fields"] == {
        "grid_power_w": -600.0,
        "output_limit_applicable": False,
        "firmware_status_applicable": False,
    }


def test_a_device_that_was_never_read_is_an_offline_tile_of_zeros():
    sessions = E3dcSessions()
    try:
        runtime = build_e3dc_device_runtime([_e3dc()], sessions)
        [tile] = runtime.tiles()
    finally:
        sessions.close()
    assert tile["online"] is False
    assert tile["state"].solar == 0
    assert tile["fields"]["grid_power_w"] is None


def test_a_failed_cycle_keeps_the_last_values_offline(device):
    runtime, sessions = _refreshed_runtime(device)
    session = sessions.session("127.0.0.1", device.port, 1)
    try:
        device.close()
        session._clock = lambda: session.last_reading.taken_monotonic + 5
        runtime.refresh()
        [tile] = runtime.tiles()
    finally:
        sessions.close()
    assert tile["online"] is False
    assert tile["state"].solar == 4321.0


def _controller(read_only_devices):
    return SimpleNamespace(
        devices=[SimpleNamespace(name="WR1")],
        runtime_state=None,
        device_online={"WR1": True},
        commanded_total_w=0,
        filtered_load_w=0,
        _dashboard_capabilities=[],
        zendure_mqtt_runtime=None,
        read_only_devices=read_only_devices,
    )


def _control_state():
    return SimpleNamespace(solar=400, output=300, pack_out=110, pack_in=10, soc=60)


def _snapshot(controller, load_w=-600):
    return build_dashboard_snapshot(
        controller,
        load_w=load_w,
        states=[_control_state()],
        targets=[300],
        effective_targets=[300],
        allocated_total_w=300,
        effective_total_w=300,
        enabled=True,
        max_total_power=1600,
        min_output_limit=35,
    )


def test_the_dashboard_shows_the_e3dc_read_only_and_counts_it_in_the_totals(device):
    runtime, sessions = _refreshed_runtime(device)
    try:
        snapshot = _snapshot(_controller(runtime))
    finally:
        sessions.close()
    tile = snapshot["devices"]["E3DC"]
    assert tile["read_only"] is True
    assert tile["online"] is True
    assert tile["pv_input_w"] == 4321.0
    assert tile["output_w"] == 3624.0
    assert tile["battery_power_w"] == -1500.0
    assert tile["soc"] == 87.0
    assert tile["grid_power_w"] == -600.0
    assert tile["output_limit_w"] == 0
    assert tile["output_limit_applicable"] is False
    assert tile["target_w"] == 0
    assert tile["capability"] is None
    assert snapshot["pv_total_w"] == 400 + 4321
    assert snapshot["inverter_output_w"] == 300 + 3624
    assert snapshot["battery_power_w"] == 100 - 1500
    assert snapshot["grid_power_w"] == -600


def test_an_offline_e3dc_keeps_its_cycle_out_of_the_energy_statistics():
    sessions = E3dcSessions()
    try:
        runtime = build_e3dc_device_runtime([_e3dc()], sessions)
        snapshot = _snapshot(_controller(runtime))
    finally:
        sessions.close()
    assert snapshot["devices"]["E3DC"]["online"] is False
    assert snapshot["device_power_valid"] is False


# --- the control cycle ------------------------------------------------------------


class _StopCycle(BaseException):
    """Ends ``run_once`` right after the read-only refresh, whatever follows."""


def test_the_control_cycle_reads_the_devices_right_after_the_grid_meter():
    calls = []

    class Meter(ShellyStub):
        def get_power(self):
            calls.append("grid")
            return super().get_power()

    class Runtime:
        def refresh(self):
            calls.append("read_only")
            raise _StopCycle()

    controller = EMSController(
        devices=[],
        shelly=Meter(0),
        sleep_enabled=False,
        runtime_state=RuntimeStateStub(),
        read_only_devices=Runtime(),
    )
    with pytest.raises(_StopCycle):
        controller.run_once()
    assert calls == ["grid", "read_only"]


def test_a_failing_refresh_never_breaks_the_control_cycle():
    class Runtime:
        def refresh(self):
            raise RuntimeError("socket gone")

    controller = EMSController(
        devices=[],
        shelly=ShellyStub(0),
        sleep_enabled=False,
        runtime_state=RuntimeStateStub(),
        read_only_devices=Runtime(),
    )
    controller.refresh_read_only_devices()


def test_no_read_only_device_ever_joins_the_controlled_set():
    source = (ROOT / "ems-solarflow-api-control.py").read_text(encoding="utf-8")
    assert "cfg.http_control_device_configs()" in source
    assert "read_only_devices=e3dc_devices" in source
    assert "e3dc_sessions.close()" in source


# --- the frontend tile ------------------------------------------------------------


def test_frontend_renders_grid_and_no_limit_on_the_e3dc_tile():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for the executable dashboard render test")

    script = textwrap.dedent(
        """
        const fs = require("fs");
        const vm = require("vm");
        const appPath = process.argv[2];
        const source = fs.readFileSync(appPath, "utf8")
          .split('document.querySelectorAll(".range-tabs button")')[0];

        class FakeElement {
          constructor(id = "") {
            this.id = id;
            this.textContent = "";
            this.innerHTML = "";
            this.className = "";
            this.children = [];
            this.dataset = {};
            this.style = { setProperty: () => {} };
          }
          setAttribute() {}
          appendChild(child) { this.children.push(child); }
          querySelectorAll() { return []; }
        }

        const elements = new Map();
        function element(id) {
          if (!elements.has(id)) elements.set(id, new FakeElement(id));
          return elements.get(id);
        }
        const context = {
          console,
          document: {
            getElementById: element,
            createElement: () => new FakeElement(),
            querySelector: () => null,
            querySelectorAll: () => [],
          },
          window: { addEventListener: () => {}, localStorage: null },
        };
        vm.createContext(context);
        vm.runInContext(source, context, { filename: appPath });
        context.readDeviceSocFillWidths = () => new Map();
        context.applyDeviceSocFillStarts = () => {};
        context.animateDeviceSocFills = () => {};

        function assert(condition, message) {
          if (!condition) throw new Error(message);
        }
        const assist = { status: "unknown", enabled: false };
        context.renderDevices({
          WR1: {
            online: true, soc: 60, pv_input_w: 400, output_w: 300, battery_power_w: 100,
            target_w: 260, output_limit_w: 300, battery_full_charge_assist: assist,
          },
          E3DC: {
            online: true, read_only: true, soc: 87, pv_input_w: 4321, output_w: 3624,
            battery_power_w: -1500, grid_power_w: -600, output_limit_w: 0,
            output_limit_applicable: false, firmware_status_applicable: false,
            target_w: 0, battery_full_charge_assist: assist,
          },
        });
        const cards = element("deviceGrid").innerHTML.split("<article").map((p) => "<article" + p);
        const wr1 = cards.find((html) => html.includes("WR1"));
        const e3dc = cards.find((html) => html.includes("E3DC"));
        assert(e3dc.includes(">Grid<"), "the E3/DC tile shows its grid value");
        assert(e3dc.includes("-600 W"), "the grid value keeps its sign");
        assert(!e3dc.includes(">Limit<"), "the E3/DC tile has no output limit");
        assert(!e3dc.includes(">Target<"), "the E3/DC tile has no target");
        assert(e3dc.includes(">PV<") && e3dc.includes(">Output<") && e3dc.includes(">Battery<"),
          "the E3/DC tile shows PV, inverter output and battery");
        assert(e3dc.split(">Grid<").length === 2, "exactly one Grid row: the measured power");
        assert(!e3dc.includes("Firmware status"), "no Zendure firmware enums on an E3/DC");
        assert(!wr1.includes("-600 W"), "a Zendure tile shows no E3/DC grid power");
        assert(wr1.includes("Firmware status"), "a Zendure tile keeps its firmware status");
        assert(wr1.includes(">Limit<"), "a Zendure tile keeps its limit");
        """
    )
    result = subprocess.run(
        [node, "-", str(ROOT / "dashboard/static/app.js")],
        input=script,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_the_probe_still_imports_only_the_shared_protocol_module():
    source = _PROBE_PATH.read_text(encoding="utf-8")
    imported = {
        line.split()[1]
        for line in source.splitlines()
        if line.startswith("from ems") or line.startswith("import ems")
    }
    assert imported == {"ems.e3dc_modbus"}
    assert os.path.exists(ROOT / "ems" / "e3dc_modbus.py")



def test_no_role_is_handed_the_same_reading_twice(device):
    _RecordingClient.reads = []
    sessions = _recording_sessions()
    try:
        meter = create_grid_meter_client(
            {"type": "e3dc_modbus", "ip": "127.0.0.1", "port": device.port},
            session=None,
            e3dc_sessions=sessions,
        )
        meter.get_power()
        meter.get_power()
    finally:
        sessions.close()
    power_reads = [read for read in _RecordingClient.reads if read[0] == modbus.POWER_COUNT]
    assert len(power_reads) == 2


def test_a_refused_inverter_block_never_costs_the_grid_meter_its_reading():
    words = probe.build_self_test_words()
    refused = set(range(41000 - 1, 41000 - 1 + modbus.INVERTER_COUNT))
    with probe._LoopbackE3dcServer(words, unsupported=refused) as server:
        sessions = E3dcSessions()
        endpoint = {"ip": "127.0.0.1", "port": server.port}
        try:
            meter = create_grid_meter_client(
                {"type": "e3dc_modbus", **endpoint}, session=None, e3dc_sessions=sessions
            )
            runtime = build_e3dc_device_runtime([_e3dc(**endpoint)], sessions)
            assert meter.get_power() == -600.0
            runtime.refresh()
            [tile] = runtime.tiles()
            session = meter.session
            assert session.consecutive_failures == 0
            assert session.mapping is not None
        finally:
            sessions.close()
    assert meter.health.success_count == 1
    assert meter.health.consecutive_failures == 0
    assert tile["online"] is False
    assert tile["state"].solar == 4321.0


def test_influx_history_counts_the_e3dc_like_the_dashboard(device):
    from ems.history.influx_writer import build_telemetry_lines

    runtime, sessions = _refreshed_runtime(device)
    try:
        lines = build_telemetry_lines(
            [SimpleNamespace(name="WR1")],
            [_control_state()],
            {"WR1": True},
            -600,
            timestamp_ns=1,
            read_only_tiles=runtime.tiles(),
        )
    finally:
        sessions.close()
    e3dc = [line for line in lines if "device=E3DC" in line]
    assert len(e3dc) == 1
    assert "source=e3dc" in e3dc[0]
    assert "solar=4321" in e3dc[0]
    meter = next(line for line in lines if line.startswith("shelly_meter"))
    assert "house_load=3324" in meter


def test_an_e3dc_takes_no_part_in_inverter_identity():
    from ems.device_identity import resolve_inverter_identity
    from ems.zendure_mqtt.config_entries import find_duplicate_zendure_device_identities

    first = _e3dc(name="E3DC", unit_id=1)
    second = _e3dc(name="E3DC2", unit_id=2)
    assert resolve_inverter_identity(first) is None
    assert find_duplicate_zendure_device_identities([first, second, ZENDURE]) == []


def test_an_unread_e3dc_does_not_drag_the_average_soc_to_zero():
    sessions = E3dcSessions()
    try:
        runtime = build_e3dc_device_runtime([_e3dc()], sessions)
        snapshot = _snapshot(_controller(runtime))
    finally:
        sessions.close()
    assert snapshot["average_soc"] == 60.0


def test_a_missing_inverter_block_keeps_the_last_known_output_on_the_tile(device):
    runtime, sessions = _refreshed_runtime(device)
    session = sessions.session("127.0.0.1", device.port, 1)
    try:
        device.unsupported = frozenset(
            range(41000 - 1, 41000 - 1 + modbus.INVERTER_COUNT)
        )
        session._clock = lambda: session.last_reading.taken_monotonic + 5
        runtime.refresh()
        [tile] = runtime.tiles()
    finally:
        sessions.close()
    assert tile["online"] is False
    assert tile["state"].output == 3624.0


def test_diagnostics_never_list_the_e3dc_as_allocated_or_assisted(tmp_path):
    from ems.diagnostics import (
        diagnose_battery_full_charge_assist_report,
        diagnose_control_distribution,
    )

    config = {
        "devices": [dict(ZENDURE), _e3dc()],
        "battery_full_charge_assist": {
            "state_database_path": str(tmp_path / "missing.sqlite")
        },
    }
    distribution = diagnose_control_distribution(config, {"devices": {}})
    assert [row["device"] for row in distribution["devices"]] == ["WR1"]
    report = diagnose_battery_full_charge_assist_report(config)
    assert [row["device"] for row in report["devices"]] == ["WR1"]
