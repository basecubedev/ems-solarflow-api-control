import json
import logging
import os
import copy
import threading

from ems import config as cfg
from ems.logging_utils import log_event


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
        self.lock = threading.RLock()

    def load_or_create(self):
        with self.lock:
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
        with self.lock:
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
        with self.lock:
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

    def snapshot(self):
        with self.lock:
            return copy.deepcopy(self.data)

    def update_section(self, section_name, values):
        with self.lock:
            section = self.data.setdefault(section_name, {})
            if not isinstance(section, dict):
                raise ValueError(f"runtime section {section_name} must be an object")
            section.update(values)
            result = copy.deepcopy(section)
            self.save_atomic()
            return result

    def update_device(self, device_name, values):
        with self.lock:
            devices = self.data.setdefault("devices", {})
            if device_name not in devices:
                known = ", ".join(sorted(devices)) or "(none)"
                raise KeyError(f"unknown device {device_name}; known devices: {known}")
            device = devices[device_name]
            if not isinstance(device, dict):
                raise ValueError(f"device {device_name} runtime state must be an object")
            device.update(values)
            result = copy.deepcopy(device)
            self.save_atomic()
            return result

    def get_system(self, key, default=None):
        with self.lock:
            system = self.data.get("system", {})
            if not isinstance(system, dict):
                return default

            return system.get(key, default)

    def get_section(self, section_name, key, default=None):
        with self.lock:
            section = self.data.get(section_name, {})
            if not isinstance(section, dict):
                return default

            return section.get(key, default)

    def set_system(self, key, value):
        with self.lock:
            system = self.data.setdefault("system", {})
            previous = system.get(key)
            system[key] = value
            return previous != value

    def set_section(self, section_name, key, value):
        with self.lock:
            section = self.data.setdefault(section_name, {})
            previous = section.get(key)
            section[key] = value
            return previous != value

    def get_device(self, device_name, key, default=None):
        with self.lock:
            devices = self.data.get("devices", {})
            device = devices.get(device_name, {})

            if not isinstance(device, dict):
                return default

            return device.get(key, default)

    def set_device(self, device_name, key, value):
        with self.lock:
            devices = self.data.setdefault("devices", {})
            device = devices.setdefault(device_name, {})
            previous = device.get(key)
            device[key] = value
            return previous != value


def build_runtime_defaults(devices):
    """Build runtime defaults from current device configuration."""

    device_defaults = {}
    ha_config = cfg.CONFIG.get("ha", {})

    for dev in devices:
        device_defaults[dev.name] = {
            "enabled": True,
            "max_power": cfg.safe_int(
                getattr(dev, "max_power", cfg.MAX_DEVICE_POWER),
                cfg.MAX_DEVICE_POWER,
                minimum=0
            ),
            "offgrid_socket_mode": "off",
            "pv_priority_factor": cfg.safe_float(
                getattr(dev, "pv_priority_factor", 1.0),
                1.0,
                minimum=0.01
            )
        }

    return {
        "system": {
            "enabled": cfg.SYSTEM_ENABLED,
            "max_total_power": cfg.MAX_TOTAL_POWER,
            "loop_interval": cfg.LOOP_INTERVAL,
            "min_output_limit": cfg.MIN_OUTPUT_LIMIT
        },
        "ha": {
            "enabled": ha_config.get("enabled", False),
            "control_enabled": ha_config.get("control_enabled", False)
        },
        "winter": {
            "enabled": cfg.winter_config_bool("enabled", False)
        },
        "devices": device_defaults
    }
