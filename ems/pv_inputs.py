# SPDX-FileCopyrightText: 2026 basecubedev
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Which PV inputs of a device count: the ones its tile shows and its history records.

A device reports every input it has, an empty one as 0 W, and nothing in its
report tells an empty input from a dark one. ``devices[].unused_pv_inputs``
names the empty ones by number. Only what is shown and recorded leaves them
out: the PV total stays the device's, and the control path reads the report as
it is.

Import-side-effect-free.
"""

from collections.abc import Mapping

PV_INPUT_COUNT = 4
UNUSED_PV_INPUTS_KEY = "unused_pv_inputs"


def is_pv_input_number(value):
    """True for an input number from 1 to ``PV_INPUT_COUNT``."""

    return isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= PV_INPUT_COUNT


def unused_pv_inputs(entry):
    """The input numbers a ``devices[]`` entry marks empty, leaving out any it cannot mean."""

    values = entry.get(UNUSED_PV_INPUTS_KEY) if isinstance(entry, Mapping) else None
    if not isinstance(values, (list, tuple)):
        return frozenset()
    return frozenset(value for value in values if is_pv_input_number(value))


def unused_pv_inputs_problem(entry):
    """Why an entry's ``unused_pv_inputs`` means less than it says, or ``None``."""

    if not isinstance(entry, Mapping) or entry.get(UNUSED_PV_INPUTS_KEY) is None:
        return None
    values = entry[UNUSED_PV_INPUTS_KEY]
    if isinstance(values, (list, tuple)) and all(is_pv_input_number(value) for value in values):
        return None
    return f"must list PV input numbers from 1 to {PV_INPUT_COUNT}"


def _reported(state):
    return getattr(state, "pv_inputs_reported", None) or (True,) * PV_INPUT_COUNT


def pv_inputs_kept(state, unused=frozenset()):
    """Which of ``solar1``..``solar4`` count: carried by the report and not marked empty."""

    return tuple(bool(carried) and index not in unused for index, carried in enumerate(_reported(state), start=1))


def pv_input_powers(state):
    """Each input's power as reported, ``None`` for one the report did not carry; a marked one included."""

    return [
        float(getattr(state, f"solar{index}", 0) or 0) if carried else None
        for index, carried in enumerate(_reported(state), start=1)
    ]
