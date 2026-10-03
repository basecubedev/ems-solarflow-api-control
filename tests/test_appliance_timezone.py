# SPDX-License-Identifier: AGPL-3.0-or-later
"""The timezone the appliance reports and plans against is the one in force."""

import pytest

from appliance.operations import STATE_SUCCEEDED
from tests.helpers.appliance import build_test_services
from tests.test_appliance_network import plan_and_execute

pytestmark = [pytest.mark.integration, pytest.mark.simulation, pytest.mark.appliance]


def test_a_changed_timezone_can_be_changed_back_and_is_reported(tmp_path):
    """The agent read the zone once at start and refused to switch back."""

    services = build_test_services(tmp_path)
    changed, _ = plan_and_execute(services, "system.timezone.plan", timezone="Europe/Berlin")
    assert changed.state == STATE_SUCCEEDED
    assert services.status.system()["timezone"] == "Europe/Berlin"

    back, plan = plan_and_execute(services, "system.timezone.plan", timezone="UTC")
    assert back.state == STATE_SUCCEEDED
    assert plan["previous_timezone"] == "Europe/Berlin"
    assert services.status.system()["timezone"] == "UTC"


def test_the_plan_does_not_promise_a_container_restart(tmp_path):
    """Only an Admin Console installed after the change carries the new zone."""

    services = build_test_services(tmp_path)
    _, plan = plan_and_execute(services, "system.timezone.plan", timezone="Europe/Berlin")
    assert "restarted" not in plan["warning"]
    assert "when it is installed" in plan["warning"]
    assert "does not reach the running containers" in plan["warning"]


def test_the_result_says_the_zone_applies_to_the_next_admin_deployment(tmp_path):
    """The bootstrap is the only path that hands the zone on, and it runs only
    for a deployment that does not exist yet."""

    services = build_test_services(tmp_path)
    changed, _ = plan_and_execute(services, "system.timezone.plan", timezone="Europe/Berlin")

    assert changed.result["applies_after"] == "the next time the Admin deployment is created"
