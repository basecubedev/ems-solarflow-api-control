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

``/api/setup/system-build/confirm`` is in the table with ``workflow_id: False``
on purpose: it creates the transition a workflow later links to and removes,
overwrites or terminalizes nothing. Its authority is the one-shot, session-bound
``setup_intent_id`` — which an old tab does not hold — and the link it writes
targets the single stored record rather than a chosen candidate.

See ``docs/technical/admin-workflow-state.md``.
"""

import pytest

from tests.test_admin_server import (
    _control_export_manager,
    _request,
    _serve,
)
from tests.test_admin_setup_preview_authority import _draft_a, _start_workflow

pytestmark = pytest.mark.simulation


@pytest.fixture(autouse=True)
def _isolate(isolated_install_root):
    return isolated_install_root


# route -> (requires workflow_id, requires preview_id, lifecycle operation)
SETUP_ROUTE_CONTRACTS = {
    "/api/setup/config/write": (True, True, "config_write"),
    "/api/setup/config/apply": (True, True, "config_apply"),
    "/api/setup/abandon": (True, False, "abandon"),
    "/api/setup/system-build/supersede": (True, False, "supersede"),
    "/api/setup/deployment/prepare": (True, False, "deployment_prepare"),
    "/api/setup/deployment/start": (True, False, "deployment_start"),
    "/api/setup/deployment/repair-permissions": (True, False, "permission_repair"),
    "/api/setup/deployment/resolve-container-conflict": (
        True,
        False,
        "container_conflict_resolution",
    ),
    "/api/setup/system-build/confirm": (False, False, None),
    # Same reasoning as confirm: it starts the Admin-alignment transition Fresh
    # Setup then links, behind the same one-shot setup intent. It removes nothing.
    "/api/setup/system-build/update-admin": (False, False, None),
    # Release preparation fills the shared release cache — installed-system state
    # that outlives any workflow. It is idempotent and owns no Setup artifact.
    "/api/setup/releases/prepare": (False, False, None),
    "/api/setup/automated/releases/prepare": (False, False, None),
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
}

WORKFLOW_ROUTES = sorted(
    route
    for route, (needs_workflow, _preview, _operation) in SETUP_ROUTE_CONTRACTS.items()
    if needs_workflow
)


def _body(route):
    builder = _ROUTE_BODIES.get(route)
    return dict(builder()) if builder else {}


def test_every_lifecycle_operation_in_the_matrix_is_a_known_claim():
    from admin.setup_lifecycle import MUTATION_OPERATIONS, TERMINAL_OPERATIONS

    declared = {
        operation
        for _needs, _preview, operation in SETUP_ROUTE_CONTRACTS.values()
        if operation
    }
    assert declared <= (MUTATION_OPERATIONS | TERMINAL_OPERATIONS)
    # Every mutation claim the coordinator knows is reachable from a route.
    assert MUTATION_OPERATIONS <= declared


@pytest.mark.parametrize("route", WORKFLOW_ROUTES)
def test_a_missing_workflow_id_is_refused(tmp_path, route):
    """No route may fall back to "whichever workflow is stored"."""

    srv, base = _serve(release_manager=_control_export_manager(tmp_path))
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
    srv, base = _serve(release_manager=_control_export_manager(tmp_path))
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


@pytest.mark.parametrize(
    "route",
    sorted(
        route
        for route, (_needs, needs_preview, _operation) in SETUP_ROUTE_CONTRACTS.items()
        if needs_preview
    ),
)
def test_a_missing_preview_id_is_refused(tmp_path, route):
    srv, base = _serve(release_manager=_control_export_manager(tmp_path))
    try:
        workflow_id = _start_workflow(base)
        status, _, payload = _request(
            f"{base}{route}",
            method="POST",
            body={**_body(route), "setup_workflow_id": workflow_id},
        )

        assert status == 409, (route, payload)
        assert payload["error"] == "setup_preview_required", (route, payload)
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
        "/api/setup/scan",
        "/api/setup/discovery/run",
    }
    undeclared = routes - set(SETUP_ROUTE_CONTRACTS) - exempt
    assert not undeclared, (
        "these mutating Setup routes are missing from SETUP_ROUTE_CONTRACTS: "
        f"{sorted(undeclared)}"
    )
