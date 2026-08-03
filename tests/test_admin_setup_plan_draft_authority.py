# SPDX-License-Identifier: AGPL-3.0-or-later
"""A device plan authorizes one draft, in one workflow, over one candidate set.

Archive 116 made Config Preview prove that *some* current plan exists. That is
not the same as proving the draft in front of it is the one that plan produced.
A plan is a decision about specific devices reached over specific connections;
used as a general permission slip it authorizes whatever the browser happens to
post next.

Three ways that gap is reachable, each one a conflict here:

* the same valid plan presented with an unrelated draft,
* a plan whose candidate kept its public handle while its identity or its
  ability to be controlled changed underneath,
* a plan issued in one Setup workflow and spent in another.

Every rejection must leave ``config/config.json`` byte-exact.
"""

from pathlib import Path

import pytest

from admin.install_context import detect_install_context
from admin.mqtt_discovery import MqttBrokerDiscovery, MqttBrokerStore
from admin.setup_planner import (
    expected_selection_projections,
    setup_draft_fingerprint,
    submitted_selection_projections,
    trusted_proposals_by_id,
)
from tests.test_admin_server import _control_export_manager, _request, _serve

pytestmark = pytest.mark.simulation

SERIAL = "EOD1AAA111"
HOST = "10.0.0.11"


@pytest.fixture(autouse=True)
def _isolate(isolated_install_root):
    return isolated_install_root


class _Devices:
    def __init__(self, devices=()):
        self.devices_list = [dict(device) for device in devices]

    def devices(self):
        return [dict(device) for device in self.devices_list]

    def ignored_devices(self):
        return []

    def status(self):
        return {"enabled": False}

    def refresh(self):
        return {}


def _inverter(serial=SERIAL, ip=HOST, **extra):
    return dict(
        {
            "id": "zendure_local_http:" + serial,
            "role_suggestion": "inverter",
            "device_type": "solarflow",
            "api_family": "solarflow",
            "ip": ip,
            "port": 8080,
            "serial_number": serial,
            "verified": True,
            "usable_for_config": True,
        },
        **extra,
    )


def _write_live(payload='{"live": "A"}\n'):
    path = Path(detect_install_context().config_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(payload, encoding="utf-8")
    return path


def _start_workflow(base):
    status, _, payload = _request(
        f"{base}/api/admin/start-path",
        method="POST",
        body={"choice": "setup_new", "confirm": True},
    )
    assert status == 200, payload
    return payload["setup_workflow_id"]


def _observation_id(base, ip=HOST):
    """The issued handle the browser would key its draft item on."""

    status, _, payload = _request(f"{base}/api/discovery/devices")
    assert status == 200, payload
    for device in payload.get("devices", []):
        if device.get("ip") == ip:
            return device["observation_id"]
    raise AssertionError(f"no observation for {ip}: {payload}")


def _draft_item(observation_id, *, serial=SERIAL, ip=HOST, name="WR1"):
    """One inverter as the browser persists it."""

    return {
        "source_id": observation_id,
        "draft_item_id": "item-1",
        "role": "inverter",
        "enabled": True,
        "config_name": name,
        "display_name": "Balcony inverter",
        "ip": ip,
        "port": 8080,
        "serial_number": serial,
        "device_type": "solarflow",
        "api_family": "solarflow",
        "auto_added": False,
    }


def _plan_state(items):
    return {"draft_items": [dict(item) for item in items]}


def _device_plan(base, state=None, workflow_id=None, **extra):
    body = dict({"state": state or {}}, **extra)
    if workflow_id is not None:
        body["setup_workflow_id"] = workflow_id
    status, _, payload = _request(
        f"{base}/api/setup/device-plan", method="POST", body=body
    )
    assert status == 200, payload
    return payload


def _preview(base, workflow_id, device_plan_id, devices):
    request = {
        "setup_workflow_id": workflow_id,
        "device_plan_id": device_plan_id,
        "devices": [dict(item) for item in devices],
        "supported_grid_meter_count": 0,
    }
    return _request(f"{base}/api/setup/config-preview", method="POST", body=request)


def _planned(base, workflow_id, items):
    """A settled plan over exactly ``items``, plus the items themselves."""

    plan = _device_plan(base, _plan_state(items), workflow_id=workflow_id)
    assert plan["confirmation_required"] is False, plan
    return plan


# --- Regression A: a valid plan does not authorize an unrelated draft --------
def _evil_item():
    return _draft_item(
        "obs:v1:invented", serial="INVENTED999", ip="192.0.2.77", name="WR-EVIL"
    )


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(
            lambda items: [dict(items[0], serial_number="INVENTED999")],
            id="changed-serial",
        ),
        pytest.param(
            lambda items: [dict(items[0], ip="192.0.2.77")], id="changed-host"
        ),
        pytest.param(
            lambda items: [dict(items[0], api_family="zendure_mqtt")],
            id="changed-connection-type",
        ),
        pytest.param(lambda items: items + [_evil_item()], id="additional-device"),
        pytest.param(lambda items: [], id="removed-device"),
        pytest.param(
            lambda items: [dict(items[0], source_id="obs:v1:elsewhere")],
            id="changed-selected-observation",
        ),
        pytest.param(
            lambda items: [dict(items[0], device_type="hyper")],
            id="changed-control-relevant-field",
        ),
    ],
)
def test_a_valid_plan_does_not_authorize_a_different_draft(tmp_path, mutate):
    srv, base = _serve(
        mdns_provider=_Devices([_inverter()]),
        release_manager=_control_export_manager(tmp_path),
    )
    live = _write_live()
    before = live.read_bytes()
    try:
        workflow_id = _start_workflow(base)
        planned = [_draft_item(_observation_id(base))]
        plan = _planned(base, workflow_id, planned)

        status, _, payload = _preview(
            base, workflow_id, plan["plan_id"], mutate(planned)
        )

        assert status == 409, payload
        assert payload["error"] in ("stale_device_plan", "device_plan_draft_mismatch")
        assert "config_preview_id" not in payload
        assert live.read_bytes() == before
    finally:
        srv.shutdown()
        srv.server_close()


def test_an_invented_device_cannot_ride_a_valid_plan_into_config(tmp_path):
    """The reported exploit, end to end: nothing invented reaches the file."""

    srv, base = _serve(
        mdns_provider=_Devices([_inverter()]),
        release_manager=_control_export_manager(tmp_path),
    )
    live = _write_live()
    before = live.read_bytes()
    try:
        workflow_id = _start_workflow(base)
        planned = [_draft_item(_observation_id(base))]
        plan = _planned(base, workflow_id, planned)

        status, _, payload = _preview(
            base, workflow_id, plan["plan_id"], [_evil_item()]
        )
        assert status == 409, payload

        written = live.read_bytes()
        assert written == before
        assert b"INVENTED999" not in written
        assert b"192.0.2.77" not in written
    finally:
        srv.shutdown()
        srv.server_close()


def test_the_exact_planned_draft_still_previews(tmp_path):
    """The binding must not cost a legitimate review its authority."""

    srv, base = _serve(
        mdns_provider=_Devices([_inverter()]),
        release_manager=_control_export_manager(tmp_path),
    )
    _write_live()
    try:
        workflow_id = _start_workflow(base)
        planned = [_draft_item(_observation_id(base))]
        plan = _planned(base, workflow_id, planned)

        status, _, payload = _preview(base, workflow_id, plan["plan_id"], planned)

        assert status == 200, payload
        assert payload["config_preview_id"]
    finally:
        srv.shutdown()
        srv.server_close()


def test_an_operator_rename_does_not_revoke_the_plan(tmp_path):
    """A label is operator intent, not a device the plan decided on."""

    srv, base = _serve(
        mdns_provider=_Devices([_inverter()]),
        release_manager=_control_export_manager(tmp_path),
    )
    _write_live()
    try:
        workflow_id = _start_workflow(base)
        planned = [_draft_item(_observation_id(base))]
        plan = _planned(base, workflow_id, planned)

        renamed = [dict(planned[0], config_name="Garage", display_name="Garage")]
        status, _, payload = _preview(base, workflow_id, plan["plan_id"], renamed)

        assert status == 200, payload
    finally:
        srv.shutdown()
        srv.server_close()


# --- Regression B: a stable handle does not freeze the candidate -------------
def test_a_changed_serial_behind_a_stable_route_stales_the_plan(tmp_path):
    """Same address, different hardware: the old decision no longer applies."""

    provider = _Devices([_inverter()])
    srv, base = _serve(
        mdns_provider=provider, release_manager=_control_export_manager(tmp_path)
    )
    live = _write_live()
    before = live.read_bytes()
    try:
        workflow_id = _start_workflow(base)
        planned = [_draft_item(_observation_id(base))]
        plan = _planned(base, workflow_id, planned)

        # The route is what the observation id is derived from, so replacing the
        # hardware at that address keeps the handle and moves the identity.
        provider.devices_list[0]["serial_number"] = "EOD1CHANGED999"

        status, _, payload = _preview(base, workflow_id, plan["plan_id"], planned)
        assert status == 409, payload
        assert payload["error"] in ("stale_device_plan", "device_plan_draft_mismatch")
        assert live.read_bytes() == before
    finally:
        srv.shutdown()
        srv.server_close()


def test_a_changed_capability_behind_a_stable_route_stales_the_plan(tmp_path):
    """What may be written to a device is part of what the plan decided."""

    provider = _Devices([_inverter()])
    srv, base = _serve(
        mdns_provider=provider, release_manager=_control_export_manager(tmp_path)
    )
    _write_live()
    try:
        workflow_id = _start_workflow(base)
        planned = [_draft_item(_observation_id(base))]
        plan = _planned(base, workflow_id, planned)

        provider.devices_list[0]["device_type"] = "hyper"
        provider.devices_list[0]["api_family"] = "hyper"

        status, _, payload = _preview(base, workflow_id, plan["plan_id"], planned)
        assert status == 409, payload
    finally:
        srv.shutdown()
        srv.server_close()


def _scalar_local_mqtt_discovery():
    store = MqttBrokerStore(clock=lambda: 100.0, proposal_ttl_seconds=900)
    generation = store.begin_refresh()
    store.complete_refresh(
        generation,
        [
            {
                "id": "mqtt:10.0.0.10:1883",
                "host": "10.0.0.10",
                "port": 1883,
                "devices": [
                    {
                        "source_type": "local_mqtt",
                        "broker_host": "10.0.0.10",
                        "broker_port": 1883,
                        "topic_family": "scalar_leaf",
                        "serial_number": SERIAL,
                        "device_id": SERIAL,
                        "metrics_seen": ["electricLevel", "outputHomePower"],
                    }
                ],
            }
        ],
        success=True,
    )
    return MqttBrokerDiscovery(store=store, topic_discoverer=None)


def test_a_new_candidate_that_changes_the_verdict_stales_the_plan(tmp_path):
    """A plan issued with no question pending must not survive one appearing."""

    srv, base = _serve(
        mdns_provider=_Devices([_inverter()]),
        mqtt_discovery=_scalar_local_mqtt_discovery(),
        release_manager=_control_export_manager(tmp_path),
    )
    _write_live()
    try:
        workflow_id = _start_workflow(base)
        planned = [_draft_item(_observation_id(base))]
        plan = _planned(base, workflow_id, planned)

        # The operator reorders discovery priority: the same candidates now
        # produce a capability-losing switch that needs confirming.
        status, _, _ = _request(
            f"{base}/api/discovery/preparation",
            method="POST",
            body={"discovery_priority": ["local_mqtt", "zendure_mqtt", "local_api"]},
        )
        assert status == 200

        status, _, payload = _preview(base, workflow_id, plan["plan_id"], planned)
        assert status == 409, payload
    finally:
        srv.shutdown()
        srv.server_close()


# --- Regression C: a plan belongs to the workflow that asked for it ----------
def _replace_workflow(srv):
    """Start a fresh Setup workflow, as a replacement or a reset would."""

    return srv.setup_workflows.start_replacement()["workflow_id"]


def test_a_plan_from_a_replaced_workflow_is_refused(tmp_path):
    srv, base = _serve(
        mdns_provider=_Devices([_inverter()]),
        release_manager=_control_export_manager(tmp_path),
    )
    live = _write_live()
    before = live.read_bytes()
    try:
        workflow_a = _start_workflow(base)
        planned = [_draft_item(_observation_id(base))]
        plan_a = _planned(base, workflow_a, planned)

        workflow_b = _replace_workflow(srv)
        assert workflow_b != workflow_a

        status, _, payload = _preview(
            base, workflow_b, plan_a["plan_id"], planned
        )
        assert status == 409, payload
        assert payload["error"] in ("stale_device_plan", "setup_workflow_not_active")
        assert live.read_bytes() == before
    finally:
        srv.shutdown()
        srv.server_close()


def test_a_plan_cannot_be_spent_under_its_own_replaced_workflow_id(tmp_path):
    """The workflow the plan names is gone; presenting its id does not revive it."""

    srv, base = _serve(
        mdns_provider=_Devices([_inverter()]),
        release_manager=_control_export_manager(tmp_path),
    )
    _write_live()
    try:
        workflow_a = _start_workflow(base)
        planned = [_draft_item(_observation_id(base))]
        plan_a = _planned(base, workflow_a, planned)
        _replace_workflow(srv)

        status, _, payload = _preview(base, workflow_a, plan_a["plan_id"], planned)
        assert status == 409, payload
    finally:
        srv.shutdown()
        srv.server_close()


def test_the_replacing_workflow_can_still_plan_for_itself(tmp_path):
    """Rejecting a foreign plan must not strand the workflow that replaced it."""

    srv, base = _serve(
        mdns_provider=_Devices([_inverter()]),
        release_manager=_control_export_manager(tmp_path),
    )
    _write_live()
    try:
        workflow_a = _start_workflow(base)
        planned = [_draft_item(_observation_id(base))]
        _planned(base, workflow_a, planned)

        workflow_b = _replace_workflow(srv)
        plan_b = _planned(base, workflow_b, planned)

        status, _, payload = _preview(base, workflow_b, plan_b["plan_id"], planned)
        assert status == 200, payload
        assert payload["config_preview_id"]
    finally:
        srv.shutdown()
        srv.server_close()


# --- the selection half of the same binding ---------------------------------
def test_a_selection_the_plan_did_not_make_changes_the_fingerprint():
    """A device reached over MQTT is a device the plan decided on too.

    An untrusted selection is already refused a layer earlier, when it is
    resolved against current discovery state. What this pins is the trusted but
    *unplanned* one: swapping which offered connection is configured is a
    different draft, and a plan that did not make that choice cannot authorize
    it.
    """

    state = {"mqtt_selections": [{"id": "zendure-mqtt:A", "broker_ref": "local"}]}
    operations = {"drop_mqtt_selections": [], "select_mqtt_proposals": []}
    key = b"k" * 32

    planned = setup_draft_fingerprint(
        [],
        expected_selection_projections(state, operations),
        identity_token_key=key,
    )
    swapped = setup_draft_fingerprint(
        [],
        submitted_selection_projections(
            [{"id": "zendure-mqtt:B", "broker_ref": "local"}]
        ),
        identity_token_key=key,
    )
    same = setup_draft_fingerprint(
        [],
        submitted_selection_projections(
            [{"id": "zendure-mqtt:A", "broker_ref": "local"}]
        ),
        identity_token_key=key,
    )

    assert planned != swapped
    assert planned == same


def test_a_dropped_selection_leaves_the_expected_set():
    """A drop is a decision; an offer the browser has not taken up is not."""

    state = {
        "mqtt_selections": [
            {"id": "zendure-mqtt:A", "broker_ref": "local"},
            {"id": "zendure-mqtt:B", "broker_ref": "local"},
        ]
    }
    operations = {
        "drop_mqtt_selections": ["zendure-mqtt:A"],
        # Additive advice: the browser can only take it up if that proposal is
        # still in the list it renders, so predicting it would refuse the draft
        # this very plan was computed over.
        "select_mqtt_proposals": [{"id": "zendure-mqtt:C", "broker_ref": "cloud"}],
    }

    assert expected_selection_projections(state, operations) == [
        "zendure-mqtt:B|local"
    ]


# --- the two sides of the candidate fingerprint must normalize alike ---------
def test_the_trusted_proposal_set_is_normalized_once():
    """A duplicated proposal id must not split the two computations apart.

    The planner keys proposals by id; a caller recomputing what a plan was
    planned over has to do the same. Ids are not unique in every discovery
    shape — one inverter offered by two local brokers is issued the same
    serial-bearing id — so iterating the raw list would produce a candidate set
    the planner never had, and every plan would read as stale forever.
    """

    duplicated = [
        {"id": "zendure-mqtt:A", "connection_source": "local_mqtt", "n": 1},
        {"id": "zendure-mqtt:A", "connection_source": "local_mqtt", "n": 2},
        {"id": "", "connection_source": "local_mqtt"},
        {"id": "zendure-mqtt:B", "connection_source": "local_mqtt"},
    ]

    normalized = trusted_proposals_by_id(duplicated)

    assert list(normalized) == ["zendure-mqtt:A", "zendure-mqtt:B"]
    # Last value wins, as the planner has always resolved it.
    assert normalized["zendure-mqtt:A"]["n"] == 2
    # Idempotent: normalizing an already-normalized set changes nothing.
    assert trusted_proposals_by_id(normalized.values()) == normalized


def test_a_duplicated_proposal_id_does_not_stale_every_plan(tmp_path):
    """The generation is computed over the same normalized set on both sides."""

    srv, base = _serve(
        mdns_provider=_Devices([_inverter()]),
        mqtt_discovery=_scalar_local_mqtt_discovery(),
        release_manager=_control_export_manager(tmp_path),
    )
    _write_live()
    try:
        workflow_id = _start_workflow(base)
        planned = [_draft_item(_observation_id(base))]
        plan = _planned(base, workflow_id, planned)

        status, _, payload = _preview(base, workflow_id, plan["plan_id"], planned)
        assert status == 200, payload
    finally:
        srv.shutdown()
        srv.server_close()
