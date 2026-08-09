# SPDX-License-Identifier: AGPL-3.0-or-later
"""Typed systemd access.

Only units the appliance owns or depends on may be queried or changed. There is
no free-form unit name and no unit-file editing: ``systemctl edit`` and friends
are simply not reachable from here.
"""

UNIT_DOCKER = "docker.service"
UNIT_SSH = "ssh.service"
UNIT_AVAHI = "avahi-daemon.service"
UNIT_NETWORK_MANAGER = "NetworkManager.service"
UNIT_APPLIANCE_WEB = "ems-appliance-web.service"
UNIT_APPLIANCE_AGENT = "ems-appliance-agent.service"
UNIT_TIMESYNC = "systemd-timesyncd.service"

READABLE_UNITS = (
    UNIT_DOCKER,
    UNIT_SSH,
    UNIT_AVAHI,
    UNIT_NETWORK_MANAGER,
    UNIT_APPLIANCE_WEB,
    UNIT_APPLIANCE_AGENT,
    UNIT_TIMESYNC,
)

CONTROLLABLE_UNITS = (UNIT_DOCKER, UNIT_SSH)


class SystemdError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


class SystemdBackend:
    def __init__(self, runner):
        self.runner = runner

    def _check_readable(self, unit):
        if unit not in READABLE_UNITS:
            raise SystemdError("unit_not_allowed", f"{unit} is not an appliance-managed unit")
        return unit

    def _check_controllable(self, unit):
        if unit not in CONTROLLABLE_UNITS:
            raise SystemdError("unit_not_controllable", f"{unit} cannot be controlled from the UI")
        return unit

    def unit_state(self, unit):
        self._check_readable(unit)
        active = self.runner.run("systemctl", ["is-active", unit], timeout=15)
        enabled = self.runner.run("systemctl", ["is-enabled", unit], timeout=15)
        return {
            "unit": unit,
            "active": active.stdout.strip() or "unknown",
            "enabled": enabled.stdout.strip() or "unknown",
            "running": active.stdout.strip() == "active",
        }

    def unit_states(self, units=READABLE_UNITS):
        states = []
        for unit in units:
            try:
                states.append(self.unit_state(unit))
            except SystemdError:
                continue
        return states

    def start(self, unit):
        self._check_controllable(unit)
        return self.runner.run("systemctl", ["start", unit], timeout=120)

    def stop(self, unit):
        self._check_controllable(unit)
        return self.runner.run("systemctl", ["stop", unit], timeout=120)

    def reload(self, unit):
        self._check_controllable(unit)
        return self.runner.run("systemctl", ["reload", unit], timeout=120)

    def enable(self, unit):
        self._check_controllable(unit)
        return self.runner.run("systemctl", ["enable", "--now", unit], timeout=120)

    def disable(self, unit):
        self._check_controllable(unit)
        return self.runner.run("systemctl", ["disable", "--now", unit], timeout=120)

    def reboot(self):
        return self.runner.run("systemctl", ["reboot"], timeout=30)

    def shutdown(self):
        return self.runner.run("systemctl", ["poweroff"], timeout=30)

    def journal(self, unit, lines):
        self._check_readable(unit)
        result = self.runner.run(
            "journalctl",
            ["-u", unit, "-n", str(int(lines)), "--no-pager", "--output", "short-iso"],
            timeout=60,
        )
        return result.stdout or result.stderr or ""

    def boot_warnings(self, lines):
        result = self.runner.run(
            "journalctl",
            ["-b", "-p", "warning", "-n", str(int(lines)), "--no-pager", "--output", "short-iso"],
            timeout=60,
        )
        return result.stdout or result.stderr or ""
