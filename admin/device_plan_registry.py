# SPDX-License-Identifier: AGPL-3.0-or-later
"""The Setup device plans this Admin process actually issued.

``plan_id`` is a keyed token, so a browser cannot invent one — but an
unforgeable id still only proves *some* plan was issued once. Config Preview
needs two further facts before it turns a plan into mutation authority: that
this process issued it, and that the world it was planned in has not moved on.

Both live here. The registry keeps the candidate generation each issued plan was
computed under, so Preview and Apply can recompute the generation from current
discovery state and compare. It also records whether the plan still had an
unanswered confirmation, because a plan that is still asking a question is not a
basis for writing config.

Transient on purpose, exactly like the Setup lifecycle coordinator: a restart
holds no plans, so a browser simply re-plans. Losing an entry fails closed.
"""

import threading

DEFAULT_LIMIT = 64


class DevicePlanRegistry:
    """Bounded, thread-safe record of issued device plans, newest last."""

    def __init__(self, limit=DEFAULT_LIMIT):
        self._limit = max(1, int(limit))
        self._lock = threading.Lock()
        self._plans = {}

    def record(self, plan_id, *, generation, confirmation_required):
        if not isinstance(plan_id, str) or not plan_id:
            return None
        entry = {
            "plan_id": plan_id,
            "generation": generation,
            "confirmation_required": bool(confirmation_required),
        }
        with self._lock:
            self._plans.pop(plan_id, None)
            self._plans[plan_id] = entry
            while len(self._plans) > self._limit:
                self._plans.pop(next(iter(self._plans)))
        return dict(entry)

    def get(self, plan_id):
        if not isinstance(plan_id, str) or not plan_id:
            return None
        with self._lock:
            entry = self._plans.get(plan_id)
        return dict(entry) if entry is not None else None


__all__ = ["DevicePlanRegistry"]
