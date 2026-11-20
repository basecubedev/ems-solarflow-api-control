# SPDX-FileCopyrightText: 2026 basecubedev
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Two observations that only *display* the same masked serial stay distinct.

A redacted view renders a serial as ``••••``. Three layers once read that
placeholder as an equality key, so two unrelated inverters collapsed into one
entry and the later observation silently overwrote the earlier one: the removed
Classic console keyed its discovery collection on ``api_family + ":" +
serial_number`` and merged two masked serials before that key was even
consulted, and ``admin/discovery_unify.py`` ``_identity_key`` grouped on
``serial_number.lower()``, so the backend emitted a single unified device with
``id="serial:••••"``.

Only ``ems/device_identity.py`` was already correct: a masked value is not
evidence there. These contracts pin that Core answer as the *only* one, and hold
the Admin console's ``observationKey`` and ``mergeObservation``
(``frontend/admin-modern/src/lib/discovery.js``) to the observation id the
backend issues.
"""

import json
from pathlib import Path

import pytest

from admin.discovery_unify import build_unified_devices
from ems.device_identity import (
    resolve_inverter_identity_evidence,
    same_physical_inverter_evidence,
)
from tests.modern_frontend import js_run

pytestmark = [
    pytest.mark.admin,
    pytest.mark.setup,
    pytest.mark.contract,
    pytest.mark.simulation,
]

DISCOVERY_JS = Path(__file__).resolve().parents[1] / "frontend" / "admin-modern" / "src" / "lib" / "discovery.js"

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


def _merged(*devices):
    """What the Admin console's discovery collection holds after each device was merged in."""

    return js_run(
        DISCOVERY_JS,
        f"""
let found = {{}};
for (const device of {json.dumps(list(devices))}) found = m.mergeObservation(found, device, "active_scan");
process.stdout.write(JSON.stringify({{
  keys: Object.keys(found),
  ips: Object.values(found).map((device) => device.ip).sort(),
}}));
""",
    )


def test_masked_serial_keeps_two_discovery_observations_apart():
    """The confirmed collision: same masked serial, two hosts, one collection entry."""

    payload = _merged(
        {"observation_id": "obs:v1:aaa", "serial_number": MASKED,
         "host": "10.0.0.1", "ip": "10.0.0.1", "api_family": "zendure"},
        {"observation_id": "obs:v1:bbb", "serial_number": MASKED,
         "host": "10.0.0.2", "ip": "10.0.0.2", "api_family": "zendure"},
    )

    assert sorted(payload["keys"]) == ["obs:v1:aaa", "obs:v1:bbb"]
    assert payload["ips"] == ["10.0.0.1", "10.0.0.2"]


def test_masked_serial_observations_without_backend_ids_never_merge():
    """Missing authoritative IDs fail closed: still two entries, never one.

    A fallback key may keep rendering non-destructive, but it must be unique per
    response item — never derived from the displayed serial.
    """

    payload = _merged(
        {"serial_number": MASKED, "host": "10.0.0.1", "ip": "10.0.0.1", "api_family": "zendure"},
        {"serial_number": MASKED, "host": "10.0.0.2", "ip": "10.0.0.2", "api_family": "zendure"},
    )

    assert len(payload["keys"]) == 2
    assert not any(MASKED in key for key in payload["keys"])
    assert payload["ips"] == ["10.0.0.1", "10.0.0.2"]


def test_browser_aggregation_key_is_never_derived_from_a_displayed_serial():
    """``observationKey`` projects the backend id; it never reads hardware fields."""

    first = {"observation_id": "obs:v1:aaa", "serial_number": MASKED, "api_family": "zendure"}
    second = {"observation_id": "obs:v1:bbb", "serial_number": MASKED, "api_family": "zendure"}
    same_id = {"observation_id": "obs:v1:aaa", "serial_number": "REAL-1", "api_family": "other"}
    keys = js_run(
        DISCOVERY_JS,
        f"process.stdout.write(JSON.stringify({json.dumps([first, second, same_id])}.map(m.observationKey)));",
    )

    assert keys[0] != keys[1]
    assert keys[0] == keys[2]
    assert MASKED not in keys[0]


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
