# SPDX-License-Identifier: AGPL-3.0-or-later
"""Guided Setup keeps one activation state across a transport switch.

Setup already carried the ``enabled`` flag from the replaced connection to the
new one, but not the control decision: an MQTT selection that was deliberately
telemetry-only became a fully controlling Local-API device. Setup and
Maintenance therefore share one activation rule, applied to a normalized view of
whichever entry shape the flow holds.
"""

import json
import os
import shutil
import subprocess

import pytest

pytestmark = [
    pytest.mark.admin,
    pytest.mark.setup,
    pytest.mark.contract,
    pytest.mark.simulation,
]

STATIC_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "admin", "static"
)

_HELPERS = (
    "mconfigIsMqttDevice",
    "mconfigDeviceIsActive",
    "mconfigDeviceInactiveByChoice",
    "inverterActivationView",
)


def _read():
    with open(os.path.join(STATIC_DIR, "admin.js"), encoding="utf-8") as handle:
        return handle.read()


def _extract_fn(js, name):
    marker = "function " + name
    assert marker in js, f"{name} is missing from admin.js"
    idx = js.index(marker)
    body = js[idx:]
    depth = 0
    for position, char in enumerate(body):
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return body[: position + 1]
    raise AssertionError(f"unbalanced braces while extracting {name}")


def _switched_enabled(item, source):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for the setup activation tests")
    js = _read()
    helpers = "\n".join(_extract_fn(js, name) for name in _HELPERS)
    action = (
        "const view = inverterActivationView("
        + json.dumps(item)
        + ", "
        + json.dumps(source)
        + ");\n"
        "console.log(JSON.stringify(!mconfigDeviceInactiveByChoice(view)));"
    )
    result = subprocess.run(
        [node, "-e", helpers + "\n" + action],
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def _mqtt_selection(*, enabled=True, controllable=True, control=True):
    return {
        "id": "local-mqtt:PHYS-1",
        "serial_number": "PHYS-1",
        "connection_source": "local_mqtt",
        "enabled": enabled,
        "output_control_supported": controllable,
        "config_fragment": {
            "type": "zendure_mqtt",
            "capabilities": {"read_power": True, "write_output_limit": control},
        },
    }


def test_controlling_mqtt_selection_switches_to_an_active_api_device():
    assert _switched_enabled(_mqtt_selection(), "local_mqtt") is True


def test_a_control_capable_selection_not_controlling_stays_active():
    """Not controlling is a capability gap, never a deactivation.

    Setup answers this the same way Maintenance and the Local API path do:
    activation is the logical device's enabled flag alone.
    """

    assert _switched_enabled(_mqtt_selection(control=False), "local_mqtt") is True


def test_uncontrollable_mqtt_selection_switches_to_an_active_api_device():
    assert (
        _switched_enabled(
            _mqtt_selection(control=False, controllable=False), "local_mqtt"
        )
        is True
    )


def test_disabled_mqtt_selection_stays_inactive():
    assert _switched_enabled(_mqtt_selection(enabled=False), "local_mqtt") is False


def test_active_api_item_switches_to_an_active_connection():
    item = {"source_id": "api:1", "serial_number": "PHYS-1", "enabled": True}

    assert _switched_enabled(item, "local_api") is True


def test_disabled_api_item_stays_inactive():
    item = {"source_id": "api:1", "serial_number": "PHYS-1", "enabled": False}

    assert _switched_enabled(item, "local_api") is False


def test_setup_switch_resolves_the_enabled_state_through_the_shared_rule():
    js = _read()
    # The switch itself only awaits the backend plan; the state it carries over
    # is applied once that plan allows it.
    fn = js.split("function applyConnectionSwitch", 1)[1].split("\nfunction ", 1)[0]
    assert "mconfigDeviceInactiveByChoice(" in fn
    assert "inverterActivationView(" in fn
    assert "current.item.enabled !== false" not in fn
