# SPDX-FileCopyrightText: 2026 basecubedev
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Where the Admin console, the one address of the whole product, answers.

The Admin console serves the dashboard's pages together with its own. The EMS
keeps its dashboard on its own port as a way in that works while the console
is down, and sends a browser that opens it on to the console. It learns the
console's ports from ``config/admin-console.json``, which the console writes
beside ``config.json`` while it listens on the network, and removes while it
listens on this host only. The host name is the one the browser already used,
so the file names ports, never an address.

The file outlives the console that wrote it: a console stopped, or replaced by
an older one that carries no dashboard, leaves it behind. So the EMS reports
the console only once the console has answered ``/api/console`` on that port
saying it carries the dashboard. Import-side-effect-free, so the Admin image
can carry it.
"""

import http.client
import json
import os
import tempfile
import threading
import time

from dashboard.host_reach import host_candidates

CONSOLE_ADDRESS_NAME = "admin-console.json"
CONSOLE_PATH = "/api/console"
CONSOLE_IDENTITY = {"console": "ems-solarflow-admin", "dashboard": True}
CONSOLE_CHECK_SECONDS = 10.0


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


def console_answers(port, hosts, timeout=0.5):
    """Whether a console carrying the dashboard answers ``/api/console`` on ``port`` at one of ``hosts``."""

    for host in hosts:
        connection = http.client.HTTPConnection(host, port, timeout=timeout)
        try:
            connection.request("GET", CONSOLE_PATH)
            response = connection.getresponse()
            body = response.read(4096)
        except (OSError, http.client.HTTPException):
            continue
        finally:
            connection.close()
        if response.status != 200:
            continue
        try:
            payload = json.loads(body)
        except ValueError:
            continue
        if isinstance(payload, dict) and payload.get("dashboard") is True:
            return True
    return False


class ConsoleCheck:
    """Whether the console a record names answers, asked again at most every 10 seconds per port."""

    def __init__(self, answers=None, clock=time.monotonic):
        self._answers = answers or (lambda port: console_answers(port, host_candidates()))
        self._clock = clock
        self._lock = threading.Lock()
        self._known = {}

    def __call__(self, record):
        port = record["port"]
        now = self._clock()
        with self._lock:
            known = self._known.get(port)
            if known and known[0] > now:
                return known[1]
        answered = bool(self._answers(port))
        with self._lock:
            self._known[port] = (now + CONSOLE_CHECK_SECONDS, answered)
        return answered
