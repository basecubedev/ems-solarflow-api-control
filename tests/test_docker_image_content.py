# SPDX-License-Identifier: AGPL-3.0-or-later
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
IMAGE = "ems-solarflow-api-control:pytest-content"


def docker_available():
    return shutil.which("docker") and subprocess.run(
        ["docker", "info"],
        text=True,
        capture_output=True,
        check=False,
    ).returncode == 0


pytestmark = [
    pytest.mark.e2e,
    pytest.mark.docker,
    pytest.mark.skipif(
        not docker_available(),
        reason="Docker daemon is not available",
    ),
]


def run(*args):
    return subprocess.run(
        [*args],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )


def test_docker_image_contains_runtime_files_and_excludes_dev_content():
    build = run("docker", "build", "-t", IMAGE, ".")
    assert build.returncode == 0, build.stderr

    required = (
        "/app/emsctl.py",
        "/app/config.template.json",
        "/app/docker-entrypoint.sh",
        "/app/docs/docker.md",
        "/app/ems/config.py",
        "/app/dashboard/server.py",
        # Runtime dependency of ems/history; without it Analytics influx
        # sync/status fail in-container with ModuleNotFoundError: scripts.
        "/app/scripts/influx_utils.py",
    )
    forbidden = (
        "/app/.git",
        "/app/tests",
        "/app/__pycache__",
        "/app/backup",
        "/app/.venv",
        "/app/deploy/docker/influxdb.env",
        # Dev-only scripts must not be shipped (only influx_utils.py is).
        "/app/scripts/capture_runtime_to_influx.py",
        "/app/scripts/docker_compose_smoke.sh",
    )
    script = " && ".join(
        [*(f"test -f {path}" for path in required)]
        + [*(f"test ! -e {path}" for path in forbidden)]
        + ['test -z "$(find /app/data -mindepth 1 -maxdepth 1 -print -quit)"']
    )
    content = run("docker", "run", "--rm", IMAGE, "sh", "-c", script)
    assert content.returncode == 0, content.stderr

    help_result = run("docker", "run", "--rm", IMAGE, "python3", "emsctl.py", "--help")
    assert help_result.returncode == 0, help_result.stderr
    assert "EMS runtime control CLI" in help_result.stdout

    analytics_import = run(
        "docker", "run", "--rm", IMAGE,
        "python3", "-c", "import ems.history.influx_client",
    )
    assert analytics_import.returncode == 0, analytics_import.stderr

    # The official influx CLI must be present for in-container bundled InfluxDB
    # backups, but the Docker CLI must not (no Docker-in-Docker / socket reliance).
    influx_cli = run("docker", "run", "--rm", IMAGE, "influx", "version")
    assert influx_cli.returncode == 0, influx_cli.stderr

    no_docker = run("docker", "run", "--rm", IMAGE, "sh", "-c", "command -v docker")
    assert no_docker.returncode != 0, "Docker CLI must not be present in the image"
