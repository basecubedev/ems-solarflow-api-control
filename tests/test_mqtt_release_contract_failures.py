# SPDX-License-Identifier: AGPL-3.0-or-later
"""Failure and rollback contract for the MQTT release lifecycle.

Every case drives the real Admin server + real credential store through the
harness, so the production promotion ordering and rollback are exercised, never
re-implemented. Focused tests, one boundary each.
"""

import json

import pytest

from admin.credential_store import CredentialStoreError
from admin.install_context import detect_install_context
from tests.helpers.mqtt_release_contract import (
    ReleaseContractHarness,
    broker_candidate,
    device_observation,
)

pytestmark = pytest.mark.simulation

SECRET = "SECOND_RELEASE_SECRET"
REF = "home"


@pytest.fixture(autouse=True)
def _isolate(isolated_install_root):
    return isolated_install_root


@pytest.fixture
def harness(tmp_path):
    with ReleaseContractHarness(tmp_path) as h:
        yield h


def _discover(harness):
    """One authenticated broker with a D0 grid meter and a control device."""

    harness.run_generation(
        [
            broker_candidate(
                "10.0.0.10",
                devices=[
                    device_observation(
                        "D0-F",
                        topic_family="zensdk_ha_scalar",
                        credentials_ref=REF,
                        metrics=["totalPower"],
                        topics=["Zendure/sensor/D0-F/totalPower"],
                    ),
                    device_observation(
                        "LEG-F",
                        topic_family="legacy_zendure_json",
                        device_id="DEV1",
                        product_key="PK1",
                        model_hint="SolarFlow 800 Pro 2",
                        credentials_ref=REF,
                        metrics=["outputLimit"],
                        topics=["Zendure/sensor/LEG-F/electricLevel"],
                    ),
                ],
            )
        ]
    )
    return [
        harness.selection(harness.proposal_for("D0-F"), replace_grid_meter=True),
        harness.selection(harness.proposal_for("LEG-F")),
    ]


def _install_config_exists():
    from pathlib import Path

    return Path(detect_install_context().config_path).exists()


# --- 3.1 promotion fails before config write --------------------------------
def test_apply_does_not_write_config_when_credential_promotion_fails(harness):
    selections = _discover(harness)
    # Never save the discovery secret: promotion must fail before any config write.
    status, payload = harness.apply(selections=selections)
    assert status == 400
    assert payload["reason"] == "credential_promotion_failed"
    assert not harness.runtime_credential_exists(REF)
    assert not _install_config_exists()
    harness.assert_no_secret(payload, tokens=[SECRET])


# --- 3.2 config write fails after staging -> rollback -----------------------
def test_write_rolls_back_new_runtime_credential_on_config_write_failure(harness):
    selections = _discover(harness)
    harness.save_discovery_credential(REF, "release-user", SECRET)

    def _boom(*args, **kwargs):
        raise OSError("disk full")

    harness.server.config_export.write = _boom
    status, payload = harness.write(selections=selections)
    assert status == 500 and payload["reason"] == "write_failed"
    # The staged runtime record must be rolled back; no orphan secret survives.
    assert not harness.runtime_credential_exists(REF)
    harness.assert_no_secret(payload, tokens=[SECRET])


# --- 3.3 pre-existing runtime credential preserved on failure ---------------
def test_preexisting_runtime_credential_is_never_rolled_back(harness):
    selections = _discover(harness)
    harness.save_discovery_credential(REF, "release-user", SECRET)
    # A pre-existing, equal runtime credential is reused, not created.
    harness.credential_store.save_mqtt_broker_secret(REF, "release-user", SECRET)

    def _boom(*args, **kwargs):
        raise OSError("disk full")

    harness.server.config_export.write = _boom
    status, _ = harness.write(selections=selections)
    assert status == 500
    # Reused (not newly created) => rollback must leave it intact.
    assert harness.runtime_credential_exists(REF)


# --- 3.4 repeated apply is idempotent ---------------------------------------
def test_apply_is_idempotent(harness):
    selections = _discover(harness)
    harness.save_discovery_credential(REF, "release-user", SECRET)

    status, _ = harness.apply(selections=selections)
    assert status == 200
    first = harness.applied_config()
    assert harness.runtime_credential_exists(REF)

    status, _ = harness.apply(selections=selections)
    assert status == 200
    second = harness.applied_config()

    assert first == second
    assert len(second["zendure_mqtt"]["brokers"]) == 1


# --- 3.5 preview and download are side-effect free --------------------------
def test_preview_and_download_have_no_secret_or_config_side_effects(harness):
    selections = _discover(harness)
    harness.save_discovery_credential(REF, "release-user", SECRET)
    before = harness.snapshot_secrets_dir()

    status, _ = harness.preview(selections=selections)
    assert status == 200
    status, _ = harness.download(selections=selections)
    assert status == 200

    assert harness.snapshot_secrets_dir() == before
    assert not harness.runtime_credential_exists(REF)
    assert not _install_config_exists()


# --- 3.6 runtime rejects a missing credentials_ref (no anonymous fallback) ---
def test_runtime_rejects_missing_credentials_ref(harness):
    config = {
        "system": {"enabled": True, "max_total_power": 1600},
        "dry_run": False,
        "zendure_mqtt": {
            "enabled": True,
            "brokers": {
                REF: {
                    "enabled": True,
                    "source": "local_mqtt",
                    "host": "10.0.0.10",
                    "port": 1883,
                    "credentials_ref": "absent-ref",
                }
            },
        },
        "devices": [
            {
                "type": "zendure_mqtt",
                "name": "LEG",
                "serial_number": "LEG",
                "mqtt": {
                    "broker_ref": REF,
                    "topic_family": "legacy_zendure_json",
                    "device_id": "DEV1",
                    "product_key": "PK1",
                    "base_topic": "iot",
                },
                "capabilities": {"write_output_limit": True},
            }
        ],
        "grid_meter": {"type": "shelly", "ip": "192.0.2.3"},
    }
    runtime = harness.start_runtime(config)
    status = runtime.control_runtime.status()
    # No service is created and no device is accepted -> no anonymous fallback.
    assert status["accepted_control_devices"] == 0
    assert status["service_count"] == 0
    assert any(r["name"] == "LEG" for r in status["rejected"])
    harness.assert_no_secret(status, tokens=["absent-ref"])


# --- 3.8 incomplete credentials are rejected at every layer ------------------
@pytest.mark.parametrize(
    "username,password",
    [("user", None), (None, "password"), ("", "password"), ("user", "")],
)
def test_incomplete_credentials_are_rejected_at_save(harness, username, password):
    with pytest.raises(CredentialStoreError, match="both be non-empty"):
        harness.save_discovery_credential("partial", username, password)
    with pytest.raises(CredentialStoreError, match="both be non-empty"):
        harness.credential_store.save_mqtt_broker_secret("partial", username, password)


def test_runtime_credential_leak_absent_from_status(harness):
    # A resolvable authenticated broker must never echo its secret into status.
    selections = _discover(harness)
    harness.save_discovery_credential(REF, "release-user", SECRET)
    status, _ = harness.apply(selections=selections)
    assert status == 200
    config = harness.applied_config()
    for device in config["devices"]:
        if device.get("type") == "zendure_mqtt":
            device["capabilities"] = {"write_output_limit": True}
    runtime = harness.start_runtime(config)
    blob = json.dumps(
        [runtime.control_runtime.status(), runtime.telemetry_runtime.status()]
    )
    assert SECRET not in blob and "release-user" not in blob
