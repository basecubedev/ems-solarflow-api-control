# SPDX-License-Identifier: AGPL-3.0-or-later
"""A first-time installer should see what EMS will be allowed to do.

Guided Setup partitions catalog sections by audience and then fields by level,
so the write gates — which are level="advanced" on purpose — ended up inside an
"Advanced settings" disclosure, inside the "System basics" row, inside the
"Advanced / System settings" card. Somebody installing this for the first time
never saw that EMS was about to write to their inverters.

Maintenance already answers this from two catalog groups. Setup reads the same
group ids rather than repeating the rule, and the field levels stay where they
are: promoting them would move Guided Setup's own partition.
"""

import os

import pytest

pytestmark = [
    pytest.mark.admin,
    pytest.mark.setup,
    pytest.mark.contract,
    pytest.mark.simulation,
]

STATIC_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "admin", "static"
)


def _read(name):
    with open(os.path.join(STATIC_DIR, name), encoding="utf-8") as handle:
        return handle.read()


def _config_step(html):
    step = html.split('data-setup-step-panel="config"', 1)[1].split(
        'data-setup-step-panel="deployment"', 1
    )[0]
    assert step, "config step slice is empty"
    return step


def test_setup_has_its_own_control_and_safety_block():
    step = _config_step(_read("index.html"))
    assert 'data-setup-group="safety"' in step
    assert 'id="config-feature-list-safety"' in step


def test_the_safety_block_comes_before_the_advanced_card():
    """Advanced is where it was hidden; it must not be hidden there again."""

    step = _config_step(_read("index.html"))
    assert step.index('data-setup-group="safety"') < step.index(
        'data-setup-group="advanced"'
    )


def test_both_consoles_read_one_list_of_safety_groups():
    """Two hard-coded copies of the same group ids is the duplication to avoid."""

    js = _read("admin.js")
    assert (
        'const SAFETY_CATALOG_GROUPS = ["safety_gates", "safety_holds", "limits"]' in js
    )
    assert "MAINTENANCE_SAFETY_GROUPS" not in js
    assert js.count('["safety_gates", "safety_holds", "limits"]') == 1


def test_setup_renders_the_safety_groups_flat():
    """The level disclosure is the thing that hid them; it must not wrap them."""

    js = _read("admin.js")
    body = js.split("function renderSetupSafetyGroups", 1)[1].split("\nfunction ", 1)[0]
    assert "SAFETY_CATALOG_GROUPS" in body
    assert "renderFeatureBody" not in body
    assert "<details" not in body


def test_setup_escapes_every_value_it_writes_as_markup():
    """This renderer builds an HTML string, unlike the Maintenance one."""

    js = _read("admin.js")
    body = js.split("function renderSetupSafetyGroups", 1)[1].split("\nfunction ", 1)[0]
    assert "escapeHtml(group.title" in body
    assert "escapeHtml(group.summary" in body


def test_a_safety_field_is_not_also_rendered_inside_the_advanced_card():
    js = _read("admin.js")
    body = js.split("function visibleFeatureFields", 1)[1].split("\nfunction ", 1)[0]
    assert "setupIsSafetyField" in body


def test_the_safety_container_is_bound_like_the_other_field_lists():
    js = _read("admin.js")
    lists = js.split("featureLists: {", 1)[1].split("}", 1)[0]
    assert 'safety: document.getElementById("config-feature-list-safety")' in lists
