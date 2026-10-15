# SPDX-License-Identifier: AGPL-3.0-or-later
"""The cloud discovery never signs in under the MQTT session the EMS holds.

Review 4 (AL17): the Zendure cloud broker admits one session per client id,
and a second session under it drops the first. ``provision_runtime_credentials``
stores the deviceList ``client_id`` in the EMS's cloud runtime record, the EMS
connects with exactly that id, and the cloud refresh listened with the same id
from a fresh deviceList: every refresh took the live EMS session over for the
listen window, and both sides knocked each other off on reconnect.

While an enabled cloud broker profile of the installed config signs in with
this account's client id, or the EMS's live status shows a connected cloud
broker and a cloud runtime record carries that id, a refresh reads the device
list only and says that live telemetry was not sampled.
"""

import json
from types import SimpleNamespace

import pytest

from admin.credential_store import ZENDURE_CLOUD_CREDENTIALS_REF, CredentialStore
from admin.discovery_preparation import SOURCE_ZENDURE_MQTT
from admin.discovery_run import refresh_enabled_sources
from admin.zendure_cloud_mqtt import ZendureCloudDiscovery, ems_cloud_session_client_ids
from admin.zendure_mqtt_broker_profiles import CLOUD_BROKER_PROFILE, CLOUD_BROKER_REF
from admin.zendure_mqtt_config_proposals import (
    WAITING_FOR_MQTT_TELEMETRY,
    proposals_from_sources,
)

pytestmark = [
    pytest.mark.admin,
    pytest.mark.mqtt,
    pytest.mark.setup,
    pytest.mark.maintenance,
    pytest.mark.contract,
]

CLIENT_ID = "account-client-id"
REPORT = (
    "iot/PK-AAA/DK-BBB/properties/report",
    json.dumps({"properties": {"electricLevel": 55, "outputHomePower": 120}}),
)


@pytest.fixture(autouse=True)
def _install(isolated_install_root):
    return isolated_install_root


class _Account:
    def __init__(self, client_id=CLIENT_ID):
        self.client_id = client_id

    def __call__(self, _key, _timeout):
        return {
            "devices": [
                {
                    "productKey": "PK-AAA",
                    "deviceKey": "DK-BBB",
                    "productModel": "SolarFlow 800 Pro",
                    "snNumber": "SN-CLOUD1",
                    "deviceName": "Cloud inverter",
                }
            ],
            "mqtt": {
                "host": "mqtteu.zen-iot.com",
                "port": 8883,
                "username": "cloud-user",
                "password": "cloud-secret",
                "client_id": self.client_id,
            },
            "app_key": "cloud-app-key",
        }


class _Broker:
    """Records every session a listener opens and replays one report."""

    def __init__(self):
        self.sessions = []

    def listener(self, connection):
        broker = self

        class _Listener:
            def listen(self, timeout_s, on_message):
                broker.sessions.append(connection.client_id)
                on_message(*REPORT)

        return _Listener()


def _cloud(store, broker, account=None, **kwargs):
    cloud = ZendureCloudDiscovery(
        store.zendure,
        device_list_fetcher=account or _Account(),
        listener_factory=broker.listener,
        timeout_s=0.0,
        **kwargs,
    )
    cloud.save_token("raw-account-api-key")
    return cloud


def _write_config(root, profile):
    config_dir = root / "config"
    config_dir.mkdir(exist_ok=True)
    (config_dir / "config.json").write_text(
        json.dumps({"zendure_mqtt": {"brokers": {CLOUD_BROKER_REF: profile}}}),
        encoding="utf-8",
    )


def _ems_on_the_cloud(root, store, cloud, **profile):
    """The state a Setup or Maintenance apply of a cloud device leaves behind."""

    cloud.provision_runtime_credentials(store)
    _write_config(root, {**CLOUD_BROKER_PROFILE, **profile})


def test_a_refresh_never_signs_in_under_the_session_the_ems_holds(_install):
    store = CredentialStore()
    broker = _Broker()
    cloud = _cloud(store, broker)
    _ems_on_the_cloud(_install, store, cloud)

    result = cloud.refresh()

    assert broker.sessions == []
    assert result["ok"] is True
    assert result["mqtt_status"] == "ems_session"
    assert "not sampled" in result["mqtt_message"]
    assert result["device_list_count"] == 1
    assert result["mqtt_observed_count"] == 0
    [candidate] = result["candidates"]
    assert candidate["discovery_status"] == "device_list_only"
    assert candidate["serial_number"] == "SN-CLOUD1"
    assert candidate["model_hint"] == "SolarFlow 800 Pro"
    assert store.zendure.settings()["last_status"] == "ok"


def test_a_device_list_refresh_proposes_the_device_and_names_what_it_did_not_see(_install):
    """What the listen adds is the telemetry: topic family, metrics, capabilities.

    The device list still names the model, the serial, the product key and the
    route id, so the proposal keeps its write route and model; what only
    telemetry proves is flagged instead of guessed.
    """

    store = CredentialStore()
    broker = _Broker()
    cloud = _cloud(store, broker)
    _ems_on_the_cloud(_install, store, cloud)
    cloud.refresh()

    [proposal] = proposals_from_sources([], cloud.trusted_candidates())

    assert proposal["serial_number"] == "SN-CLOUD1"
    assert proposal["hardware_model"] == "solarflow_800_pro"
    assert proposal["config_fragment"]["mqtt"]["device_id"] == "DK-BBB"
    assert proposal["config_fragment"]["mqtt"]["product_key"] == "PK-AAA"
    assert proposal["metrics"] == []
    assert WAITING_FOR_MQTT_TELEMETRY in proposal["warnings"]
    assert "unknown_topic_family" in proposal["warnings"]


@pytest.mark.parametrize(
    ("profile", "account"),
    (
        ({"enabled": False}, _Account()),
        ({}, _Account("another-account-client-id")),
    ),
    ids=("ems_cloud_broker_disabled", "ems_on_another_account"),
)
def test_a_refresh_listens_when_the_ems_holds_no_session_under_this_client_id(
    _install, profile, account
):
    store = CredentialStore()
    broker = _Broker()
    provisioning = _cloud(store, _Broker(), account=_Account())
    _ems_on_the_cloud(_install, store, provisioning, **profile)
    cloud = _cloud(store, broker, account=account)

    result = cloud.refresh()

    assert broker.sessions == [account.client_id]
    assert result["mqtt_status"] == "ok"
    assert result["mqtt_observed_count"] == 1


def test_a_fresh_install_listens(_install):
    store = CredentialStore()
    broker = _Broker()

    result = _cloud(store, broker).refresh()

    assert broker.sessions == [CLIENT_ID]
    assert result["mqtt_status"] == "ok"


def test_an_unreadable_config_is_not_taken_for_one_without_the_ems_session(_install):
    """Whether the EMS holds the session cannot be ruled out: no session is opened."""

    (_install / "config").mkdir(exist_ok=True)
    (_install / "config" / "config.json").write_text("{not json", encoding="utf-8")
    store = CredentialStore()
    broker = _Broker()

    result = _cloud(store, broker).refresh()

    assert ems_cloud_session_client_ids(store.secrets_dir) is None
    assert broker.sessions == []
    assert result["mqtt_status"] == "ems_session"
    assert result["device_list_count"] == 1


def test_the_session_rule_reads_the_client_id_the_ems_runtime_connects_with(_install):
    store = CredentialStore()
    cloud = _cloud(store, _Broker())
    _ems_on_the_cloud(_install, store, cloud)

    from ems.mqtt_credentials import FileMqttCredentialResolver
    from ems.zendure_mqtt.runtime import load_zendure_mqtt_broker_configs

    config = json.loads((_install / "config" / "config.json").read_text())
    brokers, _, _ = load_zendure_mqtt_broker_configs(
        config["zendure_mqtt"],
        credential_resolver=FileMqttCredentialResolver(store.secrets_dir),
    )

    assert brokers[CLOUD_BROKER_REF].client_config().client_id == CLIENT_ID
    assert ems_cloud_session_client_ids(store.secrets_dir) == frozenset({CLIENT_ID})


def test_the_discovery_run_carries_the_reason_in_its_refresh_block(_install):
    store = CredentialStore()
    broker = _Broker()
    cloud = _cloud(store, broker)
    _ems_on_the_cloud(_install, store, cloud)

    block, warnings = refresh_enabled_sources(
        [SOURCE_ZENDURE_MQTT],
        mdns_provider=SimpleNamespace(refresh=dict),
        mqtt_discovery=SimpleNamespace(refresh=dict),
        zendure_cloud_discovery=cloud,
    )

    status = block["sources"][SOURCE_ZENDURE_MQTT]
    assert status["ok"] is True
    assert status["mqtt_status"] == "ems_session"
    assert "not sampled" in status["mqtt_message"]
    assert warnings == []
    assert broker.sessions == []


class _ConnectedService:
    """A broker connection the EMS holds, as its telemetry runtime reports it."""

    def __init__(self, config, connected=True):
        self.config = config
        self.connected = connected
        self.running = connected

    def start(self):
        pass

    def stop(self):
        pass

    def snapshots(self):
        return {}

    def status(self):
        return {"connected": self.connected}


def _ems_snapshot(root, store, config, *, connected=True, written_at=None):
    """The live status the running EMS writes for the config it connected with."""

    from admin.install_context import detect_install_context
    from ems import paths
    from ems.mqtt_credentials import FileMqttCredentialResolver
    from ems.zendure_mqtt.runtime import build_zendure_mqtt_runtime

    runtime = build_zendure_mqtt_runtime(
        config,
        service_factory=lambda broker: _ConnectedService(broker, connected),
        credential_resolver=FileMqttCredentialResolver(store.secrets_dir),
    )
    path = detect_install_context().data_dir / paths.ZENDURE_MQTT_STATUS_FILENAME
    assert runtime.write_status_file(path) is True
    if written_at is not None:
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["written_at"] = written_at
        path.write_text(json.dumps(payload), encoding="utf-8")


def _running_on_v1(root, store, cloud, **snapshot):
    """The EMS connected from the installed config v1 and has not restarted since."""

    _ems_on_the_cloud(root, store, cloud)
    v1 = json.loads((root / "config" / "config.json").read_text(encoding="utf-8"))
    _ems_snapshot(root, store, v1, **snapshot)


def _config_disabled(root):
    _write_config(root, {**CLOUD_BROKER_PROFILE, "enabled": False})


def _config_removed(root):
    (root / "config" / "config.json").write_text("{}", encoding="utf-8")


@pytest.mark.parametrize(
    "applied", (_config_disabled, _config_removed), ids=("disabled", "removed")
)
def test_a_config_waiting_for_an_ems_restart_leaves_the_ems_its_session(_install, applied):
    """Review 4 (r11): the session rule read the installed config only.

    The EMS connected from config v1. The config editor then disables or
    removes the cloud broker profile, a change it lists under "Needs an EMS
    restart": until that restart the EMS still signs in under the account's
    client id, and its live status says so. A refresh reads the device list
    only.
    """

    store = CredentialStore()
    broker = _Broker()
    cloud = _cloud(store, broker)
    _running_on_v1(_install, store, cloud)
    applied(_install)

    result = cloud.refresh()

    assert broker.sessions == []
    assert result["mqtt_status"] == "ems_session"
    assert ems_cloud_session_client_ids(store.secrets_dir) == frozenset({CLIENT_ID})


def test_a_connected_cloud_session_whose_record_cannot_be_read_is_not_taken(_install):
    """The EMS holds a cloud session, and no record says under which client id."""

    store = CredentialStore()
    broker = _Broker()
    cloud = _cloud(store, broker)
    _running_on_v1(_install, store, cloud)
    _config_removed(_install)
    store.forget_mqtt_broker_secret(ZENDURE_CLOUD_CREDENTIALS_REF)

    result = cloud.refresh()

    assert ems_cloud_session_client_ids(store.secrets_dir) is None
    assert broker.sessions == []
    assert result["mqtt_status"] == "ems_session"


@pytest.mark.parametrize(
    "snapshot",
    ({"connected": False}, {"written_at": 0.0}),
    ids=("ems_not_connected", "snapshot_stale"),
)
def test_a_live_status_without_a_cloud_session_leaves_the_config_rule(_install, snapshot):
    """Only a connected cloud broker in a live status counts beside the config."""

    store = CredentialStore()
    broker = _Broker()
    cloud = _cloud(store, broker)
    _running_on_v1(_install, store, cloud, **snapshot)
    _config_disabled(_install)

    result = cloud.refresh()

    assert broker.sessions == [CLIENT_ID]
    assert result["mqtt_status"] == "ok"


def test_a_live_cloud_session_on_another_account_leaves_this_one_free(_install):
    store = CredentialStore()
    broker = _Broker()
    provisioning = _cloud(store, _Broker(), account=_Account())
    _running_on_v1(_install, store, provisioning)
    account = _Account("another-account-client-id")
    cloud = _cloud(store, broker, account=account)

    result = cloud.refresh()

    assert broker.sessions == [account.client_id]
    assert result["mqtt_status"] == "ok"


def test_an_unnamed_live_session_is_not_reported_as_an_unreadable_config(_install):
    """The config was read here: the live status shows a cloud session that no
    readable record names, and the reason must not blame the config."""

    store = CredentialStore()
    cloud = _cloud(store, _Broker())
    _running_on_v1(_install, store, cloud)
    _config_removed(_install)
    store.forget_mqtt_broker_secret(ZENDURE_CLOUD_CREDENTIALS_REF)

    result = cloud.refresh()

    assert result["mqtt_status"] == "ems_session"
    assert "could not be read" not in result["mqtt_message"]
    assert "not sampled" in result["mqtt_message"]
