# SPDX-License-Identifier: AGPL-3.0-or-later
"""Compact inverter-name contract shared by Setup and Maintenance."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from admin.config_preview import ConfigPreviewGenerator


pytestmark = pytest.mark.simulation

ROOT = Path(__file__).resolve().parents[1]
ADMIN_JS = ROOT / "admin" / "static" / "admin.js"


def _extract_function(source, name):
    marker = f"function {name}"
    start = source.find(marker)
    assert start >= 0, f"{name} is missing from admin.js"
    brace = source.find(") {", start) + 2
    assert brace >= 2, f"function body for {name} is missing"
    depth = 0
    quote = None
    escaped = False
    for index in range(brace, len(source)):
        char = source[index]
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            continue
        if char in "'\"`":
            quote = char
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return source[start : index + 1]
    raise AssertionError(f"unterminated function {name}")


def _run_node(functions, setup):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for compact inverter-name tests")
    source = ADMIN_JS.read_text(encoding="utf-8")
    script = "\n".join(_extract_function(source, name) for name in functions)
    result = subprocess.run(
        [node, "-e", script + "\n" + setup],
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


@pytest.mark.parametrize(
    ("names", "count", "expected"),
    [
        ([], 0, "INV_1"),
        (["WR1", "WR2"], 2, "INV_3"),
        (["WR1", "INV_4"], 2, "INV_5"),
        (["INV_1", "INV_3"], 2, "INV_4"),
        (["inv_1", "InV_2"], 2, "INV_3"),
        (["INV_2"], 1, "INV_3"),
    ],
)
def test_frontend_compact_allocator_contract(names, count, expected):
    result = _run_node(
        ["nextCompactInverterName"],
        "console.log(JSON.stringify(nextCompactInverterName("
        + json.dumps(names)
        + f", {count})));",
    )
    assert result == expected


@pytest.mark.parametrize(
    ("names", "count", "expected"),
    [
        ([], 0, "INV_1"),
        (["WR1", "WR2"], 2, "INV_3"),
        (["WR1", "INV_4"], 2, "INV_5"),
        (["INV_1", "INV_3"], 2, "INV_4"),
        (["inv_1", "InV_2"], 2, "INV_3"),
    ],
)
def test_backend_compact_allocator_matches_frontend(names, count, expected):
    from admin.inverter_names import next_compact_inverter_name

    assert next_compact_inverter_name(names, count) == expected


def test_fresh_local_api_and_mqtt_share_one_sequence():
    result = _run_node(
        [
            "nextCompactInverterName",
            "inverterItems",
            "selectedMqttDeviceEntries",
            "freshInverterConfigNames",
            "nextInverterName",
            "normalizeInverterAliasTokens",
            "serializeMqttProposalSelection",
        ],
        """
let configDraftItems = [];
const zendureMqttPreviewProposals = new Map();
let manualMqttDevices = [];
configDraftItems.push({role: "inverter", config_name: nextInverterName()});
const proposal = {
  id: "mqtt:1", target: "device", display_name: "SolarFlow 800 Pro 2",
  serial_number: "EOD1NLN9P010902", device_id: "EOD1NLN9P010902",
  config_fragment: {name: "Zendure MQTT SolarFlow 800 Pro 2 EOD1NLN9P010902"},
};
const selected = serializeMqttProposalSelection(proposal, {target: "device"});
zendureMqttPreviewProposals.set(selected.id, selected);
manualMqttDevices.push({name: nextInverterName(), serial_number: "MANUAL"});
console.log(JSON.stringify({
  local: configDraftItems[0].config_name,
  mqtt: selected.config_name,
  mqttDisplay: selected.display_name,
  mqttSerial: selected.serial_number,
  manual: manualMqttDevices[0].name,
}));
""",
    )
    assert result == {
        "local": "INV_1",
        "mqtt": "INV_2",
        "mqttDisplay": "SolarFlow 800 Pro 2",
        "mqttSerial": "EOD1NLN9P010902",
        "manual": "INV_3",
    }


def test_maintenance_creation_paths_share_the_allocator():
    result = _run_node(
        [
            "nextCompactInverterName",
            "mconfigNextInverterName",
            "normalizeSerial",
            "usableSerialValue",
            "physicalInverterIdentity",
            "mconfigDeviceCommonDefaults",
            "mconfigApplyCommonDefaults",
            "mconfigAddInverter",
            "mconfigAddZendureMqttDevice",
            "mconfigAddDiscovered",
            "mconfigZendureMqttDraftFromProposal",
            "mconfigAddZendureMqttProposal",
        ],
        """
const mconfigState = {
  draft: {devices: []}, catalog: {zendure_mqtt_generations: []},
  openHardware: new Set(),
};
function mconfigDeviceCatalogFields() { return []; }
const MCONFIG_DEVICE_IDENTITY_KEYS = new Set(["name", "ip", "sn"]);
function deviceFieldKey(path) { return path; }
function mconfigGenerations() { return []; }
function renderMaintenanceInverters() {}
function renderMaintenanceGridMeter() {}
function mconfigMarkDraftChanged() {}
function mconfigIdentity(value) { return String(value || "").trim().toLowerCase(); }
function mconfigMqttProposalState() { return "new"; }
mconfigAddInverter();
mconfigAddZendureMqttDevice();
mconfigAddDiscovered({
  role: "inverter",
  discovered: {ip: "192.0.2.3", serial_number: "LOCAL-3"},
});
mconfigAddZendureMqttProposal({
  id: "mqtt:4", display_name: "SolarFlow 800 Pro 2",
  serial_number: "EOD1NLN9P010902", broker_ref: "cloud",
  config_fragment: {
    name: "Zendure MQTT SolarFlow 800 Pro 2 EOD1NLN9P010902",
    serial_number: "EOD1NLN9P010902", mqtt: {broker_ref: "cloud"},
    capabilities: {write_output_limit: false},
  },
});
console.log(JSON.stringify(mconfigState.draft.devices.map((device) => device.name)));
""",
    )
    assert result == ["INV_1", "INV_2", "INV_3", "INV_4"]


def test_reset_and_reorder_preserve_metadata_and_existing_aliases():
    result = _run_node(
        [
            "nextCompactInverterName",
            "inverterItems",
            "selectedMqttDeviceEntries",
            "freshInverterConfigNames",
            "nextInverterName",
            "moveDraftItem",
            "resetDraftItemName",
        ],
        """
let configDraftItems = [
  {source_id: "a", role: "inverter", config_name: "Roof West",
   display_name: "SolarFlow 800 Pro", serial_number: "SN-A"},
  {source_id: "b", role: "inverter", config_name: "INV_2",
   display_name: "SolarFlow 800 Pro 2", serial_number: "SN-B"},
];
const zendureMqttPreviewProposals = new Map();
let manualMqttDevices = [];
let commits = 0;
function commitDraftChange() { commits += 1; }
function rememberInverterName() {}
moveDraftItem("b", -1);
const namesAfterMove = configDraftItems.map((item) => item.config_name);
resetDraftItemName("a");
console.log(JSON.stringify({
  namesAfterMove,
  order: configDraftItems.map((item) => item.source_id),
  namesAfterReset: configDraftItems.map((item) => item.config_name),
  displays: configDraftItems.map((item) => item.display_name),
  commits,
}));
""",
    )
    assert result == {
        "namesAfterMove": ["INV_2", "Roof West"],
        "order": ["b", "a"],
        "namesAfterReset": ["INV_2", "INV_3"],
        "displays": ["SolarFlow 800 Pro 2", "SolarFlow 800 Pro"],
        "commits": 2,
    }


def test_removed_generated_alias_is_not_reused():
    result = _run_node(
        ["nextCompactInverterName"],
        'console.log(JSON.stringify(nextCompactInverterName(["INV_2"], 1)));',
    )
    assert result == "INV_3"


def test_old_saved_mqtt_selection_gets_one_persisted_compact_name():
    result = _run_node(
        ["nextCompactInverterName", "upgradeStoredInverterNames"],
        """
let configDraftItems = [
  {role: "inverter", config_name: "Roof West", serial_number: "LOCAL"},
];
const zendureMqttPreviewProposals = new Map([
  ["old", {id: "old", target: "device", serial_number: "MQTT-1",
    display_name: "SolarFlow 800 Pro"}],
  ["custom", {id: "custom", target: "device", serial_number: "MQTT-2",
    config_name: "Battery Garage"}],
]);
let manualMqttDevices = [];
let proposalSaves = 0;
let manualSaves = 0;
function saveMqttPreviewProposals() { proposalSaves += 1; }
function saveManualMqttDevices() { manualSaves += 1; }
upgradeStoredInverterNames();
upgradeStoredInverterNames();
console.log(JSON.stringify({
  old: zendureMqttPreviewProposals.get("old").config_name,
  custom: zendureMqttPreviewProposals.get("custom").config_name,
  proposalSaves,
  manualSaves,
}));
""",
    )
    assert result == {
        "old": "INV_3",
        "custom": "Battery Garage",
        "proposalSaves": 1,
        "manualSaves": 0,
    }


def test_manual_transport_switch_preserves_config_name_both_directions():
    result = _run_node(
        [
            "nextCompactInverterName",
            "inverterItems",
            "selectedMqttDeviceEntries",
            "freshInverterConfigNames",
            "nextInverterName",
            "normalizeSerial",
            "usableSerialValue",
            "inverterVisibleSerial",
            "inverterIdentityTokens",
            "inverterIdentitySet",
            "inverterHasIdentity",
            "inverterIdentityConflict",
            "inverterIdentitiesMatch",
            "inverterIdentitySetOf",
            "mqttSourceOfConnection",
            "rememberedInverterName",
            "rememberInverterName",
            "inverterConfigNameForSerial",
            "normalizeInverterAliasTokens",
            "configuredInverterConnection",
            "preservedInverterValues",
            "serializeMqttProposalSelection",
            "switchInverterTransport",
        ],
        """
const DEVICE_MAPPED_FIELD_KEYS = {name: "config_name", ip: "ip", sn: "serial_number"};
let configDraftItems = [{
  source_id: "local:1", role: "inverter", config_name: "INV_1",
  serial_number: "SERIAL-1", auto_added: false,
}];
const zendureMqttPreviewProposals = new Map();
const transportInverterNames = new Map();
let manualMqttDevices = [];
const configDismissed = new Set();
const local = {id: "local:1", role_suggestion: "inverter", serial_number: "SERIAL-1"};
const mqtt = {
  id: "mqtt:1", target: "device", serial_number: "SERIAL-1",
  connection_source: "zendure_cloud_mqtt", display_name: "SolarFlow 800 Pro",
  config_fragment: {name: "Zendure MQTT SolarFlow 800 Pro SERIAL-1"},
};
const localMqtt = {
  ...mqtt, id: "mqtt:local", connection_source: "local_mqtt",
};
function undismissSerial() {}
function availableConfigDevices() { return [local]; }
function availableMqttDeviceProposals() { return [localMqtt, mqtt]; }
function deviceKey(device) { return device.id; }
function draftHasSource(id) { return configDraftItems.some((item) => item.source_id === id); }
function draftItemFromDevice(device) {
  return {source_id: device.id, role: "inverter", serial_number: device.serial_number,
    config_name: rememberedInverterName(device.serial_number) || nextInverterName()};
}
function saveConfigDismissed() {}
function saveConfigDraft() {}
function saveMqttPreviewProposals() {}
function renderMqttProposals() {}
function renderConfigDraft() {}
function renderConfigAvailable() {}
const latestMqttProposals = [localMqtt, mqtt];
switchInverterTransport("SERIAL-1", "zendure_mqtt");
const mqttName = zendureMqttPreviewProposals.get("mqtt:1").config_name;
switchInverterTransport("SERIAL-1", "local_api");
const localName = configDraftItems[0].config_name;
configDraftItems = [];
zendureMqttPreviewProposals.set("mqtt:local", {
  ...serializeMqttProposalSelection(localMqtt, {target: "device"}),
  config_name: "INV_1",
});
switchInverterTransport("SERIAL-1", "zendure_mqtt");
console.log(JSON.stringify({
  mqttName,
  localName,
  mqttCount: zendureMqttPreviewProposals.size,
  mqttIds: Array.from(zendureMqttPreviewProposals.keys()),
}));
""",
    )
    assert result == {
        "mqttName": "INV_1",
        "localName": "INV_1",
        "mqttCount": 1,
        "mqttIds": ["mqtt:1"],
    }


class _ReleaseManager:
    def config_template(self):
        return {
            "tag": "test",
            "template": {
                "system": {"max_total_power": 1600},
                "devices": [
                    {
                        "name": "WR1",
                        "ip": "192.0.2.10",
                        "sn": "YOUR_SN",
                        "max_power": 800,
                    }
                ],
                "grid_meter": {"type": "shelly", "ip": "192.0.2.20"},
            },
        }


def _generator():
    return ConfigPreviewGenerator(
        _ReleaseManager(), install_context_provider=lambda: None
    )


def _local(serial, host, **extra):
    item = {
        "display_name": "SolarFlow 800 Pro 2",
        "role": "inverter",
        "enabled": True,
        "ip": host,
        "serial_number": serial,
    }
    item.update(extra)
    return item


def _meter():
    return {
        "config_name": "grid_meter",
        "display_name": "Shelly Pro 3EM",
        "role": "grid_meter",
        "enabled": True,
        "ip": "192.0.2.20",
        "api_family": "shelly_gen2",
    }


def _mqtt_proposal(config_name_marker=True):
    proposal = {
        "id": "mqtt:EOD1NLN9P010902",
        "target": "device",
        "display_name": "SolarFlow 800 Pro 2",
        "serial_number": "EOD1NLN9P010902",
        "device_id": "EOD1NLN9P010902",
        "config_fragment": {
            "type": "zendure_mqtt",
            "enabled": True,
            "name": "Zendure MQTT SolarFlow 800 Pro 2 EOD1NLN9P010902",
            "serial_number": "EOD1NLN9P010902",
            "mqtt": {
                "topic_family": "zensdk_ha_scalar",
                "base_topic": None,
                "device_id": "EOD1NLN9P010902",
            },
            "capabilities": {
                "read_power": True,
                "read_soc": True,
                "write_output_limit": False,
            },
        },
    }
    if config_name_marker is not False:
        proposal["config_name"] = config_name_marker
    return proposal


def test_backend_falls_back_for_missing_names_but_rejects_explicit_empty():
    generator = _generator()
    fallback = generator.generate(
        [_local("LOCAL-1", "192.0.2.11"), _meter()],
        1,
        zendure_mqtt_proposals=[_mqtt_proposal(False)],
    )
    assert [device["name"] for device in fallback["config"]["devices"]] == [
        "INV_1",
        "INV_2",
    ]
    assert "EOD1NLN9P010902" not in fallback["config"]["devices"][1]["name"]

    explicit_empty = generator.generate(
        [_local("LOCAL-1", "192.0.2.11", config_name="INV_1"), _meter()],
        1,
        zendure_mqtt_proposals=[_mqtt_proposal("")],
    )
    codes = {item["code"] for item in explicit_empty["validation"]["errors"]}
    assert explicit_empty["ready"] is False
    assert "zendure_mqtt_invalid" in codes


def test_backend_uses_selected_mqtt_config_name_not_description_or_serial():
    result = _generator().generate(
        [_meter()],
        1,
        zendure_mqtt_proposals=[_mqtt_proposal("INV_1")],
    )
    mqtt = next(
        device for device in result["config"]["devices"]
        if device.get("type") == "zendure_mqtt"
    )
    assert mqtt["name"] == "INV_1"
    assert mqtt["serial_number"] == "EOD1NLN9P010902"


def test_backend_manual_mqtt_uses_compact_name_without_serial_fallback():
    manual = {
        "serial_number": "MANUAL-SERIAL",
        "generation": "solarflow_zensdk",
    }
    result = _generator().generate(
        [_local("LOCAL-1", "192.0.2.11", config_name="INV_1"), _meter()],
        1,
        zendure_mqtt_broker={
            "name": "local_mqtt",
            "host": "192.0.2.30",
            "port": 1883,
        },
        zendure_mqtt_manual_devices=[manual],
    )
    mqtt = next(
        device for device in result["config"]["devices"]
        if device.get("type") == "zendure_mqtt"
    )
    assert mqtt["name"] == "INV_2"
    assert mqtt["serial_number"] == "MANUAL-SERIAL"
    assert "MANUAL-SERIAL" not in mqtt["name"]

    explicit_empty = _generator().generate(
        [_local("LOCAL-1", "192.0.2.11", config_name="INV_1"), _meter()],
        1,
        zendure_mqtt_broker={
            "name": "local_mqtt",
            "host": "192.0.2.30",
            "port": 1883,
        },
        zendure_mqtt_manual_devices=[dict(manual, name="")],
    )
    assert explicit_empty["ready"] is False
    assert "zendure_mqtt_invalid" in {
        issue["code"] for issue in explicit_empty["validation"]["errors"]
    }


def test_maintenance_backend_preserves_existing_name_and_defaults_only_new_items():
    from admin.maintenance_config import _merge_devices

    merged = {
        "devices": [
            {
                "name": "Roof West",
                "ip": "192.0.2.10",
                "sn": "EXISTING",
                "max_power": 800,
            }
        ]
    }
    draft = [
        {
            "kind": "local_api",
            "original_name": "Roof West",
            "name": "Roof West",
            "ip": "192.0.2.10",
            "sn": "EXISTING",
        },
        {
            "kind": "local_api",
            "original_name": None,
            "ip": "192.0.2.11",
            "sn": "NEW",
        },
    ]
    _merge_devices(merged, draft, [])
    assert [device["name"] for device in merged["devices"]] == [
        "Roof West",
        "INV_2",
    ]

    cleared = {
        "devices": [
            {"name": "Roof West", "ip": "192.0.2.10", "sn": "EXISTING"}
        ]
    }
    _merge_devices(
        cleared,
        [
            {
                "kind": "local_api",
                "original_name": "Roof West",
                "name": "",
                "ip": "192.0.2.10",
                "sn": "EXISTING",
            }
        ],
        [],
    )
    assert cleared["devices"][0]["name"] == ""
