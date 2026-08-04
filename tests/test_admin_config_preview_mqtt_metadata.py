# SPDX-License-Identifier: AGPL-3.0-or-later
"""Preview provisions TLS/credential metadata and rejects invalid endpoints.

End-to-end from discovery observation through proposal to the generated EMS
broker profile and the shared Core validation path (defects 1, 2, 4).
"""

import json

import pytest

from admin.config_preview import ConfigPreviewGenerator
from admin.mqtt_topic_discovery import MqttTopicAggregator
from admin.zendure_mqtt_config_proposals import build_proposals

pytestmark = [
    pytest.mark.admin,
    pytest.mark.authority,
    pytest.mark.config,
    pytest.mark.mqtt,
    pytest.mark.integration,
    pytest.mark.simulation,
]


@pytest.fixture(autouse=True)
def _isolate_install_root(isolated_install_root):
    return isolated_install_root


TEMPLATE = {
    "system": {"max_total_power": 1600, "dry_run": False},
    "devices": [{"name": "WR1", "ip": "192.0.2.1", "sn": "YOUR_SN", "max_power": 800}],
    "grid_meter": {"type": "shelly", "ip": "192.0.2.3"},
}


class _ReleaseManager:
    def config_template(self):
        return {"tag": "v0.6.0", "template": TEMPLATE}


def _inverter():
    return {
        "config_name": "WR1", "display_name": "SolarFlow 800", "role": "inverter",
        "enabled": True, "ip": "192.168.1.10", "serial_number": "SN1",
        "device_type": "zendure_solarflow_800_pro", "api_family": "zendure_local_http",
    }


def _meter():
    return {
        "config_name": "grid", "display_name": "Shelly 3EM", "role": "grid_meter",
        "enabled": True, "ip": "192.168.1.50", "api_family": "shelly_gen2",
        "device_type": "shelly_pro_3em",
    }


def _device_proposal(*, host="10.0.0.10", port=1883, tls_mode="plaintext",
                     credentials_ref=None, serial="DEV1"):
    agg = MqttTopicAggregator(
        {
            "id": f"mqtt:{host}:{port}", "host": host, "port": port,
            "tls": tls_mode not in (None, "", "plaintext"),
            "tls_mode": tls_mode, "credentials_ref": credentials_ref,
        }
    )
    for metric in ("outputPackPower", "electricLevel"):
        agg.observe(f"Zendure/sensor/{serial}/{metric}", None)
    return build_proposals(agg.results())


def _generate(proposals):
    return ConfigPreviewGenerator(
        _ReleaseManager(), zendure_cloud_auth_available=lambda: True
    ).generate([_inverter(), _meter()], 1, zendure_mqtt_proposals=proposals)


def _broker_profile(result, proposal):
    return result["config"]["zendure_mqtt"]["brokers"][proposal["broker_ref"]]


def _legacy_control_proposal(*, host="10.0.0.10", port=1883):
    from admin.models import MqttHardwareCandidate

    candidate = MqttHardwareCandidate(
        broker_id=f"mqtt:{host}:{port}", broker_host=host, broker_port=port,
        topic_family="legacy_zendure_json", device_id="DEV1",
        serial_number="LEG1", product_key="PK1", model_hint="Hyper 2000",
        metrics_seen=["electricLevel", "outputHomePower", "outputLimit"],
    )
    return build_proposals([candidate.to_dict()])


def test_controllable_legacy_proposal_survives_into_config_device():
    # End-to-end: a discovered controllable inverter reaches config.json as a
    # normal control device — the user never edits config.json by hand.
    proposals = _legacy_control_proposal()
    assert proposals[0]["config_fragment"]["capabilities"]["write_output_limit"] is True
    result = _generate(proposals)
    assert result["ready"] is True, result["validation"]
    control = [
        d for d in result["config"]["devices"] if d.get("type") == "zendure_mqtt"
    ][0]
    assert control["capabilities"]["write_output_limit"] is True
    # The resolved hardware identity is pinned into the applied config device.
    assert control["hardware_profile"] == "hyper_2000"
    assert "write_protocol" not in control["mqtt"]
    assert control["mqtt"].get("product_key") == "PK1"


# --- Scenario 1: authenticated local broker ---------------------------------
def test_authenticated_local_broker_keeps_credentials_ref_no_secret():
    proposals = _device_proposal(credentials_ref="cred-1")
    result = _generate(proposals)
    assert result["ready"] is True
    profile = _broker_profile(result, proposals[0])
    assert profile["credentials_ref"] == "cred-1"
    assert "password" not in profile and "username" not in profile
    assert "password" not in json.dumps(result["config"])


# --- Scenario 2: TLS system CA broker ---------------------------------------
def test_system_ca_broker_profile_is_secure_tls():
    proposals = _device_proposal(port=8883, tls_mode="system_ca")
    result = _generate(proposals)
    assert result["ready"] is True
    profile = _broker_profile(result, proposals[0])
    assert profile["tls"] is True
    assert profile["tls_insecure"] is False
    assert profile["port"] == 8883


# --- Scenario 3: TLS insecure broker ----------------------------------------
def test_insecure_broker_profile_preserves_insecure_flag():
    proposals = _device_proposal(port=8883, tls_mode="insecure_no_verify")
    result = _generate(proposals)
    assert result["ready"] is True
    profile = _broker_profile(result, proposals[0])
    assert profile["tls"] is True
    assert profile["tls_insecure"] is True


def test_tls_broker_is_never_downgraded_to_plain():
    proposals = _device_proposal(port=8883, tls_mode="system_ca")
    result = _generate(proposals)
    profile = _broker_profile(result, proposals[0])
    assert profile["tls"] is True


# --- Scenario 4: explicit invalid port --------------------------------------
@pytest.mark.parametrize("bad_port", ["broken", True, 0, 70000])
def test_explicit_invalid_broker_port_is_rejected(bad_port):
    proposals = _device_proposal(port=8883, tls_mode="system_ca")
    # Simulate a browser overriding the trusted endpoint with an invalid port.
    proposals[0]["broker_port"] = bad_port
    result = _generate(proposals)
    codes = {issue["code"] for issue in result["validation"]["errors"]}
    assert "zendure_mqtt_broker_endpoint_invalid" in codes
    assert result["ready"] is False
    # No default port silently applied: no broker profile provisioned from it.
    brokers = result["config"]["zendure_mqtt"]["brokers"] if isinstance(
        result["config"].get("zendure_mqtt"), dict
    ) else {}
    assert proposals[0]["broker_ref"] not in brokers


def test_absent_port_still_allows_protocol_default():
    proposals = _device_proposal(port=8883, tls_mode="system_ca")
    proposals[0]["broker_port"] = None
    result = _generate(proposals)
    # An absent port is not an error; the broker just needs a host to provision.
    codes = {issue["code"] for issue in result["validation"]["errors"]}
    assert "zendure_mqtt_broker_endpoint_invalid" not in codes


# --- Defect 4: string booleans on broker TLS --------------------------------
def test_string_broker_tls_flag_is_rejected_not_coerced():
    proposals = _device_proposal(port=8883, tls_mode="system_ca")
    del proposals[0]["broker_tls_mode"]
    proposals[0]["broker_tls"] = "false"
    result = _generate(proposals)
    codes = {issue["code"] for issue in result["validation"]["errors"]}
    assert "zendure_mqtt_broker_endpoint_invalid" in codes


# --- D0 grid meter: credentials + string-false replacement flag --------------
def _d0_proposal(*, host="10.0.0.10", credentials_ref="cred-1", serial="D0X"):
    agg = MqttTopicAggregator(
        {"id": f"mqtt:{host}:1883", "host": host, "port": 1883,
         "tls": False, "credentials_ref": credentials_ref}
    )
    agg.observe(f"Zendure/sensor/{serial}/totalPower", b"-42")
    return build_proposals(agg.results())


def test_d0_grid_meter_credentials_ref_survives_core_resolver():
    proposals = _d0_proposal(credentials_ref="cred-1")
    result = ConfigPreviewGenerator(
        _ReleaseManager(), zendure_cloud_auth_available=lambda: True
    ).generate([_inverter()], 1, zendure_mqtt_proposals=proposals)
    assert result["ready"] is True
    profile = result["config"]["zendure_mqtt"]["brokers"][proposals[0]["broker_ref"]]
    assert profile["credentials_ref"] == "cred-1"
    assert "password" not in json.dumps(result["config"])


def test_scenario6_string_false_replace_flag_keeps_existing_grid_meter():
    proposals = _d0_proposal()
    proposals[0]["replace_grid_meter"] = "false"
    # An HTTP grid meter is already selected in the draft.
    result = ConfigPreviewGenerator(
        _ReleaseManager(), zendure_cloud_auth_available=lambda: True
    ).generate([_inverter(), _meter()], 1, zendure_mqtt_proposals=proposals)
    codes = {issue["code"] for issue in result["validation"]["errors"]}
    assert "grid_meter_replace_invalid" in codes
    assert result["ready"] is False
    # The existing HTTP grid meter is untouched (not replaced by the D0).
    assert result["config"]["grid_meter"]["type"] == "shelly"
