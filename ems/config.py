# SPDX-License-Identifier: AGPL-3.0-or-later
import copy
import json
import logging
import os
import re
import ssl
import sys
import stat
from dataclasses import dataclass
from urllib.parse import urlparse

from ems.paths import resolve_config_path, resolve_template_path

LATEST_CONFIG_SCHEMA_VERSION = 3
CURRENT_CONFIG_SCHEMA_VERSION = LATEST_CONFIG_SCHEMA_VERSION

OUTPUT_CONTROL_DEFAULTS = {
    "load_deadband_w": 5,
    "target_deadband_w": 5,
    "filter_enabled": True,
    "filter_method": "median_ema",
    "median_window": 2,
    "ema_alpha": 0.85,
    "sign_change_fast_response_enabled": True,
    "sign_change_threshold_w": 50,
    "sign_change_filter_reset_factor": 1.0,
    "ramp_enabled": True,
    "ramp_up_w_per_cycle": 500,
    # Slower down-ramp reduces undershoot when inverter output reacts more
    # slowly than the EMS target.
    "ramp_down_w_per_cycle": 300,
    "device_ramp_enabled": True,
    "device_ramp_up_w_per_cycle": 400,
    "device_ramp_down_w_per_cycle": 200,
    "large_import_bypass_w": 600,
    "large_export_bypass_w": 600,
    "bypass_ramp_multiplier": 1.5,
    "telemetry_max_age_seconds": 10,
    "stale_telemetry_ramp_factor": 0.5
}

WINTER_DEFAULTS = {
    "enabled": True,
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
    "ssl_auto_generate": True,
    "session_idle_timeout_seconds": 1800,
    "session_absolute_max_seconds": 43200,
    "log_buffer_lines": 5000,
    "log_redaction": False,
    # Dashboard animation cost. "normal" keeps the full animated flow view;
    # "reduced" trims glows/filters and slows pipe motion; "off" disables
    # continuous pipe animations and glow/blur filters. Browser-level
    # prefers-reduced-motion is always respected on top of this.
    "animation_mode": "normal"
}

DASHBOARD_ANIMATION_MODES = ("normal", "reduced", "off")

ENERGY_SAVINGS_DEFAULTS = {
    "enabled": True,
    "price_per_kwh": 0.0,
    "currency": "EUR",
    "max_sample_delta_seconds": 20,
    "timezone": "Europe/Berlin"
}

BATTERY_FULL_CHARGE_ASSIST_DEFAULTS = {
    "enabled": True,
    "interval_days": 28,
    "assist_window_days": 7,
    "assist_start_soc": 80,
    "force_time": "14:00",
    "ac_charge_power": 200,
    "enable_ac_charge_mode": True,
    "state_database_path": "data/ems_state.sqlite"
}

CONFIG_UPGRADE_DEFAULTS = {
    "on_startup": "check",
    "backup_before_apply": True,
    "backup_failure_policy": "continue_without_upgrade",
}

CONFIG_UPGRADE_STARTUP_MODES = ("disabled", "check", "apply")
CONFIG_UPGRADE_BACKUP_FAILURE_POLICIES = ("continue_without_upgrade",)

INFLUXDB_DEFAULTS = {
    "enabled": True,
    # "bundled" = the bundled docker-compose InfluxDB managed by the setup
    # helpers (emsctl influx init / stack up). "external" = a pre-existing
    # InfluxDB the operator runs and provides a token for; setup helpers do not
    # create secrets or start containers for it.
    "mode": "bundled",
    # For bundled mode, allow the CLI setup helpers to create missing local
    # secrets and start the bundled InfluxDB service.
    "auto_init": True,
    # Allow setup/start helpers to run the InfluxDB schema sync automatically.
    "auto_sync": True,
    # Local env file (relative to the project root) holding the generated
    # secrets for the bundled InfluxDB. Gitignored; never commit it.
    "secret_file": "deploy/docker/influxdb.env",
    # EMS runtime (in-container) URL. Inside the Docker network the bundled
    # InfluxDB is reachable by its compose service name.
    "url": "http://influxdb:8086",
    # Host-side URL used by emsctl (influx init/sync/status, stack up). The
    # Docker service name "influxdb" is not resolvable on the host, so host-side
    # CLI operations for bundled mode use this instead of "url".
    "host_url": "http://127.0.0.1:8086",
    "org": "ems",
    "token": "",
    "token_env": "INFLUXDB_TOKEN",
    "bucket_prefix": "ems",
    # Raw telemetry write cadence. 0 (or null) writes once per EMS control loop
    # for full-resolution spike visibility; a positive value throttles raw
    # writes to at most once every N seconds (SQLite history is unaffected).
    "raw_write_interval_seconds": 0,
    "retention": {
        "raw_days": 14,
        "one_minute_days": 90,
        "five_minute_days": 365,
        "one_hour_days": 1825,
    },
    "downsampling": [
        {"source": "raw", "target": "1m", "window": "1m"},
        {"source": "1m", "target": "5m", "window": "5m"},
        {"source": "5m", "target": "1h", "window": "1h"},
    ],
    "query_profiles": [
        {"max_range": "1h", "bucket": "raw", "window": "1s"},
        {"max_range": "6h", "bucket": "raw", "window": "10s"},
        {"max_range": "24h", "bucket": "1m", "window": "1m"},
        {"max_range": "30d", "bucket": "5m", "window": "5m"},
        {"max_range": "365d", "bucket": "1h", "window": "1h"},
    ],
}

# Maps the retention.*_days config keys to the bucket suffix they govern.
INFLUXDB_RETENTION_KEY_BY_BUCKET = {
    "raw": "raw_days",
    "1m": "one_minute_days",
    "5m": "five_minute_days",
    "1h": "one_hour_days",
}

ZENDURE_SMARTMETER_D0_GRID_METER_TYPE = "zendure_smartmeter_d0"
ZENDURE_SMARTMETER_3CT_HTTP_GRID_METER_TYPE = "zendure_smartmeter_3ct_http"
ZENDURE_SMARTMETER_D0_HTTP_GRID_METER_TYPE = "zendure_smartmeter_d0_http"
# Canonical generic local-HTTP grid-meter type. A Zendure D0, a Smart Meter 3CT
# and the generic type all expose a flat numeric ``total_power`` at
# ``/properties/report``, so one shared client serves them all. The concrete D0
# and 3CT local-API types are kept distinct (a D0 is never stored as a 3CT); the
# generic type stays a backward-compatible/discovery alias so existing configs
# keep working.
ZENDURE_GRID_METER_HTTP_GRID_METER_TYPE = "zendure_grid_meter_http"
ZENDURE_HTTP_GRID_METER_TYPES = frozenset(
    {
        ZENDURE_GRID_METER_HTTP_GRID_METER_TYPE,
        ZENDURE_SMARTMETER_3CT_HTTP_GRID_METER_TYPE,
        ZENDURE_SMARTMETER_D0_HTTP_GRID_METER_TYPE,
    }
)
MQTT_GRID_METER_TYPES = ("mqtt", ZENDURE_SMARTMETER_D0_GRID_METER_TYPE)

# Single source of truth mapping a grid-meter ``type`` to a user-facing hardware
# model name and a transport label. Diagnostics and status surfaces read this so
# a D0 always reports as a D0 (never a 3CT) and each transport is named
# correctly. Unknown types fall back to ``(None, None)``.
_LOCAL_HTTP_TRANSPORT = "local HTTP API"
_LOCAL_MQTT_TRANSPORT = "local MQTT"
GRID_METER_MODEL_TRANSPORTS = {
    "shelly": ("Shelly Pro/Plus", _LOCAL_HTTP_TRANSPORT),
    "shelly_3em_gen1": ("Shelly 3EM Gen1", _LOCAL_HTTP_TRANSPORT),
    "ecotracker": ("everHome EcoTracker", _LOCAL_HTTP_TRANSPORT),
    "tasmota_http": ("Tasmota HTTP reader", _LOCAL_HTTP_TRANSPORT),
    ZENDURE_GRID_METER_HTTP_GRID_METER_TYPE: ("Zendure grid meter", _LOCAL_HTTP_TRANSPORT),
    ZENDURE_SMARTMETER_3CT_HTTP_GRID_METER_TYPE: (
        "Zendure Smart Meter 3CT",
        _LOCAL_HTTP_TRANSPORT,
    ),
    ZENDURE_SMARTMETER_D0_HTTP_GRID_METER_TYPE: (
        "Zendure Smart Meter D0",
        _LOCAL_HTTP_TRANSPORT,
    ),
    ZENDURE_SMARTMETER_D0_GRID_METER_TYPE: ("Zendure Smart Meter D0", _LOCAL_MQTT_TRANSPORT),
    "mqtt": ("Generic MQTT meter", "MQTT"),
    "ha": ("Home Assistant (legacy)", "Home Assistant"),
}


def grid_meter_model_transport(meter_type):
    """Return ``(model, transport)`` labels for a grid-meter ``type``.

    Reuses the shared :data:`GRID_METER_MODEL_TRANSPORTS` table so status and
    diagnostics stay consistent and a D0 is never surfaced as a 3CT. Unknown
    types yield ``(None, None)``.
    """

    return GRID_METER_MODEL_TRANSPORTS.get(str(meter_type or "").strip().lower(), (None, None))

_ZENDURE_D0_TOPIC_PREFIX = "Zendure/sensor/"
_ZENDURE_D0_TOPIC_SUFFIX = "/totalPower"
_ZENDURE_D0_TOPIC_ROOT = "Zendure"
_ZENDURE_D0_TOPIC_ENTITY = "sensor"
_ZENDURE_D0_TOPIC_METRIC = "totalPower"
_MQTT_TOPIC_WILDCARDS = ("+", "#")


def zendure_smartmeter_d0_topic(serial_number):
    """Return the canonical D0 grid-meter MQTT topic for a serial number.

    This is the single source of truth shared by the CLI setup assistant and the
    Admin guided setup so both produce ``Zendure/sensor/<serial>/totalPower``. An
    empty or whitespace-only serial (or one carrying a path/wildcard character) is
    rejected rather than yielding a topic with a hole or an extra segment in it.
    """

    serial = str(serial_number or "").strip()
    if not serial:
        raise ValueError("Zendure SmartMeter D0 requires a serial number")
    if "/" in serial or any(w in serial for w in _MQTT_TOPIC_WILDCARDS):
        raise ValueError(
            "Zendure SmartMeter D0 serial must not contain '/', '+' or '#'"
        )
    return f"{_ZENDURE_D0_TOPIC_PREFIX}{serial}{_ZENDURE_D0_TOPIC_SUFFIX}"


def zendure_smartmeter_d0_serial_from_topic(topic):
    """Extract the serial from a canonical D0 topic, or ``""`` when it is not one.

    Strict canonical shape, the single accepted form for a D0 grid-meter topic::

        Zendure/sensor/<serial>/totalPower

    Exactly four segments, exact ``Zendure`` root, exact ``sensor`` entity, exact
    ``totalPower`` metric, a non-empty serial and no MQTT wildcards. Anything else
    (extra/missing segments, a foreign or cloud prefix, the ``number`` write
    channel, a leading/trailing separator, ``+``/``#``) yields ``""``.
    """

    text = str(topic or "").strip()
    if not text:
        return ""
    segments = text.split("/")
    if len(segments) != 4:
        return ""
    root, entity, serial, metric = segments
    if root != _ZENDURE_D0_TOPIC_ROOT or entity != _ZENDURE_D0_TOPIC_ENTITY:
        return ""
    if metric != _ZENDURE_D0_TOPIC_METRIC:
        return ""
    if not serial or any(w in serial for w in _MQTT_TOPIC_WILDCARDS):
        return ""
    return serial


def is_zendure_smartmeter_d0_topic(topic):
    """True when ``topic`` is exactly ``Zendure/sensor/<serial>/totalPower``."""

    return bool(zendure_smartmeter_d0_serial_from_topic(topic))


MQTT_GRID_METER_KEYS = (
    "host",
    "port",
    "tls",
    "tls_insecure",
    "username",
    "password",
    "topic",
    "payload_format",
    "value_path",
    "max_age_seconds",
    "_mqtt_client_factory",
)

# Broker-owned connection fields. When a component selects a named broker profile
# via ``broker_ref``, the profile is the single source of truth for these, so
# inlining any of them alongside ``broker_ref`` is ambiguous. One central list so
# the grid meter and any future MQTT consumer reject the same fields.
MQTT_BROKER_CONNECTION_FIELDS = frozenset(
    {
        "host",
        "port",
        "tls",
        "tls_mode",
        "tls_insecure",
        "username",
        "password",
        "credentials_ref",
    }
)


class MqttBrokerReferenceAmbiguousError(ValueError):
    """A component sets ``broker_ref`` and also inlines broker connection fields.

    Carries the stable ``code``, the config ``path`` and the sorted conflicting
    field names (never a secret value) so every validator surfaces one contract.
    """

    code = "mqtt_broker_reference_ambiguous"

    def __init__(self, fields, *, path):
        self.fields = tuple(sorted(fields))
        self.path = path
        super().__init__(
            f"{path} uses broker_ref but also inlines connection field(s): "
            + ", ".join(self.fields)
            + ". Keep connection settings in the broker profile only."
        )


def mqtt_broker_reference_conflict_fields(mqtt_settings):
    """Sorted broker-owned fields inlined in a ``broker_ref`` mqtt block, or ()."""

    if not isinstance(mqtt_settings, dict):
        return ()
    return tuple(
        sorted(
            field
            for field in MQTT_BROKER_CONNECTION_FIELDS
            if mqtt_settings.get(field) not in (None, "")
        )
    )


class ConfigUpgradeError(Exception):
    """Raised when config upgrade planning must abort before writing."""


MISSING = object()


class _JsonLayoutParser:
    def __init__(self, text):
        self.text = text
        self.pos = 0
        self.objects = {}
        self.list_item_objects = {}

    def parse(self):
        self._parse_value(())
        return {
            "objects": self.objects,
            "list_item_objects": self.list_item_objects,
        }

    def _skip_ws(self):
        start = self.pos
        while self.pos < len(self.text) and self.text[self.pos] in " \t\r\n":
            self.pos += 1
        return self.text[start:self.pos]

    @staticmethod
    def _blank_lines_in(ws):
        return max(0, ws.count("\n") - 1)

    def _parse_string(self):
        start = self.pos
        self.pos += 1
        escaped = False
        while self.pos < len(self.text):
            char = self.text[self.pos]
            self.pos += 1
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                break
        return json.loads(self.text[start:self.pos])

    def _parse_primitive(self):
        while self.pos < len(self.text) and self.text[self.pos] not in ",]} \t\r\n":
            self.pos += 1

    def _parse_value(self, path):
        self._skip_ws()
        if self.pos >= len(self.text):
            return
        char = self.text[self.pos]
        if char == "{":
            self._parse_object(path)
        elif char == "[":
            self._parse_array(path)
        elif char == '"':
            self._parse_string()
        else:
            self._parse_primitive()

    def _parse_object(self, path):
        start = self.pos
        self.pos += 1
        keys = []
        blank_lines_before = {}
        ws = self._skip_ws()
        if self.pos < len(self.text) and self.text[self.pos] == "}":
            self.pos += 1
            self.objects[path] = {
                "keys": keys,
                "blank_lines_before": blank_lines_before,
                "inline": "\n" not in self.text[start:self.pos],
            }
            return

        while self.pos < len(self.text):
            before = self._blank_lines_in(ws)
            key = self._parse_string()
            keys.append(key)
            blank_lines_before[key] = before
            self._skip_ws()
            if self.pos < len(self.text) and self.text[self.pos] == ":":
                self.pos += 1
            self._parse_value(path + (key,))
            ws = self._skip_ws()
            if self.pos < len(self.text) and self.text[self.pos] == ",":
                self.pos += 1
                ws = self._skip_ws()
                continue
            if self.pos < len(self.text) and self.text[self.pos] == "}":
                self.pos += 1
                break

        layout = {
            "keys": keys,
            "blank_lines_before": blank_lines_before,
            "inline": "\n" not in self.text[start:self.pos],
        }
        self.objects[path] = layout
        if path and isinstance(path[-1], int):
            self.list_item_objects.setdefault(path[:-1], layout)

    def _parse_array(self, path):
        self.pos += 1
        index = 0
        self._skip_ws()
        if self.pos < len(self.text) and self.text[self.pos] == "]":
            self.pos += 1
            return
        while self.pos < len(self.text):
            self._parse_value(path + (index,))
            index += 1
            self._skip_ws()
            if self.pos < len(self.text) and self.text[self.pos] == ",":
                self.pos += 1
                self._skip_ws()
                continue
            if self.pos < len(self.text) and self.text[self.pos] == "]":
                self.pos += 1
                break


def _extract_template_layout(text):
    return _JsonLayoutParser(text).parse()


def _is_json_scalar(value):
    return value is None or isinstance(value, (str, int, float, bool))


def _layout_for_path(layout, path):
    if not layout:
        return None
    if path and isinstance(path[-1], int):
        list_item = layout.get("list_item_objects", {}).get(path[:-1])
        if list_item is not None:
            return list_item
    exact = layout.get("objects", {}).get(path)
    if exact is not None:
        return exact
    return None


def _ordered_object_items(value, object_layout):
    if not object_layout:
        return list(value.items())

    known = [
        (key, value[key])
        for key in object_layout["keys"]
        if key in value
    ]
    unknown = [
        (key, item)
        for key, item in value.items()
        if key not in object_layout["keys"]
    ]
    return known + unknown


def _render_inline_object(value, object_layout):
    items = _ordered_object_items(value, object_layout)
    if not all(_is_json_scalar(item) for _, item in items):
        return None
    body = ", ".join(
        f"{json.dumps(key)}: {json.dumps(item)}"
        for key, item in items
    )
    return "{ " + body + " }"


def _render_json_value(value, path, indent, layout):
    prefix = " " * indent
    if isinstance(value, dict):
        object_layout = _layout_for_path(layout, path)
        if object_layout and object_layout.get("inline"):
            inline = _render_inline_object(value, object_layout)
            if inline is not None:
                return [prefix + inline]
        if not value:
            return [prefix + "{}"]

        lines = [prefix + "{"]
        previous_item = False
        known_keys = set(object_layout["keys"]) if object_layout else set()
        for key, item in _ordered_object_items(value, object_layout):
            if previous_item:
                lines[-1] += ","
            if previous_item and object_layout and key in known_keys:
                for _ in range(object_layout["blank_lines_before"].get(key, 0)):
                    lines.append("")
            elif previous_item and object_layout and key not in known_keys:
                lines.append("")

            child = _render_json_value(item, path + (key,), indent + 2, layout)
            key_prefix = " " * (indent + 2) + f"{json.dumps(key)}: "
            lines.append(key_prefix + child[0].strip())
            lines.extend(child[1:])
            previous_item = True
        lines.append(prefix + "}")
        return lines

    if isinstance(value, list):
        if not value:
            return [prefix + "[]"]
        is_comment_value = bool(path and _is_comment_key(path[-1]))
        if not is_comment_value and all(_is_json_scalar(item) for item in value):
            inline = "[" + ", ".join(json.dumps(item) for item in value) + "]"
            if len(prefix) + len(inline) <= 100:
                return [prefix + inline]

        lines = [prefix + "["]
        previous_item = False
        for index, item in enumerate(value):
            if previous_item:
                lines[-1] += ","
            lines.extend(
                _render_json_value(item, path + (index,), indent + 2, layout)
            )
            previous_item = True
        lines.append(prefix + "]")
        return lines

    return [prefix + json.dumps(value)]


def render_config_json(data, layout=None):
    if layout is None:
        return json.dumps(data, indent=2) + "\n"
    return "\n".join(_render_json_value(data, (), 0, layout)) + "\n"


def default_safe_config():
    """Return a minimal safe config for simulation and replay."""

    return {
        "config_schema_version": CURRENT_CONFIG_SCHEMA_VERSION,
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
            "allow_mqtt_local_control_writes": False,
            "allow_mqtt_zendure_control_writes": False,
            "allow_state_reconciliation_writes": False,
            "reconcile_ac_mode_on_start": True,
            "reconcile_smart_mode": True,
            "max_total_power": 800,
            "max_device_power": 800,
            "deadband": 2,
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
        "config_upgrade": copy.deepcopy(CONFIG_UPGRADE_DEFAULTS),
        "influxdb": copy.deepcopy(INFLUXDB_DEFAULTS),
        "devices": [],
        "zendure_mqtt": {},
        "grid_meter": {
            "type": "shelly",
            "ip": ""
        },
        "shelly": {
            "ip": ""
        }
    }


def default_runtime_config():
    """Missing-key defaults for a normal (non-simulation) config load.

    Starts from the explicit safe defaults, then switches simulation off and
    applies the canonical release write-gate defaults so a normal config that
    omits the gate keys resolves the same effective values with or without a
    config-upgrade file rewrite. Safe-mode paths keep ``default_safe_config``.
    """

    config = default_safe_config()
    config["system"]["simulation_mode"] = False
    config["system"].update(RELEASE_WRITE_GATE_DEFAULTS)
    return config


def _deep_merge_defaults(defaults, values):
    if not isinstance(defaults, dict):
        return copy.deepcopy(values if values is not None else defaults)

    if not isinstance(values, dict):
        values = {}

    merged = copy.deepcopy(defaults)
    for key, value in values.items():
        if (
            key in merged
            and isinstance(merged[key], dict)
            and isinstance(value, dict)
        ):
            merged[key] = _deep_merge_defaults(merged[key], value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def apply_runtime_config_defaults(config):
    if not isinstance(config, dict):
        config = {}

    merged = _deep_merge_defaults(default_runtime_config(), config)
    legacy_shelly = config.get("shelly", {})
    if (
        "grid_meter" not in config
        and isinstance(legacy_shelly, dict)
        and legacy_shelly.get("ip")
    ):
        merged["grid_meter"] = {
            "type": "shelly",
            "ip": str(legacy_shelly.get("ip", "")),
        }
    return merged


def _is_comment_key(key):
    return isinstance(key, str) and key.startswith("_comment")


def _load_template_upgrade_data(base_dir=None):
    base_dir = base_dir or BASE_DIR or os.getcwd()
    path = resolve_template_path(base_dir=base_dir)
    with open(path) as f:
        raw_template = f.read()
    template = json.loads(raw_template)
    if not isinstance(template, dict):
        raise ValueError("config.template.json must contain a JSON object")
    layout = _extract_template_layout(raw_template)

    sample_devices = template.get("devices", [])
    device_defaults = {}
    if sample_devices and isinstance(sample_devices[0], dict):
        device_defaults = {
            key: copy.deepcopy(value)
            for key, value in sample_devices[0].items()
            if key not in ("name", "ip", "sn")
        }

    view = copy.deepcopy(template)
    view["config_schema_version"] = LATEST_CONFIG_SCHEMA_VERSION
    view["devices"] = []
    view.pop("shelly", None)
    return view, path, device_defaults, layout


def load_template_upgrade_view(base_dir=None):
    view, path, _, _ = _load_template_upgrade_data(base_dir)
    return view, path


def _path_exists(values, path):
    cursor = values
    for key in path:
        if isinstance(cursor, dict):
            if key not in cursor:
                return False
            cursor = cursor[key]
        elif isinstance(cursor, list) and isinstance(key, int):
            if key < 0 or key >= len(cursor):
                return False
            cursor = cursor[key]
        else:
            return False
    return True


def _path_value(values, path):
    cursor = values
    for key in path:
        cursor = cursor[key]
    return cursor


def _format_path(path):
    result = ""
    for item in path:
        if isinstance(item, int):
            result += f"[{item}]"
        elif result:
            result += f".{item}"
        else:
            result = str(item)
    return result


def _collect_added_paths(original, upgraded, path=(), *, comments):
    if comments and path and _is_comment_key(path[-1]):
        if original is MISSING:
            return [path]
        return []

    if isinstance(upgraded, dict):
        added = []
        original_dict = original if isinstance(original, dict) else {}
        for key, value in upgraded.items():
            is_comment = _is_comment_key(key)
            if comments != is_comment:
                continue
            child_path = path + (key,)
            child_original = (
                original_dict[key] if key in original_dict else MISSING
            )
            added.extend(
                _collect_added_paths(
                    child_original,
                    value,
                    child_path,
                    comments=comments,
                )
            )
        if comments:
            for key, value in upgraded.items():
                if not _is_comment_key(key):
                    child_path = path + (key,)
                    child_original = (
                        original_dict[key] if key in original_dict else MISSING
                    )
                    added.extend(
                        _collect_added_paths(
                            child_original,
                            value,
                            child_path,
                            comments=comments,
                        )
                    )
        return added

    if isinstance(upgraded, list):
        added = []
        original_list = original if isinstance(original, list) else []
        for index, value in enumerate(upgraded):
            original_value = (
                original_list[index]
                if index < len(original_list)
                else MISSING
            )
            added.extend(
                _collect_added_paths(
                    original_value,
                    value,
                    path + (index,),
                    comments=comments,
                )
            )
        return added

    if comments:
        return []

    if original is MISSING:
        return [path]
    return []


def _merge_template_upgrade_view(template, user_config, device_defaults):
    upgraded = _deep_merge_defaults(template, user_config)
    devices = user_config.get("devices")
    if isinstance(devices, list):
        enriched = []
        for item in devices:
            if isinstance(item, dict):
                enriched.append(_deep_merge_defaults(device_defaults, item))
            else:
                enriched.append(copy.deepcopy(item))
        upgraded["devices"] = enriched
    return upgraded


def read_config_schema_version(config):
    value = config.get("config_schema_version")
    if value is None:
        return 1
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ConfigUpgradeError(
            f"invalid config_schema_version: {value!r}"
        ) from exc
    if parsed < 1:
        raise ConfigUpgradeError(
            f"invalid config_schema_version: {value!r}"
        )
    return parsed


def _migration_entry_parts(entry):
    if isinstance(entry, tuple):
        return entry[0], entry[1]
    return getattr(entry, "__name__", "schema migration"), entry


def migrate_config_1_to_2(config, changes):
    legacy_shelly = config.get("shelly", {})
    if (
        "grid_meter" not in config
        and isinstance(legacy_shelly, dict)
        and legacy_shelly.get("ip")
    ):
        config["grid_meter"] = {
            "type": "shelly",
            "ip": str(legacy_shelly.get("ip", "")),
        }
        changes.append({
            "path": "grid_meter.ip",
            "value": config["grid_meter"]["ip"],
            "reason": "from legacy shelly.ip",
        })
    return config


def migrate_config_2_to_3(config, changes):
    return config


CONFIG_MIGRATIONS = {
    (1, 2): ("migrate legacy grid meter settings", migrate_config_1_to_2),
    (2, 3): ("enable template-sync-only schema marker", migrate_config_2_to_3),
}

TEMPLATE_PLACEHOLDER_VALUES = {
    "192.168.1.100",
    "192.168.1.101",
    "192.168.1.50",
    "0.0.0.0",
    "example.com",
    "localhost",
    "your_sn",
    "your_token_here",
}


def _is_template_placeholder_host(host):
    if not host:
        return False
    value = str(host).strip().lower().rstrip(".")
    return value in TEMPLATE_PLACEHOLDER_VALUES or value.endswith(".example.com")


def is_template_placeholder_value(value):
    if value is None:
        return False
    text = str(value).strip()
    if not text:
        return False

    lowered = text.lower()
    if lowered in TEMPLATE_PLACEHOLDER_VALUES:
        return True
    if (
        lowered.startswith("your_")
        or lowered.startswith("your-")
        or "your_" in lowered
        or "your-" in lowered
    ):
        return True
    parsed = urlparse(text if "://" in text else f"//{text}")
    host = (parsed.hostname or "").lower()
    return _is_template_placeholder_host(host)


def _missing_or_placeholder(value):
    return (
        value is None
        or str(value).strip() == ""
        or is_template_placeholder_value(value)
    )


def grid_meter_mqtt_settings(grid_meter):
    """Return MQTT backend settings from either nested or legacy flat config."""

    if not isinstance(grid_meter, dict):
        return {}

    settings = {}
    nested = grid_meter.get("mqtt")
    if isinstance(nested, dict):
        settings.update(copy.deepcopy(nested))

    for key in MQTT_GRID_METER_KEYS:
        if key not in settings and key in grid_meter:
            settings[key] = copy.deepcopy(grid_meter[key])

    return settings


def _zendure_mqtt_broker_connection(config, broker_ref):
    """Return raw connection settings for an effective broker profile, or None.

    Resolves ``broker_ref`` through the shared effective-profile resolver so the
    grid meter, the runtime and the credential scanner agree on which broker a
    ref names: ``default`` addresses the implicit legacy top-level broker even
    when other named brokers exist. Returns a dict with
    host/port/tls/tls_insecure/username/password/source/enabled, or ``None`` when
    the ref resolves to no effective broker.
    """

    from ems.zendure_mqtt.config_entries import (
        effective_broker_enabled,
        get_effective_mqtt_broker_profile,
    )

    effective = get_effective_mqtt_broker_profile(config, broker_ref)
    if effective is None:
        return None
    profile = effective.config
    tls = safe_bool(profile.get("tls"), False)
    port = parse_mqtt_port(
        profile.get("port"), default=default_mqtt_port(tls)
    )
    return {
        "host": str(profile.get("host") or "").strip(),
        "port": port,
        "tls": tls,
        "tls_insecure": safe_bool(profile.get("tls_insecure"), False),
        "username": str(profile.get("username") or ""),
        "password": str(profile.get("password") or ""),
        "credentials_ref": profile.get("credentials_ref"),
        "source": str(profile.get("source") or "local_mqtt").strip().lower(),
        "enabled": effective_broker_enabled(effective),
    }


def resolve_grid_meter_mqtt_settings(config):
    """Resolve the MQTT grid-meter connection, honoring a named broker profile.

    - ``grid_meter.mqtt.broker_ref`` present: the broker profile owns host, port,
      TLS and credentials; the grid-meter block keeps topic, payload format,
      value path and staleness. Inlining a conflicting connection value alongside
      ``broker_ref`` is rejected as ambiguous.
    - No ``broker_ref``: the existing inline ``grid_meter.mqtt`` settings are used
      unchanged (legacy configs keep working).

    Returns the merged MQTT settings dict (never mutates ``config``). Raises
    ``ValueError`` for an unknown/disabled broker ref or a conflicting inline
    connection value. Broker secrets are never copied into the grid-meter block
    on disk; they are only merged into the in-memory runtime settings here.
    """

    grid_meter = config.get("grid_meter") if isinstance(config, dict) else None
    settings = grid_meter_mqtt_settings(grid_meter)
    broker_ref = settings.get("broker_ref")
    if not (isinstance(broker_ref, str) and broker_ref.strip()):
        return settings

    broker_ref = broker_ref.strip()
    conflicting = mqtt_broker_reference_conflict_fields(settings)
    if conflicting:
        raise MqttBrokerReferenceAmbiguousError(conflicting, path="grid_meter.mqtt")

    connection = _zendure_mqtt_broker_connection(config, broker_ref)
    if connection is None:
        raise ValueError(
            f"grid_meter.mqtt.broker_ref '{broker_ref}' is not a configured "
            "zendure_mqtt broker profile"
        )
    if not connection["enabled"]:
        raise ValueError(
            f"grid_meter.mqtt.broker_ref '{broker_ref}' is disabled"
        )
    if connection["source"] != "local_mqtt":
        raise ValueError(
            f"grid_meter.mqtt.broker_ref '{broker_ref}' is not a local_mqtt broker; "
            "Zendure Cloud MQTT grid meters are not supported"
        )

    resolved = dict(settings)
    resolved.pop("broker_ref", None)
    resolved["host"] = connection["host"]
    resolved["port"] = connection["port"]
    resolved["tls"] = connection["tls"]
    resolved["tls_insecure"] = connection["tls_insecure"]
    resolved["username"] = connection["username"]
    resolved["password"] = connection["password"]
    resolved["credentials_ref"] = connection["credentials_ref"]
    return resolved


def normalize_mqtt_grid_meter_settings(grid_meter, *, meter_type=None):
    meter_type = str(
        meter_type
        or (grid_meter.get("type") if isinstance(grid_meter, dict) else "mqtt")
    ).strip().lower()
    settings = grid_meter_mqtt_settings(grid_meter)

    credentials_ref = settings.get("credentials_ref")
    if isinstance(credentials_ref, str):
        credentials_ref = credentials_ref.strip() or None
    elif credentials_ref is not None:
        raise ValueError("MQTT grid meter credentials_ref must be a string")
    if credentials_ref and any(
        settings.get(key) not in (None, "") for key in ("username", "password")
    ):
        raise ValueError(
            "MQTT grid meter credentials_ref conflicts with inline credentials"
        )
    if credentials_ref:
        settings["credentials_ref"] = credentials_ref
    else:
        settings.pop("credentials_ref", None)

    for key in (
        "host",
        "username",
        "password",
        "topic",
        "payload_format",
        "value_path",
    ):
        if key in settings and settings[key] is not None:
            settings[key] = str(settings[key])

    if not str(settings.get("host") or "").strip():
        raise ValueError("MQTT grid meter requires grid_meter.mqtt.host")
    if not str(settings.get("topic") or "").strip():
        raise ValueError("MQTT grid meter requires grid_meter.mqtt.topic")

    tls, tls_insecure = resolve_mqtt_tls_metadata(
        tls_mode=settings.get("tls_mode"),
        tls=settings.get("tls"),
        tls_insecure=settings.get("tls_insecure"),
    )
    try:
        settings["port"] = parse_mqtt_port(
            settings.get("port"), default=default_mqtt_port(tls)
        )
    except ValueError as exc:
        raise ValueError(
            f"MQTT grid meter has an invalid grid_meter.mqtt.port: {exc}"
        ) from exc

    payload_format = str(settings.get("payload_format") or "number").strip().lower()
    if payload_format not in ("number", "json"):
        raise ValueError("MQTT grid meter payload_format must be number or json")
    if (
        meter_type == ZENDURE_SMARTMETER_D0_GRID_METER_TYPE
        and payload_format != "number"
    ):
        raise ValueError(
            "Zendure SmartMeter D0 grid meter requires payload_format number"
        )
    settings["payload_format"] = payload_format
    if payload_format != "json":
        settings.pop("value_path", None)
    settings["max_age_seconds"] = max(
        1,
        safe_int(settings.get("max_age_seconds", 15), 15, minimum=1),
    )
    # TLS mirrors the Zendure MQTT read client: plain by default, insecure only
    # when explicitly enabled.
    settings["tls"] = tls
    settings["tls_insecure"] = tls_insecure
    return settings


def template_placeholder_paths(config):
    """Return required setup fields that still contain template-like values."""

    if not isinstance(config, dict):
        return []

    paths = []
    from ems.zendure_mqtt.config_entries import is_zendure_mqtt_device_config

    devices = config.get("devices")
    configured_devices = []
    if isinstance(devices, list):
        configured_devices = [
            device for device in devices
            if isinstance(device, dict)
        ]
        for index, device in enumerate(configured_devices):
            # Telemetry-only Zendure MQTT entries have no ip/sn by design.
            if is_zendure_mqtt_device_config(device):
                continue
            if _missing_or_placeholder(device.get("ip")):
                paths.append(f"devices[{index}].ip")
            if _missing_or_placeholder(device.get("sn")):
                paths.append(f"devices[{index}].sn")

    grid_meter = config.get("grid_meter")
    if isinstance(grid_meter, dict) and configured_devices:
        meter_type = str(grid_meter.get("type") or "shelly").strip().lower()
        if meter_type in MQTT_GRID_METER_TYPES:
            mqtt_settings = grid_meter_mqtt_settings(grid_meter)
            broker_ref = mqtt_settings.get("broker_ref")
            if isinstance(broker_ref, str) and broker_ref.strip():
                # A named broker profile owns the connection, so the inline host
                # is intentionally absent — validate the profile's host instead of
                # demanding an inline one. An unknown/disabled/non-local ref is a
                # genuine misconfiguration rejected by
                # resolve_grid_meter_mqtt_settings at load time, not a template
                # placeholder, so it is not flagged here.
                try:
                    connection = _zendure_mqtt_broker_connection(
                        config, broker_ref.strip()
                    )
                except ValueError:
                    # An invalid broker port (etc.) is rejected with an explicit
                    # error by resolve_grid_meter_mqtt_settings at load time.
                    connection = None
                if connection is not None and _missing_or_placeholder(
                    connection.get("host")
                ):
                    paths.append(f"zendure_mqtt.brokers.{broker_ref.strip()}.host")
            elif _missing_or_placeholder(mqtt_settings.get("host")):
                paths.append("grid_meter.mqtt.host")
            if _missing_or_placeholder(mqtt_settings.get("topic")):
                paths.append("grid_meter.mqtt.topic")
        elif meter_type == "tasmota_http":
            endpoint = grid_meter.get("url") or grid_meter.get("ip")
            if _missing_or_placeholder(endpoint):
                paths.append("grid_meter.url")
            if _missing_or_placeholder(grid_meter.get("power_path")):
                paths.append("grid_meter.power_path")
        elif meter_type != "ha" and _missing_or_placeholder(grid_meter.get("ip")):
            paths.append("grid_meter.ip")

    ha_config = config.get("ha")
    if isinstance(ha_config, dict) and safe_bool(ha_config.get("enabled"), False):
        if _missing_or_placeholder(ha_config.get("url")):
            paths.append("ha.url")
        if _missing_or_placeholder(ha_config.get("token")):
            paths.append("ha.token")

    return paths


def apply_template_placeholder_safety(config, *, emit_message=None):
    """Force no-write runtime mode while required setup fields are placeholders."""

    paths = template_placeholder_paths(config)
    if not paths:
        return config

    protected = copy.deepcopy(config)
    system = protected.setdefault("system", {})
    system["enabled"] = False
    system["dry_run"] = True
    system["allow_hardware_writes"] = False
    system["allow_mqtt_local_control_writes"] = False
    system["allow_mqtt_zendure_control_writes"] = False
    system["allow_state_reconciliation_writes"] = False

    if emit_message:
        shown = ", ".join(paths[:6])
        if len(paths) > 6:
            shown += f", ... ({len(paths)} total)"
        emit_message(
            logging.WARNING,
            "Config still contains template placeholder values; "
            "forcing EMS control disabled, dry_run=true and hardware writes "
            f"disabled until configured fields are replaced: {shown}",
        )

    return protected


def run_config_schema_migrations(
    user_config,
    *,
    migrations=None,
    latest_schema=None,
):
    latest_schema = latest_schema or LATEST_CONFIG_SCHEMA_VERSION
    migrations = CONFIG_MIGRATIONS if migrations is None else migrations
    config = copy.deepcopy(user_config)
    current_schema = read_config_schema_version(config)

    if current_schema > latest_schema:
        raise ConfigUpgradeError(
            "config_schema_version "
            f"{current_schema} is newer than this EMS supports "
            f"({latest_schema}); config.json appears to come from a newer "
            "EMS version"
        )

    steps = []
    while current_schema < latest_schema:
        next_schema = current_schema + 1
        entry = migrations.get((current_schema, next_schema))
        if entry is None:
            raise ConfigUpgradeError(
                "missing config schema migration "
                f"{current_schema} -> {next_schema}; config.json not changed"
            )
        description, migration = _migration_entry_parts(entry)
        changes = []
        config = migration(config, changes)
        if not isinstance(config, dict):
            raise ConfigUpgradeError(
                f"config schema migration {current_schema} -> {next_schema} "
                "did not return a JSON object"
            )
        steps.append({
            "from": current_schema,
            "to": next_schema,
            "description": description,
            "changes": changes,
        })
        current_schema = next_schema

    config["config_schema_version"] = latest_schema
    return config, steps


def _items_from_paths(upgraded, paths):
    return [
        {
            "path": _format_path(path),
            "value": _path_value(upgraded, path),
        }
        for path in paths
    ]


def _collect_comment_items(values, path=()):
    items = []
    if isinstance(values, dict):
        for key, value in values.items():
            child_path = path + (key,)
            if _is_comment_key(key):
                items.append((child_path, copy.deepcopy(value)))
            else:
                items.extend(_collect_comment_items(value, child_path))
    elif isinstance(values, list):
        for index, value in enumerate(values):
            items.extend(_collect_comment_items(value, path + (index,)))
    return items


def _template_comment_items_for_config(user_config, base_dir=None):
    template, _, device_defaults, _ = _load_template_upgrade_data(base_dir)
    items = _collect_comment_items(template)

    devices = user_config.get("devices")
    if isinstance(devices, list):
        device_comment_items = _collect_comment_items(device_defaults)
        for index, device in enumerate(devices):
            if isinstance(device, dict):
                for path, value in device_comment_items:
                    items.append((("devices", index) + path, copy.deepcopy(value)))

    return items


def template_comment_differences(user_config, base_dir=None):
    if not isinstance(user_config, dict):
        raise ValueError("config.json must contain a JSON object")

    differences = []
    for path, template_value in _template_comment_items_for_config(
        user_config,
        base_dir,
    ):
        if _path_exists(user_config, path):
            current_value = _path_value(user_config, path)
            if current_value != template_value:
                differences.append({
                    "path": _format_path(path),
                    "value": copy.deepcopy(template_value),
                    "old_value": copy.deepcopy(current_value),
                })
    return differences


def refresh_template_comments(user_config, base_dir=None):
    if not isinstance(user_config, dict):
        raise ValueError("config.json must contain a JSON object")

    refreshed = copy.deepcopy(user_config)
    differences = []
    for path, template_value in _template_comment_items_for_config(
        refreshed,
        base_dir,
    ):
        if _path_exists(refreshed, path):
            current_value = _path_value(refreshed, path)
            if current_value != template_value:
                parent = _path_value(refreshed, path[:-1]) if path[:-1] else refreshed
                parent[path[-1]] = copy.deepcopy(template_value)
                differences.append({
                    "path": _format_path(path),
                    "value": copy.deepcopy(template_value),
                    "old_value": copy.deepcopy(current_value),
                })
    return refreshed, differences


def build_config_upgrade_plan(
    user_config,
    base_dir=None,
    *,
    migrations=None,
    latest_schema=None,
):
    if not isinstance(user_config, dict):
        raise ValueError("config.json must contain a JSON object")

    latest_schema = latest_schema or LATEST_CONFIG_SCHEMA_VERSION
    template, template_path, device_defaults, layout = _load_template_upgrade_data(
        base_dir
    )
    template["config_schema_version"] = latest_schema
    migrated_config, schema_steps = run_config_schema_migrations(
        user_config,
        migrations=migrations,
        latest_schema=latest_schema,
    )
    upgraded = _merge_template_upgrade_view(
        template,
        migrated_config,
        device_defaults,
    )
    upgraded["config_schema_version"] = latest_schema

    added_paths = _collect_added_paths(
        user_config,
        upgraded,
        comments=False,
    )
    comment_paths = _collect_added_paths(
        user_config,
        upgraded,
        comments=True,
    )

    migration_paths = {
        change["path"]
        for step in schema_steps
        for change in step.get("changes", [])
        if "path" in change
    }
    added = [
        item for item in _items_from_paths(upgraded, added_paths)
        if item["path"] not in migration_paths
    ]
    comments = _items_from_paths(upgraded, comment_paths)
    outdated_comments = template_comment_differences(user_config, base_dir)

    return {
        "template_path": template_path,
        "template_layout": layout,
        "upgraded_config": upgraded,
        "add": added,
        "comment_add": comments,
        "comment_refresh": outdated_comments,
        "schema_migrations": schema_steps,
        "migrate": [
            change
            for step in schema_steps
            for change in step.get("changes", [])
        ],
        "changed": upgraded != user_config,
    }


def write_config_json_atomic(path, data, *, layout=None):
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)

    mode = 0o600
    uid = gid = None
    try:
        current = os.stat(path)
        mode = stat.S_IMODE(current.st_mode)
        uid, gid = current.st_uid, current.st_gid
    except FileNotFoundError:
        pass

    tmp_path = f"{path}.{os.getpid()}.tmp"
    fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        with os.fdopen(fd, "w") as f:
            f.write(render_config_json(data, layout))
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp_path, mode)
        if uid is not None and gid is not None:
            try:
                os.chown(tmp_path, uid, gid)
            except PermissionError:
                pass
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.remove(tmp_path)
        except FileNotFoundError:
            pass
        raise


def normalize_config_upgrade_config(config, *, emit_warning=None):
    if not isinstance(config, dict):
        config = {}

    merged = {
        **CONFIG_UPGRADE_DEFAULTS,
        **config,
    }

    mode = str(merged.get("on_startup", "")).strip().lower()
    if mode not in CONFIG_UPGRADE_STARTUP_MODES:
        if emit_warning:
            emit_warning(
                "Invalid config_upgrade.on_startup "
                f"{merged.get('on_startup')!r}; using 'check'."
            )
        mode = CONFIG_UPGRADE_DEFAULTS["on_startup"]

    backup_failure_policy = str(
        merged.get("backup_failure_policy", "")
    ).strip().lower()
    if backup_failure_policy not in CONFIG_UPGRADE_BACKUP_FAILURE_POLICIES:
        if emit_warning:
            emit_warning(
                "Invalid config_upgrade.backup_failure_policy "
                f"{merged.get('backup_failure_policy')!r}; using "
                "'continue_without_upgrade'."
            )
        backup_failure_policy = CONFIG_UPGRADE_DEFAULTS[
            "backup_failure_policy"
        ]

    return {
        "on_startup": mode,
        "backup_before_apply": safe_bool(
            merged.get("backup_before_apply"),
            CONFIG_UPGRADE_DEFAULTS["backup_before_apply"],
        ),
        "backup_failure_policy": backup_failure_policy,
    }


def _emit_startup_config_message(level, message):
    if logging.getLogger().handlers:
        logging.log(level, message)
    else:
        print(message, file=sys.stderr)


def _read_raw_config(path):
    with open(path) as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError("config.json must contain a JSON object")
    return data


def _startup_config_upgrade_summary(plan):
    return len(plan.get("add", [])) + len(plan.get("comment_add", [])) + len(
        plan.get("migrate", [])
    )


def _create_startup_config_backup(raw_config, path, base_dir):
    from ems import backup as backup_mod

    runtime_config = apply_runtime_config_defaults(raw_config)
    return backup_mod.create_config_backup(
        runtime_config,
        base_dir=base_dir,
        config_path=path,
        backup_purpose="auto",
    )


def perform_startup_config_upgrade(
    raw_config,
    path,
    base_dir=None,
    *,
    backup_factory=None,
    emit_message=None,
):
    base_dir = base_dir or BASE_DIR or os.getcwd()
    emit_message = emit_message or _emit_startup_config_message

    def warn(message):
        emit_message(logging.WARNING, message)

    upgrade_config = normalize_config_upgrade_config(
        raw_config.get("config_upgrade", {}),
        emit_warning=warn,
    )
    mode = upgrade_config["on_startup"]
    if mode == "disabled":
        return raw_config

    try:
        plan = build_config_upgrade_plan(raw_config, base_dir)
    except (ConfigUpgradeError, OSError, ValueError) as exc:
        emit_message(
            logging.WARNING,
            f"Config startup upgrade check skipped: {exc}",
        )
        return raw_config

    if not plan["changed"]:
        return raw_config

    missing_count = _startup_config_upgrade_summary(plan)
    if mode == "check":
        emit_message(
            logging.INFO,
            "Config upgrade available: "
            f"{missing_count} missing keys from config.template.json.\n"
            "Run: python3 emsctl.py config upgrade --dry-run",
        )
        return raw_config

    if upgrade_config["backup_before_apply"]:
        create_backup = backup_factory or _create_startup_config_backup
        try:
            backup_path = create_backup(raw_config, path, base_dir)
        except Exception as exc:
            emit_message(
                logging.WARNING,
                "Config auto-upgrade skipped because backup failed. "
                f"Starting with existing config. Error: {exc}",
            )
            return raw_config
        emit_message(logging.INFO, f"Config auto-upgrade backup created: {backup_path}")

    write_config_json_atomic(
        path,
        plan["upgraded_config"],
        layout=plan.get("template_layout"),
    )
    emit_message(
        logging.INFO,
        "Config auto-upgrade applied; reloading config.json before startup.",
    )
    return _read_raw_config(path)


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

# Canonical release defaults for the three output-control write gates. Local API
# and both MQTT transports default on; whether a transport actually writes is
# decided by its configuration presence (API key, broker host, per-device opt-in)
# and the runtime safety preconditions (dry_run/simulation/replay). The release
# template (config/config.template.json and config_catalog._DEFAULT_TEMPLATE),
# config upgrade and the normal runtime missing-key defaults
# (default_runtime_config) all resolve from this one definition, so a normal
# config that omits the gate keys behaves the same with or without a file
# rewrite. Safe paths (default_safe_config, template-placeholder safety) force
# every gate off, and the parser below keeps a fail-safe False fallback for a
# config that bypasses the defaults merge entirely.
RELEASE_WRITE_GATE_DEFAULTS = {
    "allow_hardware_writes": True,
    "allow_mqtt_local_control_writes": True,
    "allow_mqtt_zendure_control_writes": True,
}

ALLOW_HARDWARE_WRITES = False
ALLOW_MQTT_LOCAL_CONTROL_WRITES = False
ALLOW_MQTT_ZENDURE_CONTROL_WRITES = False
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
INFLUXDB_CONFIG = None
ENERGY_SAVINGS_CONFIG = ENERGY_SAVINGS_DEFAULTS.copy()
BATTERY_FULL_CHARGE_ASSIST_CONFIG = BATTERY_FULL_CHARGE_ASSIST_DEFAULTS.copy()
OFFGRID_SOCKET_MODES = {
    "standard": 0,
    "eco": 1,
    "off": 2
}
ZENDURE_CONFIG = []
ZENDURE_MQTT_CONFIG = {}
SHELLY_IP = ""
GRID_METER_CONFIG = {
    "type": "shelly",
    "ip": ""
}


def load_config(args=None, base_dir=None):
    args = args or ARGS
    base_dir = base_dir or BASE_DIR or os.getcwd()
    path = str(resolve_config_path(args.config, base_dir=base_dir))
    args.config = path

    try:
        raw_config = _read_raw_config(path)
    except FileNotFoundError:
        if args.simulate or args.replay or args.self_test:
            return default_safe_config()

        print("config.json missing. Please create it from template.")
        sys.exit(1)

    raw_config = perform_startup_config_upgrade(raw_config, path, base_dir)
    runtime_config = apply_runtime_config_defaults(raw_config)
    return apply_template_placeholder_safety(
        runtime_config,
        emit_message=_emit_startup_config_message,
    )


def initialize(args, base_dir):
    global ARGS, BASE_DIR, CONFIG, SYSTEM_ENABLED, HA_URL, HA_TOKEN
    global MAX_TOTAL_POWER, MAX_DEVICE_POWER, DEADBAND, LOOP_INTERVAL
    global OUTPUT_CONTROL_CONFIG, RUNTIME_STATE_PATH, MIN_OUTPUT_LIMIT
    global DRY_RUN, SIMULATION_MODE, ALLOW_HARDWARE_WRITES
    global ALLOW_MQTT_LOCAL_CONTROL_WRITES, ALLOW_MQTT_ZENDURE_CONTROL_WRITES
    global ALLOW_STATE_RECONCILIATION_WRITES, RECONCILE_AC_MODE_ON_START
    global RECONCILE_SMART_MODE, HA_ENABLED, HA_CONTROL_ENABLED, LOG_LEVEL
    global REDISTRIBUTE_CLAMPED_POWER, PV_KWP_WEIGHTING
    global PV_CHARGE_BALANCE_ENABLED, PV_CHARGE_BALANCE_DEADBAND_PERCENT
    global PV_CHARGE_BALANCE_FULL_BIAS_PERCENT, PV_CHARGE_BALANCE_STRENGTH
    global BATTERY_KWH_WEIGHTING
    global SOC_RECONCILE_INTERVAL, WINTER_CONFIG, DASHBOARD_CONFIG
    global INFLUXDB_CONFIG
    global ENERGY_SAVINGS_CONFIG, BATTERY_FULL_CHARGE_ASSIST_CONFIG
    global ZENDURE_CONFIG, ZENDURE_MQTT_CONFIG, SHELLY_IP, GRID_METER_CONFIG

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
    ALLOW_MQTT_LOCAL_CONTROL_WRITES = CONFIG["system"].get(
        "allow_mqtt_local_control_writes",
        False
    )
    ALLOW_MQTT_ZENDURE_CONTROL_WRITES = CONFIG["system"].get(
        "allow_mqtt_zendure_control_writes",
        False
    )
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
    DASHBOARD_CONFIG = normalize_dashboard_config(CONFIG.get("dashboard", {}))
    INFLUXDB_CONFIG = normalize_influxdb_config(CONFIG.get("influxdb"))
    ENERGY_SAVINGS_CONFIG = {
        **ENERGY_SAVINGS_DEFAULTS,
        **CONFIG.get("energy_savings", {})
    }
    BATTERY_FULL_CHARGE_ASSIST_CONFIG = normalize_battery_full_charge_assist_config(
        CONFIG.get("battery_full_charge_assist", {})
    )
    ZENDURE_CONFIG = CONFIG["devices"]
    zendure_mqtt_config = CONFIG.get("zendure_mqtt", {})
    ZENDURE_MQTT_CONFIG = zendure_mqtt_config if isinstance(zendure_mqtt_config, dict) else {}
    legacy_shelly_config = CONFIG.get("shelly", {})
    if not isinstance(legacy_shelly_config, dict):
        legacy_shelly_config = {}
    configured_grid_meter = CONFIG.get("grid_meter")
    if isinstance(configured_grid_meter, dict):
        GRID_METER_CONFIG = dict(configured_grid_meter)
        GRID_METER_CONFIG["type"] = str(
            GRID_METER_CONFIG.get("type", "shelly")
        ).strip().lower()
        for key in (
            "ip",
            "url",
            "power_path",
            "host",
            "username",
            "password",
            "topic",
            "payload_format",
            "value_path",
        ):
            if key in GRID_METER_CONFIG and GRID_METER_CONFIG[key] is not None:
                GRID_METER_CONFIG[key] = str(GRID_METER_CONFIG[key])
        if GRID_METER_CONFIG["type"] in MQTT_GRID_METER_TYPES:
            # Resolve a named broker profile (if any) before validation so the
            # runtime settings carry the broker's host/port/TLS/credentials.
            resolved_mqtt = resolve_grid_meter_mqtt_settings(CONFIG)
            GRID_METER_CONFIG["mqtt"] = normalize_mqtt_grid_meter_settings(
                {"type": GRID_METER_CONFIG["type"], "mqtt": resolved_mqtt},
                meter_type=GRID_METER_CONFIG["type"],
            )
            for stale_key in (
                "ip",
                "url",
                "power_path",
                "channels",
                "host",
                "port",
                "tls",
                "tls_insecure",
                "username",
                "password",
                "topic",
                "payload_format",
                "value_path",
                "max_age_seconds",
            ):
                GRID_METER_CONFIG.pop(stale_key, None)
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


def http_control_device_configs(devices=None):
    """Return devices[] entries that build an HTTP-controllable ZendureClient.

    Telemetry-only Zendure MQTT entries carry no ip/sn and are not controlled;
    they are excluded so startup never passes them to ZendureClient. A disabled
    entry is excluded for the same reason it is on the MQTT control path:
    ``enabled`` means the same thing for every transport, so an operator who
    disables a device really removes it from the control loop.
    """

    from ems.zendure_mqtt.config_entries import (
        config_entry_enabled,
        is_zendure_mqtt_device_config,
    )

    if devices is None:
        devices = ZENDURE_CONFIG
    if not isinstance(devices, list):
        return []
    return [
        item
        for item in devices
        if isinstance(item, dict)
        and not is_zendure_mqtt_device_config(item)
        and config_entry_enabled(item)
    ]


def mqtt_control_device_configs(devices=None):
    """Return devices[] entries that build a write-capable MQTT control device.

    These are ``zendure_mqtt`` entries that opt in to output control via
    ``capabilities.write_output_limit=true``. Telemetry-only MQTT entries and
    HTTP/API devices are excluded.
    """

    from ems.zendure_mqtt.config_entries import (
        is_control_zendure_mqtt_device_config,
    )

    if devices is None:
        devices = ZENDURE_CONFIG
    if not isinstance(devices, list):
        return []
    return [
        item
        for item in devices
        if isinstance(item, dict) and is_control_zendure_mqtt_device_config(item)
    ]


def _writes_enabled():
    """Shared safety precondition for every control-write gate."""

    return (
        not DRY_RUN
        and not SIMULATION_MODE
        and not ARGS.replay
    )


def hardware_writes_allowed():
    """API (local HTTP) outputLimit write gate."""

    return _writes_enabled() and ALLOW_HARDWARE_WRITES


def mqtt_local_control_writes_allowed():
    """Local MQTT broker outputLimit write gate."""

    return _writes_enabled() and ALLOW_MQTT_LOCAL_CONTROL_WRITES


def mqtt_zendure_control_writes_allowed():
    """Zendure cloud MQTT outputLimit write gate."""

    return _writes_enabled() and ALLOW_MQTT_ZENDURE_CONTROL_WRITES


@dataclass(frozen=True)
class WriteGateDecision:
    """Effective write-gate outcome for one device transport.

    Single source of truth for whether an ``outputLimit`` write may proceed and
    why it is blocked, shared by the controller, logs, diagnostics and tests.
    """

    allowed: bool
    transport: str
    gate_name: str
    gate_enabled: bool
    blocked_by: tuple

    def as_log_fields(self) -> dict:
        return {
            "transport": self.transport,
            "write_gate": self.gate_name,
            "write_gate_enabled": self.gate_enabled,
            "blocked_by": ",".join(self.blocked_by),
        }


# control_gate -> (transport, gate_name). Local API and both MQTT transports are
# equal control transports; the per-transport gate is an operational safety
# control, never an experimental classification.
_CONTROL_GATE_TRANSPORT = {
    "api": ("http", "allow_hardware_writes"),
    "mqtt_local": ("mqtt_local", "allow_mqtt_local_control_writes"),
    "mqtt_zendure": ("mqtt_zendure", "allow_mqtt_zendure_control_writes"),
}


def resolve_write_gate(control_gate) -> WriteGateDecision:
    """Resolve the effective write-gate decision for a device's transport."""

    transport, gate_name = _CONTROL_GATE_TRANSPORT.get(
        control_gate, _CONTROL_GATE_TRANSPORT["api"]
    )
    gate_enabled = {
        "allow_hardware_writes": ALLOW_HARDWARE_WRITES,
        "allow_mqtt_local_control_writes": ALLOW_MQTT_LOCAL_CONTROL_WRITES,
        "allow_mqtt_zendure_control_writes": ALLOW_MQTT_ZENDURE_CONTROL_WRITES,
    }[gate_name]

    blocked = []
    if DRY_RUN:
        blocked.append("dry_run")
    if SIMULATION_MODE:
        blocked.append("simulation_mode")
    if getattr(ARGS, "replay", False):
        blocked.append("replay_mode")
    if not gate_enabled:
        blocked.append(gate_name)

    return WriteGateDecision(
        allowed=not blocked,
        transport=transport,
        gate_name=gate_name,
        gate_enabled=bool(gate_enabled),
        blocked_by=tuple(blocked),
    )


def resolve_device_write_gate(device) -> WriteGateDecision:
    """Resolve the write-gate decision for a control-loop device."""

    return resolve_write_gate(getattr(device, "control_gate", "api"))


def control_writes_allowed(control_gate):
    """Dispatch the write decision to the gate named by ``control_gate``."""

    return resolve_write_gate(control_gate).allowed


def resolve_state_write_gate(control_gate="api") -> WriteGateDecision:
    """Auditable gate decision for a state-reconciliation write.

    Single policy for every transport: the device transport's own write gate
    (never a hard-coded ``allow_hardware_writes``) plus the global
    ``allow_state_reconciliation_writes`` gate. ``blocked_by`` names every gate
    that would block the write.
    """

    gate = resolve_write_gate(control_gate)
    blocked = list(gate.blocked_by)
    if not ALLOW_STATE_RECONCILIATION_WRITES:
        blocked.append("allow_state_reconciliation_writes")
    return WriteGateDecision(
        allowed=not blocked,
        transport=gate.transport,
        gate_name=gate.gate_name,
        gate_enabled=gate.gate_enabled,
        blocked_by=tuple(blocked),
    )


def state_reconciliation_writes_allowed(control_gate="api"):
    return resolve_state_write_gate(control_gate).allowed


def runtime_state_path():
    """Return absolute path to mutable runtime state."""

    if os.path.isabs(RUNTIME_STATE_PATH):
        return RUNTIME_STATE_PATH

    return os.path.join(BASE_DIR, RUNTIME_STATE_PATH)


def config_path():
    """Return the path to the static config file the EMS was started with."""

    path = resolve_config_path(
        ARGS.config if ARGS else None,
        base_dir=BASE_DIR or os.getcwd(),
    )
    return str(path)


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


MQTT_PORT_MIN = 1
MQTT_PORT_MAX = 65535
MQTT_DEFAULT_PORT = 1883
MQTT_DEFAULT_TLS_PORT = 8883


def default_mqtt_port(tls=False):
    """Protocol default MQTT port: 8883 for TLS, 1883 for plain."""

    return MQTT_DEFAULT_TLS_PORT if tls else MQTT_DEFAULT_PORT


def parse_mqtt_port(value, *, default=None):
    """Strictly validate an MQTT broker port, or raise ``ValueError``.

    The single shared MQTT port validator used by config loading, broker-profile
    parsing, Admin preview, discovery and diagnostics so preview and runtime never
    disagree. Rules:

    - an integer (a bool is not an integer) in ``[1, 65535]`` is accepted;
    - a string form of such an integer is accepted;
    - an absent value (``None`` or ``""``) yields ``default`` when one is given,
      else raises;
    - every other value — a bool, an out-of-range number, a non-numeric or
      fractional string, a fractional float — raises. An explicit invalid port is
      never silently replaced with a default or clamped into range.
    """

    if value is None or (isinstance(value, str) and not value.strip()):
        if default is not None:
            return default
        raise ValueError("MQTT port is required")

    if isinstance(value, bool):
        raise ValueError("MQTT port must be an integer, not a boolean")

    if isinstance(value, int):
        port = value
    elif isinstance(value, float):
        if not value.is_integer():
            raise ValueError(f"MQTT port must be a whole number, got {value!r}")
        port = int(value)
    elif isinstance(value, str):
        text = value.strip()
        try:
            port = int(text)
        except ValueError:
            raise ValueError(
                f"MQTT port must be an integer, got {value!r}"
            ) from None
    else:
        raise ValueError(f"MQTT port must be an integer, got {value!r}")

    if not (MQTT_PORT_MIN <= port <= MQTT_PORT_MAX):
        raise ValueError(
            f"MQTT port must be between {MQTT_PORT_MIN} and {MQTT_PORT_MAX}, "
            f"got {port}"
        )
    return port


# Canonical MQTT TLS modes shared across discovery, proposal building, config
# preview and Core. A mode is authoritative for the ``tls``/``tls_insecure``
# pair, so a TLS broker can never be silently downgraded to plain MQTT.
MQTT_TLS_MODE_PLAIN = "plaintext"
MQTT_TLS_MODE_SYSTEM_CA = "system_ca"
MQTT_TLS_MODE_INSECURE = "insecure_no_verify"

# Aliases accepted for backward compatibility with older stored broker records.
_MQTT_TLS_PLAIN_ALIASES = frozenset(
    {"", "plaintext", "plain", "disabled", "none", "tcp"}
)
_MQTT_TLS_SYSTEM_CA_ALIASES = frozenset({"system_ca", "tls", "mqtts", "ssl", "secure"})


def normalize_mqtt_tls_mode(tls_mode):
    """Map an MQTT TLS mode string to a ``(tls, tls_insecure)`` pair.

    The single shared TLS-mode normalizer. Plain modes yield ``(False, False)``,
    ``system_ca`` yields ``(True, False)`` and ``insecure_no_verify`` yields
    ``(True, True)``. An unknown mode raises ``ValueError`` rather than defaulting
    to plain, so an unrecognized mode can never downgrade a TLS broker.
    """

    mode = str(tls_mode or "").strip().lower()
    if mode in _MQTT_TLS_PLAIN_ALIASES:
        return False, False
    if mode in _MQTT_TLS_SYSTEM_CA_ALIASES:
        return True, False
    if mode == MQTT_TLS_MODE_INSECURE:
        return True, True
    raise ValueError(f"unknown MQTT TLS mode: {tls_mode!r}")


# Transport-security modes the Zendure cloud discovery client reports for its own
# connection. ``pinned_ca`` verifies against a CA bundle only that client
# carries, so a stored config records both as ``insecure_no_verify`` rather than
# claiming a verification the EMS runtime cannot reproduce.
_MQTT_TLS_OBSERVED_INSECURE_ALIASES = frozenset({"encrypted_no_verify", "pinned_ca"})

# Every mode such an observed connection may report. The discovery client picks
# its CA strategy from the individual names; the accepted set is owned here so
# the two vocabularies cannot drift apart.
MQTT_TLS_OBSERVED_MODES = (
    frozenset({MQTT_TLS_MODE_SYSTEM_CA}) | _MQTT_TLS_OBSERVED_INSECURE_ALIASES
)


def canonical_mqtt_tls_mode(tls_mode):
    """Map an observed TLS-mode name onto the canonical stored vocabulary.

    The single owner of every TLS-mode alias, including the ones only a
    discovery observation carries. An unrecognized value is returned unchanged
    so strict validation — not a silent rewrite — decides what it means.
    """

    mode = str(tls_mode or "").strip().lower()
    if mode in _MQTT_TLS_OBSERVED_INSECURE_ALIASES:
        return MQTT_TLS_MODE_INSECURE
    return tls_mode


def mqtt_tls_mode_name(*, tls, tls_insecure=False):
    """Canonical stored mode name for a resolved ``(tls, tls_insecure)`` pair.

    ``None`` for a plain connection, so a caller can store "no TLS mode" instead
    of the string ``plaintext`` where the surrounding record uses absence.
    """

    if not tls:
        return None
    return MQTT_TLS_MODE_INSECURE if tls_insecure else MQTT_TLS_MODE_SYSTEM_CA


def resolve_mqtt_tls_metadata(*, tls_mode=None, tls=None, tls_insecure=None):
    """Reconcile TLS metadata into a canonical ``(tls, tls_insecure)`` pair.

    When a ``tls_mode`` is present it is authoritative; any explicit
    ``tls``/``tls_insecure`` flag that contradicts it is rejected. Without a mode
    the explicit flags are used, but ``tls_insecure`` without ``tls`` is rejected.
    Raises ``ValueError`` on any contradiction so a TLS broker is never
    downgraded to plain MQTT by a stray flag.
    """

    if tls is not None:
        tls = require_json_bool(tls, "tls")
    if tls_insecure is not None:
        tls_insecure = require_json_bool(tls_insecure, "tls_insecure")

    mode_present = tls_mode is not None and str(tls_mode).strip() != ""
    if mode_present:
        mode_tls, mode_insecure = normalize_mqtt_tls_mode(tls_mode)
        if tls is not None and tls != mode_tls:
            raise ValueError(
                f"tls={tls!r} contradicts tls_mode={tls_mode!r}"
            )
        if tls_insecure is not None and tls_insecure != mode_insecure:
            raise ValueError(
                f"tls_insecure={tls_insecure!r} contradicts tls_mode={tls_mode!r}"
            )
        return mode_tls, mode_insecure

    resolved_tls = tls if tls is not None else False
    resolved_insecure = tls_insecure if tls_insecure is not None else False
    if resolved_insecure and not resolved_tls:
        raise ValueError("tls_insecure is set but TLS is disabled")
    return resolved_tls, resolved_insecure


def configure_mqtt_client_tls(client, *, tls, tls_insecure, ca_certs=None):
    """Apply the resolved TLS mode to a paho-style MQTT client, before connect.

    The single shared TLS application: ``tls_insecure`` skips certificate-chain
    AND hostname verification (paho's ``tls_insecure_set`` alone only disables
    the hostname check, which still rejects self-signed broker chains such as
    the Zendure cloud broker). A ``ca_certs`` bundle pins the chain to that CA
    while tolerating hostname mismatches. Plain TLS keeps full verification and
    ``tls=False`` never touches the client.
    """

    if not tls:
        if tls_insecure:
            raise ValueError("tls_insecure is set but TLS is disabled")
        return
    if ca_certs:
        client.tls_set(ca_certs=str(ca_certs))
        client.tls_insecure_set(True)
    elif tls_insecure:
        client.tls_set(cert_reqs=ssl.CERT_NONE)
        client.tls_insecure_set(True)
    else:
        client.tls_set()


def require_json_bool(value, field_name):
    """Return ``value`` when it is a real JSON boolean, else raise ``ValueError``.

    Strict on purpose: strings like ``"false"`` and numbers like ``0`` are
    rejected rather than coerced, so a security- or safety-relevant flag can
    never be enabled by string truthiness.
    """

    if isinstance(value, bool):
        return value
    raise ValueError(
        f"{field_name} must be a JSON boolean (true or false), got {value!r}"
    )


def optional_json_bool(value, field_name, *, default=False):
    """Strict boolean that treats an absent value (``None``) as ``default``."""

    if value is None:
        return default
    return require_json_bool(value, field_name)


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


def normalize_dashboard_config(config):
    if not isinstance(config, dict):
        config = {}

    merged = {
        **DASHBOARD_DEFAULTS,
        **config,
    }

    merged["session_idle_timeout_seconds"] = safe_session_timeout(
        merged.get("session_idle_timeout_seconds"),
        DASHBOARD_DEFAULTS["session_idle_timeout_seconds"],
    )
    merged["session_absolute_max_seconds"] = safe_session_timeout(
        merged.get("session_absolute_max_seconds"),
        DASHBOARD_DEFAULTS["session_absolute_max_seconds"],
    )
    merged["log_buffer_lines"] = safe_int(
        merged.get("log_buffer_lines"),
        DASHBOARD_DEFAULTS["log_buffer_lines"],
        minimum=1,
    )
    merged["log_redaction"] = safe_bool(
        merged.get("log_redaction"),
        DASHBOARD_DEFAULTS["log_redaction"],
    )
    mode = str(merged.get("animation_mode", "")).strip().lower()
    merged["animation_mode"] = (
        mode if mode in DASHBOARD_ANIMATION_MODES
        else DASHBOARD_DEFAULTS["animation_mode"]
    )
    return merged


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


def is_influx_duration(value):
    """True when value looks like an InfluxDB/Flux duration such as 10s/1m/2h/7d."""
    text = str(value or "").strip()
    if len(text) < 2:
        return False

    unit = text[-1]
    if unit not in ("s", "m", "h", "d", "w"):
        return False

    try:
        amount = float(text[:-1])
    except (TypeError, ValueError):
        return False

    return amount > 0


def sanitize_bucket_prefix(value, default="ems"):
    text = str(value or "").strip()
    cleaned = "".join(
        char for char in text if char.isalnum() or char in ("_", "-")
    ).strip("_-")
    return cleaned or default


INFLUXDB_MODES = ("bundled", "external")


def normalize_influxdb_mode(value):
    """Return a valid influxdb mode, falling back to 'bundled' with a warning."""
    text = str(value or "").strip().lower()
    if text in INFLUXDB_MODES:
        return text
    if text:
        logging.warning(
            "Unknown influxdb.mode %r; falling back to 'bundled' "
            "(valid: %s)",
            value,
            ", ".join(INFLUXDB_MODES),
        )
    return INFLUXDB_DEFAULTS["mode"]


def normalize_secret_file(value):
    """Validate the bundled-InfluxDB secret file path.

    The secret file must be a project-local relative path: absolute paths and
    paths that escape the project root via ``..`` are rejected (falling back to
    the default) so generated secrets never land outside the repo. The path is
    returned relative to the project root; callers resolve it against BASE_DIR.
    """
    default = INFLUXDB_DEFAULTS["secret_file"]
    text = str(value or "").strip()
    if not text:
        return default

    if os.path.isabs(text):
        logging.warning(
            "influxdb.secret_file %r is an absolute path; using default %r",
            value,
            default,
        )
        return default

    normalized = os.path.normpath(text)
    if normalized == ".." or normalized.startswith(".." + os.sep):
        logging.warning(
            "influxdb.secret_file %r escapes the project root; using "
            "default %r",
            value,
            default,
        )
        return default

    return normalized


# Bucket/config names are interpolated into Flux query strings and bucket
# paths, so keep them to a conservative character set. Anything outside this
# (spaces, quotes, newlines, Flux fragments, path separators, shell
# metacharacters) is rejected rather than passed through.
INFLUX_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+$")


def is_valid_influx_name(value):
    """True when value is a safe InfluxDB bucket/config name.

    Allows only ``[A-Za-z0-9_.-]`` so spaces, quotes, newlines, Flux syntax
    fragments, path separators and shell metacharacters are rejected. Bare dot
    sequences (``.``/``..``) are also rejected to avoid path-style values.
    """
    text = str(value or "")
    if not INFLUX_NAME_PATTERN.match(text):
        return False
    if set(text) <= {"."}:
        return False
    return True


def normalize_influxdb_config(config):
    """Validate and normalize the optional influxdb config block.

    Config is the source of truth for the history schema, so this drops
    malformed downsampling/query-profile entries rather than passing them on
    to the InfluxDB schema reconciler.
    """
    if not isinstance(config, dict):
        config = {}

    retention_input = config.get("retention")
    if not isinstance(retention_input, dict):
        retention_input = {}

    retention = {}
    for key, default in INFLUXDB_DEFAULTS["retention"].items():
        retention[key] = safe_int(
            retention_input.get(key, default), default, minimum=0
        )

    downsampling = []
    raw_downsampling = config.get("downsampling")
    if not isinstance(raw_downsampling, list):
        raw_downsampling = INFLUXDB_DEFAULTS["downsampling"]

    for entry in raw_downsampling:
        if not isinstance(entry, dict):
            continue
        source = str(entry.get("source", "")).strip()
        target = str(entry.get("target", "")).strip()
        window = str(entry.get("window", "")).strip()
        if not source or not target or not is_influx_duration(window):
            continue
        if not is_valid_influx_name(source) or not is_valid_influx_name(target):
            logging.warning(
                "Dropping influxdb downsampling entry with unsafe bucket "
                "name (allowed: A-Za-z0-9_.-): source=%r target=%r",
                source,
                target,
            )
            continue
        downsampling.append(
            {"source": source, "target": target, "window": window}
        )

    query_profiles = []
    raw_profiles = config.get("query_profiles")
    if not isinstance(raw_profiles, list):
        raw_profiles = INFLUXDB_DEFAULTS["query_profiles"]

    for entry in raw_profiles:
        if not isinstance(entry, dict):
            continue
        max_range = str(entry.get("max_range", "")).strip()
        bucket = str(entry.get("bucket", "")).strip()
        window = str(entry.get("window", "")).strip()
        if not is_influx_duration(max_range) or not bucket:
            continue
        if not is_valid_influx_name(bucket):
            logging.warning(
                "Dropping influxdb query profile with unsafe bucket name "
                "(allowed: A-Za-z0-9_.-): bucket=%r",
                bucket,
            )
            continue
        if not is_influx_duration(window):
            continue
        query_profiles.append(
            {"max_range": max_range, "bucket": bucket, "window": window}
        )

    # Profiles are matched smallest-range-first when selecting a bucket.
    query_profiles.sort(key=lambda profile: influx_duration_seconds(profile["max_range"]))

    return {
        "enabled": safe_bool(config.get("enabled"), False),
        "mode": normalize_influxdb_mode(
            config.get("mode", INFLUXDB_DEFAULTS["mode"])
        ),
        "auto_init": safe_bool(
            config.get("auto_init", INFLUXDB_DEFAULTS["auto_init"]),
            INFLUXDB_DEFAULTS["auto_init"],
        ),
        "auto_sync": safe_bool(
            config.get("auto_sync", INFLUXDB_DEFAULTS["auto_sync"]),
            INFLUXDB_DEFAULTS["auto_sync"],
        ),
        "secret_file": normalize_secret_file(
            config.get("secret_file", INFLUXDB_DEFAULTS["secret_file"])
        ),
        "url": str(config.get("url", INFLUXDB_DEFAULTS["url"])).strip(),
        "host_url": (
            str(config.get("host_url", INFLUXDB_DEFAULTS["host_url"])).strip()
            or INFLUXDB_DEFAULTS["host_url"]
        ),
        "org": str(config.get("org", INFLUXDB_DEFAULTS["org"])).strip(),
        "token": str(config.get("token", "")),
        "token_env": str(
            config.get("token_env", INFLUXDB_DEFAULTS["token_env"])
        ).strip(),
        "bucket_prefix": sanitize_bucket_prefix(
            config.get("bucket_prefix", INFLUXDB_DEFAULTS["bucket_prefix"])
        ),
        "raw_write_interval_seconds": safe_float(
            config.get(
                "raw_write_interval_seconds",
                INFLUXDB_DEFAULTS["raw_write_interval_seconds"],
            ),
            INFLUXDB_DEFAULTS["raw_write_interval_seconds"],
            minimum=0,
        ),
        "retention": retention,
        "downsampling": downsampling,
        "query_profiles": query_profiles,
    }


def influx_duration_seconds(value):
    """Convert a Flux duration (10s/1m/2h/7d/4w) to seconds; 0 on parse failure."""
    text = str(value or "").strip()
    if len(text) < 2:
        return 0

    units = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}
    unit = text[-1]
    if unit not in units:
        return 0

    try:
        amount = float(text[:-1])
    except (TypeError, ValueError):
        return 0

    return int(amount * units[unit])


def resolve_influx_token(influxdb_config, environ=None):
    """Resolve the InfluxDB token: explicit config value wins, else token_env."""
    if environ is None:
        environ = os.environ

    token = str(influxdb_config.get("token", "")).strip()
    if token:
        return token

    env_name = str(influxdb_config.get("token_env", "")).strip()
    if env_name:
        return str(environ.get(env_name, "")).strip()

    return ""


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
