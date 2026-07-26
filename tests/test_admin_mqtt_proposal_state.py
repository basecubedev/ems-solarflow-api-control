# SPDX-License-Identifier: AGPL-3.0-or-later
"""Server-side proposal trust boundary (defect 3).

The browser receives a stable proposal id and, on submit, only that id (plus its
broker_ref) is resolved back to the full proposal held in current discovery
state. Trusted serial, broker identity, topic family and capabilities always
come from stored state; unknown, stale or forged selections are rejected before
any config is generated.
"""

import json

import pytest

from admin.mqtt_topic_discovery import MqttTopicAggregator
from admin.zendure_mqtt_config_proposals import (
    build_proposals,
    resolve_selected_proposals,
    resolve_trusted_proposal,
    index_trusted_proposals,
)

pytestmark = pytest.mark.simulation


def _device_proposals(serial="REAL", host="10.0.0.10", port=1883):
    agg = MqttTopicAggregator({"id": f"mqtt:{host}:{port}", "host": host, "port": port})
    for metric in ("outputPackPower", "electricLevel"):
        agg.observe(f"Zendure/sensor/{serial}/{metric}", None)
    return build_proposals(agg.results())


def _d0_proposals(serial="REAL", host="10.0.0.10"):
    agg = MqttTopicAggregator({"id": f"mqtt:{host}:1883", "host": host, "port": 1883})
    agg.observe(f"Zendure/sensor/{serial}/totalPower", b"-42")
    return build_proposals(agg.results())


def _selection(proposal, **overrides):
    sel = {
        "id": proposal["id"],
        "broker_ref": proposal["broker_ref"],
        "target": proposal["target"],
    }
    sel.update(overrides)
    return sel


def test_valid_proposal_id_resolves_to_trusted_proposal():
    trusted = _device_proposals()
    resolved, errors = resolve_selected_proposals(
        [_selection(trusted[0])], trusted
    )
    assert errors == []
    assert resolved[0]["serial_number"] == "REAL"
    assert resolved[0]["config_fragment"] == trusted[0]["config_fragment"]


def test_public_serial_alias_resolves_to_trusted_cloud_route_id():
    trusted = _device_proposals(serial="SERIAL")
    trusted[0]["device_id"] = "DEVICEKEY"
    trusted[0]["config_fragment"]["mqtt"]["device_id"] = "DEVICEKEY"
    resolved, errors = resolve_selected_proposals(
        [_selection(trusted[0], device_id="SERIAL")], trusted
    )
    assert errors == []
    assert resolved[0]["device_id"] == "DEVICEKEY"
    assert resolved[0]["config_fragment"]["mqtt"]["device_id"] == "DEVICEKEY"


def test_unknown_proposal_id_rejected():
    trusted = _device_proposals()
    resolved, errors = resolve_selected_proposals(
        [{"id": "zendure-mqtt:GHOST", "broker_ref": trusted[0]["broker_ref"]}], trusted
    )
    assert resolved == []
    assert errors[0]["code"] == "zendure_mqtt_proposal_unknown"


def test_missing_proposal_id_rejected():
    trusted = _device_proposals()
    resolved, errors = resolve_selected_proposals([{"target": "device"}], trusted)
    assert resolved == []
    assert errors[0]["code"] == "zendure_mqtt_proposal_id_missing"


def test_stale_proposal_from_previous_session_rejected():
    # A proposal captured earlier no longer present in current discovery state.
    old = _device_proposals(serial="OLD")
    current = _device_proposals(serial="NEW")
    resolved, errors = resolve_selected_proposals([_selection(old[0])], current)
    assert resolved == []
    assert errors[0]["code"] == "zendure_mqtt_proposal_unknown"


def test_browser_changed_serial_is_ignored_trusted_wins():
    # Defect 1: serial is mutable trusted evidence, not a selection assertion. A
    # stale/forged browser serial is ignored and the trusted serial wins.
    trusted = _device_proposals(serial="REAL")
    sel = _selection(trusted[0], serial_number="FAKE")
    resolved, errors = resolve_selected_proposals([sel], trusted)
    assert errors == []
    assert resolved[0]["serial_number"] == "REAL"
    assert "FAKE" not in json.dumps(resolved)


def test_browser_changed_broker_host_is_ignored_never_reaches_config():
    trusted = _device_proposals(host="10.0.0.10")
    sel = _selection(trusted[0], broker_host="10.0.0.99")
    resolved, errors = resolve_selected_proposals([sel], trusted)
    assert errors == []
    assert "10.0.0.99" not in json.dumps(resolved)


def test_browser_changed_broker_ref_is_unknown():
    trusted = _device_proposals()
    sel = _selection(trusted[0], broker_ref="local_mqtt_forged_deadbeef")
    resolved, errors = resolve_selected_proposals([sel], trusted)
    assert resolved == []
    assert errors[0]["code"] == "zendure_mqtt_proposal_unknown"


def test_browser_changed_topic_family_is_ignored_trusted_wins():
    # Defect 1: topic family is mutable discovery evidence; a stale browser echo is
    # ignored and the current trusted family is used.
    trusted = _device_proposals()
    trusted[0]["topic_family"] = "legacy_zendure_json"
    sel = _selection(trusted[0], topic_family="something_else")
    resolved, errors = resolve_selected_proposals([sel], trusted)
    assert errors == []
    assert resolved[0]["topic_family"] == "legacy_zendure_json"


def test_browser_injected_seen_topics_is_ignored_trusted_wins():
    # Defect 1: observed topics grow while the same trusted proposal is selected; a
    # stale/injected browser value is ignored and the trusted set is used.
    trusted = _d0_proposals(serial="REAL")
    sel = _selection(trusted[0], seen_topics=["Zendure/sensor/FAKE/totalPower"])
    resolved, errors = resolve_selected_proposals([sel], trusted)
    assert errors == []
    assert resolved[0]["seen_topics"] == trusted[0]["seen_topics"]
    assert "FAKE" not in json.dumps(resolved)


def test_stored_selection_survives_product_key_appearance():
    # Defect 1: the trusted proposal gains a product key the browser never had; the
    # selection still resolves and the trusted product key wins.
    trusted = _device_proposals()
    trusted[0]["product_key"] = "PKNEW"
    sel = _selection(trusted[0])
    sel.pop("product_key", None)
    resolved, errors = resolve_selected_proposals([sel], trusted)
    assert errors == []
    assert resolved[0]["product_key"] == "PKNEW"


def test_stored_selection_ignores_stale_product_key_and_model_evidence():
    trusted = _device_proposals()
    trusted[0]["product_key"] = "PKNEW"
    trusted[0]["product"] = "SolarFlow 800 Pro2"
    sel = _selection(trusted[0], product_key="PKOLD", product="Hyper 2000")
    resolved, errors = resolve_selected_proposals([sel], trusted)
    assert errors == []
    assert resolved[0]["product_key"] == "PKNEW"
    assert resolved[0]["product"] == "SolarFlow 800 Pro2"
    assert "PKOLD" not in json.dumps(resolved)


def test_trusted_current_fields_always_win_over_browser_echo():
    # Defect 1: many mutable browser echoes at once are all ignored; every trusted
    # value wins and no stale value reaches the resolved proposal.
    trusted = _device_proposals(serial="REAL")
    sel = _selection(
        trusted[0],
        serial_number="STALE",
        topic_family="stale_family",
        product_key="STALEPK",
        device_id="STALEDEV",
        seen_topics=["Zendure/sensor/STALE/totalPower"],
        broker_host="10.9.9.9",
        connection_source="zendure_cloud_mqtt",
    )
    resolved, errors = resolve_selected_proposals([sel], trusted)
    assert errors == []
    assert resolved[0]["serial_number"] == trusted[0]["serial_number"]
    assert resolved[0]["config_fragment"] == trusted[0]["config_fragment"]
    assert "STALE" not in json.dumps(resolved)


def test_replace_grid_meter_must_be_real_boolean():
    trusted = _d0_proposals()
    sel = _selection(trusted[0], replace_grid_meter="false")
    resolved, errors = resolve_selected_proposals([sel], trusted)
    assert resolved == []
    assert errors[0]["code"] == "zendure_mqtt_replace_invalid"


def test_replace_grid_meter_true_boolean_survives():
    trusted = _d0_proposals()
    resolved, errors = resolve_selected_proposals(
        [_selection(trusted[0], replace_grid_meter=True)], trusted
    )
    assert errors == []
    assert resolved[0]["replace_grid_meter"] is True


def test_scenario5_fully_forged_proposal_never_accepted():
    # Trusted: serial REAL on 10.0.0.10. Browser forges serial/broker/topic.
    trusted = _d0_proposals(serial="REAL", host="10.0.0.10")
    forged = {
        "id": "zendure-mqtt:FAKE",
        "broker_ref": "local_mqtt_10_0_0_99_deadbeef",
        "serial_number": "FAKE",
        "device_id": "FAKE",
        "broker_host": "10.0.0.99",
        "seen_topics": ["Zendure/sensor/FAKE/totalPower"],
        "target": "grid_meter",
    }
    resolved, errors = resolve_selected_proposals([forged], trusted)
    assert resolved == []
    assert errors
    # No FAKE identifier leaks into the resolved output or the error text.
    assert "FAKE" not in json.dumps(resolved)


def test_index_and_resolve_do_not_expose_full_store():
    trusted = _device_proposals()
    index = index_trusted_proposals(trusted)
    # The index is keyed by (id, broker_ref); a resolution returns a single copy,
    # never the whole set.
    resolved, issue = resolve_trusted_proposal(_selection(trusted[0]), index)
    assert issue is None
    assert isinstance(resolved, dict)
    assert resolved is not trusted[0]
