# SPDX-License-Identifier: AGPL-3.0-or-later
"""Prepare and start a Docker deployment workspace for the Admin wizard.

Step 04 of the setup wizard reuses the existing Docker-first bootstrap contract
as the source of truth. Preparation runs the release ``install-docker.sh`` with
``--no-start`` so the canonical installer writes ``docker-compose.yml``,
``config/`` and ``data/`` (and, with ``--analytics``, the bundled InfluxDB
secrets) without ever starting a container. The planned images are then pulled
with visible progress.

Step 05 starts the *already prepared* workspace with ``docker compose up -d``
(adding the ``with-analytics`` profile when the prepared stack bundles InfluxDB)
and reports container/dashboard status. Step 05 never re-runs preparation and
never pulls images itself.
"""

import copy
import hashlib
import json
import os
import re
import ssl
import subprocess
import threading
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

from admin.install_context import detect_install_context
from admin.models import utc_now_iso
from admin.releases import DOCKER_IMAGE_REPOSITORY, ReleaseError, default_admin_data_dir
from admin.setup_workflow import GENERATED_CONFIG_OWNER, read_generated_metadata

INSTALL_SCRIPT = "install-docker.sh"
INFLUX_COMPOSE_RESOURCE = "deploy/docker/compose.influxdb.yml"
_JOB_LOG_MAX_LINES = 200

_INFLUX_IMAGE_RE = re.compile(r"^\s*image:\s*(influxdb:\S+)", re.MULTILINE)
_CONTAINER_NAME_RE = re.compile(
    r"^\s*container_name:\s*[\"']?([A-Za-z0-9][A-Za-z0-9_.-]*)[\"']?\s*(?:#.*)?$",
    re.MULTILINE,
)
_CONTAINER_CONFLICT_RE = re.compile(
    r'container name\s+["\']?/?([A-Za-z0-9][A-Za-z0-9_.-]*)["\']?\s+'
    r"is already in use",
    re.IGNORECASE,
)
SAFE_STOPPED_CONTAINER_STATES = frozenset({"created", "exited", "dead", "stopped"})
WORKSPACE_PERMISSION_MESSAGE = (
    "Deployment workspace is not writable by EMS. The prepared config/ or data/ "
    "folder cannot be written by the EMS runtime user. Repair permissions and try again."
)

MAX_MARKER_BYTES = 64 * 1024

# Admin controls the host Docker engine over this mounted Unix socket
# (Docker-out-of-Docker). Its presence is what distinguishes deployment
# controller mode from discovery-only preview mode.
DOCKER_SOCKET_PATH = os.environ.get("EMS_ADMIN_DOCKER_SOCKET", "/var/run/docker.sock")

# Stable state → error-code mapping shared by probe() and check(). ``ready`` has
# no code because it is not an error.
_DOCKER_CODES = {
    "ready": None,
    "client_missing": "docker_cli_missing",
    "socket_missing": "docker_socket_not_mounted",
    "permission_denied": "docker_permission_denied",
    "daemon_unreachable": "docker_daemon_unreachable",
    "unavailable": "docker_unavailable",
}

_DOCKER_MESSAGES = {
    "ready": "Docker is available. Images will be downloaded to the host Docker engine.",
    "client_missing": (
        "The Admin image does not include Docker client support. Rebuild the "
        "Admin image with the Docker CLI to control the host Docker engine."
    ),
    "socket_missing": (
        "The Admin container was started in restricted (discovery-only) mode "
        "without the host Docker socket. Restart Admin Setup with the default "
        "Docker command (deploy/admin/start-admin-setup.sh) to download and "
        "start EMS containers."
    ),
    "permission_denied": (
        "The Admin container cannot access the Docker socket. Check the Docker "
        "socket permissions or the container user."
    ),
    "daemon_unreachable": (
        "The Docker daemon is not reachable. Start Docker and try again."
    ),
    "unavailable": (
        "Docker is not available. Check that Docker is installed and running."
    ),
}


# Registry (GHCR / Docker Hub) pull-rate-limit signals. A pull hitting these does
# not mean the tag is missing or the network is down; it is throttling that a
# retry (or an authenticated Docker login) resolves, so it maps to its own code.
REGISTRY_RATE_LIMIT_MARKERS = (
    "toomanyrequests",
    "too many requests",
    "rate limit exceeded",
    "pull rate limit",
    "reached your pull rate limit",
    "denied due to rate limit",
)

REGISTRY_RATE_LIMIT_MESSAGE = (
    "GitHub Container Registry rate limit reached.\n\n"
    "No installation changes were made. Wait before retrying, or authenticate "
    "Docker with a GitHub account to increase the available request quota."
)


def is_registry_rate_limit_text(text):
    """True when pull output signals a registry pull-rate-limit (429/throttle)."""

    lowered = (text or "").lower()
    return any(marker in lowered for marker in REGISTRY_RATE_LIMIT_MARKERS)


class DockerError(Exception):
    """A user-facing Docker/bootstrap failure with a stable ``code``."""

    def __init__(self, code, message, detail=None, conflict=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail
        self.conflict = conflict


class DockerCli:
    """Thin ``docker`` CLI wrapper: availability check and image pull.

    Kept injectable so unit tests can drive preparation without a real daemon.
    """

    def __init__(self, run=None, popen=None, socket_path=DOCKER_SOCKET_PATH):
        self._run = run or subprocess.run
        self._popen = popen or subprocess.Popen
        self._socket_path = socket_path

    def probe(self):
        """Return the structured host-Docker access state without raising.

        Distinguishes a missing client, an unmounted socket (discovery-only
        preview mode), a socket that is present but inaccessible, an unreachable
        daemon, and a reachable daemon. Step 04 renders these as distinct cases.
        """

        try:
            result = self._run(
                ["docker", "version", "--format", "{{.Server.Version}}"],
                capture_output=True,
                text=True,
                timeout=15,
            )
        except FileNotFoundError:
            return _docker_status("client_missing", self._socket_path)
        except (OSError, subprocess.SubprocessError):
            return _docker_status("unavailable", self._socket_path)
        if result.returncode == 0:
            return _docker_status(
                "ready", self._socket_path, server_version=(result.stdout or "").strip()
            )
        if self._socket_path and not _socket_present(self._socket_path):
            return _docker_status("socket_missing", self._socket_path)
        return _docker_status(_classify_check_error(result.stderr), self._socket_path)

    def check(self):
        status = self.probe()
        if status["state"] != "ready":
            raise DockerError(status["code"], status["message"])
        return status["server_version"]

    def pull(self, image, on_progress=None):
        try:
            process = self._popen(
                ["docker", "pull", image],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
        except FileNotFoundError as exc:
            raise DockerError(
                "docker_cli_missing", _DOCKER_MESSAGES["client_missing"]
            ) from exc
        state = {}
        tail = []
        stdout = process.stdout
        if stdout is not None:
            for line in stdout:
                line = line.rstrip("\n")
                tail.append(line)
                del tail[:-40]
                percent = parse_pull_progress(state, line)
                if on_progress is not None:
                    on_progress(percent, line)
        if process.wait() != 0:
            raise _docker_pull_error("\n".join(tail))

    def inspect_container(self, container_name):
        """Return one exact-name container from Docker, or ``None``."""

        try:
            result = self._run(
                [
                    "docker",
                    "ps",
                    "-a",
                    "--filter",
                    f"name=^/{container_name}$",
                    "--format",
                    "{{json .}}",
                ],
                capture_output=True,
                text=True,
                timeout=15,
            )
        except FileNotFoundError as exc:
            raise DockerError(
                "docker_cli_missing", _DOCKER_MESSAGES["client_missing"]
            ) from exc
        except (OSError, subprocess.SubprocessError) as exc:
            raise DockerError(
                "docker_container_inspect_failed",
                "Could not inspect existing Docker containers.",
            ) from exc
        if result.returncode != 0:
            raise DockerError(
                "docker_container_inspect_failed",
                "Could not inspect existing Docker containers.",
                _safe_command_detail(result.stderr),
            )
        for line in (result.stdout or "").splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if not isinstance(row, dict):
                continue
            name = str(row.get("Names") or row.get("Name") or "").lstrip("/")
            if name == container_name:
                return {
                    "container_name": name,
                    "container_id": str(row.get("ID") or row.get("Id") or ""),
                    "image": str(row.get("Image") or ""),
                    "status": str(row.get("State") or "").lower(),
                    "status_detail": str(row.get("Status") or ""),
                }
        return None

    def inspect_container_image_id(self, container_name):
        """Return the immutable image ID used by an exact running container.

        ``docker ps`` exposes the mutable tag string. Recovery/finalization must
        inspect ``docker container inspect .Image`` so a tag moved after the
        container started cannot make old or different content look current.
        """

        name = str(container_name or "").strip()
        if not name:
            return None
        try:
            result = self._run(
                [
                    "docker",
                    "container",
                    "inspect",
                    "--format",
                    "{{.Image}}",
                    name,
                ],
                capture_output=True,
                text=True,
                timeout=15,
            )
        except (FileNotFoundError, OSError, subprocess.SubprocessError):
            return None
        if result.returncode != 0:
            return None
        image_id = str(result.stdout or "").strip()
        return image_id if image_id.startswith("sha256:") else None

    def inspect_image(self, image_ref):
        """Return a sanitized identity view of one local image, or ``None``.

        Reads build-identity labels and digests from ``docker image inspect``.
        Unlike :meth:`inspect_container`, this is used by read-only Admin
        release views, so it never raises for a missing Docker CLI, an
        unreachable daemon, or an absent image — all of those return ``None``
        and let the caller degrade to an all-unknown identity. It only inspects
        an image that is already present locally; it never pulls.
        """

        ref = str(image_ref or "").strip()
        if not ref:
            return None
        try:
            result = self._run(
                ["docker", "image", "inspect", ref],
                capture_output=True,
                text=True,
                timeout=15,
            )
        except (FileNotFoundError, OSError, subprocess.SubprocessError):
            return None
        if result.returncode != 0:
            return None
        try:
            payload = json.loads(result.stdout or "")
        except ValueError:
            return None
        if not isinstance(payload, list) or not payload:
            return None
        entry = payload[0]
        if not isinstance(entry, dict):
            return None
        return _sanitize_image_inspect(entry, ref)

    def remove_container(self, container_name):
        """Remove one container without deleting its volumes."""

        try:
            result = self._run(
                ["docker", "rm", container_name],
                capture_output=True,
                text=True,
                timeout=30,
            )
        except FileNotFoundError as exc:
            raise DockerError(
                "docker_cli_missing", _DOCKER_MESSAGES["client_missing"]
            ) from exc
        except (OSError, subprocess.SubprocessError) as exc:
            raise DockerError(
                "docker_container_remove_failed",
                "Could not remove the old stopped container.",
            ) from exc
        if result.returncode != 0:
            raise DockerError(
                "docker_container_remove_failed",
                "Could not remove the old stopped container.",
                _safe_command_detail(result.stderr),
            )

    def stop_container(self, container_name):
        """Stop one running container before a confirmed replacement."""

        try:
            result = self._run(
                ["docker", "stop", "--time", "20", container_name],
                capture_output=True,
                text=True,
                timeout=45,
            )
        except FileNotFoundError as exc:
            raise DockerError(
                "docker_cli_missing", _DOCKER_MESSAGES["client_missing"]
            ) from exc
        except (OSError, subprocess.SubprocessError) as exc:
            raise DockerError(
                "docker_container_stop_failed",
                "Could not stop the running EMS container.",
            ) from exc
        if result.returncode != 0:
            raise DockerError(
                "docker_container_stop_failed",
                "Could not stop the running EMS container.",
                _safe_command_detail(result.stderr),
            )

    def check_workspace_permissions(self, workspace, image, puid, pgid):
        workspace = _validated_workspace(workspace)
        command = self._workspace_command(
            workspace,
            image,
            f"{puid}:{pgid}",
            (
                "set -eu; "
                "test -f /workspace/config/config.json; "
                "probe=/workspace/config/.admin-write-test-$$; "
                "touch \"$probe\"; rm -f \"$probe\"; "
                "probe=/workspace/data/.admin-write-test-$$; "
                "touch \"$probe\"; rm -f \"$probe\""
            ),
        )
        result = self._run(command, capture_output=True, text=True, timeout=30)
        if result.returncode != 0:
            failing_path = _workspace_failure_path(result.stderr or result.stdout)
            raise DockerError(
                "workspace_permission_denied",
                WORKSPACE_PERMISSION_MESSAGE,
                _workspace_permission_detail(workspace, puid, pgid, failing_path),
            )

    def repair_workspace_permissions(self, workspace, image, puid, pgid):
        workspace = _validated_workspace(workspace)
        command = self._workspace_command(
            workspace,
            image,
            "0:0",
            (
                f"chown -R {puid}:{pgid} /workspace/config /workspace/data && "
                "chmod -R u+rwX /workspace/config /workspace/data"
            ),
        )
        result = self._run(command, capture_output=True, text=True, timeout=60)
        if result.returncode != 0:
            raise DockerError(
                "workspace_permission_repair_failed",
                "Could not repair the deployment workspace permissions.",
                _safe_command_detail(result.stderr or result.stdout),
            )

    @staticmethod
    def _workspace_command(workspace, image, user, script):
        return [
            "docker",
            "run",
            "--rm",
            "--user",
            user,
            "--entrypoint",
            "sh",
            "--mount",
            f"type=bind,src={workspace / 'config'},dst=/workspace/config",
            "--mount",
            f"type=bind,src={workspace / 'data'},dst=/workspace/data",
            image,
            "-c",
            script,
        ]


class BootstrapInstaller:
    """Runs the release ``install-docker.sh`` with ``--no-start``.

    This never starts the stack: the installer only writes the compose/config
    scaffold (and, with ``--analytics``, generates bundled InfluxDB secrets).
    """

    def __init__(self, popen=None):
        self._popen = popen or subprocess.Popen

    def prepare(self, workspace, script_path, analytics=False, tag=None, on_line=None):
        command = ["sh", str(script_path), "--no-start"]
        if analytics:
            command.append("--analytics")
        if tag and tag != "latest":
            command += ["--tag", str(tag)]
        try:
            process = self._popen(
                command,
                cwd=str(workspace),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
        except FileNotFoundError as exc:
            raise DockerError(
                "bootstrap_unavailable",
                "The bootstrap installer could not be run.",
            ) from exc
        tail = []
        stdout = process.stdout
        if stdout is not None:
            for line in stdout:
                line = line.rstrip("\n")
                tail.append(line)
                del tail[:-60]
                if on_line is not None:
                    on_line(line)
        if process.wait() != 0:
            raise _bootstrap_error("\n".join(tail))


class DockerCompose:
    """Thin ``docker compose`` wrapper for starting and inspecting the stack.

    Injectable so Step 05 can be tested without a real daemon. Only operates on
    the already-prepared workspace; it never chooses images or compose paths.
    """

    def __init__(self, run=None, popen=None):
        self._run = run or subprocess.run
        self._popen = popen or subprocess.Popen

    def up(self, workspace, profiles=(), services=(), force_recreate=False, on_line=None):
        command = ["docker", "compose"]
        for profile in profiles:
            command += ["--profile", str(profile)]
        command += ["up", "-d"]
        if force_recreate:
            command.append("--force-recreate")
        command += [str(service) for service in services]
        try:
            process = self._popen(
                command,
                cwd=str(workspace),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
        except FileNotFoundError as exc:
            raise DockerError(
                "docker_cli_missing", _DOCKER_MESSAGES["client_missing"]
            ) from exc
        tail = []
        stdout = process.stdout
        if stdout is not None:
            for line in stdout:
                line = line.rstrip("\n")
                tail.append(line)
                del tail[:-60]
                if on_line is not None:
                    on_line(line)
        if process.wait() != 0:
            raise _compose_start_error("\n".join(tail))

    def stop(self, workspace, services, profiles=(), on_line=None):
        """Stop feature services without removing containers, volumes or data."""

        command = ["docker", "compose"]
        for profile in profiles:
            command += ["--profile", str(profile)]
        command += ["stop", "--time", "20"]
        command += [str(service) for service in services]
        try:
            process = self._popen(
                command,
                cwd=str(workspace),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
        except FileNotFoundError as exc:
            raise DockerError(
                "docker_cli_missing", _DOCKER_MESSAGES["client_missing"]
            ) from exc
        tail = []
        stdout = process.stdout
        if stdout is not None:
            for line in stdout:
                line = line.rstrip("\n")
                tail.append(line)
                del tail[:-60]
                if on_line is not None:
                    on_line(line)
        if process.wait() != 0:
            raise DockerError(
                "docker_compose_stop_failed",
                "Could not stop the optional feature container.",
                _safe_command_detail("\n".join(tail)),
            )

    def run_oneoff(self, workspace, service, command, timeout=180, input_text=None):
        """Run a one-off ``docker compose run --rm`` command.

        Returns ``(returncode, detail)`` where ``detail`` is a redacted output
        tail. Callers must never surface raw output because it may carry secrets.
        ``input_text``, when set, is piped to stdin (``-T`` disables the pseudo-TTY
        so the command reads it non-interactively); it is never placed in argv.
        """

        argv = ["docker", "compose", "run", "--rm"]
        if input_text is not None:
            argv.append("-T")
        argv.append(str(service))
        argv += [str(part) for part in command]
        try:
            result = self._run(
                argv,
                cwd=str(workspace),
                capture_output=True,
                text=True,
                timeout=timeout,
                input=input_text,
            )
        except FileNotFoundError as exc:
            raise DockerError(
                "docker_cli_missing", _DOCKER_MESSAGES["client_missing"]
            ) from exc
        except (OSError, subprocess.SubprocessError) as exc:
            raise DockerError(
                "docker_compose_run_failed",
                "Could not run the one-off container command.",
            ) from exc
        detail = _safe_command_detail(
            "\n".join(part for part in (result.stdout, result.stderr) if part)
        )
        return result.returncode, detail

    def ps(self, workspace):
        try:
            result = self._run(
                ["docker", "compose", "ps", "--format", "json"],
                cwd=str(workspace),
                capture_output=True,
                text=True,
                timeout=30,
            )
        except FileNotFoundError as exc:
            raise DockerError(
                "docker_cli_missing", _DOCKER_MESSAGES["client_missing"]
            ) from exc
        except (OSError, subprocess.SubprocessError) as exc:
            raise DockerError(
                "docker_compose_status_failed",
                "Could not read container status from Docker Compose.",
            ) from exc
        if result.returncode != 0:
            raise DockerError(
                "docker_compose_status_failed",
                "Could not read container status from Docker Compose.",
            )
        return _parse_compose_ps(result.stdout)

    def logs(self, workspace, service="ems"):
        try:
            result = self._run(
                ["docker", "compose", "logs", "--no-color", "--tail", "80", service],
                cwd=str(workspace),
                capture_output=True,
                text=True,
                timeout=30,
            )
        except (FileNotFoundError, OSError, subprocess.SubprocessError):
            return ""
        return "\n".join(part for part in (result.stdout, result.stderr) if part)


def probe_dashboard_url(url, timeout=3.0):
    """Return True when the dashboard answers at ``url`` (any HTTP status).

    A self-signed dashboard certificate or an auth challenge (401/redirect) still
    means EMS is reachable, so any HTTP response counts as reachable.
    """

    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    request = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout, context=context):
            return True
    except urllib.error.HTTPError:
        return True
    except Exception:  # unreachable, DNS/connection error, timeout
        return False


def _socket_present(socket_path):
    try:
        return Path(socket_path).exists()
    except OSError:
        return False


def _classify_check_error(stderr):
    text = (stderr or "").lower()
    if "permission denied" in text:
        return "permission_denied"
    if "cannot connect to the docker daemon" in text or "is the docker daemon running" in text:
        return "daemon_unreachable"
    return "unavailable"


def _docker_status(state, socket_path, server_version=None):
    return {
        "state": state,
        "code": _DOCKER_CODES.get(state, "docker_unavailable"),
        "message": _DOCKER_MESSAGES.get(state, _DOCKER_MESSAGES["unavailable"]),
        "mode": "deployment_controller"
        if state != "client_missing" and _socket_present(socket_path)
        else "discovery_only",
        "socket": socket_path,
        "server_version": server_version,
    }


def _docker_pull_error(tail):
    text = (tail or "").lower()
    if is_registry_rate_limit_text(text):
        return DockerError(
            "image_pull_rate_limited",
            REGISTRY_RATE_LIMIT_MESSAGE,
            _safe_command_detail(tail),
        )
    if any(
        marker in text
        for marker in (
            "no such host",
            "timeout",
            "temporary failure in name resolution",
            "connection refused",
            "network is unreachable",
            "i/o timeout",
        )
    ):
        return DockerError(
            "image_pull_network_error",
            "The image could not be downloaded because of a network error. "
            "Check the internet connection and try again.",
        )
    return DockerError(
        "image_pull_failed",
        "The image could not be pulled. Check that the release tag exists "
        "and the registry is reachable.",
    )


def _bootstrap_error(tail):
    text = (tail or "").lower()
    detail = _safe_command_detail(tail)
    if "docker is not installed" in text or "docker cli" in text:
        return DockerError(
            "docker_cli_missing", _DOCKER_MESSAGES["client_missing"], detail
        )
    if "cannot talk to the docker daemon" in text or "daemon" in text:
        return DockerError(
            "docker_daemon_unreachable",
            "The Docker daemon is not reachable. Start Docker and try again.",
            detail,
        )
    if "compose" in text and ("newer is required" in text or "not available" in text):
        return DockerError(
            "docker_compose_unsupported",
            "Docker Compose v2.24.0 or newer is required. Update Docker Compose.",
            detail,
        )
    return DockerError(
        "bootstrap_failed",
        "The bootstrap installer failed. See the deployment log for details.",
        detail,
    )


def _compose_start_error(tail):
    text = (tail or "").lower()
    detail = _safe_command_detail(tail)
    if _is_workspace_permission_log(text):
        return DockerError(
            "workspace_permission_denied", WORKSPACE_PERMISSION_MESSAGE, detail
        )
    if "permission denied" in text:
        return DockerError(
            "docker_permission_denied", _DOCKER_MESSAGES["permission_denied"], detail
        )
    if "cannot connect to the docker daemon" in text or "is the docker daemon running" in text:
        return DockerError(
            "docker_daemon_unreachable",
            "The Docker daemon is not reachable. Start Docker and try again.",
            detail,
        )
    if "port is already allocated" in text or "address already in use" in text:
        return DockerError(
            "compose_port_conflict",
            "A required port is already in use. Stop the conflicting service and try again.",
            detail,
        )
    if (
        "container name" in text
        and ("already in use" in text or "conflict" in text)
    ):
        match = _CONTAINER_CONFLICT_RE.search(tail or "")
        conflict = None
        if match:
            conflict = {
                "type": "container_name_conflict",
                "container_name": match.group(1),
                "container_id": None,
                "image": None,
                "status": "unknown",
                "selected_image": None,
                "image_mismatch": False,
                "replace_available": False,
                "safe_fix_available": False,
            }
        return DockerError(
            "compose_container_name_conflict",
            "An EMS container already exists. Stop or remove the old EMS container "
            "before starting this deployment.",
            detail,
            conflict,
        )
    if any(
        marker in text
        for marker in (
            "pull access denied",
            "manifest unknown",
            "manifest not found",
            "no matching manifest",
        )
    ):
        return DockerError(
            "compose_image_unavailable",
            "A required container image is unavailable. Check the release image "
            "and registry access, then prepare the deployment again.",
            detail,
        )
    if any(
        marker in text
        for marker in (
            "no configuration file provided",
            "can't find a suitable configuration file",
            "cannot find a suitable configuration file",
            "docker-compose.yml: no such file",
            "compose.yaml: no such file",
        )
    ):
        return DockerError(
            "deployment_workspace_missing",
            "The prepared deployment workspace is missing. Prepare the deployment again.",
            detail,
        )
    return DockerError(
        "compose_start_failed",
        "Starting the EMS containers failed. See the deployment log for details.",
        detail,
    )


def _is_workspace_permission_log(text):
    text = (text or "").lower()
    return (
        "ems refuses to start as root" in text
        or (
            "mounted /app/data or /app/config directory is not writable" in text
            and "non-root runtime user" in text
        )
    )


def _workspace_failure_path(detail):
    text = (detail or "").lower()
    if "/workspace/config" in text or "/app/config" in text:
        return "config/"
    if "/workspace/data" in text or "/app/data" in text:
        return "data/"
    return "config/ or data/"


def _workspace_permission_detail(workspace, puid, pgid, failing_path):
    lines = [
        f"Deployment workspace is not writable by EMS runtime user {puid}:{pgid}.",
        f"Workspace: {workspace}",
        "Checked:",
        f"- {workspace / 'config'}",
        f"- {workspace / 'data'}",
        f"Failing path: {failing_path}",
    ]
    if workspace == Path("/data") or Path("/data") in workspace.parents:
        lines.extend(
            [
                "",
                "The Admin container uses a /data workspace while controlling the host "
                "Docker daemon.",
                "Restart it through deploy/admin/start-admin-setup.sh so Docker bind "
                "mounts use host-visible absolute paths.",
            ]
        )
    return "\n".join(lines)


def _validated_workspace(workspace):
    workspace = Path(workspace).resolve()
    for name in ("config", "data"):
        path = workspace / name
        if path.is_symlink() or not path.is_dir() or path.resolve().parent != workspace:
            raise DockerError(
                "deployment_workspace_invalid",
                "The prepared deployment workspace contains an unsafe mounted path.",
                f"Invalid workspace path: {path}",
            )
    return workspace


def _safe_command_detail(tail, max_lines=8, max_chars=1600):
    lines = [line.strip() for line in (tail or "").splitlines() if line.strip()]
    detail = "\n".join(lines[-max_lines:])
    detail = re.sub(r"(https?://)[^/\s:@]+:[^/\s@]+@", r"\1[redacted]@", detail)
    detail = re.sub(
        r"(?i)\b(password|passwd|token|secret|authorization)(\s*[:=]\s*)\S+",
        r"\1\2[redacted]",
        detail,
    )
    if len(detail) > max_chars:
        detail = "…" + detail[-(max_chars - 1) :]
    return detail or None


def _string_list(values):
    return [item for item in (values or []) if isinstance(item, str)]


def _primary_repo_digest(repo_digests):
    """Return the registry content digest (``sha256:...``) from repo digests."""

    for item in repo_digests:
        _, sep, digest = item.partition("@")
        if sep and digest.startswith("sha256:"):
            return digest
    return None


def _sanitize_image_inspect(entry, image_ref):
    """Reduce one ``docker image inspect`` object to labels + digests we trust."""

    config = entry.get("Config")
    raw_labels = config.get("Labels") if isinstance(config, dict) else None
    labels = {
        str(key): (value if isinstance(value, str) else str(value))
        for key, value in raw_labels.items()
        if isinstance(key, str) and value is not None
    } if isinstance(raw_labels, dict) else {}
    repo_digests = _string_list(entry.get("RepoDigests"))
    image_id = str(entry.get("Id") or "") or None
    return {
        "image_ref": image_ref,
        "id": image_id,
        # Registry content digests are preferred. A source-built image has no
        # RepoDigest, so its immutable Docker image ID is the local equivalent.
        "digest": _primary_repo_digest(repo_digests) or image_id,
        "repo_digests": repo_digests,
        "repo_tags": _string_list(entry.get("RepoTags")),
        "labels": labels,
    }


def _read_env_file(path):
    values = {}
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except OSError:
        return values
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            values[key] = value
    return values


def _valid_runtime_identity(puid, pgid):
    try:
        uid, gid = int(str(puid), 10), int(str(pgid), 10)
    except (TypeError, ValueError):
        return None
    if not (1 <= uid <= 2**31 - 1 and 1 <= gid <= 2**31 - 1):
        return None
    return uid, gid


def _parse_compose_ps(text):
    """Parse ``docker compose ps --format json`` (array or NDJSON) into services."""

    text = (text or "").strip()
    if not text:
        return []
    rows = []
    try:
        parsed = json.loads(text)
        rows = parsed if isinstance(parsed, list) else [parsed]
    except ValueError:
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue
    return [_normalize_service(row) for row in rows if isinstance(row, dict)]


def _normalize_service(row):
    state = (row.get("State") or row.get("state") or "").lower() or None
    return {
        "name": row.get("Name") or row.get("name"),
        "service": row.get("Service") or row.get("service"),
        "image": row.get("Image") or row.get("image"),
        "state": state,
        "status": row.get("Status") or row.get("status"),
        "ports": _normalize_service_ports(row.get("Publishers") or row.get("Ports")),
    }


def _normalize_service_ports(value):
    if isinstance(value, str):
        return [part.strip() for part in value.split(",") if part.strip()]
    if not isinstance(value, list):
        return []
    ports = []
    for publisher in value:
        if not isinstance(publisher, dict):
            continue
        published = publisher.get("PublishedPort")
        target = publisher.get("TargetPort")
        protocol = publisher.get("Protocol") or "tcp"
        if published:
            ports.append(f"{published}:{target}/{protocol}")
        elif target:
            ports.append(f"{target}/{protocol}")
    return ports


def parse_pull_progress(state, line):
    """Update ``state`` from one ``docker pull`` line and return a rough percent.

    ``docker pull`` interleaves per-layer progress; a precise overall percentage
    is not exposed, so this counts completed layers against all seen layers.
    """

    total = state.setdefault("layers", set())
    done = state.setdefault("done", set())
    stripped = line.strip()
    if stripped.startswith("Status:") or "Image is up to date" in stripped:
        state["complete"] = True
        return 100
    head, sep, rest = stripped.partition(":")
    if not sep:
        return _progress_percent(state)
    layer = head.strip()
    status = rest.strip().lower()
    if not layer or " " in layer:
        return _progress_percent(state)
    total.add(layer)
    if status.startswith(("pull complete", "already exists", "download complete")):
        done.add(layer)
    return _progress_percent(state)


def _progress_percent(state):
    if state.get("complete"):
        return 100
    total = state.get("layers") or set()
    if not total:
        return None
    return round(100 * len(state.get("done") or set()) / len(total))


class DeploymentJob:
    """Thread-safe progress record for one prepare operation."""

    def __init__(self, job_id, workspace):
        self._lock = threading.Lock()
        self._state = {
            "job_id": job_id,
            "status": "running",
            "phase": "Starting…",
            "steps": [],
            "images": [],
            "error": None,
            "prepared": False,
            "backups": [],
            "log": [],
            "workspace": workspace,
            "started_at": utc_now_iso(),
            "finished_at": None,
        }

    @property
    def job_id(self):
        return self._state["job_id"]

    def snapshot(self):
        with self._lock:
            return copy.deepcopy(self._state)

    def note_backups(self, backups):
        with self._lock:
            self._state["backups"] = [str(path) for path in backups]

    def log_line(self, line):
        with self._lock:
            self._state["log"].append(line)
            del self._state["log"][:-_JOB_LOG_MAX_LINES]

    def start_step(self, key, label):
        with self._lock:
            self._state["phase"] = label
            self._state["steps"].append(
                {"key": key, "label": label, "status": "running", "detail": None}
            )

    def finish_step(self, key, status="done", detail=None):
        with self._lock:
            for step in self._state["steps"]:
                if step["key"] == key:
                    step["status"] = status
                    step["detail"] = detail
                    break

    def set_images(self, images):
        with self._lock:
            self._state["images"] = [
                {
                    "service": image["service"],
                    "image": image["image"],
                    "status": "pending",
                    "percent": None,
                }
                for image in images
            ]

    def update_image(self, service, status=None, percent=None):
        with self._lock:
            for image in self._state["images"]:
                if image["service"] == service:
                    if status is not None:
                        image["status"] = status
                    if percent is not None:
                        image["percent"] = percent
                    break

    def fail(self, code, message, detail=None, conflict=None):
        with self._lock:
            self._state["status"] = "failed"
            self._state["prepared"] = False
            self._state["error"] = {"code": code, "message": message}
            if detail:
                self._state["error"]["detail"] = detail
            if conflict:
                self._state["conflict"] = copy.deepcopy(conflict)
            self._state["phase"] = message
            self._state["finished_at"] = utc_now_iso()
            for step in self._state["steps"]:
                if step["status"] == "running":
                    step["status"] = "failed"

    def succeed(self, prepared):
        with self._lock:
            self._state["status"] = "succeeded"
            self._state["prepared"] = True
            self._state["phase"] = "Deployment prepared"
            self._state["finished_at"] = utc_now_iso()
            self._state["result"] = prepared


class StartJob:
    """Thread-safe progress record for one deployment-start operation."""

    def __init__(self, job_id, workspace):
        self._lock = threading.Lock()
        self._state = {
            "job_id": job_id,
            "status": "running",
            "phase": "Starting…",
            "steps": [],
            "services": [],
            "dashboard_url": None,
            "dashboard_reachable": False,
            "error": None,
            "workspace": workspace,
            "started_at": utc_now_iso(),
            "finished_at": None,
        }

    @property
    def job_id(self):
        return self._state["job_id"]

    def snapshot(self):
        with self._lock:
            return copy.deepcopy(self._state)

    def start_step(self, key, label):
        with self._lock:
            self._state["phase"] = label
            self._state["steps"].append(
                {"key": key, "label": label, "status": "running", "detail": None}
            )

    def finish_step(self, key, status="done", detail=None):
        with self._lock:
            for step in self._state["steps"]:
                if step["key"] == key:
                    step["status"] = status
                    step["detail"] = detail
                    break

    def set_services(self, services):
        with self._lock:
            self._state["services"] = list(services)

    def set_dashboard(self, url, reachable):
        with self._lock:
            self._state["dashboard_url"] = url
            self._state["dashboard_reachable"] = bool(reachable)

    def fail(self, code, message, detail=None, conflict=None):
        with self._lock:
            self._state["status"] = "failed"
            self._state["error"] = {"code": code, "message": message}
            if detail:
                self._state["error"]["detail"] = detail
            if conflict:
                self._state["conflict"] = copy.deepcopy(conflict)
            self._state["phase"] = message
            self._state["finished_at"] = utc_now_iso()
            for step in self._state["steps"]:
                if step["status"] == "running":
                    step["status"] = "failed"

    def succeed(self):
        with self._lock:
            self._state["status"] = "succeeded"
            self._state["phase"] = "EMS is running"
            self._state["finished_at"] = utc_now_iso()


class DeploymentJobRegistry:
    """In-memory registry of deployment jobs; runs each on a bounded thread."""

    def __init__(self, max_jobs=8):
        self._lock = threading.Lock()
        self._jobs = {}
        self._order = []
        self._max_jobs = max_jobs

    def submit(self, job, runner, *, on_complete=None, on_settled=None):
        with self._lock:
            self._jobs[job.job_id] = job
            self._order.append(job.job_id)
            while len(self._order) > self._max_jobs:
                self._jobs.pop(self._order.pop(0), None)
        thread = threading.Thread(
            target=self._run,
            args=(job, runner, on_complete, on_settled),
            daemon=True,
        )
        thread.start()
        return job

    def get(self, job_id):
        with self._lock:
            job = self._jobs.get(job_id)
        return job.snapshot() if job is not None else None

    @staticmethod
    def _run(job, runner, on_complete=None, on_settled=None):
        try:
            runner(job)
        except DockerError as exc:
            job.fail(exc.code, exc.message, exc.detail, exc.conflict)
        except ReleaseError as exc:
            job.fail("release_error", str(exc))
        except OSError as exc:
            start_job = isinstance(job, StartJob)
            job.fail(
                "deployment_start_failed" if start_job else "workspace_write_failed",
                (
                    "Could not start the prepared deployment."
                    if start_job
                    else f"Could not write the deployment workspace: {exc}"
                ),
            )
        except Exception:  # never leak a traceback to the UI
            if isinstance(job, StartJob):
                job.fail("start_failed", "Starting EMS failed unexpectedly.")
            else:
                job.fail("prepare_failed", "Deployment preparation failed unexpectedly.")
        finally:
            # The worker has stopped mutating: release its lifecycle ownership
            # before any completion observer runs, so a terminal observer (which
            # needs its own exclusive claim) is not blocked by this worker.
            if on_settled is not None:
                try:
                    on_settled()
                except Exception:
                    pass
            if on_complete is not None:
                try:
                    on_complete(job.snapshot())
                except Exception:
                    # Completion observers update a separate durable state
                    # machine; an observer fault must not corrupt this job.
                    pass


class DeploymentService:
    """Guarded deployment planning, preparation, start, and status operations."""

    def __init__(
        self,
        release_manager,
        config_export,
        admin_data_dir=None,
        workspace_dir=None,
        docker=None,
        installer=None,
        registry=None,
        compose=None,
        start_registry=None,
        dashboard_probe=None,
        sleep=None,
        dashboard_attempts=4,
        dashboard_retry_seconds=1.0,
        runtime_env=None,
        install_context_provider=detect_install_context,
        setup_workflows=None,
    ):
        self.release_manager = release_manager
        self.config_export = config_export
        # Guided Setup workflow authority: deployment accepts only the active
        # workflow's preview-bound generated config. Without a store every
        # generated config fails closed into the review-required path.
        self.setup_workflows = setup_workflows
        data_dir = Path(admin_data_dir) if admin_data_dir else _admin_data_dir(release_manager)
        self.admin_data_dir = data_dir
        # The live deployment target is the standard EMS install root
        # (<EMS_INSTALL_DIR>/config, /data, /docker-compose.yml), never a private
        # Admin runtime directory. Admin-owned state stays under data/admin/.
        self.workspace_dir = (
            Path(workspace_dir)
            if workspace_dir
            else Path(install_context_provider().install_root)
        )
        self.marker_path = data_dir / "state" / ".admin-deployment.json"
        self.backup_dir = data_dir / "backups"
        self.docker = docker or DockerCli()
        self.installer = installer or BootstrapInstaller()
        self.compose = compose or DockerCompose()
        self.registry = registry or DeploymentJobRegistry()
        self.start_registry = start_registry or DeploymentJobRegistry()
        self._dashboard_probe = dashboard_probe or probe_dashboard_url
        self._sleep = sleep or time.sleep
        self._dashboard_attempts = max(1, int(dashboard_attempts))
        self._dashboard_retry_seconds = max(0.0, float(dashboard_retry_seconds))
        self._runtime_env = os.environ if runtime_env is None else runtime_env
        self._operation_lock = threading.Lock()
        self._active_job = None
        self._active_start_job = None
        self._pending_resolution_log = []

    # --- plan ------------------------------------------------------------

    def plan(self):
        release = self._release_context()
        config = self._generated_config_state()
        influx = self._influx_plan(release, config)
        images = self._planned_images(release, influx)
        marker = self._prepared_marker()
        identity = self._resolve_runtime_identity()
        return {
            "release": release["tag"] if release else None,
            "bootstrap_source": str(release["resource_dir"]) if release else None,
            "generated_config": {
                "ready": config["ready"],
                "path": config["path"],
            },
            "workspace": str(self.workspace_dir),
            "existing_install": self._existing_install_state(),
            "influxdb": influx,
            "images": images,
            "docker": self._docker_status(),
            "runtime_identity": (
                {"puid": identity[0], "pgid": identity[1]} if identity else None
            ),
            "prepared": marker if self._preparation_ready(marker, release, config) else None,
            "can_prepare": bool(release and config["ready"] and identity),
        }

    def _existing_install_state(self):
        """Distinguish a fresh setup from an existing install in the standard
        layout so the UI can label maintenance vs. replacement."""

        config_exists = (self.workspace_dir / "config" / "config.json").is_file()
        compose_exists = (self.workspace_dir / "docker-compose.yml").is_file()
        data_exists = (self.workspace_dir / "data").is_dir()
        return {
            "install_root": str(self.workspace_dir),
            "config_exists": config_exists,
            "compose_exists": compose_exists,
            "data_exists": data_exists,
            "present": config_exists or compose_exists,
        }

    def _docker_status(self):
        probe = getattr(self.docker, "probe", None)
        if not callable(probe):
            return None
        try:
            return probe()
        except Exception:  # host Docker state must never break the read-only plan
            return None

    # --- prepare ---------------------------------------------------------

    def prepare(self, overwrite=False, *, workflow_id=None, on_settled=None):
        """Submit the preparation worker for one immutable workflow identity.

        The owning workflow is resolved once, here, and carried in the worker's
        context. The worker never asks which workflow is active later, so it can
        neither stamp a replacement workflow's identity into the marker nor keep
        writing for a workflow that was terminalized in the meantime.
        """

        release = self._release_context()
        if release is None:
            return _reject("release_not_prepared", "Prepare release resources first.")
        config = self._generated_config_state()
        if not config["ready"]:
            return _reject(
                "generated_config_missing",
                "Save a generated config before preparing the deployment.",
            )
        rejection = self._generated_config_rejection(config)
        if rejection is not None:
            return rejection
        identity = self._resolve_runtime_identity()
        if identity is None:
            return _reject(
                "runtime_identity_missing",
                "No valid non-root PUID/PGID is configured. Start Admin Setup with "
                "deploy/admin/start-admin-setup.sh or pass PUID and PGID to the Admin container.",
            )

        influx = self._influx_plan(release, config)
        images = self._planned_images(release, influx)

        with self._operation_lock:
            if self._active_start_job is not None:
                active_start = self.start_registry.get(self._active_start_job)
                if active_start is not None and active_start["status"] == "running":
                    return _reject(
                        "deployment_start_running",
                        "EMS is currently starting. Wait for Step 05 to finish.",
                    )
            if self._active_job is not None:
                active = self.registry.get(self._active_job)
                if active is not None and active["status"] == "running":
                    return {"ok": True, "job": active, "status": 202}
            existing_conflict = self._existing_install_conflict(overwrite)
            if existing_conflict is not None:
                return existing_conflict
            conflict = self._workspace_conflict(release, config, overwrite)
            if conflict is not None:
                return conflict
            owner = self._prepare_owner(workflow_id)
            if isinstance(owner, dict) and owner.get("rejected"):
                return owner["rejected"]
            context = {
                "release": release,
                "config": config,
                "images": images,
                "analytics": influx["bundled"],
                "overwrite": overwrite,
                "puid": identity[0],
                "pgid": identity[1],
                "workflow": owner,
            }
            job = DeploymentJob(uuid.uuid4().hex, str(self.workspace_dir))
            self._active_job = job.job_id
            self.registry.submit(
                job,
                lambda handle: self._run_prepare(handle, context),
                on_settled=on_settled,
            )
            return {"ok": True, "job": job.snapshot(), "status": 202}

    def _prepare_owner(self, workflow_id):
        """The immutable workflow identity this preparation belongs to.

        ``None`` only when no workflow store is configured at all (a
        service-level harness); a configured store must resolve the requested id
        — or the active one when the caller did not name it — to an active
        record, else the preparation is refused before any worker starts.
        """

        if self.setup_workflows is None:
            return None
        record = self.setup_workflows.active()
        if record is None or (workflow_id and record["workflow_id"] != workflow_id):
            return {
                "rejected": _reject(
                    "generated_config_review_required",
                    "This deployment preparation does not belong to the active "
                    "setup workflow. Generate the config again under the current "
                    "setup.",
                )
            }
        preview = record.get("preview") or {}
        return {
            "workflow_id": record["workflow_id"],
            "preview_id": preview.get("preview_id"),
        }

    def job(self, job_id):
        return self.registry.get(job_id)

    # --- start -----------------------------------------------------------

    def start(
        self, *, on_complete=None, on_healthcheck=None, workflow_id=None,
        on_settled=None,
    ):
        rejection = self._verify_start_ready(workflow_id=workflow_id)
        if rejection is not None:
            return rejection
        marker = self._prepared_marker()
        profiles = ["with-analytics"] if _marker_bundles_influx(marker) else []
        context = {
            "profiles": profiles,
            "dashboard_url": self._dashboard_url(),
            "resolution_log": [],
            "workflow": (
                {"workflow_id": marker.get("workflow_id")}
                if isinstance(marker, dict) and marker.get("workflow_id")
                else None
            ),
        }
        with self._operation_lock:
            if self._active_job is not None:
                active_prepare = self.registry.get(self._active_job)
                if active_prepare is not None and active_prepare["status"] == "running":
                    return _reject(
                        "deployment_prepare_running",
                        "Deployment preparation is still running. Wait for Step 04 to finish.",
                    )
            if self._active_start_job is not None:
                active = self.start_registry.get(self._active_start_job)
                if active is not None and active["status"] == "running":
                    return {"ok": True, "job": active, "status": 202}
            context["resolution_log"] = list(self._pending_resolution_log)
            self._pending_resolution_log.clear()
            job = StartJob(uuid.uuid4().hex, str(self.workspace_dir))
            self._active_start_job = job.job_id

            def runner(handle):
                self._run_start(
                    handle,
                    context,
                    on_healthcheck=on_healthcheck,
                )

            self.start_registry.submit(
                job, runner, on_complete=on_complete, on_settled=on_settled
            )
            return {"ok": True, "job": job.snapshot(), "status": 202}

    def repair_workspace_permissions(self, *, workflow_id=None):
        marker = self._prepared_marker()
        if not self._marker_matches_workspace(marker):
            return _reject(
                "deployment_not_prepared",
                "Prepare the deployment first before repairing permissions.",
            )
        rejection = self._reject_foreign_marker(marker, workflow_id)
        if rejection is not None:
            return rejection
        identity = self._marker_runtime_identity(marker)
        image = self._ems_image_from_marker(marker)
        if identity is None or image is None:
            return _reject(
                "deployment_marker_invalid",
                "The prepared deployment does not contain a valid EMS runtime identity.",
            )
        try:
            self._repair_workspace(image, *identity)
            self._verify_workspace_permissions(image, *identity)
        except DockerError as exc:
            result = _reject(exc.code, exc.message)
            if exc.detail:
                result["detail"] = exc.detail
            return result
        return {"ok": True, "repaired": True}

    def start_job(self, job_id):
        return self.start_registry.get(job_id)

    def status(self):
        marker = self._prepared_marker()
        prepared = self._marker_matches_workspace(marker)
        dashboard_url = self._dashboard_url()
        services = []
        errors = []
        running = False
        conflict = None
        docker = self._docker_status()
        if prepared:
            if not docker or docker.get("state") != "ready":
                errors.append(
                    {
                        "code": (docker or {}).get("code") or "docker_unavailable",
                        "message": (docker or {}).get("message")
                        or _DOCKER_MESSAGES["unavailable"],
                    }
                )
            else:
                try:
                    conflict = self._find_container_conflict()
                    services = self.compose.ps(self.workspace_dir)
                    selected_image = self._ems_image_from_marker(marker)
                    running = _ems_selected_service_running(services, selected_image)
                    if running:
                        # A container already owned by this prepared Compose
                        # project is deployment status, not a name conflict.
                        conflict = None
                except DockerError as exc:
                    error = {"code": exc.code, "message": exc.message}
                    if exc.detail:
                        error["detail"] = exc.detail
                    errors.append(error)
                except Exception:  # status must never raise to the read-only caller
                    errors.append(
                        {
                            "code": "status_failed",
                            "message": "Could not read deployment status.",
                        }
                    )
        dashboard_reachable = self._dashboard_probe(dashboard_url) if running else False
        return {
            "prepared": prepared,
            "running": running,
            "services": services,
            "docker": docker,
            "dashboard_url": dashboard_url,
            "dashboard_reachable": dashboard_reachable,
            "errors": errors,
            "conflict": conflict,
            "runtime_identity": (
                {
                    "puid": marker.get("puid"),
                    "pgid": marker.get("pgid"),
                }
                if isinstance(marker, dict)
                else None
            ),
        }

    def _reject_foreign_marker(self, marker, workflow_id):
        """Refuse a workspace action addressed to another workflow's preparation."""

        if self.setup_workflows is None or not workflow_id:
            return None
        if (
            not isinstance(marker, dict)
            or marker.get("owner") != GENERATED_CONFIG_OWNER
            or marker.get("workflow_id") != workflow_id
        ):
            return _reject(
                "deployment_marker_invalid",
                "The prepared deployment belongs to another setup workflow. "
                "Prepare the deployment again.",
            )
        return None

    def resolve_container_conflict(self, container_name, action, *, workflow_id=None):
        """Resolve a confirmed conflict without removing volumes or user data."""

        rejection = self._reject_foreign_marker(self._prepared_marker(), workflow_id)
        if rejection is not None:
            return rejection
        supported_actions = {
            "remove_stopped_and_continue",
            "replace_running_and_continue",
        }
        if action not in supported_actions:
            return _reject(
                "unsupported_conflict_action",
                "The requested conflict action is not supported.",
                status=400,
            )
        if not isinstance(container_name, str) or container_name not in self._container_names():
            return _reject(
                "unknown_container_name",
                "That container name is not part of the prepared deployment.",
                status=400,
            )
        inspect = getattr(self.docker, "inspect_container", None)
        remove = getattr(self.docker, "remove_container", None)
        stop = getattr(self.docker, "stop_container", None)
        if (
            not callable(inspect)
            or not callable(remove)
            or (action == "replace_running_and_continue" and not callable(stop))
        ):
            return _reject(
                "docker_container_action_unavailable",
                "Container conflict resolution is not available.",
            )
        try:
            existing = inspect(container_name)
        except DockerError as exc:
            return _reject(exc.code, exc.message)
        if existing is None:
            return _reject(
                "container_conflict_changed",
                "The existing container is no longer present. Re-check status and try again.",
            )
        state = str(existing.get("status") or "").lower()
        if action == "remove_stopped_and_continue" and state not in SAFE_STOPPED_CONTAINER_STATES:
            return _reject(
                "container_not_stopped",
                "The existing container is running or otherwise not safe to remove. "
                "The wizard will not stop or replace it automatically.",
            )
        if action == "replace_running_and_continue":
            selected_image = self._selected_image_for_container(container_name)
            existing_image = existing.get("image")
            if state != "running":
                return _reject(
                    "container_conflict_changed",
                    "The container is no longer running. Re-check status and try again.",
                )
            if not isinstance(selected_image, str) or not selected_image:
                return _reject(
                    "selected_image_missing",
                    "The prepared deployment has no selected image for this container. "
                    "Prepare the deployment again.",
                )
            if existing_image == selected_image:
                return _reject(
                    "container_conflict_changed",
                    "The running container now uses the selected image. Re-check status.",
                )
        try:
            if action == "replace_running_and_continue":
                stop(container_name)
            remove(container_name)
        except DockerError as exc:
            result = _reject(exc.code, exc.message)
            if exc.detail:
                result["detail"] = exc.detail
            return result
        with self._operation_lock:
            if action == "replace_running_and_continue":
                message = (
                    f"Replaced running container {container_name} "
                    "(bind mounts and volumes preserved)"
                )
            else:
                message = (
                    f"Removed old stopped container {container_name} "
                    "(volumes preserved)"
                )
            self._pending_resolution_log.append(message)
        next_conflict = self._find_container_conflict()
        return {
            "ok": True,
            "removed": container_name,
            "replaced": action == "replace_running_and_continue",
            "conflict": next_conflict,
            "continue": next_conflict is None,
        }

    def _verify_start_ready(self, *, workflow_id=None):
        docker = self._docker_status()
        if not docker or docker.get("state") != "ready":
            return _reject(
                (docker or {}).get("code") or "docker_unavailable",
                (docker or {}).get("message") or _DOCKER_MESSAGES["unavailable"],
            )
        marker = self._prepared_marker()
        if marker is None:
            return _reject(
                "deployment_not_prepared",
                "Prepare the deployment first before starting EMS.",
            )
        if not self.workspace_dir.is_dir() or not (
            self.workspace_dir / "docker-compose.yml"
        ).is_file():
            return _reject(
                "deployment_workspace_missing",
                "The prepared deployment workspace is missing. Prepare the deployment again.",
            )
        config = self._workspace_config_state()
        if config is None:
            return _reject(
                "generated_config_missing",
                "The prepared deployment has no config. Prepare the deployment again.",
            )
        expected = marker.get("config_sha256")
        if not isinstance(expected, str) or not expected:
            return _reject(
                "deployment_marker_invalid",
                "The deployment preparation marker is invalid. Prepare the deployment again.",
            )
        if marker.get("workspace") != str(self.workspace_dir):
            return _reject(
                "deployment_marker_invalid",
                "The deployment preparation marker does not match this workspace.",
            )
        if self.setup_workflows is not None:
            active = self.setup_workflows.active()
            if (
                active is None
                or marker.get("owner") != GENERATED_CONFIG_OWNER
                or marker.get("workflow_id") != active["workflow_id"]
                or (workflow_id and workflow_id != active["workflow_id"])
            ):
                return _reject(
                    "deployment_marker_invalid",
                    "The deployment preparation belongs to another setup "
                    "workflow. Prepare the deployment again.",
                )
        if expected != config["sha256"]:
            return _reject(
                "deployment_config_mismatch",
                "The prepared config changed. Re-prepare the deployment before starting EMS.",
            )
        identity = self._marker_runtime_identity(marker)
        image = self._ems_image_from_marker(marker)
        if identity is None or image is None:
            return _reject(
                "deployment_marker_invalid",
                "The deployment runtime identity is invalid. Prepare the deployment again.",
            )
        try:
            self._verify_workspace_permissions(image, *identity)
        except DockerError as exc:
            result = _reject(exc.code, exc.message)
            if exc.detail:
                result["detail"] = exc.detail
            return result
        return None

    def _run_start(self, job, context, *, on_healthcheck=None):
        for index, message in enumerate(context["resolution_log"]):
            key = f"resolved_container_conflict_{index}"
            job.start_step(key, message)
            job.finish_step(key)

        job.start_step("checking_deployment", "Checking prepared deployment")
        self._require_carried_workflow(context)
        rejection = self._verify_start_ready(
            workflow_id=(context.get("workflow") or {}).get("workflow_id")
        )
        if rejection is not None:
            raise DockerError(rejection["reason"], rejection["message"])
        self.docker.check()
        conflict = self._find_container_conflict()
        if conflict is not None:
            raise DockerError(
                "compose_container_name_conflict",
                _container_conflict_message(conflict),
                conflict.get("status_detail"),
                conflict,
            )
        job.finish_step("checking_deployment")

        job.start_step("starting_containers", "Starting EMS containers")
        try:
            self.compose.up(self.workspace_dir, profiles=context["profiles"])
        except DockerError as exc:
            if exc.code == "compose_container_name_conflict":
                conflict = self._find_container_conflict(
                    (exc.conflict or {}).get("container_name")
                )
                if conflict is not None:
                    exc.conflict = conflict
                    exc.message = _container_conflict_message(conflict)
            raise
        job.finish_step("starting_containers")

        job.start_step("checking_containers", "Checking container status")
        services = self.compose.ps(self.workspace_dir)
        job.set_services(services)
        if not _ems_service_running(services):
            logs = self.compose.logs(self.workspace_dir)
            if _is_workspace_permission_log(logs):
                raise DockerError(
                    "workspace_permission_denied",
                    WORKSPACE_PERMISSION_MESSAGE,
                    _safe_command_detail(logs),
                )
            raise DockerError(
                "ems_not_running",
                "Docker Compose did not report the EMS service as running.",
            )
        job.finish_step("checking_containers")

        # Container recreation has completed. Hand the durable System Build
        # transition to its explicit health stage before the dashboard probe can
        # retry/sleep, so browser polling can display step 07 live.
        if on_healthcheck is not None:
            on_healthcheck(job.snapshot())
        job.start_step("checking_dashboard", "Checking dashboard availability")
        url = context["dashboard_url"]
        reachable = False
        for attempt in range(self._dashboard_attempts):
            reachable = self._dashboard_probe(url)
            if reachable:
                break
            if attempt + 1 < self._dashboard_attempts:
                self._sleep(self._dashboard_retry_seconds)
        job.set_dashboard(url, reachable)
        job.finish_step(
            "checking_dashboard",
            detail=None if reachable else "Dashboard not reachable yet; it may still be starting.",
        )
        job.succeed()

    def _workspace_config_state(self):
        path = self.workspace_dir / "config" / "config.json"
        try:
            raw = path.read_bytes()
        except OSError:
            return None
        return {"path": str(path), "sha256": hashlib.sha256(raw).hexdigest(), "raw": raw}

    def _container_names(self):
        try:
            text = (self.workspace_dir / "docker-compose.yml").read_text(
                encoding="utf-8"
            )
        except OSError:
            return []
        return list(dict.fromkeys(_CONTAINER_NAME_RE.findall(text)))

    def _selected_image_for_container(self, container_name):
        marker = self._prepared_marker() or {}
        images = marker.get("images")
        service = "influxdb" if "influx" in container_name.lower() else "ems"
        if isinstance(images, list):
            for image in images:
                if isinstance(image, dict) and image.get("service") == service:
                    return image.get("image")
        return None

    def _find_container_conflict(self, preferred_name=None):
        inspect = getattr(self.docker, "inspect_container", None)
        if not callable(inspect):
            return None
        names = self._container_names()
        if preferred_name in names:
            names = [preferred_name] + [name for name in names if name != preferred_name]
        for name in names:
            existing = inspect(name)
            if existing is None:
                continue
            state = str(existing.get("status") or "unknown").lower()
            image = existing.get("image")
            selected_image = self._selected_image_for_container(name)
            if state == "running" and image == selected_image and selected_image:
                continue
            image_mismatch = (
                state == "running"
                and isinstance(image, str)
                and bool(image)
                and isinstance(selected_image, str)
                and bool(selected_image)
                and image != selected_image
            )
            return {
                "type": "container_name_conflict",
                "container_name": name,
                "container_id": existing.get("container_id"),
                "image": image,
                "status": state,
                "status_detail": existing.get("status_detail"),
                "selected_image": selected_image,
                "image_mismatch": image_mismatch,
                "replace_available": image_mismatch,
                "safe_fix_available": state in SAFE_STOPPED_CONTAINER_STATES,
            }
        return None

    def _dashboard_url(self):
        config = self._workspace_config_state()
        port, scheme = 8080, "http"
        if config is not None:
            try:
                parsed = json.loads(config["raw"].decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                parsed = None
            dashboard = parsed.get("dashboard") if isinstance(parsed, dict) else None
            if isinstance(dashboard, dict):
                if (
                    isinstance(dashboard.get("port"), int)
                    and not isinstance(dashboard.get("port"), bool)
                    and 1 <= dashboard["port"] <= 65535
                ):
                    port = dashboard["port"]
                if dashboard.get("ssl_enabled") is True:
                    scheme = "https"
        return f"{scheme}://localhost:{port}"

    def _marker_matches_workspace(self, marker):
        if not isinstance(marker, dict):
            return False
        if marker.get("workspace") != str(self.workspace_dir):
            return False
        expected = marker.get("config_sha256")
        config = self._workspace_config_state()
        return bool(
            isinstance(expected, str)
            and expected
            and config is not None
            and expected == config["sha256"]
            and (self.workspace_dir / "docker-compose.yml").is_file()
            and marker.get("permissions_verified") is True
            and self._marker_runtime_identity(marker) is not None
        )

    def _preparation_ready(self, marker, release, config):
        return bool(
            release
            and config.get("ready")
            and isinstance(marker, dict)
            and marker.get("release") == release["tag"]
            and marker.get("config_sha256") == config.get("sha256")
            and self._marker_matches_workspace(marker)
        )

    def _require_carried_workflow(self, context):
        """Refuse to keep writing for a workflow that is no longer the owner.

        The identity comes from the worker's own context, never from a fresh
        "which workflow is active now" lookup, so a replacement workflow can
        never inherit this worker's writes.
        """

        workflow = context.get("workflow")
        if workflow is None or self.setup_workflows is None:
            return
        active = self.setup_workflows.active()
        if active is None or active["workflow_id"] != workflow["workflow_id"]:
            raise DockerError(
                "setup_workflow_not_active",
                "The setup this deployment belongs to was discarded. Start the "
                "deployment again under the current setup.",
            )

    def _run_prepare(self, job, context):
        release = context["release"]
        config = context["config"]
        images = context["images"]
        puid, pgid = context["puid"], context["pgid"]
        job.set_images(images)
        self._require_carried_workflow(context)

        job.start_step("docker", "Checking Docker…")
        self.docker.check()
        job.finish_step("docker")

        job.start_step("workspace", "Preparing workspace…")
        self._ensure_workspace()
        # Back up an existing live config/compose before any destructive write so
        # the standard install layout is never silently replaced.
        job.note_backups(self._backup_existing_runtime())
        if context["overwrite"]:
            self._reset_for_overwrite()
        # Place the generated config before the installer runs so install-docker.sh
        # keeps it instead of re-running config init.
        self._write_config(config)
        self._write_deployment_env(puid, pgid)
        job.finish_step("workspace")

        job.start_step("bootstrap", "Writing docker-compose.yml (no start)…")
        self.installer.prepare(
            self.workspace_dir,
            release["resource_dir"] / INSTALL_SCRIPT,
            analytics=context["analytics"],
            tag=release["tag"],
            on_line=job.log_line,
        )
        # The installer keeps an existing config.json; re-assert ours to guarantee
        # the deployment uses the wizard-generated config.
        self._write_config(config)
        self._write_deployment_env(puid, pgid)
        job.finish_step("bootstrap")

        for image in images:
            service = image["service"]
            key = f"pull-{service}"
            job.start_step(key, f"Downloading {_image_label(service)} image…")
            job.update_image(service, status="downloading", percent=0)

            def _progress(percent, _line, service=service):
                if percent is not None:
                    job.update_image(service, percent=percent)

            self.docker.pull(image["image"], _progress)
            job.update_image(service, status="done", percent=100)
            job.finish_step(key)

        job.start_step("permissions", "Verifying workspace permissions…")
        ems_image = next(image["image"] for image in images if image["service"] == "ems")
        self._repair_workspace(ems_image, puid, pgid)
        self._verify_workspace_permissions(ems_image, puid, pgid)
        job.finish_step("permissions")

        self._require_carried_workflow(context)
        marker = self._write_marker(
            release, config, images, puid, pgid, workflow=context.get("workflow")
        )
        job.succeed(marker)

    # --- workspace helpers ----------------------------------------------

    def _ensure_workspace(self):
        (self.workspace_dir / "config").mkdir(parents=True, exist_ok=True)
        (self.workspace_dir / "data").mkdir(parents=True, exist_ok=True)

    def _backup_existing_runtime(self):
        """Copy an existing live config.json/docker-compose.yml to the Admin
        backup directory before a prepare overwrites them."""

        backups = []
        for target in (
            self.workspace_dir / "config" / "config.json",
            self.workspace_dir / "docker-compose.yml",
        ):
            if target.is_file():
                backups.append(self._backup_file(target))
        return backups

    def _backup_file(self, target):
        stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
        candidate = self.backup_dir / f"{target.name}.{stamp}.bak"
        counter = 1
        while candidate.exists():
            candidate = self.backup_dir / f"{target.name}.{stamp}-{counter}.bak"
            counter += 1
        _atomic_write(candidate, Path(target).read_bytes())
        return candidate

    def _reset_for_overwrite(self):
        # Drop generated scaffold so the installer regenerates it for the current
        # tag/analytics; the generated config is re-placed afterwards.
        for name in ("docker-compose.yml", ".env"):
            try:
                (self.workspace_dir / name).unlink()
            except FileNotFoundError:
                pass

    def _write_config(self, config):
        # Copy the exact generated bytes to preserve key order and formatting.
        source = Path(config["path"])
        _atomic_write(self.workspace_dir / "config" / "config.json", source.read_bytes())

    def _write_deployment_env(self, puid, pgid):
        path = self.workspace_dir / ".env"
        values = _read_env_file(path)
        values["PUID"] = str(puid)
        values["PGID"] = str(pgid)
        text = "".join(f"{key}={value}\n" for key, value in values.items())
        _atomic_write(path, text.encode("utf-8"))

    def _write_marker(self, release, config, images, puid, pgid, *, workflow=None):
        marker = {
            "release": release["tag"],
            "config_sha256": config["sha256"],
            # Marker ownership: the identity the authorized worker carries, so a
            # start can refuse a marker another (superseded) workflow prepared
            # and cleanup can prove the marker is this workflow's to remove.
            "owner": GENERATED_CONFIG_OWNER if workflow else None,
            "workflow_id": (workflow or {}).get("workflow_id"),
            "preview_id": (workflow or {}).get("preview_id"),
            "images": [
                {"service": image["service"], "image": image["image"]}
                for image in images
            ],
            "workspace": str(self.workspace_dir),
            "puid": puid,
            "pgid": pgid,
            "permissions_verified": True,
            "prepared_at": utc_now_iso(),
        }
        _atomic_write(
            self.marker_path,
            (json.dumps(marker, indent=2, sort_keys=True) + "\n").encode("utf-8"),
        )
        return marker

    def _resolve_runtime_identity(self):
        env_values = _read_env_file(self.workspace_dir / ".env")
        candidates = (
            (env_values.get("PUID"), env_values.get("PGID")),
            (self._runtime_env.get("PUID"), self._runtime_env.get("PGID")),
            (self._runtime_env.get("HOST_UID"), self._runtime_env.get("HOST_GID")),
        )
        for puid, pgid in candidates:
            identity = _valid_runtime_identity(puid, pgid)
            if identity is not None:
                return identity
        return None

    @staticmethod
    def _marker_runtime_identity(marker):
        if not isinstance(marker, dict):
            return None
        return _valid_runtime_identity(marker.get("puid"), marker.get("pgid"))

    @staticmethod
    def _ems_image_from_marker(marker):
        if not isinstance(marker, dict):
            return None
        for image in marker.get("images") or []:
            if isinstance(image, dict) and image.get("service") == "ems":
                value = image.get("image")
                return value if isinstance(value, str) and value else None
        return None

    def _verify_workspace_permissions(self, image, puid, pgid):
        check = getattr(self.docker, "check_workspace_permissions", None)
        if not callable(check):
            raise DockerError(
                "workspace_permission_check_unavailable",
                "Docker cannot verify deployment workspace permissions.",
            )
        check(self.workspace_dir, image, puid, pgid)

    def _repair_workspace(self, image, puid, pgid):
        repair = getattr(self.docker, "repair_workspace_permissions", None)
        if not callable(repair):
            raise DockerError(
                "workspace_permission_repair_unavailable",
                "Docker cannot repair deployment workspace permissions.",
            )
        repair(self.workspace_dir, image, puid, pgid)

    def _existing_install_conflict(self, overwrite):
        """Refuse to replace an existing standard EMS install without explicit
        confirmation. An install already owned by a matching Admin marker is
        updateable; a manual/unknown install must be confirmed first."""

        if overwrite:
            return None
        existing = self._existing_install_state()
        if not existing["present"]:
            return None
        if self._marker_matches_workspace(self._prepared_marker()):
            return None
        conflict = _reject(
            "existing_install_conflict",
            "Existing EMS installation detected.",
            status=409,
        )
        conflict["paths"] = {
            "config": str(self.workspace_dir / "config" / "config.json"),
            "compose": str(self.workspace_dir / "docker-compose.yml"),
            "data": str(self.workspace_dir / "data"),
        }
        conflict["existing"] = {
            "config": existing["config_exists"],
            "compose": existing["compose_exists"],
        }
        conflict["requires_confirmation"] = True
        return conflict

    def _workspace_conflict(self, release, config, overwrite):
        if overwrite:
            return None
        marker = self._prepared_marker()
        if marker is None:
            return None
        same = (
            marker.get("release") == release["tag"]
            and marker.get("config_sha256") == config["sha256"]
        )
        if same:
            return None
        return _reject(
            "workspace_conflict",
            "The deployment workspace was prepared for a different release or "
            "config. Confirm overwrite to replace it.",
            status=409,
        )

    def _prepared_marker(self):
        try:
            raw = self.marker_path.read_bytes()
        except OSError:
            return None
        if len(raw) > MAX_MARKER_BYTES:
            return None
        try:
            marker = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return None
        return marker if isinstance(marker, dict) else None

    # --- resolution helpers ---------------------------------------------

    def _release_context(self):
        try:
            resource = self.release_manager.config_template()
        except ReleaseError:
            return None
        tag = resource.get("tag")
        if not tag:
            return None
        resource_dir = Path(self.release_manager.releases_dir) / tag
        if not (resource_dir / INSTALL_SCRIPT).is_file():
            return None
        return {
            "tag": tag,
            "ems_image": resource.get("docker_image")
            or f"{DOCKER_IMAGE_REPOSITORY}:{tag}",
            "resource_dir": resource_dir,
        }

    def _generated_config_state(self):
        path = self.config_export.target_path
        try:
            raw = Path(path).read_bytes()
        except (OSError, ValueError):
            return {"ready": False, "path": str(path), "sha256": None, "config": None}
        try:
            config = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return {"ready": False, "path": str(path), "sha256": None, "config": None}
        return {
            "ready": isinstance(config, dict),
            "path": str(path),
            "sha256": hashlib.sha256(raw).hexdigest(),
            "config": config if isinstance(config, dict) else None,
        }

    def _generated_config_rejection(self, config):
        """Refuse a generated config that is unowned, foreign, tampered or stale.

        Ownership first: the metadata sidecar must name the active Guided Setup
        workflow, its exact preview and the payload hash of the generated
        bytes. A legacy sidecar-less artifact — or one from an abandoned or
        superseded workflow — requires regeneration under the active workflow
        instead of silently keeping the pre-ownership deploy path. Then
        freshness: presence is part of the revision state, so a live config
        deleted after an existing-config draft, and one that appeared after a
        fresh-install draft, are both changes. A live config equal to the
        generated bytes is this same config being redeployed. Not bypassed by
        ``overwrite``, which confirms replacing an install, not discarding an
        unseen change.
        """

        meta = read_generated_metadata(self.config_export.target_path)
        review = self._generated_config_ownership_rejection(meta, config)
        if review is not None:
            return review
        base = meta["base_config_revision"]
        try:
            live = (self.workspace_dir / "config" / "config.json").read_bytes()
        except OSError:
            unchanged = base["expect_absent"]
        else:
            live_revision = hashlib.sha256(live).hexdigest()
            unchanged = live_revision in (
                base["expected_revision"],
                config["sha256"],
            )
        if unchanged:
            return None
        return _reject(
            "stale_generated_config",
            "config/config.json changed after this configuration was generated. "
            "Review the configuration again before deploying it.",
        )

    def _generated_config_ownership_rejection(self, meta, config):
        """409 payload unless ``meta`` proves the active workflow's ownership."""

        review = _reject(
            "generated_config_review_required",
            "This generated configuration cannot prove which setup workflow "
            "reviewed it. Review the configuration and generate it again "
            "before deploying.",
        )
        if not isinstance(meta, dict):
            return review
        workflow_id = meta.get("workflow_id")
        preview_id = meta.get("preview_id")
        prepared_sha256 = meta.get("prepared_config_sha256")
        base = meta.get("base_config_revision")
        if not (
            isinstance(workflow_id, str)
            and workflow_id
            and isinstance(preview_id, str)
            and preview_id
            and isinstance(prepared_sha256, str)
            and isinstance(base, dict)
            and isinstance(base.get("expect_absent"), bool)
        ):
            return review
        if self.setup_workflows is None:
            return review
        record = self.setup_workflows.active()
        if record is None or record["workflow_id"] != workflow_id:
            return review
        # The durable write-time binding, not whichever preview is current now:
        # revisiting Config Preview issues newer previews and must not disown an
        # artifact that was legitimately generated earlier in this workflow.
        if (record.get("artifacts") or {}).get("generated_preview_id") != preview_id:
            return review
        if config["sha256"] != prepared_sha256:
            return review
        return None

    def _influx_plan(self, release, config):
        generated = config.get("config") if config else None
        influx = generated.get("influxdb") if isinstance(generated, dict) else None
        enabled = isinstance(influx, dict) and influx.get("enabled") is True
        bundled = enabled and influx.get("mode") == "bundled"
        image = self._influx_image(release) if bundled and release else None
        if not config or not config.get("ready"):
            reason = "Save a generated config to plan Analytics."
        elif not enabled:
            reason = "InfluxDB: not enabled in generated config"
        elif not bundled:
            reason = "InfluxDB Analytics uses an external instance; nothing to pull."
        elif image is None:
            reason = "InfluxDB image could not be read from the release resources."
        else:
            reason = None
        return {
            "enabled": enabled,
            "bundled": bundled,
            "planned": bool(bundled and image),
            "image": image,
            "reason": reason,
        }

    def _influx_image(self, release):
        if not release:
            return None
        path = release["resource_dir"] / INFLUX_COMPOSE_RESOURCE
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            return None
        match = _INFLUX_IMAGE_RE.search(text)
        return match.group(1) if match else None

    def _planned_images(self, release, influx):
        if not release:
            return []
        images = [{"service": "ems", "image": release["ems_image"]}]
        if influx.get("planned") and influx.get("image"):
            images.append({"service": "influxdb", "image": influx["image"]})
        return images


def _marker_bundles_influx(marker):
    if not isinstance(marker, dict):
        return False
    images = marker.get("images")
    if not isinstance(images, list):
        return False
    return any(
        isinstance(image, dict) and image.get("service") == "influxdb" for image in images
    )


def _ems_service_running(services):
    return any(
        isinstance(service, dict)
        and service.get("state") == "running"
        and (
            service.get("service") == "ems"
            or (
                not service.get("service")
                and "ems" in str(service.get("name") or "").lower()
            )
        )
        for service in services
    )


def _ems_selected_service_running(services, selected_image):
    if not isinstance(selected_image, str) or not selected_image:
        return False
    return any(
        isinstance(service, dict)
        and service.get("state") == "running"
        and service.get("image") == selected_image
        and (
            service.get("service") == "ems"
            or (
                not service.get("service")
                and "ems" in str(service.get("name") or "").lower()
            )
        )
        for service in services
    )


def _container_conflict_message(conflict):
    if conflict.get("replace_available"):
        return (
            "EMS is running with a different image. Confirm replacement to start "
            "the selected release."
        )
    if conflict.get("status") == "running":
        return (
            "EMS is already running. A running container already uses this name. "
            "The wizard will not stop or replace it automatically."
        )
    return (
        "A stopped EMS container already uses this name and blocks the selected "
        "release. Confirm removal to continue."
    )


def _admin_data_dir(release_manager):
    return Path(getattr(release_manager, "data_dir", None) or default_admin_data_dir())


def _image_label(service):
    return "InfluxDB" if service == "influxdb" else "EMS"


def _reject(code, message, status=409):
    return {"ok": False, "reason": code, "message": message, "status": status}


def _atomic_write(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    with open(temporary, "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
