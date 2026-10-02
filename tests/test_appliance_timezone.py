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
    services = build_test_services(tmp_path)
    _, plan = plan_and_execute(services, "system.timezone.plan", timezone="Europe/Berlin")
    assert "restarted" not in plan["warning"]
    assert "UTC" in plan["warning"]
