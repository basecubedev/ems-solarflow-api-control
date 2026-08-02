# SPDX-License-Identifier: AGPL-3.0-or-later
"""The one place that stamps browser-safe identities onto discovered devices.

Three different questions used to share one browser-side key derived from the
displayed serial. They are separate concepts and get separate ids here:

``observation_id``
    *this device, reached this way*. Two observations that only display the same
    masked serial answer on different hosts or routes, so they keep different
    ids and never collapse into one card, selection or dismissal.

``physical_device_id``
    *this hardware*. Issued only when Core actually resolved a physical
    identity, so "we do not know" can never be mistaken for "this device".

``connection_id``
    *this transport route*. What a replacement plan keeps or replaces.

Every id is a keyed token: the browser can compare them but cannot recover a
host, serial or route segment from one, and cannot forge one.
"""

from ems.device_identity import (
    connection_coordinates,
    opaque_connection_id,
    opaque_observation_id,
    resolve_physical_identity,
)

OBSERVATION_ID_FIELD = "observation_id"
CONNECTION_ID_FIELD = "connection_id"
PHYSICAL_DEVICE_ID_FIELD = "physical_device_id"
IDENTITY_STATUS_FIELD = "identity_status"

# The device-class discriminator. Two different device classes reached at one
# endpoint stay separate observations, which is what the browser's old
# type-then-address comparison did before a serial could override it.
_KIND_FIELDS = ("device_type", "api_family", "role_suggestion")


def observation_kind(device):
    """Normalized device class, or ``device`` when the source declared none."""

    if isinstance(device, dict):
        for field in _KIND_FIELDS:
            value = device.get(field)
            if isinstance(value, str) and value.strip():
                return value.strip().lower()
    return "device"


def _discriminator(device, fallback):
    """A per-observation discriminator for an unaddressable observation.

    Reached only when the source gave neither a route nor an endpoint. The
    source's own stable id is preferred; the caller's fallback (an ordinal
    within one response) keeps two such observations apart rather than letting
    them merge, which is the failure this module exists to prevent.
    """

    if isinstance(device, dict):
        for field in ("id", "stable_id", "scan_id"):
            value = device.get(field)
            if isinstance(value, str) and value.strip():
                return ("source_id", value.strip())
    return ("unaddressable", str(fallback))


def observation_components(device, *, broker_sources=None, fallback=""):
    """The components an observation id is derived from: class plus address."""

    coordinates = connection_coordinates(device, broker_sources=broker_sources)
    if coordinates is None:
        coordinates = _discriminator(device, fallback)
    return (observation_kind(device), *coordinates)


def stamp_observation_identity(
    device, *, key, broker_sources=None, fallback="", include_connection_id=False
):
    """Return ``device`` with its browser-facing identity fields set.

    Mutates and returns the given dict so callers can stamp a payload in place.
    A device Core could not physically identify still receives a unique
    ``observation_id``; it simply gets no ``physical_device_id``.
    """

    if not isinstance(device, dict):
        return device
    if key is None:
        # No key, no authority: emit the fields as unknown rather than inventing
        # an unkeyed id the browser would then treat as authoritative.
        device[OBSERVATION_ID_FIELD] = None
        device[IDENTITY_STATUS_FIELD] = None
        device[PHYSICAL_DEVICE_ID_FIELD] = None
        if include_connection_id:
            device[CONNECTION_ID_FIELD] = None
        return device
    components = observation_components(
        device, broker_sources=broker_sources, fallback=fallback
    )
    device[OBSERVATION_ID_FIELD] = opaque_observation_id(components, key)
    identity = resolve_physical_identity(
        device, broker_sources=broker_sources, token_key=key
    )
    device[IDENTITY_STATUS_FIELD] = identity.status
    device[PHYSICAL_DEVICE_ID_FIELD] = identity.public_identity_id
    if include_connection_id:
        coordinates = connection_coordinates(device, broker_sources=broker_sources)
        device[CONNECTION_ID_FIELD] = (
            opaque_connection_id(coordinates, key) if coordinates is not None else None
        )
    return device


def stamp_observations(devices, *, key, broker_sources=None, include_connection_id=False):
    """Stamp a whole response list, keeping unaddressable entries distinct."""

    if not isinstance(devices, list):
        return devices
    for index, device in enumerate(devices):
        stamp_observation_identity(
            device,
            key=key,
            broker_sources=broker_sources,
            fallback=str(index),
            include_connection_id=include_connection_id,
        )
    return devices


__all__ = [
    "CONNECTION_ID_FIELD",
    "IDENTITY_STATUS_FIELD",
    "OBSERVATION_ID_FIELD",
    "PHYSICAL_DEVICE_ID_FIELD",
    "observation_components",
    "observation_kind",
    "stamp_observation_identity",
    "stamp_observations",
]
