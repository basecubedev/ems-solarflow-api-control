# SPDX-License-Identifier: AGPL-3.0-or-later
"""Exclusive ownership of one operation's Admin replacement dispatch.

Three different ownerships guard the Admin replacement, and they are not
interchangeable:

* **durable stage ownership** — B1's ``admin_update_pending`` →
  ``admin_reconnect_pending`` write (:mod:`admin.system_alignment`) decides that
  a replacement is expected at all, and survives an Admin restart;
* **transient dispatch ownership** — this coordinator, which decides which of
  several concurrent callers performs the one launcher invocation;
* **worker-side Admin-update ownership** — ``claim_admin_update()`` in the
  sidecar, the durable guard *inside* the replacement, which runs far too late
  to keep a second one from being started.

One caller at a time holds an operation's claim; every other caller blocks until
it is released and then learns whether the launch already happened. Blocking
rather than refusing is deliberate: a concurrent retry must answer the
authoritative transition, never "try again".

The claim records the outcome of the launcher call only: a failure raised
*before* it (an uncommitted or unprovable stage write) dispatched nothing, so it
publishes nothing and the next caller may still perform the single dispatch.

An operation's claim covers one *dispatch attempt*. An explicit retry is a new
attempt, because the outcome it must produce is a new launcher call — answering
it from the settled attempt it is recovering from would reopen the durable
transition and dispatch nothing. :meth:`ReplacementDispatchCoordinator.owned_retry`
therefore detaches a settled attempt and starts a fresh one. Detaching, not
clearing: the old attempt keeps its published outcome for the waiters that are
owed it, and disappears with its own last caller. Two simultaneous retries still
share one new attempt, because a settled attempt is detached once, under the
registry guard.

An entry lives exactly as long as it has live callers. That bounds this registry
to the attempts actually in flight, and it is why the claim answers only the
callers that overlap *in time*: one that read ``admin_update_pending`` and was
descheduled before entering finds no entry at all. Nothing here can cover it —
retaining every completed dispatch forever would, but at the price of an
unbounded registry that also has to forget a failure precisely enough to let an
explicit recovery through. Its owner re-reads the durable stage instead
(:meth:`admin.system_alignment.SystemAlignmentService._dispatch_authority`),
which is bounded, survives a restart and is the same authority the operator's
retry answers to.

Transient by design: claims live in this process only, shared by the HTTP and
HTTPS listeners through the one ``SystemAlignmentService`` in ``AdminRuntime``.
An Admin restart therefore holds no claims. A durable ``admin_reconnect_pending``
stage is still not proof that a replacement was dispatched, so no path relaunches
from it — which is why the process-crash window between that stage and the launch
stays a documented limitation rather than a guarantee.

See ``docs/technical/admin-workflow-state.md``.
"""

import threading
from contextlib import contextmanager


class _DispatchEntry:
    """One dispatch attempt's lock, live interest count and published outcome."""

    __slots__ = ("lock", "waiting", "result", "failure")

    def __init__(self):
        self.lock = threading.Lock()
        self.waiting = 0
        self.result = None
        self.failure = None

    @property
    def settled(self) -> bool:
        return self.result is not None or self.failure is not None


class ReplacementDispatchClaim:
    """The caller's exclusive right to dispatch one replacement attempt."""

    def __init__(self, entry):
        self._entry = entry

    @property
    def completed(self) -> bool:
        """True when a launcher call for this attempt already ran."""

        return self._entry.settled

    @property
    def result(self):
        """The dispatching caller's published result, or ``None``."""

        return self._entry.result

    @property
    def failure(self):
        """The dispatching caller's published ``(code, message)``, or ``None``."""

        return self._entry.failure

    def publish_result(self, result):
        self._entry.result = result

    def publish_failure(self, code, message):
        self._entry.failure = (code, message)


class ReplacementDispatchCoordinator:
    """Serialize the Admin replacement dispatch per durable operation id."""

    def __init__(self):
        self._guard = threading.Lock()
        self._entries = {}

    def owned(self, operation_id):
        """Block until this caller exclusively owns ``operation_id``'s attempt."""

        return self._owned(operation_id, new_attempt=False)

    def owned_retry(self, operation_id):
        """Own an attempt that no settled one may answer for.

        A settled attempt is detached rather than answered, so the retry
        dispatches instead of inheriting an outcome, and rather than cleared, so
        its waiters still receive it. Simultaneous retries share the one new
        attempt: the detach happens once, under the registry guard.
        """

        return self._owned(operation_id, new_attempt=True)

    @contextmanager
    def _owned(self, operation_id, *, new_attempt):
        entry = self._enter(operation_id, new_attempt=new_attempt)
        entry.lock.acquire()
        try:
            yield ReplacementDispatchClaim(entry)
        finally:
            entry.lock.release()
            self._leave(operation_id, entry)

    def _enter(self, operation_id, *, new_attempt=False):
        with self._guard:
            entry = self._entries.get(operation_id)
            if entry is None or (new_attempt and entry.settled):
                entry = self._entries[operation_id] = _DispatchEntry()
            entry.waiting += 1
            return entry

    def _leave(self, operation_id, entry):
        with self._guard:
            entry.waiting -= 1
            if entry.waiting <= 0 and self._entries.get(operation_id) is entry:
                del self._entries[operation_id]


__all__ = ["ReplacementDispatchClaim", "ReplacementDispatchCoordinator"]
