# SPDX-FileCopyrightText: 2026 basecubedev
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The allocation switches load as the other switches do.

``system.pv_kwp_weighting``, ``battery_kwh_weighting`` and
``redistribute_clamped_power`` were taken from the file as written and tested
for truth, so a hand-written ``"false"`` or ``"off"`` ran them on.
"""

import json
from types import SimpleNamespace

import pytest

from ems import config as cfg

pytestmark = [
    pytest.mark.power_control,
    pytest.mark.unit,
]

SWITCHES = {
    "pv_kwp_weighting": "PV_KWP_WEIGHTING",
    "battery_kwh_weighting": "BATTERY_KWH_WEIGHTING",
    "redistribute_clamped_power": "REDISTRIBUTE_CLAMPED_POWER",
}


def _loaded(tmp_path, system):
    config = cfg.default_runtime_config()
    for key in SWITCHES:
        config["system"].pop(key, None)
    config["system"].update(system)
    config["config_upgrade"] = {"on_startup": "disabled"}
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config))
    args = SimpleNamespace(config=str(path), dry_run=False, simulate=False, replay=None, self_test=False)
    snapshot = {name: getattr(cfg, name) for name in dir(cfg) if name.isupper()}
    try:
        cfg.initialize(args, str(tmp_path))
        return {key: getattr(cfg, name) for key, name in SWITCHES.items()}
    finally:
        for name, value in snapshot.items():
            setattr(cfg, name, value)


@pytest.mark.parametrize(
    ("stored", "read"),
    [
        ("false", False),
        ("off", False),
        ("no", False),
        ("0", False),
        (0, False),
        (False, False),
        ("true", True),
        ("on", True),
        (1, True),
        (True, True),
        ("maybe", True),
        (None, False),
        (0.0, False),
        ("", False),
        (2, True),
    ],
)
def test_an_allocation_switch_reads_a_word_as_it_says_and_anything_else_as_before(tmp_path, stored, read):
    loaded = _loaded(tmp_path, {key: stored for key in SWITCHES})

    assert loaded == {key: read for key in SWITCHES}


def test_an_allocation_switch_the_file_leaves_out_is_on(tmp_path):
    assert _loaded(tmp_path, {}) == {key: True for key in SWITCHES}
