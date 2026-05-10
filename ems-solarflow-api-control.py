import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import argparse
import time
import logging
import json
import os
import sys
from dataclasses import dataclass
from concurrent.futures import ThreadPoolExecutor, as_completed

# =====================
# CLI
# =====================


def parse_args():
    parser = argparse.ArgumentParser(
        description="Local Zendure SolarFlow EMS controller"
    )

    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Read live telemetry and calculate targets without hardware writes"
    )
    parser.add_argument(
        "--config",
        help="Path to config file"
    )
    parser.add_argument(
        "--no-ha",
        action="store_true",
        help="Disable Home Assistant reads and writes for this run"
    )
    parser.add_argument(
        "--simulate",
        action="store_true",
        help="Run deterministic built-in simulation without hardware access"
    )
    parser.add_argument(
        "--replay",
        help="Replay a JSONL runtime trace without hardware access"
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run one loop iteration and exit"
    )
    parser.add_argument(
        "--duration",
        type=float,
        default=0,
        help="Run for this many seconds and then exit"
    )
    parser.add_argument(
        "--max-cycles",
        type=int,
        default=0,
        help="Run for this many EMS cycles and then exit"
    )
    parser.add_argument(
        "--preflight",
        action="store_true",
        help="Read telemetry and validate live-test prerequisites without control writes"
    )

    return parser.parse_args()


ARGS = parse_args()

# =====================
# CONFIG LOADING
# =====================

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def default_safe_config():
    """Return a minimal safe config for simulation and replay."""

    return {
        "ha": {
            "enabled": False,
            "url": "",
            "token": ""
        },
        "system": {
            "enabled": True,
            "dry_run": True,
            "simulation_mode": True,
            "allow_hardware_writes": False,
            "max_total_power": 800,
            "max_device_power": 800,
            "deadband": 10,
            "loop_interval": 5,
            "soc_reconcile_interval": 0,
            "log_level": "debug",
            "redistribute_clamped_power": True,
            "pv_kwp_weighting": True,
            "battery_kwh_weighting": True
        },
        "devices": [],
        "shelly": {
            "ip": ""
        }
    }


def load_config():
    path = ARGS.config or os.path.join(BASE_DIR, "config.json")

    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        if ARGS.simulate or ARGS.replay:
            return default_safe_config()

        print("config.json missing. Please create it from template.")
        sys.exit(1)


CONFIG = load_config()

# Extract configuration
SYSTEM_ENABLED = CONFIG["system"].get("enabled", True)

HA_URL = CONFIG["ha"].get("url", "")
HA_TOKEN = CONFIG["ha"].get("token", "")

MAX_TOTAL_POWER = CONFIG["system"]["max_total_power"]
MAX_DEVICE_POWER = CONFIG["system"]["max_device_power"]
DEADBAND = CONFIG["system"]["deadband"]
LOOP_INTERVAL = CONFIG["system"]["loop_interval"]
DRY_RUN = CONFIG["system"].get("dry_run", True) or ARGS.dry_run
SIMULATION_MODE = CONFIG["system"].get("simulation_mode", False) or ARGS.simulate
ALLOW_HARDWARE_WRITES = CONFIG["system"].get("allow_hardware_writes", False)
ALLOW_STATE_RECONCILIATION_WRITES = CONFIG["system"].get(
    "allow_state_reconciliation_writes",
    False
)
HA_ENABLED = (
    CONFIG["ha"].get("enabled", True)
    and not ARGS.no_ha
    and not SIMULATION_MODE
    and not ARGS.replay
)
HA_CONTROL_ENABLED = (
    HA_ENABLED
    and CONFIG["ha"].get("control_enabled", True)
)
LOG_LEVEL = CONFIG["system"].get("log_level", "info").lower()

if ARGS.simulate or ARGS.replay:
    LOG_LEVEL = "debug"
REDISTRIBUTE_CLAMPED_POWER = CONFIG["system"].get(
    "redistribute_clamped_power",
    True
)
PV_KWP_WEIGHTING = CONFIG["system"].get(
    "pv_kwp_weighting",
    True
)
BATTERY_KWH_WEIGHTING = CONFIG["system"].get(
    "battery_kwh_weighting",
    True
)
SOC_RECONCILE_INTERVAL = CONFIG["system"].get(
    "soc_reconcile_interval",
    10
)

ZENDURE_CONFIG = CONFIG["devices"]
SHELLY_IP = CONFIG["shelly"]["ip"]

# =====================
# LOGGING
# =====================

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL.upper(), logging.INFO),
    format="%(asctime)s | %(levelname)s | %(message)s"
)


def log_event(level, event, **fields):
    """Write simple structured key=value log lines."""

    parts = [f"event={event}"]

    for key in sorted(fields):
        value = fields[key]
        parts.append(f"{key}={value}")

    logging.log(level, " ".join(parts))


def hardware_writes_allowed():
    return (
        not DRY_RUN
        and not SIMULATION_MODE
        and not ARGS.replay
        and ALLOW_HARDWARE_WRITES
    )


def state_reconciliation_writes_allowed():
    return (
        hardware_writes_allowed()
        and ALLOW_STATE_RECONCILIATION_WRITES
    )

# =====================
# HTTP SESSION
# =====================


def create_session():
    """Create a requests session with retry logic."""

    session = requests.Session()

    retry = Retry(
        total=3,
        backoff_factor=0.3,
        status_forcelist=[500, 502, 503, 504],
        allowed_methods=["GET", "POST"]
    )

    adapter = HTTPAdapter(max_retries=retry)

    session.mount("http://", adapter)
    session.mount("https://", adapter)

    return session

# =====================
# HOME ASSISTANT CLIENT
# =====================


class HAClient:
    """Simple REST client for Home Assistant."""

    def __init__(self, base_url, token, session):
        self.base_url = base_url.rstrip("/")
        self.session = session

        self.headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json"
        }

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
        """Write a sensor state to HA."""

        attributes = {}

        if unit:
            attributes["unit_of_measurement"] = unit

        if device_class:
            attributes["device_class"] = device_class

        if state_class:
            attributes["state_class"] = state_class

        if icon:
            attributes["icon"] = icon

        if extra_attributes:
            attributes.update(extra_attributes)

        payload = {
            "state": state,
            "attributes": attributes
        }

        try:
            self.session.post(
                f"{self.base_url}/api/states/{entity_id}",
                headers=self.headers,
                json=payload,
                timeout=2
            )

        except Exception as e:
            logging.warning(f"HA write {entity_id}: {e}")

    def get_state(self, entity_id):
        """Read a state from HA."""

        try:
            r = self.session.get(
                f"{self.base_url}/api/states/{entity_id}",
                headers=self.headers,
                timeout=2
            )

            if r.status_code != 200:
                return None

            data = r.json()

            if not isinstance(data, dict):
                return None

            return data.get("state")

        except Exception:
            return None

    def ping(self):
        """Return True when Home Assistant API is reachable."""

        try:
            r = self.session.get(
                f"{self.base_url}/api/",
                headers=self.headers,
                timeout=2
            )

            return r.status_code == 200

        except Exception:
            return False

    def get_float(self, entity_id, default):
        """Return HA state as float or fallback."""

        val = self.get_state(entity_id)

        try:
            return float(val)
        except:
            return default

# =====================
# DATA MODEL
# =====================


@dataclass
class DeviceState:
    soc: float

    min_soc: float
    max_soc: float

    solar: float
    output: float

    pack_in: float
    pack_out: float

    temp: float
    voltage: float
    rssi: int

    remain_minutes: float

    solar1: float
    solar2: float
    solar3: float
    solar4: float

    output_limit: float
    soc_limit: int
    pack_state: int

    fault_level: int
    
    smart_mode: int
    grid_off_mode: int
    ac_mode: int
    ac_status: int
    dc_status: int
    grid_state: int


@dataclass
class DeviceCapabilities:
    can_charge: bool
    can_discharge: bool
    can_export: bool
    can_ac_charge: bool
    reason: str

# =====================
# DEVICE PARSING
# =====================


def parse_device(data):
    """Extract relevant values from Zendure API response."""

    props = data.get("properties", {})

    return DeviceState(
        soc=props.get("electricLevel") or 0,

        min_soc=(props.get("minSoc") or 0) / 10,
        max_soc=(props.get("socSet") or 0) / 10,

        solar=props.get("solarInputPower") or 0,
        output=props.get("outputHomePower") or 0,

        pack_in=props.get("packInputPower") or 0,
        pack_out=props.get("outputPackPower") or 0,

        temp=round(((props.get("hyperTmp") or 0) / 100), 2),
        voltage=round(((props.get("BatVolt") or 0) / 100), 2),
        rssi=props.get("rssi") or 0,

        remain_minutes=round(((props.get("remainOutTime") or 0) / 60), 1),

        solar1=props.get("solarPower1") or 0,
        solar2=props.get("solarPower2") or 0,
        solar3=props.get("solarPower3") or 0,
        solar4=props.get("solarPower4") or 0,

        output_limit=props.get("outputLimit") or 0,
        soc_limit=props.get("socLimit") or 0,
        pack_state=props.get("packState") or 0,

        fault_level=props.get("faultLevel") or 0,

        smart_mode=props.get("smartMode") or 0,
        grid_off_mode=props.get("gridOffMode") or 2,
        ac_mode=props.get("acMode") or 0,
        ac_status=props.get("acStatus") or 0,
        dc_status=props.get("dcStatus") or 0,
        grid_state=props.get("gridState") or 0,
    )


def zero_device_state():
    """Create an empty telemetry state for unavailable devices."""

    return DeviceState(
        soc=0,
        min_soc=0,
        max_soc=0,
        solar=0,
        output=0,
        pack_in=0,
        pack_out=0,
        temp=0,
        voltage=0,
        rssi=0,
        remain_minutes=0,
        solar1=0,
        solar2=0,
        solar3=0,
        solar4=0,
        output_limit=0,
        soc_limit=0,
        pack_state=0,
        fault_level=0,
        smart_mode=0,
        grid_off_mode=0,
        ac_mode=0,
        ac_status=0,
        dc_status=0,
        grid_state=0,
    )


def detect_capabilities(state):
    """Derive runtime capabilities from firmware telemetry."""

    reasons = []

    can_charge = state.soc_limit != 1
    can_discharge = state.soc_limit != 2 and state.dc_status != 0
    can_export = state.soc_limit != 2 and not (
        state.dc_status == 0 and state.ac_status == 0
    )
    can_ac_charge = state.ac_status == 2 and can_charge

    if state.soc_limit == 1:
        reasons.append("charge_inhibit")

    if state.soc_limit == 2:
        reasons.append("discharge_inhibit")

    if state.dc_status == 0:
        reasons.append("dc_inactive")

    if state.ac_status == 0:
        reasons.append("ac_inactive")

    if state.pack_state == 0:
        reasons.append("pack_standby")

    return DeviceCapabilities(
        can_charge=can_charge,
        can_discharge=can_discharge,
        can_export=can_export,
        can_ac_charge=can_ac_charge,
        reason=",".join(reasons) if reasons else "normal"
    )

# =====================
# DEVICE CLIENTS
# =====================


class ZendureClient:
    """Client for a single Zendure device."""

    def __init__(
        self,
        name,
        ip,
        sn,
        session,
        min_soc,
        max_soc,
        smart_mode,
        grid_off_mode,
        max_power=None,
        pv_kwp=1.0,
        battery_kwh=1.0,
        pv_priority_factor=1.0
    ):
        self.name = name
        self.ip = ip
        self.sn = sn
        self.session = session
        self.min_soc = min_soc
        self.max_soc = max_soc
        self.smart_mode = smart_mode
        self.grid_off_mode = grid_off_mode
        self.max_power = max_power or MAX_DEVICE_POWER
        self.pv_kwp = pv_kwp or 1.0
        self.battery_kwh = battery_kwh or 1.0
        self.pv_priority_factor = pv_priority_factor or 1.0

    def fetch(self):
        """Fetch current device state."""

        try:
            r = self.session.get(
                f"http://{self.ip}/properties/report",
                timeout=2
            )

            return parse_device(r.json())

        except Exception as e:
            logging.warning(f"{self.name} fetch failed: {e}")
            return None


class ShellyClient:
    """Client for Shelly power meter."""

    def __init__(self, ip, session):
        self.ip = ip
        self.session = session
        self.last_value = 0

    def get_power(self):
        """Return current household power usage."""

        try:
            r = self.session.get(
                f"http://{self.ip}/rpc/Shelly.GetStatus",
                timeout=3
            )

            self.last_value = round(
                r.json()["em:0"]["total_act_power"],
                1
            )

        except:
            pass

        return self.last_value


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
        max_power=MAX_DEVICE_POWER,
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
        self.grid_off_mode = 2
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


def fetch_all_devices(devices):
    """Fetch all device states in parallel."""

    results = [None] * len(devices)

    with ThreadPoolExecutor(max_workers=len(devices)) as executor:

        futures = {
            executor.submit(dev.fetch): i
            for i, dev in enumerate(devices)
        }

        for future in as_completed(futures):
            i = futures[future]

            try:
                results[i] = future.result()
            except:
                pass

    return results

# =====================
# CONTROL LOGIC
# =====================


def get_device_max_power(device_config):
    if device_config is None:
        return MAX_DEVICE_POWER

    return device_config.max_power


def get_device_pv_kwp(device_config):
    if device_config is None:
        return 1.0

    return max(0.01, device_config.pv_kwp)


def get_device_pv_priority_factor(device_config):
    if device_config is None:
        return 1.0

    return max(0.01, device_config.pv_priority_factor)


def get_device_battery_kwh(device_config):
    if device_config is None:
        return 1.0

    return max(0.01, device_config.battery_kwh)


def charge_headroom_weight(state, device_config, capability):
    """Return available charge headroom in weighted units."""

    if capability and not capability.can_charge:
        return 0.01

    if state.max_soc <= 0:
        return 1.0

    headroom_percent = max(1.0, state.max_soc - state.soc)

    if not BATTERY_KWH_WEIGHTING:
        return headroom_percent

    return max(
        0.01,
        get_device_battery_kwh(device_config) * headroom_percent / 100
    )


def usable_battery_weight(state, device_config, capability):
    """Return usable discharge energy in weighted units."""

    if capability and not capability.can_discharge:
        return 0

    if state.max_soc <= 0:
        return 0

    usable_percent = max(0, state.soc - state.min_soc)

    if not BATTERY_KWH_WEIGHTING:
        return usable_percent

    return max(
        0,
        get_device_battery_kwh(device_config) * usable_percent / 100
    )


def solar_weight(state, device_config, capability):
    """Return weighted current PV contribution for allocation."""

    if capability and not capability.can_export:
        return 0

    if not PV_KWP_WEIGHTING:
        return state.solar

    return (
        state.solar
        * get_device_pv_kwp(device_config)
        * get_device_pv_priority_factor(device_config)
    )


def apply_constraints_and_redistribute(
    targets,
    device_configs=None,
    capabilities=None
):
    """Clamp targets and redistribute excess power to devices with headroom."""

    device_count = len(targets)
    limits = []

    for i in range(device_count):
        cap = capabilities[i] if capabilities else None
        dev_config = device_configs[i] if device_configs else None

        limit = get_device_max_power(dev_config)

        if cap and not cap.can_export:
            limit = 0

        limits.append(limit)

    clamped = []
    excess = 0

    for target, limit in zip(targets, limits):
        value = max(0, min(limit, round(target)))
        clamped.append(value)
        excess += max(0, round(target) - value)

    if not REDISTRIBUTE_CLAMPED_POWER or excess <= 0:
        return clamped, excess

    redistributed = clamped[:]

    while excess > 0:
        candidates = [
            i
            for i, value in enumerate(redistributed)
            if value < limits[i]
        ]

        if not candidates:
            break

        share = max(1, round(excess / len(candidates)))
        moved = 0

        for i in candidates:
            headroom = limits[i] - redistributed[i]
            add = min(headroom, share, excess)

            redistributed[i] += add
            excess -= add
            moved += add

            if excess <= 0:
                break

        if moved <= 0:
            break

    return redistributed, excess


def calculate_targets(
    load,
    devices,
    max_power,
    device_configs=None,
    capabilities=None
):
    """
    Intelligent EMS target calculation.

    Strategy:

    Solar surplus:
    - prioritize batteries with remaining charge capacity

    Battery discharge:
    - prioritize batteries with more usable SOC

    This avoids:
    - PV curtailment on full batteries
    - overusing nearly empty batteries
    """

    current_total = sum(d.output for d in devices)
    solar_total = sum(d.solar for d in devices)

    new_total = max(
        0,
        min(max_power, current_total + load)
    )

    targets = [0] * len(devices)

    # =====================
    # CASE 1:
    # Enough solar available
    # =====================

    if solar_total >= new_total:

        #
        # PV-first mode:
        # When total PV can cover the requested AC output,
        # never allocate more output to a device than its
        # currently available PV-only contribution.
        #
        # This avoids inefficient simultaneous battery charging
        # on one device and battery discharging on another device.
        #

        pv_only_limits = []

        for i, d in enumerate(devices):
            cap = capabilities[i] if capabilities else None

            if cap and not cap.can_export:
                pv_only = 0
            else:
                #
                # pack_in = battery discharge power
                # effective PV-only contribution:
                # current solar minus current battery discharge
                #
                pv_only = max(0, d.solar - d.pack_in)

            pv_only_limits.append(pv_only)

            log_event(
                logging.DEBUG,
                "pv_first_limit",
                device=device_configs[i].name if device_configs else i,
                solar_w=d.solar,
                pack_input_w=d.pack_in,
                output_w=d.output,
                output_limit_w=d.output_limit,
                pv_only_limit_w=round(pv_only),
                soc=d.soc,
                pack_state=d.pack_state,
                soc_limit=d.soc_limit,
                can_export=cap.can_export if cap else True,
                can_discharge=cap.can_discharge if cap else True
            )

        pv_only_total = sum(pv_only_limits)

        if pv_only_total > 0:

            for i, pv_only in enumerate(pv_only_limits):

                share = pv_only / pv_only_total

                targets[i] = min(
                    pv_only,
                    new_total * share
                )

        else:

            targets = [0] * len(devices)

    # =====================
    # CASE 2:
    # Battery discharge required
    # =====================

    else:

        targets = [d.solar for d in devices]

        remaining = new_total - solar_total

        weights = []

        for i, d in enumerate(devices):
            dev_config = device_configs[i] if device_configs else None
            cap = capabilities[i] if capabilities else None

            if d.max_soc <= 0:

                #
                # No battery available
                #

                usable_soc = 0

            else:

                usable_soc = usable_battery_weight(
                    d,
                    dev_config,
                    cap
                )

            weights.append(usable_soc)

            log_event(
                logging.DEBUG,
                "balance_weight",
                device=device_configs[i].name if device_configs else i,
                mode="battery_discharge",
                solar_w=d.solar,
                soc=d.soc,
                min_soc=d.min_soc,
                usable_battery=round(usable_soc, 3),
                pv_kwp=get_device_pv_kwp(dev_config),
                battery_kwh=get_device_battery_kwh(dev_config),
                can_export=cap.can_export if cap else True,
                can_discharge=cap.can_discharge if cap else True,
                weight=round(usable_soc, 3)
            )

        weight_total = sum(weights)

        for i, d in enumerate(devices):

            share = (
                weights[i] / weight_total
                if weight_total else
                1 / len(devices)
            )

            targets[i] += remaining * share

    raw_targets = targets[:]

    targets, undistributed = apply_constraints_and_redistribute(
        targets,
        device_configs=device_configs,
        capabilities=capabilities
    )

    log_event(
        logging.DEBUG,
        "target_calculation",
        load=load,
        current_total=current_total,
        requested_total=new_total,
        raw_targets=json.dumps([round(t) for t in raw_targets]),
        final_targets=json.dumps(targets),
        undistributed=undistributed
    )

    return targets, current_total, new_total

# =====================
# EMS CONTROLLER
# =====================


class EMSController:
    """Main EMS control loop."""

    def __init__(self, devices, shelly, ha=None, sleep_enabled=True):
        self.devices = devices
        self.shelly = shelly
        self.ha = ha
        self.sleep_enabled = sleep_enabled
        self.soc_reconcile_counter = SOC_RECONCILE_INTERVAL

        self.last_states = {}
        self.last_seen = {}
        self.device_online = {}

    def set_output_limit(self, dev, value):
        """Write output limit to device."""

        if not hardware_writes_allowed():
            log_event(
                logging.INFO,
                "dry_run_output_limit",
                device=dev.name,
                target_w=value,
                dry_run=DRY_RUN,
                simulation=SIMULATION_MODE,
                allow_hardware_writes=ALLOW_HARDWARE_WRITES
            )
            return

        try:
            dev.session.post(
                f"http://{dev.ip}/properties/write",
                json={
                    "sn": dev.sn,
                    "properties": {
                        "outputLimit": int(value)
                    }
                },
                timeout=2
            )

            log_event(
                logging.INFO,
                "write_output_limit",
                device=dev.name,
                target_w=value
            )

        except Exception as e:
            log_event(
                logging.WARNING,
                "write_output_limit_error",
                device=dev.name,
                error=e
            )

    def apply_soc_limits(self, dev, state):
        """Apply configured SOC limits if required."""

        #
        # 0 = unmanaged
        #

        if dev.min_soc <= 0 and dev.max_soc <= 0:
            return

        #
        # Already configured
        #

        if (
            int(state.min_soc) == int(dev.min_soc)
            and
            int(state.max_soc) == int(dev.max_soc)
        ):

            log_event(
                logging.INFO,
                "soc_limits_unchanged",
                device=dev.name
            )

            return

        if not state_reconciliation_writes_allowed():
            log_event(
                logging.INFO,
                "dry_run_soc_limits",
                device=dev.name,
                min_soc=dev.min_soc,
                max_soc=dev.max_soc,
                dry_run=DRY_RUN,
                simulation=SIMULATION_MODE,
                allow_hardware_writes=ALLOW_HARDWARE_WRITES,
                allow_state_reconciliation_writes=(
                    ALLOW_STATE_RECONCILIATION_WRITES
                )
            )
            return

        try:

            dev.session.post(
                f"http://{dev.ip}/properties/write",
                json={
                    "sn": dev.sn,
                    "properties": {
                        "minSoc": int(dev.min_soc * 10),
                        "maxSoc": int(dev.max_soc * 10)
                    }
                },
                timeout=2
            )

            log_event(
                logging.INFO,
                "write_soc_limits",
                device=dev.name,
                min_soc=dev.min_soc,
                max_soc=dev.max_soc
            )

        except Exception as e:

            log_event(
                logging.WARNING,
                "write_soc_limits_error",
                device=dev.name,
                error=e
            )

    def apply_device_modes(self, dev, state):
        """Apply device operating modes if required."""

        #
        # unmanaged
        #

        if dev.smart_mode is None:
            return

        #
        # already configured
        #

        if (
            int(state.smart_mode) == int(dev.smart_mode)
            and
            int(state.ac_mode) == 2
        ):

            log_event(
                logging.INFO,
                "device_modes_unchanged",
                device=dev.name
            )

            return

        if not state_reconciliation_writes_allowed():
            log_event(
                logging.INFO,
                "dry_run_device_modes",
                device=dev.name,
                smart_mode=dev.smart_mode,
                ac_mode=2,
                grid_off_mode=dev.grid_off_mode,
                dry_run=DRY_RUN,
                simulation=SIMULATION_MODE,
                allow_hardware_writes=ALLOW_HARDWARE_WRITES,
                allow_state_reconciliation_writes=(
                    ALLOW_STATE_RECONCILIATION_WRITES
                )
            )
            return

        try:

            dev.session.post(
                f"http://{dev.ip}/properties/write",
                json={
                    "sn": dev.sn,
                    "properties": {
                        "smartMode": int(dev.smart_mode),
                        "acMode": 2,
                    }
                },
                timeout=2
            )

            log_event(
                logging.INFO,
                "write_device_modes",
                device=dev.name,
                smart_mode=dev.smart_mode,
                ac_mode=2,
            )
            
        except Exception as e:

            log_event(
                logging.WARNING,
                "write_device_modes_error",
                device=dev.name,
                error=e
            )


    def publish_sensor(
        self,
        entity,
        value,
        unit=None,
        device_class=None,
        state_class="measurement",
        icon=None,
        extra=None
    ):
        self.ha.set_state(
            entity,
            value,
            unit=unit,
            device_class=device_class,
            state_class=state_class,
            icon=icon,
            extra_attributes=extra
        )

    def publish_to_ha(
        self,
        load,
        states,
        targets,
        current,
        new
    ):
        """Publish values to Home Assistant."""

        p = "sensor.ems_solarflow_"

        solar_total = sum(d.solar for d in states)
        pack_in_total = sum(d.pack_in for d in states)
        pack_out_total = sum(d.pack_out for d in states)

        battery_power = pack_out_total - pack_in_total
        home = current + max(load, 0)

        soc_avg = round(
            sum(d.soc for d in states) / len(states),
            1
        )

        # =====================
        # GLOBAL
        # =====================

        self.publish_sensor(
            p + "load",
            round(load, 1),
            "W",
            "power"
        )

        self.publish_sensor(
            p + "target_total",
            round(new, 1),
            "W",
            "power"
        )

        self.publish_sensor(
            p + "solar_total",
            round(solar_total, 1),
            "W",
            "power"
        )

        self.publish_sensor(
            p + "battery_power",
            round(battery_power, 1),
            "W",
            "power"
        )

        self.publish_sensor(
            p + "home",
            round(home, 1),
            "W",
            "power"
        )

        self.publish_sensor(
            p + "soc_avg",
            soc_avg,
            "%",
            "battery"
        )

        # =====================
        # PER DEVICE
        # =====================

        for i, dev in enumerate(self.devices):

            d = states[i]

            base = p + dev.name.lower() + "_"

            # Core
            self.publish_sensor(
                base + "soc",
                d.soc,
                "%",
                "battery"
            )

            self.publish_sensor(
                base + "min_soc",
                d.min_soc,
                "%",
                "battery"
            )

            self.publish_sensor(
                base + "max_soc",
                d.max_soc,
                "%",
                "battery"
            )

            self.publish_sensor(
                base + "solar",
                d.solar,
                "W",
                "power"
            )

            self.publish_sensor(
                base + "output",
                d.output,
                "W",
                "power"
            )

            self.publish_sensor(
                base + "target",
                targets[i],
                "W",
                "power"
            )

            self.publish_sensor(
                base + "output_limit",
                d.output_limit,
                "W",
                "power"
            )

            self.publish_sensor(
                base + "soc_limit",
                d.soc_limit,
                state_class=None
            )

            self.publish_sensor(
                base + "pack_state",
                d.pack_state,
                state_class=None
            )

            # Zendure API uses controller/inverter perspective:
            # outputPackPower = charging
            # packInputPower  = discharging
            #
            # EMS convention:
            # Positive = charging
            # Negative = discharging

            device_battery_power = d.pack_out - d.pack_in

            self.publish_sensor(
                base + "battery_power",
                round(device_battery_power, 1),
                "W",
                "power"
            )

            self.publish_sensor(
                base + "voltage",
                d.voltage,
                "V",
                "voltage"
            )

            self.publish_sensor(
                base + "remaining_minutes",
                d.remain_minutes,
                "min",
                state_class=None,
                icon="mdi:timer-outline"
            )

            # Thermal / Signal
            self.publish_sensor(
                base + "temp",
                d.temp,
                "°C",
                "temperature"
            )

            self.publish_sensor(
                base + "rssi",
                d.rssi,
                "dBm",
                "signal_strength"
            )

            # Panels
            self.publish_sensor(
                base + "panel1",
                d.solar1,
                "W",
                "power"
            )

            self.publish_sensor(
                base + "panel2",
                d.solar2,
                "W",
                "power"
            )

            self.publish_sensor(
                base + "panel3",
                d.solar3,
                "W",
                "power"
            )

            self.publish_sensor(
                base + "panel4",
                d.solar4,
                "W",
                "power"
            )

            # Status
            self.ha.set_state(
                f"binary_sensor.{dev.name.lower()}_fault",
                "on" if d.fault_level > 0 else "off",
                device_class="problem",
                extra_attributes={
                    "fault_level": d.fault_level
                }
            )

            self.ha.set_state(
                f"binary_sensor.{dev.name.lower()}_ac_active",
                "on" if d.ac_status else "off",
                device_class="power"
            )

            self.ha.set_state(
                f"binary_sensor.{dev.name.lower()}_dc_active",
                "on" if d.dc_status else "off",
                device_class="power"
            )

            self.ha.set_state(
                f"binary_sensor.{dev.name.lower()}_grid_online",
                "on" if d.grid_state else "off",
                device_class="connectivity"
            )

    def run_once(self):
        """Execute one EMS cycle."""

        start = time.time()

        load = self.shelly.get_power()

        # =====================
        # HA CONFIG
        # =====================

        if self.ha and HA_CONTROL_ENABLED:

            max_power = self.ha.get_float(
                "input_number.ems_solarflow_max_power",
                MAX_TOTAL_POWER
            )

            val = self.ha.get_state(
                "input_boolean.ems_solarflow_enable"
            )

            enabled = (
                str(val).strip().lower() == "on"
            )

            interval = self.ha.get_float(
                "input_number.ems_solarflow_interval",
                LOOP_INTERVAL
            )

        else:

            max_power = MAX_TOTAL_POWER
            enabled = SYSTEM_ENABLED
            interval = LOOP_INTERVAL

        # =====================
        # FETCH STATES
        # =====================

        raw_states = fetch_all_devices(self.devices)

        states = []

        for dev, state in zip(self.devices, raw_states):

            #
            # Fresh state available
            #

            if state:

                self.last_states[dev.name] = state
                self.last_seen[dev.name] = time.time()
                self.device_online[dev.name] = True

                states.append(state)

                continue

            #
            # Fallback to last known state
            #

            if dev.name in self.last_states:

                self.device_online[dev.name] = False

                cached = self.last_states[dev.name]

                age = round(
                    time.time() - self.last_seen.get(dev.name, 0),
                    1
                )

                logging.warning(
                    f"{dev.name}: using cached state "
                    f"{age}s old "
                    f"(output={cached.output}W "
                    f"solar={cached.solar}W "
                    f"soc={cached.soc}%)"
                )

                states.append(cached)

                continue

            #
            # No valid state available
            #

            logging.error(
                f"{dev.name}: no valid state available"
            )

            self.device_online[dev.name] = False

            states.append(zero_device_state())

        capabilities = [
            detect_capabilities(state)
            for state in states
        ]

        for dev, state, cap in zip(
            self.devices,
            states,
            capabilities
        ):
            log_event(
                logging.DEBUG,
                "capability_detection",
                device=dev.name,
                soc_limit=state.soc_limit,
                dc_status=state.dc_status,
                ac_status=state.ac_status,
                pack_state=state.pack_state,
                can_charge=cap.can_charge,
                can_discharge=cap.can_discharge,
                can_export=cap.can_export,
                can_ac_charge=cap.can_ac_charge,
                reason=cap.reason
            )

        #
        # Reconcile SOC limits
        #

        if SOC_RECONCILE_INTERVAL > 0:

            self.soc_reconcile_counter += 1

            if (
                self.soc_reconcile_counter
                >= SOC_RECONCILE_INTERVAL
            ):

                self.soc_reconcile_counter = 0

                for dev, state in zip(
                    self.devices,
                    raw_states
                ):

                    if state:

                        self.apply_soc_limits(
                            dev,
                            state
                        )

                        self.apply_device_modes(
                            dev,
                            state
                        )

        # =====================
        # CALCULATE TARGETS
        # =====================

        targets, current, new = calculate_targets(
            load,
            states,
            max_power,
            device_configs=self.devices,
            capabilities=capabilities
        )

        logging.info(
            f"Load={load}W "
            f"Target={new}W "
            f"Enabled={enabled}"
        )

        # =====================
        # PUBLISH TO HA
        # =====================

        if self.ha:
            self.publish_to_ha(
                load,
                states,
                targets,
                current,
                new
            )

        # =====================
        # APPLY CONTROL
        # =====================

        for i, dev in enumerate(self.devices):

            if not enabled:
                log_event(
                    logging.INFO,
                    "control_disabled_skip_write",
                    device=dev.name,
                    target_w=targets[i]
                )
                continue

            if not self.device_online.get(dev.name, True):

                log_event(
                    logging.WARNING,
                    "offline_skip_write",
                    device=dev.name
                )

                continue

            target = targets[i]

            current_output = states[i].output

            if abs(target - current_output) < DEADBAND:
                log_event(
                    logging.DEBUG,
                    "deadband_skip_write",
                    device=dev.name,
                    target_w=target,
                    current_output_w=current_output,
                    deadband_w=DEADBAND
                )
                continue

            target = max(
                0,
                min(dev.max_power, target)
            )

            self.set_output_limit(dev, target)

        # =====================
        # LOOP TIMING
        # =====================

        elapsed = time.time() - start

        if self.sleep_enabled:
            time.sleep(max(0, interval - elapsed))

# =====================
# SIMULATION / REPLAY
# =====================


def value_from_trace(data, *keys, default=0):
    for key in keys:
        if key in data:
            return data[key]

    return default


def state_from_trace_device(data):
    """Build DeviceState from replay/simulation trace data."""

    state = zero_device_state()

    state.soc = value_from_trace(data, "soc", "electricLevel")
    state.min_soc = value_from_trace(data, "min_soc", "minSoc")
    state.max_soc = value_from_trace(data, "max_soc", "socSet")
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

    devices = []

    for i, data in enumerate(first_devices):
        devices.append(
            SimulatedZendureClient(
                data.get("name", f"SIM{i + 1}"),
                max_power=data.get("max_power", MAX_DEVICE_POWER),
                pv_kwp=data.get("pv_kwp", 1.0),
                battery_kwh=data.get("battery_kwh", 1.0),
                pv_priority_factor=data.get("pv_priority_factor", 1.0)
            )
        )

    shelly = SimulatedShellyClient()
    ems = EMSController(
        devices,
        shelly,
        ha=None,
        sleep_enabled=False
    )

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


# =====================
# PREFLIGHT
# =====================


def run_live_preflight(devices, shelly, ha=None):
    """Validate live-test prerequisites without dispatching control writes."""

    log_event(
        logging.INFO,
        "preflight_start",
        dry_run=DRY_RUN,
        allow_hardware_writes=ALLOW_HARDWARE_WRITES,
        ha_enabled=bool(ha)
    )

    if hardware_writes_allowed():
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


# =====================
# MAIN
# =====================


if __name__ == "__main__":

    log_event(
        logging.INFO,
        "startup",
        dry_run=DRY_RUN,
        simulation=SIMULATION_MODE,
        replay=bool(ARGS.replay),
        allow_hardware_writes=ALLOW_HARDWARE_WRITES,
        allow_state_reconciliation_writes=ALLOW_STATE_RECONCILIATION_WRITES,
        ha_enabled=HA_ENABLED,
        ha_control_enabled=HA_CONTROL_ENABLED
    )

    if SIMULATION_MODE and not ARGS.replay:
        run_frames(built_in_simulation_frames(), "built_in_simulation")
        sys.exit(0)

    if ARGS.replay:
        run_frames(load_replay_frames(ARGS.replay), ARGS.replay)
        sys.exit(0)

    session = create_session()

    ha = None

    if HA_ENABLED and not SIMULATION_MODE and not ARGS.replay:
        ha = HAClient(
            HA_URL,
            HA_TOKEN,
            session
        )

    devices = [
        ZendureClient(
            d["name"],
            d["ip"],
            d["sn"],
            session,
            d.get("min_soc", 0),
            d.get("max_soc", 0),
            d.get("smart_mode", 1),
            d.get("grid_off_mode", 2),
            d.get("max_power", MAX_DEVICE_POWER),
            d.get("pv_kwp", 1.0),
            d.get("battery_kwh", 1.0),
            d.get("pv_priority_factor", 1.0)
        )
        for d in ZENDURE_CONFIG
    ]

    shelly = ShellyClient(
        SHELLY_IP,
        session
    )

    if ARGS.preflight:
        if run_live_preflight(devices, shelly, ha):
            sys.exit(0)

        sys.exit(2)

    ems = EMSController(
        devices,
        shelly,
        ha
    )

    log_event(logging.INFO, "ems_started")

    start_time = time.time()
    cycles = 0

    while True:
        ems.run_once()
        cycles += 1

        if ARGS.once:
            log_event(
                logging.INFO,
                "ems_stopped",
                reason="once",
                cycles=cycles
            )
            break

        if ARGS.max_cycles and cycles >= ARGS.max_cycles:
            log_event(
                logging.INFO,
                "ems_stopped",
                reason="max_cycles",
                cycles=cycles
            )
            break

        if ARGS.duration and time.time() - start_time >= ARGS.duration:
            log_event(
                logging.INFO,
                "ems_stopped",
                reason="duration",
                cycles=cycles,
                duration_s=round(time.time() - start_time, 1)
            )
            break
