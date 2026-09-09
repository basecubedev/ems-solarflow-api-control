# SPDX-License-Identifier: AGPL-3.0-or-later
"""The settings editor is four doors over one draft.

136 settings behind a single accordion is the depth problem this page had. The
tabs do not hide anything: every catalog section still renders, on exactly one
tab, and switching a tab only toggles ``hidden`` — it never reloads and never
replaces the draft, so an unsaved edit made under one tab survives a visit to
another.
"""

import json
import os
import shutil
import subprocess

import pytest

from ems.config_catalog import get_config_feature_sections

pytestmark = [
    pytest.mark.admin,
    pytest.mark.maintenance,
    pytest.mark.contract,
    pytest.mark.simulation,
]

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RUNNER = os.path.join(ROOT, "tests", "js", "maintenance_settings_tabs_runner.js")
STATIC_DIR = os.path.join(ROOT, "admin", "static")

TABS = ("devices", "features", "safety", "expert")


def _read(name):
    with open(os.path.join(STATIC_DIR, name), encoding="utf-8") as handle:
        return handle.read()


def _settings_panel():
    html = _read("index.html")
    panel = html.split('id="maintenance-settings-panel"', 1)[1].split(
        'id="maintenance-upgrade-panel"', 1
    )[0]
    assert panel, "settings panel slice is empty"
    return panel


def _route(sections):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not available")
    result = subprocess.run(
        [node, RUNNER],
        input=json.dumps({"sections": sections}),
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


# --- the four doors -------------------------------------------------------


def test_each_tab_has_a_button_and_a_pane():
    panel = _settings_panel()
    for tab in TABS:
        assert 'data-settings-tab="' + tab + '"' in panel
        assert 'data-settings-pane="' + tab + '"' in panel
    assert panel.count('role="tab"') == len(TABS)
    assert panel.count('role="tabpanel"') == len(TABS)


def test_the_devices_tab_opens_first_and_the_rest_start_hidden():
    panel = _settings_panel()
    devices = panel.split('data-settings-tab="devices"', 1)[1].split(">", 1)[0]
    assert 'aria-selected="true"' in devices
    assert panel.count('aria-selected="true"') == 1
    for tab in ("features", "safety", "expert"):
        pane = panel.split('data-settings-pane="' + tab + '"', 1)[1].split(">", 1)[0]
        assert "hidden" in pane


def test_switching_a_tab_only_toggles_hidden():
    """The draft lives in the browser; a tab must never be a reason to lose it."""

    js = _read("admin.js")
    body = js.split("function setMaintenanceSettingsTab", 1)[1].split("\n}", 1)[0]
    assert "hidden" in body
    for forbidden in (
        "loadMaintenanceConfig",
        "renderMaintenanceConfig",
        "mconfigState.draft =",
        "mconfigState.pristine =",
        "fetch(",
    ):
        assert forbidden not in body, f"tab switching must not reach {forbidden}"


def test_one_footer_serves_every_tab():
    panel = _settings_panel()
    assert panel.count('id="maintenance-settings-footer"') == 1
    assert panel.count('id="maintenance-config-preview-btn"') == 1
    # The footer sits after the last tab pane, so it belongs to none of them.
    assert panel.index('data-settings-pane="expert"') < panel.index(
        'id="maintenance-settings-footer"'
    )


# --- every setting has exactly one home -----------------------------------


def test_every_catalog_section_lands_on_one_tab():
    sections = get_config_feature_sections("maintenance")
    routed = _route(sections)["tabs"]
    assert set(routed) == {section["id"] for section in sections}
    assert set(routed.values()) <= {"features", "expert"}


def test_the_system_section_is_expert_and_the_features_are_features():
    routed = _route(get_config_feature_sections("maintenance"))["tabs"]
    assert routed["system"] == "expert"
    assert routed["config_upgrade"] == "expert"
    assert routed["winter"] == "features"
    assert routed["influxdb"] == "features"


def test_the_write_gates_and_the_envelope_go_to_the_safety_tab():
    routed = _route(get_config_feature_sections("maintenance"))
    assert set(routed["safety"]) == {
        "system.enabled",
        "system.dry_run",
        "system.simulation_mode",
        "system.allow_hardware_writes",
        "system.allow_mqtt_local_control_writes",
        "system.allow_mqtt_zendure_control_writes",
        "system.allow_state_reconciliation_writes",
        "system.max_total_power",
        "system.max_device_power",
        "system.min_output_limit",
    }


def test_a_safety_field_is_never_rendered_twice():
    routed = _route(get_config_feature_sections("maintenance"))
    assert not set(routed["safety"]) & set(routed["elsewhere"])
    js = _read("admin.js")
    body = js.split("function mconfigFeatureBody", 1)[1].split("\n}", 1)[0]
    assert "!mconfigIsSafetyField(field)" in body


def test_the_safety_tab_does_not_hide_a_gate_behind_a_disclosure():
    """Four of the five gates are level="advanced"; the veil is the bug."""

    js = _read("admin.js")
    body = js.split("function renderMaintenanceSafetyGroups", 1)[1].split("\n}", 1)[0]
    assert "mconfigLevelledFields" not in body
    assert "mconfig-fields feature-fields" in body


def test_the_safety_tab_takes_its_grouping_from_the_catalog():
    js = _read("admin.js")
    assert 'const MAINTENANCE_SAFETY_GROUPS = ["safety_gates", "limits"]' in js
    body = js.split("function renderMaintenanceSafetyGroups", 1)[1].split("\n}", 1)[0]
    # Titles and order come from the catalog entry, never from a copy here.
    assert "group.title" in body
    assert "group.summary" in body
    assert "group.order" in body


# --- search ---------------------------------------------------------------


def test_the_search_filters_rendered_rows_instead_of_reloading():
    js = _read("admin.js")
    body = js.split("function applyMaintenanceSettingsSearch", 1)[1].split("\n}", 1)[0]
    for forbidden in ("fetch(", "loadMaintenanceConfig", "renderMaintenanceFeatures"):
        assert forbidden not in body
    assert "dataset.searchHit" in body


def test_the_search_reaches_the_setting_path_not_only_its_label():
    js = _read("admin.js")
    terms = js.split("function maintenanceSettingsSearchTerms", 1)[1].split("\n}", 1)[0]
    assert "dataset.path" in terms
    assert "feature-field-desc" in terms


# --- a switch shows what EMS will do, not what the file happens to store ---


def test_an_absent_switch_is_shown_at_its_catalog_default():
    """Four write gates default to on and are absent from most config files.

    Rendering an absent boolean unchecked told the owner their inverters were
    safe while EMS was free to drive them. RELEASE_WRITE_GATE_DEFAULTS resolves
    a missing gate to true, so that is what the box has to show.
    """

    js = _read("admin.js")
    body = js.split("function mconfigCatalogControl", 1)[1].split("\n}", 1)[0]
    assert "field.default !== undefined ? field.default : value" in body
    assert 'control.dataset.fromDefault = "true"' in body


def test_showing_a_default_does_not_store_it():
    """The row displays the default; the draft keeps saying nothing about it."""

    js = _read("admin.js")
    body = js.split("function mconfigCatalogControl", 1)[1].split("\n}", 1)[0]
    for forbidden in ("onChange(", "features[", "mconfigState"):
        assert forbidden not in body


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
