# SPDX-FileCopyrightText: 2026 basecubedev
# SPDX-License-Identifier: AGPL-3.0-or-later
"""One grid reading, and whether the control loop has used it already.

The control loop adds each grid reading to the total it already commands, so a
reading counted twice is a correction made twice. A meter that keeps its last
reading between two new ones -- a background reader of a stream or a broker, or
a reader of a smart meter slower than the loop -- hands the same reading to
several cycles, and only the first of them may regulate on it.
"""

from dataclasses import dataclass

_NEVER = object()


@dataclass(frozen=True)
class GridReading:
    """A grid power reading and the identity of the measurement behind it.

    ``sequence`` changes whenever the meter delivers a new measurement and
    stays the same while the meter repeats one. ``None`` says the client cannot
    tell its readings apart, which holds for a client that measures on every
    read: each such reading is a new one.
    """

    power_w: object
    sequence: object = None


def read_grid_meter(client):
    """This cycle's reading from a grid meter client."""

    get_reading = getattr(client, "get_reading", None)
    if callable(get_reading):
        return get_reading()
    return GridReading(client.get_power())


class ReadingTracker:
    """Tells a reading the control loop has not used yet from one it has."""

    def __init__(self):
        self._client = None
        self._sequence = _NEVER

    def is_new(self, client, reading):
        """Whether ``reading`` from ``client`` is one no earlier cycle saw."""

        if reading.sequence is None:
            self._client, self._sequence = None, _NEVER
            return True
        new = client is not self._client or reading.sequence != self._sequence
        self._client, self._sequence = client, reading.sequence
        return new
