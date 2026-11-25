# SPDX-FileCopyrightText: 2026 basecubedev
# SPDX-License-Identifier: AGPL-3.0-or-later
"""A runtime state started by emsctl and one started by the EMS begin alike.

The owner's defaults (2026-11-25): winter mode on, a standby floor of 35 W. A
config that leaves them out gets them from both starters; emsctl seeded winter
off and a floor of 0 before.
"""

import pytest

import emsctl
from ems import config as cfg

pytestmark = [
    pytest.mark.unit,
    pytest.mark.config,
    pytest.mark.power_control,
]


def test_emsctl_seeds_the_owner_s_defaults_where_the_config_leaves_them_out():
    seeded = emsctl.runtime_defaults({"system": {}, "devices": []})

    assert seeded["winter"]["enabled"] is True
    assert seeded["system"]["min_output_limit"] == 35


def test_the_ems_defaults_are_the_same():
    defaults = cfg.default_runtime_config()

    assert defaults["system"]["min_output_limit"] == 35
    assert cfg.WINTER_DEFAULTS["enabled"] is True


@pytest.mark.parametrize(("stored", "read"), [(False, False), ("off", False), (True, True), ("yes", True)])
def test_emsctl_reads_a_stored_winter_switch_as_the_ems_does(stored, read):
    assert emsctl.runtime_defaults({"system": {}, "devices": [], "winter": {"enabled": stored}})["winter"]["enabled"] is read
