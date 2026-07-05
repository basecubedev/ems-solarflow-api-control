# SPDX-License-Identifier: AGPL-3.0-or-later
"""Contract tests for the installable Admin Console bootstrap.

These keep the no-Git installer, the published-image runtime Compose files, and
the Docker publish workflow honest without a real daemon. The one test that
generates and validates Compose is gated behind Docker availability.
"""

import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.simulation

ROOT = Path(__file__).resolve().parents[1]
DEPLOY_ADMIN_DIR = ROOT / "deploy" / "admin"
INSTALLER = DEPLOY_ADMIN_DIR / "install-admin-console.sh"
RUNTIME_COMPOSE = DEPLOY_ADMIN_DIR / "docker-compose.runtime.yml"
RUNTIME_BRIDGE = DEPLOY_ADMIN_DIR / "docker-compose.runtime.bridge.yml"
RUNTIME_DISCOVERY = DEPLOY_ADMIN_DIR / "docker-compose.runtime.discovery-only.yml"
PUBLISH_WORKFLOW = ROOT / ".github" / "workflows" / "docker-publish.yml"

ADMIN_IMAGE = "ghcr.io/basecubedev/ems-solarflow-admin"


def read(path):
    return path.read_text(encoding="utf-8")


# --- Installer script contract --------------------------------------------


def test_admin_installer_exists_and_is_posix_shell():
    assert INSTALLER.is_file()
    assert INSTALLER.stat().st_mode & stat.S_IXUSR
    result = subprocess.run(
        ["sh", "-n", str(INSTALLER)], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr


def test_admin_installer_help_lists_documented_flags():
    result = subprocess.run(
        ["sh", str(INSTALLER), "--help"], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr
    for flag in (
        "--tag",
        "--bridge",
        "--bind",
        "--port",
        "--discovery-only",
        "--no-start",
        "--dry-run",
        "--force",
        "--help",
    ):
        assert flag in result.stdout, flag
    # Host networking is the documented default; --hostnet is not a user flag.
    assert "--hostnet" not in result.stdout


def test_admin_installer_declares_all_flags_in_parser():
    text = read(INSTALLER)
    for flag in (
        "--tag",
        "--bridge",
        "--bind",
        "--port",
        "--discovery-only",
        "--no-start",
        "--dry-run",
        "--force",
        "--install-dir",
    ):
        assert flag in text, flag


def test_admin_installer_defaults_to_host_networking():
    text = read(INSTALLER)
    assert "network_mode: host" in text
    assert "--bridge" in text


def test_admin_installer_supports_bridge_mode():
    text = read(INSTALLER)
    assert "--bridge" in text
    assert "127.0.0.1" in text
    assert "EMS_ADMIN_BIND" in text or "127.0.0.1:8090:8090" in text


def test_admin_installer_accepts_hostnet_as_compat_noop():
    # The old flag is still accepted so existing scripts do not break, but it is
    # documented only as a compatibility alias, not the primary path.
    text = read(INSTALLER)
    assert "--hostnet" in text
    assert "Host networking is already the default." in text


def test_admin_installer_mentions_no_git_runtime_path():
    text = read(INSTALLER)
    assert ADMIN_IMAGE in text
    assert "docker-compose.admin.yml" in text
    assert "EMS_INSTALL_DIR" in text
    assert "EMS_ADMIN_DATA_DIR" in text
    assert "DOCKER_GID" in text
    # No repository checkout is required for the end-user path.
    assert "git clone" not in text


def test_admin_installer_creates_admin_data_layout():
    text = read(INSTALLER)
    for sub in ("releases", "state", "staging", "backups"):
        assert f"data/admin/{sub}" in text, sub
    assert "mkdir -p" in text


def test_admin_installer_creates_config_dir_for_shared_password():
    # config/ must exist so the Admin Console can create config/dashboard-auth.json
    # before EMS or config/config.json exist (fresh install).
    text = read(INSTALLER)
    make_dirs = text.split("make_dirs()", 1)[1].split("}", 1)[0]
    assert "config" in make_dirs


def test_admin_installer_does_not_generate_password_or_token():
    # Auth is created in the browser (first visitor wins); the installer must not
    # bake a password, an env password, or a setup token.
    text = read(INSTALLER)
    assert "dashboard-auth.json" not in text
    assert "set-password" not in text
    assert "SETUP_TOKEN" not in text
    assert "ADMIN_PASSWORD" not in text


def test_admin_installer_help_lists_https_flags():
    result = subprocess.run(
        ["sh", str(INSTALLER), "--help"], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr
    for flag in ("--https", "--https-port", "--https-bind", "--no-https-auto-generate"):
        assert flag in result.stdout, flag


def test_admin_installer_declares_https_env_keys():
    text = read(INSTALLER)
    for key in (
        "EMS_ADMIN_HTTPS_ENABLED",
        "EMS_ADMIN_HTTPS_PORT",
        "EMS_ADMIN_HTTPS_CERT_FILE",
        "EMS_ADMIN_HTTPS_KEY_FILE",
        "EMS_ADMIN_HTTPS_AUTO_GENERATE",
    ):
        assert key in text, key
    # Default generated cert/key paths under the EMS install root.
    assert "config/admin.crt" in text
    assert "config/admin.key" in text


def test_admin_installer_declares_admin_update_metadata():
    # Non-secret identity so the Admin Console can update itself before a Guided
    # EMS Upgrade. The target image is always derived server-side from a tag.
    text = read(INSTALLER)
    for key in (
        "EMS_ADMIN_IMAGE",
        "EMS_ADMIN_TAG",
        "EMS_ADMIN_COMPOSE_FILE",
        "EMS_ADMIN_COMPOSE_SERVICE",
        "EMS_ADMIN_CONTAINER_NAME",
    ):
        assert key in text, key
    # A stable container name so the self-update can recreate the Admin service,
    # and a distinct compose service name for `docker compose up <service>`.
    assert "container_name:" in text
    assert 'CONTAINER_NAME="ems-solarflow-admin"' in text
    assert 'COMPOSE_SERVICE="ems-solarflow-admin"' in text


def test_admin_installer_generates_admin_update_metadata(tmp_path):
    # --no-start writes the compose without needing a Docker daemon.
    work = tmp_path / "work"
    work.mkdir()
    result = subprocess.run(
        ["sh", str(INSTALLER), "--no-start", "--install-dir", str(work)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    text = read(work / "docker-compose.admin.yml")
    assert "container_name: ems-solarflow-admin" in text
    assert 'EMS_ADMIN_IMAGE: "' + ADMIN_IMAGE + '"' in text
    assert 'EMS_ADMIN_TAG: "latest"' in text
    assert "EMS_ADMIN_COMPOSE_FILE:" in text
    assert "docker-compose.admin.yml" in text
    assert 'EMS_ADMIN_COMPOSE_SERVICE: "ems-solarflow-admin"' in text
    assert 'EMS_ADMIN_CONTAINER_NAME: "ems-solarflow-admin"' in text
    # The recorded env file also carries the reference identity.
    env_text = read(work / ".env.admin")
    assert "EMS_ADMIN_IMAGE=" + ADMIN_IMAGE in env_text
    assert "EMS_ADMIN_COMPOSE_SERVICE=ems-solarflow-admin" in env_text
    assert "EMS_ADMIN_CONTAINER_NAME=ems-solarflow-admin" in env_text


def test_admin_installer_https_dry_run_mentions_browser_warning(tmp_path):
    # Dry-run degrades without Docker and still prints the HTTPS/self-signed note.
    result = subprocess.run(
        ["sh", str(INSTALLER), "--dry-run", "--https"],
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "https://127.0.0.1:8091" in result.stdout
    assert "certificate warning" in result.stdout
    assert "8090/8091" in result.stdout


def test_admin_installer_dry_run_writes_nothing(tmp_path):
    # Dry-run degrades gracefully with or without Docker and must not touch disk.
    result = subprocess.run(
        ["sh", str(INSTALLER), "--dry-run"],
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "DRY-RUN" in result.stdout
    assert not (tmp_path / "docker-compose.admin.yml").exists()
    assert not (tmp_path / "config").exists()


def test_admin_installer_dry_run_ok_with_root_ids(tmp_path):
    # Root/zero ids must not fail --dry-run (it writes nothing anyway).
    env = dict(os.environ, PUID="0", PGID="0")
    result = subprocess.run(
        ["sh", str(INSTALLER), "--dry-run"],
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
        env=env,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert not (tmp_path / "docker-compose.admin.yml").exists()


def test_admin_installer_no_start_ok_with_root_ids(tmp_path):
    # --no-start writes config even with root ids (warns, does not fail); this is
    # what lets a root/no-Docker CI sandbox exercise config generation.
    work = tmp_path / "work"
    work.mkdir()
    env = dict(os.environ, PUID="0", PGID="0")
    result = subprocess.run(
        ["sh", str(INSTALLER), "--no-start", "--install-dir", str(work)],
        capture_output=True,
        text=True,
        env=env,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert (work / "docker-compose.admin.yml").is_file()


def test_admin_installer_real_install_still_refuses_root(tmp_path):
    # A real install (START=1) must still refuse to proceed with root ids.
    work = tmp_path / "work"
    work.mkdir()
    env = dict(os.environ, PUID="0", PGID="0")
    result = subprocess.run(
        ["sh", str(INSTALLER), "--install-dir", str(work)],
        capture_output=True,
        text=True,
        env=env,
    )
    assert result.returncode != 0
    assert not (work / "docker-compose.admin.yml").exists()


# --- Runtime Compose contract ---------------------------------------------


def test_admin_runtime_compose_uses_published_image_without_build():
    text = read(RUNTIME_COMPOSE)
    assert ADMIN_IMAGE in text
    assert "build:" not in text
    assert "/var/run/docker.sock:/var/run/docker.sock" in text
    assert '"${EMS_INSTALL_DIR}:${EMS_INSTALL_DIR}"' in text
    assert '"${EMS_ADMIN_DATA_DIR}:${EMS_ADMIN_DATA_DIR}"' in text
    # Hardened runtime settings carry over from the source compose.
    assert "read_only: true" in text
    assert "no-new-privileges:true" in text
    assert "${DOCKER_GID" in text
    # The end-user default is host networking, so no Docker port mapping.
    assert "network_mode: host" in text
    assert "ports:" not in text


def test_admin_runtime_bridge_compose_publishes_loopback_port():
    text = read(RUNTIME_BRIDGE)
    assert ADMIN_IMAGE in text
    assert "build:" not in text
    # Bridge mode publishes a port (loopback by default) instead of host net.
    assert "network_mode: host" not in text
    assert "ports:" in text
    assert '"${EMS_ADMIN_BIND:-127.0.0.1}:${EMS_ADMIN_PORT:-8090}:8090"' in text
    # Still deployment-capable and same-path mounted like the host-net default.
    assert "/var/run/docker.sock:/var/run/docker.sock" in text
    assert "${DOCKER_GID" in text
    assert '"${EMS_INSTALL_DIR}:${EMS_INSTALL_DIR}"' in text
    assert '"${EMS_ADMIN_DATA_DIR}:${EMS_ADMIN_DATA_DIR}"' in text


def test_admin_runtime_compose_files_declare_optional_https_env():
    for compose in (RUNTIME_COMPOSE, RUNTIME_BRIDGE):
        text = read(compose)
        # HTTPS is opt-in and defaults to off; HTTP is never disabled.
        assert 'EMS_ADMIN_HTTPS_ENABLED: "${EMS_ADMIN_HTTPS_ENABLED:-false}"' in text
        assert 'EMS_ADMIN_HTTPS_PORT: "${EMS_ADMIN_HTTPS_PORT:-8091}"' in text
        assert "EMS_ADMIN_HTTPS_AUTO_GENERATE" in text


def test_admin_runtime_compose_files_declare_admin_update_metadata():
    for compose in (RUNTIME_COMPOSE, RUNTIME_BRIDGE):
        text = read(compose)
        # Stable container name + non-secret identity for the self-update flow.
        assert "container_name: ${EMS_ADMIN_CONTAINER_NAME:-ems-solarflow-admin}" in text
        assert "EMS_ADMIN_IMAGE:" in text
        assert 'EMS_ADMIN_TAG: "${EMS_ADMIN_TAG:-latest}"' in text
        assert "EMS_ADMIN_COMPOSE_FILE:" in text
        assert "EMS_ADMIN_COMPOSE_SERVICE:" in text
        assert "EMS_ADMIN_CONTAINER_NAME:" in text


def test_admin_runtime_bridge_compose_maps_optional_https_port():
    text = read(RUNTIME_BRIDGE)
    assert (
        '"${EMS_ADMIN_HTTPS_BIND:-127.0.0.1}:${EMS_ADMIN_HTTPS_PORT:-8091}:8091"'
        in text
    )


def test_admin_runtime_discovery_only_has_no_socket_or_build():
    text = read(RUNTIME_DISCOVERY)
    assert ADMIN_IMAGE in text
    assert "build:" not in text
    assert "docker.sock" not in text
    assert "DOCKER_GID" not in text
    # Discovery-only also defaults to host networking so scans see the LAN.
    assert "network_mode: host" in text
    assert "ports:" not in text
    # Same-path mounting is preserved even without the socket.
    assert '"${EMS_INSTALL_DIR}:${EMS_INSTALL_DIR}"' in text
    assert '"${EMS_ADMIN_DATA_DIR}:${EMS_ADMIN_DATA_DIR}"' in text


# --- Docker publish workflow contract -------------------------------------


def test_publish_workflow_builds_both_images():
    text = read(PUBLISH_WORKFLOW)
    # The existing EMS image publish stays in place...
    assert "ghcr.io/basecubedev/ems-solarflow-api-control" in text
    # ...alongside the new Admin Console image from the Admin Dockerfile.
    assert ADMIN_IMAGE in text
    assert "deploy/admin/Dockerfile" in text
    # Admin image follows the same tag rules as the EMS image.
    assert "type=ref,event=tag" in text
    assert (
        "type=raw,value=latest,enable=${{ github.ref == 'refs/heads/main' }}" in text
    )


# --- Gated: generate and validate Compose with a real daemon --------------


def _docker_ready():
    if not shutil.which("docker"):
        return False
    try:
        if subprocess.run(
            ["docker", "compose", "version"], capture_output=True
        ).returncode != 0:
            return False
        return subprocess.run(["docker", "info"], capture_output=True).returncode == 0
    except OSError:
        return False


docker_required = pytest.mark.skipif(
    not _docker_ready(), reason="docker daemon not available"
)


@docker_required
@pytest.mark.parametrize("mode", ["deployment", "discovery-only"])
def test_installer_generates_valid_compose(tmp_path, mode):
    work = tmp_path / "work"
    work.mkdir()
    args = ["sh", str(INSTALLER), "--no-start"]
    if mode == "discovery-only":
        args.append("--discovery-only")
    result = subprocess.run(args, cwd=str(work), capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr

    compose = work / "docker-compose.admin.yml"
    assert compose.is_file()
    text = read(compose)
    assert ADMIN_IMAGE in text
    assert "build:" not in text
    for sub in ("releases", "state", "staging", "backups"):
        assert (work / "data" / "admin" / sub).is_dir(), sub

    if mode == "deployment":
        assert "/var/run/docker.sock:/var/run/docker.sock" in text
    else:
        assert "docker.sock" not in text

    validated = subprocess.run(
        ["docker", "compose", "-f", str(compose), "config"],
        capture_output=True,
        text=True,
    )
    assert validated.returncode == 0, validated.stderr


@docker_required
def test_installer_default_generates_host_network_compose(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    result = subprocess.run(
        ["sh", str(INSTALLER), "--no-start"],
        cwd=str(work),
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    compose = work / "docker-compose.admin.yml"
    text = read(compose)
    assert "network_mode: host" in text
    assert "127.0.0.1:8090:8090" not in text
    validated = subprocess.run(
        ["docker", "compose", "-f", str(compose), "config"],
        capture_output=True,
        text=True,
    )
    assert validated.returncode == 0, validated.stderr


@docker_required
def test_installer_host_https_enables_env_without_port_mapping(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    result = subprocess.run(
        ["sh", str(INSTALLER), "--no-start", "--https"],
        cwd=str(work),
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    text = read(work / "docker-compose.admin.yml")
    assert 'EMS_ADMIN_HTTPS_ENABLED: "true"' in text
    # Host networking never adds a Docker port mapping, even with HTTPS.
    assert "network_mode: host" in text
    assert "ports:" not in text
    validated = subprocess.run(
        ["docker", "compose", "-f", str(work / "docker-compose.admin.yml"), "config"],
        capture_output=True,
        text=True,
    )
    assert validated.returncode == 0, validated.stderr


@docker_required
def test_installer_bridge_https_maps_8091(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    result = subprocess.run(
        ["sh", str(INSTALLER), "--no-start", "--bridge", "--https"],
        cwd=str(work),
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    text = read(work / "docker-compose.admin.yml")
    assert 'EMS_ADMIN_HTTPS_ENABLED: "true"' in text
    assert "127.0.0.1:8091:8091" in text
    validated = subprocess.run(
        ["docker", "compose", "-f", str(work / "docker-compose.admin.yml"), "config"],
        capture_output=True,
        text=True,
    )
    assert validated.returncode == 0, validated.stderr


@docker_required
def test_installer_default_omits_https_port_mapping(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    result = subprocess.run(
        ["sh", str(INSTALLER), "--no-start", "--bridge"],
        cwd=str(work),
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    text = read(work / "docker-compose.admin.yml")
    assert 'EMS_ADMIN_HTTPS_ENABLED: "false"' in text
    # Without --https the bridge compose maps only the HTTP port.
    assert "8091:8091" not in text


@docker_required
def test_installer_bridge_publishes_loopback_port(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    result = subprocess.run(
        ["sh", str(INSTALLER), "--no-start", "--bridge"],
        cwd=str(work),
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    compose = work / "docker-compose.admin.yml"
    text = read(compose)
    assert "network_mode: host" not in text
    assert "127.0.0.1:8090:8090" in text
    validated = subprocess.run(
        ["docker", "compose", "-f", str(compose), "config"],
        capture_output=True,
        text=True,
    )
    assert validated.returncode == 0, validated.stderr


def test_smoke_marker():
    assert sys.executable
