# SPDX-License-Identifier: AGPL-3.0-or-later
"""At-rest storage for the Admin-only Zendure API cloud token.

The raw token is encrypted with a locally generated key (``cryptography`` /
Fernet) and both files are written ``0600`` best-effort. The token is never
returned to the browser and never logged; only redaction-safe metadata (last
check time, status, device count, broker host/port, TLS mode) is exposed.

This module owns exactly one secret. It never touches the EMS config, never
stores the fetched MQTT password, and degrades to a clear status (rather than
crashing) when file permissions cannot be enforced.
"""

import base64
import json
import os
import stat
from pathlib import Path

from admin.models import utc_now_iso
from admin.releases import default_admin_data_dir

TOKEN_FILENAME = "zendure-cloud-token.json"
KEY_FILENAME = ".admin-secret-key"

_ENCRYPT_ERROR_MESSAGE = (
    "Could not encrypt the credential. Check the Admin secret-key file and "
    "permissions."
)

_METADATA_FIELDS = (
    "last_checked",
    "last_status",
    "last_error",
    "last_device_count",
    "last_broker",
    "tls_mode",
    "saved_at",
)


class SecretStoreError(Exception):
    """A save/delete failure whose message is safe to show to the operator."""


def _empty_metadata():
    return {field: None for field in _METADATA_FIELDS}


def _chmod_best_effort(path):
    try:
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
        return True
    except (OSError, NotImplementedError):
        return False


class ZendureTokenStore:
    """Encrypted single-token store plus its non-secret discovery metadata."""

    def __init__(self, data_dir=None):
        base = Path(data_dir) if data_dir else default_admin_data_dir()
        self.state_dir = base / "state"
        self.token_path = self.state_dir / TOKEN_FILENAME
        self.key_path = self.state_dir / KEY_FILENAME

    # --- public API ------------------------------------------------------

    def token_saved(self):
        record = self._read_record()
        return bool(record and record.get("token"))

    def save_token(self, token):
        if not isinstance(token, str) or not token.strip():
            raise SecretStoreError("A non-empty Zendure token is required.")
        blob, encrypted = self._encrypt(token.strip())
        record = {
            "version": 1,
            "encrypted": encrypted,
            "token": blob,
            "metadata": {**_empty_metadata(), "saved_at": utc_now_iso()},
        }
        perms_ok = self._write_record(record)
        return {
            "token_saved": True,
            "encrypted": encrypted,
            "permissions_enforced": perms_ok,
        }

    def load_token(self):
        record = self._read_record()
        if not record or not record.get("token"):
            return None
        try:
            return self._decrypt(record["token"], bool(record.get("encrypted")))
        except Exception:
            return None

    def delete_token(self):
        removed = False
        try:
            self.token_path.unlink()
            removed = True
        except FileNotFoundError:
            removed = False
        except OSError as exc:
            raise SecretStoreError("Could not remove the stored Zendure token.") from exc
        return {"token_saved": False, "removed": removed}

    def settings(self):
        """Redacted status only; the raw token is never included."""

        record = self._read_record()
        metadata = _empty_metadata()
        if record and isinstance(record.get("metadata"), dict):
            for field in _METADATA_FIELDS:
                if field in record["metadata"]:
                    metadata[field] = record["metadata"][field]
        payload = {
            "token_saved": bool(record and record.get("token")),
            "encrypted": bool(record and record.get("encrypted")),
        }
        payload.update(
            {
                "last_checked": metadata["last_checked"],
                "last_status": metadata["last_status"],
                "last_error": metadata["last_error"],
                "last_device_count": metadata["last_device_count"],
                "last_broker": metadata["last_broker"],
                "tls_mode": metadata["tls_mode"],
            }
        )
        return payload

    def update_metadata(self, **fields):
        record = self._read_record()
        if not record:
            return
        metadata = record.get("metadata")
        if not isinstance(metadata, dict):
            metadata = _empty_metadata()
        for key, value in fields.items():
            if key in _METADATA_FIELDS:
                metadata[key] = value
        record["metadata"] = metadata
        self._write_record(record)

    # --- storage ---------------------------------------------------------

    def _read_record(self):
        try:
            raw = self.token_path.read_text(encoding="utf-8")
        except (FileNotFoundError, OSError):
            return None
        try:
            record = json.loads(raw)
        except ValueError:
            return None
        return record if isinstance(record, dict) else None

    def _write_record(self, record):
        try:
            self.state_dir.mkdir(parents=True, exist_ok=True)
            try:
                os.chmod(self.state_dir, stat.S_IRWXU)
            except (OSError, NotImplementedError):
                pass
            tmp = self.token_path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(record), encoding="utf-8")
            perms_ok = _chmod_best_effort(tmp)
            os.replace(tmp, self.token_path)
        except OSError as exc:
            raise SecretStoreError("Could not save the Zendure token.") from exc
        return perms_ok

    # --- encryption ------------------------------------------------------

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
            self.state_dir.mkdir(parents=True, exist_ok=True)
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

    def _encrypt(self, token):
        # Fail closed: any missing-key, key-IO or cipher failure raises a safe
        # store error instead of downgrading the secret to reversible base64.
        try:
            from cryptography.fernet import Fernet

            fernet = Fernet(self._load_or_create_key())
            return fernet.encrypt(token.encode("utf-8")).decode("ascii"), True
        except Exception as exc:
            raise SecretStoreError(_ENCRYPT_ERROR_MESSAGE) from exc

    def _decrypt(self, blob, encrypted):
        if not encrypted:
            return base64.b64decode(blob.encode("ascii")).decode("utf-8")
        from cryptography.fernet import Fernet

        fernet = Fernet(self.key_path.read_bytes())
        return fernet.decrypt(blob.encode("ascii")).decode("utf-8")
