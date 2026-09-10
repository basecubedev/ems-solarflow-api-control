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
    UNIT_BACKUP_ACCESS_DISABLE,
    UNIT_CONFIG_SEED,
    UNIT_DOCKER,
    UNIT_EXPORT,
    UNIT_GROW_ROOT,
    UNIT_MANAGER_INSTALL,
    UNIT_MANAGER_VERIFY,
    UNIT_SSHD_KEYS,
)

SECTION_OK = "ok"
SECTION_UNAVAILABLE = "unavailable"

HEALTH_HEALTHY = "healthy"
HEALTH_ATTENTION = "attention"
HEALTH_DEGRADED = "degraded"

FINDING_ERROR = "error"
FINDING_WARNING = "warning"

# Where a finding is acted on, in the manager's own view ids. An unreadable
# section routes to Diagnostics rather than to its own page: the probe that
# failed is the same one that page would have to render.
VIEW_DIAGNOSTICS = "diagnostics"
VIEW_OVERVIEW = "overview"
VIEW_ADMIN = "admin"
VIEW_UPDATES = "updates"

SECTION_LABELS = {
    "system": "Raspberry Pi",
    "docker": "Docker",
    "admin": "EMS Admin",
    "updates": "Updates",
    "network": "Network",
    "ssh": "SSH and backup access",
    "operations": "Operations",
}


def finding(code, severity, section, title, message, next_step):
    return {
        "code": code,
        "severity": severity,
        "section": section,
        "title": title,
        "message": message,
        "next_step": next_step,
    }


def health_level(findings):
    """The level is the worst finding, not a second opinion about the same host."""

    severities = {item["severity"] for item in findings}
    if FINDING_ERROR in severities:
        return HEALTH_DEGRADED
    if FINDING_WARNING in severities:
        return HEALTH_ATTENTION
    return HEALTH_HEALTHY


DPKG_LOG = "var/log/dpkg.log"

# One entry per unit this package ships whose journal is the only account
# of what it did. Routing is a table rather than another elif so a source
# that is declared and not routed fails instead of serving the dpkg log.
UNIT_LOG_SOURCES = {
    validation.LOG_SOURCE_MANAGER_INSTALL: UNIT_MANAGER_INSTALL,
    validation.LOG_SOURCE_MANAGER_VERIFY: UNIT_MANAGER_VERIFY,
    validation.LOG_SOURCE_EXPORT: UNIT_EXPORT,
    validation.LOG_SOURCE_CONFIG_SEED: UNIT_CONFIG_SEED,
    validation.LOG_SOURCE_GROW_ROOT: UNIT_GROW_ROOT,
    validation.LOG_SOURCE_SSHD_KEYS: UNIT_SSHD_KEYS,
    validation.LOG_SOURCE_BACKUP_ACCESS: UNIT_BACKUP_ACCESS_DISABLE,
}


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
        time_fn=None,
        installed_version=None,
    ):
        self.paths = paths
        self.config = config
        self.probe = probe
        # Resolved once by the composition root rather than looked up here, the
        # way ManagerUpdateService already takes it: dpkg is the authority on
        # which Manager is installed, and a service that asks it directly cannot
        # be told anything else by a test.
        self.installed_version = installed_version or ""
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
            "appliance_version": self.installed_version,
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
        """What apt has for this host.

        The operating system is patched in place, so there is one update path
        and one answer: what the package manager sees.
        """

        return self.packages.check().to_dict()

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
        sections["appliance_version"] = self.installed_version
        sections["collected_at"] = self._time()
        return sections

    def _health(self, sections):
        findings = []

        for name, payload in sections.items():
            if isinstance(payload, dict) and payload.get("status") == SECTION_UNAVAILABLE:
                label = SECTION_LABELS.get(name, name)
                findings.append(
                    finding(
                        f"{name}_unavailable",
                        FINDING_WARNING,
                        VIEW_DIAGNOSTICS,
                        f"{label} status could not be read",
                        f"{name} status is unavailable",
                        "Open Diagnostics and read the appliance log for the probe that failed.",
                    )
                )

        docker = sections.get("docker", {})
        if docker.get("status") == SECTION_OK:
            daemon = docker.get("daemon", {})
            if daemon.get("state") != DAEMON_RUNNING:
                findings.append(
                    finding(
                        "docker_not_running",
                        FINDING_ERROR,
                        VIEW_DIAGNOSTICS,
                        "Docker is not running",
                        "the Docker daemon is not running",
                        "Nothing containerised runs without it. Collect a support archive in "
                        "Diagnostics; a restart from the Overview is the usual repair.",
                    )
                )

        admin = sections.get("admin", {})
        if admin.get("status") == SECTION_OK:
            if not admin.get("installed"):
                findings.append(
                    finding(
                        "admin_not_installed",
                        FINDING_ERROR,
                        VIEW_ADMIN,
                        "No EMS Admin is installed",
                        "the EMS Admin container is missing",
                        "Open Admin and install it; that is where an EMS is set up.",
                    )
                )
            elif not admin.get("healthy"):
                findings.append(
                    finding(
                        "admin_unhealthy",
                        FINDING_ERROR,
                        VIEW_ADMIN,
                        "EMS Admin is not answering",
                        "the EMS Admin container is not healthy",
                        "Open Admin and restart it. Repair reinstalls the container if a "
                        "restart does not bring it back.",
                    )
                )

        updates = sections.get("updates", {})
        if updates.get("status") == SECTION_OK:
            if updates.get("security_count"):
                count = updates["security_count"]
                findings.append(
                    finding(
                        "security_updates_pending",
                        FINDING_WARNING,
                        VIEW_UPDATES,
                        "Security updates are waiting",
                        f"{count} security update(s) available",
                        "Open System Updates and install them.",
                    )
                )
            if updates.get("reboot_required"):
                findings.append(
                    finding(
                        "reboot_required",
                        FINDING_WARNING,
                        VIEW_OVERVIEW,
                        "A restart is needed to finish the updates",
                        "a reboot is required to finish updates",
                        "Restart the Raspberry Pi from the power actions on this page.",
                    )
                )
            if not (updates.get("package_manager") or {}).get("healthy", True):
                findings.append(
                    finding(
                        "package_manager_unhealthy",
                        FINDING_ERROR,
                        VIEW_UPDATES,
                        "The package manager needs recovery",
                        "the package manager needs recovery",
                        "No update can install until it is repaired. Open System Updates and "
                        "run the repair.",
                    )
                )

        system = sections.get("system", {})
        if system.get("status") == SECTION_OK:
            storage = system.get("storage") or {}
            # Everything grows on one root here: the OS, the Docker stores,
            # the journal, the EMS data and the operator's backups. Both
            # entries are judged, because the deployment root may be a separate
            # filesystem an operator mounted there.
            for name, label, code, title in (
                (
                    "root",
                    "the root filesystem",
                    "storage_low",
                    "The root filesystem is nearly full",
                ),
                (
                    "ems_data",
                    "the EMS deployment",
                    "persistent_storage_low",
                    "The EMS deployment is nearly full",
                ),
            ):
                entry = storage.get(name) or {}
                if entry.get("available") and (entry.get("used_percent") or 0) >= 90:
                    findings.append(
                        finding(
                            code,
                            FINDING_ERROR,
                            VIEW_DIAGNOSTICS,
                            title,
                            f"{label} is nearly full",
                            "Writes fail once it is full, including backups and updates. "
                            "Open Diagnostics to collect a support archive before freeing space.",
                        )
                    )

        last = None
        operations = sections.get("operations", {})
        if operations.get("status") == SECTION_OK:
            recent = operations.get("recent") or []
            succeeded = [item for item in recent if item.get("state") == "succeeded"]
            last = succeeded[0] if succeeded else None

        return {
            "level": health_level(findings),
            "warnings": findings,
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
        elif source in UNIT_LOG_SOURCES:
            raw = self._unit_or_file(UNIT_LOG_SOURCES[source], None, lines)
        elif source == validation.LOG_SOURCE_PACKAGES:
            raw = self._tail_file(self.probe.root / DPKG_LOG, lines)
        else:
            raise validation.ValidationError("log_source_unrouted", f"{source} has no reader")

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
