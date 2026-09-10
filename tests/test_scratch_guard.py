# SPDX-License-Identifier: AGPL-3.0-or-later
"""A full run must refuse to start rather than fill the host's temp filesystem.

This exists because the rule lived only in prose and was skipped: a broad run
filled a 12 GB tmpfs, pytest died with ``INTERNALERROR`` after half an hour, and
every later command on the machine failed with ENOSPC — including the ones that
would have diagnosed it. A rule a tool enforces cannot be skipped.
"""

import pytest

from tests.scratch_guard import (
    BROAD_SELECTION_ITEMS,
    OVERRIDE_ENV,
    REQUIRED_FREE_BYTES,
    scratch_shortfall,
)

pytestmark = [pytest.mark.unit, pytest.mark.simulation]

GB = 1024**3


def _shortfall(free_gb, items, environ=None):
    return scratch_shortfall(
        int(free_gb * GB), items, "/tmp", environ={} if environ is None else environ
    )


def test_a_broad_run_without_room_is_refused():
    message = _shortfall(2, BROAD_SELECTION_ITEMS)
    assert message is not None
    # The message has to carry the fix, not just the symptom.
    assert "TMPDIR" in message
    assert "2.0 GB free" in message


def test_a_broad_run_with_room_proceeds():
    assert _shortfall(REQUIRED_FREE_BYTES / GB, BROAD_SELECTION_ITEMS) is None


def test_a_targeted_run_is_never_refused():
    """A handful of tests needs no room worth naming."""

    assert _shortfall(0.1, BROAD_SELECTION_ITEMS - 1) is None


def test_the_override_is_honoured():
    assert _shortfall(0.1, BROAD_SELECTION_ITEMS, {OVERRIDE_ENV: "1"}) is None


def test_the_floor_is_the_measurement_that_caused_this():
    """A 12 GB tmpfs was not enough; the floor must be above it."""

    assert REQUIRED_FREE_BYTES > 12 * GB


def test_the_guard_judges_the_selection_that_will_actually_run():
    """Marker deselection happens inside pytest_collection_modifyitems.

    Counting there sees the whole suite, so `pytest -m "simulation and
    power_control"` — 512 tests — would be refused for the size of the 11k it
    was filtered out of. pytest_collection_finish sees the final selection.
    """

    import inspect

    from tests import conftest

    source = inspect.getsource(conftest)
    assert "scratch_shortfall" in source
    hook = source.split("def pytest_collection_finish", 1)[1].split("\ndef ", 1)[0]
    assert "scratch_shortfall" in hook
    assert "session.items" in hook
    assert "UsageError" in hook
    modify = source.split("def pytest_collection_modifyitems", 1)[1].split(
        "\ndef ", 1
    )[0]
    assert "scratch_shortfall" not in modify
