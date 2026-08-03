# SPDX-License-Identifier: AGPL-3.0-or-later
"""Broker services are shared between the telemetry and control runtimes.

A broker profile referenced by both a telemetry device and a control device must
open a single connection and keep a single snapshot cache. The telemetry runtime
borrows the control runtime's per-broker service instead of building a parallel
one, and it never starts or stops a borrowed service (the control runtime owns
the lifecycle).
"""

import pytest

from ems.zendure_mqtt.control_runtime import build_zendure_mqtt_control_runtime
from ems.zendure_mqtt.runtime import build_zendure_mqtt_runtime

pytestmark = [
    pytest.mark.mqtt,
    pytest.mark.unit,
    pytest.mark.simulation,
]


class FakeService:
    def __init__(self, broker_config):
        self.broker_config = broker_config
        self.start_calls = 0
        self.stop_calls = 0
        self.connected = False

    def start(self):
        self.start_calls += 1
        self.connected = True

    def stop(self):
        self.stop_calls += 1
        self.connected = False

    def snapshots(self):
        return {}

    def snapshot_status(self, device_id, *, now_monotonic=None):
        from ems.zendure_mqtt.service import classify_snapshot

        return classify_snapshot(None, 60.0, now_monotonic=now_monotonic or 0.0)

    def publish_output_limit(self, topic, payload):
        return True


def _broker(source, host, port):
    return {"enabled": True, "source": source, "host": host, "port": port}


def _telemetry_device(name, broker_ref, device_id):
    return {
        "type": "zendure_mqtt",
        "name": name,
        "mqtt": {
            "broker_ref": broker_ref,
            "topic_family": "zensdk_ha_scalar",
            "device_id": device_id,
        },
    }


def _control_device(name, broker_ref, device_id, product_key):
    return {
        "type": "zendure_mqtt",
        "name": name,
        "hardware_profile": "solarflow_800_pro_2",
        "mqtt": {
            "broker_ref": broker_ref,
            "topic_family": "legacy_zendure_json",
            "device_id": device_id,
            "product_key": product_key,
        },
        "capabilities": {"write_output_limit": True},
    }


def _config():
    return {
        "zendure_mqtt": {
            "enabled": True,
            "brokers": {
                "shared": _broker("local_mqtt", "s", 1883),
                "telemetry_only": _broker("local_mqtt", "t", 1883),
                "control_only": _broker("local_mqtt", "c", 1883),
            },
        },
        "devices": [
            _telemetry_device("TeleShared", "shared", "DEV-TS"),
            _control_device("CtrlShared", "shared", "DEV-CS", "PK-CS"),
            _telemetry_device("TeleOnly", "telemetry_only", "DEV-TO"),
            _control_device("CtrlOnly", "control_only", "DEV-CO", "PK-CO"),
        ],
    }


def _build_both():
    config = _config()
    control = build_zendure_mqtt_control_runtime(config, service_factory=FakeService)
    telemetry = build_zendure_mqtt_runtime(
        config,
        service_factory=FakeService,
        shared_services=control.services_by_ref,
    )
    return control, telemetry


def _telemetry_service(telemetry, ref):
    for broker in telemetry._brokers:
        if broker.broker_ref == ref:
            return broker._service
    return None


def test_same_broker_ref_yields_one_shared_service():
    control, telemetry = _build_both()
    assert _telemetry_service(telemetry, "shared") is control.services_by_ref["shared"]
    # The control-only broker is also borrowed rather than reconnected.
    assert (
        _telemetry_service(telemetry, "control_only")
        is control.services_by_ref["control_only"]
    )


def test_different_broker_ref_gets_separate_service():
    control, telemetry = _build_both()
    tele_only = _telemetry_service(telemetry, "telemetry_only")
    assert tele_only is not None
    assert tele_only not in control.services_by_ref.values()


def test_control_uses_shared_snapshot_cache():
    control, telemetry = _build_both()
    ctrl_shared = next(d for d in control.devices if d.name == "CtrlShared")
    # The control device and the telemetry broker resolve to the exact same
    # service object, so they share one snapshot cache (no parallel state).
    assert ctrl_shared._service is _telemetry_service(telemetry, "shared")


def test_broker_shutdown_happens_once():
    control, telemetry = _build_both()
    control.start()
    telemetry.start()
    shared_service = control.services_by_ref["shared"]
    # Borrowing must not re-start the shared connection.
    assert shared_service.start_calls == 1

    telemetry.stop()
    # Telemetry must not stop a borrowed service.
    assert shared_service.stop_calls == 0
    control.stop()
    assert shared_service.stop_calls == 1
