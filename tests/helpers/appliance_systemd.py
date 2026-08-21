# SPDX-License-Identifier: AGPL-3.0-or-later
"""A real systemd host for Appliance Manager package tests.

The FakeHost suite proves the Python behaviour; this helper proves the packaged
reality: the ``.deb`` installs, the shipped units load under a real systemd, the
sandbox actually permits what the agent needs and the web account really can (or
cannot) reach what the ownership model claims.

A privileged Debian container with systemd as PID 1 is used instead of a full
VM so the check runs on a normal developer machine and in CI. It is a genuine
systemd instance — units are started by systemd, not simulated.
"""

import json
import shlex
import shutil
import subprocess
import uuid
from pathlib import Path

from appliance.config import DEFAULT_WEB_PORT

ROOT = Path(__file__).resolve().parents[2]
BUILD_SCRIPT = ROOT / "packaging" / "appliance" / "build-deb.sh"

DEFAULT_IMAGE = "debian:trixie-slim"
BOOT_TIMEOUT = 120
AGENT_UNIT = "ems-appliance-agent.service"
WEB_UNIT = "ems-appliance-web.service"

RUNTIME_DIR = "/run/ems-appliance-manager"
SOCKET_PATH = f"{RUNTIME_DIR}/agent.sock"
STATE_DIR = "/var/lib/ems-appliance-manager"
LOG_DIR = "/var/log/ems-appliance-manager"

WEB_USER = "ems-appliance-web"
APPLIANCE_GROUP = "ems-appliance"
BACKUP_USER = "ems-backup"
UNRELATED_USER = "ems-outsider"

EXPORT_ROOT = "/srv/ems-appliance-export"
EXPORT_NAMES = ("config", "backups", "data")
BACKUP_KEY = "/root/appliance-backup-key"
INSTALL_ROOT = "/opt/ems-solarflow"

APPLIANCE_CONF = "/etc/ems-appliance-manager/appliance.conf"
RELEASE_INDEX_DIR = "/srv/release-index"
RELEASE_INDEX_PORT = 18088
LOCAL_APT_DIR = "/srv/local-apt"
SMOKE_PASSWORD = "packaged-smoke-password"
HTTP_CLIENT = Path(__file__).with_name("appliance_http_client.py")


class SystemdUnavailable(RuntimeError):
    """The environment cannot host a real systemd instance."""


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


def build_package(destination, *, architecture="amd64"):
    """Build the real ``.deb`` for the local architecture."""

    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        [str(BUILD_SCRIPT), "--output", str(destination), "--arch", architecture],
        capture_output=True,
        text=True,
        check=False,
        timeout=300,
    )
    if result.returncode != 0:
        raise RuntimeError(f"package build failed: {result.stdout}\n{result.stderr}")
    packages = sorted(destination.glob("*.deb"))
    if not packages:
        raise RuntimeError("package build produced no .deb")
    return packages[-1]


def repack_with_version(package, version, destination):
    """Repack an existing .deb under a higher version for upgrade tests."""

    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    staging = destination / "staging"
    if staging.exists():
        shutil.rmtree(staging)
    subprocess.run(["dpkg-deb", "-R", str(package), str(staging)], check=True, timeout=180)
    control = staging / "DEBIAN" / "control"
    text = control.read_text(encoding="utf-8")
    control.write_text(
        "\n".join(
            f"Version: {version}" if line.startswith("Version:") else line
            for line in text.splitlines()
        )
        + "\n",
        encoding="utf-8",
    )
    target = destination / f"ems-appliance-manager_{version}_amd64.deb"
    subprocess.run(
        ["dpkg-deb", "--root-owner-group", "--build", str(staging), str(target)],
        check=True,
        capture_output=True,
        timeout=180,
    )
    return target


REQUIRED_PACKAGES = ("systemd", "systemd-sysv", "adduser", "python3")
PREREQUISITE_MARKER = "/run/ems-appliance-prerequisites-missing"


class SystemdContainer:
    """A booted Debian container used as a disposable appliance host."""

    def __init__(self, *, image=DEFAULT_IMAGE, name=None):
        self.image = image
        self.name = name or f"ems-appliance-test-{uuid.uuid4().hex[:10]}"
        self.started = False

    # --- lifecycle -------------------------------------------------------

    def start(self):
        if not docker_available():
            raise SystemdUnavailable("docker is not available")
        # The failure is recorded rather than swallowed: without these packages
        # the container boots into something that is not an appliance host, and
        # every check above it then fails for reasons that never name apt.
        boot = (
            "{ apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install "
            f"-y -qq {' '.join(REQUIRED_PACKAGES)}; }} >/dev/null 2>&1 "
            f"|| touch {PREREQUISITE_MARKER}; "
            "exec /lib/systemd/systemd"
        )
        result = subprocess.run(
            [
                "docker", "run", "-d", "--name", self.name,
                "--privileged", "--cgroupns=host",
                "-v", "/sys/fs/cgroup:/sys/fs/cgroup:rw",
                "--tmpfs", "/run", "--tmpfs", "/run/lock",
                self.image, "/bin/bash", "-c", boot,
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=300,
        )
        if result.returncode != 0:
            raise SystemdUnavailable(f"cannot start a systemd container: {result.stderr.strip()}")
        self.started = True
        self._await_boot()
        self._require_prerequisites()
        return self

    def _require_prerequisites(self):
        marker = self.run(["test", "-e", PREREQUISITE_MARKER], timeout=30)
        if marker.returncode == 0:
            self.stop()
            raise SystemdUnavailable(
                "the container has no package archive: apt could not install "
                f"{', '.join(REQUIRED_PACKAGES)}. This tier needs network access to "
                "Debian, and without it the backup and SFTP checks below verify nothing"
            )

    def _await_boot(self):
        deadline = BOOT_TIMEOUT
        while deadline > 0:
            state = self.run(["systemctl", "is-system-running"], timeout=30)
            if state.stdout.strip() in ("running", "degraded"):
                return
            subprocess.run(["sleep", "3"], check=False)
            deadline -= 3
        raise SystemdUnavailable("systemd did not finish booting in the container")

    def stop(self):
        if not self.started:
            return
        subprocess.run(["docker", "rm", "-f", self.name], capture_output=True, check=False, timeout=120)
        self.started = False

    def __enter__(self):
        return self.start()

    def __exit__(self, *_):
        self.stop()

    # --- commands --------------------------------------------------------

    def run(self, argv, *, user=None, check=False, timeout=300, input_text=None):
        command = ["docker", "exec"]
        if input_text is not None:
            # Without -i the container process gets no stdin at all.
            command.append("-i")
        if user:
            command += ["--user", user]
        command += [self.name, *argv]
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
            input=input_text,
        )
        if check and result.returncode != 0:
            raise RuntimeError(
                f"command failed in {self.name}: {' '.join(argv)}\n"
                f"stdout: {result.stdout}\nstderr: {result.stderr}"
            )
        return result

    def shell(self, script, *, user=None, check=False, timeout=300):
        argv = ["/bin/sh", "-c", script]
        return self.run(argv, user=user, check=check, timeout=timeout)

    def copy_in(self, source, destination):
        subprocess.run(
            ["docker", "cp", str(source), f"{self.name}:{destination}"],
            capture_output=True,
            check=True,
            timeout=180,
        )

    # --- package ---------------------------------------------------------

    def install_package(self, package_path, *, expect_success=True):
        self.copy_in(package_path, "/root/appliance.deb")
        result = self.shell(
            "DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --allow-downgrades "
            "/root/appliance.deb 2>&1",
            timeout=900,
        )
        if expect_success and result.returncode != 0:
            raise RuntimeError(f"package install failed:\n{result.stdout}\n{result.stderr}")
        return result

    def remove_package(self):
        return self.shell(
            "DEBIAN_FRONTEND=noninteractive apt-get remove -y -qq ems-appliance-manager 2>&1",
            timeout=600,
        )

    def purge_package(self):
        return self.shell(
            "DEBIAN_FRONTEND=noninteractive apt-get purge -y -qq ems-appliance-manager 2>&1",
            timeout=600,
        )

    def account_exists(self, name):
        return self.shell(f"getent passwd {name} >/dev/null").returncode == 0

    def acl_entries(self, path, *, user):
        """Every ACL entry of ``path`` that names ``user`` (access and default)."""

        result = self.shell(f"getfacl -p --absolute-names {path} 2>/dev/null")
        return [
            line.strip()
            for line in result.stdout.splitlines()
            if line.strip().startswith((f"user:{user}:", f"default:user:{user}:"))
        ]

    def reload_sshd(self):
        return self.shell(
            "systemctl reload ssh.service 2>/dev/null || systemctl restart ssh.service 2>&1",
            timeout=180,
        )

    # --- systemd introspection -------------------------------------------

    def unit_property(self, unit, name):
        result = self.run(["systemctl", "show", unit, "--property", name, "--value"], timeout=60)
        return result.stdout.strip()

    def unit_properties(self, unit, names):
        return {name: self.unit_property(unit, name) for name in names}

    def unit_active(self, unit):
        return self.run(["systemctl", "is-active", unit], timeout=60).stdout.strip()

    def wait_for_unit(self, unit, expected="active", attempts=20):
        for _ in range(attempts):
            if self.unit_active(unit) == expected:
                return True
            self.run(["sleep", "1"], timeout=30)
        return False

    def wait_for_path(self, path, attempts=30):
        """Type=simple reports active at exec, so wait for the socket itself."""

        for _ in range(attempts):
            if self.exists(path):
                return True
            self.run(["sleep", "1"], timeout=30)
        return False

    def journal(self, unit, lines=60):
        return self.run(
            ["journalctl", "-u", unit, "-n", str(lines), "--no-pager"], timeout=120
        ).stdout

    # --- filesystem ------------------------------------------------------

    def stat(self, path):
        result = self.run(["stat", "-c", "%a %U %G", path], timeout=60)
        if result.returncode != 0:
            return None
        mode, owner, group = result.stdout.strip().split()
        return {"mode": mode, "owner": owner, "group": group}

    def exists(self, path):
        return self.run(["test", "-e", path], timeout=60).returncode == 0

    def read_file(self, path):
        result = self.run(["cat", path], timeout=60)
        return result.stdout if result.returncode == 0 else ""

    def can_traverse(self, path, *, user):
        """True when ``user`` may pass through every directory in ``path``."""

        return self.run(["test", "-x", path], user=user, timeout=60).returncode == 0

    def can_write(self, path, *, user):
        probe = f"{path.rstrip('/')}/.write-probe-{uuid.uuid4().hex[:8]}"
        result = self.shell(f"touch {probe} 2>/dev/null && rm -f {probe}", user=user, timeout=60)
        return result.returncode == 0

    def can_append(self, path, *, user):
        return self.shell(f"printf 'x' >> {path} 2>/dev/null", user=user, timeout=60).returncode == 0

    # --- appliance helpers ------------------------------------------------

    def agent_socket_reachable(self, *, user):
        """Connect to the agent socket as ``user`` and issue a read-only call."""

        return self.agent_call({"operation": "system.get"}, user=user, timeout=120)

    def agent_call(self, payload, *, user="root", timeout=180):
        request = json.dumps(json.dumps(payload))
        script = (
            "import socket\n"
            "s=socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)\n"
            "s.settimeout(120)\n"
            f"s.connect('{SOCKET_PATH}')\n"
            f"s.sendall({request}.encode()+b'\\n')\n"
            "data=b''\n"
            "while not data.endswith(b'\\n'):\n"
            "    chunk=s.recv(65536)\n"
            "    if not chunk: break\n"
            "    data+=chunk\n"
            "print(data.decode().strip())\n"
        )
        return self.run(["python3", "-c", script], user=user, timeout=timeout)

    # --- SSH / SFTP -------------------------------------------------------

    def enable_sshd(self, *, key_path=BACKUP_KEY, user=BACKUP_USER):
        """Install and start a real sshd, with a key authorised for ``user``."""

        result = self.shell(
            "DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "
            "openssh-server openssh-sftp-server openssh-client >/dev/null 2>&1; "
            "command -v sshd >/dev/null 2>&1 || command -v /usr/sbin/sshd >/dev/null 2>&1",
            timeout=900,
        )
        if result.returncode != 0:
            raise SystemdUnavailable("openssh-server is not installable in this guest")

        home = self.shell(f"getent passwd {user} | cut -d: -f6").stdout.strip()
        if not home:
            raise SystemdUnavailable(f"the {user} account has no home directory")
        self.shell(
            f"rm -f {key_path} {key_path}.pub; "
            f"ssh-keygen -t ed25519 -N '' -C appliance-test -f {key_path} >/dev/null 2>&1; "
            f"install -d -o {user} -g {user} -m 0700 {home}/.ssh; "
            f"cp {key_path}.pub {home}/.ssh/authorized_keys; "
            f"chown {user}:{user} {home}/.ssh/authorized_keys; "
            f"chmod 0600 {home}/.ssh/authorized_keys",
            timeout=300,
        )
        self.attribute_backup_key(home)
        self.shell(
            "mkdir -p /run/sshd; ssh-keygen -A >/dev/null 2>&1; "
            "systemctl restart ssh.service 2>/dev/null || systemctl restart sshd.service "
            "2>/dev/null || /usr/sbin/sshd",
            timeout=300,
        )
        for _ in range(20):
            if self.shell("ss -ltn | grep -q ':22 '").returncode == 0:
                return True
            self.run(["sleep", "1"], timeout=30)
        raise SystemdUnavailable(f"sshd did not start:\n{self.journal('ssh.service')}")

    def attribute_backup_key(self, home):
        """Record the seeded key the way the appliance records its own.

        A real installation adds the backup key through the appliance, which
        hashes the key body into ``managed-keys.list``. A key copied straight
        into ``authorized_keys`` is one the appliance cannot attribute, so
        ``backup-access activate`` correctly refuses to enable the account —
        the guest has to seed the same attribution, through the same function.
        """

        return self.shell(
            "cd /usr/lib/ems-appliance-manager && python3 -c \""
            "import sys; sys.path.insert(0, '.');"
            "from appliance.paths import resolve_paths;"
            "from appliance import backup_ownership;"
            f"blob = open('{home}/.ssh/authorized_keys').read().split()[1];"
            "backup_ownership.record_managed_keys(resolve_paths(), [blob])\"",
            timeout=180,
        )

    def sftp(self, commands, *, user=BACKUP_USER, key_path=BACKUP_KEY, timeout=180):
        """Run one SFTP batch as ``user`` over a real SSH connection."""

        script = "\n".join(commands)
        return self.shell(
            f"printf {shlex.quote(script + chr(10))} | "
            f"sftp -b - -o BatchMode=yes -o StrictHostKeyChecking=no "
            f"-o UserKnownHostsFile=/dev/null -o ConnectTimeout=15 "
            f"-i {key_path} {user}@127.0.0.1 2>&1",
            timeout=timeout,
        )

    def ssh(self, arguments, *, user=BACKUP_USER, key_path=BACKUP_KEY, timeout=120):
        return self.shell(
            f"ssh -o BatchMode=yes -o StrictHostKeyChecking=no "
            f"-o UserKnownHostsFile=/dev/null -o ConnectTimeout=15 "
            f"-i {key_path} {arguments} {user}@127.0.0.1 2>&1 </dev/null",
            timeout=timeout,
        )

    def seed_ems_installation(self, *, marker="exported-config-marker"):
        self.shell(
            "mkdir -p /opt/ems-solarflow/config /opt/ems-solarflow/backups "
            "/opt/ems-solarflow/data /opt/ems-solarflow/secrets && "
            f"printf '%s\\n' {shlex.quote(marker)} > /opt/ems-solarflow/config/config.json && "
            "printf 'backup-bytes\\n' > /opt/ems-solarflow/backups/backup-1.tar && "
            "printf 'runtime\\n' > /opt/ems-solarflow/data/runtime-state.json && "
            "printf 'do-not-export\\n' > /opt/ems-solarflow/secrets/mqtt.pass",
            timeout=180,
        )
        return marker

    def setup_export_root(self, timeout=300):
        return self.shell("/usr/lib/ems-appliance-manager/setup-export-root.sh 2>&1", timeout=timeout)

    def publish_exports(self, timeout=300):
        """Rebuild the export root the way the host does it.

        The unit is the production path: it runs the setup script and then
        re-validates backup access, so authentication follows the boundary the
        run produced. Calling the script directly does neither.
        """

        return self.shell(
            "systemctl start ems-appliance-export.service 2>&1; "
            "systemctl is-active ems-appliance-export.service",
            timeout=timeout,
        )

    # --- fixtures inside the guest ----------------------------------------

    def write_file(self, path, content, *, mode="0644"):
        result = self.run(["/bin/sh", "-c", f"cat > {path} && chmod {mode} {path}"],
                          input_text=content, timeout=120)
        if result.returncode != 0:
            raise RuntimeError(f"cannot write {path}: {result.stderr}")
        return path

    def set_appliance_option(self, key, value):
        """Rewrite one option in the packaged /etc configuration."""

        self.shell(
            f"sed -i 's|^{key} *=.*|{key} = {value}|' {APPLIANCE_CONF} && grep -q '^{key} = ' "
            f"{APPLIANCE_CONF} || printf '%s = %s\\n' {shlex.quote(key)} {shlex.quote(value)} "
            f">> {APPLIANCE_CONF}",
            timeout=120,
        )
        return self.read_file(APPLIANCE_CONF)

    def port_listening(self, port):
        return self.shell(f"ss -ltn | grep -q ':{port} '").returncode == 0

    def serve_release_index(self, tags, *, port=RELEASE_INDEX_PORT):
        """A local HTTP release index the agent can actually fetch.

        Deliberately no ``pkill``: the pattern would also match this shell's own
        command line, which names the server it is about to start.
        """

        self.shell(f"mkdir -p {RELEASE_INDEX_DIR}", timeout=60)
        self.write_file(
            f"{RELEASE_INDEX_DIR}/releases.json",
            json.dumps([{"tag_name": tag, "prerelease": "-" in tag} for tag in tags]),
        )
        url = f"http://127.0.0.1:{port}/releases.json"
        if self.port_listening(port):
            return url

        self.shell(
            f"cd {RELEASE_INDEX_DIR} && nohup python3 -m http.server {port} --bind 127.0.0.1 "
            ">/tmp/release-index.log 2>&1 & sleep 2",
            timeout=120,
        )
        for _ in range(15):
            if self.port_listening(port):
                return url
            self.run(["sleep", "1"], timeout=30)
        raise SystemdUnavailable(
            f"the release index fixture did not start:\n{self.read_file('/tmp/release-index.log')}"
        )

    def isolate_apt(self):
        """Replace every APT source with an empty local repository."""

        self.shell(
            f"mkdir -p {LOCAL_APT_DIR} && : > {LOCAL_APT_DIR}/Packages && "
            f"gzip -kf {LOCAL_APT_DIR}/Packages; "
            "mkdir -p /root/apt-sources-backup && "
            "mv /etc/apt/sources.list /root/apt-sources-backup/ 2>/dev/null; "
            "mv /etc/apt/sources.list.d/* /root/apt-sources-backup/ 2>/dev/null; "
            "mkdir -p /etc/apt/sources.list.d && "
            f"printf 'deb [trusted=yes] file:{LOCAL_APT_DIR} ./\\n' "
            "> /etc/apt/sources.list.d/ems-appliance-isolated.list; "
            "rm -rf /var/lib/apt/lists/*",
            timeout=180,
        )
        return LOCAL_APT_DIR

    def restore_apt(self):
        self.shell(
            "rm -f /etc/apt/sources.list.d/ems-appliance-isolated.list; "
            "mv /root/apt-sources-backup/sources.list /etc/apt/ 2>/dev/null; "
            "mv /root/apt-sources-backup/* /etc/apt/sources.list.d/ 2>/dev/null; true",
            timeout=180,
        )

    # --- the installed web service ----------------------------------------

    def drive_web_service(self, *, password=SMOKE_PASSWORD, port=None, timeout=600):
        """Run the packaged HTTP flow and return the client's JSON report.

        The port comes from the module that owns it, never from a copy here.
        A second constant is a probe that keeps pointing at the old port after
        the service moves, and then reports the move as "connection refused".
        """

        port = DEFAULT_WEB_PORT if port is None else port

        self.copy_in(HTTP_CLIENT, "/root/appliance_http_client.py")
        result = self.run(
            ["python3", "/root/appliance_http_client.py", f"http://127.0.0.1:{port}", password],
            timeout=timeout,
        )
        marker = "APPLIANCE_HTTP_REPORT:"
        for line in result.stdout.splitlines():
            if line.startswith(marker):
                return json.loads(line[len(marker):])
        raise AssertionError(
            f"the packaged web service produced no report\n{result.stdout}\n{result.stderr}"
        )

    def run_operation(self, plan_payload, *, attempts=90):
        """Plan, confirm and poll one agent operation through the real socket."""

        planned = self.agent_call(plan_payload)
        payload = json.loads(planned.stdout.strip())
        if not payload.get("ok"):
            return payload
        operation_id = payload["result"]["operation"]["operation_id"]
        confirmed = self.agent_call(
            {
                "operation": "operations.execute",
                "operation_id": operation_id,
                "confirmation_token": payload["result"]["confirmation_token"],
            }
        )
        if not json.loads(confirmed.stdout.strip()).get("ok"):
            return json.loads(confirmed.stdout.strip())

        for _ in range(attempts):
            fetched = self.agent_call(
                {"operation": "operations.get", "operation_id": operation_id}
            )
            record = json.loads(fetched.stdout.strip())["result"]["operation"]
            if record.get("terminal"):
                return {"ok": True, "operation": record}
            self.run(["sleep", "2"], timeout=30)
        return {"ok": False, "operation": record, "error": {"code": "operation_timeout"}}

    def add_unrelated_user(self, name=UNRELATED_USER):
        self.shell(
            f"id -u {name} >/dev/null 2>&1 || adduser --system --no-create-home "
            f"--home /nonexistent --shell /usr/sbin/nologin {name}",
            timeout=120,
        )
        return name


class OfflineRootContainer(SystemdContainer):
    """A guest without a running systemd — an image-build chroot in miniature.

    ``/run/systemd/system`` does not exist here, so the package takes exactly
    the path it takes while a Raspberry Pi image is assembled: create
    everything, enable the units, start nothing.
    """

    def __init__(self, *, image=DEFAULT_IMAGE, name=None):
        super().__init__(image=image, name=name or f"ems-appliance-offline-{uuid.uuid4().hex[:10]}")

    def start(self):
        if not docker_available():
            raise SystemdUnavailable("docker is not available")
        result = subprocess.run(
            ["docker", "run", "-d", "--name", self.name, self.image, "sleep", "infinity"],
            capture_output=True,
            text=True,
            check=False,
            timeout=300,
        )
        if result.returncode != 0:
            raise SystemdUnavailable(f"cannot start an offline guest: {result.stderr.strip()}")
        self.started = True
        prepared = self.shell(
            "apt-get update -qq >/dev/null 2>&1; "
            "DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "
            "adduser python3 systemd acl iproute2 procps >/dev/null 2>&1; "
            "rm -rf /run/systemd/system; command -v python3",
            timeout=900,
        )
        if prepared.returncode != 0:
            self.stop()
            raise SystemdUnavailable(f"cannot prepare an offline guest: {prepared.stderr}")
        return self
