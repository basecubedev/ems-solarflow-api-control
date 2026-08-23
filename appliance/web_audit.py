# SPDX-License-Identifier: AGPL-3.0-or-later
"""Authentication auditing for the unprivileged web service.

The audit trail belongs to root and the agent is its only writer, so the web
service reports an authentication event instead of appending to the log. Two
rules govern the failure case: a missing agent must never lock an operator out
of the recovery UI, and the appliance must never claim an event reached the
authoritative trail when it did not.
"""

import threading
import time

from appliance.agent_client import AgentCallError, AgentUnavailableError
from appliance.audit import RESULT_SUCCESS

AGENT_OPERATION = "audit.record_web_event"

STATE_HEALTHY = "healthy"
STATE_DEGRADED = "degraded"

DEGRADED_MESSAGE = (
    "Authentication events could not be written to the audit log because the "
    "appliance agent was unreachable. Authentication itself still works."
)


class WebAuditReporter:
    """Hands one fixed authentication event to the agent and tracks the truth."""

    def __init__(self, agent, *, log=None, actor="appliance-admin", time_fn=None):
        self.agent = agent
        self.log = log
        self.actor = actor
        self._time = time_fn or time.time
        self._lock = threading.Lock()
        self.recorded_events = 0
        self.unrecorded_events = 0
        self.last_error = ""
        self.last_error_at = 0.0
        self.last_recorded_at = 0.0

    def record(self, event, *, source_ip="", result=RESULT_SUCCESS, reason=""):
        """Return True only when the agent confirmed the authoritative write."""

        try:
            self.agent.call(
                AGENT_OPERATION,
                actor=self.actor,
                source_ip=source_ip,
                event=event,
                result=result,
                reason=reason,
            )
        except (AgentUnavailableError, AgentCallError) as exc:
            self._degrade(event, result, getattr(exc, "code", "agent_unavailable"))
            return False
        except Exception as exc:  # a broken transport must not break a login
            self._degrade(event, result, exc.__class__.__name__)
            return False

        with self._lock:
            self.recorded_events += 1
            self.last_recorded_at = self._time()
        return True

    def _degrade(self, event, result, code):
        with self._lock:
            self.unrecorded_events += 1
            self.last_error = str(code)
            self.last_error_at = self._time()
        if self.log is not None:
            self.log.warn(
                "audit_unavailable",
                audit_event=str(event),
                audit_result=str(result),
                error=str(code),
            )

    def status(self):
        with self._lock:
            degraded = self.unrecorded_events > 0
            return {
                "state": STATE_DEGRADED if degraded else STATE_HEALTHY,
                "authoritative": not degraded,
                "degraded": degraded,
                "recorded_events": self.recorded_events,
                "unrecorded_events": self.unrecorded_events,
                "last_error": self.last_error,
                "last_error_at": self.last_error_at,
                "last_recorded_at": self.last_recorded_at,
                "message": DEGRADED_MESSAGE if degraded else "",
            }
