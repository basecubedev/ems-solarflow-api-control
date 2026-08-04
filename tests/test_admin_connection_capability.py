# SPDX-License-Identifier: AGPL-3.0-or-later
"""Output-control capability for a *discovered connection*, for Setup planning.

Runtime, Maintenance and the proposal generator already answer "can this write
``outputLimit``?" from ``ems.zendure_mqtt.capability``. Setup's batch planner
needs the same answer for a candidate it has not configured yet, and
``admin/connection_capability.py`` is the adapter rather than a fourth opinion.

What this module pins is the adapter's contract: which record maps onto which
canonical input, and that "not resolved" stays distinct from "cannot" — because
the connection planner treats ``None`` as unresolved and never as capable. The
capability matrix itself belongs to
``tests/test_zendure_mqtt_write_capability_matrix.py`` and
``tests/test_zendure_mqtt_broker_source_capability.py``.
"""

import pytest

from admin.connection_capability import connection_output_control, payload_output_control

pytestmark = [
    pytest.mark.admin,
    pytest.mark.maintenance,
    pytest.mark.mqtt,
    pytest.mark.integration,
    pytest.mark.simulation,
]


class _Entry:
    def __init__(self, payload, source):
        self.payload = payload
        self.source = source


def _local_api(**overrides):
    device = {
        "role_suggestion": "inverter",
        "api_family": "zendure_local_http",
        "ip": "10.0.0.11",
        "port": 8080,
        "verified": True,
        "usable_for_config": True,
    }
    device.update(overrides)
    return device


def _mqtt(source, *, profile=None, family="zensdk_ha_scalar", device_id="DEV1", **overrides):
    mqtt = {"source": source, "device_id": device_id, "product_key": "PK1"}
    mqtt.update(overrides.pop("mqtt", {}))
    fragment = {"mqtt": mqtt}
    if profile:
        fragment["hardware_profile"] = profile
    proposal = {
        "connection_source": source,
        "topic_family": family,
        "config_fragment": fragment,
    }
    proposal.update(overrides)
    return proposal


# --- local API ---------------------------------------------------------------
def test_a_local_api_inverter_can_write_output_limit():
    """The EMS's own native write transport; there is no axis to resolve."""

    assert payload_output_control(_local_api(), "local_api") is True


def test_an_unverified_local_api_observation_is_not_resolved():
    assert payload_output_control(_local_api(verified=False), "local_api") is None


def test_a_local_api_grid_meter_is_not_an_output_control_connection():
    assert payload_output_control(_local_api(role_suggestion="grid_meter"), "local_api") is None


# --- MQTT: Core's own verdict wins ------------------------------------------
@pytest.mark.parametrize("supported", [True, False])
def test_a_proposals_resolved_verdict_is_the_answer(supported):
    proposal = _mqtt("zendure_cloud_mqtt", output_control_supported=supported)
    assert payload_output_control(proposal, "zendure_mqtt") is supported


# --- MQTT: derived from the canonical resolver ------------------------------
def test_cloud_mqtt_with_a_complete_route_keeps_control():
    proposal = _mqtt("zendure_cloud_mqtt", profile="solarflow_800_pro_2")
    assert payload_output_control(proposal, "zendure_mqtt") is True


def test_a_local_scalar_broker_is_fail_closed_for_the_same_model():
    """Only the broker source differs, and no hardware proves it relays a write."""

    proposal = _mqtt("local_mqtt", profile="solarflow_800_pro_2")
    assert payload_output_control(proposal, "local_mqtt") is False


def test_a_supported_local_json_write_path_keeps_control():
    proposal = _mqtt("local_mqtt", profile="hyper_2000", family="legacy_zendure_json")
    assert payload_output_control(proposal, "local_mqtt") is True


def test_an_incomplete_route_never_counts_as_capable():
    proposal = _mqtt("zendure_cloud_mqtt", profile="solarflow_800_pro_2")
    del proposal["config_fragment"]["mqtt"]["product_key"]
    assert payload_output_control(proposal, "zendure_mqtt") is not True


def test_a_record_without_a_broker_source_is_not_resolved():
    assert payload_output_control({"topic_family": "zensdk_ha_scalar"}, "local_mqtt") is None


# --- the planner-entry adapter ----------------------------------------------
def test_the_entry_adapter_reads_the_payload_and_its_source():
    assert connection_output_control(_Entry(_local_api(), "local_api")) is True
    assert (
        connection_output_control(
            _Entry(_mqtt("local_mqtt", profile="solarflow_800_pro_2"), "local_mqtt")
        )
        is False
    )


def test_an_entry_without_a_trusted_payload_is_not_resolved():
    """An unresolved legacy hint has no capability, because nothing resolved it."""

    assert connection_output_control(_Entry(None, "local_api")) is None
    assert connection_output_control(object()) is None
