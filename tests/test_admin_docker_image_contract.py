# SPDX-License-Identifier: AGPL-3.0-or-later
"""The Admin image must copy every file its startup import chain needs.

`admin/install_context.py` imports `ems.paths`, so the Admin runtime reaches the
EMS package during startup. This test rebuilds the exact file set that
`deploy/admin/Dockerfile` copies into `/app` and asserts `python -m admin`
still imports, catching a Dockerfile that forgets a runtime dependency again.
No Docker, network, ports, or device discovery are required.
"""

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.simulation

ROOT = Path(__file__).resolve().parent.parent
DOCKERFILE = ROOT / "deploy" / "admin" / "Dockerfile"
DEPLOY_ADMIN_DIR = ROOT / "deploy" / "admin"
RUNTIME_COMPOSE_FILES = (
    DEPLOY_ADMIN_DIR / "docker-compose.runtime.yml",
    DEPLOY_ADMIN_DIR / "docker-compose.runtime.bridge.yml",
)

_COPY = re.compile(r"^COPY\s+(?!--)(\S+)\s+(\S+)\s*$")


def _dockerfile_app_copies():
    """Yield (src, dest) pairs for COPY directives that land under /app."""

    for line in DOCKERFILE.read_text(encoding="utf-8").splitlines():
        match = _COPY.match(line.strip())
        if not match:
            continue
        src, dest = match.group(1), match.group(2)
        if dest.startswith("./") or dest == ".":
            yield src, dest.lstrip("./")


def _mirror_image_files(app_root):
    copied = []
    for src, dest in _dockerfile_app_copies():
        source = ROOT / src
        target = app_root / (dest or src)
        if source.is_dir():
            shutil.copytree(
                source,
                target,
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
            )
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
        copied.append(dest)
    return copied


def test_dockerfile_copies_the_ems_path_resolver():
    copies = dict(
        (dest, src) for src, dest in _dockerfile_app_copies()
    )
    assert "ems/__init__.py" in copies
    assert "ems/paths.py" in copies


def test_admin_image_contains_dashboard_auth_helper():
    # Admin shares the EMS Dashboard password; the auth helper must ship in the
    # image so the Admin server can hash/verify against config/dashboard-auth.json.
    dockerfile = DOCKERFILE.read_text(encoding="utf-8")
    assert "COPY dashboard/auth.py ./dashboard/auth.py" in dockerfile


def test_admin_image_contains_https_helper():
    # The optional Admin HTTPS listener reuses dashboard/https.py; it must ship in
    # the image (certificate generation lives there, never duplicated in admin/).
    text = DOCKERFILE.read_text(encoding="utf-8")
    assert "COPY dashboard/https.py ./dashboard/https.py" in text
    assert "EXPOSE 8090 8091" in text or "EXPOSE 8091" in text


def test_admin_image_ships_no_docker_daemon():
    # Docker-out-of-Docker: the Admin container controls the *host* engine over
    # the mounted socket. It must never ship a daemon (the self-update recreate
    # runs against the host daemon, not one inside the container).
    text = DOCKERFILE.read_text(encoding="utf-8")
    # No daemon package and no privileged/dind installation is present.
    assert "docker.io" not in text
    assert "--privileged" not in text
    assert "install -m 0755 /tmp/docker/dockerd" not in text
    # It installs only the static Docker CLI binary, not a daemon; the daemon
    # binary from the static tarball is never extracted or installed.
    assert "docker/docker" in text
    assert "docker/dockerd" not in text


def test_admin_image_keeps_docker_cli_and_compose_plugin():
    # The Admin self-update recreates the Admin service with `docker compose`, so
    # the CLI and the Compose plugin must remain in the image.
    text = DOCKERFILE.read_text(encoding="utf-8")
    assert "/usr/local/bin/docker" in text
    assert "cli-plugins/docker-compose" in text
    assert "docker compose version" in text


def test_admin_image_runs_as_non_root():
    text = DOCKERFILE.read_text(encoding="utf-8")
    assert "USER admin" in text
    # The non-root user is created before it is selected.
    assert "adduser --system --ingroup admin" in text


def test_admin_runtime_compose_keeps_hardening():
    # The self-update recreates the Admin service from these compose files; the
    # hardened runtime settings must survive.
    for compose in RUNTIME_COMPOSE_FILES:
        text = compose.read_text(encoding="utf-8")
        assert "read_only: true" in text, compose.name
        assert "no-new-privileges:true" in text, compose.name
        assert "cap_drop:" in text and "- ALL" in text, compose.name


def test_copied_files_are_sufficient_for_admin_startup(tmp_path):
    app_root = tmp_path / "app"
    app_root.mkdir()
    _mirror_image_files(app_root)

    # Drop PYTHONPATH so imports resolve only against the mirrored image layout,
    # not the real repo checkout — otherwise a missing COPY would go unnoticed.
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)

    result = subprocess.run(
        [sys.executable, "-B", "-m", "admin", "--help"],
        cwd=app_root,
        capture_output=True,
        text=True,
        env=env,
    )

    assert result.returncode == 0, (
        f"admin --help failed inside the mirrored image layout:\n{result.stderr}"
    )
    assert "usage" in result.stdout.lower()


def test_mirrored_image_builds_offline_zendure_mqtt_status(tmp_path):
    """The offline telemetry status must work inside the image file set.

    The Maintenance card degraded to a red "Unavailable" because the container
    lacked ems.zendure_mqtt.runtime: the offline builder raised ImportError for
    every install, even ones that do not use MQTT at all. The image must ship
    the (client-free) status import chain so a no-MQTT install reads
    "inactive", not "unavailable".
    """

    app_root = tmp_path / "app"
    app_root.mkdir()
    _mirror_image_files(app_root)

    install = tmp_path / "install"
    (install / "config").mkdir(parents=True)
    (install / "config" / "config.json").write_text(
        json.dumps(
            {
                "config_schema_version": 3,
                "system": {"enabled": True},
                "grid_meter": {"type": "shelly", "ip": "192.168.1.50"},
                "devices": [{"name": "WR1", "ip": "192.168.1.100", "sn": "SN1"}],
            }
        ),
        encoding="utf-8",
    )

    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    script = (
        "import sys, json\n"
        "from admin.zendure_mqtt_runtime_status import build_runtime_status_view\n"
        f"view = build_runtime_status_view(base_dir={str(install)!r})\n"
        "print(json.dumps({'state': view['runtime_state'], 'available': view['available']}))\n"
        # The image must keep excluding the paho-backed client module.
        "assert 'ems.zendure_mqtt.client' not in sys.modules\n"
    )
    result = subprocess.run(
        [sys.executable, "-B", "-c", script],
        cwd=app_root,
        capture_output=True,
        text=True,
        env=env,
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout.strip())
    assert payload == {"state": "inactive", "available": True}


def test_mirrored_image_imports_zendure_mqtt_migration(tmp_path):
    """The packaged Admin migration path must import inside the image file set.

    Admin's migration review/apply and the Guided Upgrade preflight all reach
    ``ems.zendure_mqtt.migration`` through EMS/Core. A Dockerfile that copies
    ``admin/`` but forgets ``ems/zendure_mqtt/migration.py`` makes every
    migration endpoint fail with ImportError inside the container. Reproduce the
    exact image file set (no Docker) and import the migration entry points.
    """

    app_root = tmp_path / "app"
    app_root.mkdir()
    _mirror_image_files(app_root)

    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    script = (
        "import admin.zendure_mqtt_migration_review as review\n"
        "from ems.zendure_mqtt.migration import plan_zendure_mqtt_migration\n"
        "assert callable(review.zendure_mqtt_migration_review)\n"
        "assert callable(review.apply_zendure_mqtt_migration)\n"
        "assert callable(plan_zendure_mqtt_migration)\n"
        "print('ok')\n"
    )
    result = subprocess.run(
        [sys.executable, "-B", "-c", script],
        cwd=app_root,
        capture_output=True,
        text=True,
        env=env,
    )
    assert result.returncode == 0, (
        f"packaged Admin migration import failed inside the mirrored image "
        f"layout:\n{result.stderr}"
    )
    assert result.stdout.strip() == "ok"


def test_mirrored_image_runs_migration_review(tmp_path):
    """The EMS-owned migration review must run inside the image file set."""

    app_root = tmp_path / "app"
    app_root.mkdir()
    _mirror_image_files(app_root)

    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    script = (
        "import json\n"
        "from admin.zendure_mqtt_migration_review import zendure_mqtt_migration_review\n"
        "review = zendure_mqtt_migration_review({'devices': []})\n"
        "print(json.dumps({'needs_migration': review['needs_migration'],"
        " 'final_valid': review['final_valid']}))\n"
    )
    result = subprocess.run(
        [sys.executable, "-B", "-c", script],
        cwd=app_root,
        capture_output=True,
        text=True,
        env=env,
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout.strip())
    assert payload == {"needs_migration": False, "final_valid": True}


def test_dockerfile_copies_the_migration_module():
    copies = dict((dest, src) for src, dest in _dockerfile_app_copies())
    assert "ems/zendure_mqtt/migration.py" in copies


def test_mirrored_image_provisions_cloud_runtime_credentials(tmp_path):
    """Apply-time cloud credential provisioning must work inside the image.

    ``provision_runtime_credentials`` lazily imports ``ems.mqtt_credentials``
    to verify the persisted record resolves; a missing COPY would make every
    cloud-device apply fail with ImportError inside the Admin container.
    """

    app_root = tmp_path / "app"
    app_root.mkdir()
    _mirror_image_files(app_root)

    config_dir = tmp_path / "install" / "config"
    config_dir.mkdir(parents=True)

    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    script = (
        "import json\n"
        "from admin.credential_store import CredentialStore\n"
        "from admin.zendure_cloud_mqtt import ZendureCloudDiscovery\n"
        "class _Store:\n"
        "    def load_token(self):\n"
        "        return 'api-key'\n"
        "def _fetch(_token, _timeout):\n"
        "    return {'devices': [], 'mqtt': {'host': 'h', 'port': 8883,"
        " 'username': 'u', 'password': 'p', 'client_id': 'c'},"
        " 'app_key': 'a'}\n"
        "discovery = ZendureCloudDiscovery(_Store(), device_list_fetcher=_fetch,"
        " listener_factory=lambda c: None)\n"
        f"store = CredentialStore(config_dir={str(config_dir)!r})\n"
        "created = discovery.provision_runtime_credentials(store)\n"
        "print(json.dumps(created))\n"
    )
    result = subprocess.run(
        [sys.executable, "-B", "-c", script],
        cwd=app_root,
        capture_output=True,
        text=True,
        env=env,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout.strip()) == ["zendure-cloud"]
