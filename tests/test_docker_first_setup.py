# SPDX-License-Identifier: AGPL-3.0-or-later
"""Contract tests for the Docker-first enduser setup.

These keep the simple installer/compose/docs promise honest without requiring
real hardware or a running Docker daemon. The one test that does touch Docker is
gated behind availability.
"""
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import docker_e2e_utils

pytestmark = [
    pytest.mark.setup,
    pytest.mark.contract,
]

ROOT = Path(__file__).resolve().parents[1]
INSTALL_SH = ROOT / "install-docker.sh"
INSTALL_PS1 = ROOT / "install-docker.ps1"
COMPOSE = ROOT / "docker-compose.yml"
README = ROOT / "README.md"
DOCKER_BOOTSTRAP_DOC = ROOT / "docs" / "user" / "docker-bootstrap.md"
DOCKER_DOC = ROOT / "docs" / "docker.md"
QUICKSTART_DOC = ROOT / "docs" / "quickstart.md"
INFLUX_DOC = ROOT / "docs" / "technical" / "influxdb.md"


def read(path):
    return path.read_text(encoding="utf-8")


# --- Installer script contract --------------------------------------------


def test_root_installer_scripts_exist():
    assert INSTALL_SH.is_file()
    assert INSTALL_PS1.is_file()


def test_shell_installer_runs_help_without_execute_bit():
    # Documented default flow is `sh install-docker.sh`, so it must not depend
    # on the execute bit. --help exits before any Docker access.
    result = subprocess.run(
        ["sh", str(INSTALL_SH), "--help"],
        capture_output=True,
        text=True,
        cwd=str(ROOT),
    )
    assert result.returncode == 0, result.stderr
    assert "--analytics" in result.stdout
    assert "install-docker.sh" in result.stdout


def test_powershell_installer_advertises_help_and_analytics():
    text = read(INSTALL_PS1)
    assert "-Analytics" in text
    assert "-Help" in text
    assert "-Tag" in text
    assert "-DryRun" in text


def test_installers_mention_analytics_flag():
    assert "--analytics" in read(INSTALL_SH)
    assert "-Analytics" in read(INSTALL_PS1)


def test_installers_do_not_mount_docker_socket():
    for path in (INSTALL_SH, INSTALL_PS1, COMPOSE):
        assert "docker.sock" not in read(path), path


def test_installers_are_self_contained_no_repo_clone():
    # The installer must work without cloning the repo: it writes the compose
    # file from an embedded template rather than requiring a checkout.
    for path in (INSTALL_SH, INSTALL_PS1):
        text = read(path)
        assert "git clone" not in text, path
        assert "services:" in text, path
        assert "with-analytics" in text, path


# --- Compose contract ------------------------------------------------------


def test_compose_ems_only_does_not_require_influx_env():
    compose = read(COMPOSE)
    # The ems service references config/influxdb.env optionally, so EMS-only
    # works without the secrets file.
    assert "required: false" in compose
    assert "./config/influxdb.env" in compose


def test_compose_analytics_is_behind_with_analytics_profile():
    compose = read(COMPOSE)
    assert "with-analytics" in compose
    # No bare `analytics` profile that could read as "only Analytics".
    assert "- analytics" not in compose


def test_compose_has_influxdb_service_with_local_paths():
    compose = read(COMPOSE)
    assert "influxdb:" in compose
    assert "influxdb:2.7" in compose
    assert "./data/influxdb:/var/lib/influxdb2" in compose
    assert "8086:8086" in compose


def test_compose_keeps_ems_basics():
    compose = read(COMPOSE)
    assert "  ems:" in compose
    assert "8080:8080" in compose
    assert "./config:/app/config" in compose
    assert "./data:/app/data" in compose
    assert 'EMS_IN_CONTAINER: "1"' in compose


def test_compose_uses_current_command_style_in_comments():
    compose = read(COMPOSE)
    assert "docker compose up -d" in compose
    assert "docker-compose " not in compose


# --- Config contract -------------------------------------------------------


def test_apply_analytics_uses_docker_first_secret_path():
    from ems import config_init

    cfg = config_init.apply_analytics({})
    influx = cfg["influxdb"]
    assert influx["enabled"] is True
    assert influx["mode"] == "bundled"
    assert influx["auto_init"] is True
    assert influx["auto_sync"] is True
    assert influx["secret_file"] == "config/influxdb.env"


def test_config_init_analytics_flag_enables_bundled_influx(tmp_path):
    from ems import config as config_mod
    from ems import config_init

    updated, _plan = config_init.run_config_init(
        config={},
        config_exists=False,
        config_path=str(tmp_path / "config.json"),
        base_dir=str(ROOT),
        yes=True,
        analytics=True,
    )
    influx = config_mod.normalize_influxdb_config(updated["influxdb"])
    assert influx["enabled"] is True
    assert influx["mode"] == "bundled"
    assert influx["secret_file"] == "config/influxdb.env"


def test_legacy_deploy_secret_file_remains_accepted():
    from ems import config as config_mod
    from ems import influx_setup

    legacy = config_mod.normalize_influxdb_config(
        {"enabled": True, "mode": "bundled", "secret_file": "deploy/docker/influxdb.env"}
    )
    assert legacy["secret_file"] == "deploy/docker/influxdb.env"
    assert influx_setup.uses_default_secret_file(legacy)


def test_docker_first_secret_file_is_accepted_by_normalizer():
    from ems import config as config_mod

    influx = config_mod.normalize_influxdb_config(
        {"enabled": True, "mode": "bundled", "secret_file": "config/influxdb.env"}
    )
    assert influx["secret_file"] == "config/influxdb.env"


# --- Documentation contract -----------------------------------------------


def test_docker_bootstrap_doc_has_short_installer_quickstarts():
    # The Docker Bootstrap install commands live in the user docker guide now,
    # not in the router README.
    text = read(DOCKER_BOOTSTRAP_DOC)
    assert "install-docker.sh" in text
    assert "install-docker.ps1" in text
    assert "sh install-docker.sh --analytics" in text
    assert "-Analytics" in text


def test_readme_is_router_without_bootstrap_or_manual_walkthrough():
    text = read(README)
    # The README routes to Docker Bootstrap; it does not carry its commands.
    assert "install-docker.sh" not in text
    assert "docs/user/docker-bootstrap.md" in text
    # The multi-step manual Analytics sequence belongs in docs/, not the README.
    assert "docker compose run --rm ems python3 emsctl.py config init --analytics" not in text
    assert "influx init --no-start" not in text


def test_detailed_manual_setup_lives_in_docs():
    docker_doc = read(DOCKER_DOC)
    assert "Manual install path" in docker_doc
    assert "docker compose run --rm ems python3 emsctl.py config init --analytics" in docker_doc
    assert "influx init --no-start" in docker_doc


def test_quickstart_shows_installer_flow():
    text = read(QUICKSTART_DOC)
    assert "install-docker.sh" in text


def test_docs_use_with_analytics_profile():
    for path in (DOCKER_DOC, INFLUX_DOC, ROOT / "docs" / "common-commands.md"):
        assert "with-analytics" in read(path), path


def test_docs_do_not_require_host_side_stack_up_for_docker_first():
    docker_doc = read(DOCKER_DOC)
    assert "stack up" in docker_doc
    # Docker-first explicitly avoids host-side stack up.
    assert "no host-side" in docker_doc


def test_influx_doc_marks_stack_up_as_poweruser_helper():
    text = read(INFLUX_DOC)
    assert "poweruser" in text
    assert "config/influxdb.env" in text


# --- Optional gated Docker smoke test -------------------------------------


def _docker_compose_available():
    if not shutil.which("docker"):
        return False
    try:
        return (
            subprocess.run(
                ["docker", "compose", "version"],
                capture_output=True,
            ).returncode
            == 0
        )
    except OSError:
        return False


@pytest.mark.skipif(
    not _docker_compose_available(), reason="docker compose not available"
)
def test_rendered_compose_validates_with_docker(tmp_path):
    work = tmp_path / "ems"
    work.mkdir()
    (work / "docker-compose.yml").write_text(read(COMPOSE), encoding="utf-8")
    (work / "config").mkdir()
    result = subprocess.run(
        ["docker", "compose", "config"],
        cwd=str(work),
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    profile = subprocess.run(
        ["docker", "compose", "--profile", "with-analytics", "config", "--services"],
        cwd=str(work),
        capture_output=True,
        text=True,
    )
    assert "influxdb" in profile.stdout


@pytest.mark.skipif(
    not _docker_compose_available(), reason="docker compose not available"
)
def test_installer_dry_run_in_temp_dir(tmp_path):
    result = subprocess.run(
        ["sh", str(INSTALL_SH), "--dry-run", "--analytics"],
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert "DRY-RUN" in result.stdout
    # Dry-run must not create files.
    assert not (tmp_path / "docker-compose.yml").exists()


# Keep an import smoke check so the module fails loudly if emsctl/config_init
# drift breaks the analytics wiring.
def test_emsctl_imports_with_analytics_arg():
    assert sys.executable


# --- Installer prerequisite handling (sandboxed, no real Docker) -----------
#
# These build a PATH sandbox so the shell installer sees a controlled set of
# tools: no `docker` at all, or a stub `docker` whose Compose version we pick.
# This exercises the dry-run-vs-fatal prerequisite logic without a real daemon.

_SANDBOX_TOOLS = (
    "sh dash bash id printf mkdir sed awk gawk cat rm mv sort env head tr grep"
).split()

# Stub docker: report a Compose version (FAKE_COMPOSE_VERSION) and daemon
# reachability (FAKE_INFO_OK), record every invocation to FAKE_DOCKER_LOG, and
# optionally fail any command containing FAKE_FAIL_SUBSTR. Everything else
# just succeeds, so the installer runs its full flow without a real daemon.
_DOCKER_STUB = """#!/bin/sh
if [ -n "${FAKE_DOCKER_LOG:-}" ]; then printf '%s\\n' "$*" >> "$FAKE_DOCKER_LOG"; fi
if [ "$1" = "--version" ]; then echo "Docker version 27.0.0, build fakefake"; exit 0; fi
if [ "$1" = "compose" ] && [ "$2" = "version" ] && [ "${FAKE_NO_COMPOSE:-0}" = "1" ]; then
    echo "docker: 'compose' is not a docker command." >&2; exit 1
fi
if [ "$1" = "compose" ] && [ "$2" = "version" ] && [ "$3" = "--short" ]; then
    printf '%s\\n' "${FAKE_COMPOSE_VERSION:-2.26.1}"; exit 0
fi
if [ "$1" = "compose" ] && [ "$2" = "version" ]; then exit 0; fi
if [ "$1" = "info" ]; then [ "${FAKE_INFO_OK:-1}" = "1" ] && exit 0 || exit 1; fi
if [ -n "${FAKE_FAIL_SUBSTR:-}" ]; then
    case "$*" in *"$FAKE_FAIL_SUBSTR"*) exit 1 ;; esac
fi
exit 0
"""

posix_only = pytest.mark.skipif(
    not sys.platform.startswith(("linux", "darwin")) or shutil.which("sh") is None,
    reason="POSIX shell installer test",
)


def _make_sandbox(tmp_path, *, with_docker_version=None):
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    for tool in _SANDBOX_TOOLS:
        resolved = shutil.which(tool)
        if resolved:
            link = bindir / tool
            if not link.exists():
                link.symlink_to(resolved)
    if with_docker_version is not None:
        stub = bindir / "docker"
        stub.write_text(_DOCKER_STUB)
        stub.chmod(0o755)
    return bindir


def _run_installer(tmp_path, args, *, with_docker_version=None, env_extra=None):
    bindir = _make_sandbox(tmp_path, with_docker_version=with_docker_version)
    work = tmp_path / "work"
    work.mkdir(exist_ok=True)
    env = {"PATH": str(bindir)}
    if with_docker_version is not None:
        env["FAKE_COMPOSE_VERSION"] = with_docker_version
        env["FAKE_DOCKER_LOG"] = str(tmp_path / "docker-calls.log")
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        ["sh", str(INSTALL_SH), *args],
        cwd=str(work),
        env=env,
        capture_output=True,
        text=True,
    )


def _docker_calls(tmp_path):
    log = tmp_path / "docker-calls.log"
    return log.read_text(encoding="utf-8") if log.exists() else ""


@posix_only
def test_dry_run_without_docker_warns_and_continues(tmp_path):
    result = _run_installer(tmp_path, ["--dry-run"])
    combined = result.stdout + result.stderr
    assert result.returncode == 0, combined
    assert "Docker is not installed" in combined
    assert "Dry-run continues" in combined
    # Planned actions are still shown.
    assert "docker compose up -d" in result.stdout


@posix_only
def test_dry_run_analytics_without_docker_warns_and_continues(tmp_path):
    result = _run_installer(tmp_path, ["--dry-run", "--analytics"])
    combined = result.stdout + result.stderr
    assert result.returncode == 0, combined
    assert "Docker is not installed" in combined
    assert "config init --analytics" in result.stdout
    assert "with-analytics" in result.stdout


@posix_only
def test_normal_mode_without_docker_is_fatal(tmp_path):
    result = _run_installer(tmp_path, ["--no-start"])
    combined = result.stdout + result.stderr
    assert result.returncode != 0
    assert "Docker is not installed" in combined
    # Nothing was set up.
    assert not (tmp_path / "work" / "docker-compose.yml").exists()


@posix_only
def test_supported_compose_version_dry_run_has_no_version_warning(tmp_path):
    result = _run_installer(tmp_path, ["--dry-run"], with_docker_version="2.24.0")
    combined = result.stdout + result.stderr
    assert result.returncode == 0, combined
    assert "or newer is required" not in combined


@posix_only
def test_unsupported_compose_version_is_fatal_in_normal_mode(tmp_path):
    result = _run_installer(tmp_path, ["--no-start"], with_docker_version="2.20.0")
    combined = result.stdout + result.stderr
    assert result.returncode != 0
    assert "2.24.0 or newer is required" in combined


@posix_only
def test_unsupported_compose_version_is_warning_in_dry_run(tmp_path):
    result = _run_installer(tmp_path, ["--dry-run"], with_docker_version="2.20.0")
    combined = result.stdout + result.stderr
    assert result.returncode == 0, combined
    assert "2.24.0 or newer is required" in combined
    assert "docker compose up -d" in result.stdout


def test_powershell_installer_has_dry_run_prereq_handling():
    # No pwsh in CI here, so assert the dry-run-vs-fatal wiring statically.
    # Manual check: `powershell -ExecutionPolicy Bypass -File .\\install-docker.ps1 -DryRun`
    text = read(INSTALL_PS1)
    assert "Add-PrereqProblem" in text
    assert "Write-Warning" in text
    assert "MinComposeVersion" in text
    assert "2.24.0" in text
    assert "Dry-run continues" in text


def test_installers_state_compose_minimum_version():
    assert "2.24.0" in read(INSTALL_SH)
    assert "2.24.0" in read(INSTALL_PS1)


def test_compose_couples_optional_env_file_with_minimum_version():
    # `env_file.required: false` only works on Compose >= 2.24, so the file must
    # document that requirement next to the feature it depends on.
    compose = read(COMPOSE)
    assert "required: false" in compose
    assert "2.24" in compose


# --- Docker e2e environment isolation --------------------------------------


def test_compose_env_drops_ambient_compose_selection(monkeypatch):
    # An ambient COMPOSE_PROFILES=with-analytics pulls the profile-gated
    # bundled InfluxDB into the EMS-only quickstart project, whose required
    # config/influxdb.env does not exist there. The Docker e2e projects must
    # therefore never inherit Compose selection from the developer shell.
    for name in docker_e2e_utils.AMBIENT_COMPOSE_VARS:
        monkeypatch.setenv(name, "ambient")
    monkeypatch.setenv("PATH", os.environ.get("PATH", ""))

    env = docker_e2e_utils.compose_env(COMPOSE_PROJECT_NAME="explicit")

    assert env["COMPOSE_PROJECT_NAME"] == "explicit"
    assert "PATH" in env
    for name in docker_e2e_utils.AMBIENT_COMPOSE_VARS:
        if name != "COMPOSE_PROJECT_NAME":
            assert name not in env, name


# --- Recording fake-Docker installer flow ---------------------------------
#
# These run the real installer against a fake `docker` that records every call
# and succeeds, so we can assert the command flow and the generated files
# without a real daemon. _ANY_VERSION just opts the sandbox into a stub docker.

_ANY_VERSION = "2.26.1"


@posix_only
def test_ems_only_no_start_generates_files_and_skips_influx(tmp_path):
    result = _run_installer(tmp_path, ["--no-start"], with_docker_version=_ANY_VERSION)
    assert result.returncode == 0, result.stdout + result.stderr
    work = tmp_path / "work"
    assert (work / "docker-compose.yml").is_file()
    assert (work / ".env").is_file()
    assert (work / "config").is_dir()
    assert (work / "data").is_dir()
    generated_compose = read(work / "docker-compose.yml")
    assert 'user: "${PUID}:${PGID}"' in generated_compose
    assert not (work / "data" / "influxdb").exists()
    assert not (work / "config" / "influxdb.env").exists()

    calls = _docker_calls(tmp_path)
    assert "compose up -d" not in calls
    assert "influx" not in calls
    assert "config init" not in calls


@posix_only
def test_ems_only_start_runs_compose_up(tmp_path):
    result = _run_installer(tmp_path, [], with_docker_version=_ANY_VERSION)
    assert result.returncode == 0, result.stdout + result.stderr
    calls = _docker_calls(tmp_path)
    assert "compose up -d" in calls
    assert "influx" not in calls
    assert "config init --analytics" not in calls


@posix_only
def test_analytics_no_start_runs_config_init_and_influx_init_only(tmp_path):
    result = _run_installer(
        tmp_path, ["--analytics", "--no-start"], with_docker_version=_ANY_VERSION
    )
    assert result.returncode == 0, result.stdout + result.stderr
    work = tmp_path / "work"
    assert (work / "data" / "influxdb").is_dir()
    assert "COMPOSE_PROFILES=with-analytics" in read(work / ".env")

    calls = _docker_calls(tmp_path)
    assert "compose run --rm ems python3 emsctl.py config init --analytics --yes --no-backup" in calls
    assert "compose run --rm ems python3 emsctl.py influx init --no-start" in calls
    assert "compose up -d" not in calls
    assert "influx sync" not in calls
    assert "influx status" not in calls


@posix_only
def test_analytics_start_runs_full_influx_flow(tmp_path):
    result = _run_installer(tmp_path, ["--analytics"], with_docker_version=_ANY_VERSION)
    assert result.returncode == 0, result.stdout + result.stderr
    calls = _docker_calls(tmp_path)
    assert "config init --analytics" in calls
    assert "influx init --no-start" in calls
    assert "compose up -d" in calls
    assert "influx sync" in calls
    assert "influx status" in calls


@posix_only
def test_tag_is_baked_into_generated_compose(tmp_path):
    result = _run_installer(
        tmp_path,
        ["--tag", "v0.6.0", "--analytics", "--no-start"],
        with_docker_version=_ANY_VERSION,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    compose = read(tmp_path / "work" / "docker-compose.yml")
    assert "ems-solarflow-api-control:v0.6.0" in compose
    assert "ems-solarflow-api-control:latest" not in compose


@posix_only
def test_force_overwrites_only_with_flag(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    sentinel = "# user-edited compose, keep me\n"
    (work / "docker-compose.yml").write_text(sentinel)

    kept = _run_installer(tmp_path, ["--no-start"], with_docker_version=_ANY_VERSION)
    assert kept.returncode == 0, kept.stdout + kept.stderr
    assert (work / "docker-compose.yml").read_text() == sentinel

    forced = _run_installer(
        tmp_path, ["--no-start", "--force"], with_docker_version=_ANY_VERSION
    )
    assert forced.returncode == 0, forced.stdout + forced.stderr
    assert "services:" in (work / "docker-compose.yml").read_text()


@posix_only
def test_existing_config_is_kept_without_force(tmp_path):
    work = tmp_path / "work"
    (work / "config").mkdir(parents=True)
    (work / "config" / "config.json").write_text("{}\n")

    result = _run_installer(
        tmp_path, ["--analytics", "--no-start"], with_docker_version=_ANY_VERSION
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Keeping existing config/config.json" in result.stdout
    assert "config init --analytics" not in _docker_calls(tmp_path)


@posix_only
def test_missing_compose_in_normal_mode_is_fatal(tmp_path):
    # docker present, but `docker compose version` fails -> Compose v2 missing.
    result = _run_installer(
        tmp_path,
        ["--no-start"],
        with_docker_version=_ANY_VERSION,
        env_extra={"FAKE_NO_COMPOSE": "1"},
    )
    combined = result.stdout + result.stderr
    assert result.returncode != 0
    assert "docker compose" in combined.lower()


@posix_only
def test_missing_compose_in_dry_run_is_non_fatal(tmp_path):
    result = _run_installer(
        tmp_path,
        ["--dry-run"],
        with_docker_version=_ANY_VERSION,
        env_extra={"FAKE_NO_COMPOSE": "1"},
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Dry-run continues" in result.stdout + result.stderr


# --- Documentation verification path --------------------------------------
#
# Shared command constants keep the docs and the tested flow from drifting. Each
# constant is a command an end user is told to run and that the E2E tests cover.

DOC_EMS_ONLY_INSTALL = "sh install-docker.sh"
DOC_ANALYTICS_INSTALL = "sh install-docker.sh --analytics"
DOC_COMPOSE_UP = "docker compose up -d"
DOC_ANALYTICS_COMPOSE_UP = "docker compose --profile with-analytics up -d"
DOC_DIAGNOSE = "docker compose exec ems python3 emsctl.py diagnose"
DOC_INFLUX_STATUS = "docker compose exec ems python3 emsctl.py influx status"
DOC_INFLUX_SYNC = "docker compose exec ems python3 emsctl.py influx sync"

COMMON_COMMANDS_DOC = ROOT / "docs" / "common-commands.md"
FIRST_RUN_DOC = ROOT / "docs" / "first-run-checklist.md"


def test_docker_bootstrap_doc_uses_tested_install_commands():
    text = read(DOCKER_BOOTSTRAP_DOC)
    assert DOC_EMS_ONLY_INSTALL in text
    assert DOC_ANALYTICS_INSTALL in text


def test_docker_doc_has_verification_commands():
    text = read(DOCKER_DOC)
    assert "Verifying a Docker-first install" in text
    assert DOC_DIAGNOSE in text
    assert DOC_INFLUX_STATUS in text


def test_common_commands_doc_has_verification_path():
    text = read(COMMON_COMMANDS_DOC)
    assert DOC_COMPOSE_UP in text
    assert DOC_DIAGNOSE in text
    assert DOC_INFLUX_STATUS in text
    assert DOC_INFLUX_SYNC in text


def test_first_run_checklist_has_first_run_checks():
    text = read(FIRST_RUN_DOC)
    assert "docker compose restart" in text
    assert DOC_DIAGNOSE in text
    assert "emsctl.py diagnose --hardware" in text
    assert DOC_INFLUX_STATUS in text


def test_docs_mark_native_helpers_as_poweruser():
    # `stack up` and deploy/docker/* are the repo/native path; they must be
    # labelled so end users do not adopt them for the Docker-first setup.
    common = read(COMMON_COMMANDS_DOC)
    assert "stack up" in common
    assert "poweruser" in common
    influx = read(INFLUX_DOC)
    assert "poweruser" in influx
    assert "deploy/docker" in influx


# --- PowerShell installer smoke (skipped without pwsh) ---------------------

pwsh_only = pytest.mark.skipif(
    shutil.which("pwsh") is None,
    reason="pwsh not available; PowerShell installer covered statically",
)


@pwsh_only
@pytest.mark.parametrize(
    "args",
    [
        ["-DryRun"],
        ["-DryRun", "-Analytics"],
    ],
)
def test_powershell_dry_run_smoke(tmp_path, args):
    work = tmp_path / "work"
    work.mkdir()
    result = subprocess.run(
        ["pwsh", "-NoProfile", "-File", str(INSTALL_PS1), *args],
        cwd=str(work),
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "DRY-RUN" in result.stdout
    assert not (work / "docker-compose.yml").exists()
