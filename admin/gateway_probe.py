# SPDX-License-Identifier: AGPL-3.0-or-later
"""Gateway candidate network probe for admin discovery.

Home routers usually sit on ``.1`` of their subnet. Probing a short list of
common gateway IPs with cheap TCP connects lets the UI suggest a reachable
``/24`` for the normal device scan even when that network is behind a routed
VLAN the container's own interfaces do not expose.

This is manual-trigger only, bounded, and never scans a full ``/24``: it opens a
few short-timeout TCP connections to a handful of candidate gateway addresses and
maps a responder to its ``/24``. No ICMP/ping, no shell-out, no background loop.
"""

import errno
import ipaddress
import socket
from concurrent.futures import ThreadPoolExecutor

from admin.discovery import CidrValidationError, validate_cidr

SOURCE = "gateway_candidate_probe"

# Common home-router gateway addresses (subnet ``.1``).
GATEWAY_CANDIDATES = (
    "192.168.0.1",
    "192.168.1.1",
    "192.168.2.1",
    "192.168.3.1",
    "192.168.4.1",
    "192.168.5.1",
    "192.168.6.1",
    "192.168.7.1",
    "192.168.8.1",
    "192.168.9.1",
    "192.168.10.1",
    "192.168.20.1",
    "192.168.30.1",
    "192.168.40.1",
    "192.168.50.1",
    "192.168.60.1",
    "192.168.70.1",
    "192.168.80.1",
    "192.168.90.1",
    "192.168.100.1",
    "192.168.110.1",
    "192.168.120.1",
    "192.168.130.1",
    "192.168.140.1",
    "192.168.150.1",
    "192.168.160.1",
    "192.168.170.1",
    "192.168.178.1",
    "192.168.179.1",
    "192.168.190.1",
    "192.168.200.1",
    "192.168.210.1",
    "192.168.220.1",
    "192.168.230.1",
    "192.168.240.1",
    "192.168.250.1",
)

# Some routers use ``.254`` instead of ``.1`` on the same subnets.
FALLBACK_CANDIDATES = tuple(
    ip.rsplit(".", 1)[0] + ".254" for ip in GATEWAY_CANDIDATES
)

PROBE_PORTS = (80, 443, 53)

TIMEOUT_MS_MIN = 200
TIMEOUT_MS_MAX = 1000
TIMEOUT_MS_DEFAULT = 400
MAX_WORKERS_MIN = 1
MAX_WORKERS_MAX = 32
MAX_WORKERS_DEFAULT = 32

STATUS_RANK = {"reachable": 2, "error": 1, "no_response": 0}


def _clamp_int(value, default, low, high):
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, number))


def clamp_timeout_ms(value):
    return _clamp_int(value, TIMEOUT_MS_DEFAULT, TIMEOUT_MS_MIN, TIMEOUT_MS_MAX)


def clamp_max_workers(value):
    return _clamp_int(value, MAX_WORKERS_DEFAULT, MAX_WORKERS_MIN, MAX_WORKERS_MAX)


def candidate_network(ip):
    """Return the ``/24`` string that a gateway candidate belongs to."""

    return str(ipaddress.ip_network(f"{ip}/24", strict=False))


def _scan_supported(cidr):
    try:
        validate_cidr(cidr)
    except CidrValidationError:
        return False
    return True


def tcp_connect(ip, port, timeout_s):
    """Single TCP connect probe. Returns ``open``/``refused``/``filtered``/``error``.

    ``refused`` (RST) still means the host answered, so it counts as a
    reachability signal; ``filtered`` (timeout) does not.
    """

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(timeout_s)
    try:
        result = sock.connect_ex((ip, port))
    except OSError:
        return "error"
    finally:
        sock.close()
    if result == 0:
        return "open"
    if result == errno.ECONNREFUSED:
        return "refused"
    return "filtered"


def _confidence(signals):
    open_count = sum(1 for s in signals if s.endswith("_open"))
    if open_count >= 2:
        return 0.9
    if open_count == 1:
        return 0.8
    if signals:  # refused only: host responded but no open port
        return 0.6
    return 0.0


def classify_probe_result(ip, ports=PROBE_PORTS, timeout_s=None, connect=tcp_connect):
    """Probe one gateway candidate across ``ports`` and build its result dict."""

    if timeout_s is None:
        timeout_s = TIMEOUT_MS_DEFAULT / 1000.0

    signals = []
    saw_error = False
    for port in ports:
        outcome = connect(ip, port, timeout_s)
        if outcome == "open":
            signals.append(f"tcp_{port}_open")
        elif outcome == "refused":
            signals.append(f"tcp_{port}_refused")
        elif outcome == "error":
            saw_error = True

    if signals:
        status = "reachable"
    elif saw_error:
        status = "error"
    else:
        status = "no_response"

    network = candidate_network(ip)
    return {
        "network": network,
        "gateway_candidate": ip,
        "status": status,
        "signals": signals,
        "confidence": _confidence(signals),
        "source": SOURCE,
        "scan_supported": _scan_supported(network),
    }


def _better(current, candidate):
    """Pick the more useful of two results for the same ``/24``."""

    if STATUS_RANK[candidate["status"]] != STATUS_RANK[current["status"]]:
        return candidate if STATUS_RANK[candidate["status"]] > STATUS_RANK[current["status"]] else current
    if candidate["confidence"] > current["confidence"]:
        return candidate
    return current  # tie keeps the first (primary) candidate for its network


def _sort_key(entry):
    try:
        network_int = int(ipaddress.ip_network(entry["network"]).network_address)
    except ValueError:
        network_int = 0
    return (-STATUS_RANK[entry["status"]], -entry["confidence"], network_int)


def probe_gateway_candidates(candidates=None, timeout_ms=None, max_workers=None,
                             connect=tcp_connect):
    """Probe gateway candidates and return one deduplicated result per ``/24``.

    Pure aside from the injected ``connect`` probe, so it is fully testable with a
    fake connect callable. Results are sorted reachable-first, then by network.
    """

    if candidates is None:
        candidates = GATEWAY_CANDIDATES + FALLBACK_CANDIDATES
    candidates = list(dict.fromkeys(candidates))  # de-dup, keep primary order

    timeout_s = clamp_timeout_ms(timeout_ms) / 1000.0
    workers = clamp_max_workers(max_workers)

    def _probe(ip):
        return classify_probe_result(ip, timeout_s=timeout_s, connect=connect)

    with ThreadPoolExecutor(max_workers=min(workers, len(candidates))) as pool:
        results = list(pool.map(_probe, candidates))

    by_network = {}
    for result in results:
        network = result["network"]
        existing = by_network.get(network)
        by_network[network] = result if existing is None else _better(existing, result)

    ordered = sorted(by_network.values(), key=_sort_key)
    return {
        "candidates": ordered,
        "source": SOURCE,
        "probed": len(candidates),
    }
