# SPDX-FileCopyrightText: 2026 basecubedev
# SPDX-License-Identifier: AGPL-3.0-or-later
"""``devices[]`` entries the EMS reads and never controls.

Every question of the form "does this entry reach a control or write path?"
asks :func:`is_read_only_device_config` first. An entry of a type named here
never becomes a controller device, never gets a runtime-state entry, and is
never reconciled, written or commanded, whatever else it carries.
"""

import logging
from collections.abc import Mapping

from ems.logging_utils import log_event

E3DC_MODBUS_DEVICE_TYPE = "e3dc_modbus"
OPENDTU_HTTP_DEVICE_TYPE = "opendtu_http"

READ_ONLY_DEVICE_TYPES = frozenset({E3DC_MODBUS_DEVICE_TYPE, OPENDTU_HTTP_DEVICE_TYPE})


def device_config_type(item):
    """The normalized ``type`` of one ``devices[]`` entry, or ``""``."""

    if not isinstance(item, Mapping):
        return ""
    return str(item.get("type") or "").strip().lower()


def is_read_only_device_config(item):
    """True for a ``devices[]`` entry the EMS may only read."""

    return device_config_type(item) in READ_ONLY_DEVICE_TYPES


def is_e3dc_modbus_device_config(item):
    return device_config_type(item) == E3DC_MODBUS_DEVICE_TYPE


def is_opendtu_http_device_config(item):
    return device_config_type(item) == OPENDTU_HTTP_DEVICE_TYPE


class ReadOnlyDevices:
    """Several kinds of read-only device read as one: refreshed in turn, tiled together.

    A kind that fails to refresh leaves the others read.
    """

    def __init__(self, runtimes):
        self._runtimes = list(runtimes)

    @property
    def names(self):
        return [name for runtime in self._runtimes for name in runtime.names]

    def refresh(self):
        for runtime in self._runtimes:
            try:
                runtime.refresh()
            except Exception as exc:
                log_event(logging.WARNING, "read_only_devices_refresh_failed", kind=type(runtime).__name__, error=exc)

    def tiles(self):
        return [tile for runtime in self._runtimes for tile in runtime.tiles()]

    def close(self):
        for runtime in self._runtimes:
            close = getattr(runtime, "close", None)
            if callable(close):
                close()


def combine_read_only_devices(*runtimes):
    """The read-only devices of every kind that has some, or ``None`` when no kind has."""

    present = [runtime for runtime in runtimes if runtime is not None]
    return ReadOnlyDevices(present) if present else None


def read_only_runtime_builders(e3dc_sessions):
    """``type -> build(device_configs)`` for every read-only device type, one each."""

    from ems.e3dc_runtime import build_e3dc_device_runtime
    from ems.opendtu_http import build_opendtu_device_runtime

    return {
        E3DC_MODBUS_DEVICE_TYPE: lambda configs: build_e3dc_device_runtime(configs, e3dc_sessions),
        OPENDTU_HTTP_DEVICE_TYPE: build_opendtu_device_runtime,
    }


def build_read_only_devices(device_configs, *, e3dc_sessions):
    """The runtime that reads every enabled read-only entry of ``devices[]``, or ``None``."""

    builders = read_only_runtime_builders(e3dc_sessions)
    return combine_read_only_devices(*(build(device_configs) for build in builders.values()))
