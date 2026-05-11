import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import argparse
import time
import logging
import json
import os
import sys
from collections import deque
from dataclasses import dataclass
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

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
    parser.add_argument(
        "--self-test",
        action="store_true",
        help="Run local helper tests without hardware access"
    )

    return parser.parse_args()


ARGS = parse_args()

# =====================
# CONFIG LOADING
# =====================

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

OUTPUT_CONTROL_DEFAULTS = {
    "load_deadband_w": 5,
    "target_deadband_w": 10,
    "filter_enabled": True,
    "filter_method": "median_ema",
    "median_window": 3,
    "ema_alpha": 0.65,
    "ramp_enabled": True,
    "ramp_up_w_per_cycle": 300,
    "ramp_down_w_per_cycle": 500,
    "device_ramp_enabled": True,
    "device_ramp_up_w_per_cycle": 250,
    "device_ramp_down_w_per_cycle": 400,
    "write_cooldown_seconds": 2,
    "large_import_bypass_w": 600,
    "large_export_bypass_w": 500,
    "bypass_ramp_multiplier": 1.5,
    "telemetry_max_age_seconds": 10,
    "stale_telemetry_ramp_factor": 0.5
}

WINTER_DEFAULTS = {
    "enabled": False,
    "months": [10, 11, 12, 1, 2],
    "summer_min_soc": 15,
    "winter_min_soc": 40,
    "ramp_step_percent": 5,
    "adjust_hour": 12,
    "ac_charge_power": 200
}


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
            "reconcile_ac_mode_on_start": True,
            "reconcile_smart_mode": True,
            "max_total_power": 800,
            "max_device_power": 800,
            "deadband": 10,
            "runtime_state_path": "runtime-state.json",
            "min_output_limit": 0,
            "loop_interval": 5,
            "output_control": OUTPUT_CONTROL_DEFAULTS,
            "soc_reconcile_interval": 0,
            "log_level": "debug",
            "redistribute_clamped_power": True,
            "pv_kwp_weighting": True,
            "battery_kwh_weighting": True
        },
        "winter": WINTER_DEFAULTS,
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
        if ARGS.simulate or ARGS.replay or ARGS.self_test:
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
OUTPUT_CONTROL_CONFIG = {
    **OUTPUT_CONTROL_DEFAULTS,
    **CONFIG["system"].get("output_control", {})
}
RUNTIME_STATE_PATH = CONFIG["system"].get(
    "runtime_state_path",
    "runtime-state.json"
)
REMAINING_TIME_POWER_SAMPLES = 10
REMAINING_TIME_MIN_POWER_W = 10
REMAINING_TIME_MAX_HOURS = 999
try:
    MIN_OUTPUT_LIMIT = max(
        0,
        int(CONFIG["system"].get("min_output_limit", 0))
    )
except (TypeError, ValueError):
    MIN_OUTPUT_LIMIT = 0
DRY_RUN = CONFIG["system"].get("dry_run", True) or ARGS.dry_run
SIMULATION_MODE = CONFIG["system"].get("simulation_mode", False) or ARGS.simulate
ALLOW_HARDWARE_WRITES = CONFIG["system"].get("allow_hardware_writes", False)
ALLOW_STATE_RECONCILIATION_WRITES = CONFIG["system"].get(
    "allow_state_reconciliation_writes",
    False
)
RECONCILE_AC_MODE_ON_START = CONFIG["system"].get(
    "reconcile_ac_mode_on_start",
    True
)
RECONCILE_SMART_MODE = CONFIG["system"].get(
    "reconcile_smart_mode",
    True
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
WINTER_CONFIG = {
    **WINTER_DEFAULTS,
    **CONFIG.get("winter", {})
}
OFFGRID_SOCKET_MODES = {
    "standard": 0,
    "eco": 1,
    "off": 2
}

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


def runtime_state_path():
    """Return absolute path to mutable runtime state."""

    if os.path.isabs(RUNTIME_STATE_PATH):
        return RUNTIME_STATE_PATH

    return os.path.join(BASE_DIR, RUNTIME_STATE_PATH)


def safe_int(value, default=0, minimum=None):
    try:
        parsed = int(float(value))
    except (TypeError, ValueError):
        parsed = default

    if minimum is not None:
        parsed = max(minimum, parsed)

    return parsed


def safe_float(value, default=0.0, minimum=None):
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = default

    if minimum is not None:
        parsed = max(minimum, parsed)

    return parsed


def safe_bool(value, default=False):
    if isinstance(value, bool):
        return value

    if value is None:
        return default

    normalized = str(value).strip().lower()

    if normalized in ("on", "true", "1", "yes", "enabled"):
        return True

    if normalized in ("off", "false", "0", "no", "disabled"):
        return False

    return default


def winter_config_bool(key, default=False):
    return safe_bool(WINTER_CONFIG.get(key, default), default)


def winter_config_int(key, default=0, minimum=None):
    return safe_int(WINTER_CONFIG.get(key, default), default, minimum=minimum)


def winter_months():
    months = WINTER_CONFIG.get("months", WINTER_DEFAULTS["months"])

    if not isinstance(months, list):
        months = WINTER_DEFAULTS["months"]

    parsed = []

    for month in months:
        value = safe_int(month, 0)
        if 1 <= value <= 12:
            parsed.append(value)

    return parsed or WINTER_DEFAULTS["months"]


def winter_month_active(now):
    """Return True when the configured winter month set contains now.month."""

    return now.month in winter_months()


def winter_feature_enabled(runtime_state=None):
    """Return whether winter mode is enabled.

    HA/runtime toggles can be layered here later. V1 is config-controlled.
    """

    if runtime_state:
        winter = runtime_state.data.get("winter", {})
        if isinstance(winter, dict) and "enabled" in winter:
            return safe_bool(winter.get("enabled"), False)

    return winter_config_bool("enabled", False)


def winter_mode_active(now, runtime_state=None):
    return winter_feature_enabled(runtime_state) and winter_month_active(now)


def calculate_winter_min_soc_target(
    current_soc,
    effective_min_soc,
    winter_active,
    summer_min_soc=None,
    winter_min_soc=None,
    ramp_step=None
):
    """Calculate the next minSoc target for winter/summer reconciliation."""

    if summer_min_soc is None:
        summer_min_soc = winter_config_int("summer_min_soc", 15, minimum=0)
    else:
        summer_min_soc = safe_int(summer_min_soc, 15, minimum=0)

    if winter_min_soc is None:
        winter_min_soc = winter_config_int("winter_min_soc", 40, minimum=0)
    else:
        winter_min_soc = safe_int(winter_min_soc, 40, minimum=0)

    if ramp_step is None:
        ramp_step = winter_config_int("ramp_step_percent", 5, minimum=1)
    else:
        ramp_step = safe_int(ramp_step, 5, minimum=1)

    if not winter_active:
        return summer_min_soc

    if current_soc >= winter_min_soc:
        return winter_min_soc

    if current_soc > effective_min_soc + ramp_step:
        return min(current_soc, winter_min_soc)

    return min(effective_min_soc + ramp_step, winter_min_soc)


def estimate_winter_ramp_days(current_min_soc):
    """Estimate remaining daily adjustments until winter minSoc is reached."""

    winter_min_soc = winter_config_int("winter_min_soc", 40, minimum=0)
    ramp_step = winter_config_int("ramp_step_percent", 5, minimum=1)
    remaining = max(0, winter_min_soc - current_min_soc)

    return int((remaining + ramp_step - 1) / ramp_step)


def winter_adjustment_window_active(now):
    adjust_hour = winter_config_int("adjust_hour", 12, minimum=0) % 24
    return adjust_hour <= now.hour < adjust_hour + 1


def build_winter_ac_charge_limit_payload():
    return {
        "inputLimit": winter_config_int("ac_charge_power", 200, minimum=0)
    }


def merge_runtime_defaults(data, defaults):
    """Merge runtime data over defaults while preserving unknown keys."""

    if not isinstance(data, dict):
        data = {}

    merged = dict(data)

    system = merged.get("system")
    if not isinstance(system, dict):
        system = {}

    merged["system"] = {
        **defaults.get("system", {}),
        **system
    }

    for section_name in ("ha", "winter"):
        section = merged.get(section_name)
        if not isinstance(section, dict):
            section = {}

        merged[section_name] = {
            **defaults.get(section_name, {}),
            **section
        }

    devices = merged.get("devices")
    if not isinstance(devices, dict):
        devices = {}

    merged_devices = {}

    for name, device_defaults in defaults.get("devices", {}).items():
        device_state = devices.get(name)
        if not isinstance(device_state, dict):
            device_state = {}

        merged_devices[name] = {
            **device_defaults,
            **device_state
        }
        merged_devices[name].pop("offgrid_socket", None)

    for name, device_state in devices.items():
        if name not in merged_devices:
            merged_devices[name] = device_state
            if isinstance(merged_devices[name], dict):
                merged_devices[name].pop("offgrid_socket", None)

    merged["devices"] = merged_devices

    return merged


class RuntimeState:
    """Persist mutable operator state outside static config."""

    def __init__(self, path, defaults):
        self.path = path
        self.tmp_path = f"{path}.tmp"
        self.defaults = defaults
        self.data = merge_runtime_defaults({}, defaults)
        self.last_mtime = None

    def load_or_create(self):
        if not os.path.exists(self.path):
            self.data = merge_runtime_defaults({}, self.defaults)
            log_event(
                logging.INFO,
                "runtime_state_created",
                path=self.path
            )
            self.save_atomic()
            return self.data

        return self.load_if_changed(force=True)

    def load_if_changed(self, force=False):
        try:
            mtime = os.path.getmtime(self.path)
        except FileNotFoundError:
            return self.load_or_create()

        if not force and self.last_mtime == mtime:
            return self.data

        try:
            with open(self.path) as f:
                loaded = json.load(f)

            if not force and self.last_mtime is not None:
                log_event(
                    logging.INFO,
                    "runtime_state_changed",
                    path=self.path
                )

            self.data = merge_runtime_defaults(loaded, self.defaults)
            self.last_mtime = mtime

            log_event(
                logging.INFO,
                "runtime_state_loaded",
                path=self.path
            )

        except Exception as e:
            log_event(
                logging.WARNING,
                "runtime_state_load_error",
                path=self.path,
                error=e
            )

        return self.data

    def save_atomic(self):
        parent = os.path.dirname(self.path)
        if parent:
            os.makedirs(parent, exist_ok=True)

        with open(self.tmp_path, "w") as f:
            json.dump(self.data, f, indent=2, sort_keys=True)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())

        os.replace(self.tmp_path, self.path)
        self.last_mtime = os.path.getmtime(self.path)

        log_event(
            logging.INFO,
            "runtime_state_saved",
            path=self.path
        )

    def get_system(self, key, default=None):
        system = self.data.get("system", {})
        if not isinstance(system, dict):
            return default

        return system.get(key, default)

    def get_section(self, section_name, key, default=None):
        section = self.data.get(section_name, {})
        if not isinstance(section, dict):
            return default

        return section.get(key, default)

    def set_system(self, key, value):
        system = self.data.setdefault("system", {})
        previous = system.get(key)
        system[key] = value
        return previous != value

    def set_section(self, section_name, key, value):
        section = self.data.setdefault(section_name, {})
        previous = section.get(key)
        section[key] = value
        return previous != value

    def get_device(self, device_name, key, default=None):
        devices = self.data.get("devices", {})
        device = devices.get(device_name, {})

        if not isinstance(device, dict):
            return default

        return device.get(key, default)

    def set_device(self, device_name, key, value):
        devices = self.data.setdefault("devices", {})
        device = devices.setdefault(device_name, {})
        previous = device.get(key)
        device[key] = value
        return previous != value


def build_runtime_defaults(devices):
    """Build runtime defaults from current device configuration."""

    device_defaults = {}

    for dev in devices:
        device_defaults[dev.name] = {
            "enabled": True,
            "max_power": safe_int(
                getattr(dev, "max_power", MAX_DEVICE_POWER),
                MAX_DEVICE_POWER,
                minimum=0
            ),
            "offgrid_socket_mode": "off"
        }

    return {
        "system": {
            "enabled": SYSTEM_ENABLED,
            "max_total_power": MAX_TOTAL_POWER,
            "loop_interval": LOOP_INTERVAL,
            "min_output_limit": MIN_OUTPUT_LIMIT
        },
        "ha": {
            "enabled": CONFIG["ha"].get("enabled", True),
            "control_enabled": CONFIG["ha"].get("control_enabled", True)
        },
        "winter": {
            "enabled": winter_config_bool("enabled", False)
        },
        "devices": device_defaults
    }


def zendure_write_succeeded(error_event, dev, response, **fields):
    """Log failed Zendure write responses and return success state."""

    status_code = getattr(response, "status_code", 0)

    if status_code < 300:
        return True

    response_text = getattr(response, "text", "") or ""
    fields.setdefault("device", dev.name)

    log_event(
        logging.WARNING,
        error_event,
        status_code=status_code,
        response_text=response_text[:200],
        **fields
    )

    return False

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
            r = self.session.post(
                f"{self.base_url}/api/states/{entity_id}",
                headers=self.headers,
                json=payload,
                timeout=2
            )

            if r.status_code >= 300:
                log_event(
                    logging.WARNING,
                    "ha_write_error",
                    entity=entity_id,
                    status_code=r.status_code
                )

        except Exception as e:
            log_event(
                logging.WARNING,
                "ha_write_error",
                entity=entity_id,
                error=e
            )

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

        remain_minutes=round((props.get("remainOutTime") or 0), 1),

        solar1=props.get("solarPower1") or 0,
        solar2=props.get("solarPower2") or 0,
        solar3=props.get("solarPower3") or 0,
        solar4=props.get("solarPower4") or 0,

        output_limit=props.get("outputLimit") or 0,
        soc_limit=props.get("socLimit") or 0,
        pack_state=props.get("packState") or 0,

        fault_level=props.get("faultLevel") or 0,

        smart_mode=props.get("smartMode") or 0,
        grid_off_mode=props.get("gridOffMode") or 0,
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

    fault_observed = state.fault_level > 0
    pv_evidence = state.solar > 0
    output_evidence = state.output > 0
    output_limit_evidence = state.output_limit > 0
    ac_evidence = state.ac_status != 0
    discharge_evidence = (
        state.dc_status != 0
        or state.pack_in > 0
        or output_evidence
    )
    export_evidence = (
        pv_evidence
        or output_evidence
        or output_limit_evidence
        or ac_evidence
    )
    paths_inactive = state.dc_status == 0 and state.ac_status == 0

    # faultLevel is observed firmware telemetry. Live testing showed it may be
    # present while output/PV export is still possible, so it is logged as a
    # warning signal instead of being used as a blanket capability blocker.
    can_charge = state.soc_limit != 1
    can_discharge = (
        state.soc_limit != 2
        and discharge_evidence
    )
    can_export = export_evidence or (
        state.soc_limit != 2
        and not paths_inactive
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

    if fault_observed:
        reasons.append("fault_observed")

    if pv_evidence:
        reasons.append("pv_evidence")

    if output_evidence:
        reasons.append("output_evidence")

    if output_limit_evidence:
        reasons.append("output_limit_evidence")

    if ac_evidence:
        reasons.append("ac_evidence")

    return DeviceCapabilities(
        can_charge=can_charge,
        can_discharge=can_discharge,
        can_export=can_export,
        can_ac_charge=can_ac_charge,
        reason=",".join(reasons) if reasons else "normal"
    )


def derive_soc_runtime_state(state):
    """Classify SOC telemetry for diagnostics only."""

    if state.soc_limit == 1 or state.soc >= state.max_soc:
        return "soc_full"

    if state.soc_limit == 2 or state.soc <= state.min_soc:
        return "soc_empty"

    return "soc_normal"


def firmware_recovery_or_ac_charge_active(state):
    """Return True when firmware appears to be handling charge/recovery."""

    return (
        int(state.ac_status) == 2
        or int(state.soc_limit) == 2
        or (
            state.min_soc > 0
            and state.soc <= state.min_soc
        )
        or state.pack_out > 0
    )


def startup_ac_mode_initialization_blocker(state):
    """Return the reason acMode startup initialization should be skipped."""

    if int(state.ac_mode) != 1:
        return "unknown_or_unsupported_ac_mode"

    if int(state.ac_status) == 2:
        return "ac_charge_active"

    if int(state.soc_limit) == 2:
        return "discharge_cutoff"

    if state.min_soc > 0 and state.soc <= state.min_soc:
        return "soc_at_or_below_min"

    if state.output > 0:
        return "output_active"

    if state.pack_in > 0:
        return "battery_discharge_active"

    if state.pack_out > 0:
        return "battery_charge_active"

    return None

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


def fetch_all_devices(devices):
    """Fetch all device states in parallel."""

    results = [None] * len(devices)

    if not devices:
        return results

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


def calculate_remaining_time_hours(state, device_config, avg_battery_power_w):
    """Estimate remaining charge/discharge time from smoothed battery power."""

    battery_kwh = getattr(device_config, "battery_kwh", 0) or 0

    if battery_kwh <= 0:
        return 0

    if abs(avg_battery_power_w) < REMAINING_TIME_MIN_POWER_W:
        return 0

    if avg_battery_power_w > 0:
        max_soc = getattr(device_config, "max_soc", 0) or state.max_soc or 100
        soc_delta = max(0, max_soc - state.soc)
        power_kw = avg_battery_power_w / 1000
    else:
        min_soc = getattr(device_config, "min_soc", 0) or state.min_soc or 0
        soc_delta = max(0, state.soc - min_soc)
        power_kw = abs(avg_battery_power_w) / 1000

    if soc_delta <= 0 or power_kw <= 0:
        return 0

    energy_kwh = battery_kwh * soc_delta / 100
    hours = energy_kwh / power_kw

    return round(min(REMAINING_TIME_MAX_HOURS, hours), 1)


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


def pv_first_weight(pv_only, device_config):
    """Return the weighted PV-first allocation signal."""

    if pv_only <= 0:
        return 0

    if not PV_KWP_WEIGHTING:
        return pv_only

    return (
        pv_only
        * get_device_pv_kwp(device_config)
        * get_device_pv_priority_factor(device_config)
    )


def weighted_limited_allocation(total, weights, limits):
    """Allocate a total by weights while preserving per-device limits."""

    allocation = [0] * len(weights)
    active = [
        i
        for i, limit in enumerate(limits)
        if limit > 0 and weights[i] > 0
    ]
    remaining = total

    while active and remaining > 0:
        weight_total = sum(weights[i] for i in active)

        if weight_total <= 0:
            break

        saturated = []
        proposed = {}

        for i in active:
            share = remaining * weights[i] / weight_total
            headroom = limits[i] - allocation[i]

            if share >= headroom:
                proposed[i] = headroom
                saturated.append(i)
            else:
                proposed[i] = share

        if not saturated:
            for i, value in proposed.items():
                allocation[i] += value
            break

        for i in saturated:
            moved = proposed[i]
            allocation[i] += moved
            remaining -= moved

        active = [
            i
            for i in active
            if i not in saturated and allocation[i] < limits[i]
        ]

    return allocation


def apply_battery_topup_after_pv_first(
    targets,
    states,
    device_configs,
    capabilities,
    requested_total
):
    """Top up PV-first targets with battery power where safely available."""

    deliverable_targets = []

    for i, target in enumerate(targets):
        dev_config = device_configs[i] if device_configs else None
        cap = capabilities[i] if capabilities else None
        max_power = get_device_max_power(dev_config)

        if cap and not cap.can_export:
            max_power = 0

        deliverable_targets.append(max(0, min(max_power, target)))

    targets = deliverable_targets
    pv_first_total = sum(targets)
    missing = max(0, requested_total - pv_first_total)

    if missing <= 0:
        return targets

    weights = []
    limits = []
    reasons = []

    for i, state in enumerate(states):
        dev_config = device_configs[i] if device_configs else None
        cap = capabilities[i] if capabilities else None
        device_name = dev_config.name if dev_config else i
        max_power = get_device_max_power(dev_config)
        headroom = max(0, max_power - targets[i])

        if cap and not cap.can_export:
            weights.append(0)
            limits.append(0)
            reasons.append(f"{device_name}:cannot_export")
            continue

        if cap and not cap.can_discharge:
            weights.append(0)
            limits.append(0)
            reasons.append(f"{device_name}:cannot_discharge")
            continue

        if state.soc <= state.min_soc:
            weights.append(0)
            limits.append(0)
            reasons.append(f"{device_name}:soc_at_or_below_min")
            continue

        if headroom <= 0:
            weights.append(0)
            limits.append(0)
            reasons.append(f"{device_name}:no_headroom")
            continue

        weight = usable_battery_weight(
            state,
            dev_config,
            cap
        )

        weights.append(weight)
        limits.append(headroom)

        if weight <= 0:
            reasons.append(f"{device_name}:no_usable_battery")

    topup = weighted_limited_allocation(
        missing,
        weights,
        limits
    )
    topup_total = sum(topup)

    if topup_total > 0:
        targets = [
            target + add
            for target, add in zip(targets, topup)
        ]

        log_event(
            logging.INFO,
            "pv_first_battery_topup",
            requested_total=requested_total,
            pv_first_total=round(pv_first_total),
            topup_w=round(topup_total),
            final_targets=json.dumps([round(t) for t in targets])
        )

    unmet = max(0, requested_total - sum(targets))

    if unmet > 0:
        reason = "no_topup_candidates" if topup_total <= 0 else "topup_limited"
        if reasons:
            reason = f"{reason}:{','.join(reasons)}"

        log_event(
            logging.WARNING,
            "pv_first_battery_topup_unmet",
            requested_total=requested_total,
            pv_first_total=round(pv_first_total),
            topup_w=round(topup_total),
            unmet_w=round(unmet),
            final_targets=json.dumps([round(t) for t in targets]),
            reason=reason
        )

    return targets


def apply_constraints_and_redistribute(
    targets,
    device_configs=None,
    capabilities=None,
    target_limits=None
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

        if target_limits:
            limit = min(limit, max(0, target_limits[i]))

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


def apply_min_output_limit(target, device, min_output_limit):
    """Apply the configured minimum outputLimit for enabled EMS control."""

    if min_output_limit <= 0:
        return target

    guarded_target = max(target, min_output_limit)

    if guarded_target != target:
        log_event(
            logging.INFO,
            "min_output_limit_applied",
            device=device.name,
            original_target_w=target,
            guarded_target_w=guarded_target,
            min_output_limit_w=min_output_limit
        )

    return guarded_target


def calculate_targets(
    load,
    devices,
    max_power,
    device_configs=None,
    capabilities=None,
    requested_total=None
):
    """
    Intelligent EMS target calculation.

    Strategy:

    Solar surplus:
    - allocate by weighted PV-only contribution

    Battery discharge:
    - allocate only confirmed usable battery energy

    This avoids:
    - cross-device battery charge/discharge churn
    - overusing nearly empty batteries
    """

    if not devices:
        log_event(
            logging.WARNING,
            "no_devices",
            load=load,
            max_power=max_power
        )
        return [], 0, 0

    current_total = sum(d.output for d in devices)
    solar_total = sum(d.solar for d in devices)

    if requested_total is None:
        new_total = max(
            0,
            min(max_power, current_total + load)
        )
    else:
        new_total = max(
            0,
            min(max_power, requested_total)
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
        pv_weights = []

        for i, d in enumerate(devices):
            cap = capabilities[i] if capabilities else None
            dev_config = device_configs[i] if device_configs else None

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
            pv_weight = pv_first_weight(pv_only, dev_config)
            pv_weights.append(pv_weight)

            log_event(
                logging.DEBUG,
                "pv_first_limit",
                device=device_configs[i].name if device_configs else i,
                solar_w=d.solar,
                pack_input_w=d.pack_in,
                output_w=d.output,
                output_limit_w=d.output_limit,
                pv_only_limit_w=round(pv_only),
                pv_weight=round(pv_weight, 3),
                pv_kwp=get_device_pv_kwp(dev_config),
                pv_priority_factor=get_device_pv_priority_factor(dev_config),
                soc=d.soc,
                pack_state=d.pack_state,
                soc_limit=d.soc_limit,
                can_export=cap.can_export if cap else True,
                can_discharge=cap.can_discharge if cap else True
            )

        pv_only_total = sum(pv_only_limits)

        if pv_only_total > 0:
            targets = weighted_limited_allocation(
                new_total,
                pv_weights,
                pv_only_limits
            )

            pv_unmet = new_total - sum(targets)

            if pv_unmet > 0:
                log_event(
                    logging.WARNING,
                    "pv_first_limited",
                    requested_total=new_total,
                    pv_only_total=round(pv_only_total),
                    unmet_w=round(pv_unmet)
                )

            targets = apply_battery_topup_after_pv_first(
                targets,
                devices,
                device_configs,
                capabilities,
                new_total
            )

        else:

            targets = [0] * len(devices)

            if new_total > 0:
                log_event(
                    logging.WARNING,
                    "pv_first_limited",
                    requested_total=new_total,
                    pv_only_total=0,
                    unmet_w=round(new_total)
                )

                targets = apply_battery_topup_after_pv_first(
                    targets,
                    devices,
                    device_configs,
                    capabilities,
                    new_total
                )

    # =====================
    # CASE 2:
    # Battery discharge required
    # =====================

    else:

        targets = []

        for i, d in enumerate(devices):
            cap = capabilities[i] if capabilities else None
            targets.append(
                d.solar
                if not cap or cap.can_export
                else 0
            )

        exportable_solar_total = sum(targets)
        remaining = max(0, new_total - exportable_solar_total)

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

        if weight_total > 0:
            for i, d in enumerate(devices):

                share = weights[i] / weight_total

                targets[i] += remaining * share
        elif remaining > 0:
            log_event(
                logging.WARNING,
                "no_discharge_capacity",
                requested_total=new_total,
                exportable_solar_total=exportable_solar_total,
                unmet_w=round(remaining)
            )

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

    def __init__(
        self,
        devices,
        shelly,
        ha=None,
        sleep_enabled=True,
        runtime_state=None
    ):
        self.devices = devices
        self.shelly = shelly
        self.ha = ha
        self.sleep_enabled = sleep_enabled
        self.runtime_state = runtime_state
        self.soc_reconcile_counter = SOC_RECONCILE_INTERVAL

        self.last_states = {}
        self.last_seen = {}
        self.device_online = {}
        self.battery_power_history = {}
        self.initial_ac_mode_reconciled = {}
        self.last_ha_seen = {}
        self.last_ha_written = {}
        self.commanded_total_w = None
        self.filtered_load_w = None
        self.load_history = deque(
            maxlen=safe_int(
                OUTPUT_CONTROL_CONFIG.get("median_window", 3),
                3,
                minimum=1
            )
        )
        self.commanded_device_targets = {}
        self.last_output_write_at = {}
        self.last_winter_adjust_date = None
        self.winter_min_soc_targets = {}

    def output_control_bool(self, key, default=False):
        return safe_bool(
            OUTPUT_CONTROL_CONFIG.get(key, default),
            default
        )

    def output_control_int(self, key, default=0, minimum=0):
        return safe_int(
            OUTPUT_CONTROL_CONFIG.get(key, default),
            default,
            minimum=minimum
        )

    def output_control_float(self, key, default=0.0, minimum=0.0):
        return safe_float(
            OUTPUT_CONTROL_CONFIG.get(key, default),
            default,
            minimum=minimum
        )

    def output_control_bypass_active(self, raw_load):
        import_limit = self.output_control_float(
            "large_import_bypass_w",
            600,
            minimum=0
        )
        export_limit = self.output_control_float(
            "large_export_bypass_w",
            500,
            minimum=0
        )

        return raw_load >= import_limit or raw_load <= -export_limit

    def filter_output_control_load(self, raw_load):
        if not self.output_control_bool("filter_enabled", True):
            return raw_load

        window = self.output_control_int("median_window", 3, minimum=1)

        if self.load_history.maxlen != window:
            self.load_history = deque(
                list(self.load_history)[-window:],
                maxlen=window
            )

        self.load_history.append(raw_load)
        values = sorted(self.load_history)
        mid = len(values) // 2

        if len(values) % 2:
            median = values[mid]
        else:
            median = (values[mid - 1] + values[mid]) / 2

        method = str(
            OUTPUT_CONTROL_CONFIG.get("filter_method", "median_ema")
        )

        if method != "median_ema":
            return raw_load

        alpha = min(
            1.0,
            self.output_control_float("ema_alpha", 0.65, minimum=0.0)
        )

        if self.filtered_load_w is None:
            self.filtered_load_w = median
        else:
            self.filtered_load_w = (
                alpha * median
                + (1 - alpha) * self.filtered_load_w
            )

        return self.filtered_load_w

    def initialize_commanded_total(self, states, max_power):
        limit_total = sum(
            state.output_limit
            for state in states
            if state.output_limit > 0
        )

        if limit_total > 0:
            initial = limit_total
            source = "output_limit"
        else:
            initial = sum(state.output for state in states)
            source = "output"

        self.commanded_total_w = max(0, min(max_power, initial))

        log_event(
            logging.INFO,
            "output_control_state",
            initialized=True,
            initialization_source=source,
            commanded_total_w=round(self.commanded_total_w, 1),
            raw_load_w=0,
            filtered_load_w=0,
            desired_total_w=round(self.commanded_total_w, 1),
            ramped_total_w=round(self.commanded_total_w, 1)
        )

    def telemetry_stale(self):
        max_age = self.output_control_float(
            "telemetry_max_age_seconds",
            10,
            minimum=0
        )

        if max_age <= 0:
            log_event(
                logging.WARNING,
                "output_control_stale_telemetry",
                device="all",
                age_s=0,
                max_age_s=max_age
            )
            return True

        now = time.time()
        stale = False

        for dev in self.devices:
            seen = self.last_seen.get(dev.name)
            if not seen:
                continue

            age = now - seen

            if age > max_age:
                stale = True
                log_event(
                    logging.WARNING,
                    "output_control_stale_telemetry",
                    device=dev.name,
                    age_s=round(age, 1),
                    max_age_s=max_age
                )

        return stale

    def stabilized_total_target(self, raw_load, states, max_power):
        if self.commanded_total_w is None:
            self.initialize_commanded_total(states, max_power)

        filtered_load = self.filter_output_control_load(raw_load)
        desired = self.commanded_total_w
        held = False

        load_deadband = self.output_control_float(
            "load_deadband_w",
            5,
            minimum=0
        )

        if abs(filtered_load) <= load_deadband:
            held = True
            log_event(
                logging.INFO,
                "output_control_deadband_hold",
                reason="load_deadband",
                raw_load_w=round(raw_load, 1),
                filtered_load_w=round(filtered_load, 1),
                commanded_total_w=round(self.commanded_total_w, 1),
                deadband_w=load_deadband
            )
        else:
            desired = self.commanded_total_w + filtered_load

        desired = max(0, min(max_power, desired))

        target_deadband = self.output_control_float(
            "target_deadband_w",
            10,
            minimum=0
        )

        if (
            not held
            and abs(desired - self.commanded_total_w) <= target_deadband
        ):
            desired = self.commanded_total_w
            held = True
            log_event(
                logging.INFO,
                "output_control_deadband_hold",
                reason="target_deadband",
                raw_load_w=round(raw_load, 1),
                filtered_load_w=round(filtered_load, 1),
                commanded_total_w=round(self.commanded_total_w, 1),
                deadband_w=target_deadband
            )

        ramped = desired
        delta = desired - self.commanded_total_w
        bypass = self.output_control_bypass_active(raw_load)
        stale = self.telemetry_stale()

        if bypass:
            log_event(
                logging.INFO,
                "output_control_bypass",
                raw_load_w=round(raw_load, 1),
                filtered_load_w=round(filtered_load, 1)
            )

        if (
            not held
            and self.output_control_bool("ramp_enabled", True)
            and delta != 0
        ):
            if delta > 0:
                ramp_limit = self.output_control_float(
                    "ramp_up_w_per_cycle",
                    300,
                    minimum=0
                )
            else:
                ramp_limit = self.output_control_float(
                    "ramp_down_w_per_cycle",
                    500,
                    minimum=0
                )

            if bypass:
                ramp_limit *= self.output_control_float(
                    "bypass_ramp_multiplier",
                    1.5,
                    minimum=1.0
                )

            if stale:
                ramp_limit *= self.output_control_float(
                    "stale_telemetry_ramp_factor",
                    0.5,
                    minimum=0.0
                )

            if ramp_limit > 0 and abs(delta) > ramp_limit:
                ramped = (
                    self.commanded_total_w + ramp_limit
                    if delta > 0
                    else self.commanded_total_w - ramp_limit
                )
                log_event(
                    logging.INFO,
                    "output_control_ramp_limited",
                    previous_total_w=round(self.commanded_total_w, 1),
                    desired_total_w=round(desired, 1),
                    ramped_total_w=round(ramped, 1),
                    ramp_limit_w=round(ramp_limit, 1)
                )

        ramped = max(0, min(max_power, ramped))

        log_event(
            logging.INFO,
            "output_control_state",
            initialized=False,
            raw_load_w=round(raw_load, 1),
            filtered_load_w=round(filtered_load, 1),
            commanded_total_w=round(self.commanded_total_w, 1),
            desired_total_w=round(desired, 1),
            ramped_total_w=round(ramped, 1)
        )

        self.commanded_total_w = ramped
        return ramped

    def apply_device_ramp(self, targets, raw_load):
        if not self.output_control_bool("device_ramp_enabled", True):
            for dev, target in zip(self.devices, targets):
                self.commanded_device_targets[dev.name] = target
            return targets

        bypass = self.output_control_bypass_active(raw_load)
        multiplier = (
            self.output_control_float(
                "bypass_ramp_multiplier",
                1.5,
                minimum=1.0
            )
            if bypass
            else 1.0
        )
        up_limit = (
            self.output_control_float(
                "device_ramp_up_w_per_cycle",
                250,
                minimum=0
            )
            * multiplier
        )
        down_limit = (
            self.output_control_float(
                "device_ramp_down_w_per_cycle",
                400,
                minimum=0
            )
            * multiplier
        )
        ramped_targets = []

        for dev, target in zip(self.devices, targets):
            previous = self.commanded_device_targets.get(dev.name)

            if previous is None:
                self.commanded_device_targets[dev.name] = target
                ramped_targets.append(target)
                continue

            delta = target - previous
            limit = up_limit if delta > 0 else down_limit
            ramped = target

            if limit > 0 and abs(delta) > limit:
                ramped = previous + limit if delta > 0 else previous - limit
                log_event(
                    logging.INFO,
                    "output_control_device_ramp_limited",
                    device=dev.name,
                    previous_target_w=round(previous),
                    desired_target_w=round(target),
                    ramped_target_w=round(ramped),
                    ramp_limit_w=round(limit)
                )

            ramped = max(0, min(dev.max_power, ramped))
            self.commanded_device_targets[dev.name] = ramped
            ramped_targets.append(ramped)

        return ramped_targets

    def runtime_system_bool(self, key, default):
        if not self.runtime_state:
            return default

        return safe_bool(
            self.runtime_state.get_system(key, default),
            default
        )

    def runtime_system_int(self, key, default, minimum=0):
        if not self.runtime_state:
            return default

        return safe_int(
            self.runtime_state.get_system(key, default),
            default,
            minimum=minimum
        )

    def runtime_section_bool(self, section_name, key, default):
        if not self.runtime_state:
            return default

        return safe_bool(
            self.runtime_state.get_section(section_name, key, default),
            default
        )

    def runtime_ha_enabled(self):
        return self.runtime_section_bool(
            "ha",
            "enabled",
            CONFIG["ha"].get("enabled", True)
        )

    def runtime_ha_control_enabled(self):
        return self.runtime_section_bool(
            "ha",
            "control_enabled",
            CONFIG["ha"].get("control_enabled", True)
        )

    def runtime_device_bool(self, device_name, key, default):
        if not self.runtime_state:
            return default

        return safe_bool(
            self.runtime_state.get_device(device_name, key, default),
            default
        )

    def runtime_device_int(self, device_name, key, default, minimum=0):
        if not self.runtime_state:
            return default

        return safe_int(
            self.runtime_state.get_device(device_name, key, default),
            default,
            minimum=minimum
        )

    def ha_update_runtime_field(
        self,
        entity_id,
        runtime_getter,
        runtime_setter,
        parser,
        formatter=lambda value: value
    ):
        """Synchronize one HA helper with one runtime-state field."""

        if not self.ha or not HA_CONTROL_ENABLED or not self.runtime_state:
            return False

        try:
            ha_state = self.ha.get_state(entity_id)
        except Exception as e:
            log_event(
                logging.WARNING,
                "runtime_state_ha_read_error",
                entity=entity_id,
                error=e
            )
            return False

        runtime_value = runtime_getter()

        if ha_state is None:
            return False

        try:
            parsed_ha = parser(ha_state, runtime_value)
        except Exception as e:
            log_event(
                logging.WARNING,
                "runtime_state_ha_read_error",
                entity=entity_id,
                state=ha_state,
                error=e
            )
            return False

        last_written = self.last_ha_written.get(entity_id)
        last_seen = self.last_ha_seen.get(entity_id)

        if (
            last_seen is not None
            and parsed_ha != last_seen
            and parsed_ha != last_written
        ):
            changed = runtime_setter(parsed_ha)
            self.last_ha_seen[entity_id] = parsed_ha

            if changed:
                log_event(
                    logging.INFO,
                    "runtime_state_ha_sync",
                    direction="ha_to_runtime",
                    entity=entity_id,
                    value=parsed_ha
                )

            return changed

        if parsed_ha != runtime_value and runtime_value != last_written:
            self.ha.set_state(entity_id, formatter(runtime_value))
            self.last_ha_written[entity_id] = runtime_value
            self.last_ha_seen[entity_id] = runtime_value
            log_event(
                logging.INFO,
                "runtime_state_ha_write",
                entity=entity_id,
                value=runtime_value
            )
            return False

        self.last_ha_seen[entity_id] = parsed_ha
        return False

    def sync_ha_runtime_state(self):
        """Use HA helpers as an optional UI over runtime-state."""

        if not self.ha or not HA_CONTROL_ENABLED or not self.runtime_state:
            return

        changed = False

        changed |= self.ha_update_runtime_field(
            "input_boolean.ems_solarflow_ha_enabled",
            lambda: self.runtime_ha_enabled(),
            lambda value: self.runtime_state.set_section(
                "ha",
                "enabled",
                value
            ),
            lambda value, default: safe_bool(value, default),
            lambda value: "on" if value else "off"
        )

        changed |= self.ha_update_runtime_field(
            "input_boolean.ems_solarflow_ha_control_enabled",
            lambda: self.runtime_ha_control_enabled(),
            lambda value: self.runtime_state.set_section(
                "ha",
                "control_enabled",
                value
            ),
            lambda value, default: safe_bool(value, default),
            lambda value: "on" if value else "off"
        )

        if (
            not self.runtime_ha_enabled()
            or not self.runtime_ha_control_enabled()
        ):
            if changed:
                self.runtime_state.save_atomic()
            return

        changed |= self.ha_update_runtime_field(
            "input_boolean.ems_solarflow_enable",
            lambda: self.runtime_system_bool("enabled", SYSTEM_ENABLED),
            lambda value: self.runtime_state.set_system("enabled", value),
            lambda value, default: safe_bool(value, default),
            lambda value: "on" if value else "off"
        )

        changed |= self.ha_update_runtime_field(
            "input_number.ems_solarflow_max_power",
            lambda: self.runtime_system_int(
                "max_total_power",
                MAX_TOTAL_POWER,
                minimum=0
            ),
            lambda value: self.runtime_state.set_system(
                "max_total_power",
                value
            ),
            lambda value, default: safe_int(value, default, minimum=0)
        )

        changed |= self.ha_update_runtime_field(
            "input_number.ems_solarflow_interval",
            lambda: self.runtime_system_int(
                "loop_interval",
                LOOP_INTERVAL,
                minimum=1
            ),
            lambda value: self.runtime_state.set_system(
                "loop_interval",
                value
            ),
            lambda value, default: safe_int(value, default, minimum=1)
        )

        changed |= self.ha_update_runtime_field(
            "input_number.ems_solarflow_min_output_limit",
            lambda: self.runtime_system_int(
                "min_output_limit",
                MIN_OUTPUT_LIMIT,
                minimum=0
            ),
            lambda value: self.runtime_state.set_system(
                "min_output_limit",
                value
            ),
            lambda value, default: safe_int(value, default, minimum=0)
        )

        changed |= self.ha_update_runtime_field(
            "input_boolean.ems_solarflow_winter_enabled",
            lambda: winter_feature_enabled(self.runtime_state),
            lambda value: self.runtime_state.set_section(
                "winter",
                "enabled",
                value
            ),
            lambda value, default: safe_bool(value, default),
            lambda value: "on" if value else "off"
        )

        for dev in self.devices:
            base = f"ems_solarflow_{dev.name.lower()}"

            changed |= self.ha_update_runtime_field(
                f"input_boolean.{base}_enabled",
                lambda dev=dev: self.runtime_device_bool(
                    dev.name,
                    "enabled",
                    True
                ),
                lambda value, dev=dev: self.runtime_state.set_device(
                    dev.name,
                    "enabled",
                    value
                ),
                lambda value, default: safe_bool(value, default),
                lambda value: "on" if value else "off"
            )

            changed |= self.ha_update_runtime_field(
                f"input_number.{base}_max_power",
                lambda dev=dev: self.runtime_device_int(
                    dev.name,
                    "max_power",
                    dev.max_power,
                    minimum=0
                ),
                lambda value, dev=dev: self.runtime_state.set_device(
                    dev.name,
                    "max_power",
                    value
                ),
                lambda value, default: safe_int(value, default, minimum=0)
            )

            changed |= self.ha_update_runtime_field(
                f"input_select.{base}_offgrid_socket_mode",
                lambda dev=dev: str(
                    self.runtime_state.get_device(
                        dev.name,
                        "offgrid_socket_mode",
                        "off"
                    )
                ),
                lambda value, dev=dev: self.runtime_state.set_device(
                    dev.name,
                    "offgrid_socket_mode",
                    value
                ),
                lambda value, default: (
                    str(value).strip().lower()
                    if str(value).strip().lower() in OFFGRID_SOCKET_MODES
                    else default
                )
            )

        if changed:
            self.runtime_state.save_atomic()

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
            response = dev.session.post(
                f"http://{dev.ip}/properties/write",
                json={
                    "sn": dev.sn,
                    "properties": {
                        "outputLimit": int(value)
                    }
                },
                timeout=2
            )

            if not zendure_write_succeeded(
                "write_output_limit_error",
                dev,
                response,
                target_w=value
            ):
                return

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

    def apply_soc_limits(self, dev, state, desired_min_soc=None):
        """Apply configured SOC limits if required."""

        effective_min_soc = (
            safe_int(desired_min_soc, dev.min_soc, minimum=0)
            if desired_min_soc is not None
            else dev.min_soc
        )

        #
        # 0 = unmanaged
        #

        if effective_min_soc <= 0 and dev.max_soc <= 0:
            return

        #
        # Already configured
        #

        if (
            int(state.min_soc) == int(effective_min_soc)
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
                min_soc=effective_min_soc,
                max_soc=dev.max_soc,
                max_soc_property="socSet",
                dry_run=DRY_RUN,
                simulation=SIMULATION_MODE,
                allow_hardware_writes=ALLOW_HARDWARE_WRITES,
                allow_state_reconciliation_writes=(
                    ALLOW_STATE_RECONCILIATION_WRITES
                )
            )
            return

        try:

            response = dev.session.post(
                f"http://{dev.ip}/properties/write",
                json={
                    "sn": dev.sn,
                    "properties": {
                        "minSoc": int(effective_min_soc * 10),
                        "socSet": int(dev.max_soc * 10)
                    }
                },
                timeout=2
            )

            if not zendure_write_succeeded(
                "write_soc_limits_error",
                dev,
                response,
                min_soc=effective_min_soc,
                max_soc=dev.max_soc,
                max_soc_property="socSet"
            ):
                return

            log_event(
                logging.INFO,
                "write_soc_limits",
                device=dev.name,
                min_soc=effective_min_soc,
                max_soc=dev.max_soc,
                max_soc_property="socSet"
            )

        except Exception as e:

            log_event(
                logging.WARNING,
                "write_soc_limits_error",
                device=dev.name,
                min_soc=effective_min_soc,
                max_soc=dev.max_soc,
                max_soc_property="socSet",
                error=e
            )

    def winter_reconciliation_target(self, dev, state, winter_active, adjust_today):
        """Return desired winter/summer minSoc target and adjustment context."""

        if not winter_feature_enabled(self.runtime_state):
            return None, False

        summer_min_soc = winter_config_int("summer_min_soc", 15, minimum=0)

        if not winter_active:
            had_target = dev.name in self.winter_min_soc_targets
            self.winter_min_soc_targets.pop(dev.name, None)

            if had_target or int(state.min_soc) != int(summer_min_soc):
                log_event(
                    logging.INFO,
                    "winter_summer_reset",
                    device=dev.name,
                    current_min_soc=state.min_soc,
                    target_min_soc=summer_min_soc
                )

            return summer_min_soc, False

        if dev.name in self.winter_min_soc_targets and not adjust_today:
            return self.winter_min_soc_targets[dev.name], False

        if not adjust_today:
            return None, False

        effective_min_soc = self.winter_min_soc_targets.get(
            dev.name,
            state.min_soc if state.min_soc > 0 else dev.min_soc
        )
        target = calculate_winter_min_soc_target(
            state.soc,
            effective_min_soc,
            winter_active
        )
        self.winter_min_soc_targets[dev.name] = target

        log_event(
            logging.INFO,
            "winter_ramp",
            device=dev.name,
            current_soc=state.soc,
            current_min_soc=state.min_soc,
            effective_min_soc=effective_min_soc,
            target_min_soc=target,
            winter_min_soc=winter_config_int("winter_min_soc", 40, minimum=0),
            estimated_days_remaining=estimate_winter_ramp_days(target)
        )

        return target, True

    def apply_winter_ac_charge_limit(self, dev):
        """Apply conservative winter AC charge input limit."""

        properties = build_winter_ac_charge_limit_payload()
        fields = {
            "device": dev.name,
            "input_limit_w": properties["inputLimit"]
        }

        if not state_reconciliation_writes_allowed():
            fields.update({
                "dry_run": DRY_RUN,
                "simulation": SIMULATION_MODE,
                "allow_hardware_writes": ALLOW_HARDWARE_WRITES,
                "allow_state_reconciliation_writes": (
                    ALLOW_STATE_RECONCILIATION_WRITES
                )
            })

            log_event(
                logging.INFO,
                "dry_run_winter_ac_charge_limit",
                **fields
            )
            return

        try:
            response = dev.session.post(
                f"http://{dev.ip}/properties/write",
                json={
                    "sn": dev.sn,
                    "properties": properties
                },
                timeout=2
            )

            if not zendure_write_succeeded(
                "write_winter_ac_charge_limit_error",
                dev,
                response,
                **fields
            ):
                return

            log_event(
                logging.INFO,
                "write_winter_ac_charge_limit",
                **fields
            )

        except Exception as e:
            log_event(
                logging.WARNING,
                "write_winter_ac_charge_limit_error",
                device=dev.name,
                input_limit_w=properties["inputLimit"],
                error=e
            )

    def run_startup_ac_mode_reconcile_once(self, dev, state):
        """Initialize acMode=2 at most once after first valid telemetry."""

        if self.initial_ac_mode_reconciled.get(dev.name, False):
            return

        self.initial_ac_mode_reconciled[dev.name] = True

        if not RECONCILE_AC_MODE_ON_START:
            log_event(
                logging.INFO,
                "startup_ac_mode_reconcile_disabled",
                device=dev.name
            )
            return

        if int(state.ac_mode) == 2:
            log_event(
                logging.INFO,
                "startup_ac_mode_already_ok",
                device=dev.name,
                ac_mode=state.ac_mode,
                ac_status=state.ac_status
            )
            return

        skip_reason = startup_ac_mode_initialization_blocker(state)

        if skip_reason:
            log_event(
                logging.INFO,
                "startup_ac_mode_skip",
                device=dev.name,
                ac_mode=state.ac_mode,
                ac_status=state.ac_status,
                soc=state.soc,
                min_soc=state.min_soc,
                soc_limit=state.soc_limit,
                output_w=state.output,
                pack_input_w=state.pack_in,
                output_pack_w=state.pack_out,
                reason=skip_reason
            )
            return

        if not state_reconciliation_writes_allowed():
            log_event(
                logging.INFO,
                "dry_run_startup_ac_mode_write",
                device=dev.name,
                ac_mode=2,
                dry_run=DRY_RUN,
                simulation=SIMULATION_MODE,
                allow_hardware_writes=ALLOW_HARDWARE_WRITES,
                allow_state_reconciliation_writes=(
                    ALLOW_STATE_RECONCILIATION_WRITES
                )
            )
            return

        try:
            response = dev.session.post(
                f"http://{dev.ip}/properties/write",
                json={
                    "sn": dev.sn,
                    "properties": {
                        "acMode": 2
                    }
                },
                timeout=2
            )

            if not zendure_write_succeeded(
                "startup_ac_mode_write_error",
                dev,
                response,
                ac_mode=2
            ):
                return

            log_event(
                logging.INFO,
                "startup_ac_mode_write",
                device=dev.name,
                ac_mode=2
            )

        except Exception as e:
            log_event(
                logging.WARNING,
                "startup_ac_mode_write_error",
                device=dev.name,
                error=e
            )

    def apply_device_modes(self, dev, state):
        """Apply device operating modes if required."""

        manage_grid_off_mode = dev.grid_off_mode is not None
        properties = {}
        fields = {
            "device": dev.name
        }

        if (
            RECONCILE_SMART_MODE
            and dev.smart_mode is not None
            and int(state.smart_mode) != int(dev.smart_mode)
        ):
            properties["smartMode"] = int(dev.smart_mode)
            fields["smart_mode"] = dev.smart_mode

        if (
            manage_grid_off_mode
            and int(state.grid_off_mode) != int(dev.grid_off_mode)
        ):
            properties["gridOffMode"] = int(dev.grid_off_mode)
            fields["grid_off_mode"] = dev.grid_off_mode

        if (
            int(state.ac_mode) != 2
            or firmware_recovery_or_ac_charge_active(state)
        ):
            log_event(
                logging.INFO,
                "ac_mode_firmware_control_observed",
                device=dev.name,
                ac_mode=state.ac_mode,
                ac_status=state.ac_status,
                soc=state.soc,
                min_soc=state.min_soc,
                soc_limit=state.soc_limit,
                output_w=state.output,
                pack_input_w=state.pack_in,
                output_pack_w=state.pack_out
            )

        if not properties:

            log_event(
                logging.INFO,
                "device_modes_unchanged",
                device=dev.name
            )

            return

        if not state_reconciliation_writes_allowed():
            fields.update({
                "dry_run": DRY_RUN,
                "simulation": SIMULATION_MODE,
                "allow_hardware_writes": ALLOW_HARDWARE_WRITES,
                "allow_state_reconciliation_writes": (
                    ALLOW_STATE_RECONCILIATION_WRITES
                )
            })

            log_event(
                logging.INFO,
                "dry_run_device_modes",
                **fields
            )
            return

        try:
            response = dev.session.post(
                f"http://{dev.ip}/properties/write",
                json={
                    "sn": dev.sn,
                    "properties": properties
                },
                timeout=2
            )

            if not zendure_write_succeeded(
                "write_device_modes_error",
                dev,
                response,
                **fields
            ):
                return

            log_event(
                logging.INFO,
                "write_device_modes",
                **fields
            )
            
        except Exception as e:

            log_event(
                logging.WARNING,
                "write_device_modes_error",
                device=dev.name,
                error=e
            )

    def apply_runtime_device_state(self, dev, state):
        """Apply runtime-state device intents through safe reconciliation."""

        if not self.runtime_state:
            return

        desired_offgrid_socket_mode = self.runtime_state.get_device(
            dev.name,
            "offgrid_socket_mode",
            None
        )

        if desired_offgrid_socket_mode is None:
            return

        desired_offgrid_socket_mode = str(
            desired_offgrid_socket_mode
        ).strip().lower()

        if desired_offgrid_socket_mode not in OFFGRID_SOCKET_MODES:
            log_event(
                logging.WARNING,
                "runtime_device_state_invalid",
                device=dev.name,
                field="offgrid_socket_mode",
                value=desired_offgrid_socket_mode,
                runtime_source="runtime-state"
            )
            return

        # Zendure gridOffMode mapping:
        # off      -> gridOffMode=2
        # eco      -> gridOffMode=1
        # standard -> gridOffMode=0
        desired_grid_off_mode = OFFGRID_SOCKET_MODES[
            desired_offgrid_socket_mode
        ]
        current_grid_off_mode = int(state.grid_off_mode)
        fields = {
            "device": dev.name,
            "field": "gridOffMode",
            "current_value": current_grid_off_mode,
            "desired_mode": desired_offgrid_socket_mode,
            "desired_value": desired_grid_off_mode,
            "runtime_source": "runtime-state"
        }

        if current_grid_off_mode == desired_grid_off_mode:
            log_event(
                logging.INFO,
                "runtime_device_state_unchanged",
                **fields
            )
            return

        if not state_reconciliation_writes_allowed():
            fields.update({
                "dry_run": DRY_RUN,
                "simulation": SIMULATION_MODE,
                "allow_hardware_writes": ALLOW_HARDWARE_WRITES,
                "allow_state_reconciliation_writes": (
                    ALLOW_STATE_RECONCILIATION_WRITES
                )
            })

            log_event(
                logging.INFO,
                "dry_run_runtime_device_state_write",
                **fields
            )
            return

        try:
            response = dev.session.post(
                f"http://{dev.ip}/properties/write",
                json={
                    "sn": dev.sn,
                    "properties": {
                        "gridOffMode": desired_grid_off_mode
                    }
                },
                timeout=2
            )

            if not zendure_write_succeeded(
                "write_runtime_device_state_error",
                dev,
                response,
                **fields
            ):
                return

            log_event(
                logging.INFO,
                "write_runtime_device_state",
                **fields
            )

        except Exception as e:
            log_event(
                logging.WARNING,
                "write_runtime_device_state_error",
                device=dev.name,
                field="gridOffMode",
                current_value=current_grid_off_mode,
                desired_value=desired_grid_off_mode,
                runtime_source="runtime-state",
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

    def publish_winter_to_ha(self, states):
        """Publish winter-mode state and calculated targets to HA."""

        now = datetime.now()
        enabled = winter_feature_enabled(self.runtime_state)
        active = winter_mode_active(now, self.runtime_state)
        adjust_window = winter_adjustment_window_active(now)
        p = "sensor.ems_solarflow_"

        self.ha.set_state(
            "binary_sensor.ems_solarflow_winter_enabled",
            "on" if enabled else "off",
            extra_attributes={
                "months": ",".join(str(m) for m in winter_months())
            }
        )

        self.ha.set_state(
            "binary_sensor.ems_solarflow_winter_active",
            "on" if active else "off",
            extra_attributes={
                "month": now.month
            }
        )

        self.ha.set_state(
            "binary_sensor.ems_solarflow_winter_adjust_window",
            "on" if adjust_window else "off",
            extra_attributes={
                "adjust_hour": winter_config_int(
                    "adjust_hour",
                    12,
                    minimum=0
                ) % 24
            }
        )

        self.publish_sensor(
            p + "winter_summer_min_soc",
            winter_config_int("summer_min_soc", 15, minimum=0),
            "%",
            "battery"
        )

        self.publish_sensor(
            p + "winter_min_soc",
            winter_config_int("winter_min_soc", 40, minimum=0),
            "%",
            "battery"
        )

        self.publish_sensor(
            p + "winter_ramp_step",
            winter_config_int("ramp_step_percent", 5, minimum=1),
            "%",
            None
        )

        self.publish_sensor(
            p + "winter_ac_charge_power",
            winter_config_int("ac_charge_power", 200, minimum=0),
            "W",
            "power"
        )

        self.publish_sensor(
            p + "winter_last_adjust_date",
            self.last_winter_adjust_date or "never",
            state_class=None,
            icon="mdi:calendar-clock"
        )

        for dev, state in zip(self.devices, states):
            base = p + dev.name.lower() + "_winter_"
            effective_min_soc = self.winter_min_soc_targets.get(
                dev.name,
                state.min_soc if state.min_soc > 0 else dev.min_soc
            )
            target = calculate_winter_min_soc_target(
                state.soc,
                effective_min_soc,
                active
            )

            self.publish_sensor(
                base + "min_soc_target",
                target,
                "%",
                "battery",
                extra={
                    "effective_min_soc": effective_min_soc,
                    "current_soc": state.soc,
                    "winter_active": active
                }
            )

            self.publish_sensor(
                base + "estimated_ramp_days",
                estimate_winter_ramp_days(target),
                "d",
                None,
                icon="mdi:calendar-range"
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

        if not states:
            log_event(logging.WARNING, "ha_publish_no_devices")
            return

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

        self.publish_winter_to_ha(states)

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
            history = self.battery_power_history.setdefault(
                dev.name,
                deque(maxlen=REMAINING_TIME_POWER_SAMPLES)
            )
            history.append(device_battery_power)

            avg_battery_power = sum(history) / len(history)
            remaining_time = calculate_remaining_time_hours(
                d,
                dev,
                avg_battery_power
            )

            self.publish_sensor(
                base + "battery_power",
                round(device_battery_power, 1),
                "W",
                "power"
            )

            self.publish_sensor(
                base + "battery_power_avg",
                round(avg_battery_power, 1),
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

            self.publish_sensor(
                base + "remaining_time",
                remaining_time,
                "h",
                "duration",
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
            self.publish_sensor(
                base + "fault_level",
                d.fault_level,
                state_class=None
            )

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

        if self.runtime_state:
            self.runtime_state.load_if_changed()

        self.sync_ha_runtime_state()

        load = self.shelly.get_power()

        # =====================
        # RUNTIME CONFIG
        # =====================

        max_power = self.runtime_system_int(
            "max_total_power",
            MAX_TOTAL_POWER,
            minimum=0
        )
        enabled = self.runtime_system_bool(
            "enabled",
            SYSTEM_ENABLED
        )
        interval = self.runtime_system_int(
            "loop_interval",
            LOOP_INTERVAL,
            minimum=1
        )
        min_output_limit = self.runtime_system_int(
            "min_output_limit",
            MIN_OUTPUT_LIMIT,
            minimum=0
        )

        for dev in self.devices:
            dev.max_power = self.runtime_device_int(
                dev.name,
                "max_power",
                dev.max_power,
                minimum=0
            )

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

                self.run_startup_ac_mode_reconcile_once(dev, state)

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
                fault_level=state.fault_level,
                solar_w=state.solar,
                output_w=state.output,
                output_limit_w=state.output_limit,
                pack_input_w=state.pack_in,
                soc_runtime_state=derive_soc_runtime_state(state),
                can_charge=cap.can_charge,
                can_discharge=cap.can_discharge,
                can_export=cap.can_export,
                can_ac_charge=cap.can_ac_charge,
                reason=cap.reason
            )

        #
        # Reconcile SOC limits
        #

        if (
            SOC_RECONCILE_INTERVAL > 0
            and not SIMULATION_MODE
            and not ARGS.replay
        ):

            self.soc_reconcile_counter += 1

            if (
                self.soc_reconcile_counter
                >= SOC_RECONCILE_INTERVAL
            ):

                self.soc_reconcile_counter = 0
                now = datetime.now()
                winter_active = winter_mode_active(now, self.runtime_state)
                winter_window_active = winter_adjustment_window_active(now)
                today = now.date().isoformat()
                winter_adjust_today = (
                    winter_active
                    and winter_window_active
                    and self.last_winter_adjust_date != today
                )

                if winter_feature_enabled(self.runtime_state):
                    log_event(
                        logging.INFO,
                        "winter_mode_state",
                        active=winter_active,
                        month=now.month,
                        adjust_window=winter_window_active,
                        adjust_today=winter_adjust_today,
                        last_adjust_date=self.last_winter_adjust_date,
                        summer_min_soc=winter_config_int(
                            "summer_min_soc",
                            15,
                            minimum=0
                        ),
                        winter_min_soc=winter_config_int(
                            "winter_min_soc",
                            40,
                            minimum=0
                        )
                    )

                for dev, state in zip(
                    self.devices,
                    raw_states
                ):

                    if state:
                        desired_min_soc, winter_adjustment = (
                            self.winter_reconciliation_target(
                                dev,
                                state,
                                winter_active,
                                winter_adjust_today
                            )
                        )

                        self.apply_soc_limits(
                            dev,
                            state,
                            desired_min_soc=desired_min_soc
                        )

                        self.apply_device_modes(
                            dev,
                            state
                        )

                        if winter_adjustment:
                            self.apply_winter_ac_charge_limit(dev)

                if winter_adjust_today:
                    self.last_winter_adjust_date = today

        for dev, state in zip(
            self.devices,
            raw_states
        ):

            if state:

                self.apply_runtime_device_state(
                    dev,
                    state
                )

        # =====================
        # CALCULATE TARGETS
        # =====================

        stabilized_total = self.stabilized_total_target(
            load,
            states,
            max_power
        )

        targets, current, new = calculate_targets(
            load,
            states,
            max_power,
            device_configs=self.devices,
            capabilities=capabilities,
            requested_total=stabilized_total
        )

        targets = self.apply_device_ramp(
            targets,
            load
        )

        logging.info(
            f"Load={load}W "
            f"Target={new}W "
            f"Enabled={enabled}"
        )

        # =====================
        # PUBLISH TO HA
        # =====================

        if self.ha and self.runtime_ha_enabled():
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

            if not self.runtime_device_bool(dev.name, "enabled", True):
                log_event(
                    logging.INFO,
                    "device_disabled_skip_write",
                    device=dev.name,
                    target_w=targets[i]
                )
                continue

            target = targets[i]

            target = apply_min_output_limit(
                target,
                dev,
                min_output_limit
            )

            deadband_reference = (
                states[i].output_limit
                if states[i].output_limit > 0
                else states[i].output
            )
            deadband_reference_source = (
                "output_limit"
                if states[i].output_limit > 0
                else "output"
            )

            if abs(target - deadband_reference) < DEADBAND:
                log_event(
                    logging.DEBUG,
                    "deadband_skip_write",
                    device=dev.name,
                    target_w=target,
                    reference_w=deadband_reference,
                    reference_source=deadband_reference_source,
                    deadband_w=DEADBAND
                )
                continue

            target = max(
                0,
                min(dev.max_power, target)
            )

            cooldown = self.output_control_float(
                "write_cooldown_seconds",
                2,
                minimum=0
            )
            last_write = self.last_output_write_at.get(dev.name)
            bypass = self.output_control_bypass_active(load)

            if (
                cooldown > 0
                and last_write is not None
                and not bypass
            ):
                age = time.time() - last_write

                if age < cooldown:
                    log_event(
                        logging.INFO,
                        "output_control_settle_hold",
                        device=dev.name,
                        target_w=target,
                        last_write_age_s=round(age, 2),
                        cooldown_s=cooldown
                    )
                    continue

            self.set_output_limit(dev, target)
            self.last_output_write_at[dev.name] = time.time()

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
                max_power=data.get("max_power", MAX_DEVICE_POWER),
                pv_kwp=data.get("pv_kwp", 1.0),
                battery_kwh=data.get("battery_kwh", 1.0),
                pv_priority_factor=data.get("pv_priority_factor", 1.0)
            )
        )

    shelly = SimulatedShellyClient()
    runtime_state = RuntimeState(
        runtime_state_path(),
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

        if ARGS.once:
            log_event(
                logging.INFO,
                "replay_stopped",
                reason="once",
                cycles=cycles
            )
            break

        if ARGS.max_cycles and cycles >= ARGS.max_cycles:
            log_event(
                logging.INFO,
                "replay_stopped",
                reason="max_cycles",
                cycles=cycles
            )
            break

        if ARGS.duration and time.time() - start_time >= ARGS.duration:
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
        actual = calculate_winter_min_soc_target(
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
                test="calculate_winter_min_soc_target",
                current_min_soc=current_min,
                current_soc=current_soc,
                winter_active=winter_active,
                expected=expected,
                actual=actual
            )

    payload = build_winter_ac_charge_limit_payload()
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

    if ARGS.self_test:
        sys.exit(0 if run_self_tests() else 2)

    if SIMULATION_MODE and not ARGS.replay:
        run_frames(built_in_simulation_frames(), "built_in_simulation")
        sys.exit(0)

    if ARGS.replay:
        try:
            replay_frames = load_replay_frames(ARGS.replay)
        except Exception as e:
            log_event(
                logging.ERROR,
                "replay_load_error",
                source=ARGS.replay,
                error=e
            )
            sys.exit(1)

        run_frames(replay_frames, ARGS.replay)
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
            d.get("grid_off_mode"),
            d.get("max_power", MAX_DEVICE_POWER),
            d.get("pv_kwp", 1.0),
            d.get("battery_kwh", 1.0),
            d.get("pv_priority_factor", 1.0)
        )
        for d in ZENDURE_CONFIG
    ]

    if not devices:
        log_event(logging.ERROR, "startup_abort", reason="no_devices")
        sys.exit(1)

    shelly = ShellyClient(
        SHELLY_IP,
        session
    )

    runtime_state = RuntimeState(
        runtime_state_path(),
        build_runtime_defaults(devices)
    )
    runtime_state.load_or_create()

    if ARGS.preflight:
        if run_live_preflight(devices, shelly, ha):
            sys.exit(0)

        sys.exit(2)

    ems = EMSController(
        devices,
        shelly,
        ha,
        runtime_state=runtime_state
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
