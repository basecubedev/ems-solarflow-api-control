# SPDX-License-Identifier: AGPL-3.0-or-later
"""Stable default names for newly created EMS inverter entries."""

import re


_COMPACT_INVERTER_NAME = re.compile(r"^INV_([1-9][0-9]*)$", re.IGNORECASE)


def next_compact_inverter_name(existing_names, inverter_count=None):
    """Return the next case-insensitively unique ``INV_n`` alias."""

    names = [str(name or "").strip() for name in (existing_names or ())]
    used = {name.casefold() for name in names if name}
    highest = 0
    for name in names:
        match = _COMPACT_INVERTER_NAME.fullmatch(name)
        if match:
            highest = max(highest, int(match.group(1)))
    try:
        count = max(0, int(inverter_count))
    except (TypeError, ValueError):
        count = len(names)
    number = max(1, highest + 1, count + 1)
    while f"inv_{number}" in used:
        number += 1
    return f"INV_{number}"
