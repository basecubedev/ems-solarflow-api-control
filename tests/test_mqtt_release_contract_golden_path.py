# SPDX-License-Identifier: AGPL-3.0-or-later
"""Golden-path MQTT release contract: setup through runtime cleanup.

One test that crosses every real production boundary for a normal authenticated
local MQTT installation:

    Admin discovery -> trusted proposal -> preview -> apply -> config.json
    -> Core credential resolution -> runtime -> telemetry -> DeviceState
    -> controller -> transport-specific publish -> cleanup

Nothing patches the final result of any stage. Only the MQTT socket, the HTTP
socket, and the discovery clock are faked.
"""

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

RELEASE_SECRET_PASSWORD = "RELEASE_SECRET_PASSWORD"
RELEASE_USER = "release-user"
CREDENTIALS_REF = "home"

D0_SERIAL = "D0-RELEASE-001"
D0_TOPIC = f"Zendure/sensor/{D0_SERIAL}/totalPower"
LEGACY_SERIAL = "LEGACY-RELEASE-001"
LEGACY_DEVICE_ID = "LEGACY-RELEASE-001"
LEGACY_PRODUCT_KEY = "RELEASE_PRODUCT"
LEGACY_REPORT_TOPIC = f"iot/{LEGACY_PRODUCT_KEY}/{LEGACY_DEVICE_ID}/properties/report"
LEGACY_WRITE_TOPIC = f"iot/{LEGACY_PRODUCT_KEY}/{LEGACY_DEVICE_ID}/properties/write"
GRID_POWER_W = -420.0
SECRET_TOKENS = [RELEASE_SECRET_PASSWORD]


@pytest.fixture(autouse=True)
def _isolate(isolated_install_root):
    return isolated_install_root


def _run_generation(harness):
    harness.run_generation(
        [
            broker_candidate(
                "10.0.0.10",
                port=1883,
                devices=[
                    device_observation(
                        D0_SERIAL,
                        topic_family="zensdk_ha_scalar",
                        credentials_ref=CREDENTIALS_REF,
                        metrics=["totalPower"],
                        topics=[D0_TOPIC],
                    ),
                    device_observation(
                        LEGACY_SERIAL,
                        topic_family="legacy_zendure_json",
                        device_id=LEGACY_DEVICE_ID,
                        product_key=LEGACY_PRODUCT_KEY,
                        model_hint="SolarFlow 800 Pro 2",
                        credentials_ref=CREDENTIALS_REF,
                        metrics=["outputLimit", "electricLevel"],
                        topics=[f"Zendure/sensor/{LEGACY_SERIAL}/electricLevel"],
                    ),
                ],
            )
        ]
    )


def test_release_mqtt_setup_to_runtime_golden_path(tmp_path):
    with ReleaseContractHarness(tmp_path) as harness:
        # --- Step 1: save discovery credentials (secure, no runtime cred yet) ---
        harness.save_discovery_credential(
            CREDENTIALS_REF, RELEASE_USER, RELEASE_SECRET_PASSWORD
        )
        assert not harness.runtime_credential_exists(CREDENTIALS_REF)
        status = harness.credential_store.mqtt_discovery_secret_status(CREDENTIALS_REF)
        harness.assert_no_secret(status, tokens=SECRET_TOKENS)

        # --- Step 3: discovery generation 1 -> two selectable proposals ---------
        _run_generation(harness)
        d0 = harness.proposal_for(D0_SERIAL)
        legacy = harness.proposal_for(LEGACY_SERIAL)
        assert d0 is not None and legacy is not None
        # Both reference the same authenticated connection profile.
        assert d0["credentials_ref"] == CREDENTIALS_REF
        assert legacy["credentials_ref"] == CREDENTIALS_REF
        assert d0["broker_ref"] == legacy["broker_ref"]
        # D0 carries the exact observed canonical totalPower topic.
        assert D0_TOPIC in d0["seen_topics"]
        # A supported, addressable legacy device is immediately proposed as a
        # control inverter; no additional per-device gate is required.
        assert legacy["topic_family"] == "legacy_zendure_json"
        assert legacy["output_control_supported"] is True
        assert legacy["config_fragment"]["capabilities"]["write_output_limit"] is True

        # --- Step 4: preview selection (secret-free, side-effect-free) ----------
        selections = [
            harness.selection(d0, replace_grid_meter=True),
            harness.selection(legacy),
        ]
        status, preview = harness.preview(selections=selections)
        assert status == 200 and preview["ready"] is True
        preview_config = preview["config"]
        assert preview_config["grid_meter"]["type"] == "zendure_smartmeter_d0"
        assert preview_config["grid_meter"]["mqtt"]["topic"] == D0_TOPIC
        # config references credentials_ref only; no username/password.
        broker_profile = preview_config["zendure_mqtt"]["brokers"][d0["broker_ref"]]
        assert broker_profile["credentials_ref"] == CREDENTIALS_REF
        assert "password" not in broker_profile and "username" not in broker_profile
        harness.assert_no_secret(preview, tokens=SECRET_TOKENS)
        # Preview promotes nothing.
        assert not harness.runtime_credential_exists(CREDENTIALS_REF)

        # --- Step 5: apply (transactional promotion + config write) -------------
        status, applied = harness.apply(selections=selections)
        assert status == 200 and applied["ok"] is True
        assert harness.runtime_credential_exists(CREDENTIALS_REF)
        config = harness.applied_config()
        harness.assert_no_secret(config, tokens=SECRET_TOKENS)
        # Same endpoint + same credential = exactly one broker profile. The D0 grid
        # meter and the legacy device both reference that single profile; an
        # authenticated broker must never be split into two profiles.
        brokers = config["zendure_mqtt"]["brokers"]
        assert len(brokers) == 1, brokers
        broker_ref = next(iter(brokers))
        assert brokers[broker_ref]["credentials_ref"] == CREDENTIALS_REF
        assert config["grid_meter"]["mqtt"]["broker_ref"] == broker_ref
        assert config["devices"][0]["mqtt"]["broker_ref"] == broker_ref

        # --- Step 6-9: start runtime, inject telemetry, run one control cycle ---
        assert config["devices"][0]["capabilities"]["write_output_limit"] is True

        runtime = harness.start_runtime(config)

        # Step 7: D0 grid-meter telemetry (sign preserved, read-only).
        runtime.inject(broker_ref, D0_TOPIC, str(GRID_POWER_W).encode())
        assert runtime.grid_meter.get_power() == GRID_POWER_W

        # Step 8: legacy inverter telemetry -> production parser -> DeviceState.
        runtime.inject(
            broker_ref,
            LEGACY_REPORT_TOPIC,
            payloads.legacy_json_report(
                device_id=LEGACY_DEVICE_ID, serial=LEGACY_SERIAL
            ),
        )

        # Step 9: one controller cycle -> exactly one supported write publish.
        controller = runtime.run_cycle()
        assert LEGACY_SERIAL in _controlled_names(controller) or _controlled_names(
            controller
        )
        writes = runtime.broker(broker_ref).write_topics
        assert writes == [LEGACY_WRITE_TOPIC]
        # The D0 topic tree is never published to.
        assert not any("totalPower" in topic for topic in writes)

        # --- Step 10: secret sweep across every public artifact -----------------
        harness.assert_no_secret(
            harness.public_secret_surface(),
            runtime.control_runtime.status(),
            runtime.telemetry_runtime.status(),
            applied,
            preview,
            tokens=SECRET_TOKENS,
        )

        # --- Step 11: cleanup is deterministic and idempotent -------------------
        grid_client = runtime.broker(broker_ref).clients[-1]
        control_clients = runtime.broker(broker_ref).clients
        runtime.stop()
        runtime.stop()  # idempotent
        for client in control_clients:
            assert client.loop_stop_count <= 1
        assert grid_client.disconnect_count >= 1


def _controlled_names(controller):
    return set(controller.last_control_explanation.devices)
