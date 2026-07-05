# SPDX-License-Identifier: AGPL-3.0-or-later
"""Local IPv4 network suggestions for admin discovery.

Detection is stdlib/Linux only (``socket.if_nameindex`` + ``fcntl`` ioctls +
``/proc/net/route``); it never shells out to ``ip``/``ifconfig``/``route`` and
never sends packets. It runs only when the API is requested, is cheap, and never
triggers a scan on its own. Broad networks are narrowed to a scan-safe ``/24``
around the interface address so the scan endpoint's limits still hold.
"""

import ipaddress
from dataclasses import dataclass

# Docker bridge networks are almost never useful LAN scan targets; VPN-like
# interfaces are private but should stay "advanced" unless nothing else exists.
DOCKER_ONLY_WARNING = (
    "Only Docker bridge networks were detected. For LAN discovery on "
    "Linux/Raspberry Pi, start the admin preview with host networking or enter "
    "your LAN CIDR manually."
)
NO_NETWORK_WARNING = (
    "No local IPv4 networks were detected. Enter your LAN CIDR manually."
)
PLATFORM_WARNING = (
    "Automatic network detection is not available on this platform. Enter your "
    "LAN CIDR manually."
)
NO_LAN_WARNING = (
    "No obvious LAN network was detected. Review the advanced entries or enter "
    "your CIDR manually."
)

# Narrower than this stays as-is; anything broader is reduced to a /24 window.
SAFE_SCAN_PREFIX = 24


@dataclass(frozen=True)
class Interface:
    """A detected IPv4 interface (name, address, netmask)."""

    name: str
    address: str
    netmask: str


def _is_docker_like(name):
    return name == "docker0" or name.startswith(("br-", "veth"))


def _is_vpn_like(name):
    return name.startswith(("wg", "tun", "tap", "tailscale", "zt"))


def _is_lan_like(name):
    return name.startswith(("eth", "en", "wlan", "wl"))


def _classify(iface, default_iface):
    """Turn one interface into a suggestion dict, or None to exclude it."""

    try:
        address = ipaddress.IPv4Address(iface.address)
        network = ipaddress.IPv4Network(f"{iface.address}/{iface.netmask}", strict=False)
    except (ipaddress.AddressValueError, ipaddress.NetmaskValueError, ValueError):
        return None

    if address.is_loopback or network.is_loopback:
        return None

    is_link_local = network.is_link_local or address.is_link_local
    is_private = network.is_private and not is_link_local
    if not (is_private or is_link_local):
        # Public ranges are never recommended and never scanned.
        return None

    is_docker = _is_docker_like(iface.name)
    is_vpn = _is_vpn_like(iface.name)
    is_default = iface.name == default_iface
    scan_recommended = is_private and not is_docker and not is_vpn and not is_link_local

    broad = network.prefixlen < SAFE_SCAN_PREFIX
    scan_net = (
        ipaddress.IPv4Network(f"{address}/{SAFE_SCAN_PREFIX}", strict=False)
        if broad
        else network
    )

    return {
        "cidr": str(scan_net),
        "original_cidr": str(network),
        "interface": iface.name,
        "address": str(address),
        "netmask": iface.netmask,
        "prefix_length": network.prefixlen,
        "is_private": is_private,
        "is_loopback": False,
        "is_link_local": is_link_local,
        "is_default_route": is_default,
        "is_docker_like": is_docker,
        "is_vpn_like": is_vpn,
        "scan_recommended": scan_recommended,
        "priority": _priority(is_default, is_private, is_docker, is_vpn,
                              is_link_local, _is_lan_like(iface.name)),
        "label": _label(is_default, scan_recommended, is_docker, is_vpn,
                        is_link_local),
        "reason": _reason(broad, is_default, scan_recommended, is_docker, is_vpn,
                         is_link_local),
    }


def _priority(is_default, is_private, is_docker, is_vpn, is_link_local, is_lan):
    if is_docker:
        return 10
    if is_link_local:
        return 20
    if is_vpn:
        return 40
    if is_private:
        if is_default:
            return 100
        return 80 if is_lan else 70
    return 0


def _label(is_default, scan_recommended, is_docker, is_vpn, is_link_local):
    if is_docker:
        return "Docker network"
    if is_link_local:
        return "Advanced (link-local)"
    if is_vpn:
        return "Advanced (VPN)"
    if scan_recommended and is_default:
        return "Recommended LAN"
    if scan_recommended:
        return "Private LAN"
    return "Network"


def _reason(broad, is_default, scan_recommended, is_docker, is_vpn, is_link_local):
    if broad:
        return "Original network is broad; using /24 around the interface address"
    if is_docker:
        return "Docker bridge network; usually not useful for LAN discovery"
    if is_vpn:
        return "VPN-like interface; not the default route"
    if is_link_local:
        return "Link-local network; advanced use only"
    if is_default and scan_recommended:
        return "Default route on a private IPv4 network"
    return "Private IPv4 network"


def _family_rank(cidr):
    """Group networks so 192.168.x sort first, then 10.x, then everything else."""

    if cidr.startswith("192.168."):
        return 0
    if cidr.startswith("10."):
        return 1
    return 2


def _sort_key(entry):
    try:
        address_int = int(ipaddress.ip_network(entry["cidr"]).network_address)
    except ValueError:
        address_int = 0
    return (_family_rank(entry["cidr"]), -entry["priority"], address_int)


def build_suggestions(interfaces, default_iface=None):
    """Classify, de-duplicate, and sort interfaces into scan suggestions.

    Pure and side-effect-free so it is fully testable with faked interface data.
    Duplicate suggested CIDRs keep the higher-priority interface.
    """

    by_cidr = {}
    for iface in interfaces:
        entry = _classify(iface, default_iface)
        if entry is None:
            continue
        existing = by_cidr.get(entry["cidr"])
        if existing is None or entry["priority"] > existing["priority"]:
            by_cidr[entry["cidr"]] = entry

    ordered = sorted(by_cidr.values(), key=_sort_key)

    warnings = []
    if not ordered:
        warnings.append(NO_NETWORK_WARNING)
    elif not any(e["scan_recommended"] for e in ordered):
        if all(e["is_docker_like"] for e in ordered):
            warnings.append(DOCKER_ONLY_WARNING)
        else:
            warnings.append(NO_LAN_WARNING)

    return {
        "networks": ordered,
        "warnings": warnings,
        "manual_entry_supported": True,
    }


# --- platform detection (Linux stdlib) ----------------------------------

def enumerate_interfaces():
    """Return detected IPv4 interfaces via stdlib ioctls (Linux/Unix).

    Raises ``NotImplementedError`` on platforms without ``fcntl`` so callers can
    surface a warning and keep manual CIDR entry working.
    """

    try:
        import fcntl
        import socket
        import struct
    except ImportError as exc:  # e.g. Windows
        raise NotImplementedError("interface detection requires fcntl") from exc

    siocgifaddr = 0x8915
    siocgifnetmask = 0x891B

    def _ioctl_ipv4(sock, name, request):
        packed = struct.pack("256s", name.encode("utf-8")[:15])
        result = fcntl.ioctl(sock.fileno(), request, packed)
        return socket.inet_ntoa(result[20:24])

    interfaces = []
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        for _index, name in socket.if_nameindex():
            try:
                address = _ioctl_ipv4(sock, name, siocgifaddr)
                netmask = _ioctl_ipv4(sock, name, siocgifnetmask)
            except OSError:
                # No IPv4 assigned to this interface; skip it.
                continue
            interfaces.append(Interface(name=name, address=address, netmask=netmask))
    return interfaces


def default_route_interface():
    """Return the default-route interface name from ``/proc/net/route``, or None."""

    try:
        with open("/proc/net/route", encoding="utf-8") as handle:
            next(handle, None)  # header
            for line in handle:
                fields = line.split()
                if len(fields) >= 2 and fields[1] == "00000000":
                    return fields[0]
    except (OSError, StopIteration):
        return None
    return None


def detect_network_suggestions():
    """Detect local networks and return the API payload (never raises)."""

    try:
        interfaces = enumerate_interfaces()
    except NotImplementedError:
        return {
            "networks": [],
            "warnings": [PLATFORM_WARNING],
            "manual_entry_supported": True,
        }
    except OSError:
        return {
            "networks": [],
            "warnings": [PLATFORM_WARNING],
            "manual_entry_supported": True,
        }

    return build_suggestions(interfaces, default_route_interface())
