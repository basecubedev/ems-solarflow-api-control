# SPDX-License-Identifier: AGPL-3.0-or-later
"""Full Setup/Maintenance workflow parity for every supported MQTT broker kind.

Runs the real HTTP workflows — Fresh Setup ``/api/setup/config/apply`` and
Maintenance ``/api/admin/maintenance/config/apply`` — for a local anonymous
broker, a local authenticated broker and the Zendure cloud broker, then checks
the *complete* result of each: the broker profile exists and the device's
``broker_ref`` resolves, required credential references resolve through the
Core resolver, the applied device passes the EMS validators, the write
capability is correct, no plaintext secret reaches config.json, and an EMS
runtime can be reconstructed with no Admin process memory. Finally, both
workflows must produce an equivalent complete MQTT configuration (the whole
``zendure_mqtt`` block plus the full device entries, never only fragments).
"""

import json
import threading
import urllib.error
import urllib.request

import pytest

from admin.credential_store import CredentialStore
from admin.inverter_names import next_compact_inverter_name
from admin.mqtt_discovery import MqttBrokerDiscovery, MqttBrokerStore
from admin.server import ScanRegistry, create_server
from admin.zendure_cloud_mqtt import FakeCloudMqttListener, ZendureCloudDiscovery
from ems.mqtt_credentials import FileMqttCredentialResolver
from ems.zendure_mqtt.config_entries import (
    find_zendure_mqtt_broker_profile_issues,
    is_control_zendure_mqtt_device_config,
    validate_zendure_mqtt_control_device_config,
    validate_zendure_mqtt_device_config,
)
from ems.zendure_mqtt.runtime import build_zendure_mqtt_runtime
from tests.admin_auth_helpers import auth_headers, authenticate
from tests.helpers.fake_mqtt import FakeMqttNetwork
from tests.helpers.system_alignment import SetupReadySystemAlignment
from tests.test_admin_maintenance_config import _draft_item_from_proposal
from tests.test_admin_server import (
    _FakeReleaseManager,
    _fake_gateway_prober,
    _fake_scan,
)

pytestmark = pytest.mark.simulation

API_KEY = "raw-account-api-key"
MQTT_USER = "cloud-mqtt-user"
CLOUD_PASSWORD = "cloud-mqtt-secret-1"
CLOUD_APP_KEY = "cloud-app-key-1"
LOCAL_PASSWORD = "local-broker-password"


@pytest.fixture(autouse=True)
def _isolate(isolated_install_root):
    return isolated_install_root


def _cloud_fetch(_token, _timeout):
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
            "password": CLOUD_PASSWORD,
            "client_id": "cloud-client-1",
        },
        "app_key": CLOUD_APP_KEY,
    }


CASES = {
    # A supported legacy-JSON inverter observed acting as an inverter: the one
    # writable case in the matrix.
    "local_anonymous": {
        "observation": {
            "source_type": "local_mqtt",
            "broker_host": "10.0.0.10",
            "broker_port": 1883,
            "topic_family": "legacy_zendure_json",
            "serial_number": "PARITY1",
            "device_id": "PARITY1",
            "product_key": "PKPAR",
            "model_hint": "Hyper 2000",
            "metrics_seen": ["outputLimit", "electricLevel", "outputHomePower"],
        },
        "credentials_ref": None,
        "expect_write": True,
    },
    # A supported inverter on an authenticated broker. It defaults directly to
    # output control and its runtime credential is promoted from discovery.
    "local_authenticated": {
        "observation": {
            "source_type": "local_mqtt",
            "broker_host": "10.0.0.10",
            "broker_port": 1883,
            "topic_family": "legacy_zendure_json",
            "serial_number": "PARITY2",
            "device_id": "PARITY2",
            "product_key": "PKPAR2",
            "model_hint": "Hyper 2000",
            "credentials_ref": "home",
            "metrics_seen": ["electricLevel", "outputHomePower"],
        },
        "credentials_ref": "home",
        "expect_write": True,
    },
    # The supported Zendure cloud inverter defaults to output control; its
    # runtime credential is provisioned from the deviceList response.
    "zendure_cloud": {
        "observation": None,
        "credentials_ref": "zendure-cloud",
        "expect_write": True,
    },
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


def _setup_local_device():
    return {
        "source_id": "local:wr1",
        "config_name": "WR1",
        "display_name": "SolarFlow 800",
        "role": "inverter",
        "enabled": True,
        "ip": "192.168.1.100",
        "serial_number": "AAA",
    }


def _append_proposal_with_next_name(draft, proposal):
    names = [str(item.get("name") or "") for item in draft["devices"]]
    config_name = next_compact_inverter_name(names, len(draft["devices"]))
    draft["devices"].append(_draft_item_from_proposal(proposal, config_name))


def _local_discovery(observation):
    store = MqttBrokerStore(clock=lambda: 100.0, proposal_ttl_seconds=900)
    if observation is not None:
        broker = {
            "id": "mqtt:10.0.0.10:1883",
            "host": "10.0.0.10",
            "port": 1883,
            "devices": [observation],
        }
        generation = store.begin_refresh()
        store.complete_refresh(generation, [broker], success=True)
    return MqttBrokerDiscovery(store=store, topic_discoverer=None)


def _serve(tmp_path, case, fetch=None):
    messages = [
        (
            "iot/PK-AAA/DK-BBB/properties/report",
            json.dumps({"properties": {"electricLevel": 55, "outputHomePower": 120}}),
        )
    ]
    cloud = ZendureCloudDiscovery(
        CredentialStore().zendure,
        device_list_fetcher=fetch if fetch is not None else _cloud_fetch,
        listener_factory=lambda c: FakeCloudMqttListener(c, messages),
        timeout_s=0.0,
    )
    srv = create_server(
        "127.0.0.1",
        0,
        registry=ScanRegistry(scan_runner=_fake_scan),
        gateway_prober=_fake_gateway_prober,
        mqtt_discovery=_local_discovery(case["observation"]),
        zendure_cloud_discovery=cloud,
        release_manager=_FakeReleaseManager(tmp_path),
        system_alignment=SetupReadySystemAlignment(),
    )
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    authenticate(base)
    return srv, base


def _stage_case_inputs(srv, base, case):
    """Run the discovery-side prerequisites of a case (credentials, refresh)."""

    if case["credentials_ref"] == "home":
        srv.credential_store.save_mqtt_discovery_secret("home", "user", LOCAL_PASSWORD)
    if case["observation"] is None:
        status, payload = _request(
            f"{base}/api/discovery/zendure-cloud-mqtt/token",
            "POST",
            {"api_key": API_KEY},
        )
        assert status == 200 and payload["ok"] is True, payload
        status, payload = _request(
            f"{base}/api/discovery/zendure-cloud-mqtt/refresh", "POST", {}
        )
        assert status == 200 and payload["ok"] is True, payload


def _case_proposal(base, case):
    status, payload = _request(f"{base}/api/discovery/mqtt-proposals")
    assert status == 200
    wanted = "zendure_cloud" if case["observation"] is None else "local"
    for proposal in payload["proposals"]:
        if wanted == "zendure_cloud" and proposal["broker_ref"] == "zendure_cloud":
            return proposal
        if wanted == "local" and proposal["broker_ref"] != "zendure_cloud":
            return proposal
    raise AssertionError(f"no proposal for case: {payload}")


def _run_setup_workflow(root, case, monkeypatch):
    monkeypatch.setenv("EMS_INSTALL_DIR", str(root))
    srv, base = _serve(root, case)
    try:
        _stage_case_inputs(srv, base, case)
        proposal = _case_proposal(base, case)
        status, payload = _request(
            f"{base}/api/setup/config/apply",
            "POST",
            {
                "devices": [_setup_local_device()],
                "supported_grid_meter_count": 0,
                "zendure_mqtt_proposals": [
                    {"id": proposal["id"], "broker_ref": proposal["broker_ref"]}
                ],
            },
        )
        assert status == 200 and payload.get("ok") is True, payload
    finally:
        srv.shutdown()
        srv.server_close()
    config_path = root / "config" / "config.json"
    return config_path.read_text(encoding="utf-8"), root / "config" / "secrets"


def _write_maintenance_base_config(root):
    config_dir = root / "config"
    config_dir.mkdir(exist_ok=True)
    (config_dir / "config.json").write_text(
        json.dumps(
            {
                "system": {"max_total_power": 1600},
                "devices": [
                    {"name": "WR1", "ip": "192.168.1.100", "sn": "AAA", "max_power": 800}
                ],
                "grid_meter": {"type": "shelly", "ip": "192.168.1.50"},
            }
        ),
        encoding="utf-8",
    )
    return config_dir


def _run_maintenance_workflow(root, case, monkeypatch):
    monkeypatch.setenv("EMS_INSTALL_DIR", str(root))
    config_dir = _write_maintenance_base_config(root)
    srv, base = _serve(root, case)
    try:
        _stage_case_inputs(srv, base, case)
        proposal = _case_proposal(base, case)
        status, loaded = _request(f"{base}/api/admin/maintenance/config")
        assert status == 200 and loaded["status"] == "ok", loaded
        draft = loaded["draft"]
        _append_proposal_with_next_name(draft, proposal)
        status, payload = _request(
            f"{base}/api/admin/maintenance/config/apply",
            "POST",
            {"draft": draft, "revision": loaded["revision"], "confirm": True},
        )
        assert status == 200 and payload.get("ok") is True, payload
    finally:
        srv.shutdown()
        srv.server_close()
    return (config_dir / "config.json").read_text(encoding="utf-8"), config_dir / "secrets"


def _mqtt_devices(config):
    return [d for d in config["devices"] if d.get("type") == "zendure_mqtt"]


def _verify_complete_result(raw, secrets_dir, case):
    config = json.loads(raw)
    devices = _mqtt_devices(config)
    assert len(devices) == 1
    device = devices[0]

    # Broker profile exists and the device's broker_ref resolves to it.
    ref = device["mqtt"]["broker_ref"]
    brokers = config["zendure_mqtt"]["brokers"]
    assert ref in brokers, config["zendure_mqtt"]
    profile = brokers[ref]
    assert profile.get("enabled") is True
    assert profile.get("host")
    assert not find_zendure_mqtt_broker_profile_issues(config)

    # Credential reference resolves where required — through the Core resolver,
    # independent of any Admin process memory.
    if case["credentials_ref"] is None:
        assert "credentials_ref" not in profile
    else:
        assert profile["credentials_ref"] == case["credentials_ref"]
        credentials = FileMqttCredentialResolver(secrets_dir).resolve(
            case["credentials_ref"]
        )
        assert credentials.password

    # The applied device passes the matching EMS validator.
    validator = (
        validate_zendure_mqtt_control_device_config
        if is_control_zendure_mqtt_device_config(device)
        else validate_zendure_mqtt_device_config
    )
    issues = [
        issue
        for issue in validator(
            device, known_broker_refs=set(brokers), brokers_defined=True
        )
        if issue.get("severity") == "error"
    ]
    assert issues == []

    # Write capability is exactly what the case supports.
    assert device["capabilities"]["write_output_limit"] is case["expect_write"]

    # No plaintext secret in the config document.
    for secret in (API_KEY, CLOUD_PASSWORD, CLOUD_APP_KEY, MQTT_USER, LOCAL_PASSWORD):
        assert secret not in raw
    assert '"password"' not in raw

    # A reconstructed EMS runtime accepts the profile (no auth/profile issue).
    network = FakeMqttNetwork()
    runtime = build_zendure_mqtt_runtime(
        config,
        service_factory=network.telemetry_service_factory(),
        credential_resolver=FileMqttCredentialResolver(secrets_dir),
    )
    try:
        summaries = {
            broker["broker_ref"]: broker for broker in runtime.status()["brokers"]
        }
        assert ref in summaries
        assert summaries[ref]["issue"] is None, summaries[ref]
    finally:
        runtime.stop()
    return config


@pytest.mark.parametrize("case_name", sorted(CASES))
@pytest.mark.parametrize("workflow", ["setup", "maintenance"])
def test_workflow_produces_complete_resolvable_mqtt_config(
    workflow, case_name, tmp_path_factory, monkeypatch
):
    case = CASES[case_name]
    run = _run_setup_workflow if workflow == "setup" else _run_maintenance_workflow
    raw, secrets_dir = run(tmp_path_factory.mktemp(workflow), case, monkeypatch)
    _verify_complete_result(raw, secrets_dir, case)


@pytest.mark.parametrize("case_name", sorted(CASES))
def test_setup_and_maintenance_produce_equivalent_mqtt_config(
    case_name, tmp_path_factory, monkeypatch
):
    case = CASES[case_name]
    setup_raw, _ = _run_setup_workflow(
        tmp_path_factory.mktemp("setup"), case, monkeypatch
    )
    maintenance_raw, _ = _run_maintenance_workflow(
        tmp_path_factory.mktemp("maintenance"), case, monkeypatch
    )
    setup_config = json.loads(setup_raw)
    maintenance_config = json.loads(maintenance_raw)

    # The complete MQTT configuration must match: every broker profile field
    # (source, endpoint, TLS, credentials_ref) and every device field
    # (identifiers, topic_family, write_protocol, capabilities, broker_ref).
    assert setup_config["zendure_mqtt"] == maintenance_config["zendure_mqtt"]
    assert _mqtt_devices(setup_config) == _mqtt_devices(maintenance_config)


# --- credential-decision parity across the real HTTP Apply paths ------------
# Both workflows must make the identical credential decision for every state a
# referenced record can be in: reuse without a network call, rotate from a
# trusted source, reprovision through the shared service, or block with the
# same stable error code and rollback report.


CLOUD_REF = "zendure-cloud"
STALE_CLOUD_PASSWORD = "stale-cloud-secret"
ACTIVE_CLOUD_PASSWORD = "already-active-secret"
OLD_LOCAL_PASSWORD = "old-local-password"


def _seed_cloud_record(srv, **overrides):
    fields = {
        "username": MQTT_USER,
        "password": ACTIVE_CLOUD_PASSWORD,
        "client_id": "active-client",
        "app_key": "active-app-key",
    }
    fields.update(overrides)
    srv.credential_store.save_mqtt_cloud_runtime_secret(CLOUD_REF, **fields)


def _write_record_bytes(srv, ref, data):
    srv.credential_store.secrets_dir.mkdir(parents=True, exist_ok=True)
    (srv.credential_store.secrets_dir / f"mqtt-{ref}.json").write_bytes(data)


def _prepare_local_valid_record(srv, fetch, monkeypatch):
    srv.credential_store.save_mqtt_broker_secret("home", "user", LOCAL_PASSWORD)


def _prepare_local_rotation(srv, fetch, monkeypatch):
    srv.credential_store.save_mqtt_broker_secret("home", "user", OLD_LOCAL_PASSWORD)


def _prepare_local_empty_record(srv, fetch, monkeypatch):
    srv.credential_store.save_mqtt_broker_secret("home", None, None)
    srv.credential_store.forget_mqtt_discovery_secret("home")


def _prepare_local_malformed_record(srv, fetch, monkeypatch):
    _write_record_bytes(srv, "home", b"{broken json")
    srv.credential_store.forget_mqtt_discovery_secret("home")


def _prepare_cloud_valid_record(srv, fetch, monkeypatch):
    _seed_cloud_record(srv)
    # A deviceList outage must not matter when nothing needs the API.
    fetch.fail = True


def _prepare_cloud_missing_app_key(srv, fetch, monkeypatch):
    _seed_cloud_record(srv, password=STALE_CLOUD_PASSWORD, app_key=None)


def _prepare_cloud_missing_client_id(srv, fetch, monkeypatch):
    _seed_cloud_record(srv, password=STALE_CLOUD_PASSWORD, client_id=None)


def _prepare_cloud_invalid_without_api_key(srv, fetch, monkeypatch):
    _seed_cloud_record(srv, password=STALE_CLOUD_PASSWORD, app_key=None)
    srv.credential_store.zendure.delete_token()


def _prepare_cloud_failed_internal_rollback(srv, fetch, monkeypatch):
    import ems.mqtt_credentials as mqtt_credentials

    from tests.test_admin_mqtt_runtime_provisioning import (
        _corrupt_record,
        _patch_restore_failure,
    )

    _seed_cloud_record(srv, password=STALE_CLOUD_PASSWORD)
    _corrupt_record(srv.credential_store, CLOUD_REF)

    def _broken_resolve(self, ref):
        raise mqtt_credentials.MqttCredentialError("verification failed")

    monkeypatch.setattr(
        mqtt_credentials.FileMqttCredentialResolver, "resolve", _broken_resolve
    )
    _patch_restore_failure(monkeypatch)


def _prepare_cloud_post_staging_validation_failure(srv, fetch, monkeypatch):
    # Provisioning writes a valid record, but the final transaction boundary
    # rejects it — modelling a later staging step that silently broke an
    # earlier-validated record. Both flows must fail identically and roll the
    # provisioned record back.
    import admin.mqtt_runtime_provisioning as provisioning

    monkeypatch.setattr(
        provisioning,
        "validate_all_runtime_credentials",
        lambda config, *, credential_store: [CLOUD_REF],
    )


def _verify_local_rotated(outcome):
    resolved = FileMqttCredentialResolver(outcome["secrets_dir"]).resolve("home")
    assert (resolved.username, resolved.password) == ("user", LOCAL_PASSWORD)


def _verify_cloud_reused(outcome):
    resolved = FileMqttCredentialResolver(outcome["secrets_dir"]).resolve(CLOUD_REF)
    assert resolved.password == ACTIVE_CLOUD_PASSWORD
    assert resolved.app_key == "active-app-key"


def _verify_cloud_reprovisioned(outcome):
    resolved = FileMqttCredentialResolver(outcome["secrets_dir"]).resolve(CLOUD_REF)
    assert resolved.password == CLOUD_PASSWORD
    assert resolved.client_id == "cloud-client-1"
    assert resolved.app_key == CLOUD_APP_KEY


def _verify_malformed_record_preserved(outcome):
    record = outcome["secrets_dir"] / "mqtt-home.json"
    assert record.read_bytes() == b"{broken json"


CREDENTIAL_SCENARIOS = {
    "local_anonymous_no_credentials": {
        "case": "local_anonymous",
        "prepare": None,
        "status": 200,
        "code": None,
        "calls": 0,
        "rollback": None,
        "verify": None,
    },
    "local_valid_record_reused": {
        "case": "local_authenticated",
        "prepare": _prepare_local_valid_record,
        "status": 200,
        "code": None,
        "calls": 0,
        "rollback": None,
        "verify": _verify_local_rotated,
    },
    "local_rotation_from_discovery": {
        "case": "local_authenticated",
        "prepare": _prepare_local_rotation,
        "status": 200,
        "code": None,
        "calls": 0,
        "rollback": None,
        "verify": _verify_local_rotated,
    },
    "local_empty_record_blocked": {
        "case": "local_authenticated",
        "prepare": _prepare_local_empty_record,
        "status": 400,
        "code": "credential_provisioning_failed",
        "calls": 0,
        "rollback": None,
        "verify": None,
    },
    "local_malformed_record_blocked": {
        "case": "local_authenticated",
        "prepare": _prepare_local_malformed_record,
        "status": 400,
        "code": "credential_provisioning_failed",
        "calls": 0,
        "rollback": None,
        "verify": _verify_malformed_record_preserved,
    },
    "cloud_valid_record_reused": {
        "case": "zendure_cloud",
        "prepare": _prepare_cloud_valid_record,
        "status": 200,
        "code": None,
        "calls": 0,
        "rollback": None,
        "verify": _verify_cloud_reused,
    },
    "cloud_missing_app_key_reprovisioned": {
        "case": "zendure_cloud",
        "prepare": _prepare_cloud_missing_app_key,
        "status": 200,
        "code": None,
        "calls": 1,
        "rollback": None,
        "verify": _verify_cloud_reprovisioned,
    },
    "cloud_missing_client_id_reprovisioned": {
        "case": "zendure_cloud",
        "prepare": _prepare_cloud_missing_client_id,
        "status": 200,
        "code": None,
        "calls": 1,
        "rollback": None,
        "verify": _verify_cloud_reprovisioned,
    },
    "cloud_invalid_without_api_key_blocked": {
        "case": "zendure_cloud",
        "prepare": _prepare_cloud_invalid_without_api_key,
        "status": 400,
        "code": "credential_provisioning_failed",
        "calls": 0,
        "rollback": None,
        "verify": None,
    },
    "cloud_failed_internal_rollback": {
        "case": "zendure_cloud",
        "prepare": _prepare_cloud_failed_internal_rollback,
        "status": 400,
        "code": "credential_provisioning_failed",
        "calls": 1,
        "rollback": {"severity": "high", "failed_refs": [CLOUD_REF]},
        "verify": None,
    },
    "cloud_post_staging_validation_failure": {
        "case": "zendure_cloud",
        "prepare": _prepare_cloud_post_staging_validation_failure,
        "status": 400,
        "code": "credential_provisioning_failed",
        "calls": 1,
        "rollback": None,
        "verify": None,
    },
}


def _credential_apply_outcome(workflow, root, case, monkeypatch, fetch, prepare):
    monkeypatch.setenv("EMS_INSTALL_DIR", str(root))
    config_path = root / "config" / "config.json"
    config_before = None
    if workflow == "maintenance":
        _write_maintenance_base_config(root)
        config_before = config_path.read_bytes()
    srv, base = _serve(root, case, fetch=fetch)
    try:
        _stage_case_inputs(srv, base, case)
        proposal = _case_proposal(base, case)
        if prepare is not None:
            prepare(srv, fetch, monkeypatch)
        calls_before = fetch.calls
        if workflow == "setup":
            status, payload = _request(
                f"{base}/api/setup/config/apply",
                "POST",
                {
                    "devices": [_setup_local_device()],
                    "supported_grid_meter_count": 0,
                    "zendure_mqtt_proposals": [
                        {"id": proposal["id"], "broker_ref": proposal["broker_ref"]}
                    ],
                },
            )
        else:
            status, loaded = _request(f"{base}/api/admin/maintenance/config")
            assert status == 200 and loaded["status"] == "ok", loaded
            draft = loaded["draft"]
            _append_proposal_with_next_name(draft, proposal)
            status, payload = _request(
                f"{base}/api/admin/maintenance/config/apply",
                "POST",
                {"draft": draft, "revision": loaded["revision"], "confirm": True},
            )
    finally:
        srv.shutdown()
        srv.server_close()
    return {
        "workflow": workflow,
        "status": status,
        "payload": payload,
        "calls": fetch.calls - calls_before,
        "config_path": config_path,
        "config_before": config_before,
        "secrets_dir": root / "config" / "secrets",
    }


def _try_resolve(secrets_dir, ref):
    try:
        got = FileMqttCredentialResolver(secrets_dir).resolve(ref)
        return (got.username, got.password, got.client_id, got.app_key)
    except Exception as exc:
        return type(exc).__name__


def _runtime_issues(outcome):
    config = json.loads(outcome["config_path"].read_text(encoding="utf-8"))
    network = FakeMqttNetwork()
    runtime = build_zendure_mqtt_runtime(
        config,
        service_factory=network.telemetry_service_factory(),
        credential_resolver=FileMqttCredentialResolver(outcome["secrets_dir"]),
    )
    try:
        return {
            broker["broker_ref"]: broker["issue"]
            for broker in runtime.status()["brokers"]
        }
    finally:
        runtime.stop()


@pytest.mark.parametrize("scenario_name", sorted(CREDENTIAL_SCENARIOS))
def test_setup_and_maintenance_share_credential_decisions(
    scenario_name, tmp_path_factory, monkeypatch
):
    from tests.test_admin_maintenance_mqtt_apply import _CloudFetch

    scenario = CREDENTIAL_SCENARIOS[scenario_name]
    case = CASES[scenario["case"]]
    outcomes = {}
    for workflow in ("setup", "maintenance"):
        fetch = _CloudFetch()
        root = tmp_path_factory.mktemp(f"{workflow}-cred")
        outcomes[workflow] = _credential_apply_outcome(
            workflow, root, case, monkeypatch, fetch, scenario["prepare"]
        )
    setup, maintenance = outcomes["setup"], outcomes["maintenance"]

    for outcome in outcomes.values():
        assert outcome["status"] == scenario["status"], outcome["payload"]
        assert outcome["payload"].get("code") == scenario["code"], outcome["payload"]
        assert outcome["calls"] == scenario["calls"]
        rollback = outcome["payload"].get("credential_rollback")
        if scenario["rollback"] is None:
            assert rollback is None, outcome["payload"]
        else:
            assert rollback is not None, outcome["payload"]
            assert rollback["severity"] == scenario["rollback"]["severity"]
            assert rollback["failed_refs"] == scenario["rollback"]["failed_refs"]
        blob = json.dumps(outcome["payload"])
        for secret in (
            API_KEY,
            CLOUD_PASSWORD,
            CLOUD_APP_KEY,
            LOCAL_PASSWORD,
            ACTIVE_CLOUD_PASSWORD,
            STALE_CLOUD_PASSWORD,
            OLD_LOCAL_PASSWORD,
        ):
            assert secret not in blob
        if scenario["verify"] is not None:
            scenario["verify"](outcome)

    # The Core-visible credential state must be identical in both workflows.
    ref = case["credentials_ref"]
    if ref is not None:
        assert _try_resolve(setup["secrets_dir"], ref) == _try_resolve(
            maintenance["secrets_dir"], ref
        )
    if scenario["status"] == 200:
        setup_config = json.loads(setup["config_path"].read_text(encoding="utf-8"))
        maintenance_config = json.loads(
            maintenance["config_path"].read_text(encoding="utf-8")
        )
        assert setup_config["zendure_mqtt"] == maintenance_config["zendure_mqtt"]
        assert _mqtt_devices(setup_config) == _mqtt_devices(maintenance_config)
        assert _runtime_issues(setup) == _runtime_issues(maintenance)
    else:
        # A blocked apply commits nothing: Setup writes no config at all and
        # Maintenance leaves the existing one untouched.
        assert not setup["config_path"].exists()
        assert maintenance["config_path"].read_bytes() == maintenance["config_before"]


# --- integrity-violation parity across the real HTTP Apply paths ------------
# A non-canonical configured reference and a cross-source shared reference are
# rejected identically by Setup and Maintenance: same status, same stable code,
# same reference/sources, and neither flow mutates config.json.


def _local_auth_case(credentials_ref, serial="PARITYX"):
    return {
        "observation": {
            "source_type": "local_mqtt",
            "broker_host": "10.0.0.10",
            "broker_port": 1883,
            "topic_family": "legacy_zendure_json",
            "serial_number": serial,
            "device_id": serial,
            "product_key": f"PK-{serial}",
            "model_hint": "Hyper 2000",
            "credentials_ref": credentials_ref,
            "metrics_seen": ["electricLevel", "outputHomePower"],
        },
        "credentials_ref": credentials_ref,
        "expect_write": True,
    }


def _apply_maintenance_proposal(base, case):
    proposal = _case_proposal(base, case)
    status, loaded = _request(f"{base}/api/admin/maintenance/config")
    assert status == 200 and loaded["status"] == "ok", loaded
    draft = loaded["draft"]
    _append_proposal_with_next_name(draft, proposal)
    return _request(
        f"{base}/api/admin/maintenance/config/apply",
        "POST",
        {"draft": draft, "revision": loaded["revision"], "confirm": True},
    )


def _reapply_maintenance(base):
    status, loaded = _request(f"{base}/api/admin/maintenance/config")
    assert status == 200 and loaded["status"] == "ok", loaded
    return _request(
        f"{base}/api/admin/maintenance/config/apply",
        "POST",
        {"draft": loaded["draft"], "revision": loaded["revision"], "confirm": True},
    )


def _cloud_token_and_refresh(base):
    status, payload = _request(
        f"{base}/api/discovery/zendure-cloud-mqtt/token", "POST", {"api_key": API_KEY}
    )
    assert status == 200 and payload["ok"] is True, payload
    status, payload = _request(
        f"{base}/api/discovery/zendure-cloud-mqtt/refresh", "POST", {}
    )
    assert status == 200 and payload["ok"] is True, payload


def _assert_no_secrets(payload):
    blob = json.dumps(payload)
    for secret in (API_KEY, CLOUD_PASSWORD, CLOUD_APP_KEY, LOCAL_PASSWORD, MQTT_USER):
        assert secret not in blob


def test_setup_and_maintenance_reject_invalid_configured_ref_identically(
    tmp_path_factory, monkeypatch
):
    from tests.test_admin_maintenance_mqtt_apply import _CloudFetch

    # Setup: a non-canonical observation reference flows into the generated
    # config and is blocked at apply.
    setup_root = tmp_path_factory.mktemp("setup-invalid-ref")
    monkeypatch.setenv("EMS_INSTALL_DIR", str(setup_root))
    case_bad = _local_auth_case("Bad Ref")
    srv, base = _serve(setup_root, case_bad, fetch=_CloudFetch())
    try:
        srv.credential_store.save_mqtt_discovery_secret("bad-ref", "user", LOCAL_PASSWORD)
        proposal = _case_proposal(base, case_bad)
        s_status, s_payload = _request(
            f"{base}/api/setup/config/apply",
            "POST",
            {
                "devices": [],
                "supported_grid_meter_count": 0,
                "zendure_mqtt_proposals": [
                    {"id": proposal["id"], "broker_ref": proposal["broker_ref"]}
                ],
            },
        )
    finally:
        srv.shutdown()
        srv.server_close()
    assert not (setup_root / "config" / "config.json").exists()

    # Maintenance: a valid apply, then the stored reference is hand-edited to a
    # non-canonical value, is blocked on re-apply with config untouched.
    maint_root = tmp_path_factory.mktemp("maint-invalid-ref")
    monkeypatch.setenv("EMS_INSTALL_DIR", str(maint_root))
    _write_maintenance_base_config(maint_root)
    case_ok = _local_auth_case("home")
    srv, base = _serve(maint_root, case_ok, fetch=_CloudFetch())
    try:
        srv.credential_store.save_mqtt_discovery_secret("home", "user", LOCAL_PASSWORD)
        status, payload = _apply_maintenance_proposal(base, case_ok)
        assert status == 200 and payload.get("ok") is True, payload
        config_path = maint_root / "config" / "config.json"
        config = json.loads(config_path.read_text(encoding="utf-8"))
        for profile in config["zendure_mqtt"]["brokers"].values():
            if profile.get("credentials_ref") == "home":
                profile["credentials_ref"] = "Bad Ref"
        config_path.write_text(json.dumps(config), encoding="utf-8")
        before = config_path.read_bytes()
        m_status, m_payload = _reapply_maintenance(base)
        assert config_path.read_bytes() == before
        assert not (
            maint_root / "config" / "secrets" / "mqtt-bad-ref.json"
        ).exists()
    finally:
        srv.shutdown()
        srv.server_close()

    # Both workflows: identical stable rejection, no secrets leaked.
    assert s_status == m_status == 400
    assert (
        s_payload.get("code")
        == m_payload.get("code")
        == "mqtt_credentials_ref_invalid"
    )
    assert s_payload.get("credentials_ref") == m_payload.get("credentials_ref") == "Bad Ref"
    _assert_no_secrets(s_payload)
    _assert_no_secrets(m_payload)


def test_setup_and_maintenance_reject_cross_source_shared_ref_identically(
    tmp_path_factory, monkeypatch
):
    from tests.test_admin_maintenance_mqtt_apply import _CloudFetch

    # Setup: a local broker whose reference collides with the cloud reference is
    # blocked at apply when both brokers are selected.
    setup_root = tmp_path_factory.mktemp("setup-conflict")
    monkeypatch.setenv("EMS_INSTALL_DIR", str(setup_root))
    case = _local_auth_case("zendure-cloud")
    srv, base = _serve(setup_root, case, fetch=_CloudFetch())
    try:
        srv.credential_store.save_mqtt_discovery_secret(
            "zendure-cloud", "user", LOCAL_PASSWORD
        )
        _cloud_token_and_refresh(base)
        status, payload = _request(f"{base}/api/discovery/mqtt-proposals")
        assert status == 200
        local = next(p for p in payload["proposals"] if p["broker_ref"] != "zendure_cloud")
        cloud = next(p for p in payload["proposals"] if p["broker_ref"] == "zendure_cloud")
        s_status, s_payload = _request(
            f"{base}/api/setup/config/apply",
            "POST",
            {
                "devices": [],
                "supported_grid_meter_count": 0,
                "zendure_mqtt_proposals": [
                    {"id": local["id"], "broker_ref": local["broker_ref"]},
                    {"id": cloud["id"], "broker_ref": cloud["broker_ref"]},
                ],
            },
        )
    finally:
        srv.shutdown()
        srv.server_close()
    assert not (setup_root / "config" / "config.json").exists()

    # Maintenance: apply a valid cloud device and a valid local device, then
    # repoint the local broker at the cloud reference and re-apply.
    maint_root = tmp_path_factory.mktemp("maint-conflict")
    monkeypatch.setenv("EMS_INSTALL_DIR", str(maint_root))
    _write_maintenance_base_config(maint_root)
    case_local = _local_auth_case("home")
    srv, base = _serve(maint_root, case_local, fetch=_CloudFetch())
    try:
        srv.credential_store.save_mqtt_discovery_secret("home", "user", LOCAL_PASSWORD)
        _cloud_token_and_refresh(base)
        status, payload = _apply_maintenance_proposal(base, {"observation": None})
        assert status == 200 and payload.get("ok") is True, payload
        status, payload = _apply_maintenance_proposal(base, case_local)
        assert status == 200 and payload.get("ok") is True, payload
        config_path = maint_root / "config" / "config.json"
        config = json.loads(config_path.read_text(encoding="utf-8"))
        for profile in config["zendure_mqtt"]["brokers"].values():
            if profile.get("source") == "local_mqtt":
                profile["credentials_ref"] = "zendure-cloud"
        config_path.write_text(json.dumps(config), encoding="utf-8")
        before = config_path.read_bytes()
        m_status, m_payload = _reapply_maintenance(base)
        assert config_path.read_bytes() == before
    finally:
        srv.shutdown()
        srv.server_close()

    assert s_status == m_status == 400
    assert (
        s_payload.get("code")
        == m_payload.get("code")
        == "mqtt_credential_source_conflict"
    )
    assert (
        s_payload.get("credentials_ref")
        == m_payload.get("credentials_ref")
        == "zendure-cloud"
    )
    assert sorted(s_payload.get("sources") or []) == sorted(
        m_payload.get("sources") or []
    ) == ["local_mqtt", "zendure_cloud_mqtt"]
    _assert_no_secrets(s_payload)
    _assert_no_secrets(m_payload)
