# SPDX-License-Identifier: AGPL-3.0-or-later
"""How a number comes out of a device payload, and every reason it does not.

The external-device catalog and the MQTT mapping read numbers with the same
rule, so a value the mapping republishes is one the catalog reads the same way.
The catalog's own cases in ``test_external_device_namespace.py`` pin the rule;
these pin the paths and reasons the mapping adds around it.
"""

import json

import pytest

from ems.zendure_mqtt import external_catalog
from ems.zendure_mqtt.payload_values import (
    EXTRACT_REASONS,
    MAX_PATH_SEGMENTS,
    extract_number,
    json_path_value,
    plain_number,
    valid_json_path,
)
from ems.zendure_mqtt.payloads import MAX_PAYLOAD_BYTES

pytestmark = [
    pytest.mark.unit,
    pytest.mark.mqtt,
]

SHELLY_GEN2_SWITCH = json.dumps(
    {"id": 0, "apower": -612.3, "voltage": 231.4, "aenergy": {"total": 1234.5}}
).encode()
TASMOTA_SENSOR = json.dumps({"Time": "2026-10-30T12:00:00", "ENERGY": {"Power": 412}}).encode()
SHELLY_GEN1_3EM = json.dumps({"emeters": [{"power": 100.5}, {"power": -20}, {"power": 3}]}).encode()
SHELLY_PRO_3EM_RPC = json.dumps({"params": {"em:0": {"total_act_power": 1520.7}}}).encode()


def test_the_catalog_reads_numbers_with_this_rule_and_no_copy_of_it():
    assert external_catalog.plain_number is plain_number


# --- a whole payload ----------------------------------------------------------


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        (b"765", (765, None)),
        (b" -612.3\n", (-612.3, None)),
        ("42", (42, None)),
        (7, (7, None)),
        (0.5, (0.5, None)),
        (b"1000000", (1000000, None)),
        (b"-1000000", (-1000000, None)),
    ],
)
def test_a_number_payload_is_read_whole(payload, expected):
    assert extract_number(payload, "number") == expected


@pytest.mark.parametrize(
    ("payload", "reason"),
    [
        (b"1000001", "out_of_range"),
        (b"-1000001", "out_of_range"),
        (10**400, "out_of_range"),
        (float("inf"), "out_of_range"),
        (float("nan"), "not_a_number"),
        (b"765 W", "not_a_number"),
        (b"765,0", "not_a_number"),
        (b"1e3", "not_a_number"),
        (b"1_000", "not_a_number"),
        (b"nan", "not_a_number"),
        (b"", "not_a_number"),
        (b"\xff\xfe", "not_a_number"),
        (True, "not_a_number"),
        (None, "not_a_number"),
        (b'{"apower": 5}', "not_a_number"),
        (b"1" * 5000, "not_a_number"),
    ],
)
def test_a_number_payload_that_is_no_reading_says_why(payload, reason):
    assert extract_number(payload, "number") == (None, reason)


@pytest.mark.parametrize("payload_format", ["text", "JSON", "", None])
def test_only_the_two_formats_are_read(payload_format):
    assert extract_number(b"5", payload_format) == (None, "format_invalid")


# --- a value inside a JSON object -----------------------------------------------


@pytest.mark.parametrize(
    ("payload", "path", "expected"),
    [
        (SHELLY_GEN2_SWITCH, "apower", -612.3),
        (SHELLY_GEN2_SWITCH, "aenergy.total", 1234.5),
        (TASMOTA_SENSOR, "ENERGY.Power", 412),
        (SHELLY_GEN1_3EM, "emeters.0.power", 100.5),
        (SHELLY_GEN1_3EM, "emeters.1.power", -20),
        (SHELLY_PRO_3EM_RPC, "params.em:0.total_act_power", 1520.7),
        (json.dumps({"soc": "87"}).encode(), "soc", 87),
        (json.dumps({"0": 5}).encode(), "0", 5),
    ],
)
def test_a_json_payload_is_read_at_its_path(payload, path, expected):
    assert extract_number(payload, "json", path) == (expected, None)


@pytest.mark.parametrize(
    ("payload", "path", "reason"),
    [
        (SHELLY_GEN2_SWITCH, "power", "path_missing"),
        (SHELLY_GEN2_SWITCH, "apower.value", "path_missing"),
        (SHELLY_GEN1_3EM, "emeters.3.power", "path_missing"),
        (SHELLY_GEN1_3EM, "emeters.01.power", "path_missing"),
        (SHELLY_GEN1_3EM, "emeters.-1.power", "path_missing"),
        (SHELLY_GEN1_3EM, "emeters.power", "path_missing"),
        (SHELLY_GEN1_3EM, "emeters", "not_a_number"),
        (SHELLY_GEN2_SWITCH, "aenergy", "not_a_number"),
        (json.dumps({"on": True}).encode(), "on", "not_a_number"),
        (json.dumps({"p": None}).encode(), "p", "not_a_number"),
        (json.dumps({"p": "612 W"}).encode(), "p", "not_a_number"),
        (b'{"p": NaN}', "p", "not_a_number"),
        (json.dumps({"p": 2_000_000}).encode(), "p", "out_of_range"),
        (b'{"p": 1e400}', "p", "not_a_number"),
        (b"765", "p", "not_json"),
        (b"[1, 2]", "0", "not_json"),
        (b'"765"', "p", "not_json"),
        (b"{", "p", "not_json"),
        (b"", "p", "not_json"),
        (b"\xff\xfe", "p", "not_json"),
    ],
)
def test_a_json_payload_that_is_no_reading_says_why(payload, path, reason):
    assert extract_number(payload, "json", path) == (None, reason)


def test_an_oversized_payload_is_refused_before_it_is_parsed():
    payload = b'{"p": 5, "pad": "' + b"x" * MAX_PAYLOAD_BYTES + b'"}'

    assert extract_number(payload, "json", "p") == (None, "not_json")


def test_a_payload_nested_too_deeply_for_the_parser_is_refused_without_raising():
    depth = 30_000
    nested = b'{"a":' * depth + b"1" + b"}" * depth

    assert extract_number(nested, "json", "a") == (None, "not_json")


# --- what a path may be -------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    ["apower", "ENERGY.Power", "params.em:0.total_act_power", ".".join("a" * MAX_PATH_SEGMENTS)],
)
def test_a_path_names_one_value(path):
    assert valid_json_path(path)


@pytest.mark.parametrize(
    "path",
    ["", None, 5, ".apower", "apower.", "a..b", ".".join("a" * (MAX_PATH_SEGMENTS + 1))],
)
def test_a_path_that_names_nothing_is_refused_before_the_payload_is_read(path):
    assert not valid_json_path(path)
    assert json_path_value({"apower": 1}, path) == (False, None)
    assert extract_number(SHELLY_GEN2_SWITCH, "json", path) == (None, "path_invalid")


def test_a_path_reaches_a_value_that_is_no_number_and_says_so_rather_than_missing():
    assert json_path_value({"a": {"b": None}}, "a.b") == (True, None)
    assert json_path_value({"a": {"b": None}}, "a.c") == (False, None)


# --- the reasons ---------------------------------------------------------------


def test_every_reason_is_listed_and_every_listed_reason_happens():
    """The list is what a status view and its translations are paired against."""

    cases = [
        (b"5", "text", None),
        (b"765", "json", "p"),
        (SHELLY_GEN2_SWITCH, "json", ""),
        (SHELLY_GEN2_SWITCH, "json", "power"),
        (b"765 W", "number", None),
        (b"1000001", "number", None),
    ]
    reasons = {extract_number(payload, fmt, path)[1] for payload, fmt, path in cases}

    assert reasons == set(EXTRACT_REASONS)
