# SPDX-FileCopyrightText: 2026 basecubedev
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The deterministic Admin never sweeps the test machine's LAN.

Browser specs run against ``EMS_ADMIN_TEST_MODE`` and seed the devices they
need. A network scan the page starts there must therefore check every host
without contacting one: the real scan would send HTTP requests and Modbus TCP
reads to whatever network the test machine sits on. The router probe and the
MQTT broker probe are held to the same rule; only a broker a test brought up on
this machine can be reached.
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
    """Every connection attempt, refused and recorded: a probe that swallows the refusal still shows up here."""

    attempts = []

    def refuse(*args, **_kwargs):
        attempts.append(args)
        raise OSError("the test-mode Admin opened a connection")

    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", refuse)
    return attempts


def test_the_test_mode_scan_checks_every_host_and_contacts_none(no_sockets):
    updates = []

    devices, errors = test_support.offline_network_scan(
        "192.168.1.0/29", timeout_ms=200, max_workers=4, progress_callback=updates.append
    )

    assert (devices, errors) == ([], [])
    assert no_sockets == []
    assert [update["checked_hosts"] for update in updates] == [1, 2, 3, 4, 5, 6]
    assert {update["total_hosts"] for update in updates} == {6}
    assert updates[-1]["current_ip"] == "192.168.1.6"


def test_the_test_mode_scan_refuses_what_the_real_scan_refuses():
    with pytest.raises(CidrValidationError):
        test_support.offline_network_scan("8.8.8.0/24")


def test_the_test_mode_runtime_scans_offline(tmp_path):
    runtime = test_support.build_test_runtime(data_dir=str(tmp_path))

    assert runtime.registry._scan_runner is test_support.offline_network_scan


def test_the_test_mode_runtime_probes_no_router(tmp_path, no_sockets):
    runtime = test_support.build_test_runtime(data_dir=str(tmp_path))

    result = runtime.gateway_prober(timeout_ms=200, max_workers=4)

    assert result["probed"] > 0
    assert [candidate for candidate in result["candidates"] if candidate["status"] == "reachable"] == []
    assert no_sockets == []


def test_the_test_mode_runtime_probes_no_mqtt_broker_on_the_lan(tmp_path, no_sockets):
    runtime = test_support.build_test_runtime(data_dir=str(tmp_path))

    result = runtime.mqtt_discovery.probe("192.168.1.0/29", timeout_ms=200, max_workers=4)

    assert result["probed"] > 0
    assert result["found"] == 0
    assert no_sockets == []


def test_a_broker_on_this_machine_stays_reachable_in_test_mode(tmp_path):
    runtime = test_support.build_test_runtime(data_dir=str(tmp_path))
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]

        assert runtime.mqtt_discovery._connector("127.0.0.1", port, 2.0) is True
        assert runtime.mqtt_discovery._connector("localhost", port, 2.0) is True
