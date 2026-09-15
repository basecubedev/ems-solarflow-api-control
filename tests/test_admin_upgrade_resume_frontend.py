# SPDX-License-Identifier: AGPL-3.0-or-later
"""A Guided Upgrade resumes after a slow Admin replacement.

Seen on a live Raspberry Pi: the Admin container was replaced, the durable
transition sat at ``admin_reconnect_pending`` with everything in order, and
the page hung on the reconnect spinner. Two defects, both in the browser.

The reconnect poller gave up after 120 seconds -- a laptop's replacement --
and only offered a manual reload; the Pi took longer. And after that reload,
the resume never reached the server: the hash route had already started an
*unpinned* catalogue load, the resume's pinned load shared that in-flight run,
inherited the server default instead of the transition tag, and then failed
closed in silence because the selected build was not the one being resumed.

This module drives the race against the shipped ``loadUpgradePlanning``. The
reconnect wait keeps its contract next to the other reconnect tests in
``test_admin_frontend.py``.
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
RUNNER = os.path.join(ROOT, "tests", "js", "upgrade_planning_runner.js")


def _race(calls, default="latest"):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not available")
    result = subprocess.run(
        [node, RUNNER],
        input=json.dumps({"calls": calls, "default": default}),
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_a_resume_that_pins_a_build_never_inherits_the_hash_routes_load():
    """The exact race: `#maintenance-upgrade` loads unpinned first, the resume
    pins the transition tag second. The resume must end on its own tag."""

    outcome = _race([None, "dev-feat-x-aaaaaaaaaa-1234567-99-1"])

    assert outcome["selected"] == "dev-feat-x-aaaaaaaaaa-1234567-99-1"
    assert outcome["pins"] == [None, "dev-feat-x-aaaaaaaaaa-1234567-99-1"]
    assert outcome["runs"] == 2


def test_concurrent_unpinned_loads_still_share_one_run():
    outcome = _race([None, None])

    assert outcome["runs"] == 1
    assert outcome["selected"] == "latest"


def test_a_pinned_load_alone_runs_once():
    outcome = _race(["v0.8.4"])

    assert outcome == {"selected": "v0.8.4", "pins": ["v0.8.4"], "runs": 1}
