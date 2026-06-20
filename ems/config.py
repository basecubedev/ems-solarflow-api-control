# SPDX-License-Identifier: AGPL-3.0-or-later
import copy
import json
import logging
import os
import re
import sys
import stat
from datetime import datetime
from urllib.parse import urlparse

LATEST_CONFIG_SCHEMA_VERSION = 3
CURRENT_CONFIG_SCHEMA_VERSION = LATEST_CONFIG_SCHEMA_VERSION

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
    "enabled": False,
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
    "enabled": False,
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
        if all(_is_json_scalar(item) for item in value):
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
        "config_upgrade": copy.deepcopy(CONFIG_UPGRADE_DEFAULTS),
        "influxdb": copy.deepcopy(INFLUXDB_DEFAULTS),
        "devices": [],
        "grid_meter": {
            "type": "shelly",
            "ip": ""
        },
        "shelly": {
            "ip": ""
        }
    }


def default_runtime_config():
    config = default_safe_config()
    config["system"]["simulation_mode"] = False
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
    path = os.path.join(base_dir, "config.template.json")
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
        if path and _is_comment_key(path[-1]) and original is MISSING:
            return [path]
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


def is_template_placeholder_value(value):
    if value is None:
        return False
    text = str(value).strip()
    if not text:
        return False

    lowered = text.lower()
    if lowered in TEMPLATE_PLACEHOLDER_VALUES:
        return True
    if lowered.startswith("your_") or lowered.startswith("your-"):
        return True
    if "example.com" in lowered:
        return True

    parsed = urlparse(text if "://" in text else f"//{text}")
    host = (parsed.hostname or "").lower()
    return host in TEMPLATE_PLACEHOLDER_VALUES


def _missing_or_placeholder(value):
    return (
        value is None
        or str(value).strip() == ""
        or is_template_placeholder_value(value)
    )


def template_placeholder_paths(config):
    """Return required setup fields that still contain template-like values."""

    if not isinstance(config, dict):
        return []

    paths = []
    devices = config.get("devices")
    configured_devices = []
    if isinstance(devices, list):
        configured_devices = [
            device for device in devices
            if isinstance(device, dict)
        ]
        for index, device in enumerate(configured_devices):
            if _missing_or_placeholder(device.get("ip")):
                paths.append(f"devices[{index}].ip")
            if _missing_or_placeholder(device.get("sn")):
                paths.append(f"devices[{index}].sn")

    grid_meter = config.get("grid_meter")
    if isinstance(grid_meter, dict) and configured_devices:
        meter_type = str(grid_meter.get("type") or "shelly").strip().lower()
        if meter_type == "tasmota_http":
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

    return {
        "template_path": template_path,
        "template_layout": layout,
        "upgraded_config": upgraded,
        "add": added,
        "comment_add": comments,
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
INFLUXDB_CONFIG = None
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
    global ALLOW_STATE_RECONCILIATION_WRITES, RECONCILE_AC_MODE_ON_START
    global RECONCILE_SMART_MODE, HA_ENABLED, HA_CONTROL_ENABLED, LOG_LEVEL
    global REDISTRIBUTE_CLAMPED_POWER, PV_KWP_WEIGHTING
    global PV_CHARGE_BALANCE_ENABLED, PV_CHARGE_BALANCE_DEADBAND_PERCENT
    global PV_CHARGE_BALANCE_FULL_BIAS_PERCENT, PV_CHARGE_BALANCE_STRENGTH
    global BATTERY_KWH_WEIGHTING
    global SOC_RECONCILE_INTERVAL, WINTER_CONFIG, DASHBOARD_CONFIG
    global INFLUXDB_CONFIG
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


def config_path():
    """Return the path to the static config file the EMS was started with."""

    path = (ARGS.config if ARGS else None) or os.path.join(
        BASE_DIR or os.getcwd(), "config.json"
    )
    return path


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
