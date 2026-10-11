# SPDX-License-Identifier: AGPL-3.0-or-later
"""Live control snapshot the running EMS writes once per control cycle.

A non-authoritative projection of controller memory for out-of-process readers
(``emsctl diagnose --control`` / ``--control-quality`` and the dashboard's
Diagnose tab). The EMS never reads it back and nothing in it is operator state;
``runtime-state.json`` stays the only home of that.
"""

import json
import logging
import math
import os
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

from ems import config as cfg
from ems.logging_utils import log_event

CONTROL_STATUS_SCHEMA_VERSION = 1
GRID_SAMPLE_WINDOW = 30


def _iso(epoch, timespec="milliseconds"):
    if epoch is None:
        return None
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat(timespec=timespec)


def _number(value):
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _control_block(explanation):
    if explanation is None:
        return {
            "grid_power_w": None,
            "filtered_load_w": None,
            "commanded_total_w": None,
            "allocated_target_total_w": None,
            "effective_target_total_w": None,
        }
    return {
        "grid_power_w": _number(explanation.load_w),
        "filtered_load_w": _number(explanation.filtered_load_w),
        "commanded_total_w": _number(explanation.commanded_total_w),
        "allocated_target_total_w": _number(explanation.allocated_target_total_w),
        "effective_target_total_w": _number(explanation.effective_target_total_w),
    }


def _grid_meter_block(controller, now, now_monotonic):
    """Read failures and measurement time from the meter's own health record.

    A meter that misses a read keeps serving its last value, so the time of the
    last successful read is what dates the grid power, not the cycle.
    """

    health = getattr(getattr(controller, "shelly", None), "health", None)
    failures = getattr(health, "consecutive_failures", None)
    age_seconds = getattr(health, "age_seconds", None)
    age = age_seconds(now_monotonic) if callable(age_seconds) else None
    return {
        "consecutive_read_failures": int(failures) if isinstance(failures, int) else None,
        "measured_at": None if age is None else _iso(now - age, "seconds"),
    }


def _device_entry(controller, dev, explanation):
    name = dev.name
    state = controller.last_states.get(name)
    decided = explanation.devices.get(name) if explanation is not None else None
    return {
        "online": bool(controller.device_online.get(name, False)),
        "last_seen_at": _iso(controller.last_seen.get(name)),
        "max_power_w": _number(getattr(dev, "max_power", None)),
        "soc": _number(getattr(state, "soc", None)),
        "min_soc": _number(getattr(state, "min_soc", None)),
        "pv_input_w": _number(getattr(state, "solar", None)),
        "output_w": _number(getattr(state, "output", None)),
        "output_limit_w": _number(getattr(state, "output_limit", None)),
        "battery_charge_w": _number(getattr(state, "pack_out", None)),
        "battery_discharge_w": _number(getattr(state, "pack_in", None)),
        "allocated_target_w": _number(getattr(decided, "allocated_target_w", None)),
        "effective_target_w": _number(getattr(decided, "effective_target_w", None)),
        "limiting_reason": getattr(decided, "limiting_reason", None),
    }


def build_control_status(controller, *, now, now_monotonic):
    """Project what the controller holds after a cycle into the snapshot shape.

    ``grid_samples`` is left empty; the writer that keeps them fills it in.
    """

    explanation = controller.last_control_explanation
    return {
        "schema_version": CONTROL_STATUS_SCHEMA_VERSION,
        "written_at": _iso(now),
        "loop_interval_s": controller.runtime_system_int(
            "loop_interval", cfg.LOOP_INTERVAL, minimum=1
        ),
        "cycle": {
            "fetched_at": _iso(controller.last_fetch_at),
            "failed_cycles": int(controller.cycle_failures),
            "mode": getattr(explanation, "mode", None),
        },
        "control": _control_block(explanation),
        "grid_meter": _grid_meter_block(controller, now, now_monotonic),
        "devices": {
            dev.name: _device_entry(controller, dev, explanation)
            for dev in controller.devices
        },
        "grid_samples": [],
    }


def _write_atomically(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    staged = path.with_name(path.name + ".tmp")
    try:
        staged.write_text(json.dumps(payload, allow_nan=False), encoding="utf-8")
        os.replace(staged, path)
    finally:
        staged.unlink(missing_ok=True)


class ControlStatusWriter:
    """Keeps the recent grid samples and writes the snapshot after each cycle."""

    def __init__(self, path, *, window=GRID_SAMPLE_WINDOW):
        self.path = Path(path)
        self.grid_samples = deque(maxlen=window)
        self.failing = False

    def _record_sample(self, status):
        cycle_at = status["cycle"]["fetched_at"]
        grid_power = status["control"]["grid_power_w"]
        if cycle_at is None or grid_power is None:
            return
        if self.grid_samples and self.grid_samples[-1]["cycle_at"] == cycle_at:
            return
        self.grid_samples.append({
            "cycle_at": cycle_at,
            "grid_power_w": grid_power,
            "measured_at": status["grid_meter"]["measured_at"],
        })

    def write(self, controller, *, now=None, now_monotonic=None):
        """Write the snapshot; return whether it reached the disk."""

        now = time.time() if now is None else now
        now_monotonic = time.monotonic() if now_monotonic is None else now_monotonic
        try:
            status = build_control_status(
                controller, now=now, now_monotonic=now_monotonic
            )
            self._record_sample(status)
            status["grid_samples"] = list(self.grid_samples)
            _write_atomically(self.path, status)
        except Exception as exc:
            # A projection for diagnostics must never stop the loop that
            # drives the hardware, whatever went wrong building or writing it.
            if not self.failing:
                log_event(
                    logging.WARNING,
                    "control_status_write_failed",
                    path=str(self.path),
                    error=exc,
                )
            self.failing = True
            return False
        if self.failing:
            log_event(logging.INFO, "control_status_write_recovered", path=str(self.path))
            self.failing = False
        return True
