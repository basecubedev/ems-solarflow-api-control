# SPDX-License-Identifier: AGPL-3.0-or-later
"""What each key of the project's MQTT namespace means, and what is no reading.

``ems-solarflow/<id>/<key>`` carries one value and ``ems-solarflow/<id>/state``
a JSON object of several. Discovery and the runtime both ask the catalog what a
payload means, so every case here holds for both; the aggregator cases show the
runtime side.
"""

import json

import pytest

from ems.zendure_mqtt.config_entries import validate_external_mqtt_device_config
from ems.zendure_mqtt.external_catalog import (
    EMS_SOLARFLOW,
    match_external_topic,
    valid_device_id,
)
from ems.zendure_mqtt.payloads import MAX_PAYLOAD_BYTES
from ems.zendure_mqtt.runtime import (
    classify_zendure_mqtt_devices,
    summarize_zendure_mqtt_devices,
)
from ems.zendure_mqtt.snapshot import ZendureMqttAggregator

pytestmark = [
    pytest.mark.unit,
    pytest.mark.mqtt,
]

DEVICE = "garage-inverter"
_DEVICE_ID_CODES = {
    "external_mqtt_device_id_missing",
    "external_mqtt_device_id_invalid",
    "mqtt_route_segment_invalid",
}


def _topic(key, device_id=DEVICE):
    return f"ems-solarflow/{device_id}/{key}"


def _bundle(values):
    return json.dumps(values).encode()


def _metrics(aggregator):
    (snapshot,) = aggregator.snapshots()
    return snapshot.metrics


# --- one key, one topic -------------------------------------------------------


@pytest.mark.parametrize(
    ("key", "payload", "expected"),
    [
        ("inverterPower", b"765", {"outputHomePower": 765}),
        ("inverterPower", b"0", {"outputHomePower": 0}),
        ("inverterPower", b" 765.5\n", {"outputHomePower": 765.5}),
        ("solarPower", b"820", {"solarInputPower": 820}),
        ("batteryPower", b"40", {"outputPackPower": 40, "packInputPower": 0}),
        ("batteryPower", b"-55", {"outputPackPower": 0, "packInputPower": 55}),
        ("batteryPower", b"0", {"outputPackPower": 0, "packInputPower": 0}),
        ("batteryPower", b"-0.0", {"outputPackPower": 0, "packInputPower": 0}),
        ("batterySoc", b"0", {"electricLevel": 0}),
        ("batterySoc", b"42.5", {"electricLevel": 42.5}),
        ("batterySoc", b"100", {"electricLevel": 100}),
        ("inverterPower", b"1000000", {"outputHomePower": 1000000}),
        ("batteryPower", b"-1000000", {"outputPackPower": 0, "packInputPower": 1000000}),
        ("solarPower", b"0.1234567890123456", {"solarInputPower": 0.1234567890123456}),
    ],
)
def test_a_key_becomes_the_metrics_a_zendure_device_reports(key, payload, expected):
    assert EMS_SOLARFLOW.readings(key, payload) == expected


@pytest.mark.parametrize(
    ("key", "payload"),
    [
        ("inverterPower", b"-1"),
        ("solarPower", b"-0.5"),
        ("batterySoc", b"-1"),
        ("batterySoc", b"100.5"),
        ("inverterPower", b"765 W"),
        ("inverterPower", b"765,0"),
        ("inverterPower", b"true"),
        ("inverterPower", b"nan"),
        ("inverterPower", b"Infinity"),
        ("inverterPower", b"1e400"),
        ("inverterPower", b'{"inverterPower": 765}'),
        ("inverterPower", b"\xff\xfe"),
        ("inverterPower", b""),
        ("dailyYield", b"5"),
        ("outputHomePower", b"765"),
        ("inverterPower", b"1e3"),
        ("inverterPower", b"1_000"),
        ("inverterPower", b"+5"),
        ("inverterPower", b".5"),
        ("inverterPower", b"765."),
        ("inverterPower", "\u0667\u0666\u0665".encode()),
        ("inverterPower", b"1000001"),
        ("batteryPower", b"-1000001"),
        ("inverterPower", b"1" * 17),
        ("inverterPower", b"1" * 5000),
        ("inverterPower", b"1" + b"0" * 400),
    ],
)
def test_what_is_no_reading(key, payload):
    assert EMS_SOLARFLOW.readings(key, payload) is None


# --- several keys, one bundle ----------------------------------------------------


def test_the_bundle_carries_every_key_at_once():
    assert EMS_SOLARFLOW.readings(
        "state",
        _bundle(
            {"inverterPower": 765, "solarPower": 820, "batteryPower": -55, "batterySoc": 42}
        ),
    ) == {
        "outputHomePower": 765,
        "solarInputPower": 820,
        "outputPackPower": 0,
        "packInputPower": 55,
        "electricLevel": 42,
    }


def test_the_bundle_reports_only_the_keys_it_carries():
    assert EMS_SOLARFLOW.readings("state", _bundle({"inverterPower": 765})) == {
        "outputHomePower": 765
    }


def test_a_field_the_namespace_does_not_list_is_ignored_even_if_it_names_a_metric():
    """The bundle is read by its keys, never by the project's own metric names."""

    assert EMS_SOLARFLOW.readings(
        "state",
        _bundle({"inverterPower": 765, "dailyYield": 5, "outputHomePower": 9999, "outputLimit": 1}),
    ) == {"outputHomePower": 765}


def test_a_field_that_is_no_reading_is_skipped_and_the_rest_still_read():
    assert EMS_SOLARFLOW.readings(
        "state",
        _bundle(
            {"inverterPower": -5, "solarPower": "820 W", "batteryPower": True, "batterySoc": 42}
        ),
    ) == {"electricLevel": 42}


def test_a_numeric_string_reads_in_the_bundle_as_it_does_on_its_own_topic():
    assert EMS_SOLARFLOW.readings("state", _bundle({"inverterPower": "765"})) == (
        EMS_SOLARFLOW.readings("inverterPower", b"765")
    )


@pytest.mark.parametrize(
    "payload",
    [
        b"{}",
        b"[]",
        b"765",
        b'"765"',
        b"null",
        b"{",
        b"",
        b"\xff\xfe",
        b'{"inverterPower": {"value": 765}}',
        b'{"inverterPower": [765]}',
        b'{"inverterPower": NaN}',
        b'{"inverterPower": Infinity}',
        b'{"inverterPower": null}',
        b'{"batterySoc": 101}',
        b'{"inverterPower": 100000000000000000000}',
        b'{"inverterPower": 1e400}',
        b'{"inverterPower": "1_000"}',
        b'{"inverterPower": ' + b"9" * 5000 + b"}",
    ],
)
def test_a_bundle_that_carries_no_reading_is_none(payload):
    assert EMS_SOLARFLOW.readings("state", payload) is None


def test_a_bundle_nested_too_deeply_for_the_parser_is_refused_without_raising():
    depth = 30_000
    nested = b'{"a":' * depth + b"1" + b"}" * depth
    assert len(nested) < MAX_PAYLOAD_BYTES

    assert EMS_SOLARFLOW.readings("state", nested) is None


def test_an_oversized_bundle_is_refused():
    padding = b"x" * MAX_PAYLOAD_BYTES
    payload = b'{"inverterPower": 765, "pad": "' + padding + b'"}'

    assert EMS_SOLARFLOW.readings("state", payload) is None


# --- the runtime aggregator ----------------------------------------------------


def test_both_forms_mix_and_each_key_keeps_its_latest_value():
    aggregator = ZendureMqttAggregator()

    aggregator.observe(_topic("state"), _bundle({"inverterPower": 700, "batterySoc": 40}))
    aggregator.observe(_topic("inverterPower"), b"765")
    assert _metrics(aggregator)["outputHomePower"] == 765
    assert _metrics(aggregator)["electricLevel"] == 40

    aggregator.observe(_topic("state"), _bundle({"inverterPower": 800}))
    assert _metrics(aggregator)["outputHomePower"] == 800
    assert _metrics(aggregator)["electricLevel"] == 40


def test_a_charge_after_a_discharge_leaves_no_discharge_behind():
    """Each battery reading writes both directions, so the old one cannot linger."""

    aggregator = ZendureMqttAggregator()

    aggregator.observe(_topic("batteryPower"), b"-55")
    aggregator.observe(_topic("state"), _bundle({"batteryPower": 40}))

    metrics = _metrics(aggregator)
    assert (metrics["outputPackPower"], metrics["packInputPower"]) == (40, 0)


def test_a_message_without_a_reading_is_no_sign_of_life():
    clock = [0.0]
    aggregator = ZendureMqttAggregator(
        monotonic=lambda: clock[0], wall_clock=lambda: clock[0]
    )
    aggregator.observe(_topic("state"), _bundle({"inverterPower": 765}))

    clock[0] = 600.0
    aggregator.observe(_topic("state"), _bundle({"inverterPower": -1, "x": 2}))
    aggregator.observe(_topic("batterySoc"), b"250")

    (snapshot,) = aggregator.snapshots()
    assert snapshot.last_seen_monotonic == 0.0
    assert snapshot.metrics == {"outputHomePower": 765}


def test_the_last_message_names_exactly_the_metrics_it_carried():
    aggregator = ZendureMqttAggregator()

    aggregator.observe(_topic("state"), _bundle({"inverterPower": 765, "batterySoc": 40}))
    aggregator.observe(_topic("batteryPower"), b"12")

    (snapshot,) = aggregator.snapshots()
    assert snapshot.observed_metrics == {"outputPackPower", "packInputPower"}


def test_a_retained_reading_is_read_like_any_other():
    """The broker replays it on subscribing; it is the device's last known value.

    Skipping it would depend on every broker setting the retain flag only for a
    replay, which nobody has checked for the brokers bridges commonly ship.
    """

    from types import SimpleNamespace

    from ems.zendure_mqtt.client import ZendureMqttReadClient
    from ems.zendure_mqtt.config import ZendureMqttClientConfig

    client = ZendureMqttReadClient(ZendureMqttClientConfig(host="10.0.0.5"))

    client._on_message(
        None, None, SimpleNamespace(topic=_topic("inverterPower"), payload=b"765", retain=True)
    )

    assert client.snapshots()[f"ems-solarflow/{DEVICE}"].metrics == {"outputHomePower": 765}


def test_two_devices_in_the_namespace_stay_two():
    aggregator = ZendureMqttAggregator()

    aggregator.observe(_topic("inverterPower", "garage"), b"100")
    aggregator.observe(_topic("inverterPower", "roof"), b"200")

    outputs = {snap.device_id: snap.metrics["outputHomePower"] for snap in aggregator.snapshots()}
    assert outputs == {"ems-solarflow/garage": 100, "ems-solarflow/roof": 200}


def _same_id_on_one_broker(*messages):
    clock = [0.0]
    aggregator = ZendureMqttAggregator(
        monotonic=lambda: clock[0], wall_clock=lambda: clock[0]
    )
    for at, topic, payload in messages:
        clock[0] = at
        aggregator.observe(topic, payload)
    return {snap.device_id: snap for snap in aggregator.snapshots()}


_SHARED_ID_DEVICES = [
    {
        "name": "Hyper",
        "type": "zendure_mqtt",
        "mqtt": {"topic_family": "legacy_zendure_json", "device_id": "ABC123", "product_key": "PK1"},
    },
    {
        "name": "Garage",
        "type": "external_mqtt",
        "mqtt": {"topic_family": "ems_solarflow", "device_id": "ABC123"},
    },
]


def _summaries(snapshots, now):
    valid, invalid = classify_zendure_mqtt_devices(_SHARED_ID_DEVICES)
    assert invalid == []
    return {
        summary["name"]: summary
        for summary in summarize_zendure_mqtt_devices(
            valid, invalid, snapshots, now_monotonic=now, stale_after_seconds=60
        )
    }


def test_a_zendure_device_and_an_external_one_with_the_same_id_keep_their_readings_apart():
    """One broker, one snapshot cache, one id: neither writes the other's metrics.

    The control path reads a Zendure device's telemetry from this cache, so an
    external battery's charge level must never land in it, and its messages
    must never make a silent Zendure device look alive.
    """

    snapshots = _same_id_on_one_broker(
        (
            0.0,
            "iot/PK1/ABC123/properties/report",
            _bundle({"properties": {"electricLevel": 80, "outputHomePower": 300}}),
        ),
        (
            600.0,
            _topic("state", "ABC123"),
            _bundle({"inverterPower": 765, "batterySoc": 3, "batteryPower": -500}),
        ),
    )

    zendure = snapshots["ABC123"]
    assert zendure.metrics == {"electricLevel": 80, "outputHomePower": 300}
    assert zendure.topic_families == {"legacy_zendure_json"}
    assert zendure.last_seen_monotonic == 0.0
    assert snapshots["ems-solarflow/ABC123"].metrics["electricLevel"] == 3

    summaries = _summaries(snapshots, now=600.0)
    assert summaries["Hyper"]["status"] == "stale"
    assert "packInputPower" not in summaries["Hyper"]["metrics"]
    assert summaries["Garage"]["status"] == "online"
    assert "packInputPower" in summaries["Garage"]["metrics"]


def test_an_external_device_is_never_found_as_a_zendure_device_by_its_id():
    """The runtime also matches a Zendure device by serial; a catalog id is none."""

    snapshots = _same_id_on_one_broker(
        (0.0, _topic("inverterPower", "ABC123"), b"765"),
    )

    summaries = _summaries(snapshots, now=0.0)
    assert summaries["Hyper"]["status"] == "unseen"
    assert summaries["Garage"]["status"] == "online"


# --- the device id -------------------------------------------------------------


DEVICE_IDS = (
    "_",
    "-",
    "---",
    "redacted",
    "Redacted",
    "your_x",
    "your-pv",
    "garage-inverter",
    "WR_1",
    "EXAMPLE0000001",
    "x" * 64,
    "x" * 65,
    "garage inverter",
    "garage.inverter",
    "Wechselrichter-Süd",
    "+",
    "#",
    "a\x00b",
)


@pytest.mark.parametrize("device_id", DEVICE_IDS)
def test_the_topic_matcher_and_the_config_validator_agree_on_every_device_id(device_id):
    """An id one accepts and the other refuses is a device that stays dark.

    Either the entry validates and its topics are never matched, or discovery
    offers a device whose entry cannot be applied.
    """

    matched = match_external_topic(["ems-solarflow", device_id, "inverterPower"]) is not None
    entry = {
        "name": "Garage",
        "type": "external_mqtt",
        "mqtt": {"broker_ref": "local_mqtt", "topic_family": "ems_solarflow", "device_id": device_id},
    }
    issues = {issue["code"] for issue in validate_external_mqtt_device_config(entry)}

    assert matched == valid_device_id(device_id)
    assert matched == (not issues & _DEVICE_ID_CODES), issues


@pytest.mark.parametrize("device_id", ["", None, 7, ["garage"]])
def test_a_device_id_that_is_no_string_or_empty_is_invalid(device_id):
    assert not valid_device_id(device_id)
