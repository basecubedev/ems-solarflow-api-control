# SPDX-License-Identifier: AGPL-3.0-or-later
"""The A/B privilege boundary: what a browser may say, and what it may not.

The whole point of the typed operation allowlist is that a request cannot carry
a device path, a partition number, a URL, a signing key or a reboot string. This
module states that as tests rather than as a comment, for every A/B operation
and for the web routes in front of them.
"""

import inspect
import json
from pathlib import Path

import pytest

from appliance import agent, audit, os_fetch, os_update, protocol, validation
from appliance.protocol import OPERATIONS, ProtocolError, ValidationContext, validate_request
from appliance.validation import ValidationError

pytestmark = [pytest.mark.contract, pytest.mark.simulation]

ROOT = Path(__file__).resolve().parents[1]

AB_OPERATIONS = ("ab.status", "ab.plan_update", "ab.plan_rollback", "ab.acknowledge")


class Context(ValidationContext):
    def __init__(self):
        super().__init__(
            type(
                "Config",
                (),
                {
                    "ssh_key_accounts": ("ems-backup",),
                    "managed_containers": (),
                    "images": type("Images", (), {"allow_prerelease": False})(),
                },
            )()
        )


# --- the operation allowlist -------------------------------------------------


def test_every_ab_operation_is_on_the_allowlist():
    for name in AB_OPERATIONS:
        assert name in OPERATIONS, name


def test_only_the_release_id_and_a_repair_flag_may_be_sent():
    fields = {field.name for field in OPERATIONS["ab.plan_update"].fields}

    assert fields == {"release_id", "repair"}


def test_planning_a_rollback_takes_no_field_at_all():
    assert OPERATIONS["ab.plan_rollback"].fields == ()


@pytest.mark.parametrize(
    "field",
    [
        "device",
        "partition",
        "partuuid",
        "path",
        "url",
        "keyring",
        "checksum",
        "digest",
        "boot_partition",
        "root_partition",
        "reboot_argument",
        "dd_args",
        "mount_flags",
    ],
)
def test_a_request_may_never_carry_a_physical_identity(field):
    payload = {
        "operation": "ab.plan_update",
        "release_id": "ems-solarflow-appliance-1.5.0-arm64-ab",
        field: "/dev/mmcblk0p3",
    }

    with pytest.raises(ProtocolError) as caught:
        validate_request(payload, Context())

    assert caught.value.code == "unknown_field"


@pytest.mark.parametrize(
    "value",
    [
        "/dev/mmcblk0p3",
        "../../etc/passwd",
        "https://example.invalid/image.tar.zst",
        "release id",
        "",
        "a" * 200,
        "release;reboot",
    ],
)
def test_a_release_id_that_is_not_an_identifier_is_refused(value):
    with pytest.raises(ValidationError) as caught:
        validation.validate_os_release_id(value)

    assert caught.value.code == "invalid_release_id"


def test_a_valid_release_id_passes_the_protocol():
    spec, args = validate_request(
        {
            "operation": "ab.plan_update",
            "release_id": "ems-solarflow-appliance-1.5.0-arm64-ab",
        },
        Context(),
    )

    assert spec.name == "ab.plan_update"
    assert args["release_id"] == "ems-solarflow-appliance-1.5.0-arm64-ab"
    assert args["repair"] is False


def test_the_status_operation_is_read_only():
    assert OPERATIONS["ab.status"].mutating is False


def test_both_plans_take_the_mutation_lock():
    assert OPERATIONS["ab.plan_update"].takes_lock is True
    assert OPERATIONS["ab.plan_rollback"].takes_lock is True


def test_acknowledging_is_bookkeeping_and_never_blocks_a_host_mutation():
    assert OPERATIONS["ab.acknowledge"].takes_lock is False


# --- execution goes through the generic confirmation path --------------------


def test_the_ab_plans_map_onto_operation_types():
    assert agent.PLAN_TYPES["ab.plan_update"] == os_update.TYPE_OS_UPDATE
    assert agent.PLAN_TYPES["ab.plan_rollback"] == os_update.TYPE_OS_ROLLBACK
    assert agent.PLAN_TYPES["ab.plan_fetch"] == os_fetch.TYPE_OS_FETCH


# Read-only A/B endpoints. Everything else under ab.* must be a plan, so that
# execution can only happen through operations.execute with its confirmation
# token. The list is spelled out rather than pattern-matched: a new endpoint
# should have to be named here by somebody who thought about which it is.
AB_READ_ONLY = ("ab.status", "ab.sources")
AB_NON_EXECUTING = ("ab.acknowledge",)


def test_there_is_no_unaudited_ab_execution_endpoint():
    """Execution runs through operations.execute, with its confirmation token."""

    for name in OPERATIONS:
        if not name.startswith("ab."):
            continue
        if name in AB_READ_ONLY + AB_NON_EXECUTING:
            assert not OPERATIONS[name].mutating or name in AB_NON_EXECUTING, name
            continue
        assert name.startswith("ab.plan_"), name
        assert name in agent.PLAN_TYPES, name


def test_every_ab_read_only_endpoint_really_is_read_only():
    for name in AB_READ_ONLY:
        assert name in OPERATIONS, name
        assert OPERATIONS[name].mutating is False, name
        assert name not in agent.PLAN_TYPES, name


# --- audit -------------------------------------------------------------------


@pytest.mark.parametrize(
    "action",
    [
        "ab.update.plan",
        "ab.update.confirm",
        "ab.update.stage_started",
        "ab.update.stage_verified",
        "ab.tryboot_requested",
        "ab.trial_boot_started",
        "ab.trial_health_failed",
        "ab.commit",
        "ab.fallback_observed",
        "ab.rollback.plan",
        "ab.rollback.commit",
    ],
)
def test_every_ab_action_is_an_audited_action(action):
    assert action in audit.AUDITED_ACTIONS


def test_planning_an_ab_operation_is_audited():
    assert agent.AUDITED_PLANS[os_update.TYPE_OS_UPDATE] == "ab.update.plan"
    assert agent.AUDITED_PLANS[os_update.TYPE_OS_ROLLBACK] == "ab.rollback.plan"


def test_an_audit_entry_never_carries_key_material(tmp_path):
    log = audit.AuditLog(tmp_path / "audit.log")

    log.record(
        "ab.commit",
        target="slot B",
        detail={"build_id": "20260807-1", "signing_key": "-----BEGIN PRIVATE KEY-----"},
    )

    text = (tmp_path / "audit.log").read_text(encoding="utf-8")
    assert "BEGIN PRIVATE KEY" not in text
    assert "20260807-1" in text


# --- the web surface ---------------------------------------------------------


def web_source():
    return (ROOT / "appliance" / "web.py").read_text(encoding="utf-8")


def test_the_web_routes_forward_only_a_release_id():
    source = web_source()

    assert '"/api/ab/plan-update"' in source
    assert '"release_id": b.get("release_id")' in source
    assert '"/api/ab/plan-rollback": ("ab.plan_rollback", lambda _: {})' in source


def test_the_ab_execution_route_uses_the_shared_confirmation_path():
    source = web_source()

    assert '"/api/ab/execute",' in source
    marker = source.index('confirm_paths = (')
    assert source.index('"/api/ab/execute",') > marker


@pytest.mark.parametrize(
    "forbidden",
    ["boot_device", "root_device", "partuuid", "device", "keyring", "artifact_url"],
)
def test_no_web_route_forwards_a_physical_identity(forbidden):
    source = web_source()
    ab_section = source[source.index('"/api/ab/plan-update"') : source.index('"/api/ssh/enable"')]

    assert forbidden not in ab_section


# --- the frontend ------------------------------------------------------------


def app_source():
    return (ROOT / "appliance" / "static" / "app.js").read_text(encoding="utf-8")


def test_the_frontend_sends_only_a_release_id():
    source = app_source()

    assert 'body: { release_id: release.release_id }' in source
    assert "/dev/" not in source


def test_the_frontend_states_the_ab_mode_and_the_fallback_behaviour():
    source = app_source()

    assert "fail-safe A/B OS images" in source
    assert "staged into the " in source
    assert "inactive slot and tested before becoming active" in source
    assert "one-shot" in source


def test_a_single_slot_appliance_is_told_that_ab_needs_re_imaging():
    source = app_source()

    assert "single root filesystem" in source
    assert "A/B-capable appliance image" in source


def test_the_ab_page_reuses_the_control_stage_family():
    """No third card style: the same stage() and card() helpers as everywhere."""

    source = app_source()
    section = source[source.index("function renderAbUpdates") : source.index("function renderPackageUpdates")]

    # The stage numbers are computed, because the page grew a first stage that
    # only exists on an appliance with a release index. What matters to the
    # style guide is that it is the shared stage() helper doing the rendering,
    # not that the literal "1" appears here.
    assert "stages.push(stage(" in section
    assert 'class: "card-grid"' in section
    assert 'class: "stage-grid"' in section
    assert "control-stage-actions" in section


def test_the_update_page_picks_exactly_one_mode():
    source = app_source()
    section = source[source.index("function renderUpdates") : source.index("function renderAbUpdates")]

    assert "renderAbUpdates(main, ab)" in section
    assert "renderPackageUpdates(main, updates, ab)" in section
    assert section.count("return;") == 1


# --- the plan an operator confirms -------------------------------------------


def test_the_plan_states_every_field_an_operator_needs():
    fields = set(
        os_update.UpdatePlan(
            current_release="",
            current_build_id="",
            current_slot="",
            target_release="",
            target_build_id="",
            target_slot="",
            artifact_digest="",
            boot_image_bytes=0,
            rootfs_image_bytes=0,
            persistent_schema_version=1,
            appliance_manager_version="",
        ).to_dict()
    )

    for name in (
        "current_release",
        "current_slot",
        "target_release",
        "target_slot",
        "artifact_digest",
        "boot_image_bytes",
        "rootfs_image_bytes",
        "persistent_schema_version",
        "appliance_manager_version",
        "expects_reboot",
        "automatic_fallback",
        "risk",
        "blockers",
    ):
        assert name in fields, name


def test_the_write_authority_binds_the_whole_physical_target():
    fields = set(inspect.signature(os_update.WriteAuthority).parameters)

    for name in (
        "layout_id",
        "slot_schema_version",
        "persistent_schema_version",
        "device",
        "active_slot",
        "target_slot",
        "boot_device",
        "boot_partuuid",
        "root_device",
        "root_partuuid",
        "artifact_digest",
        "boot_digest",
        "rootfs_digest",
    ):
        assert name in fields, name


def test_the_protocol_kind_is_registered_once():
    assert protocol.KIND_OS_RELEASE_ID == "os_release_id"
    assert json.dumps(sorted(OPERATIONS))
