# SPDX-License-Identifier: AGPL-3.0-or-later
"""A discovery source is refreshed by one caller at a time.

Setup's discovery run (``POST /api/discovery/run`` with ``refresh``) and
Maintenance's discovery (``/api/discovery/mqtt-brokers/refresh``,
``/api/discovery/zendure-cloud-mqtt/refresh``) reach the same Zendure cloud and
local MQTT discovery services with the same saved credentials. The cloud
listener signs in with the account's one MQTT ``client_id``, and the local
broker refresh numbers its results by one shared generation, so two refreshes
side by side disturb each other. A refresh asked for while one with the same
inputs runs joins it; one with other inputs waits and runs on its own.
"""

import base64
import threading
from types import SimpleNamespace

import pytest

from admin.discovery_connections import DiscoveryConnectionsStore
from admin.mqtt_discovery import MqttBrokerDiscovery
from admin.secret_store import ZendureTokenStore
from admin.server import ScanRegistry, create_admin_runtime, create_server
from admin.single_flight import SingleFlight
from admin.zendure_cloud_auth import ZendureCloudError
from admin.zendure_cloud_mqtt import ZendureCloudDiscovery
from tests.admin_auth_helpers import authenticate, request

pytestmark = [
    pytest.mark.admin,
    pytest.mark.setup,
    pytest.mark.maintenance,
    pytest.mark.contract,
]

WAIT_S = 5
FIRST = "refresh-first"
SECOND = "refresh-second"


def _api_key(account):
    return base64.b64encode(
        f"https://app.zendure.tech.APP-KEY-{account}".encode("ascii")
    ).decode("ascii")


ACCOUNT_A = _api_key("A")
ACCOUNT_B = _api_key("B")


def _start(name, call, results):
    def target():
        results[name] = call()

    thread = threading.Thread(target=target, name=name, daemon=True)
    thread.start()
    return thread


def _serials(candidates):
    return sorted(candidate.get("serial_number") for candidate in candidates)


class _CloudAccounts:
    """deviceList per API key: each account has its own device and client id."""

    def __init__(self):
        self.fetched_with = []

    def __call__(self, api_key, _timeout):
        self.fetched_with.append(api_key)
        account = "A" if api_key == ACCOUNT_A else "B"
        return {
            "devices": [
                {
                    "productKey": f"PK-{account}",
                    "deviceKey": f"DK-{account}",
                    "productModel": "SolarFlow 800 Pro 2",
                    "snNumber": f"SN-{account}",
                    "deviceName": f"Battery {account}",
                }
            ],
            "mqtt": {
                "host": "mqtt.example.invalid",
                "port": 8883,
                "username": f"user-{account}",
                "password": f"pw-{account}",
                "client_id": f"client-{account}",
            },
            "api_url": "https://app.zendure.tech",
            "app_key": f"APP-KEY-{account}",
        }


class _CloudBroker:
    """Records every session by client id; the first one holds until released."""

    def __init__(self):
        self.sessions = []
        self.open_sessions = 0
        self.most_open = 0
        self.first_listening = threading.Event()
        self.release_first = threading.Event()
        self._lock = threading.Lock()

    def listener(self, connection):
        return SimpleNamespace(
            listen=lambda timeout_s, on_message: self._listen(connection)
        )

    def _listen(self, connection):
        with self._lock:
            first = not self.sessions
            self.sessions.append(connection.client_id)
            self.open_sessions += 1
            self.most_open = max(self.most_open, self.open_sessions)
        try:
            if first:
                self.first_listening.set()
                self.release_first.wait(WAIT_S)
        finally:
            with self._lock:
                self.open_sessions -= 1


class _WatchedTokenStore(ZendureTokenStore):
    """Signals when the second refresh reads the saved API key."""

    def __init__(self, data_dir, second_reads):
        super().__init__(data_dir)
        self.second_arrived = threading.Event()
        self._second_reads = second_reads

    def load_token(self):
        if self._second_reads():
            self.second_arrived.set()
        return super().load_token()


def _second_thread():
    return threading.current_thread().name == SECOND


def _cloud(tmp_path, second_reads=_second_thread):
    broker = _CloudBroker()
    store = _WatchedTokenStore(tmp_path, second_reads)
    accounts = _CloudAccounts()
    discovery = ZendureCloudDiscovery(
        store=store, device_list_fetcher=accounts, listener_factory=broker.listener
    )
    return discovery, broker, store, accounts


def test_a_cloud_refresh_asked_for_while_one_listens_joins_it(tmp_path):
    """Interleaving: one refresh listens on the cloud broker when a second one,
    for the same account, reads the saved key.

    Before, the second refresh read the deviceList again and signed in with the
    account's client id while the first session was open, which a broker answers
    by dropping the older session. Now it joins the first: one deviceList read,
    one session, and both callers get the devices it found.
    """

    discovery, broker, store, accounts = _cloud(tmp_path)
    discovery.save_token(ACCOUNT_A)
    results = {}

    first = _start(FIRST, discovery.refresh, results)
    try:
        assert broker.first_listening.wait(WAIT_S)
        second = _start(SECOND, discovery.refresh, results)
        assert store.second_arrived.wait(WAIT_S)
    finally:
        broker.release_first.set()
    first.join(WAIT_S)
    second.join(WAIT_S)

    assert broker.sessions == ["client-A"]
    assert accounts.fetched_with == [ACCOUNT_A]
    assert _serials(results[FIRST]["candidates"]) == ["SN-A"]
    assert results[SECOND] == results[FIRST]


def test_a_cloud_refresh_for_another_account_waits_and_reads_its_own(tmp_path):
    """Interleaving: the API key is replaced while a refresh with the old key
    listens, and a refresh is asked for.

    The new refresh does not join the old one: it waits until that session has
    ended and then reads the new account, so it never answers with the old
    account's devices and the two sessions never overlap.
    """

    discovery, broker, store, accounts = _cloud(tmp_path)
    discovery.save_token(ACCOUNT_A)
    results = {}

    first = _start(FIRST, discovery.refresh, results)
    try:
        assert broker.first_listening.wait(WAIT_S)
        discovery.save_token(ACCOUNT_B)
        second = _start(SECOND, discovery.refresh, results)
        assert store.second_arrived.wait(WAIT_S)
    finally:
        broker.release_first.set()
    first.join(WAIT_S)
    second.join(WAIT_S)

    assert broker.sessions == ["client-A", "client-B"]
    assert broker.most_open == 1
    assert accounts.fetched_with == [ACCOUNT_A, ACCOUNT_B]
    assert _serials(results[FIRST]["candidates"]) == ["SN-A"]
    assert _serials(results[SECOND]["candidates"]) == ["SN-B"]
    assert _serials(discovery.candidates()) == ["SN-B"]


def test_a_cloud_refresh_that_outlives_its_deleted_key_keeps_none_of_its_devices(tmp_path):
    """Interleaving: a refresh with key A listens on the cloud broker when the
    saved key is deleted, and ends after it.

    Before, the refresh stored A's deviceList candidates once it ended, the
    trusted ones config-preview resolves selections against included, and they
    stayed until the next refresh. Now its result is dropped, since the key it
    read with is no longer the saved one.
    """

    discovery, broker, _store, _accounts = _cloud(tmp_path)
    discovery.save_token(ACCOUNT_A)
    results = {}

    first = _start(FIRST, discovery.refresh, results)
    try:
        assert broker.first_listening.wait(WAIT_S)
        discovery.delete_token()
    finally:
        broker.release_first.set()
    first.join(WAIT_S)

    assert not first.is_alive()
    assert discovery.candidates() == []
    assert discovery.trusted_candidates() == []


def test_a_cloud_refresh_that_outlives_its_replaced_key_keeps_none_of_its_devices(tmp_path):
    """Interleaving: a refresh with key A listens on the cloud broker when key B
    is saved, and ends after it.

    A's devices are not kept for B, and B's record does not take A's result:
    nothing has been read with B yet.
    """

    discovery, broker, _store, _accounts = _cloud(tmp_path)
    discovery.save_token(ACCOUNT_A)
    results = {}

    first = _start(FIRST, discovery.refresh, results)
    try:
        assert broker.first_listening.wait(WAIT_S)
        discovery.save_token(ACCOUNT_B)
    finally:
        broker.release_first.set()
    first.join(WAIT_S)

    assert not first.is_alive()
    assert discovery.candidates() == []
    assert discovery.trusted_candidates() == []
    settings = discovery.settings()
    assert settings["last_status"] is None, settings
    assert settings["last_device_count"] is None, settings


def test_a_device_list_seed_that_outlives_its_deleted_key_keeps_none_of_its_devices(tmp_path):
    """Interleaving: the lazy deviceList seed reads with key A when the saved
    key is deleted, and its read ends after it."""

    reading = threading.Event()
    release = threading.Event()
    accounts = _CloudAccounts()

    def fetch(api_key, timeout):
        reading.set()
        release.wait(WAIT_S)
        return accounts(api_key, timeout)

    discovery = ZendureCloudDiscovery(
        store=ZendureTokenStore(tmp_path),
        device_list_fetcher=fetch,
        listener_factory=_CloudBroker().listener,
    )
    discovery.save_token(ACCOUNT_A)
    results = {}

    seed = _start(FIRST, discovery.ensure_device_list_candidates, results)
    try:
        assert reading.wait(WAIT_S)
        discovery.delete_token()
    finally:
        release.set()
    seed.join(WAIT_S)

    assert accounts.fetched_with == [ACCOUNT_A]
    assert results[FIRST] == 0
    assert discovery.candidates() == []
    assert discovery.trusted_candidates() == []


def test_a_failed_cloud_refresh_that_outlives_its_replaced_key_leaves_the_new_record_alone(
    tmp_path,
):
    """Interleaving: a refresh reads the deviceList with key A when key B is
    saved, and the read fails after it.

    The failure is A's: B's record does not report it."""

    reading = threading.Event()
    release = threading.Event()

    def fetch(_api_key, _timeout):
        reading.set()
        release.wait(WAIT_S)
        raise ZendureCloudError("deviceList unavailable")

    discovery = ZendureCloudDiscovery(
        store=ZendureTokenStore(tmp_path),
        device_list_fetcher=fetch,
        listener_factory=_CloudBroker().listener,
    )
    discovery.save_token(ACCOUNT_A)
    results = {}

    first = _start(FIRST, discovery.refresh, results)
    try:
        assert reading.wait(WAIT_S)
        discovery.save_token(ACCOUNT_B)
    finally:
        release.set()
    first.join(WAIT_S)

    assert results[FIRST]["error"] == "device_list_failed"
    settings = discovery.settings()
    assert settings["last_status"] is None, settings
    assert settings["last_error"] is None, settings


def test_the_cached_devices_follow_the_saved_key(tmp_path):
    """Saving the same key again keeps its devices; another key or none drops them."""

    discovery, broker, _store, _accounts = _cloud(tmp_path)
    broker.release_first.set()
    discovery.save_token(ACCOUNT_A)
    assert discovery.refresh()["ok"] is True

    discovery.save_token(ACCOUNT_A)
    assert _serials(discovery.candidates()) == ["SN-A"]
    assert _serials(discovery.trusted_candidates()) == ["SN-A"]

    discovery.save_token(ACCOUNT_B)
    assert discovery.candidates() == []
    assert discovery.trusted_candidates() == []

    assert discovery.refresh()["ok"] is True
    assert _serials(discovery.trusted_candidates()) == ["SN-B"]
    discovery.delete_token()
    assert discovery.candidates() == []
    assert discovery.trusted_candidates() == []


class _SignallingLock:
    """A lock that sets ``contended`` when a caller has to wait for it."""

    def __init__(self, contended):
        self._lock = threading.Lock()
        self._contended = contended

    def __enter__(self):
        if not self._lock.acquire(blocking=False):
            self._contended.set()
            self._lock.acquire()
        return self

    def __exit__(self, *_exc):
        self._lock.release()


class _PausingStore(ZendureTokenStore):
    """Pauses the first thread at the next key read, or record write, once armed."""

    def __init__(self, data_dir, pause_at):
        super().__init__(data_dir)
        self.armed = False
        self.paused = threading.Event()
        self.go_on = threading.Event()
        self._pause_at = pause_at

    def _pause(self, step):
        if self.armed and step == self._pause_at and threading.current_thread().name == FIRST:
            self.armed = False
            self.paused.set()
            self.go_on.wait(WAIT_S)

    def load_token(self):
        token = super().load_token()
        self._pause("load_token")
        return token

    def _write_record(self, record):
        self._pause("write_record")
        return super()._write_record(record)


def _paused_cloud(tmp_path, pause_at):
    """A cloud discovery whose store pauses the first thread after its deviceList read."""

    store = _PausingStore(tmp_path, pause_at)
    broker = _CloudBroker()
    broker.release_first.set()
    accounts = _CloudAccounts()

    def fetch(api_key, timeout):
        result = accounts(api_key, timeout)
        if threading.current_thread().name == FIRST:
            store.armed = True
        return result

    discovery = ZendureCloudDiscovery(
        store=store, device_list_fetcher=fetch, listener_factory=broker.listener
    )
    contended = threading.Event()
    discovery._candidates_lock = _SignallingLock(contended)
    return discovery, store, contended


@pytest.mark.parametrize(
    "change",
    [
        lambda discovery: discovery.save_token(ACCOUNT_B),
        lambda discovery: discovery.delete_token(),
    ],
    ids=["replaced", "deleted"],
)
def test_a_key_changed_between_a_refreshs_key_check_and_its_store_drops_its_result(
    tmp_path, change
):
    """Interleaving: a refresh with key A has read the saved key to keep its
    result, and pauses there; key B is saved, or the key deleted, on another
    thread; the refresh then stores.

    The check and the store hold the lock the key change takes, so the change
    waits for the store and then drops it.
    """

    discovery, store, contended = _paused_cloud(tmp_path, "load_token")
    discovery.save_token(ACCOUNT_A)
    results = {}

    first = _start(FIRST, discovery.refresh, results)
    assert store.paused.wait(WAIT_S)

    def change_key():
        change(discovery)
        contended.set()

    second = _start(SECOND, change_key, results)
    try:
        assert contended.wait(WAIT_S)
    finally:
        store.go_on.set()
    first.join(WAIT_S)
    second.join(WAIT_S)

    assert not first.is_alive() and not second.is_alive()
    assert store.load_token() in (ACCOUNT_B, None)
    assert discovery.trusted_candidates() == [], _serials(discovery.trusted_candidates())
    assert discovery.candidates() == []


def test_a_credential_test_cannot_bring_a_deleted_key_back(tmp_path):
    """Interleaving: a credential test with the saved key A has read the record
    to write its status and pauses before the write; the key is deleted on
    another thread; the test then writes.

    The record write rewrites the whole record, token included. It holds the
    lock delete_token takes, so the delete waits for it and the key stays gone.
    """

    discovery, store, contended = _paused_cloud(tmp_path, "write_record")
    discovery.save_token(ACCOUNT_A)
    results = {}

    first = _start(FIRST, discovery.test, results)
    assert store.paused.wait(WAIT_S)

    def delete():
        discovery.delete_token()
        contended.set()

    second = _start(SECOND, delete, results)
    try:
        assert contended.wait(WAIT_S)
    finally:
        store.go_on.set()
    first.join(WAIT_S)
    second.join(WAIT_S)

    assert not first.is_alive() and not second.is_alive()
    assert results[FIRST]["ok"] is True
    assert store.load_token() is None
    assert discovery.settings()["token_saved"] is False


def test_a_validation_on_save_that_outlives_its_key_leaves_the_new_record_alone(tmp_path):
    """Interleaving: save_token(A, validate=True) reads the deviceList with A
    when key B is saved, and the read ends after it.

    The validation describes A: B's record does not take its status."""

    reading = threading.Event()
    release = threading.Event()
    accounts = _CloudAccounts()

    def fetch(api_key, timeout):
        if api_key == ACCOUNT_A:
            reading.set()
            release.wait(WAIT_S)
        return accounts(api_key, timeout)

    discovery = ZendureCloudDiscovery(
        store=ZendureTokenStore(tmp_path),
        device_list_fetcher=fetch,
        listener_factory=_CloudBroker().listener,
    )
    results = {}

    first = _start(FIRST, lambda: discovery.save_token(ACCOUNT_A, validate=True), results)
    try:
        assert reading.wait(WAIT_S)
        discovery.save_token(ACCOUNT_B)
    finally:
        release.set()
    first.join(WAIT_S)

    assert results[FIRST]["validation"]["ok"] is True
    assert discovery.store.load_token() == ACCOUNT_B
    settings = discovery.settings()
    assert settings["last_status"] is None, settings
    assert settings["last_device_count"] is None, settings
    assert settings["last_broker"] is None, settings


@pytest.mark.parametrize("found", [True, False], ids=["device-list-read", "device-list-failed"])
def test_a_credential_test_of_another_key_leaves_the_saved_keys_record_alone(tmp_path, found):
    accounts = _CloudAccounts()

    def fetch(api_key, timeout):
        if not found:
            raise ZendureCloudError("deviceList unavailable")
        return accounts(api_key, timeout)

    discovery = ZendureCloudDiscovery(
        store=ZendureTokenStore(tmp_path),
        device_list_fetcher=fetch,
        listener_factory=_CloudBroker().listener,
    )
    discovery.save_token(ACCOUNT_A)

    assert discovery.test(api_key=ACCOUNT_B)["ok"] is found
    settings = discovery.settings()
    assert settings["last_status"] is None, settings
    assert settings["last_error"] is None, settings
    assert settings["last_device_count"] is None, settings

    assert discovery.test()["ok"] is found
    settings = discovery.settings()
    assert settings["last_status"] == ("ok" if found else "error"), settings
    if found:
        assert settings["last_device_count"] == 1, settings
    else:
        assert settings["last_error"] == "deviceList unavailable", settings


class _BrokerNetwork:
    """One reachable broker; the first topic listen holds until released."""

    def __init__(self):
        self.listens = []
        self.first_listening = threading.Event()
        self.release_first = threading.Event()
        self.release_second = threading.Event()
        self.second_arrived = threading.Event()
        self._lock = threading.Lock()

    @staticmethod
    def connector(_host, _port, _timeout_s):
        return True

    def topic_discoverer(self, attempt_broker):
        caller = threading.current_thread().name
        with self._lock:
            first = not self.listens
            self.listens.append((caller, attempt_broker.get("username")))
        if first:
            self.first_listening.set()
            self.release_first.wait(WAIT_S)
        elif caller == SECOND:
            self.second_arrived.set()
            self.release_second.wait(WAIT_S)
        return {
            "status": "topics_seen",
            "devices": [
                {
                    "id": "mqtt-device:local_mqtt:broker:sn-local-1",
                    "serial_number": "sn-local-1",
                    "device_id": "local-device-1",
                    "display_name": "Local battery",
                    "source_type": "local_mqtt",
                }
            ],
        }

    def credential_refs(self, refs):
        def provider():
            if threading.current_thread().name == SECOND:
                self.second_arrived.set()
            return list(refs)

        return provider


POOL_SECRETS = {
    "pool": SimpleNamespace(label="pool", username="pool-user", password="pool-pw"),
    "added": SimpleNamespace(label="added", username="added-user", password="added-pw"),
}


def _local_mqtt(network, refs, secrets=POOL_SECRETS):
    discovery = MqttBrokerDiscovery(
        connector=network.connector,
        topic_discoverer=network.topic_discoverer,
        credential_lookup=secrets.get,
        credential_refs_provider=network.credential_refs(refs),
    )
    discovery.add_mdns_candidate(
        {
            "host": "192.168.1.60",
            "port": 1883,
            "source": "network_probe",
            "status": "tcp_open",
            "confidence": 0.6,
        }
    )
    return discovery


def _broker_devices(result):
    return [
        device["serial_number"]
        for broker in result["candidates"]
        for device in broker.get("devices") or []
    ]


def test_a_broker_refresh_asked_for_while_one_listens_joins_it():
    """Interleaving: one broker refresh listens on a broker when a second one,
    with the same broker list and credential pool, starts.

    Before, the second refresh opened a new generation, the first one's results
    were discarded as stale, and its caller got the broker with no devices while
    the second was still listening. Now the second joins the first: one listen
    per credential, and both callers get the broker's devices.
    """

    network = _BrokerNetwork()
    discovery = _local_mqtt(network, ["pool"])
    results = {}

    first = _start(FIRST, discovery.refresh, results)
    try:
        assert network.first_listening.wait(WAIT_S)
        second = _start(SECOND, discovery.refresh, results)
        assert network.second_arrived.wait(WAIT_S)
        network.release_first.set()
        first.join(WAIT_S)
    finally:
        network.release_first.set()
        network.release_second.set()
    second.join(WAIT_S)

    assert _broker_devices(results[FIRST]) == ["sn-local-1"]
    assert _broker_devices(results[SECOND]) == ["sn-local-1"]
    assert network.listens == [(FIRST, None), (FIRST, "pool-user")]


def test_a_broker_refresh_with_another_credential_pool_waits_and_uses_its_own():
    """Interleaving: a pooled credential is added while a refresh listens, and a
    refresh is asked for.

    The new refresh does not join the one that started without the credential:
    it runs after it, and its listens use the pool as it now stands.
    """

    network = _BrokerNetwork()
    refs = ["pool"]
    discovery = _local_mqtt(network, refs)
    results = {}

    first = _start(FIRST, discovery.refresh, results)
    try:
        assert network.first_listening.wait(WAIT_S)
        refs.append("added")
        second = _start(SECOND, discovery.refresh, results)
        assert network.second_arrived.wait(WAIT_S)
        network.release_first.set()
        first.join(WAIT_S)
    finally:
        network.release_first.set()
        network.release_second.set()
    second.join(WAIT_S)

    assert network.listens == [
        (FIRST, None),
        (FIRST, "pool-user"),
        (SECOND, None),
        (SECOND, "pool-user"),
        (SECOND, "added-user"),
    ]
    assert _broker_devices(results[SECOND]) == ["sn-local-1"]


class _Mdns:
    def refresh(self):
        return self.status()

    def devices(self):
        return []

    def ignored_devices(self):
        return []

    def status(self):
        return {"available": True, "enabled": True, "running": True, "state": "running_no_devices"}


def test_setups_run_joins_the_cloud_refresh_maintenance_started(
    tmp_path, monkeypatch, isolated_install_root
):
    """Interleaving: Maintenance's cloud refresh listens on the cloud broker when
    Setup's discovery run refreshes the cloud source.

    The two routes reach one cloud discovery service with one saved key, so the
    run joins the refresh instead of signing in under the account's client id a
    second time, and both answer with the account's device.
    """

    monkeypatch.setenv("EMS_ADMIN_DATA_DIR", str(tmp_path))
    broker = _CloudBroker()
    store = _WatchedTokenStore(tmp_path, lambda: broker.first_listening.is_set())
    accounts = _CloudAccounts()
    cloud = ZendureCloudDiscovery(
        store=store, device_list_fetcher=accounts, listener_factory=broker.listener
    )
    cloud.save_token(ACCOUNT_A)
    runtime = create_admin_runtime(
        registry=ScanRegistry(),
        mdns_provider=_Mdns(),
        mqtt_discovery=MqttBrokerDiscovery(connector=_BrokerNetwork.connector),
        zendure_cloud_discovery=cloud,
        discovery_preparation=DiscoveryConnectionsStore(),
    )
    srv = create_server("127.0.0.1", 0, runtime=runtime)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    authenticate(base)
    responses = {}
    try:
        maintenance = _start(
            FIRST,
            lambda: request(
                f"{base}/api/discovery/zendure-cloud-mqtt/refresh", method="POST"
            ),
            responses,
        )
        try:
            assert broker.first_listening.wait(WAIT_S)
            setup = _start(
                SECOND,
                lambda: request(
                    f"{base}/api/discovery/run", method="POST", body={"refresh": True}
                ),
                responses,
            )
            assert store.second_arrived.wait(WAIT_S)
        finally:
            broker.release_first.set()
        maintenance.join(WAIT_S)
        setup.join(WAIT_S)
    finally:
        srv.shutdown()
        srv.server_close()

    assert broker.sessions == ["client-A"]
    assert accounts.fetched_with == [ACCOUNT_A]
    status, _, refreshed = responses[FIRST]
    assert status == 200
    assert _serials(refreshed["candidates"]) == ["SN-A"]
    status, _, run = responses[SECOND]
    assert status == 200
    assert run["refresh"]["sources"]["zendure_mqtt"]["ok"] is True
    assert "SN-A" in [device.get("serial_number") for device in run["devices"]]


def test_a_joined_caller_gets_the_failure_of_the_run_it_joined():
    """Interleaving: a caller joins a run that then fails.

    The joined caller is answered with that failure, not with a run of its own,
    and nothing of the failed run is kept: the next caller runs again.
    """

    flight = SingleFlight()
    running = threading.Event()
    release = threading.Event()
    second_read = threading.Event()
    runs = []

    def read():
        if threading.current_thread().name == SECOND:
            second_read.set()
        return "same", "inputs"

    def failing(_inputs):
        runs.append(threading.current_thread().name)
        running.set()
        release.wait(WAIT_S)
        raise RuntimeError("refresh failed")

    outcomes = {}

    def call():
        try:
            flight.run(read, failing)
        except RuntimeError as exc:
            outcomes[threading.current_thread().name] = str(exc)

    first = threading.Thread(target=call, name=FIRST, daemon=True)
    second = threading.Thread(target=call, name=SECOND, daemon=True)
    first.start()
    try:
        assert running.wait(WAIT_S)
        second.start()
        assert second_read.wait(WAIT_S)
    finally:
        release.set()
    first.join(WAIT_S)
    second.join(WAIT_S)

    assert runs == [FIRST]
    assert outcomes == {FIRST: "refresh failed", SECOND: "refresh failed"}
    assert flight.run(read, lambda inputs: inputs) == "inputs"
