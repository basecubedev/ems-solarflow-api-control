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
    )
    forbidden = (
        "/app/.git",
        "/app/tests",
        "/app/__pycache__",
        "/app/backup",
        "/app/.venv",
        "/app/deploy/docker/influxdb.env",
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
