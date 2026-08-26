# SPDX-License-Identifier: AGPL-3.0-or-later
"""The one schema record that is not written from an image.

Every other schema number is stamped into a slot and then compared against a
constant compiled into that same slot, so it can only ever agree with itself.
The layout descriptor looks like it escapes that — it lives on ``/persistent`` —
but upstream re-seeds every shared path from the booting slot before the binds
activate, so the number the manager reads is the number its own image shipped.
That is why a schema-2 image can be installed over schema-3 state today and pass
every gate: the comparison was never between the code and the state.

This record sits outside the shared paths, so nothing re-seeds it, and it names
every format independently — because the shared-path count is not the only thing
a step back can cross.
"""

import ast
import json
from pathlib import Path

import pytest

from appliance import persistent_state

ROOT = Path(__file__).resolve().parents[1]

pytestmark = [pytest.mark.unit, pytest.mark.simulation, pytest.mark.appliance]

AXES = persistent_state.implemented_schemas()
NEWER = {name: value + 1 for name, value in AXES.items()}
OLDER = {name: max(1, value - 1) for name, value in AXES.items()}


def reconcile(mountpoint, implemented=None, **kwargs):
    return persistent_state.reconcile(str(mountpoint), implemented=implemented, **kwargs)


def test_a_partition_that_carries_no_record_is_adopted_at_the_running_schemas(tmp_path):
    verdict, stamp = reconcile(tmp_path)

    assert verdict.outcome == persistent_state.STATE_ADOPTED
    assert verdict.compatible
    assert stamp.schemas == AXES


def test_a_record_that_agrees_is_left_exactly_as_it_was(tmp_path):
    reconcile(tmp_path, written_at="2026-08-24T00:00:00+00:00")
    before = persistent_state.stamp_path(tmp_path).read_bytes()

    verdict, _ = reconcile(tmp_path, written_at="2026-08-25T00:00:00+00:00")

    assert verdict.outcome == persistent_state.STATE_MATCHED
    assert persistent_state.stamp_path(tmp_path).read_bytes() == before


def test_a_newer_manager_claims_the_partition_it_can_read(tmp_path):
    reconcile(tmp_path, OLDER)

    verdict, stamp = reconcile(tmp_path)

    assert verdict.outcome == persistent_state.STATE_RAISED
    assert verdict.compatible
    assert stamp.schemas == AXES


def test_an_older_manager_is_behind_the_state_and_says_so(tmp_path):
    """The case the descriptor could never report, because it travelled along."""

    reconcile(tmp_path, NEWER)

    verdict, stamp = reconcile(tmp_path)

    assert verdict.outcome == persistent_state.STATE_BEHIND
    assert not verdict.compatible
    assert set(verdict.behind) == set(AXES)
    assert stamp.schemas == NEWER, "an older manager must not restamp"


def test_one_axis_alone_is_enough_to_be_behind(tmp_path):
    """A step back need not move the shared-path count to be a step back."""

    recorded = dict(AXES)
    recorded["ab_state"] += 1
    reconcile(tmp_path, recorded)

    verdict, _ = reconcile(tmp_path)

    assert verdict.outcome == persistent_state.STATE_BEHIND
    assert verdict.behind == ("ab_state",)


def test_being_behind_survives_repeated_boots_of_the_older_manager(tmp_path):
    reconcile(tmp_path, NEWER)

    for _ in range(3):
        assert reconcile(tmp_path)[0].outcome == persistent_state.STATE_BEHIND

    assert persistent_state.read_stamp(str(tmp_path)).schemas == NEWER


def test_an_axis_this_manager_never_heard_of_is_carried_through(tmp_path):
    """A temporary step back must not erase what a newer manager recorded."""

    recorded = {**AXES, "something_later": 7}
    persistent_state.write_stamp(str(tmp_path), schemas=recorded)

    reconcile(tmp_path, OLDER)

    assert persistent_state.read_stamp(str(tmp_path)).schemas["something_later"] == 7


def test_a_record_written_by_a_newer_envelope_is_not_overwritten(tmp_path):
    """The record must not reproduce the bug it exists to prevent."""

    target = persistent_state.stamp_path(tmp_path)
    target.parent.mkdir(parents=True)
    target.write_text(
        json.dumps({"stamp_schema_version": 99, "schemas": AXES}),
        encoding="utf-8",
    )

    verdict, _ = reconcile(tmp_path)

    assert verdict.outcome == persistent_state.STATE_UNREADABLE
    assert json.loads(target.read_text(encoding="utf-8"))["stamp_schema_version"] == 99


def test_a_corrupt_record_is_refused_rather_than_replaced(tmp_path):
    target = persistent_state.stamp_path(tmp_path)
    target.parent.mkdir(parents=True)
    target.write_text("{not json", encoding="utf-8")

    verdict, _ = reconcile(tmp_path)

    assert verdict.outcome == persistent_state.STATE_UNREADABLE
    assert target.read_text(encoding="utf-8") == "{not json"


@pytest.mark.parametrize("value", [0, -1, "3", 3.0, True, None])
def test_a_schema_number_that_is_not_one_is_not_believed(tmp_path, value):
    target = persistent_state.stamp_path(tmp_path)
    target.parent.mkdir(parents=True)
    target.write_text(
        json.dumps({"stamp_schema_version": 1, "schemas": {"ab_state": value}}),
        encoding="utf-8",
    )

    assert persistent_state.read_stamp(str(tmp_path)).unreadable


def test_the_record_names_what_wrote_it(tmp_path):
    _, stamp = reconcile(
        tmp_path,
        written_by={"appliance_version": "0.1.0", "build_id": "20260824120000"},
        written_at="2026-08-24T12:00:00+00:00",
    )

    assert stamp.written_by["build_id"] == "20260824120000"
    assert stamp.written_at == "2026-08-24T12:00:00+00:00"


def test_a_reader_may_ask_without_claiming_anything(tmp_path):
    verdict, stamp = reconcile(tmp_path, write=False)

    assert verdict.outcome == persistent_state.STATE_ADOPTED
    assert not stamp.present
    assert not persistent_state.stamp_path(tmp_path).exists()


def test_the_record_lives_where_nothing_re_seeds_it():
    """Placement is the whole mechanism.

    Under /persistent/shared it would be rewritten from the booting slot before
    the binds come up; under /persistent/slots it would be per-slot and would
    not survive a switch. Either would make it as tautological as the descriptor
    it exists to replace.
    """

    path = str(persistent_state.stamp_path("/persistent"))

    assert path.startswith("/persistent/")
    for owned in ("/persistent/shared", "/persistent/slots", "/persistent/common"):
        assert owned not in path


def test_a_partition_that_cannot_be_written_reports_it_rather_than_pretending(tmp_path):
    unwritable = tmp_path / "ro"
    unwritable.mkdir(mode=0o500)

    with pytest.raises(persistent_state.PersistentStateError) as caught:
        persistent_state.write_stamp(str(unwritable), schemas=AXES)

    assert caught.value.code == "persistent_state_not_writable"


def test_every_schema_constant_the_appliance_keeps_on_a_shared_path_has_an_axis():
    """A schema that moves without an axis moving with it is a silent gap.

    Both sets below are load-bearing. A constant in neither is a new format
    nobody decided about, and deciding is the point: a step back that crosses
    an unrecorded format is exactly the failure this record exists to refuse.
    """

    on_a_shared_path = {
        "ab_persistence.PERSISTENT_SCHEMA_VERSION",
        "ab_layout.LAYOUT_SCHEMA_VERSION",
        "ab_state.AB_STATE_SCHEMA_VERSION",
        "ab_bootstrap.RECORD_VERSION",
        "backup_ownership.RECORD_SCHEMA_VERSION",
        "backup_ownership.ACCOUNT_ORIGIN_SCHEMA_VERSION",
        "backup_ownership.ACL_MANIFEST_SCHEMA_VERSION",
        "backup_ownership.HOME_MARKER_SCHEMA_VERSION",
        "operation_schema.OPERATION_SCHEMA_VERSION",
        "operation_schema.AUTHORITY_SCHEMA_VERSION",
        "operation_schema.RECOVERY_SCHEMA_VERSION",
        "os_update.CONFIRMED_AUTHORITY_SCHEMA_VERSION",
        "manager_retention.RECORD_SCHEMA_VERSION",
    }
    # Not appliance state: these version a release's evidence, or this record's
    # own envelope. None of them is ever read back off the persistent partition
    # by a manager that did not write it, so none can be crossed by a downgrade.
    not_appliance_state = {
        "persistent_state.STAMP_SCHEMA_VERSION",
        "backup_ownership.LEGACY_RECORD_SCHEMA_VERSION",
        "build_authority.SCHEMA_VERSION",
        "build_authority.ENVIRONMENT_SCHEMA_VERSION",
        "release_attestation.SCHEMA_VERSION",
        "release_inputs.SCHEMA_VERSION",
        "runtime_gates.SCHEMA_VERSION",
    }

    assert len(AXES) == len(on_a_shared_path)

    found = set()
    for path in sorted((ROOT / "appliance").glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in tree.body:
            if not isinstance(node, ast.Assign):
                continue
            for target in node.targets:
                name = getattr(target, "id", "")
                if name.endswith("SCHEMA_VERSION") or name == "RECORD_VERSION":
                    found.add(f"{path.stem}.{name}")

    assert found - on_a_shared_path - not_appliance_state == set(), "a schema nobody classified"
    assert on_a_shared_path | not_appliance_state == found


# --- what actually runs on the appliance -----------------------------------


class FakeReport:
    def __init__(self, mountpoint):
        self.mountpoint = str(mountpoint)


class FakeLayout:
    def __init__(self, build_id=""):
        self.os_build = {"build_id": build_id} if build_id else {}


def test_the_boot_time_reconciliation_records_the_running_schemas(tmp_path):
    from appliance import cli

    payload = cli._reconcile_state_schema(FakeReport(tmp_path), FakeLayout("20260824120000"))

    assert payload["outcome"] == persistent_state.STATE_ADOPTED
    assert payload["compatible"]
    assert payload["stamp"]["schemas"] == AXES
    assert payload["stamp"]["written_by"]["build_id"] == "20260824120000"


def test_the_boot_time_reconciliation_reports_being_behind_without_refusing(tmp_path):
    """A rolled-back appliance has to stay reachable to be rolled forward.

    Every unit that can write appliance state Requires= the persistence unit, so
    a refusal here would leave no agent and no web UI — and no way off the slot
    being complained about except physical access.
    """

    from appliance import cli

    persistent_state.write_stamp(str(tmp_path), schemas=NEWER)

    payload = cli._reconcile_state_schema(FakeReport(tmp_path), FakeLayout())

    assert payload["outcome"] == persistent_state.STATE_BEHIND
    assert not payload["compatible"]
    assert persistent_state.read_stamp(str(tmp_path)).schemas == NEWER


def test_a_partition_that_cannot_be_stamped_does_not_stop_the_boot(tmp_path):
    from appliance import cli

    unwritable = tmp_path / "ro"
    unwritable.mkdir(mode=0o500)

    payload = cli._reconcile_state_schema(FakeReport(unwritable), FakeLayout())

    assert payload["outcome"] == "persistent_state_not_writable"
    assert payload["compatible"], "an unwritable record is a problem, not a reason to refuse boot"
