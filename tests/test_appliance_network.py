# SPDX-License-Identifier: AGPL-3.0-or-later
"""Network overview, WLAN change with automatic revert, and hostname changes.

A WLAN change can lock the operator out, so the previous profile is preserved
and reactivated when the new network does not reach connectivity. The
passphrase must never reach argv, the operation record or a log.
"""

import pytest

from appliance.agent import AgentHandlers
from appliance.network import parse_device_status, parse_wifi_list
from appliance.operations import STATE_FAILED_TERMINAL, STATE_SUCCEEDED
from tests.helpers.appliance import build_test_services

pytestmark = [pytest.mark.integration, pytest.mark.simulation, pytest.mark.appliance]

PASSPHRASE = "correct-horse-battery"


def handlers_for(services):
    return AgentHandlers(services, executor=lambda target: target())


def plan_and_execute(services, operation, **fields):
    handlers = handlers_for(services)
    planned = handlers.dispatch({"operation": operation, **fields})
    handlers.dispatch(
        {
            "operation": "operations.execute",
            "operation_id": planned["operation"]["operation_id"],
            "confirmation_token": planned["confirmation_token"],
        }
    )
    return services.operations.get(planned["operation"]["operation_id"]), planned["plan"]


# --- parsing ---------------------------------------------------------------


def test_device_status_is_parsed():
    devices = parse_device_status("wlan0:wifi:connected:HomeNet\neth0:ethernet:unavailable:\n")
    assert [item.device for item in devices] == ["wlan0", "eth0"]
    assert devices[0].connection == "HomeNet"


def test_escaped_colons_in_a_connection_name_are_handled():
    devices = parse_device_status("wlan0:wifi:connected:Home\\:Net\n")
    assert devices[0].connection == "Home:Net"


def test_wifi_list_is_sorted_by_signal():
    networks = parse_wifi_list("yes:HomeNet:71:WPA2\n:GuestNet:44:WPA2\n:OpenNet:22:\n")
    assert [item["ssid"] for item in networks] == ["HomeNet", "GuestNet", "OpenNet"]
    assert networks[0]["active"] is True
    assert networks[2]["security"] == "open"


# --- overview --------------------------------------------------------------


def test_network_overview_lists_interfaces_addresses_and_mdns(tmp_path):
    services = build_test_services(tmp_path)
    status = services.network.status()

    assert status["hostname"] == "ems-solarflow"
    assert status["mdns"] == "ems-solarflow.local"
    assert status["connectivity"] == "full"
    devices = {item["device"] for item in status["interfaces"]}
    assert devices == {"wlan0", "eth0"}
    wlan = [item for item in status["interfaces"] if item["device"] == "wlan0"][0]
    assert wlan["addresses"] == ["192.168.1.50/24"]
    assert wlan["gateway"] == "192.168.1.1"
    assert wlan["dns"] == ["192.168.1.1"]


def test_missing_network_manager_degrades_without_raising(tmp_path):
    services = build_test_services(tmp_path)
    services.host.tools.discard("nmcli")
    status = services.network.status()
    assert status["error"] == "network_manager_unavailable"
    assert status["interfaces"] == []


def test_wifi_scan_returns_visible_networks(tmp_path):
    services = build_test_services(tmp_path)
    networks = handlers_for(services).dispatch({"operation": "network.wifi.scan"})["networks"]
    assert {item["ssid"] for item in networks} == {"HomeNet", "GuestNet", "OpenNet"}


# --- WLAN change -----------------------------------------------------------


def test_wifi_plan_records_the_previous_profile_and_warns(tmp_path):
    services = build_test_services(tmp_path)
    plan = handlers_for(services).dispatch(
        {"operation": "network.wifi.plan", "ssid": "GuestNet", "passphrase": PASSPHRASE}
    )["plan"]

    assert plan["ssid"] == "GuestNet"
    assert plan["previous_profile"] == "HomeNet"
    assert plan["revert_timeout_seconds"] >= 1
    assert "disconnect" in plan["warning"]


def test_wifi_plan_refuses_an_invisible_network_unless_marked_hidden(tmp_path):
    services = build_test_services(tmp_path)
    handlers = handlers_for(services)
    with pytest.raises(Exception) as excinfo:
        handlers.dispatch(
            {"operation": "network.wifi.plan", "ssid": "NoSuchNet", "passphrase": PASSPHRASE}
        )
    assert getattr(excinfo.value, "code", "") == "wifi_network_not_found"


def test_hidden_network_may_be_planned(tmp_path):
    services = build_test_services(tmp_path)
    plan = handlers_for(services).dispatch(
        {
            "operation": "network.wifi.plan",
            "ssid": "HiddenNet",
            "passphrase": PASSPHRASE,
            "hidden": True,
        }
    )["plan"]
    assert plan["hidden"] is True


def test_successful_wifi_change_reports_connectivity(tmp_path):
    services = build_test_services(tmp_path)
    operation, _ = plan_and_execute(
        services, "network.wifi.plan", ssid="GuestNet", passphrase=PASSPHRASE
    )
    assert operation.state == STATE_SUCCEEDED
    assert operation.result["reverted"] is False
    assert operation.result["ssid"] == "GuestNet"


def test_failed_wifi_change_reverts_to_the_previous_profile(tmp_path):
    services = build_test_services(tmp_path)
    services.host.wifi_connect_ok = False
    services.host.nmcli_connectivity = "none"

    operation, _ = plan_and_execute(
        services, "network.wifi.plan", ssid="GuestNet", passphrase=PASSPHRASE
    )

    assert operation.state == STATE_FAILED_TERMINAL
    assert operation.error["code"] == "wifi_connection_failed"
    assert operation.result["reverted"] is True
    assert ("nmcli", ("connection", "up", "HomeNet"), None) in services.host.calls


def test_the_previous_profile_is_never_deleted(tmp_path):
    services = build_test_services(tmp_path)
    services.host.wifi_connect_ok = False
    services.host.nmcli_connectivity = "none"
    plan_and_execute(services, "network.wifi.plan", ssid="GuestNet", passphrase=PASSPHRASE)

    deletions = [args for tool, args, _ in services.host.calls if tool == "nmcli" and "delete" in args]
    assert deletions == []


def test_the_passphrase_never_reaches_argv(tmp_path):
    services = build_test_services(tmp_path)
    plan_and_execute(services, "network.wifi.plan", ssid="GuestNet", passphrase=PASSPHRASE)

    for tool, args, stdin in services.host.calls:
        assert PASSPHRASE not in " ".join(args), (tool, args)
    connect_calls = [
        (args, stdin)
        for tool, args, stdin in services.host.calls
        if tool == "nmcli" and "connect" in args
    ]
    assert connect_calls
    assert connect_calls[0][1] == f"{PASSPHRASE}\n"
    assert "--ask" in connect_calls[0][0]


def test_the_passphrase_never_reaches_the_operation_record(tmp_path):
    services = build_test_services(tmp_path)
    operation, _ = plan_and_execute(
        services, "network.wifi.plan", ssid="GuestNet", passphrase=PASSPHRASE
    )
    assert PASSPHRASE not in str(operation.to_dict())
    assert operation.requested_target["has_passphrase"] is True


def test_an_open_network_needs_no_passphrase(tmp_path):
    services = build_test_services(tmp_path)
    operation, _ = plan_and_execute(
        services, "network.wifi.plan", ssid="OpenNet", passphrase=""
    )
    assert operation.state == STATE_SUCCEEDED


def test_a_short_passphrase_is_refused_before_anything_happens(tmp_path):
    services = build_test_services(tmp_path)
    services.host.calls.clear()
    handlers = handlers_for(services)
    with pytest.raises(Exception) as excinfo:
        handlers.dispatch(
            {"operation": "network.wifi.plan", "ssid": "GuestNet", "passphrase": "short"}
        )
    assert getattr(excinfo.value, "code", "") == "invalid_wifi_passphrase"
    assert services.host.calls == []


# --- hostname --------------------------------------------------------------


def test_hostname_plan_shows_the_new_urls_and_warns(tmp_path):
    services = build_test_services(tmp_path)
    plan = handlers_for(services).dispatch(
        {"operation": "network.hostname.plan", "hostname": "ems-pi5"}
    )["plan"]
    assert plan["hostname"] == "ems-pi5"
    assert plan["previous_hostname"] == "ems-solarflow"
    assert plan["new_url"].startswith("http://ems-pi5.local:")
    assert "URL changes" in plan["warning"]


def test_hostname_change_updates_the_host_and_mdns(tmp_path):
    services = build_test_services(tmp_path)
    operation, _ = plan_and_execute(services, "network.hostname.plan", hostname="ems-pi5")

    assert operation.state == STATE_SUCCEEDED
    assert services.host.hostname == "ems-pi5"
    assert ("systemctl", ("try-restart", "avahi-daemon.service"), None) in services.host.calls


def test_an_invalid_hostname_is_refused(tmp_path):
    services = build_test_services(tmp_path)
    handlers = handlers_for(services)
    with pytest.raises(Exception) as excinfo:
        handlers.dispatch({"operation": "network.hostname.plan", "hostname": "not a hostname"})
    assert getattr(excinfo.value, "code", "") == "invalid_hostname"


def test_an_unchanged_hostname_is_refused(tmp_path):
    services = build_test_services(tmp_path)
    handlers = handlers_for(services)
    with pytest.raises(Exception) as excinfo:
        handlers.dispatch({"operation": "network.hostname.plan", "hostname": "ems-solarflow"})
    assert getattr(excinfo.value, "code", "") == "hostname_unchanged"


def test_a_single_slot_appliance_still_changes_its_hostname(tmp_path):
    services = build_test_services(tmp_path)

    operation, _ = plan_and_execute(services, "network.hostname.plan", hostname="ems-pi5")

    assert operation.state == STATE_SUCCEEDED
    assert services.host.hostname == "ems-pi5"


# --- what "the WLAN worked" is measured on -----------------------------------


def test_a_wlan_without_internet_is_still_a_successful_join(tmp_path):
    """`nmcli general CONNECTIVITY` is an HTTP fetch to the internet.

    An EMS is sold on having no cloud dependency, and a LAN behind a filtering
    resolver never reaches `full`. Tearing the join down after 90 s would mean
    that network can never be joined at all.
    """

    services = build_test_services(tmp_path)
    services.host.nmcli_connectivity = "limited"

    operation, _ = plan_and_execute(
        services, "network.wifi.plan", ssid="GuestNet", passphrase=PASSPHRASE
    )

    assert operation.state == STATE_SUCCEEDED
    assert operation.result["reverted"] is False
    assert operation.result["ssid"] == "GuestNet"


def test_a_wlan_that_never_joined_is_reverted_even_with_ethernet_up(tmp_path):
    """The other direction: host-wide connectivity says nothing about the WLAN.

    With a cable plugged in the host reads `full` whatever the radio did, so a
    join onto a wrong network would be recorded as success and never reverted --
    and the lockout appears later, when the cable is pulled.
    """

    services = build_test_services(tmp_path)
    services.host.wifi_connect_ok = False
    services.host.nmcli_connectivity = "full"

    operation, _ = plan_and_execute(
        services, "network.wifi.plan", ssid="GuestNet", passphrase=PASSPHRASE
    )

    assert operation.state == STATE_FAILED_TERMINAL
    assert operation.error["code"] == "wifi_connection_failed"
    assert operation.result["reverted"] is True


def test_a_joined_wlan_without_an_address_is_not_accepted(tmp_path):
    """Associated is not reachable: DHCP still has to have answered."""

    services = build_test_services(tmp_path)
    services.host.wifi_address = ""
    services.host.nmcli_connectivity = "full"

    operation, _ = plan_and_execute(
        services, "network.wifi.plan", ssid="GuestNet", passphrase=PASSPHRASE
    )

    assert operation.state == STATE_FAILED_TERMINAL
    assert operation.result["reverted"] is True


def test_a_cancelled_wlan_plan_does_not_retain_the_passphrase(tmp_path):
    """The plan-to-apply window is the whole intended lifetime of a PSK."""

    services = build_test_services(tmp_path)
    handlers = handlers_for(services)
    planned = handlers.dispatch(
        {"operation": "network.wifi.plan", "ssid": "GuestNet", "passphrase": PASSPHRASE}
    )
    operation_id = planned["operation"]["operation_id"]
    assert services.network._secrets

    handlers.dispatch({"operation": "operations.cancel", "operation_id": operation_id})

    assert not services.network._secrets


def test_a_failed_scan_is_reported_by_the_scan_operation(tmp_path):
    """An empty list said "no networks here", which is a different fact."""

    from appliance.network import NetworkError

    services = build_test_services(tmp_path)
    services.host.nmcli_scan_ok = False

    with pytest.raises(NetworkError) as error:
        services.network.scan()

    assert error.value.code == "wifi_scan_failed"


def test_a_failed_scan_still_leaves_an_overview_to_read(tmp_path):
    """Signal strength is decoration; losing it must not lose the whole page."""

    services = build_test_services(tmp_path)
    services.host.nmcli_scan_ok = False

    record = services.network.status()

    assert record["interfaces"]


# --- the revert has to survive the process that armed it ----------------------


def test_a_crash_inside_the_revert_window_still_restores_the_previous_profile(tmp_path):
    """The regression: the revert was an in-memory step of the executing call.

    A power cut or an agent restart inside the 90-180 s window left the new
    profile active with nothing to take it back, and NetworkManager reconnects
    to it on every boot afterwards -- the lockout the whole feature exists to
    prevent.
    """

    services = build_test_services(tmp_path)
    services.network.arm_revert("op-1", "HomeNet")

    assert services.network.pending_revert() is not None

    restored = services.network.recover_revert()

    assert restored == "HomeNet"
    assert ("nmcli", ("connection", "up", "HomeNet"), None) in services.host.calls
    assert services.network.pending_revert() is None


def test_a_completed_wlan_change_leaves_no_revert_armed(tmp_path):
    from appliance.agent import AgentHandlers

    services = build_test_services(tmp_path)
    handlers = AgentHandlers(services, executor=lambda target: target())
    planned = handlers.dispatch(
        {"operation": "network.wifi.plan", "ssid": "GuestNet", "passphrase": "correct-horse"}
    )
    handlers.dispatch(
        {
            "operation": "operations.execute",
            "operation_id": planned["operation"]["operation_id"],
            "confirmation_token": planned["confirmation_token"],
        }
    )

    assert services.network.pending_revert() is None


def test_the_agent_recovers_an_armed_revert_at_startup():
    """A durable intent nothing reads at boot is not durable."""

    import inspect

    from appliance import cli

    source = inspect.getsource(cli.command_agent)

    assert "recover_revert()" in source


def test_a_timezone_change_is_planned_confirmed_and_stored(tmp_path):
    """The whole path: plan, confirm, and a value that outlives the process."""

    from appliance.config import load_config

    services = build_test_services(tmp_path)
    operation, plan = plan_and_execute(
        services, "system.timezone.plan", timezone="Europe/Berlin"
    )

    assert operation.state == STATE_SUCCEEDED
    assert plan["timezone"] == "Europe/Berlin"
    assert plan["previous_timezone"] == "UTC"
    assert services.paths.timezone_file.read_text(encoding="utf-8").strip() == "Europe/Berlin"
    assert load_config(services.paths).timezone == "Europe/Berlin"


def test_a_timezone_the_appliance_does_not_carry_is_refused(tmp_path):
    services = build_test_services(tmp_path)
    handlers = handlers_for(services)

    with pytest.raises(Exception) as error:
        handlers.dispatch(
            {"operation": "system.timezone.plan", "timezone": "Europe/Nowhere"}
        )

    assert "timezone" in str(error.value).lower()
