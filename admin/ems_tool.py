# SPDX-License-Identifier: AGPL-3.0-or-later
"""Run internal allowlisted ``emsctl.py`` commands for Guided Upgrade.

Admin orchestrates; EMS Core / ``emsctl.py`` is the tool source. The runner
picks a mode against the current install: ``container`` (docker exec into the
running EMS), ``compose`` (one-off ``docker compose run``), or ``blocked`` when
neither exists. Args are always an internal allowlisted argv suffix; never
``shell=True``.
"""

import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from admin.deployment import DockerCli, DockerCompose, DockerError, _safe_command_detail
from admin.container_names import DEFAULT_EMS_CONTAINER

# Container paths inside the published EMS image.
CONTAINER_EMSCTL_PATH = "/app/emsctl.py"
CONTAINER_CONFIG_DIR = "/app/config"
CONTAINER_DATA_DIR = "/app/data"

# The compose service name of the EMS container in the standard install.
EMS_COMPOSE_SERVICE = "ems"

# Default timeout (seconds) for a normal EMS tool command.
DEFAULT_TIMEOUT = 180

# Long-running backup/restore jobs may stream large InfluxDB archives.
BACKUP_RESTORE_TIMEOUT = 1800

# Docker's ``exec`` cleanup lives in the client, so a command killed by its
# ceiling here keeps running inside the container. Measured on Docker 26.1.5: a
# killed ``docker exec`` left its process alive, and the next attempt ran beside
# the first. A restore is not idempotent against a second copy of itself, and
# there is no name to address an exec by afterwards the way a one-off container
# has one, so the ceiling travels into the container with the command.
EXEC_GUARD = "timeout"

# The guard gets the ceiling the caller asked for; this process waits past it,
# so the guard is what ends a slow command and the client only steps in when the
# guard itself could not.
EXEC_CLIENT_GRACE_SECONDS = 15

# A command that ignores the stop signal still has to end.
EXEC_GUARD_KILL_AFTER_SECONDS = 10

# GNU timeout reports its own kill instead of the command's status: 124 after
# the signal, 137 when the kill had to follow.
EXEC_GUARD_TIMED_OUT_CODES = (124, 137)

# Docker's exit code for "the command could not be invoked at all".
EXEC_NOT_INVOCABLE_CODE = 126

EXEC_GUARD_TIMEOUT_DETAIL = (
    "The EMS command timed out and was stopped inside the container."
)
EXEC_CLIENT_TIMEOUT_DETAIL = (
    "The EMS command timed out and may still be running inside the container; "
    "check the container before trying again."
)

BLOCKED_MESSAGE = (
    "No running EMS container and no Docker Compose context were found, so the "
    "EMS command could not be run against the installed system."
)

_COMPOSE_EMS_NAME_RE = re.compile(
    r"^\s*container_name:\s*[\"']?([A-Za-z0-9][A-Za-z0-9_.-]*)[\"']?\s*(?:#.*)?$",
    re.MULTILINE,
)

# Friendly, UI-safe descriptions of the EMS tool context a command ran in.
# Callers use these to report which context was used without exposing argv.
MODE_DETAILS = {
    "container": "via running EMS container",
    "compose": "via compose one-off EMS container",
}


def mode_detail(mode):
    """Friendly, UI-safe description of an EMS tool mode, or ``None``."""

    return MODE_DETAILS.get(mode)


def guarded_container_command(command, timeout):
    """``command`` wrapped so the container enforces ``timeout`` itself.

    Shared with :mod:`admin.ems_cli`, which runs its checks through the same
    ``docker exec`` and inherits the same orphan otherwise.
    """

    # GNU timeout reads a zero duration as "no timeout at all", so a ceiling
    # that rounds down to nothing would silently disarm the guard.
    return [
        EXEC_GUARD,
        f"--kill-after={EXEC_GUARD_KILL_AFTER_SECONDS}",
        "--signal=TERM",
        str(max(1, int(timeout or 0))),
        *command,
    ]


def guard_stopped_command(returncode, elapsed, timeout):
    """True when the guard's exit code can only mean it ended the command.

    ``docker exec`` returns 137 whenever anything kills the command -- the OOM
    killer, an operator, the container going away -- so the code alone would
    report a backup killed after forty seconds as a timeout on a thirty-minute
    budget, and send the operator to raise a ceiling that was never reached. A
    command the guard stopped has necessarily run for its whole deadline.
    """

    return returncode in EXEC_GUARD_TIMED_OUT_CODES and elapsed >= (timeout or 0)


def guard_not_invocable(result):
    """True when Docker could not invoke the guard itself.

    The message names the binary Docker could not find, which is what keeps a
    missing interpreter from being read as a missing guard. Docker writes it to
    stdout on some versions and stderr on others, so both are searched.
    """

    if result.returncode != EXEC_NOT_INVOCABLE_CODE:
        return False
    streams = " ".join(
        part for part in (result.stdout, result.stderr) if part
    )
    return f'"{EXEC_GUARD}"' in streams and "not found" in streams


@dataclass(frozen=True)
class EmsToolResult:
    """One EMS tool command outcome.

    ``mode`` is ``container``/``compose``/``blocked``. ``blocked`` is True only
    when no EMS context exists (no running container and no compose file).
    ``returncode`` is the command exit code, or ``None`` when it never ran
    (timeout/spawn error). ``detail`` is UI-safe command output; ``message`` is
    the friendly blocked message (only set when ``blocked``).
    """

    mode: str
    blocked: bool
    returncode: int | None
    detail: str | None
    message: str | None


def ems_container_name(context):
    """EMS container name from ``context``'s compose file, else the default."""

    if not getattr(context, "compose_exists", False):
        return DEFAULT_EMS_CONTAINER
    try:
        text = Path(context.compose_path).read_text(encoding="utf-8")
    except OSError:
        return DEFAULT_EMS_CONTAINER
    for name in _COMPOSE_EMS_NAME_RE.findall(text):
        if "influx" not in name.lower():
            return name
    return DEFAULT_EMS_CONTAINER


def resolve_running_ems_container(docker, context):
    """Running EMS container name for ``context``, or ``None``.

    Shared with :mod:`admin.ems_cli`; any Docker probe/inspect failure degrades
    to "no running container".
    """

    probe = getattr(docker, "probe", None)
    inspect = getattr(docker, "inspect_container", None)
    if not callable(probe) or not callable(inspect):
        return None
    try:
        state = probe()
    except Exception:  # host Docker state must never break mode selection
        return None
    if not state or state.get("state") != "ready":
        return None
    name = ems_container_name(context)
    try:
        existing = inspect(name)
    except Exception:  # a failed inspect degrades to no container
        return None
    if existing is None:
        return None
    if str(existing.get("status") or "").lower() != "running":
        return None
    return existing.get("container_name") or name


class EmsToolRunner:
    """Run allowlisted ``emsctl.py`` commands via the best EMS context.

    Injectable for tests (fake docker/compose/run).
    """

    def __init__(self, docker=None, compose=None, run=None):
        self._docker = docker or DockerCli()
        self._compose = compose or DockerCompose()
        self._run = run or subprocess.run

    def resolve_mode(self, context):
        """Return the best EMS tool mode for a *current-install* command."""

        container = resolve_running_ems_container(self._docker, context)
        if container is not None:
            return {"mode": "container", "container": container}
        if getattr(context, "compose_exists", False):
            return {"mode": "compose", "workspace": Path(context.install_root)}
        return {"mode": "blocked"}

    def run(self, context, args, timeout=DEFAULT_TIMEOUT, input_text=None):
        """Run ``emsctl.py <args>`` against the current install.

        ``args`` is an internal allowlisted argv suffix, never frontend input.
        ``input_text``, when set, is written to the command's stdin (used to feed
        a backup password to a non-interactive restore). It is never placed in
        argv and never logged.
        """

        mode = self.resolve_mode(context)
        if mode["mode"] == "container":
            return self._exec_in_container(mode["container"], args, timeout, input_text)
        if mode["mode"] == "compose":
            return self._run_via_compose(mode["workspace"], args, timeout, input_text)
        return EmsToolResult("blocked", True, None, None, BLOCKED_MESSAGE)

    def build_target_image_command(self, context, target_image, args):
        """Build a ``docker run --rm <target-image>`` command with host config/
        and data/ bind-mounted. Returns the argv; does not execute it.
        """

        config_dir = Path(context.config_path).parent
        data_dir = Path(context.data_dir)
        return [
            "docker",
            "run",
            "--rm",
            "--mount",
            f"type=bind,src={config_dir},dst={CONTAINER_CONFIG_DIR}",
            "--mount",
            f"type=bind,src={data_dir},dst={CONTAINER_DATA_DIR}",
            str(target_image),
            "python3",
            CONTAINER_EMSCTL_PATH,
            *[str(part) for part in args],
        ]

    # --- execution -------------------------------------------------------

    def _exec_in_container(self, container, args, timeout, input_text=None):
        command = ["python3", CONTAINER_EMSCTL_PATH, *[str(part) for part in args]]
        guarded = guarded_container_command(command, timeout)
        started = time.monotonic()
        bounded = True
        try:
            result = self._exec(
                container, guarded, timeout + EXEC_CLIENT_GRACE_SECONDS, input_text
            )
            if guard_not_invocable(result):
                # An image without the guard would otherwise lose backup and
                # restore outright, which is the worse of the two failures.
                bounded = False
                result = self._exec(container, command, timeout, input_text)
        except subprocess.TimeoutExpired:
            return EmsToolResult(
                "container", False, None, EXEC_CLIENT_TIMEOUT_DETAIL, None
            )
        except FileNotFoundError:
            return EmsToolResult(
                "container", False, None, "The docker command is not available.", None
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return EmsToolResult(
                "container", False, None, f"Could not run the EMS command: {exc}", None
            )
        if bounded and guard_stopped_command(
            result.returncode, time.monotonic() - started, timeout
        ):
            return EmsToolResult(
                "container", False, None, EXEC_GUARD_TIMEOUT_DETAIL, None
            )
        detail = _safe_command_detail(
            "\n".join(part for part in (result.stdout, result.stderr) if part)
        )
        return EmsToolResult("container", False, int(result.returncode), detail, None)

    def _exec(self, container, command, timeout, input_text):
        argv = ["docker", "exec"]
        if input_text is not None:
            argv.append("-i")  # keep stdin open so a password can be piped in
        argv.append(container)
        argv += command
        return self._run(
            argv, capture_output=True, text=True, timeout=timeout, input=input_text,
        )

    def _run_via_compose(self, workspace, args, timeout, input_text=None):
        command = ["python3", "emsctl.py", *[str(part) for part in args]]
        kwargs = {"timeout": timeout}
        if input_text is not None:
            kwargs["input_text"] = input_text
        try:
            returncode, detail = self._compose.run_oneoff(
                str(workspace), EMS_COMPOSE_SERVICE, command, **kwargs
            )
        except DockerError as exc:
            return EmsToolResult("compose", False, None, exc.message, None)
        return EmsToolResult("compose", False, int(returncode), detail, None)
