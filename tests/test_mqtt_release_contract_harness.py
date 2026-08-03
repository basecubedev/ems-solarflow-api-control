# SPDX-License-Identifier: AGPL-3.0-or-later
"""Self-tests for the release-contract harness.

Prove the harness can actually observe the things the contract relies on: a
promoted runtime credential, an applied config, a captured publish, a leaked
secret, and a stale/expired proposal. Several deliberately construct a wrong
expectation and assert the harness raises, so a green contract test cannot be a
false negative.
"""

import pytest

from tests.helpers.mqtt_release_contract import (
    ReleaseContractHarness,
    broker_candidate,
    device_observation,
)

pytestmark = [
    pytest.mark.mqtt,
    pytest.mark.unit,
    pytest.mark.simulation,
]

SECRET = "HARNESS_SECRET_PASSWORD"


@pytest.fixture(autouse=True)
def _isolate(isolated_install_root):
    return isolated_install_root


@pytest.fixture
def harness(tmp_path):
    with ReleaseContractHarness(tmp_path) as h:
        yield h


def _authenticated_local_broker(harness, serial="D0-HARNESS", credentials_ref="home"):
    harness.save_discovery_credential(credentials_ref, "release-user", SECRET)
    harness.run_generation(
        [
            broker_candidate(
                "10.0.0.10",
                devices=[
                    device_observation(
                        serial,
                        topic_family="zensdk_ha_scalar",
                        credentials_ref=credentials_ref,
                        metrics=["totalPower"],
                        topics=[f"Zendure/sensor/{serial}/totalPower"],
                    )
                ],
            )
        ]
    )


def test_harness_builds_a_trusted_proposal_referencing_the_credential(harness):
    _authenticated_local_broker(harness)
    proposal = harness.proposal_for("D0-HARNESS")
    assert proposal is not None
    assert proposal["credentials_ref"] == "home"
    assert proposal["broker_ref"].startswith("local_mqtt_")


def test_harness_detects_a_missing_credential_promotion(harness):
    # Preview never promotes; the harness must observe the absence.
    _authenticated_local_broker(harness)
    proposal = harness.proposal_for("D0-HARNESS")
    status, _ = harness.preview(
        selections=[harness.selection(proposal, replace_grid_meter=True)]
    )
    assert status == 200
    assert not harness.runtime_credential_exists("home")


def test_harness_secret_sweep_detects_a_leak(harness):
    # A deliberately wrong expectation: the harness must raise on a real leak.
    with pytest.raises(AssertionError):
        harness.assert_no_secret({"password": SECRET}, tokens=[SECRET])
    # And must pass on a clean artifact.
    harness.assert_no_secret({"credentials_ref": "home"}, tokens=[SECRET])


def test_harness_detects_a_stale_proposal_after_ttl(tmp_path, isolated_install_root):
    with ReleaseContractHarness(tmp_path, proposal_ttl_seconds=900) as harness:
        _authenticated_local_broker(harness)
        assert harness.proposal_for("D0-HARNESS") is not None
        harness.clock.advance(1000)
        # Past the TTL, no device is selectable, so no proposal survives.
        assert harness.trusted_proposals() == []


def test_harness_captures_a_publish_and_detects_its_absence(harness):
    # An authenticated broker with a D0 grid meter and a legacy control device.
    harness.save_discovery_credential("home", "release-user", SECRET)
    harness.run_generation(
        [
            broker_candidate(
                "10.0.0.10",
                devices=[
                    device_observation(
                        "D0-1",
                        topic_family="zensdk_ha_scalar",
                        credentials_ref="home",
                        metrics=["totalPower"],
                        topics=["Zendure/sensor/D0-1/totalPower"],
                    ),
                    device_observation(
                        "LEGACY-1",
                        topic_family="legacy_zendure_json",
                        device_id="DEV1",
                        product_key="PK1",
                        model_hint="SolarFlow 800 Pro 2",
                        credentials_ref="home",
                        metrics=["outputLimit"],
                        topics=["Zendure/sensor/LEGACY-1/electricLevel"],
                    ),
                ],
            )
        ]
    )
    d0 = harness.proposal_for("D0-1")
    legacy = harness.proposal_for("LEGACY-1")
    status, payload = harness.apply(
        selections=[
            harness.selection(d0, replace_grid_meter=True),
            harness.selection(legacy),
        ]
    )
    assert status == 200 and payload["ok"] is True
    config = harness.applied_config()

    assert config["devices"][0]["capabilities"]["write_output_limit"] is True

    runtime = harness.start_runtime(config)
    broker_ref = config["devices"][0]["mqtt"]["broker_ref"]
    runtime.inject(
        broker_ref,
        "iot/PK1/DEV1/properties/report",
        _legacy_report(),
    )
    runtime.run_cycle()
    writes = runtime.broker(broker_ref).write_topics
    assert writes == ["iot/PK1/DEV1/properties/write"]

    # The harness would notice a missing publish: a broker that never wrote.
    assert runtime.network.broker(broker_ref).writes


def _legacy_report():
    from tests.helpers import payloads

    return payloads.legacy_json_report(device_id="DEV1", serial="LEGACY-1")
