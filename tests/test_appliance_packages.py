# SPDX-License-Identifier: AGPL-3.0-or-later
"""Operating-system update checking, installation and package-manager recovery.

The package manager is driven through a controlled fake backend: no apt process
runs, and the check path must never modify a package or an index.
"""

import pytest

from appliance.agent import AgentHandlers
from appliance.operations import STATE_FAILED_TERMINAL, STATE_SUCCEEDED
from appliance.packages import (
    LOCK_FREE,
    LOCK_HELD,
    LOCK_UNKNOWN,
    parse_dpkg_audit,
    parse_held_packages,
    parse_simulated_upgrade,
)
from tests.helpers.appliance import build_test_services

pytestmark = [pytest.mark.integration, pytest.mark.simulation]

NO_UPDATES = "Reading package lists...\nBuilding dependency tree...\n0 upgraded, 0 newly installed.\n"

SECURITY_ONLY = """Reading package lists...
Inst openssl [3.0.11-1] (3.0.14-1 Debian-Security:12/stable-security [arm64])
"""

WITH_KERNEL = """Inst linux-image-arm64 [6.1.0-17] (6.1.0-18 Debian-Security:12/stable-security [arm64])
Inst raspi-firmware [1.20240101] (1.20240601 Raspberry Pi Foundation:12/stable [arm64])
"""


def appliance(tmp_path, **kwargs):
    services = build_test_services(tmp_path, **kwargs)
    (tmp_path / "var" / "lib" / "dpkg").mkdir(parents=True, exist_ok=True)
    (tmp_path / "var" / "lib" / "dpkg" / "lock-frontend").write_text("", encoding="utf-8")
    (tmp_path / "var" / "lib" / "apt" / "lists").mkdir(parents=True, exist_ok=True)
    (tmp_path / "var" / "run").mkdir(parents=True, exist_ok=True)
    return services


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


def test_simulated_upgrade_lines_are_parsed():
    updates = parse_simulated_upgrade(SECURITY_ONLY)
    assert len(updates) == 1
    assert updates[0].name == "openssl"
    assert updates[0].current_version == "3.0.11-1"
    assert updates[0].new_version == "3.0.14-1"
    assert updates[0].security is True


def test_non_security_origin_is_not_marked_security():
    updates = parse_simulated_upgrade("Inst libfoo [1.0-1] (1.0-2 Debian:12/stable [arm64])\n")
    assert updates[0].security is False


def test_unparsable_lines_are_ignored():
    assert parse_simulated_upgrade("Reading package lists...\nConf libfoo (1.0-2)\n") == []


def test_package_names_must_look_like_package_names():
    assert parse_simulated_upgrade("Inst ../../etc/passwd [1] (2 Debian:12 [arm64])\n") == []


def test_held_packages_are_parsed():
    assert parse_held_packages("libfoo\tinstall\nheld-package\thold\n") == ["held-package"]


def test_dpkg_audit_output_is_parsed():
    issues = parse_dpkg_audit("The following packages are in a mess:\n libbroken\n")
    assert issues == ["libbroken"]


# --- check -----------------------------------------------------------------


def test_no_updates_is_reported_cleanly(tmp_path):
    services = appliance(tmp_path)
    services.host.apt_simulation = NO_UPDATES
    state = services.packages.check().to_dict()
    assert state["security_count"] == 0
    assert state["normal_count"] == 0
    assert state["package_manager"]["healthy"] is True


def test_security_and_normal_updates_are_reported_separately(tmp_path):
    services = appliance(tmp_path)
    state = services.packages.check().to_dict()
    assert state["security_count"] == 2
    assert state["normal_count"] == 1
    assert {item["name"] for item in state["security_updates"]} == {"openssl", "linux-image-arm64"}


def test_kernel_and_firmware_updates_are_flagged(tmp_path):
    services = appliance(tmp_path)
    services.host.apt_simulation = WITH_KERNEL
    state = services.packages.check().to_dict()
    assert state["kernel_update"] is True
    assert state["firmware_update"] is True


def test_held_packages_are_reported(tmp_path):
    services = appliance(tmp_path)
    assert services.packages.check().to_dict()["held"] == ["held-package"]


def test_reboot_requirement_is_detected(tmp_path):
    services = appliance(tmp_path)
    (tmp_path / "var" / "run" / "reboot-required").write_text("", encoding="utf-8")
    (tmp_path / "var" / "run" / "reboot-required.pkgs").write_text(
        "linux-image-arm64\n", encoding="utf-8"
    )
    state = services.packages.check().to_dict()
    assert state["reboot_required"] is True
    assert state["reboot_packages"] == ["linux-image-arm64"]


def test_interrupted_dpkg_is_reported_as_unhealthy(tmp_path):
    services = appliance(tmp_path)
    services.host.dpkg_audit = "The following packages are in a mess:\n libbroken\n"
    state = services.packages.check().to_dict()
    assert state["package_manager"]["healthy"] is False
    assert state["package_manager"]["dpkg_issues"] == ["libbroken"]


def test_check_never_runs_a_modifying_apt_command(tmp_path):
    services = appliance(tmp_path)
    services.host.calls.clear()
    services.packages.check()
    for tool, args, _ in services.host.calls:
        if tool == "apt-get":
            assert "-s" in args, args
            assert "install" not in args and "upgrade" not in args[:1]


def test_lock_probe_reports_free_and_never_deletes_the_lock(tmp_path):
    services = appliance(tmp_path)
    lock = tmp_path / "var" / "lib" / "dpkg" / "lock-frontend"
    assert services.packages.lock_state() == LOCK_FREE
    assert lock.is_file()


def test_missing_lock_file_is_unknown_not_free(tmp_path):
    services = build_test_services(tmp_path)
    assert services.packages.lock_state() == LOCK_UNKNOWN


def test_a_held_lock_is_detected(tmp_path):
    import fcntl
    import os

    services = appliance(tmp_path)
    lock = tmp_path / "var" / "lib" / "dpkg" / "lock-frontend"
    handle = os.open(str(lock), os.O_RDWR)
    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        assert services.packages.lock_state() == LOCK_HELD
    finally:
        fcntl.flock(handle, fcntl.LOCK_UN)
        os.close(handle)
    assert lock.is_file()


# --- planning --------------------------------------------------------------


def test_security_plan_lists_only_security_packages(tmp_path):
    services = appliance(tmp_path)
    handlers = handlers_for(services)
    plan = handlers.dispatch({"operation": "updates.plan", "scope": "security"})["plan"]
    assert {item["name"] for item in plan["packages"]} == {"openssl", "linux-image-arm64"}
    assert plan["scope"] == "security"
    assert plan["blockers"] == []


def test_full_plan_lists_every_pending_package(tmp_path):
    services = appliance(tmp_path)
    handlers = handlers_for(services)
    plan = handlers.dispatch({"operation": "updates.plan", "scope": "all"})["plan"]
    assert plan["package_count"] == 3


def test_plan_reports_insufficient_disk_space_as_a_blocker(tmp_path):
    services = appliance(tmp_path)
    services.config = services.config.__class__(
        **{**services.config.__dict__, "minimum_free_megabytes": 10**9}
    )
    services.packages.config = services.config
    handlers = handlers_for(services)
    plan = handlers.dispatch({"operation": "updates.plan", "scope": "security"})["plan"]
    assert [item["code"] for item in plan["blockers"]] == ["insufficient_disk_space"]


def test_plan_reports_an_interrupted_dpkg_as_a_blocker(tmp_path):
    services = appliance(tmp_path)
    services.host.dpkg_audit = "libbroken\n"
    handlers = handlers_for(services)
    plan = handlers.dispatch({"operation": "updates.plan", "scope": "security"})["plan"]
    assert "dpkg_incomplete" in [item["code"] for item in plan["blockers"]]


def test_nothing_to_do_is_refused_as_a_plan(tmp_path):
    services = appliance(tmp_path)
    services.host.apt_simulation = NO_UPDATES
    handlers = handlers_for(services)
    with pytest.raises(Exception) as excinfo:
        handlers.dispatch({"operation": "updates.plan", "scope": "security"})
    assert getattr(excinfo.value, "code", "") == "no_updates_available"


# --- execution -------------------------------------------------------------


def test_security_install_upgrades_exactly_the_planned_packages(tmp_path):
    services = appliance(tmp_path)
    operation, _ = plan_and_execute(services, "updates.plan", scope="security")

    assert operation.state == STATE_SUCCEEDED
    assert operation.result["requested_packages"] == ["openssl", "linux-image-arm64"]

    install_calls = [
        args for tool, args, _ in services.host.calls if tool == "apt-get" and "install" in args
    ]
    assert install_calls, services.host.calls
    assert "--only-upgrade" in install_calls[0]
    assert "openssl" in install_calls[0]


def test_full_install_uses_upgrade(tmp_path):
    services = appliance(tmp_path)
    operation, _ = plan_and_execute(services, "updates.plan", scope="all")
    assert operation.state == STATE_SUCCEEDED
    assert any(
        "upgrade" in args for tool, args, _ in services.host.calls if tool == "apt-get" and "-s" not in args
    )


def test_failed_package_install_is_reported_with_bounded_output(tmp_path):
    services = appliance(tmp_path)
    services.host.apt_exit_code = 100
    operation, _ = plan_and_execute(services, "updates.plan", scope="security")
    assert operation.state == STATE_FAILED_TERMINAL
    assert operation.error["code"] == "package_install_failed"
    assert operation.result["output"]["truncated"] is False


def test_install_is_blocked_when_the_lock_is_taken_before_execution(tmp_path):
    import fcntl
    import os

    services = appliance(tmp_path)
    handlers = handlers_for(services)
    planned = handlers.dispatch({"operation": "updates.plan", "scope": "security"})

    lock = tmp_path / "var" / "lib" / "dpkg" / "lock-frontend"
    handle = os.open(str(lock), os.O_RDWR)
    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        handlers.dispatch(
            {
                "operation": "operations.execute",
                "operation_id": planned["operation"]["operation_id"],
                "confirmation_token": planned["confirmation_token"],
            }
        )
    finally:
        fcntl.flock(handle, fcntl.LOCK_UN)
        os.close(handle)

    operation = services.operations.get(planned["operation"]["operation_id"])
    assert operation.state == STATE_FAILED_TERMINAL
    assert operation.error["code"] == "package_lock_held"
    assert lock.is_file()


def test_install_reports_the_changed_package_count_and_reboot_state(tmp_path):
    services = appliance(tmp_path)
    handlers = handlers_for(services)
    planned = handlers.dispatch({"operation": "updates.plan", "scope": "all"})
    (tmp_path / "var" / "run" / "reboot-required").write_text("", encoding="utf-8")
    handlers.dispatch(
        {
            "operation": "operations.execute",
            "operation_id": planned["operation"]["operation_id"],
            "confirmation_token": planned["confirmation_token"],
        }
    )
    operation = services.operations.get(planned["operation"]["operation_id"])
    assert operation.result["changed_package_count"] == 3
    assert operation.result["remaining_updates"] == 0
    assert operation.result["reboot_required"] is True


# --- recovery --------------------------------------------------------------


def test_pending_configuration_repair_runs_dpkg_configure(tmp_path):
    services = appliance(tmp_path)
    services.host.dpkg_audit = "libbroken\n"
    operation, _ = plan_and_execute(services, "updates.plan_repair", action="configure_pending")

    assert operation.state == STATE_SUCCEEDED
    assert ("dpkg", ("--configure", "-a"), None) in services.host.calls
    assert operation.result["package_manager"]["healthy"] is True


def test_dependency_repair_runs_apt_fix_broken(tmp_path):
    services = appliance(tmp_path)
    operation, _ = plan_and_execute(services, "updates.plan_repair", action="fix_broken")
    assert operation.state == STATE_SUCCEEDED
    assert any(
        tool == "apt-get" and "-f" in args for tool, args, _ in services.host.calls
    )


def test_index_refresh_runs_apt_update(tmp_path):
    services = appliance(tmp_path)
    operation, _ = plan_and_execute(services, "updates.plan_repair", action="refresh_index")
    assert operation.state == STATE_SUCCEEDED
    assert any(
        tool == "apt-get" and "update" in args for tool, args, _ in services.host.calls
    )


def test_repair_never_removes_an_active_lock(tmp_path):
    import fcntl
    import os

    services = appliance(tmp_path)
    handlers = handlers_for(services)
    planned = handlers.dispatch(
        {"operation": "updates.plan_repair", "action": "configure_pending"}
    )
    lock = tmp_path / "var" / "lib" / "dpkg" / "lock-frontend"
    handle = os.open(str(lock), os.O_RDWR)
    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        handlers.dispatch(
            {
                "operation": "operations.execute",
                "operation_id": planned["operation"]["operation_id"],
                "confirmation_token": planned["confirmation_token"],
            }
        )
    finally:
        fcntl.flock(handle, fcntl.LOCK_UN)
        os.close(handle)

    operation = services.operations.get(planned["operation"]["operation_id"])
    assert operation.error["code"] == "package_lock_held"
    assert lock.is_file()
    assert not any(tool == "dpkg" and "--configure" in args for tool, args, _ in services.host.calls)


def test_no_distribution_upgrade_is_reachable(tmp_path):
    services = appliance(tmp_path)
    handlers = handlers_for(services)
    with pytest.raises(Exception) as excinfo:
        handlers.dispatch({"operation": "updates.plan", "scope": "dist-upgrade"})
    assert getattr(excinfo.value, "code", "") == "invalid_update_scope"


def test_apt_is_refused_on_a_read_only_root(tmp_path, monkeypatch):
    """The UI hides the path on an A/B image, but the browser is not the gate:
    apt would fail partway and anything it wrote is discarded at the next slot
    switch."""

    import os

    from tests.helpers.appliance import build_test_services

    services = build_test_services(tmp_path)
    real_statvfs = os.statvfs

    def read_only(path):
        result = real_statvfs(path)
        if str(path) == "/":
            class _Stat:
                f_flag = result.f_flag | os.ST_RDONLY
                f_frsize = result.f_frsize
                f_blocks = result.f_blocks
                f_bavail = result.f_bavail
                f_bfree = result.f_bfree
            return _Stat()
        return result

    monkeypatch.setattr(os, "statvfs", read_only)
    blockers = services.packages._blockers(services.packages.check())

    assert any(b["code"] == "read_only_root" for b in blockers), blockers
