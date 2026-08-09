# SPDX-License-Identifier: AGPL-3.0-or-later
"""Appliance Manager authentication.

Deliberately independent from the EMS Admin password: the Appliance Manager
must still authenticate when the EMS install root is unreadable or the Admin
container is gone. The stored record uses the same PBKDF2-SHA256 shape as the
EMS dashboard so the two never drift apart in strength.

A password reset rotates a generation marker, which invalidates every existing
session without needing a shared session store.
"""

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from dataclasses import dataclass
from pathlib import Path

ALGORITHM = "pbkdf2-sha256"
DEFAULT_ITERATIONS = 600000
MIN_PASSWORD_LENGTH = 12
SESSION_COOKIE_NAME = "ems_appliance_session"
CSRF_HEADER = "X-Appliance-CSRF"

DEFAULT_IDLE_TIMEOUT = 1800
DEFAULT_ABSOLUTE_MAX = 43200
DEFAULT_MAX_FAILURES = 5
DEFAULT_FAILURE_WINDOW = 300


class AuthError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


def _b64encode(raw):
    return base64.b64encode(raw).decode("ascii")


def _b64decode(value):
    return base64.b64decode(str(value).encode("ascii"), validate=True)


def hash_password(password, iterations=DEFAULT_ITERATIONS):
    if not password:
        raise AuthError("password_required", "a password is required")
    salt = secrets.token_bytes(32)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return {
        "algorithm": ALGORITHM,
        "iterations": int(iterations),
        "salt": _b64encode(salt),
        "hash": _b64encode(digest),
    }


def verify_password_record(password, record):
    if not password or not isinstance(record, dict):
        return False
    if record.get("algorithm") != ALGORITHM:
        return False
    try:
        iterations = int(record.get("iterations"))
        salt = _b64decode(record.get("salt", ""))
        expected = _b64decode(record.get("hash", ""))
    except Exception:
        return False
    if iterations <= 0 or not salt or not expected:
        return False
    actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return hmac.compare_digest(actual, expected)


def validate_password(password, confirmation=None):
    if not isinstance(password, str) or len(password) < MIN_PASSWORD_LENGTH:
        raise AuthError(
            "password_too_short",
            f"the appliance password must be at least {MIN_PASSWORD_LENGTH} characters",
        )
    if confirmation is not None and password != confirmation:
        raise AuthError("password_mismatch", "the two passwords do not match")
    return password


class AuthStore:
    """The appliance password file, owned by the web service account."""

    def __init__(self, path, *, time_fn=None, iterations=DEFAULT_ITERATIONS):
        self.path = Path(path)
        self._time = time_fn or time.time
        self.iterations = iterations

    def load(self):
        try:
            record = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, ValueError):
            raise AuthError("auth_file_invalid", "the appliance password file cannot be read")
        if not isinstance(record, dict):
            raise AuthError("auth_file_invalid", "the appliance password file is malformed")
        return record

    def configured(self):
        try:
            return self.load() is not None
        except AuthError:
            return True

    def generation(self):
        try:
            record = self.load() or {}
        except AuthError:
            return ""
        return str(record.get("generation") or "")

    def _write(self, password, *, exclusive):
        record = hash_password(password, self.iterations)
        record["generation"] = secrets.token_hex(16)
        record["updated_at"] = self._time()
        payload = json.dumps(record, indent=2, sort_keys=True) + "\n"

        self.path.parent.mkdir(parents=True, exist_ok=True)
        if exclusive:
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            handle = os.open(self.path, flags, 0o600)
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
        else:
            tmp = self.path.with_name(f".{self.path.name}.{os.getpid()}.tmp")
            with open(tmp, "w", encoding="utf-8") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.path)
        os.chmod(self.path, 0o600)
        return record

    def create(self, password, confirmation=None):
        validate_password(password, confirmation)
        try:
            return self._write(password, exclusive=True)
        except FileExistsError:
            raise AuthError(
                "password_already_configured", "an appliance password already exists"
            )

    def reset(self, password, confirmation=None):
        """Replace the password and rotate the generation, killing all sessions."""

        validate_password(password, confirmation)
        return self._write(password, exclusive=False)

    def change(self, current_password, new_password, confirmation=None):
        if not self.verify(current_password):
            raise AuthError("current_password_invalid", "the current password is not correct")
        return self.reset(new_password, confirmation)

    def verify(self, password):
        try:
            record = self.load()
        except AuthError:
            return False
        if record is None:
            return False
        return verify_password_record(password, record)


@dataclass
class Session:
    session_id: str
    csrf_token: str
    generation: str
    created_at: float
    expires_at: float


class SessionStore:
    def __init__(
        self,
        *,
        idle_timeout=DEFAULT_IDLE_TIMEOUT,
        absolute_max=DEFAULT_ABSOLUTE_MAX,
        time_fn=None,
    ):
        self.idle_timeout = self._normalise(idle_timeout)
        self.absolute_max = self._normalise(absolute_max)
        self._time = time_fn or time.time
        self.sessions = {}

    @staticmethod
    def _normalise(value):
        if value is None:
            return None
        value = int(value)
        return None if value <= 0 else value

    def _expiry(self, created_at, now):
        bounds = []
        if self.idle_timeout is not None:
            bounds.append(now + self.idle_timeout)
        if self.absolute_max is not None:
            bounds.append(created_at + self.absolute_max)
        return min(bounds) if bounds else None

    def create(self, generation):
        self.cleanup()
        now = self._time()
        session = Session(
            session_id=secrets.token_urlsafe(32),
            csrf_token=secrets.token_urlsafe(32),
            generation=str(generation),
            created_at=now,
            expires_at=self._expiry(now, now),
        )
        self.sessions[session.session_id] = session
        return session

    def get(self, session_id, generation):
        if not session_id:
            return None
        session = self.sessions.get(session_id)
        if session is None:
            return None
        if session.generation != str(generation):
            self.sessions.pop(session_id, None)
            return None
        if session.expires_at is not None and session.expires_at <= self._time():
            self.sessions.pop(session_id, None)
            return None
        return session

    def touch(self, session_id, generation):
        session = self.get(session_id, generation)
        if session is None:
            return None
        session.expires_at = self._expiry(session.created_at, self._time())
        return session

    def destroy(self, session_id):
        if session_id:
            self.sessions.pop(session_id, None)

    def destroy_all(self):
        self.sessions.clear()

    def cleanup(self):
        now = self._time()
        for session_id in [
            key
            for key, session in self.sessions.items()
            if session.expires_at is not None and session.expires_at <= now
        ]:
            self.sessions.pop(session_id, None)


class LoginRateLimiter:
    def __init__(
        self,
        *,
        max_failures=DEFAULT_MAX_FAILURES,
        window_seconds=DEFAULT_FAILURE_WINDOW,
        max_entries=1024,
        time_fn=None,
    ):
        self.max_failures = int(max_failures)
        self.window_seconds = int(window_seconds)
        self.max_entries = int(max_entries)
        self._time = time_fn or time.time
        self.failures = {}

    def limited(self, key):
        return len(self._active(key)) >= self.max_failures

    def record_failure(self, key):
        attempts = self._active(key)
        attempts.append(self._time())
        self.failures[key] = attempts
        if len(self.failures) > self.max_entries:
            oldest = sorted(self.failures.items(), key=lambda item: max(item[1] or [0]))
            for stale, _ in oldest[: len(self.failures) - self.max_entries]:
                self.failures.pop(stale, None)

    def reset(self, key):
        self.failures.pop(key, None)

    def retry_after(self, key):
        attempts = self._active(key)
        if len(attempts) < self.max_failures:
            return 0
        return max(0, int(min(attempts) + self.window_seconds - self._time()))

    def _active(self, key):
        now = self._time()
        attempts = [ts for ts in self.failures.get(key, []) if ts > now - self.window_seconds]
        if attempts:
            self.failures[key] = attempts
        else:
            self.failures.pop(key, None)
        return attempts
