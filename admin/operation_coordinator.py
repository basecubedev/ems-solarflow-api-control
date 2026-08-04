# SPDX-License-Identifier: AGPL-3.0-or-later
"""Atomic worker-ownership and abandonment coordination per operation id.

TTL expiry proves a transition's forward path is closed, not that its mutating
worker stopped. Claim/release and abandon share one lock, so no worker can
register between "proven inactive" and the durable cancel, and a worker that
loses the race is refused instead of starting. The durable cancellation is a
callback; the coordinator never learns about the store, Docker or HTTP.
"""

import threading


class OperationWorkerActive(Exception):
    """A live worker owns the operation, so it must not be abandoned yet."""

    def __init__(self, operation_id):
        super().__init__(f"a worker is still active for operation {operation_id!r}")
        self.operation_id = operation_id


class OperationWorkerStatusUnavailable(Exception):
    """Worker liveness could not be evaluated, so abandonment fails closed."""

    def __init__(self, operation_id):
        super().__init__(
            f"worker liveness for operation {operation_id!r} could not be verified"
        )
        self.operation_id = operation_id


class OperationCoordinator:
    """Serialize worker ownership and abandonment for durable operations."""

    def __init__(self):
        self._lock = threading.Lock()
        self._active = {}
        self._abandoned = set()
        self._sequence = 0

    def claim(self, operation_id):
        """Register a live worker for ``operation_id``.

        Returns an opaque token to pass back to :meth:`release`, or ``None`` when
        abandonment has already won and no worker may start.
        """

        if not operation_id:
            return None
        with self._lock:
            if operation_id in self._abandoned:
                return None
            self._sequence += 1
            token = (operation_id, self._sequence)
            self._active.setdefault(operation_id, set()).add(token)
            return token

    def release(self, token):
        """Drop a single worker claim previously returned by :meth:`claim`."""

        if not token:
            return
        operation_id = token[0]
        with self._lock:
            tokens = self._active.get(operation_id)
            if tokens is None:
                return
            tokens.discard(token)
            if not tokens:
                del self._active[operation_id]

    def release_operation(self, operation_id):
        """Drop every worker claim for ``operation_id`` (idempotent).

        Used where a worker's terminal callback knows only its operation id, not
        its claim token; a repeated call after the claim is gone is a no-op.
        """

        if not operation_id:
            return
        with self._lock:
            self._active.pop(operation_id, None)

    def is_active(self, operation_id):
        """True only while a live worker holds a claim for ``operation_id``."""

        if not operation_id:
            return False
        with self._lock:
            return bool(self._active.get(operation_id))

    def abandon(self, operation_id, cancel):
        """Atomically require no live worker, block future claims, then cancel.

        Runs entirely under the coordinator lock, so ``cancel`` (and anything it
        reaches) must never call back into this coordinator. If ``cancel``
        raises, the abandonment marker is rolled back so a still-recoverable
        transition is not left permanently unclaimable.
        """

        with self._lock:
            try:
                active = bool(self._active.get(operation_id))
            except Exception as exc:  # pragma: no cover - defensive fail-closed
                raise OperationWorkerStatusUnavailable(operation_id) from exc
            if active:
                raise OperationWorkerActive(operation_id)
            self._abandoned.add(operation_id)
            try:
                return cancel()
            except BaseException:
                self._abandoned.discard(operation_id)
                raise
