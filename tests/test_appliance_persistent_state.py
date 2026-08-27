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


# --- what actually runs on the appliance -----------------------------------


class FakeReport:
    def __init__(self, mountpoint):
        self.mountpoint = str(mountpoint)


class FakeLayout:
    def __init__(self, build_id=""):
        self.os_build = {"build_id": build_id} if build_id else {}




# --- formats this manager no longer writes ------------------------------------


def test_a_retired_axis_is_still_declared():
    """Dropping one makes the next package uninstallable on every appliance.

    ``state_schema_problems`` refuses any artifact that does not declare an axis
    the appliance's own record names, and it refuses it *before* anything could
    be installed to fix that. So an axis is retired by freezing it, never by
    removing it.
    """

    implemented = persistent_state.implemented_schemas()

    for axis, version in persistent_state.RETIRED_SCHEMAS.items():
        assert implemented[axis] == version, axis


def test_a_retired_axis_is_read_at_every_version():
    """Which is true: it is read at none."""

    floors = persistent_state.readable_floors()

    for axis in persistent_state.RETIRED_SCHEMAS:
        assert floors[axis] == 1, axis


def test_a_package_built_now_installs_on_an_appliance_that_recorded_the_old_axes():
    """The failure this rule exists to prevent, stated as a test.

    An appliance stamped by the last manager that still wrote these axes must
    accept a package built after they were retired -- otherwise the first
    post-removal release is refused everywhere, and refused before the release
    that would fix it could be installed.
    """

    from appliance import artifact_trust

    recorded = dict(persistent_state.implemented_schemas())

    class Release:
        state_implements = persistent_state.implemented_schemas()
        state_reads = persistent_state.readable_floors()

    assert artifact_trust.state_schema_problems(Release(), recorded=recorded) == []

    stamp = persistent_state.StateStamp(present=True, schemas=recorded)
    assert persistent_state.compare(stamp).outcome == persistent_state.STATE_MATCHED


def test_dropping_a_retired_axis_would_refuse_the_package():
    """The counter-case, so the test above cannot pass vacuously."""

    from appliance import artifact_trust

    recorded = dict(persistent_state.implemented_schemas())

    class Naive:
        state_implements = {
            axis: version
            for axis, version in persistent_state.implemented_schemas().items()
            if axis not in persistent_state.RETIRED_SCHEMAS
        }
        state_reads = dict(state_implements)

    problems = artifact_trust.state_schema_problems(Naive(), recorded=recorded)

    assert len(problems) == len(persistent_state.RETIRED_SCHEMAS)
    assert {problem["code"] for problem in problems} == {"artifact_state_schema_undeclared"}
