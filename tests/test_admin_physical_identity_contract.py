# SPDX-License-Identifier: AGPL-3.0-or-later
"""Backend/frontend physical-identity equivalence contract.

``ems.zendure_mqtt.config_entries.zendure_physical_identity`` (backend) and
``physicalInverterIdentity`` (admin.js) must resolve the same identity for the
same device shape: physical serial first (Local API ``sn``, MQTT
``serial_number``), otherwise a server-issued opaque equality token. MQTT route
ids, placeholders, and display-masked values never become browser identity.
Fresh Setup and Maintenance share these semantics so a divergence would let the
two flows disagree about whether two observations are one physical inverter.
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
    {"serial_number": "<redacted>"},
    {"serial_number": "[REDACTED]"},
    {"serial_number": "redacted"},
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
        for name in ("normalizeSerial", "usableSerialValue", "physicalInverterIdentity")
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
        for name in ("normalizeSerial", "usableSerialValue", "physicalInverterIdentity")
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
    assert zendure_physical_identity({"mqtt": {"device_id": "R9"}}) is None
    assert zendure_physical_identity({"sn": "YOUR_SN"}) is None
    assert zendure_physical_identity({"serial_number": "••••"}) is None
    assert zendure_physical_identity({"serial_number": "<redacted>"}) is None
    assert zendure_physical_identity({"serial_number": "[redacted]"}) is None
    assert zendure_physical_identity({"serial_number": "redacted"}) is None
    assert zendure_physical_identity({}) is None


def test_frontend_uses_server_token_but_never_reconstructs_masked_cloud_route():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for the identity contract test")
    with open(os.path.join(STATIC_DIR, "admin.js"), encoding="utf-8") as handle:
        js = handle.read()
    helpers = "\n".join(
        _extract_fn(js, name)
        for name in ("normalizeSerial", "usableSerialValue", "physicalInverterIdentity")
    )
    cases = [
        {
            "physical_identity_token": "opaque:v1:same-token",
            "mqtt": {"device_id": "…1234"},
        },
        {
            "physical_identity_token": "opaque:v1:same-token",
            "device_id": "…1234",
        },
        {
            "physical_identity_token": "opaque:v1:other-token",
            "mqtt": {"device_id": "ACCOUNT_ROUTE_1234"},
        },
        {"mqtt": {"device_id": "ACCOUNT_ROUTE_1234"}},
    ]
    script = (
        helpers
        + "\nconst cases = "
        + json.dumps(cases)
        + ";\nconsole.log(JSON.stringify(cases.map(physicalInverterIdentity)));"
    )
    result = subprocess.run(
        [node, "-e", script], text=True, capture_output=True, check=False
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == [
        "opaque:v1:same-token",
        "opaque:v1:same-token",
        "opaque:v1:other-token",
        "",
    ]


def test_maintenance_proposal_state_matches_tokens_and_keeps_scopes_separate():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for the identity contract test")
    with open(os.path.join(STATIC_DIR, "admin.js"), encoding="utf-8") as handle:
        js = handle.read()
    helpers = _proposal_state_helpers(js)
    script = helpers + """
function mconfigIsMqttDevice(device) { return device && device.kind === "zendure_mqtt"; }
// Maintenance loads the draft as a clone of the installed config, so the
// configured device is present in both.
const configured = { kind: "zendure_mqtt",
  physical_identity_token: "opaque:v1:scope-a",
  physical_identity_alias_tokens: ["opaque:v1:scope-a"],
  mqtt: { device_id: "…1234" } };
const mconfigState = {
  pristine: { devices: [configured] },
  draft: { devices: [configured] },
};
const same = { physical_identity_token: "opaque:v1:scope-a",
  physical_identity_alias_tokens: ["opaque:v1:scope-a"],
  config_fragment: { mqtt: { device_id: "…1234" } } };
const other = { physical_identity_token: "opaque:v1:scope-b",
  physical_identity_alias_tokens: ["opaque:v1:scope-b"],
  config_fragment: { mqtt: { device_id: "…1234" } } };
const routeOnly = { device_id: "ACCOUNT_ROUTE_1234",
  config_fragment: { mqtt: { device_id: "ACCOUNT_ROUTE_1234" } } };
console.log(JSON.stringify([
  mconfigMqttProposalState(same),
  mconfigMqttProposalState(other),
  mconfigMqttProposalState(routeOnly),
]));
"""
    result = subprocess.run(
        [node, "-e", script], text=True, capture_output=True, check=False
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == ["found", "new", "new"]


PROPOSAL_STATE_HELPER_NAMES = (
    "normalizeSerial",
    "usableSerialValue",
    "physicalInverterIdentity",
    "inverterVisibleSerial",
    "inverterIdentityTokens",
    "inverterIdentitySet",
    "inverterHasIdentity",
    "inverterIdentityConflict",
    "inverterIdentitiesMatch",
    "mqttSourceOfConnection",
    "connectionBrokerScope",
    "mconfigIsMqttDevice",
    "mconfigDeviceMqttSource",
    "mconfigDeviceConnectionSource",
    "mconfigSameMqttConnection",
    "mconfigProposalIdentityView",
    "mconfigDraftDevicesMatchingCandidate",
    "mconfigPristineHasCandidateConnection",
    "mconfigMqttProposalState",
)


def _proposal_state_helpers(js):
    return "\n".join(_extract_fn(js, name) for name in PROPOSAL_STATE_HELPER_NAMES)


def test_route_only_device_recognizes_later_serial_enrichment():
    """A route-only configured device must still recognize a same-route proposal
    once it carries a physical serial (enrichment, not a duplicate); a different
    route stays an independent new device."""

    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for the identity contract test")
    with open(os.path.join(STATIC_DIR, "admin.js"), encoding="utf-8") as handle:
        js = handle.read()
    helpers = _proposal_state_helpers(js)
    script = helpers + """
function mconfigIsMqttDevice(device) { return device && device.kind === "zendure_mqtt"; }
// Existing configured device is route-only: its only alias token is the route.
const configured = { kind: "zendure_mqtt",
  physical_identity_token: "opaque:v1:route-1",
  physical_identity_alias_tokens: ["opaque:v1:route-1"],
  mqtt: { device_id: "…1234" } };
const mconfigState = {
  pristine: { devices: [configured] },
  draft: { devices: [configured] },
};
// Same route, now with a serial: primary token is the serial, but the route
// survives as an alias, so it intersects the existing route-only device.
const enriched = {
  serial_number: "SERIAL-001",
  physical_identity_token: "opaque:v1:serial-1",
  physical_identity_alias_tokens: ["opaque:v1:serial-1", "opaque:v1:route-1"],
  config_fragment: { mqtt: { device_id: "…1234" } },
};
// A different route entirely: independent new device.
const unrelated = {
  serial_number: "SERIAL-003",
  physical_identity_token: "opaque:v1:serial-3",
  physical_identity_alias_tokens: ["opaque:v1:serial-3", "opaque:v1:route-9"],
  config_fragment: { mqtt: { device_id: "…9999" } },
};
console.log(JSON.stringify([
  mconfigMqttProposalState(enriched),
  mconfigMqttProposalState(unrelated),
]));
"""
    result = subprocess.run(
        [node, "-e", script], text=True, capture_output=True, check=False
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == ["found", "new"]


def test_serial_device_matches_route_rediscovery_and_blocks_serial_conflict():
    """A serial-configured device (with a route alias) recognizes a later
    route-only rediscovery of the same route as itself, but blocks a same-route
    proposal that claims a different physical serial."""

    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for the identity contract test")
    with open(os.path.join(STATIC_DIR, "admin.js"), encoding="utf-8") as handle:
        js = handle.read()
    helpers = _proposal_state_helpers(js)
    script = helpers + """
function mconfigIsMqttDevice(device) { return device && device.kind === "zendure_mqtt"; }
const configured = { kind: "zendure_mqtt", serial_number: "SERIAL-001",
  physical_identity_token: "opaque:v1:serial-1",
  physical_identity_alias_tokens: ["opaque:v1:serial-1", "opaque:v1:route-1"],
  mqtt: { device_id: "…1234" } };
const mconfigState = {
  pristine: { devices: [configured] },
  draft: { devices: [configured] },
};
const routeOnly = {
  physical_identity_token: "opaque:v1:route-1",
  physical_identity_alias_tokens: ["opaque:v1:route-1"],
  config_fragment: { mqtt: { device_id: "…1234" } },
};
// Same route, a DIFFERENT serial: contradiction, must be blocked.
const contradiction = {
  serial_number: "SERIAL-002",
  physical_identity_token: "opaque:v1:serial-2",
  physical_identity_alias_tokens: ["opaque:v1:serial-2", "opaque:v1:route-1"],
  config_fragment: { mqtt: { device_id: "…1234" } },
};
console.log(JSON.stringify([
  mconfigMqttProposalState(routeOnly),
  mconfigMqttProposalState(contradiction),
]));
"""
    result = subprocess.run(
        [node, "-e", script], text=True, capture_output=True, check=False
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == ["found", "identity_conflict"]
