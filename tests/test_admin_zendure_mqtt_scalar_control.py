# SPDX-FileCopyrightText: 2026 basecubedev
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Admin must not turn a control-capable Zendure model into a telemetry source.

The observed telemetry family is a discovery artifact: the same physical
inverter is classified ``zensdk_ha_scalar``, ``zendure_cloud_scalar`` or
``legacy_zendure_json_alt`` depending on which topic discovery matched first,
while the write route stays ``iot/<productKey>/<deviceId>/…`` in every case.

These contracts pin the Admin-facing consequences: a discovery proposal with a
complete write route is controllable regardless of the family, the manual form
collects the identifiers that route actually needs, and an incomplete or unknown
device still fails closed with a precise reason.

The broker source is a separate axis with its own contracts
(``test_zendure_mqtt_broker_source_capability.py`` /
``…_enforcement.py``); here it is held at the Zendure cloud broker, which
carries the write route on every family, so these cases isolate the family and
route axes.
"""

from pathlib import Path

import pytest

from admin.models import MqttHardwareCandidate
from admin.zendure_mqtt_config_draft import build_manual_zendure_mqtt_fragment
from admin.zendure_mqtt_config_proposals import build_proposals
from tests.modern_frontend import js_call

pytestmark = [
    pytest.mark.admin,
    pytest.mark.mqtt,
    pytest.mark.contract,
    pytest.mark.simulation,
]

PRODUCT_KEY = "TESTPK0001"
ROUTE_DEVICE_ID = "TESTROUTE01"
SETUP_DRAFT_JS = Path(__file__).resolve().parents[1] / "frontend" / "admin-modern" / "src" / "lib" / "setupDraft.js"


def manual_output_control_capability(*args, **kwargs):
    from admin.zendure_mqtt_config_draft import (
        manual_output_control_capability as projection,
    )

    return projection(*args, **kwargs)


CLOUD_SOURCE = "zendure_cloud_mqtt"
LOCAL_SOURCE = "local_mqtt"


def _candidate(**overrides):
    fields = dict(
        source_type=CLOUD_SOURCE,
        tls_mode="encrypted_no_verify",
        broker_id="b1",
        broker_host="mqtteu.zen-iot.com",
        broker_port=8883,
        topic_family="zensdk_ha_scalar",
        device_id=ROUTE_DEVICE_ID,
        serial_number="TESTSN000001",
        product_key=PRODUCT_KEY,
        model_hint="SolarFlow 800 Pro 2",
        metrics_seen=[
            "electricLevel",
            "outputHomePower",
            "solarInputPower",
            "outputLimit",
        ],
    )
    fields.update(overrides)
    return MqttHardwareCandidate(**fields)


# --- discovery proposals ------------------------------------------------------


@pytest.mark.parametrize(
    "family",
    ["zensdk_ha_scalar", "zendure_cloud_scalar", "legacy_zendure_json", "legacy_zendure_json_alt"],
)
def test_a_known_model_with_a_complete_route_is_controllable_on_every_family(family):
    """The reproduction: a scalar family made an 800 Pro 2 telemetry-only."""

    proposals = build_proposals([_candidate(topic_family=family).to_dict()])

    assert len(proposals) == 1
    proposal = proposals[0]
    assert proposal["output_control_supported"] is True, proposal["output_control_reason"]
    assert proposal["config_fragment"]["capabilities"]["write_output_limit"] is True
    assert proposal["config_fragment"]["hardware_profile"] == "solarflow_800_pro_2"


def test_a_known_model_without_a_product_key_names_the_missing_write_target():
    """Fail closed, but say what is missing — not "the transport is wrong"."""

    proposals = build_proposals([_candidate(product_key=None).to_dict()])

    proposal = proposals[0]
    assert proposal["output_control_supported"] is False
    assert proposal["output_control_reason"] == "write_target_missing"
    assert proposal["config_fragment"]["capabilities"]["write_output_limit"] is False


def test_an_unknown_model_on_a_scalar_family_still_fails_closed():
    proposals = build_proposals([_candidate(model_hint="Totally Unknown 9000").to_dict()])

    proposal = proposals[0]
    assert proposal["output_control_supported"] is False
    assert proposal["config_fragment"]["capabilities"]["write_output_limit"] is False


@pytest.mark.parametrize(
    ("model_hint", "product_key"),
    [
        ("SolarFlow 800 Pro 2", PRODUCT_KEY),
        ("SolarFlow 800 Pro 2", None),
        ("Hyper 2000", PRODUCT_KEY),
        ("ACE 1500", PRODUCT_KEY),
        ("Totally Unknown 9000", PRODUCT_KEY),
    ],
)
def test_the_proposal_verdict_is_the_core_verdict(model_hint, product_key):
    """Admin projects EMS/Core's capability; it never computes a second one."""

    from ems.mqtt_control.zendure_profiles import resolve_hardware_profile
    from ems.zendure_mqtt.capability import resolve_output_control_capability

    proposal = build_proposals(
        [_candidate(model_hint=model_hint, product_key=product_key).to_dict()]
    )[0]
    resolved = resolve_hardware_profile(model_hint)
    core = resolve_output_control_capability(
        topic_family="zensdk_ha_scalar",
        hardware_profile=resolved.canonical_name if resolved else "",
        broker_source=CLOUD_SOURCE,
        product_key=product_key,
        device_id=ROUTE_DEVICE_ID,
    )

    assert proposal["output_control_supported"] is core.supported


# --- manual Setup -------------------------------------------------------------


def _manual_item(**overrides):
    item = {
        "name": "INV_1",
        "serial_number": "TESTSN000001",
        "mqtt_device_id": ROUTE_DEVICE_ID,
        "product_key": PRODUCT_KEY,
        "hardware_generation": "solarflow_zensdk",
        "hardware_model": "solarflow_800_pro_2",
        "capabilities": {"write_output_limit": True},
    }
    item.update(overrides)
    return item


def test_a_manual_zensdk_device_keeps_its_product_key_and_output_control():
    """A ZenSDK write needs a product key; the manual form must persist it."""

    fragment, issues = build_manual_zendure_mqtt_fragment(
        _manual_item(), "cloud", broker_source=CLOUD_SOURCE
    )

    assert fragment is not None, issues
    assert fragment["capabilities"]["write_output_limit"] is True
    assert fragment["mqtt"]["product_key"] == PRODUCT_KEY
    assert fragment["mqtt"]["device_id"] == ROUTE_DEVICE_ID
    assert fragment["hardware_profile"] == "solarflow_800_pro_2"
    assert issues == []


def test_a_manual_legacy_device_stays_controllable():
    """The legacy JSON report family is an implemented local write path."""

    fragment, issues = build_manual_zendure_mqtt_fragment(
        _manual_item(
            hardware_generation="hub_hyper_legacy", hardware_model="hyper_2000"
        ),
        "local_mqtt",
        broker_source=LOCAL_SOURCE,
    )

    assert fragment is not None, issues
    assert fragment["capabilities"]["write_output_limit"] is True
    assert fragment["hardware_profile"] == "hyper_2000"


def test_a_manual_device_without_a_product_key_is_refused_with_the_write_target_reason():
    fragment, issues = build_manual_zendure_mqtt_fragment(
        _manual_item(product_key=""), "cloud", broker_source=CLOUD_SOURCE
    )

    assert fragment is None
    assert [issue["code"] for issue in issues] == [
        "zendure_mqtt_control_write_target_missing"
    ]


def test_a_manual_device_without_a_route_id_is_refused():
    fragment, issues = build_manual_zendure_mqtt_fragment(
        _manual_item(mqtt_device_id=""), "cloud", broker_source=CLOUD_SOURCE
    )

    assert fragment is None
    assert [issue["code"] for issue in issues] == ["mqtt_device_id_missing"]


def test_a_manual_telemetry_only_model_is_added_without_control():
    fragment, issues = build_manual_zendure_mqtt_fragment(
        _manual_item(hardware_generation="hub_hyper_legacy", hardware_model="ace_1500"),
        "local_mqtt",
        broker_source=LOCAL_SOURCE,
    )

    assert fragment is not None
    assert fragment["capabilities"]["write_output_limit"] is False
    assert [issue["code"] for issue in issues] == ["zendure_mqtt_control_unavailable"]


# --- the shared manual projection Setup and Maintenance both read -------------


def test_the_manual_projection_reports_available_for_a_complete_known_profile():
    verdict = manual_output_control_capability(
        "solarflow_zensdk",
        "solarflow_800_pro_2",
        product_key=PRODUCT_KEY,
        device_id=ROUTE_DEVICE_ID,
        broker_source=CLOUD_SOURCE,
    )

    assert verdict["supported"] is True
    assert verdict["reason"] is None
    assert verdict["model_supported"] is True
    assert verdict["transport_supported"] is True
    assert verdict["write_route_ready"] is True
    assert verdict["telemetry_family"] == "zensdk_ha_scalar"
    assert verdict["write_family"] == "iot_properties_write"


def test_the_manual_projection_names_the_missing_identifier():
    verdict = manual_output_control_capability(
        "solarflow_zensdk",
        "solarflow_800_pro_2",
        product_key="",
        device_id="",
        broker_source=CLOUD_SOURCE,
    )

    assert verdict["supported"] is False
    assert verdict["model_supported"] is True
    assert verdict["write_route_ready"] is False
    assert verdict["reason"] == "missing_product_key"


def test_the_manual_projection_fails_closed_without_a_model():
    verdict = manual_output_control_capability(
        "solarflow_zensdk",
        "",
        product_key=PRODUCT_KEY,
        device_id=ROUTE_DEVICE_ID,
        broker_source=CLOUD_SOURCE,
    )

    assert verdict["supported"] is False
    assert verdict["model_supported"] is False
    assert verdict["reason"] == "hardware_profile_missing"


def test_the_manual_projection_rejects_an_unknown_generation():
    verdict = manual_output_control_capability(
        "not_a_generation",
        "solarflow_800_pro_2",
        product_key=PRODUCT_KEY,
        device_id=ROUTE_DEVICE_ID,
        broker_source=CLOUD_SOURCE,
    )

    assert verdict["supported"] is False
    assert verdict["reason"] == "hardware_generation_unknown"


def _browser_manual_verdict(generation, model, route_id, product_key):
    """The Admin console's own manual-control verdict, read against the real setup catalog.

    The catalog carries the Core-derived ``control_broker_sources`` projection,
    so the console reads that rather than a rule of its own.
    """

    from admin.setup_config import build_setup_catalog

    return js_call(
        SETUP_DRAFT_JS,
        "manualControl",
        build_setup_catalog(),
        {"generation": generation, "model": model, "routeId": route_id, "productKey": product_key},
    )


@pytest.mark.parametrize(
    ("generation_id", "model_id", "route_id", "product_key", "controllable"),
    [
        ("hub_hyper_legacy", "hyper_2000", ROUTE_DEVICE_ID, PRODUCT_KEY, True),
        ("solarflow_zensdk", "solarflow_800_pro_2", ROUTE_DEVICE_ID, PRODUCT_KEY, False),
        ("zendure_cloud", "solarflow_800_pro_2", ROUTE_DEVICE_ID, PRODUCT_KEY, False),
        ("hub_hyper_legacy", "ace_1500", ROUTE_DEVICE_ID, PRODUCT_KEY, False),
        ("hub_hyper_legacy", "hyper_2000", ROUTE_DEVICE_ID, "", False),
        ("hub_hyper_legacy", "hyper_2000", "", PRODUCT_KEY, False),
        ("hub_hyper_legacy", "hyper_2000", "", "", False),
        ("hub_hyper_legacy", "", ROUTE_DEVICE_ID, PRODUCT_KEY, False),
    ],
)
def test_the_browser_and_the_backend_agree_on_manual_capability(
    generation_id, model_id, route_id, product_key, controllable
):
    """One verdict per input, whichever layer is asked.

    The browser projects; the fragment builder decides. They must never disagree
    about whether a manually entered profile ends up controllable. The Admin
    console enters such a device on the local broker only
    (``MANUAL_BROKER_SOURCE``), so both are asked there, and a generation that
    controls only over the Zendure cloud broker stays telemetry-only in both.
    """

    browser = _browser_manual_verdict(generation_id, model_id, route_id, product_key)
    fragment, _issues = build_manual_zendure_mqtt_fragment(
        _manual_item(
            hardware_generation=generation_id,
            hardware_model=model_id,
            mqtt_device_id=route_id,
            product_key=product_key,
        ),
        "b1",
        broker_source=LOCAL_SOURCE,
    )
    backend = bool(
        fragment is not None
        and fragment["capabilities"]["write_output_limit"] is True
    )

    assert browser["enabled"] is backend is controllable


def test_every_control_capable_model_needs_a_product_key_route():
    """One rule for all generations: the canonical topic is iot/<pk>/<dev>/…"""

    for generation, model in (
        ("solarflow_zensdk", "solarflow_800_pro_2"),
        ("hub_hyper_legacy", "hyper_2000"),
        ("zendure_cloud", "solarflow_800_pro_2"),
    ):
        verdict = manual_output_control_capability(
            generation,
            model,
            product_key="",
            device_id=ROUTE_DEVICE_ID,
            broker_source=CLOUD_SOURCE,
        )
        assert verdict["reason"] == "missing_product_key", (generation, model)
