import json
import logging
import time
from copy import deepcopy

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
        max_power=None,
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
        self.max_power = (
            cfg.MAX_DEVICE_POWER
            if max_power is None
            else max_power
        )
        self.pv_kwp = pv_kwp
        self.battery_kwh = battery_kwh
        self.pv_priority_factor = pv_priority_factor
        self.state = zero_device_state()

    def set_state(self, state):
        self.state = state

    def fetch(self):
        return self.state


class SimulatedHAClient:
    """Minimal HA-compatible state sink for self-tests."""

    def __init__(self):
        self.states = {}

    def set_state(
        self,
        entity_id,
        state,
        unit=None,
        device_class=None,
        state_class=None,
        icon=None,
        extra_attributes=None
    ):
        self.states[entity_id] = {
            "state": state,
            "attributes": extra_attributes or {}
        }

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
    state.solar1 = value_from_trace(data, "solar1", "solarPower1")
    state.solar2 = value_from_trace(data, "solar2", "solarPower2")
    state.solar3 = value_from_trace(data, "solar3", "solarPower3")
    state.solar4 = value_from_trace(data, "solar4", "solarPower4")
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

    original_output_control = deepcopy(cfg.OUTPUT_CONTROL_CONFIG)
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

    sim_device = SimulatedZendureClient("WR1")
    sim_ems = EMSController(
        [sim_device],
        SimulatedShellyClient(),
        ha=None,
        sleep_enabled=False,
        runtime_state=None
    )
    idle_state = zero_device_state()
    idle_state.soc = 15
    idle_state.min_soc = 15
    idle_state.soc_limit = 2

    if not sim_ems.state_is_strict_night_min_soc_idle(idle_state):
        ok = False
        log_event(
            logging.ERROR,
            "self_test_failed",
            test="night_min_soc_idle_detection",
            reason="strict_idle_not_detected"
        )

    idle_state.solar1 = 1

    if sim_ems.state_is_strict_night_min_soc_idle(idle_state):
        ok = False
        log_event(
            logging.ERROR,
            "self_test_failed",
            test="night_min_soc_idle_detection",
            reason="positive_panel_power_accepted"
        )

    if not sim_ems.state_has_positive_pv(idle_state):
        ok = False
        log_event(
            logging.ERROR,
            "self_test_failed",
            test="night_min_soc_idle_detection",
            reason="positive_panel_power_not_detected"
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

    sim_devices = [
        SimulatedZendureClient("WR1"),
        SimulatedZendureClient("WR2")
    ]
    sim_ems = EMSController(
        sim_devices,
        SimulatedShellyClient(),
        ha=None,
        sleep_enabled=False,
        runtime_state=None
    )

    idle_states = []

    for _dev in sim_devices:
        state = zero_device_state()
        state.soc = 15
        state.min_soc = 15
        state.max_soc = 100
        state.soc_limit = 2
        state.output_limit = 30
        idle_states.append(state)

    idle_capabilities = [
        detect_capabilities(state)
        for state in idle_states
    ]
    active_indexes = [0, 1]

    if sim_ems.has_output_control_export_capacity(
        idle_states,
        idle_capabilities,
        active_indexes
    ):
        ok = False
        log_event(
            logging.ERROR,
            "self_test_failed",
            test="output_control_export_capacity",
            reason="idle_state_has_export_capacity"
        )

    first_target = sim_ems.stabilized_total_target(
        300,
        idle_states,
        800,
        has_export_capacity=False,
        standby_total_w=60,
        active_device_count=2
    )
    second_target = sim_ems.stabilized_total_target(
        300,
        idle_states,
        800,
        has_export_capacity=False,
        standby_total_w=60,
        active_device_count=2
    )

    if first_target != 60 or second_target != 60:
        ok = False
        log_event(
            logging.ERROR,
            "self_test_failed",
            test="output_control_no_export_capacity_hold",
            expected="60,60",
            actual=f"{first_target},{second_target}"
        )

    sim_ems.commanded_total_w = 400
    max_change_target = sim_ems.stabilized_total_target(
        300,
        idle_states,
        800,
        has_export_capacity=False,
        standby_total_w=60,
        active_device_count=2
    )

    if max_change_target != 60:
        ok = False
        log_event(
            logging.ERROR,
            "self_test_failed",
            test="output_control_no_export_capacity_max_change",
            expected=60,
            actual=max_change_target
        )

    pv_state = zero_device_state()
    pv_state.soc = 15
    pv_state.min_soc = 15
    pv_state.max_soc = 100
    pv_state.soc_limit = 2
    pv_state.output_limit = 30
    pv_state.solar1 = 1

    if not sim_ems.has_output_control_export_capacity(
        [pv_state],
        [detect_capabilities(pv_state)],
        [0]
    ):
        ok = False
        log_event(
            logging.ERROR,
            "self_test_failed",
            test="output_control_export_capacity",
            reason="positive_panel_power_not_detected"
        )

    discharge_state = zero_device_state()
    discharge_state.soc = 50
    discharge_state.min_soc = 15
    discharge_state.max_soc = 100
    discharge_state.soc_limit = 0
    discharge_state.dc_status = 1
    discharge_state.output_limit = 30
    discharge_capability = detect_capabilities(discharge_state)

    if not sim_ems.has_output_control_export_capacity(
        [discharge_state],
        [discharge_capability],
        [0]
    ):
        ok = False
        log_event(
            logging.ERROR,
            "self_test_failed",
            test="output_control_export_capacity",
            reason="discharge_capacity_not_detected"
        )

    sim_ems.commanded_total_w = None
    sim_ems.filtered_load_w = None
    sim_ems.load_history.clear()
    discharge_target = sim_ems.stabilized_total_target(
        300,
        [discharge_state],
        800,
        has_export_capacity=True,
        standby_total_w=30,
        active_device_count=1
    )

    if discharge_target <= 30:
        ok = False
        log_event(
            logging.ERROR,
            "self_test_failed",
            test="output_control_export_capacity_ramp",
            expected=">30",
            actual=discharge_target
        )

    cfg.OUTPUT_CONTROL_CONFIG = {
        **original_output_control,
        "filter_enabled": True,
        "filter_method": "median_ema",
        "median_window": 1,
        "ema_alpha": 0.0,
        "sign_change_fast_response_enabled": True,
        "sign_change_threshold_w": 50,
        "sign_change_filter_reset_factor": 1.0
    }
    sim_ems.load_history.clear()
    sim_ems.filtered_load_w = 150
    export_fast_response = sim_ems.filter_output_control_load(-120)

    if export_fast_response != -120:
        ok = False
        log_event(
            logging.ERROR,
            "self_test_failed",
            test="output_control_sign_change_fast_response_export",
            expected=-120,
            actual=export_fast_response
        )

    sim_ems.load_history.clear()
    sim_ems.filtered_load_w = -120
    import_fast_response = sim_ems.filter_output_control_load(150)

    if import_fast_response != 150:
        ok = False
        log_event(
            logging.ERROR,
            "self_test_failed",
            test="output_control_sign_change_fast_response_import",
            expected=150,
            actual=import_fast_response
        )

    cfg.OUTPUT_CONTROL_CONFIG = {
        **cfg.OUTPUT_CONTROL_CONFIG,
        "sign_change_filter_reset_factor": 0.5
    }
    sim_ems.load_history.clear()
    sim_ems.filtered_load_w = 30
    partial_fast_response = sim_ems.filter_output_control_load(-120)

    if partial_fast_response != -45:
        ok = False
        log_event(
            logging.ERROR,
            "self_test_failed",
            test="output_control_sign_change_fast_response_partial",
            expected=-45,
            actual=partial_fast_response
        )

    cfg.OUTPUT_CONTROL_CONFIG = {
        **cfg.OUTPUT_CONTROL_CONFIG,
        "sign_change_fast_response_enabled": False,
        "sign_change_filter_reset_factor": 1.0
    }
    sim_ems.load_history.clear()
    sim_ems.filtered_load_w = 30
    disabled_fast_response = sim_ems.filter_output_control_load(-120)

    if disabled_fast_response != 30:
        ok = False
        log_event(
            logging.ERROR,
            "self_test_failed",
            test="output_control_sign_change_fast_response_disabled",
            expected=30,
            actual=disabled_fast_response
        )

    cfg.OUTPUT_CONTROL_CONFIG = {
        **cfg.OUTPUT_CONTROL_CONFIG,
        "sign_change_fast_response_enabled": True
    }
    sim_ems.load_history.clear()
    sim_ems.filtered_load_w = 30
    below_threshold_response = sim_ems.filter_output_control_load(-30)

    if below_threshold_response != 30:
        ok = False
        log_event(
            logging.ERROR,
            "self_test_failed",
            test="output_control_sign_change_fast_response_threshold",
            expected=30,
            actual=below_threshold_response
        )

    sim_ems.runtime_state = RuntimeState(
        "",
        build_runtime_defaults(sim_devices)
    )
    sim_ems.runtime_state.set_device("WR2", "enabled", False)
    sim_ems.device_online = {
        "WR1": True,
        "WR2": True
    }
    effective_targets = sim_ems.effective_control_targets(
        [10, 100],
        enabled=True,
        min_output_limit=30
    )

    if effective_targets != [30, 0]:
        ok = False
        log_event(
            logging.ERROR,
            "self_test_failed",
            test="effective_control_targets_disabled_min_output",
            expected="[30,0]",
            actual=json.dumps(effective_targets)
        )

    sim_ems.device_online["WR1"] = False
    effective_targets = sim_ems.effective_control_targets(
        [10, 100],
        enabled=True,
        min_output_limit=30
    )

    if effective_targets != [0, 0]:
        ok = False
        log_event(
            logging.ERROR,
            "self_test_failed",
            test="effective_control_targets_offline",
            expected="[0,0]",
            actual=json.dumps(effective_targets)
        )

    sim_ems.device_online["WR1"] = True
    sim_ems.runtime_state.set_device("WR2", "enabled", True)
    effective_targets = sim_ems.effective_control_targets(
        [10, 100],
        enabled=False,
        min_output_limit=30
    )

    if effective_targets != [0, 0]:
        ok = False
        log_event(
            logging.ERROR,
            "self_test_failed",
            test="effective_control_targets_system_disabled",
            expected="[0,0]",
            actual=json.dumps(effective_targets)
        )

    sim_ha = SimulatedHAClient()
    sim_ems.ha = sim_ha
    sim_ems.publish_to_ha(
        50,
        idle_states,
        [10, 100],
        [30, 0],
        30,
        110
    )
    target_total = sim_ha.states.get("sensor.ems_solarflow_target_total")
    wr1_target = sim_ha.states.get("sensor.ems_solarflow_wr1_target")
    wr2_target = sim_ha.states.get("sensor.ems_solarflow_wr2_target")

    if (
        not target_total
        or target_total["state"] != 30
        or target_total["attributes"].get("controller_target_w") != 110
        or target_total["attributes"].get("allocated_target_w") != 110
        or not wr1_target
        or wr1_target["state"] != 30
        or wr1_target["attributes"].get("allocated_target_w") != 10
        or not wr2_target
        or wr2_target["state"] != 0
        or wr2_target["attributes"].get("allocated_target_w") != 100
    ):
        ok = False
        log_event(
            logging.ERROR,
            "self_test_failed",
            test="ha_effective_target_publish",
            target_total=json.dumps(target_total, sort_keys=True),
            wr1_target=json.dumps(wr1_target, sort_keys=True),
            wr2_target=json.dumps(wr2_target, sort_keys=True)
        )

    if ok:
        cfg.OUTPUT_CONTROL_CONFIG = original_output_control
        log_event(logging.INFO, "self_test_ok")
        return True

    cfg.OUTPUT_CONTROL_CONFIG = original_output_control
    return False
