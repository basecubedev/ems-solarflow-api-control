# SPDX-License-Identifier: AGPL-3.0-or-later
"""One run of a shared refresh at a time, joined by callers with the same inputs."""

import copy
import threading


class _Flight:
    def __init__(self, key):
        self.key = key
        self.done = threading.Event()
        self.result = None
        self.error = None


class SingleFlight:
    """Serializes the runs of one refresh whose resources cannot be shared.

    ``read()`` returns ``(key, inputs)``: what the run depends on, and what it
    runs with. It is read and compared in one step under the flight's lock. A
    caller whose key equals the key of the run in flight waits for that run and
    gets a copy of its result or its error; a caller with another key waits for
    it to end and then reads again, so no caller is answered from inputs other
    than its own. Nothing is kept once a run has ended.
    """

    def __init__(self):
        self._cond = threading.Condition()
        self._flight = None

    def run(self, read, work):
        with self._cond:
            while True:
                key, inputs = read()
                flight = self._flight
                if flight is None:
                    flight = self._flight = _Flight(key)
                    owner = True
                    break
                if flight.key == key:
                    owner = False
                    break
                self._cond.wait()
        if not owner:
            flight.done.wait()
            if flight.error is not None:
                raise flight.error
            return copy.deepcopy(flight.result)
        try:
            result = work(inputs)
            flight.result = copy.deepcopy(result)
            return result
        except BaseException as exc:
            flight.error = exc
            raise
        finally:
            with self._cond:
                self._flight = None
                self._cond.notify_all()
            flight.done.set()
