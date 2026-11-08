# SPDX-FileCopyrightText: 2026 basecubedev
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The Manager's sessions end when they say they do, whatever the wall clock does.

No test pinned the idle limit, the absolute cap under constant use, or what a
limit of 0 meant, and the store ran on the wall clock of a board without a
real-time clock: NTP stepping it after boot stretched or ended every session.
"""

import pytest

from appliance.auth import SessionStore
from appliance.auth import container_networks as read_container_networks
from appliance.auth import host_addresses as read_host_addresses

pytestmark = [pytest.mark.unit, pytest.mark.appliance]


class Clock:
    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now


def store(clock, wall=None, *, idle=1800, absolute=43200):
    return SessionStore(idle_timeout=idle, absolute_max=absolute, time_fn=clock, wall_fn=wall or clock)


def test_an_idle_session_ends_after_the_idle_limit():
    clock = Clock()
    sessions = store(clock)
    session = sessions.create("g1")

    clock.now += 1799
    assert sessions.get(session.session_id, "g1") is not None
    clock.now += 2
    assert sessions.get(session.session_id, "g1") is None


def test_constant_use_does_not_outlive_the_absolute_cap():
    clock = Clock()
    sessions = store(clock, idle=1800, absolute=7200)
    session = sessions.create("g1")

    for _ in range(8):
        clock.now += 1000
        sessions.touch(session.session_id, "g1")

    assert sessions.get(session.session_id, "g1") is None


@pytest.mark.parametrize("value", [0, -1])
def test_a_limit_of_zero_or_less_is_refused_not_unlimited(value):
    with pytest.raises(ValueError):
        SessionStore(idle_timeout=value)
    with pytest.raises(ValueError):
        SessionStore(absolute_max=value)


def test_a_wall_clock_step_neither_ends_nor_stretches_a_session():
    """The board has no RTC; NTP steps the wall clock after boot."""

    monotonic, wall = Clock(500.0), Clock(1_600_000_000.0)
    sessions = store(monotonic, wall, idle=1800)
    session = sessions.create("g1")

    wall.now += 86_400 * 365
    assert sessions.get(session.session_id, "g1") is not None
    wall.now -= 86_400 * 730
    monotonic.now += 1801
    assert sessions.get(session.session_id, "g1") is None


def test_the_displayed_expiry_is_on_the_wall_clock():
    monotonic, wall = Clock(500.0), Clock(1_600_000_000.0)
    sessions = store(monotonic, wall, idle=1800, absolute=43200)
    session = sessions.create("g1")

    assert sessions.expires_at_wall(session) == pytest.approx(1_600_000_000.0 + 1800)


class CountingLock:
    def __init__(self):
        self.entered = 0

    def __enter__(self):
        self.entered += 1
        return self

    def __exit__(self, *exc):
        return False


@pytest.mark.parametrize(
    "call",
    [
        lambda s, sid: s.create("g1"),
        lambda s, sid: s.get(sid, "g1"),
        lambda s, sid: s.touch(sid, "g1"),
        lambda s, sid: s.destroy(sid),
        lambda s, sid: s.destroy_all(),
        lambda s, sid: s.cleanup(),
    ],
)
def test_every_change_to_the_session_table_holds_its_lock(call):
    """cleanup() iterated the table while another request thread popped from
    it: "dictionary changed size during iteration" dropped a sign-in."""

    sessions = store(Clock())
    sid = sessions.create("g1").session_id
    sessions._lock = CountingLock()

    call(sessions, sid)

    assert sessions._lock.entered >= 1


# --- a session belongs to the client that signed in --------------------------


@pytest.fixture(autouse=True)
def this_host(monkeypatch):
    """The image's container pool and one LAN address of the appliance's own."""

    import ipaddress

    from appliance import auth

    monkeypatch.setattr(auth, "container_networks", lambda: (auth.CONTAINER_NETWORK,))
    monkeypatch.setattr(
        auth,
        "host_addresses",
        lambda: frozenset({ipaddress.ip_address("192.168.1.26"), ipaddress.ip_address("2001:db8:1:2::26")}),
    )


@pytest.mark.parametrize("own", ["192.168.1.26", "2001:db8:1:2::26"])
def test_a_session_is_never_honoured_from_the_hosts_own_address(own):
    """A host-networked service reaches the Manager from the host's LAN address,
    and the host's IPv6 address shares the /64 its LAN clients have."""

    from appliance.auth import session_binding

    sessions = store(Clock())
    session = sessions.create("g1", binding=session_binding("2001:db8:1:2::10"))

    assert sessions.get(session.session_id, "g1", binding=session_binding(own)) is None


def test_docker_networks_are_its_pools_and_its_live_bridges_not_the_lan(tmp_path):
    """Docker's built-in default pools include 192.168.0.0/16: assumed for a
    card flashed before this image, they made every FritzBox client a
    container and took its dual-stack tolerance away."""

    import ipaddress

    (tmp_path / "route").write_text(
        "Iface\tDestination\tGateway\tFlags\tRefCnt\tUse\tMetric\tMask\tMTU\tWindow\tIRTT\n"
        "eth0\t00B2A8C0\t00000000\t0001\t0\t0\t0\t00FFFFFF\t0\t0\t0\n"
        "docker0\t000011AC\t00000000\t0001\t0\t0\t0\t0000FFFF\t0\t0\t0\n"
        "br-0123abcd\t00C0A8C0\t00000000\t0001\t0\t0\t0\t00F0FFFF\t0\t0\t0\n",
        encoding="utf-8",
    )

    networks = read_container_networks(path=tmp_path / "missing.json", proc=tmp_path)

    assert ipaddress.ip_network("172.17.0.0/16") in networks
    assert ipaddress.ip_network("192.168.192.0/20") in networks
    assert not any(ipaddress.ip_address("192.168.178.20") in network for network in networks)


def test_this_hosts_addresses_are_read_from_the_kernel(tmp_path):
    import ipaddress


    (tmp_path / "fib_trie").write_text(
        "Main:\n  +-- 0.0.0.0/0 3 0 5\n     |-- 192.168.1.26\n        /32 host LOCAL\n"
        "     |-- 192.168.1.255\n        /32 link BROADCAST\n",
        encoding="utf-8",
    )
    (tmp_path / "if_inet6").write_text(
        "20010db8000100020000000000000026 02 40 00 80 eth0\n", encoding="utf-8"
    )

    found = read_host_addresses(proc=tmp_path, now=-1e9)

    assert ipaddress.ip_address("192.168.1.26") in found
    assert ipaddress.ip_address("2001:db8:1:2::26") in found
    assert ipaddress.ip_address("192.168.1.255") not in found


def test_a_session_presented_from_another_address_is_not_honoured():
    """The cookie is scoped to the host, not the port, so the browser hands it
    to EMS and the Admin on the same name; a container that reads it must not
    be able to drive the Manager with it."""

    from appliance.auth import session_binding

    sessions = store(Clock())
    session = sessions.create("g1", binding=session_binding("192.168.1.20"))

    assert sessions.get(session.session_id, "g1", binding=session_binding("172.18.0.3")) is None
    assert sessions.get(session.session_id, "g1", binding=session_binding("192.168.1.20")) is session


@pytest.mark.parametrize(
    "signed_in, later",
    [
        ("192.168.1.20", "2001:db8:1:2::10"),
        ("2001:db8:1:2::10", "192.168.1.20"),
        ("2001:db8:1:2::10", "2001:db8:9:2::10"),
    ],
)
def test_a_session_survives_the_browser_changing_address_family_or_prefix(signed_in, later):
    """One dual-stack browser reaches the appliance over either family, and a
    provider rotates the IPv6 prefix; neither is somebody else's copy."""

    from appliance.auth import session_binding

    sessions = store(Clock())
    session = sessions.create("g1", binding=session_binding(signed_in))

    assert sessions.get(session.session_id, "g1", binding=session_binding(later)) is session


@pytest.mark.parametrize(
    "signed_in, later",
    [
        ("2001:db8:1:2::10", "172.18.0.3"),
        ("192.168.1.20", "127.0.0.1"),
        ("192.168.1.20", "::1"),
        ("127.0.0.1", "192.168.1.20"),
        ("172.18.0.3", "192.168.1.20"),
    ],
)
def test_a_session_is_never_honoured_across_this_host_and_the_network(signed_in, later):
    from appliance.auth import session_binding

    sessions = store(Clock())
    session = sessions.create("g1", binding=session_binding(signed_in))

    assert sessions.get(session.session_id, "g1", binding=session_binding(later)) is None


def test_the_container_network_a_session_refuses_is_the_one_docker_hands_out():
    import json
    from pathlib import Path

    from appliance.auth import CONTAINER_NETWORK

    overlay = Path(__file__).resolve().parents[1] / (
        "packaging/appliance/image/layer/ems-appliance.rootfs-overlay"
    )
    daemon = json.loads((overlay / "etc/docker/daemon.json").read_text())
    pools = [pool["base"] for pool in daemon["default-address-pools"]]

    assert pools == [str(CONTAINER_NETWORK)]


def test_an_ipv6_client_keeps_its_session_across_privacy_addresses():
    from appliance.auth import session_binding

    assert session_binding("2001:db8:1:2::10") == session_binding("2001:db8:1:2:abcd::99")
    assert session_binding("::ffff:192.168.1.20") == session_binding("192.168.1.20")
