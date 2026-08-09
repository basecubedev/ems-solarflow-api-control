# SPDX-License-Identifier: AGPL-3.0-or-later
"""Deterministic Appliance Manager server for browser tests.

Runs the real web service and the real agent handlers against the scripted
``FakeHost`` from the Python test suite, so the browser drives production code
without touching Docker, apt, systemd or NetworkManager.

Started by ``tests/e2e-appliance/run-appliance.sh``; never part of the package.
"""

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
    StaticCatalogue,
)

OS_RELEASE = """NAME="Raspberry Pi OS"
VERSION="12 (bookworm)"
VERSION_ID="12"
VERSION_CODENAME=bookworm
ID=debian
"""


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

    def seed_appliance_state():
        """Restore the scripted host so browser tests do not depend on order."""
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

    seed_appliance_state()

    agent = InProcessAgentClient(AgentHandlers(services, executor=lambda target: target()))
    app = ApplianceWebApp(paths=services.paths, config=services.config, agent=agent)
    app.auth = AuthStore(services.paths.auth_file, iterations=1000)
    app.test_reset_hook = seed_appliance_state

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
