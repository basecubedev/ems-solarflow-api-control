# SPDX-License-Identifier: AGPL-3.0-or-later
"""What the Guided Upgrade panel knows once the upgrade is over.

The panel reads the installed version once, when it is opened. Everything else
about it is refreshed on navigation -- leaving Maintenance and coming back
reloads the whole planning page -- but the operator who just pressed "Upgrade
system" does not navigate anywhere: they watch the steps run on the panel they
started from. So when the job finished, the two facts naming the installed
version still named the release that had just been replaced, and only reloading
the page in the browser corrected them.

A failed run is read again for the same reason. It may have replaced the
container and rolled back, and either way the panel must not keep asserting a
version it has not re-read.
"""

import json
import os
import shutil
import subprocess

import pytest

pytestmark = [
    pytest.mark.admin,
    pytest.mark.contract,
    pytest.mark.simulation,
]

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RUNNER = os.path.join(ROOT, "tests", "js", "upgrade_completion_runner.js")
ADMIN_JS = open(
    os.path.join(ROOT, "admin", "static", "admin.js"), encoding="utf-8"
).read()

OVERVIEW = {
    "containers": {"ems": {"tag": "v0.8.9", "image": "repo:v0.8.9"}},
    "components": {"admin": {"tag": "v0.8.9", "image": "repo:v0.8.9"}},
    "install_state": {"label": "Installed"},
}


def drive(status):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not available")
    result = subprocess.run(
        [node, RUNNER],
        input=json.dumps({"status": status, "overview": OVERVIEW}),
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_a_finished_upgrade_reads_the_version_that_is_now_installed():
    outcome = drive("succeeded")

    assert any(
        url.startswith("/api/admin/maintenance/overview") for url in outcome["fetched"]
    ), "the panel never asked what is installed now"
    assert outcome["current"]["tag"] == "v0.8.9"


def test_a_failed_upgrade_reads_it_too():
    """It may have replaced the container and rolled back."""

    outcome = drive("failed")

    assert outcome["current"]["tag"] == "v0.8.9"


def test_the_outcome_is_shown_before_the_version_is_read():
    """The overview read is a docker inspect per compose service, with no timeout.

    It is also the call most likely to be slow or to block right after the EMS
    container was recreated. Awaiting it before painting the result left the panel
    saying "Running", with every action disabled, for as long as it took -- and
    for good if it never answered.
    """

    outcome = drive("succeeded")

    assert outcome["rendered"][0] == "result", (
        "the outcome waits on something: " + repr(outcome["rendered"])
    )


def test_the_result_the_operator_is_reading_is_not_overwritten():
    """A failed run leaves `completed` false, and rendering the plan for a run
    that is not complete replaces the validation list -- which is where the
    failure the operator needs to read has just been written. The version render
    writes different elements, so it is free to follow.
    """

    outcome = drive("failed")

    assert "plan" not in outcome["rendered"], (
        "the plan render replaced the failure: " + repr(outcome["rendered"])
    )
    assert "validation" not in outcome["rendered"]


def test_the_admin_alignment_fact_is_re_rendered_with_the_version():
    """The same read refreshes `runningAdmin`, and that card is its only reader.

    Refreshing the value and not the card that shows it leaves a third place on
    the panel naming a build that was just re-read correctly.
    """

    outcome = drive("succeeded")

    assert "admin-alignment" in outcome["rendered"]


def test_the_installed_version_has_one_writer():
    """Both facts on the panel are the same value from the same field.

    They were written in two places, which is how one of them stayed correct
    while the other did not. `renderUpgradeCurrent` owns it; the plan render
    reads it through that function rather than reaching for the state again.
    """

    plan = ADMIN_JS.split("function renderUpgradePlan(", 1)[1].split("\nfunction ", 1)[0]

    assert "factCurrent" not in plan, "the plan render writes the version a second time"
    assert "renderUpgradeCurrent()" in plan
