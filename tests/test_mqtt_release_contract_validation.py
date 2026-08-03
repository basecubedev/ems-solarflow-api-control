# SPDX-License-Identifier: AGPL-3.0-or-later
"""Strict input and trust-boundary contract for the MQTT release.

Drives the real legacy broker-save API and the real proposal trust boundary
through the harness. Explicit invalid input is rejected (HTTP 400) with nothing
persisted; forged browser proposal fields never reach config; and no secret
appears in any public artifact.
"""

import json

import pytest

from tests.helpers.mqtt_release_contract import (
    ReleaseContractHarness,
    api_device_selection,
    broker_candidate,
    device_observation,
)

pytestmark = [
    pytest.mark.mqtt,
    pytest.mark.unit,
    pytest.mark.simulation,
]

BROKER_ENDPOINT = "/api/discovery/connections/mqtt-brokers"


@pytest.fixture(autouse=True)
def _isolate(isolated_install_root):
    return isolated_install_root


@pytest.fixture
def harness(tmp_path):
    with ReleaseContractHarness(tmp_path) as h:
        yield h


# --- 5.2 / 5.4 strict port validation on the legacy broker API --------------
@pytest.mark.parametrize(
    "port", [0, -1, 65536, 70000, "1883x", "broken", 1.5, [], {}, True]
)
def test_broker_save_rejects_invalid_port(harness, port):
    before = harness.snapshot_secrets_dir()
    status, payload = harness.request(
        BROKER_ENDPOINT,
        body={"host": "10.0.0.10", "port": port, "username": "u", "password": "p"},
    )
    assert status == 400, (port, payload)
    assert payload["error"] == "invalid_broker"
    # No secret file is created ahead of a rejected broker.
    assert harness.snapshot_secrets_dir() == before


@pytest.mark.parametrize("port", [1883, 8883])
def test_broker_save_accepts_valid_port(harness, port):
    status, payload = harness.request(
        BROKER_ENDPOINT, body={"host": "10.0.0.10", "port": port}
    )
    assert status == 200, payload


# --- 5.3 strict TLS validation ----------------------------------------------
@pytest.mark.parametrize("tls_mode", ["nonsense", "maybe", 5])
def test_broker_save_rejects_unknown_tls_mode(harness, tls_mode):
    status, payload = harness.request(
        BROKER_ENDPOINT, body={"host": "10.0.0.10", "port": 1883, "tls_mode": tls_mode}
    )
    assert status == 400
    assert payload["error"] == "invalid_broker"


@pytest.mark.parametrize(
    "tls_mode,expect_tls",
    [("plain", False), ("system_ca", True), ("insecure_no_verify", True)],
)
def test_broker_save_accepts_known_tls_modes(harness, tls_mode, expect_tls):
    status, payload = harness.request(
        BROKER_ENDPOINT, body={"host": "10.0.0.10", "port": None, "tls_mode": tls_mode}
    )
    assert status == 200, payload


# --- 5.4 incomplete credential pair rejected --------------------------------
@pytest.mark.parametrize(
    "username,password", [("u", None), (None, "p"), ("", "p"), ("u", "")]
)
def test_broker_save_rejects_incomplete_credentials(harness, username, password):
    before = harness.snapshot_secrets_dir()
    status, payload = harness.request(
        BROKER_ENDPOINT,
        body={"host": "10.0.0.10", "port": 1883, "username": username, "password": password},
    )
    assert status == 400 and payload["error"] == "invalid_broker"
    assert harness.snapshot_secrets_dir() == before


# --- 5.5 browser proposal forgery -------------------------------------------
def _discover_one(harness, serial="REAL"):
    harness.run_generation(
        [
            broker_candidate(
                "10.0.0.10",
                devices=[
                    device_observation(
                        serial,
                        topic_family="zensdk_ha_scalar",
                        metrics=["totalPower"],
                        topics=[f"Zendure/sensor/{serial}/totalPower"],
                    )
                ],
            )
        ]
    )
    return harness.proposal_for(serial)


@pytest.mark.parametrize(
    "forged",
    [
        {"serial_number": "FORGED"},
        {"device_id": "FORGED"},
        {"topic_family": "legacy_zendure_json"},
        {"broker_host": "10.9.9.9"},
        {"broker_port": 9999},
        {"seen_topics": ["Zendure/sensor/FORGED/totalPower"]},
    ],
)
def test_forged_proposal_fields_never_reach_config(harness, forged):
    # The browser is not authoritative for discovery evidence: a forged mutable
    # field is ignored (the trusted server value wins), so the selection still
    # resolves and applies, and the forged value never reaches the generated
    # config. The trusted D0 topic (derived from the trusted serial) is used.
    proposal = _discover_one(harness)
    selection = harness.selection(proposal, replace_grid_meter=True)
    selection.update(forged)
    status, payload = harness.apply(
        devices=[api_device_selection()], selections=[selection]
    )
    assert status == 200, payload
    blob = json.dumps(harness.applied_config())
    assert "Zendure/sensor/REAL/totalPower" in blob
    for sentinel in ("FORGED", "10.9.9.9", "9999"):
        assert sentinel not in blob


def test_unknown_broker_ref_selection_is_rejected(harness):
    proposal = _discover_one(harness)
    selection = {"id": proposal["id"], "broker_ref": "not-a-real-ref"}
    status, payload = harness.apply_untrusted(selections=[selection])
    assert status == 400


# --- 5.7 secret-redaction sweep ---------------------------------------------
def test_no_secret_appears_in_any_public_artifact(harness):
    tokens = ["PRIVATE_MQTT_TOKEN", "SECRET_USER_123"]
    harness.save_discovery_credential("home", "SECRET_USER_123", "PRIVATE_MQTT_TOKEN")
    proposal = _discover_one(harness, serial="D0-SWEEP")

    status, _ = harness.apply(
        devices=[api_device_selection()],
        selections=[harness.selection(proposal, replace_grid_meter=True)],
    )
    assert status == 200
    config = harness.applied_config()
    for device in config.get("devices", []):
        if device.get("type") == "zendure_mqtt":
            device["capabilities"] = {"write_output_limit": True}
    runtime = harness.start_runtime(config)

    harness.assert_no_secret(
        harness.public_secret_surface(),
        runtime.control_runtime.status(),
        runtime.telemetry_runtime.status(),
        harness.credential_store.mqtt_discovery_secret_status("home"),
        harness.credential_store.mqtt_broker_secret_status("home"),
        json.dumps([p["id"] for p in harness.trusted_proposals()]),
        tokens=tokens,
    )
