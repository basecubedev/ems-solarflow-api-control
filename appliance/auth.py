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
import fcntl
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
    if not isinstance(password, str) or not password or not isinstance(record, dict):
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
    """Non-empty, and matching its confirmation. Nothing about length.

    There is deliberately no length rule. One password now opens the appliance,
    the Admin console and the dashboard, and the other two have always accepted
    any non-empty one -- a minimum here would mean a password set from the EMS
    side could not be changed from this one. How strong it is, is the operator's
    decision about their own device.
    """

    if not isinstance(password, str) or not password:
        raise AuthError("password_required", "a password is required")
    if confirmation is not None and password != confirmation:
        raise AuthError("password_mismatch", "the two passwords do not match")
    return password


class AuthStore:
    """The appliance password file, owned by the web service account."""

    def __init__(self, path, *, time_fn=None, iterations=DEFAULT_ITERATIONS, owner=None):
        """``owner`` is the (uid, gid) the EMS containers run as.

        The store is shared with the Admin console, which reads it from inside a
        container running as the deployment user. A file the agent wrote as
        root:root 0600 would lock that container out of the password it is
        supposed to check, so the owner is decided here, on the temporary file,
        before the name exists -- not chowned afterwards as a second authority.
        """

        self.path = Path(path)
        self._time = time_fn or time.time
        self.iterations = iterations
        self.owner = owner

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
        """A marker that changes whenever the stored password changes.

        Derived from the record rather than stored in it. The file is shared
        with the Admin console and the dashboard, and `emsctl dashboard
        set-password` rewrites it with the four fields those two agree on -- a
        marker only this side maintained would be dropped on every change made
        from there, and appliance sessions would survive a password change they
        should not survive.
        """

        try:
            record = self.load() or {}
        except AuthError:
            return ""
        material = f"{record.get('salt', '')}:{record.get('hash', '')}"
        if material == ":":
            return ""
        return hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]

    def _locked(self):
        """One writer at a time, across processes.

        The agent is a threading server and the CLI is a second process, so two
        password changes can overlap. Without this the loser's temporary file is
        renamed over the winner's record -- or vanishes under it -- and one of
        the two operators is told a password was stored that was not.
        """

        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock = self.path.with_name(f".{self.path.name}.lock")
        handle = os.open(str(lock), os.O_WRONLY | os.O_CREAT, 0o600)
        try:
            fcntl.flock(handle, fcntl.LOCK_EX)
        except OSError:
            os.close(handle)
            raise
        return handle

    def _write(self, password, *, exclusive):
        lock = self._locked()
        try:
            return self._write_locked(password, exclusive=exclusive)
        finally:
            os.close(lock)

    def _write_locked(self, password, *, exclusive):
        # Exactly the four fields the dashboard and the Admin console agree on.
        # Anything else is dropped the moment a password is changed from there,
        # so a reader could not tell a stale extra field from a current one.
        record = hash_password(password, self.iterations)
        payload = json.dumps(record, indent=2, sort_keys=True) + "\n"

        self.path.parent.mkdir(parents=True, exist_ok=True)
        if exclusive:
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            handle = os.open(self.path, flags, 0o600)
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            self._own(self.path)
        else:
            tmp = self.path.with_name(
                f".{self.path.name}.{os.getpid()}.{secrets.token_hex(8)}.tmp"
            )
            with open(tmp, "w", encoding="utf-8") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(tmp, 0o600)
            self._own(tmp)
            os.replace(tmp, self.path)
        os.chmod(self.path, 0o600)
        # The rename is a directory operation: without flushing the parent a
        # power cut can leave no password file at all, and the box would boot
        # into first-run enrolment with a root-capable agent behind it.
        self._sync_parent()
        return record

    def _sync_parent(self):
        try:
            handle = os.open(str(self.path.parent), os.O_RDONLY)
        except OSError:
            return False
        try:
            os.fsync(handle)
        except OSError:
            return False
        finally:
            os.close(handle)
        return True

    def _own(self, path):
        if not self.owner:
            return False
        try:
            if os.geteuid() != 0:
                return False
            os.chown(path, int(self.owner[0]), int(self.owner[1]))
        except (AttributeError, OSError, TypeError, ValueError):
            return False
        return True

    def state_is_known(self):
        """A store read from the local filesystem always knows its own state.

        The agent-mediated store in web.py cannot say the same, and callers that
        turn "configured" into a message have to tell the two apart."""

        return True

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


def deployment_owner(install_root):
    """The (uid, gid) the hosted containers run as, or ``None``.

    Read from the deployment root itself, not by resolving an account name:
    ``/etc/passwd`` is slot-local on an A/B image, so the same name can carry a
    different uid in the other slot, while the containers keep running as the
    uid baked into the compose file. The owner of the root is the identity --
    the same rule the deployment bootstrap already applies.

    ``None`` when the root does not exist yet or is still root-owned, in which
    case adoption has not happened and the file stays with whoever wrote it.
    """

    try:
        entry = Path(install_root).stat()
    except OSError:
        return None
    if entry.st_uid == 0:
        return None
    return (entry.st_uid, entry.st_gid)
