# SPDX-License-Identifier: AGPL-3.0-or-later
"""The address range the config template takes its sample addresses from.

A range reserved for documentation (RFC 5737) that home routers do not hand
out. The template names only addresses from it, and the placeholder check
holds every address in it written in dotted or IPv6 notation, so an address a
router really hands out is never held, however common: the first DHCP lease
of many routers is
``192.168.1.100``. A network that assigns this range statically anyway is
held in safe mode.

Import-side-effect-free and dependency-free, so the template builder and the
placeholder check can both read it without importing each other.
"""

import ipaddress

TEMPLATE_ADDRESS_NETWORK = ipaddress.ip_network("198.51.100.0/24")


def template_address(host):
    """The template's sample address ``host`` within the range."""

    return str(TEMPLATE_ADDRESS_NETWORK[host])


def is_template_address(value):
    """True for an address in the range, also in its IPv4-mapped IPv6 form."""

    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    address = getattr(address, "ipv4_mapped", None) or address
    return address in TEMPLATE_ADDRESS_NETWORK
