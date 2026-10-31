# SPDX-License-Identifier: AGPL-3.0-or-later
"""The MQTT mapping end to end in a browser, on a real Mosquitto and the real EMS.

Runs tests/e2e-mqtt/mqtt-mapping-end-to-end.spec.ts through
playwright.mqtt-e2e.config.ts in Chromium and Firefox: the live topics
recognise a played Shelly Gen2 plug, the browser maps it, Find devices adds
the published external device to config.json, and the EMS -- dry, every write
gate off -- reads it at 612.3 W. Needs Docker, Node and the Playwright
browsers; without the browsers it is skipped and says so, unless
EMS_REQUIRE_REAL_MQTT_TESTS=1 makes a missing browser a failure, as the CI job
that exists to run it does.
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from tests.helpers.mosquitto import real_mqtt_tests_required, require_real_broker_environment

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.docker,
    pytest.mark.browser,
    pytest.mark.mqtt,
]

require_real_broker_environment()

ROOT = Path(__file__).resolve().parents[1]


def _browser_ready(project):
    if shutil.which("node") is None or not (ROOT / "node_modules" / "playwright").is_dir():
        return False
    script = f"const p=require('playwright').{project}.executablePath();process.exit(require('fs').existsSync(p)?0:1)"
    return subprocess.run(["node", "-e", script], cwd=ROOT, capture_output=True).returncode == 0


@pytest.mark.parametrize("project", ["chromium", "firefox"])
def test_a_mapped_device_reaches_the_ems(project, tmp_path):
    if not _browser_ready(project):
        missing = f"the Playwright {project} browser is not installed"
        if real_mqtt_tests_required():
            pytest.fail(missing)
        pytest.skip(missing)
    env = dict(
        os.environ,
        EMS_MQTT_E2E_PYTHON=sys.executable,
        EMS_MQTT_E2E_STATE=str(tmp_path / "state.json"),
        TMPDIR=os.environ.get("TMPDIR", str(tmp_path)),
    )
    result = subprocess.run(
        ["npx", "playwright", "test", "--config=playwright.mqtt-e2e.config.ts", f"--project={project}", "--workers=1"],
        cwd=ROOT, env=env, capture_output=True, text=True, timeout=600,
    )

    assert result.returncode == 0, result.stdout[-4000:] + result.stderr[-2000:]
