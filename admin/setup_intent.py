# SPDX-License-Identifier: AGPL-3.0-or-later
"""Short-lived proof that an authenticated user selected Fresh Setup."""

import hashlib
import json
import secrets
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from admin.install_state import PATH_SETUP_NEW, detect_install_state

DEFAULT_SETUP_INTENT_TTL_SECONDS = 20 * 60
DEFAULT_CONSUMED_TTL_SECONDS = 5 * 60
MAX_SETUP_INTENTS = 128

_REQUIRED_MESSAGE = "Confirm Fresh Setup before changing setup state."
_EXPIRED_MESSAGE = "The Fresh Setup confirmation expired. Confirm it again."
_CHANGED_MESSAGE = "The installation state changed. Confirm Fresh Setup again."
_CONSUMED_MESSAGE = "Confirm Fresh Setup again before starting another operation."


class SetupIntentError(Exception):
    def __init__(self, reason, message, status=409):
        super().__init__(message)
        self.reason = reason
        self.message = message
        self.status = status


@dataclass(frozen=True)
class SetupIntent:
    intent_id: str
    session_id: str
    action: str
    install_state_fingerprint: str
    created_at: float
    expires_at: float


_HASH_CHUNK_BYTES = 65536


def sha256_file(path):
    """SHA-256 a file in fixed-size chunks, never loading it fully into memory.

    Only the digest is ever retained, so a config or compose file that carries
    secrets contributes its identity to the fingerprint without its content being
    stored. Raises ``OSError`` when an existing file cannot be read so the caller
    can treat that fail-closed rather than assume the file is unchanged.
    """

    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(_HASH_CHUNK_BYTES), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _file_digest_or_absent(path):
    """Digest an existing file, or ``None`` when it is absent (never a directory)."""

    candidate = Path(path)
    if not candidate.is_file():
        return None
    return sha256_file(candidate)


def default_runtime_state_fingerprint(base_dir=None, docker=None):
    """Non-sensitive EMS container/image identity for the setup fingerprint.

    Returns a deterministic ``{"available": False}`` when Docker cannot be
    queried so an intent issued and validated while Docker is down stays valid.
    Only existence, running state and the immutable image id are reported —
    never container environments or secrets.
    """

    try:
        from admin.install_context import detect_install_context
        from admin.ems_tool import ems_container_name

        cli = docker
        if cli is None:
            from admin.deployment import DockerCli

            cli = DockerCli()
        context = detect_install_context(base_dir=base_dir)
        name = ems_container_name(context)
        info = cli.inspect_container(name)
    except Exception:
        return {"available": False}
    if not info:
        return {
            "available": True,
            "container_name": name,
            "container_exists": False,
            "container_running": False,
            "image_id": None,
        }
    image_id = None
    try:
        image_id = cli.inspect_container_image_id(name)
    except Exception:
        image_id = None
    return {
        "available": True,
        "container_name": name,
        "container_exists": True,
        "container_running": str(info.get("status") or "").lower() == "running",
        "image_id": image_id or info.get("image") or None,
    }


def installation_state_fingerprint(base_dir=None, runtime_provider=None):
    """Hash install-state identity — layout, file digests and runtime — no content.

    Beyond mere file existence this folds in the SHA-256 of the standard config,
    a legacy root config and docker-compose.yml (when present) plus the running
    EMS container/image identity, so a manual edit of any of those invalidates a
    Fresh Setup intent. Only digests and non-sensitive status values are hashed;
    file contents and secrets are never stored. A read error on an existing
    security-relevant file propagates as ``OSError`` for fail-closed handling.
    """

    state = detect_install_state(base_dir=base_dir)
    path_info = state.paths
    provider = runtime_provider or (lambda: default_runtime_state_fingerprint(base_dir))
    facts = {
        "state": state.state,
        "config_layout_state": state.config_layout_state,
        "standard_config": _file_digest_or_absent(path_info["standard_config"]),
        "legacy_config": _file_digest_or_absent(path_info["legacy_config"]),
        "compose": _file_digest_or_absent(path_info["compose"]),
        "data_exists": Path(path_info["data"]).is_dir(),
        "runtime": provider(),
    }
    encoded = json.dumps(facts, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


class SetupIntentStore:
    """Thread-safe process-local setup intents that cannot cross sessions.

    Session binding prevents an intent copied from another authenticated browser
    from authorizing a Fresh Setup mutation here.
    """

    def __init__(
        self,
        *,
        ttl_seconds=DEFAULT_SETUP_INTENT_TTL_SECONDS,
        consumed_ttl_seconds=DEFAULT_CONSUMED_TTL_SECONDS,
        max_records=MAX_SETUP_INTENTS,
        time_fn=None,
        state_fingerprint=None,
        id_factory=None,
    ):
        self.ttl_seconds = int(ttl_seconds)
        if self.ttl_seconds <= 0:
            raise ValueError("setup intent ttl must be positive")
        self.consumed_ttl_seconds = int(consumed_ttl_seconds)
        if self.consumed_ttl_seconds <= 0:
            raise ValueError("consumed tombstone ttl must be positive")
        self.max_records = int(max_records)
        if self.max_records <= 0:
            raise ValueError("setup intent store limit must be positive")
        self.time_fn = time_fn or time.time
        self.state_fingerprint = state_fingerprint or installation_state_fingerprint
        self.id_factory = id_factory or (lambda: secrets.token_urlsafe(32))
        self._records = {}
        # A claimed intent leaves a short-lived tombstone (intent_id -> (session,
        # expiry)) so a duplicate or racing request reliably learns the intent was
        # already used instead of a bare "unknown intent". Bounded by its own TTL.
        self._consumed = {}
        self._lock = threading.Lock()

    def issue(self, *, session_id, action=PATH_SETUP_NEW):
        if not session_id:
            raise ValueError("an authenticated session id is required")
        now = self.time_fn()
        record = SetupIntent(
            intent_id=self.id_factory(),
            session_id=session_id,
            action=action,
            install_state_fingerprint=self.state_fingerprint(),
            created_at=now,
            expires_at=now + self.ttl_seconds,
        )
        with self._lock:
            self._prune_locked(now)
            self._invalidate_session_locked(session_id)
            self._records[record.intent_id] = record
            self._enforce_limit_locked()
        return record

    def validate(self, intent_id, *, session_id, action=PATH_SETUP_NEW):
        """Read-only check that ``intent_id`` is still usable; never consumes it."""

        with self._lock:
            now = self.time_fn()
            record = self._resolve_locked(intent_id, session_id, action, now)
            self._prune_locked(now)
            return record

    def claim(self, intent_id, *, session_id, action=PATH_SETUP_NEW):
        """Atomically consume ``intent_id`` for exactly one setup mutation.

        The status, expiry, fingerprint and consume step all run under one lock so
        two concurrent mutations can never both proceed on the same intent: the
        second sees ``setup_intent_consumed``. An intent is never re-released, even
        if the mutation it authorized later fails.
        """

        with self._lock:
            now = self.time_fn()
            record = self._resolve_locked(intent_id, session_id, action, now)
            self._records.pop(intent_id, None)
            self._consumed[intent_id] = (
                session_id,
                now + self.consumed_ttl_seconds,
            )
            self._prune_locked(now)
            self._enforce_limit_locked()
        return record

    def _resolve_locked(self, intent_id, session_id, action, now):
        if not isinstance(intent_id, str) or not intent_id:
            raise SetupIntentError("setup_intent_required", _REQUIRED_MESSAGE)
        record = self._records.get(intent_id)
        if record is None:
            if intent_id in self._consumed:
                raise SetupIntentError("setup_intent_consumed", _CONSUMED_MESSAGE)
            raise SetupIntentError("setup_intent_required", _REQUIRED_MESSAGE)
        if record.session_id != session_id or record.action != action:
            raise SetupIntentError("setup_intent_required", _REQUIRED_MESSAGE)
        if record.expires_at <= now:
            self._records.pop(intent_id, None)
            raise SetupIntentError("setup_intent_expired", _EXPIRED_MESSAGE)
        try:
            current_fingerprint = self.state_fingerprint()
        except OSError:
            # A security-relevant file that exists but cannot be read is treated
            # fail-closed: refuse the intent rather than assume it is unchanged.
            self._records.pop(intent_id, None)
            raise SetupIntentError("setup_state_changed", _CHANGED_MESSAGE)
        if record.install_state_fingerprint != current_fingerprint:
            self._records.pop(intent_id, None)
            raise SetupIntentError("setup_state_changed", _CHANGED_MESSAGE)
        return record

    def invalidate(self, intent_id):
        if not intent_id:
            return
        with self._lock:
            self._records.pop(intent_id, None)

    def invalidate_session(self, session_id):
        if not session_id:
            return
        with self._lock:
            self._invalidate_session_locked(session_id)

    def _invalidate_session_locked(self, session_id):
        stale = [
            intent_id
            for intent_id, record in self._records.items()
            if record.session_id == session_id
        ]
        for intent_id in stale:
            self._records.pop(intent_id, None)
        consumed = [
            intent_id
            for intent_id, (owner, _expiry) in self._consumed.items()
            if owner == session_id
        ]
        for intent_id in consumed:
            self._consumed.pop(intent_id, None)

    def _prune_locked(self, now):
        expired = [
            intent_id
            for intent_id, record in self._records.items()
            if record.expires_at <= now
        ]
        for intent_id in expired:
            self._records.pop(intent_id, None)
        stale_tombstones = [
            intent_id
            for intent_id, (_owner, expiry) in self._consumed.items()
            if expiry <= now
        ]
        for intent_id in stale_tombstones:
            self._consumed.pop(intent_id, None)

    def _enforce_limit_locked(self):
        # Expired records are already pruned before this runs; when still over the
        # limit, drop the oldest unclaimed records first. Consumed tombstones are
        # bounded separately by the same ceiling so the store never grows without
        # a hard cap.
        overflow = len(self._records) - self.max_records
        if overflow > 0:
            oldest = sorted(
                self._records.items(), key=lambda item: item[1].created_at
            )
            for intent_id, _record in oldest[:overflow]:
                self._records.pop(intent_id, None)
        tombstone_overflow = len(self._consumed) - self.max_records
        if tombstone_overflow > 0:
            oldest_tombstones = sorted(
                self._consumed.items(), key=lambda item: item[1][1]
            )
            for intent_id, _meta in oldest_tombstones[:tombstone_overflow]:
                self._consumed.pop(intent_id, None)
