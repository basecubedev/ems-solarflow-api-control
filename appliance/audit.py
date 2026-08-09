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

_NEVER_LOGGED = frozenset({"password", "passphrase", "token", "public_key", "secret"})


class JsonlLog:
    def __init__(self, path, *, time_fn=None, mode=0o640):
        self.path = Path(path)
        self._time = time_fn or time.time
        self._mode = mode
        self._lock = threading.Lock()

    def append(self, record):
        entry = dict(record)
        entry.setdefault("timestamp", self._time())
        payload = json.dumps(redact_mapping(entry), sort_keys=True)
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.path, "a", encoding="utf-8") as handle:
                handle.write(payload + "\n")
                handle.flush()
            try:
                os.chmod(self.path, self._mode)
            except OSError:
                pass
        return entry

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
                key: value
                for key, value in dict(detail).items()
                if key.lower() not in _NEVER_LOGGED
            }
        return self.append(entry)


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
