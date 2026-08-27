# SPDX-License-Identifier: AGPL-3.0-or-later
"""The zone the EMS runs its local-hour control windows in.

The host stays on a deterministic UTC rather than inheriting somebody else's
clock. The operator's choice is kept beside the rest of their configuration and
carried into the containers as `TZ`, which is what actually decides when a
charge window opens.
"""

from appliance.operations import STATE_SUCCEEDED
from appliance.paths import atomic_write
from appliance.validation import validate_timezone

TYPE_TIMEZONE = "system.timezone"


class TimezoneError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


class TimezoneService:
    def __init__(self, *, paths, config, operations):
        self.paths = paths
        self.config = config
        self.operations = operations

    def current(self):
        return str(getattr(self.config, "timezone", "UTC") or "UTC")

    def plan(self, operation, timezone):
        target = validate_timezone(timezone)
        current = self.current()
        if target == current:
            raise TimezoneError(
                "timezone_unchanged", "this is already the appliance timezone"
            )
        values = {"timezone": target, "previous_timezone": current}
        operation.requested_target.update(values)
        self.operations.update_target(operation.operation_id, values)
        return {
            "type": TYPE_TIMEZONE,
            "timezone": target,
            "previous_timezone": current,
            "warning": (
                "The EMS containers are restarted so they pick the new zone up. "
                "Control windows that name an hour move with it."
            ),
        }

    def execute(self, operation):
        target = operation.requested_target
        timezone = validate_timezone(target["timezone"])
        path = self.paths.timezone_file
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(path, timezone + "\n")
        payload = {
            "timezone": timezone,
            "previous_timezone": target.get("previous_timezone", ""),
            "applies_after": "the next deployment start",
        }
        self.operations.finish(operation.operation_id, STATE_SUCCEEDED, result=payload)
        return payload
