# SPDX-License-Identifier: AGPL-3.0-or-later
"""Gateway candidate probe tests (mocked TCP connect, no real sockets)."""

import pytest

from admin.discovery import validate_cidr
from admin.gateway_probe import (
    FALLBACK_CANDIDATES,
    GATEWAY_CANDIDATES,
    PROBE_PORTS,
    candidate_network,
    classify_probe_result,
    probe_gateway_candidates,
)

pytestmark = [
    pytest.mark.admin,
    pytest.mark.setup,
    pytest.mark.integration,
    pytest.mark.simulation,
]


def _by_network(result, network):
    for entry in result["candidates"]:
        if entry["network"] == network:
            return entry
    return None


def test_candidate_maps_to_slash_24():
    assert candidate_network("192.168.20.1") == "192.168.20.0/24"
    assert candidate_network("192.168.178.254") == "192.168.178.0/24"


def test_initial_candidate_list_maps_to_expected_networks():
    for ip in GATEWAY_CANDIDATES:
        network = candidate_network(ip)
        assert network.endswith(".0/24")
        # Every suggested network must satisfy the scan endpoint's validation.
        assert validate_cidr(network)


def test_open_port_is_reachable_with_signal():
    def connect(ip, port, timeout_s):
        return "open" if port == 443 else "filtered"

    result = classify_probe_result("192.168.20.1", connect=connect)
    assert result["status"] == "reachable"
    assert result["signals"] == ["tcp_443_open"]
    assert result["network"] == "192.168.20.0/24"
    assert result["gateway_candidate"] == "192.168.20.1"
    assert result["source"] == "gateway_candidate_probe"
    assert result["scan_supported"] is True
    assert result["confidence"] == 0.8


def test_two_open_ports_raise_confidence():
    result = classify_probe_result("192.168.1.1", connect=lambda *_: "open")
    assert result["status"] == "reachable"
    assert result["confidence"] == 0.9
    assert len(result["signals"]) == len(PROBE_PORTS)


def test_refused_still_counts_as_reachable():
    result = classify_probe_result(
        "192.168.2.1",
        connect=lambda ip, port, t: "refused" if port == 53 else "filtered",
    )
    assert result["status"] == "reachable"
    assert result["signals"] == ["tcp_53_refused"]
    assert result["confidence"] == 0.6


def test_all_filtered_is_no_response():
    result = classify_probe_result("192.168.30.1", connect=lambda *_: "filtered")
    assert result["status"] == "no_response"
    assert result["signals"] == []
    assert result["confidence"] == 0.0


def test_socket_error_is_reported_as_error():
    result = classify_probe_result("192.168.99.1", connect=lambda *_: "error")
    assert result["status"] == "error"
    assert result["signals"] == []


def test_probe_maps_responding_gateway_to_scannable_network():
    # Only 192.168.20.1 responds; the /24 should be suggested for a device scan.
    def connect(ip, port, timeout_s):
        if ip == "192.168.20.1" and port == 443:
            return "open"
        return "filtered"

    result = probe_gateway_candidates(candidates=GATEWAY_CANDIDATES, connect=connect)
    entry = _by_network(result, "192.168.20.0/24")
    assert entry is not None
    assert entry["status"] == "reachable"
    assert entry["scan_supported"] is True
    # Reachable candidates sort ahead of the silent ones.
    assert result["candidates"][0]["network"] == "192.168.20.0/24"
    assert result["source"] == "gateway_candidate_probe"


def test_probe_dedupes_primary_and_fallback_to_one_network():
    def connect(ip, port, timeout_s):
        return "open" if ip.endswith(".254") else "filtered"

    candidates = ("192.168.20.1", "192.168.20.254")
    result = probe_gateway_candidates(candidates=candidates, connect=connect)
    matches = [c for c in result["candidates"] if c["network"] == "192.168.20.0/24"]
    assert len(matches) == 1
    # The responding fallback wins over the silent primary.
    assert matches[0]["gateway_candidate"] == "192.168.20.254"
    assert matches[0]["status"] == "reachable"


def test_fallback_candidates_share_primary_networks():
    for ip in FALLBACK_CANDIDATES:
        assert ip.endswith(".254")
        assert candidate_network(ip) in {candidate_network(p) for p in GATEWAY_CANDIDATES}


def test_no_response_does_not_block_reachable_results():
    result = probe_gateway_candidates(
        candidates=GATEWAY_CANDIDATES, connect=lambda *_: "filtered"
    )
    assert result["candidates"]  # still returns every candidate as no_response
    assert all(c["status"] == "no_response" for c in result["candidates"])
