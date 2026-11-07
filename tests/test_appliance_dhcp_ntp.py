# SPDX-FileCopyrightText: 2026 basecubedev
# SPDX-License-Identifier: AGPL-3.0-or-later
"""A LAN's own time server reaches the clock, and a refusal names what it asked.

The board has no real-time clock. systemd-timesyncd learns time servers from
systemd-networkd, which this appliance masks, so on a network that blocks NTP to
the internet and offers its own server by DHCP the clock stayed at the image's
build time, and every signed download was refused with "wait" for ever.
"""

import os
import subprocess
from pathlib import Path

import re

import pytest

pytestmark = [pytest.mark.integration, pytest.mark.simulation, pytest.mark.appliance]

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "packaging/appliance/network/50-ems-appliance-dhcp-ntp"


class Dispatcher:
    """NetworkManager's dispatcher calling the script, with systemctl recorded."""

    def __init__(self, tmp_path):
        self.drop_ins = tmp_path / "timesyncd.conf.d"
        self.bin = tmp_path / "bin"
        self.bin.mkdir()
        self.calls = tmp_path / "systemctl.log"
        systemctl = self.bin / "systemctl"
        systemctl.write_text(f'#!/bin/sh\necho "$*" >> {self.calls}\n')
        systemctl.chmod(0o755)

    def run(self, interface, action, servers=None):
        environment = {
            "PATH": f"{self.bin}:/usr/bin:/bin",
            "EMS_NTP_DROP_IN_DIR": str(self.drop_ins),
        }
        if servers is not None:
            environment["DHCP4_NTP_SERVERS"] = servers
        return subprocess.run(
            ["sh", str(SCRIPT), interface, action],
            capture_output=True, text=True, env=environment, check=False,
        )

    def drop_in(self, interface):
        path = self.drop_ins / f"50-ems-appliance-dhcp-{interface}.conf"
        return path.read_text(encoding="utf-8") if path.exists() else None

    def restarts(self):
        if not self.calls.exists():
            return []
        return [line for line in self.calls.read_text().splitlines() if "timesyncd" in line]


@pytest.fixture
def dispatcher(tmp_path):
    return Dispatcher(tmp_path)


def test_a_leases_time_servers_reach_timesyncd(dispatcher):
    result = dispatcher.run("eth0", "up", "192.168.1.1 ntp.example.lan")

    assert result.returncode == 0, result.stderr
    assert dispatcher.drop_in("eth0") == "[Time]\nNTP=192.168.1.1 ntp.example.lan\n"
    assert dispatcher.restarts() == ["try-restart systemd-timesyncd.service"]


def test_the_same_servers_again_do_not_restart_timesyncd(dispatcher):
    dispatcher.run("eth0", "up", "192.168.1.1")
    dispatcher.run("eth0", "dhcp4-change", "192.168.1.1")

    assert len(dispatcher.restarts()) == 1


def test_a_lease_that_goes_takes_its_servers_with_it(dispatcher):
    dispatcher.run("wlan0", "up", "10.0.0.1")
    dispatcher.run("wlan0", "down")

    assert dispatcher.drop_in("wlan0") is None
    assert len(dispatcher.restarts()) == 2


def test_a_lease_without_time_servers_leaves_timesyncd_alone(dispatcher):
    dispatcher.run("eth0", "up", "")

    assert dispatcher.drop_in("eth0") is None
    assert dispatcher.restarts() == []


def test_a_server_name_cannot_write_anything_but_a_server(dispatcher):
    """The lease comes from whoever answers DHCP on the LAN."""

    dispatcher.run("eth0", "up", "192.168.1.1\n[Time]\nFallbackNTP=evil.example 10.0.0.$(id)")

    assert dispatcher.drop_in("eth0") == "[Time]\nNTP=192.168.1.1\n"


def test_an_interface_name_cannot_choose_the_file(dispatcher):
    dispatcher.run("../../etc/x", "up", "192.168.1.1")

    assert not dispatcher.drop_ins.exists() or list(dispatcher.drop_ins.iterdir()) == []


def test_the_package_installs_the_script_where_networkmanager_runs_it():
    build = (ROOT / "packaging/appliance/build-deb.sh").read_text(encoding="utf-8")

    assert (
        'install -m 0755 "$PACKAGING/network/50-ems-appliance-dhcp-ntp" \\\n'
        '        "$STAGE/usr/lib/NetworkManager/dispatcher.d/"'
    ) in build
    assert os.access(SCRIPT, os.R_OK)


# --- what the refusal says ---------------------------------------------------


class TimesyncRunner:
    def __init__(self, show, timesync):
        self.answers = {"show": show, "show-timesync": timesync}

    def available(self, name):
        return name == "timedatectl"

    def run(self, name, args, timeout=None):
        from types import SimpleNamespace

        return SimpleNamespace(ok=True, stdout=self.answers.get(args[0], ""))


def test_the_time_probe_names_the_servers_timesyncd_asks(tmp_path):
    from appliance.hostprobe import HostProbe

    runner = TimesyncRunner(
        "Timezone=Europe/Berlin\nNTPSynchronized=no\n",
        "LinkNTPServers=\nSystemNTPServers=192.168.1.1\n"
        "FallbackNTPServers=0.debian.pool.ntp.org 1.debian.pool.ntp.org\n"
        "ServerName=192.168.1.1\nServerAddress=192.168.1.1\n",
    )

    record = HostProbe(runner, root=str(tmp_path)).system_time()

    assert record["ntp_synchronized"] is False
    assert record["ntp_servers"] == ["192.168.1.1"]
    assert record["ntp_fallback_servers"] == ["0.debian.pool.ntp.org", "1.debian.pool.ntp.org"]


def test_a_synchronised_clock_is_not_asked_about_its_servers(tmp_path):
    from appliance.hostprobe import HostProbe

    runner = TimesyncRunner("NTPSynchronized=yes\n", "SystemNTPServers=192.168.1.1\n")

    record = HostProbe(runner, root=str(tmp_path)).system_time()

    assert "ntp_servers" not in record


def test_the_clock_refusal_names_the_servers_and_a_way_out():
    from appliance.manager_update import clock_refusal_message

    message = clock_refusal_message(
        {"ntp_synchronized": False, "ntp_servers": [], "ntp_fallback_servers": ["0.debian.pool.ntp.org"]}
    )

    assert re.search(r"(?<![\w.])0\.debian\.pool\.ntp\.org(?![\w-])", message), message
    assert "DHCP" in message
    assert "timesyncd.conf.d" in message
