# SPDX-FileCopyrightText: 2026 basecubedev
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Where a port this host publishes answers, from wherever this process runs.

The EMS and the Admin console each run in a container, and each finds a port
the other publishes on the host: the console the EMS's dashboard, the EMS the
console. A container on the host's network finds it on its own loopback; one
in a Docker bridge network finds the host at its default gateway instead.

The default gateway is the host only inside such a bridge network. On the
host's own network it is the LAN's router, which must never be taken for the
EMS: whatever is sent there, a password or a session, leaves the host. So the
gateway is a candidate only for a process that runs in a container and sees
none of the host's Docker interfaces, which a process on the host's network
of a Docker host always sees. Import-side-effect-free, so the Admin image can
carry it.
"""

import os
import socket
import struct

ROUTE_TABLE = "/proc/net/route"
NET_CLASS = "/sys/class/net"
CONTAINER_MARK = "/.dockerenv"
LOOPBACK = "127.0.0.1"


def in_bridge_network(net_class=NET_CLASS, mark=CONTAINER_MARK):
    """Whether this process runs in a Docker container on a network of its own, not the host's."""

    if not os.path.exists(mark):
        return False
    try:
        names = os.listdir(net_class)
    except OSError:
        return False
    return not any(name == "docker0" or name.startswith(("br-", "veth")) for name in names)


def default_gateway(route_table=ROUTE_TABLE):
    """The IPv4 default gateway from the kernel's route table, or ``None``."""

    try:
        with open(route_table, encoding="ascii") as handle:
            lines = handle.read().splitlines()[1:]
    except (OSError, UnicodeDecodeError):
        return None
    for line in lines:
        fields = line.split()
        if len(fields) < 4 or fields[1] != "00000000":
            continue
        try:
            gateway = int(fields[2], 16)
            flags = int(fields[3], 16)
        except ValueError:
            continue
        if flags & 0x2 and gateway:
            return socket.inet_ntoa(struct.pack("<L", gateway))
    return None


def host_candidates(route_table=ROUTE_TABLE, bridged=None):
    """This host's loopback, then, in a container's bridge network only, the default gateway."""

    if bridged is None:
        bridged = in_bridge_network()
    gateway = default_gateway(route_table) if bridged else None
    return [LOOPBACK] + ([gateway] if gateway and gateway != LOOPBACK else [])


def first_answering(port, candidates, timeout=0.5):
    """The first candidate that accepts a connection on ``port``, or ``None``."""

    for host in candidates:
        try:
            with socket.create_connection((host, port), timeout=timeout):
                return host
        except OSError:
            continue
    return None
