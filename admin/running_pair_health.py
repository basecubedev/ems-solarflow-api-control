# SPDX-License-Identifier: AGPL-3.0-or-later
"""The upgrade's EMS health check, taken for a running pair beside the read that asks.

The known-good record follows the running Admin/EMS pair only once its EMS
passes the check an upgrade requires before it records a pair:
``validate_system_health_result`` over ``EmsCliDiagnostics.run``. That check
runs several ``docker exec`` commands one after another, so the status read
that asks never waits for it. The first read for a pair starts one check in
the background and is answered ``None``; when that check passes, the read's
``on_pass`` runs in the check's thread, so the pair is recorded without a later
read. A verdict belongs to the pair it was started for and answers reads for
``VERDICT_SECONDS`` from its start; the first read after that starts the check
again. One check runs at a time.
"""

import threading
import time

from admin.system_health import validate_system_health_result

VERDICT_SECONDS = 120.0
THREAD_NAME = "running-pair-health"


def _start_in_background(work):
    threading.Thread(target=work, name=THREAD_NAME, daemon=True).start()


class RunningPairHealthCheck:
    """Runs ``diagnose`` for a running pair outside the caller, one run at a time."""

    def __init__(self, diagnose, *, verdict_seconds=VERDICT_SECONDS,
                 clock=time.monotonic, start=None):
        self._diagnose = diagnose
        self._verdict_seconds = float(verdict_seconds)
        self._clock = clock
        self._start = start or _start_in_background
        self._lock = threading.Lock()
        self._verdict = None
        self._running = False

    def verdict(self, pair, on_pass=None):
        """``True`` or ``False`` once ``pair`` was checked within the window, else ``None``.

        A ``None`` answer starts the check when none is running; ``on_pass`` is
        called once that check passed and its verdict is kept.
        """

        with self._lock:
            known = self._known(pair)
            if known is not None or self._running:
                return known
            self._running = True
            started = self._clock()
        try:
            self._start(lambda: self._check(pair, started, on_pass))
        except Exception:
            with self._lock:
                self._running = False
            return None
        with self._lock:
            return self._known(pair)

    def _known(self, pair):
        if self._verdict is None:
            return None
        checked, passed, started = self._verdict
        if checked != pair or self._clock() - started >= self._verdict_seconds:
            return None
        return passed

    def _check(self, pair, started, on_pass):
        passed = False
        try:
            passed = validate_system_health_result(self._diagnose()).success
        except Exception:
            passed = False
        finally:
            with self._lock:
                self._verdict = (pair, passed, started)
                self._running = False
        if passed and on_pass is not None:
            on_pass()
