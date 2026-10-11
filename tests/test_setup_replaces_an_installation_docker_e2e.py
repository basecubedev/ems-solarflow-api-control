# SPDX-License-Identifier: AGPL-3.0-or-later
"""A fresh Setup over a running installation, on a real Docker daemon.

An EMS built from this checkout runs an existing installation: its own config,
a second device, a hand-kept key, and a runtime state in which the operator
switched the EMS and a device off. A Guided Setup then generates a config from
the release template and deploys it through the productive deployment service:
the real release installer, the permission repair in a container, ``docker
compose`` and the dashboard probe. The result has to be the config Setup
generated and nothing of the old one but where the password file lives, an EMS
recreated onto it, and a runtime state that EMS rebuilt from the new config
after keeping the old one as a backup.

One thing is not the product's: ``docker pull`` is skipped for the image built
here, which no registry carries. The containers carry the standard install's
fixed names, so the test skips on a host where containers of that name exist.
"""

import hashlib
import json
import os
import shutil
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from admin.config_export import config_payload_bytes
from admin.config_preview import ConfigPreviewGenerator
from admin.deployment import BootstrapInstaller, DeploymentService, DockerCli
from admin.guided_setup_workflow import GuidedSetupWorkflowStore
from admin.install_context import detect_install_context
from admin.setup_workflow import SetupWorkflowArtifacts

pytestmark = [
    pytest.mark.admin,
    pytest.mark.setup,
    pytest.mark.e2e,
    pytest.mark.docker,
    pytest.mark.slow,
]

ROOT = Path(__file__).resolve().parents[1]
TAG = "e2e-fresh-setup"
IMAGE = f"ghcr.io/basecubedev/ems-solarflow-api-control:{TAG}"
SERIAL = "E2ESN0001"
CONTAINERS = ("ems-solarflow-api-control", "ems-influxdb")

OLD_CONFIG = {
    "system": {
        "enabled": True,
        "dry_run": True,
        "max_total_power": 2400,
        "runtime_state_path": "data/runtime-state.json",
    },
    "devices": [
        {"name": "WR1", "ip": "192.0.2.10", "sn": SERIAL, "max_power": 600},
        {"name": "OLD2", "ip": "192.0.2.11", "sn": "E2ESN0002", "max_power": 600},
    ],
    "grid_meter": {"type": "shelly", "ip": "192.0.2.30"},
    "dashboard": {"enabled": True, "port": 8080, "auth_file": "config/admin-auth.json"},
    "operator_only": {"kept": "by hand"},
}

OLD_RUNTIME = {
    "system": {"enabled": False, "max_total_power": 2400},
    "devices": {"WR1": {"identity": SERIAL, "enabled": False, "max_power": 600}},
}

DRAFT = [
    {
        "config_name": "INV_1",
        "display_name": "SolarFlow 800 Pro",
        "role": "inverter",
        "enabled": True,
        "ip": "192.0.2.20",
        "serial_number": SERIAL,
    },
    {
        "config_name": "grid_meter",
        "display_name": "Shelly Pro 3EM",
        "role": "grid_meter",
        "enabled": True,
        "ip": "192.0.2.30",
        "api_family": "shelly_gen2",
    },
]


def _docker_ready():
    if not shutil.which("docker"):
        return False
    info = subprocess.run(["docker", "info"], capture_output=True, text=True)
    compose = subprocess.run(["docker", "compose", "version"], capture_output=True, text=True)
    return info.returncode == 0 and compose.returncode == 0


pytestmark.append(
    pytest.mark.skipif(not _docker_ready(), reason="needs a Docker daemon with Compose v2")
)


@pytest.fixture(scope="module")
def built_image():
    build = subprocess.run(
        ["docker", "build", "-t", IMAGE, str(ROOT)], capture_output=True, text=True
    )
    if build.returncode != 0:
        pytest.fail("docker build failed:\n" + build.stderr[-4000:])
    return IMAGE


class _LocallyBuiltImage(DockerCli):
    """The real Docker CLI, except that an image built here is not pulled."""

    def pull(self, image, on_progress=None):
        present = subprocess.run(
            ["docker", "image", "inspect", image], capture_output=True, text=True
        )
        if present.returncode == 0:
            if on_progress is not None:
                on_progress(100, "present locally")
            return
        super().pull(image, on_progress)


def _existing_containers():
    names = subprocess.run(
        ["docker", "ps", "-a", "--format", "{{.Names}}"], capture_output=True, text=True
    ).stdout.split()
    return [name for name in CONTAINERS if name in names]


def _compose(root, *args):
    return subprocess.run(
        ["docker", "compose", *args], cwd=root, capture_output=True, text=True
    )


def _container_id(name):
    inspect = subprocess.run(
        ["docker", "inspect", "-f", "{{.Id}}", name], capture_output=True, text=True
    )
    return inspect.stdout.strip() if inspect.returncode == 0 else None


def _logs(name, since=None):
    command = ["docker", "logs", name] if since is None else ["docker", "logs", "--since", since, name]
    logs = subprocess.run(command, capture_output=True, text=True)
    return logs.stdout + logs.stderr


def _wait_for(predicate, *, timeout, message):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        threading.Event().wait(1.0)
    raise AssertionError(message)


def _wait_for_job(lookup, job_id, timeout=600, container=None):
    deadline = time.monotonic() + timeout
    job = None
    while time.monotonic() < deadline:
        job = lookup(job_id)
        if job is not None and job["status"] != "running":
            return job
        threading.Event().wait(0.5)
    logs = _logs(container)[-3000:] if container else ""
    raise AssertionError(f"job {job_id} never settled: {json.dumps(job, default=str)[-3000:]}\n{logs}")


def _release(data_dir):
    """The release resources a prepared release carries, from this checkout's tracked files."""

    resources = data_dir / "releases" / TAG
    tracked = subprocess.run(
        ["git", "ls-files", "deploy/docker", "docker-compose.example.yml",
         "install-docker.sh", "install-docker.ps1"],
        cwd=ROOT, capture_output=True, text=True, check=True,
    ).stdout.split()
    for relative in tracked:
        (resources / relative).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / relative, resources / relative)
    shutil.copy2(ROOT / "config" / "config.template.json", resources / "config.template.json")
    template = json.loads((resources / "config.template.json").read_text(encoding="utf-8"))
    return SimpleNamespace(
        releases_dir=data_dir / "releases",
        data_dir=data_dir,
        config_template=lambda: {"tag": TAG, "template": template, "docker_image": IMAGE},
    )


def _influx_env_file(root):
    """The env file the compose file names, empty as it is without Analytics.

    Some Compose builds (Debian's 2.26.1) refuse a missing ``env_file`` even
    marked ``required: false``; an installation with Analytics always has it.
    """

    env_file = root / "config" / "influxdb.env"
    if not env_file.exists():
        env_file.write_text("", encoding="utf-8")


def _deploy(store, data_dir, release, root, env):
    """One Guided Setup run through the deployment service: generate, prepare, start."""

    workflow_id, target, payload = _generated_setup_config(store, data_dir, release, root)
    service = DeploymentService(
        release,
        SimpleNamespace(target_path=target),
        workspace_dir=root,
        admin_data_dir=data_dir,
        docker=_LocallyBuiltImage(),
        installer=BootstrapInstaller(),
        runtime_env=env,
        setup_workflows=store,
    )
    prepared = service.prepare(overwrite=True, workflow_id=workflow_id)
    job = _wait_for_job(service.job, prepared["job"]["job_id"])
    assert job["status"] == "succeeded", json.dumps(job, default=str)
    _influx_env_file(root)
    started = service.start(workflow_id=workflow_id)
    assert started.get("ok") is True, started
    job = _wait_for_job(
        service.start_job, started["job"]["job_id"], timeout=300, container=CONTAINERS[0]
    )
    assert job["status"] == "succeeded", json.dumps(job, default=str)
    return payload


def _running_installation(store, data_dir, release, root, env):
    """The installation the Setup replaces, as the Admin installed and its operator kept it.

    A first Setup deploys it, so the second writes the very compose file and
    environment this one runs on and Compose sees nothing to recreate. Its
    config and runtime state are then what years of use leave behind: a second
    device, a hand-kept key, and the EMS and a device switched off.
    """

    _deploy(store, data_dir, release, root, env)
    name = CONTAINERS[0]
    stopped = subprocess.run(["docker", "stop", name], capture_output=True, text=True)
    assert stopped.returncode == 0, stopped.stderr
    (root / "config" / "config.json").write_text(json.dumps(OLD_CONFIG), encoding="utf-8")
    (root / "data" / "runtime-state.json").write_text(json.dumps(OLD_RUNTIME), encoding="utf-8")
    since = datetime.now(timezone.utc).isoformat()
    restarted = subprocess.run(["docker", "start", name], capture_output=True, text=True)
    assert restarted.returncode == 0, restarted.stderr
    _wait_for(
        lambda: "event=runtime_state_loaded" in _logs(name, since=since),
        timeout=120,
        message="the old EMS never loaded its runtime state:\n" + _logs(name)[-3000:],
    )
    return name


def _generated_setup_config(store, data_dir, release, root):
    """What Guided Setup's Config step generates and binds to its workflow."""

    generator = ConfigPreviewGenerator(
        release, install_context_provider=lambda: detect_install_context(base_dir=root)
    )
    preview = generator.generate(DRAFT, 1, {"system.dry_run": True})
    assert preview["ready"] is True, preview["validation"]
    payload = config_payload_bytes(preview["config"])
    live = root / "config" / "config.json"
    base = (
        {"expected_revision": hashlib.sha256(live.read_bytes()).hexdigest(), "expect_absent": False}
        if live.exists()
        else {"expected_revision": None, "expect_absent": True}
    )
    digest = hashlib.sha256(payload).hexdigest()
    workflow_id = store.ensure_active()["workflow_id"]
    record = store.record_preview(
        workflow_id,
        draft_fingerprint="sha256:" + "0" * 64,
        base_config_revision=base,
        prepared_config_sha256=digest,
    )
    preview_id = record["preview"]["preview_id"]
    store.bind_generated_artifacts(workflow_id, preview_id=preview_id)
    artifacts = SetupWorkflowArtifacts(data_dir, workflow_id=workflow_id)
    target = artifacts.generated_config_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(payload)
    artifacts.record_generated(
        workflow_id=workflow_id,
        preview_id=preview_id,
        draft_fingerprint="sha256:" + "0" * 64,
        base_config_revision=base,
        prepared_config_sha256=digest,
    )
    return workflow_id, target, payload


def test_a_fresh_setup_replaces_a_running_installation_end_to_end(
    built_image, tmp_path, monkeypatch
):
    root = tmp_path / "install"
    data_dir = tmp_path / "admin-data"
    (data_dir / "state").mkdir(parents=True)
    monkeypatch.setenv("EMS_INSTALL_DIR", str(root))
    monkeypatch.delenv("COMPOSE_PROFILES", raising=False)
    env = {
        "PUID": str(os.getuid()),
        "PGID": str(os.getgid()),
    }
    present = _existing_containers()
    if present:
        pytest.skip(f"containers of the standard install exist here: {', '.join(present)}")
    release = _release(data_dir)
    try:
        store = GuidedSetupWorkflowStore(data_dir)
        container = _running_installation(store, data_dir, release, root, env)
        before = _container_id(container)
        since = datetime.now(timezone.utc).isoformat()

        payload = _deploy(store, data_dir, release, root, env)

        installed = (root / "config" / "config.json").read_bytes()
        assert installed == payload
        config = json.loads(installed)
        assert "operator_only" not in config
        assert [device["name"] for device in config["devices"]] == ["INV_1"]
        assert config["devices"][0]["sn"] == SERIAL
        assert config["dashboard"]["auth_file"] == "config/admin-auth.json"

        assert _container_id(container) not in (None, before)
        _wait_for(
            lambda: "event=runtime_state_reset_by_setup" in _logs(container, since=since),
            timeout=120,
            message="the new EMS never reset its runtime state:\n" + _logs(container)[-3000:],
        )
        runtime = json.loads((root / "data" / "runtime-state.json").read_text(encoding="utf-8"))
        assert runtime["system"]["enabled"] is True
        assert set(runtime["devices"]) == {"INV_1"}
        assert runtime["devices"]["INV_1"]["enabled"] is True
        assert runtime["devices"]["INV_1"]["identity"] == SERIAL
        (backup,) = (root / "data").glob("runtime-state.json.before-setup-*")
        kept = json.loads(backup.read_text(encoding="utf-8"))
        assert kept["system"]["enabled"] is False
        assert kept["system"]["max_total_power"] == 2400
        assert kept["devices"]["WR1"]["enabled"] is False
        assert set(kept["devices"]) == {"WR1", "OLD2"}
        assert not (root / "data" / "runtime-state.json.reset-requested").exists()
    finally:
        _compose(root, "down", "--remove-orphans")
        for name in CONTAINERS:
            subprocess.run(["docker", "rm", "-f", name], capture_output=True, text=True)
