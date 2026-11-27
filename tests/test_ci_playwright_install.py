# SPDX-FileCopyrightText: 2026 basecubedev
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Every CI job installs its Playwright browsers through one script that survives a stalled apt mirror.

On 2026-11-06 the Docker browser e2e job failed before any test ran: the first
install attempt stalled on the runner's apt mirror and was killed, the apt-get
that sudo had started kept the dpkg lock, and both retries died on that lock.
The script ends what apt left running and lets dpkg finish before each retry.
On 2026-11-18 a mirror that served one package a minute outlasted all three
attempts, so the jobs now install through one action that caches the browser
downloads and the .deb archives of their system packages between runs, and the
mirror is asked only for what the cache does not hold. These tests run the
script against stand-ins for npx, sudo, pkill, dpkg and chown, so nothing on the
machine running them is touched.
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
ACTION_PATH = "./.github/actions/playwright-browsers"
ACTION = ROOT / ACTION_PATH / "action.yml"
APT_ARCHIVES = "~/.cache/playwright-apt"
RELEASE = ["pkill -KILL -x apt-get", "pkill -KILL -x apt", "pkill -KILL -f ^/usr/lib/apt/methods/", "dpkg --configure -a"]


def _workflow_steps():
    for path in sorted(WORKFLOWS.glob("*.yml")):
        jobs = yaml.safe_load(path.read_text(encoding="utf-8")).get("jobs") or {}
        for name, job in jobs.items():
            for step in job.get("steps") or []:
                yield f"{path.name}:{name}", step


def test_every_workflow_installs_playwright_browsers_through_the_cached_action():
    steps = list(_workflow_steps())
    runs = [(where, str(step.get("run", ""))) for where, step in steps]
    installs = [(where, step) for where, step in steps if step.get("uses") == ACTION_PATH]

    assert [where for where, run in runs if "playwright install" in run] == []
    assert [where for where, run in runs if "ci-install-playwright-browsers.sh" in run] == []
    assert len(installs) >= 11
    assert [where for where, step in installs if not str((step.get("with") or {}).get("browsers", "")).strip()] == []


def test_the_action_restores_the_cache_before_it_installs_and_keys_it_on_what_it_holds():
    steps = yaml.safe_load(ACTION.read_text(encoding="utf-8"))["runs"]["steps"]
    uses = [str(step.get("uses", "")) for step in steps]
    runs = [str(step.get("run", "")) for step in steps]
    cache = next(index for index, name in enumerate(uses) if name.startswith("actions/cache@"))
    install = next(index for index, run in enumerate(runs) if "scripts/ci-install-playwright-browsers.sh" in run)

    assert cache < install
    assert steps[cache]["with"]["path"].split() == ["~/.cache/ms-playwright", APT_ARCHIVES]
    assert f'PLAYWRIGHT_APT_ARCHIVES="$HOME/{APT_ARCHIVES[2:]}"' in runs[install]
    key = steps[cache]["with"]["key"]
    key_step = next(step for step in steps if step.get("id") and f"steps.{step['id']}.outputs" in key)
    for part in ("ImageOS", "ImageVersion", "playwright-core", "BROWSERS"):
        assert part in key_step["run"], part
    assert steps[cache]["with"]["restore-keys"] == f"${{{{ steps.{key_step['id']}.outputs.prefix }}}}"
    assert "tr -s ' ' '+'" in key_step["run"], "one browser set's prefix must not match another set's keys"
    assert "${{" not in "".join(runs), "inputs reach a shell only through env"


def test_a_change_to_the_install_runs_the_browser_jobs_on_its_pull_request():
    triggers = yaml.safe_load((WORKFLOWS / "playwright-e2e.yml").read_text(encoding="utf-8"))
    paths = (triggers.get("on") or triggers.get(True))["pull_request"]["paths"]

    assert f"{ACTION_PATH[2:]}/**" in paths
    assert "scripts/ci-install-playwright-browsers.sh" in paths


def test_dependabot_keeps_the_actions_the_local_action_pins_current():
    config = yaml.safe_load((ROOT / ".github" / "dependabot.yml").read_text(encoding="utf-8"))
    actions = next(update for update in config["updates"] if update["package-ecosystem"] == "github-actions")

    assert "/.github/actions/*" in actions["directories"]
    assert ACTION.parent.parent == ROOT / ".github" / "actions"


def _install(tmp_path, *browsers, failures, archives=None):
    stand_ins = tmp_path / "bin"
    stand_ins.mkdir()
    calls = tmp_path / "calls.log"
    count = tmp_path / "npx-count"
    bodies = {
        "npx": f'echo "npx $*" >> "{calls}"\n'
        f'cp "$PLAYWRIGHT_APT_CONF_DIR/80-ci-playwright-install" "{tmp_path}/apt-during-install" 2>/dev/null || true\n'
        f'n=$(( $(cat "{count}" 2>/dev/null || echo 0) + 1 ))\n'
        f'echo "$n" > "{count}"\n'
        f'[ "$n" -gt {failures} ]\n',
        "sudo": '[ "$1" = "-n" ] && shift\nexec "$@"\n',
        "pkill": f'echo "pkill $*" >> "{calls}"\nexit 1\n',
        "dpkg": f'echo "dpkg $*" >> "{calls}"\n',
        "chown": f'echo "chown $*" >> "{calls}"\n',
        "apt-get": f'echo "apt-get $*" >> "{calls}"\n',
    }
    for name, body in bodies.items():
        path = stand_ins / name
        path.write_text("#!/usr/bin/env bash\n" + body, encoding="utf-8")
        path.chmod(0o755)
    apt_conf = tmp_path / "apt.conf.d"
    apt_conf.mkdir()
    sources = tmp_path / "sources.list.d"
    sources.mkdir(exist_ok=True)
    env = dict(
        os.environ,
        PATH=f"{stand_ins}{os.pathsep}{os.environ.get('PATH', '')}",
        PLAYWRIGHT_INSTALL_BACKOFF="0",
        PLAYWRIGHT_INSTALL_TIMEOUT="60",
        PLAYWRIGHT_APT_CONF_DIR=str(apt_conf),
        PLAYWRIGHT_APT_SOURCES_DIR=str(sources),
    )
    env.pop("PLAYWRIGHT_APT_ARCHIVES", None)
    env.pop("PLAYWRIGHT_APT_UNNEEDED_HOSTS", None)
    if archives is not None:
        env["PLAYWRIGHT_APT_ARCHIVES"] = str(archives)
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


def test_the_packages_apt_downloads_stay_in_the_cached_archive_directory(tmp_path):
    archives = tmp_path / "playwright-apt"
    done, logged, apt_conf = _install(tmp_path, "chromium", failures=0, archives=archives)

    owner = f"{os.getuid()}:{os.getgid()}"
    assert done.returncode == 0, done.stderr
    assert logged == ["npx playwright install --with-deps chromium", "apt-get autoclean -qq", f"chown -R {owner} {archives}"]
    assert (archives / "partial").is_dir()
    during = (tmp_path / "apt-during-install").read_text(encoding="utf-8")
    assert f'Dir::Cache::Archives "{archives}/";' in during
    assert 'APT::Keep-Downloaded-Packages "true";' in during
    after = (apt_conf / "80-ci-playwright-install").read_text(encoding="utf-8")
    assert "Dir::Cache::Archives" not in after, "a later apt-get in the job would take the cache over"
    assert 'DPkg::Lock::Timeout "180";' in after


def test_without_an_archive_directory_apt_keeps_its_own(tmp_path):
    done, logged, apt_conf = _install(tmp_path, "chromium", failures=0)

    assert done.returncode == 0, done.stderr
    assert logged == ["npx playwright install --with-deps chromium"]
    assert "Dir::Cache::Archives" not in (apt_conf / "80-ci-playwright-install").read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "name,text",
    [
        ("microsoft-prod.list", "deb [arch=amd64] https://packages.microsoft.com/ubuntu/26.04/prod resolute main\n"),
        ("microsoft-prod.sources", "Types: deb\nURIs: https://packages.microsoft.com/ubuntu/26.04/prod\nSuites: resolute\n"),
    ],
)
def test_a_source_the_browsers_do_not_need_is_set_aside_before_the_install(tmp_path, name, text):
    """packages.microsoft.com refused ubuntu-26.04's index on 2026-11-27, and
    the apt-get update --with-deps runs failed every attempt with it. A
    one-line source and a deb822 one are both set aside."""

    sources = tmp_path / "sources.list.d"
    sources.mkdir()
    (sources / name).write_text(text, encoding="utf-8")
    (sources / "ubuntu.sources").write_text("Types: deb\nURIs: http://azure.archive.ubuntu.com/ubuntu/\n", encoding="utf-8")

    done, logged, _apt_conf = _install(tmp_path, "firefox", failures=0)

    assert done.returncode == 0, done.stderr
    assert sorted(path.name for path in sources.iterdir()) == sorted([f"{name}.playwright-unneeded", "ubuntu.sources"])
    assert f"Set aside {sources / name} (packages.microsoft.com) for the browser install." in done.stderr.splitlines()
    assert logged == ["npx playwright install --with-deps firefox"]
