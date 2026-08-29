# SPDX-License-Identifier: AGPL-3.0-or-later
"""Operations through the *running* Agent service, under its full sandbox.

Copying selected unit properties into a ``systemd-run`` unit proves that those
properties allow something. It does not prove that the installed Agent, with
its complete effective sandbox, its own user, its RuntimeDirectory and its real
socket, can carry out an operation end to end.

Everything here goes through the installed package: the Unix socket the Agent
service is actually listening on, and the HTTP port the packaged web service is
actually bound to. Nothing is copied, re-declared or simulated.

Marked ``docker`` because a privileged, systemd-capable container is required.
"""

import json

import pytest

from tests.helpers.appliance_systemd import (
    AGENT_UNIT,
    RUNTIME_DIR,
    SMOKE_PASSWORD,
    SOCKET_PATH,
    STATE_DIR,
    WEB_UNIT,
    SystemdContainer,
    SystemdUnavailable,
    build_package,
    docker_available,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.docker,
    pytest.mark.slow,
    pytest.mark.skipif(not docker_available(), reason="a Docker daemon is required"), pytest.mark.appliance,]

RELEASE_TAGS = ["v1.2.0", "v1.1.0", "v1.0.0", "v2.0.0-rc.1"]


@pytest.fixture(scope="module")
def package(tmp_path_factory):
    return build_package(tmp_path_factory.mktemp("appliance-deb"))


@pytest.fixture(scope="module")
def host(package):
    container = SystemdContainer()
    try:
        container.start()
    except SystemdUnavailable as exc:
        pytest.skip(str(exc))
    try:
        container.install_package(package)
        if not container.wait_for_unit(AGENT_UNIT):
            pytest.fail(f"the agent did not start:\n{container.journal(AGENT_UNIT)}")
        container.wait_for_path(SOCKET_PATH)
        yield container
    finally:
        container.stop()


# --- the agent is the one that is running -----------------------------------


def test_the_socket_belongs_to_the_running_agent_service(host):
    main_pid = host.unit_property(AGENT_UNIT, "MainPID")
    assert main_pid not in ("", "0"), host.journal(AGENT_UNIT)
    assert host.exists(SOCKET_PATH)
    # The socket the tests talk to is the one this PID opened.
    listed = host.shell(f"ls -l /proc/{main_pid}/fd 2>/dev/null | grep -c socket").stdout.strip()
    assert listed != "0", listed


def test_a_read_only_call_answers_through_the_real_socket(host):
    result = host.agent_call({"operation": "status.get"})
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout.strip())
    assert payload["ok"] is True
    assert payload["result"]["system"]["status"] == "ok"


# --- network under the full sandbox -----------------------------------------


def test_the_running_agent_fetches_the_release_index_over_http(host):
    """Not a copied RestrictAddressFamilies: the service itself does the fetch."""

    url = host.serve_release_index(RELEASE_TAGS)
    host.set_appliance_option("release_index_url", url)
    host.run(["systemctl", "restart", AGENT_UNIT], timeout=180)
    assert host.wait_for_unit(AGENT_UNIT)
    assert host.wait_for_path(SOCKET_PATH), host.journal(AGENT_UNIT)

    result = host.agent_call({"operation": "admin.releases"}, timeout=240)
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout.strip())
    assert payload["ok"] is True, payload

    releases = payload["result"]
    assert releases["error"] == "", releases
    # Every published version, releases before candidates, each newest first.
    # The candidate is listed rather than hidden; whether it may be installed is
    # the entry's own answer, taken from the host's own setting rather than from
    # what the package happens to ship as the default.
    allowed = releases["allow_prerelease"]
    listed = [(item["tag"], item["installable"]) for item in releases["available"]]
    assert listed == [
        ("v1.2.0", True),
        ("v1.1.0", True),
        ("v1.0.0", True),
        ("v2.0.0-rc.1", allowed),
    ], listed


def test_an_unreachable_release_index_is_reported_not_guessed(host):
    host.set_appliance_option("release_index_url", "http://127.0.0.1:18099/nothing.json")
    host.run(["systemctl", "restart", AGENT_UNIT], timeout=180)
    assert host.wait_for_path(SOCKET_PATH)
    try:
        payload = json.loads(host.agent_call({"operation": "admin.releases"}).stdout.strip())
        assert payload["ok"] is True
        assert payload["result"]["available"] == []
        assert payload["result"]["error"] == "release_index_unreachable"
    finally:
        host.set_appliance_option(
            "release_index_url", host.serve_release_index(RELEASE_TAGS)
        )
        host.run(["systemctl", "restart", AGENT_UNIT], timeout=180)
        host.wait_for_path(SOCKET_PATH)


# --- apt under the full sandbox ---------------------------------------------


def test_a_package_index_refresh_runs_through_the_agent_socket(host):
    """plan → confirm → execute → terminal, all in the installed Agent."""

    host.isolate_apt()
    try:
        outcome = host.run_operation(
            {"operation": "updates.plan_repair", "action": "refresh_index"}
        )
        assert outcome["ok"] is True, outcome
        operation = outcome["operation"]
        assert operation["state"] == "succeeded", operation
        assert operation["type"] == "updates.repair"
        assert operation["result"]["action"] == "refresh_index"
        stages = [entry["stage"] for entry in operation["progress"]]
        assert "repair_refresh_index" in stages, stages
        assert host.exists("/var/lib/apt/lists")
    finally:
        host.restore_apt()


def test_the_package_state_the_agent_reports_comes_from_the_guest(host):
    payload = json.loads(host.agent_call({"operation": "updates.get"}).stdout.strip())
    assert payload["ok"] is True, payload
    updates = payload["result"]
    assert "security_count" in updates
    assert updates["package_manager"]["healthy"] in (True, False)


def test_a_second_conflicting_mutation_is_refused_by_the_running_agent(host):
    planned = json.loads(host.agent_call({"operation": "admin.plan_repair"}).stdout.strip())
    assert planned["ok"] is True, planned
    operation_id = planned["result"]["operation"]["operation_id"]
    try:
        second = json.loads(
            host.agent_call({"operation": "updates.plan_repair", "action": "refresh_index"}).stdout
        )
        assert second["ok"] is False
        assert second["error"]["code"] == "operation_conflict"
        # Read-only calls stay available while a mutation is pending.
        assert json.loads(host.agent_call({"operation": "status.get"}).stdout)["ok"] is True
    finally:
        host.agent_call({"operation": "operations.cancel", "operation_id": operation_id})


# --- the packaged web service to the packaged agent -------------------------


def test_the_installed_web_service_talks_to_the_installed_agent(host):
    assert host.wait_for_unit(WEB_UNIT), host.journal(WEB_UNIT)

    report = host.drive_web_service()

    assert report["setup"]["status"] in (200, 409), report["setup"]
    assert report["login"]["status"] == 200, report["login"]
    assert report["status"]["status"] == 200, report["status"]
    assert report["status"]["body"]["system"]["status"] == "ok"
    assert report["settings"]["status"] == 200
    assert report["plan"]["status"] == 200, report["plan"]
    assert report["plan"]["body"]["plan"]["type"] == "admin.repair"
    assert report["cancel"]["status"] == 200, report["cancel"]
    assert report["logout"]["status"] == 200
    # The session really ended.
    assert report["after_logout"]["status"] == 401, report["after_logout"]


def test_the_packaged_login_writes_an_agent_owned_audit_entry(host):
    host.drive_web_service()

    audit = host.read_file("/var/log/ems-appliance-manager/audit/audit.log")
    assert "login" in audit or "password.change" in audit, audit
    assert SMOKE_PASSWORD not in audit
    entry = host.stat("/var/log/ems-appliance-manager/audit/audit.log")
    assert entry["owner"] == "root", entry
    assert entry["group"] == "root", entry


def test_the_packaged_web_service_reports_a_healthy_security_audit(host):
    report = host.drive_web_service()

    audit = report["session"]["body"]["security_audit"]
    assert audit["authoritative"] is True, audit
    assert audit["degraded"] is False, audit
    assert audit["recorded_events"] >= 1, audit


def test_the_web_account_never_wrote_agent_state_during_the_flow(host):
    host.drive_web_service()

    for path in (f"{STATE_DIR}/agent/operations", "/var/log/ems-appliance-manager/audit"):
        entry = host.stat(path)
        assert entry["owner"] == "root", entry
        assert entry["group"] == "root", entry
    owners = host.shell(
        f"find {STATE_DIR}/agent -type f -printf '%u\\n' 2>/dev/null | sort -u"
    ).stdout.split()
    assert set(owners) <= {"root"}, owners


def test_operations_survive_an_agent_restart(host):
    """The durable record, not a thread, is the authority for what is running."""

    planned = json.loads(host.agent_call({"operation": "admin.plan_repair"}).stdout.strip())
    operation_id = planned["result"]["operation"]["operation_id"]

    host.run(["systemctl", "restart", AGENT_UNIT], timeout=180)
    assert host.wait_for_path(SOCKET_PATH), host.journal(AGENT_UNIT)

    fetched = json.loads(
        host.agent_call({"operation": "operations.get", "operation_id": operation_id}).stdout
    )
    assert fetched["ok"] is True, fetched
    record = fetched["result"]["operation"]
    assert record["terminal"] is True
    assert record["state"] == "cancelled"
    assert record["error"]["code"] == "operation_interrupted"
    assert not record.get("confirmation_token")


def test_the_runtime_socket_is_recreated_with_its_ownership_after_a_restart(host):
    host.run(["systemctl", "restart", AGENT_UNIT], timeout=180)
    assert host.wait_for_path(SOCKET_PATH)
    assert host.stat(RUNTIME_DIR)["mode"] == "750"
    assert host.stat(SOCKET_PATH)["mode"] == "660"
    assert json.loads(host.agent_call({"operation": "status.get"}).stdout)["ok"] is True


# --- port inspection under the real sandbox ---------------------------------


def repair_finding(host, check, *, attempts=3):
    """One Repair finding, planned by the running Agent over its own socket."""

    for _ in range(attempts):
        result = host.agent_call({"operation": "admin.plan_repair"})
        payload = json.loads(result.stdout.strip())
        if payload.get("ok"):
            findings = {item["check"]: item for item in payload["result"]["plan"]["findings"]}
            host.agent_call(
                {
                    "operation": "operations.cancel",
                    "operation_id": payload["result"]["operation"]["operation_id"],
                }
            )
            if check in findings:
                return findings[check]
        host.run(["sleep", "2"], timeout=30)
    raise AssertionError(f"the running agent never reported a {check} finding: {payload}")


def test_the_running_agent_can_inspect_listening_ports(host):
    """``ss`` needs AF_NETLINK; the full effective unit has to permit it."""

    finding = repair_finding(host, "admin_port")

    assert finding.get("indeterminate") is not True, finding
    assert "could not be checked" not in finding["detail"], finding


def test_the_running_agent_detects_a_real_admin_port_conflict(host):
    admin_port = 8090
    host.shell(
        "nohup python3 -c \"import socket,time;"
        "s=socket.socket();s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1);"
        f"s.bind(('0.0.0.0',{admin_port}));s.listen(5);time.sleep(600)\" "
        "> /tmp/port-squatter.log 2>&1 & echo $! > /tmp/port-squatter.pid; sleep 2",
        timeout=120,
    )
    assert host.port_listening(admin_port), host.read_file("/tmp/port-squatter.log")
    try:
        conflict = repair_finding(host, "admin_port")

        assert conflict["ok"] is False, conflict
        assert str(admin_port) in conflict["detail"], conflict
        assert conflict["action"] == "", conflict
    finally:
        host.shell("kill -9 $(cat /tmp/port-squatter.pid) 2>/dev/null; true", timeout=60)

    for _ in range(15):
        if not host.port_listening(admin_port):
            break
        host.run(["sleep", "1"], timeout=30)
    assert not host.port_listening(admin_port), "the squatter did not release the port"

    released = repair_finding(host, "admin_port")
    assert released["ok"] is True, released
    assert "available" in released["detail"], released
