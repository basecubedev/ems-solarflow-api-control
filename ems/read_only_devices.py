# SPDX-License-Identifier: AGPL-3.0-or-later
"""``devices[]`` entries the EMS reads and never controls.

Every question of the form "does this entry reach a control or write path?"
asks :func:`is_read_only_device_config` first. An entry of a type named here
never becomes a controller device, never gets a runtime-state entry, and is
never reconciled, written or commanded, whatever else it carries.
"""

from collections.abc import Mapping

E3DC_MODBUS_DEVICE_TYPE = "e3dc_modbus"

READ_ONLY_DEVICE_TYPES = frozenset({E3DC_MODBUS_DEVICE_TYPE})


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
