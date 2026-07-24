# SPDX-License-Identifier: AGPL-3.0-or-later
"""Transactional MQTT credential promotion on config write/apply.

Proves the setup config/write and config/apply endpoints stage the runtime
credential before touching config, and that neither a promotion failure nor a
config-write failure can leave config referencing a missing secret or leave an
orphan newly promoted secret. Also asserts preview/download stay side-effect-free.
"""

import json
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from admin.mqtt_discovery import MqttBrokerDiscovery, MqttBrokerStore
from admin.server import ScanRegistry, create_server
from admin import zendure_mqtt_config_proposals
from tests.admin_auth_helpers import auth_headers, authenticate
from tests.helpers.system_alignment import SetupReadySystemAlignment
from tests.test_admin_server import (
    _FakeReleaseManager,
    _fake_gateway_prober,
    _fake_scan,
)

pytestmark = pytest.mark.simulation


@pytest.fixture(autouse=True)
def _isolate(isolated_install_root):
    return isolated_install_root


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


def _device_observation(credentials_ref="home"):
    return {
        "broker_host": "broker.local",
        "broker_port": 1883,
        "source_type": "local_mqtt",
        "topic_family": "legacy_zendure_json",
        "device_id": "SN-PROMO",
        "serial_number": "SN-PROMO",
        "product_key": "PK-PROMO",
        "model_hint": "SolarFlow 800 Pro 2",
        "credentials_ref": credentials_ref,
        "metrics_seen": ["electricLevel", "outputHomePower"],
        "topics_seen": ["iot/PK-PROMO/SN-PROMO/properties/report"],
    }


def _discovery_with_proposal(credentials_ref="home"):
    store = MqttBrokerStore(clock=lambda: 100.0, proposal_ttl_seconds=900)
    broker = {
        "id": "mqtt:broker.local:1883",
        "host": "broker.local",
        "port": 1883,
        "devices": [_device_observation(credentials_ref)],
    }
    generation = store.begin_refresh()
    store.complete_refresh(generation, [broker], success=True)
    return MqttBrokerDiscovery(store=store, topic_discoverer=None)


def _serve(discovery, tmp_path, **kwargs):
    srv = create_server(
        "127.0.0.1",
        0,
        registry=ScanRegistry(scan_runner=_fake_scan),
        gateway_prober=_fake_gateway_prober,
        mqtt_discovery=discovery,
        release_manager=_FakeReleaseManager(tmp_path),
        system_alignment=SetupReadySystemAlignment(),
        **kwargs,
    )
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    authenticate(base)
    return srv, base


def _selection(discovery):
    proposals = zendure_mqtt_config_proposals.proposals_from_brokers(
        discovery.candidates()
    )
    assert proposals, "expected a trusted proposal with a credentials_ref"
    proposal = proposals[0]
    return {"id": proposal["id"], "broker_ref": proposal["broker_ref"]}


def _write_body(discovery):
    return {
        "devices": [],
        "supported_grid_meter_count": 0,
        "zendure_mqtt_proposals": [_selection(discovery)],
    }


def _runtime_secret_saved(srv, ref="home"):
    return srv.credential_store.load_mqtt_broker_secret(ref) is not None


def test_preview_and_download_are_side_effect_free(tmp_path):
    discovery = _discovery_with_proposal()
    srv, base = _serve(discovery, tmp_path)
    srv.credential_store.save_mqtt_discovery_secret("home", "user", "password")
    body = _write_body(discovery)
    try:
        status, _ = _request(f"{base}/api/setup/config-preview", "POST", body)
        assert status == 200
        status, _ = _request(f"{base}/api/setup/config/download", "POST", body)
        assert status == 200
        # No runtime credential is promoted by a read-only preview/download.
        assert not _runtime_secret_saved(srv)
    finally:
        srv.shutdown()
        srv.server_close()


def test_setup_preview_surfaces_invalid_credentials_ref(tmp_path):
    # Setup Preview enforces the same canonical credentials_ref contract as
    # Apply, the Core validator and the runtime resolver: a non-canonical
    # reference makes the preview not ready with the stable code.
    discovery = _discovery_with_proposal(credentials_ref="Bad Ref")
    srv, base = _serve(discovery, tmp_path)
    body = _write_body(discovery)
    try:
        status, preview = _request(f"{base}/api/setup/config-preview", "POST", body)
        assert status == 200
        assert preview["ready"] is False
        codes = {e["code"] for e in preview["validation"]["errors"]}
        assert "mqtt_credentials_ref_invalid" in codes
    finally:
        srv.shutdown()
        srv.server_close()


# --- Fresh Setup revision / expected-absence protection ---------------------
# A fresh Setup apply prepares an immutable payload, but must not silently
# overwrite a config.json changed or created externally while credentials were
# being staged. It captures the config's revision (or its expected absence) at
# preparation time and re-checks it under the apply lock before writing.


def _install_config_path():
    from admin.install_context import detect_install_context

    return Path(detect_install_context().config_path)


def _patch_external_edit_during_staging(monkeypatch, config_path, external_bytes):
    """Simulate an external process editing config.json during credential staging."""

    import admin.server as server_mod

    original = server_mod.stage_setup_runtime_credentials

    def _stage_then_external_edit(config, broker, changes, **kwargs):
        original(config, broker, changes, **kwargs)
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_bytes(external_bytes)

    monkeypatch.setattr(
        server_mod, "stage_setup_runtime_credentials", _stage_then_external_edit
    )


def test_setup_apply_conflicts_when_config_changed_during_staging(tmp_path, monkeypatch):
    discovery = _discovery_with_proposal()
    srv, base = _serve(discovery, tmp_path)
    srv.credential_store.save_mqtt_discovery_secret("home", "user", "password")
    config_path = _install_config_path()
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text('{"system": {"max_total_power": 800}}\n', encoding="utf-8")
    external = b'{"externally": "edited"}\n'
    _patch_external_edit_during_staging(monkeypatch, config_path, external)
    try:
        status, payload = _request(
            f"{base}/api/setup/config/apply", "POST", _write_body(discovery)
        )
        assert status == 409, payload
        # The external edit is preserved; the prepared payload was not written.
        assert config_path.read_bytes() == external
        # The staged runtime credential is rolled back.
        assert not _runtime_secret_saved(srv)
    finally:
        srv.shutdown()
        srv.server_close()


def test_setup_apply_conflicts_when_config_created_during_staging(tmp_path, monkeypatch):
    discovery = _discovery_with_proposal()
    srv, base = _serve(discovery, tmp_path)
    srv.credential_store.save_mqtt_discovery_secret("home", "user", "password")
    config_path = _install_config_path()
    assert not config_path.exists()
    external = b'{"externally": "created"}\n'
    _patch_external_edit_during_staging(monkeypatch, config_path, external)
    try:
        status, payload = _request(
            f"{base}/api/setup/config/apply", "POST", _write_body(discovery)
        )
        assert status == 409, payload
        # The config that appeared externally is preserved, not overwritten.
        assert config_path.read_bytes() == external
        assert not _runtime_secret_saved(srv)
    finally:
        srv.shutdown()
        srv.server_close()


def test_setup_apply_writes_when_config_absent_and_unchanged(tmp_path):
    discovery = _discovery_with_proposal()
    srv, base = _serve(discovery, tmp_path)
    srv.credential_store.save_mqtt_discovery_secret("home", "user", "password")
    config_path = _install_config_path()
    assert not config_path.exists()
    try:
        status, payload = _request(
            f"{base}/api/setup/config/apply", "POST", _write_body(discovery)
        )
        assert status == 200 and payload["ok"] is True, payload
        assert config_path.exists()
        assert _runtime_secret_saved(srv)
    finally:
        srv.shutdown()
        srv.server_close()


def test_successful_write_promotes_credential_and_writes_config(tmp_path):
    discovery = _discovery_with_proposal()
    srv, base = _serve(discovery, tmp_path)
    srv.credential_store.save_mqtt_discovery_secret("home", "user", "password")
    try:
        status, payload = _request(
            f"{base}/api/setup/config/write", "POST", _write_body(discovery)
        )
        assert status == 200 and payload["ok"] is True
        assert _runtime_secret_saved(srv)
        config = json.loads((tmp_path / "generated" / "config.json").read_text())
        blob = json.dumps(config)
        assert "credentials_ref" in blob
        # No secret material ever lands in config.
        assert "password" not in blob
        assert "user" not in blob or '"username"' not in blob
    finally:
        srv.shutdown()
        srv.server_close()


def test_missing_promotion_source_leaves_config_and_secrets_untouched(tmp_path):
    discovery = _discovery_with_proposal()
    srv, base = _serve(discovery, tmp_path)
    # Deliberately do NOT save the discovery secret; promotion must fail.
    try:
        status, payload = _request(
            f"{base}/api/setup/config/write", "POST", _write_body(discovery)
        )
        assert status == 400
        assert payload["reason"] == "credential_promotion_failed"
        assert not (tmp_path / "generated" / "config.json").exists()
        assert not _runtime_secret_saved(srv)
    finally:
        srv.shutdown()
        srv.server_close()


def test_config_write_failure_after_staging_leaves_no_orphan_secret(tmp_path):
    discovery = _discovery_with_proposal()
    srv, base = _serve(discovery, tmp_path)
    srv.credential_store.save_mqtt_discovery_secret("home", "user", "password")

    def _boom(*args, **kwargs):
        raise OSError("disk full")

    srv.config_export.write = _boom
    try:
        status, payload = _request(
            f"{base}/api/setup/config/write", "POST", _write_body(discovery)
        )
        assert status == 500
        assert payload["reason"] == "write_failed"
        assert not (tmp_path / "generated" / "config.json").exists()
        # The staged runtime record must be rolled back on write failure.
        assert not _runtime_secret_saved(srv)
    finally:
        srv.shutdown()
        srv.server_close()


def test_target_exists_without_overwrite_rolls_back_new_secret(tmp_path):
    discovery = _discovery_with_proposal()
    srv, base = _serve(discovery, tmp_path)
    srv.credential_store.save_mqtt_discovery_secret("home", "user", "password")
    try:
        status, payload = _request(
            f"{base}/api/setup/config/write", "POST", _write_body(discovery)
        )
        assert status == 200 and _runtime_secret_saved(srv)

        # Remove the just-promoted runtime record, then repeat without overwrite:
        # the write is refused (target_exists) so the freshly staged record must
        # be rolled back rather than left orphaned.
        srv.credential_store.forget_mqtt_broker_secret("home")
        status, payload = _request(
            f"{base}/api/setup/config/write", "POST", _write_body(discovery)
        )
        assert status == 409 and payload["reason"] == "target_exists"
        assert not _runtime_secret_saved(srv)
    finally:
        srv.shutdown()
        srv.server_close()


def test_successful_apply_promotes_credential_and_writes_install_config(tmp_path):
    discovery = _discovery_with_proposal()
    srv, base = _serve(discovery, tmp_path)
    srv.credential_store.save_mqtt_discovery_secret("home", "user", "password")
    try:
        status, payload = _request(
            f"{base}/api/setup/config/apply", "POST", _write_body(discovery)
        )
        assert status == 200 and payload["ok"] is True
        assert _runtime_secret_saved(srv)
        config = json.loads(Path(payload["path"]).read_text())
        blob = json.dumps(config)
        assert "credentials_ref" in blob and "password" not in blob
    finally:
        srv.shutdown()
        srv.server_close()


def test_repeated_write_promotion_is_idempotent(tmp_path):
    # A repeated write regenerates config from the release template, so promoting
    # the same credential twice must stay idempotent: the runtime record already
    # exists (reuse), nothing new is created, and the secret is never lost.
    discovery = _discovery_with_proposal()
    srv, base = _serve(discovery, tmp_path)
    srv.credential_store.save_mqtt_discovery_secret("home", "user", "password")
    body = {**_write_body(discovery), "overwrite": True}
    try:
        for _ in range(2):
            status, payload = _request(
                f"{base}/api/setup/config/write", "POST", body
            )
            assert status == 200 and payload["ok"] is True
            assert _runtime_secret_saved(srv)
    finally:
        srv.shutdown()
        srv.server_close()
