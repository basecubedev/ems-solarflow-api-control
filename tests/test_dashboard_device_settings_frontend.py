# SPDX-License-Identifier: AGPL-3.0-or-later
"""The dashboard shows and edits a device setting only where the device has it."""

import json
import subprocess
from pathlib import Path

import pytest

from ems import device_settings

pytestmark = [
    pytest.mark.contract,
]

ROOT = Path(__file__).resolve().parents[1]
APP_JS = ROOT / "dashboard" / "static" / "app.js"
CATALOGUE = device_settings.catalogue()


def run_node(script):
    result = subprocess.run(["node", "-e", script], cwd=ROOT, text=True, capture_output=True, check=False)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def _panel(devices, offered):
    script = f"""
const app = require({json.dumps(str(APP_JS))});
app.state.auth = {{ configured: true, authenticated: true, csrfToken: "token" }};
app.state.runtime = {{
  system: {{ enabled: true, max_total_power: 1200, min_output_limit: 0, loop_interval: 5 }},
  devices: {json.dumps(devices)},
  winter: {{ enabled: false }},
  _limits: {{
    system: {{ max_total_power: 1200, min_output_limit: 1200 }},
    devices: {{}},
    fallback_device_max_power: 800,
    device_settings: {json.dumps(CATALOGUE)},
    device_settings_offered: {json.dumps(offered)}
  }}
}};
const html = app.runtimeControlPanel();
const forms = {{}};
for (const name of Object.keys(app.state.runtime.devices)) {{
  const start = html.indexOf(`>${{name}}<`);
  const end = html.indexOf("</form>", start);
  forms[name] = html.slice(start, end);
}}
console.log(JSON.stringify(forms));
"""
    return run_node(script)


def test_the_runtime_form_offers_the_settings_a_device_has():
    device = {"enabled": True, "max_power": 800, "offgrid_socket_mode": "off", "pv_priority_factor": 1.0}
    forms = _panel(
        {
            "WR1": {**device, "fan": "2"},
            "WR2": dict(device),
            "WR3": {**device, "light": "off"},
            "WR4": {**device, "light": "OFF", "fan": "blink"},
        },
        {"WR1": ["light", "fan"], "WR2": [], "WR4": ["light", "fan"]},
    )

    assert 'name="light"' in forms["WR1"] and 'name="fan"' in forms["WR1"]
    assert 'name="buzzer"' not in forms["WR1"]
    assert '<option value="unmanaged" selected>Device decides</option>' in forms["WR1"]
    assert '<option value="2" selected>Speed 2</option>' in forms["WR1"]
    assert 'name="light"' not in forms["WR2"] and 'name="fan"' not in forms["WR2"]
    assert '<option value="off" selected>Off</option>' in forms["WR3"]
    assert '<option value="on"' not in forms["WR3"]
    assert 'name="fan"' not in forms["WR3"]
    assert '<option value="off" selected>Off</option>' in forms["WR4"]
    assert '<option value="blink" selected>blink (not a value)</option>' in forms["WR4"]


def test_the_device_card_shows_the_reported_setting_and_the_one_set():
    malicious = "<img src=x onerror=alert(1)>"
    script = f"""
const app = require({json.dumps(str(APP_JS))});
console.log(JSON.stringify({{
  shown: app.renderDeviceSettings({{ settings: {{
    light: {{ label: "Light", reported: "On", desired: "Off" }},
    fan: {{ label: "Fan", reported: "Auto", desired: null }},
    buzzer: {{ label: "Buzzer", reported: null, desired: null }}
  }} }}),
  escaped: app.renderDeviceSettings({{ settings: {{ light: {{ label: {json.dumps(malicious)}, reported: {json.dumps(malicious)} }} }} }}),
  none: app.renderDeviceSettings({{}})
}}));
"""
    output = run_node(script)

    assert "Device settings" in output["shown"]
    assert "On → Off" in output["shown"]
    assert "<strong>Auto</strong>" in output["shown"]
    assert "Buzzer" not in output["shown"]
    assert malicious not in output["escaped"]
    assert output["none"] == ""


@pytest.mark.parametrize(
    ("payload", "says"),
    [
        ({"light": "off"}, "Change the light of WR1?"),
        ({"fan": "off"}, "Change the fan of WR1? The EMS writes the new value to the inverter and keeps it there."),
        ({"fan": "unmanaged"}, "Hand the fan of WR1 back to the inverter? The EMS writes nothing for the fan from now on"),
        ({"light": "off", "fan": "unmanaged"}, "Change the light of WR1 and hand its fan back to the inverter?"),
        ({"enabled": False, "auto_heat": "off"}, "does not warm itself"),
        ({"offgrid_socket_mode": "eco", "auto_heat": "off"}, "does not warm itself"),
        ({"runtime_role": "ac_input", "auto_heat": "off"}, "does not warm itself"),
        ({"auto_heat": "off"}, "does not warm itself"),
    ],
)
def test_changing_a_setting_asks_first_and_battery_heating_off_says_what_it_costs(payload, says):
    script = f"""
const app = require({json.dumps(str(APP_JS))});
app.state.runtime = {{ system: {{ enabled: true }}, devices: {{ WR1: {{ enabled: true }} }}, _limits: {{ device_settings: {json.dumps(CATALOGUE)} }} }};
const form = {{ dataset: {{ runtimeEndpoint: "/api/runtime/device/WR1" }} }};
console.log(JSON.stringify({{ text: app.runtimeChangeWarning(form, {json.dumps(payload)}) }}));
"""
    text = run_node(script)["text"]

    assert says in text
    assert "cool" not in text
    assert ("does not warm itself" in text) == (payload.get("auto_heat") == "off")


def test_back_to_output_while_the_ems_is_off_still_says_what_heating_off_costs():
    script = f"""
const app = require({json.dumps(str(APP_JS))});
app.state.runtime = {{ system: {{ enabled: false }}, devices: {{ WR1: {{ enabled: true }} }}, _limits: {{ device_settings: {json.dumps(CATALOGUE)} }} }};
const form = {{ dataset: {{ runtimeEndpoint: "/api/runtime/device/WR1" }} }};
console.log(JSON.stringify({{ text: app.runtimeChangeWarning(form, {{ runtime_role: "ac_output", auto_heat: "off" }}) }}));
"""
    text = run_node(script)["text"]

    assert text.startswith("Switch WR1 back to output?")
    assert "The EMS is off, so this takes effect once it is on again." in text
    assert "does not warm itself" in text


def _warning(payload, wr1, *, system=True, offered=("light", "fan")):
    script = f"""
const app = require({json.dumps(str(APP_JS))});
app.state.runtime = {{
  system: {{ enabled: {json.dumps(system)} }},
  devices: {{ WR1: {json.dumps(wr1)} }},
  _limits: {{ device_settings: {json.dumps(CATALOGUE)}, device_settings_offered: {{ WR1: {json.dumps(list(offered))} }} }},
}};
const form = {{ dataset: {{ runtimeEndpoint: "/api/runtime/device/WR1" }} }};
console.log(JSON.stringify({{ text: app.runtimeChangeWarning(form, {json.dumps(payload)}) }}));
"""
    return run_node(script)["text"]


@pytest.mark.parametrize(
    ("payload", "wr1", "system", "says", "never"),
    [
        ({"enabled": True, "light": "off"}, {"enabled": False}, True, "Change the light of WR1?", "out of EMS control"),
        ({"fan": "unmanaged"}, {"enabled": False, "fan": "off"}, True, "Hand the fan of WR1 back", "out of EMS control"),
        ({"light": "off"}, {"enabled": False}, True, "out of EMS control, so this takes effect", None),
        ({"enabled": False, "light": "off"}, {"enabled": True}, True,
         "The light is saved and written once you enable WR1 again.", None),
        ({"enabled": False, "light": "off"}, {"enabled": True}, False,
         "written once you enable WR1 again and the EMS is on.", None),
        ({"enabled": False}, {"enabled": True, "fan": "off"}, True,
         "Its fan stays as it is on the inverter while the EMS does not control WR1; after that the EMS sets it again as you set it.",
         None),
        ({"enabled": False}, {"enabled": True, "fan": "blink"}, True, "Take WR1 out of EMS control?", "Its fan"),
        ({"enabled": False}, {"enabled": True, "buzzer": "off"}, True, "Take WR1 out of EMS control?", "buzzer"),
        ({"light": "off"}, {"enabled": False}, False,
         "The EMS is off and WR1 is out of EMS control, so this takes effect once the EMS is on and you enable WR1 again.",
         None),
        ({"offgrid_socket_mode": "eco", "light": "unmanaged"}, {"enabled": True}, True,
         "The EMS writes nothing for the light from now on", None),
        ({"runtime_role": "ac_input", "light": "off"}, {"enabled": True}, True, "The light is set as well and kept there.", None),
        ({"light": "off", "fan": "2", "auto_heat": "on"}, {"enabled": True}, True,
         "Change the light, fan and battery heating of WR1? The EMS writes the new values to the inverter and keeps them there.",
         None),
    ],
)
def test_every_confirmation_names_the_settings_it_changes_and_when_they_apply(payload, wr1, system, says, never):
    text = _warning(payload, wr1, system=system)

    assert says in text
    assert never is None or never not in text


@pytest.mark.parametrize(
    ("payload", "wr1", "system"),
    [
        ({"runtime_role": "ac_output", "light": "off"}, {"enabled": False}, True),
        ({"light": "off", "fan": "unmanaged"}, {"enabled": False}, True),
        ({"offgrid_socket_mode": "eco", "light": "unmanaged"}, {"enabled": True}, False),
    ],
)
def test_what_takes_effect_later_comes_before_what_is_handed_back(payload, wr1, system):
    text = _warning(payload, wr1, system=system)
    later = text.index("takes effect once")

    assert all(text.index(part) < later for part in ("The light is set", "The EMS writes the new value") if part in text)
    assert "writes nothing for the" not in text or text.index("writes nothing for the") > later


def test_switching_back_on_asks_nothing_and_switching_the_ems_off_names_the_settings_it_keeps():
    script = f"""
const app = require({json.dumps(str(APP_JS))});
app.state.runtime = {{
  system: {{ enabled: true }},
  devices: {{ WR1: {{ enabled: false, fan: "off" }}, WR2: {{ enabled: true }} }},
  _limits: {{ device_settings: {json.dumps(CATALOGUE)}, device_settings_offered: {{ WR1: ["fan"] }} }},
}};
const device = {{ dataset: {{ runtimeEndpoint: "/api/runtime/device/WR1" }} }};
const system = {{ dataset: {{ runtimeEndpoint: "/api/runtime/system" }} }};
const enable = app.runtimeChangeWarning(device, {{ enabled: true }});
const emsOffDeviceOff = app.runtimeChangeWarning(system, {{ enabled: false }});
app.state.runtime.devices.WR1.enabled = true;
const emsOff = app.runtimeChangeWarning(system, {{ enabled: false }});
app.state.runtime.devices.WR1.fan = null;
const emsOffWithout = app.runtimeChangeWarning(system, {{ enabled: false }});
console.log(JSON.stringify({{ enable, emsOffDeviceOff, emsOff, emsOffWithout }}));
"""
    output = run_node(script)

    assert output["enable"] is None
    assert "device settings" not in output["emsOffDeviceOff"]
    assert "The device settings it keeps stay as they are" in output["emsOff"]
    assert "device settings" not in output["emsOffWithout"]
