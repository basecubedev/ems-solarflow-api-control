# SPDX-License-Identifier: AGPL-3.0-or-later
"""Append-only audit and operation logs.

Entries are written as one JSON object per line. Everything is redacted before
it is written, so a password, a WLAN passphrase or a full public key can never
end up in the audit trail even if a caller passes one by accident.
"""

import json
import os
import threading
import time
from pathlib import Path

from appliance.paths import own_by_root
from appliance.redaction import redact_mapping, redact_text

RESULT_SUCCESS = "success"
RESULT_FAILURE = "failure"
RESULT_DENIED = "denied"

AUDITED_ACTIONS = (
    "login.success",
    "login.failure",
    "logout",
    "password.change",
    "password.reset",
    "admin.install",
    "admin.update",
    "admin.rollback",
    "admin.repair",
    "admin.start",
    "admin.stop",
    "admin.restart",
    "updates.install",
    "updates.repair",
    "ssh.enable",
    "ssh.disable",
    "ssh.key_added",
    "ssh.key_removed",
    "ssh.keys_revoked",
    "network.wifi",
    "network.hostname",
    "system.reboot",
    "system.shutdown",
    "support.archive",
)

# Matched as substrings, not exact names: a detail key called signing_key,
# private_key or api_token is the same thing as one called key or token, and an
# audit trail that only recognised the exact spellings would log the rest.
_NEVER_LOGGED = ("password", "passphrase", "token", "public_key", "secret", "key")


def _loggable(key):
    lowered = str(key).lower()
    return not any(marker in lowered for marker in _NEVER_LOGGED)


DEFAULT_MAX_BYTES = 4 * 1024 * 1024


class JsonlLog:
    def __init__(
        self, path, *, time_fn=None, mode=0o600, max_bytes=DEFAULT_MAX_BYTES, root_owned=True
    ):
        self.path = Path(path)
        self._time = time_fn or time.time
        self._mode = mode
        self._max_bytes = int(max_bytes) if max_bytes else 0
        self._root_owned = bool(root_owned)
        self._lock = threading.Lock()

    def append(self, record):
        entry = dict(record)
        entry.setdefault("timestamp", self._time())
        payload = json.dumps(redact_mapping(entry), sort_keys=True)
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._rotate_if_needed()
            with open(self.path, "a", encoding="utf-8") as handle:
                handle.write(payload + "\n")
                handle.flush()
            try:
                os.chmod(self.path, self._mode)
            except OSError:
                pass
            if self._root_owned:
                own_by_root(self.path)
        return entry

    def _rotate_if_needed(self):
        """Keep one generation so a log can never grow without a bound."""

        if not self._max_bytes:
            return
        try:
            if self.path.stat().st_size < self._max_bytes:
                return
            os.replace(self.path, self.path.with_name(self.path.name + ".1"))
        except OSError:
            return

    def tail(self, limit=200):
        try:
            lines = self.path.read_text(encoding="utf-8", errors="replace").splitlines()
        except FileNotFoundError:
            return []
        records = []
        for line in lines[-int(limit) :]:
            try:
                records.append(json.loads(line))
            except ValueError:
                records.append({"raw": redact_text(line)})
        return records


class AuditLog(JsonlLog):
    """Sensitive-action trail: who did what, against which target, with which result."""

    def record(
        self,
        action,
        *,
        user="",
        source_ip="",
        target="",
        result=RESULT_SUCCESS,
        operation_id="",
        detail=None,
    ):
        entry = {
            "action": str(action),
            "user": str(user or ""),
            "source_ip": str(source_ip or ""),
            "target": redact_text(target) if target else "",
            "result": str(result),
            "operation_id": str(operation_id or ""),
        }
        if detail:
            entry["detail"] = {
                key: value for key, value in dict(detail).items() if _loggable(key)
            }
        return self.append(entry)


class WebLog(JsonlLog):
    """The web service's own log: everything it may write itself.

    It is not the audit trail. Its only security role is to record, truthfully,
    that an authentication event could *not* be handed to the agent.
    """

    def __init__(self, path, *, time_fn=None, max_bytes=DEFAULT_MAX_BYTES):
        super().__init__(
            path, time_fn=time_fn, mode=0o640, max_bytes=max_bytes, root_owned=False
        )

    def warn(self, event, **detail):
        entry = {"level": "warning", "event": str(event)}
        entry.update({key: value for key, value in detail.items() if _loggable(key)})
        try:
            return self.append(entry)
        except OSError:
            # A web service that cannot write its own log must still answer.
            return None


class OperationLog(JsonlLog):
    def record(self, operation_id, stage, *, operation_type="", detail=None):
        return self.append(
            {
                "operation_id": str(operation_id),
                "type": str(operation_type or ""),
                "stage": str(stage),
                "detail": redact_text(detail) if detail else "",
            }
        )
