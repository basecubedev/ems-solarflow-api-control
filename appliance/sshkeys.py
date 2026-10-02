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
import secrets
import stat
import struct
from dataclasses import dataclass
from pathlib import Path

from appliance.validation import (
    MAX_PUBLIC_KEY_LENGTH,
    SUPPORTED_KEY_TYPES,
    ValidationError,
)

MAX_COMMENT_LENGTH = 128
# sshd opens authorized_keys as the account, so the account's group needs read
# and search; root ownership is what stops the account authorising itself.
SSH_DIR_MODE = 0o750
AUTHORIZED_KEYS_MODE = 0o640
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


def unparsed_lines(text):
    """Lines this parser does not understand, which are not this file's to drop.

    OpenSSH accepts more than ``validate_public_key`` does: an options prefix
    such as ``from="10.0.0.1",no-pty``, a ``cert-authority`` line, a key type
    outside SUPPORTED_KEY_TYPES, and the operator's own comments. Rewriting the
    file from the parsed list alone deleted every one of them on the next add or
    remove -- silently, and with a success message. On ``ems-shell``, the
    account that reaches root and exists for the case where the console is the
    broken thing, that is an operator locked out by an operation that reported
    success.
    """

    kept = []
    for line in (text or "").splitlines():
        entry = line.strip()
        if not entry:
            continue
        if entry.startswith("#"):
            kept.append(line)
            continue
        try:
            validate_public_key(entry)
        except ValidationError:
            kept.append(line)
    return kept


def foreign_key_lines(text):
    """Unparsed lines that are not comments, which sshd will still honour.

    The attribution gate works on parsed keys, so an options-prefixed or
    certificate line was invisible to it: the subsystem reported every key
    attributed while sshd was accepting one nothing here could account for.
    """

    return [line for line in unparsed_lines(text) if not line.strip().startswith("#")]


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
        return parse_authorized_keys(self._text())

    def _open_ssh_dir(self, *, create):
        """The ``.ssh`` directory as a descriptor, never through a symlink."""

        if create:
            self.home.mkdir(parents=True, exist_ok=True)
            try:
                os.mkdir(self.ssh_dir, SSH_DIR_MODE)
            except FileExistsError:
                pass
        try:
            return os.open(self.ssh_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        except FileNotFoundError:
            if create:
                raise
            return None
        except OSError:
            raise ValidationError(
                "ssh_directory_unsafe", f"{self.ssh_dir} is not a real directory"
            )

    def _text(self):
        directory = self._open_ssh_dir(create=False)
        if directory is None:
            return ""
        try:
            return self._read_at(directory)
        finally:
            os.close(directory)

    def _read_at(self, directory):
        try:
            descriptor = os.open(
                self.path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory
            )
        except FileNotFoundError:
            return ""
        except OSError:
            raise ValidationError("authorized_keys_unsafe", f"{self.path} is not a regular file")
        with os.fdopen(descriptor, "rb") as handle:
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                raise ValidationError(
                    "authorized_keys_unsafe", f"{self.path} is not a regular file"
                )
            return handle.read().decode("utf-8", errors="replace")

    def unparsed(self):
        """What is in the file that this store does not manage."""

        return unparsed_lines(self._text())

    def _own(self, descriptor):
        # Ownership boundary: root owns the key material, the account's group
        # only reads it. An account that cannot write here cannot authorise
        # itself, replace the key file, or touch the marker beside it.
        if self.owner_gid is None:
            return
        try:
            os.fchown(descriptor, 0, self.owner_gid)
        except (OSError, PermissionError):
            pass

    def _write(self, keys, *, preserve=()):
        directory = self._open_ssh_dir(create=True)
        try:
            os.fchmod(directory, SSH_DIR_MODE)
            self._own(directory)
            tmp = f".authorized_keys.{secrets.token_hex(8)}.tmp"
            descriptor = os.open(
                tmp,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                AUTHORIZED_KEYS_MODE,
                dir_fd=directory,
            )
            try:
                with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                    for line in preserve:
                        handle.write(line.rstrip("\n") + "\n")
                    handle.write(render_authorized_keys(keys))
                    handle.flush()
                    os.fchmod(handle.fileno(), AUTHORIZED_KEYS_MODE)
                    self._own(handle.fileno())
                    os.fsync(handle.fileno())
                os.replace(tmp, self.path.name, src_dir_fd=directory, dst_dir_fd=directory)
            except BaseException:
                try:
                    os.unlink(tmp, dir_fd=directory)
                except OSError:
                    pass
                raise
        finally:
            os.close(directory)
        return keys

    def add(self, public_key):
        key = public_key if isinstance(public_key, PublicKey) else validate_public_key(public_key)
        existing = self.list()
        if any(item.fingerprint == key.fingerprint for item in existing):
            raise ValidationError("duplicate_public_key", "this key is already authorized")
        self._write(existing + [key], preserve=self.unparsed())
        return key

    def remove(self, fingerprint):
        existing = self.list()
        remaining = [item for item in existing if item.fingerprint != fingerprint]
        if len(remaining) == len(existing):
            raise ValidationError("unknown_public_key", "no authorized key with that fingerprint")
        self._write(remaining, preserve=self.unparsed())
        return len(existing) - len(remaining)

    def revoke_all(self):
        """Everything goes, including what this store cannot read.

        The one place where preserving an unparsed line would defeat the point:
        the operator asked for every way in to be closed, and a line that grants
        access is a way in whether or not this parser understands it. The count
        says how many lines went, not how many of them were parsable.
        """

        removed = len(self.list()) + len(self.unparsed())
        self._write([])
        return removed
