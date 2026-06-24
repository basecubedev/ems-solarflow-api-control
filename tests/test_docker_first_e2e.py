# SPDX-License-Identifier: AGPL-3.0-or-later
"""Real Docker end-to-end tests for the Docker-first end-user setup.

These run the actual `install-docker.sh` flow against a locally built image and
a real Docker daemon, covering the two documented quickstarts:

  A. EMS only        -> sh install-docker.sh --no-start; docker compose up -d
  B. EMS + Analytics -> sh install-docker.sh --analytics --no-start;
                        docker compose --profile with-analytics up -d

They skip cleanly without Docker and always clean up the test project. A local
override pins the freshly built image and drops published host ports so the
tests do not collide with anything already bound on 8080/8086.
"""
import json
import os
import re
import subprocess
import time
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
INSTALL_SH = ROOT / "install-docker.sh"
IMAGE_TAG = "ems-solarflow-api-control:first-e2e"


def docker_available():
    from shutil import which

    if not which("docker"):
        return False
    if subprocess.run(["docker", "info"], capture_output=True).returncode != 0:
        return False
    return subprocess.run(["docker", "compose", "version"], capture_output=True).returncode == 0


pytestmark = [
    pytest.mark.docker,
    pytest.mark.skipif(not hasattr(os, "getuid"), reason="POSIX-only Docker e2e"),
    pytest.mark.skipif(not docker_available(), reason="Docker is not available"),
]


_OVERRIDE = """\
services:
  ems:
    image: {image}
    pull_policy: never
    container_name: ${{COMPOSE_PROJECT_NAME}}-ems
    ports: !override []
  influxdb:
    image: influxdb:2.7
    pull_policy: missing
    container_name: ${{COMPOSE_PROJECT_NAME}}-influxdb
    ports: !override []
"""


@pytest.fixture(scope="session")
def built_image():
    build = subprocess.run(
        ["docker", "build", "-t", IMAGE_TAG, str(ROOT)],
        capture_output=True,
        text=True,
    )
    if build.returncode != 0:
        pytest.fail("docker build failed:\n" + build.stderr)
    return IMAGE_TAG


_SECRET_KEY = re.compile(r"(?i)(password|token|secret)")
_SENSITIVE_CONFIG_KEYS = {"sn", "ip", "url", "host_url", "token"}


def _redact_env(text):
    lines = []
    for line in text.splitlines():
        key = line.split("=", 1)[0]
        lines.append(f"{key}=***REDACTED***" if _SECRET_KEY.search(key) else line)
    return "\n".join(lines)


def _redact_config(text):
    try:
        data = json.loads(text)
    except ValueError:
        return "***unparseable config redacted***"

    def walk(obj):
        if isinstance(obj, dict):
            return {
                k: ("***REDACTED***" if _SECRET_KEY.search(k) or k in _SENSITIVE_CONFIG_KEYS else walk(v))
                for k, v in obj.items()
            }
        if isinstance(obj, list):
            return [walk(v) for v in obj]
        return obj

    return json.dumps(walk(data), indent=2)


class Project:
    def __init__(self, path, image):
        self.path = path
        self.name = "ems-first-e2e-" + uuid.uuid4().hex[:10]
        self.env = {
            **os.environ,
            "COMPOSE_PROJECT_NAME": self.name,
            "PUID": str(os.getuid()),
            "PGID": str(os.getgid()),
        }
        (path / "docker-compose.override.yml").write_text(_OVERRIDE.format(image=image))

    def install(self, *args):
        return subprocess.run(
            ["sh", str(INSTALL_SH), *args],
            cwd=self.path,
            env=self.env,
            capture_output=True,
            text=True,
        )

    def compose(self, *args, profile=False):
        prefix = ["docker", "compose"]
        if profile:
            prefix += ["--profile", "with-analytics"]
        return subprocess.run(
            [*prefix, *args],
            cwd=self.path,
            env=self.env,
            capture_output=True,
            text=True,
        )

    def container_exists(self, suffix):
        return subprocess.run(
            ["docker", "inspect", f"{self.name}-{suffix}"],
            capture_output=True,
        ).returncode == 0

    def down(self):
        self.compose("down", "-v", "--remove-orphans", profile=True)

    def diagnostics(self, failed_command):
        out = [f"FAILED COMMAND: {failed_command}", "", "docker compose ps:"]
        out.append(self.compose("ps", "--all", profile=True).stdout)
        out.append("\ndocker compose logs:")
        out.append(self.compose("logs", "--no-color", profile=True).stdout)
        env_file = self.path / ".env"
        if env_file.exists():
            out.append("\n.env (redacted):\n" + _redact_env(env_file.read_text()))
        config_file = self.path / "config" / "config.json"
        if config_file.exists():
            out.append("\nconfig/config.json (redacted):\n" + _redact_config(config_file.read_text()))
        return "\n".join(out)


def _wait(predicate, timeout=30, interval=1.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


def test_ems_only_quickstart(built_image, tmp_path):
    project = Project(tmp_path, built_image)
    failed = "setup"
    try:
        install = project.install("--no-start")
        assert install.returncode == 0, install.stderr

        assert (project.path / "docker-compose.yml").is_file()
        assert not (project.path / "config" / "influxdb.env").exists()
        assert not (project.path / "data" / "influxdb").exists()
        assert "docker.sock" not in (project.path / "docker-compose.yml").read_text()

        failed = "docker compose up -d"
        up = project.compose("up", "-d")
        assert up.returncode == 0, up.stderr

        config = project.path / "config" / "config.json"
        assert _wait(config.is_file, timeout=30), "container did not seed config.json"

        failed = "docker compose ps"
        ps = project.compose("ps")
        assert f"{project.name}-ems" in ps.stdout
        assert "Up" in ps.stdout

        failed = "docker compose logs ems"
        logs = project.compose("logs", "--no-color", "ems")
        assert "Traceback" not in logs.stdout + logs.stderr

        failed = "config validate (config upgrade --dry-run)"
        validate = project.compose("exec", "-T", "ems", "python3", "emsctl.py", "config", "upgrade", "--dry-run")
        assert validate.returncode == 0, validate.stdout + validate.stderr

        failed = "emsctl status"
        status = project.compose("exec", "-T", "ems", "python3", "emsctl.py", "status")
        assert status.returncode == 0, status.stdout + status.stderr

        # EMS-only must not start or require the bundled InfluxDB.
        assert not project.container_exists("influxdb")
        assert not (project.path / "config" / "influxdb.env").exists()
    except Exception:
        print(project.diagnostics(failed))
        raise
    finally:
        project.down()


def test_analytics_quickstart(built_image, tmp_path):
    project = Project(tmp_path, built_image)
    failed = "setup"
    try:
        install = project.install("--analytics", "--no-start")
        assert install.returncode == 0, install.stdout + install.stderr

        assert (project.path / "config" / "influxdb.env").is_file()
        assert (project.path / "data" / "influxdb").is_dir()
        assert not (project.path / "docker-compose.yml").read_text().count("docker.sock")

        influx = json.loads((project.path / "config" / "config.json").read_text())["influxdb"]
        assert influx["enabled"] is True
        assert influx["mode"] == "bundled"
        assert influx["secret_file"] == "config/influxdb.env"

        failed = "docker compose --profile with-analytics up -d"
        up = project.compose("up", "-d", profile=True)
        assert up.returncode == 0, up.stderr

        assert project.container_exists("ems")
        assert project.container_exists("influxdb")

        # InfluxDB needs a moment to finish first-run setup; influx sync is the
        # documented readiness/verification command, so poll it.
        failed = "influx sync"
        sync = None

        def synced():
            nonlocal sync
            sync = project.compose("exec", "-T", "ems", "python3", "emsctl.py", "influx", "sync")
            return sync.returncode == 0

        assert _wait(synced, timeout=90, interval=3), (sync.stdout + sync.stderr if sync else "no sync output")

        failed = "influx status"
        status = project.compose("exec", "-T", "ems", "python3", "emsctl.py", "influx", "status")
        assert status.returncode == 0, status.stdout + status.stderr
        assert "healthy: yes" in status.stdout
    except Exception:
        print(project.diagnostics(failed))
        raise
    finally:
        project.down()
