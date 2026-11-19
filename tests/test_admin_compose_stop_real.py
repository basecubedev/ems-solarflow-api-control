# SPDX-FileCopyrightText: 2026 basecubedev
# SPDX-License-Identifier: AGPL-3.0-or-later
"""``DockerCompose.stop`` against a real ``docker compose``.

Compose v2 names the grace period ``--timeout`` (``-t``); ``--time`` is the
plain ``docker stop`` flag and Compose refuses it as an unknown flag. Every
fake accepted it, so switching Analytics or the built-in broker off reported
"Could not stop the optional feature container" on a real installation.
"""

import os
import shutil
import subprocess
import uuid

import pytest

from admin.deployment import DockerCompose
from docker_e2e_utils import AMBIENT_COMPOSE_VARS

IMAGE = "influxdb:2.7.12"


def _docker_ready():
    if shutil.which("docker") is None:
        return False
    if subprocess.run(["docker", "info"], capture_output=True).returncode != 0:
        return False
    return subprocess.run(["docker", "compose", "version"], capture_output=True).returncode == 0


pytestmark = [
    pytest.mark.e2e,
    pytest.mark.docker,
    pytest.mark.maintenance,
    pytest.mark.skipif(not hasattr(os, "getuid"), reason="POSIX-only Docker e2e"),
    pytest.mark.skipif(not _docker_ready(), reason="Docker is not available"),
]


def test_a_profiled_service_stops_and_keeps_its_container(tmp_path, monkeypatch):
    for key in AMBIENT_COMPOSE_VARS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("COMPOSE_PROJECT_NAME", f"ems-stop-e2e-{uuid.uuid4().hex[:10]}")
    name = f"ems-stop-e2e-{uuid.uuid4().hex[:10]}"
    (tmp_path / "docker-compose.yml").write_text(
        f"services:\n  influxdb:\n    image: {IMAGE}\n    container_name: {name}\n    profiles: [with-analytics]\n",
        encoding="utf-8",
    )
    assert subprocess.run(["docker", "pull", "-q", IMAGE], capture_output=True, timeout=600).returncode == 0
    compose = DockerCompose()
    try:
        compose.up(tmp_path, profiles=("with-analytics",), services=("influxdb",))
        compose.stop(tmp_path, services=("influxdb",), profiles=("with-analytics",))
        state = subprocess.run(
            ["docker", "inspect", "-f", "{{.State.Status}}", name], capture_output=True, text=True, timeout=30
        )
        assert state.stdout.strip() == "exited"
    finally:
        subprocess.run(["docker", "compose", "--profile", "with-analytics", "down", "-v", "--remove-orphans"],
                       cwd=tmp_path, capture_output=True, timeout=120)
        subprocess.run(["docker", "rm", "-f", "-v", name], capture_output=True, timeout=60)
