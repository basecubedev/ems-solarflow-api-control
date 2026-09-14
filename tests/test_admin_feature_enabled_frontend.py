# SPDX-License-Identifier: AGPL-3.0-or-later
"""A feature missing from a config is unset, not off.

Maintenance read the enabled state as ``Boolean(features[path])``, with no
fallback to the catalog default, while Guided Setup fell back to it. An
installation configured before a feature existed has no block for it, so
Maintenance called it "Disabled" — and for ``ac_charge_control`` the EMS
normalises that same missing block to ``enabled: True`` and charges. The
console understated what the software does to power hardware.

Both views now resolve the state through one function, and these cases pin
what it answers.
"""

import json
import os
import shutil
import subprocess

import pytest

from ems.config_catalog import get_config_catalog

pytestmark = [
    pytest.mark.admin,
    pytest.mark.contract,
    pytest.mark.simulation,
]

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RUNNER = os.path.join(ROOT, "tests", "js", "feature_enabled_runner.js")


def _enabled(section, values):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not available")
    result = subprocess.run(
        [node, RUNNER],
        input=json.dumps({"section": section, "values": values}),
        text=True,
        capture_output=True,
        check=True,
    )
    return json.loads(result.stdout)["enabled"]


def _sections():
    catalog = get_config_catalog()
    return catalog["sections"] if isinstance(catalog, dict) else catalog


def _section(section_id):
    for section in _sections():
        if section.get("id") == section_id:
            return section
    raise AssertionError(f"no such catalog section: {section_id}")


def test_a_feature_absent_from_the_values_reads_as_its_catalog_default():
    for section in _sections():
        if not section.get("enabled_path"):
            continue
        field = next(
            item
            for item in section["fields"]
            if item["path"] == section["enabled_path"]
        )
        assert _enabled(section, {}) is bool(field["default"]), section["id"]


def test_ac_charging_missing_from_an_older_config_does_not_read_as_off():
    section = _section("ac_charge_control")
    assert _enabled(section, {}) is True


def test_an_explicit_value_still_wins_over_the_default():
    section = _section("ac_charge_control")
    assert _enabled(section, {"ac_charge_control.enabled": False}) is False
    assert _enabled(section, {"ac_charge_control.enabled": True}) is True


def test_a_section_without_an_enabled_path_has_no_state_to_report():
    section = {"id": "devices", "fields": []}
    assert _enabled(section, {}) is None
