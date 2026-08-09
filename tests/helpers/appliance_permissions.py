# SPDX-License-Identifier: AGPL-3.0-or-later
"""A disposable host that reproduces the packaged appliance ownership layout.

The FakeHost suite owns every file it writes, so it can never see a permission
defect. This helper creates the real service accounts and the real directory
ownership a ``.deb`` installation produces, and then runs appliance code as the
actual unprivileged web account. What fails here fails on a Raspberry Pi.
"""

import json
import shutil
import subprocess
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

DEFAULT_IMAGE = "debian:trixie-slim"

WEB_USER = "ems-appliance-web"
APPLIANCE_GROUP = "ems-appliance"
STATE_DIR = "/var/lib/ems-appliance-manager"
LOG_DIR = "/var/log/ems-appliance-manager"
RUNTIME_DIR = "/run/ems-appliance-manager"
AUDIT_DIR = f"{LOG_DIR}/audit"
AUDIT_LOG = f"{AUDIT_DIR}/audit.log"
AGENT_STATE_DIR = f"{STATE_DIR}/agent"
WEB_LOG = f"{LOG_DIR}/web/appliance.log"

# Run as a module from the repository root: ``tests/helpers/appliance.py`` would
# otherwise shadow the real ``appliance`` package on sys.path.
DRIVER_MODULE = "tests.helpers.appliance_web_driver"

_PROVISION = f"""
set -eu
apt-get update -qq >/dev/null 2>&1
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq python3 procps >/dev/null 2>&1
getent group {APPLIANCE_GROUP} >/dev/null || groupadd --system {APPLIANCE_GROUP}
getent passwd {WEB_USER} >/dev/null || useradd --system --gid {APPLIANCE_GROUP} \
    --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin {WEB_USER}
"""


def docker_available():
    if not shutil.which("docker"):
        return False
    result = subprocess.run(
        ["docker", "info", "--format", "{{.ServerVersion}}"],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    return result.returncode == 0


class PermissionHost:
    """A container with the packaged accounts, used to run appliance code as them."""

    def __init__(self, *, image=DEFAULT_IMAGE, name=None):
        self.image = image
        self.name = name or f"ems-appliance-perm-{uuid.uuid4().hex[:10]}"
        self.started = False

    # --- lifecycle -------------------------------------------------------

    def start(self):
        result = subprocess.run(
            [
                "docker", "run", "-d", "--name", self.name,
                "-v", f"{ROOT}:/src:ro",
                "-e", "PYTHONDONTWRITEBYTECODE=1",
                self.image, "sleep", "infinity",
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=300,
        )
        if result.returncode != 0:
            raise RuntimeError(f"cannot start the permission host: {result.stderr.strip()}")
        self.started = True
        provision = self.shell(_PROVISION, timeout=900)
        if provision.returncode != 0:
            self.stop()
            raise RuntimeError(f"cannot provision the permission host: {provision.stderr}")
        return self

    def stop(self):
        if not self.started:
            return
        subprocess.run(
            ["docker", "rm", "-f", self.name], capture_output=True, check=False, timeout=120
        )
        self.started = False

    def __enter__(self):
        return self.start()

    def __exit__(self, *_):
        self.stop()

    # --- commands --------------------------------------------------------

    def run(self, argv, *, user=None, timeout=300, env=None):
        command = ["docker", "exec"]
        if user:
            command += ["--user", user]
        for key, value in (env or {}).items():
            command += ["--env", f"{key}={value}"]
        command += [self.name, *argv]
        return subprocess.run(
            command, capture_output=True, text=True, check=False, timeout=timeout
        )

    def shell(self, script, *, user=None, timeout=300, env=None):
        return self.run(["/bin/sh", "-c", script], user=user, timeout=timeout, env=env)

    def read(self, path):
        result = self.run(["cat", path], timeout=60)
        return result.stdout if result.returncode == 0 else ""

    def stat(self, path):
        result = self.run(["stat", "-c", "%a %U %G", path], timeout=60)
        if result.returncode != 0:
            return None
        mode, owner, group = result.stdout.strip().split()
        return {"mode": mode, "owner": owner, "group": group}

    def exists(self, path):
        return self.run(["test", "-e", path], timeout=60).returncode == 0

    # --- packaged layout --------------------------------------------------

    def apply_layout(self, rules):
        """Create the appliance state layout from ``(path, owner, group, mode)`` rules."""

        self.shell(f"rm -rf {STATE_DIR} {LOG_DIR} {RUNTIME_DIR}", timeout=120)
        script = "set -eu\n"
        for path, owner, group, mode in rules:
            script += f"install -d -o {owner} -g {group} -m {mode} {path}\n"
        result = self.shell(script, timeout=180)
        if result.returncode != 0:
            raise RuntimeError(f"cannot apply the appliance layout: {result.stderr}")
        return self

    def reset_state(self, rules):
        self.apply_layout(rules)

    # --- privileged agent -------------------------------------------------

    def start_agent(self, *, attempts=30):
        """Run the real agent as root so the audit log gets an authoritative writer."""

        self.stop_agent()
        self.shell(
            "cd /src && nohup python3 -m appliance agent >/tmp/agent.out 2>&1 & sleep 1",
            timeout=120,
        )
        for _ in range(attempts):
            if self.exists(f"{RUNTIME_DIR}/agent.sock"):
                return True
            self.run(["sleep", "1"], timeout=30)
        raise RuntimeError(f"the appliance agent did not start:\n{self.read('/tmp/agent.out')}")

    def stop_agent(self):
        # The bracket keeps the pattern from matching this shell's own command
        # line, which would kill the shell instead of the agent.
        self.shell("pkill -f 'appliance [a]gent' >/dev/null 2>&1; sleep 1", timeout=60)

    def agent_output(self):
        return self.read("/tmp/agent.out")

    # --- appliance driver -------------------------------------------------

    def drive_web(self, scenario, *, user=WEB_USER, extra=None, timeout=300):
        """Run the web service as ``user`` and return the driver's JSON report."""

        arguments = " ".join([scenario, *(extra or [])])
        result = self.shell(
            f"cd /src && exec python3 -m {DRIVER_MODULE} {arguments}",
            user=user,
            timeout=timeout,
            env={"PYTHONDONTWRITEBYTECODE": "1"},
        )
        marker = "APPLIANCE_REPORT:"
        for line in result.stdout.splitlines():
            if line.startswith(marker):
                return json.loads(line[len(marker) :])
        raise AssertionError(
            f"the web driver produced no report\nstdout: {result.stdout}\nstderr: {result.stderr}"
        )


def packaged_layout(*, agent_private):
    """The directory ownership a packaged installation produces.

    ``agent_private`` selects the Phase 3 model (agent state readable by root
    only) over the earlier group-readable one.
    """

    agent_owner = ("root", "root", "0700") if agent_private else ("root", APPLIANCE_GROUP, "0750")
    rules = [
        (STATE_DIR, "root", APPLIANCE_GROUP, "0750"),
        (LOG_DIR, "root", APPLIANCE_GROUP, "0750"),
        (f"{STATE_DIR}/web", WEB_USER, APPLIANCE_GROUP, "0750"),
        (f"{STATE_DIR}/web/auth", WEB_USER, APPLIANCE_GROUP, "0700"),
        (f"{STATE_DIR}/web/sessions", WEB_USER, APPLIANCE_GROUP, "0700"),
        (f"{STATE_DIR}/web/ui-preferences", WEB_USER, APPLIANCE_GROUP, "0750"),
        (f"{LOG_DIR}/web", WEB_USER, APPLIANCE_GROUP, "0750"),
    ]
    for directory in (
        AGENT_STATE_DIR,
        f"{AGENT_STATE_DIR}/operations",
        f"{AGENT_STATE_DIR}/known-good",
        f"{AGENT_STATE_DIR}/compose-backup",
        f"{AGENT_STATE_DIR}/package-state",
        f"{AGENT_STATE_DIR}/recovery",
        f"{AGENT_STATE_DIR}/ssh-keys",
        f"{AGENT_STATE_DIR}/support",
        f"{AGENT_STATE_DIR}/packages",
        f"{LOG_DIR}/agent",
        AUDIT_DIR,
    ):
        rules.append((directory, *agent_owner))
    return tuple(rules)
