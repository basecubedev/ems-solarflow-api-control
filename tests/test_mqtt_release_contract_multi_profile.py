# SPDX-License-Identifier: AGPL-3.0-or-later
"""Discovery-generation and multi-profile contract for the MQTT release.

Covers per-broker generation validity (a partial refresh must not keep a
down broker's devices selectable), proposal TTL, generation invalidation, and
two credential profiles on one endpoint staying isolated all the way into the
runtime. Deterministic: the discovery clock is injected, no real sleeps.
"""

import json

import pytest

from tests.helpers import payloads
from tests.helpers.mqtt_release_contract import (
    ReleaseContractHarness,
    broker_candidate,
    device_observation,
)

pytestmark = [
    pytest.mark.mqtt,
    pytest.mark.unit,
    pytest.mark.simulation,
    pytest.mark.power_control,
]


@pytest.fixture(autouse=True)
def _isolate(isolated_install_root):
    return isolated_install_root


@pytest.fixture
def harness(tmp_path):
    with ReleaseContractHarness(tmp_path) as h:
        yield h


def _dev(serial, **kw):
    return device_observation(
        serial,
        topic_family="zensdk_ha_scalar",
        metrics=["totalPower"],
        topics=[f"Zendure/sensor/{serial}/totalPower"],
        **kw,
    )


# --- 4.2 new generation invalidates an old proposal -------------------------
def test_new_discovery_generation_invalidates_old_proposal(harness):
    harness.run_generation([broker_candidate("10.0.0.10", devices=[_dev("A1")])])
    stale = harness.selection(harness.proposal_for("A1"))
    assert "g1" in stale["id"]

    # Generation 2 rediscovers the same device with a current proposal id.
    harness.run_generation([broker_candidate("10.0.0.10", devices=[_dev("A1")])])
    current = harness.selection(harness.proposal_for("A1"))
    assert "g2" in current["id"] and current["id"] != stale["id"]

    # The stale generation-1 selection is rejected server-side.
    status, payload = harness.apply_untrusted(selections=[stale])
    assert status == 400
    assert "discovery" in payload["error"].lower() or "not present" in payload["error"].lower()


# --- 4.3 proposal TTL expires (injected clock, no sleep) --------------------
def test_proposal_ttl_expires(tmp_path, isolated_install_root):
    with ReleaseContractHarness(tmp_path, proposal_ttl_seconds=900) as harness:
        harness.run_generation([broker_candidate("10.0.0.10", devices=[_dev("A1")])])
        assert harness.proposal_for("A1") is not None
        harness.clock.advance(901)
        assert harness.trusted_proposals() == []


# --- 4.4 partial multi-broker refresh (mandatory) ---------------------------
def test_partial_multi_broker_refresh_keeps_only_reachable_broker(harness):
    # Generation 1: both brokers reachable, each with its own device.
    harness.run_generation(
        [
            broker_candidate("10.0.0.10", devices=[_dev("DEV-A")]),
            broker_candidate("10.0.0.20", devices=[_dev("DEV-B")]),
        ]
    )
    assert harness.proposal_for("DEV-A") is not None
    assert harness.proposal_for("DEV-B") is not None

    # Generation 2: broker A reachable, broker B unreachable.
    harness.run_generation(
        [
            broker_candidate("10.0.0.10", devices=[_dev("DEV-A")]),
            broker_candidate("10.0.0.20", devices=[_dev("DEV-B")], reachable=False),
        ]
    )
    # Broker A's device stays selectable; broker B's does not. Broker A's global
    # success never validates broker B.
    assert harness.proposal_for("DEV-A") is not None
    assert harness.proposal_for("DEV-B") is None


# --- 4.5 topic refresh failure on one broker --------------------------------
def test_topic_refresh_failure_on_one_broker_drops_only_that_broker(harness):
    harness.run_generation(
        [
            broker_candidate("10.0.0.10", devices=[_dev("DEV-A")]),
            broker_candidate(
                "10.0.0.20", devices=[_dev("DEV-B")], topic_refresh_success=False
            ),
        ]
    )
    assert harness.proposal_for("DEV-A") is not None
    assert harness.proposal_for("DEV-B") is None


# --- 4.7 failed broker recovers in a later generation -----------------------
def test_failed_broker_recovers_in_later_generation(harness):
    harness.run_generation(
        [
            broker_candidate("10.0.0.10", devices=[_dev("DEV-A")]),
            broker_candidate("10.0.0.20", devices=[_dev("DEV-B")], reachable=False),
        ]
    )
    assert harness.proposal_for("DEV-B") is None

    harness.run_generation(
        [
            broker_candidate("10.0.0.10", devices=[_dev("DEV-A")]),
            broker_candidate("10.0.0.20", devices=[_dev("DEV-B")]),
        ]
    )
    recovered = harness.proposal_for("DEV-B")
    assert recovered is not None and "g2" in recovered["id"]


# --- 4.8 same endpoint, two credential profiles -----------------------------
def test_same_endpoint_two_credentials_are_distinct_profiles(harness):
    # One endpoint, two devices each seen through a different credential pool.
    harness.run_generation(
        [
            broker_candidate(
                "10.0.0.20",
                devices=[
                    _dev("DEV-A", credentials_ref="account-a"),
                    _dev("DEV-B", credentials_ref="account-b"),
                ],
            )
        ]
    )
    a = harness.proposal_for("DEV-A")
    b = harness.proposal_for("DEV-B")
    assert a is not None and b is not None
    # Distinct connection-profile refs; neither falls back to the other's account.
    assert a["broker_ref"] != b["broker_ref"]
    assert a["credentials_ref"] == "account-a"
    assert b["credentials_ref"] == "account-b"


# --- 4.10 topology expansion does not rename an existing profile ------------
def test_topology_expansion_keeps_existing_broker_ref(harness):
    harness.run_generation([broker_candidate("10.0.0.10", devices=[_dev("A1")])])
    ref_alone = harness.proposal_for("A1")["broker_ref"]

    harness.run_generation(
        [
            broker_candidate("10.0.0.10", devices=[_dev("A1")]),
            broker_candidate("10.0.0.30", devices=[_dev("C1")]),
        ]
    )
    assert harness.proposal_for("A1")["broker_ref"] == ref_alone


# --- 4.8 runtime: two credential profiles isolated at runtime ---------------
def _legacy_device(name, broker_ref, device_id, product_key):
    return {
        "type": "zendure_mqtt",
        "name": name,
        "serial_number": name,
        "hardware_profile": "solarflow_800_pro_2",
        "mqtt": {
            "broker_ref": broker_ref,
            "topic_family": "legacy_zendure_json",
            "device_id": device_id,
            "product_key": product_key,
            "base_topic": "iot",
        },
        "capabilities": {"write_output_limit": True},
    }


def test_two_credential_profiles_isolated_at_runtime(harness):
    # Two profiles on the same endpoint, distinct credentials_ref -> two runtime
    # services, isolated reads and writes, each with its own resolved secret.
    harness.credential_store.save_mqtt_broker_secret("account-a", "user-a", "SECRET-A")
    harness.credential_store.save_mqtt_broker_secret("account-b", "user-b", "SECRET-B")
    config = {
        "system": {"enabled": True, "max_total_power": 3200},
        "dry_run": False,
        "zendure_mqtt": {
            "enabled": True,
            "brokers": {
                "acct_a": {
                    "enabled": True, "source": "local_mqtt", "host": "10.0.0.20",
                    "port": 1883, "credentials_ref": "account-a",
                },
                "acct_b": {
                    "enabled": True, "source": "local_mqtt", "host": "10.0.0.20",
                    "port": 1883, "credentials_ref": "account-b",
                },
            },
        },
        "devices": [
            _legacy_device("A", "acct_a", "DEVA", "PKA"),
            _legacy_device("B", "acct_b", "DEVB", "PKB"),
        ],
        "grid_meter": {"type": "shelly", "ip": "192.0.2.3"},
    }
    runtime = harness.start_runtime(config)
    # Two distinct broker services, one per profile.
    assert len(runtime.control_runtime.services) == 2

    runtime.inject("acct_a", "iot/PKA/DEVA/properties/report",
                   payloads.legacy_json_report(device_id="DEVA", serial="A"))
    runtime.inject("acct_b", "iot/PKB/DEVB/properties/report",
                   payloads.legacy_json_report(device_id="DEVB", serial="B"))
    runtime.run_cycle()

    writes_a = runtime.broker("acct_a").write_topics
    writes_b = runtime.broker("acct_b").write_topics
    assert writes_a == ["iot/PKA/DEVA/properties/write"]
    assert writes_b == ["iot/PKB/DEVB/properties/write"]
    # No write crosses profiles.
    assert "DEVB" not in " ".join(writes_a)
    assert "DEVA" not in " ".join(writes_b)

    # Each broker's client authenticated with its own resolved secret, no leak.
    creds_a = runtime.broker("acct_a").captured_credentials()
    creds_b = runtime.broker("acct_b").captured_credentials()
    assert ("user-a", "SECRET-A") in creds_a
    assert ("user-b", "SECRET-B") in creds_b
    blob = json.dumps(
        [runtime.control_runtime.status(), runtime.telemetry_runtime.status()]
    )
    assert "SECRET-A" not in blob and "SECRET-B" not in blob
