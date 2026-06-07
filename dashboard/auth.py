# SPDX-License-Identifier: AGPL-3.0-or-later
import base64
import hmac
import json
import os
import secrets
import time
from dataclasses import dataclass

import hashlib


ALGORITHM = "pbkdf2-sha256"
DEFAULT_ITERATIONS = 600000
DEFAULT_AUTH_FILE = os.path.join("config", "dashboard-auth.json")
SESSION_COOKIE_NAME = "ems_dashboard_session"


def resolve_auth_path(base_dir, auth_file=None):
    path = auth_file or DEFAULT_AUTH_FILE
    if os.path.isabs(path):
        return path
    return os.path.join(base_dir, path)


def _b64encode(raw):
    return base64.b64encode(raw).decode("ascii")


def _b64decode(value):
    return base64.b64decode(str(value).encode("ascii"), validate=True)


def hash_password(password, iterations=DEFAULT_ITERATIONS):
    if not password:
        raise ValueError("password must not be empty")

    salt = secrets.token_bytes(32)
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt,
        iterations,
    )
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

    actual = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt,
        iterations,
    )
    return hmac.compare_digest(actual, expected)


def load_auth_file(path):
    try:
        with open(path) as f:
            record = json.load(f)
    except FileNotFoundError:
        return None
    except Exception as exc:
        raise ValueError(f"cannot read dashboard auth file {path}: {exc}") from exc

    if not isinstance(record, dict):
        raise ValueError(f"dashboard auth file {path} must contain a JSON object")

    return record


def auth_configured(path):
    return load_auth_file(path) is not None


def verify_password_file(path, password):
    record = load_auth_file(path)
    if record is None:
        return False
    return verify_password_record(password, record)


def write_password_file(path, password):
    record = hash_password(password)
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)

    tmp_path = f"{path}.{os.getpid()}.tmp"
    with open(tmp_path, "w") as f:
        json.dump(record, f, indent=2, sort_keys=True)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())

    try:
        os.chmod(tmp_path, 0o600)
    except OSError:
        pass

    os.replace(tmp_path, path)

    try:
        os.chmod(path, 0o600)
    except OSError:
        pass

    return record


def remove_auth_file(path):
    try:
        os.remove(path)
        return True
    except FileNotFoundError:
        return False


@dataclass
class Session:
    session_id: str
    csrf_token: str
    expires_at: float


class SessionStore:
    def __init__(self, timeout_seconds=1800, time_fn=None):
        self.timeout_seconds = int(timeout_seconds)
        self.time_fn = time_fn or time.time
        self.sessions = {}

    def create(self):
        self.cleanup()
        session_id = secrets.token_urlsafe(32)
        csrf_token = secrets.token_urlsafe(32)
        session = Session(
            session_id=session_id,
            csrf_token=csrf_token,
            expires_at=self.time_fn() + self.timeout_seconds,
        )
        self.sessions[session_id] = session
        return session

    def get(self, session_id):
        if not session_id:
            return None

        session = self.sessions.get(session_id)
        if session is None:
            return None

        if session.expires_at <= self.time_fn():
            self.sessions.pop(session_id, None)
            return None

        return session

    def destroy(self, session_id):
        if session_id:
            self.sessions.pop(session_id, None)

    def cleanup(self):
        now = self.time_fn()
        expired = [
            session_id
            for session_id, session in self.sessions.items()
            if session.expires_at <= now
        ]
        for session_id in expired:
            self.sessions.pop(session_id, None)


class LoginRateLimiter:
    def __init__(
        self,
        max_failures=5,
        window_seconds=60,
        max_entries=1024,
        time_fn=None,
    ):
        self.max_failures = int(max_failures)
        self.window_seconds = int(window_seconds)
        self.max_entries = int(max_entries)
        self.time_fn = time_fn or time.time
        self.failures = {}

    def is_limited(self, key):
        self.prune()
        attempts = self._active_attempts(key)
        return len(attempts) >= self.max_failures

    def record_failure(self, key):
        self.prune()
        attempts = self._active_attempts(key)
        attempts.append(self.time_fn())
        self.failures[key] = attempts
        self._cap_entries()

    def reset(self, key):
        self.failures.pop(key, None)

    def prune(self):
        for key in list(self.failures):
            self._active_attempts(key)

    def _active_attempts(self, key):
        now = self.time_fn()
        attempts = [
            timestamp
            for timestamp in self.failures.get(key, [])
            if timestamp > now - self.window_seconds
        ]
        if attempts:
            self.failures[key] = attempts
        else:
            self.failures.pop(key, None)
        return attempts

    def _cap_entries(self):
        if len(self.failures) <= self.max_entries:
            return
        ordered = sorted(
            self.failures.items(),
            key=lambda item: max(item[1]) if item[1] else 0,
        )
        for key, _ in ordered[:len(self.failures) - self.max_entries]:
            self.failures.pop(key, None)
