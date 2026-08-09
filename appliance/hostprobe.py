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
MEMINFO_FILE = "proc/meminfo"
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
        raw = (self._read(UPTIME_FILE) or "").split()
        seconds = float(raw[0]) if raw else 0.0
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
