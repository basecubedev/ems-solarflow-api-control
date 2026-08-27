# SPDX-License-Identifier: AGPL-3.0-or-later
"""Network overview, WLAN changes with automatic revert, and hostname changes.

A WLAN change can disconnect the operator, so the previously working profile is
kept and reactivated when the new one does not reach the operator within the
configured timeout. What "reached" means is measured on the WLAN device itself
-- joined the requested SSID and holds an address -- and never on host-wide
connectivity, which NetworkManager decides by fetching a URL over the internet
and which therefore answers a different question in both directions. WLAN
passphrases are handed to ``nmcli`` on stdin, never in argv, so they never
appear in the host process table.
"""

import json
import time

from pathlib import Path

from dataclasses import dataclass, field

from appliance.operations import STATE_FAILED_TERMINAL, STATE_SUCCEEDED, STATE_VERIFYING
from appliance.paths import atomic_write
from appliance.validation import validate_hostname

TYPE_WIFI = "network.wifi"
TYPE_HOSTNAME = "network.hostname"

STATE_CONNECTED = "connected"

WIFI_PROFILE_PREFIX = "ems-appliance-"


class NetworkError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class Interface:
    device: str
    kind: str
    state: str
    connection: str = ""
    addresses: list = field(default_factory=list)
    gateway: str = ""
    dns: list = field(default_factory=list)
    ssid: str = ""
    signal: int = None

    def to_dict(self):
        return {
            "device": self.device,
            "type": self.kind,
            "state": self.state,
            "connection": self.connection,
            "addresses": list(self.addresses),
            "gateway": self.gateway,
            "dns": list(self.dns),
            "ssid": self.ssid,
            "signal": self.signal,
        }


def _split_escaped(line):
    fields, current, escaped = [], "", False
    for char in line:
        if escaped:
            current += char
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == ":":
            fields.append(current)
            current = ""
        else:
            current += char
    fields.append(current)
    return fields


def parse_device_status(text):
    devices = []
    for line in (text or "").splitlines():
        if not line.strip():
            continue
        parts = _split_escaped(line)
        if len(parts) < 3:
            continue
        devices.append(
            Interface(
                device=parts[0],
                kind=parts[1],
                state=parts[2],
                connection=parts[3] if len(parts) > 3 else "",
            )
        )
    return devices


def parse_device_details(text):
    details = {"addresses": [], "gateway": "", "dns": [], "ssid": ""}
    for line in (text or "").splitlines():
        key, _, value = line.partition(":")
        key, value = key.strip(), value.strip()
        if not key or not value:
            continue
        if key.startswith("IP4.ADDRESS"):
            details["addresses"].append(value)
        elif key.startswith("IP6.ADDRESS"):
            details["addresses"].append(value)
        elif key == "IP4.GATEWAY":
            details["gateway"] = value
        elif key.startswith("IP4.DNS"):
            details["dns"].append(value)
        elif key in ("GENERAL.CONNECTION", "GENERAL.SSID"):
            if key == "GENERAL.SSID":
                details["ssid"] = value
    return details


def parse_wifi_list(text):
    networks = []
    for line in (text or "").splitlines():
        if not line.strip():
            continue
        parts = _split_escaped(line)
        if len(parts) < 4:
            continue
        active, ssid, signal, security = parts[0], parts[1], parts[2], parts[3]
        if not ssid:
            continue
        networks.append(
            {
                "ssid": ssid,
                "active": active.lower() in ("yes", "*"),
                "signal": int(signal) if signal.isdigit() else None,
                "security": security or "open",
            }
        )
    networks.sort(key=lambda item: (item["signal"] or 0), reverse=True)
    return networks


class NetworkService:
    def __init__(self, *, runner, probe, config, operations, time_fn=None, sleep=None,
                 operation_log=None, revert_intent_dir=None):
        self.runner = runner
        self.probe = probe
        self.config = config
        self.operations = operations
        self._time = time_fn or time.monotonic
        self._sleep = sleep or time.sleep
        self._operation_log = operation_log
        self._revert_intent_dir = revert_intent_dir
        # WLAN passphrases live in memory between plan and apply only; they are
        # never written to the operation record, a log or the state directory.
        self._secrets = {}

    @property
    def available(self):
        return self.runner.available("nmcli")

    # --- read-only -------------------------------------------------------

    def status(self):
        hostname = self.probe.hostname()
        record = {
            "hostname": hostname["hostname"],
            "mdns": hostname["mdns"],
            "manager": "NetworkManager" if self.available else "unavailable",
            "connectivity": "unknown",
            "interfaces": [],
            "active_connection": "",
            "error": "",
        }
        if not self.available:
            record["error"] = "network_manager_unavailable"
            return record

        general = self.runner.run("nmcli", ["-t", "-f", "STATE,CONNECTIVITY", "general"], timeout=20)
        if general.ok and general.stdout.strip():
            parts = _split_escaped(general.stdout.strip().splitlines()[0])
            record["connectivity"] = parts[1] if len(parts) > 1 else "unknown"
            record["state"] = parts[0]

        status = self.runner.run(
            "nmcli", ["-t", "-f", "DEVICE,TYPE,STATE,CONNECTION", "device", "status"], timeout=20
        )
        interfaces = parse_device_status(status.stdout if status.ok else "")
        for interface in interfaces:
            if interface.kind in ("loopback",):
                continue
            detail = self.runner.run(
                "nmcli",
                ["-t", "-f", "IP4,IP6,GENERAL", "device", "show", interface.device],
                timeout=20,
            )
            parsed = parse_device_details(detail.stdout if detail.ok else "")
            interface.addresses = parsed["addresses"]
            interface.gateway = parsed["gateway"]
            interface.dns = parsed["dns"]
            if interface.kind == "wifi":
                interface.ssid = parsed["ssid"] or interface.connection
                interface.signal = self._signal_for(interface.ssid)
            record["interfaces"].append(interface.to_dict())
            if interface.state.startswith(STATE_CONNECTED) and not record["active_connection"]:
                record["active_connection"] = interface.connection
        return record

    def _signal_for(self, ssid):
        """Signal strength is decoration on the overview, never its verdict.

        A scan that failed is reported by the scan operation; making the whole
        status call fail over a missing signal bar would replace a degraded
        overview with no overview at all.
        """

        try:
            networks = self.scan(rescan=False)
        except NetworkError:
            return None
        for network in networks:
            if network["ssid"] == ssid:
                return network["signal"]
        return None

    def scan(self, *, rescan=True):
        if not self.available:
            return []
        args = ["-t", "-f", "ACTIVE,SSID,SIGNAL,SECURITY", "device", "wifi", "list"]
        if rescan:
            args += ["--rescan", "yes"]
        result = self.runner.run("nmcli", args, timeout=60)
        if not result.ok:
            raise NetworkError(
                "wifi_scan_failed",
                "the WLAN scan did not complete; no network list could be read",
            )
        return parse_wifi_list(result.stdout)

    def active_wifi_profile(self):
        result = self.runner.run(
            "nmcli", ["-t", "-f", "NAME,TYPE,DEVICE", "connection", "show", "--active"], timeout=20
        )
        for line in (result.stdout or "").splitlines():
            parts = _split_escaped(line)
            if len(parts) >= 2 and "wireless" in parts[1]:
                return parts[0]
        return ""

    def connectivity(self):
        result = self.runner.run("nmcli", ["-t", "-f", "CONNECTIVITY", "general"], timeout=20)
        return (result.stdout or "").strip() or "unknown"

    def wifi_link(self, ssid):
        """Whether a WLAN device actually joined ``ssid`` and reached L3.

        This is what "the operator can still reach me" depends on. Host-wide
        connectivity is a different question: NetworkManager decides it by
        fetching a URL over the internet, so with a cable plugged in it reads
        ``full`` whatever the radio did, and on a LAN with no internet or a
        filtering resolver it never reads ``full`` even when the join was
        perfect. Neither direction is a verdict about the WLAN.
        """

        status = self.runner.run(
            "nmcli", ["-t", "-f", "DEVICE,TYPE,STATE,CONNECTION", "device", "status"], timeout=20
        )
        for interface in parse_device_status(status.stdout if status.ok else ""):
            if interface.kind != "wifi":
                continue
            if not interface.state.startswith(STATE_CONNECTED):
                continue
            detail = self.runner.run(
                "nmcli",
                ["-t", "-f", "IP4,IP6,GENERAL", "device", "show", interface.device],
                timeout=20,
            )
            parsed = parse_device_details(detail.stdout if detail.ok else "")
            joined = parsed["ssid"] or interface.connection
            if joined != ssid:
                continue
            if parsed["addresses"]:
                return True
        return False

    # --- planning --------------------------------------------------------

    def plan_wifi(self, operation, *, ssid, passphrase, hidden=False):
        if not self.available:
            raise NetworkError("network_manager_unavailable", "NetworkManager is not available")

        previous = self.active_wifi_profile()
        networks = {item["ssid"]: item for item in self.scan(rescan=True)}
        visible = networks.get(ssid)
        if visible is None and not hidden:
            raise NetworkError(
                "wifi_network_not_found",
                f"{ssid} was not found; mark it hidden if the network does not broadcast",
            )

        values = {
            "ssid": ssid,
            "hidden": bool(hidden),
            "previous_profile": previous,
            "has_passphrase": bool(passphrase),
        }
        operation.requested_target.update(values)
        self.operations.update_target(operation.operation_id, values)
        self._secrets[operation.operation_id] = passphrase

        return {
            "type": TYPE_WIFI,
            "ssid": ssid,
            "hidden": bool(hidden),
            "security": (visible or {}).get("security", "unknown"),
            "signal": (visible or {}).get("signal"),
            "previous_profile": previous,
            "revert_timeout_seconds": self.config.wifi_revert_timeout_seconds,
            "warning": "Applying a WLAN change can disconnect this browser session. "
            "The previous profile is kept and reactivated automatically if the new "
            "network does not reach connectivity.",
        }

    def plan_hostname(self, operation, hostname):
        current = self.probe.hostname()
        target = validate_hostname(hostname)
        if target == current["hostname"]:
            raise NetworkError("hostname_unchanged", "this is already the appliance hostname")
        values = {"hostname": target, "previous_hostname": current["hostname"]}
        operation.requested_target.update(values)
        self.operations.update_target(operation.operation_id, values)
        return {
            "type": TYPE_HOSTNAME,
            "hostname": target,
            "previous_hostname": current["hostname"],
            "new_url": f"http://{target}.local:{self.config.web_port}",
            "admin_url": f"http://{target}.local:{self.config.admin_port}",
            "warning": "The appliance URL changes. Bookmarks using the old name stop working.",
        }

    # --- the revert has to outlive the call that armed it -------------------

    def _revert_intent_path(self):
        if self._revert_intent_dir is None:
            return None
        return Path(self._revert_intent_dir) / "wifi-revert.json"

    def arm_revert(self, operation_id, previous_profile):
        """Record the profile to fall back to before nmcli is touched at all.

        The revert used to be a step of the executing call, so a power cut or an
        agent restart inside the window left the new profile active with nothing
        to take it back -- and NetworkManager reconnects to it on every boot
        afterwards, which is the lockout this whole feature exists to prevent.
        """

        path = self._revert_intent_path()
        if path is None or not previous_profile:
            return None
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(
            path,
            json.dumps(
                {"operation_id": str(operation_id), "previous_profile": str(previous_profile)}
            )
            + "\n",
        )
        return str(path)

    def pending_revert(self):
        path = self._revert_intent_path()
        if path is None or not path.is_file():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def disarm_revert(self):
        path = self._revert_intent_path()
        if path is not None:
            try:
                path.unlink()
            except OSError:
                pass

    def recover_revert(self):
        """Reactivate an armed profile the process that armed it never reached."""

        intent = self.pending_revert()
        if not intent:
            return None
        previous = str(intent.get("previous_profile") or "")
        if not previous:
            self.disarm_revert()
            return None
        self._revert(previous)
        self.disarm_revert()
        return previous

    def discard_secret(self, operation_id):
        """Drop a passphrase a plan will never apply.

        The plan-to-apply window is the whole intended lifetime, but a plan that
        is cancelled, expires or fails before execution never reaches the pop in
        _execute_wifi, so without this the PSK for every network the operator
        thought about stays in the long-lived root process.
        """

        self._secrets.pop(operation_id, None)

    # --- execution -------------------------------------------------------

    def execute(self, operation):
        if operation.type == TYPE_WIFI:
            return self._execute_wifi(operation)
        if operation.type == TYPE_HOSTNAME:
            return self._execute_hostname(operation)
        raise NetworkError("unknown_operation_type", f"{operation.type} is not executable")

    def _execute_wifi(self, operation):
        target = operation.requested_target
        ssid = target["ssid"]
        previous = target.get("previous_profile") or ""
        passphrase = self._secrets.pop(operation.operation_id, "")

        self.arm_revert(operation.operation_id, previous)

        self._advance(operation, "applying_wifi")
        args = ["device", "wifi", "connect", ssid]
        if target.get("hidden"):
            args += ["hidden", "yes"]
        input_text = None
        if passphrase:
            # The passphrase goes to nmcli on stdin so it never appears in argv
            # and therefore never in the host process table.
            args.append("--ask")
            input_text = f"{passphrase}\n"
        result = self.runner.run("nmcli", args, timeout=90, input_text=input_text)

        self._advance(operation, "waiting_for_connectivity", state=STATE_VERIFYING)
        connected = self._wait_for_wifi(ssid)

        if result.ok and connected:
            self.disarm_revert()
            payload = {
                "ssid": ssid,
                "connectivity": self.connectivity(),
                "reverted": False,
                "previous_profile": previous,
            }
            self.operations.finish(operation.operation_id, STATE_SUCCEEDED, result=payload)
            return payload

        self._advance(operation, "reverting_wifi")
        reverted = self._revert(previous)
        self.disarm_revert()
        payload = {
            "ssid": ssid,
            "reverted": reverted,
            "previous_profile": previous,
            "connectivity": self.connectivity(),
        }
        self.operations.finish(
            operation.operation_id,
            STATE_FAILED_TERMINAL,
            stage="reverted" if reverted else "revert_failed",
            error={
                "code": "wifi_connection_failed",
                "message": "the new WLAN did not reach connectivity"
                + ("; the previous network was restored" if reverted else ""),
            },
            result=payload,
        )
        raise NetworkError("wifi_connection_failed", "the new WLAN did not reach connectivity")

    def _wait_for_wifi(self, ssid):
        deadline = self._time() + self.config.wifi_revert_timeout_seconds
        while True:
            if self.wifi_link(ssid):
                return True
            if self._time() >= deadline:
                return False
            self._sleep(3)

    def _revert(self, previous_profile):
        """Reactivate the saved profile. The previous profile is never deleted."""

        if not previous_profile:
            return False
        result = self.runner.run("nmcli", ["connection", "up", previous_profile], timeout=90)
        return bool(result.ok)

    def _execute_hostname(self, operation):
        target = operation.requested_target
        hostname = target["hostname"]

        self._advance(operation, "applying_hostname")
        result = self.runner.run("hostnamectl", ["set-hostname", hostname], timeout=30)
        if not result.ok:
            self.operations.finish(
                operation.operation_id,
                STATE_FAILED_TERMINAL,
                stage="hostname_failed",
                error={"code": "hostname_change_failed", "message": "hostnamectl reported an error"},
            )
            raise NetworkError("hostname_change_failed", "the hostname could not be changed")

        self._advance(operation, "updating_mdns", state=STATE_VERIFYING)
        self.runner.run("systemctl", ["try-restart", "avahi-daemon.service"], timeout=60)

        payload = {
            "hostname": hostname,
            "previous_hostname": target.get("previous_hostname", ""),
            "new_url": f"http://{hostname}.local:{self.config.web_port}",
            "admin_url": f"http://{hostname}.local:{self.config.admin_port}",
        }
        self.operations.finish(operation.operation_id, STATE_SUCCEEDED, result=payload)
        return payload

    def _advance(self, operation, stage, *, state=None, detail=None):
        self.operations.advance(operation.operation_id, stage, state=state, detail=detail)
        if self._operation_log is not None:
            self._operation_log.record(
                operation.operation_id, stage, operation_type=operation.type, detail=detail
            )
