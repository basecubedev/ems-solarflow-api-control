# SPDX-License-Identifier: AGPL-3.0-or-later
"""How Maintenance labels a configured connection and refreshes after a change.

Three behaviors are pinned here:

- An unresolved MQTT source stays unknown. The shared ``mqttSourceOfConnection``
  normalizer folds every unrecognized value to ``local_mqtt`` (correct for a
  discovered proposal, which always states its source), so the configured-device
  path must not feed an empty source into it — a Cloud device whose source could
  not be resolved must never be labeled "MQTT".
- An alias matching more than one draft device names no inverter at all.
- Every draft mutation that changes discovered hardware rebuilds the discovery
  review from the retained session instead of patching the clicked button.

The real admin.js helpers are extracted (brace-matched) and executed in node.
"""

import json
import os
import shutil
import subprocess

import pytest

pytestmark = pytest.mark.simulation

STATIC_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "admin", "static"
)


def _read():
    with open(os.path.join(STATIC_DIR, "admin.js"), encoding="utf-8") as handle:
        return handle.read()


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


def _node(script):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for the configured connection tests")
    result = subprocess.run(
        [node, "-e", script], text=True, capture_output=True, check=False
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


_HELPERS = (
    "issuedPhysicalIdentity",
    "issuedConnectionId",
    "issuedIdentityTokens",
    "isConfirmedIdentity",
    "inverterHasIdentity",
    "inverterIdentityConflict",
    "inverterIdentitiesMatch",
    "normalizeInverterAliasTokens",
    "mqttSourceOfConnection",
    "connectionLabelFor",
    "mconfigIsMqttDevice",
    "connectionBrokerScope",
    "mconfigDeviceMqttSource",
    "mconfigDeviceConnectionSource",
    "mconfigProposalIdentityView",
    "mconfigDraftDevicesMatchingCandidate",
    "mconfigConfiguredDeviceForCandidate",
)


def _run(device_action, proposals=(), devices=()):
    js = _read()
    helpers = "\n".join(_extract_fn(js, name) for name in _HELPERS)
    stub = (
        "function maintenanceMqttProposals() { return "
        + json.dumps(list(proposals))
        + "; }\n"
        "const mconfigState = { pristine: null, draft: { devices: "
        + json.dumps(list(devices))
        + " } };\n"
    )
    return _node(helpers + "\n" + stub + device_action)


def _connection(device, proposals=()):
    return _run(
        "console.log(JSON.stringify({\n"
        "  source: mconfigDeviceConnectionSource(" + json.dumps(device) + "),\n"
        "  label: connectionLabelFor(mconfigDeviceConnectionSource("
        + json.dumps(device)
        + ")),\n"
        "}));",
        proposals=proposals,
    )


# --- Configured source resolution -----------------------------------------


def test_backend_resolved_cloud_source_labels_zendure_mqtt_before_discovery():
    """14. Device source omitted, broker profile is zendure_cloud_mqtt."""

    device = {
        "kind": "zendure_mqtt",
        "mqtt": {"broker_ref": "cloud_a", "effective_source": "zendure_cloud_mqtt"},
    }
    assert _connection(device) == {"source": "zendure_mqtt", "label": "Zendure MQTT"}


def test_backend_resolved_local_source_labels_mqtt_before_discovery():
    """15. Device source omitted, broker profile is local_mqtt."""

    device = {
        "kind": "zendure_mqtt",
        "mqtt": {"broker_ref": "local_b1", "effective_source": "local_mqtt"},
    }
    assert _connection(device) == {"source": "local_mqtt", "label": "MQTT"}


def test_unresolved_source_never_claims_local_mqtt():
    """16. No stated source, no resolved broker source, no matching proposal."""

    device = {"kind": "zendure_mqtt", "mqtt": {"broker_ref": "unknown_ref"}}
    assert _connection(device) == {"source": "", "label": "Unknown"}


def test_device_without_any_broker_reference_stays_unknown():
    device = {"kind": "zendure_mqtt", "mqtt": {}}
    assert _connection(device) == {"source": "", "label": "Unknown"}


def test_stated_device_source_still_wins():
    device = {
        "kind": "zendure_mqtt",
        "mqtt": {
            "broker_ref": "cloud_a",
            "source": "zendure_cloud_mqtt",
            "effective_source": "zendure_cloud_mqtt",
        },
    }
    assert _connection(device) == {"source": "zendure_mqtt", "label": "Zendure MQTT"}


def test_current_proposals_still_resolve_a_source_for_a_known_broker_ref():
    """The browser fallback stays: a trusted proposal names its broker's source."""

    device = {"kind": "zendure_mqtt", "mqtt": {"broker_ref": "cloud_a"}}
    proposals = [{"broker_ref": "cloud_a", "connection_source": "zendure_cloud_mqtt"}]
    assert _connection(device, proposals=proposals) == {
        "source": "zendure_mqtt",
        "label": "Zendure MQTT",
    }


def test_local_api_device_is_unaffected():
    assert _connection({"kind": "local_api", "ip": "192.168.1.100"}) == {
        "source": "local_api",
        "label": "API",
    }


# --- Ambiguous alias names no inverter ------------------------------------


def _candidate_owner(devices, proposal):
    return _run(
        "console.log(JSON.stringify(mconfigConfiguredDeviceForCandidate("
        + json.dumps({"mqttProposal": proposal, "state": "transport"})
        + ")));",
        devices=devices,
    )


def test_single_matching_draft_device_owns_the_candidate():
    device = {
        "kind": "zendure_mqtt",
        "name": "INV_1",
        "serial_number": "PHYS-1",
        "physical_device_id": "opaque:v1:PHYS-1",
        "identity_status": "confirmed",
        "mqtt": {"broker_ref": "local_b1", "source": "local_mqtt"},
    }
    owner = _candidate_owner([device], {"physical_device_id": "opaque:v1:PHYS-1"})
    assert owner is not None and owner["name"] == "INV_1"


def test_ambiguous_alias_names_no_configured_inverter():
    """17. No relationship note may pick the first of two matching devices."""

    first = {
        "kind": "zendure_mqtt",
        "name": "INV_1",
        "physical_identity_token": "opaque:v1:aliasA",
        "physical_identity_alias_tokens": ["opaque:v1:aliasA"],
    }
    second = {
        "kind": "zendure_mqtt",
        "name": "INV_2",
        "physical_identity_token": "opaque:v1:aliasB",
        "physical_identity_alias_tokens": ["opaque:v1:aliasB"],
    }
    proposal = {
        "physical_identity_token": "opaque:v1:aliasA",
        "physical_identity_alias_tokens": ["opaque:v1:aliasA", "opaque:v1:aliasB"],
    }
    assert _candidate_owner([first, second], proposal) is None


# --- Discovery review refresh ---------------------------------------------


_MUTATORS = (
    "mconfigSwitchInverterTransport",
    "mconfigAddZendureMqttProposal",
    "mconfigAddDiscovered",
)


def test_every_hardware_draft_mutation_rebuilds_the_discovery_review():
    """10-12. One renderer refreshes every card, note and count after a change."""

    js = _read()
    for name in _MUTATORS:
        assert "mconfigRerenderDiscoveryReview()" in _extract_fn(js, name), name


def test_rerender_uses_the_retained_session_without_a_network_request():
    """13. The refresh reads the retained discovery state; it starts no scan."""

    body = _extract_fn(_read(), "mconfigRerenderDiscoveryReview")
    assert "discoverySessions.maintenance" in body
    assert "buildMaintenanceDiscoveryReview(" in body
    assert "renderMaintenanceDiscoveryReview(" in body
    for forbidden in ("fetch(", "discoveryFetch(", "startMaintenanceDiscovery"):
        assert forbidden not in body, forbidden


def test_candidate_actions_do_not_leave_a_hand_patched_button_behind():
    """The rerendered card is the final UI state, not a mutated stale button."""

    js = _read()
    for name in ("renderMaintenanceMqttProposalCard", "renderMaintenanceDiscoveryCard"):
        body = _extract_fn(js, name)
        assert '"Connection selected"' not in body, name
