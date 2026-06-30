# SPDX-License-Identifier: AGPL-3.0-or-later
"""Optional password protection for backups.

A ``.tar.gz`` archive provides no confidentiality, so password-protected
backups are encrypted into a ``.tar.gz.enc`` file. New backups use a versioned
streaming AEAD format (version 2): the archive is encrypted in independently
authenticated chunks, so neither encryption nor decryption ever loads the whole
archive into memory. Older whole-file Fernet backups (version 1) remain
restorable.

Encrypted file layout (version 2)::

    MAGIC (8) | version (1)=2 | header_len (4, big-endian) | header_json |
    chunk record, chunk record, ...

with each chunk record::

    final_flag (1) | ciphertext_len (4, big-endian) | ciphertext

``header_json`` is self-describing (algorithm, KDF, iterations, chunk size,
salt, base nonce) so restore needs no external metadata and auto-detects the
algorithm. Each chunk's associated data binds the magic, version, algorithm,
chunk index and final flag, so a wrong password, tampering, truncation,
reordered chunks and unsupported algorithms all fail cleanly.

This module is import-side-effect-free and never logs or stores passwords.
"""

import base64
import binascii
import json
import os
import struct
import tempfile

from cryptography.exceptions import InvalidTag
from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM, ChaCha20Poly1305
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

MAGIC = b"EMSBKENC"
LEGACY_FERNET_VERSION = 1
STREAMING_VERSION = 2

# ChaCha20-Poly1305 is the default: it is fast on ARM/Raspberry Pi hardware
# without AES acceleration. AES-256-GCM is offered as a standard alternative.
DEFAULT_ALGORITHM = "chacha20-poly1305"
SUPPORTED_AEAD_ALGORITHMS = {
    "chacha20-poly1305": ChaCha20Poly1305,
    "aes-256-gcm": AESGCM,
}

KDF_NAME = "pbkdf2-sha256"
DEFAULT_CHUNK_SIZE = 4 * 1024 * 1024  # 4 MiB
DEFAULT_KDF_ITERATIONS = 300_000
SALT_BYTES = 16
NONCE_PREFIX_BYTES = 8
KEY_BYTES = 32

# Bounds for user-supplied parameters.
MIN_CHUNK_SIZE = 4 * 1024
MAX_CHUNK_SIZE = 64 * 1024 * 1024
MIN_KDF_ITERATIONS = 50_000
MAX_KDF_ITERATIONS = 10_000_000

# Bounds enforced when parsing an untrusted ``.enc`` file, so a hostile or
# corrupted backup fails cleanly instead of forcing a huge allocation, an
# unreasonable PBKDF2 cost, or an unbounded chunk read.
MAX_HEADER_SIZE = 64 * 1024
MAX_CIPHERTEXT_OVERHEAD = 16  # AEAD authentication tag bytes per chunk

# Legacy whole-file Fernet parameters (read-only support).
LEGACY_KDF_ITERATIONS = 200_000
LEGACY_ENCRYPTION_METHOD = "fernet-pbkdf2-sha256"


class BackupPasswordError(Exception):
    """Raised when an encrypted backup cannot be decrypted with the password."""


class BackupFormatError(Exception):
    """Raised for a malformed, truncated or unsupported encrypted backup."""


def encryption_method_label(algorithm):
    return f"{algorithm}-{KDF_NAME}-stream-v{STREAMING_VERSION}"


def create_aead_cipher(algorithm, key):
    try:
        cls = SUPPORTED_AEAD_ALGORITHMS[algorithm]
    except KeyError as exc:
        raise ValueError(f"unsupported encryption algorithm: {algorithm}") from exc
    return cls(key)


def validate_encryption_params(
    algorithm=DEFAULT_ALGORITHM,
    chunk_size=DEFAULT_CHUNK_SIZE,
    iterations=DEFAULT_KDF_ITERATIONS,
):
    """Reject parameters that fall outside the supported restore bounds.

    Encryption and restore share these bounds so the tool can never write an
    encrypted backup its own restore path would later refuse to read. Returns
    the normalised ``(algorithm, chunk_size, iterations)``.
    """

    if algorithm not in SUPPORTED_AEAD_ALGORITHMS:
        raise ValueError(f"unsupported encryption algorithm: {algorithm}")
    chunk_size = int(chunk_size)
    if not MIN_CHUNK_SIZE <= chunk_size <= MAX_CHUNK_SIZE:
        raise ValueError(
            f"chunk_size out of range: {chunk_size} "
            f"(allowed {MIN_CHUNK_SIZE}..{MAX_CHUNK_SIZE})"
        )
    iterations = int(iterations)
    if not MIN_KDF_ITERATIONS <= iterations <= MAX_KDF_ITERATIONS:
        raise ValueError(
            f"iterations out of range: {iterations} "
            f"(allowed {MIN_KDF_ITERATIONS}..{MAX_KDF_ITERATIONS})"
        )
    return algorithm, chunk_size, iterations


def _derive_key(password, salt, iterations):
    if not password:
        raise ValueError("password must not be empty")
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=KEY_BYTES,
        salt=salt,
        iterations=iterations,
    )
    return kdf.derive(password.encode("utf-8"))


def _chunk_nonce(nonce_prefix, index):
    return nonce_prefix + struct.pack(">I", index)


def _chunk_aad(algorithm, index, final):
    return b"|".join((
        MAGIC,
        bytes([STREAMING_VERSION]),
        algorithm.encode("ascii"),
        struct.pack(">I", index),
        b"\x01" if final else b"\x00",
    ))


def _validated_existing_file_path(path, allowed_root=None):
    """Return a canonical, existing regular-file path."""

    if not isinstance(path, (str, os.PathLike)):
        raise BackupFormatError("invalid backup path")
    try:
        path = os.fspath(path)
    except TypeError as exc:
        raise BackupFormatError("invalid backup path") from exc
    if not isinstance(path, str) or not path or "\x00" in path:
        raise BackupFormatError("invalid backup path")
    if os.path.islink(path):
        raise BackupFormatError("backup path must not be a symlink")

    path = os.path.realpath(path)
    if allowed_root is not None:
        if not isinstance(allowed_root, (str, os.PathLike)):
            raise BackupFormatError("invalid allowed backup directory")
        try:
            allowed_root = os.fspath(allowed_root)
        except TypeError as exc:
            raise BackupFormatError("invalid allowed backup directory") from exc
        if (
            not isinstance(allowed_root, str)
            or not allowed_root
            or "\x00" in allowed_root
            or os.path.islink(allowed_root)
        ):
            raise BackupFormatError("invalid allowed backup directory")
        allowed_root = os.path.realpath(allowed_root)
        if not os.path.isdir(allowed_root):
            raise BackupFormatError("allowed backup directory not found")
        try:
            inside_allowed_root = (
                os.path.commonpath([allowed_root, path]) == allowed_root
            )
        except ValueError:
            inside_allowed_root = False
        if not inside_allowed_root:
            raise BackupFormatError("backup path is outside allowed directory")

    if not os.path.isfile(path):
        raise BackupFormatError(f"backup file not found: {path}")
    return path


def _validated_temp_dir(temp_dir):
    if temp_dir is None:
        return None
    if not isinstance(temp_dir, (str, os.PathLike)):
        raise BackupFormatError("invalid temporary directory")
    try:
        temp_dir = os.fspath(temp_dir)
    except TypeError as exc:
        raise BackupFormatError("invalid temporary directory") from exc
    if (
        not isinstance(temp_dir, str)
        or not temp_dir
        or "\x00" in temp_dir
        or os.path.islink(temp_dir)
    ):
        raise BackupFormatError("invalid temporary directory")
    temp_dir = os.path.realpath(temp_dir)
    if not os.path.isdir(temp_dir):
        raise BackupFormatError("temporary directory not found")
    return temp_dir


def is_encrypted_backup(path, allowed_root=None):
    """Return True when ``path`` is an EMS encrypted-backup envelope."""

    try:
        path = _validated_existing_file_path(path, allowed_root=allowed_root)
        with open(path, "rb") as handle:
            return handle.read(len(MAGIC)) == MAGIC
    except (BackupFormatError, OSError):
        return False


# ---------------------------------------------------------------------------
# Encrypt (streaming, version 2)
# ---------------------------------------------------------------------------

def encrypt_file(
    plaintext_path,
    encrypted_path,
    password,
    *,
    algorithm=DEFAULT_ALGORITHM,
    chunk_size=DEFAULT_CHUNK_SIZE,
    iterations=DEFAULT_KDF_ITERATIONS,
):
    """Stream-encrypt ``plaintext_path`` into the ``.enc`` file at ``encrypted_path``."""

    algorithm, chunk_size, iterations = validate_encryption_params(
        algorithm, chunk_size, iterations
    )

    salt = os.urandom(SALT_BYTES)
    nonce_prefix = os.urandom(NONCE_PREFIX_BYTES)
    key = _derive_key(password, salt, iterations)
    cipher = create_aead_cipher(algorithm, key)

    header = {
        "format": "ems-backup-encrypted",
        "version": STREAMING_VERSION,
        "algorithm": algorithm,
        "kdf": KDF_NAME,
        "iterations": int(iterations),
        "chunk_size": chunk_size,
        "salt": base64.b64encode(salt).decode("ascii"),
        "base_nonce": base64.b64encode(nonce_prefix).decode("ascii"),
    }
    header_bytes = json.dumps(header, sort_keys=True).encode("utf-8")

    with open(plaintext_path, "rb") as src, open(encrypted_path, "wb") as out:
        out.write(MAGIC)
        out.write(bytes([STREAMING_VERSION]))
        out.write(struct.pack(">I", len(header_bytes)))
        out.write(header_bytes)

        index = 0
        chunk = src.read(chunk_size)
        while True:
            next_chunk = src.read(chunk_size)
            final = not next_chunk
            ciphertext = cipher.encrypt(
                _chunk_nonce(nonce_prefix, index),
                chunk,
                _chunk_aad(algorithm, index, final),
            )
            out.write(b"\x01" if final else b"\x00")
            out.write(struct.pack(">I", len(ciphertext)))
            out.write(ciphertext)
            if final:
                break
            chunk = next_chunk
            index += 1

    return encrypted_path


# ---------------------------------------------------------------------------
# Decrypt (auto-detects format version)
# ---------------------------------------------------------------------------

def _read_exact(handle, count):
    data = handle.read(count)
    if len(data) != count:
        raise BackupFormatError("truncated encrypted backup")
    return data


def decrypt_file_to_temp(
    encrypted_path, password, temp_dir=None, allowed_root=None
):
    """Decrypt ``encrypted_path`` to a temp ``.tar.gz`` file and return its path.

    Auto-detects the streaming (version 2) and legacy Fernet (version 1)
    formats. Raises :class:`BackupPasswordError` on a wrong password or tampered
    data and :class:`BackupFormatError` on a malformed/unsupported file. The
    caller is responsible for deleting the returned temp file.
    """

    encrypted_path = _validated_existing_file_path(
        encrypted_path, allowed_root=allowed_root
    )
    temp_dir = _validated_temp_dir(temp_dir)
    with open(encrypted_path, "rb") as handle:
        if handle.read(len(MAGIC)) != MAGIC:
            raise BackupFormatError("not an EMS encrypted backup")
        version_byte = handle.read(1)
        if len(version_byte) != 1:
            raise BackupFormatError("truncated encrypted backup header")
        version = version_byte[0]
        if version == STREAMING_VERSION:
            return _decrypt_streaming(handle, password, temp_dir)
        if version == LEGACY_FERNET_VERSION:
            return _decrypt_legacy_fernet(handle, password, temp_dir)
        raise BackupFormatError(f"unsupported encrypted backup version {version}")


def _require_int(header, name):
    value = header.get(name)
    # bool is an int subclass; reject it so true/false can't masquerade.
    if not isinstance(value, int) or isinstance(value, bool):
        raise BackupFormatError(f"encrypted backup header has invalid {name}")
    return value


def _decode_b64_field(header, name):
    value = header.get(name)
    if not isinstance(value, str):
        raise BackupFormatError(f"encrypted backup header is missing {name}")
    try:
        return base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise BackupFormatError(
            f"encrypted backup header has invalid {name}"
        ) from exc


def _parse_streaming_header(header):
    """Validate a decoded version-2 header and return its decryption params."""

    if not isinstance(header, dict):
        raise BackupFormatError("encrypted backup header is not a JSON object")

    algorithm = header.get("algorithm")
    if algorithm not in SUPPORTED_AEAD_ALGORITHMS:
        raise BackupFormatError(f"unsupported encryption algorithm: {algorithm!r}")
    if header.get("kdf") != KDF_NAME:
        raise BackupFormatError(f"unsupported KDF: {header.get('kdf')!r}")

    iterations = _require_int(header, "iterations")
    if not MIN_KDF_ITERATIONS <= iterations <= MAX_KDF_ITERATIONS:
        raise BackupFormatError(f"PBKDF2 iterations out of range: {iterations}")

    salt = _decode_b64_field(header, "salt")
    if len(salt) != SALT_BYTES:
        raise BackupFormatError("encrypted backup header has invalid salt length")

    nonce_prefix = _decode_b64_field(header, "base_nonce")
    if len(nonce_prefix) != NONCE_PREFIX_BYTES:
        raise BackupFormatError("encrypted backup header has invalid base nonce length")

    chunk_size = _require_int(header, "chunk_size")
    if not MIN_CHUNK_SIZE <= chunk_size <= MAX_CHUNK_SIZE:
        raise BackupFormatError(f"chunk size out of range: {chunk_size}")

    return algorithm, iterations, salt, nonce_prefix, chunk_size


def _decrypt_streaming(handle, password, temp_dir):
    header_len = struct.unpack(">I", _read_exact(handle, 4))[0]
    if header_len == 0:
        raise BackupFormatError("encrypted backup header is empty")
    if header_len > MAX_HEADER_SIZE:
        raise BackupFormatError(
            f"encrypted backup header too large: {header_len} bytes"
        )
    try:
        header = json.loads(_read_exact(handle, header_len).decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise BackupFormatError(f"invalid encrypted backup header: {exc}") from exc

    algorithm, iterations, salt, nonce_prefix, chunk_size = _parse_streaming_header(
        header
    )
    max_ciphertext = chunk_size + MAX_CIPHERTEXT_OVERHEAD

    key = _derive_key(password, salt, iterations)
    cipher = create_aead_cipher(algorithm, key)

    fd, tmp_path = tempfile.mkstemp(suffix=".tar.gz", dir=temp_dir)
    try:
        with os.fdopen(fd, "wb") as out:
            index = 0
            while True:
                flag_byte = handle.read(1)
                if not flag_byte:
                    raise BackupFormatError(
                        "truncated encrypted backup: missing final chunk"
                    )
                if flag_byte not in (b"\x00", b"\x01"):
                    raise BackupFormatError("invalid chunk flag in encrypted backup")
                final = flag_byte == b"\x01"
                length = struct.unpack(">I", _read_exact(handle, 4))[0]
                if length == 0 and not final:
                    raise BackupFormatError("empty non-final chunk in encrypted backup")
                if length > max_ciphertext:
                    raise BackupFormatError("encrypted chunk exceeds maximum size")
                ciphertext = _read_exact(handle, length)
                try:
                    plaintext = cipher.decrypt(
                        _chunk_nonce(nonce_prefix, index),
                        ciphertext,
                        _chunk_aad(algorithm, index, final),
                    )
                except InvalidTag as exc:
                    raise BackupPasswordError(
                        "incorrect password or corrupted backup"
                    ) from exc
                out.write(plaintext)
                index += 1
                if final:
                    if handle.read(1):
                        raise BackupFormatError(
                            "trailing data after final encrypted chunk"
                        )
                    break
    except Exception:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        raise
    return tmp_path


def _decrypt_legacy_fernet(handle, password, temp_dir):
    iterations = struct.unpack(">I", _read_exact(handle, 4))[0]
    salt_len = _read_exact(handle, 1)[0]
    salt = _read_exact(handle, salt_len)
    token = handle.read()

    key = base64.urlsafe_b64encode(_derive_key(password, salt, iterations))
    fernet = Fernet(key)
    try:
        plaintext = fernet.decrypt(token)
    except InvalidToken as exc:
        raise BackupPasswordError(
            "incorrect password or corrupted backup"
        ) from exc

    fd, tmp_path = tempfile.mkstemp(suffix=".tar.gz", dir=temp_dir)
    try:
        with os.fdopen(fd, "wb") as out:
            out.write(plaintext)
    except Exception:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        raise
    return tmp_path


def _encrypt_file_legacy_fernet(
    plaintext_path, encrypted_path, password, iterations=LEGACY_KDF_ITERATIONS
):
    """Produce a legacy version-1 Fernet backup (kept for restore-compat tests)."""

    salt = os.urandom(SALT_BYTES)
    key = base64.urlsafe_b64encode(_derive_key(password, salt, iterations))
    fernet = Fernet(key)
    with open(plaintext_path, "rb") as handle:
        token = fernet.encrypt(handle.read())

    header = (
        MAGIC
        + bytes([LEGACY_FERNET_VERSION])
        + struct.pack(">I", iterations)
        + bytes([len(salt)])
        + salt
    )
    with open(encrypted_path, "wb") as out:
        out.write(header)
        out.write(token)
    return encrypted_path
