# SPDX-FileCopyrightText: 2026 basecubedev
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Which PV inputs look empty, and which marked empty deliver: the hints the Admin offers.

The watch keeps each input's highest power of the day. A day on which an input
stays dark while another input of the same device delivers counts one day;
``DARK_DAYS`` such days make the hint that the input carries no module, or one
that failed. A day on which no input delivers says nothing either way, and a
day the input gives any light starts the count again. An input marked empty
(``devices[].unused_pv_inputs``) that delivers is the opposite hint. The watch
only reads: the operator decides, through the config.

Import-side-effect-free.
"""

import json
import logging
import math
import time
from datetime import date, datetime, timedelta
from pathlib import Path

from ems.file_io import atomic_write_bytes
from ems.logging_utils import log_event
from ems.pv_inputs import PV_INPUT_COUNT, pv_input_powers

DARK_W = 1.0
DELIVERING_W = 50.0
DARK_DAYS = 3
SAVE_INTERVAL_SECONDS = 600
KEEP_DAYS = 30
WATCH_VERSION = 1


def _fresh(day):
    return {
        "day": day,
        "peaks": [None] * PV_INPUT_COUNT,
        "last_day": None,
        "last_peaks": [None] * PV_INPUT_COUNT,
        "dark_days": [0] * PV_INPUT_COUNT,
    }


def _is_power(value):
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def _is_day(value):
    if not isinstance(value, str):
        return False
    try:
        return date.fromisoformat(value).isoformat() == value
    except ValueError:
        return False


def _days_between(earlier, later):
    return (date.fromisoformat(later) - date.fromisoformat(earlier)).days


def _valid_entry(entry):
    if not isinstance(entry, dict) or not _is_day(entry.get("day")):
        return False
    if entry.get("last_day") is not None and not _is_day(entry["last_day"]):
        return False
    for key in ("peaks", "last_peaks"):
        values = entry.get(key)
        if not isinstance(values, list) or len(values) != PV_INPUT_COUNT:
            return False
        if not all(value is None or _is_power(value) for value in values):
            return False
    days = entry.get("dark_days")
    return (
        isinstance(days, list)
        and len(days) == PV_INPUT_COUNT
        and all(isinstance(value, int) and not isinstance(value, bool) and value >= 0 for value in days)
    )


def _close_day(entry):
    """Count the day ``entry`` holds; a day not right after the last one counted breaks every run."""

    if entry["last_day"] is not None and _days_between(entry["last_day"], entry["day"]) != 1:
        entry["dark_days"] = [0] * PV_INPUT_COUNT
    peaks = entry["peaks"]
    delivering = {index for index, peak in enumerate(peaks) if peak is not None and peak >= DELIVERING_W}
    for index, peak in enumerate(peaks):
        if peak is None or peak > DARK_W:
            entry["dark_days"][index] = 0
        elif delivering - {index}:
            entry["dark_days"][index] += 1
    entry["last_day"] = entry["day"]
    entry["last_peaks"] = peaks
    entry["peaks"] = [None] * PV_INPUT_COUNT


class PvInputWatch:
    """The day's peaks and dark days of each device's inputs, kept in ``path`` across restarts.

    ``unused_for(name)`` gives the inputs a device's entry marks empty. A day
    begins at midnight in ``zone``, or in the C library's local time without
    one.
    """

    def __init__(self, path, *, unused_for, zone=None):
        self.path = Path(path)
        self._unused_for = unused_for
        self._zone = zone
        try:
            self._devices = self._load()
        except Exception as exc:
            log_event(logging.WARNING, "pv_input_watch_unreadable", path=str(self.path), error=exc)
            self._devices = {}
        self._seen = set()
        self._dirty = False
        self._day_ended = False
        self._saved_at = None
        self._failing = False
        self._observing_failed = False

    def _load(self):
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as exc:
            log_event(logging.WARNING, "pv_input_watch_unreadable", path=str(self.path), error=exc)
            return {}
        if not isinstance(raw, dict) or raw.get("version") != WATCH_VERSION or not isinstance(raw.get("devices"), dict):
            return {}
        return {str(name): {**_fresh(entry["day"]), **entry} for name, entry in raw["devices"].items() if _valid_entry(entry)}

    def _day(self, now):
        moment = datetime.fromtimestamp(now, self._zone) if self._zone is not None else datetime.fromtimestamp(now)
        return moment.date().isoformat()

    def observe(self, name, powers, now=None):
        """Take one reading of a device's inputs, ``None`` for one its report did not carry."""

        now = time.time() if now is None else now
        day = self._day(now)
        self._seen.add(name)
        entry = self._devices.setdefault(name, _fresh(day))
        if day < entry["day"]:
            if _days_between(day, entry["day"]) <= 1:
                return
            log_event(logging.WARNING, "pv_input_watch_clock_back", device=name, day=entry["day"], now=day)
            entry["day"] = day
            entry["peaks"] = [None] * PV_INPUT_COUNT
            self._day_ended = True
        if day > entry["day"]:
            _close_day(entry)
            entry["day"] = day
            self._day_ended = True
            self._dirty = True
        for index, value in enumerate(powers[:PV_INPUT_COUNT]):
            if not _is_power(value):
                continue
            peak = entry["peaks"][index]
            if peak is None or value > peak:
                entry["peaks"][index] = float(value)
                self._dirty = True

    def observe_controller(self, controller, now=None):
        """Take this cycle's PV inputs of every device the dashboard shows that is online.

        A failure is logged once and never raised, so the loop that drives the
        hardware goes on.
        """

        try:
            from dashboard.telemetry import telemetry_only_device_tiles

            for dev in controller.devices:
                state = controller.last_states.get(dev.name)
                if state is not None and controller.device_online.get(dev.name, False):
                    self.observe(dev.name, pv_input_powers(state), now)
            for tile in telemetry_only_device_tiles(controller):
                if tile.get("online") and tile.get("has_reading", True):
                    self.observe(tile["name"], pv_input_powers(tile["state"]), now)
            self.save_if_due(now)
        except Exception as exc:
            if not self._observing_failed:
                log_event(logging.WARNING, "pv_input_watch_failed", error=exc)
            self._observing_failed = True
            return
        if self._observing_failed:
            log_event(logging.INFO, "pv_input_watch_recovered")
            self._observing_failed = False

    def hints(self, now=None):
        """``[{device, input, kind, ...}]`` for the devices seen since the EMS started.

        ``kind`` ``dark`` carries ``days``, the dark days counted in a run, a
        day on which no input delivered left out, while its device was seen
        today or the day before and until the input gives light again;
        ``delivers``, for an input marked empty, ``peak_w``, its highest power
        today or the day before.
        """

        today = self._day(time.time() if now is None else now)
        found = []
        for name in sorted(self._seen):
            entry = self._devices.get(name)
            if entry is None or _days_between(entry["day"], today) not in (0, 1):
                continue
            unused = self._unused_for(name)
            days_held = entry["last_day"] is not None and _days_between(entry["last_day"], entry["day"]) == 1
            before = days_held and entry["day"] == today
            for index in range(PV_INPUT_COUNT):
                number = index + 1
                current = entry["peaks"][index]
                if number in unused:
                    peaks = [peak for peak in (current, entry["last_peaks"][index] if before else None) if peak is not None]
                    if peaks and max(peaks) >= DELIVERING_W:
                        found.append({"device": name, "input": number, "kind": "delivers", "peak_w": round(max(peaks))})
                elif days_held and entry["dark_days"][index] >= DARK_DAYS and (current is None or current <= DARK_W):
                    found.append({"device": name, "input": number, "kind": "dark", "days": entry["dark_days"][index]})
        return found

    def save_if_due(self, now=None):
        """Save at once when a day ended, else at most every ``SAVE_INTERVAL_SECONDS`` after a new peak."""

        now = time.time() if now is None else now
        due = self._saved_at is None or now - self._saved_at >= SAVE_INTERVAL_SECONDS
        if self._day_ended or (self._dirty and due):
            self.save(now)

    def save(self, now=None):
        """Write the watch; a failure is logged once and never raised."""

        now = time.time() if now is None else now
        self._saved_at = now
        self._day_ended = False
        oldest = (date.fromisoformat(self._day(now)) - timedelta(days=KEEP_DAYS)).isoformat()
        devices = {name: entry for name, entry in self._devices.items() if name in self._seen or entry["day"] >= oldest}
        try:
            raw = json.dumps({"version": WATCH_VERSION, "devices": devices}, allow_nan=False).encode("utf-8")
            atomic_write_bytes(str(self.path), raw)
        except Exception as exc:
            if not self._failing:
                log_event(logging.WARNING, "pv_input_watch_save_failed", path=str(self.path), error=exc)
            self._failing = True
            return False
        if self._failing:
            log_event(logging.INFO, "pv_input_watch_save_recovered", path=str(self.path))
            self._failing = False
        self._devices = devices
        self._dirty = False
        return True

