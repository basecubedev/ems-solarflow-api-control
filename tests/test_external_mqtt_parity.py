# SPDX-License-Identifier: AGPL-3.0-or-later
"""An external inverter is treated like a Zendure device with no write method.

That is the whole design claim of the type, and it is the kind of claim that
stops being true quietly: someone adds a field to the Zendure summary, or a new
case to the status masking, and the two shapes drift apart without any test
failing.

So these compare the two side by side through the shared code paths rather than
asserting either one's contents. A difference is the failure, whichever side
introduced it.
"""

from types import SimpleNamespace

import pytest

from dashboard.telemetry import build_dashboard_snapshot
from ems.external_status import sanitize_external_mqtt_status
from ems.zendure_mqtt.runtime import build_zendure_mqtt_runtime
from ems.zendure_mqtt.snapshot import ZendureMqttAggregator

pytestmark = [
    pytest.mark.contract,
    pytest.mark.mqtt,
]


EXTERNAL_TOPIC = "KostlPico/EXTERNAL01/solarPower"


def _config(broker_source="local_mqtt"):
    return {
        "zendure_mqtt": {
            "brokers": {
                "house": {"enabled": True, "source": broker_source, "host": "10.0.0.5"}
            }
        },
        "devices": [
            {
                "name": "External",
                "type": "external_mqtt",
                "mqtt": {
                    "broker_ref": "house",
                    "device_id": "EXTERNAL01",
                    "topics": {"outputHomePower": EXTERNAL_TOPIC},
                },
            },
            {
                "name": "Zendure",
                "type": "zendure_mqtt",
                "mqtt": {
                    "broker_ref": "house",
                    "device_id": "ZENDURE01",
                    "topic_family": "zensdk_ha_scalar",
                },
            },
        ],
    }


def _by_name(items):
    return {item["name"]: item for item in items}


def test_both_appear_in_the_runtime_status_with_the_same_shape():
    runtime = build_zendure_mqtt_runtime(_config())

    devices = _by_name(runtime.status()["devices"])

    assert set(devices) == {"External", "Zendure"}
    assert devices["External"].keys() == devices["Zendure"].keys()
    # And the one field that must differ, differs the right way.
    assert devices["External"]["topic_family"] == "external_scalar"
    assert devices["External"]["write_output_limit"] is False
    assert devices["Zendure"]["write_output_limit"] is False


def test_both_are_masked_by_the_same_rule_in_the_same_context():
    for source in ("local_mqtt", "zendure_cloud_mqtt"):
        status = {
            "brokers": {"house": {"source": source, "host": "h"}},
            "devices": [
                {
                    "name": "External",
                    "type": "external_mqtt",
                    "broker_ref": "house",
                    "identifier": "SERIAL000001",
                    "mqtt": {"device_id": "SERIAL000001"},
                },
                {
                    "name": "Zendure",
                    "type": "zendure_mqtt",
                    "broker_ref": "house",
                    "identifier": "SERIAL000001",
                    "mqtt": {"device_id": "SERIAL000001"},
                },
            ],
        }

        devices = _by_name(sanitize_external_mqtt_status(status)["devices"])

        assert devices["External"]["identifier"] == devices["Zendure"]["identifier"], source
        assert devices["External"]["mqtt"] == devices["Zendure"]["mqtt"], source


def test_both_reach_the_cockpit_as_the_same_kind_of_tile():
    aggregator = ZendureMqttAggregator(
        external_topics={EXTERNAL_TOPIC: ("EXTERNAL01", "outputHomePower")}
    )
    aggregator.observe(EXTERNAL_TOPIC, b"1234")
    aggregator.observe("Zendure/HB/ZENDURE01/outputHomePower", b"1234")
    snapshots = {snap.device_id: snap for snap in aggregator.snapshots()}

    class Runtime:
        def device_summaries(self):
            return [
                {"name": "External", "identifier": "EXTERNAL01", "status": "online"},
                {"name": "Zendure", "identifier": "ZENDURE01", "status": "online"},
            ]

        def snapshots(self):
            return snapshots

    controller = SimpleNamespace(
        devices=[],
        runtime_state=None,
        device_online={},
        commanded_total_w=0,
        filtered_load_w=0,
        _dashboard_capabilities=[],
        zendure_mqtt_runtime=Runtime(),
    )

    snapshot = build_dashboard_snapshot(
        controller,
        load_w=0,
        states=[],
        targets=[],
        effective_targets=[],
        allocated_total_w=0,
        effective_total_w=0,
        enabled=True,
        max_total_power=1600,
        min_output_limit=35,
    )

    external = snapshot["devices"]["External"]
    zendure = snapshot["devices"]["Zendure"]
    assert external.keys() == zendure.keys()
    assert external["read_only"] is zendure["read_only"] is True
    assert external["output_w"] == zendure["output_w"] == 1234
    # Both counted once, neither twice.
    assert snapshot["inverter_output_w"] == 2468
