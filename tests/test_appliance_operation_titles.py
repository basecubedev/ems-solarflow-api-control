# SPDX-License-Identifier: AGPL-3.0-or-later
"""Every operation this appliance can run has a name a person can read.

The banner is what an operator watches while their appliance is being changed,
and it led with the payload's own identifier: "admin.install · verifying". A
type is a closed set declared here in Python, so the interface can carry a
sentence for each one -- and this pins that it does, because the failure mode is
silent: a new operation ships, nobody adds a title, and the banner quietly goes
back to printing an identifier.

The stage beside it is deliberately not mapped. Every executor names its own,
so a stage nobody anticipated has to stay readable rather than disappear.
"""

import re
from pathlib import Path

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.simulation, pytest.mark.appliance]

ROOT = Path(__file__).resolve().parents[1]
APP = (ROOT / "appliance" / "static" / "app.js").read_text(encoding="utf-8")

# Built in agent.py as f"system.{action}", so they are not TYPE_ constants and
# the scan below cannot see them.
DYNAMIC_TYPES = ("system.reboot", "system.shutdown")


def titled():
    block = APP.split("var OPERATION_TITLES = {", 1)[1].split("\n  };", 1)[0]
    titles = dict(re.findall(r'"([a-z][a-z._]+)":\s*"([^"]+)"', block))
    assert titles, "OPERATION_TITLES changed shape; this test can no longer read it"
    return titles


def declared():
    types = {}
    for path in sorted((ROOT / "appliance").glob("*.py")):
        for name, value in re.findall(
            r'^(TYPE_[A-Z_]+) = "([a-z][a-z._]+)"', path.read_text(encoding="utf-8"), re.M
        ):
            types[value] = f"{path.name}:{name}"
    assert types, "no operation types were found; the constant shape changed"
    return types


def test_every_operation_type_has_a_title():
    titles = titled()
    missing = {value: where for value, where in declared().items() if value not in titles}
    assert missing == {}, f"operation types with no readable title: {missing}"


def test_the_two_dynamically_built_types_are_covered_as_well():
    titles = titled()
    assert [name for name in DYNAMIC_TYPES if name not in titles] == []


def test_no_title_is_the_identifier_spelled_differently():
    for value, title in titled().items():
        assert title != value
        assert title != value.replace(".", " ")
        assert title[0].isupper(), f"{value} reads as a fragment: {title}"
