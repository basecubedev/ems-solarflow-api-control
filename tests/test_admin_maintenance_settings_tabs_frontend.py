# SPDX-FileCopyrightText: 2026 basecubedev
# SPDX-License-Identifier: AGPL-3.0-or-later
"""A switch the config file leaves out is shown at what EMS will do.

The write gates default to on and are absent from most config files. The
settings editor shows an absent switch at its catalog default rather than
unchecked (``frontend/admin-modern/tests/settings.test.js``), which only works
while every gate declares that default in the catalog.
"""

import pytest

pytestmark = [
    pytest.mark.admin,
    pytest.mark.maintenance,
    pytest.mark.contract,
    pytest.mark.simulation,
]


def test_the_gates_all_declare_a_default_for_that_to_work():
    from ems.config_catalog import get_config_feature_field_index

    fields = get_config_feature_field_index()
    for path in (
        "system.allow_hardware_writes",
        "system.allow_mqtt_local_control_writes",
        "system.allow_mqtt_zendure_control_writes",
        "system.allow_state_reconciliation_writes",
        "system.enabled",
        "system.dry_run",
        "system.simulation_mode",
    ):
        assert fields[path]["type"] == "boolean"
        assert "default" in fields[path], path
