# SPDX-License-Identifier: AGPL-3.0-or-later
"""Numbers out of device payloads, under one strict rule.

The external-device catalog reads its namespace with this rule, and the MQTT
mapping reads the topics it republishes into that namespace with the same one,
so the mapping never publishes a value the catalog would read differently.
Every helper answers hostile input with a reason rather than an exception.
"""

import math
import re

from ems.zendure_mqtt.payloads import parse_json_object

MAX_READING = 1_000_000
MAX_PATH_SEGMENTS = 8
PAYLOAD_FORMATS = ("number", "json")
EXTRACT_REASONS = (
    "format_invalid",
    "not_json",
    "path_invalid",
    "path_missing",
    "not_a_number",
    "out_of_range",
)

_PLAIN_NUMBER = re.compile(r"-?[0-9]{1,16}(?:\.[0-9]{1,16})?")
_LIST_INDEX = re.compile(r"0|[1-9][0-9]{0,3}")


def plain_number(value) -> int | float | None:
    """A plain decimal number of plausible size, or ``None``.

    Text must be ASCII digits with an optional sign and fraction, sixteen
    digits at most on either side: Python's own parsers would also take
    ``1_000``, other scripts' digits and exponents, and refuse a few thousand
    digits by raising. A number beyond ``MAX_READING`` is refused before
    anything converts it to a float, which a long enough integer overflows.
    """

    return _read_number(value)[0]


def _read_number(value) -> tuple[int | float | None, str | None]:
    if isinstance(value, (bytes, bytearray)):
        try:
            value = value.decode("utf-8")
        except UnicodeDecodeError:
            return None, "not_a_number"
    if isinstance(value, str):
        text = value.strip()
        if _PLAIN_NUMBER.fullmatch(text) is None:
            return None, "not_a_number"
        value = float(text) if "." in text else int(text)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None, "not_a_number"
    if abs(value) > MAX_READING:
        return None, "out_of_range"
    if not math.isfinite(value):
        return None, "not_a_number"
    return value, None


def valid_json_path(path) -> bool:
    """Whether ``path`` names a value: dot-separated, no empty segment, not too deep.

    A key that itself contains a dot cannot be named.
    """

    if not isinstance(path, str) or not path:
        return False
    segments = path.split(".")
    return len(segments) <= MAX_PATH_SEGMENTS and all(segments)


def json_path_value(data, path) -> tuple[bool, object]:
    """``(True, value)`` at ``path`` in decoded JSON, or ``(False, None)``.

    On an object a segment is a key; on a list it is an index without leading
    zeros, so ``emeters.1.power`` reads the second meter.
    """

    if not valid_json_path(path):
        return False, None
    value = data
    for segment in path.split("."):
        if isinstance(value, dict) and segment in value:
            value = value[segment]
        elif isinstance(value, list) and _LIST_INDEX.fullmatch(segment):
            index = int(segment)
            if index >= len(value):
                return False, None
            value = value[index]
        else:
            return False, None
    return True, value


def extract_number(payload, payload_format, path=None) -> tuple[int | float | None, str | None]:
    """``(number, None)`` from a payload, or ``(None, reason)`` from ``EXTRACT_REASONS``.

    ``number`` reads the whole payload; ``json`` reads the value at ``path``
    in a JSON object. Either way the value passes :func:`plain_number`.
    """

    if payload_format == "number":
        return _read_number(payload)
    if payload_format != "json":
        return None, "format_invalid"
    if not valid_json_path(path):
        return None, "path_invalid"
    data = parse_json_object(payload)
    if data is None:
        return None, "not_json"
    found, value = json_path_value(data, path)
    if not found:
        return None, "path_missing"
    return _read_number(value)


__all__ = [
    "EXTRACT_REASONS",
    "MAX_PATH_SEGMENTS",
    "MAX_READING",
    "PAYLOAD_FORMATS",
    "extract_number",
    "json_path_value",
    "plain_number",
    "valid_json_path",
]
