# SPDX-License-Identifier: AGPL-3.0-or-later
"""Settings of the inverter itself that the operator can hand to the EMS.

Light, fan, buzzer and battery heating. A setting's value is a key of the
device's runtime-state entry: absent or ``null`` leaves the setting to the
device and the Zendure app, and nothing is written. A value the operator set
is kept on the device by :class:`DeviceSettingsReconciler` under the rules of
the state reconciliation: over the local API only, behind
``allow_state_reconciliation_writes``, nothing while the EMS or the device is
switched off, and only for a setting the device's catalogue model lists and
its report carries.

Zendure-HA writes these properties over the ZenSDK local API for any model
that reports them; ``lampSwitch``, ``Fanmode`` and ``Fanspeed`` were written
on a SolarFlow 800 Pro 2 on 2026-10-17, ``buzzerSwitch`` and ``autoHeat`` on
no hardware yet. Import-side-effect-free.
"""

import logging
import time
from dataclasses import dataclass

from ems.logging_utils import log_event
from ems.mqtt_control.zendure_profiles import hardware_profile_by_name

UNMANAGED = "unmanaged"
RESEND_SECONDS = 30.0
MAX_ATTEMPTS = 3
HOLD_SECONDS = 3600.0


@dataclass(frozen=True)
class SettingValue:
    """One value of a setting and the property values that make it."""

    word: str
    label: str
    properties: tuple


@dataclass(frozen=True)
class DeviceSetting:
    """A setting: its runtime key, its emsctl command and its values."""

    key: str
    command: str
    label: str
    help: str
    values: tuple

    @property
    def words(self):
        return tuple(value.word for value in self.values)

    @property
    def properties(self):
        names = []
        for value in self.values:
            names.extend(name for name, _ in value.properties if name not in names)
        return tuple(names)

    def value(self, word):
        return next((value for value in self.values if value.word == word), None)


def _switch(key, command, label, help, prop):
    return DeviceSetting(key, command, label, help, (
        SettingValue("on", "On", ((prop, 1),)),
        SettingValue("off", "Off", ((prop, 0),)),
    ))


SETTINGS = (
    _switch("light", "light", "Light", "The inverter's LED light.", "lampSwitch"),
    DeviceSetting(
        "fan",
        "fan",
        "Fan",
        "Auto lets the inverter run its fan by its temperature; off keeps the fan off.",
        (
            SettingValue("auto", "Auto", (("Fanmode", 1), ("Fanspeed", 0))),
            SettingValue("1", "Speed 1", (("Fanmode", 1), ("Fanspeed", 1))),
            SettingValue("2", "Speed 2", (("Fanmode", 1), ("Fanspeed", 2))),
            SettingValue("off", "Off", (("Fanmode", 0),)),
        ),
    ),
    _switch("buzzer", "buzzer", "Buzzer", "The inverter's beeper.", "buzzerSwitch"),
    _switch(
        "auto_heat",
        "auto-heat",
        "Battery heating",
        "Whether the battery heats itself when it is cold.",
        "autoHeat",
    ),
)
SETTINGS_BY_KEY = {setting.key: setting for setting in SETTINGS}
SETTING_PROPERTIES = frozenset(name for setting in SETTINGS for name in setting.properties)


def catalogue():
    """The settings as the dashboard lists them: key, label, help and values."""

    return [
        {
            "key": setting.key,
            "label": setting.label,
            "help": setting.help,
            "values": [{"value": value.word, "label": value.label} for value in setting.values],
        }
        for setting in SETTINGS
    ]


def reported_properties(properties):
    """The setting properties a report carries as whole numbers."""

    if properties is None:
        return {}
    reported = {}
    for name in SETTING_PROPERTIES:
        value = properties.get(name)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        if float(value).is_integer():
            reported[name] = int(value)
    return reported


def reported_word(setting, reported):
    """The value the report shows ``setting`` at, or None when it shows none of them."""

    for value in setting.values:
        if all(reported.get(name) == wanted for name, wanted in value.properties):
            return value.word
    return None


def changes_for(setting, word, reported):
    """The properties that differ from ``word`` in the report; None when the report lacks one."""

    value = setting.value(word)
    if value is None or any(name not in reported for name, _ in value.properties):
        return None
    return {name: wanted for name, wanted in value.properties if reported[name] != wanted}


def normalized_word(setting, stored):
    """``(word, valid)`` for a stored value; the word is None for an unmanaged setting."""

    if stored is None:
        return None, True
    if isinstance(stored, float) and stored.is_integer():
        stored = int(stored)
    word = str(stored).strip().lower()
    if word == UNMANAGED:
        return None, True
    return (word, True) if word in setting.words else (None, False)


def stored_settings(runtime_state, device_name):
    """The setting values in a device's runtime entry, by key, as stored."""

    if runtime_state is None:
        return {}
    stored = {}
    for setting in SETTINGS:
        value = runtime_state.get_device(device_name, setting.key, None)
        if value is not None:
            stored[setting.key] = value
    return stored


def desired_words(stored):
    """The valid values the operator set, by key; unmanaged and invalid ones are left out."""

    desired = {}
    for key, value in stored.items():
        setting = SETTINGS_BY_KEY.get(key)
        word = normalized_word(setting, value)[0] if setting is not None else None
        if word is not None:
            desired[key] = word
    return desired


def model_settings(dev):
    """The setting keys the catalogue lists for ``dev``'s identified model."""

    resolve = getattr(dev, "resolved_hardware_profile", None)
    profile_id = resolve() if callable(resolve) else None
    profile = hardware_profile_by_name(profile_id) if profile_id else None
    return tuple(profile.device_settings) if profile is not None else ()


def offered_settings(dev, reported):
    """The settings ``dev`` has: its model lists them, its report carries them, its state is written."""

    if not getattr(dev, "supports_state_reconciliation", True):
        return ()
    listed = set(model_settings(dev))
    return tuple(
        setting
        for setting in SETTINGS
        if setting.key in listed and all(name in reported for name in setting.properties)
    )


def offered_by_device(devices):
    """The setting keys each device has, from the report its client read last."""

    return {
        dev.name: [
            setting.key
            for setting in offered_settings(dev, getattr(dev, "observed_settings", None) or {})
        ]
        for dev in devices
    }


def settings_view(dev, reported, desired):
    """Each setting ``dev`` has or the operator set: the reported and the set value."""

    offered = {setting.key for setting in offered_settings(dev, reported)}
    view = {}
    for setting in SETTINGS:
        wanted = desired.get(setting.key)
        if setting.key not in offered and wanted is None:
            continue
        view[setting.key] = {
            "offered": setting.key in offered,
            "reported": reported_word(setting, reported) if setting.key in offered else None,
            "desired": wanted,
        }
    return view


@dataclass
class _Sent:
    word: str
    attempts: int
    last: float
    warned: bool = False


class DeviceSettingsReconciler:
    """Keeps each device's settings at the values the operator set.

    Each setting that differs goes out as its own write, so one the device
    refuses holds back no other; a write that raises ends the device's cycle,
    so a device that does not answer costs one timeout. A value the device has
    not taken goes out again after ``RESEND_SECONDS``; once ``MAX_ATTEMPTS``
    sends have gone that long unshown, it is said once and held for
    ``HOLD_SECONDS`` before the next round, unless its value changes, it is
    handed back, or the device shows it.
    """

    def __init__(self, *, clock=time.monotonic):
        self._clock = clock
        self._sent = {}
        self._noted = set()
        self._held = {}

    def forget(self, name):
        """Drop what is kept for a device none of whose settings is set any more."""

        for key in [key for key in self._sent if key[0] == name]:
            del self._sent[key]
        for key in [key for key in self._held if key[0] == name]:
            del self._held[key]

    def reconcile(self, dev, reported, stored, *, allowed, gate_fields, write):
        """Write each setting that differs; return how many writes the device answered.

        ``write(properties, fields)`` sends one, ``gate_fields()`` says why none may go.
        """

        name = dev.name
        offered = {setting.key for setting in offered_settings(dev, reported)}
        listed = set(model_settings(dev))
        now = self._clock()
        written = 0
        for setting in SETTINGS:
            slot = (name, setting.key)
            word = self._wanted(name, setting, stored.get(setting.key))
            if word is None or (setting.key in offered and reported_word(setting, reported) == word):
                self._sent.pop(slot, None)
                self._held.pop(slot, None)
                continue
            if setting.key not in offered:
                self._note(name, setting, "model_does_not_list" if setting.key not in listed else "not_reported")
                continue
            if not self._due(name, setting, word, now):
                continue
            changes = changes_for(setting, word, reported)
            fields = {
                "device": name,
                "setting": setting.key,
                "value": word,
                "properties": ",".join(f"{prop}={value}" for prop, value in changes.items()),
            }
            if not allowed:
                if self._held.get(slot) != changes:
                    self._held[slot] = changes
                    log_event(logging.INFO, "dry_run_device_settings_write", **fields, **gate_fields())
                continue
            self._held.pop(slot, None)
            self._count(name, setting.key, word, now)
            try:
                answered = bool(write(changes, fields))
            except Exception as exc:
                log_event(logging.WARNING, "write_device_settings_error", **fields, error=exc)
                break
            if answered:
                written += 1
                log_event(logging.INFO, "write_device_settings", **fields)
        return written

    def _wanted(self, name, setting, stored):
        word, valid = normalized_word(setting, stored)
        if not valid:
            self._note(name, setting, "invalid_value", value=stored)
        return word

    def _note(self, name, setting, reason, **fields):
        key = (name, setting.key, reason, str(fields.get("value")))
        if key in self._noted:
            return
        self._noted.add(key)
        log_event(
            logging.WARNING if reason == "invalid_value" else logging.INFO,
            "device_setting_not_applied",
            device=name,
            setting=setting.key,
            reason=reason,
            **fields,
        )

    def _due(self, name, setting, word, now):
        sent = self._sent.get((name, setting.key))
        if sent is None or sent.word != word:
            return True
        if now - sent.last < RESEND_SECONDS:
            return False
        if sent.attempts < MAX_ATTEMPTS:
            return True
        if not sent.warned:
            sent.warned = True
            log_event(
                logging.WARNING,
                "device_setting_not_taken",
                device=name,
                setting=setting.key,
                value=word,
                attempts=sent.attempts,
                retry_after_s=int(max(0.0, HOLD_SECONDS - (now - sent.last))),
            )
        if now - sent.last < HOLD_SECONDS:
            return False
        sent.attempts = 0
        return True

    def _count(self, name, key, word, now):
        sent = self._sent.get((name, key))
        if sent is None or sent.word != word:
            self._sent[(name, key)] = _Sent(word, 1, now)
            return
        sent.attempts += 1
        sent.last = now
