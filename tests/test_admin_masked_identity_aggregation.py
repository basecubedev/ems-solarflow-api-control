# SPDX-License-Identifier: AGPL-3.0-or-later
"""Two observations that only *display* the same masked serial stay distinct.

A redacted view renders a serial as ``••••``. Three layers used to read that
placeholder as an equality key, so two unrelated inverters collapsed into one
entry and the later observation silently overwrote the earlier one:

``admin.js`` ``deviceKey``
    keyed the discovery Map on ``api_family + ":" + serial_number`` whenever any
    serial string existed, so both observations produced the key
    ``zendure:••••``. The parent collection (``session.devices`` /
    ``aggregateDevices``) therefore held one entry, carrying the *second*
    observation's host — and that same key also drove DOM identity, dismissal
    and selection.

``admin.js`` ``discoveryDeviceMatch``
    compared raw serials first and returned ``true`` for two masked values, so
    ``mergeDiscoveryDevice`` merged before ``deviceKey`` was even consulted.

``admin/discovery_unify.py`` ``_identity_key``
    grouped on ``serial_number.lower()`` with only whitespace cleaning, so the
    backend emitted a single unified device with ``id="serial:••••"``.

Only ``ems/device_identity.py`` was already correct: a masked value is not
evidence there. These contracts pin that Core answer as the *only* one.
"""

import json
import os
import shutil
import subprocess

import pytest

from admin.discovery_unify import build_unified_devices
from ems.device_identity import (
    resolve_inverter_identity_evidence,
    same_physical_inverter_evidence,
)

pytestmark = pytest.mark.simulation

STATIC_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "admin", "static"
)

MASKED = "••••"
TOKEN_KEY = b"masked-identity-test-key-32-bytes"

# The placeholder vocabulary the project actually emits, audited against
# ``admin/secret_policy.py`` (``REDACTED_PLACEHOLDER``), the ``•``/``…`` mask
# markers in ``ems/device_identity.py`` and the ``<redacted>`` family used by
# ``ems/external_status.py``. Values made only of mask punctuation are included
# because a redacted view legitimately renders one.
PLACEHOLDER_SERIALS = (
    "••••",
    "…abcd",
    "****",
    "<redacted>",
    "[REDACTED]",
    "redacted",
    "YOUR_SN",
    "your-serial",
    "",
    "   ",
)


def _read():
    with open(os.path.join(STATIC_DIR, "admin.js"), encoding="utf-8") as handle:
        return handle.read()


def _extract_fn(js, name):
    marker = "function " + name
    assert marker in js, f"{name} is missing from admin.js"
    idx = js.index(marker)
    prefix = "async " if js[idx - 6 : idx] == "async " else ""
    body = js[idx:]
    depth = 0
    for position, char in enumerate(body):
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return prefix + body[: position + 1]
    raise AssertionError(f"unbalanced braces while extracting {name}")


def _run(names, script):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for the discovery aggregation contract")
    js = _read()
    helpers = "\n".join(_extract_fn(js, name) for name in names)
    result = subprocess.run(
        [node, "-e", helpers + "\n" + script], text=True, capture_output=True, check=False
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


MERGE_HELPERS = (
    "observationKey",
    "sourcesOf",
    "normalizeDiscoverySource",
    "mergeDiscoveryDevice",
)


def test_masked_serial_keeps_two_discovery_observations_apart():
    """The confirmed collision: same masked serial, two hosts, one Map entry."""

    payload = _run(
        MERGE_HELPERS,
        """
const session = { devices: new Map() };
const a = { observation_id: "obs:v1:aaa", serial_number: "%s",
  host: "10.0.0.1", ip: "10.0.0.1", api_family: "zendure" };
const b = { observation_id: "obs:v1:bbb", serial_number: "%s",
  host: "10.0.0.2", ip: "10.0.0.2", api_family: "zendure" };
mergeDiscoveryDevice(session, a, "active_scan");
mergeDiscoveryDevice(session, b, "active_scan");
console.log(JSON.stringify({
  size: session.devices.size,
  ips: [...session.devices.values()].map((d) => d.ip).sort(),
}));
"""
        % (MASKED, MASKED),
    )

    assert payload["size"] == 2
    assert payload["ips"] == ["10.0.0.1", "10.0.0.2"]


def test_masked_serial_observations_without_backend_ids_never_merge():
    """Missing authoritative IDs fail closed: still two entries, never one.

    A fallback key may keep rendering non-destructive, but it must be unique per
    response item — never derived from the displayed serial.
    """

    payload = _run(
        MERGE_HELPERS,
        """
const session = { devices: new Map() };
const a = { serial_number: "%s", host: "10.0.0.1", ip: "10.0.0.1", api_family: "zendure" };
const b = { serial_number: "%s", host: "10.0.0.2", ip: "10.0.0.2", api_family: "zendure" };
mergeDiscoveryDevice(session, a, "active_scan");
mergeDiscoveryDevice(session, b, "active_scan");
console.log(JSON.stringify({ size: session.devices.size }));
"""
        % (MASKED, MASKED),
    )

    assert payload["size"] == 2


def test_browser_aggregation_key_is_never_derived_from_a_displayed_serial():
    """``observationKey`` projects the backend id; it never reads hardware fields."""

    payload = _run(
        ("observationKey",),
        """
const first = { observation_id: "obs:v1:aaa", serial_number: "%s", api_family: "zendure" };
const second = { observation_id: "obs:v1:bbb", serial_number: "%s", api_family: "zendure" };
const sameId = { observation_id: "obs:v1:aaa", serial_number: "REAL-1", api_family: "other" };
console.log(JSON.stringify({
  distinct: observationKey(first) !== observationKey(second),
  stable: observationKey(first) === observationKey(sameId),
  carriesSerial: observationKey(first).includes("%s"),
}));
"""
        % (MASKED, MASKED, MASKED),
    )

    assert payload["distinct"] is True
    assert payload["stable"] is True
    assert payload["carriesSerial"] is False


def test_unified_discovery_never_groups_two_masked_serials():
    """The backend unifier must not treat a redacted placeholder as identity."""

    devices = build_unified_devices(
        {
            "local_api": [
                {
                    "id": "scan-a",
                    "serial_number": MASKED,
                    "ip": "10.0.0.1",
                    "api_family": "zendure",
                    "display_name": "Inverter",
                },
                {
                    "id": "scan-b",
                    "serial_number": MASKED,
                    "ip": "10.0.0.2",
                    "api_family": "zendure",
                    "display_name": "Inverter",
                },
            ]
        },
        ["local_api"],
    )

    assert len(devices) == 2
    assert len({device["id"] for device in devices}) == 2
    assert sorted(device["ip"] for device in devices) == ["10.0.0.1", "10.0.0.2"]


def test_unified_discovery_emits_a_unique_observation_id_per_device():
    devices = build_unified_devices(
        {
            "local_api": [
                {"id": "scan-a", "serial_number": MASKED, "ip": "10.0.0.1"},
                {"id": "scan-b", "serial_number": MASKED, "ip": "10.0.0.2"},
            ]
        },
        ["local_api"],
        identity_token_key=TOKEN_KEY,
    )

    observation_ids = [device["observation_id"] for device in devices]
    assert all(observation_ids)
    assert len(set(observation_ids)) == len(devices)
    assert not any(MASKED in value for value in observation_ids)
    # Unresolved identity still gets an observation id, but never a physical one.
    assert [device["identity_status"] for device in devices] == ["unresolved"] * 2
    assert [device["physical_device_id"] for device in devices] == [None, None]


def test_unified_discovery_without_a_key_fails_closed_instead_of_guessing():
    devices = build_unified_devices(
        {"local_api": [{"id": "scan-a", "serial_number": "EOD1AAA111", "ip": "10.0.0.1"}]},
        ["local_api"],
    )

    assert devices[0]["observation_id"] is None
    assert devices[0]["physical_device_id"] is None


def test_unified_discovery_still_groups_a_real_shared_serial():
    """The fix must not stop two sources of one real inverter from unifying."""

    devices = build_unified_devices(
        {
            "local_api": [{"id": "a", "serial_number": "EOD1AAA111", "ip": "10.0.0.1"}],
            "local_mqtt": [{"id": "b", "serial_number": "eod1aaa111", "device_id": "DEV"}],
        },
        ["local_api", "local_mqtt"],
    )

    assert len(devices) == 1
    assert devices[0]["sources"] == ["local_api", "local_mqtt"]


@pytest.mark.parametrize("placeholder", PLACEHOLDER_SERIALS)
def test_placeholder_serial_is_never_positive_identity_evidence(placeholder):
    left = resolve_inverter_identity_evidence(
        {"serial_number": placeholder, "ip": "10.0.0.1"}
    )
    right = resolve_inverter_identity_evidence(
        {"serial_number": placeholder, "ip": "10.0.0.2"}
    )

    assert not same_physical_inverter_evidence(left, right)
    assert not left.serial_keys()
    assert not right.serial_keys()
