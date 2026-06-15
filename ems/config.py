# SPDX-License-Identifier: AGPL-3.0-or-later
import copy
import json
import os
import sys
from datetime import datetime

OUTPUT_CONTROL_DEFAULTS = {
    "load_deadband_w": 5,
    "target_deadband_w": 10,
    "filter_enabled": True,
    "filter_method": "median_ema",
    "median_window": 2,
    "ema_alpha": 0.85,
    "sign_change_fast_response_enabled": True,
    "sign_change_threshold_w": 50,
    "sign_change_filter_reset_factor": 1.0,
    "ramp_enabled": True,
    "ramp_up_w_per_cycle": 500,
    "ramp_down_w_per_cycle": 500,
    "device_ramp_enabled": True,
    "device_ramp_up_w_per_cycle": 400,
    "device_ramp_down_w_per_cycle": 400,
    "large_import_bypass_w": 600,
    "large_export_bypass_w": 600,
    "bypass_ramp_multiplier": 1.5,
    "telemetry_max_age_seconds": 10,
    "stale_telemetry_ramp_factor": 0.5
}

WINTER_DEFAULTS = {
    "enabled": False,
    "months": [10, 11, 12, 1, 2, 3],
    "summer_min_soc": 15,
    "winter_min_soc": 40,
    "ramp_step_percent": 5,
    "adjust_hour": 12,
    "ac_charge_power": 200
}

DASHBOARD_DEFAULTS = {
    "enabled": True,
    "host": "0.0.0.0",
    "port": 8080,
    "database_path": "data/ems_dashboard.sqlite",
    "history_hours": 48,
    "write_interval_seconds": 5,
    "auth_file": "config/dashboard-auth.json",
    "ssl_enabled": False,
    "ssl_cert_file": "config/dashboard.crt",
    "ssl_key_file": "config/dashboard.key",
    "ssl_auto_generate": True
}

ENERGY_SAVINGS_DEFAULTS = {
    "enabled": True,
    "price_per_kwh": 0.0,
    "currency": "EUR",
    "max_sample_delta_seconds": 20,
    "timezone": "Europe/Berlin"
}

BATTERY_FULL_CHARGE_ASSIST_DEFAULTS = {
    "enabled": False,
    "interval_days": 28,
    "assist_window_days": 7,
    "assist_start_soc": 80,
    "force_time": "14:00",
    "ac_charge_power": 200,
    "enable_ac_charge_mode": True,
    "state_database_path": "data/ems_state.sqlite"
}


def default_safe_config():
    """Return a minimal safe config for simulation and replay."""

    return {
        "ha": {
            "enabled": False,
            "control_enabled": False,
            "url": "",
            "token": ""
        },
        "system": {
            "enabled": True,
            "dry_run": True,
            "simulation_mode": True,
            "allow_hardware_writes": False,
            "allow_state_reconciliation_writes": False,
            "reconcile_ac_mode_on_start": True,
            "reconcile_smart_mode": True,
            "max_total_power": 800,
            "max_device_power": 800,
            "deadband": 10,
            "runtime_state_path": "runtime-state.json",
            "min_output_limit": 0,
            "loop_interval": 5,
            "output_control": copy.deepcopy(OUTPUT_CONTROL_DEFAULTS),
            "soc_reconcile_interval": 0,
            "log_level": "debug",
            "redistribute_clamped_power": True,
            "pv_kwp_weighting": True,
            "pv_charge_balance_enabled": True,
            "pv_charge_balance_deadband_percent": 5,
            "pv_charge_balance_full_bias_percent": 15,
            "pv_charge_balance_strength": 1.0,
            "battery_kwh_weighting": True
        },
        "winter": copy.deepcopy(WINTER_DEFAULTS),
        "dashboard": copy.deepcopy(DASHBOARD_DEFAULTS),
        "energy_savings": copy.deepcopy(ENERGY_SAVINGS_DEFAULTS),
        "battery_full_charge_assist": copy.deepcopy(
            BATTERY_FULL_CHARGE_ASSIST_DEFAULTS
        ),
        "devices": [],
        "grid_meter": {
            "type": "shelly",
            "ip": ""
        },
        "shelly": {
            "ip": ""
        }
    }


ARGS = None
BASE_DIR = None
CONFIG = None
SYSTEM_ENABLED = True
HA_URL = ""
HA_TOKEN = ""
MAX_TOTAL_POWER = 0
MAX_DEVICE_POWER = 0
DEADBAND = 0
LOOP_INTERVAL = 5
OUTPUT_CONTROL_CONFIG = OUTPUT_CONTROL_DEFAULTS.copy()
RUNTIME_STATE_PATH = "runtime-state.json"
REMAINING_TIME_POWER_SAMPLES = 10
REMAINING_TIME_MIN_POWER_W = 10
REMAINING_TIME_MAX_HOURS = 999
MIN_OUTPUT_LIMIT = 0
DRY_RUN = True
SIMULATION_MODE = False
ALLOW_HARDWARE_WRITES = False
ALLOW_STATE_RECONCILIATION_WRITES = False
RECONCILE_AC_MODE_ON_START = True
RECONCILE_SMART_MODE = True
HA_ENABLED = False
HA_CONTROL_ENABLED = False
LOG_LEVEL = "info"
REDISTRIBUTE_CLAMPED_POWER = True
PV_KWP_WEIGHTING = True
PV_CHARGE_BALANCE_ENABLED = True
PV_CHARGE_BALANCE_DEADBAND_PERCENT = 5.0
PV_CHARGE_BALANCE_FULL_BIAS_PERCENT = 15.0
PV_CHARGE_BALANCE_STRENGTH = 1.0
BATTERY_KWH_WEIGHTING = True
SOC_RECONCILE_INTERVAL = 10
WINTER_CONFIG = WINTER_DEFAULTS.copy()
DASHBOARD_CONFIG = DASHBOARD_DEFAULTS.copy()
ENERGY_SAVINGS_CONFIG = ENERGY_SAVINGS_DEFAULTS.copy()
BATTERY_FULL_CHARGE_ASSIST_CONFIG = BATTERY_FULL_CHARGE_ASSIST_DEFAULTS.copy()
OFFGRID_SOCKET_MODES = {
    "standard": 0,
    "eco": 1,
    "off": 2
}
ZENDURE_CONFIG = []
SHELLY_IP = ""
GRID_METER_CONFIG = {
    "type": "shelly",
    "ip": ""
}


def load_config(args=None, base_dir=None):
    args = args or ARGS
    base_dir = base_dir or BASE_DIR or os.getcwd()
    path = args.config or os.path.join(base_dir, "config.json")

    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        if args.simulate or args.replay or args.self_test:
            return default_safe_config()

        print("config.json missing. Please create it from template.")
        sys.exit(1)


def initialize(args, base_dir):
    global ARGS, BASE_DIR, CONFIG, SYSTEM_ENABLED, HA_URL, HA_TOKEN
    global MAX_TOTAL_POWER, MAX_DEVICE_POWER, DEADBAND, LOOP_INTERVAL
    global OUTPUT_CONTROL_CONFIG, RUNTIME_STATE_PATH, MIN_OUTPUT_LIMIT
    global DRY_RUN, SIMULATION_MODE, ALLOW_HARDWARE_WRITES
    global ALLOW_STATE_RECONCILIATION_WRITES, RECONCILE_AC_MODE_ON_START
    global RECONCILE_SMART_MODE, HA_ENABLED, HA_CONTROL_ENABLED, LOG_LEVEL
    global REDISTRIBUTE_CLAMPED_POWER, PV_KWP_WEIGHTING
    global PV_CHARGE_BALANCE_ENABLED, PV_CHARGE_BALANCE_DEADBAND_PERCENT
    global PV_CHARGE_BALANCE_FULL_BIAS_PERCENT, PV_CHARGE_BALANCE_STRENGTH
    global BATTERY_KWH_WEIGHTING
    global SOC_RECONCILE_INTERVAL, WINTER_CONFIG, DASHBOARD_CONFIG
    global ENERGY_SAVINGS_CONFIG, BATTERY_FULL_CHARGE_ASSIST_CONFIG
    global ZENDURE_CONFIG, SHELLY_IP, GRID_METER_CONFIG

    ARGS = args
    BASE_DIR = base_dir
    CONFIG = load_config(args, base_dir)
    ha_config = CONFIG.get("ha", {})

    SYSTEM_ENABLED = CONFIG["system"].get("enabled", True)
    HA_URL = ha_config.get("url", "")
    HA_TOKEN = ha_config.get("token", "")
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

    try:
        MIN_OUTPUT_LIMIT = max(
            0,
            int(CONFIG["system"].get("min_output_limit", 0))
        )
    except (TypeError, ValueError):
        MIN_OUTPUT_LIMIT = 0

    DRY_RUN = CONFIG["system"].get("dry_run", True) or args.dry_run
    SIMULATION_MODE = CONFIG["system"].get("simulation_mode", False) or args.simulate
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
        ha_config.get("enabled", False)
        and not args.no_ha
        and not SIMULATION_MODE
        and not args.replay
    )
    HA_CONTROL_ENABLED = (
        HA_ENABLED
        and ha_config.get("control_enabled", False)
    )
    LOG_LEVEL = CONFIG["system"].get("log_level", "info").lower()

    if args.simulate or args.replay:
        LOG_LEVEL = "debug"

    REDISTRIBUTE_CLAMPED_POWER = CONFIG["system"].get(
        "redistribute_clamped_power",
        True
    )
    PV_KWP_WEIGHTING = CONFIG["system"].get(
        "pv_kwp_weighting",
        True
    )
    PV_CHARGE_BALANCE_ENABLED = safe_bool(
        CONFIG["system"].get("pv_charge_balance_enabled", True),
        True
    )
    PV_CHARGE_BALANCE_DEADBAND_PERCENT = safe_float(
        CONFIG["system"].get("pv_charge_balance_deadband_percent", 5),
        5.0,
        minimum=0.0
    )
    PV_CHARGE_BALANCE_FULL_BIAS_PERCENT = safe_float(
        CONFIG["system"].get("pv_charge_balance_full_bias_percent", 15),
        15.0,
        minimum=0.0
    )
    PV_CHARGE_BALANCE_STRENGTH = min(
        1.0,
        safe_float(
            CONFIG["system"].get("pv_charge_balance_strength", 1.0),
            1.0,
            minimum=0.0
        )
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
    DASHBOARD_CONFIG = {
        **DASHBOARD_DEFAULTS,
        **CONFIG.get("dashboard", {})
    }
    ENERGY_SAVINGS_CONFIG = {
        **ENERGY_SAVINGS_DEFAULTS,
        **CONFIG.get("energy_savings", {})
    }
    BATTERY_FULL_CHARGE_ASSIST_CONFIG = normalize_battery_full_charge_assist_config(
        CONFIG.get("battery_full_charge_assist", {})
    )
    ZENDURE_CONFIG = CONFIG["devices"]
    legacy_shelly_config = CONFIG.get("shelly", {})
    if not isinstance(legacy_shelly_config, dict):
        legacy_shelly_config = {}
    configured_grid_meter = CONFIG.get("grid_meter")
    if isinstance(configured_grid_meter, dict):
        GRID_METER_CONFIG = dict(configured_grid_meter)
        GRID_METER_CONFIG["type"] = str(GRID_METER_CONFIG.get("type", "shelly"))
        for key in ("ip", "url", "power_path"):
            if key in GRID_METER_CONFIG and GRID_METER_CONFIG[key] is not None:
                GRID_METER_CONFIG[key] = str(GRID_METER_CONFIG[key])
        if (
            "channels" in GRID_METER_CONFIG
            and GRID_METER_CONFIG["channels"] is not None
        ):
            if not isinstance(GRID_METER_CONFIG["channels"], list):
                raise ValueError("grid_meter.channels must be a list")
            normalized_channels = []
            for item in GRID_METER_CONFIG["channels"]:
                value = str(item).strip().lower()
                if not value:
                    raise ValueError(
                        "grid_meter.channels must not contain empty values"
                    )
                normalized_channels.append(value)
            GRID_METER_CONFIG["channels"] = normalized_channels
    else:
        GRID_METER_CONFIG = {
            "type": "shelly",
            "ip": str(legacy_shelly_config.get("ip", ""))
        }
    SHELLY_IP = str(legacy_shelly_config.get("ip", GRID_METER_CONFIG.get("ip", "")))

    return CONFIG

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


def dashboard_database_path():
    """Return absolute path to the dashboard SQLite database."""

    database_path = str(
        DASHBOARD_CONFIG.get(
            "database_path",
            DASHBOARD_DEFAULTS["database_path"]
        )
    )

    if os.path.isabs(database_path):
        return database_path

    return os.path.join(BASE_DIR, database_path)


def battery_full_charge_state_database_path():
    """Return absolute path to the core EMS state SQLite database."""

    database_path = str(
        BATTERY_FULL_CHARGE_ASSIST_CONFIG.get(
            "state_database_path",
            BATTERY_FULL_CHARGE_ASSIST_DEFAULTS["state_database_path"]
        )
    )

    if os.path.isabs(database_path):
        return database_path

    return os.path.join(BASE_DIR, database_path)


def dashboard_file_path(key, default):
    """Return an absolute path for dashboard-local files."""

    path = str(DASHBOARD_CONFIG.get(key, default))

    if os.path.isabs(path):
        return path

    return os.path.join(BASE_DIR, path)


def safe_int(value, default=0, minimum=None):
    try:
        parsed = int(float(value))
    except (TypeError, ValueError):
        parsed = default

    if minimum is not None:
        parsed = max(minimum, parsed)

    return parsed


def safe_session_timeout(value, default):
    """Parse a session-timeout config value.

    ``0`` is a deliberate "disabled / infinite" opt-in and is preserved.
    Invalid or negative values fall back to the (secure) default rather than
    being clamped to ``0`` — a negative typo must never silently disable a
    timeout.
    """
    try:
        parsed = int(float(value))
    except (TypeError, ValueError):
        return int(default)

    if parsed < 0:
        return int(default)

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


def safe_percent(value, default=0):
    return max(0, min(100, safe_int(value, default, minimum=0)))


def normalize_force_time(value):
    text = str(value or BATTERY_FULL_CHARGE_ASSIST_DEFAULTS["force_time"]).strip()
    parts = text.split(":")
    if len(parts) != 2:
        raise ValueError(
            "battery_full_charge_assist.force_time must use HH:MM format"
        )

    try:
        hour = int(parts[0])
        minute = int(parts[1])
    except ValueError as exc:
        raise ValueError(
            "battery_full_charge_assist.force_time must use HH:MM format"
        ) from exc

    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError(
            "battery_full_charge_assist.force_time must use HH:MM format"
        )

    return f"{hour:02d}:{minute:02d}"


def normalize_battery_full_charge_assist_config(config):
    if not isinstance(config, dict):
        config = {}

    merged = {
        **BATTERY_FULL_CHARGE_ASSIST_DEFAULTS,
        **config
    }

    return {
        "enabled": safe_bool(merged.get("enabled"), False),
        "interval_days": safe_int(
            merged.get("interval_days"),
            BATTERY_FULL_CHARGE_ASSIST_DEFAULTS["interval_days"],
            minimum=1
        ),
        "assist_window_days": safe_int(
            merged.get("assist_window_days"),
            BATTERY_FULL_CHARGE_ASSIST_DEFAULTS["assist_window_days"],
            minimum=0
        ),
        "assist_start_soc": safe_percent(
            merged.get("assist_start_soc"),
            BATTERY_FULL_CHARGE_ASSIST_DEFAULTS["assist_start_soc"]
        ),
        "force_time": normalize_force_time(merged.get("force_time")),
        "ac_charge_power": safe_int(
            merged.get("ac_charge_power"),
            BATTERY_FULL_CHARGE_ASSIST_DEFAULTS["ac_charge_power"],
            minimum=0
        ),
        "enable_ac_charge_mode": safe_bool(
            merged.get("enable_ac_charge_mode"),
            True
        ),
        "state_database_path": str(
            merged.get(
                "state_database_path",
                BATTERY_FULL_CHARGE_ASSIST_DEFAULTS["state_database_path"]
            )
        ),
    }


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
