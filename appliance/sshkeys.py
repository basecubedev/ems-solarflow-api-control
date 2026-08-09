# SPDX-License-Identifier: AGPL-3.0-or-later
"""SSH public-key parsing and atomic ``authorized_keys`` maintenance.

Private keys are refused before anything is parsed: the appliance never asks
for one and must not store one by accident. ``authorized_keys`` is replaced
atomically so an interrupted write can never leave an account without its keys.
"""

import base64
import binascii
import hashlib
import os
import struct
from dataclasses import dataclass
from pathlib import Path

from appliance.validation import (
    MAX_PUBLIC_KEY_LENGTH,
    SUPPORTED_KEY_TYPES,
    ValidationError,
)

MAX_COMMENT_LENGTH = 128
SSH_DIR_MODE = 0o700
AUTHORIZED_KEYS_MODE = 0o600
PRIVATE_KEY_MARKERS = ("PRIVATE KEY", "PuTTY-User-Key-File")


@dataclass(frozen=True)
class PublicKey:
    key_type: str
    blob: str
    comment: str
    fingerprint: str

    @property
    def line(self):
        base = f"{self.key_type} {self.blob}"
        return f"{base} {self.comment}" if self.comment else base

    def to_dict(self):
        return {
            "key_type": self.key_type,
            "comment": self.comment,
            "fingerprint": self.fingerprint,
        }


def fingerprint_of(blob):
    raw = base64.b64decode(blob, validate=True)
    digest = hashlib.sha256(raw).digest()
    return "SHA256:" + base64.b64encode(digest).decode("ascii").rstrip("=")


def _declared_blob_type(raw):
    if len(raw) < 4:
        raise ValidationError("invalid_public_key", "key body is truncated")
    (length,) = struct.unpack(">I", raw[:4])
    if length <= 0 or length > 64 or len(raw) < 4 + length:
        raise ValidationError("invalid_public_key", "key body is malformed")
    return raw[4 : 4 + length].decode("ascii", errors="replace")


def validate_public_key(value):
    """Parse one OpenSSH public key line into a :class:`PublicKey`."""

    if not isinstance(value, str):
        raise ValidationError("invalid_public_key", "public key must be a string")

    text = value.strip()
    if not text:
        raise ValidationError("empty_public_key", "public key must not be empty")
    if len(text) > MAX_PUBLIC_KEY_LENGTH:
        raise ValidationError("public_key_too_large", "public key exceeds the size limit")
    if any(marker in text for marker in PRIVATE_KEY_MARKERS):
        raise ValidationError("private_key_rejected", "this is a private key; never upload one")
    if "\n" in text or "\r" in text:
        raise ValidationError("invalid_public_key", "public key must be a single line")

    parts = text.split(None, 2)
    if len(parts) < 2:
        raise ValidationError("invalid_public_key", "public key must be '<type> <base64> [comment]'")

    key_type, blob = parts[0], parts[1]
    comment = parts[2].strip() if len(parts) > 2 else ""

    if key_type not in SUPPORTED_KEY_TYPES:
        raise ValidationError(
            "unsupported_key_type", f"{key_type} is not an accepted key type on this appliance"
        )

    try:
        raw = base64.b64decode(blob, validate=True)
    except (binascii.Error, ValueError):
        raise ValidationError("invalid_public_key", "key body is not valid base64")

    if _declared_blob_type(raw) != key_type:
        raise ValidationError("invalid_public_key", "key body does not match the declared key type")

    if len(comment) > MAX_COMMENT_LENGTH:
        comment = comment[:MAX_COMMENT_LENGTH]
    comment = "".join(char for char in comment if char.isprintable())

    return PublicKey(
        key_type=key_type, blob=blob, comment=comment, fingerprint=fingerprint_of(blob)
    )


def parse_authorized_keys(text):
    keys = []
    for line in (text or "").splitlines():
        entry = line.strip()
        if not entry or entry.startswith("#"):
            continue
        try:
            keys.append(validate_public_key(entry))
        except ValidationError:
            continue
    return keys


def render_authorized_keys(keys):
    return "".join(f"{key.line}\n" for key in keys)


class AuthorizedKeysStore:
    """Read and atomically rewrite one account's ``authorized_keys``."""

    def __init__(self, home, *, owner_uid=None, owner_gid=None):
        self.home = Path(home)
        self.owner_uid = owner_uid
        self.owner_gid = owner_gid

    @property
    def ssh_dir(self):
        return self.home / ".ssh"

    @property
    def path(self):
        return self.ssh_dir / "authorized_keys"

    def list(self):
        try:
            return parse_authorized_keys(self.path.read_text(encoding="utf-8", errors="replace"))
        except FileNotFoundError:
            return []

    def _own(self, target):
        if self.owner_uid is None or self.owner_gid is None:
            return
        try:
            os.chown(target, self.owner_uid, self.owner_gid)
        except (OSError, PermissionError):
            pass

    def _write(self, keys):
        # The directory stays root-owned on purpose: an account that cannot write
        # it cannot replace the key file or the package's ownership marker beside
        # it. Only the key file itself is handed to the account.
        self.ssh_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(self.ssh_dir, SSH_DIR_MODE)

        tmp = self.ssh_dir / f".authorized_keys.{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            handle.write(render_authorized_keys(keys))
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, AUTHORIZED_KEYS_MODE)
        self._own(tmp)
        os.replace(tmp, self.path)
        os.chmod(self.path, AUTHORIZED_KEYS_MODE)
        self._own(self.path)
        return keys

    def add(self, public_key):
        key = public_key if isinstance(public_key, PublicKey) else validate_public_key(public_key)
        existing = self.list()
        if any(item.fingerprint == key.fingerprint for item in existing):
            raise ValidationError("duplicate_public_key", "this key is already authorized")
        self._write(existing + [key])
        return key

    def remove(self, fingerprint):
        existing = self.list()
        remaining = [item for item in existing if item.fingerprint != fingerprint]
        if len(remaining) == len(existing):
            raise ValidationError("unknown_public_key", "no authorized key with that fingerprint")
        self._write(remaining)
        return len(existing) - len(remaining)

    def revoke_all(self):
        removed = len(self.list())
        self._write([])
        return removed
