# SPDX-License-Identifier: AGPL-3.0-or-later
"""End-to-end Admin workflow: Zendure Cloud MQTT runtime credential persistence.

The production Apply flow — enter API key, run cloud discovery, select the
cloud device, apply the configuration — must persist an independently
resolvable runtime credential record before the config becomes active:

* the applied config carries only ``credentials_ref``, never a plaintext secret;
* a reconstructed EMS runtime (no Admin process memory) resolves the reference
  and reaches the mocked broker;
* a persistence failure blocks the apply and commits nothing;
* rotation replaces the active runtime record;
* a missing record at EMS startup reports ``broker_auth_missing``.
"""

import json
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from admin.credential_store import CredentialStore
from admin.mqtt_discovery import MqttBrokerDiscovery, MqttBrokerStore
from admin.server import ScanRegistry, create_server
from admin.zendure_cloud_auth import ZendureCloudError
from admin.zendure_cloud_mqtt import FakeCloudMqttListener, ZendureCloudDiscovery
from ems.mqtt_credentials import FileMqttCredentialResolver
from ems.zendure_mqtt.runtime import build_zendure_mqtt_runtime
from ems.zendure_mqtt.control_runtime import build_zendure_mqtt_control_runtime
from tests.admin_auth_helpers import auth_headers, authenticate
from tests.helpers.fake_mqtt import FakeMqttNetwork
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

CLOUD_REF = "zendure-cloud"
API_KEY = "raw-account-api-key"
MQTT_USER = "cloud-mqtt-user"


@pytest.fixture(autouse=True)
def _isolate(isolated_install_root):
    return isolated_install_root


class _CloudFetch:
    """Mutable fake deviceList backend (supports rotation and outages)."""

    def __init__(self):
        self.password = "cloud-mqtt-secret-1"
        self.app_key = "cloud-app-key-1"
        self.client_id = "cloud-client-1"
        self.fail = False
        self.calls = 0

    def __call__(self, _token, _timeout):
        self.calls += 1
        if self.fail:
            raise ZendureCloudError("deviceList unavailable")
        return {
            "devices": [
                {
                    "productKey": "PK-AAA",
                    "deviceKey": "DK-BBB",
                    "productModel": "SolarFlow 800",
                    "snNumber": "SN-CLOUD1",
                    "deviceName": "Cloud inverter",
                }
            ],
            "mqtt": {
                "host": "mqtteu.zen-iot.com",
                "port": 8883,
                "username": MQTT_USER,
                "password": self.password,
                "client_id": self.client_id,
            },
            "app_key": self.app_key,
        }


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


def _serve(tmp_path, fetch):
    messages = [
        (
            "iot/PK-AAA/DK-BBB/properties/report",
            json.dumps({"properties": {"electricLevel": 55, "outputHomePower": 120}}),
        )
    ]
    cloud = ZendureCloudDiscovery(
        CredentialStore().zendure,
        device_list_fetcher=fetch,
        listener_factory=lambda c: FakeCloudMqttListener(c, messages),
        timeout_s=0.0,
    )
    store = MqttBrokerStore(clock=lambda: 100.0, proposal_ttl_seconds=900)
    srv = create_server(
        "127.0.0.1",
        0,
        registry=ScanRegistry(scan_runner=_fake_scan),
        gateway_prober=_fake_gateway_prober,
        mqtt_discovery=MqttBrokerDiscovery(store=store, topic_discoverer=None),
        zendure_cloud_discovery=cloud,
        release_manager=_FakeReleaseManager(tmp_path),
        system_alignment=SetupReadySystemAlignment(),
    )
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    authenticate(base)
    return srv, base


def _cloud_selection(base):
    """Run the real workflow: save API key, cloud discovery, select the device."""

    status, payload = _request(
        f"{base}/api/discovery/zendure-cloud-mqtt/token", "POST", {"api_key": API_KEY}
    )
    assert status == 200 and payload["ok"] is True, payload
    status, payload = _request(
        f"{base}/api/discovery/zendure-cloud-mqtt/refresh", "POST", {}
    )
    assert status == 200 and payload["ok"] is True, payload
    status, payload = _request(f"{base}/api/discovery/mqtt-proposals")
    assert status == 200
    cloud = [p for p in payload["proposals"] if p["broker_ref"] == "zendure_cloud"]
    assert cloud, payload
    return {"id": cloud[0]["id"], "broker_ref": cloud[0]["broker_ref"]}


def _workflow_request(url, method="GET", body=None):
    status, payload = _request(url, method, body)
    return status, {}, payload


def _apply(base, selection, srv=None):
    body = authorize_setup_mutation(base, _workflow_request, {
        "devices": [],
        "supported_grid_meter_count": 0,
        "zendure_mqtt_proposals": [selection],
    }, srv=srv)
    return _request(f"{base}/api/setup/config/apply", "POST", body)


def _paths(root):
    config_path = Path(root) / "config" / "config.json"
    secrets_dir = Path(root) / "config" / "secrets"
    return config_path, secrets_dir


def _cloud_broker_summary(config, resolver):
    control = build_zendure_mqtt_control_runtime(
        config,
        service_factory=FakeMqttNetwork().control_service_factory(),
        credential_resolver=resolver,
    )
    if control.rejected:
        codes = {
            issue["code"]
            for entry in control.rejected
            for issue in entry.issues
        }
        return {"broker_ref": "zendure_cloud", "issue": next(iter(codes), None)}
    if control.devices:
        return {"broker_ref": "zendure_cloud", "issue": None}

    network = FakeMqttNetwork()
    runtime = build_zendure_mqtt_runtime(
        config,
        service_factory=network.telemetry_service_factory(),
        credential_resolver=resolver,
    )
    try:
        for broker in runtime.status()["brokers"]:
            if broker["broker_ref"] == "zendure_cloud":
                return broker
        return None
    finally:
        runtime.stop()


def test_apply_persists_cloud_runtime_credentials_end_to_end(
    tmp_path, isolated_install_root
):
    fetch = _CloudFetch()
    srv, base = _serve(tmp_path, fetch)
    try:
        selection = _cloud_selection(base)
        status, payload = _apply(base, selection, srv)
        assert status == 200 and payload.get("ok") is True, payload

        config_path, secrets_dir = _paths(isolated_install_root)
        raw = config_path.read_text(encoding="utf-8")
        config = json.loads(raw)
        broker = config["zendure_mqtt"]["brokers"]["zendure_cloud"]
        assert broker["credentials_ref"] == CLOUD_REF
        # No plaintext secret may enter the applied config.
        for secret in (API_KEY, MQTT_USER, fetch.password, fetch.app_key, fetch.client_id):
            assert secret not in raw

        # The runtime credential record exists and resolves through the
        # Core-owned resolver with no Admin process memory.
        assert (secrets_dir / f"mqtt-{CLOUD_REF}.json").is_file()
        resolver = FileMqttCredentialResolver(secrets_dir)
        credentials = resolver.resolve(CLOUD_REF)
        assert credentials.username == MQTT_USER
        assert credentials.password == fetch.password
        assert credentials.client_id == fetch.client_id
        assert credentials.app_key == fetch.app_key

        # A reconstructed EMS runtime reaches the mocked broker instead of
        # reporting missing broker auth.
        summary = _cloud_broker_summary(config, resolver)
        assert summary is not None
        assert summary.get("issue") != "broker_auth_missing"
    finally:
        srv.shutdown()
        srv.server_close()


def test_apply_blocks_when_cloud_secret_persistence_fails(
    tmp_path, isolated_install_root
):
    fetch = _CloudFetch()
    srv, base = _serve(tmp_path, fetch)
    try:
        selection = _cloud_selection(base)
        fetch.fail = True
        status, payload = _apply(base, selection, srv)
        assert status >= 400, payload
        assert payload.get("ok") is False
        assert payload.get("message"), payload

        config_path, secrets_dir = _paths(isolated_install_root)
        # Nothing is committed: no config, no partially writable device, no
        # orphan runtime secret.
        assert not config_path.exists()
        assert not (secrets_dir / f"mqtt-{CLOUD_REF}.json").exists()
    finally:
        srv.shutdown()
        srv.server_close()


def test_apply_fails_closed_when_cloud_credential_encryption_fails(
    tmp_path, isolated_install_root, monkeypatch
):
    # A broken cipher must block the apply (never store a base64 downgrade) and
    # must not leak any of the four deviceList runtime fields into the response.
    from tests.test_admin_server import _poison_fernet_encrypt

    fetch = _CloudFetch()
    srv, base = _serve(tmp_path, fetch)
    try:
        selection = _cloud_selection(base)  # saves the API key with a real cipher
        _poison_fernet_encrypt(monkeypatch)
        status, payload = _apply(base, selection, srv)
        assert status >= 400, payload
        assert payload.get("ok") is False

        body = json.dumps(payload)
        for secret in (MQTT_USER, fetch.password, fetch.app_key, fetch.client_id, API_KEY):
            assert secret not in body

        config_path, secrets_dir = _paths(isolated_install_root)
        assert not config_path.exists()
        assert not (secrets_dir / f"mqtt-{CLOUD_REF}.json").exists()
        if secrets_dir.exists():
            assert list(secrets_dir.glob("*.tmp")) == []
    finally:
        srv.shutdown()
        srv.server_close()


def test_setup_apply_reuses_valid_cloud_credential_without_refetch(
    tmp_path, isolated_install_root
):
    # Fresh Setup must reuse an existing complete runtime record exactly like
    # Maintenance: no deviceList call, no rotation — so a temporary Zendure
    # API outage cannot fail an apply that needs nothing from the API.
    fetch = _CloudFetch()
    srv, base = _serve(tmp_path, fetch)
    try:
        selection = _cloud_selection(base)
        srv.credential_store.save_mqtt_cloud_runtime_secret(
            CLOUD_REF,
            username=MQTT_USER,
            password="already-active-secret",
            client_id="active-client",
            app_key="active-app-key",
        )
        calls_before_apply = fetch.calls
        fetch.fail = True
        status, payload = _apply(base, selection, srv)
        assert status == 200 and payload.get("ok") is True, payload
        assert fetch.calls == calls_before_apply
        _, secrets_dir = _paths(isolated_install_root)
        resolved = FileMqttCredentialResolver(secrets_dir).resolve(CLOUD_REF)
        assert resolved.password == "already-active-secret"
        assert resolved.app_key == "active-app-key"
    finally:
        srv.shutdown()
        srv.server_close()


def test_setup_apply_reprovisions_incomplete_cloud_record(
    tmp_path, isolated_install_root
):
    # An unusable record (missing app_key) with the account key available is
    # reprovisioned transactionally through the four-field contract.
    fetch = _CloudFetch()
    srv, base = _serve(tmp_path, fetch)
    try:
        selection = _cloud_selection(base)
        srv.credential_store.save_mqtt_cloud_runtime_secret(
            CLOUD_REF,
            username=MQTT_USER,
            password="stale-secret",
            client_id="stale-client",
        )
        status, payload = _apply(base, selection, srv)
        assert status == 200 and payload.get("ok") is True, payload
        _, secrets_dir = _paths(isolated_install_root)
        resolved = FileMqttCredentialResolver(secrets_dir).resolve(CLOUD_REF)
        assert resolved.password == fetch.password
        assert resolved.client_id == fetch.client_id
        assert resolved.app_key == fetch.app_key
    finally:
        srv.shutdown()
        srv.server_close()


def test_setup_apply_blocks_invalid_cloud_record_without_api_key(
    tmp_path, isolated_install_root
):
    # Without the account key an unusable record cannot be repaired: the apply
    # is blocked with the same stable code Maintenance uses, and no config is
    # written.
    fetch = _CloudFetch()
    srv, base = _serve(tmp_path, fetch)
    try:
        selection = _cloud_selection(base)
        srv.credential_store.save_mqtt_cloud_runtime_secret(
            CLOUD_REF,
            username=MQTT_USER,
            password="stale-secret",
            client_id="stale-client",
        )
        srv.credential_store.zendure.delete_token()
        status, payload = _apply(base, selection, srv)
        assert status == 400, payload
        assert payload.get("ok") is False
        assert payload.get("code") == "credential_provisioning_failed"
        assert "stale-secret" not in json.dumps(payload)
        config_path, _ = _paths(isolated_install_root)
        assert not config_path.exists()
    finally:
        srv.shutdown()
        srv.server_close()


def test_cloud_credential_rotation_replaces_runtime_record(
    tmp_path, isolated_install_root
):
    fetch = _CloudFetch()
    srv, base = _serve(tmp_path, fetch)
    try:
        selection = _cloud_selection(base)
        status, payload = _apply(base, selection, srv)
        assert status == 200 and payload.get("ok") is True, payload
        old_password = fetch.password

        # The stored record decayed (missing app_key): a re-apply must
        # reprovision it with freshly fetched material — a complete record
        # would have been reused untouched instead.
        config_path, secrets_dir = _paths(isolated_install_root)
        record_path = secrets_dir / f"mqtt-{CLOUD_REF}.json"
        record = json.loads(record_path.read_text(encoding="utf-8"))
        record.pop("app_key", None)
        record.pop("app_key_encrypted", None)
        record_path.write_text(json.dumps(record), encoding="utf-8")

        fetch.password = "cloud-mqtt-secret-2"
        fetch.app_key = "cloud-app-key-2"
        status, payload = _apply(base, selection, srv)
        assert status == 200 and payload.get("ok") is True, payload

        credentials = FileMqttCredentialResolver(secrets_dir).resolve(CLOUD_REF)
        # The rotated material is active; the old secret is not.
        assert credentials.password == "cloud-mqtt-secret-2"
        assert credentials.app_key == "cloud-app-key-2"
        raw = config_path.read_text(encoding="utf-8")
        for secret in (old_password, fetch.password, fetch.app_key):
            assert secret not in raw
    finally:
        srv.shutdown()
        srv.server_close()


def test_verification_failure_rolls_back_new_cloud_record(
    tmp_path, isolated_install_root, monkeypatch
):
    # The provisioning helper owns its own cleanup: when the freshly stored
    # record does not resolve, it is removed before the error propagates, so
    # the caller never has to know what to clean up after a failed return.
    import ems.mqtt_credentials as mqtt_credentials
    from admin.credential_store import CredentialStore, CredentialStoreError

    fetch = _CloudFetch()
    store = CredentialStore()
    store.zendure.save_token(API_KEY)
    cloud = ZendureCloudDiscovery(
        store.zendure, device_list_fetcher=fetch, listener_factory=lambda c: None
    )

    def _broken_resolve(self, ref):
        raise mqtt_credentials.MqttCredentialError("verification failed")

    monkeypatch.setattr(
        mqtt_credentials.FileMqttCredentialResolver, "resolve", _broken_resolve
    )
    with pytest.raises(CredentialStoreError):
        cloud.provision_runtime_credentials(store)
    assert store.load_mqtt_broker_secret(CLOUD_REF) is None


def test_verification_failure_restores_rotated_cloud_record(
    tmp_path, isolated_install_root, monkeypatch
):
    import ems.mqtt_credentials as mqtt_credentials
    from admin.credential_store import CredentialStore, CredentialStoreError

    fetch = _CloudFetch()
    store = CredentialStore()
    store.zendure.save_token(API_KEY)
    store.save_mqtt_cloud_runtime_secret(
        CLOUD_REF,
        username=MQTT_USER,
        password="previous-active-secret",
        client_id="old-client",
        app_key="old-app-key",
    )
    cloud = ZendureCloudDiscovery(
        store.zendure, device_list_fetcher=fetch, listener_factory=lambda c: None
    )

    def _broken_resolve(self, ref):
        raise mqtt_credentials.MqttCredentialError("verification failed")

    monkeypatch.setattr(
        mqtt_credentials.FileMqttCredentialResolver, "resolve", _broken_resolve
    )
    with pytest.raises(CredentialStoreError):
        cloud.provision_runtime_credentials(store)
    restored = store.load_mqtt_broker_secret(CLOUD_REF)
    assert restored is not None
    assert restored.password == "previous-active-secret"


def _fetch_omitting(field):
    """A deviceList backend whose response lacks one required credential field."""

    inner = _CloudFetch()

    def _fetch(token, timeout):
        result = inner(token, timeout)
        if field == "app_key":
            result["app_key"] = None
        else:
            result["mqtt"][field] = None
        return result

    return _fetch


@pytest.mark.parametrize("field", ("username", "password", "client_id", "app_key"))
def test_provisioning_rejects_incomplete_device_list_response(
    tmp_path, isolated_install_root, field
):
    # The runtime cloud contract needs all four fields (username, password,
    # client_id, app_key); a deviceList response missing any of them must
    # block provisioning and leave no staged record behind.
    from admin.credential_store import CredentialStore, CredentialStoreError

    store = CredentialStore()
    store.zendure.save_token(API_KEY)
    cloud = ZendureCloudDiscovery(
        store.zendure,
        device_list_fetcher=_fetch_omitting(field),
        listener_factory=lambda c: None,
    )
    with pytest.raises(CredentialStoreError) as excinfo:
        cloud.provision_runtime_credentials(store)
    message = str(excinfo.value)
    assert CLOUD_REF in message
    assert "cloud-mqtt-secret-1" not in message
    assert API_KEY not in message
    assert store.load_mqtt_broker_secret(CLOUD_REF) is None


def test_provisioning_keeps_previous_record_when_device_list_incomplete(
    tmp_path, isolated_install_root
):
    # An incomplete deviceList response must not replace a working record:
    # the previously active credential stays resolvable after the failure.
    from admin.credential_store import CredentialStore, CredentialStoreError

    store = CredentialStore()
    store.zendure.save_token(API_KEY)
    store.save_mqtt_cloud_runtime_secret(
        CLOUD_REF,
        username=MQTT_USER,
        password="previous-active-secret",
        client_id="old-client",
        app_key="old-app-key",
    )
    cloud = ZendureCloudDiscovery(
        store.zendure,
        device_list_fetcher=_fetch_omitting("app_key"),
        listener_factory=lambda c: None,
    )
    with pytest.raises(CredentialStoreError):
        cloud.provision_runtime_credentials(store)
    restored = FileMqttCredentialResolver(store.secrets_dir).resolve(CLOUD_REF)
    assert restored.password == "previous-active-secret"
    assert restored.app_key == "old-app-key"


def test_provision_delete_failure_after_verification_reports_failed_refs(
    tmp_path, isolated_install_root, monkeypatch
):
    # When the post-save verification fails AND deleting the fresh record also
    # fails, the raised error itself must carry the affected refs: the caller
    # never receives a staging result to roll back, so the provisioning helper
    # owns the reporting.
    import ems.mqtt_credentials as mqtt_credentials
    from admin import credential_store as credential_store_module
    from admin.credential_store import CredentialStore, CredentialStoreError

    fetch = _CloudFetch()
    store = CredentialStore()
    store.zendure.save_token(API_KEY)
    cloud = ZendureCloudDiscovery(
        store.zendure, device_list_fetcher=fetch, listener_factory=lambda c: None
    )

    def _broken_resolve(self, ref):
        raise mqtt_credentials.MqttCredentialError("verification failed")

    def _broken_delete(self, path):
        raise credential_store_module.CredentialStoreError(
            "Could not remove the secret file."
        )

    monkeypatch.setattr(
        mqtt_credentials.FileMqttCredentialResolver, "resolve", _broken_resolve
    )
    monkeypatch.setattr(
        credential_store_module._EncryptedFiles, "delete_file", _broken_delete
    )
    with pytest.raises(CredentialStoreError) as excinfo:
        cloud.provision_runtime_credentials(store)
    exc = excinfo.value
    assert getattr(exc, "credentials_ref", None) == CLOUD_REF
    assert tuple(getattr(exc, "rollback_failed_refs", ())) == (CLOUD_REF,)
    for secret in (API_KEY, fetch.password, fetch.app_key, fetch.client_id):
        assert secret not in str(exc)


def test_provision_restore_failure_after_verification_reports_failed_refs(
    tmp_path, isolated_install_root, monkeypatch
):
    import ems.mqtt_credentials as mqtt_credentials
    from admin import credential_store as credential_store_module
    from admin.credential_store import CredentialStore, CredentialStoreError

    fetch = _CloudFetch()
    store = CredentialStore()
    store.zendure.save_token(API_KEY)
    store.save_mqtt_cloud_runtime_secret(
        CLOUD_REF, username=MQTT_USER, password="previous-active-secret"
    )
    cloud = ZendureCloudDiscovery(
        store.zendure, device_list_fetcher=fetch, listener_factory=lambda c: None
    )

    def _broken_resolve(self, ref):
        raise mqtt_credentials.MqttCredentialError("verification failed")

    files = credential_store_module._EncryptedFiles
    original_write = files.write_record
    calls = {"count": 0}

    def _write_then_fail(self, path, record):
        # The rotation itself succeeds; the byte-restore during rollback fails.
        calls["count"] += 1
        if calls["count"] >= 2:
            raise credential_store_module.CredentialStoreError(
                "Could not write the secret file."
            )
        return original_write(self, path, record)

    def _fail_raw_restore(self, path, data):
        raise credential_store_module.CredentialStoreError(
            "Could not write the secret file."
        )

    monkeypatch.setattr(
        mqtt_credentials.FileMqttCredentialResolver, "resolve", _broken_resolve
    )
    monkeypatch.setattr(files, "write_record", _write_then_fail)
    if hasattr(files, "write_raw"):
        monkeypatch.setattr(files, "write_raw", _fail_raw_restore)
    with pytest.raises(CredentialStoreError) as excinfo:
        cloud.provision_runtime_credentials(store)
    exc = excinfo.value
    assert tuple(getattr(exc, "rollback_failed_refs", ())) == (CLOUD_REF,)
    assert "previous-active-secret" not in str(exc)


def test_provision_successful_internal_rollback_carries_no_failed_refs(
    tmp_path, isolated_install_root, monkeypatch
):
    # A verification failure whose rollback succeeds is a normal provisioning
    # error: no high-severity rollback flag, and the new record is gone.
    import ems.mqtt_credentials as mqtt_credentials
    from admin.credential_store import CredentialStore, CredentialStoreError

    fetch = _CloudFetch()
    store = CredentialStore()
    store.zendure.save_token(API_KEY)
    cloud = ZendureCloudDiscovery(
        store.zendure, device_list_fetcher=fetch, listener_factory=lambda c: None
    )

    def _broken_resolve(self, ref):
        raise mqtt_credentials.MqttCredentialError("verification failed")

    monkeypatch.setattr(
        mqtt_credentials.FileMqttCredentialResolver, "resolve", _broken_resolve
    )
    with pytest.raises(CredentialStoreError) as excinfo:
        cloud.provision_runtime_credentials(store)
    assert tuple(getattr(excinfo.value, "rollback_failed_refs", ())) == ()
    assert store.load_mqtt_broker_secret(CLOUD_REF) is None


def test_config_write_failure_removes_new_cloud_record(
    tmp_path, isolated_install_root
):
    fetch = _CloudFetch()
    srv, base = _serve(tmp_path, fetch)
    try:
        selection = _cloud_selection(base)

        def _boom(*args, **kwargs):
            raise OSError("disk full")

        srv.config_apply.apply = _boom
        status, payload = _apply(base, selection, srv)
        assert status == 500, payload
        assert payload.get("ok") is False

        config_path, secrets_dir = _paths(isolated_install_root)
        assert not config_path.exists()
        # The record created for this apply is rolled back, never orphaned.
        assert not (secrets_dir / f"mqtt-{CLOUD_REF}.json").exists()
    finally:
        srv.shutdown()
        srv.server_close()


def test_config_write_failure_restores_rotated_cloud_record(
    tmp_path, isolated_install_root
):
    fetch = _CloudFetch()
    srv, base = _serve(tmp_path, fetch)
    try:
        selection = _cloud_selection(base)
        status, payload = _apply(base, selection, srv)
        assert status == 200 and payload.get("ok") is True, payload

        fetch.password = "cloud-mqtt-secret-2"
        fetch.app_key = "cloud-app-key-2"

        def _boom(*args, **kwargs):
            raise OSError("disk full")

        srv.config_apply.apply = _boom
        status, payload = _apply(base, selection, srv)
        assert status == 500, payload
        assert payload.get("ok") is False

        # The rotation is rolled back: the previously active secret is
        # restored, so the live config keeps resolving its credential.
        _, secrets_dir = _paths(isolated_install_root)
        credentials = FileMqttCredentialResolver(secrets_dir).resolve(CLOUD_REF)
        assert credentials.password == "cloud-mqtt-secret-1"
        assert credentials.app_key == "cloud-app-key-1"
    finally:
        srv.shutdown()
        srv.server_close()


def test_rollback_failure_reports_high_severity_error(
    tmp_path, isolated_install_root, monkeypatch
):
    from admin import credential_store as credential_store_module

    fetch = _CloudFetch()
    srv, base = _serve(tmp_path, fetch)
    try:
        selection = _cloud_selection(base)

        def _boom(*args, **kwargs):
            raise OSError("disk full")

        srv.config_apply.apply = _boom

        def _broken_delete(self, path):
            raise credential_store_module.CredentialStoreError(
                "Could not remove the secret file."
            )

        monkeypatch.setattr(
            credential_store_module._EncryptedFiles, "delete_file", _broken_delete
        )
        status, payload = _apply(base, selection, srv)
        assert status == 500, payload
        # The apply must not claim success, and the failed rollback is an
        # explicit, actionable, high-severity part of the response.
        assert payload.get("ok") is False
        rollback = payload.get("credential_rollback")
        assert rollback is not None, payload
        assert rollback["severity"] == "high"
        assert rollback["failed_refs"] == [CLOUD_REF]
        assert rollback["message"]
        # Safe diagnostics only: no secret value may appear in the response.
        blob = json.dumps(payload)
        for secret in (API_KEY, fetch.password, fetch.app_key, fetch.client_id):
            assert secret not in blob
    finally:
        srv.shutdown()
        srv.server_close()


def test_missing_runtime_credential_reports_broker_auth_missing(
    tmp_path, isolated_install_root
):
    fetch = _CloudFetch()
    srv, base = _serve(tmp_path, fetch)
    try:
        selection = _cloud_selection(base)
        status, payload = _apply(base, selection, srv)
        assert status == 200 and payload.get("ok") is True, payload
    finally:
        srv.shutdown()
        srv.server_close()

    config_path, secrets_dir = _paths(isolated_install_root)
    record = secrets_dir / f"mqtt-{CLOUD_REF}.json"
    assert record.is_file()
    record.unlink()

    config = json.loads(config_path.read_text(encoding="utf-8"))
    summary = _cloud_broker_summary(config, FileMqttCredentialResolver(secrets_dir))
    assert summary is not None
    assert summary.get("issue") == "broker_auth_missing"
