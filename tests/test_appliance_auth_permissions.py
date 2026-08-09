# SPDX-License-Identifier: AGPL-3.0-or-later
"""Authentication under the packaged file ownership.

Every other appliance test owns the directories it writes, so none of them can
see what a real installation does: the audit trail belongs to root, the web
service does not, and a web process that writes it directly fails with
``PermissionError`` on the very first password. These tests run the production
web service as the actual ``ems-appliance-web`` account against the packaged
ownership layout, inside a disposable Docker container.
"""

import json

import pytest

from tests.helpers.appliance_permissions import (
    AGENT_STATE_DIR,
    AUDIT_DIR,
    AUDIT_LOG,
    WEB_LOG,
    WEB_USER,
    PermissionHost,
    docker_available,
    packaged_layout,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.docker,
    pytest.mark.slow,
    pytest.mark.skipif(not docker_available(), reason="a Docker daemon is required"),
]

SECRETS = (
    "appliance-permission-probe",
    "appliance-permission-probe-2",
    "definitely-not-the-password",
)


@pytest.fixture(scope="module")
def host():
    container = PermissionHost()
    try:
        container.start()
    except RuntimeError as exc:  # pragma: no cover - environment dependent
        pytest.skip(str(exc))
    try:
        yield container
    finally:
        container.stop()


@pytest.fixture
def packaged(host):
    """A clean packaged layout, agent state private to root."""

    host.apply_layout(packaged_layout(agent_private=True))
    return host


@pytest.fixture
def group_readable(host):
    """The earlier layout where the appliance group could read agent state."""

    host.apply_layout(packaged_layout(agent_private=False))
    return host


@pytest.fixture
def live_agent(packaged):
    """The packaged layout plus a real privileged agent on the Unix socket."""

    try:
        packaged.start_agent()
    except RuntimeError as exc:  # pragma: no cover - environment dependent
        pytest.fail(str(exc))
    try:
        yield packaged
    finally:
        packaged.stop_agent()


# --- the packaged authentication path --------------------------------------


def test_first_password_setup_completes_under_packaged_ownership(packaged):
    """The defect this suite was written for: setup wrote auth, then died.

    ``create_first_password`` stored the password and only then appended to the
    root-owned audit log, so the response never arrived while the password was
    already set — an appliance locked out of its own recovery UI.
    """

    report = packaged.drive_web("authentication")

    assert report["scenario_error"] == "", report["scenario_error"]
    assert report["auth_file_exists"] is True
    setup = report["setup"]
    assert setup["completed"] is True, f"the setup request did not complete: {setup}"
    assert setup["status"] == 200, setup
    assert setup["body"].get("authenticated") is True, setup


def test_login_logout_and_password_change_complete_under_packaged_ownership(packaged):
    report = packaged.drive_web("authentication")

    assert report["scenario_error"] == "", report["scenario_error"]
    for step in ("logout", "login_failure", "login_success", "password_change"):
        assert report[step]["completed"] is True, f"{step} did not complete: {report[step]}"
    assert report["login_failure"]["status"] == 401, report["login_failure"]
    assert report["login_success"]["status"] == 200, report["login_success"]
    assert report["password_change"]["status"] == 200, report["password_change"]


def test_the_rate_limiter_still_answers_under_packaged_ownership(packaged):
    report = packaged.drive_web("authentication")

    attempts = report["rate_limit_attempts"]
    assert all(item["completed"] for item in attempts), attempts
    assert report["rate_limited"]["status"] == 429, report["rate_limited"]
    assert report["rate_limited"]["body"]["error"] == "login_rate_limited"


# --- who owns the audit trail ----------------------------------------------


def test_authentication_events_reach_the_authoritative_audit_log(live_agent):
    """End to end: the web account authenticates, root writes the audit entry."""

    report = live_agent.drive_web("authentication_live_agent")

    assert report["scenario_error"] == "", report["scenario_error"]
    assert report["setup"]["status"] == 200, report["setup"]
    assert report["audit_status"]["degraded"] is False, report["audit_status"]

    audit = live_agent.read(AUDIT_LOG)
    for action in ("password.change", "login.failure", "login.success", "logout"):
        assert action in audit, f"{action} missing from the audit log:\n{audit}"
    assert "first_password" in audit, audit
    assert "rate_limited" in audit, audit


def test_the_audit_log_is_written_by_root_not_by_the_web_account(live_agent):
    live_agent.drive_web("authentication_live_agent")

    entry = live_agent.stat(AUDIT_LOG)
    assert entry is not None, live_agent.agent_output()
    assert entry["owner"] == "root", entry


def test_the_web_account_cannot_write_the_audit_log_itself(packaged):
    """Whatever records the event, it must not be a write from the web account."""

    packaged.shell(f"touch {AUDIT_LOG} && chown root:root {AUDIT_LOG} && chmod 0600 {AUDIT_LOG}")
    denied = packaged.shell(f"printf 'x' >> {AUDIT_LOG}", user=WEB_USER)
    assert denied.returncode != 0, "the web account must not be able to append to the audit log"
    assert packaged.shell(f"rm -f {AUDIT_LOG}", user=WEB_USER).returncode != 0


def test_the_group_readable_layout_also_refuses_a_direct_web_write(group_readable):
    """Even the older 0750 root:ems-appliance layout never allowed a web write."""

    group_readable.shell(
        f"touch {AUDIT_LOG} && chown root:ems-appliance {AUDIT_LOG} && chmod 0640 {AUDIT_LOG}"
    )
    assert group_readable.shell(f"printf 'x' >> {AUDIT_LOG}", user=WEB_USER).returncode != 0


# --- availability when the agent is gone ------------------------------------


def test_authentication_survives_an_unreachable_agent(packaged):
    """A missing agent must never lock an operator out of the recovery UI."""

    report = packaged.drive_web("authentication_agent_down")

    assert report["scenario_error"] == "", report["scenario_error"]
    setup = report["setup"]
    assert setup["completed"] is True, f"setup must answer without the agent: {setup}"
    assert setup["status"] == 200, setup
    assert report["login_failure"]["completed"] is True
    assert report["login_failure"]["status"] == 401
    assert report["login_success"]["status"] == 200


def test_an_unreachable_agent_is_reported_as_degraded_security_audit(packaged):
    report = packaged.drive_web("authentication_agent_down")

    session = report["session_after_setup"]["body"]
    audit = session.get("security_audit") or {}
    assert audit.get("authoritative") is False, session
    assert audit.get("degraded") is True, session
    assert audit.get("last_error"), session


def test_a_reachable_agent_reports_a_healthy_security_audit(packaged):
    report = packaged.drive_web("authentication")

    audit = (report["session_after_setup"]["body"].get("security_audit") or {})
    assert audit.get("authoritative") is True, report["session_after_setup"]
    assert audit.get("degraded") is False, report["session_after_setup"]


def test_a_degraded_audit_is_recorded_in_the_web_owned_log(packaged):
    packaged.drive_web("authentication_agent_down")

    web_log = packaged.read(WEB_LOG)
    assert "audit_unavailable" in web_log, web_log
    assert "password.change" in web_log, web_log


# --- agent state confidentiality --------------------------------------------


def test_the_web_account_cannot_traverse_or_read_agent_state(live_agent):
    """Under the private layout the web process gets nothing off the disk."""

    live_agent.drive_web("authentication_live_agent")
    report = live_agent.drive_web("state_access")

    assert report["scenario_error"] == "", report["scenario_error"]
    for name in (
        "agent_state_dir",
        "operations_dir",
        "known_good_dir",
        "agent_log_dir",
        "audit_log_dir",
    ):
        entry = report[name]
        assert entry["listed"] is False, f"{name} was listable: {entry}"
        assert entry["error"] == "PermissionError", entry

    for name in ("audit_log", "agent_log", "operations_log"):
        entry = report[name]
        assert entry["read"] is False, f"{name} was readable: {entry}"
        assert entry["error"] in ("PermissionError", "FileNotFoundError"), entry

    assert report["confirmation_tokens_read"] == []


def test_a_persisted_confirmation_token_is_unreadable_for_the_web_account(live_agent):
    planned = live_agent.shell(
        "cd /src && python3 - <<'PY'\n"
        "import json, socket\n"
        "s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)\n"
        "s.connect('/run/ems-appliance-manager/agent.sock')\n"
        "s.sendall(json.dumps({'operation': 'admin.plan_repair'}).encode() + b'\\n')\n"
        "print(s.recv(1 << 20).decode())\n"
        "PY",
        timeout=180,
    )
    payload = json.loads(planned.stdout.strip())
    assert payload["ok"] is True, payload
    token = payload["result"]["confirmation_token"]

    record = (
        f"{AGENT_STATE_DIR}/operations/"
        f"{payload['result']['operation']['operation_id']}.json"
    )
    assert token in live_agent.read(record), "the token must be persisted for a reload"

    as_web = live_agent.shell(f"cat {record}", user=WEB_USER)
    assert as_web.returncode != 0
    assert token not in as_web.stdout

    report = live_agent.drive_web("state_access")
    assert token not in json.dumps(report)


def test_agent_state_stays_private_after_the_agent_recreates_it(live_agent):
    """Starting the agent must not hand the layout back to the shared group."""

    for path in (AGENT_STATE_DIR, f"{AGENT_STATE_DIR}/operations", AUDIT_DIR):
        entry = live_agent.stat(path)
        assert entry is not None, path
        assert entry["owner"] == "root", entry
        assert entry["group"] == "root", entry
        assert entry["mode"] == "700", entry


# --- secrets -----------------------------------------------------------------


@pytest.mark.parametrize("scenario", ["authentication", "authentication_agent_down"])
def test_no_password_reaches_either_log(packaged, scenario):
    packaged.drive_web(scenario)

    combined = packaged.read(AUDIT_LOG) + packaged.read(WEB_LOG)
    for secret in SECRETS:
        assert secret not in combined, f"{secret!r} leaked into a log"


def test_no_password_reaches_the_agent_written_audit_log(live_agent):
    live_agent.drive_web("authentication_live_agent")

    combined = live_agent.read(AUDIT_LOG) + live_agent.read(WEB_LOG)
    combined += live_agent.agent_output()
    for secret in SECRETS:
        assert secret not in combined, f"{secret!r} leaked into a log"
