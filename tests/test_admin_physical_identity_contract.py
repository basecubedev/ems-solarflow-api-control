# SPDX-License-Identifier: AGPL-3.0-or-later
"""Backend/frontend physical-identity equivalence contract.

``ems.zendure_mqtt.config_entries.zendure_physical_identity`` (backend) and
``physicalInverterIdentity`` (admin.js) must resolve the same identity for the
same device shape: physical serial first (Local API ``sn``, MQTT
``serial_number``), MQTT routing id only when no serial exists, placeholders
and display-masked values never. Fresh Setup and Maintenance share these
helpers, so a divergence would let the two flows disagree about whether two
observations are one physical inverter.
"""

import json
import os
import shutil
import subprocess

import pytest

from ems.zendure_mqtt.config_entries import zendure_physical_identity

pytestmark = pytest.mark.simulation

STATIC_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "admin", "static"
)

CASES = [
    {"sn": " ABC "},
    {"sn": "AbC-123"},
    {"serial_number": "AbC-123"},
    {"sn": "YOUR_SN"},
    {"serial_number": "your_serial"},
    {"sn": "", "serial_number": ""},
    {"mqtt": {"device_id": "DEV1"}},
    {"device_id": "DEV2"},
    {"serial_number": "S1", "mqtt": {"device_id": "ROUTE9"}},
    {"sn": "S1", "device_id": "ROUTE9"},
    {"serial_number": "••••"},
    {"mqtt": {"device_id": "…abcd"}},
    {"serial_number": "••••", "mqtt": {"device_id": "DEV3"}},
    {},
    {"name": "WR1", "ip": "192.168.1.100"},
]


def _extract_fn(js, name):
    marker = "function " + name
    assert marker in js, f"{name} is missing from admin.js"
    idx = js.index(marker)
    body = js[idx:]
    depth = 0
    for position, char in enumerate(body):
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return body[: position + 1]
    raise AssertionError(f"unbalanced braces while extracting {name}")


def test_backend_and_frontend_identity_resolvers_agree():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for the identity contract test")
    with open(os.path.join(STATIC_DIR, "admin.js"), encoding="utf-8") as handle:
        js = handle.read()
    helpers = "\n".join(
        _extract_fn(js, name)
        for name in ("normalizeSerial", "physicalInverterIdentity")
    )
    script = (
        helpers
        + "\nconst cases = "
        + json.dumps(CASES)
        + ";\nconsole.log(JSON.stringify(cases.map(physicalInverterIdentity)));"
    )
    result = subprocess.run(
        [node, "-e", script], text=True, capture_output=True, check=False
    )
    assert result.returncode == 0, result.stderr
    frontend = json.loads(result.stdout)
    backend = [zendure_physical_identity(case) or "" for case in CASES]
    assert frontend == backend


def test_maintenance_identity_matches_fresh_setup_serial_grouping():
    """Fresh Setup groups by normalizeSerial(serial_number); Maintenance by
    physicalInverterIdentity. For every discovery-realistic serial the two keys
    must be identical, so both flows agree whether two observations are one
    physical inverter."""

    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for the identity contract test")
    with open(os.path.join(STATIC_DIR, "admin.js"), encoding="utf-8") as handle:
        js = handle.read()
    helpers = "\n".join(
        _extract_fn(js, name)
        for name in ("normalizeSerial", "physicalInverterIdentity")
    )
    serials = ["EOD1AAA111", " eod1AAA111 ", "S1-x_9", "0"]
    script = (
        helpers
        + "\nconst serials = "
        + json.dumps(serials)
        + ";\nconsole.log(JSON.stringify(serials.map((s) => ["
        + "normalizeSerial(s), physicalInverterIdentity({ serial_number: s })"
        + "])));"
    )
    result = subprocess.run(
        [node, "-e", script], text=True, capture_output=True, check=False
    )
    assert result.returncode == 0, result.stderr
    for setup_key, maintenance_key in json.loads(result.stdout):
        assert setup_key == maintenance_key


def test_backend_identity_prefers_serial_over_routing_id():
    assert zendure_physical_identity({"serial_number": "S1", "mqtt": {"device_id": "R"}}) == "s1"
    assert zendure_physical_identity({"mqtt": {"device_id": "R9"}}) == "r9"
    assert zendure_physical_identity({"sn": "YOUR_SN"}) is None
    assert zendure_physical_identity({"serial_number": "••••"}) is None
    assert zendure_physical_identity({}) is None
