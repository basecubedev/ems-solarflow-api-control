# SPDX-License-Identifier: AGPL-3.0-or-later
"""Maintenance discovery-review transport handling.

These tests drive the real admin.js maintenance helpers that build and label the
discovery review for an EXISTING config. Maintenance has no discovery priority
(unlike Fresh Install); every discovered device is an independent, manually
addable row. Two behaviors are pinned here:

- A configured Zendure MQTT device must NOT be mis-rendered as a phantom
  "missing inverter" row. buildMaintenanceDiscoveryReview iterates the draft
  devices through the Local-API-only matcher (keyed on sn/ip); an MQTT-kind
  device never carries sn/ip, so without a guard it is pushed as a bogus
  {role:"inverter", state:"missing"} row.
- A local-MQTT proposal must be labeled as its own transport ("Local MQTT"),
  not collapsed into the hardcoded "zendure mqtt" bucket.
"""

import json
import os
import shutil
import subprocess

import pytest

pytestmark = [
    pytest.mark.admin,
    pytest.mark.maintenance,
    pytest.mark.mqtt,
    pytest.mark.contract,
    pytest.mark.simulation,
]

STATIC_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "admin", "static"
)


def _read(name="admin.js"):
    with open(os.path.join(STATIC_DIR, name), encoding="utf-8") as handle:
        return handle.read()


def _extract_fn(js, name):
    marker = "function " + name
    assert marker in js, f"{name} is missing from admin.js"
    idx = js.index(marker)
    prefix = "async " if js[idx - 6 : idx] == "async " else ""
    body = js[idx:].split("\nfunction ", 1)[0]
    return prefix + body


def _run(names, setup):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for the maintenance review behavior tests")
    js = _read()
    helpers = "\n".join(_extract_fn(js, name) for name in names)
    result = subprocess.run(
        [node, "-e", helpers + "\n" + setup], text=True, capture_output=True, check=False
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


_REVIEW_STUBS = """
const mconfigState = {
  draft: {
    devices: [
      { kind: "zendure_mqtt", serial_number: "S1", name: "mqtt1" },
      { kind: "local_api", sn: "H1", ip: "1.2.3.4", name: "http1" },
    ],
    grid_meter: { present: false },
  },
};
function isConfigCandidate(d) { return true; }
function observationKey(d) { return (d && (d.observation_id || d.id)) || ""; }
function mconfigIdentity(v) { return String(v == null ? "" : v).trim().toLowerCase(); }
function mconfigDiscoveryRole(d) { return "inverter"; }
function mconfigFindInverterMatch() { return null; }
function maintenanceMqttProposals() { return []; }
function mconfigMqttProposalState() { return "new"; }
"""


def test_configured_mqtt_device_is_not_a_phantom_missing_inverter():
    results = _run(
        ("mconfigIsMqttDevice", "buildMaintenanceDiscoveryReview"),
        _REVIEW_STUBS
        + "console.log(JSON.stringify(buildMaintenanceDiscoveryReview([])));",
    )
    missing_inverters = [
        r for r in results if r.get("state") == "missing" and r.get("role") == "inverter"
    ]
    # The configured Local-API device legitimately reports missing; the MQTT
    # device must never produce a missing-inverter row.
    for row in missing_inverters:
        assert (row.get("configured") or {}).get("kind") != "zendure_mqtt"
    assert any(
        (r.get("configured") or {}).get("sn") == "H1" for r in missing_inverters
    ), "the Local-API device should still report missing"


def test_maintenance_review_role_reflects_mqtt_transport():
    js = _read()
    fn = _extract_fn(js, "buildMaintenanceDiscoveryReview")
    # The review item role must derive from the proposal transport, not be frozen
    # to zendure_mqtt for every MQTT proposal.
    assert 'role: "zendure_mqtt"' not in fn
    assert "mqttSourceOfConnection(" in fn


def test_maintenance_mqtt_card_labels_transport_from_source():
    js = _read()
    card = _extract_fn(js, "renderMaintenanceMqttProposalCard")
    # The transport pill and data-connection must reflect the real transport
    # (Local MQTT vs Zendure Cloud MQTT), not a hardcoded "zendure mqtt".
    assert 'transportPill.textContent = "zendure mqtt"' not in card
    assert 'card.dataset.connection = "zendure_mqtt"' not in card
    assert "mqttTransportLabel(proposal)" in card
    assert "mqttSourceOfConnection(proposal.connection_source)" in card
    assert "card.dataset.connection = transportSource" in card
    # data-role carries the hardware role, never the transport.
    assert "card.dataset.role = hardwareRole" in card


# --- Manual local MQTT entry (Part C) --------------------------------------


def test_derive_local_broker_ref_is_canonical_and_local():
    from admin.credential_store import CredentialStore

    cases = {
        "": "local_mqtt",
        "local_mqtt": "local_mqtt",
        "myhome": "local_mqtt_myhome",
        "default": "local_mqtt_default",
        "local_mqtt_home": "local_mqtt_home",
        "My Home!": "local_mqtt_myhome",
        "   ": "local_mqtt",
        # Trailing/leading "_"/"-" must be stripped so the derived ref survives
        # the backend credential-ref normalization unchanged.
        "home_": "local_mqtt_home",
        "home-": "local_mqtt_home",
        "_home_": "local_mqtt_home",
        "local_mqtt_": "local_mqtt",
        "a__b": "local_mqtt_a__b",
    }
    out = _run(
        ("deriveLocalBrokerRef",),
        "const cases = " + json.dumps(list(cases)) + ";"
        "console.log(JSON.stringify(cases.map((c) => deriveLocalBrokerRef(c))));",
    )
    assert out == list(cases.values())
    for ref in out:
        # Always provisionable (local_mqtt or local_mqtt_*), never the reserved
        # "default", and canonical so the mint-response id matches the ref (a
        # mismatch drops the device and orphans the staged secret).
        assert ref == "local_mqtt" or ref.startswith("local_mqtt_")
        assert ref != "default"
        assert CredentialStore.normalize_ref(ref) == ref


def test_manual_broker_block_reads_fields_and_defaults_port():
    def _block(fields):
        stubs = "\n".join(
            f'  {k}: {{ value: {json.dumps(v)} }},' for k, v in fields.items()
        )
        return _run(
            ("deriveLocalBrokerRef", "mconfigManualBrokerBlock"),
            "const mconfigEls = {\n" + stubs + "\n};\n"
            "const b = mconfigManualBrokerBlock();\n"
            "console.log(JSON.stringify(b));",
        )

    # No host -> no broker block (backward-compatible bare device).
    assert (
        _block(
            {
                "maintenanceManualBrokerHost": "",
                "maintenanceManualBrokerName": "",
                "maintenanceManualBrokerPort": "",
                "maintenanceManualBrokerSecurity": "plain",
                "maintenanceManualBrokerUsername": "",
                "maintenanceManualBrokerPassword": "",
            }
        )
        is None
    )

    # Auth broker with a blank port defaults to 1883 (plain) and reports auth.
    auth = _block(
        {
            "maintenanceManualBrokerHost": "192.168.1.20",
            "maintenanceManualBrokerName": "home",
            "maintenanceManualBrokerPort": "",
            "maintenanceManualBrokerSecurity": "plain",
            "maintenanceManualBrokerUsername": "u",
            "maintenanceManualBrokerPassword": "p",
        }
    )
    assert auth["ref"] == "local_mqtt_home"
    assert auth["host"] == "192.168.1.20"
    assert auth["port"] == 1883
    assert auth["tls"] is False
    assert auth["hasAuth"] is True
    assert auth["authPartial"] is False

    # TLS with a blank port defaults to 8883; username-only is a partial auth.
    partial = _block(
        {
            "maintenanceManualBrokerHost": "10.0.0.5",
            "maintenanceManualBrokerName": "",
            "maintenanceManualBrokerPort": "",
            "maintenanceManualBrokerSecurity": "tls",
            "maintenanceManualBrokerUsername": "u",
            "maintenanceManualBrokerPassword": "",
        }
    )
    assert partial["port"] == 8883
    assert partial["tls"] is True
    assert partial["hasAuth"] is False
    assert partial["authPartial"] is True


def test_manual_mqtt_add_attaches_broker_and_mints_without_leaking_secret():
    js = _read()
    fn = _extract_fn(js, "addManualMaintenanceMqttDevice")
    # The manual add reads a broker block, mints typed creds through the existing
    # discovery credential endpoint, and attaches a broker block + broker_ref.
    assert "mconfigManualBrokerBlock()" in fn
    assert "/api/discovery/connections/mqtt-credentials" in fn
    assert "device.broker" in fn
    assert "broker_ref" in fn
    # The typed password must never ride on the draft device (only in the mint).
    assert "device.broker.password" not in fn
    assert "_password" not in fn.split("device.broker", 1)[1]


def test_manual_mqtt_add_execution_omits_credentials_from_device():
    # Executes the real add path with a stubbed mint and asserts the produced
    # draft device carries the broker endpoint + resolved credentials_ref but
    # NEVER the typed username/password (which must ride only on the mint call).
    out = _run(
        (
            "deriveLocalBrokerRef",
            "mconfigManualBrokerBlock",
            "mconfigManualBrokerError",
            "addManualMaintenanceMqttDevice",
        ),
        """
const mconfigState = { loaded: true, draft: { devices: [] } };
const mconfigEls = {
  maintenanceManualBrokerHost: { value: "192.168.1.20" },
  maintenanceManualBrokerName: { value: "home" },
  maintenanceManualBrokerPort: { value: "1883" },
  maintenanceManualBrokerSecurity: { value: "plain" },
  maintenanceManualBrokerUsername: { value: "SEKRETUSER" },
  maintenanceManualBrokerPassword: { value: "SEKRETPASS" },
  addMqttDevice: { disabled: false },
  discoveryStatus: { textContent: "" },
  discoveryError: { hidden: true, textContent: "" },
  maintenanceManualBrokerForm: { reset() {} },
};
let mintBody = null;
function discoveryContextFor() { return "maintenance"; }
function discoveryFetch(url, init) {
  mintBody = JSON.parse(init.body);
  return Promise.resolve({
    ok: true,
    json: () => Promise.resolve({ local_mqtt: { credentials: [{ id: "local_mqtt_home" }] } }),
  });
}
function mconfigAddZendureMqttDevice() {
  mconfigState.draft.devices.push({ kind: "zendure_mqtt" });
}
function mconfigMarkDraftChanged() {}
function renderMaintenanceInverters() {}
function mconfigRerenderDiscoveryReview() {}
addManualMaintenanceMqttDevice().then(() => {
  const dev = mconfigState.draft.devices[mconfigState.draft.devices.length - 1];
  console.log(JSON.stringify({ dev, mintBody }));
});
""",
    )
    dev = out["dev"]
    # The broker endpoint + resolved ref are attached.
    assert dev["broker"]["credentials_ref"] == "local_mqtt_home"
    assert dev["broker"]["host"] == "192.168.1.20"
    assert dev["mqtt"]["broker_ref"] == "local_mqtt_home"
    # The typed secret is only in the mint request, never on the device.
    serialized = json.dumps(dev)
    assert "SEKRETPASS" not in serialized
    assert "SEKRETUSER" not in serialized
    assert "username" not in dev["broker"]
    assert "password" not in dev["broker"]
    assert out["mintBody"]["password"] == "SEKRETPASS"


def test_maintenance_manual_broker_form_present_in_html():
    html = _read("index.html")
    manual = html.split('id="maintenance-manual"', 1)[1].split("</details>", 1)[0]
    for field in (
        "maintenance-manual-mqtt-broker-name",
        "maintenance-manual-mqtt-broker-host",
        "maintenance-manual-mqtt-broker-port",
        "maintenance-manual-mqtt-broker-security",
        "maintenance-manual-mqtt-broker-username",
        "maintenance-manual-mqtt-broker-password",
    ):
        assert field in manual, field
