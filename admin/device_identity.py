# SPDX-License-Identifier: AGPL-3.0-or-later
"""Admin-owned HMAC key lifecycle for browser inverter identity tokens."""

from __future__ import annotations

import os
import secrets
import stat
from pathlib import Path

IDENTITY_TOKEN_KEY_FILENAME = ".device-identity-key"
IDENTITY_TOKEN_KEY_BYTES = 32


class IdentityTokenKeyError(RuntimeError):
    """A safe startup error when keyed browser identity cannot be issued."""


class IdentityTokenKeyStore:
    def __init__(self, state_dir):
        self.state_dir = Path(state_dir)
        self.path = self.state_dir / IDENTITY_TOKEN_KEY_FILENAME

    def load_or_create(self) -> bytes:
        """Load the stable key or atomically create it with restrictive mode."""

        try:
            self.state_dir.mkdir(parents=True, exist_ok=True)
            if os.name == "posix":
                os.chmod(self.state_dir, stat.S_IRWXU)
        except OSError as exc:
            raise IdentityTokenKeyError(
                "Admin device identity state is unavailable."
            ) from exc

        try:
            key = self.path.read_bytes()
        except FileNotFoundError:
            key = self._create()
        except OSError as exc:
            raise IdentityTokenKeyError(
                "Admin device identity key could not be read."
            ) from exc
        if len(key) != IDENTITY_TOKEN_KEY_BYTES:
            raise IdentityTokenKeyError("Admin device identity key is invalid.")
        try:
            if os.name == "posix":
                os.chmod(self.path, stat.S_IRUSR | stat.S_IWUSR)
        except OSError as exc:
            raise IdentityTokenKeyError(
                "Admin device identity key permissions could not be enforced."
            ) from exc
        return key

    def _create(self) -> bytes:
        key = secrets.token_bytes(IDENTITY_TOKEN_KEY_BYTES)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        try:
            descriptor = os.open(
                self.path, flags, stat.S_IRUSR | stat.S_IWUSR
            )
        except FileExistsError:
            try:
                return self.path.read_bytes()
            except OSError as exc:
                raise IdentityTokenKeyError(
                    "Admin device identity key could not be read."
                ) from exc
        except OSError as exc:
            raise IdentityTokenKeyError(
                "Admin device identity key could not be created."
            ) from exc
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(key)
                handle.flush()
                os.fsync(handle.fileno())
        except OSError as exc:
            raise IdentityTokenKeyError(
                "Admin device identity key could not be written."
            ) from exc
        return key


__all__ = [
    "IDENTITY_TOKEN_KEY_BYTES",
    "IDENTITY_TOKEN_KEY_FILENAME",
    "IdentityTokenKeyError",
    "IdentityTokenKeyStore",
]
