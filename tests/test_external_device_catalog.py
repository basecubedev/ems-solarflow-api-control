# SPDX-License-Identifier: AGPL-3.0-or-later
"""Every device the external catalog lists is known everywhere it has to be.

The catalog is a whitelist: a device is read, discovered, offered, adopted and
kept away from every control path because the catalog lists it, and for no
other reason. Each of those places reads the catalog, but a place that forgot to
would fail by missing something -- a device discovery never offers, a topic the
runtime never subscribes, a key the dashboard never shows -- and nothing would
say so. So this walks the catalog and asks each place about each entry.
"""

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
    return [(f"{entry.prefix}/{DEVICE}/{key}", key) for key in entry.metrics]


def test_the_catalog_is_not_empty_and_its_names_are_distinct():
    assert ENTRIES
    assert len({entry.prefix for entry in ENTRIES}) == len(ENTRIES)
    assert len({entry.label for entry in ENTRIES}) == len(ENTRIES)


@pytest.mark.parametrize("entry", ENTRIES, ids=_ids)
def test_its_family_and_prefix_belong_to_no_zendure_device(entry):
    assert entry.family not in ZENDURE_FAMILIES | SCALAR_FAMILIES | JSON_FAMILIES
    assert entry.prefix not in ("Zendure", "iot", "")
    assert not set(entry.prefix) & set("/+#\x00")
    assert entry.metrics, "an entry that reads nothing is no device"


@pytest.mark.parametrize("entry", ENTRIES, ids=_ids)
def test_the_runtime_classifier_reads_every_key_it_lists(entry):
    for topic, key in _topics(entry):
        match = classify_topic(topic)
        assert match.family == entry.family, topic
        assert match.device_id == DEVICE
        assert match.metric == entry.metrics[key]


@pytest.mark.parametrize("entry", ENTRIES, ids=_ids)
def test_discovery_subscribes_and_classifies_it_as_the_runtime_does(entry):
    assert entry.subscription() in DEFAULT_SUBSCRIPTIONS
    for topic, _key in _topics(entry):
        assert discovery_classify_topic(topic) == classify_topic(topic)


PAYLOADS = (b"765", b"765.5", b" 765\n", b"765 W", b"765,0", b'{"value": 765}', b"n/a", b"", b"true")


@pytest.mark.parametrize("entry", ENTRIES, ids=_ids)
@pytest.mark.parametrize("payload", PAYLOADS)
def test_discovery_offers_it_on_exactly_the_payloads_the_runtime_reads(entry, payload):
    """Topic and payload both: an offer the runtime would never read is a trap."""

    topic, _key = _topics(entry)[0]
    discovery = MqttTopicAggregator({"host": "10.0.0.71", "port": 1883})
    discovery.observe(topic, payload)
    runtime = ZendureMqttAggregator()
    runtime.observe(topic, payload)

    assert bool(discovery.results()) == bool(runtime.snapshots()), payload


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
    for index, (topic, _key) in enumerate(_topics(entry), start=1):
        aggregator.observe(topic, str(index * 100).encode())
    (snapshot,) = aggregator.snapshots()
    assert {entry.metrics[key] for _topic, key in _topics(entry)} <= set(snapshot.metrics)


@pytest.mark.parametrize("entry", ENTRIES, ids=_ids)
def test_every_metric_it_maps_to_is_one_the_device_view_reads(entry):
    empty = parse_device({"properties": {}})
    for metric in set(entry.metrics.values()):
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
    for key in entry.metrics:
        assert f"`{entry.prefix}/<serial>/{key}`" in section
    assert entry.label in section


@pytest.mark.parametrize("entry", ENTRIES, ids=_ids)
def test_the_configuration_reference_lists_it(entry):
    page = (ROOT / "docs" / "technical" / "configuration.md").read_text(encoding="utf-8")
    section = page.split("## External Inverters over MQTT", 1)[1].split("\n## ", 1)[0]
    assert f"`{entry.family}`" in section
    assert f"`{entry.prefix}/<serial>/<key>`" in section
    for key in entry.metrics:
        assert f"`{key}`" in section
