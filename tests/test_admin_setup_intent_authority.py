# SPDX-License-Identifier: AGPL-3.0-or-later
"""A setup intent authorizes one workflow — never its replacement.

``SetupIntent`` used to prove only "this authenticated session confirmed Fresh
Setup, and the installation has not changed since". That is not enough: issuing
an intent for another session invalidated only *that* session's earlier intents,
so two browsers in the same Fresh Setup each held a usable confirmation. When one
of them superseded the workflow, the other's intent still authorized a System
Build mutation — against the *replacement* workflow it was never issued for.

These tests pin the workflow binding end to end: the server owns it, terminal
lifecycle events invalidate every remaining intent for that workflow across all
sessions, and a foreign or replacement workflow is refused before any transition
work happens.

See ``docs/technical/admin-workflow-state.md``.
"""

import pytest

from tests.admin_auth_helpers import authenticate, raw_request
from tests.test_admin_server import (
    _attach_system_alignment,
    _control_export_manager,
    _request,
    _serve,
)
from tests.test_admin_setup_transition_authority import (
    CONFIRM,
    UPDATE_ADMIN,
    _CommitTrackingAlignment,
    _workflow_view,
)

pytestmark = pytest.mark.simulation

# The three ways a stale confirmation is refused, all 409 and all authorizing
# nothing: its workflow is gone (``setup_workflow_not_active``), the intent names
# a retired workflow (``setup_intent_workflow_mismatch``), or the terminal event
# already removed it and the same session re-confirmed since, which drops the
# tombstone too (``setup_intent_required``). Which one a caller sees depends on
# whose session terminalized the workflow, never on what it may change.
STALE_AUTHORITY_ERRORS = {
    "setup_workflow_not_active",
    "setup_intent_workflow_mismatch",
    "setup_intent_required",
}


@pytest.fixture(autouse=True)
def _isolate(isolated_install_root):
    return isolated_install_root


def _login(base):
    """An independent authenticated Admin session (its own cookie and CSRF)."""

    cookie, csrf = authenticate(base)
    return {"Cookie": cookie, "X-CSRF-Token": csrf}


def _post(base, path, body, session, *, intent_id=None):
    headers = dict(session)
    if intent_id:
        headers["X-Setup-Intent-ID"] = intent_id
    return raw_request(f"{base}{path}", method="POST", body=body, headers=headers)


def _enter_setup(base, session):
    """Confirm Fresh Setup in this session; returns (workflow_id, intent_id)."""

    status, _, payload = _post(
        base, "/api/admin/start-path", {"choice": "setup_new", "confirm": True}, session
    )
    assert status == 200, payload
    assert payload["ok"] is True, payload
    return payload["setup_workflow_id"], payload["setup_intent_id"]


def _serve_setup(tmp_path):
    alignment = _CommitTrackingAlignment()
    srv, base = _serve(release_manager=_control_export_manager(tmp_path))
    _attach_system_alignment(srv, alignment)
    return srv, base, alignment


# --- the binding is server-owned ---------------------------------------------


def test_setup_intent_is_bound_to_workflow(tmp_path):
    """An intent issued for W1 cannot authorize a different workflow."""

    srv, base, alignment = _serve_setup(tmp_path)
    try:
        session = _login(base)
        first, stale_intent = _enter_setup(base, session)

        status, _, superseded = _post(
            base,
            "/api/setup/system-build/supersede",
            {"setup_workflow_id": first, "tag": "v0.9.0"},
            session,
        )
        assert status == 200, superseded
        replacement = superseded["setup_workflow_id"]
        assert replacement != first

        status, _, refused = _post(
            base,
            CONFIRM,
            {"tag": "v0.8.0", "setup_workflow_id": replacement},
            session,
            intent_id=stale_intent,
        )

        assert status == 409, refused
        assert refused["error"] in STALE_AUTHORITY_ERRORS, refused
        assert alignment.committed == [], "no transition may be created"
        view = _workflow_view(base)
        assert view["workflow_id"] == replacement
        assert view["operation_id"] is None
    finally:
        srv.shutdown()
        srv.server_close()


def test_old_session_intent_cannot_authorize_replacement_workflow(tmp_path):
    """The two-session reproduction, exactly.

    Session A keeps the intent it was issued for W1 while session B supersedes
    W1 into W2. A's confirmation is not authority over W2.
    """

    srv, base, alignment = _serve_setup(tmp_path)
    try:
        session_a = _login(base)
        session_b = _login(base)
        first, intent_a = _enter_setup(base, session_a)
        same, _intent_b = _enter_setup(base, session_b)
        assert same == first, "both sessions must be in the same workflow"

        status, _, superseded = _post(
            base,
            "/api/setup/system-build/supersede",
            {"setup_workflow_id": first, "tag": "v0.9.0"},
            session_b,
        )
        assert status == 200, superseded
        replacement = superseded["setup_workflow_id"]

        status, _, refused = _post(
            base,
            UPDATE_ADMIN,
            {"tag": "v0.8.0", "setup_workflow_id": replacement},
            session_a,
            intent_id=intent_a,
        )

        assert status == 409, refused
        assert refused["error"] == "setup_intent_workflow_mismatch", refused
        assert alignment.committed == []
        assert alignment.launched == []
        view = _workflow_view(base)
        assert view["workflow_id"] == replacement
        assert view["status"] == "active"
        assert view["operation_id"] is None
        assert view["selected_system_tag"] == "v0.9.0", (
            "the replacement workflow's selected build must be unchanged"
        )
    finally:
        srv.shutdown()
        srv.server_close()


def test_supersede_invalidates_intents_for_old_workflow_across_sessions(tmp_path):
    srv, base, alignment = _serve_setup(tmp_path)
    try:
        session_a = _login(base)
        session_b = _login(base)
        first, intent_a = _enter_setup(base, session_a)
        _same, intent_b = _enter_setup(base, session_b)

        status, _, superseded = _post(
            base,
            "/api/setup/system-build/supersede",
            {"setup_workflow_id": first, "tag": "v0.9.0"},
            session_b,
        )
        assert status == 200, superseded

        for session, intent_id in ((session_a, intent_a), (session_b, intent_b)):
            status, _, refused = _post(
                base,
                CONFIRM,
                {"tag": "v0.8.0", "setup_workflow_id": first},
                session,
                intent_id=intent_id,
            )
            assert status == 409, refused
            assert refused["error"] == "setup_workflow_not_active", refused
        assert alignment.committed == []
    finally:
        srv.shutdown()
        srv.server_close()


def test_abandon_invalidates_all_intents_for_workflow(tmp_path):
    """Every remaining intent for an abandoned workflow is gone, in every session."""

    srv, base, alignment = _serve_setup(tmp_path)
    try:
        session_a = _login(base)
        session_b = _login(base)
        workflow_id, intent_a = _enter_setup(base, session_a)
        _same, intent_b = _enter_setup(base, session_b)

        status, _, abandoned = _post(
            base, "/api/setup/abandon", {"setup_workflow_id": workflow_id}, session_a
        )
        assert status == 200, abandoned
        assert abandoned["ok"] is True

        for session, intent_id in ((session_a, intent_a), (session_b, intent_b)):
            status, _, refused = _post(
                base,
                CONFIRM,
                {"tag": "v0.8.0", "setup_workflow_id": workflow_id},
                session,
                intent_id=intent_id,
            )
            assert status == 409, refused
            assert refused["error"] == "setup_workflow_not_active", refused

        # Re-entering Setup mints a fresh workflow; the retired intents cannot
        # authorize it either.
        replacement, _fresh_intent = _enter_setup(base, session_a)
        assert replacement != workflow_id
        for session, intent_id in ((session_a, intent_a), (session_b, intent_b)):
            status, _, refused = _post(
                base,
                CONFIRM,
                {"tag": "v0.8.0", "setup_workflow_id": replacement},
                session,
                intent_id=intent_id,
            )
            assert status == 409, refused
        assert alignment.committed == []
    finally:
        srv.shutdown()
        srv.server_close()


def test_completion_invalidates_all_intents_for_workflow(tmp_path):
    """A completed Setup keeps no usable confirmation behind."""

    class _HealthyDeployment:
        @staticmethod
        def status():
            return {"running": True, "dashboard_reachable": True}

    alignment = _CommitTrackingAlignment()
    srv, base = _serve(
        release_manager=_control_export_manager(tmp_path),
        deployment=_HealthyDeployment(),
    )
    _attach_system_alignment(srv, alignment)
    try:
        session_a = _login(base)
        session_b = _login(base)
        workflow_id, intent_a = _enter_setup(base, session_a)
        _same, intent_b = _enter_setup(base, session_b)

        status, _, confirmed = _post(
            base,
            CONFIRM,
            {"tag": "v0.8.0", "setup_workflow_id": workflow_id},
            session_a,
            intent_id=intent_a,
        )
        assert status == 200, confirmed
        alignment.stage = "healthcheck_pending"

        status, _, completed = _post(
            base,
            "/api/admin/system-alignment/resume",
            {"operation_id": "op-1"},
            session_a,
        )
        assert status == 200, completed
        assert _workflow_view(base)["status"] == "completed"

        status, _, refused = _post(
            base,
            CONFIRM,
            {"tag": "v0.8.0", "setup_workflow_id": workflow_id},
            session_b,
            intent_id=intent_b,
        )
        assert status == 409, refused
        assert refused["error"] == "setup_workflow_not_active", refused
    finally:
        srv.shutdown()
        srv.server_close()


def test_issuing_a_second_intent_for_one_workflow_grants_no_later_authority(tmp_path):
    """Two sessions in one workflow do not hand either of them a later workflow."""

    srv, base, alignment = _serve_setup(tmp_path)
    try:
        session_a = _login(base)
        session_b = _login(base)
        first, intent_a = _enter_setup(base, session_a)
        _same, intent_b = _enter_setup(base, session_b)

        status, _, abandoned = _post(
            base, "/api/setup/abandon", {"setup_workflow_id": first}, session_b
        )
        assert status == 200, abandoned
        replacement, intent_new = _enter_setup(base, session_b)

        # A's older intent is worthless for the replacement...
        status, _, refused = _post(
            base,
            CONFIRM,
            {"tag": "v0.8.0", "setup_workflow_id": replacement},
            session_a,
            intent_id=intent_a,
        )
        assert status == 409, refused
        assert alignment.committed == []

        # ...and neither is B's older one, while its fresh intent works.
        status, _, stale = _post(
            base,
            CONFIRM,
            {"tag": "v0.8.0", "setup_workflow_id": replacement},
            session_b,
            intent_id=intent_b,
        )
        assert status == 409, stale
        status, _, accepted = _post(
            base,
            CONFIRM,
            {"tag": "v0.8.0", "setup_workflow_id": replacement},
            session_b,
            intent_id=intent_new,
        )
        assert status == 200, accepted
        assert alignment.committed == ["op-1"]
    finally:
        srv.shutdown()
        srv.server_close()


def test_a_setup_intent_still_cannot_cross_sessions(tmp_path):
    """The workflow binding is added to session binding, not instead of it."""

    srv, base, alignment = _serve_setup(tmp_path)
    try:
        session_a = _login(base)
        session_b = _login(base)
        workflow_id, intent_a = _enter_setup(base, session_a)

        status, _, refused = _post(
            base,
            CONFIRM,
            {"tag": "v0.8.0", "setup_workflow_id": workflow_id},
            session_b,
            intent_id=intent_a,
        )

        assert status == 409, refused
        assert refused["error"] == "setup_intent_required", refused
        assert alignment.committed == []
    finally:
        srv.shutdown()
        srv.server_close()


def test_workflow_view_is_readable_with_the_shared_session(tmp_path):
    """The helper's shared session still sees the same record (sanity guard)."""

    srv, base, _alignment = _serve_setup(tmp_path)
    try:
        session = _login(base)
        workflow_id, _intent = _enter_setup(base, session)
        status, _, payload = _request(f"{base}/api/setup/workflow")
        assert status == 200, payload
        assert payload["workflow"]["workflow_id"] == workflow_id
    finally:
        srv.shutdown()
        srv.server_close()
