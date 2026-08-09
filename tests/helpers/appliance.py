# SPDX-License-Identifier: AGPL-3.0-or-later
"""Deterministic host fakes for the Appliance Manager tests.

``FakeHost`` models exactly the host surface the appliance touches: a Docker
engine with images and containers, systemd units, an apt/dpkg backend, nmcli
and ``getent``. No test starts a real process, contacts a network or writes
outside its temporary directory.
"""

import json
import re

from appliance.admin_deployment import read_service_image
from appliance.commands import CommandError, CommandResult
from appliance.config import ApplianceConfig, AllowedImages
from appliance.health import HealthResult
from appliance.paths import AppliancePaths
from appliance.services import build_services

ADMIN_REPOSITORY = "ghcr.io/basecubedev/ems-solarflow-admin"
IMAGE_SOURCE = "https://github.com/basecubedev/ems-solarflow-api-control"
ADMIN_CONTAINER = "ems-solarflow-admin"
EMS_CONTAINER = "ems-solarflow"
ADMIN_SERVICE = "ems-solarflow-admin"

TAG_VARIABLE = re.compile(r"\$\{EMS_ADMIN_TAG(?::-([^}]*))?\}")

COMPOSE_TEMPLATE = """services:
  {service}:
    image: {image}
    container_name: {container}
    network_mode: host
    restart: unless-stopped
  {ems_service}:
    image: ghcr.io/basecubedev/ems-solarflow-api-control:v1.0.0
    container_name: {ems_container}
"""

ENV_TEMPLATE = """EMS_INSTALL_DIR={install_dir}
EMS_ADMIN_IMAGE={repository}
EMS_ADMIN_TAG={tag}
"""

APT_SIMULATION = """Reading package lists...
Building dependency tree...
Inst libfoo [1.0-1] (1.0-2 Debian:12/stable [arm64])
Inst openssl [3.0.11-1] (3.0.14-1 Debian-Security:12/stable-security [arm64])
Inst linux-image-arm64 [6.1.0-17] (6.1.0-18 Debian-Security:12/stable-security [arm64])
Conf libfoo (1.0-2 Debian:12/stable [arm64])
"""

NMCLI_DEVICE_STATUS = """wlan0:wifi:connected:HomeNet
eth0:ethernet:unavailable:
lo:loopback:unmanaged:
"""

NMCLI_DEVICE_SHOW = """GENERAL.CONNECTION:HomeNet
GENERAL.SSID:HomeNet
IP4.ADDRESS[1]:192.168.1.50/24
IP4.GATEWAY:192.168.1.1
IP4.DNS[1]:192.168.1.1
"""

NMCLI_WIFI_LIST = """yes:HomeNet:71:WPA2
:GuestNet:44:WPA2
:OpenNet:22:
"""

SSHD_CONFIG = """port 22
permitrootlogin no
passwordauthentication no
pubkeyauthentication yes
"""


class FakeHost:
    """A scripted Raspberry Pi host."""

    def __init__(self, paths, *, tools=None):
        self.paths = paths
        self.tools = set(
            tools
            or {
                "docker",
                "systemctl",
                "journalctl",
                "apt-get",
                "dpkg",
                "nmcli",
                "hostnamectl",
                "timedatectl",
                "getent",
                "sshd",
                "ss",
            }
        )
        self.docker_running = True
        self.registry = {}
        self.images = {}
        self.containers = {}
        self.units = {
            "docker.service": {"active": "active", "enabled": "enabled"},
            "ssh.service": {"active": "inactive", "enabled": "disabled"},
            "ems-appliance-web.service": {"active": "active", "enabled": "enabled"},
            "ems-appliance-agent.service": {"active": "active", "enabled": "enabled"},
        }
        self.accounts = {}
        self.hostname = "ems-solarflow"
        self.apt_simulation = APT_SIMULATION
        self.apt_exit_code = 0
        self.dpkg_audit = ""
        self.dpkg_selections = "libfoo\tinstall\nheld-package\thold\n"
        self.nmcli_connectivity = "full"
        self.wifi_connect_ok = True
        self.listening_ports = ""
        self.calls = []
        self.compose_up_fails = False

    # --- image / container helpers ---------------------------------------

    def publish_image(
        self,
        tag,
        *,
        digest=None,
        version=None,
        revision="abc1234",
        architecture="arm64",
        source=IMAGE_SOURCE,
        labels=None,
        healthy=True,
        repository=ADMIN_REPOSITORY,
    ):
        reference = f"{repository}:{tag}"
        digest = digest or ("sha256:" + (tag.replace(".", "").replace("v", "") * 16)[:64].ljust(64, "0"))
        entry = {
            "Id": "sha256:" + digest.split(":")[1],
            "RepoDigests": [f"{repository}@{digest}"],
            "Architecture": architecture,
            "Os": "linux",
            "Config": {
                "Labels": labels
                if labels is not None
                else {
                    "org.opencontainers.image.source": source,
                    "org.opencontainers.image.version": version or tag,
                    "org.opencontainers.image.revision": revision,
                    "org.opencontainers.image.created": "2026-01-01T00:00:00Z",
                }
            },
            "_digest": digest,
            "_healthy": healthy,
        }
        self.registry[reference] = entry
        self.registry[f"{repository}@{digest}"] = entry
        return entry

    def pull_local(self, reference):
        entry = self.registry.get(reference)
        if entry is None:
            return False
        self.images[reference] = entry
        self.images[f"{ADMIN_REPOSITORY}@{entry['_digest']}"] = entry
        return True

    def run_container(self, name, reference, *, health="healthy", state="running"):
        entry = self.images.get(reference) or self.registry.get(reference) or {}
        self.containers[name] = {
            "Id": "c" * 64,
            "Image": reference,
            "State": {
                "Running": state == "running",
                "Restarting": state == "restarting",
                "Status": state,
                "StartedAt": "2026-01-01T00:00:00Z",
                "ExitCode": 0 if state == "running" else 1,
                "Health": {"Status": health, "Log": [{"Output": "ok"}]},
            },
            "Config": {"Image": reference},
            "RestartCount": 0,
            "NetworkSettings": {"Ports": {}},
            "_entry": entry,
        }
        return self.containers[name]

    def running_admin_entry(self):
        container = self.containers.get(ADMIN_CONTAINER)
        if not container:
            return None
        reference = container["Config"]["Image"]
        return self.images.get(reference) or self.registry.get(reference)

    # --- deployment files ------------------------------------------------

    def write_deployment(self, *, tag="v1.0.0", variable_tag=True, service=ADMIN_SERVICE):
        image = (
            f"{ADMIN_REPOSITORY}:${{EMS_ADMIN_TAG:-latest}}"
            if variable_tag
            else f"{ADMIN_REPOSITORY}:{tag}"
        )
        compose = COMPOSE_TEMPLATE.format(
            service=service,
            image=image,
            container=ADMIN_CONTAINER,
            ems_service="ems",
            ems_container=EMS_CONTAINER,
        )
        self.paths.install_root.mkdir(parents=True, exist_ok=True)
        (self.paths.install_root / "docker-compose.admin.yml").write_text(compose, encoding="utf-8")
        (self.paths.install_root / ".env.admin").write_text(
            ENV_TEMPLATE.format(
                install_dir=self.paths.install_root, repository=ADMIN_REPOSITORY, tag=tag
            ),
            encoding="utf-8",
        )
        for directory in ("config", "data", "backups"):
            (self.paths.install_root / directory).mkdir(parents=True, exist_ok=True)
        return compose

    def resolved_compose_image(self, service=ADMIN_SERVICE):
        compose = (self.paths.install_root / "docker-compose.admin.yml").read_text(encoding="utf-8")
        image = read_service_image(compose, service) or ""
        env = {}
        env_file = self.paths.install_root / ".env.admin"
        if env_file.is_file():
            for line in env_file.read_text(encoding="utf-8").splitlines():
                key, _, value = line.partition("=")
                env[key.strip()] = value.strip()

        def substitute(match):
            return env.get("EMS_ADMIN_TAG") or match.group(1) or "latest"

        return TAG_VARIABLE.sub(substitute, image)

    # --- runner ----------------------------------------------------------

    def runner(self):
        return ScriptedRunner(self)

    def handle(self, tool, args, input_text=None):
        self.calls.append((tool, tuple(args), input_text))
        handler = getattr(self, f"_{tool.replace('-', '_')}", None)
        if handler is None:
            return CommandResult(tool, tuple(args), 1, "", f"unhandled tool {tool}")
        return handler(list(args))

    def _result(self, tool, args, code=0, stdout="", stderr=""):
        return CommandResult(tool, tuple(args), code, stdout, stderr)

    # --- docker ----------------------------------------------------------

    def _docker(self, args):
        if not self.docker_running:
            return self._result("docker", args, 1, "", "Cannot connect to the Docker daemon")
        if args[:1] == ["version"]:
            return self._result("docker", args, 0, "26.1.5\n")
        if args[:1] == ["inspect"] and "container" in args:
            name = args[-1]
            payload = self.containers.get(name)
            if payload is None:
                return self._result("docker", args, 1, "", "No such container")
            return self._result("docker", args, 0, json.dumps(payload))
        if args[:2] == ["image", "inspect"]:
            reference = args[-1]
            payload = self.images.get(reference)
            if payload is None:
                return self._result("docker", args, 1, "", "No such image")
            return self._result("docker", args, 0, json.dumps(payload))
        if args[:1] == ["pull"]:
            reference = args[-1]
            if not self.pull_local(reference):
                return self._result("docker", args, 1, "", "manifest unknown")
            return self._result("docker", args, 0, f"Pulled {reference}\n")
        if args[:1] == ["tag"]:
            source, target = args[1], args[2]
            entry = self.images.get(source)
            if entry is None:
                return self._result("docker", args, 1, "", "No such image")
            self.images[target] = entry
            return self._result("docker", args, 0)
        if args[:1] == ["stop"]:
            name = args[-1]
            if name in self.containers:
                self.containers[name]["State"]["Running"] = False
                self.containers[name]["State"]["Status"] = "exited"
            return self._result("docker", args, 0)
        if args[:1] == ["start"]:
            name = args[-1]
            if name not in self.containers:
                return self._result("docker", args, 1, "", "No such container")
            self.containers[name]["State"]["Running"] = True
            self.containers[name]["State"]["Status"] = "running"
            return self._result("docker", args, 0)
        if args[:1] == ["restart"]:
            name = args[-1]
            if name not in self.containers:
                return self._result("docker", args, 1, "", "No such container")
            self.containers[name]["State"]["Running"] = True
            self.containers[name]["State"]["Status"] = "running"
            return self._result("docker", args, 0)
        if args[:1] == ["logs"]:
            return self._result("docker", args, 0, "admin log line\npassword=supersecret\n")
        if args[:1] == ["compose"]:
            return self._compose(args)
        return self._result("docker", args, 1, "", "unsupported docker command")

    def _compose(self, args):
        if self.compose_up_fails and "up" in args:
            return self._result("docker", args, 1, "", "compose up failed")
        if "up" in args:
            service = args[-1]
            reference = self.resolved_compose_image(service)
            entry = self.images.get(reference)
            if entry is None:
                return self._result("docker", args, 1, "", f"image {reference} not present")
            healthy = bool(entry.get("_healthy", True))
            self.run_container(
                ADMIN_CONTAINER, reference, health="healthy" if healthy else "unhealthy"
            )
            return self._result("docker", args, 0, "recreated\n")
        if "stop" in args:
            name = ADMIN_CONTAINER
            if name in self.containers:
                self.containers[name]["State"]["Running"] = False
            return self._result("docker", args, 0)
        if "config" in args:
            return self._result("docker", args, 0, f"{ADMIN_SERVICE}\nems\n")
        return self._result("docker", args, 0)

    # --- systemd ---------------------------------------------------------

    def _systemctl(self, args):
        if args[:1] == ["is-active"]:
            return self._result("systemctl", args, 0, self.units.get(args[1], {}).get("active", "inactive"))
        if args[:1] == ["is-enabled"]:
            return self._result("systemctl", args, 0, self.units.get(args[1], {}).get("enabled", "disabled"))
        if args[:1] == ["enable"]:
            unit = args[-1]
            self.units.setdefault(unit, {}).update({"active": "active", "enabled": "enabled"})
            return self._result("systemctl", args, 0)
        if args[:1] == ["disable"]:
            unit = args[-1]
            self.units.setdefault(unit, {}).update({"active": "inactive", "enabled": "disabled"})
            return self._result("systemctl", args, 0)
        if args[:1] in (["start"], ["stop"], ["reboot"], ["poweroff"], ["try-restart"]):
            return self._result("systemctl", args, 0)
        return self._result("systemctl", args, 0)

    def _journalctl(self, args):
        return self._result("journalctl", args, 0, "journal line one\njournal line two\n")

    # --- packages --------------------------------------------------------

    def _apt_get(self, args):
        if args[:1] == ["-s"] or "-s" in args[:2]:
            return self._result("apt-get", args, 0, self.apt_simulation)
        if "update" in args:
            return self._result("apt-get", args, 0, "Reading package lists... Done\n")
        if self.apt_exit_code == 0:
            self.apt_simulation = "Reading package lists...\nBuilding dependency tree...\n"
        return self._result(
            "apt-get", args, self.apt_exit_code, "Setting up libfoo (1.0-2) ...\n", ""
        )

    def _dpkg(self, args):
        if args[:1] == ["--get-selections"]:
            return self._result("dpkg", args, 0, self.dpkg_selections)
        if args[:1] == ["--audit"]:
            return self._result("dpkg", args, 0, self.dpkg_audit)
        if args[:1] == ["--configure"]:
            self.dpkg_audit = ""
            return self._result("dpkg", args, 0, "Setting up pending packages\n")
        return self._result("dpkg", args, 0)

    # --- network ---------------------------------------------------------

    def _nmcli(self, args):
        joined = " ".join(args)
        if "general" in joined and "CONNECTIVITY" in joined and "STATE" in joined:
            return self._result("nmcli", args, 0, f"connected:{self.nmcli_connectivity}\n")
        if "general" in joined and "CONNECTIVITY" in joined:
            return self._result("nmcli", args, 0, f"{self.nmcli_connectivity}\n")
        if "device status" in joined:
            return self._result("nmcli", args, 0, NMCLI_DEVICE_STATUS)
        if "device show" in joined:
            return self._result("nmcli", args, 0, NMCLI_DEVICE_SHOW)
        if "device wifi list" in joined:
            return self._result("nmcli", args, 0, NMCLI_WIFI_LIST)
        if "connection show --active" in joined:
            return self._result("nmcli", args, 0, "HomeNet:802-11-wireless:wlan0\n")
        if "device wifi connect" in joined:
            if not self.wifi_connect_ok:
                return self._result("nmcli", args, 1, "", "Error: Connection activation failed")
            return self._result("nmcli", args, 0, "Device 'wlan0' successfully activated\n")
        if "connection up" in joined:
            self.nmcli_connectivity = "full"
            return self._result("nmcli", args, 0, "Connection successfully activated\n")
        return self._result("nmcli", args, 0, "")

    def _hostnamectl(self, args):
        if args[:1] == ["--static"]:
            return self._result("hostnamectl", args, 0, f"{self.hostname}\n")
        if args[:1] == ["set-hostname"]:
            self.hostname = args[1]
            return self._result("hostnamectl", args, 0)
        return self._result("hostnamectl", args, 0, f"{self.hostname}\n")

    def _timedatectl(self, args):
        return self._result(
            "timedatectl", args, 0, "Timezone=Europe/Berlin\nNTPSynchronized=yes\n"
        )

    # --- accounts / ssh --------------------------------------------------

    def add_account(self, name, home, *, uid=1500, gid=1500, shell="/usr/sbin/nologin"):
        self.accounts[name] = {"home": str(home), "uid": uid, "gid": gid, "shell": shell}
        return self.accounts[name]

    def _getent(self, args):
        if args[:1] == ["passwd"]:
            account = self.accounts.get(args[1])
            if account is None:
                return self._result("getent", args, 2, "")
            line = ":".join(
                [
                    args[1],
                    "x",
                    str(account["uid"]),
                    str(account["gid"]),
                    "",
                    account["home"],
                    account["shell"],
                ]
            )
            return self._result("getent", args, 0, line + "\n")
        return self._result("getent", args, 2, "")

    def _sshd(self, args):
        return self._result("sshd", args, 0, SSHD_CONFIG)

    def _ss(self, args):
        return self._result("ss", args, 0, self.listening_ports)


class ScriptedRunner:
    def __init__(self, host):
        self.host = host

    def available(self, tool):
        return tool in self.host.tools

    def resolve(self, tool):
        if tool not in self.host.tools:
            raise CommandError("tool_unavailable", f"{tool} is not installed")
        return f"/usr/bin/{tool}"

    def run(self, tool, args=(), *, timeout=None, input_text=None, check=False):
        self.resolve(tool)
        result = self.host.handle(tool, list(args), input_text)
        if check and not result.ok:
            raise CommandError("command_failed", f"{tool} failed")
        return result


class FakeHealthChecker:
    """Reports the health of whatever image the Admin container currently runs."""

    def __init__(self, host, *, forced=None):
        self.host = host
        self.forced = forced
        self.probes = 0

    def probe(self, url):
        self.probes += 1
        if self.forced is not None:
            return self.forced
        entry = self.host.running_admin_entry()
        container = self.host.containers.get(ADMIN_CONTAINER)
        if entry is None or container is None or not container["State"]["Running"]:
            return HealthResult(reachable=False, error="connection_refused")
        if not entry.get("_healthy", True):
            return HealthResult(reachable=False, status_code=503, error="unhealthy")
        version = (entry.get("Config", {}).get("Labels", {}) or {}).get(
            "org.opencontainers.image.version", ""
        )
        return HealthResult(reachable=True, status_code=200, version=version)

    def wait_until_healthy(self, url, *, timeout, interval=0, on_attempt=None):
        return self.probe(url)


class StaticCatalogue:
    def __init__(self, tags=(), *, error=None):
        self.tags = list(tags)
        self.error = error

    def available(self):
        if self.error:
            raise self.error
        from appliance.releases import ReleaseTarget

        return [ReleaseTarget(tag=tag, channel="exact") for tag in self.tags]

    def latest_stable(self):
        from appliance.releases import ReleaseResolutionError, ReleaseTarget

        stable = [tag for tag in self.tags if "-" not in tag]
        if not stable:
            raise ReleaseResolutionError("release_channel_unresolved", "no stable release")
        return ReleaseTarget(tag=stable[0], channel="latest_stable")


class FrozenClock:
    def __init__(self, start=1_800_000_000.0):
        self.now = float(start)

    def __call__(self):
        self.now += 1.0
        return self.now

    def sleep(self, seconds):
        self.now += float(seconds)


def appliance_paths(tmp_path):
    paths = AppliancePaths(
        install_root=tmp_path / "opt" / "ems-solarflow",
        config_dir=tmp_path / "etc" / "ems-appliance-manager",
        state_dir=tmp_path / "var" / "lib" / "ems-appliance-manager",
        log_dir=tmp_path / "var" / "log" / "ems-appliance-manager",
        runtime_dir=tmp_path / "run" / "ems-appliance-manager",
    )
    for directory in (paths.install_root, paths.config_dir, paths.runtime_dir):
        directory.mkdir(parents=True, exist_ok=True)
    return paths


def appliance_config(**overrides):
    values = {
        "admin_container": ADMIN_CONTAINER,
        "ems_container": EMS_CONTAINER,
        "admin_service": ADMIN_SERVICE,
        "ssh_key_accounts": ("ems-backup", "pi"),
        "health_timeout_seconds": 1,
        "wifi_revert_timeout_seconds": 1,
        "images": AllowedImages(
            repositories=(ADMIN_REPOSITORY,), expected_source=IMAGE_SOURCE
        ),
    }
    values.update(overrides)
    return ApplianceConfig(**values)


def build_test_services(tmp_path, *, host=None, config=None, catalogue=None, health=None, clock=None):
    paths = appliance_paths(tmp_path)
    host = host or FakeHost(paths)
    clock = clock or FrozenClock()
    services = build_services(
        paths=paths,
        config=config or appliance_config(),
        runner=host.runner(),
        root=str(tmp_path),
        health=health or FakeHealthChecker(host),
        catalogue=catalogue or StaticCatalogue(["v1.1.0", "v1.0.0"]),
        time_fn=clock,
        sleep=clock.sleep,
    )
    services.host = host
    services.clock = clock
    return services
