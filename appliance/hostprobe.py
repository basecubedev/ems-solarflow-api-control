# SPDX-License-Identifier: AGPL-3.0-or-later
"""Read-only host inspection.

Every probe reads a file or runs a read-only tool and returns a plain mapping.
``root`` makes the whole set testable against a fixture tree instead of the
running machine.
"""

import os
import platform
import re
import time
from pathlib import Path

MODEL_FILE = "proc/device-tree/model"
OS_RELEASE_FILE = "etc/os-release"
UPTIME_FILE = "proc/uptime"


def uptime_seconds(root="/"):
    """Seconds since this boot, from the kernel.

    The one reader in this project. A wall clock on a board with no real-time
    clock does not survive a reboot, so anything measuring an interval across
    one has to ask the kernel instead.
    """

    try:
        raw = (Path(root) / UPTIME_FILE).read_text(encoding="utf-8", errors="replace").split()
    except OSError:
        return 0.0
    try:
        return float(raw[0]) if raw else 0.0
    except ValueError:
        return 0.0
MEMINFO_FILE = "proc/meminfo"
# PID 1 is in the initial mount namespace. The agent's own namespace is a
# snapshot taken when systemd applied ProtectHome/PrivateTmp, so /proc/self
# would keep reporting the mount table as it looked at service start.
HOST_MOUNTINFO_FILE = "proc/1/mountinfo"
MOUNTINFO_FILE = "proc/self/mountinfo"
HWMON_DIR = "sys/class/hwmon"
UNDERVOLTAGE_SENSOR = "rpi_volt"
UNDERVOLTAGE_ALARM = "in0_lcrit_alarm"

THERMAL_FILE = "sys/class/thermal/thermal_zone0/temp"
REBOOT_REQUIRED_FILE = "var/run/reboot-required"
REBOOT_REQUIRED_PKGS = "var/run/reboot-required.pkgs"
# Debian's and Raspberry Pi's kernel and firmware packages never write the
# marker above, so a new kernel and new boot files are read off the system.
RUNNING_KERNEL_FILE = "proc/sys/kernel/osrelease"
KERNEL_MODULES_DIR = "lib/modules"
BOOT_FIRMWARE_DIR = "boot/firmware"
DPKG_LOCK_FILE = "var/lib/dpkg/lock-frontend"
DPKG_DATABASE_LOCK_FILE = "var/lib/dpkg/lock"
APT_LISTS_DIR = "var/lib/apt/lists"
APT_DAILY_UPDATE_STAMP = "var/lib/apt/periodic/update-stamp"


class HostProbe:
    def __init__(self, runner=None, *, root="/", time_fn=None):
        self.runner = runner
        self.root = Path(root)
        self._time = time_fn or time.time

    def _read(self, relative):
        try:
            return (self.root / relative).read_text(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            return None

    def hardware(self):
        model = (self._read(MODEL_FILE) or "").strip().strip("\x00")
        return {
            "model": model or "unknown",
            "architecture": platform.machine(),
            "cpu_count": os.cpu_count() or 0,
        }

    def operating_system(self):
        values = {}
        for line in (self._read(OS_RELEASE_FILE) or "").splitlines():
            key, _, value = line.partition("=")
            if key:
                values[key.strip()] = value.strip().strip('"')
        return {
            "name": values.get("NAME", "unknown"),
            "version": values.get("VERSION", values.get("VERSION_ID", "unknown")),
            "version_id": values.get("VERSION_ID", ""),
            "codename": values.get("VERSION_CODENAME", ""),
            "id": values.get("ID", ""),
            "kernel": platform.release(),
            "bits": "64-bit" if platform.machine() in ("aarch64", "arm64", "x86_64") else "32-bit",
        }

    def uptime(self):
        seconds = uptime_seconds(self.root)
        return {"seconds": int(seconds), "days": int(seconds // 86400)}

    def system_time(self):
        now = self._time()
        record = {
            "epoch": now,
            "iso": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(now)),
            "timezone": time.strftime("%Z", time.localtime(now)),
            "ntp_synchronized": None,
        }
        if self.runner is not None and self.runner.available("timedatectl"):
            result = self.runner.run("timedatectl", ["show"], timeout=15)
            for line in (result.stdout or "").splitlines():
                key, _, value = line.partition("=")
                if key.strip() == "NTPSynchronized":
                    record["ntp_synchronized"] = value.strip().lower() == "yes"
                elif key.strip() == "Timezone" and value.strip():
                    record["timezone"] = value.strip()
            if record["ntp_synchronized"] is not True:
                record.update(self._time_servers())
        return record

    def _time_servers(self):
        """Which servers timesyncd asks, for a clock that is not synchronised."""

        result = self.runner.run("timedatectl", ["show-timesync"], timeout=15)
        fields = {}
        for line in (result.stdout or "").splitlines():
            key, _, value = line.partition("=")
            fields[key.strip()] = value.split()
        servers = fields.get("SystemNTPServers", []) + fields.get("LinkNTPServers", [])
        return {
            "ntp_servers": servers,
            "ntp_fallback_servers": fields.get("FallbackNTPServers", []),
        }

    def temperature(self):
        raw = (self._read(THERMAL_FILE) or "").strip()
        if not raw:
            return {"celsius": None, "available": False}
        try:
            value = float(raw)
        except ValueError:
            return {"celsius": None, "available": False}
        celsius = value / 1000.0 if value > 200 else value
        return {"celsius": round(celsius, 1), "available": True}

    def power(self):
        """Whether the board is reporting an under-voltage condition.

        A Pi that browns out under load corrupts a card write and produces
        failures that look like anything but a power supply. The alarm is a
        sticky bit on the Raspberry Pi hwmon device; a board that does not
        publish it is reported as unknown rather than as healthy.
        """

        raw = self._undervoltage_alarm()
        if raw not in ("0", "1"):
            return {"available": False, "under_voltage": None}
        return {"available": True, "under_voltage": raw == "1"}

    def _undervoltage_alarm(self):
        """The board's own under-voltage bit, wherever its hwmon device landed.

        The index depends on which drivers registered first, so a fixed
        hwmon0 reports the supply as unknown on a board that is browning out.
        A device that names itself answers; an unnamed one is only a fallback,
        because a foreign rail's alarm is not this board's supply.
        """

        try:
            names = sorted(entry.name for entry in (self.root / HWMON_DIR).iterdir())
        except OSError:
            return None
        fallback = None
        for name in names:
            alarm = (self._read(f"{HWMON_DIR}/{name}/{UNDERVOLTAGE_ALARM}") or "").strip()
            if alarm not in ("0", "1"):
                continue
            if (self._read(f"{HWMON_DIR}/{name}/name") or "").strip() == UNDERVOLTAGE_SENSOR:
                return alarm
            if fallback is None:
                fallback = alarm
        return fallback

    def memory(self):
        values = {}
        for line in (self._read(MEMINFO_FILE) or "").splitlines():
            key, _, value = line.partition(":")
            digits = value.strip().split(" ")[0]
            if digits.isdigit():
                values[key.strip()] = int(digits)
        total = values.get("MemTotal", 0)
        available = values.get("MemAvailable", 0)
        used = max(total - available, 0)
        return {
            "total_mb": total // 1024,
            "available_mb": available // 1024,
            "used_mb": used // 1024,
            "used_percent": round(used * 100.0 / total, 1) if total else None,
        }

    def _path(self, absolute):
        root = Path(self.root)
        if root == Path("/"):
            return Path(absolute)
        return root / str(absolute).lstrip("/")

    def filesystem(self, path):
        """Usage of the filesystem carrying ``path``, through the probe root.

        Every other read here is root-relative; this one was not, so a test
        host answered from its own ``/var`` and the disk-space blocker fired --
        or did not -- depending on how full the machine running the suite was.
        """

        target = self._path(path)
        try:
            stats = os.statvfs(str(target))
        except OSError:
            return {"path": str(target), "available": False}
        block = stats.f_frsize
        total = stats.f_blocks * block
        free = stats.f_bavail * block
        used = total - stats.f_bfree * block
        return {
            "path": str(target),
            "available": True,
            "total_mb": total // (1024 * 1024),
            "free_mb": free // (1024 * 1024),
            "used_mb": used // (1024 * 1024),
            "used_percent": round(used * 100.0 / total, 1) if total else None,
        }

    def mounts(self):
        """Mount point → option set, as the host sees it right now."""

        return {target: record["options"] for target, record in self.mount_records().items()}

    def mount_records(self):
        """Mount point → what the kernel publishes there.

        ``root`` is the path of the mounted subtree inside its own filesystem
        and ``device`` is that filesystem's device number, so together they say
        which directory a bind mount actually exposes.
        """

        raw = self._read(HOST_MOUNTINFO_FILE)
        if not raw:
            raw = self._read(MOUNTINFO_FILE)
        table = {}
        for line in (raw or "").splitlines():
            fields = line.split(" ")
            if len(fields) < 6:
                continue
            try:
                separator = fields.index("-", 6)
            except ValueError:
                separator = len(fields)
            table[fields[4].replace("\\040", " ")] = {
                "options": frozenset(fields[5].split(",")),
                "device": fields[2],
                "root": fields[3].replace("\\040", " "),
                "fstype": fields[separator + 1] if len(fields) > separator + 1 else "",
                "source": fields[separator + 2] if len(fields) > separator + 2 else "",
            }
        return table

    def reboot_required(self):
        """Whether a restart is owed, and what owes it.

        ``/run/reboot-required`` is written by a few packages only -- dbus is
        one -- and by no kernel, firmware or libc package on Debian or
        Raspberry Pi OS, so after those the page said "not required" and old
        code kept running. A kernel of the running flavour newer than the
        running one, or boot files written since this boot, owe one too.
        """

        marker = self.root / REBOOT_REQUIRED_FILE
        packages = []
        raw = self._read(REBOOT_REQUIRED_PKGS)
        if raw:
            packages = sorted({line.strip() for line in raw.splitlines() if line.strip()})
        kernel = self._newer_kernel()
        if kernel:
            packages.append(f"kernel {kernel}")
        firmware = self._boot_files_since_boot()
        if firmware:
            packages.append("boot firmware")
        return {
            "required": marker.exists() or bool(kernel) or firmware,
            "packages": packages,
        }

    def _newer_kernel(self):
        """The newest installed kernel of the running flavour, if it is not the running one."""

        running = (self._read(RUNNING_KERNEL_FILE) or "").strip()
        if not running:
            return ""
        flavour = running.partition("+")[2] or running.rpartition("-")[2]
        try:
            installed = [
                entry.name
                for entry in (self.root / KERNEL_MODULES_DIR).iterdir()
                if entry.is_dir() and entry.name.endswith(flavour)
            ]
        except OSError:
            return ""
        if not installed:
            return ""
        newest = max(installed, key=_kernel_key)
        return newest if _kernel_key(newest) > _kernel_key(running) else ""

    def _boot_files_since_boot(self):
        """Whether a file the bootloader reads was written after this boot."""

        uptime = uptime_seconds(self.root)
        if not uptime:
            return False
        booted = self._time() - uptime
        try:
            entries = list((self.root / BOOT_FIRMWARE_DIR).iterdir())
        except OSError:
            return False
        for entry in entries:
            try:
                if entry.is_file() and entry.stat().st_mtime > booted:
                    return True
            except OSError:
                continue
        return False

    def hostname(self):
        record = {"hostname": platform.node(), "mdns": ""}
        if self.runner is not None and self.runner.available("hostnamectl"):
            result = self.runner.run("hostnamectl", ["--static"], timeout=15)
            if result.ok and result.stdout.strip():
                record["hostname"] = result.stdout.strip()
        if record["hostname"]:
            record["mdns"] = f"{record['hostname']}.local"
        return record


def _kernel_key(release):
    """A kernel release as numbers and words, so 6.18.52 sorts after 6.18.9."""

    return tuple(
        (0, int(part), "") if part.isdigit() else (1, 0, part)
        for part in re.findall(r"\d+|[A-Za-z]+", release)
    )


def host_architecture(machine=None):
    """This machine's OCI architecture name, or nothing if it has no mapping.

    The Appliance Manager's own package index is per-architecture, so a release
    that was built for another board must be refused before it is unpacked
    rather than after.
    """

    known = {"aarch64": "arm64", "arm64": "arm64", "x86_64": "amd64", "amd64": "amd64"}
    return known.get(machine if machine is not None else platform.machine(), "")
