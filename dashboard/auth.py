# SPDX-FileCopyrightText: 2026 basecubedev
# SPDX-License-Identifier: AGPL-3.0-or-later
import base64
import hmac
import json
import logging
import os
import secrets
import tempfile
import threading
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


def create_password_file_if_missing(path, password):
    """Atomically create the auth file, refusing to overwrite an existing one.

    Unlike :func:`write_password_file` (which replaces), this uses ``O_EXCL`` so
    a first-visitor password creation can never clobber a password another
    browser created a moment earlier. Raises ``FileExistsError`` if the file
    already exists.
    """

    record = hash_password(password)
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)

    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    fd = os.open(path, flags, 0o600)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(record, f, indent=2, sort_keys=True)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
    except Exception:
        try:
            os.unlink(path)
        except OSError:
            pass
        raise

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


def auth_file_fingerprint(path):
    """Digest of the password record a session was opened against, or None.

    A session bound to it ends when the password changes, is removed or is
    restored from a backup, instead of keeping write access for hours.
    """

    try:
        with open(path, "rb") as handle:
            return hashlib.sha256(handle.read()).hexdigest()
    except OSError:
        return None


@dataclass
class Session:
    session_id: str
    csrf_token: str
    created_at: float
    # None means "never expires" (a disabled timeout). Otherwise the wall-clock
    # time at which the session becomes invalid.
    expires_at: float = None
    credential: str = None


class SessionStore:
    """Session store with idle-sliding bounded by an absolute lifetime.

    ``timeout_seconds`` is the idle timeout that slides on genuine activity (via
    :meth:`touch`); ``absolute_max_seconds`` is the hard cap measured from
    session creation that sliding can never exceed. A value of ``0`` or ``None``
    for either disables that bound (an explicit "infinite" opt-in). Negative
    values are not expected here — they are rejected at config load — but are
    treated as disabled defensively.

    Sessions are kept by a digest of their id, never the id itself. With
    ``path`` they are also kept in that file (``0600``), so a restart, an
    update that replaces the process included, keeps every sign-in: the file
    holds no session id, only the digests, the CSRF tokens, the times and the
    password record each was opened against, and a session read back is ended
    as it would have been, at its expiry or once the password changed. A file
    that cannot be read starts with no sessions; one that cannot be written
    leaves them in memory only and removes the file, so a sign-out that could
    not be written does not come back with the next process; a file in a place
    this process cannot write to is not read. A file that can be neither
    written nor removed in a writable place is left and logged as an error, and
    a sign-out in the moments before a power cut may come back with the file as
    it was before. A session read back takes today's absolute limit, and
    today's idle limit counted from the restart. The file is
    written outside the lock every request takes, newest state last.
    """

    FILE_VERSION = 1

    def __init__(self, timeout_seconds=1800, absolute_max_seconds=43200, time_fn=None, path=None):
        self.timeout_seconds = self._normalize_timeout(timeout_seconds)
        self.absolute_max_seconds = self._normalize_timeout(absolute_max_seconds)
        self.time_fn = time_fn or time.time
        self.path = os.fspath(path) if path else None
        self.sessions = {}
        self._lock = threading.RLock()
        self._write_lock = threading.Lock()
        self._generation = 0
        self._written = 0
        self._pending = None
        if self.path:
            self._load()

    @staticmethod
    def _normalize_timeout(value):
        if value is None:
            return None
        value = int(value)
        return None if value <= 0 else value

    @staticmethod
    def _key(session_id):
        return hashlib.sha256(str(session_id).encode("utf-8")).hexdigest()

    def _expiry(self, created_at, now):
        bounds = []
        if self.timeout_seconds is not None:
            bounds.append(now + self.timeout_seconds)
        if self.absolute_max_seconds is not None:
            bounds.append(created_at + self.absolute_max_seconds)
        return min(bounds) if bounds else None

    def create(self, credential=None):
        with self._lock:
            self._drop_expired()
            now = self.time_fn()
            session = Session(
                session_id=secrets.token_urlsafe(32),
                csrf_token=secrets.token_urlsafe(32),
                created_at=now,
                expires_at=self._expiry(now, now),
                credential=credential,
            )
            self.sessions[self._key(session.session_id)] = session
            self._changed()
        self._flush()
        return session

    def get(self, session_id, credential=None):
        """The live session, or None.

        A session opened against a password record (``credential``) is ended
        once the current record differs.
        """

        if not session_id:
            return None

        with self._lock:
            key = self._key(session_id)
            session = self.sessions.get(key)
            if session is None:
                return None
            ended = (session.expires_at is not None and session.expires_at <= self.time_fn()) or (
                session.credential is not None and not hmac.compare_digest(str(credential or ""), session.credential)
            )
            if ended:
                self._end(key)
        if ended:
            self._flush()
            return None
        return session

    def touch(self, session_id):
        """Slide the idle timeout on genuine activity, capped at the absolute max.

        Renewal is explicit (never inside :meth:`get`, which stays read-only).
        Once the absolute cap is reached ``expires_at`` stops moving, so a session
        can never be extended past ``created_at + absolute_max_seconds``.
        """
        with self._lock:
            session = self.sessions.get(self._key(session_id)) if session_id else None
            stale = session is None or (
                session.expires_at is not None
                and session.expires_at <= self.time_fn()
            )
            if not stale:
                expires_at = self._expiry(session.created_at, self.time_fn())
                if expires_at != session.expires_at:
                    session.expires_at = expires_at
                    self._changed()
        if stale:
            return self.get(session_id)
        self._flush()
        return session

    def destroy(self, session_id):
        if session_id:
            with self._lock:
                self._end(self._key(session_id))
            self._flush()

    def cleanup(self):
        with self._lock:
            self._drop_expired()
        self._flush()

    def _drop_expired(self):
        now = self.time_fn()
        expired = [
            key
            for key, session in self.sessions.items()
            if session.expires_at is not None and session.expires_at <= now
        ]
        for key in expired:
            self.sessions.pop(key, None)
        if expired:
            self._changed()

    def _end(self, key):
        if self.sessions.pop(key, None) is not None:
            self._changed()

    def _changed(self):
        """Note the sessions as they are now for the file; the caller holds the lock and flushes after releasing it."""

        if not self.path:
            return
        self._generation += 1
        self._pending = (self._generation, {
            "version": self.FILE_VERSION,
            "sessions": {
                key: {
                    "csrf_token": session.csrf_token,
                    "created_at": session.created_at,
                    "expires_at": session.expires_at,
                    "credential": session.credential,
                }
                for key, session in self.sessions.items()
            },
        })

    def _flush(self):
        """Write the newest noted state, never an older one after it; a write that fails removes the file."""

        if not self.path:
            return
        with self._write_lock:
            with self._lock:
                pending = self._pending
            if pending is None or pending[0] <= self._written:
                return
            generation, record = pending
            if self._write(record):
                self._written = generation
                return
            try:
                os.unlink(self.path)
                _sync_directory(self.path)
            except FileNotFoundError:
                pass
            except OSError:
                logging.error("session_store_unremovable path=%s", self.path)
            self._written = generation

    def _write(self, record):
        directory = os.path.dirname(self.path) or "."
        scratch = None
        try:
            os.makedirs(directory, exist_ok=True)
            fd, scratch = tempfile.mkstemp(prefix=".sessions.", dir=directory)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(record, handle, sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(scratch, self.path)
            scratch = None
            _sync_directory(self.path)
            return True
        except OSError:
            logging.warning("session_store_unwritable path=%s", self.path)
            return False
        finally:
            if scratch is not None:
                try:
                    os.unlink(scratch)
                except OSError:
                    pass

    def _load(self):
        if not os.access(os.path.dirname(self.path) or ".", os.W_OK):
            logging.warning("session_store_not_writable path=%s", self.path)
            return
        try:
            with open(self.path, encoding="utf-8") as handle:
                record = json.load(handle)
        except FileNotFoundError:
            return
        except (OSError, ValueError, UnicodeDecodeError):
            logging.warning("session_store_unreadable path=%s", self.path)
            return
        if not isinstance(record, dict) or record.get("version") != self.FILE_VERSION or not isinstance(record.get("sessions"), dict):
            return
        now = self.time_fn()
        for key, entry in record["sessions"].items():
            session = _stored_session(key, entry)
            if session is None:
                continue
            bound = self._expiry(session.created_at, now)
            if bound is not None:
                session.expires_at = bound if session.expires_at is None else min(session.expires_at, bound)
            if session.expires_at is None or session.expires_at > now:
                self.sessions[key] = session


def _sync_directory(path):
    """Make a rename or removal in ``path``'s directory survive a power cut."""

    try:
        fd = os.open(os.path.dirname(path) or ".", os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _stored_session(key, entry):
    """A session read back from the store's file, or ``None`` for an entry that is not one."""

    if not isinstance(key, str) or len(key) != 64 or not isinstance(entry, dict):
        return None
    csrf_token, created_at = entry.get("csrf_token"), entry.get("created_at")
    expires_at, credential = entry.get("expires_at"), entry.get("credential")
    if not isinstance(csrf_token, str) or not csrf_token:
        return None
    if isinstance(created_at, bool) or not isinstance(created_at, (int, float)):
        return None
    if expires_at is not None and (isinstance(expires_at, bool) or not isinstance(expires_at, (int, float))):
        return None
    if credential is not None and not isinstance(credential, str):
        return None
    return Session(session_id=None, csrf_token=csrf_token, created_at=created_at, expires_at=expires_at, credential=credential)


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
        self._lock = threading.RLock()

    def is_limited(self, key):
        with self._lock:
            self.prune()
            attempts = self._active_attempts(key)
            return len(attempts) >= self.max_failures

    def try_attempt(self, key):
        """Count an attempt before the password is checked; False when limited.

        Checking the limit first and recording the failure after the slow hash
        let any number of concurrent guesses through the same window.
        """

        with self._lock:
            if self.is_limited(key):
                return False
            self.record_failure(key)
            return True

    def record_failure(self, key):
        with self._lock:
            self.prune()
            attempts = self._active_attempts(key)
            attempts.append(self.time_fn())
            self.failures[key] = attempts
            self._cap_entries()

    def reset(self, key):
        with self._lock:
            self.failures.pop(key, None)

    def prune(self):
        with self._lock:
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
