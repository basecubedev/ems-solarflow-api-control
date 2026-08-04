# SPDX-License-Identifier: AGPL-3.0-or-later
"""The authority contract of every mutating Guided Setup route, enforced.

One table, one meaning: for each Setup route that changes durable state, which
authority it demands. The table is the contract — a new mutating route that
forgets its workflow identity, or an existing one that quietly accepts a missing
one again, fails here.

Deliberately outside the table: read-only routes (``/api/setup/workflow``,
``/api/setup/config-preview?…/validate``, plan/status/job polls) and
``/api/setup/config/download``, which serializes a draft to the browser without
touching any durable state.

The four System Build routes are in the table with the *same* authority as every
other Setup mutation. They used to be exempt on the argument that they only
create the transition a workflow later links to — but creating a transition is
what makes a workflow the owner of an irreversible operation, so each of them
names the exact active workflow, holds a lifecycle claim while its transition can
still commit, and consumes a setup intent that was issued for that workflow.

See ``docs/technical/admin-workflow-state.md``.
"""

from collections import namedtuple

import pytest

from tests.test_admin_server import (
    _attach_system_alignment,
    _control_export_manager,
    _own_active_setup_transition,
    _request,
    _serve,
)
from tests.helpers.setup_config import current_device_plan_id
from tests.test_admin_setup_preview_authority import _draft_a, _start_workflow
from tests.test_admin_setup_transition_authority import _ActiveTransitionAlignment

pytestmark = [
    pytest.mark.admin,
    pytest.mark.setup,
    pytest.mark.integration,
    pytest.mark.simulation,
]


@pytest.fixture(autouse=True)
def _isolate(isolated_install_root):
    return isolated_install_root


def _matrix_server(tmp_path):
    """A server whose alignment service can actually serve every matrix route.

    Resource-verified *and* offering the System Build entry points, so a route
    that forgets its authority fails on that — never on a service the harness
    could not have satisfied anyway.
    """

    srv, base = _serve(release_manager=_control_export_manager(tmp_path))
    _attach_system_alignment(srv, _ActiveTransitionAlignment())
    return srv, base


Contract = namedtuple(
    "Contract", "workflow_id setup_intent preview_id operation"
)

SETUP_ROUTE_CONTRACTS = {
    "/api/setup/config/write": Contract(True, False, True, "config_write"),
    "/api/setup/config/apply": Contract(True, False, True, "config_apply"),
    "/api/setup/abandon": Contract(True, False, False, "abandon"),
    "/api/setup/system-build/supersede": Contract(True, False, False, "supersede"),
    "/api/setup/deployment/prepare": Contract(
        True, False, False, "deployment_prepare"
    ),
    "/api/setup/deployment/start": Contract(True, False, False, "deployment_start"),
    "/api/setup/deployment/repair-permissions": Contract(
        True, False, False, "permission_repair"
    ),
    "/api/setup/deployment/resolve-container-conflict": Contract(
        True, False, False, "container_conflict_resolution"
    ),
    "/api/setup/system-build/update-admin": Contract(
        True, True, False, "system_build_update_admin"
    ),
    "/api/setup/system-build/confirm": Contract(
        True, True, False, "system_build_confirm"
    ),
    # Both release-prepare routes share one handler and differ only in the
    # transition mode they open, so they share one claim name.
    "/api/setup/releases/prepare": Contract(
        True, True, False, "setup_release_prepare"
    ),
    "/api/setup/automated/releases/prepare": Contract(
        True, True, False, "setup_release_prepare"
    ),
}

# The bodies the routes need beyond their authority fields.
_ROUTE_BODIES = {
    "/api/setup/config/write": _draft_a,
    "/api/setup/config/apply": _draft_a,
    "/api/setup/system-build/supersede": lambda: {"tag": "v0.9.0"},
    "/api/setup/deployment/resolve-container-conflict": lambda: {
        "container_name": "ems-solarflow-api-control",
        "action": "remove_stopped_and_continue",
    },
    "/api/setup/system-build/update-admin": lambda: {"tag": "v0.8.0"},
    "/api/setup/system-build/confirm": lambda: {"tag": "v0.8.0"},
    "/api/setup/releases/prepare": lambda: {"tag": "v0.8.0"},
    "/api/setup/automated/releases/prepare": lambda: {"tag": "v0.8.0"},
}


def _routes(field):
    return sorted(
        route
        for route, contract in SETUP_ROUTE_CONTRACTS.items()
        if getattr(contract, field)
    )


WORKFLOW_ROUTES = _routes("workflow_id")


def _body(route):
    builder = _ROUTE_BODIES.get(route)
    return dict(builder()) if builder else {}


def test_every_lifecycle_operation_in_the_matrix_is_a_known_claim():
    from admin.setup_lifecycle import MUTATION_OPERATIONS, TERMINAL_OPERATIONS

    declared = {
        contract.operation
        for contract in SETUP_ROUTE_CONTRACTS.values()
        if contract.operation
    }
    assert declared <= (MUTATION_OPERATIONS | TERMINAL_OPERATIONS)
    # Every mutation claim the coordinator knows is reachable from a route.
    assert MUTATION_OPERATIONS <= declared


def test_every_mutating_setup_route_names_its_workflow():
    assert WORKFLOW_ROUTES == sorted(SETUP_ROUTE_CONTRACTS), (
        "no mutating Setup route may act without naming its exact workflow"
    )


@pytest.mark.parametrize("route", WORKFLOW_ROUTES)
def test_a_missing_workflow_id_is_refused(tmp_path, route):
    """No route may fall back to "whichever workflow is stored"."""

    srv, base = _matrix_server(tmp_path)
    try:
        _start_workflow(base)
        status, _, payload = _request(f"{base}{route}", method="POST", body=_body(route))

        assert status == 409, (route, payload)
        assert payload["error"] == "setup_workflow_required", (route, payload)
    finally:
        srv.shutdown()
        srv.server_close()


@pytest.mark.parametrize("route", WORKFLOW_ROUTES)
def test_a_foreign_workflow_id_is_refused(tmp_path, route):
    srv, base = _matrix_server(tmp_path)
    try:
        current = _start_workflow(base)
        status, _, payload = _request(
            f"{base}{route}",
            method="POST",
            body={**_body(route), "setup_workflow_id": "an-older-tab"},
        )

        assert status == 409, (route, payload)
        assert payload["error"] == "setup_workflow_not_active", (route, payload)
        # The refusal changed nothing about the current workflow.
        _, _, view = _request(f"{base}/api/setup/workflow")
        assert view["workflow"]["workflow_id"] == current
        assert view["workflow"]["status"] == "active"
    finally:
        srv.shutdown()
        srv.server_close()


@pytest.mark.parametrize("route", _routes("preview_id"))
def test_a_missing_device_plan_id_is_refused(tmp_path, route):
    """The first link of the mutation chain, refused on its own terms."""

    srv, base = _matrix_server(tmp_path)
    try:
        workflow_id = _start_workflow(base)
        status, _, payload = _request(
            f"{base}{route}",
            method="POST",
            body={**_body(route), "setup_workflow_id": workflow_id},
        )

        assert status == 409, (route, payload)
        assert payload["error"] == "device_plan_required", (route, payload)
    finally:
        srv.shutdown()
        srv.server_close()


@pytest.mark.parametrize("route", _routes("preview_id"))
def test_a_missing_preview_id_is_refused(tmp_path, route):
    srv, base = _matrix_server(tmp_path)
    try:
        workflow_id = _start_workflow(base)
        status, _, payload = _request(
            f"{base}{route}",
            method="POST",
            body={
                **_body(route),
                "setup_workflow_id": workflow_id,
                "device_plan_id": current_device_plan_id(base, _request),
            },
        )

        assert status == 409, (route, payload)
        assert payload["error"] == "setup_preview_required", (route, payload)
    finally:
        srv.shutdown()
        srv.server_close()


@pytest.mark.parametrize("route", _routes("setup_intent"))
def test_a_missing_setup_intent_is_refused(tmp_path, route):
    """Naming the workflow is not the user's confirmation."""

    srv, base = _matrix_server(tmp_path)
    try:
        workflow_id = _start_workflow(base)
        _own_active_setup_transition(srv, base, workflow_id)
        status, _, payload = _request(
            f"{base}{route}",
            method="POST",
            body={**_body(route), "setup_workflow_id": workflow_id},
        )

        assert status == 409, (route, payload)
        assert payload["error"] == "setup_intent_required", (route, payload)
    finally:
        srv.shutdown()
        srv.server_close()


def test_the_matrix_covers_every_mutating_setup_route():
    """A new POST route under /api/setup must declare its authority here."""

    import re
    from pathlib import Path

    source = Path("admin/server.py").read_text(encoding="utf-8")
    post_section = source.split("def do_POST", 1)[1].split("\n    def ", 1)[0]
    routes = set(re.findall(r'path == "(/api/setup/[a-z0-9/-]+)"', post_section))
    # Read-only or browser-only POSTs that change no durable Setup state.
    exempt = {
        "/api/setup/config-preview",
        "/api/setup/config-preview/validate",
        "/api/setup/config/download",
        # Read-only planning: it resolves identities and returns operations for
        # the browser's own draft. It writes nothing and claims no authority.
        "/api/setup/device-plan",
        "/api/setup/scan",
        "/api/setup/discovery/run",
    }
    undeclared = routes - set(SETUP_ROUTE_CONTRACTS) - exempt
    assert not undeclared, (
        "these mutating Setup routes are missing from SETUP_ROUTE_CONTRACTS: "
        f"{sorted(undeclared)}"
    )
