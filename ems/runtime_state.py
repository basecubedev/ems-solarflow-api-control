# SPDX-License-Identifier: AGPL-3.0-or-later
import json
import logging
import os
import copy
import threading

from ems import config as cfg
from ems.logging_utils import log_event


def _clean_identity(value):
    """Return a non-empty stable identity string, or None."""

    if not isinstance(value, str):
        return None
    value = value.strip()
    return value or None


def _sanitized_device(state):
    """Copy a loaded device entry, dropping the legacy offgrid_socket key."""

    if not isinstance(state, dict):
        return state
    entry = dict(state)
    entry.pop("offgrid_socket", None)
    return entry


def reconcile_runtime_devices(loaded_devices, default_devices, *, prune):
    """Reconcile loaded runtime device state against the configured devices.

    Devices are matched to their config entry by stable identity (serial),
    falling back to the device name for entries written before an identity was
    stamped. A matched entry keeps its operator settings under the (possibly
    new) config name, so renaming a device in config never loses its runtime
    settings. Unmatched loaded entries are orphans: dropped when ``prune`` is
    true (a device removed from config), kept otherwise.

    Returns ``(devices, changes)`` where ``changes`` carries ``added``,
    ``renamed``, ``pruned`` and ``rekeyed`` lists for auditing. When no
    configured devices are known (empty/unreadable config) reconciliation is a
    fail-closed no-op: the loaded devices are kept verbatim and never pruned.
    """

    if not isinstance(loaded_devices, dict):
        loaded_devices = {}

    changes = {"added": [], "renamed": [], "pruned": [], "rekeyed": []}

    if not default_devices:
        return {name: _sanitized_device(state) for name, state in loaded_devices.items()}, changes

    loaded_by_identity = {}
    for name, state in loaded_devices.items():
        if isinstance(state, dict):
            ident = _clean_identity(state.get("identity"))
            if ident is not None:
                loaded_by_identity.setdefault(ident, name)

    merged = {}
    consumed = set()

    for cfg_name, device_defaults in default_devices.items():
        ident = None
        if isinstance(device_defaults, dict):
            ident = _clean_identity(device_defaults.get("identity"))

        match_name = None
        if ident is not None and ident in loaded_by_identity:
            match_name = loaded_by_identity[ident]
        elif isinstance(loaded_devices.get(cfg_name), dict):
            candidate_ident = _clean_identity(loaded_devices[cfg_name].get("identity"))
            if candidate_ident is None or candidate_ident == ident:
                match_name = cfg_name

        if match_name is not None and match_name not in consumed:
            operator_state = dict(loaded_devices[match_name])
            prior_identity = _clean_identity(operator_state.pop("identity", None))
            entry = {**device_defaults, **operator_state}
            if ident is not None:
                entry["identity"] = ident
            else:
                entry.pop("identity", None)
            entry.pop("offgrid_socket", None)
            merged[cfg_name] = entry
            consumed.add(match_name)
            if match_name != cfg_name:
                changes["renamed"].append(
                    {"from": match_name, "to": cfg_name, "identity": ident}
                )
            elif ident is not None and prior_identity != ident:
                changes["rekeyed"].append({"name": cfg_name, "identity": ident})
        else:
            entry = dict(device_defaults) if isinstance(device_defaults, dict) else {}
            entry.pop("offgrid_socket", None)
            merged[cfg_name] = entry
            changes["added"].append({"name": cfg_name, "identity": ident})

    for name, state in loaded_devices.items():
        if name in consumed:
            continue
        if prune:
            ident = _clean_identity(state.get("identity")) if isinstance(state, dict) else None
            changes["pruned"].append({"name": name, "identity": ident})
        elif name not in merged:
            merged[name] = _sanitized_device(state)

    return merged, changes


def merge_runtime_defaults(data, defaults):
    """Merge runtime data over defaults while preserving unknown keys.

    Non-destructive: orphaned device entries (present on disk but not in the
    configured devices) are kept. Authoritative pruning of removed devices
    happens only in :meth:`RuntimeState.load_or_create`.
    """

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

    merged_devices, _changes = reconcile_runtime_devices(
        devices, defaults.get("devices", {}), prune=False
    )
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

            return self._reconcile_on_load()

    def _reconcile_on_load(self):
        """Load the file and reconcile device entries against config (EMS-owned).

        The EMS is authoritative for runtime device state: on load it migrates
        renamed devices by identity, prunes devices removed from config, and
        backfills identities. Destructive changes (rename/prune) first back up
        the file and are audit-logged. Any device-level change is persisted so
        the file converges to a clean state.
        """

        try:
            mtime = os.path.getmtime(self.path)
        except FileNotFoundError:
            return self.load_or_create()

        try:
            with open(self.path) as f:
                loaded = json.load(f)
        except Exception as e:
            log_event(
                logging.WARNING,
                "runtime_state_load_error",
                path=self.path,
                error=e
            )
            return self.data

        merged = merge_runtime_defaults(loaded, self.defaults)
        loaded_devices = loaded.get("devices") if isinstance(loaded, dict) else {}
        reconciled_devices, changes = reconcile_runtime_devices(
            loaded_devices if isinstance(loaded_devices, dict) else {},
            self.defaults.get("devices", {}),
            prune=True,
        )
        merged["devices"] = reconciled_devices

        destructive = bool(changes["renamed"] or changes["pruned"])
        changed = destructive or bool(changes["added"] or changes["rekeyed"])

        if destructive:
            self._backup_runtime_state()

        for entry in changes["renamed"]:
            log_event(
                logging.INFO,
                "runtime_device_renamed",
                path=self.path,
                device_from=entry["from"],
                device_to=entry["to"],
                identity=entry.get("identity"),
            )
        for entry in changes["pruned"]:
            log_event(
                logging.INFO,
                "runtime_device_pruned",
                path=self.path,
                device=entry["name"],
                identity=entry.get("identity"),
            )
        for entry in changes["added"]:
            log_event(
                logging.INFO,
                "runtime_device_added",
                path=self.path,
                device=entry["name"],
                identity=entry.get("identity"),
            )

        self.data = merged
        self.last_mtime = mtime

        log_event(
            logging.INFO,
            "runtime_state_loaded",
            path=self.path
        )

        if changed:
            self.save_atomic()

        return self.data

    def _backup_runtime_state(self):
        """Best-effort one-step backup of the current file before pruning."""

        try:
            import shutil

            shutil.copy2(self.path, f"{self.path}.bak")
            log_event(
                logging.INFO,
                "runtime_state_backup_created",
                path=f"{self.path}.bak"
            )
        except OSError as e:
            log_event(
                logging.WARNING,
                "runtime_state_backup_failed",
                path=self.path,
                error=e
            )

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
        entry = {
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
        identity = _clean_identity(str(getattr(dev, "sn", "") or ""))
        if identity is not None:
            entry["identity"] = identity
        device_defaults[dev.name] = entry

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
