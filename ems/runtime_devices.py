# SPDX-FileCopyrightText: 2026 basecubedev
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Which runtime-state entry a device's config entry owns, and what a new one starts with.

docs/developer/settings-operation-store.md: a device's operating values live
in one place, its entry in ``runtime-state.json``. Every device the EMS can
control has one, switched on or off, from its first start until it is removed
from ``config.json``. The EMS and the Admin join a config entry to its runtime
entry through this module, so they cannot disagree about which device an
entry is.

Import-side-effect-free.
"""

import copy


def clean_identity(value):
    """A non-empty serial as text, or None."""

    if not isinstance(value, str):
        return None
    value = value.strip()
    return value or None


def _text(value):
    return "" if value is None or isinstance(value, (bool, dict, list)) else str(value).strip()


def device_name(entry):
    return str(entry.get("name") or "").strip() if isinstance(entry, dict) else ""


def device_key(entry):
    """The device a config entry stands for: ``("serial", serial)``, or ``("name", name)`` for an entry naming no serial.

    The serial is ``sn``, else ``serial_number``, else ``mqtt.device_id``, as
    text. None for a read-only entry, a nameless one without a serial, and one
    whose ``sn`` and ``serial_number`` name different serials: the EMS reads an
    HTTP device by the one and an MQTT device by the other, so neither is safe
    to follow.
    """

    from ems.read_only_devices import is_read_only_device_config

    if not isinstance(entry, dict) or is_read_only_device_config(entry):
        return None
    serials = [_text(entry.get(key)) for key in ("sn", "serial_number")]
    serials = [serial for serial in serials if serial]
    if len(set(serials)) > 1:
        return None
    mqtt = entry.get("mqtt") if isinstance(entry.get("mqtt"), dict) else {}
    identity = (serials or [_text(mqtt.get("device_id"))])[0]
    if identity:
        return ("serial", identity)
    name = device_name(entry)
    return ("name", name) if name else None


def device_serial(entry):
    """The serial ``device_key`` follows a config entry by, or None."""

    key = device_key(entry)
    return key[1] if key is not None and key[0] == "serial" else None


def runtime_device_for(devices, name, identity):
    """The runtime-state entry a configured device owns: the one stamped with its serial, else the one under its name unless that one carries another serial; None when there is neither."""

    ident = clean_identity(identity)
    if ident is not None:
        for entry_name, state in devices.items():
            if isinstance(state, dict) and clean_identity(state.get("identity")) == ident:
                return entry_name
    state = devices.get(name)
    if isinstance(state, dict) and clean_identity(state.get("identity")) in (None, ident):
        return name
    return None


def owned_runtime_device(devices, name, identity):
    """runtime_device_for for a writer: None also where several entries carry the serial.

    The EMS's own matching cannot tell such devices apart, so a writer that
    followed the first of them could reach the wrong one.
    """

    ident = clean_identity(identity)
    carriers = [
        state for state in devices.values()
        if isinstance(state, dict) and ident is not None and clean_identity(state.get("identity")) == ident
    ]
    return None if len(carriers) > 1 else runtime_device_for(devices, name, identity)


def _controlled(entries):
    from ems.config import http_control_device_configs, mqtt_control_device_configs

    return http_control_device_configs(entries) + mqtt_control_device_configs(entries)


def is_built(entry):
    """Whether the EMS builds ``entry`` to control: switched on, over HTTP or MQTT with output control."""

    return bool(_controlled([entry]))


def is_controllable(entry):
    """Whether the EMS would build ``entry`` to control if it were switched on."""

    return isinstance(entry, dict) and is_built(dict(entry, enabled=True))


def stored_devices(devices):
    """``[(name, serial, entry)]``: the config entries that own a runtime entry, in config order.

    One per device (device_key) the EMS can control, switched on or off: the
    entry it builds, else the first it would build if switched on. A device
    whose name an earlier one already took gets none, since the runtime state
    keys its entries by name; neither does a device with several entries the
    EMS builds, which it cannot tell apart.
    """

    entries = [entry for entry in devices if isinstance(entry, dict)] if isinstance(devices, list) else []
    voices = {}
    for entry in entries:
        key = device_key(entry)
        if key is None or not is_controllable(entry):
            continue
        voices.setdefault(key, []).append(entry)
    chosen = []
    for key, carriers in voices.items():
        built = [entry for entry in carriers if is_built(entry)]
        if len(built) > 1:
            continue
        chosen.append((built or carriers)[0])
    order = {id(entry): index for index, entry in enumerate(entries)}
    chosen.sort(key=lambda entry: (not is_built(entry), order[id(entry)]))
    result, names = [], set()
    for entry in chosen:
        name = device_name(entry)
        if not name or name in names:
            continue
        names.add(name)
        result.append((name, device_serial(entry), entry))
    result.sort(key=lambda item: order[id(item[2])])
    return result


def config_device_keys(devices):
    """The device keys every config entry names, controlled or not.

    A runtime entry one of them owns stays: only a device removed from
    ``config.json`` loses its entry. A device switched off or read over MQTT
    without control keeps its values, and gets them back when it controls
    again.
    """

    entries = [entry for entry in devices if isinstance(entry, dict)] if isinstance(devices, list) else []
    keys = {key for key in (device_key(entry) for entry in entries) if key is not None}
    keys |= {("serial", serial) for entry in entries for serial in serial_candidates(entry)}
    return keys | {("name", device_name(entry)) for entry in entries if device_name(entry)}


def serial_candidates(entry):
    """Every serial a config entry names (``sn``, ``serial_number``, ``mqtt.device_id``), as text; none for a read-only entry.

    An entry whose ``sn`` and ``serial_number`` disagree has no device_key,
    but the EMS stamps one of them on its runtime entry, so neither may be
    taken for a removed device.
    """

    from ems.read_only_devices import is_read_only_device_config

    if not isinstance(entry, dict) or is_read_only_device_config(entry):
        return []
    mqtt = entry.get("mqtt") if isinstance(entry.get("mqtt"), dict) else {}
    found = []
    for value in (entry.get("sn"), entry.get("serial_number"), mqtt.get("device_id")):
        text = _text(value)
        if text and text not in found:
            found.append(text)
    return found


def config_entry_named(devices, name):
    """The config entry of ``devices`` that owns the runtime entry ``name`` (stored_devices), else the first entry so named."""

    for entry_name, _serial, entry in stored_devices(devices):
        if entry_name == name:
            return entry
    entries = [entry for entry in devices if isinstance(entry, dict)] if isinstance(devices, list) else []
    return next((entry for entry in entries if device_name(entry) == name), None)


def parked_values_for(parked, entry):
    """The values v0.10.8 parked for the device of config entry ``entry``, popped from ``parked``, with the serial they were parked under.

    ``(values, serial)``; ``({}, None)`` where none were.
    """

    for serial in serial_candidates(entry):
        key = f"identity:{serial}"
        if isinstance(parked.get(key), dict):
            return parked.pop(key), serial
    key = f"name:{device_name(entry)}"
    if device_name(entry) and isinstance(parked.get(key), dict):
        return parked.pop(key), None
    return {}, None


def names_a_runtime_entry(keys, name, state):
    """Whether the runtime entry ``name`` belongs to a device the config's ``keys`` (config_device_keys) still name.

    By the rule devices own entries with: its serial where it carries one,
    else its name.
    """

    identity = clean_identity(state.get("identity")) if isinstance(state, dict) else None
    return ("serial", identity) in keys if identity is not None else ("name", name) in keys


def new_runtime_entry(entry, max_device_power):
    """The runtime entry a device's first start leaves for config entry ``entry``, once its operating values moved.

    device_seed, with every operating value the entry holds in a form the EMS
    reads (ems.config.held_operating_device_values) over it: the Admin writes
    this for a device a save adds, so those values never live in
    ``config.json``.
    """

    from ems import config as cfg

    seed = device_seed(entry, max_device_power)
    seed.update(cfg.held_operating_device_values(entry))
    return seed


def device_seed(entry, max_device_power):
    """The values a new runtime entry starts with for the config entry ``entry``.

    As the EMS reads them: a ``max_power`` that is no positive number is the
    installation's fallback ``max_device_power``, the off-grid socket mode
    comes from the legacy ``grid_off_mode``, and an operating value the entry
    still holds (written by hand or by an older release) seeds its own key.
    """

    from ems import config as cfg

    serial = device_serial(entry)
    seed = {
        "enabled": True,
        "max_power": cfg.safe_int(
            cfg.device_power_ceiling(entry.get("max_power", max_device_power), max_device_power),
            max_device_power,
            minimum=0,
        ),
        "offgrid_socket_mode": cfg.offgrid_socket_mode_for(entry.get("grid_off_mode")),
        "pv_priority_factor": cfg.safe_float(entry.get("pv_priority_factor", 1.0) or 1.0, 1.0, minimum=0.01),
        "ac_charge_enabled": bool(entry.get("ac_charge_enabled", True)),
        "ac_discharge_enabled": bool(entry.get("ac_discharge_enabled", True)),
    }
    if serial is not None:
        seed["identity"] = serial
    return copy.deepcopy(seed)
