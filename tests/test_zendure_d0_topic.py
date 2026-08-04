# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for the shared Zendure SmartMeter D0 topic helper (EMS source of truth)."""

import pytest

from ems import config

pytestmark = [
    pytest.mark.mqtt,
    pytest.mark.unit,
    pytest.mark.simulation,
]


def test_topic_generated_from_serial():
    assert config.zendure_smartmeter_d0_topic("D0SN") == "Zendure/sensor/D0SN/totalPower"


def test_topic_strips_surrounding_whitespace():
    assert (
        config.zendure_smartmeter_d0_topic("  D0SN  ")
        == "Zendure/sensor/D0SN/totalPower"
    )


@pytest.mark.parametrize("serial", ["", "   ", None])
def test_empty_or_whitespace_serial_is_rejected(serial):
    with pytest.raises(ValueError):
        config.zendure_smartmeter_d0_topic(serial)


def test_serial_from_generated_topic_round_trips():
    topic = config.zendure_smartmeter_d0_topic("D0SN")
    assert config.zendure_smartmeter_d0_serial_from_topic(topic) == "D0SN"


@pytest.mark.parametrize(
    "topic",
    ["some/custom/topic", "", "Zendure/sensor//totalPower", "Zendure/sensor/D0SN/other"],
)
def test_serial_from_custom_topic_is_empty(topic):
    assert config.zendure_smartmeter_d0_serial_from_topic(topic) == ""


# --- Strict canonical-topic acceptance/rejection (defect 5) -----------------

def test_canonical_topic_is_accepted():
    assert config.is_zendure_smartmeter_d0_topic("Zendure/sensor/D0SERIAL/totalPower")
    assert (
        config.zendure_smartmeter_d0_serial_from_topic(
            "Zendure/sensor/D0SERIAL/totalPower"
        )
        == "D0SERIAL"
    )


@pytest.mark.parametrize(
    "topic",
    [
        "Zendure/number/D0SERIAL/totalPower",
        "Zendure/sensor/D0SERIAL/extra/totalPower",
        "Zendure/sensor//totalPower",
        "Other/sensor/D0SERIAL/totalPower",
        "prefix/Zendure/sensor/D0SERIAL/totalPower",
        "Zendure/sensor/D0SERIAL/totalPower/extra",
        "Zendure/sensor/+/totalPower",
        "Zendure/sensor/#/totalPower",
        "/Zendure/sensor/D0SERIAL/totalPower",
        "Zendure/sensor/D0SERIAL/totalPower/",
    ],
)
def test_non_canonical_topics_are_rejected(topic):
    assert config.is_zendure_smartmeter_d0_topic(topic) is False
    assert config.zendure_smartmeter_d0_serial_from_topic(topic) == ""


@pytest.mark.parametrize("value", [None, 1883, 12.5, ["x"], {"a": 1}, True])
def test_non_string_topic_does_not_crash(value):
    # Malformed/non-string input yields a controlled empty result, never a crash.
    assert config.zendure_smartmeter_d0_serial_from_topic(value) == ""
    assert config.is_zendure_smartmeter_d0_topic(value) is False


@pytest.mark.parametrize("serial", ["a/b", "a+b", "a#b"])
def test_topic_generation_rejects_separator_or_wildcard_serial(serial):
    with pytest.raises(ValueError):
        config.zendure_smartmeter_d0_topic(serial)
