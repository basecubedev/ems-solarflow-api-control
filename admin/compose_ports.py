# SPDX-FileCopyrightText: 2026 basecubedev
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The ports an installation's ``docker-compose.yml`` publishes, in today's layout.

The Admin console reaches the EMS through a socket file and serves the one
app, so the EMS's own port only points there, and the bundled InfluxDB needs
no port of the network's: the EMS reaches it inside Docker, the console with
``docker exec``. A compose file written before publishes both on every address.
The guided upgrade brings it to the installers' layout, where ``.env`` may
open or move either (``EMS_DASHBOARD_PUBLISH``, ``EMS_INFLUX_PUBLISH``).

Only the entries an installer wrote are rewritten, exactly, and only in the
service the installers named and gave their container name: an operator's own
InfluxDB, or a port an operator chose, stays as it is. The result works with
every EMS image, an older one after going back a version included, since the
container's own ports are unchanged. Import-side-effect-free.
"""

import re

_SERVICE = re.compile(r"^  (?P<name>[A-Za-z0-9_.-]+):[ \t]*(#.*)?$")
_TOP_LEVEL = re.compile(r"^[^\s#]")

_WRITTEN_BY_INSTALLERS = {
    "ems": (
        "ems-solarflow-api-control",
        re.compile(r'^(?P<lead>[ \t]*-[ \t]*)"?8080:8080"?[ \t]*$'),
        '"${EMS_DASHBOARD_PUBLISH:-8080}:8080"',
    ),
    "influxdb": (
        "ems-influxdb",
        re.compile(r'^(?P<lead>[ \t]*-[ \t]*)"?8086:8086"?[ \t]*$'),
        '"${EMS_INFLUX_PUBLISH:-127.0.0.1:8086}:8086"',
    ),
}


def _service_blocks(lines):
    """Each service under ``services:`` with the line range of its block."""

    blocks = []
    inside = False
    for index, line in enumerate(lines):
        text = line.rstrip("\r\n")
        if _TOP_LEVEL.match(text):
            inside = text.startswith("services:")
            if blocks and blocks[-1][2] is None:
                blocks[-1][2] = index
            continue
        match = _SERVICE.match(text) if inside else None
        if match:
            if blocks and blocks[-1][2] is None:
                blocks[-1][2] = index
            blocks.append([match.group("name"), index + 1, None])
    if blocks and blocks[-1][2] is None:
        blocks[-1][2] = len(lines)
    return blocks


def current_publishes(compose_text):
    """``compose_text`` with the installers' former port entries in today's layout, and the services whose entry moved."""

    lines = compose_text.splitlines(keepends=True)
    moved = []
    for name, start, end in _service_blocks(lines):
        written = _WRITTEN_BY_INSTALLERS.get(name)
        if written is None:
            continue
        container, pattern, entry = written
        block = lines[start:end]
        if not any(re.fullmatch(rf'[ \t]+container_name:[ \t]*"?{re.escape(container)}"?[ \t]*', line.rstrip("\r\n")) for line in block):
            continue
        for offset, line in enumerate(block):
            body = line.rstrip("\r\n")
            match = pattern.match(body)
            if match:
                lines[start + offset] = match.group("lead") + entry + line[len(body):]
                if name not in moved:
                    moved.append(name)
    return "".join(lines), tuple(moved)
