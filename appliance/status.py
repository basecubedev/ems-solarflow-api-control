# SPDX-License-Identifier: AGPL-3.0-or-later
"""Read-only status aggregation and bounded log access.

Each section is collected independently: a probe that fails degrades to an
``unavailable`` section with an error code instead of taking the whole overview
down with it.
"""

import time

from appliance import rescue_account, validation
from appliance.docker_backend import DAEMON_RUNNING
from appliance.redaction import bounded_redacted_log
from appliance.systemd import (
    UNIT_APPLIANCE_AGENT,
    UNIT_APPLIANCE_WEB,
    UNIT_DOCKER,
)
from appliance.version import APPLIANCE_VERSION

SECTION_OK = "ok"
SECTION_UNAVAILABLE = "unavailable"

HEALTH_HEALTHY = "healthy"
HEALTH_ATTENTION = "attention"
HEALTH_DEGRADED = "degraded"

DPKG_LOG = "var/log/dpkg.log"


def section(name, collector):
    """Run one collector and never let it fail the whole overview."""

    try:
        payload = collector()
    except Exception as exc:
        return {"section": name, "status": SECTION_UNAVAILABLE, "error": exc.__class__.__name__}
    payload = dict(payload) if isinstance(payload, dict) else {"value": payload}
    payload.update({"section": name, "status": SECTION_OK})
    return payload


class StatusService:
    def __init__(
        self,
        *,
        paths,
        config,
        probe,
        docker,
        systemd,
        admin,
        packages,
        network,
        ssh,
        backup,
        operations,
        os_update=None,
        time_fn=None,
    ):
        self.paths = paths
        self.os_update = os_update
        self.config = config
        self.probe = probe
        self.docker = docker
        self.systemd = systemd
        self.admin = admin
        self.packages = packages
        self.network = network
        self.ssh = ssh
        self.backup = backup
        self.operations = operations
        self._time = time_fn or time.time

    # --- sections --------------------------------------------------------

    def system(self):
        return {
            "appliance_version": APPLIANCE_VERSION,
            "hardware": self.probe.hardware(),
            "operating_system": self.probe.operating_system(),
            "uptime": self.probe.uptime(),
            "time": self.probe.system_time(),
            "temperature": self.probe.temperature(),
            "power": self.probe.power(),
            "memory": self.probe.memory(),
            "storage": {
                "root": self.probe.filesystem("/"),
                "ems_data": self.probe.filesystem(str(self.paths.ems_data_dir)),
            },
            "hostname": self.probe.hostname(),
            "timezone": str(getattr(self.config, "timezone", "UTC") or "UTC"),
            "services": self.systemd.unit_states(
                (UNIT_APPLIANCE_WEB, UNIT_APPLIANCE_AGENT, UNIT_DOCKER)
            ),
            # Reported, never demanded: the console says whether the rescue
            # account still carries the shipped password so an operator can see
            # the answer without going to look for it.
            "rescue": rescue_account.state(getattr(self.probe, "root", "/")).to_dict(),
        }

    def docker_state(self):
        daemon = self.docker.daemon_state()
        containers = []
        if daemon["state"] == DAEMON_RUNNING:
            for name in self.config.managed_containers:
                containers.append(self.docker.inspect_container(name).to_dict())
        return {"daemon": daemon, "containers": containers}

    def admin_state(self):
        return self.admin.detect()

    def updates(self):
        """Package updates plus, on an image-managed host, the A/B slot state.

        Both modes are reported from one place so the page can show exactly one
        of them: a single-slot appliance keeps package updates, an A/B appliance
        stages images into the inactive slot instead.
        """

        payload = self.packages.check().to_dict()
        payload["ab"] = self.ab_state()
        payload["update_mode"] = (
            "ab_image" if payload["ab"].get("ab_supported") else "single_slot"
        )
        return payload

    def ab_state(self):
        if self.os_update is None:
            return {"mode": "unsupported", "ab_supported": False, "reason": "ab_unavailable"}
        try:
            return self.os_update.status()
        except Exception as exc:
            return {
                "mode": "unsupported",
                "ab_supported": False,
                "reason": getattr(exc, "code", "ab_status_unavailable"),
            }

    def network_state(self):
        return self.network.status()

    def ssh_state(self):
        return self.ssh.status()

    def backup_state(self):
        return self.backup.status()

    def operations_state(self):
        active = self.operations.active()
        return {
            "active": active.to_dict() if active else None,
            "recent": [item.to_dict() for item in self.operations.list(limit=10)],
            "unacknowledged": [item.to_dict() for item in self.operations.unacknowledged()],
        }

    # --- aggregate -------------------------------------------------------

    def overview(self):
        sections = {
            "system": section("system", self.system),
            "docker": section("docker", self.docker_state),
            "admin": section("admin", self.admin_state),
            "updates": section("updates", self.updates),
            "network": section("network", self.network_state),
            "ssh": section("ssh", self.ssh_state),
            "operations": section("operations", self.operations_state),
        }
        sections["health"] = self._health(sections)
        sections["appliance_version"] = APPLIANCE_VERSION
        sections["collected_at"] = self._time()
        return sections

    def _health(self, sections):
        warnings = []
        level = HEALTH_HEALTHY

        for name, payload in sections.items():
            if isinstance(payload, dict) and payload.get("status") == SECTION_UNAVAILABLE:
                warnings.append(
                    {"code": f"{name}_unavailable", "message": f"{name} status is unavailable"}
                )
                level = HEALTH_ATTENTION

        docker = sections.get("docker", {})
        if docker.get("status") == SECTION_OK:
            daemon = docker.get("daemon", {})
            if daemon.get("state") != DAEMON_RUNNING:
                warnings.append(
                    {"code": "docker_not_running", "message": "the Docker daemon is not running"}
                )
                level = HEALTH_DEGRADED

        admin = sections.get("admin", {})
        if admin.get("status") == SECTION_OK:
            if not admin.get("installed"):
                warnings.append(
                    {"code": "admin_not_installed", "message": "the EMS Admin container is missing"}
                )
                level = HEALTH_DEGRADED
            elif not admin.get("healthy"):
                warnings.append(
                    {"code": "admin_unhealthy", "message": "the EMS Admin container is not healthy"}
                )
                level = HEALTH_DEGRADED

        updates = sections.get("updates", {})
        if updates.get("status") == SECTION_OK:
            if updates.get("security_count"):
                warnings.append(
                    {
                        "code": "security_updates_pending",
                        "message": f"{updates['security_count']} security update(s) available",
                    }
                )
                level = HEALTH_ATTENTION if level == HEALTH_HEALTHY else level
            if updates.get("reboot_required"):
                warnings.append(
                    {"code": "reboot_required", "message": "a reboot is required to finish updates"}
                )
                level = HEALTH_ATTENTION if level == HEALTH_HEALTHY else level
            if not (updates.get("package_manager") or {}).get("healthy", True):
                warnings.append(
                    {
                        "code": "package_manager_unhealthy",
                        "message": "the package manager needs recovery",
                    }
                )
                level = HEALTH_DEGRADED

        system = sections.get("system", {})
        if system.get("status") == SECTION_OK:
            storage = system.get("storage") or {}
            # On an A/B appliance / is the slot's fixed system partition,
            # written once at build time and mounted read-only, so its usage
            # cannot move. Everything that grows -- EMS data and backups, both
            # slots' /var, the Docker stores, the journal and the update
            # staging -- is on the persistent partition, which was measured and
            # then never judged.
            for name, label in (
                ("root", "the root filesystem"),
                ("ems_data", "the persistent partition"),
            ):
                entry = storage.get(name) or {}
                if entry.get("available") and (entry.get("used_percent") or 0) >= 90:
                    warnings.append(
                        {
                            "code": "storage_low" if name == "root" else "persistent_storage_low",
                            "message": f"{label} is nearly full",
                        }
                    )
                    level = HEALTH_DEGRADED

        last = None
        operations = sections.get("operations", {})
        if operations.get("status") == SECTION_OK:
            recent = operations.get("recent") or []
            succeeded = [item for item in recent if item.get("state") == "succeeded"]
            last = succeeded[0] if succeeded else None

        return {
            "level": level,
            "warnings": warnings,
            "last_successful_operation": last,
        }

    # --- logs ------------------------------------------------------------

    def read_log(self, source, lines=validation.DEFAULT_LOG_LINES):
        source = validation.validate_log_source(source)
        lines = validation.validate_line_count(lines)

        if source == validation.LOG_SOURCE_APPLIANCE_WEB:
            raw = self._unit_or_file(UNIT_APPLIANCE_WEB, self.paths.appliance_log, lines)
        elif source == validation.LOG_SOURCE_APPLIANCE_AGENT:
            raw = self._unit_or_file(UNIT_APPLIANCE_AGENT, None, lines)
        elif source == validation.LOG_SOURCE_OPERATIONS:
            raw = self._tail_file(self.paths.operations_log, lines)
        elif source == validation.LOG_SOURCE_AUDIT:
            raw = self._tail_file(self.paths.audit_log, lines)
        elif source == validation.LOG_SOURCE_ADMIN_CONTAINER:
            raw = self.docker.container_logs(self.config.admin_container, lines)
        elif source == validation.LOG_SOURCE_EMS_CONTAINER:
            raw = self.docker.container_logs(self.config.ems_container, lines)
        elif source == validation.LOG_SOURCE_DOCKER_DAEMON:
            raw = self.systemd.journal(UNIT_DOCKER, lines)
        elif source == validation.LOG_SOURCE_BOOT:
            raw = self.systemd.boot_warnings(lines)
        else:
            raw = self._tail_file(self.probe.root / DPKG_LOG, lines)

        bounded = bounded_redacted_log(raw, max_lines=lines)
        bounded["source"] = source
        return bounded

    def _unit_or_file(self, unit, fallback, lines):
        try:
            text = self.systemd.journal(unit, lines)
        except Exception:
            text = ""
        if text.strip():
            return text
        return self._tail_file(fallback, lines) if fallback is not None else ""

    def _tail_file(self, path, lines):
        try:
            content = path.read_text(encoding="utf-8", errors="replace")
        except (OSError, AttributeError):
            return ""
        return "\n".join(content.splitlines()[-int(lines) :])
