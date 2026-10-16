# SPDX-License-Identifier: AGPL-3.0-or-later
"""Which Admin and which EMS the Guided Upgrade panel says are running.

Field report 2026-10-03 (Pi 3B+): the Appliance Manager named Admin v0.8.10,
read from the running container's labels, while the panel's only "Current" was
the EMS container's v0.8.8, under a heading that says Admin and EMS are aligned
together. A Guided Upgrade to v0.8.10 had been cancelled after its Admin was
replaced, and the System Build known-good record still named the v0.8.8 pair.
"""

import json
import os

import pytest

from tests.test_admin_frontend import _extract_fn, _run_node

pytestmark = [
    pytest.mark.admin,
    pytest.mark.maintenance,
    pytest.mark.contract,
    pytest.mark.simulation,
]

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ADMIN_JS = open(os.path.join(ROOT, "admin", "static", "admin.js"), encoding="utf-8").read()
INDEX_HTML = open(
    os.path.join(ROOT, "admin", "static", "index.html"), encoding="utf-8"
).read()

CANCELLED_AFTER_ADMIN = {
    "ok": True,
    "active": False,
    "transition": {
        "mode": "guided_upgrade",
        "stage": "cancelled",
        "system_tag": "v0.8.10",
        "admin_alignment_required": True,
    },
    "known_good": {"system_tag": "v0.8.8"},
    "known_good_admin_running": False,
}


def _panel(status):
    script = (
        "\n".join(
            _extract_fn(ADMIN_JS, name)
            for name in (
                "upgradeAlignmentState",
                "upgradeAdminApartText",
                "renderUpgradeAdminAlignment",
                "applyUpgradeAlignmentTransition",
                "renderUpgradeCurrent",
            )
        )
        + """
const UPGRADE_ALIGNMENT_STATUS_TEXT = {};
function updateUpgradeActionButtons() {}
const el = () => ({ textContent: "", hidden: true });
const upgradeEls = { currentVersion: el(), currentDetail: el(), factCurrent: el() };
const upgradeAdminEls = { current: el(), target: el(), status: el(), apart: el() };
const upgradeState = {
  current: { tag: "v0.8.8", image: "repo@sha256:e", state: "Standard installation" },
  runningAdmin: { tag: "v0.8.10", image: "repo:v0.8.10" },
  validation: null,
  selected: null,
  alignmentTransition: null,
};
let systemAlignmentState = __STATUS_JSON__;
renderUpgradeCurrent();
applyUpgradeAlignmentTransition();
console.log(JSON.stringify({
  ems: upgradeEls.currentVersion.textContent,
  fact: upgradeEls.factCurrent.textContent,
  admin: upgradeAdminEls.current.textContent,
  apart: upgradeAdminEls.apart.textContent,
  apartHidden: upgradeAdminEls.apart.hidden,
}));
""".replace("__STATUS_JSON__", json.dumps(status))
    )
    return _run_node(script)


def test_the_panel_names_the_running_ems_and_the_running_admin_apart():
    out = _panel(CANCELLED_AFTER_ADMIN)

    assert out["ems"] == "v0.8.8"
    assert out["fact"] == "EMS v0.8.8 · Admin v0.8.10"
    assert out["admin"] == "v0.8.10"


def test_the_first_current_fact_says_it_is_the_ems():
    stage = INDEX_HTML.split('aria-label="Target System Build"', 1)[1].split("</section>", 1)[0]

    assert '<span class="maintenance-fact-label">Current EMS</span>' in stage
    assert '<span class="maintenance-fact-label">Current</span>' not in stage


def test_a_cancelled_upgrade_that_left_admin_and_ems_apart_is_said_and_the_record_named():
    out = _panel(CANCELLED_AFTER_ADMIN)

    assert out["apartHidden"] is False
    assert "The last verified System Build on record is v0.8.8" in out["apart"]
    assert "the Admin running here is v0.8.10" in out["apart"]
    assert "A Guided Upgrade to v0.8.10 that needed to replace the Admin was cancelled" in (
        out["apart"]
    )


def test_a_record_the_server_cannot_judge_or_finds_current_says_nothing():
    for running in (True, None):
        out = _panel({**CANCELLED_AFTER_ADMIN, "known_good_admin_running": running})

        assert out["apartHidden"] is True
        assert out["apart"] == ""


def test_a_stale_record_without_a_cancelled_upgrade_is_still_named():
    """An Admin the Appliance Manager reinstalled leaves the record behind too."""

    out = _panel({**CANCELLED_AFTER_ADMIN, "transition": None})

    assert out["apartHidden"] is False
    assert "The last verified System Build on record is v0.8.8" in out["apart"]
    assert "Guided Upgrade" not in out["apart"]


def test_the_note_has_a_place_on_the_panel():
    assert 'id="upgrade-admin-apart"' in INDEX_HTML
