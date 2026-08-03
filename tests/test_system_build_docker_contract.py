# SPDX-License-Identifier: AGPL-3.0-or-later
"""Real Docker contract for one runnable local Admin/EMS System Build pair."""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
ADMIN_DOCKERFILE = ROOT / "deploy" / "admin" / "Dockerfile"
ADMIN_COMPOSE = ROOT / "deploy" / "admin" / "docker-compose.yml"
START_ADMIN = ROOT / "deploy" / "admin" / "start-admin-setup.sh"


def _run(*argv, env=None, timeout=900):
    return subprocess.run(
        list(argv),
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
        timeout=timeout,
    )


def _docker_available():
    return bool(shutil.which("docker")) and _run("docker", "info", timeout=15).returncode == 0


pytestmark = [
    pytest.mark.system_build,
    pytest.mark.e2e,
    pytest.mark.docker,
    pytest.mark.skipif(
        not _docker_available(),
        reason="Docker daemon is unavailable; paired System Build images were not built",
    ),
]


def _git(*args):
    result = _run("git", *args, timeout=30)
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def _local_identity():
    revision = _git("rev-parse", "HEAD")
    dirty = bool(_git("status", "--porcelain"))
    build_id = f"local-{revision[:7]}" + ("-dirty" if dirty else "")
    return {
        "system_tag": "local",
        "channel": "development",
        "revision": revision,
        "build_id": build_id,
        "release_tag": "local",
        "dirty": "true" if dirty else "false",
    }


def _assert_command(result, description):
    assert result.returncode == 0, (
        f"{description} failed\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )


@pytest.fixture(scope="module")
def built_system_pair():
    identity = _local_identity()
    suffix = uuid.uuid4().hex[:10]
    admin_image = f"ems-solarflow-admin:contract-{suffix}"
    ems_image = f"ems-solarflow-control:contract-{suffix}"

    admin_build = _run(
        "docker", "build",
        "-f", str(ADMIN_DOCKERFILE),
        "--build-arg", f"EMS_SYSTEM_TAG={identity['system_tag']}",
        "--build-arg", f"EMS_CHANNEL={identity['channel']}",
        "--build-arg", f"EMS_REVISION={identity['revision']}",
        "--build-arg", f"EMS_BUILD_ID={identity['build_id']}",
        "--build-arg", f"EMS_RELEASE_TAG={identity['release_tag']}",
        "-t", admin_image,
        ".",
    )
    _assert_command(admin_build, "real Admin image build")

    ems_build = _run(
        "docker", "build",
        "-f", str(ROOT / "Dockerfile"),
        "--build-arg", f"EMS_RELEASE_TAG={identity['release_tag']}",
        "--build-arg", f"EMS_GIT_COMMIT={identity['revision']}",
        "--build-arg", f"EMS_GIT_COMMIT_SHORT={identity['revision'][:12]}",
        "--build-arg", "EMS_GIT_BRANCH=local",
        "--build-arg", f"EMS_GIT_DIRTY={identity['dirty']}",
        "--build-arg", f"EMS_BUILD_ID={identity['build_id']}",
        "--build-arg", "EMS_BUILD_SERIAL=0",
        "--build-arg", f"EMS_CHANNEL={identity['channel']}",
        "-t", ems_image,
        ".",
    )
    _assert_command(ems_build, "real EMS image build")

    try:
        yield {**identity, "admin_image": admin_image, "ems_image": ems_image}
    finally:
        _run("docker", "image", "rm", "-f", admin_image, ems_image, timeout=120)


def _labels(image):
    result = _run("docker", "image", "inspect", image)
    _assert_command(result, f"inspect {image}")
    data = json.loads(result.stdout)
    return data[0]["Config"].get("Labels") or {}


def _container_json(image, path):
    result = _run("docker", "run", "--rm", image, "cat", path)
    _assert_command(result, f"read {path} from {image}")
    return json.loads(result.stdout)


def test_both_real_images_expose_the_same_oci_system_identity(built_system_pair):
    pair = built_system_pair
    expected = {
        "org.opencontainers.image.revision": pair["revision"],
        "de.basecubedev.ems.build_id": pair["build_id"],
        "de.basecubedev.ems.channel": pair["channel"],
        "de.basecubedev.ems.release_tag": pair["release_tag"],
    }
    for image in (pair["admin_image"], pair["ems_image"]):
        labels = _labels(image)
        for name, value in expected.items():
            assert labels.get(name) == value, f"{image} label {name} disagrees"


def test_admin_embeds_and_verifies_all_release_resources(built_system_pair):
    pair = built_system_pair
    system_build = _container_json(
        pair["admin_image"], "/app/release-resources/system-build.json"
    )
    manifest = _container_json(
        pair["admin_image"], "/app/release-resources/resource-manifest.json"
    )
    assert system_build["system_tag"] == pair["system_tag"]
    assert system_build["channel"] == pair["channel"]
    assert system_build["revision"] == pair["revision"]
    assert system_build["build_id"] == pair["build_id"]
    assert system_build["release_tag"] == pair["release_tag"]
    expected_identity = {
        "system_tag": pair["system_tag"],
        "channel": pair["channel"],
        "revision": pair["revision"],
        "build_id": pair["build_id"],
        "release_tag": pair["release_tag"],
        "admin_image": f"ghcr.io/basecubedev/ems-solarflow-admin:{pair['system_tag']}",
        "ems_image": (
            "ghcr.io/basecubedev/ems-solarflow-api-control:"
            f"{pair['system_tag']}"
        ),
    }
    for field, value in expected_identity.items():
        assert system_build[field] == value
        assert manifest[field] == value
    for required in (
        "config.template.json",
        "docker-compose.example.yml",
        "install-docker.sh",
        "install-docker.ps1",
    ):
        assert required in manifest["files"]

    verification = _run(
        "docker", "run", "--rm", pair["admin_image"],
        "python", "-c",
        (
            "from admin.embedded_resources import EmbeddedReleaseResources; "
            "manager=type('M',(),{})(); "
            "EmbeddedReleaseResources(release_manager=manager).verify(running_build="
            f"{expected_identity!r})"
        ),
    )
    _assert_command(verification, "in-container embedded resource verification")


def _wait_for_auth_status(port, *, process=None, timeout=90):
    url = f"http://127.0.0.1:{port}/api/admin/auth/status"
    deadline = time.monotonic() + timeout
    last_error = None
    while time.monotonic() < deadline:
        if process is not None and process.poll() is not None:
            raise AssertionError(f"Admin launcher exited early with {process.returncode}")
        try:
            with urllib.request.urlopen(url, timeout=2) as response:
                if response.status == 200:
                    return json.loads(response.read().decode("utf-8"))
        except (OSError, ValueError, urllib.error.URLError) as exc:
            last_error = exc
        time.sleep(0.5)
    raise AssertionError(f"Admin auth status did not respond at {url}: {last_error}")


def test_real_admin_container_starts_and_serves_health_status(built_system_pair):
    pair = built_system_pair
    name = "ems-admin-contract-" + uuid.uuid4().hex[:10]
    started = _run(
        "docker", "run", "-d", "--name", name,
        "-p", "127.0.0.1::8090",
        pair["admin_image"],
    )
    _assert_command(started, "Admin container start")
    try:
        port_result = _run(
            "docker", "inspect", "--format",
            '{{(index (index .NetworkSettings.Ports "8090/tcp") 0).HostPort}}',
            name,
        )
        _assert_command(port_result, "Admin published-port inspection")
        payload = _wait_for_auth_status(int(port_result.stdout.strip()))
        assert "auth_configured" in payload
    finally:
        _run("docker", "rm", "-f", name, timeout=60)


def _free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def test_start_admin_setup_builds_valid_local_identity_and_cleans_up(tmp_path):
    identity = _local_identity()
    project = "ems-admin-script-" + uuid.uuid4().hex[:10]
    port = _free_port()
    admin_data = tmp_path / "admin-data"
    env = {
        **os.environ,
        "COMPOSE_PROJECT_NAME": project,
        "EMS_ADMIN_BIND": "127.0.0.1",
        "EMS_ADMIN_PORT": str(port),
        "EMS_ADMIN_DATA_DIR": str(admin_data),
        "PUID": str(os.getuid()),
        "PGID": str(os.getgid()),
    }
    log_path = tmp_path / "start-admin.log"
    process = None
    try:
        with log_path.open("w+", encoding="utf-8") as log:
            process = subprocess.Popen(
                [str(START_ADMIN)],
                cwd=ROOT,
                env=env,
                text=True,
                stdout=log,
                stderr=subprocess.STDOUT,
            )
            try:
                payload = _wait_for_auth_status(port, process=process, timeout=600)
            except AssertionError as exc:
                log.flush()
                log.seek(0)
                raise AssertionError(f"{exc}\nlauncher log:\n{log.read()}") from exc
            assert "auth_configured" in payload

        ps = _run(
            "docker", "compose", "-f", str(ADMIN_COMPOSE), "ps", "-q",
            env=env,
        )
        _assert_command(ps, "locate launcher-created Admin container")
        container = ps.stdout.strip()
        assert container
        inspect = _run("docker", "inspect", container)
        _assert_command(inspect, "inspect launcher-created Admin container")
        info = json.loads(inspect.stdout)[0]
        mounts = {(item["Source"], item["Destination"]) for item in info["Mounts"]}
        assert ("/var/run/docker.sock", "/var/run/docker.sock") in mounts
        assert (str(admin_data), str(admin_data)) in mounts

        labels = (info["Config"].get("Labels") or {})
        assert labels["org.opencontainers.image.revision"] == identity["revision"]
        assert labels["de.basecubedev.ems.build_id"] == identity["build_id"]
        assert labels["de.basecubedev.ems.channel"] == identity["channel"]
        assert labels["de.basecubedev.ems.release_tag"] == identity["release_tag"]
    finally:
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=30)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)
        _run(
            "docker", "compose", "-f", str(ADMIN_COMPOSE), "down",
            "--remove-orphans", env=env, timeout=120,
        )


def test_start_script_smoke_uses_configurable_data_path_and_port():
    script = START_ADMIN.read_text(encoding="utf-8")
    compose = ADMIN_COMPOSE.read_text(encoding="utf-8")
    assert 'EMS_ADMIN_DATA_DIR:-' in script
    assert "EMS_ADMIN_PORT" in compose
    assert "EMS_ADMIN_BIND" in compose
