# SPDX-License-Identifier: AGPL-3.0-or-later
"""A declared config key that reaches nothing is invisible until someone relies on it.

Two device keys shipped this way: `ac_charge_enabled` and `max_charge_power_w`
were both documented, schema-validated and editable in the Admin console, and
neither was passed to the device object by either transport. Nothing failed --
the values were simply ignored, and `resolve_max_charge_power_w`'s "an explicit
setting always wins" was dead code that its own docstring advertised as the way
to unblock an unidentified device.

Code review is a poor net for this. A reviewer reads what is written, and this
class of defect is something *missing*: the wiring that was never added. What
catches it is walking one authority and asserting the other side honours every
entry, which is what this module does.
"""

import inspect

import pytest

from ems.clients import ZendureClient
from ems.config_catalog import _SECTIONS
from ems.zendure_mqtt.device_client import ZendureMqttDeviceClient

pytestmark = [
    pytest.mark.contract,
    pytest.mark.config,
]

# Keys that legitimately do not reach a client constructor, each with the reason
# it does not. Adding a name here is a deliberate statement, not a way to make a
# failure go away.
NOT_A_CONSTRUCTOR_ARGUMENT = {
    # The device's identity, not a setting: the caller resolves it and passes it
    # positionally under a different name.
    "name": {"http", "mqtt"},
    # An MQTT device is addressed by route and topic, never by IP.
    "ip": {"mqtt"},
    # The MQTT client takes the serial as `serial_number`, and distinguishes a
    # real one from a route-id fallback.
    "sn": {"mqtt"},
}


def declared_device_fields():
    """Every ``devices[].*`` key the config catalogue declares."""

    found = set()

    def walk(node):
        if isinstance(node, dict):
            path = node.get("path")
            if isinstance(path, str) and path.startswith("devices[]."):
                found.add(path.split(".", 1)[1])
            for value in node.values():
                walk(value)
        elif isinstance(node, (list, tuple)):
            for value in node:
                walk(value)

    walk(_SECTIONS)
    return found


@pytest.mark.parametrize(
    "transport,client",
    [("http", ZendureClient), ("mqtt", ZendureMqttDeviceClient)],
)
def test_every_declared_device_key_reaches_the_device(transport, client):
    accepted = set(inspect.signature(client.__init__).parameters)

    unreachable = sorted(
        field
        for field in declared_device_fields()
        if field not in accepted
        and transport not in NOT_A_CONSTRUCTOR_ARGUMENT.get(field, set())
    )

    assert unreachable == [], (
        f"{transport}: these config keys are declared to operators but never "
        f"reach the device object, so setting them does nothing: {unreachable}"
    )


def test_the_catalogue_actually_declares_the_charge_keys():
    """Guards the guard: an empty walk would make the test above vacuous."""

    declared = declared_device_fields()

    assert "ac_charge_enabled" in declared
    assert "max_charge_power_w" in declared
    assert len(declared) >= 10


def test_an_explicit_charge_limit_beats_a_device_that_reports_none():
    """The precedence its docstring promises, end to end from the config value.

    This was unreachable: with no attribute on the device, the explicit branch
    could never be taken, and a device reporting no ceiling could not be
    unblocked at all.
    """

    from types import SimpleNamespace

    from ems.ac_charge_control import resolve_max_charge_power_w

    silent_device = SimpleNamespace(charge_max_limit_w=0)
    explicit = ZendureClient(
        "WR1", "192.0.2.10", "SN", None, 15, 100, 1, None,
        800, 1.0, 1.0, 1.0, max_charge_power_w=400,
    )
    unset = ZendureClient(
        "WR2", "192.0.2.11", "SN", None, 15, 100, 1, None, 800, 1.0, 1.0, 1.0
    )

    assert resolve_max_charge_power_w(explicit, silent_device) == 400
    assert resolve_max_charge_power_w(unset, silent_device) == 0
    # Still capped by what the device reports for itself.
    assert resolve_max_charge_power_w(
        ZendureClient(
            "WR3", "192.0.2.12", "SN", None, 15, 100, 1, None,
            800, 1.0, 1.0, 1.0, max_charge_power_w=5000,
        ),
        SimpleNamespace(charge_max_limit_w=1000),
    ) == 1000


def test_every_model_the_owner_catalogue_names_resolves_to_a_profile():
    """A product name that resolves to nothing leaves the device telemetry-only.

    The same class again, one layer down: the device reports its model, the
    registry does not recognise the spelling, and the EMS quietly never controls
    it. Found "SolarFlow Hub 1200" (the 2000 carried the prefixed alias, the 1200
    did not) and both Mix models this way.
    """

    from ems.mqtt_control.zendure_profiles import resolve_hardware_profile

    catalogue = [
        "SolarFlow 800",
        "SolarFlow 800 Pro",
        "SolarFlow 800 Pro 2",
        "SolarFlow 800 Plus",
        "Hyper 2000",
        "SolarFlow Hub 1200",
        "SolarFlow Hub 2000",
        "ACE 1500",
        "SolarFlow 2400 AC",
        "SolarFlow 2400 AC+",
        "SolarFlow 1600 AC+",
        "SolarFlow 2400 Pro",
        "AIO 2400",
        "SolarFlow Mix 3000 AC+",
        "SolarFlow Mix 4000 AC+",
    ]

    unresolved = [name for name in catalogue if resolve_hardware_profile(name) is None]

    assert unresolved == [], (
        "these models are in the device catalogue but no profile recognises the "
        f"name, so such a device stays telemetry-only: {unresolved}"
    )


# Property names observed on real hardware: an 800 Pro 2 over the local API,
# 41 samples across an AC charge cycle (2026-09-13). Names only -- no values, no
# serial, nothing device-identifying. Extend this list when another model is
# probed; that is what makes the check grow with the fleet.
OBSERVED_ON_HARDWARE = {
    "acMode",
    "acStatus",
    "dcStatus",
    "electricLevel",
    "faultLevel",
    "gridInputPower",
    "gridState",
    "inputLimit",
    "outputHomePower",
    "outputLimit",
    "outputPackPower",
    "packInputPower",
    "packState",
    "pvStatus",
    "smartMode",
    "socLimit",
    "solarInputPower",
}

# Observed and deliberately not carried into DeviceState, each with the reason.
DELIBERATELY_UNREAD = {
    # PV presence is derived from solarInputPower, which is a power rather than
    # an enum and is already the basis for every PV decision. A second, coarser
    # signal for the same fact would be a competing authority.
    "pvStatus",
}


def test_every_property_real_hardware_reports_is_read_or_explicitly_ignored():
    """The largest miss of 2026-09-14 was a field nobody read.

    `gridInputPower` -- the measured AC input -- was in the hardware probe from
    the day before and was mapped nowhere, so a charging device reported
    `output == 0` and four surfaces showed it as idle. Nothing failed; the field
    was simply absent, which is why walking the observed set is what finds it.
    """

    from ems.diagnostics import diagnose_reported_property_names

    # Distinct non-zero values so that dropping any one name changes what
    # parse_device produces; a field read into a 0 default would otherwise look
    # unread. Reuses the same check `diagnose --hardware` runs on real hardware,
    # so the test and the field report can never disagree about what "read"
    # means.
    payload = {
        "properties": {
            name: index + 1 for index, name in enumerate(sorted(OBSERVED_ON_HARDWARE))
        }
    }
    unread = sorted(
        set(diagnose_reported_property_names(payload)["unmapped"]) - DELIBERATELY_UNREAD
    )

    assert unread == [], (
        "real hardware reports these and parse_device maps none of them, so "
        f"nothing downstream can see them: {unread}"
    )

    # Guards the guard: a typo in the ignore list must not silently widen it.
    assert DELIBERATELY_UNREAD <= OBSERVED_ON_HARDWARE
