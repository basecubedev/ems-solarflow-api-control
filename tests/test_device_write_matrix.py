# SPDX-License-Identifier: AGPL-3.0-or-later
"""Who may be written to, on every path: one matrix for every device kind.

Each kind of ``devices[]`` entry is walked through every decision that can put
it on a control, write, runtime-state or identity path, and the result is held
against one declared table. A kind added to the code without a row fails the
registry guard, so a new device type can never become controllable by falling
through a check nobody updated -- which is exactly how an E3/DC would once have
been handed a ``ZendureClient`` and an ``outputLimit`` write.

The second half runs whole control cycles with every write gate open and
records every write the controller issues and every Modbus request that reaches
the E3/DC: controlled devices get their writes, a read-only device gets none.
"""

import importlib.util
from pathlib import Path
from unittest.mock import patch

import pytest

import emsctl
from admin.runtime_convergence import _classify as admin_runtime_target
from dashboard.runtime_write import RuntimeWriteError, apply_device_update, build_validation_context
from ems import config as cfg
from ems import e3dc_modbus as modbus
from ems.controller import EMSController
from ems.device_identity import resolve_inverter_identity
from ems.diagnostics import diagnose_controllable_config_device_names
from ems.e3dc_runtime import E3dcModbusSession, E3dcSessions, build_e3dc_device_runtime
from ems.clients import create_grid_meter_client
from ems.health import CommHealth
from ems.read_only_devices import READ_ONLY_DEVICE_TYPES
from ems.zendure_mqtt.config_entries import ZENDURE_MQTT_TYPE, has_runtime_control_device
from tests.test_write_gates import RuntimeStateStub, state

pytestmark = [
    pytest.mark.power_control,
    pytest.mark.contract,
]

ROOT = Path(__file__).resolve().parents[1]


def _mqtt(name, broker, *, control=True, enabled=True):
    return {
        "name": name,
        "type": ZENDURE_MQTT_TYPE,
        "enabled": enabled,
        "capabilities": {"write_output_limit": control},
        "mqtt": {"broker_ref": broker, "device_id": name.upper()},
    }


KINDS = {
    "api": {"name": "api", "ip": "192.0.2.1", "sn": "SN1"},
    "api_disabled": {"name": "api_disabled", "ip": "192.0.2.2", "sn": "SN2", "enabled": False},
    # Configs in the wild carry a model name as ``type`` on a local-API device;
    # an unrecognised type therefore still means local API, deliberately.
    "api_model_typed": {"name": "api_model_typed", "type": "solarflow_800", "ip": "192.0.2.3", "sn": "SN3"},
    "mqtt_local_control": _mqtt("mqtt_local_control", "home"),
    "mqtt_cloud_control": _mqtt("mqtt_cloud_control", "account"),
    "mqtt_telemetry": _mqtt("mqtt_telemetry", "home", control=False),
    "mqtt_disabled": _mqtt("mqtt_disabled", "home", enabled=False),
    "e3dc": {"name": "e3dc", "type": "e3dc_modbus", "ip": "192.0.2.60"},
    "e3dc_disabled": {"name": "e3dc_disabled", "type": "e3dc_modbus", "ip": "192.0.2.61", "enabled": False},
}

_BROKERS = {
    "brokers": {
        "home": {"host": "192.168.50.10", "source": "local_mqtt"},
        "account": {"host": "mq.zen-iot.com", "source": "zendure_cloud_mqtt"},
    }
}


def _config(entry):
    return {"system": {}, "devices": [dict(entry)], "zendure_mqtt": _BROKERS}


def _gate(entry):
    grouped = cfg.config_control_devices_by_gate(_config(entry))
    armed = [gate for gate, items in grouped.items() if items]
    return armed[0] if armed else None


PATHS = {
    "http_control_client": lambda entry: bool(cfg.http_control_device_configs([dict(entry)])),
    "mqtt_control_entry": lambda entry: bool(cfg.mqtt_control_device_configs([dict(entry)])),
    "write_gate": _gate,
    "makes_ems_bootable": lambda entry: has_runtime_control_device(_config(entry)),
    "emsctl_runtime_defaults": lambda entry: entry["name"] in emsctl.config_device_defaults(_config(entry)),
    "diagnose_controllable": lambda entry: entry["name"]
    in diagnose_controllable_config_device_names(_config(entry)),
    "admin_runtime_mirror": lambda entry: admin_runtime_target("devices[0].enabled", _config(entry))
    is not None,
    "inverter_identity": lambda entry: resolve_inverter_identity(dict(entry)) is not None,
}

# fmt: off
EXPECTED = {
    #                     http   mqtt   gate            boots  rt-def diag   mirror ident
    "api":                (True,  False, "api",          True,  True,  True,  True,  True),
    "api_disabled":       (False, False, None,           False, False, False, True,  True),
    "api_model_typed":    (True,  False, "api",          True,  True,  True,  True,  True),
    "mqtt_local_control": (False, True,  "mqtt_local",   True,  True,  True,  True,  True),
    "mqtt_cloud_control": (False, True,  "mqtt_zendure", True,  True,  True,  True,  True),
    "mqtt_telemetry":     (False, False, None,           False, False, False, True,  True),
    # A disabled MQTT control entry arms no gate and builds no control device
    # (the control runtime skips it), but it still gets runtime defaults and
    # counts as controllable in diagnose; a disabled API entry does neither.
    "mqtt_disabled":      (False, True,  None,           False, True,  True,  True,  True),
    "e3dc":               (False, False, None,           False, False, False, False, False),
    "e3dc_disabled":      (False, False, None,           False, False, False, False, False),
}
# fmt: on


def _actual_table():
    return {kind: tuple(path(entry) for path in PATHS.values()) for kind, entry in KINDS.items()}


def test_every_device_kind_reaches_exactly_the_paths_it_is_declared_for():
    actual = _actual_table()
    names = list(PATHS)
    differences = [
        f"{kind}.{names[index]}: expected {EXPECTED[kind][index]!r}, got {actual[kind][index]!r}"
        for kind in KINDS
        for index in range(len(names))
        if actual[kind][index] != EXPECTED[kind][index]
    ]
    assert differences == []


def test_the_matrix_has_a_row_for_every_kind_and_a_column_for_every_path():
    assert set(EXPECTED) == set(KINDS)
    assert all(len(row) == len(PATHS) for row in EXPECTED.values())


def test_every_device_type_the_code_defines_has_a_row():
    covered = {str(entry.get("type") or "").strip().lower() for entry in KINDS.values()}
    defined = {"", ZENDURE_MQTT_TYPE, *READ_ONLY_DEVICE_TYPES}
    assert defined <= covered, f"device types without a matrix row: {sorted(defined - covered)}"


def test_read_only_kinds_reach_no_path_at_all():
    for kind, entry in KINDS.items():
        if str(entry.get("type") or "") in READ_ONLY_DEVICE_TYPES:
            assert not any(EXPECTED[kind]), kind


# --- the writers that act on names ------------------------------------------------


def test_no_runtime_writer_accepts_a_read_only_device_name():
    config = {"system": {}, "devices": [dict(KINDS["api"]), dict(KINDS["e3dc"])]}
    runtime_state = RuntimeStateStub(devices={"api": {}})
    context = build_validation_context(config, runtime_state)
    with pytest.raises(RuntimeWriteError):
        apply_device_update(runtime_state, "e3dc", {"max_power": 100}, context)


# --- whole control cycles with every gate open ---------------------------------------


class _RecordingDevice:
    """A controlled device that records every write the controller issues."""

    control_gate = "api"

    def __init__(self, name):
        self.name = name
        self.ip = "192.0.2.1"
        self.sn = f"{name}-SN"
        self.max_power = 800
        self.pv_kwp = 1.0
        self.pv_priority_factor = 1.0
        self.battery_kwh = 1.0
        self.min_soc = 15
        self.max_soc = 100
        self.smart_mode = 1
        self.grid_off_mode = None
        self.read_health = CommHealth(name, kind="read")
        self.write_health = CommHealth(name, kind="write")
        self.writes = []

    def write_output_limit(self, value):
        self.writes.append(("outputLimit", value))
        return True

    def write_properties(self, properties, **kwargs):
        self.writes.append(("properties", dict(properties)))
        from ems.mqtt_control import dispatch

        return dispatch.published(None)


class _WireRecorder(modbus.ReadOnlyModbusTcpClient):
    function_codes = []

    def read_registers(self, address, count, function_code=3, unit_id=None):
        _WireRecorder.function_codes.append(function_code)
        return super().read_registers(address, count, function_code, unit_id)


_PROBE = ROOT / "scripts" / "e3dc_modbus_probe.py"
_spec = importlib.util.spec_from_file_location("e3dc_probe_for_write_matrix", _PROBE)
probe = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(probe)


def _all_gates_open():
    return patch.multiple(
        cfg,
        SYSTEM_ENABLED=True,
        DRY_RUN=False,
        SIMULATION_MODE=False,
        ALLOW_HARDWARE_WRITES=True,
        ALLOW_MQTT_LOCAL_CONTROL_WRITES=True,
        ALLOW_MQTT_ZENDURE_CONTROL_WRITES=True,
        ALLOW_STATE_RECONCILIATION_WRITES=True,
        MAX_TOTAL_POWER=800,
        MAX_DEVICE_POWER=800,
        MIN_OUTPUT_LIMIT=0,
        LOOP_INTERVAL=5,
        DEADBAND=0,
        SOC_RECONCILE_INTERVAL=0,
    )


def test_whole_cycles_with_every_gate_open_write_only_to_controlled_devices():
    _WireRecorder.function_codes = []
    controlled = _RecordingDevice("WR1")
    words = probe.build_self_test_words()
    with probe._LoopbackE3dcServer(words) as server:

        def factory(host, *, port, unit_id):
            return E3dcModbusSession(
                host, port=port, unit_id=unit_id, modbus_client_factory=_WireRecorder
            )

        sessions = E3dcSessions(session_factory=factory)
        endpoint = {"ip": "127.0.0.1", "port": server.port}
        try:
            meter = create_grid_meter_client(
                {"type": "e3dc_modbus", **endpoint}, session=None, e3dc_sessions=sessions
            )
            read_only = build_e3dc_device_runtime(
                [{"name": "E3DC", "type": "e3dc_modbus", **endpoint}], sessions
            )
            controller = EMSController(
                devices=[controlled],
                shelly=meter,
                sleep_enabled=False,
                runtime_state=RuntimeStateStub(),
                read_only_devices=read_only,
            )
            with _all_gates_open(), patch(
                "ems.controller.fetch_all_devices",
                return_value=[state(soc=50, solar=600, output=100, output_limit=100)],
            ):
                for _ in range(3):
                    controller.run_once()
        finally:
            sessions.close()

    assert controlled.writes, "the controlled device was never written: the harness proves nothing"
    assert {kind for kind, _ in controlled.writes} <= {"outputLimit", "properties"}
    assert _WireRecorder.function_codes
    assert set(_WireRecorder.function_codes) <= modbus.READ_ONLY_FUNCTION_CODES
    assert [device.name for device in controller.devices] == ["WR1"]


def test_the_controller_uses_a_read_only_runtime_only_to_read_it():
    source = (ROOT / "ems" / "controller.py").read_text(encoding="utf-8")
    uses = {
        line.split("read_only_devices.", 1)[1].split("(", 1)[0]
        for line in source.splitlines()
        if "read_only_devices." in line and "self.read_only_devices =" not in line
    }
    assert uses <= {"refresh", "tiles"}, uses
