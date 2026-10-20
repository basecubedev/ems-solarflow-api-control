# SPDX-License-Identifier: AGPL-3.0-or-later
"""The Admin page's side of the analytics repair and of a restart with warnings.

Kept apart from tests/test_admin_frontend.py, which is at the repository's
source-size limit.
"""

import pytest

from tests.test_admin_frontend import _js_function_body, _read

pytestmark = [
    pytest.mark.admin,
    pytest.mark.contract,
    pytest.mark.simulation,
]


def test_maintenance_offers_an_analytics_repair_while_analytics_is_on():
    html = _read("index.html")
    js = _read("admin.js")

    assert (
        '<button type="button" id="maintenance-runtime-analytics-repair"\n'
        '                      class="secondary-button compact" hidden>Repair analytics</button>'
    ) in html
    assert (
        "maintenanceEls.runtimeAnalyticsRepair.hidden = !(plan.available && plan.analytics_enabled);"
    ) in js
    repair = _js_function_body(js, "async function repairAnalytics(")
    assert '"/api/admin/maintenance/analytics/repair"' in repair
    assert "JSON.stringify({ confirm: true })" in repair
    assert "window.confirm(ANALYTICS_REPAIR_CONFIRM)" in repair
    assert "innerHTML" not in repair
    assert "setAnalyticsRepairBusy(true);" in repair
    assert 'maintenanceEls.runtimeAnalyticsRepair.addEventListener("click", repairAnalytics);' in js


def test_the_repair_and_the_restart_disable_each_other_while_one_runs():
    """The server refuses the second one anyway; the page should not offer it."""

    js = _read("admin.js")
    restart = _js_function_body(js, "function setContainerSyncBusy(")
    repair = _js_function_body(js, "function setAnalyticsRepairBusy(")

    assert "maintenanceEls.runtimeAnalyticsRepair.disabled = isBusy;" in restart
    assert "[mconfigEls.containersSync, maintenanceEls.runtimeContainersSync]" in repair
    assert "other.disabled = isBusy;" in repair


def test_a_container_sync_shows_its_warnings_with_the_steps():
    """An analytics step that failed no longer stops the restart; the page has to
    say so, or a completed sync hides an Analytics tab that stays empty."""

    sync = _js_function_body(_read("admin.js"), "async function syncMaintenanceContainers(")

    assert "const warnings = Array.isArray(data.warnings) ? data.warnings : [];" in sync
    assert "statusEl.textContent = [done, ...warnings].join(\" \");" in sync
    assert '[humanErrorText(data, "Container sync failed."), ...warnings].join(" ")' in sync
