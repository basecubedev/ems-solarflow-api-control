import json
import logging
import time

from ems import config as cfg
from ems.clients import fetch_all_devices, zero_device_state
from ems.controller import EMSController
from ems.logging_utils import log_event
from ems.runtime_state import RuntimeState, build_runtime_defaults
from ems.target_control import detect_capabilities


class SimulatedShellyClient:
    """Shelly-compatible source for simulation and replay."""

    def __init__(self):
        self.power = 0

    def set_power(self, power):
        self.power = power

    def get_power(self):
        return self.power


class SimulatedZendureClient:
    """Zendure-compatible source for simulation and replay."""

    def __init__(
        self,
        name,
        max_power=cfg.MAX_DEVICE_POWER,
        pv_kwp=1.0,
        battery_kwh=1.0,
        pv_priority_factor=1.0
    ):
        self.name = name
        self.ip = "simulation"
        self.sn = "simulation"
        self.session = None
        self.min_soc = 0
        self.max_soc = 100
        self.smart_mode = 1
        self.grid_off_mode = None
        self.max_power = max_power
        self.pv_kwp = pv_kwp
        self.battery_kwh = battery_kwh
        self.pv_priority_factor = pv_priority_factor
        self.state = zero_device_state()

    def set_state(self, state):
        self.state = state

    def fetch(self):
        return self.state

# =====================
# PARALLEL FETCH
# =====================

def value_from_trace(data, *keys, default=0):
    for key in keys:
        if key in data:
            return data[key]

    return default


def percent_from_trace(data, normalized_key, raw_key):
    if normalized_key in data:
        return data[normalized_key]

    if raw_key in data:
        return data[raw_key] / 10

    return 0


def state_from_trace_device(data):
    """Build DeviceState from replay/simulation trace data."""

    state = zero_device_state()

    state.soc = value_from_trace(data, "soc", "electricLevel")
    state.min_soc = percent_from_trace(data, "min_soc", "minSoc")
    state.max_soc = percent_from_trace(data, "max_soc", "socSet")
    state.solar = value_from_trace(data, "solar", "solarInputPower")
    state.output = value_from_trace(data, "output", "outputHomePower")
    state.pack_in = value_from_trace(data, "pack_in", "packInputPower")
    state.pack_out = value_from_trace(data, "pack_out", "outputPackPower")
    state.output_limit = value_from_trace(data, "output_limit", "outputLimit")
    state.soc_limit = value_from_trace(data, "soc_limit", "socLimit")
    state.pack_state = value_from_trace(data, "pack_state", "packState")
    state.fault_level = value_from_trace(data, "fault_level", "faultLevel")
    state.ac_status = value_from_trace(data, "ac_status", "acStatus")
    state.dc_status = value_from_trace(data, "dc_status", "dcStatus")
    state.grid_state = value_from_trace(data, "grid_state", "gridState")
    state.smart_mode = value_from_trace(data, "smart_mode", "smartMode")
    state.grid_off_mode = value_from_trace(data, "grid_off_mode", "gridOffMode")
    state.ac_mode = value_from_trace(data, "ac_mode", "acMode")

    return state


def load_replay_frames(path):
    frames = []

    with open(path) as f:
        for line_number, line in enumerate(f, start=1):
            line = line.strip()

            if not line:
                continue

            try:
                frames.append(json.loads(line))
            except json.JSONDecodeError as e:
                raise ValueError(
                    f"Invalid JSONL at line {line_number}: {e}"
                )

    return frames


def built_in_simulation_frames():
    return [
        {
            "timestamp": 1,
            "house_load": 300,
            "devices": [
                {
                    "name": "WR1",
                    "soc": 80,
                    "min_soc": 15,
                    "max_soc": 100,
                    "solarInputPower": 260,
                    "outputHomePower": 150,
                    "socLimit": 0,
                    "dcStatus": 1,
                    "acStatus": 1,
                    "packState": 2
                },
                {
                    "name": "WR2",
                    "soc": 45,
                    "min_soc": 15,
                    "max_soc": 100,
                    "solarInputPower": 180,
                    "outputHomePower": 150,
                    "socLimit": 0,
                    "dcStatus": 1,
                    "acStatus": 1,
                    "packState": 2
                }
            ]
        },
        {
            "timestamp": 2,
            "house_load": 500,
            "devices": [
                {
                    "name": "WR1",
                    "soc": 78,
                    "min_soc": 15,
                    "max_soc": 100,
                    "solarInputPower": 120,
                    "outputHomePower": 220,
                    "socLimit": 0,
                    "dcStatus": 1,
                    "acStatus": 1,
                    "packState": 2
                },
                {
                    "name": "WR2",
                    "soc": 15,
                    "min_soc": 15,
                    "max_soc": 100,
                    "solarInputPower": 80,
                    "outputHomePower": 0,
                    "socLimit": 2,
                    "dcStatus": 0,
                    "acStatus": 0,
                    "packState": 0
                }
            ]
        }
    ]


def run_frames(frames, source_name):
    if not frames:
        log_event(logging.WARNING, "no_frames", source=source_name)
        return

    first_devices = frames[0].get("devices", [])

    if not first_devices:
        log_event(logging.ERROR, "no_frame_devices", source=source_name)
        return

    devices = []

    for i, data in enumerate(first_devices):
        devices.append(
            SimulatedZendureClient(
                data.get("name", f"SIM{i + 1}"),
                max_power=data.get("max_power", cfg.MAX_DEVICE_POWER),
                pv_kwp=data.get("pv_kwp", 1.0),
                battery_kwh=data.get("battery_kwh", 1.0),
                pv_priority_factor=data.get("pv_priority_factor", 1.0)
            )
        )

    shelly = SimulatedShellyClient()
    runtime_state = RuntimeState(
        cfg.runtime_state_path(),
        build_runtime_defaults(devices)
    )
    runtime_state.load_or_create()

    ems = EMSController(
        devices,
        shelly,
        ha=None,
        sleep_enabled=False,
        runtime_state=runtime_state
    )

    start_time = time.time()
    cycles = 0

    for frame_index, frame in enumerate(frames, start=1):
        shelly.set_power(frame.get("house_load", 0))

        for dev, trace_device in zip(
            devices,
            frame.get("devices", [])
        ):
            dev.set_state(state_from_trace_device(trace_device))

        log_event(
            logging.INFO,
            "replay_frame",
            source=source_name,
            frame=frame_index,
            timestamp=frame.get("timestamp", "")
        )
        ems.run_once()
        cycles += 1

        if cfg.ARGS.once:
            log_event(
                logging.INFO,
                "replay_stopped",
                reason="once",
                cycles=cycles
            )
            break

        if cfg.ARGS.max_cycles and cycles >= cfg.ARGS.max_cycles:
            log_event(
                logging.INFO,
                "replay_stopped",
                reason="max_cycles",
                cycles=cycles
            )
            break

        if cfg.ARGS.duration and time.time() - start_time >= cfg.ARGS.duration:
            log_event(
                logging.INFO,
                "replay_stopped",
                reason="duration",
                cycles=cycles,
                duration_s=round(time.time() - start_time, 1)
            )
            break


# =====================
# PREFLIGHT
# =====================


def run_live_preflight(devices, shelly, ha=None):
    """Validate live-test prerequisites without dispatching control writes."""

    log_event(
        logging.INFO,
        "preflight_start",
        dry_run=cfg.DRY_RUN,
        allow_hardware_writes=cfg.ALLOW_HARDWARE_WRITES,
        ha_enabled=bool(ha)
    )

    if cfg.hardware_writes_allowed():
        log_event(
            logging.ERROR,
            "preflight_abort",
            reason="hardware_writes_enabled"
        )
        return False

    if ha:
        if not ha.ping():
            log_event(
                logging.ERROR,
                "preflight_abort",
                reason="ha_unreachable"
            )
            return False

        log_event(logging.INFO, "preflight_ha_ok")

    load = shelly.get_power()
    log_event(logging.INFO, "preflight_shelly_ok", load=load)

    states = fetch_all_devices(devices)
    ok = True

    for dev, state in zip(devices, states):
        if not state:
            log_event(
                logging.ERROR,
                "preflight_device_unreachable",
                device=dev.name
            )
            ok = False
            continue

        cap = detect_capabilities(state)

        log_event(
            logging.INFO,
            "preflight_device_ok",
            device=dev.name,
            soc=state.soc,
            output_w=state.output,
            solar_w=state.solar,
            pack_input_w=state.pack_in,
            output_pack_w=state.pack_out,
            output_limit_w=state.output_limit,
            smart_mode=state.smart_mode,
            grid_off_mode=state.grid_off_mode,
            ac_mode=state.ac_mode,
            soc_limit=state.soc_limit,
            dc_status=state.dc_status,
            ac_status=state.ac_status,
            pack_state=state.pack_state,
            can_export=cap.can_export,
            reason=cap.reason
        )

        if int(state.smart_mode) != 1:
            log_event(
                logging.ERROR,
                "preflight_abort",
                device=dev.name,
                reason="smart_mode_not_1",
                smart_mode=state.smart_mode
            )
            ok = False

    if ok:
        log_event(logging.INFO, "preflight_ok")
    else:
        log_event(logging.ERROR, "preflight_failed")

    return ok


def run_self_tests():
    """Run local helper checks without hardware or HA access."""

    cases = [
        (15, 18, True, 20),
        (15, 22, True, 22),
        (15, 13, True, 20),
        (15, 45, True, 40),
        (40, 30, False, 15),
        (38, 39, True, 40)
    ]
    ok = True

    for current_min, current_soc, winter_active, expected in cases:
        actual = cfg.calculate_winter_min_soc_target(
            current_soc,
            current_min,
            winter_active,
            summer_min_soc=15,
            winter_min_soc=40,
            ramp_step=5
        )

        if actual != expected:
            ok = False
            log_event(
                logging.ERROR,
                "self_test_failed",
                test="cfg.calculate_winter_min_soc_target",
                current_min_soc=current_min,
                current_soc=current_soc,
                winter_active=winter_active,
                expected=expected,
                actual=actual
            )

    payload = cfg.build_winter_ac_charge_limit_payload()
    if set(payload.keys()) != {"inputLimit"}:
        ok = False
        log_event(
            logging.ERROR,
            "self_test_failed",
            test="winter_ac_charge_limit_payload",
            payload=json.dumps(payload, sort_keys=True)
        )

    if ok:
        log_event(logging.INFO, "self_test_ok")
        return True

    return False

