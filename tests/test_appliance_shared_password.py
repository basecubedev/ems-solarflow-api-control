# SPDX-License-Identifier: AGPL-3.0-or-later
"""One password for the appliance, the Admin console and the dashboard.

An operator sets one secret and can change it from either side. That is a
deliberate decision: this is a local appliance on a private network, and two
passwords produce two weak ones rather than one strong one. What it costs is
stated in docs/appliance/security-model.md -- a compromised Admin container sees
the plaintext at login and therefore reaches the host agent.

The store is the file the dashboard and Admin already share. It is mode 0600 in
the EMS deployment root, so the unprivileged web process cannot read it: every
appliance-side answer comes through the agent, like all other privileged state.
"""

import json

import pytest

from appliance import auth as appliance_auth
from dashboard import auth as dashboard_auth

pytestmark = [pytest.mark.integration, pytest.mark.simulation, pytest.mark.appliance]

PASSWORD = "one-shared-secret-1"


# --- the two sides speak the same file ---------------------------------------


def test_the_dashboard_accepts_a_record_the_appliance_wrote(tmp_path):
    path = tmp_path / "dashboard-auth.json"
    appliance_auth.AuthStore(path, iterations=1000).create(PASSWORD, PASSWORD)

    assert dashboard_auth.auth_configured(str(path)) is True
    assert dashboard_auth.verify_password_file(str(path), PASSWORD) is True
    assert dashboard_auth.verify_password_file(str(path), "wrong") is False


def test_the_appliance_accepts_a_record_the_dashboard_wrote(tmp_path):
    path = tmp_path / "dashboard-auth.json"
    dashboard_auth.create_password_file_if_missing(str(path), PASSWORD)
    store = appliance_auth.AuthStore(path, iterations=1000)

    assert store.configured() is True
    assert store.verify(PASSWORD) is True
    assert store.verify("wrong") is False


def test_a_password_changed_from_either_side_ends_every_appliance_session(tmp_path):
    """The generation is derived from the stored record, so a change made by
    emsctl invalidates appliance sessions too -- a stored marker only the
    appliance maintains would be dropped by the other writer."""

    path = tmp_path / "dashboard-auth.json"
    store = appliance_auth.AuthStore(path, iterations=1000)
    store.create(PASSWORD, PASSWORD)
    before = store.generation()

    dashboard_auth.write_password_file(str(path), "a-different-secret-2")
    after = store.generation()

    assert before
    assert after
    assert after != before


def test_two_changes_in_a_row_from_the_other_side_are_both_noticed(tmp_path):
    path = tmp_path / "dashboard-auth.json"
    store = appliance_auth.AuthStore(path, iterations=1000)
    store.create(PASSWORD, PASSWORD)

    dashboard_auth.write_password_file(str(path), "second-secret-22")
    first = store.generation()
    dashboard_auth.write_password_file(str(path), "third-secret-333")
    second = store.generation()

    assert first != second


def test_the_record_carries_nothing_the_other_side_does_not_understand(tmp_path):
    path = tmp_path / "dashboard-auth.json"
    appliance_auth.AuthStore(path, iterations=1000).create(PASSWORD, PASSWORD)

    record = json.loads(path.read_text(encoding="utf-8"))

    assert set(record) == {"algorithm", "iterations", "salt", "hash"}


# --- a missing file is a refusal, and the appliance offers to set one ----------


def test_a_missing_file_is_not_passwordless(tmp_path):
    """Admin and the dashboard refuse without a file; a root-capable host agent
    must not be the one component that opens instead."""

    path = tmp_path / "dashboard-auth.json"
    store = appliance_auth.AuthStore(path, iterations=1000)

    assert store.configured() is False
    assert store.verify(PASSWORD) is False
    assert dashboard_auth.auth_configured(str(path)) is False


# --- the web process never reads the file itself ------------------------------


def test_the_web_tier_answers_through_the_agent(tmp_path):
    """The store is 0600 in the deployment root: this process cannot read it,
    and the hash never leaves the agent -- only a verdict comes back."""

    from appliance.agent import AgentHandlers
    from appliance.agent_client import InProcessAgentClient
    from appliance.web import AgentAuth
    from tests.helpers.appliance import build_test_services

    services = build_test_services(tmp_path)
    agent = InProcessAgentClient(AgentHandlers(services, executor=lambda target: target()))
    web_auth = AgentAuth(agent)

    assert web_auth.configured() is False

    web_auth.create(PASSWORD, PASSWORD)

    assert web_auth.configured() is True
    assert web_auth.verify(PASSWORD) is True
    assert web_auth.verify("wrong-one") is False
    assert web_auth.generation()

    # And the file the agent wrote is the one the dashboard reads.
    assert dashboard_auth.verify_password_file(str(services.paths.auth_file), PASSWORD)


def test_a_change_through_the_agent_moves_the_generation(tmp_path):
    from appliance.agent import AgentHandlers
    from appliance.agent_client import InProcessAgentClient
    from appliance.web import AgentAuth
    from tests.helpers.appliance import build_test_services

    services = build_test_services(tmp_path)
    agent = InProcessAgentClient(AgentHandlers(services, executor=lambda target: target()))
    web_auth = AgentAuth(agent)
    web_auth.create(PASSWORD, PASSWORD)
    before = web_auth.generation()

    web_auth.change(PASSWORD, "a-new-shared-secret", "a-new-shared-secret")

    assert web_auth.generation() != before
    assert web_auth.verify("a-new-shared-secret") is True


def test_an_unreachable_agent_does_not_look_like_an_unconfigured_box(tmp_path):
    """Offering first-time setup because the agent is down would hand the
    appliance to whoever asks while it is degraded."""

    from appliance.agent_client import AgentUnavailableError
    from appliance.web import AgentAuth

    class _Down:
        def call(self, *_args, **_kwargs):
            raise AgentUnavailableError("the appliance agent is not reachable")

    web_auth = AgentAuth(_Down())

    assert web_auth.configured() is True

    with pytest.raises(appliance_auth.AuthError) as error:
        web_auth.verify(PASSWORD)
    assert error.value.code == "agent_unavailable"


# --- what the adversarial review found, and must not come back ----------------


def test_setting_a_password_does_not_block_the_admin_install(tmp_path):
    """The shared file is the first file ever placed in the deployment root,
    and adoption refuses a root-owned root that holds any file. Treating the
    password as evidence of an installation made setting one the thing that
    prevents installing Admin at all."""

    from appliance.admin_bootstrap import DeploymentBootstrap

    root = tmp_path / "opt" / "ems-solarflow"
    for name in ("config", "data", "backups"):
        (root / name).mkdir(parents=True)

    before = DeploymentBootstrap._unclaimed_directories(root)
    assert before is not None and len(before) == 4

    (root / "config" / "dashboard-auth.json").write_text("{}", encoding="utf-8")
    after = DeploymentBootstrap._unclaimed_directories(root)

    assert after is not None, "the password made the deployment root un-adoptable"
    assert root / "config" / "dashboard-auth.json" in after, "it is not handed over"


def test_any_other_file_still_refuses_adoption(tmp_path):
    """The exemption is one named file, not a class of them."""

    from appliance.admin_bootstrap import DeploymentBootstrap

    root = tmp_path / "opt" / "ems-solarflow"
    (root / "config").mkdir(parents=True)
    (root / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")

    assert DeploymentBootstrap._unclaimed_directories(root) is None


def test_the_cli_reset_leaves_the_file_readable_by_the_containers(tmp_path):
    """password-reset is the documented recovery path. Handing the file to the
    web account there locked Admin and the dashboard out of the secret they
    authenticate against -- and printed success."""

    import inspect

    from appliance import cli

    source = inspect.getsource(cli.command_password_reset)

    assert "_chown_web_user" not in source
    assert "deployment_owner" in source
    assert not hasattr(cli, "_chown_web_user")


def test_the_owner_comes_from_the_root_not_from_a_name(tmp_path):
    """/etc/passwd is slot-local on an A/B image, so the same account name can
    carry a different uid in the other slot while the containers keep running
    as the uid baked into the compose file."""

    import inspect
    import os

    from appliance.auth import deployment_owner

    root = tmp_path / "opt" / "ems-solarflow"
    root.mkdir(parents=True)
    entry = os.stat(root)

    assert deployment_owner(tmp_path / "absent") is None
    assert "getpwnam" not in inspect.getsource(deployment_owner)
    if entry.st_uid == 0:
        assert deployment_owner(root) is None, "a root-owned root is not yet adopted"
    else:
        assert deployment_owner(root) == (entry.st_uid, entry.st_gid)


def test_a_refused_password_is_an_answer_not_a_dropped_connection(tmp_path):
    """Every ordinary input error on the one page a fresh appliance offers used
    to produce no HTTP response at all."""

    from appliance.agent import AgentHandlers
    from appliance.agent_client import InProcessAgentClient
    from appliance.auth import AuthError
    from appliance.web import AgentAuth
    from tests.helpers.appliance import build_test_services

    services = build_test_services(tmp_path)
    agent = InProcessAgentClient(AgentHandlers(services, executor=lambda target: target()))
    web_auth = AgentAuth(agent)

    with pytest.raises(AuthError) as error:
        web_auth.create("a-secret", "a-different-secret")
    assert error.value.code == "password_mismatch"

    web_auth.create(PASSWORD, PASSWORD)
    with pytest.raises(AuthError):
        web_auth.change("not-the-current-one", "another-secret-1", "another-secret-1")


def test_an_unreachable_agent_does_not_log_everyone_out(tmp_path):
    """An empty generation differs from every live session's, so one socket
    timeout would end every signed-in operator's session."""

    from appliance.agent_client import AgentUnavailableError
    from appliance.web import AgentAuth

    class _Flaky:
        def __init__(self):
            self.up = True

        def call(self, operation, **_fields):
            if not self.up:
                raise AgentUnavailableError("gone")
            return {"configured": True, "generation": "abc123"}

    agent = _Flaky()
    web_auth = AgentAuth(agent)

    assert web_auth.generation() == "abc123"
    agent.up = False
    assert web_auth.generation() == "abc123"


def test_two_writers_do_not_lose_each_others_password(tmp_path):
    """The agent is a threading server and the CLI is a second process."""

    import threading

    path = tmp_path / "dashboard-auth.json"
    appliance_auth.AuthStore(path, iterations=1000).create(PASSWORD, PASSWORD)

    start = threading.Barrier(2)
    errors = []

    def change(secret):
        store = appliance_auth.AuthStore(path, iterations=1000)
        start.wait()
        try:
            store.reset(secret, secret)
        except Exception as exc:  # noqa: BLE001 - the test reports it
            errors.append(exc)

    threads = [
        threading.Thread(target=change, args=("first-writer-secret",)),
        threading.Thread(target=change, args=("second-writer-secret",)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert not errors, errors
    store = appliance_auth.AuthStore(path, iterations=1000)
    assert store.verify("first-writer-secret") or store.verify("second-writer-secret")
    assert not list(path.parent.glob(".*.tmp")), "a temporary file was left behind"


def test_there_is_no_minimum_password_length():
    """One password opens all three, and the other two have never imposed one.
    A minimum here would refuse to change a password set from the EMS side, and
    how strong it is, is the operator's decision about their own device."""

    from appliance import auth

    assert not hasattr(auth, "MIN_PASSWORD_LENGTH")
    assert not hasattr(auth, "SHORT_PASSWORD_LENGTH")

    for short in ("a", "1234", "hunter2"):
        assert auth.validate_password(short) == short

    with pytest.raises(auth.AuthError) as error:
        auth.validate_password("")
    assert error.value.code == "password_required"


def test_a_short_password_set_from_the_ems_side_can_be_changed_from_here(tmp_path):
    """The concrete failure a minimum would produce: a password the EMS accepts
    and the appliance then refuses to touch."""

    path = tmp_path / "dashboard-auth.json"
    dashboard_auth.write_password_file(str(path), "short")
    store = appliance_auth.AuthStore(path, iterations=1000)

    assert store.verify("short") is True
    store.change("short", "also-short", "also-short")
    assert store.verify("also-short") is True


def test_the_documentation_states_no_minimum():
    from pathlib import Path

    doc = (
        Path(__file__).resolve().parents[1] / "docs" / "appliance" / "installation.md"
    ).read_text(encoding="utf-8")

    assert "at least 12 characters" not in doc
    assert "no minimum length" in doc


def test_the_browser_form_imposes_no_length_either():
    """The rule has to be absent where an operator meets it, not only in the
    validator. A length floor on the input refuses the password before the
    request is made, so the browser would reject a secret the EMS side accepts
    -- the same lockout the validator was freed of. Admin already pins this for
    its own form in test_admin_frontend.py."""

    from pathlib import Path

    static = Path(__file__).resolve().parents[1] / "appliance" / "static"
    html = (static / "index.html").read_text(encoding="utf-8")
    app_js = (static / "app.js").read_text(encoding="utf-8")

    assert "minlength" not in html
    for text in ("at least", "characters long", "password_too_short"):
        assert text not in html
        assert text not in app_js


def _appliance_doc(name):
    from pathlib import Path

    return (Path(__file__).resolve().parents[1] / "docs" / "appliance" / name).read_text(
        encoding="utf-8"
    )


def test_no_page_still_promises_a_separate_appliance_password():
    """The security model's own summary table outlived the decision it
    summarises: it listed a 12-character minimum that no longer exists and an
    independence from the Admin password that is the opposite of the feature,
    while the prose three sections above described the sharing correctly. A
    reader checking the table would have been told the reverse of the truth.
    """

    security = _appliance_doc("security-model.md")
    installation = _appliance_doc("installation.md")

    for claim in ("12 characters", "separate from the EMS Admin password"):
        assert claim not in security, claim
    assert "independent from the EMS Admin password" not in installation
    assert "| Minimum length | none" in security
    assert "| Independence | none" in security
