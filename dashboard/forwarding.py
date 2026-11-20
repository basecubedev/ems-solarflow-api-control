# SPDX-FileCopyrightText: 2026 basecubedev
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Which client a request forwarded by the Admin console comes from.

The Admin console serves the one browser app and forwards the EMS's API to the
dashboard server, so every forwarded request reaches the EMS from the Admin's
address. The sign-in limit and the limit on live streams count per client, and
would count the whole household as one. The Admin therefore names the client in
``X-Forwarded-For`` and proves it is the Admin with a key both read from the
installation's ``config/`` directory; without that key, or with a wrong one,
the header is ignored and the request counts as the address it came from.
Import-side-effect-free, so the Admin image can carry it.
"""

import hmac
import ipaddress
import os
import secrets
import tempfile
import threading

FORWARDING_KEY_NAME = "admin-forwarding.key"
FORWARDED_FOR_HEADER = "X-Forwarded-For"
FORWARDING_KEY_HEADER = "X-EMS-Forwarding-Key"
_KEY_BYTES = 32


class ForwardingKey:
    """The key in one file, read again only when the file changes."""

    def __init__(self, path):
        self.path = os.fspath(path)
        self._lock = threading.Lock()
        self._signature = None
        self._value = None

    def read(self):
        """The key, or ``None`` when the file is missing, unreadable or malformed."""

        try:
            stat = os.stat(self.path)
        except OSError:
            return None
        signature = (stat.st_mtime_ns, stat.st_size, stat.st_ino)
        with self._lock:
            if signature != self._signature:
                self._value = _load_key(self.path)
                self._signature = signature
            return self._value

    def ensure(self):
        """The key, writing the file when it is missing or does not hold one.

        The key is written whole to a file of its own and then put in place,
        so a crash or a full disk leaves either no key file or a complete one,
        never an empty file that would stay. When two writers race, the file
        that ends up in place is the key both return.
        """

        value = self.read()
        if value is not None:
            return value
        directory = os.path.dirname(self.path) or "."
        try:
            fd, scratch = tempfile.mkstemp(prefix=".admin-forwarding.", dir=directory)
        except OSError:
            return None
        try:
            with os.fdopen(fd, "w", encoding="ascii") as handle:
                handle.write(secrets.token_hex(_KEY_BYTES) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            if os.path.lexists(self.path):
                os.replace(scratch, self.path)
            else:
                try:
                    os.link(scratch, self.path)
                except FileExistsError:
                    pass
        except OSError:
            return self.read()
        finally:
            try:
                os.unlink(scratch)
            except OSError:
                pass
        return self.read()


def _load_key(path):
    try:
        with open(path, encoding="ascii") as handle:
            value = handle.read().strip()
    except (OSError, UnicodeDecodeError):
        return None
    if len(value) != _KEY_BYTES * 2:
        return None
    try:
        bytes.fromhex(value)
    except ValueError:
        return None
    return value


def forwarded_client(headers, remote, key):
    """The client a request names, when the forwarding key proves the Admin sent it.

    ``key`` is the installation's key or ``None``; ``remote`` is the address
    the request arrived from and is returned for anything not proven.
    """

    claimed = headers.get(FORWARDED_FOR_HEADER)
    presented = headers.get(FORWARDING_KEY_HEADER)
    if not key or not claimed or not presented:
        return remote
    if not hmac.compare_digest(presented.encode("utf-8", "replace"), key.encode("ascii")):
        return remote
    try:
        return str(ipaddress.ip_address(claimed.strip()))
    except ValueError:
        return remote
