# SPDX-License-Identifier: AGPL-3.0-or-later
"""Every device the external catalog lists is known everywhere it has to be.

The catalog is a whitelist: a device is read, discovered, offered, adopted and
kept away from every control path because the catalog lists it, and for no
other reason. Each of those places reads the catalog, but a place that forgot to
would fail by missing something -- a device discovery never offers, a topic the
runtime never subscribes, a key the dashboard never shows -- and nothing would
say so. So this walks the catalog and asks each place about each entry.
"""

import json
from pathlib import Path

import pytest

from admin.mqtt_topic_discovery import DEFAULT_SUBSCRIPTIONS, MqttTopicAggregator
from admin.mqtt_topic_discovery import classify_topic as discovery_classify_topic
from admin.zendure_mqtt_config_proposals import build_proposals
from ems.clients import parse_device
from ems.config import http_control_device_configs, mqtt_control_device_configs
from ems.diagnostics import diagnose_ac_charge_model_refusal
from ems.mqtt_control.topic_families import (
    FAMILY_LEGACY_JSON,
    FAMILY_LEGACY_JSON_ALT,
    FAMILY_ZENDURE_CLOUD_SCALAR,
    FAMILY_ZENSDK_HA_SCALAR,
    JSON_FAMILIES,
    SCALAR_FAMILIES,
)
from ems.zendure_mqtt.config_entries import (
    external_device_subscriptions,
    validate_external_mqtt_device_config,
)
from ems.zendure_mqtt.external_catalog import EXTERNAL_TOPIC_FAMILIES
from ems.zendure_mqtt.snapshot import ZendureMqttAggregator
from ems.zendure_mqtt.topics import classify_topic

pytestmark = [
    pytest.mark.contract,
    pytest.mark.mqtt,
]

ROOT = Path(__file__).resolve().parents[1]
DEVICE = "EXAMPLE0000001"
ENTRIES = sorted(EXTERNAL_TOPIC_FAMILIES.values(), key=lambda entry: entry.family)
ZENDURE_FAMILIES = {
    FAMILY_ZENSDK_HA_SCALAR,
    FAMILY_LEGACY_JSON,
    FAMILY_LEGACY_JSON_ALT,
    FAMILY_ZENDURE_CLOUD_SCALAR,
}


def _ids(entry):
    return entry.family


def _entry_config(entry, broker_ref="house"):
    return {
        "name": entry.label,
        "type": "external_mqtt",
        "mqtt": {
            "broker_ref": broker_ref,
            "topic_family": entry.family,
            "device_id": DEVICE,
        },
    }


def _topics(entry):
    return [(f"{entry.prefix}/{DEVICE}/{key}", key) for key in entry.keys]


def _all_metrics(entry):
    return {metric for spec in entry.keys.values() for metric in spec.metrics}


def _required_topic(entry):
    return f"{entry.prefix}/{DEVICE}/{entry.required_key}"


def test_the_catalog_is_not_empty_and_its_names_are_distinct():
    assert ENTRIES
    assert len({entry.prefix for entry in ENTRIES}) == len(ENTRIES)
    assert len({entry.label for entry in ENTRIES}) == len(ENTRIES)


@pytest.mark.parametrize("entry", ENTRIES, ids=_ids)
def test_its_family_and_prefix_belong_to_no_zendure_device(entry):
    assert entry.family not in ZENDURE_FAMILIES | SCALAR_FAMILIES | JSON_FAMILIES
    assert entry.prefix not in ("Zendure", "iot", "")
    assert not set(entry.prefix) & set("/+#\x00")
    assert entry.keys, "an entry that reads nothing is no device"


@pytest.mark.parametrize("entry", ENTRIES, ids=_ids)
def test_its_required_key_is_a_key_and_its_bundle_is_not(entry):
    assert entry.required_key in entry.keys
    assert entry.bundle_key not in entry.keys


@pytest.mark.parametrize("entry", ENTRIES, ids=_ids)
def test_every_key_produces_exactly_the_metrics_it_declares(entry):
    """``CatalogKey.metrics`` is what discovery requires and what the docs list.

    A reader that produced a metric it does not declare, or declared one it never
    produces, would make the two disagree without failing anything else.
    """

    for key, spec in entry.keys.items():
        assert set(entry.readings(key, b"1")) == set(spec.metrics), key


@pytest.mark.parametrize("entry", ENTRIES, ids=_ids)
def test_the_bundle_reads_every_key_as_its_own_topic_does(entry):
    for key in entry.keys:
        bundle = entry.readings(entry.bundle_key, json.dumps({key: 1}).encode())
        assert bundle == entry.readings(key, b"1"), key


@pytest.mark.parametrize("entry", ENTRIES, ids=_ids)
def test_the_runtime_classifier_reads_every_key_it_lists(entry):
    for topic, key in _topics(entry) + [
        (f"{entry.prefix}/{DEVICE}/{entry.bundle_key}", entry.bundle_key)
    ]:
        match = classify_topic(topic)
        assert match.family == entry.family, topic
        assert match.device_id == DEVICE
        assert match.metric == key


@pytest.mark.parametrize("entry", ENTRIES, ids=_ids)
def test_discovery_subscribes_and_classifies_it_as_the_runtime_does(entry):
    assert entry.subscription() in DEFAULT_SUBSCRIPTIONS
    for topic, _key in _topics(entry):
        assert discovery_classify_topic(topic) == classify_topic(topic)


PAYLOADS = (
    b"765",
    b"765.5",
    b" 765\n",
    b"0",
    b"-5",
    b"765 W",
    b"765,0",
    b'{"value": 765}',
    b"n/a",
    b"",
    b"true",
    b"nan",
)


@pytest.mark.parametrize("entry", ENTRIES, ids=_ids)
@pytest.mark.parametrize("payload", PAYLOADS)
def test_discovery_offers_it_on_exactly_the_payloads_the_runtime_reads(entry, payload):
    """Topic and payload both: an offer the runtime would never read is a trap."""

    topic = _required_topic(entry)
    discovery = MqttTopicAggregator({"host": "10.0.0.71", "port": 1883})
    discovery.observe(topic, payload)
    runtime = ZendureMqttAggregator()
    runtime.observe(topic, payload)

    assert bool(discovery.results()) == bool(runtime.snapshots()), payload


@pytest.mark.parametrize("entry", ENTRIES, ids=_ids)
def test_discovery_offers_nothing_until_the_required_key_arrives(entry):
    """Optional keys alone are no device; the required one makes it one."""

    discovery = MqttTopicAggregator({"host": "10.0.0.71", "port": 1883})
    for topic, key in _topics(entry):
        if key != entry.required_key:
            discovery.observe(topic, b"1")
    assert discovery.results() == []

    discovery.observe(_required_topic(entry), b"1")
    (candidate,) = discovery.results()
    assert set(candidate["metrics_seen"]) == _all_metrics(entry)


@pytest.mark.parametrize("entry", ENTRIES, ids=_ids)
def test_core_proposes_nothing_for_an_observation_without_the_required_key(entry):
    """The rule holds in Core too, for any observation that reaches the mapper."""

    def observation(metrics):
        return {
            "broker_host": "10.0.0.71",
            "broker_port": 1883,
            "topic_family": entry.family,
            "device_id": DEVICE,
            "serial_number": DEVICE,
            "metrics_seen": sorted(metrics),
            "topics_seen": [topic for topic, _key in _topics(entry)],
        }

    required = set(entry.keys[entry.required_key].metrics)
    assert build_proposals([observation(_all_metrics(entry) - required)]) == []
    assert len(build_proposals([observation(_all_metrics(entry))])) == 1


@pytest.mark.parametrize("entry", ENTRIES, ids=_ids)
def test_zendure_traffic_cannot_use_up_the_budget_before_a_catalog_device_reports(entry):
    discovery = MqttTopicAggregator({"host": "10.0.0.71", "port": 1883}, max_topics=10)
    for n in range(50):
        discovery.observe(f"Zendure/sensor/ZEN{n % 3}/metric{n}", b"1")

    discovery.observe(_required_topic(entry), b"765")

    families = {candidate["topic_family"] for candidate in discovery.results()}
    assert entry.family in families
    assert FAMILY_ZENSDK_HA_SCALAR in families


@pytest.mark.parametrize("entry", ENTRIES, ids=_ids)
def test_the_catalog_namespace_has_a_bounded_budget_of_its_own(entry):
    """A bridge publishing more than the catalog reads starves nothing else."""

    discovery = MqttTopicAggregator({"host": "10.0.0.71", "port": 1883}, max_topics=10)
    for n in range(50):
        discovery.observe(f"{entry.prefix}/dev{n}/dailyYield", b"1")
    discovery.observe(f"{entry.prefix}/late/{entry.required_key}", b"765")
    discovery.observe("Zendure/sensor/ZEN0/electricLevel", b"55")

    assert discovery.topics_seen_count == 10 + 1
    families = {candidate["topic_family"] for candidate in discovery.results()}
    assert families == {FAMILY_ZENSDK_HA_SCALAR}


@pytest.mark.parametrize("entry", ENTRIES, ids=_ids)
def test_two_ids_that_differ_only_in_case_are_two_devices(entry):
    """Topic segments are case-sensitive, and so is everything that reads them."""

    discovery = MqttTopicAggregator({"host": "10.0.0.71", "port": 1883})
    discovery.observe(f"{entry.prefix}/Garage/{entry.required_key}", b"1")
    discovery.observe(f"{entry.prefix}/garage/{entry.required_key}", b"2")

    proposals = build_proposals(discovery.results())

    assert sorted(proposal["device_id"] for proposal in proposals) == ["Garage", "garage"]


ZENDURE_SHARING_THE_ID = (
    ("Zendure/sensor/ABC123/electricLevel", b"80"),
    ("iot/PK1/ABC123/properties/report", b'{"properties": {"electricLevel": 80}}'),
)


@pytest.mark.parametrize("entry", ENTRIES, ids=_ids)
@pytest.mark.parametrize("zendure", ZENDURE_SHARING_THE_ID, ids=lambda message: message[0])
def test_a_zendure_device_and_a_catalog_device_sharing_an_id_are_two_offers(entry, zendure):
    """Neither offer takes the other's metrics or topics, and both can be adopted."""

    alone = MqttTopicAggregator({"host": "10.0.0.71", "port": 1883})
    alone.observe(*zendure)
    (expected,) = build_proposals(alone.results())

    both = MqttTopicAggregator({"host": "10.0.0.71", "port": 1883})
    both.observe(*zendure)
    both.observe(f"{entry.prefix}/ABC123/{entry.required_key}", b"765")
    offers = {proposal["topic_family"]: proposal for proposal in build_proposals(both.results())}

    assert set(offers) == {expected["topic_family"], entry.family}
    zendure_offer = offers[expected["topic_family"]]
    for field in ("id", "metrics", "seen_topics", "config_fragment"):
        assert zendure_offer[field] == expected[field], field
    assert offers[entry.family]["device_id"] == "ABC123"
    assert offers[entry.family]["config_fragment"]["type"] == "external_mqtt"


@pytest.mark.parametrize("entry", ENTRIES, ids=_ids)
def test_discovery_finds_it_from_its_bundle_alone(entry):
    discovery = MqttTopicAggregator({"host": "10.0.0.71", "port": 1883})
    discovery.observe(
        f"{entry.prefix}/{DEVICE}/{entry.bundle_key}",
        json.dumps({key: 1 for key in entry.keys}).encode(),
    )

    (candidate,) = discovery.results()
    (proposal,) = build_proposals([candidate])
    assert proposal["config_fragment"]["mqtt"]["device_id"] == DEVICE


@pytest.mark.parametrize("entry", ENTRIES, ids=_ids)
def test_discovery_offers_it_as_a_read_only_device_entry(entry):
    aggregator = MqttTopicAggregator({"host": "10.0.0.71", "port": 1883})
    for topic, _key in _topics(entry):
        aggregator.observe(topic, b"1")
    (candidate,) = aggregator.results()
    assert candidate["display_name"] == entry.label

    (proposal,) = build_proposals([candidate])

    fragment = proposal["config_fragment"]
    assert fragment["type"] == "external_mqtt"
    assert fragment["mqtt"]["topic_family"] == entry.family
    assert proposal["catalog_label"] == entry.label
    assert proposal["output_control_supported"] is False
    assert validate_external_mqtt_device_config(
        fragment, broker_sources={fragment["mqtt"]["broker_ref"]: "local_mqtt"}
    ) == []


@pytest.mark.parametrize("entry", ENTRIES, ids=_ids)
def test_the_runtime_subscribes_and_reads_every_key(entry):
    assert external_device_subscriptions([_entry_config(entry)], "house") == (
        entry.subscription(DEVICE),
    )
    aggregator = ZendureMqttAggregator()
    for topic, _key in _topics(entry):
        aggregator.observe(topic, b"50")
    (snapshot,) = aggregator.snapshots()
    assert _all_metrics(entry) <= set(snapshot.metrics)


@pytest.mark.parametrize("entry", ENTRIES, ids=_ids)
def test_every_metric_it_maps_to_is_one_the_device_view_reads(entry):
    empty = parse_device({"properties": {}})
    for metric in _all_metrics(entry):
        assert parse_device({"properties": {metric: 123}}) != empty, metric


@pytest.mark.parametrize("entry", ENTRIES, ids=_ids)
def test_it_never_reaches_a_control_path(entry):
    device = _entry_config(entry)
    assert validate_external_mqtt_device_config(device) == []
    assert http_control_device_configs([device]) == []
    assert mqtt_control_device_configs([device]) == []
    assert diagnose_ac_charge_model_refusal(device) == "telemetry_only"


@pytest.mark.parametrize("entry", ENTRIES, ids=_ids)
def test_the_supported_setups_page_lists_it(entry):
    page = (ROOT / "docs" / "user" / "supported-setups.md").read_text(encoding="utf-8")
    section = page.split("## External devices (read only)", 1)[1].split("\n## ", 1)[0]
    for key in (*entry.keys, entry.bundle_key):
        assert f"`{entry.prefix}/<id>/{key}`" in section
    assert entry.label in section


@pytest.mark.parametrize("entry", ENTRIES, ids=_ids)
def test_the_configuration_reference_lists_it(entry):
    page = (ROOT / "docs" / "technical" / "configuration.md").read_text(encoding="utf-8")
    section = page.split("## External Devices over MQTT", 1)[1].split("\n## ", 1)[0]
    assert f"`{entry.family}`" in section
    assert f"`{entry.prefix}/<id>/<key>`" in section
    assert f"`{entry.prefix}/<id>/{entry.bundle_key}`" in section
    for key in entry.keys:
        assert f"`{key}`" in section
