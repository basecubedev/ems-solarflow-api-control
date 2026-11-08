# SPDX-FileCopyrightText: 2026 basecubedev
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Every CI job installs its Playwright browsers through one script that survives a stalled apt mirror.

On 2026-11-06 the Docker browser e2e job failed before any test ran: the first
install attempt stalled on the runner's apt mirror and was killed, the apt-get
that sudo had started kept the dpkg lock, and both retries died on that lock.
The script ends what apt left running and lets dpkg finish before each retry.
These tests run it against stand-ins for npx, sudo, pkill and dpkg, so nothing
on the machine running them is touched.
"""

import os
import subprocess
from pathlib import Path

import pytest
import yaml

pytestmark = [pytest.mark.unit]

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "ci-install-playwright-browsers.sh"
WORKFLOWS = ROOT / ".github" / "workflows"
RELEASE = ["pkill -KILL -x apt-get", "pkill -KILL -x apt", "pkill -KILL -f ^/usr/lib/apt/methods/", "dpkg --configure -a"]


def _workflow_runs():
    for path in sorted(WORKFLOWS.glob("*.yml")):
        jobs = yaml.safe_load(path.read_text(encoding="utf-8")).get("jobs") or {}
        for name, job in jobs.items():
            for step in job.get("steps") or []:
                yield f"{path.name}:{name}", str(step.get("run", ""))


def test_every_workflow_installs_playwright_browsers_through_the_script():
    runs = list(_workflow_runs())

    assert [where for where, run in runs if "playwright install" in run] == []
    assert sum("scripts/ci-install-playwright-browsers.sh " in run for _, run in runs) >= 11


def _install(tmp_path, *browsers, failures):
    stand_ins = tmp_path / "bin"
    stand_ins.mkdir()
    calls = tmp_path / "calls.log"
    count = tmp_path / "npx-count"
    bodies = {
        "npx": f'echo "npx $*" >> "{calls}"\n'
        f'n=$(( $(cat "{count}" 2>/dev/null || echo 0) + 1 ))\n'
        f'echo "$n" > "{count}"\n'
        f'[ "$n" -gt {failures} ]\n',
        "sudo": '[ "$1" = "-n" ] && shift\nexec "$@"\n',
        "pkill": f'echo "pkill $*" >> "{calls}"\nexit 1\n',
        "dpkg": f'echo "dpkg $*" >> "{calls}"\n',
    }
    for name, body in bodies.items():
        path = stand_ins / name
        path.write_text("#!/usr/bin/env bash\n" + body, encoding="utf-8")
        path.chmod(0o755)
    apt_conf = tmp_path / "apt.conf.d"
    apt_conf.mkdir()
    env = dict(
        os.environ,
        PATH=f"{stand_ins}{os.pathsep}{os.environ.get('PATH', '')}",
        PLAYWRIGHT_INSTALL_BACKOFF="0",
        PLAYWRIGHT_INSTALL_TIMEOUT="60",
        PLAYWRIGHT_APT_CONF_DIR=str(apt_conf),
    )
    for name in bodies:
        resolved = subprocess.run(["bash", "-c", f"command -v {name}"], env=env, capture_output=True, text=True, check=True)
        assert resolved.stdout.strip() == str(stand_ins / name)
    done = subprocess.run(["bash", str(SCRIPT), *browsers], env=env, capture_output=True, text=True, timeout=60)
    logged = calls.read_text(encoding="utf-8").splitlines() if calls.exists() else []
    return done, logged, apt_conf


def test_a_failed_attempt_releases_what_apt_left_running_before_the_retry(tmp_path):
    done, logged, apt_conf = _install(tmp_path, "chromium", "firefox", failures=1)

    install = "npx playwright install --with-deps chromium firefox"
    assert done.returncode == 0, done.stderr
    assert logged == [install, *RELEASE, install]
    settings = (apt_conf / "80-ci-playwright-install").read_text(encoding="utf-8")
    assert 'DPkg::Lock::Timeout "180";' in settings
    assert 'Acquire::http::Timeout "30";' in settings


def test_the_install_fails_after_its_last_attempt_without_releasing_again(tmp_path):
    done, logged, _apt_conf = _install(tmp_path, "webkit", failures=3)

    install = "npx playwright install --with-deps webkit"
    assert done.returncode == 1
    assert logged == [install, *RELEASE, install, *RELEASE, install]
    assert "attempt 3 of 3 failed or stalled" in done.stderr


def test_the_script_refuses_to_run_without_a_browser(tmp_path):
    done, logged, _apt_conf = _install(tmp_path, failures=0)

    assert (done.returncode, logged) == (2, [])
