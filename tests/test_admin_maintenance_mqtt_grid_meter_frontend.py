# SPDX-License-Identifier: AGPL-3.0-or-later
"""Maintenance adopts a discovered MQTT grid meter as the grid meter.

The hardware role decides which action a Maintenance MQTT proposal offers: an
inverter keeps "Add inverter", a grid meter offers "Use as grid meter" and never
an inverter action, and an unclassified device is mapped to neither. The
adoption itself goes through the one shared proposal → grid_meter mapping
(``mqttGridMeterConfigFromProposal``), writes only the in-memory Maintenance
draft, and never replaces an already configured grid meter without confirmation.

The real admin.js renderers and helpers are executed (brace-matched extraction,
DOM shim in tests/js/maintenance_card_runner.js) so the contract is tested
against the shipped code and not a rebuilt copy.
"""

import json
import os
import shutil
import subprocess

import pytest

pytestmark = pytest.mark.simulation

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATIC_DIR = os.path.join(ROOT, "admin", "static")
RUNNER = os.path.join(ROOT, "tests", "js", "maintenance_card_runner.js")

D0_TOPIC = "Zendure/sensor/E2ED0/totalPower"
CT3_TOPIC = "Zendure/sensor/E2E3CT/totalPower"


def _read(name="admin.js"):
    with open(os.path.join(STATIC_DIR, name), encoding="utf-8") as handle:
        return handle.read()


def _extract_fn(js, name):
    marker = "function " + name + "("
    assert marker in js, f"{name} is missing from admin.js"
    start = js.index(marker)
    depth = 0
    cursor = js.index("(", start)
    for position in range(cursor, len(js)):
        if js[position] == "(":
            depth += 1
        elif js[position] == ")":
            depth -= 1
            if depth == 0:
                cursor = position
                break
    depth = 0
    for position in range(js.index("{", cursor), len(js)):
        if js[position] == "{":
            depth += 1
        elif js[position] == "}":
            depth -= 1
            if depth == 0:
                return js[start : position + 1]
    raise AssertionError(f"unbalanced braces while extracting {name}")


def _node():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for the maintenance grid-meter tests")
    return node


# --- rendered proposal cards ---------------------------------------------


def _card(proposal, draft=None, pristine=None, state=None):
    payload = {"mqttProposal": proposal}
    if state is not None:
        payload["state"] = state
    request = {"card": "mqtt_proposal", "payload": payload}
    if draft is not None:
        request["draft"] = draft
    if pristine is not None:
        request["pristine"] = pristine
    result = subprocess.run(
        [_node(), RUNNER],
        input=json.dumps(request),
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


# --- proposal fixtures ---------------------------------------------------


def _inverter(source="local_mqtt", **extra):
    proposal = {
        "id": "zendure-mqtt:" + source + ":INV",
        "connection_source": source,
        "display_name": "MQTT inverter",
        "serial_number": "SN-INV",
        "device_id": "DEV-INV",
        "target": "device",
        "role_hint": "battery_inverter_candidate",
        "output_control_supported": True,
        "config_fragment": {"mqtt": {"write_protocol": "legacy_properties_write"}},
    }
    proposal.update(extra)
    return proposal


def _grid_meter(source="local_mqtt", serial="E2ED0", topic=D0_TOPIC, **extra):
    """A backend-classified MQTT grid meter carrying its trusted fragment.

    Like a real discovery proposal it also carries the non-secret endpoint of the
    broker it was seen on, which the adoption passes to the backend so a broker
    discovered in this session can be provisioned.
    """

    proposal = {
        "id": "zendure-mqtt:" + source + ":" + serial,
        "connection_source": source,
        "display_name": "MQTT smart meter " + serial,
        "serial_number": serial,
        "device_id": serial,
        "target": "grid_meter",
        "role_hint": "grid_meter_candidate",
        "broker_ref": "local_mqtt_e2e",
        "broker_host": "192.168.50.30",
        "broker_port": 1883,
        "broker_tls": False,
        "output_control_supported": False,
        "metrics": ["totalPower"],
        "grid_meter_fragment": {
            "type": "zendure_smartmeter_d0",
            "mqtt": {
                "broker_ref": "local_mqtt_e2e",
                "topic": topic,
                "payload_format": "number",
                "max_age_seconds": 15,
            },
        },
    }
    proposal.update(extra)
    return proposal


def _grid_meter_candidate(source="zendure_cloud_mqtt", **extra):
    """Grid-meter hardware the backend refused to map (no trusted topic)."""

    proposal = {
        "id": "zendure-mqtt:" + source + ":GRIDCAND",
        "connection_source": source,
        "display_name": "MQTT grid meter candidate",
        "serial_number": "SN-CAND",
        "target": "device",
        "role_hint": "grid_meter_candidate",
        "output_control_supported": False,
        "warnings": ["grid_metric_without_topic"],
        "config_fragment": {"mqtt": {}},
    }
    proposal.update(extra)
    return proposal


def _unknown(source="local_mqtt", **extra):
    proposal = {
        "id": "zendure-mqtt:" + source + ":UNK",
        "connection_source": source,
        "display_name": "MQTT device",
        "serial_number": "SN-UNK",
        "target": "device",
        "role_hint": "unknown_candidate",
        "output_control_supported": False,
        "config_fragment": {"mqtt": {}},
    }
    proposal.update(extra)
    return proposal


def _drafted_meter(topic=D0_TOPIC, broker_ref="local_mqtt_e2e"):
    return {
        "grid_meter": {
            "present": True,
            "type": "zendure_smartmeter_d0",
            "mqtt": {
                "broker_ref": broker_ref,
                "topic": topic,
                "payload_format": "number",
                "max_age_seconds": 15,
            },
        }
    }


# --- which action a role offers ------------------------------------------


@pytest.mark.parametrize("source", ["local_mqtt", "zendure_cloud_mqtt"])
def test_mqtt_inverter_proposal_keeps_the_inverter_action(source):
    card = _card(_inverter(source))
    assert card["dataset"]["role"] == "inverter"
    assert card["action"]["text"] == "Add inverter"
    assert card["action"]["disabled"] is False
    assert "Use as grid meter" not in card["text"]


@pytest.mark.parametrize(
    "proposal",
    [
        pytest.param(_grid_meter("local_mqtt"), id="local_mqtt_d0"),
        pytest.param(
            _grid_meter("local_mqtt", serial="E2E3CT", topic=CT3_TOPIC),
            id="local_mqtt_3ct",
        ),
        pytest.param(_grid_meter("zendure_cloud_mqtt"), id="zendure_mqtt"),
    ],
)
def test_mqtt_grid_meter_proposal_offers_the_grid_meter_action(proposal):
    card = _card(proposal)
    assert card["dataset"]["role"] == "grid_meter"
    assert "hardware-card-grid-meter" in card["classes"]
    assert card["action"]["text"] == "Use as grid meter"
    assert card["action"]["disabled"] is False
    assert "Add inverter" not in card["text"]


def test_grid_meter_card_shows_its_role_and_topic_instead_of_output_control():
    card = _card(_grid_meter())
    assert "Role" in card["text"] and "Grid meter" in card["text"]
    assert D0_TOPIC in card["text"]
    assert "Output control" not in card["text"]
    assert "Write protocol" not in card["text"]


def test_grid_meter_without_a_trusted_topic_is_never_offered_as_an_inverter():
    card = _card(_grid_meter_candidate())
    assert card["dataset"]["role"] == "grid_meter"
    assert card["dataset"]["state"] == "unavailable"
    assert card["action"]["text"] == "Use as grid meter"
    assert card["action"]["disabled"] is True
    assert "Add inverter" not in card["text"]


@pytest.mark.parametrize("source", ["local_mqtt", "zendure_cloud_mqtt"])
def test_unknown_proposal_is_not_silently_mapped_to_a_grid_meter(source):
    card = _card(_unknown(source))
    assert card["dataset"]["role"] == "unknown"
    assert "hardware-card-grid-meter" not in card["classes"]
    assert "hardware-card-inverter" not in card["classes"]
    # The neutral candidate keeps the existing explicit inverter action.
    assert card["action"]["text"] == "Add inverter"
    assert "Use as grid meter" not in card["text"]


# --- state against the draft and the installed config ---------------------


def test_adopted_grid_meter_reports_added_to_draft():
    card = _card(_grid_meter(), draft=_drafted_meter())
    assert card["dataset"]["state"] == "added"
    assert card["action"]["text"] == "Added to draft"
    assert card["action"]["disabled"] is True


def test_installed_grid_meter_reports_in_config():
    card = _card(_grid_meter(), draft=_drafted_meter(), pristine=_drafted_meter())
    assert card["dataset"]["state"] == "found"
    assert card["action"]["text"] == "In config"
    assert card["action"]["disabled"] is True


def test_another_broker_bridging_the_same_topic_stays_selectable():
    card = _card(_grid_meter(), draft=_drafted_meter(broker_ref="other_broker"))
    assert card["dataset"]["state"] == "new"
    assert card["action"]["text"] == "Use as grid meter"


def test_a_configured_http_grid_meter_leaves_the_proposal_selectable():
    draft = {"grid_meter": {"present": True, "type": "shelly", "ip": "192.168.50.2"}}
    card = _card(_grid_meter(), draft=draft)
    assert card["dataset"]["state"] == "new"
    assert card["action"]["disabled"] is False


# --- adoption writes only the draft ---------------------------------------

_ADOPT_HELPERS = (
    "isMqttGridMeterProposal",
    "mqttProposalHardwareRole",
    "mqttGridMeterProposalTopic",
    "mqttProposalBrokerRef",
    "mqttProposalBrokerProfile",
    "mqttGridMeterConfigFromProposal",
    "confirmGridMeterReplacement",
    "mconfigGridMeterIsMapping",
    "mconfigMqttGridMeterState",
    "mconfigMqttProposalReviewState",
    "mconfigAdoptMqttGridMeterProposal",
)


def _adopt(proposal, draft=None, pristine=None, confirm=True):
    js = _read()
    helpers = "\n".join(_extract_fn(js, name) for name in _ADOPT_HELPERS)
    draft = draft if draft is not None else {"devices": [], "grid_meter": {}}
    script = (
        "const calls = [];\n"
        "const window = { confirm: () => { calls.push('confirm'); return "
        + ("true" if confirm else "false")
        + "; } };\n"
        "function renderMaintenanceGridMeter() { calls.push('render'); }\n"
        "function mconfigMarkDraftChanged(source) { calls.push('changed:' + source); }\n"
        "function mconfigRerenderDiscoveryReview() { calls.push('rerender'); }\n"
        "function mconfigMqttProposalState() { return 'new'; }\n"
        "const mconfigState = {\n"
        "  openHardware: new Set(),\n"
        "  draft: " + json.dumps(draft) + ",\n"
        "  pristine: " + json.dumps(pristine if pristine is not None else {}) + ",\n"
        "};\n"
        + helpers
        + "\nconst proposal = " + json.dumps(proposal) + ";\n"
        "const adopted = mconfigAdoptMqttGridMeterProposal(proposal);\n"
        "process.stdout.write(JSON.stringify({\n"
        "  adopted,\n"
        "  draft: mconfigState.draft,\n"
        "  opened: Array.from(mconfigState.openHardware),\n"
        "  calls,\n"
        "  state: mconfigMqttProposalReviewState(proposal),\n"
        "}));\n"
    )
    result = subprocess.run(
        [_node(), "-e", script], text=True, capture_output=True, check=False
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


@pytest.mark.parametrize("source", ["local_mqtt", "zendure_cloud_mqtt"])
def test_adoption_writes_the_mapped_grid_meter_into_the_draft(source):
    out = _adopt(_grid_meter(source))
    meter = out["draft"]["grid_meter"]
    assert out["adopted"] is True
    assert meter["present"] is True
    assert meter["type"] == "zendure_smartmeter_d0"
    assert meter["mqtt"] == {
        "broker_ref": "local_mqtt_e2e",
        "topic": D0_TOPIC,
        "payload_format": "number",
        "max_age_seconds": 15,
    }
    # Only the draft changed; nothing was added to the inverter list.
    assert out["draft"]["devices"] == []
    assert "maintenance-grid-meter" in out["opened"]
    assert "changed:discovery" in out["calls"]
    assert "rerender" in out["calls"]


# --- the broker of a newly discovered connection ---------------------------


def test_adoption_carries_the_shared_broker_profile_of_a_new_broker():
    """The regression: nothing in the draft declares the discovered broker yet."""

    out = _adopt(_grid_meter())
    meter = out["draft"]["grid_meter"]
    assert meter["broker"] == {
        "ref": "local_mqtt_e2e",
        "host": "192.168.50.30",
        "port": 1883,
        "tls": False,
        "tls_insecure": False,
        "tls_mode": "",
        "credentials_ref": "",
        "source": "local_mqtt",
    }
    assert meter["mqtt"]["broker_ref"] == meter["broker"]["ref"]
    assert out["draft"]["devices"] == []


def test_adoption_preserves_tls_and_the_credential_reference():
    proposal = _grid_meter(
        broker_port=8883,
        broker_tls=True,
        broker_tls_insecure=True,
        broker_tls_mode="insecure_no_verify",
        credentials_ref="local_mqtt_e2e",
    )
    broker = _adopt(proposal)["draft"]["grid_meter"]["broker"]
    assert broker["port"] == 8883
    assert broker["tls"] is True
    assert broker["tls_insecure"] is True
    assert broker["tls_mode"] == "insecure_no_verify"
    assert broker["credentials_ref"] == "local_mqtt_e2e"


def test_adoption_never_copies_a_broker_secret_into_the_draft():
    proposal = _grid_meter(broker_username="mqttuser", broker_password="s3cret")
    meter = _adopt(proposal)["draft"]["grid_meter"]
    assert "s3cret" not in json.dumps(meter)
    assert "password" not in json.dumps(meter)


def test_adoption_uses_the_same_broker_profile_helper_as_an_inverter_draft():
    js = _read()
    adopt = _extract_fn(js, "mconfigAdoptMqttGridMeterProposal")
    device = _extract_fn(js, "mconfigZendureMqttDraftFromProposal")
    assert "mqttProposalBrokerProfile(proposal, mapping.mqtt)" in adopt
    assert "mqttProposalBrokerProfile(proposal, mqtt)" in device
    assert js.count("function mqttProposalBrokerProfile(") == 1
    for forbidden in (
        "maintenanceGridMeterBroker",
        "provisionGridMeterBroker",
        "copyGridMeterBrokerProfile",
        "mconfigGridMeterBrokerProfile",
    ):
        assert forbidden not in js, forbidden


def test_a_declined_replacement_leaves_no_broker_in_the_draft():
    draft = {
        "devices": [],
        "grid_meter": {"present": True, "type": "shelly", "ip": "192.168.50.2"},
    }
    out = _adopt(_grid_meter(), draft=draft, confirm=False)
    assert out["adopted"] is False
    assert out["draft"]["grid_meter"] == draft["grid_meter"]
    assert "broker" not in json.dumps(out["draft"])


def test_re_adopting_the_same_meter_keeps_the_broker_unchanged():
    adopted = _adopt(_grid_meter())["draft"]["grid_meter"]
    draft = {"devices": [], "grid_meter": adopted}
    out = _adopt(_grid_meter(), draft=draft)
    assert out["adopted"] is False
    assert out["draft"]["grid_meter"] == adopted


def test_adoption_survives_rerender_as_added_to_draft():
    out = _adopt(_grid_meter())
    assert out["state"] == "added"


def test_adoption_keeps_the_broker_reference_of_its_own_connection():
    proposal = _grid_meter("zendure_cloud_mqtt")
    proposal["broker_ref"] = "cloud_broker"
    proposal["grid_meter_fragment"]["mqtt"]["broker_ref"] = "cloud_broker"
    out = _adopt(proposal)
    assert out["draft"]["grid_meter"]["mqtt"]["broker_ref"] == "cloud_broker"


def test_adoption_falls_back_to_the_trusted_proposal_broker_reference():
    proposal = _grid_meter()
    del proposal["grid_meter_fragment"]["mqtt"]["broker_ref"]
    out = _adopt(proposal)
    assert out["draft"]["grid_meter"]["mqtt"]["broker_ref"] == "local_mqtt_e2e"


def test_a_proposal_without_a_trusted_mapping_never_touches_the_draft():
    out = _adopt(_grid_meter_candidate())
    assert out["adopted"] is False
    assert out["draft"]["grid_meter"] == {}
    assert out["calls"] == []


def test_a_configured_grid_meter_is_never_replaced_silently():
    draft = {
        "devices": [],
        "grid_meter": {"present": True, "type": "shelly", "ip": "192.168.50.2"},
    }
    declined = _adopt(_grid_meter(), draft=draft, confirm=False)
    assert declined["adopted"] is False
    assert declined["calls"] == ["confirm"]
    assert declined["draft"]["grid_meter"]["type"] == "shelly"

    accepted = _adopt(_grid_meter(), draft=draft, confirm=True)
    assert accepted["adopted"] is True
    assert accepted["calls"][0] == "confirm"
    assert accepted["draft"]["grid_meter"]["type"] == "zendure_smartmeter_d0"
    # The replaced HTTP endpoint never lingers beside the MQTT settings.
    assert "ip" not in accepted["draft"]["grid_meter"]


def test_re_adopting_the_same_grid_meter_is_a_no_op():
    draft = dict(_drafted_meter(), devices=[])
    out = _adopt(_grid_meter(), draft=draft)
    assert out["adopted"] is False
    assert out["calls"] == []


# --- one shared mapping ---------------------------------------------------


def test_maintenance_adoption_uses_the_shared_proposal_mapping():
    js = _read()
    adopt = _extract_fn(js, "mconfigAdoptMqttGridMeterProposal")
    assert "mqttGridMeterConfigFromProposal(proposal)" in adopt
    assert "confirmGridMeterReplacement()" in adopt
    state = _extract_fn(js, "mconfigMqttGridMeterState")
    assert "mqttGridMeterConfigFromProposal(proposal)" in state
    review = _extract_fn(js, "mconfigMqttProposalReviewState")
    assert "mqttProposalHardwareRole(proposal)" in review
    card = _extract_fn(js, "renderMaintenanceMqttProposalCard")
    assert "mqttProposalHardwareRole(proposal)" in card
    assert 'hardwareRole === "grid_meter"' in card


def test_guided_setup_uses_the_same_mapping_and_replacement_confirmation():
    toggle = _extract_fn(_read(), "toggleMqttPreviewProposal")
    assert "mqttGridMeterConfigFromProposal(proposal)" in toggle
    assert "confirmGridMeterReplacement()" in toggle


def test_the_shared_mapping_only_trusts_the_backend_fragment():
    mapping = _extract_fn(_read(), "mqttGridMeterConfigFromProposal")
    assert "grid_meter_fragment" in mapping
    assert "mqttGridMeterProposalTopic(proposal)" in mapping
    # No re-derived D0 rule, meter type or payload default lives in the mapping.
    for forbidden in (
        "Zendure/sensor",
        "zendure_smartmeter_d0",
        "zendureD0Topic",
        '"number"',
        "max_age_seconds",
    ):
        assert forbidden not in mapping, forbidden


def test_no_second_d0_or_grid_meter_mapping_exists():
    js = _read()
    for forbidden in (
        "maintenanceMqttGridMeterMapping",
        "maintenanceD0Config",
        "maintenanceUseAsGridMeter",
        "mconfigD0Topic",
        "mconfigGridMeterFromProposal",
        "mconfigD0GridMeterConfig",
        "mconfigZendureD0Topic",
    ):
        assert forbidden not in js, forbidden
    for once in (
        "function mqttGridMeterConfigFromProposal(",
        "function confirmGridMeterReplacement(",
        "function mqttGridMeterProposalTopic(",
        "function zendureD0Topic(",
        "function zendureD0SerialFromTopic(",
        "const ZENDURE_D0_TOPIC_PREFIX = ",
        "const ZENDURE_D0_TOPIC_SUFFIX = ",
    ):
        assert js.count(once) == 1, once
    # The canonical D0 topic shape is declared once, by the shared constants.
    assert js.count('"Zendure/sensor/"') == 1
    assert js.count('"/totalPower"') == 1


def test_grid_meter_action_table_never_offers_an_inverter_action():
    js = _read()
    start = js.index("const MCONFIG_MQTT_GRID_METER_ACTIONS = ")
    table = js[start : js.index("};", start)]
    assert "Add inverter" not in table
    assert "Use connection" not in table
    assert table.count("Use as grid meter") == 2
