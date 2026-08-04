# SPDX-License-Identifier: AGPL-3.0-or-later
"""The browser holds no placeholder or identity policy to keep in parity.

An earlier release resolved physical identity twice — ``zendure_physical_identity``
in Core and ``physicalInverterIdentity`` in admin.js — so a contract had to keep
the two placeholder vocabularies aligned. That duplication is gone: Core resolves
identity (``tests/test_device_identity.py``), Admin issues the public ids, and the
browser compares only what it was handed.

What survives here is the Maintenance projection: given server-issued tokens and
an identity status, the discovery review must classify a proposal the same way
the backend would — enrichment recognized, scopes kept apart, a contradiction
blocked. The inputs are all issued; none of them is a serial, host or route id.
"""

import json
import os
import shutil
import subprocess

import pytest

pytestmark = [
    pytest.mark.admin,
    pytest.mark.setup,
    pytest.mark.contract,
    pytest.mark.simulation,
]

STATIC_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "admin", "static"
)


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


def _constants(js):
    return "\n".join(
        line
        for line in js.split("\n")
        if line.startswith("const ") and "PATTERN = /" in line
    )


def test_the_browser_has_no_second_identity_resolver_to_keep_in_parity():
    """The helpers this contract used to mirror are gone, not renamed."""

    with open(os.path.join(STATIC_DIR, "admin.js"), encoding="utf-8") as handle:
        js = handle.read()
    for removed in (
        "function physicalInverterIdentity(",
        "function usableSerialValue(",
        "function normalizeSerial(",
        "function reconcileTransportSelection(",
        "function resolveSelectedDeviceSource(",
        "function serialSelectedOverMqtt(",
    ):
        assert removed not in js, (
            f"{removed} is back in admin.js; physical identity and transport "
            "selection belong to ems/device_identity.py and "
            "admin/setup_planner.py"
        )


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
const configured = { kind: "zendure_mqtt", identity_status: "probable",
  physical_identity_token: "opaque:v1:scope-a",
  physical_identity_alias_tokens: ["opaque:v1:scope-a"],
  mqtt: { device_id: "…1234" } };
const mconfigState = {
  pristine: { devices: [configured] },
  draft: { devices: [configured] },
};
const same = { identity_status: "probable", physical_identity_token: "opaque:v1:scope-a",
  physical_identity_alias_tokens: ["opaque:v1:scope-a"],
  config_fragment: { mqtt: { device_id: "…1234" } } };
const other = { identity_status: "probable", physical_identity_token: "opaque:v1:scope-b",
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
    "issuedPhysicalIdentity",
    "issuedConnectionId",
    "issuedIdentityTokens",
    "isConfirmedIdentity",
    "inverterHasIdentity",
    "inverterIdentityConflict",
    "inverterIdentitiesMatch",
    "mqttSourceOfConnection",
    "mconfigIsMqttDevice",
    "connectionBrokerScope",
    "mconfigDeviceMqttSource",
    "mconfigDeviceConnectionSource",
    "mconfigSameMqttConnection",
    "mconfigProposalIdentityView",
    "mconfigDraftDevicesMatchingCandidate",
    "mconfigPristineHasCandidateConnection",
    "mconfigDraftHasProposal",
    "mconfigMqttProposalState",
)


def _proposal_state_helpers(js):
    return _constants(js) + "\n" + "\n".join(
        _extract_fn(js, name) for name in PROPOSAL_STATE_HELPER_NAMES
    )


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
const configured = { kind: "zendure_mqtt", identity_status: "probable",
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
  identity_status: "confirmed",
  physical_identity_token: "opaque:v1:serial-1",
  physical_identity_alias_tokens: ["opaque:v1:serial-1", "opaque:v1:route-1"],
  config_fragment: { mqtt: { device_id: "…1234" } },
};
// A different route entirely: independent new device.
const unrelated = {
  identity_status: "confirmed",
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
const configured = { kind: "zendure_mqtt", identity_status: "confirmed",
  physical_identity_token: "opaque:v1:serial-1",
  physical_identity_alias_tokens: ["opaque:v1:serial-1", "opaque:v1:route-1"],
  mqtt: { device_id: "…1234" } };
const mconfigState = {
  pristine: { devices: [configured] },
  draft: { devices: [configured] },
};
const routeOnly = {
  identity_status: "probable",
  physical_identity_token: "opaque:v1:route-1",
  physical_identity_alias_tokens: ["opaque:v1:route-1"],
  config_fragment: { mqtt: { device_id: "…1234" } },
};
// Same route, a DIFFERENT serial: contradiction, must be blocked.
const contradiction = {
  identity_status: "confirmed",
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
