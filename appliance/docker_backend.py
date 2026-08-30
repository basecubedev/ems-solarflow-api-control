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
    def __init__(self, runner, *, timeout=120):
        self.runner = runner
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

    def inspect_container(self, name, *, strict=False):
        """One container's state. ``strict`` separates absent from unknown.

        "No such container" and "the daemon did not answer" are the same exit
        status. Only the first is a state a plan may be made against, so a
        caller that has to tell them apart asks for ``strict`` and gets an
        error instead of a container that looks absent.
        """

        result = self.runner.run(
            "docker", ["inspect", "--type", "container", "--format", "{{json .}}", name], timeout=30
        )
        if not result.ok:
            if strict and not _container_absent(result):
                raise DockerError(
                    "container_state_unknown",
                    f"the state of {name} could not be determined: "
                    f"{(result.stderr or '').strip()[:200]}",
                )
            return ContainerState(name=name)
        try:
            payload = json.loads(result.stdout.strip() or "{}")
        except ValueError:
            if strict:
                raise DockerError(
                    "container_state_unknown", f"{name} did not inspect as JSON"
                )
            return ContainerState(name=name)
        return _container_state(name, payload)

    def containers_publishing_port(self, port):
        """Which running containers publish ``port`` on the host.

        Ownership of a listening socket is a question only the engine can
        answer; a process name like ``docker-proxy`` says nothing about which
        container it belongs to.
        """

        result = self.runner.run(
            "docker",
            ["ps", "--filter", f"publish={int(port)}", "--format", "{{.Names}}"],
            timeout=30,
        )
        if not result.ok:
            raise DockerError(
                "port_owner_unknown", f"cannot list the containers publishing port {port}"
            )
        return sorted({line.strip() for line in (result.stdout or "").splitlines() if line.strip()})

    def exec_in_container(self, name, argv, *, timeout=60):
        """Run one fixed command inside a container. Bounded, never a shell.

        The argv is a constant in the caller. Nothing is interpolated, nothing
        comes from a request, and the timeout is the caller's, so a service that
        hangs fails its gate instead of holding the trial open.
        """

        return self.runner.run(
            "docker",
            ["exec", str(name), *[str(item) for item in argv]],
            timeout=timeout,
        )

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

    def save_image(self, reference, path):
        """Write one image to a file, so it can be loaded again without a WAN."""

        result = self.runner.run(
            "docker", ["save", "-o", str(path), reference], timeout=max(self.timeout, 900)
        )
        if not result.ok:
            raise DockerError("image_save_failed", f"cannot save {reference}")
        return result

    def load_image(self, path):
        result = self.runner.run(
            "docker", ["load", "-i", str(path)], timeout=max(self.timeout, 900)
        )
        if not result.ok:
            raise DockerError("image_load_failed", f"cannot load {path}")
        return result

    # --- compose ---------------------------------------------------------

    def compose(self, args, *, compose_file, timeout=None, overrides=()):
        """Run ``docker compose`` against the file the caller resolved.

        The path is never cached here: the deployment it names is created and
        edited by the very operations that then have to run against it, so a
        copy taken earlier can name a file that did not exist yet.
        """

        if not compose_file:
            raise DockerError("compose_file_missing", "no compose file is configured")
        argv = ["compose", "-f", str(compose_file)]
        for override in overrides:
            argv += ["-f", str(override)]
        argv += list(args)
        return self.runner.run("docker", argv, timeout=timeout or max(self.timeout, 600))

    def compose_services(self, *, compose_file):
        try:
            result = self.compose(["config", "--services"], compose_file=compose_file, timeout=60)
        except (DockerError, CommandError):
            return []
        if not result.ok:
            return []
        return [line.strip() for line in result.stdout.splitlines() if line.strip()]

    def compose_up_service(self, service, *, compose_file, overrides=()):
        return self.compose(
            ["up", "-d", "--no-deps", service], compose_file=compose_file, overrides=overrides
        )

    def compose_stop_service(self, service, *, compose_file):
        return self.compose(["stop", service], compose_file=compose_file, timeout=120)


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


def _container_absent(result):
    return "no such" in ((result.stderr or "") + (result.stdout or "")).lower()


def _repository_digest(reference, digests):
    """The digest of the repository that was asked about, not just the first.

    An image can carry repo digests for several repositories. Answering with
    whichever came first would compare the Admin image against the digest of
    something else that happens to share the layers.
    """

    repository = str(reference).partition("@")[0].rpartition(":")[0] or (
        str(reference).partition("@")[0]
    )
    fallback = ""
    for entry in digests:
        name, _, candidate = str(entry).partition("@")
        if not candidate:
            continue
        if name == repository:
            return candidate
        fallback = fallback or candidate
    return fallback


def _image_state(reference, payload):
    if not payload.get("Id"):
        return ImageState(reference=reference)
    return ImageState(
        reference=reference,
        exists=True,
        digest=_repository_digest(reference, payload.get("RepoDigests") or []),
        image_id=str(payload.get("Id") or ""),
        architecture=str(payload.get("Architecture") or ""),
        os=str(payload.get("Os") or ""),
        labels=dict((payload.get("Config") or {}).get("Labels") or {}),
    )
