# SPDX-FileCopyrightText: 2026 basecubedev
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Where the Admin console, the one address of the whole product, answers, and whether it still does.

The Admin console serves the dashboard's pages together with its own. The EMS
keeps its dashboard on its own port, which points to the console while the
console runs and is the way in while it does not. It learns the console's
ports from ``config/admin-console.json``, which the console writes beside
``config.json`` while it listens on the network and removes while it listens
on this host only. The host name is the one the browser already used, so the
file names ports, never an address.

The file is a lease: the console renews it every few seconds
(:class:`ConsoleLease`), and the EMS takes it as the console's word only while
it is fresh (:func:`read_live_console`). A console that stopped, or an older one
put back that carries no dashboard and renews nothing, leaves a file that goes
stale within half a minute, and the EMS answers in full again. Neither side
reaches the other over the network for it. Import-side-effect-free, so the
Admin image can carry it.
"""

import json
import logging
import os
import tempfile
import threading
import time

CONSOLE_ADDRESS_NAME = "admin-console.json"
CONSOLE_PATH = "/api/console"
CONSOLE_IDENTITY = {"console": "ems-solarflow-admin", "dashboard": True}
LEASE_SECONDS = 30.0
RENEW_SECONDS = 10.0


def _port(value):
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 65535:
        return None
    return value


def console_address(port, https_port=None):
    """The record for a console listening on ``port`` and, optionally, ``https_port``."""

    record = {"port": _port(port), "https_port": _port(https_port)}
    if record["port"] is None:
        raise ValueError("the console needs a valid HTTP port")
    return record


def read_console_address(path):
    """The console's ports from ``path``, or ``None`` when absent or malformed."""

    try:
        with open(path, encoding="utf-8") as handle:
            record = json.load(handle)
    except (OSError, ValueError, UnicodeDecodeError):
        return None
    if not isinstance(record, dict) or _port(record.get("port")) is None:
        return None
    return {"port": record["port"], "https_port": _port(record.get("https_port"))}


def publish_console_address(config_dir, record):
    """Write ``record`` into ``config_dir``, or remove the file when it is ``None``.

    Nothing happens while the directory does not exist yet (no installation),
    or when the file already says the same; a failure leaves the file as it
    was, since the EMS then only keeps serving its own dashboard.
    """

    config_dir = os.fspath(config_dir)
    if not os.path.isdir(config_dir):
        return False
    path = os.path.join(config_dir, CONSOLE_ADDRESS_NAME)
    if record is None:
        try:
            os.unlink(path)
        except OSError:
            return False
        return True
    if read_console_address(path) == record:
        return False
    try:
        fd, temporary = tempfile.mkstemp(prefix=".admin-console.", dir=config_dir)
    except OSError:
        return False
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(record, handle, sort_keys=True)
            handle.write("\n")
        os.chmod(temporary, 0o644)
        os.replace(temporary, path)
    except OSError:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        return False
    return True


def read_live_console(path, now=None):
    """The console's ports from ``path`` while the console keeps renewing it, else ``None``.

    A file renewed less than :data:`LEASE_SECONDS` ago is live; one from the
    future by more than that, as after the clock went back, is not.
    """

    try:
        renewed = os.stat(path).st_mtime
    except OSError:
        return None
    age = (time.time() if now is None else now) - renewed
    if not -LEASE_SECONDS <= age <= LEASE_SECONDS:
        return None
    return read_console_address(path)


class ConsoleLease:
    """The console's side of the lease: publish its record and renew it, from a thread of its own."""

    def __init__(self, config_dir, record, interval=RENEW_SECONDS):
        self._config_dir = config_dir
        self._record = record
        self._interval = interval
        self._stop = threading.Event()

    def renew(self):
        """Publish the record where the installation's ``config.json`` lies now, and mark it renewed."""

        config_dir = os.fspath(self._config_dir())
        publish_console_address(config_dir, self._record)
        try:
            os.utime(os.path.join(config_dir, CONSOLE_ADDRESS_NAME))
        except OSError:
            return False
        return True

    def start(self):
        threading.Thread(target=self._run, name="console-lease", daemon=True).start()

    def stop(self):
        self._stop.set()

    def _run(self):
        while not self._stop.is_set():
            try:
                self.renew()
            except Exception:
                logging.exception("console_lease_renew_failed")
            self._stop.wait(self._interval)
