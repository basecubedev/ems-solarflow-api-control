# SPDX-FileCopyrightText: 2026 basecubedev
# SPDX-License-Identifier: AGPL-3.0-or-later
"""``align_bundled_influx`` against a real ``docker compose``.

The update rewrites InfluxDB's published port in docker-compose.yml, and only a
recreated container takes it. Whether one is due is Compose's own fingerprint,
the config hash it stamps on every container it creates, so these tests hold
that the fingerprint read from ``docker compose ps`` and the one ``docker
compose config --hash`` gives agree exactly when nothing changed, on the Compose
this host runs.
"""

import os
import socket
import subprocess
import uuid
from types import SimpleNamespace

import pytest

from admin.container_actions import (
    ALIGN_IMAGE_UNCONFIRMED,
    ALIGN_NOT_RUNNING,
    ALIGN_RECREATED,
    ALIGN_UNCHANGED,
    align_bundled_influx,
)
from admin.deployment import DockerCli, DockerCompose
from docker_e2e_utils import AMBIENT_COMPOSE_VARS, docker_compose_ready

IMAGE = "influxdb:2.7.12"


pytestmark = [
    pytest.mark.e2e,
    pytest.mark.docker,
    pytest.mark.maintenance,
    pytest.mark.skipif(not hasattr(os, "getuid"), reason="POSIX-only Docker e2e"),
    pytest.mark.skipif(not docker_compose_ready(), reason="Docker is not available"),
]


def _free_port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _write(path, image, name, publish):
    path.write_text(
        "services:\n"
        "  influxdb:\n"
        f"    image: {image}\n"
        f"    container_name: {name}\n"
        "    profiles: [with-analytics]\n"
        '    entrypoint: ["sleep", "3600"]\n'
        "    ports:\n"
        f'      - "{publish}:8086"\n',
        encoding="utf-8",
    )


def _inspect(name, template):
    return subprocess.run(
        ["docker", "inspect", "-f", template, name], capture_output=True, text=True, timeout=30
    ).stdout.strip()


def test_a_running_influxdb_takes_a_changed_entry_and_only_then(tmp_path, monkeypatch):
    for key in AMBIENT_COMPOSE_VARS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("COMPOSE_PROJECT_NAME", f"ems-align-e2e-{uuid.uuid4().hex[:10]}")
    name = f"ems-align-e2e-{uuid.uuid4().hex[:10]}"
    port = _free_port()
    compose_path = tmp_path / "docker-compose.yml"
    context = SimpleNamespace(install_root=str(tmp_path), compose_path=str(compose_path))
    compose, docker = DockerCompose(), DockerCli()
    assert subprocess.run(["docker", "pull", "-q", IMAGE], capture_output=True, timeout=600).returncode == 0
    _write(compose_path, IMAGE, name, port)
    try:
        compose.up(tmp_path, profiles=("with-analytics",), services=("influxdb",))
        _write(compose_path, IMAGE, name, f"127.0.0.1:{port}")

        assert align_bundled_influx(context, compose, docker) == ALIGN_RECREATED
        assert _inspect(name, '{{(index (index .HostConfig.PortBindings "8086/tcp") 0).HostIp}}') == "127.0.0.1"
        recreated = _inspect(name, "{{.Id}}")

        assert align_bundled_influx(context, compose, docker) == ALIGN_UNCHANGED

        _write(compose_path, "influxdb:2.7.11", name, port)
        assert align_bundled_influx(context, compose, docker) == ALIGN_IMAGE_UNCONFIRMED
        assert _inspect(name, "{{.Id}}") == recreated

        compose.stop(tmp_path, services=("influxdb",), profiles=("with-analytics",))
        assert align_bundled_influx(context, compose, docker) == ALIGN_NOT_RUNNING
        assert _inspect(name, "{{.State.Status}}") == "exited"
    finally:
        subprocess.run(["docker", "compose", "--profile", "with-analytics", "down", "-v", "--remove-orphans"],
                       cwd=tmp_path, capture_output=True, timeout=120)
        subprocess.run(["docker", "rm", "-f", "-v", name], capture_output=True, timeout=60)
