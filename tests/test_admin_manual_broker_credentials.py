# SPDX-License-Identifier: AGPL-3.0-or-later
"""Manual Zendure MQTT broker credentials stay out of config.json.

A manually entered broker username/password must never be written into the
generated config. The broker profile carries only a non-secret
``credentials_ref``; the secret is persisted to the external EMS credential store
by the apply/write transaction (encrypted, resolvable by the Core resolver) and
rolled back if the config write fails.
"""

import json
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from admin.config_preview import ConfigPreviewGenerator
from admin.server import ScanRegistry, create_server
from ems.mqtt_credentials import FileMqttCredentialResolver, default_mqtt_secrets_dir
from tests.admin_auth_helpers import auth_headers, authenticate
from tests.helpers.system_alignment import SetupReadySystemAlignment
from tests.helpers.setup_config import authorize_setup_mutation
from tests.test_admin_server import (
    _FakeReleaseManager,
    _fake_gateway_prober,
    _fake_scan,
)

pytestmark = [
    pytest.mark.admin,
    pytest.mark.mqtt,
    pytest.mark.integration,
    pytest.mark.simulation,
]


@pytest.fixture(autouse=True)
def _isolate(isolated_install_root):
    return isolated_install_root


TEMPLATE = {
    "system": {"max_total_power": 1600},
    "devices": [{"name": "inverter_1", "ip": "192.0.2.1", "sn": "YOUR_SN", "max_power": 800}],
    "grid_meter": {"type": "shelly", "ip": "192.0.2.3"},
}


class _ReleaseManager:
    def config_template(self):
        return {"template": TEMPLATE, "tag": "v-test"}


def _device(index=1):
    return {
        "config_name": f"inverter_{index}", "display_name": f"SolarFlow {index}",
        "role": "inverter", "enabled": True, "ip": f"192.168.1.{index}",
        "serial_number": f"SN{index}", "device_type": "zendure_solarflow_800_pro",
        "api_family": "zendure_local_http",
    }


def _meter():
    return {
        "config_name": "grid_meter", "display_name": "Shelly", "role": "grid_meter",
        "enabled": True, "ip": "shelly.local", "api_family": "shelly_gen2",
        "device_type": "shelly_pro_3em",
    }


def _manual_mqtt():
    return {"name": "SolarFlow", "serial_number": "DEVSN1", "generation": "solarflow_zensdk"}


BROKER = {"name": "local_mqtt", "host": "192.168.1.20", "port": 1883,
          "username": "brokeruser", "password": "broker-secret-pw"}


# --- Preview: the secret never enters the generated config ----------------


def test_manual_broker_secret_absent_from_generated_config():
    result = ConfigPreviewGenerator(_ReleaseManager()).generate(
        [_device(1), _meter()], 1,
        zendure_mqtt_broker=dict(BROKER),
        zendure_mqtt_manual_devices=[_manual_mqtt()],
    )
    assert result["ready"] is True, result["validation"]
    profile = result["config"]["zendure_mqtt"]["brokers"]["local_mqtt"]
    assert profile.get("credentials_ref")
    assert "password" not in profile
    assert "username" not in profile
    blob = json.dumps(result["config"])
    assert "broker-secret-pw" not in blob
    assert "brokeruser" not in blob


# --- Server apply: secret is staged, resolvable, and out of config --------


def _request(url, method="GET", body=None):
    data = None
    headers = dict(auth_headers(url, method))
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read() or b"null")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"null")


def _workflow_request(url, method="GET", body=None):
    status, payload = _request(url, method, body)
    return status, {}, payload


def _authorized(base, body, srv=None, **kwargs):
    return authorize_setup_mutation(
        base, _workflow_request, body, srv=srv, **kwargs
    )


def _serve(tmp_path):
    srv = create_server(
        "127.0.0.1", 0,
        registry=ScanRegistry(scan_runner=_fake_scan),
        gateway_prober=_fake_gateway_prober,
        release_manager=_FakeReleaseManager(tmp_path),
        system_alignment=SetupReadySystemAlignment(),
    )
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    authenticate(base)
    return srv, base


def _body():
    return {
        # Keep this credential-transaction fixture bootable: the manually added
        # ZenSDK device is telemetry-only, while the selected API inverter owns
        # the EMS control loop.
        "devices": [_device(1)],
        "supported_grid_meter_count": 0,
        "zendure_mqtt_broker": dict(BROKER),
        "zendure_mqtt_manual_devices": [_manual_mqtt()],
    }


def test_apply_stages_manual_broker_secret_and_keeps_config_clean(tmp_path):
    srv, base = _serve(tmp_path)
    try:
        body = _authorized(base, _body(), srv)
        status, payload = _request(f"{base}/api/setup/config/apply", "POST", body)
        assert status == 200 and payload["ok"] is True, payload
        config = json.loads(Path(payload["path"]).read_text())
        blob = json.dumps(config)
        assert "broker-secret-pw" not in blob
        ref = config["zendure_mqtt"]["brokers"]["local_mqtt"]["credentials_ref"]
        secret = srv.credential_store.load_mqtt_broker_secret(ref)
        assert secret is not None
        assert secret.password == "broker-secret-pw"
        assert secret.username == "brokeruser"
        # The Core resolver reads the same record with no Admin dependency.
        resolved = FileMqttCredentialResolver(default_mqtt_secrets_dir()).resolve(ref)
        assert resolved.password == "broker-secret-pw"
    finally:
        srv.shutdown()
        srv.server_close()


# --- Rotation replaces the stored secret transactionally ------------------


def test_reapply_with_different_password_rotates_credential(tmp_path):
    srv, base = _serve(tmp_path)
    try:
        body = _authorized(base, _body(), srv)
        status, payload = _request(f"{base}/api/setup/config/apply", "POST", body)
        assert status == 200 and payload["ok"] is True, payload
        ref = json.loads(Path(payload["path"]).read_text())["zendure_mqtt"][
            "brokers"
        ]["local_mqtt"]["credentials_ref"]

        rotated = _body()
        rotated["zendure_mqtt_broker"]["password"] = "rotated-broker-pw"
        rotated = _authorized(base, rotated, srv)
        status, payload = _request(
            f"{base}/api/setup/config/apply", "POST", rotated
        )
        assert status == 200 and payload["ok"] is True, payload
        # No secret leaks into the response.
        for secret in ("broker-secret-pw", "rotated-broker-pw", "brokeruser"):
            assert secret not in json.dumps(payload)

        # The new password is active; the old one no longer is.
        secret = srv.credential_store.load_mqtt_broker_secret(ref)
        assert secret is not None
        assert secret.password == "rotated-broker-pw"
    finally:
        srv.shutdown()
        srv.server_close()


def test_config_write_failure_restores_rotated_manual_broker_secret(tmp_path):
    srv, base = _serve(tmp_path)
    try:
        body = _authorized(base, _body(), srv)
        status, payload = _request(f"{base}/api/setup/config/apply", "POST", body)
        assert status == 200 and payload["ok"] is True, payload
        ref = json.loads(Path(payload["path"]).read_text())["zendure_mqtt"][
            "brokers"
        ]["local_mqtt"]["credentials_ref"]

        rotated = _body()
        rotated["zendure_mqtt_broker"]["password"] = "rotated-broker-pw"
        rotated = _authorized(base, rotated, srv)

        def _boom(*args, **kwargs):
            raise OSError("disk full")

        srv.config_apply.apply = _boom
        status, payload = _request(
            f"{base}/api/setup/config/apply", "POST", rotated
        )
        assert status == 500
        assert payload["ok"] is False

        # The staged rotation is rolled back: the original password stays active.
        secret = srv.credential_store.load_mqtt_broker_secret(ref)
        assert secret is not None
        assert secret.password == "broker-secret-pw"
    finally:
        srv.shutdown()
        srv.server_close()


def test_config_write_failure_rolls_back_manual_broker_secret(tmp_path):
    srv, base = _serve(tmp_path)

    def _boom(*args, **kwargs):
        raise OSError("disk full")

    srv.config_apply.apply = _boom
    try:
        body = _authorized(base, _body(), srv)
        status, payload = _request(f"{base}/api/setup/config/apply", "POST", body)
        assert status == 500
        ref = srv.credential_store.normalize_ref("local_mqtt")
        assert srv.credential_store.load_mqtt_broker_secret(ref) is None
    finally:
        srv.shutdown()
        srv.server_close()
