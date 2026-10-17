# SPDX-License-Identifier: AGPL-3.0-or-later
"""The deterministic Admin never sweeps the test machine's LAN.

Browser specs run against ``EMS_ADMIN_TEST_MODE`` and seed the devices they
need. A network scan the page starts there must therefore check every host
without contacting one: the real scan would send HTTP requests and Modbus TCP
reads to whatever network the test machine sits on.
"""

import socket

import pytest

from admin import test_support
from admin.discovery import CidrValidationError

pytestmark = [
    pytest.mark.admin,
    pytest.mark.setup,
    pytest.mark.maintenance,
    pytest.mark.unit,
]


@pytest.fixture
def no_sockets(monkeypatch):
    def refuse(*_args, **_kwargs):
        raise AssertionError("the test-mode scan opened a connection")

    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket.socket, "connect", refuse)


def test_the_test_mode_scan_checks_every_host_and_contacts_none(no_sockets):
    updates = []

    devices, errors = test_support.offline_network_scan(
        "192.168.1.0/29", timeout_ms=200, max_workers=4, progress_callback=updates.append
    )

    assert (devices, errors) == ([], [])
    assert [update["checked_hosts"] for update in updates] == [1, 2, 3, 4, 5, 6]
    assert {update["total_hosts"] for update in updates} == {6}
    assert updates[-1]["current_ip"] == "192.168.1.6"


def test_the_test_mode_scan_refuses_what_the_real_scan_refuses():
    with pytest.raises(CidrValidationError):
        test_support.offline_network_scan("8.8.8.0/24")


def test_the_test_mode_runtime_scans_offline(tmp_path):
    runtime = test_support.build_test_runtime(data_dir=str(tmp_path))

    assert runtime.registry._scan_runner is test_support.offline_network_scan
