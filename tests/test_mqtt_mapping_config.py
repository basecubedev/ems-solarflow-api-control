# SPDX-License-Identifier: AGPL-3.0-or-later
"""What a mapping file may say, and the code for every way it can be wrong.

The Admin refuses a draft with these codes before it writes the file, and the
service refuses the file with the same ones before it subscribes to anything.
"""

import copy
import json

import pytest

from mqtt_mapping.brokers import load_broker_file
from mqtt_mapping.config import (
    DEFAULT_MAX_AGE_SECONDS,
    DEFAULT_PUBLISH_INTERVAL_SECONDS,
    ISSUE_CODES,
    MAX_BROKERS,
    MAX_DEVICES,
    MAX_SOURCES,
    MAX_SUBSCRIPTIONS,
    TARGETS,
    Mappings,
    load_mappings_file,
    parse_mappings,
    validate_mappings,
)

pytestmark = [
    pytest.mark.unit,
    pytest.mark.mqtt,
]

SHELLY = {"topic": "shellyplusplugs-balkon/status/switch:0", "format": "json", "path": "apower"}


def _document(*devices, **top):
    return {"schema_version": 1, "devices": list(devices), **top}


def _device(device_id="balkon", broker_ref="ems-mqtt", **values):
    return {
        "id": device_id,
        "broker_ref": broker_ref,
        "values": values or {"inverterPower": {"sources": [dict(SHELLY)]}},
    }


def _codes(document, **kwargs):
    return [issue["code"] for issue in validate_mappings(document, **kwargs)]


def test_the_targets_are_the_catalog_keys_and_nothing_else():
    assert TARGETS == ("inverterPower", "solarPower", "batteryPower", "batterySoc")


def test_a_valid_document_becomes_its_model_with_every_default_filled_in():
    battery = {
        "charge": [{"topic": "akku/dc/charge_w", "format": "number"}],
        "discharge": [{"topic": "akku/dc/discharge_w", "format": "number", "factor": 1000}],
    }
    document = _document(
        _device(
            inverterPower={"sources": [dict(SHELLY, invert=True, negative="zero", max_age_seconds=600)]},
            solarPower={"sources": [dict(SHELLY, invert=True)]},
            batteryPower=battery,
            batterySoc={"sources": [{"topic": "akku/bms", "format": "json", "path": "soc", "factor": 100}]},
        )
    )

    mappings, issues = parse_mappings(document, broker_refs={"ems-mqtt"})

    assert issues == []
    assert mappings.publish_interval_seconds == DEFAULT_PUBLISH_INTERVAL_SECONDS
    (device,) = mappings.devices
    assert device.enabled is True and device.label == ""
    assert device.output_topic == "ems-solarflow/balkon/state"
    inverter, solar, battery_target, soc = device.targets
    assert inverter.sources[0].invert is True and inverter.sources[0].max_age_seconds == 600
    assert solar.sources[0].negative == "keep"
    assert solar.sources[0].max_age_seconds == DEFAULT_MAX_AGE_SECONDS
    assert battery_target.split and battery_target.discharge[0].factor == 1000
    assert battery_target.charge[0].path is None
    assert soc.sources[0].factor == 100
    assert mappings.subscriptions() == {
        "ems-mqtt": (
            "shellyplusplugs-balkon/status/switch:0",
            "akku/dc/charge_w",
            "akku/dc/discharge_w",
            "akku/bms",
        )
    }


def test_one_source_may_serve_two_targets_of_one_device():
    document = _document(
        _device(inverterPower={"sources": [dict(SHELLY)]}, solarPower={"sources": [dict(SHELLY)]})
    )

    assert validate_mappings(document) == []


def test_a_disabled_device_is_kept_but_subscribes_to_nothing():
    mappings, _ = parse_mappings(_document(dict(_device(), enabled=False)))

    assert mappings.devices[0].enabled is False
    assert mappings.active_devices() == ()
    assert mappings.subscriptions() == {}


def _with_source(**changes):
    source = dict(SHELLY)
    for key, value in changes.items():
        if value is ...:
            source.pop(key, None)
        else:
            source[key] = value
    return _document(_device(inverterPower={"sources": [source]}))


RULE_CASES = [
        ([], "mqtt_mapping_document_invalid"),
        ({"schema_version": 1, "devices": {}}, "mqtt_mapping_document_invalid"),
        (_document("balkon"), "mqtt_mapping_document_invalid"),
        ({"schema_version": 2, "devices": []}, "mqtt_mapping_schema_unsupported"),
        ({"devices": []}, "mqtt_mapping_schema_unsupported"),
        (_document(publish_interval_seconds=4), "mqtt_mapping_option_invalid"),
        (_document(publish_interval_seconds=31), "mqtt_mapping_option_invalid"),
        (_document(publish_interval_seconds=True), "mqtt_mapping_option_invalid"),
        (_document(_device(device_id="bal kon")), "mqtt_mapping_device_id_invalid"),
        (_document(_device(device_id="+")), "mqtt_mapping_device_id_invalid"),
        (_document(_device(device_id="redacted")), "mqtt_mapping_device_id_invalid"),
        (_document(_device(), _device()), "mqtt_mapping_device_id_duplicate"),
        (_document(dict(_device(), label=5)), "mqtt_mapping_option_invalid"),
        (_document(dict(_device(), label="x" * 65)), "mqtt_mapping_option_invalid"),
        (_document(dict(_device(), enabled="yes")), "mqtt_mapping_option_invalid"),
        (_document(_device(broker_ref="")), "mqtt_mapping_broker_invalid"),
        (_document(_device(broker_ref=None)), "mqtt_mapping_broker_invalid"),
        (_document(_device(broker_ref="x" * 65)), "mqtt_mapping_broker_invalid"),
        (
            _document(_device(inverterPower={"sources": [dict(SHELLY)]}, dailyYield={"sources": []})),
            "mqtt_mapping_target_unknown",
        ),
        (_document(_device(solarPower={"sources": [dict(SHELLY)]})), "mqtt_mapping_target_required_missing"),
        (
            _document(_device(inverterPower={"charge": [dict(SHELLY)]})),
            "mqtt_mapping_battery_shape_invalid",
        ),
        (
            _document(
                _device(
                    inverterPower={"sources": [dict(SHELLY)]},
                    batteryPower={"sources": [dict(SHELLY, path="x")], "charge": [dict(SHELLY, path="y")]},
                )
            ),
            "mqtt_mapping_battery_shape_invalid",
        ),
        (
            _document(_device(inverterPower={"sources": [dict(SHELLY)]}, batteryPower={})),
            "mqtt_mapping_battery_shape_invalid",
        ),
        (
            _document(
                _device(
                    inverterPower={"sources": [dict(SHELLY)]},
                    batteryPower={"charge": [], "discharge": []},
                )
            ),
            "mqtt_mapping_battery_shape_invalid",
        ),
        (
            _document(_device(inverterPower={"sources": [dict(SHELLY)], "factor": 2})),
            "mqtt_mapping_battery_shape_invalid",
        ),
        (_document(_device(inverterPower={"sources": []})), "mqtt_mapping_option_invalid"),
        (
            _document(_device(inverterPower={"sources": [dict(SHELLY, path=f"p{i}") for i in range(MAX_SOURCES + 1)]})),
            "mqtt_mapping_option_invalid",
        ),
        (_with_source(topic="shellies/+/relay/0/power"), "mqtt_mapping_topic_invalid"),
        (_with_source(topic="shellies/#"), "mqtt_mapping_topic_invalid"),
        (_with_source(topic="$SYS/broker/load"), "mqtt_mapping_topic_invalid"),
        (_with_source(topic="ems-solarflow/other/state"), "mqtt_mapping_topic_invalid"),
        (_with_source(topic="a" * 257), "mqtt_mapping_topic_invalid"),
        (_with_source(topic=""), "mqtt_mapping_topic_invalid"),
        (_with_source(topic="a\nb"), "mqtt_mapping_topic_invalid"),
        (_with_source(topic=5), "mqtt_mapping_topic_invalid"),
        (_with_source(topic="iot/pk/dev/properties/report"), "mqtt_mapping_topic_native"),
        (_with_source(topic="Zendure/sensor/SN/totalPower"), "mqtt_mapping_topic_native"),
        (_with_source(format="text"), "mqtt_mapping_path_invalid"),
        (_with_source(path=...), "mqtt_mapping_path_invalid"),
        (_with_source(path="a..b"), "mqtt_mapping_path_invalid"),
        (_with_source(path=".".join("a" * 9)), "mqtt_mapping_path_invalid"),
        (_with_source(format="number"), "mqtt_mapping_path_invalid"),
        (_with_source(factor=0), "mqtt_mapping_option_invalid"),
        (_with_source(factor=1_000_001), "mqtt_mapping_option_invalid"),
        (_with_source(factor=True), "mqtt_mapping_option_invalid"),
        (_with_source(factor="2"), "mqtt_mapping_option_invalid"),
        (_with_source(invert="yes"), "mqtt_mapping_option_invalid"),
        (_with_source(negative="clip"), "mqtt_mapping_option_invalid"),
        (_with_source(max_age_seconds=9), "mqtt_mapping_option_invalid"),
        (_with_source(max_age_seconds=901), "mqtt_mapping_option_invalid"),
        (_with_source(max_age_seconds=60.5), "mqtt_mapping_option_invalid"),
        (
            _document(_device(device_id="a"), _device(device_id="b")),
            "mqtt_mapping_source_shared",
        ),
        (
            _document(_device(inverterPower={"sources": [dict(SHELLY), dict(SHELLY)]})),
            "mqtt_mapping_source_shared",
        ),
]


@pytest.mark.parametrize(("document", "code"), RULE_CASES)
def test_a_document_that_breaks_a_rule_names_it(document, code):
    assert code in _codes(document)
    assert parse_mappings(document)[0] is None


def test_a_broker_the_caller_does_not_offer_is_refused():
    document = _document(_device(broker_ref="iobroker"))

    assert _codes(document, broker_refs={"ems-mqtt"}) == ["mqtt_mapping_broker_invalid"]
    assert _codes(document, broker_refs={"ems-mqtt", "iobroker"}) == []


def test_the_same_topic_on_two_brokers_is_two_sources():
    document = _document(_device(device_id="a"), _device(device_id="b", broker_ref="iobroker"))

    assert validate_mappings(document) == []


def test_too_many_devices_brokers_or_topics_are_refused():
    too_many_devices = _document(
        *[_device(device_id=f"d{i}", inverterPower={"sources": [{"topic": f"t/{i}", "format": "number"}]})
          for i in range(MAX_DEVICES + 1)]
    )
    too_many_brokers = _document(
        *[_device(device_id=f"d{i}", broker_ref=f"b{i}") for i in range(MAX_BROKERS + 1)]
    )
    many_topics = [{"topic": f"t/{i}", "format": "number"} for i in range(MAX_SUBSCRIPTIONS + 1)]
    too_many_topics = _document(
        *[
            _device(device_id=f"d{start}", inverterPower={"sources": many_topics[start : start + MAX_SOURCES]})
            for start in range(0, len(many_topics), MAX_SOURCES)
        ]
    )

    for document in (too_many_devices, too_many_brokers, too_many_topics):
        assert "mqtt_mapping_limit_exceeded" in _codes(document)


def test_every_issue_code_is_listed_and_every_listed_code_happens(tmp_path):
    """The list is what the Admin's translations are paired against."""

    produced = set()
    for document, _code in RULE_CASES:
        produced.update(_codes(document))
    produced.update(
        _codes(_document(*[_device(device_id=f"d{i}", broker_ref=f"b{i}") for i in range(MAX_BROKERS + 1)]))
    )
    broken = tmp_path / "brokers.json"
    broken.write_text("{", encoding="utf-8")
    produced.update(issue["code"] for issue in load_broker_file(broken)[1])

    assert produced == set(ISSUE_CODES)


def test_issues_carry_a_path_and_a_message_and_never_a_value():
    document = _document(_device(inverterPower={"sources": [dict(SHELLY, factor=0)]}))

    (issue,) = validate_mappings(document)

    assert issue == {
        "code": "mqtt_mapping_option_invalid",
        "path": "devices[0].values.inverterPower.sources[0].factor",
        "message": issue["message"],
    }
    assert issue["message"]


def test_validation_does_not_change_the_document():
    document = _document(_device())
    before = copy.deepcopy(document)

    parse_mappings(document)

    assert document == before


def test_no_file_is_no_mappings_and_no_error(tmp_path):
    assert load_mappings_file(tmp_path / "mappings.json") == (Mappings(), [])


def test_a_file_that_is_no_json_is_refused(tmp_path):
    path = tmp_path / "mappings.json"
    path.write_text("{", encoding="utf-8")

    mappings, issues = load_mappings_file(path)

    assert mappings is None
    assert [issue["code"] for issue in issues] == ["mqtt_mapping_document_invalid"]


def test_a_file_is_parsed_like_the_document(tmp_path):
    path = tmp_path / "mappings.json"
    path.write_text(json.dumps(_document(_device())), encoding="utf-8")

    mappings, issues = load_mappings_file(path, broker_refs={"ems-mqtt"})

    assert issues == [] and mappings.devices[0].id == "balkon"
