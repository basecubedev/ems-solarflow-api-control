# SPDX-License-Identifier: AGPL-3.0-or-later
"""EMS-owned persistent credential store under ``config/secrets/``.

This is the long-term home for setup credentials that a later EMS runtime may
need to read (the Zendure Cloud API token and per-broker local MQTT
credentials). It supersedes the Admin-local Zendure token store: credentials
live beside the EMS config instead of under ``data/admin/state``.

Secrets are encrypted at rest with a locally generated key (``cryptography`` /
Fernet) and every secret/key file is written ``0600`` best-effort. Raw secrets
are never logged, never returned to the browser, and never written into
``config/config.json``; only redaction-safe status (configured yes/no,
credential ref, encryption flag) leaves this module.
"""

import base64
import json
import os
import re
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path

from admin.models import utc_now_iso
from ems.mqtt_credentials import (
    MQTT_CREDENTIAL_REQUIRED_FIELDS,
    missing_mqtt_credential_fields,
)
from ems.zendure_mqtt.config_entries import SOURCE_ZENDURE_CLOUD_MQTT

SECRETS_DIRNAME = "secrets"

# The one runtime credential reference the Zendure cloud broker profile uses in
# generated configs; the record lives at config/secrets/mqtt-<ref>.json.
ZENDURE_CLOUD_CREDENTIALS_REF = "zendure-cloud"
KEY_FILENAME = ".secret-key"
ZENDURE_SECRET_FILENAME = "zendure-cloud.json"

_ENCRYPT_ERROR_MESSAGE = (
    "Could not encrypt the credential. Check the Admin secret-key file and "
    "permissions."
)

_MQTT_REF_RE = re.compile(r"[^a-z0-9_-]+")

# A managed secrets record is a flat "<canonical>.json" basename. The anchored
# allowlist admits no path separator or ".." sequence, so it is the sanitizer
# barrier for the secrets-dir path built from it.
_MANAGED_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*\.json$")

_ZENDURE_METADATA_FIELDS = (
    "last_checked",
    "last_status",
    "last_error",
    "last_device_count",
    "last_broker",
    "tls_mode",
    "saved_at",
)


class CredentialStoreError(Exception):
    """A save/delete failure whose message is safe to show to the operator."""


class CredentialProvisioningError(CredentialStoreError):
    """A staging/provisioning failure whose internal rollback (partly) failed.

    Raised instead of a plain :class:`CredentialStoreError` when records could
    not be restored/removed during the rollback, so the caller can surface a
    high-severity warning naming ``rollback_failed_refs``. Carries refs only,
    never secret values.
    """

    def __init__(self, message, *, credentials_ref=None, rollback_failed_refs=()):
        super().__init__(message)
        self.credentials_ref = credentials_ref
        self.rollback_failed_refs = tuple(rollback_failed_refs)


class MqttCredentialsRefInvalidError(CredentialStoreError):
    """A configured credentials_ref is not a canonical credential reference.

    Carries the offending (non-secret) reference and a stable machine-readable
    ``code`` so the HTTP layer can block the apply before any credential file
    is created or renamed.
    """

    code = "mqtt_credentials_ref_invalid"

    def __init__(self, message=None, *, credentials_ref=None):
        super().__init__(
            message
            or "A configured MQTT credentials_ref is not a canonical reference."
        )
        self.credentials_ref = credentials_ref


class MqttCredentialSourceConflictError(CredentialStoreError):
    """One credentials_ref is claimed by more than one credential source.

    A reference resolves to a single credential file, so it can back only one
    source (all-local or all-cloud). Carries the reference, the conflicting
    (non-secret) source names and the components that claim it (e.g.
    ``grid_meter``, ``zendure_mqtt_broker``) plus a stable ``code`` so the
    conflict blocks the apply before any credential mutation or network call.
    """

    code = "mqtt_credential_source_conflict"

    def __init__(self, message=None, *, credentials_ref=None, sources=(), consumers=()):
        super().__init__(
            message
            or "A configured MQTT credentials_ref cannot serve multiple sources."
        )
        self.credentials_ref = credentials_ref
        self.sources = list(sources)
        self.consumers = list(consumers)


@dataclass(frozen=True)
class MqttBrokerSecret:
    ref: str
    username: str | None
    password: str | None
    encrypted: bool = True
    label: str | None = None


@dataclass(frozen=True)
class CredentialChange:
    """Pre-change snapshot of one runtime MQTT credential record.

    ``raw_bytes`` holds the file content exactly as it was on disk (encrypted
    blobs, never a decrypted secret), captured without parsing: a rollback
    must restore malformed or future-format records byte for byte, and
    ``existed_before`` comes from the filesystem — never from whether the
    content parsed.
    """

    credentials_ref: str
    existed_before: bool
    raw_bytes: bytes | None = None


@dataclass(frozen=True)
class CredentialValidationResult:
    """Redaction-safe verdict about one runtime credential record.

    ``status`` is ``missing`` (no record), ``valid`` (the Core resolver accepts
    it and it matches every supplied expectation), ``invalid`` (the record
    exists but cannot be used) or ``mismatch`` (usable, but its credentials
    differ from the expected replacement). ``reason`` carries only sanitized
    resolver/shape messages, never a secret value.
    """

    credentials_ref: str
    status: str
    reason: str | None = None


# Admin translates the Core result into a CredentialValidationResult but keeps no
# required-field list of its own: the per-source contract lives in the EMS Core
# (ems.mqtt_credentials.MQTT_CREDENTIAL_REQUIRED_FIELDS), re-exported here only so
# existing Admin importers keep resolving the same object.
RUNTIME_CREDENTIAL_REQUIRED_FIELDS = MQTT_CREDENTIAL_REQUIRED_FIELDS


def validate_resolved_mqtt_credential(*, credentials_ref, source, resolved):
    """Source-specific completeness verdict for one resolved runtime record.

    Delegates the per-source required-field contract to the EMS Core
    (:func:`ems.mqtt_credentials.missing_mqtt_credential_fields`) so credential-
    store validation, provisioning result checks, post-save verification and the
    Setup/Maintenance reuse decisions can never drift from the runtime. ``resolved``
    may be a Core ``MqttCredentials`` result or a plain mapping using the same
    field names. The verdict names missing fields only, never a secret value.
    """

    missing = missing_mqtt_credential_fields(resolved, source=source)
    if missing:
        return CredentialValidationResult(
            credentials_ref,
            "invalid",
            "record is missing required field(s): " + ", ".join(missing),
        )
    return CredentialValidationResult(credentials_ref, "valid")


def default_config_dir():
    """The EMS-owned config directory that hosts ``secrets/``.

    Overridable via ``EMS_CONFIG_DIR`` (tests, non-standard installs). Otherwise
    ``EMS_INSTALL_DIR`` supplies the mounted install root, exactly like
    ``admin.install_context.detect_install_context``: in the read-only Admin
    container ``ems.paths.BASE_DIR`` points at ``/app`` (only the path resolver
    is copied in), so falling back to it there would target an unwritable path.
    With neither env var set (native runs, tests) it defaults to
    ``<ems.paths.BASE_DIR>/config`` so test isolation of ``ems.paths.BASE_DIR``
    still applies.
    """

    configured = os.environ.get("EMS_CONFIG_DIR")
    if configured:
        return Path(configured)
    install_root = os.environ.get("EMS_INSTALL_DIR")
    if install_root:
        return Path(install_root) / "config"
    from ems import paths

    return Path(paths.BASE_DIR) / "config"


def _empty_zendure_metadata():
    return {field: None for field in _ZENDURE_METADATA_FIELDS}


def _chmod_best_effort(path):
    try:
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
        return True
    except (OSError, NotImplementedError):
        return False


class _EncryptedFiles:
    """Shared Fernet key management and managed-record IO for one secrets dir.

    Public record IO takes a *managed filename* (a bare basename), never an
    arbitrary path: :meth:`_managed_path` is the single resolver that proves the
    name stays a direct, non-escaping child of ``secrets_dir`` before any read,
    write or delete. A caller therefore cannot smuggle a full path, a traversal
    or an escaping symlink across this boundary.
    """

    def __init__(self, secrets_dir):
        self.secrets_dir = Path(secrets_dir)
        self.key_path = self.secrets_dir / KEY_FILENAME

    def _managed_path(self, filename):
        """Resolve a managed record filename to a path inside secrets_dir.

        The allowlist match admits only a flat ``<canonical>.json`` basename;
        containment then rejects an escaping symlink via the resolved real path.
        """

        name = os.fspath(filename) if isinstance(filename, (str, os.PathLike)) else ""
        if not _MANAGED_NAME_RE.fullmatch(name) or ".." in name or len(name) > 255:
            raise MqttCredentialsRefInvalidError(
                "credential record name is not a managed basename",
                credentials_ref=name,
            )
        base = os.path.realpath(self.secrets_dir)
        resolved = os.path.realpath(os.path.join(base, name))
        if not resolved.startswith(base + os.sep):
            raise MqttCredentialsRefInvalidError(
                "credential record path escapes the secrets directory",
                credentials_ref=name,
            )
        return Path(resolved)

    def read_record(self, filename):
        try:
            raw = self._managed_path(filename).read_text(encoding="utf-8")
        except (FileNotFoundError, OSError):
            return None
        try:
            record = json.loads(raw)
        except ValueError:
            return None
        return record if isinstance(record, dict) else None

    def _atomic_write(self, path, write_body):
        """Write via a unique temp file in secrets_dir, then atomic replace.

        The temp file gets a unique name (never a deterministic ``<name>.tmp``)
        so concurrent writers cannot collide; ``0600`` permissions are applied
        before the replace and the temp file is always removed on failure, never
        leaving a partial target or a stray temporary file behind.
        """

        self.secrets_dir.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.secrets_dir, stat.S_IRWXU)
        except (OSError, NotImplementedError):
            pass
        tmp = None
        perms_ok = False
        try:
            fd, tmp_name = tempfile.mkstemp(
                prefix=f".{path.name}.", suffix=".tmp", dir=self.secrets_dir
            )
            tmp = Path(tmp_name)
            with os.fdopen(fd, "wb") as handle:
                write_body(handle)
            perms_ok = _chmod_best_effort(tmp)
            os.replace(tmp, path)
            tmp = None
        except OSError as exc:
            raise CredentialStoreError("Could not write the secret file.") from exc
        finally:
            if tmp is not None:
                try:
                    tmp.unlink()
                except OSError:
                    pass
        return perms_ok

    def write_record(self, filename, record):
        path = self._managed_path(filename)
        payload = json.dumps(record).encode("utf-8")
        return self._atomic_write(path, lambda handle: handle.write(payload))

    def write_raw(self, filename, data):
        """Atomically restore exact file bytes (rollback put-back).

        Same unique-temp + atomic-replace path as :meth:`write_record`, but
        without any serialization, so malformed and future-format files survive
        byte for byte.
        """

        path = self._managed_path(filename)
        self._atomic_write(path, lambda handle: handle.write(data))

    def delete_file(self, filename):
        try:
            self._managed_path(filename).unlink()
            return True
        except FileNotFoundError:
            return False
        except OSError as exc:
            raise CredentialStoreError("Could not remove the secret file.") from exc

    def read_named_bytes(self, filename):
        """Read a managed record's raw bytes; raises ``FileNotFoundError`` if absent."""

        return self._managed_path(filename).read_bytes()

    def _load_or_create_key(self):
        try:
            return self.key_path.read_bytes()
        except FileNotFoundError:
            pass
        from cryptography.fernet import Fernet

        # A malformed existing key is never silently replaced (that would orphan
        # every record it encrypted); only a truly absent key is created here.
        key = Fernet.generate_key()
        tmp = self.key_path.with_suffix(".key.tmp")
        try:
            self.secrets_dir.mkdir(parents=True, exist_ok=True)
            tmp.write_bytes(key)
            _chmod_best_effort(tmp)
            os.replace(tmp, self.key_path)
        except OSError:
            try:
                tmp.unlink()
            except OSError:
                pass
            raise
        _chmod_best_effort(self.key_path)
        return key

    def encrypt(self, value):
        # Fail closed: any missing-key, key-IO or cipher failure raises a safe
        # store error instead of downgrading the secret to reversible base64.
        try:
            from cryptography.fernet import Fernet

            fernet = Fernet(self._load_or_create_key())
            return fernet.encrypt(value.encode("utf-8")).decode("ascii"), True
        except Exception as exc:
            raise CredentialStoreError(_ENCRYPT_ERROR_MESSAGE) from exc

    def decrypt(self, blob, encrypted):
        if not encrypted:
            return base64.b64decode(blob.encode("ascii")).decode("utf-8")
        from cryptography.fernet import Fernet

        fernet = Fernet(self.key_path.read_bytes())
        return fernet.decrypt(blob.encode("ascii")).decode("utf-8")


class ZendureCloudTokenStore:
    """Encrypted Zendure token store plus its redaction-safe metadata.

    Interface-compatible with the legacy ``admin.secret_store.ZendureTokenStore``
    so cloud discovery consumes it unchanged, but the file lives under
    ``config/secrets/`` and a legacy Admin-local token is migrated in on first
    read.
    """

    def __init__(self, config_dir=None, *, legacy_store=None):
        base = Path(config_dir) if config_dir else default_config_dir()
        self.secrets_dir = base / SECRETS_DIRNAME
        self.token_path = self.secrets_dir / ZENDURE_SECRET_FILENAME
        self._files = _EncryptedFiles(self.secrets_dir)
        self._legacy_store = legacy_store

    @property
    def key_path(self):
        return self._files.key_path

    def token_saved(self):
        self._migrate_legacy_if_needed()
        record = self._files.read_record(ZENDURE_SECRET_FILENAME)
        return bool(record and record.get("token"))

    def save_token(self, token):
        if not isinstance(token, str) or not token.strip():
            raise CredentialStoreError("A non-empty Zendure token is required.")
        blob, encrypted = self._files.encrypt(token.strip())
        record = {
            "version": 1,
            "encrypted": encrypted,
            "token": blob,
            "metadata": {**_empty_zendure_metadata(), "saved_at": utc_now_iso()},
        }
        perms_ok = self._files.write_record(ZENDURE_SECRET_FILENAME, record)
        return {
            "token_saved": True,
            "encrypted": encrypted,
            "permissions_enforced": perms_ok,
        }

    def load_token(self):
        self._migrate_legacy_if_needed()
        record = self._files.read_record(ZENDURE_SECRET_FILENAME)
        if not record or not record.get("token"):
            return None
        try:
            return self._files.decrypt(record["token"], bool(record.get("encrypted")))
        except Exception:
            return None

    def delete_token(self):
        try:
            removed = self._files.delete_file(ZENDURE_SECRET_FILENAME)
        except CredentialStoreError as exc:
            raise CredentialStoreError(
                "Could not remove the stored Zendure token."
            ) from exc
        return {"token_saved": False, "removed": removed}

    def settings(self):
        """Redacted status only; the raw token is never included."""

        self._migrate_legacy_if_needed()
        record = self._files.read_record(ZENDURE_SECRET_FILENAME)
        metadata = _empty_zendure_metadata()
        if record and isinstance(record.get("metadata"), dict):
            for field in _ZENDURE_METADATA_FIELDS:
                if field in record["metadata"]:
                    metadata[field] = record["metadata"][field]
        return {
            "token_saved": bool(record and record.get("token")),
            "encrypted": bool(record and record.get("encrypted")),
            "credentials_ref": "zendure-cloud" if record and record.get("token") else None,
            "last_checked": metadata["last_checked"],
            "last_status": metadata["last_status"],
            "last_error": metadata["last_error"],
            "last_device_count": metadata["last_device_count"],
            "last_broker": metadata["last_broker"],
            "tls_mode": metadata["tls_mode"],
        }

    def update_metadata(self, **fields):
        record = self._files.read_record(ZENDURE_SECRET_FILENAME)
        if not record:
            return
        metadata = record.get("metadata")
        if not isinstance(metadata, dict):
            metadata = _empty_zendure_metadata()
        for key, value in fields.items():
            if key in _ZENDURE_METADATA_FIELDS:
                metadata[key] = value
        record["metadata"] = metadata
        self._files.write_record(ZENDURE_SECRET_FILENAME, record)

    def _migrate_legacy_if_needed(self):
        """Import a legacy Admin-local token once, without deleting the source.

        Idempotent: skips when the new file already exists or no legacy token is
        available. The legacy file is intentionally left in place.
        """

        if self._legacy_store is None or self.token_path.exists():
            return
        try:
            token = self._legacy_store.load_token()
        except Exception:
            token = None
        if not token:
            return
        try:
            self.save_token(token)
            legacy_settings = self._legacy_store.settings()
        except Exception:
            return
        carry = {
            key: legacy_settings.get(key)
            for key in _ZENDURE_METADATA_FIELDS
            if legacy_settings.get(key) is not None
        }
        if carry:
            self.update_metadata(**carry)


class CredentialStore:
    """Coordinator over the EMS-owned ``config/secrets/`` credential files."""

    def __init__(self, config_dir=None, *, legacy_admin_data_dir=None):
        base = Path(config_dir) if config_dir else default_config_dir()
        self.config_dir = base
        self.secrets_dir = base / SECRETS_DIRNAME
        self._files = _EncryptedFiles(self.secrets_dir)
        legacy_store = None
        if legacy_admin_data_dir is not None:
            from admin.secret_store import ZendureTokenStore

            legacy_store = ZendureTokenStore(legacy_admin_data_dir)
        self.zendure = ZendureCloudTokenStore(base, legacy_store=legacy_store)

    # --- Zendure token (task-named API) ----------------------------------

    def save_zendure_token(self, token):
        self.zendure.save_token(token)

    def load_zendure_token(self):
        return self.zendure.load_token()

    def forget_zendure_token(self):
        self.zendure.delete_token()

    # --- local MQTT broker credentials -----------------------------------

    @staticmethod
    def normalize_ref(ref):
        """Generate a canonical credentials_ref from a user-facing label.

        This is the one *generation* step: a UI/label input is transformed once
        into a canonical reference (lowercase; only ``[a-z0-9_-]``; a leading
        ``[a-z0-9]``). The result always satisfies the Core
        :func:`ems.mqtt_credentials.validate_mqtt_credentials_ref` contract, so
        every later operation can validate it exactly instead of re-normalizing —
        a configured or existing ref is never silently changed again.
        """

        text = _MQTT_REF_RE.sub("-", str(ref or "").strip().lower()).strip("-_")
        return text or "broker"

    @staticmethod
    def _validated_ref(ref):
        """Validate an existing/configured ref through the Core authority.

        Never normalizes: an out-of-contract reference raises
        :class:`MqttCredentialsRefInvalidError` (no file is touched) rather than
        being silently rewritten to a different — possibly colliding — filename.
        """

        from ems.mqtt_credentials import MqttCredentialError, validate_mqtt_credentials_ref

        try:
            return validate_mqtt_credentials_ref(ref)
        except MqttCredentialError as exc:
            raise MqttCredentialsRefInvalidError(credentials_ref=ref) from exc

    def _mqtt_filename(self, ref):
        return f"mqtt-{self._validated_ref(ref)}.json"

    def _mqtt_path(self, ref):
        return self.secrets_dir / self._mqtt_filename(ref)

    @staticmethod
    def _validate_mqtt_auth_pair(username, password):
        username_present = isinstance(username, str) and bool(username.strip())
        password_present = isinstance(password, str) and bool(password)
        if username_present != password_present:
            raise CredentialStoreError(
                "MQTT username and password must both be non-empty or both be omitted."
            )
        if username is not None and not isinstance(username, str):
            raise CredentialStoreError("MQTT username must be a string.")
        if password is not None and not isinstance(password, str):
            raise CredentialStoreError("MQTT password must be a string.")

    def save_mqtt_broker_secret(self, ref, username, password):
        self._validate_mqtt_auth_pair(username, password)
        ref = self._validated_ref(ref)
        record = {"version": 1, "ref": ref}
        if username:
            blob, encrypted = self._files.encrypt(str(username))
            record["username"] = blob
            record["username_encrypted"] = encrypted
        if password:
            blob, encrypted = self._files.encrypt(str(password))
            record["password"] = blob
            record["password_encrypted"] = encrypted
        self._files.write_record(self._mqtt_filename(ref), record)
        return ref

    def save_mqtt_cloud_runtime_secret(
        self, ref, *, username, password, client_id=None, app_key=None
    ):
        """Persist a Core-resolvable Zendure cloud runtime credential record.

        Written atomically (tmp + replace) so a rotation replaces the last
        known-good record in one step. The record carries the encrypted MQTT
        ``username``/``password``/``client_id``/``app_key`` the deviceList
        response yielded — never product/device identifiers — in the same
        ``mqtt-<ref>.json`` shape the Core ``FileMqttCredentialResolver`` reads,
        so EMS resolves it at startup with no Admin dependency. Distinct from the
        Zendure API-token file (``zendure-cloud.json``), which holds the account
        key used only for Admin-side discovery.
        """

        self._validate_mqtt_auth_pair(username, password)
        ref = self._validated_ref(ref)
        record = {
            "version": 1,
            "ref": ref,
            "source": SOURCE_ZENDURE_CLOUD_MQTT,
            "saved_at": utc_now_iso(),
        }
        for field_name, value in (
            ("username", username),
            ("password", password),
            ("client_id", client_id),
            ("app_key", app_key),
        ):
            if value:
                blob, encrypted = self._files.encrypt(str(value))
                record[field_name] = blob
                record[f"{field_name}_encrypted"] = encrypted
        self._files.write_record(self._mqtt_filename(ref), record)
        return ref

    def load_mqtt_broker_secret(self, ref):
        ref = self._validated_ref(ref)
        record = self._files.read_record(self._mqtt_filename(ref))
        if not record:
            return None
        username = self._decrypt_field(record, "username")
        password = self._decrypt_field(record, "password")
        encrypted = bool(
            record.get("username_encrypted") or record.get("password_encrypted")
        )
        return MqttBrokerSecret(
            ref=ref, username=username, password=password, encrypted=encrypted
        )

    def forget_mqtt_broker_secret(self, ref):
        self._files.delete_file(self._mqtt_filename(ref))

    def mqtt_broker_secret_status(self, ref):
        """Redaction-safe status for an API/UI response (never the secret)."""

        ref = self._validated_ref(ref)
        record = self._files.read_record(self._mqtt_filename(ref))
        if not record:
            return {
                "credentials_ref": ref,
                "saved": False,
                "username_configured": False,
                "password_configured": False,
                "encrypted": False,
            }
        return {
            "credentials_ref": ref,
            "saved": True,
            "username_configured": bool(record.get("username")),
            "password_configured": bool(record.get("password")),
            "encrypted": bool(
                record.get("username_encrypted") or record.get("password_encrypted")
            ),
        }

    # --- endpoint-independent discovery credential pool -------------------
    # A separate secret namespace from broker-specific credentials: these are a
    # reusable pool tried against every reachable broker endpoint, not tied to a
    # single host/port. Kept apart so the code never mixes them up with the
    # legacy per-broker connection secrets.

    def _mqtt_discovery_filename(self, ref):
        return f"mqtt-discovery-{self._validated_ref(ref)}.json"

    def _mqtt_discovery_path(self, ref):
        return self.secrets_dir / self._mqtt_discovery_filename(ref)

    def save_mqtt_discovery_secret(self, ref, username, password, label=None):
        self._validate_mqtt_auth_pair(username, password)
        ref = self._validated_ref(ref)
        record = {"version": 1, "ref": ref}
        if label:
            record["label"] = str(label)
        if username:
            blob, encrypted = self._files.encrypt(str(username))
            record["username"] = blob
            record["username_encrypted"] = encrypted
        if password:
            blob, encrypted = self._files.encrypt(str(password))
            record["password"] = blob
            record["password_encrypted"] = encrypted
        self._files.write_record(self._mqtt_discovery_filename(ref), record)
        return ref

    def load_mqtt_discovery_secret(self, ref):
        ref = self._validated_ref(ref)
        record = self._files.read_record(self._mqtt_discovery_filename(ref))
        if not record:
            return None
        return MqttBrokerSecret(
            ref=ref,
            username=self._decrypt_field(record, "username"),
            password=self._decrypt_field(record, "password"),
            encrypted=bool(
                record.get("username_encrypted") or record.get("password_encrypted")
            ),
            label=record.get("label"),
        )

    def forget_mqtt_discovery_secret(self, ref):
        self._files.delete_file(self._mqtt_discovery_filename(ref))

    def mqtt_discovery_secret_status(self, ref):
        """Redaction-safe status for an API/UI response (never the secret)."""

        ref = self._validated_ref(ref)
        record = self._files.read_record(self._mqtt_discovery_filename(ref))
        if not record:
            return {
                "id": ref,
                "label": None,
                "saved": False,
                "username_configured": False,
                "password_configured": False,
                "credentials_encrypted": False,
            }
        return {
            "id": ref,
            "label": record.get("label") or ref,
            "saved": True,
            "username_configured": bool(record.get("username")),
            "password_configured": bool(record.get("password")),
            "credentials_encrypted": bool(
                record.get("username_encrypted") or record.get("password_encrypted")
            ),
        }

    def credential_exists(self, ref):
        """Return whether a runtime MQTT credential record exists.

        Existence alone never proves usability; apply flows must go through
        :meth:`validate_runtime_credential` before reusing a record.
        """

        return self._mqtt_path(ref).is_file()

    def validate_runtime_credential(
        self,
        credentials_ref,
        *,
        expected_source=None,
        expected_username=None,
        expected_password=None,
    ):
        """Validate a runtime record through the Core resolver, never by file.

        The Core ``FileMqttCredentialResolver`` proves a record decodes; the
        source-specific completeness contract on top comes from
        :func:`validate_resolved_mqtt_credential`: a cloud record needs all
        four runtime fields, a local record a full username/password pair —
        a referenced record must never resolve to anonymous credentials. With
        ``expected_source`` the record type is checked as well; with
        ``expected_username``/``expected_password`` a usable record whose
        credentials differ reports ``mismatch`` so the caller can rotate it.
        """

        from ems.mqtt_credentials import FileMqttCredentialResolver, MqttCredentialError

        ref = self._validated_ref(credentials_ref)
        if not self._mqtt_path(ref).is_file():
            return CredentialValidationResult(
                ref, "missing", "no runtime credential record exists"
            )
        try:
            resolved = FileMqttCredentialResolver(self.secrets_dir).resolve(ref)
        except MqttCredentialError as exc:
            return CredentialValidationResult(ref, "invalid", str(exc))
        record = self._files.read_record(self._mqtt_filename(ref)) or {}
        source = record.get("source")
        if expected_source == SOURCE_ZENDURE_CLOUD_MQTT:
            if source != SOURCE_ZENDURE_CLOUD_MQTT:
                return CredentialValidationResult(
                    ref, "invalid", "record is not a Zendure cloud runtime credential"
                )
            completeness = validate_resolved_mqtt_credential(
                credentials_ref=ref,
                source=SOURCE_ZENDURE_CLOUD_MQTT,
                resolved=resolved,
            )
            if completeness.status != "valid":
                return completeness
        elif expected_source is not None and source not in (None, expected_source):
            return CredentialValidationResult(
                ref,
                "invalid",
                f"record belongs to source '{source}', not '{expected_source}'",
            )
        else:
            completeness = validate_resolved_mqtt_credential(
                credentials_ref=ref,
                source=expected_source or source,
                resolved=resolved,
            )
            if completeness.status != "valid":
                return completeness
        if expected_username is not None or expected_password is not None:
            if (resolved.username, resolved.password) != (
                expected_username,
                expected_password,
            ):
                return CredentialValidationResult(
                    ref,
                    "mismatch",
                    "stored credentials differ from the supplied replacement",
                )
        return CredentialValidationResult(ref, "valid")

    def resolve_mqtt_credentials(self, ref):
        """Resolve a runtime record through the store API (Admin-internal)."""

        return self.load_mqtt_broker_secret(ref)

    # --- transactional change tracking ------------------------------------
    # Staging (create / reuse / rotate) for both local and cloud runtime
    # records lives in admin.mqtt_runtime_provisioning and always records a
    # pre-change snapshot here, so one rollback primitive serves every flow.

    def snapshot_mqtt_credential_change(self, ref):
        """Snapshot a runtime record's raw bytes before changing it.

        The file is read without parsing so a rollback can restore malformed
        or future-format records exactly. An existing file that cannot be
        read blocks the change (raising here) instead of being misclassified
        as nonexistent, which would turn the rollback into a deletion.
        """

        ref = self._validated_ref(ref)
        try:
            raw = self._files.read_named_bytes(self._mqtt_filename(ref))
        except FileNotFoundError:
            return CredentialChange(
                credentials_ref=ref, existed_before=False, raw_bytes=None
            )
        except OSError as exc:
            raise CredentialStoreError("Could not read the secret file.") from exc
        return CredentialChange(
            credentials_ref=ref, existed_before=True, raw_bytes=raw
        )

    def rollback_credential_changes(self, changes):
        """Undo credential changes: restore rotated records, delete new ones.

        Applied in reverse order. Restores are byte-exact atomic writes of the
        snapshot's ``raw_bytes``. Returns the refs that could *not* be rolled
        back (so the caller can surface an explicit high-severity error) instead
        of raising, because a rollback failure must never mask the original
        apply failure.
        """

        failed = []
        for change in reversed(list(changes or [])):
            try:
                if change.existed_before and change.raw_bytes is not None:
                    self._files.write_raw(
                        self._mqtt_filename(change.credentials_ref),
                        change.raw_bytes,
                    )
                else:
                    self._files.delete_file(
                        self._mqtt_filename(change.credentials_ref)
                    )
            except CredentialStoreError:
                failed.append(change.credentials_ref)
        return failed

    def delete_temporary_credentials(self, ref):
        self.forget_mqtt_discovery_secret(ref)

    def _decrypt_field(self, record, field):
        blob = record.get(field)
        if not blob:
            return None
        try:
            return self._files.decrypt(blob, bool(record.get(f"{field}_encrypted")))
        except Exception:
            return None
