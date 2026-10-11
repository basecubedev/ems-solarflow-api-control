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
def test_the_configuration_reference_lists_it(entry):
    page = (ROOT / "docs" / "technical" / "configuration.md").read_text(encoding="utf-8")
    section = page.split("## External Devices over MQTT", 1)[1].split("\n## ", 1)[0]
    assert f"`{entry.family}`" in section
    assert f"`{entry.prefix}/<id>/<key>`" in section
    assert f"`{entry.prefix}/<id>/{entry.bundle_key}`" in section
    for key in entry.keys:
        assert f"`{key}`" in section
