# SPDX-License-Identifier: AGPL-3.0-or-later
"""Preview-only EMS configuration generation for the Admin setup wizard."""

import copy
import ipaddress
import json
import re
from pathlib import Path

from admin.install_context import detect_install_context
from admin.releases import ReleaseError
from admin.setup_config import apply_device_config_values, apply_setup_features
from ems.influx_setup import DOCKER_FIRST_SECRET_FILE


_HOST_LABEL = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")
_GRID_TYPES = {
    "shelly_gen2": "shelly",
    "shelly_3em_gen1": "shelly_3em_gen1",
    "ecotracker": "ecotracker",
    "zendure_smartmeter_3ct_http": "zendure_smartmeter_3ct_http",
    "tasmota_http": "tasmota_http",
}
# Explicit meter types a manual (non-discovered) grid meter may declare. A manual
# entry has no api_family/device_type, so its type must be chosen, not inferred.
_GRID_TYPE_CHOICES = {
    "shelly",
    "shelly_3em_gen1",
    "ecotracker",
    "zendure_smartmeter_3ct_http",
    "tasmota_http",
    "zendure_smartmeter_d0",
    "mqtt",
    "ha",
}


def _issue(code, message):
    return {"code": code, "message": message}


def _valid_host(value):
    value = str(value or "").strip()
    if not value or len(value) > 253:
        return False
    try:
        return ipaddress.ip_address(value).version == 4
    except ValueError:
        pass
    hostname = value[:-1] if value.endswith(".") else value
    return bool(hostname) and all(_HOST_LABEL.fullmatch(label) for label in hostname.split("."))


def _grid_type(item, fallback):
    # An explicit meter type (chosen manually) always wins over inference so the
    # system never has to guess a manual meter's hardware from IP/port.
    for key in ("grid_meter_type", "meter_type"):
        explicit = str(item.get(key) or "").strip().lower()
        if explicit in _GRID_TYPE_CHOICES:
            return explicit
    family = str(item.get("api_family") or "").lower()
    device_type = str(item.get("device_type") or "").lower()
    if family in _GRID_TYPES:
        return _GRID_TYPES[family]
    description = f"{family} {device_type}"
    if "ecotracker" in description:
        return "ecotracker"
    if "3ct" in description:
        return "zendure_smartmeter_3ct_http"
    if "3em" in description and "gen1" in description:
        return "shelly_3em_gen1"
    if "tasmota" in description:
        return "tasmota_http"
    return fallback or "shelly"


def _apply_typed_fields(device, item):
    for source, candidates in (
        ("device_type", ("device_type", "type")),
        ("api_family", ("api_family", "api_type")),
    ):
        value = item.get(source)
        if value:
            for key in candidates:
                if key in device:
                    device[key] = value
                    break


def _build_grid_meter(meter, defaults, validation):
    display_name = str(meter.get("display_name") or "").strip()
    host = str(meter.get("ip") or "").strip()
    if not display_name:
        validation["errors"].append(
            _issue("display_name_empty", "The grid meter needs a display name.")
        )
    if not _valid_host(host):
        validation["errors"].append(
            _issue("grid_meter_host_invalid", "The grid meter has an invalid IP address or hostname.")
        )
    grid = copy.deepcopy(defaults) if isinstance(defaults, dict) else {}
    grid["type"] = _grid_type(meter, grid.get("type"))
    grid["ip"] = host
    if "port" in grid and meter.get("port"):
        grid["port"] = meter["port"]
    return grid


def _normalize_bundled_influx_secret(config):
    """Point bundled InfluxDB secrets at the mounted ``config/`` volume.

    Admin deployments are always Docker-first, where only ``/app/config`` and
    ``/app/data`` are writable. The template default (``deploy/docker/...``)
    lives in the read-only image, so bundled ``influx init`` cannot write there.
    External InfluxDB keeps its own secret path.
    """

    influx = config.get("influxdb")
    if not isinstance(influx, dict):
        return
    if influx.get("enabled") is True and influx.get("mode") == "bundled":
        influx["secret_file"] = DOCKER_FIRST_SECRET_FILE


def _load_existing_config(path):
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        return None, _issue(
            "existing_config_unreadable", f"Could not read the existing EMS config at {path}."
        )
    try:
        data = json.loads(text)
    except ValueError:
        return None, _issue(
            "existing_config_invalid_json", f"The existing EMS config at {path} is not valid JSON."
        )
    if not isinstance(data, dict):
        return None, _issue(
            "existing_config_not_object", f"The existing EMS config at {path} is not a JSON object."
        )
    return data, None


class ConfigPreviewGenerator:
    """Generate a config preview, using a real EMS config as base when one exists."""

    def __init__(self, release_manager, install_context_provider=detect_install_context):
        self.release_manager = release_manager
        self.install_context_provider = install_context_provider

    def _install_context(self):
        # A failing install probe must not break the release-template preview path.
        try:
            return self.install_context_provider()
        except Exception:
            return None

    def _resolve_base(self, template, validation):
        context = self._install_context()
        if context is not None and context.config_exists:
            base_meta = {
                "source": "existing_config",
                "config_path": str(context.config_path),
                "config_source": context.config_source,
                "template_path": str(context.template_path),
                "template_source": context.template_source,
            }
            base_config, error = _load_existing_config(context.config_path)
            if error is not None:
                validation["errors"].append(error)
                return base_meta, None
            return base_meta, base_config
        return {"source": "release_template"}, template

    def generate(self, draft=None, supported_grid_meter_count=None, features=None):
        validation = {"errors": [], "warnings": [], "info": []}
        try:
            resource = self.release_manager.config_template()
        except ReleaseError:
            validation["errors"].append(
                _issue(
                    "release_resources_not_prepared",
                    "Prepare release resources before generating a config preview.",
                )
            )
            return {
                "ready": False,
                "release": None,
                "template_loaded": False,
                "config": None,
                "base": {"source": "release_template"},
                "validation": validation,
            }

        template = resource.get("template")
        tag = resource.get("tag")
        if not isinstance(template, dict):
            validation["errors"].append(
                _issue("template_invalid", "config.template.json is not a JSON object.")
            )
            return {
                "ready": False,
                "release": tag,
                "template_loaded": False,
                "config": None,
                "base": {"source": "release_template"},
                "validation": validation,
            }

        base_meta, base_config = self._resolve_base(template, validation)
        if base_config is None:
            return {
                "ready": False,
                "release": tag,
                "template_loaded": True,
                "config": None,
                "base": base_meta,
                "validation": validation,
            }
        existing_base = base_meta["source"] == "existing_config"
        preview = copy.deepcopy(base_config)

        items = draft if isinstance(draft, list) else []
        enabled = [item for item in items if isinstance(item, dict) and item.get("enabled", True)]
        inverters = [item for item in enabled if item.get("role") == "inverter"]
        meters = [item for item in enabled if item.get("role") == "grid_meter"]

        prototypes = template.get("devices")
        if not isinstance(prototypes, list) or not all(
            isinstance(item, dict) for item in prototypes
        ):
            validation["errors"].append(
                _issue("template_devices_invalid", "The release template has no usable devices list.")
            )
            prototypes = []
        prototype = prototypes[0] if prototypes else {}

        names = []
        if existing_base:
            self._merge_existing_devices(preview, inverters, prototype, names, validation)
        else:
            self._build_template_devices(preview, inverters, prototypes, prototype, names, validation)

        if not inverters:
            validation["warnings"].append(
                _issue("inverter_missing", "No active Zendure inverter is selected.")
            )

        self._apply_grid_meter(
            preview, meters, template, names, validation, supported_grid_meter_count, existing_base
        )

        # Catalog-driven feature values are applied last so setup choices (winter,
        # dashboard, InfluxDB, grid meter variant, ...) override template/base
        # defaults while device entries stay owned by the draft above.
        applied_features = apply_setup_features(preview, features)
        if applied_features:
            validation["info"].append(
                _issue(
                    "setup_features_applied",
                    f"Applied {len(applied_features)} setup feature value(s).",
                )
            )

        _normalize_bundled_influx_secret(preview)

        duplicate_names = sorted({name for name in names if name and names.count(name) > 1})
        if any(not name for name in names):
            validation["errors"].append(
                _issue("config_name_empty", "Every selected device needs a config name.")
            )
        if duplicate_names:
            validation["errors"].append(
                _issue(
                    "config_name_duplicate",
                    f"Config names must be unique: {', '.join(duplicate_names)}.",
                )
            )

        try:
            json.dumps(preview, allow_nan=False)
        except (TypeError, ValueError):
            validation["errors"].append(
                _issue("config_not_serializable", "The generated config is not valid JSON data.")
            )

        validation["info"].append(
            _issue("template_loaded", f"Release template {tag} loaded.")
        )
        if existing_base:
            validation["info"].append(
                _issue("existing_config_base", "Existing EMS config used as preview base.")
            )
        if not duplicate_names and names and all(names):
            validation["info"].append(
                _issue("config_names_unique", "Device config names are unique.")
            )
        return {
            "ready": not validation["errors"],
            "release": tag,
            "template_loaded": True,
            "config": preview,
            "base": base_meta,
            "summary": {
                "inverters": len(inverters),
                "grid_meters": len(meters),
            },
            "validation": validation,
        }

    def _build_template_devices(self, preview, inverters, prototypes, prototype, names, validation):
        generated_devices = []
        for index, item in enumerate(inverters, 1):
            raw_name = str(item.get("config_name") or "").strip()
            name = raw_name or f"inverter_{index}"
            label = name or f"inverter_{index}"
            display_name = str(item.get("display_name") or "").strip()
            host = str(item.get("ip") or "").strip()
            serial = str(item.get("serial_number") or "").strip()
            names.append(raw_name)
            if not display_name:
                validation["errors"].append(_issue("display_name_empty", f"{label} needs a display name."))
            if not _valid_host(host):
                validation["errors"].append(
                    _issue("device_host_invalid", f"{label} has an invalid IP address or hostname.")
                )
            device = copy.deepcopy(prototypes[index - 1] if index <= len(prototypes) else prototype)
            if "name" in prototype or device:
                device["name"] = name
            if "ip" in prototype or device:
                device["ip"] = host
            if "sn" in device:
                device["sn"] = serial
                if not serial:
                    validation["errors"].append(
                        _issue("device_serial_missing", f"{label} requires a serial number.")
                    )
            _apply_typed_fields(device, item)
            apply_device_config_values(device, item.get("config_values"))
            generated_devices.append(device)
        preview["devices"] = generated_devices

    def _merge_existing_devices(self, preview, inverters, prototype, names, validation):
        devices = preview.get("devices")
        if not isinstance(devices, list):
            devices = []
            preview["devices"] = devices
        by_name = {}
        for device in devices:
            if isinstance(device, dict):
                key = str(device.get("name") or "")
                if key and key not in by_name:
                    by_name[key] = device

        for index, item in enumerate(inverters, 1):
            raw_name = str(item.get("config_name") or "").strip()
            name = raw_name or f"inverter_{index}"
            label = name or f"inverter_{index}"
            display_name = str(item.get("display_name") or "").strip()
            host = str(item.get("ip") or "").strip()
            serial = str(item.get("serial_number") or "").strip()
            names.append(raw_name)
            if not display_name:
                validation["errors"].append(_issue("display_name_empty", f"{label} needs a display name."))
            if not _valid_host(host):
                validation["errors"].append(
                    _issue("device_host_invalid", f"{label} has an invalid IP address or hostname.")
                )

            match = by_name.get(name)
            if match is not None:
                match["ip"] = host
                if serial:
                    match["sn"] = serial
                _apply_typed_fields(match, item)
                apply_device_config_values(match, item.get("config_values"))
                if "sn" in match and not str(match.get("sn") or "").strip():
                    validation["errors"].append(
                        _issue("device_serial_missing", f"{label} requires a serial number.")
                    )
                continue

            device = copy.deepcopy(prototype)
            device["name"] = name
            device["ip"] = host
            if "sn" in device:
                device["sn"] = serial
                if not serial:
                    validation["errors"].append(
                        _issue("device_serial_missing", f"{label} requires a serial number.")
                    )
            _apply_typed_fields(device, item)
            apply_device_config_values(device, item.get("config_values"))
            devices.append(device)

    def _apply_grid_meter(
        self, preview, meters, template, names, validation, supported_grid_meter_count, existing_base
    ):
        if len(meters) > 1:
            validation["errors"].append(
                _issue("grid_meter_duplicate", "Choose only one grid meter for EMS control.")
            )
            if not existing_base:
                preview.pop("grid_meter", None)
            return

        if len(meters) == 1:
            meter = meters[0]
            names.append(str(meter.get("config_name") or "").strip())
            defaults = preview.get("grid_meter") if existing_base else template.get("grid_meter", {})
            if not isinstance(defaults, dict):
                defaults = template.get("grid_meter", {})
            preview["grid_meter"] = _build_grid_meter(meter, defaults, validation)
            return

        if existing_base and isinstance(preview.get("grid_meter"), dict):
            return

        preview.pop("grid_meter", None)
        if isinstance(supported_grid_meter_count, int) and supported_grid_meter_count >= 2:
            validation["errors"].append(
                _issue(
                    "grid_meter_ambiguous",
                    "Multiple supported grid meters were found. Choose one grid meter for EMS control.",
                )
            )
        elif "grid_meter" in template:
            validation["errors"].append(
                _issue("grid_meter_missing", "Choose a grid meter for EMS control.")
            )
        else:
            validation["warnings"].append(
                _issue("grid_meter_missing", "No grid meter is selected.")
            )
