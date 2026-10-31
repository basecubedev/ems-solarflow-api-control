# SPDX-License-Identifier: AGPL-3.0-or-later
"""The world the MQTT end-to-end browser test runs in, as Playwright's web server.

It builds an isolated installation laid out like the EMS container (/app with
config/ and data/), starts a real Mosquitto on the pinned image, plays a
Shelly Gen2 plug and a grid meter on it, and serves the test-mode Admin for
that installation. The EMS itself is started by the test once the inverter is
added, from the same layout. Every write gate is off and the EMS runs dry, so
nothing here could command hardware. The EMS needs one output-control inverter
to run; the one it gets is a local-API device on 192.0.2.10 (TEST-NET-1, never
a real host), so it only ever fails to read it.

The paths the test needs are written to the file EMS_MQTT_E2E_STATE names.
Stopping the harness stops the Admin, the plug and removes the broker.
"""

import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MOSQUITTO_IMAGE = "eclipse-mosquitto:2.1.2-alpine"
SHELLY_ID = "shellyplusplugs-e2etest"
GRID_TOPIC = "e2e/grid/power"
BROKER_REF = "e2e_broker"
PUBLISH_EVERY_SECONDS = 2.0


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def wait_for_port(port, deadline):
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return True
        except OSError:
            threading.Event().wait(0.1)
    return False


def build_install(tmp, broker_port, dashboard_port):
    app = tmp / "app"
    for name in ("config", "data"):
        (app / name).mkdir(parents=True)
    for name in ("ems", "dashboard", "ems-solarflow-api-control.py"):
        (app / name).symlink_to(ROOT / name)
    shutil.copy(ROOT / "config" / "config.template.json", app / "config" / "config.template.json")
    config = json.loads((ROOT / "config" / "config.template.json").read_text(encoding="utf-8"))
    config["system"].update(
        dry_run=True,
        simulation_mode=False,
        allow_hardware_writes=False,
        allow_mqtt_local_control_writes=False,
        allow_mqtt_zendure_control_writes=False,
        allow_state_reconciliation_writes=False,
        loop_interval=2,
        runtime_state_path=str(app / "data" / "runtime-state.json"),
    )
    config["grid_meter"] = {
        "type": "mqtt",
        "mqtt": {"broker_ref": BROKER_REF, "topic": GRID_TOPIC, "payload_format": "number"},
    }
    inverter = {key: value for key, value in config["devices"][0].items() if not key.startswith("_comment")}
    inverter.update(name="E2E_API", ip="192.0.2.10", sn="E2EAPI00001")
    config["devices"] = [inverter]
    config["zendure_mqtt"] = {
        "brokers": {BROKER_REF: {"enabled": True, "source": "local_mqtt", "host": "127.0.0.1", "port": broker_port}}
    }
    config["dashboard"].update(
        enabled=True,
        host="127.0.0.1",
        port=dashboard_port,
        ssl_enabled=False,
        ssl_auto_generate=False,
        auth_file=str(app / "data" / "dashboard-auth.json"),
        database_path=str(app / "data" / "ems_dashboard.sqlite"),
    )
    config["influxdb"]["enabled"] = False
    (app / "config" / "config.json").write_text(json.dumps(config, indent=2), encoding="utf-8")
    return app


def start_broker(tmp):
    conf = tmp / "mosquitto.conf"
    conf.write_text("listener 1883\nallow_anonymous true\npersistence false\n", encoding="utf-8")
    port = free_port()
    name = f"ems-mqtt-e2e-{uuid.uuid4().hex[:8]}"
    subprocess.run(
        ["docker", "run", "-d", "--rm", "--name", name, "-p", f"127.0.0.1:{port}:1883",
         "-v", f"{conf}:/mosquitto/config/mosquitto.conf:ro", MOSQUITTO_IMAGE],
        check=True, capture_output=True, text=True,
    )
    if not wait_for_port(port, time.monotonic() + 30):
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)
        raise RuntimeError("the e2e broker never answered")
    return name, port


def play_devices(port, stop):
    """A Shelly Gen2 plug on a balcony inverter and a grid meter, publishing as the devices do."""

    import paho.mqtt.client as mqtt

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    client.connect("127.0.0.1", port)
    client.loop_start()
    client.publish(f"{SHELLY_ID}/online", "true", qos=1, retain=True)
    while not stop.is_set():
        client.publish(f"{SHELLY_ID}/status/switch:0", json.dumps({"id": 0, "apower": -612.3, "voltage": 230.1}), qos=1)
        client.publish(GRID_TOPIC, "150", qos=1)
        stop.wait(PUBLISH_EVERY_SECONDS)
    client.loop_stop()
    client.disconnect()


def main():
    tmp = Path(tempfile.mkdtemp(prefix="ems-mqtt-e2e-", dir=os.environ.get("TMPDIR")))
    broker_name, broker_port = start_broker(tmp)
    dashboard_port = free_port()
    app = build_install(tmp, broker_port, dashboard_port)
    stop = threading.Event()
    player = threading.Thread(target=play_devices, args=(broker_port, stop), daemon=True)
    player.start()
    state_path = Path(os.environ["EMS_MQTT_E2E_STATE"])
    state_path.write_text(
        json.dumps({
            "app": str(app), "python": sys.executable, "broker_port": broker_port, "broker_container": broker_name,
            "dashboard_port": dashboard_port, "broker_ref": BROKER_REF, "shelly_id": SHELLY_ID,
        }),
        encoding="utf-8",
    )
    env = dict(
        os.environ,
        EMS_ADMIN_TEST_MODE="1",
        EMS_ADMIN_DATA_DIR=str(tmp / "admin-data"),
        EMS_INSTALL_DIR=str(app),
        PUID=os.environ.get("PUID", str(os.getuid())),
        PGID=os.environ.get("PGID", str(os.getgid())),
    )
    admin = subprocess.Popen(
        [sys.executable, "-m", "admin", "--host", "127.0.0.1", "--port", os.environ.get("EMS_MQTT_E2E_PORT", "8129")],
        cwd=str(ROOT), env=env,
    )

    def shut_down(*_args):
        stop.set()
        admin.terminate()

    signal.signal(signal.SIGTERM, shut_down)
    signal.signal(signal.SIGINT, shut_down)
    try:
        admin.wait()
    finally:
        stop.set()
        player.join(timeout=10)
        subprocess.run(["docker", "rm", "-f", broker_name], capture_output=True)
        state_path.unlink(missing_ok=True)


if __name__ == "__main__":
    sys.exit(main())
