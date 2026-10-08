# SPDX-License-Identifier: AGPL-3.0-or-later
"""Appliance Manager authentication.

The password is the one the EMS Admin console and the dashboard share, in their
file in the EMS deployment root, with the same PBKDF2-SHA256 record. A
root-owned copy of the record the Appliance Manager last confirmed decides what
a shared file rewritten from a container can no longer open.

A password reset rotates a generation marker, which invalidates every existing
session without needing a shared session store.
"""

import base64
import contextlib
import errno
import fcntl
import hashlib
import hmac
import ipaddress
import json
import os
import secrets
import stat
import time
from dataclasses import dataclass
from pathlib import Path

from appliance.paths import atomic_write

ALGORITHM = "pbkdf2-sha256"
DEFAULT_ITERATIONS = 600000
MAX_ITERATIONS = 10 * DEFAULT_ITERATIONS
SESSION_COOKIE_NAME = "ems_appliance_session"
CSRF_HEADER = "X-Appliance-CSRF"

DEFAULT_IDLE_TIMEOUT = 1800
DEFAULT_ABSOLUTE_MAX = 43200
DEFAULT_MAX_FAILURES = 5
DEFAULT_MAX_NETWORK_FAILURES = 20
DEFAULT_FAILURE_WINDOW = 300
DEFAULT_CONCURRENT_PASSWORD_CHECKS = 2
DEFAULT_LOCK_WAIT_SECONDS = 10
LOCK_POLL_SECONDS = 0.05
MAX_RECORD_BYTES = 64 * 1024
IPV6_NETWORK_PREFIX = 64


def lock_path(path):
    """The lock a writer of ``path`` takes, beside the record it guards.

    It outlives the writer on purpose -- ``flock`` needs a file both processes
    can open -- so it is a second artifact every writer of the shared password
    leaves behind. Anything that reasons about what is in that directory has to
    ask here rather than spell the name a second time: the deployment root's
    adoption check did spell it, did not have it, and setting a password became
    the thing that prevented ever installing Admin.
    """

    path = Path(path)
    return path.with_name(f".{path.name}.lock")


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
    if not 0 < iterations <= MAX_ITERATIONS or not salt or not expected:
        return False
    actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return hmac.compare_digest(actual, expected)


RECORD_FIELDS = ("algorithm", "iterations", "salt", "hash")


def record_generation(record):
    """A marker that changes whenever a stored password record changes.

    Derived from the whole record, so any writer moves it without having to
    know about it, and two records that verify differently never share one.
    """

    if not isinstance(record, dict) or not (record.get("salt") or record.get("hash")):
        return ""
    material = ":".join(str(record.get(name, "")) for name in RECORD_FIELDS)
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]


def _well_formed_record(record):
    if not isinstance(record, dict) or record.get("algorithm") != ALGORITHM:
        return False
    try:
        iterations = int(record.get("iterations"))
        salt = _b64decode(record.get("salt", ""))
        digest = _b64decode(record.get("hash", ""))
    except Exception:
        return False
    return 0 < iterations <= MAX_ITERATIONS and bool(salt) and bool(digest)


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


class ConfirmedPassword:
    """The password record the Appliance Manager itself last confirmed.

    The shared store lives in the EMS deployment root, which the containers
    mount read-write, so whoever runs code in one can rewrite or delete it. This
    copy lives in the agent's root-owned state, and the actions that hand out a
    shell on the host ask for it rather than for whatever the shared file says
    now. It has the four fields of the shared record and no version of its own:
    an older Appliance Manager does not know it and leaves it alone.
    """

    def __init__(self, path):
        self.path = Path(path)

    def exists(self):
        return os.path.lexists(self.path)

    def load(self):
        try:
            record = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, ValueError, RecursionError):
            raise AuthError(
                "confirmed_password_invalid", "the confirmed password record cannot be read"
            )
        if not _well_formed_record(record):
            raise AuthError(
                "confirmed_password_invalid", "the confirmed password record is malformed"
            )
        return record

    def generation(self):
        try:
            return record_generation(self.load())
        except AuthError:
            return ""

    def verify(self, password):
        try:
            record = self.load()
        except AuthError:
            return False
        return record is not None and verify_password_record(password, record)

    def store(self, record):
        if not _well_formed_record(record):
            raise AuthError("auth_file_invalid", "the appliance password file is malformed")
        kept = {name: record[name] for name in RECORD_FIELDS}
        atomic_write(
            self.path,
            json.dumps(kept, indent=2, sort_keys=True) + "\n",
            mode=0o600,
            owner_root=True,
        )
        return kept


def _lock_refused(lock, reason):
    return AuthError(
        "password_store_unavailable",
        f"the lock beside the password file ({Path(lock).name}) {reason}",
    )


class AuthStore:
    """The shared password file in the EMS deployment root."""

    def __init__(
        self,
        path,
        *,
        time_fn=None,
        iterations=DEFAULT_ITERATIONS,
        owner=None,
        confirmed=None,
        lock_wait=DEFAULT_LOCK_WAIT_SECONDS,
    ):
        """``owner`` is the (uid, gid) the EMS containers run as.

        The store is shared with the Admin console, which reads it from inside a
        container running as the deployment user. A file the agent wrote as
        root:root 0600 would lock that container out of the password it is
        supposed to check, so the owner is decided here, on the temporary file,
        before the name exists -- not chowned afterwards as a second authority.

        It may be a callable, and from a long-lived agent it has to be: the root
        is still root-owned on a freshly flashed appliance, so the owner is
        ``None`` then, and the first Admin install hands the root to the
        deployment account inside that same process. A value captured at start
        stays ``None`` for the rest of its life, and every later password write
        lands root-owned -- locking the Admin console out of the very file it
        authenticates against.

        ``confirmed`` is the :class:`ConfirmedPassword` kept in step where the
        Appliance Manager sets a password, and ``None`` for a writer that cannot
        reach root-owned state.

        ``lock_wait`` bounds the wait for the writers' lock, which lives where a
        container can hold it for ever.
        """

        self.path = Path(path)
        self._time = time_fn or time.time
        self.iterations = iterations
        self.owner = owner
        self.confirmed = confirmed
        self.lock_wait = lock_wait

    def load(self):
        """The record, read without following a link or waiting on a pipe.

        The file sits where a container can replace it with anything, and the
        agent reads it as root before it serves anyone.
        """

        try:
            handle = os.open(str(self.path), os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
        except FileNotFoundError:
            return None
        except OSError:
            raise AuthError("auth_file_invalid", "the appliance password file cannot be read")
        try:
            if not stat.S_ISREG(os.fstat(handle).st_mode):
                raise AuthError(
                    "auth_file_invalid", "the appliance password file is not a regular file"
                )
            raw = os.read(handle, MAX_RECORD_BYTES + 1)
        except OSError:
            raise AuthError("auth_file_invalid", "the appliance password file cannot be read")
        finally:
            os.close(handle)
        try:
            if len(raw) > MAX_RECORD_BYTES:
                raise ValueError("too large")
            record = json.loads(raw.decode("utf-8"))
        except (ValueError, RecursionError):
            raise AuthError("auth_file_invalid", "the appliance password file cannot be read")
        if not isinstance(record, dict):
            raise AuthError("auth_file_invalid", "the appliance password file is malformed")
        return record

    def configured(self):
        """A shared file gone from under a confirmed password is still a set one:
        deleting it from a container must not reopen first-run setup."""

        try:
            if self.load() is not None:
                return True
        except AuthError:
            return True
        return self._anchored()

    def _anchored(self):
        return self.confirmed is not None and self.confirmed.exists()

    def status(self):
        """What the agent answers when asked about the password."""

        try:
            record = self.load()
        except AuthError:
            return {"configured": True, "generation": "", "confirmed": False, "file_missing": False}
        generation = record_generation(record)
        return {
            "configured": record is not None or self._anchored(),
            "generation": generation,
            "confirmed": self.confirms(generation),
            "file_missing": record is None and self._anchored(),
        }

    def confirms(self, generation):
        """Whether ``generation`` is that of the password this appliance confirmed."""

        if self.confirmed is None or not generation:
            return False
        return hmac.compare_digest(self.confirmed.generation(), str(generation))

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
            return record_generation(self.load())
        except AuthError:
            return ""

    def _locked(self):
        """One writer at a time, across processes.

        The agent is a threading server and the CLI is a second process, so two
        password changes can overlap. Without this the loser's temporary file is
        renamed over the winner's record -- or vanishes under it -- and one of
        the two operators is told a password was stored that was not.
        """

        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock = lock_path(self.path)
        try:
            handle = os.open(
                str(lock), os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600
            )
        except PermissionError:
            raise _lock_refused(lock, "cannot be opened; run this as root")
        except OSError as exc:
            if exc.errno in (errno.ELOOP, errno.ENXIO, errno.EISDIR):
                raise _lock_refused(lock, "is not a regular file; remove it and try again")
            raise _lock_refused(lock, f"cannot be opened: {exc.strerror}")
        deadline = time.monotonic() + max(0, self.lock_wait)
        try:
            if not stat.S_ISREG(os.fstat(handle).st_mode):
                raise _lock_refused(lock, "is not a regular file; remove it and try again")
            while True:
                try:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    return handle
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise _lock_refused(
                            lock,
                            "is held by another process; stop the EMS and Admin containers "
                            "and try again",
                        )
                time.sleep(LOCK_POLL_SECONDS)
        except BaseException:
            os.close(handle)
            raise

    @contextlib.contextmanager
    def _exclusive(self):
        handle = self._locked()
        try:
            yield
        finally:
            os.close(handle)

    def _confirm_locked(self, record):
        if self.confirmed is None:
            return
        try:
            self.confirmed.store(record)
        except OSError as exc:
            raise AuthError(
                "confirmed_password_unwritten",
                "the password could not be recorded as the confirmed one "
                f"({exc.strerror or exc}); SSH and Manager changes wait until it is: confirm "
                "it again once there is space, or run 'sudo ems-appliance password-reset'",
            )

    def _write_locked(self, password, *, exclusive):
        # Exactly the four fields the dashboard and the Admin console agree on.
        # Anything else is dropped the moment a password is changed from there,
        # so a reader could not tell a stale extra field from a current one.
        record = hash_password(password, self.iterations)
        payload = json.dumps(record, indent=2, sort_keys=True) + "\n"

        # Container-writable directory: mode and owner change only through our own descriptor.
        self.path.parent.mkdir(parents=True, exist_ok=True)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
        if exclusive:
            self._write_new(self.path, flags, payload)
        else:
            tmp = self.path.with_name(
                f".{self.path.name}.{os.getpid()}.{secrets.token_hex(8)}.tmp"
            )
            if os.path.isdir(self.path) and not os.path.islink(self.path):
                raise AuthError(
                    "password_store_unavailable",
                    f"the password file ({self.path.name}) is a directory; remove it and "
                    "try again",
                )
            self._write_new(tmp, flags, payload)
            try:
                os.replace(tmp, self.path)
            except OSError:
                tmp.unlink(missing_ok=True)
                raise
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

    def _resolved_owner(self):
        owner = self.owner
        if callable(owner):
            try:
                owner = owner()
            except Exception:
                return None
        return owner or None

    def _write_new(self, path, flags, payload):
        """A file this call created and could not finish is removed again: a
        half-written record reads as a set password nobody can sign in with."""

        handle = os.open(path, flags, 0o600)
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                os.fchmod(stream.fileno(), 0o600)
                self._own(stream.fileno())
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
        except BaseException:
            Path(path).unlink(missing_ok=True)
            raise

    def _own(self, descriptor):
        owner = self._resolved_owner()
        if not owner:
            return False
        try:
            if os.geteuid() != 0:
                return False
            os.fchown(descriptor, int(owner[0]), int(owner[1]))
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
        with self._exclusive():
            if self._anchored():
                raise AuthError(
                    "password_already_configured",
                    "a password was already set on this appliance; reset it with "
                    "'sudo ems-appliance password-reset'",
                )
            try:
                record = self._write_locked(password, exclusive=True)
            except FileExistsError:
                raise AuthError(
                    "password_already_configured", "an appliance password already exists"
                )
            self._confirm_locked(record)
        return record

    def reset(self, password, confirmation=None):
        """Replace the password and rotate the generation, killing all sessions."""

        validate_password(password, confirmation)
        with self._exclusive():
            record = self._write_locked(password, exclusive=False)
            self._confirm_locked(record)
        return record

    def change(self, current_password, new_password, confirmation=None):
        """The confirmed record follows a change made with the confirmed password only.

        Whoever rewrote the shared file can sign in with their own password and
        change it from there; that must not make theirs the confirmed one. The
        current password and the generation it is judged by come from one read.
        """

        with self._exclusive():
            current = self._load_or_none()
            if current is None or not verify_password_record(current_password, current):
                raise AuthError("current_password_invalid", "the current password is not correct")
            validate_password(new_password, confirmation)
            follows = self.confirms(record_generation(current))
            record = self._write_locked(new_password, exclusive=False)
            if follows:
                self._confirm_locked(record)
        return record

    def confirm(self, previous_password, generation):
        """Make the shared password the confirmed one, given the confirmed one.

        ``generation`` is the one the caller signed in with. The shared record
        is taken only while it is still that one, so a file swapped in between
        is not what becomes confirmed.
        """

        try:
            confirmed = self.confirmed.load() if self.confirmed is not None else None
        except AuthError:
            confirmed = None
        if confirmed is None:
            raise AuthError(
                "confirmed_password_unavailable",
                "this appliance keeps no readable record of a confirmed password; reset the "
                "password with 'sudo ems-appliance password-reset'",
            )
        if not verify_password_record(previous_password, confirmed):
            raise AuthError(
                "previous_password_invalid",
                "that is not the password the Appliance Manager knew before",
            )
        with self._exclusive():
            current = self._load_or_none()
            if current is None or not hmac.compare_digest(
                record_generation(current), str(generation or "")
            ):
                raise AuthError(
                    "password_changed",
                    "the password changed while it was being confirmed; sign in again",
                )
            self._confirm_locked(current)
        return current

    def adopt(self):
        """Take the shared password as the confirmed one where none was ever confirmed.

        An appliance updated from a Manager that kept no confirmed record has
        only the shared file to go by, and whatever it holds at that moment is
        trusted from then on -- a file rewritten before the update included,
        which nothing here can tell apart. No lock: this runs before the agent
        serves anyone, and the lock lives where a container could hold it.
        """

        if self.confirmed is None or self._anchored():
            return False
        record = self._load_or_none()
        if not _well_formed_record(record):
            return False
        self._confirm_locked(record)
        return True

    def _load_or_none(self):
        try:
            return self.load()
        except AuthError:
            return None

    def check(self, password):
        """The generation of the record ``password`` matches, or "" for none.

        One read for both answers: a sign-in bound to a generation read in a
        second step is bound to whatever record was swapped in between.
        """

        record = self._load_or_none()
        if record is None or not verify_password_record(password, record):
            return ""
        return record_generation(record)

    def verify(self, password):
        return bool(self.check(password))


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


def throttle_sources(address):
    """The client address a failed password counts against, and its IPv6 /64.

    The /64 is ``None`` for an IPv4 client, including one that reached the
    dual-stack listener mapped into IPv6 as ``::ffff:a.b.c.d``, and for
    anything that is not an address. The zone index of a link-local address
    names this appliance's interface, not the client, and is left out.
    """

    text = str(address or "").strip()
    try:
        parsed = ipaddress.ip_address(text.partition("%")[0])
    except ValueError:
        return text, None
    if parsed.version == 6 and parsed.ipv4_mapped is not None:
        parsed = parsed.ipv4_mapped
    if parsed.version == 4:
        return str(parsed), None
    network = ipaddress.IPv6Network((parsed, IPV6_NETWORK_PREFIX), strict=False)
    return str(parsed), str(network)


class LoginRateLimiter:
    """Failed password checks, counted per client address and per IPv6 /64.

    Five per address stop one device guessing. An IPv6 host chooses its own
    addresses inside its /64, so for IPv6 the address alone is a budget renewed
    at will; the /64 is the bound it cannot renew. Every host on a SLAAC network
    shares that /64, and every link-local client shares ``fe80::/64`` whatever
    its link, so the /64 gets a larger budget of its own: twenty, four devices'
    worth of typos. Five on one device lock out that device, not the network.

    There is no ceiling across sources. One dual-stack host holds four of them
    (IPv4, global, unique-local and link-local IPv6), and a ceiling it can fill
    on its own locks the operator out with only the physical console left.

    Windows run on the monotonic clock, so a wall-clock step neither extends a
    lockout nor ends one.
    """

    def __init__(
        self,
        *,
        max_failures=DEFAULT_MAX_FAILURES,
        max_network_failures=DEFAULT_MAX_NETWORK_FAILURES,
        window_seconds=DEFAULT_FAILURE_WINDOW,
        max_entries=1024,
        time_fn=None,
    ):
        self.max_failures = int(max_failures)
        self.max_network_failures = int(max_network_failures)
        self.window_seconds = int(window_seconds)
        self.max_entries = int(max_entries)
        self._time = time_fn or time.monotonic
        self.failures = {}

    def limited(self, key):
        return self._wait(key) > 0

    def record_failure(self, key):
        """Record one failure and name it, so an attempt nobody judged can be
        taken back."""

        stamp = self._time()
        for counter, _ in self._counters(key):
            attempts = self._active(counter)
            attempts.append(stamp)
            self.failures[counter] = attempts
        self._evict()
        return stamp

    def forget(self, key, stamp):
        """Take back one recorded attempt. Missing is not an error."""

        if stamp is None:
            return
        for counter, _ in self._counters(key):
            attempts = self.failures.get(counter)
            if not attempts or stamp not in attempts:
                continue
            attempts.remove(stamp)
            if attempts:
                self.failures[counter] = attempts
            else:
                self.failures.pop(counter, None)

    def _counters(self, key):
        """Each counter ``key`` is charged to, with its budget."""

        address, network = throttle_sources(key)
        counters = [(address, self.max_failures)]
        if network is not None:
            counters.append((network, self.max_network_failures))
        return counters

    def _evict(self):
        """Expired keys first: evicting a live one flushes somebody's lockout."""

        if len(self.failures) <= self.max_entries:
            return
        cutoff = self._time() - self.window_seconds
        for key in [
            key
            for key, attempts in self.failures.items()
            if not attempts or max(attempts) <= cutoff
        ]:
            self.failures.pop(key, None)
        if len(self.failures) <= self.max_entries:
            return
        oldest = sorted(self.failures.items(), key=lambda item: max(item[1] or [0]))
        for stale, _ in oldest[: len(self.failures) - self.max_entries]:
            self.failures.pop(stale, None)

    def reset(self, key):
        """Clear the client address. Its /64 keeps counting: a correct password
        proves nothing about the other addresses in it."""

        address, _ = throttle_sources(key)
        self.failures.pop(address, None)

    def retry_after(self, key):
        return max(0, int(self._wait(key)))

    def held_by_network(self, key):
        """Whether only the IPv6 /64 holds ``key`` back, not its own address."""

        counters = self._counters(key)
        if len(counters) < 2:
            return False
        (address, budget), (network, network_budget) = counters
        return (
            self._until_below(self._active(address), budget) <= 0
            and self._until_below(self._active(network), network_budget) > 0
        )

    def _wait(self, key):
        """Seconds until ``key`` may try again; zero or less when it may now."""

        return max(
            self._until_below(self._active(counter), budget)
            for counter, budget in self._counters(key)
        )

    def _until_below(self, stamps, limit):
        if len(stamps) < limit:
            return 0
        deciding = sorted(stamps)[len(stamps) - limit]
        return deciding + self.window_seconds - self._time()

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
    a name can be re-created with a different uid, while the containers keep
    running as the uid baked into the compose file. The owner of the root is the
    identity -- the same rule the deployment bootstrap already applies.

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
