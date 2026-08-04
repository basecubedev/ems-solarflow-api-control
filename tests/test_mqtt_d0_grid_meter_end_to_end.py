# SPDX-License-Identifier: AGPL-3.0-or-later
"""End-to-end: a D0 observed on a local broker becomes the EMS grid meter.

Exercises the full path without hardware or a real broker:

    fake local broker observation
      -> discovery candidate
      -> config proposal (target=grid_meter)
      -> Admin config preview
      -> generated config
      -> config resolution/validation
      -> MqttGridMeterClient
      -> fake totalPower message
      -> get_power() returns the signed value

and asserts the D0 is never added to devices[], the client never publishes, and
no output-control write gate is involved.
"""

import json
import os
import shutil
import subprocess
from types import SimpleNamespace

import pytest

import ems.config as cfg
from admin.config_preview import ConfigPreviewGenerator
from admin.models import MqttHardwareCandidate
from admin.zendure_mqtt_config_proposals import build_proposals
from ems.clients import close_grid_meter_client, create_grid_meter_client

pytestmark = [
    pytest.mark.mqtt,
    pytest.mark.e2e,
    pytest.mark.simulation,
    pytest.mark.power_control,
]

_SERIALIZER_RUNNER = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "js", "mqtt_serializer_runner.js"
)


def _serialize_selection_with_real_js(proposal, options):
    """Run the real admin.js serializer over a proposal, or skip without node.

    This exercises the actual browser payload contract (the extracted
    ``serializeMqttProposalSelection`` source) instead of rebuilding its expected
    JSON in Python, so dropping a required serializer field fails this test.
    """

    node = shutil.which("node")
    if node is None:
        pytest.skip("node is required for the real browser serializer test")
    result = subprocess.run(
        [node, _SERIALIZER_RUNNER],
        input=json.dumps({"proposal": proposal, "options": options}),
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


@pytest.fixture(autouse=True)
def _isolate_install_root(isolated_install_root):
    """Keep the preview off the developer's real repo-local config/data."""

    return isolated_install_root


# --- Supplied real hardware samples (redacted) ------------------------------

D0_HTTP_SAMPLE = {
    "a_aprt_power": 0,
    "b_aprt_power": 0,
    "c_aprt_power": 0,
    "deviceId": "D0_REDACTED",
    "messageId": 3882,
    "meterType": 2,
    "protocolType": 72,
    "timestamp": 1757249434,
    "total_power": -43,
}

SMARTMETER_3CT_HTTP_SAMPLE = {
    "timestamp": 1783163312,
    "messageId": 12,
    "deviceId": "rhRkw909",
    "a_aprt_power": 0,
    "b_aprt_power": 0,
    "c_aprt_power": -798,
    "total_power": -798,
}


_TEMPLATE = {
    "system": {"max_total_power": 1600, "dry_run": False},
    "devices": [{"name": "WR1", "ip": "192.0.2.1", "sn": "YOUR_SN", "max_power": 800}],
    "grid_meter": {"type": "shelly", "ip": "192.0.2.3"},
    "zendure_mqtt": {"enabled": True, "brokers": {}},
}


class _ReleaseManager:
    def config_template(self):
        return {"tag": "v0.7.0", "template": _TEMPLATE}


class _FakeMqttClient:
    """Minimal paho-compatible stub; records every publish to prove read-only."""

    def __init__(self):
        self.on_connect = None
        self.on_message = None
        self.on_disconnect = None
        self.subscriptions = []
        self.published = []
        self.username_password = None

    def username_pw_set(self, username, password=None):
        self.username_password = (username, password)

    def connect_async(self, host, port, keepalive=60):
        self._endpoint = (host, port)

    def loop_start(self):
        pass

    def loop_stop(self):
        pass

    def disconnect(self):
        pass

    def subscribe(self, topic):
        self.subscriptions.append(topic)

    def publish(self, *args, **kwargs):
        self.published.append((args, kwargs))


def _local_d0_candidate():
    return MqttHardwareCandidate(
        broker_id="b1",
        broker_host="10.0.0.9",
        broker_port=1883,
        topic_family="zensdk_ha_scalar",
        device_id="D0SN",
        serial_number="D0SN",
        metrics_seen=["totalPower"],
        topics_seen=["Zendure/sensor/D0SN/totalPower"],
        source_type="local_mqtt",
    )


def _inverter_item():
    return {
        "config_name": "WR1",
        "display_name": "SolarFlow 800",
        "role": "inverter",
        "enabled": True,
        "ip": "192.168.1.10",
        "serial_number": "SN1",
        "device_type": "zendure_solarflow_800_pro",
        "api_family": "zendure_local_http",
    }


def test_d0_local_mqtt_becomes_grid_meter_end_to_end():
    # 1. fake local broker observation -> 2. candidate -> 3. proposal
    proposals = build_proposals([_local_d0_candidate().to_dict()])
    assert len(proposals) == 1
    proposal = proposals[0]
    assert proposal["target"] == "grid_meter"
    assert proposal["role_hint"] == "grid_meter_candidate"

    # 4. Admin preview -> 5. generated config
    result = ConfigPreviewGenerator(_ReleaseManager()).generate(
        [_inverter_item()], 1, zendure_mqtt_proposals=proposals
    )
    config = result["config"]
    grid = config["grid_meter"]
    expected_ref = proposal["broker_ref"]
    assert expected_ref.startswith("local_mqtt_")
    assert grid["type"] == "zendure_smartmeter_d0"
    assert grid["mqtt"]["broker_ref"] == expected_ref
    assert grid["mqtt"]["topic"] == "Zendure/sensor/D0SN/totalPower"
    assert grid["mqtt"]["payload_format"] == "number"

    # The D0 is the central grid meter only; never a devices[] entry.
    assert all(
        d.get("type") not in ("zendure_mqtt", "zendure_smartmeter_d0")
        for d in config["devices"]
    )
    broker = config["zendure_mqtt"]["brokers"][expected_ref]
    assert broker["source"] == "local_mqtt"
    assert "password" not in broker

    # 6. config resolution/validation through the EMS-owned resolver.
    resolved = cfg.resolve_grid_meter_mqtt_settings(config)
    assert resolved["host"] == "10.0.0.9"
    assert resolved["topic"] == "Zendure/sensor/D0SN/totalPower"
    normalized = cfg.normalize_mqtt_grid_meter_settings(
        {"type": "zendure_smartmeter_d0", "mqtt": resolved},
        meter_type="zendure_smartmeter_d0",
    )

    # 7. grid-meter client creation with a fake broker connection.
    fake = _FakeMqttClient()
    normalized["_mqtt_client_factory"] = lambda: fake
    client = create_grid_meter_client(
        {"type": "zendure_smartmeter_d0", "mqtt": normalized}, session=object()
    )
    assert fake.subscriptions == []  # not yet connected

    # 8. fake MQTT totalPower message -> get_power() returns the signed value.
    fake.on_connect(fake, None, None, 0)
    assert fake.subscriptions == ["Zendure/sensor/D0SN/totalPower"]
    fake.on_message(
        fake,
        None,
        SimpleNamespace(topic="Zendure/sensor/D0SN/totalPower", payload=b"-43"),
    )
    assert client.get_power() == -43.0

    # Read-only: the client never publishes an MQTT command.
    client.close()
    assert fake.published == []
    # The grid meter is a read client, never an output-control writer.
    assert not hasattr(client, "write_output_limit")
    assert getattr(client, "transport", "mqtt") == "mqtt"


# --- A. Real browser serializer -> backend preview contract -----------------

def test_real_browser_serializer_reaches_preview_grid_meter():
    # 1-3. observation -> candidate -> discovery proposal (as the UI receives it).
    proposal = build_proposals([_local_d0_candidate().to_dict()])[0]
    assert proposal["target"] == "grid_meter"

    # 4. The real JavaScript serializer produces the browser payload.
    payload = _serialize_selection_with_real_js(
        proposal, {"target": "grid_meter", "replaceGridMeter": False}
    )
    # Required trusted metadata survives the serializer.
    assert payload["target"] == "grid_meter"
    assert payload["topic_family"] == "zensdk_ha_scalar"
    assert payload["broker_ref"] == proposal["broker_ref"]
    assert payload["broker_ref"].startswith("local_mqtt_")
    assert payload["serial_number"] == "D0SN"
    assert "Zendure/sensor/D0SN/totalPower" in payload["seen_topics"]
    assert payload["connection_source"] == "local_mqtt"

    # 5-7. backend adapter -> shared Core validation -> preview.
    result = ConfigPreviewGenerator(_ReleaseManager()).generate(
        [_inverter_item()], 1, zendure_mqtt_proposals=[payload]
    )
    codes = [
        issue["code"]
        for issue in result["validation"]["errors"]
    ]
    assert "grid_meter_family_invalid" not in codes
    grid = result["config"]["grid_meter"]
    assert grid["type"] == "zendure_smartmeter_d0"
    assert grid["mqtt"]["broker_ref"] == proposal["broker_ref"]
    assert grid["mqtt"]["topic"] == "Zendure/sensor/D0SN/totalPower"
    # The D0 is the central grid meter only, never a devices[] entry.
    assert all(
        d.get("type") not in ("zendure_mqtt", "zendure_smartmeter_d0")
        for d in result["config"]["devices"]
    )
    # The final summary reports the selected D0 grid meter.
    assert result["summary"]["grid_meters"] == 1
    assert result["summary"]["grid_meter"]["type"] == "zendure_smartmeter_d0"
    assert result["summary"]["grid_meter"]["transport"] == "mqtt"


# --- B. Config loading with a valid broker_ref ------------------------------

_BROKER_REF_CONFIG = {
    "system": {"enabled": True},
    "dry_run": False,
    "devices": [{"name": "WR1", "ip": "192.168.1.10", "sn": "SN1", "max_power": 800}],
    "grid_meter": {
        "type": "zendure_smartmeter_d0",
        "mqtt": {
            "broker_ref": "home_broker",
            "topic": "Zendure/sensor/D0SERIAL/totalPower",
            "payload_format": "number",
        },
    },
    "zendure_mqtt": {
        "enabled": True,
        "brokers": {
            "home_broker": {
                "enabled": True,
                "source": "local_mqtt",
                "host": "192.168.1.10",
                "port": 1883,
                "password": "s3cret",
            }
        },
    },
}


def test_valid_broker_ref_does_not_trip_placeholder_protection():
    import copy

    config = copy.deepcopy(_BROKER_REF_CONFIG)
    # The missing inline host must not be reported as a template placeholder.
    paths = cfg.template_placeholder_paths(config)
    assert "grid_meter.mqtt.host" not in paths

    protected = cfg.apply_template_placeholder_safety(config)
    system = protected.get("system", {})
    assert system.get("enabled") is True
    assert system.get("dry_run") is not True


def test_valid_broker_ref_resolves_without_copying_password():
    resolved = cfg.resolve_grid_meter_mqtt_settings(_BROKER_REF_CONFIG)
    assert resolved["host"] == "192.168.1.10"
    assert resolved["port"] == 1883
    assert resolved["topic"] == "Zendure/sensor/D0SERIAL/totalPower"
    # The on-disk grid_meter.mqtt never carries the broker password.
    assert "password" not in _BROKER_REF_CONFIG["grid_meter"]["mqtt"]


# --- C. Admin/Core validation parity ----------------------------------------

def _base_broker_config():
    return {
        "grid_meter": {
            "type": "zendure_smartmeter_d0",
            "mqtt": {
                "broker_ref": "home",
                "topic": "Zendure/sensor/D0SN/totalPower",
                "payload_format": "number",
            },
        },
        "zendure_mqtt": {
            "enabled": True,
            "brokers": {
                "home": {
                    "enabled": True,
                    "source": "local_mqtt",
                    "host": "192.168.1.10",
                    "port": 1883,
                }
            },
        },
    }


def _mutate(config, mutation):
    import copy

    config = copy.deepcopy(config)
    mutation(config)
    return config


_PARITY_CASES = {
    "unknown_ref": lambda c: c["grid_meter"]["mqtt"].__setitem__("broker_ref", "nope"),
    "disabled_profile": lambda c: c["zendure_mqtt"]["brokers"]["home"].__setitem__(
        "enabled", False
    ),
    "cloud_profile": lambda c: c["zendure_mqtt"]["brokers"]["home"].__setitem__(
        "source", "zendure_cloud_mqtt"
    ),
    "missing_host": lambda c: c["zendure_mqtt"]["brokers"]["home"].__setitem__(
        "host", ""
    ),
    "invalid_port": lambda c: c["zendure_mqtt"]["brokers"]["home"].__setitem__(
        "port", 70000
    ),
    "conflict_host": lambda c: c["grid_meter"]["mqtt"].__setitem__("host", "10.0.0.1"),
    "conflict_port": lambda c: c["grid_meter"]["mqtt"].__setitem__("port", 1884),
    "conflict_tls": lambda c: c["grid_meter"]["mqtt"].__setitem__("tls", True),
    "conflict_credentials": lambda c: c["grid_meter"]["mqtt"].__setitem__(
        "username", "u"
    ),
}


def _core_validate(config):
    """The exact Core grid-meter validation EMS runs at startup (resolve+normalize)."""

    grid_type = config["grid_meter"]["type"]
    resolved = cfg.resolve_grid_meter_mqtt_settings(config)
    cfg.normalize_mqtt_grid_meter_settings(
        {"type": grid_type, "mqtt": resolved}, meter_type=grid_type
    )


@pytest.mark.parametrize("name", sorted(_PARITY_CASES))
def test_admin_preview_and_core_agree_on_broker_ref_validity(name):
    from admin.config_preview import _validate_mqtt_grid_meter_via_core

    config = _mutate(_base_broker_config(), _PARITY_CASES[name])

    # Core rejects (same resolve+normalize path EMS uses at load).
    with pytest.raises(ValueError):
        _core_validate(config)

    # Admin preview rejects the same config via the shared Core resolver, and
    # never leaks a secret in the error text.
    validation = {"errors": [], "warnings": [], "info": []}
    _validate_mqtt_grid_meter_via_core(config, validation)
    assert validation["errors"], f"Admin accepted a config Core rejects: {name}"
    joined = json.dumps(validation["errors"])
    assert "password" not in joined.lower() or "s3cret" not in joined


def test_admin_and_core_accept_valid_broker_ref():
    from admin.config_preview import _validate_mqtt_grid_meter_via_core

    config = _base_broker_config()
    resolved = cfg.resolve_grid_meter_mqtt_settings(config)
    assert resolved["host"] == "192.168.1.10"
    validation = {"errors": [], "warnings": [], "info": []}
    _validate_mqtt_grid_meter_via_core(config, validation)
    assert validation["errors"] == []


# --- D. Multiple local brokers ----------------------------------------------

def _d0_candidate(broker_id, host, serial):
    return MqttHardwareCandidate(
        broker_id=broker_id,
        broker_host=host,
        broker_port=1883,
        topic_family="zensdk_ha_scalar",
        device_id=serial,
        serial_number=serial,
        metrics_seen=["totalPower"],
        topics_seen=[f"Zendure/sensor/{serial}/totalPower"],
        source_type="local_mqtt",
    )


def test_two_local_brokers_stay_separate_and_deterministic():
    observations = [
        _d0_candidate("b1", "10.0.0.10", "D0A").to_dict(),
        _d0_candidate("b2", "10.0.0.20", "D0B").to_dict(),
    ]
    first = build_proposals(observations)
    second = build_proposals(observations)

    refs = {p["serial_number"]: p["broker_ref"] for p in first}
    assert len(set(refs.values())) == 2
    # Each proposal keeps its own broker endpoint (TLS/host never cross over).
    hosts = {p["serial_number"]: p["broker_host"] for p in first}
    assert hosts == {"D0A": "10.0.0.10", "D0B": "10.0.0.20"}
    # Deterministic across repeated generation.
    assert {p["serial_number"]: p["broker_ref"] for p in second} == refs


def test_inverter_on_broker_a_and_d0_on_broker_b_resolve_correctly():
    inverter = MqttHardwareCandidate(
        broker_id="bA",
        broker_host="10.0.0.10",
        broker_port=1883,
        topic_family="zensdk_ha_scalar",
        device_id="INV1",
        serial_number="INV1",
        metrics_seen=["outputLimit", "electricLevel"],
        topics_seen=["Zendure/sensor/INV1/electricLevel"],
        source_type="local_mqtt",
    ).to_dict()
    d0 = _d0_candidate("bB", "10.0.0.20", "D0B").to_dict()

    proposals = build_proposals([inverter, d0])
    by_serial = {p["serial_number"]: p for p in proposals}
    assert by_serial["INV1"]["broker_ref"] != by_serial["D0B"]["broker_ref"]
    assert by_serial["INV1"]["broker_host"] == "10.0.0.10"
    assert by_serial["D0B"]["broker_host"] == "10.0.0.20"

    # Selecting the D0 produces a config that resolves to its own broker.
    payload = _serialize_selection_with_real_js(
        by_serial["D0B"], {"target": "grid_meter", "replaceGridMeter": False}
    )
    result = ConfigPreviewGenerator(_ReleaseManager()).generate(
        [_inverter_item()], 1, zendure_mqtt_proposals=[payload]
    )
    config = result["config"]
    ref = config["grid_meter"]["mqtt"]["broker_ref"]
    assert config["zendure_mqtt"]["brokers"][ref]["host"] == "10.0.0.20"


# --- H. Client lifecycle cleanup --------------------------------------------

class _RecordingCloseClient:
    def __init__(self, fail=False):
        self.closed = 0
        self._fail = fail

    def close(self):
        self.closed += 1
        if self._fail:
            raise RuntimeError("cleanup boom")


def test_close_grid_meter_client_is_idempotent():
    client = _RecordingCloseClient()
    close_grid_meter_client(client)
    close_grid_meter_client(client)
    assert client.closed == 2  # repeated cleanup is tolerated


def test_close_grid_meter_client_tolerates_missing_close():
    close_grid_meter_client(object())
    close_grid_meter_client(None)


def test_close_grid_meter_client_does_not_hide_primary_error():
    client = _RecordingCloseClient(fail=True)
    # A cleanup failure inside a finally must not mask the primary error.
    with pytest.raises(ValueError, match="primary"):
        try:
            raise ValueError("primary failure")
        finally:
            close_grid_meter_client(client)
    assert client.closed == 1


def test_startup_failure_after_client_creation_still_closes():
    client = _RecordingCloseClient()
    with pytest.raises(RuntimeError, match="startup step"):
        try:
            # Simulate a startup step failing after the client was created.
            raise RuntimeError("startup step failed")
        finally:
            close_grid_meter_client(client)
    assert client.closed == 1


@pytest.mark.parametrize(
    "sample,expected",
    [
        (D0_HTTP_SAMPLE, -43.0),
        (SMARTMETER_3CT_HTTP_SAMPLE, -798.0),
    ],
)
def test_http_samples_classify_as_generic_grid_meter_and_preserve_sign(sample, expected):
    from admin.discovery import classify_zendure_report
    from ems.clients import _parse_zendure_grid_meter_http_power

    device = classify_zendure_report(sample, ip="192.0.2.80", port=80)
    assert device is not None
    # Both models use the same generic local-HTTP grid-meter path.
    assert device.api_family == "zendure_grid_meter_http"
    assert device.role_suggestion == "grid_meter"
    assert device.config_ready is True
    # The functional read criterion is numeric total_power, sign preserved.
    assert _parse_zendure_grid_meter_http_power(sample) == expected
