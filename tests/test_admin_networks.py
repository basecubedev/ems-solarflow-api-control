# SPDX-License-Identifier: AGPL-3.0-or-later
"""Network suggestion detection / classification tests (faked interfaces)."""

import pytest

from admin import networks
from admin.discovery import validate_cidr
from admin.networks import (
    DOCKER_ONLY_WARNING,
    NO_NETWORK_WARNING,
    PLATFORM_WARNING,
    Interface,
    build_suggestions,
    detect_network_suggestions,
)

pytestmark = pytest.mark.simulation


def _by_cidr(result, cidr):
    for entry in result["networks"]:
        if entry["cidr"] == cidr:
            return entry
    return None


def test_private_default_route_is_recommended():
    result = build_suggestions(
        [Interface("eth0", "192.168.178.25", "255.255.255.0")], default_iface="eth0"
    )
    entry = _by_cidr(result, "192.168.178.0/24")
    assert entry is not None
    assert entry["scan_recommended"] is True
    assert entry["is_default_route"] is True
    assert entry["label"] == "Recommended LAN"
    assert entry["priority"] == 100
    assert result["manual_entry_supported"] is True


def test_loopback_is_ignored():
    result = build_suggestions([Interface("lo", "127.0.0.1", "255.0.0.0")])
    assert result["networks"] == []
    assert NO_NETWORK_WARNING in result["warnings"]


def test_docker_like_is_deprioritized():
    result = build_suggestions([Interface("docker0", "172.17.0.1", "255.255.0.0")])
    entry = _by_cidr(result, "172.17.0.0/24")
    assert entry is not None
    assert entry["is_docker_like"] is True
    assert entry["scan_recommended"] is False
    assert entry["priority"] == 10
    assert DOCKER_ONLY_WARNING in result["warnings"]


def test_vpn_like_is_advanced():
    result = build_suggestions([Interface("wg0", "192.168.50.2", "255.255.255.0")])
    entry = _by_cidr(result, "192.168.50.0/24")
    assert entry is not None
    assert entry["is_vpn_like"] is True
    assert entry["scan_recommended"] is False
    assert entry["label"] == "Advanced (VPN)"


def test_broad_network_produces_scan_safe_24():
    result = build_suggestions([Interface("eth0", "10.20.30.5", "255.255.0.0")])
    entry = _by_cidr(result, "10.20.30.0/24")
    assert entry is not None
    assert entry["original_cidr"] == "10.20.0.0/16"
    assert entry["prefix_length"] == 16
    assert "broad" in entry["reason"].lower()
    assert entry["scan_recommended"] is True


def test_duplicate_cidrs_are_deduplicated():
    result = build_suggestions(
        [
            Interface("eth0", "192.168.178.25", "255.255.255.0"),
            Interface("eth1", "192.168.178.30", "255.255.255.0"),
        ],
        default_iface="eth0",
    )
    matches = [e for e in result["networks"] if e["cidr"] == "192.168.178.0/24"]
    assert len(matches) == 1
    # Higher-priority (default-route) interface wins the dedupe.
    assert matches[0]["interface"] == "eth0"


def test_public_network_is_not_suggested():
    result = build_suggestions([Interface("eth0", "8.8.8.8", "255.255.255.0")])
    assert result["networks"] == []


def test_link_local_is_advanced_only():
    result = build_suggestions([Interface("eth0", "169.254.10.5", "255.255.0.0")])
    entry = _by_cidr(result, "169.254.10.0/24")
    assert entry is not None
    assert entry["is_link_local"] is True
    assert entry["scan_recommended"] is False


def test_vpn_only_still_warns_no_lan():
    result = build_suggestions([Interface("wg0", "192.168.50.2", "255.255.255.0")])
    assert result["networks"]
    assert result["warnings"]  # advanced-only: a "no obvious LAN" warning is set


def test_suggested_cidrs_are_valid_scan_inputs():
    result = build_suggestions(
        [
            Interface("eth0", "192.168.178.25", "255.255.255.0"),
            Interface("wg0", "10.8.0.2", "255.255.255.0"),
            Interface("eth1", "10.20.30.5", "255.255.0.0"),
        ],
        default_iface="eth0",
    )
    assert result["networks"]
    for entry in result["networks"]:
        # Every suggested CIDR must pass the scan endpoint's strict validation.
        assert validate_cidr(entry["cidr"])


def test_networks_sorted_192_then_10_then_rest():
    result = build_suggestions(
        [
            Interface("eth2", "172.20.5.4", "255.255.255.0"),
            Interface("eth1", "10.0.0.5", "255.255.255.0"),
            Interface("eth0", "192.168.5.10", "255.255.255.0"),
        ]
    )
    order = [entry["cidr"] for entry in result["networks"]]
    assert order == ["192.168.5.0/24", "10.0.0.0/24", "172.20.5.0/24"]


def test_unavailable_platform_returns_warning_not_failure(monkeypatch):
    def _boom():
        raise NotImplementedError("no fcntl here")

    monkeypatch.setattr(networks, "enumerate_interfaces", _boom)
    result = detect_network_suggestions()
    assert result["networks"] == []
    assert PLATFORM_WARNING in result["warnings"]
    assert result["manual_entry_supported"] is True
