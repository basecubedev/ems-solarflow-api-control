# SPDX-License-Identifier: AGPL-3.0-or-later
"""Read-only host inspection.

Every probe reads a file or runs a read-only tool and returns a plain mapping.
``root`` makes the whole set testable against a fixture tree instead of the
running machine.
"""

import os
import platform
import time
from pathlib import Path

MODEL_FILE = "proc/device-tree/model"
OS_RELEASE_FILE = "etc/os-release"
UPTIME_FILE = "proc/uptime"


def uptime_seconds(root="/"):
    """Seconds since this boot, from the kernel.

    The one reader in this project. It is what the A/B trial window measures,
    because a wall clock on a board with no real-time clock does not survive
    the reboot the window spans.
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
THERMAL_FILE = "sys/class/thermal/thermal_zone0/temp"
REBOOT_REQUIRED_FILE = "var/run/reboot-required"
REBOOT_REQUIRED_PKGS = "var/run/reboot-required.pkgs"
DPKG_LOCK_FILE = "var/lib/dpkg/lock-frontend"
APT_LISTS_DIR = "var/lib/apt/lists"


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
        return record

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

    def filesystem(self, path):
        target = Path(path)
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
        marker = self.root / REBOOT_REQUIRED_FILE
        packages = []
        raw = self._read(REBOOT_REQUIRED_PKGS)
        if raw:
            packages = sorted({line.strip() for line in raw.splitlines() if line.strip()})
        return {"required": marker.exists(), "packages": packages}

    def hostname(self):
        record = {"hostname": platform.node(), "mdns": ""}
        if self.runner is not None and self.runner.available("hostnamectl"):
            result = self.runner.run("hostnamectl", ["--static"], timeout=15)
            if result.ok and result.stdout.strip():
                record["hostname"] = result.stdout.strip()
        if record["hostname"]:
            record["mdns"] = f"{record['hostname']}.local"
        return record
