# SPDX-License-Identifier: AGPL-3.0-or-later
"""Deterministic Appliance Manager server for browser tests.

Runs the real web service and the real agent handlers against the scripted
``FakeHost`` from the Python test suite, so the browser drives production code
without touching Docker, apt, systemd or NetworkManager.

Started by ``tests/e2e-appliance/run-appliance.sh``; never part of the package.
"""

import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from appliance.agent import AgentHandlers  # noqa: E402
from appliance.agent_client import InProcessAgentClient  # noqa: E402
from appliance.auth import AuthStore  # noqa: E402
from appliance.web import ApplianceWebApp, ApplianceWebServer  # noqa: E402
from tests.helpers.appliance import (  # noqa: E402
    ADMIN_CONTAINER,
    APT_SIMULATION,
    ADMIN_REPOSITORY,
    build_test_services,
    SSHD_BACKUP_MATCH,
    StaticCatalogue,
)

OS_RELEASE = """NAME="Raspberry Pi OS"
VERSION="12 (bookworm)"
VERSION_ID="12"
VERSION_CODENAME=bookworm
ID=debian
"""


class _OfflineAgent:
    """An agent socket that is simply not there."""

    def call(self, operation, **kwargs):
        from appliance.agent_client import AgentUnavailableError

        raise AgentUnavailableError("the appliance agent is not reachable")

    def available(self):
        return False


def seed_host_files(root):
    (root / "etc").mkdir(parents=True, exist_ok=True)
    (root / "etc" / "os-release").write_text(OS_RELEASE, encoding="utf-8")
    (root / "proc" / "device-tree").mkdir(parents=True, exist_ok=True)
    (root / "proc" / "device-tree" / "model").write_text("Raspberry Pi 5 Model B Rev 1.0\x00")
    (root / "proc" / "uptime").write_text("1036800.00 900000.00\n", encoding="utf-8")
    (root / "proc" / "meminfo").write_text(
        "MemTotal:        8054304 kB\nMemAvailable:    6000000 kB\n", encoding="utf-8"
    )
    thermal = root / "sys" / "class" / "thermal" / "thermal_zone0"
    thermal.mkdir(parents=True, exist_ok=True)
    (thermal / "temp").write_text("51234\n", encoding="utf-8")
    (root / "var" / "lib" / "dpkg").mkdir(parents=True, exist_ok=True)
    (root / "var" / "lib" / "dpkg" / "lock-frontend").write_text("", encoding="utf-8")
    (root / "var" / "lib" / "apt" / "lists").mkdir(parents=True, exist_ok=True)
    (root / "var" / "run").mkdir(parents=True, exist_ok=True)
    return root


def main():
    os.environ["EMS_APPLIANCE_TEST_MODE"] = "1"
    port = int(os.environ.get("EMS_APPLIANCE_E2E_PORT", "8124"))
    root = Path(tempfile.mkdtemp(prefix="ems-appliance-e2e-"))
    seed_host_files(root)

    services = build_test_services(root, catalogue=StaticCatalogue(["v1.1.0", "v1.0.0"]))
    host = services.host
    host.write_deployment(tag="v1.0.0")
    host.publish_image("v1.0.0")
    host.publish_image("v1.1.0")
    host.pull_local(f"{ADMIN_REPOSITORY}:v1.0.0")
    host.run_container(ADMIN_CONTAINER, f"{ADMIN_REPOSITORY}:v1.0.0")
    host.run_container("ems-solarflow", "ghcr.io/basecubedev/ems-solarflow-api-control:v1.0.0")

    backup_home = root / "home" / "ems-backup"
    backup_home.mkdir(parents=True, exist_ok=True)
    host.add_account("ems-backup", backup_home)

    live = {"app": None, "agent": None}

    def seed_appliance_state(options=None):
        """Restore the scripted host so browser tests do not depend on order."""

        options = options or {}
        # Agent state is privileged; the harness owns it here, the web service
        # never does.
        for entry in services.paths.operations_dir.glob("*.json"):
            entry.unlink()
        for entry in services.known_good.directory.glob("*.json"):
            entry.unlink()
        services.known_good.record(
            admin_image=f"{ADMIN_REPOSITORY}:v0.9.0",
            admin_digest="sha256:" + "9" * 64,
            admin_version="v0.9.0",
        )
        services.known_good.record(
            admin_image=f"{ADMIN_REPOSITORY}:v1.0.0",
            admin_digest=host.registry[f"{ADMIN_REPOSITORY}:v1.0.0"]["_digest"],
            admin_version="v1.0.0",
        )
        host.write_deployment(tag="v1.0.0")
        host.run_container(ADMIN_CONTAINER, f"{ADMIN_REPOSITORY}:v1.0.0")
        host.apt_simulation = APT_SIMULATION
        # Re-publish every scripted image so a previous test that broke one
        # cannot leak into the next.
        host.publish_image("v1.0.0")
        host.publish_image("v1.1.0")
        host.pull_local(f"{ADMIN_REPOSITORY}:v1.0.0")
        host.tools.add("docker")
        host.docker_running = True
        host.write_export_mounts(read_only=True)
        # authorized_keys survives on the scripted host, so without this the
        # second browser project deploying the same key gets
        # duplicate_public_key and an empty plan dialog.
        for account in services.config.ssh_key_accounts:
            try:
                services.ssh.keystore(account).revoke_all()
            except Exception:
                continue
        host.units["ssh.service"] = {"active": "inactive", "enabled": "disabled"}
        host.sshd_backup_match = SSHD_BACKUP_MATCH.format(export_root=services.paths.export_root)
        host.ss_exit_code, host.ss_stderr, host.listening_ports = 0, "", ""
        services.paths.export_status_file.unlink(missing_ok=True)
        if live["app"] is not None:
            live["app"].agent = live["agent"]
            live["app"].audit.agent = live["agent"]
            live["app"].audit.unrecorded_events = 0
            live["app"].audit.recorded_events = 0
            live["app"].audit.last_error = ""

        if options.get("break_compose"):
            (services.paths.install_root / "docker-compose.admin.yml").unlink()
        if options.get("break_digest"):
            host.registry[f"{ADMIN_REPOSITORY}:v1.1.0"]["RepoDigests"] = []
        if options.get("agent_offline") and live["app"] is not None:
            # Authentication must keep working while the audit trail cannot be
            # written; the UI has to say so instead of implying it was.
            live["app"].audit.agent = _OfflineAgent()
        if options.get("admin_unreachable"):
            host.publish_image("v1.0.0", healthy=False)
            host.pull_local(f"{ADMIN_REPOSITORY}:v1.0.0")
            host.run_container(ADMIN_CONTAINER, f"{ADMIN_REPOSITORY}:v1.0.0", health="none")
        if options.get("ab_deployment_drift"):
            # An OS update refused before the first destructive byte. The
            # browser has to say that nothing was written, because an operator
            # reading "incomplete" would otherwise go looking for an outage.
            from appliance.operations import STATE_FAILED_RECOVERABLE

            operation = services.operations.create("ab.update", {"kind": "update"})
            services.operations.await_confirmation(operation.operation_id, {"plan": True})
            record = services.operations.get(operation.operation_id, include_token=True)
            services.operations.confirm(operation.operation_id, record.confirmation_token)
            services.operations.finish(
                operation.operation_id,
                STATE_FAILED_RECOVERABLE,
                stage="preflight_failed",
                result={
                    "default_slot_unchanged": True,
                    "inactive_slot_untouched": True,
                    "replan_required": True,
                    "target_slot": "B",
                },
                error={
                    "code": "deployment_authority_drift",
                    "message": "the EMS deployment changed after this update was confirmed",
                },
            )
        if options.get("rollback_image_missing"):
            previous = services.known_good.previous()
            host.images.pop(previous["admin_reference"], None)
            host.registry.pop(previous["admin_reference"], None)
        if options.get("export_read_write"):
            host.write_export_mounts(read_only=False)
        if options.get("forwarding_allowed"):
            # The chroot is in force but the daemon still permits forwarding:
            # confinement that is partly enforced is not confinement.
            host.sshd_backup_match = host.sshd_backup_match.replace(
                "allowtcpforwarding no", "allowtcpforwarding yes"
            )
        if options.get("export_source_rejected"):
            services.paths.export_status_file.parent.mkdir(parents=True, exist_ok=True)
            services.paths.export_status_file.write_text(
                json.dumps(
                    {
                        "status": "failed",
                        "detail": "config is a symlink; an export source must be a real "
                        "directory in /opt/ems-solarflow",
                        "paths": [],
                    }
                ),
                encoding="utf-8",
            )
        if options.get("port_check_broken"):
            host.ss_exit_code = 1
            host.ss_stderr = "Cannot open netlink socket: Address family not supported"
        if options.get("docker_missing"):
            host.tools.discard("docker")
            host.docker_running = False

    seed_appliance_state({})

    agent = InProcessAgentClient(AgentHandlers(services, executor=lambda target: target()))
    app = ApplianceWebApp(paths=services.paths, config=services.config, agent=agent)
    app.auth = AuthStore(services.paths.auth_file, iterations=1000)
    app.test_reset_hook = seed_appliance_state
    live["app"], live["agent"] = app, agent

    server = ApplianceWebServer(app, ("127.0.0.1", port))
    print(f"appliance test server on http://127.0.0.1:{port} (state {root})", flush=True)
    try:
        server.serve_forever(poll_interval=0.05)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
