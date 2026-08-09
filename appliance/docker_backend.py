# SPDX-License-Identifier: AGPL-3.0-or-later
"""Typed Docker access for the privileged agent.

Docker access is root-equivalent, so it lives behind the agent and never behind
the web process. Every argument here comes from host configuration or from a
value that passed :mod:`appliance.validation`; nothing is interpolated into a
shell.
"""

import json
from dataclasses import dataclass, field

from appliance.commands import CommandError

DAEMON_RUNNING = "running"
DAEMON_STOPPED = "stopped"
DAEMON_UNAVAILABLE = "unavailable"

CONTAINER_MISSING = "missing"
CONTAINER_RUNNING = "running"
CONTAINER_EXITED = "exited"
CONTAINER_CREATED = "created"
CONTAINER_RESTARTING = "restarting"

HEALTH_HEALTHY = "healthy"
HEALTH_UNHEALTHY = "unhealthy"
HEALTH_STARTING = "starting"
HEALTH_NONE = "none"


class DockerError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class ContainerState:
    name: str
    exists: bool = False
    state: str = CONTAINER_MISSING
    health: str = HEALTH_NONE
    container_id: str = ""
    image: str = ""
    image_id: str = ""
    started_at: str = ""
    exit_code: int = None
    restart_count: int = 0
    health_output: str = ""
    ports: tuple = ()

    def to_dict(self):
        return {
            "name": self.name,
            "exists": self.exists,
            "state": self.state,
            "health": self.health,
            "container_id": self.container_id[:12],
            "image": self.image,
            "image_id": self.image_id,
            "started_at": self.started_at,
            "exit_code": self.exit_code,
            "restart_count": self.restart_count,
            "ports": list(self.ports),
        }


@dataclass(frozen=True)
class ImageState:
    reference: str
    exists: bool = False
    digest: str = ""
    image_id: str = ""
    architecture: str = ""
    os: str = ""
    labels: dict = field(default_factory=dict)

    def to_dict(self):
        return {
            "reference": self.reference,
            "exists": self.exists,
            "digest": self.digest,
            "image_id": self.image_id[:19],
            "architecture": self.architecture,
            "os": self.os,
            "labels": dict(self.labels),
        }


class DockerBackend:
    def __init__(self, runner, *, compose_file=None, timeout=120):
        self.runner = runner
        self.compose_file = str(compose_file) if compose_file else ""
        self.timeout = timeout

    # --- daemon ----------------------------------------------------------

    def daemon_state(self):
        if not self.runner.available("docker"):
            return {"state": DAEMON_UNAVAILABLE, "version": "", "error": "docker_not_installed"}
        result = self.runner.run("docker", ["version", "--format", "{{.Server.Version}}"], timeout=15)
        if result.ok:
            return {"state": DAEMON_RUNNING, "version": result.stdout.strip(), "error": ""}
        return {
            "state": DAEMON_STOPPED,
            "version": "",
            "error": "docker_daemon_unreachable",
        }

    def daemon_running(self):
        return self.daemon_state()["state"] == DAEMON_RUNNING

    # --- containers ------------------------------------------------------

    def inspect_container(self, name):
        result = self.runner.run(
            "docker", ["inspect", "--type", "container", "--format", "{{json .}}", name], timeout=30
        )
        if not result.ok:
            return ContainerState(name=name)
        try:
            payload = json.loads(result.stdout.strip() or "{}")
        except ValueError:
            return ContainerState(name=name)
        return _container_state(name, payload)

    def container_logs(self, name, lines):
        result = self.runner.run(
            "docker", ["logs", "--tail", str(int(lines)), name], timeout=self.timeout
        )
        if not result.ok and not result.stdout:
            raise DockerError("container_logs_unavailable", f"cannot read logs of {name}")
        return (result.stdout or "") + (result.stderr or "")

    def start_container(self, name):
        return self.runner.run("docker", ["start", name], timeout=self.timeout)

    def stop_container(self, name, *, seconds=20):
        return self.runner.run("docker", ["stop", "-t", str(int(seconds)), name], timeout=self.timeout)

    def restart_container(self, name):
        return self.runner.run("docker", ["restart", name], timeout=self.timeout)

    def remove_container(self, name):
        return self.runner.run("docker", ["rm", "-f", name], timeout=self.timeout)

    # --- images ----------------------------------------------------------

    def inspect_image(self, reference):
        result = self.runner.run(
            "docker", ["image", "inspect", "--format", "{{json .}}", reference], timeout=60
        )
        if not result.ok:
            return ImageState(reference=reference)
        try:
            payload = json.loads(result.stdout.strip() or "{}")
        except ValueError:
            return ImageState(reference=reference)
        return _image_state(reference, payload)

    def tag_image(self, source, target):
        return self.runner.run("docker", ["tag", source, target], timeout=60)

    def pull_image(self, reference):
        result = self.runner.run("docker", ["pull", reference], timeout=max(self.timeout, 600))
        if not result.ok:
            raise DockerError("image_pull_failed", f"cannot pull {reference}")
        return result

    # --- compose ---------------------------------------------------------

    def compose(self, args, *, timeout=None):
        if not self.compose_file:
            raise DockerError("compose_file_missing", "no compose file is configured")
        argv = ["compose", "-f", self.compose_file, *args]
        return self.runner.run("docker", argv, timeout=timeout or max(self.timeout, 600))

    def compose_services(self):
        try:
            result = self.compose(["config", "--services"], timeout=60)
        except (DockerError, CommandError):
            return []
        if not result.ok:
            return []
        return [line.strip() for line in result.stdout.splitlines() if line.strip()]

    def compose_up_service(self, service):
        return self.compose(["up", "-d", "--no-deps", service])

    def compose_stop_service(self, service):
        return self.compose(["stop", service], timeout=120)


def _container_state(name, payload):
    if not payload.get("Id"):
        return ContainerState(name=name)
    state = payload.get("State") or {}
    config = payload.get("Config") or {}
    health = (state.get("Health") or {}).get("Status") or HEALTH_NONE
    logs = (state.get("Health") or {}).get("Log") or []
    health_output = ""
    if logs:
        health_output = str(logs[-1].get("Output") or "")

    if state.get("Restarting"):
        lifecycle = CONTAINER_RESTARTING
    elif state.get("Running"):
        lifecycle = CONTAINER_RUNNING
    elif state.get("Status") == "created":
        lifecycle = CONTAINER_CREATED
    else:
        lifecycle = CONTAINER_EXITED

    ports = []
    for port, bindings in ((payload.get("NetworkSettings") or {}).get("Ports") or {}).items():
        for binding in bindings or []:
            ports.append(f"{binding.get('HostPort', '')}->{port}")

    return ContainerState(
        name=name,
        exists=True,
        state=lifecycle,
        health=health,
        container_id=str(payload.get("Id") or ""),
        image=str(config.get("Image") or payload.get("Image") or ""),
        image_id=str(payload.get("Image") or ""),
        started_at=str(state.get("StartedAt") or ""),
        exit_code=state.get("ExitCode"),
        restart_count=int(payload.get("RestartCount") or 0),
        health_output=health_output,
        ports=tuple(ports),
    )


def _image_state(reference, payload):
    if not payload.get("Id"):
        return ImageState(reference=reference)
    digests = payload.get("RepoDigests") or []
    digest = ""
    for entry in digests:
        _, _, candidate = str(entry).partition("@")
        if candidate:
            digest = candidate
            break
    return ImageState(
        reference=reference,
        exists=True,
        digest=digest,
        image_id=str(payload.get("Id") or ""),
        architecture=str(payload.get("Architecture") or ""),
        os=str(payload.get("Os") or ""),
        labels=dict((payload.get("Config") or {}).get("Labels") or {}),
    )
