# SPDX-License-Identifier: AGPL-3.0-or-later
"""The records that have to survive being read by the wrong version.

A/B state is written by one slot and read by another, and after a step back the
reader is the older of the two. What it does with a field it has never seen
decides whether the appliance keeps its way back or loses it.
"""

import pytest

from appliance.ab_state import SlotRecord

pytestmark = [pytest.mark.unit, pytest.mark.simulation, pytest.mark.appliance]


def test_a_record_written_by_a_newer_manager_is_still_readable():
    """Refusing an unfamiliar field would turn an addition into a lost slot.

    The known-good record is the appliance's way back from a bad update. A
    newer manager may record more than this one reads, and dropping the whole
    record over that would discard exactly what it exists to preserve.
    """

    record = SlotRecord.from_dict(
        {
            "slot": "A",
            "build_id": "20260824-1",
            "state_schemas": {"ab_state": 1},
            "something_a_later_version_added": {"nested": True},
        }
    )

    assert record.slot == "A"
    assert record.build_id == "20260824-1"
    assert record.state_schemas == {"ab_state": 1}


def test_a_record_that_names_nothing_this_version_knows_is_still_a_slot():
    record = SlotRecord.from_dict({"slot": "B", "unknown": 1})

    assert record.slot == "B"
    assert record.state_schemas == {}


def test_a_slot_recorded_before_the_schemas_were_kept_says_so_by_being_empty():
    """Unprovable and unsafe are different answers, and the planner tells them apart."""

    assert SlotRecord(slot="A").state_schemas == {}
