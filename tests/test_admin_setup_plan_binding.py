# SPDX-License-Identifier: AGPL-3.0-or-later
"""Device Plan → Config Preview → Apply is one authority chain.

The device plan is where Setup decides what each physical device is and which
connection it is configured over. Config Preview and Apply are where that
decision reaches ``config/config.json``. Without a binding between them, a
browser could plan against one discovery state, keep the resulting draft, and
apply it after the world changed underneath — including after a switch the plan
said needed confirming.

So Preview refuses to issue mutation authority for anything but the current
device plan, that plan's identity goes into the exact-preview fingerprint, and
Apply re-verifies both. Every rejection is a conflict that leaves the live
config byte-exact.
"""

from pathlib import Path

import pytest

from admin.device_plan_registry import (
    CONTRACT_FIELDS,
    DevicePlanRegistry,
    mutation_authority_fingerprint,
    plan_fingerprint,
)
from admin.install_context import detect_install_context
from admin.mqtt_discovery import MqttBrokerDiscovery, MqttBrokerStore
from tests.test_admin_server import _control_export_manager, _request, _serve

pytestmark = [
    pytest.mark.admin,
    pytest.mark.authority,
    pytest.mark.setup,
    pytest.mark.integration,
    pytest.mark.simulation,
]

SERIAL = "EOD1AAA111"


@pytest.fixture(autouse=True)
def _isolate(isolated_install_root):
    return isolated_install_root


class _Devices:
    """A mutable mDNS provider stand-in: discovery state can change mid-test."""

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


def _inverter(serial=SERIAL, ip="10.0.0.11"):
    return {
        "id": "zendure_local_http:" + serial,
        "role_suggestion": "inverter",
        "device_type": "solarflow",
        "api_family": "solarflow",
        "ip": ip,
        "port": 8080,
        "serial_number": serial,
        "verified": True,
        "usable_for_config": True,
    }


def _write_live(payload='{"live": "A"}\n'):
    path = Path(detect_install_context().config_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(payload, encoding="utf-8")
    return path


def _draft():
    return {
        "devices": [
            {
                "role": "inverter",
                "enabled": True,
                "config_name": "WR1",
                "display_name": "Balcony inverter",
                "ip": "10.0.0.11",
                "serial_number": SERIAL,
            }
        ],
        "supported_grid_meter_count": 0,
    }


def _start_workflow(base):
    status, _, payload = _request(
        f"{base}/api/admin/start-path",
        method="POST",
        body={"choice": "setup_new", "confirm": True},
    )
    assert status == 200, payload
    return payload["setup_workflow_id"]


def _device_plan(base, state=None, **extra):
    # A plan authorizes the draft it was computed over, so the default state is
    # the default draft these tests go on to review.
    if state is None:
        state = {"draft_items": _draft()["devices"]}
    status, _, payload = _request(
        f"{base}/api/setup/device-plan",
        method="POST",
        body=dict({"state": state}, **extra),
    )
    assert status == 200, payload
    return payload


def _preview(base, workflow_id, device_plan_id, body=None):
    request = dict(body or _draft())
    request["setup_workflow_id"] = workflow_id
    if device_plan_id is not None:
        request["device_plan_id"] = device_plan_id
    return _request(f"{base}/api/setup/config-preview", method="POST", body=request)


def _apply(base, workflow_id, preview_id, device_plan_id, body=None):
    request = dict(body or _draft())
    request["setup_workflow_id"] = workflow_id
    request["config_preview_id"] = preview_id
    if device_plan_id is not None:
        request["device_plan_id"] = device_plan_id
    return _request(f"{base}/api/setup/config/apply", method="POST", body=request)


# --- preview binding ---------------------------------------------------------
def test_preview_refuses_to_issue_authority_without_a_device_plan(tmp_path):
    srv, base = _serve(
        mdns_provider=_Devices([_inverter()]),
        release_manager=_control_export_manager(tmp_path),
    )
    _write_live()
    try:
        workflow_id = _start_workflow(base)
        status, _, payload = _preview(base, workflow_id, None)

        assert status == 409, payload
        assert payload["error"] == "device_plan_required"
    finally:
        srv.shutdown()
        srv.server_close()


def test_preview_refuses_a_forged_device_plan_id(tmp_path):
    srv, base = _serve(
        mdns_provider=_Devices([_inverter()]),
        release_manager=_control_export_manager(tmp_path),
    )
    _write_live()
    try:
        workflow_id = _start_workflow(base)
        status, _, payload = _preview(base, workflow_id, "plan:v1:forged")

        assert status == 409, payload
        assert payload["error"] == "stale_device_plan"
    finally:
        srv.shutdown()
        srv.server_close()


def test_preview_refuses_a_plan_from_an_older_discovery_generation(tmp_path):
    provider = _Devices([_inverter()])
    srv, base = _serve(
        mdns_provider=provider, release_manager=_control_export_manager(tmp_path)
    )
    _write_live()
    try:
        workflow_id = _start_workflow(base)
        plan_id = _device_plan(base)["plan_id"]

        provider.devices_list.append(_inverter(serial="EOD1BBB222", ip="10.0.0.12"))

        status, _, payload = _preview(base, workflow_id, plan_id)
        assert status == 409, payload
        assert payload["error"] == "stale_device_plan"
    finally:
        srv.shutdown()
        srv.server_close()


def _scalar_local_mqtt_discovery():
    """A local broker offering the same inverter without a proven write route."""

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


def test_preview_refuses_a_plan_with_an_unconfirmed_switch(tmp_path):
    """A plan that still asks a question is not a basis for writing config."""

    srv, base = _serve(
        mdns_provider=_Devices([_inverter()]),
        mqtt_discovery=_scalar_local_mqtt_discovery(),
        release_manager=_control_export_manager(tmp_path),
    )
    _write_live()
    try:
        workflow_id = _start_workflow(base)
        status, _, _ = _request(
            f"{base}/api/discovery/preparation",
            method="POST",
            body={"discovery_priority": ["local_mqtt", "zendure_mqtt", "local_api"]},
        )
        assert status == 200
        plan = _device_plan(
            base,
            {
                "draft_items": [
                    {
                        "draft_item_id": "item-1",
                        "role": "inverter",
                        "serial_number": SERIAL,
                        "ip": "10.0.0.11",
                        "port": 8080,
                        "auto_added": True,
                    }
                ]
            },
        )
        assert plan["confirmation_required"] is True, plan["groups"]
        # The capability-losing switch is proposed, never executed.
        assert plan["operations"]["drop_draft_items"] == []

        status, _, payload = _preview(base, workflow_id, plan["plan_id"])
        assert status == 409, payload
        assert payload["error"] == "device_plan_confirmation_required"
    finally:
        srv.shutdown()
        srv.server_close()


def test_a_current_device_plan_issues_preview_authority(tmp_path):
    srv, base = _serve(
        mdns_provider=_Devices([_inverter()]),
        release_manager=_control_export_manager(tmp_path),
    )
    _write_live()
    try:
        workflow_id = _start_workflow(base)
        plan_id = _device_plan(base)["plan_id"]

        status, _, payload = _preview(base, workflow_id, plan_id)
        assert status == 200, payload
        assert payload["config_preview_id"]
        assert payload["device_plan_id"] == plan_id
    finally:
        srv.shutdown()
        srv.server_close()


def test_the_device_plan_is_part_of_the_exact_preview_fingerprint(tmp_path):
    """A preview issued under plan A must not authorize an apply under plan B."""

    provider = _Devices([_inverter()])
    srv, base = _serve(
        mdns_provider=provider, release_manager=_control_export_manager(tmp_path)
    )
    live = _write_live()
    before = live.read_bytes()
    try:
        workflow_id = _start_workflow(base)
        plan_a = _device_plan(base)["plan_id"]
        status, _, preview = _preview(base, workflow_id, plan_a)
        assert status == 200, preview
        preview_id = preview["config_preview_id"]

        provider.devices_list.append(_inverter(serial="EOD1BBB222", ip="10.0.0.12"))
        plan_b = _device_plan(base)["plan_id"]
        assert plan_b != plan_a

        status, _, payload = _apply(base, workflow_id, preview_id, plan_b)
        assert status == 409, payload
        assert payload["error"] in ("setup_preview_mismatch", "stale_device_plan")
        assert live.read_bytes() == before
    finally:
        srv.shutdown()
        srv.server_close()


# --- apply binding -----------------------------------------------------------
def test_apply_refuses_a_device_plan_that_went_stale_after_the_preview(tmp_path):
    provider = _Devices([_inverter()])
    srv, base = _serve(
        mdns_provider=provider, release_manager=_control_export_manager(tmp_path)
    )
    live = _write_live()
    before = live.read_bytes()
    try:
        workflow_id = _start_workflow(base)
        plan_id = _device_plan(base)["plan_id"]
        status, _, preview = _preview(base, workflow_id, plan_id)
        assert status == 200, preview

        # Discovery moves on between the review and the apply.
        provider.devices_list.append(_inverter(serial="EOD1BBB222", ip="10.0.0.12"))

        status, _, payload = _apply(
            base, workflow_id, preview["config_preview_id"], plan_id
        )
        assert status == 409, payload
        assert payload["error"] == "stale_device_plan"
        assert live.read_bytes() == before
    finally:
        srv.shutdown()
        srv.server_close()


def test_apply_refuses_a_missing_device_plan(tmp_path):
    srv, base = _serve(
        mdns_provider=_Devices([_inverter()]),
        release_manager=_control_export_manager(tmp_path),
    )
    live = _write_live()
    before = live.read_bytes()
    try:
        workflow_id = _start_workflow(base)
        plan_id = _device_plan(base)["plan_id"]
        status, _, preview = _preview(base, workflow_id, plan_id)
        assert status == 200, preview

        status, _, payload = _apply(
            base, workflow_id, preview["config_preview_id"], None
        )
        assert status == 409, payload
        assert payload["error"] == "device_plan_required"
        assert live.read_bytes() == before
    finally:
        srv.shutdown()
        srv.server_close()


# --- the registry the preview check reads ------------------------------------
def _contract(**overrides):
    contract = {
        "workflow_id": "wf-1",
        "workflow_revision": "sha256:" + "1" * 64,
        "draft_revision": "plan:v1:state",
        "candidate_authority_fingerprint": "gen",
        "confirmation_fingerprint": "plan:v1:settled",
        "decision_fingerprint": "plan:v1:decisions",
        "executable_operations_fingerprint": "plan:v1:operations",
        "expected_draft_fingerprint": "plan:v1:draft",
    }
    contract.update(overrides)
    return contract


def test_the_registry_forgets_the_oldest_plans_first():
    """Bounded on purpose: a forgotten plan makes the browser re-plan."""

    registry = DevicePlanRegistry(limit=2)
    for index in range(3):
        registry.record(f"plan:v1:{index}", **_contract())

    assert registry.get("plan:v1:0") is None
    assert registry.get("plan:v1:2")["candidate_authority_fingerprint"] == "gen"


def test_the_registry_never_answers_for_a_plan_it_did_not_record():
    registry = DevicePlanRegistry()
    assert registry.get("plan:v1:forged") is None
    assert registry.get("") is None
    assert registry.record("", **_contract()) is None


def test_the_registry_refuses_an_incomplete_authority_contract():
    """A plan recorded without one of its facts would validate against nothing."""

    registry = DevicePlanRegistry()
    for missing in CONTRACT_FIELDS:
        contract = _contract()
        contract.pop(missing)
        with pytest.raises(TypeError):
            registry.record("plan:v1:a", **contract)
    with pytest.raises(TypeError):
        registry.record("plan:v1:a", **_contract(), confirmation_required=False)


def test_re_recording_a_plan_keeps_it_current():
    registry = DevicePlanRegistry(limit=2)
    registry.record("plan:v1:a", **_contract(candidate_authority_fingerprint="one"))
    registry.record("plan:v1:b", **_contract(candidate_authority_fingerprint="one"))
    registry.record("plan:v1:a", **_contract(candidate_authority_fingerprint="two"))
    registry.record("plan:v1:c", **_contract(candidate_authority_fingerprint="two"))

    assert registry.get("plan:v1:b") is None
    entry = registry.get("plan:v1:a")
    assert entry == dict(
        _contract(candidate_authority_fingerprint="two"),
        plan_id="plan:v1:a",
        plan_fingerprint=plan_fingerprint(entry),
        mutation_authority_fingerprint=mutation_authority_fingerprint(entry),
    )


def test_a_recorded_contract_reproduces_its_own_digests():
    """Every part is bound: change one and the stored authority no longer holds."""

    registry = DevicePlanRegistry()
    entry = registry.record("plan:v1:a", **_contract())
    assert entry["plan_fingerprint"] == plan_fingerprint(entry)
    assert entry["mutation_authority_fingerprint"] == mutation_authority_fingerprint(
        entry
    )
    for field in CONTRACT_FIELDS:
        tampered = dict(entry, **{field: "moved"})
        assert (
            tampered["plan_fingerprint"] != plan_fingerprint(tampered)
            or tampered["mutation_authority_fingerprint"]
            != mutation_authority_fingerprint(tampered)
        ), field


def test_a_workflow_that_ends_takes_its_plans_with_it():
    """Completion, abandonment and replacement all spend the run's plans."""

    registry = DevicePlanRegistry()
    for plan_id, workflow in (("plan:v1:a", "wf-1"), ("plan:v1:b", "wf-1"), ("plan:v1:c", "wf-2")):
        registry.record(plan_id, **_contract(workflow_id=workflow))

    assert registry.forget_workflow("wf-1") == 2
    assert registry.get("plan:v1:a") is None
    assert registry.get("plan:v1:b") is None
    # Another run's plans are untouched, and forgetting twice is harmless.
    assert registry.get("plan:v1:c")["workflow_id"] == "wf-2"
    assert registry.forget_workflow("wf-1") == 0
    assert registry.forget_workflow("") == 0


def test_the_full_chain_applies(tmp_path):
    srv, base = _serve(
        mdns_provider=_Devices([_inverter()]),
        release_manager=_control_export_manager(tmp_path),
    )
    _write_live()
    try:
        workflow_id = _start_workflow(base)
        plan_id = _device_plan(base)["plan_id"]
        status, _, preview = _preview(base, workflow_id, plan_id)
        assert status == 200, preview

        status, _, payload = _apply(
            base, workflow_id, preview["config_preview_id"], plan_id
        )
        assert status == 200, payload
        assert payload["ok"] is True
    finally:
        srv.shutdown()
        srv.server_close()
