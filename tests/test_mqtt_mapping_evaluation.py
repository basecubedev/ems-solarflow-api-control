# SPDX-License-Identifier: AGPL-3.0-or-later
"""The calculation from a source payload to a published value, as a table.

Factor, sign, the negative rule, sums, charge minus discharge, freshness and the
catalog's own gate, each in isolation; a device publishes only when every
target passes, so a dead power source cannot hide behind a live charge level.
"""

import json

import pytest

from mqtt_mapping.config import parse_mappings
from mqtt_mapping.mapping import (
    PAYLOAD_PREVIEW_CHARS,
    SILENCE_REASONS,
    Received,
    evaluate_device,
    preview_device,
    source_value,
)

pytestmark = [
    pytest.mark.unit,
    pytest.mark.mqtt,
]


def _device(enabled=True, **values):
    document = {
        "schema_version": 1,
        "devices": [{"id": "balkon", "broker_ref": "ems-mqtt", "enabled": enabled, "values": values}],
    }
    mappings, issues = parse_mappings(document)
    assert issues == []
    return mappings.devices[0]


def _source(**fields):
    device = _device(inverterPower={"sources": [{"topic": "t", "format": "number", **fields}]})
    return device.targets[0].sources[0]


def _at(payloads, at=100.0):
    return {topic: Received(json.dumps(value).encode() if not isinstance(value, bytes) else value, at)
            for topic, value in payloads.items()}


@pytest.mark.parametrize(
    ("fields", "payload", "expected"),
    [
        ({}, b"612.3", 612.3),
        ({"factor": 1000}, b"1.5", 1500.0),
        ({"factor": 100}, b"0.87", 87.0),
        ({"invert": True}, b"-612.3", 612.3),
        ({"invert": True}, b"20", -20),
        ({"negative": "zero"}, b"-0.4", 0),
        ({"negative": "zero"}, b"5", 5),
        ({"invert": True, "negative": "zero"}, b"3", 0),
        ({"factor": -1, "negative": "zero"}, b"-7", 7),
    ],
)
def test_a_source_value_is_factor_then_sign_then_the_negative_rule(fields, payload, expected):
    assert source_value(_source(**fields), payload) == (pytest.approx(expected), None)


def test_a_source_that_is_no_number_says_why():
    assert source_value(_source(), b"612 W") == (None, "not_a_number")


def test_a_device_publishes_every_target_from_fresh_sources():
    shelly = {"topic": "plug/status", "format": "json", "path": "apower", "invert": True, "negative": "zero"}
    device = _device(inverterPower={"sources": [shelly]}, solarPower={"sources": [shelly]})

    evaluation = evaluate_device(device, _at({"plug/status": {"apower": -612.3}}), 101.0)

    assert evaluation.state == {"inverterPower": 612.3, "solarPower": 612.3}
    assert evaluation.reason is None
    assert [(source.topic, source.value, source.age_seconds) for source in evaluation.sources] == [
        ("plug/status", 612.3, 1.0),
        ("plug/status", 612.3, 1.0),
    ]


def test_sources_of_one_target_add_up():
    phases = [{"topic": f"em/{n}", "format": "number"} for n in range(3)]
    device = _device(inverterPower={"sources": phases})

    evaluation = evaluate_device(device, _at({"em/0": 100.1, "em/1": 200.2, "em/2": 0.3}), 100.0)

    assert evaluation.state == {"inverterPower": 300.6}


def test_a_split_battery_is_charge_minus_discharge_and_one_side_may_be_missing():
    both = _device(
        inverterPower={"sources": [{"topic": "ac", "format": "number"}]},
        batteryPower={
            "charge": [{"topic": "in", "format": "number"}],
            "discharge": [{"topic": "out", "format": "number"}],
        },
    )
    only_discharge = _device(
        inverterPower={"sources": [{"topic": "ac", "format": "number"}]},
        batteryPower={"discharge": [{"topic": "out", "format": "number"}]},
    )
    payloads = _at({"ac": 300, "in": 0, "out": 320})

    assert evaluate_device(both, payloads, 100.0).state == {"inverterPower": 300, "batteryPower": -320}
    assert evaluate_device(only_discharge, payloads, 100.0).state == {
        "inverterPower": 300,
        "batteryPower": -320,
    }


def test_a_stale_source_silences_the_whole_device_even_when_another_is_fresh():
    device = _device(
        inverterPower={"sources": [{"topic": "power", "format": "number", "max_age_seconds": 60}]},
        batterySoc={"sources": [{"topic": "soc", "format": "number"}]},
    )
    latest = {"power": Received(b"500", 0.0), "soc": Received(b"80", 100.0)}

    evaluation = evaluate_device(device, latest, 100.0)

    assert evaluation.state is None
    assert evaluation.reason == "stale"
    assert evaluation.targets[0].reason == "stale"
    assert evaluation.targets[1].value == 80


def test_a_source_exactly_at_its_maximum_age_is_still_fresh():
    device = _device(inverterPower={"sources": [{"topic": "power", "format": "number", "max_age_seconds": 60}]})

    assert evaluate_device(device, {"power": Received(b"5", 40.0)}, 100.0).state == {"inverterPower": 5}


def test_a_source_never_received_is_missing():
    device = _device(inverterPower={"sources": [{"topic": "power", "format": "number"}]})

    evaluation = evaluate_device(device, {}, 100.0)

    assert (evaluation.state, evaluation.reason) == (None, "source_missing")


def test_an_unreadable_source_silences_the_device_and_keeps_its_own_reason():
    device = _device(inverterPower={"sources": [{"topic": "p", "format": "json", "path": "apower"}]})

    evaluation = evaluate_device(device, _at({"p": {"voltage": 230}}), 100.0)

    assert evaluation.reason == "source_unreadable"
    assert evaluation.sources[0].reason == "path_missing"


@pytest.mark.parametrize(
    ("values", "payloads"),
    [
        ({"inverterPower": {"sources": [{"topic": "p", "format": "number"}]}}, {"p": -5}),
        (
            {
                "inverterPower": {"sources": [{"topic": "p", "format": "number"}]},
                "batterySoc": {"sources": [{"topic": "s", "format": "number", "factor": 100}]},
            },
            {"p": 5, "s": 1.2},
        ),
        ({"inverterPower": {"sources": [{"topic": "p", "format": "number", "factor": 1000}]}}, {"p": 1001}),
    ],
)
def test_a_value_the_catalog_would_refuse_is_never_published(values, payloads):
    evaluation = evaluate_device(_device(**values), _at(payloads), 100.0)

    assert evaluation.state is None
    assert evaluation.reason == "catalog_refused"


def test_a_disabled_device_publishes_nothing():
    device = _device(enabled=False, inverterPower={"sources": [{"topic": "p", "format": "number"}]})

    assert evaluate_device(device, _at({"p": 5}), 100.0).reason == "disabled"


def test_values_are_rounded_to_milliwatts_and_whole_numbers_stay_whole():
    device = _device(inverterPower={"sources": [{"topic": "a", "format": "number"}, {"topic": "b", "format": "number"}]})

    assert evaluate_device(device, _at({"a": 0.1, "b": 0.2}), 100.0).state == {"inverterPower": 0.3}
    assert evaluate_device(device, _at({"a": 100.5, "b": 99.5}), 100.0).state == {"inverterPower": 200}


def test_the_payload_preview_is_cut_short():
    device = _device(inverterPower={"sources": [{"topic": "p", "format": "json", "path": "w"}]})
    payload = json.dumps({"w": 5, "pad": "x" * 1000}).encode()

    evaluation = evaluate_device(device, {"p": Received(payload, 100.0)}, 100.0)

    assert len(evaluation.sources[0].payload_preview) == PAYLOAD_PREVIEW_CHARS


def test_a_preview_is_what_the_service_would_publish():
    device = _device(inverterPower={"sources": [{"topic": "p", "format": "json", "path": "apower", "invert": True}]})

    assert preview_device(device, {"p": '{"apower": -612.3}'}).state == {"inverterPower": 612.3}
    assert preview_device(device, {"p": '{"apower": 612.3}'}).reason == "catalog_refused"


def test_every_reason_a_device_falls_silent_for_is_listed_and_happens():
    values = {"inverterPower": {"sources": [{"topic": "p", "format": "number", "max_age_seconds": 10}]}}
    device = _device(**values)
    reasons = {
        evaluate_device(_device(enabled=False, **values), {}, 0.0).reason,
        evaluate_device(device, {}, 0.0).reason,
        evaluate_device(device, {"p": Received(b"5", 0.0)}, 100.0).reason,
        evaluate_device(device, {"p": Received(b"x", 0.0)}, 0.0).reason,
        evaluate_device(device, {"p": Received(b"-5", 0.0)}, 0.0).reason,
    }

    assert reasons == set(SILENCE_REASONS)
