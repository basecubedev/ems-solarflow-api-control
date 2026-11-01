# SPDX-License-Identifier: AGPL-3.0-or-later
"""The brokers a mapping can read: one list, a real login check, and what saving remembers.

The login is a fake that answers per account, discovery and the network list
are fakes too, so every test decides what each broker says. A real Mosquitto
answering ``paho_login`` is ``test_admin_mqtt_broker_mosquitto``.
"""

import json
import threading

import pytest

from admin.credential_store import CredentialStore
from admin.mqtt_broker import BROKER_SERVICE, BuiltinBroker, saved_broker_endpoints
from admin.mqtt_mapping_brokers import MappingBrokers, found_ref, seed_saved_brokers
from admin.mqtt_mapping_editor import MappingEditor
from ems.mqtt_credentials import validate_mqtt_credentials_ref
from ems.zendure_mqtt.builtin_broker import BUILTIN_PROFILE_REF
from mqtt_mapping.config import SAVED_BROKER_REF

pytestmark = [
    pytest.mark.unit,
    pytest.mark.admin,
    pytest.mark.mqtt,
]


class FakeCompose:
    def __init__(self):
        self.calls = []
        self.states = {}

    def up(self, workspace, profiles=(), services=(), force_recreate=False, on_line=None):
        self.calls.append(("up", tuple(services)))
        for service in services:
            self.states[service] = "running"

    def stop(self, workspace, services, profiles=(), on_line=None):
        self.calls.append(("stop", tuple(services)))

    def reload(self, workspace, service):
        self.calls.append(("reload", service))

    def ps(self, workspace):
        return [{"service": name, "state": state} for name, state in self.states.items()]


class FakeDiscovery:
    def __init__(self, candidates=()):
        self.list = [dict(item) for item in candidates]
        self.probed = []
        self.configured = []

    def candidates(self):
        return [dict(item) for item in self.list]

    def probe(self, cidr):
        self.probed.append(cidr)
        return {"cidr": cidr}

    def set_configured_brokers(self, brokers):
        self.configured.append(list(brokers))


class FakePreparation:
    def __init__(self, refs=()):
        self.refs = list(refs)

    def load(self):
        return {"local_mqtt": {"credential_refs": list(self.refs), "brokers": []}}

    def add_credential_ref(self, ref):
        if ref not in self.refs:
            self.refs.append(ref)


class Login:
    """Answers per ``(host, username)``; anything unlisted is unreachable."""

    def __init__(self, answers):
        self.answers = answers
        self.calls = []

    def __call__(self, endpoint):
        self.calls.append((endpoint["host"], endpoint.get("username")))
        return self.answers.get((endpoint["host"], endpoint.get("username")), "unreachable")


@pytest.fixture
def install(tmp_path):
    (tmp_path / "config" / "mqtt").mkdir(parents=True)
    (tmp_path / "data").mkdir()
    return tmp_path


def _broker(install, config=None, compose=None):
    (install / "config" / "config.json").write_text(json.dumps(config or {}), encoding="utf-8")
    return BuiltinBroker(
        install_root=install,
        config_dir=install / "config",
        data_dir=install / "data",
        credential_store=CredentialStore(install / "config"),
        compose=compose or FakeCompose(),
        admin_image="ghcr.io/basecubedev/ems-solarflow-admin:test",
        runtime_identity=(1000, 1000),
        port_free=lambda port: True,
        port_answers=lambda port: True,
        host_addresses=lambda: (),
    )


def _brokers(install, *, candidates=(), answers=None, refs=(), networks=None):
    store = CredentialStore(install / "config")
    discovery = FakeDiscovery(candidates)
    preparation = FakePreparation(refs)
    login = Login(answers or {})
    chooser = MappingBrokers(
        discovery=discovery,
        credential_store=store,
        preparation=preparation,
        login=login,
        networks=networks or (lambda: {"networks": []}),
        workers=1,
    )
    return chooser, discovery, preparation, login, store


def _kinds(choices):
    return [(item["kind"], item["ref"], item.get("access") or item.get("state"), item["usable"]) for item in choices["brokers"]]


def _mapping(broker_ref, device_id="balcony"):
    return {
        "id": device_id,
        "broker_ref": broker_ref,
        "values": {"inverterPower": {"sources": [{"topic": f"{device_id}/power", "format": "number"}]}},
    }


def test_a_found_broker_name_is_a_valid_mapping_and_credential_name():
    for host in ("192.168.1.7", "IOBroker.Fritz.Box", "a" * 200):
        ref = found_ref(host, 1883)
        assert SAVED_BROKER_REF.fullmatch(ref)
        assert validate_mqtt_credentials_ref(ref) == ref


def test_the_list_holds_the_built_in_broker_the_profiles_and_every_other_broker_discovery_knows(install):
    config = {"zendure_mqtt": {"brokers": {"iobroker": {"source": "local_mqtt", "host": "192.168.1.5", "port": 1883}}}}
    broker = _broker(install, config)
    chooser, *_ = _brokers(
        install,
        candidates=[
            {"host": "192.168.1.40", "port": 1883},
            {"host": "192.168.1.5", "port": 1883},
            {"host": "192.168.1.7", "port": 1883},
        ],
    )

    choices = chooser.choices(broker, address="192.168.1.40")

    assert _kinds(choices) == [
        ("builtin", BUILTIN_PROFILE_REF, "off", False),
        ("profile", "iobroker", "account", True),
        ("found", "broker-192-168-1-7-1883", "unchecked", False),
    ]


def test_a_running_built_in_broker_can_be_read(install):
    compose = FakeCompose()
    compose.states[BROKER_SERVICE] = "running"
    broker = _broker(install, compose=compose)
    (install / "config" / "mqtt" / "broker.json").write_text(
        json.dumps({"schema_version": 1, "enabled": True, "accounts": [{"name": "devices"}]}), encoding="utf-8"
    )
    (install / "mqtt").mkdir()
    (install / "mqtt" / "compose.yml").write_text("services: {}\n", encoding="utf-8")
    chooser, *_ = _brokers(install)

    assert _kinds(chooser.choices(broker, address="192.168.1.40")) == [("builtin", BUILTIN_PROFILE_REF, "running", True)]


@pytest.mark.parametrize(
    ("answers", "access", "credentials_ref"),
    [
        ({("192.168.1.7", None): "accepted"}, "anonymous", None),
        ({("192.168.1.7", None): "refused", ("192.168.1.7", "iob"): "accepted"}, "account", "home"),
        ({("192.168.1.7", None): "refused", ("192.168.1.7", "iob"): "refused"}, "needs_account", None),
        ({}, "unreachable", None),
    ],
    ids=["anonymous", "a-pool-account", "no-account-fits", "unreachable"],
)
def test_a_check_logs_in_anonymously_first_then_with_each_pool_account(install, answers, access, credentials_ref):
    chooser, _discovery, _prep, login, store = _brokers(
        install, candidates=[{"host": "192.168.1.7", "port": 1883}], answers=answers, refs=["home"]
    )
    store.save_mqtt_discovery_secret("home", "iob", "iob-password")
    broker = _broker(install)
    ref = found_ref("192.168.1.7", 1883)
    chooser.choices(broker)

    assert chooser.check(ref) == {"ok": True}

    item = next(entry for entry in chooser.choices(broker)["brokers"] if entry["ref"] == ref)
    assert item["access"] == access and item["usable"] == (access in ("anonymous", "account"))
    assert login.calls[0] == ("192.168.1.7", None)
    endpoint = chooser.endpoint(ref)
    if access == "account":
        assert endpoint["username"] == "iob" and chooser.usable()[ref]["credentials_ref"] == credentials_ref
    elif access == "anonymous":
        assert "username" not in endpoint
    else:
        assert endpoint is None and ref not in chooser.usable()


def test_an_account_the_broker_accepts_is_kept_for_the_mapping_and_for_find_devices(install):
    answers = {("192.168.1.7", None): "refused", ("192.168.1.7", "iob"): "refused"}
    chooser, _discovery, preparation, _login, store = _brokers(
        install, candidates=[{"host": "192.168.1.7", "port": 1883}], answers=answers
    )
    broker = _broker(install)
    ref = found_ref("192.168.1.7", 1883)
    chooser.choices(broker)

    refused = chooser.use_account(ref, "iob", "wrong")
    kept_after_refusal = store.load_mqtt_discovery_secret(ref)
    answers[("192.168.1.7", "iob")] = "accepted"
    accepted = chooser.use_account(ref, "iob", "iob-password")

    assert refused == {"ok": False, "error": "mqtt_mapping_broker_refused"} and kept_after_refusal is None
    assert accepted == {"ok": True, "ref": ref}
    assert store.load_mqtt_discovery_secret(ref).password == "iob-password"
    assert preparation.refs == [ref]
    assert chooser.endpoint(ref)["username"] == "iob"


@pytest.mark.parametrize(
    "change",
    [{"host": "not a host"}, {"port": 0}, {"port": True}, {"tls": "yes"}],
    ids=["host", "port-zero", "port-bool", "tls-text"],
)
def test_a_broker_added_by_hand_needs_an_address_and_a_port(install, change):
    chooser, *_ = _brokers(install)
    form = {"host": "192.168.2.20", "port": 1883, **change}

    result = chooser.add_manual(form.pop("host"), form.pop("port"), **form)

    assert result == {"ok": False, "error": "mqtt_mapping_broker_endpoint_invalid"}


def test_a_broker_added_by_hand_is_checked_at_once_and_listed_first(install):
    chooser, *_ = _brokers(
        install,
        candidates=[{"host": "192.168.1.3", "port": 1883}],
        answers={("192.168.2.20", None): "accepted"},
    )
    broker = _broker(install)

    result = chooser.add_manual("192.168.2.20", 1883)

    assert result == {"ok": True, "ref": "broker-192-168-2-20-1883"}
    found = [item for item in chooser.choices(broker)["brokers"] if item["kind"] == "found"]
    assert [(item["ref"], item["origin"], item["access"]) for item in found] == [
        ("broker-192-168-2-20-1883", "manual", "anonymous"),
        ("broker-192-168-1-3-1883", "discovery", "unchecked"),
    ]


@pytest.mark.parametrize(
    ("username", "password"),
    [("", "x"), ("user", "a\x00b"), ("a" * 300, "x"), (None, "x")],
    ids=["empty-user", "control-char", "too-long", "no-user"],
)
def test_an_account_is_a_user_name_and_a_password_without_control_characters(install, username, password):
    chooser, *_ = _brokers(install, candidates=[{"host": "192.168.1.7", "port": 1883}])
    chooser.choices(_broker(install))

    result = chooser.use_account(found_ref("192.168.1.7", 1883), username, password)

    assert result == {"ok": False, "error": "mqtt_mapping_broker_account_invalid"}


def test_a_search_probes_the_recommended_networks_and_checks_every_broker_it_finds(install):
    networks = {"networks": [{"cidr": "192.168.1.0/24", "scan_recommended": True}, {"cidr": "172.16.0.0/24", "scan_recommended": False}]}
    chooser, discovery, _prep, _login, _store = _brokers(
        install,
        candidates=[{"host": "192.168.1.7", "port": 1883}, {"host": "192.168.1.8", "port": 8883, "tls": True}],
        answers={("192.168.1.7", None): "accepted"},
        networks=lambda: networks,
    )
    broker = _broker(install)

    result = chooser.search(broker, address="192.168.1.40")

    assert result == {"ok": True, "networks": ["192.168.1.0/24"], "found": 2}
    assert discovery.probed == ["192.168.1.0/24"]
    found = {item["ref"]: item["access"] for item in chooser.choices(broker)["brokers"] if item["kind"] == "found"}
    assert found == {"broker-192-168-1-7-1883": "anonymous", "broker-192-168-1-8-8883": "unreachable"}


def test_a_second_search_while_one_runs_is_refused_not_queued(install):
    entered, release = threading.Event(), threading.Event()

    def login(_endpoint):
        entered.set()
        release.wait(5)
        return "accepted"

    chooser = MappingBrokers(
        discovery=FakeDiscovery([{"host": "192.168.1.7", "port": 1883}]),
        credential_store=CredentialStore(install / "config"),
        preparation=FakePreparation(),
        login=login,
        networks=lambda: {"networks": []},
        workers=1,
    )
    broker = _broker(install)
    first = {}
    worker = threading.Thread(target=lambda: first.update(chooser.search(broker)))
    worker.start()
    assert entered.wait(5)

    second = chooser.search(broker)
    searching = chooser.choices(broker)["searching"]
    release.set()
    worker.join(5)

    assert second == {"ok": False, "error": "mqtt_mapping_broker_search_busy"}
    assert searching is True and first["ok"] is True


def test_saving_a_mapping_on_a_found_broker_remembers_it_and_forgets_it_with_the_last_mapping(install):
    chooser, discovery, _prep, _login, _store = _brokers(
        install, candidates=[{"host": "192.168.1.7", "port": 1883}], answers={("192.168.1.7", None): "accepted"}
    )
    broker = _broker(install)
    ref = found_ref("192.168.1.7", 1883)
    chooser.choices(broker)
    chooser.check(ref)
    editor = MappingEditor(broker, found=chooser.usable)

    saved = editor.apply({"schema_version": 1, "devices": [_mapping(ref)], "brokers": {"stray": {"host": "x", "port": 1}}})
    written = json.loads((install / "config" / "mqtt" / "mappings.json").read_text(encoding="utf-8"))
    seed_saved_brokers(discovery, install / "config")
    endpoints = saved_broker_endpoints(install / "config", CredentialStore(install / "config"))
    refs = broker.mapping_broker_refs()
    removed = editor.apply({"schema_version": 1, "devices": []})
    after = json.loads((install / "config" / "mqtt" / "mappings.json").read_text(encoding="utf-8"))

    assert saved["ok"] and removed["ok"]
    assert written["brokers"] == {
        ref: {"host": "192.168.1.7", "port": 1883, "tls": False, "tls_insecure": False, "credentials_ref": None}
    }
    assert ref in refs
    assert endpoints == {ref: {"host": "192.168.1.7", "port": 1883, "tls": False, "tls_insecure": False}}
    assert discovery.configured[-1] == [
        {"id": "mqtt:192.168.1.7:1883", "host": "192.168.1.7", "port": 1883, "label": ref, "tls": False, "tls_insecure": False, "credentials_ref": None}
    ]
    assert "brokers" not in after


def test_a_remembered_broker_stays_readable_after_the_admin_restarts(install):
    store = CredentialStore(install / "config")
    store.save_mqtt_discovery_secret("broker-192-168-1-7-1883", "iob", "iob-password")
    ref = "broker-192-168-1-7-1883"
    (install / "config" / "mqtt" / "mappings.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "devices": [_mapping(ref)],
                "brokers": {ref: {"host": "192.168.1.7", "port": 1883, "tls": False, "tls_insecure": False, "credentials_ref": ref}},
            }
        ),
        encoding="utf-8",
    )
    broker = _broker(install)
    chooser, *_ = _brokers(install, candidates=[{"host": "192.168.1.7", "port": 1883}])
    settings, _issues, _exists = broker.load()

    assert broker.render(settings) == []

    credentials = json.loads((install / "mqtt" / "mapping" / "mapping-credentials.json").read_text(encoding="utf-8"))
    assert credentials["brokers"][ref]["username"] == "iob"
    assert _kinds(chooser.choices(broker)) == [("builtin", BUILTIN_PROFILE_REF, "off", False), ("saved", ref, "account", True)]
    assert MappingEditor(broker).apply(json.loads((install / "config" / "mqtt" / "mappings.json").read_text()))["ok"]
    rewritten = json.loads((install / "config" / "mqtt" / "mappings.json").read_text(encoding="utf-8"))
    assert rewritten["brokers"][ref]["credentials_ref"] == ref


def test_a_mapping_on_a_broker_nobody_found_is_refused(install):
    chooser, *_ = _brokers(install)
    editor = MappingEditor(_broker(install), found=chooser.usable)

    result = editor.apply({"schema_version": 1, "devices": [_mapping("broker-10-0-0-1-1883")]})

    assert not result["ok"] and [issue["code"] for issue in result["issues"]] == ["mqtt_mapping_broker_invalid"]


def test_a_broker_found_on_the_tls_port_is_checked_over_tls(install):
    seen = []

    def login(endpoint):
        seen.append(endpoint["tls"])
        return "accepted"

    chooser = MappingBrokers(
        discovery=FakeDiscovery([{"host": "192.168.1.8", "port": 8883, "transport": "tls"}]),
        credential_store=CredentialStore(install / "config"),
        preparation=FakePreparation(),
        login=login,
        networks=lambda: {"networks": []},
        workers=1,
    )
    broker = _broker(install)
    chooser.choices(broker)

    chooser.check(found_ref("192.168.1.8", 8883))

    assert seen == [True]


def test_a_broker_whose_certificate_does_not_verify_says_so(install):
    chooser, *_ = _brokers(install, candidates=[{"host": "192.168.1.8", "port": 8883, "tls": True}], answers={("192.168.1.8", None): "untrusted"})
    broker = _broker(install)
    chooser.choices(broker)
    ref = found_ref("192.168.1.8", 8883)

    chooser.check(ref)

    item = next(entry for entry in chooser.choices(broker)["brokers"] if entry["ref"] == ref)
    assert (item["access"], item["usable"]) == ("untrusted", False)


def test_the_built_in_broker_on_another_address_of_this_host_is_never_found(install):
    networks = {"networks": [{"address": "192.168.1.40", "scan_recommended": True}, {"address": "192.168.1.41", "scan_recommended": True}]}
    chooser, *_ = _brokers(
        install,
        candidates=[{"host": "192.168.1.41", "port": 1883}, {"host": "192.168.1.41", "port": 1884}],
        networks=lambda: networks,
    )

    found = [item["ref"] for item in chooser.choices(_broker(install), address="192.168.1.40")["brokers"] if item["kind"] == "found"]

    assert found == ["broker-192-168-1-41-1884"]


def test_an_account_needs_a_password_as_the_store_does(install):
    chooser, *_ = _brokers(install, answers={("192.168.2.20", "user"): "accepted"})

    result = chooser.add_manual("192.168.2.20", 1883, username="user", password="")

    assert result == {"ok": False, "error": "mqtt_mapping_broker_account_invalid"}


def _remember(install, ref, credentials_ref):
    (install / "config" / "mqtt" / "mappings.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "devices": [_mapping(ref)],
                "brokers": {ref: {"host": "192.168.1.7", "port": 1883, "tls": False, "tls_insecure": False, "credentials_ref": credentials_ref}},
            }
        ),
        encoding="utf-8",
    )


def _remembered_ref(install, ref):
    return json.loads((install / "config" / "mqtt" / "mappings.json").read_text(encoding="utf-8"))["brokers"][ref]["credentials_ref"]


def test_a_remembered_broker_that_lost_its_account_takes_a_new_one_and_the_mapping_reads_with_it(install):
    ref = found_ref("192.168.1.7", 1883)
    _remember(install, ref, ref)
    chooser, *_ = _brokers(install, answers={("192.168.1.7", "iob"): "accepted"})
    broker = _broker(install)
    before = _kinds(chooser.choices(broker))

    result = chooser.use_account(ref, "iob", "iob-password", broker)

    credentials = json.loads((install / "mqtt" / "mapping" / "mapping-credentials.json").read_text(encoding="utf-8"))
    assert before[-1] == ("saved", ref, "needs_account", False)
    assert result == {"ok": True, "ref": ref}
    assert _kinds(chooser.choices(broker))[-1] == ("saved", ref, "account", True)
    assert credentials["brokers"][ref]["username"] == "iob"


def test_a_remembered_anonymous_broker_that_now_wants_an_account_is_moved_to_a_pool_account_on_check(install):
    ref = found_ref("192.168.1.7", 1883)
    _remember(install, ref, None)
    chooser, _discovery, _prep, _login, store = _brokers(
        install, answers={("192.168.1.7", None): "refused", ("192.168.1.7", "iob"): "accepted"}, refs=["home"]
    )
    store.save_mqtt_discovery_secret("home", "iob", "iob-password")
    broker = _broker(install)

    assert chooser.check(ref, broker) == {"ok": True}

    assert _remembered_ref(install, ref) == "home"
    assert _kinds(chooser.choices(broker))[-1] == ("saved", ref, "account", True)


def test_a_restore_lets_find_devices_listen_on_the_restored_brokers(monkeypatch, install):
    from types import SimpleNamespace

    from admin import server as admin_server

    seeded = []
    broker = _broker(install)
    monkeypatch.setattr(admin_server, "builtin_broker_for_install", lambda *_args, **_kwargs: broker)
    monkeypatch.setattr(admin_server, "converge_after_restore", lambda _broker: ("ok", "done"))
    monkeypatch.setattr(admin_server, "seed_saved_brokers", lambda discovery, config_dir: seeded.append((discovery, config_dir)))
    discovery = FakeDiscovery()
    server = SimpleNamespace(credential_store=None, mqtt_compose=None, mqtt_discovery=discovery)

    assert admin_server.AdminServer.converge_builtin_broker(server) == ("ok", "done")
    assert seeded == [(discovery, broker.config_dir)]


def test_a_broker_found_and_saved_in_this_run_takes_a_new_account_as_a_remembered_one(install):
    answers = {("192.168.1.7", None): "accepted"}
    chooser, *_ = _brokers(install, candidates=[{"host": "192.168.1.7", "port": 1883}], answers=answers)
    broker = _broker(install)
    ref = found_ref("192.168.1.7", 1883)
    chooser.choices(broker)
    chooser.check(ref)
    assert MappingEditor(broker, found=chooser.usable).apply({"schema_version": 1, "devices": [_mapping(ref)]})["ok"]
    answers[("192.168.1.7", None)] = "refused"
    answers[("192.168.1.7", "user")] = "accepted"

    result = chooser.use_account(ref, "user", "secret-pw", broker)

    credentials = json.loads((install / "mqtt" / "mapping" / "mapping-credentials.json").read_text(encoding="utf-8"))
    assert result == {"ok": True, "ref": ref}
    assert _remembered_ref(install, ref) == ref
    assert credentials["brokers"][ref]["username"] == "user"


def test_a_checked_state_gives_way_when_the_account_behind_it_is_gone(install):
    ref = found_ref("192.168.1.7", 1883)
    _remember(install, ref, ref)
    chooser, _discovery, _prep, _login, store = _brokers(install, answers={("192.168.1.7", "iob"): "accepted"})
    broker = _broker(install)
    chooser.use_account(ref, "iob", "iob-password", broker)
    usable = _kinds(chooser.choices(broker))[-1]

    store.forget_mqtt_discovery_secret(ref)

    assert usable == ("saved", ref, "account", True)
    assert _kinds(chooser.choices(broker))[-1] == ("saved", ref, "needs_account", False)


def test_an_id_is_known_with_every_broker_that_has_it_configured_or_published(install):
    config = {
        "zendure_mqtt": {"brokers": {"home": {"source": "local_mqtt", "host": "192.168.1.5", "port": 1883}, "ems-mqtt": {"source": "local_mqtt", "host": "192.168.1.40", "port": 1883, "credentials_ref": "ems-mqtt"}}},
        "devices": [
            {"name": "Balkon", "type": "external_mqtt", "mqtt": {"broker_ref": "home", "topic_family": "ems_solarflow", "device_id": "balkon"}},
            {"name": "Terrasse", "type": "external_mqtt", "mqtt": {"broker_ref": "ems-mqtt", "topic_family": "ems_solarflow", "device_id": "terrasse"}},
            {"name": "Off", "type": "external_mqtt", "enabled": False, "mqtt": {"broker_ref": "home", "topic_family": "ems_solarflow", "device_id": "off"}},
        ],
    }
    candidates = [
        {"host": "192.168.1.7", "port": 1883, "devices": [{"topic_family": "ems_solarflow", "device_id": "balkon"}, {"topic_family": "zendure", "device_id": "HOA1"}]},
        {"host": "192.168.1.41", "port": 1883, "devices": [{"topic_family": "ems_solarflow", "device_id": "terrasse"}]},
    ]
    networks = {"networks": [{"address": "192.168.1.40"}, {"address": "192.168.1.41"}]}
    broker = _broker(install, config)
    chooser, *_ = _brokers(install, candidates=candidates, networks=lambda: networks)

    sources = chooser.external_sources(broker, address="192.168.1.40")

    assert sources == {
        "balkon": {"192.168.1.5:1883": {"configured"}, "192.168.1.7:1883": {"published"}},
        "terrasse": {"builtin": {"configured", "published"}},
    }
    assert chooser.elsewhere(broker, "balkon", "home", address="192.168.1.40") == "192.168.1.7:1883"
    assert chooser.elsewhere(broker, "terrasse", BUILTIN_PROFILE_REF, address="192.168.1.40") is None
    assert chooser.elsewhere(broker, "neu", BUILTIN_PROFILE_REF, address="192.168.1.40") is None
    assert chooser.taken_ids(broker, BUILTIN_PROFILE_REF, address="192.168.1.40") == ["balkon"]
    assert chooser.taken_ids(broker, "home", address="192.168.1.40") == ["balkon", "terrasse"]


def test_an_id_the_ems_only_has_through_another_broker_may_move_there(install):
    config = {
        "zendure_mqtt": {"brokers": {"ems-mqtt": {"source": "local_mqtt", "host": "192.168.1.40", "port": 1883, "credentials_ref": "ems-mqtt"}}},
        "devices": [{"name": "Balkon", "type": "external_mqtt", "mqtt": {"broker_ref": "ems-mqtt", "topic_family": "ems_solarflow", "device_id": "balkon"}}],
    }
    broker = _broker(install, config)
    chooser, *_ = _brokers(install, candidates=[{"host": "192.168.1.7", "port": 1883}], answers={("192.168.1.7", None): "accepted"})
    ref = found_ref("192.168.1.7", 1883)
    chooser.choices(broker)
    chooser.check(ref)

    assert chooser.elsewhere(broker, "balkon", ref, address="192.168.1.40") is None
    assert chooser.taken_ids(broker, ref, address="192.168.1.40") == ["balkon"]


def test_the_broker_an_ems_device_was_added_through_never_blocks_its_move_even_while_it_still_publishes(install):
    config = {
        "zendure_mqtt": {"brokers": {"ems-mqtt": {"source": "local_mqtt", "host": "192.168.1.40", "port": 1883, "credentials_ref": "ems-mqtt"}}},
        "devices": [{"name": "Balkon", "type": "external_mqtt", "mqtt": {"broker_ref": "ems-mqtt", "topic_family": "ems_solarflow", "device_id": "balkon"}}],
    }
    candidates = [
        {"host": "192.168.1.40", "port": 1883, "devices": [{"topic_family": "ems_solarflow", "device_id": "balkon"}]},
        {"host": "192.168.1.9", "port": 1883, "devices": [{"topic_family": "ems_solarflow", "device_id": "garten"}]},
    ]
    broker = _broker(install, config)
    chooser, *_ = _brokers(install, candidates=candidates, answers={("192.168.1.7", None): "accepted"})
    chooser.add_manual("192.168.1.7", 1883)
    ref = found_ref("192.168.1.7", 1883)

    assert chooser.elsewhere(broker, "balkon", ref, address="192.168.1.40") is None
    assert chooser.elsewhere(broker, "garten", ref, address="192.168.1.40") == "192.168.1.9:1883"


def test_a_saved_mapping_whose_id_turns_up_elsewhere_is_named_but_still_saves(install):
    broker = _broker(install)
    (install / "config" / "mqtt" / "mappings.json").write_text(
        json.dumps({"schema_version": 1, "devices": [_mapping(BUILTIN_PROFILE_REF, "balkon")]}), encoding="utf-8"
    )
    editor = MappingEditor(broker, elsewhere=lambda device_id, _ref: "192.168.1.7:1883" if device_id == "balkon" else None)

    view_codes = [issue["code"] for issue in editor.view()["issues"]]
    kept = editor.apply({"schema_version": 1, "devices": [_mapping(BUILTIN_PROFILE_REF, "balkon"), _mapping(BUILTIN_PROFILE_REF, "neu")]})
    added = editor.apply({"schema_version": 1, "devices": [_mapping(BUILTIN_PROFILE_REF, "balkon"), _mapping(BUILTIN_PROFILE_REF, "neu"), _mapping(BUILTIN_PROFILE_REF, "zweit")]})

    assert view_codes == ["mqtt_mapping_device_id_taken"]
    assert kept["ok"] and added["ok"]
