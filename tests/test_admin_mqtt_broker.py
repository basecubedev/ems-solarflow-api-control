# SPDX-License-Identifier: AGPL-3.0-or-later
"""The built-in broker as the Admin runs it: settings, files, accounts, switching.

Docker is a fake that records every call; ports and the clock are stubs. What a
real Mosquitto does with the rendered files is ``test_admin_mqtt_broker_mosquitto``.
"""

import base64
import hashlib
import json
import os
import stat

import pytest

from admin.credential_store import CredentialStore
from admin.deployment import DockerError
from admin.mqtt_broker import (
    BROKER_SERVICE,
    MAPPING_SERVICE,
    MOSQUITTO_IMAGE,
    Account,
    BrokerSettings,
    BuiltinBroker,
    credentials_ref_for,
    hash_password,
    parse_settings,
    render_acl,
    render_compose,
    render_mosquitto_conf,
)
from ems.zendure_mqtt.builtin_broker import BUILTIN_PROFILE_REF

pytestmark = [
    pytest.mark.unit,
    pytest.mark.admin,
    pytest.mark.mqtt,
]


class FakeCompose:
    def __init__(self, fail=None):
        self.calls = []
        self.states = {}
        self.fail = fail or {}

    def _maybe_fail(self, name):
        if name in self.fail:
            raise DockerError(self.fail[name], "failed")

    def up(self, workspace, profiles=(), services=(), force_recreate=False, on_line=None):
        self.calls.append(("up", tuple(services), force_recreate))
        self._maybe_fail("up")
        for service in services:
            self.states[service] = "running"

    def stop(self, workspace, services, profiles=(), on_line=None):
        self.calls.append(("stop", tuple(services)))
        self._maybe_fail("stop")
        for service in services:
            self.states[service] = "exited"

    def reload(self, workspace, service):
        self.calls.append(("reload", service))
        self._maybe_fail("reload")

    def ps(self, workspace):
        return [{"service": name, "state": state} for name, state in self.states.items()]


class Passwords:
    def __init__(self):
        self.count = 0

    def __call__(self):
        self.count += 1
        return f"generated-password-{self.count}"


@pytest.fixture
def install(tmp_path):
    (tmp_path / "config").mkdir()
    (tmp_path / "data").mkdir()
    return tmp_path


def _broker(install, *, config=None, compose=None, port_free=True, answers=True):
    if config is not None:
        (install / "config" / "config.json").write_text(json.dumps(config), encoding="utf-8")
    clock = {"now": 0.0}

    def sleep(seconds):
        clock["now"] += seconds

    return BuiltinBroker(
        install_root=install,
        config_dir=install / "config",
        data_dir=install / "data",
        credential_store=CredentialStore(install / "config"),
        compose=compose or FakeCompose(),
        admin_image="ghcr.io/basecubedev/ems-solarflow-admin:v1.2.3",
        runtime_identity=(1000, 1000),
        port_free=lambda port: port_free,
        port_answers=lambda port: answers,
        monotonic=lambda: clock["now"],
        sleep=sleep,
        password_factory=Passwords(),
    )


def _settings_file(install):
    return json.loads((install / "config" / "mqtt" / "broker.json").read_text(encoding="utf-8"))


def _write_settings(install, **changes):
    document = {
        "schema_version": 1,
        "enabled": False,
        "port": 1883,
        "anonymous": False,
        "accounts": [{"name": "devices", "credentials_ref": "mqtt-broker-devices"}],
        "grid_meter_account": None,
        **changes,
    }
    path = install / "config" / "mqtt" / "broker.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document), encoding="utf-8")


# --- the settings file ------------------------------------------------------


@pytest.mark.parametrize(
    ("changes", "code"),
    [
        ({"schema_version": 2}, "mqtt_broker_settings_invalid"),
        ({"enabled": "yes"}, "mqtt_broker_settings_invalid"),
        ({"accounts": {}}, "mqtt_broker_settings_invalid"),
        ({"accounts": ["devices"]}, "mqtt_broker_settings_invalid"),
        ({"accounts": [{"name": "devices", "bridge": "yes"}]}, "mqtt_broker_settings_invalid"),
        ({"port": 0}, "mqtt_broker_port_invalid"),
        ({"port": "1883"}, "mqtt_broker_port_invalid"),
        ({"accounts": []}, "mqtt_broker_no_access"),
        ({"accounts": [{"name": "my devices"}]}, "mqtt_broker_account_name_invalid"),
        ({"accounts": [{"name": "x" * 33}]}, "mqtt_broker_account_name_invalid"),
        ({"accounts": [{"name": "devices"}, {"name": "Devices"}]}, "mqtt_broker_account_duplicate"),
        ({"accounts": [{"name": "ems"}]}, "mqtt_broker_account_reserved"),
        ({"accounts": [{"name": "EMS-Admin"}]}, "mqtt_broker_account_reserved"),
        ({"accounts": [{"name": "a", "topic_prefix": "ems-solarflow"}]}, "mqtt_broker_topic_prefix_invalid"),
        ({"accounts": [{"name": "a", "topic_prefix": "iot/x"}]}, "mqtt_broker_topic_prefix_invalid"),
        ({"accounts": [{"name": "a", "topic_prefix": "a b"}]}, "mqtt_broker_topic_prefix_invalid"),
        ({"accounts": [{"name": "a", "topic_prefix": "shellies/#"}]}, "mqtt_broker_topic_prefix_invalid"),
        ({"accounts": [{"name": "a", "topic_prefix": ""}]}, "mqtt_broker_topic_prefix_invalid"),
        ({"grid_meter_account": "nobody"}, "mqtt_broker_account_unknown"),
    ],
)
def test_a_settings_document_that_breaks_a_rule_names_it(changes, code):
    document = {"schema_version": 1, "accounts": [{"name": "devices"}], **changes}

    settings, issues = parse_settings(document)

    assert settings is None
    assert code in [issue["code"] for issue in issues]


def test_anonymous_alone_or_an_account_alone_is_enough():
    assert parse_settings({"schema_version": 1, "anonymous": True, "accounts": []})[1] == []
    settings, issues = parse_settings({"schema_version": 1, "accounts": [{"name": "devices"}]})
    assert issues == [] and settings.accounts == (Account("devices"),)
    assert settings.accounts[0].credentials_ref == "mqtt-broker-devices"


# --- the rendered files -------------------------------------------------------


def test_the_acl_protects_what_the_regulation_reads_in_every_mode():
    settings = BrokerSettings(
        enabled=True,
        anonymous=True,
        accounts=(Account("devices"), Account("zendure", zendure=True), Account("nodered", bridge=True)),
    )

    acl = render_acl(settings).split("\n")

    anonymous = acl[: acl.index("user devices")]
    assert anonymous[:2] == ["topic readwrite #", "topic deny ems-solarflow/#"]
    assert {"topic deny iot/#", "topic deny Zendure/#", "topic deny /#"} <= set(anonymous)
    devices = acl[acl.index("user devices") : acl.index("user zendure")]
    assert "topic deny ems-solarflow/#" in devices and "topic deny iot/#" in devices
    zendure = acl[acl.index("user zendure") : acl.index("user nodered")]
    assert "topic readwrite iot/#" in zendure and "topic deny iot/#" not in zendure
    assert "topic deny ems-solarflow/#" in zendure
    nodered = acl[acl.index("user nodered") : acl.index("user ems")]
    assert "topic readwrite ems-solarflow/#" in nodered and "topic deny ems-solarflow/#" not in nodered
    ems = acl[acl.index("user ems") : acl.index("user ems-mapping")]
    assert ems == [
        "user ems",
        "topic read ems-solarflow/#",
        "topic read iot/#",
        "topic read Zendure/#",
        "topic read /#",
        "topic write iot/+/+/properties/write",
        "",
    ]
    assert acl[acl.index("user ems-mapping") : acl.index("user ems-admin")] == [
        "user ems-mapping", "topic read #", "topic write ems-solarflow/#", "",
    ]
    assert "topic read $SYS/#" in acl[acl.index("user ems-admin") :]


def test_without_anonymous_access_no_line_comes_before_the_first_account():
    acl = render_acl(BrokerSettings(accounts=(Account("devices"),)))

    assert acl.startswith("user devices\n")


def test_a_prefixed_account_reaches_only_its_prefix():
    acl = render_acl(BrokerSettings(accounts=(Account("shelly", topic_prefix="shellies/balkon"),)))

    lines = acl.split("\n")
    block = lines[: lines.index("user ems")]
    assert "topic readwrite shellies/balkon/#" in block
    assert "topic readwrite #" not in block


def test_the_grid_meter_topic_belongs_to_its_account_alone():
    settings = BrokerSettings(
        anonymous=True,
        accounts=(Account("devices"), Account("meter", topic_prefix="shellies/em3")),
        grid_meter_account="meter",
    )
    topic = "shellies/em3/emeter/0/power"

    lines = render_acl(settings, grid_topic=topic).split("\n")

    assert lines.count(f"topic deny {topic}") == 2
    meter = lines[lines.index("user meter") : lines.index("user ems")]
    assert f"topic readwrite {topic}" in meter and f"topic deny {topic}" not in meter
    assert f"topic read {topic}" in lines[lines.index("user ems") :]


@pytest.mark.parametrize("topic", ["a b", "power/+", "power/#", "a\tb"])
def test_a_grid_meter_topic_the_acl_cannot_name_is_refused(topic):
    with pytest.raises(ValueError):
        render_acl(BrokerSettings(anonymous=True), grid_topic=topic)


def test_the_anonymous_switch_reaches_mosquitto():
    assert "allow_anonymous true" in render_mosquitto_conf(BrokerSettings(anonymous=True))
    assert "allow_anonymous false" in render_mosquitto_conf(BrokerSettings())


def test_a_password_is_written_as_mosquittos_pbkdf2_hash():
    line = hash_password("secret-password", salt=b"0123456789ab")

    _, kind, iterations, salt, digest = line.split("$")
    assert (kind, iterations) == ("7", "101")
    assert base64.b64decode(salt) == b"0123456789ab"
    assert base64.b64decode(digest) == hashlib.pbkdf2_hmac(
        "sha512", b"secret-password", b"0123456789ab", 101
    )
    assert "secret-password" not in line


def test_the_compose_project_pins_its_images_and_runs_as_the_install_user():
    text = render_compose(port=1884, admin_image="ghcr.io/basecubedev/ems-solarflow-admin:v1.2.3", puid=1000, pgid=1001)

    assert text.startswith("name: ems-mqtt\n")
    assert f"image: {MOSQUITTO_IMAGE}" in text and MOSQUITTO_IMAGE.count(":") == 1
    assert "image: ghcr.io/basecubedev/ems-solarflow-admin:v1.2.3" in text
    assert text.count('user: "1000:1001"') == 2
    assert 'ports: ["1884:1883"]' in text
    assert "../config/mqtt:/app/config/mqtt:ro" in text
    assert "./broker:/mosquitto/config:ro" in text and "./mapping:/app/mqtt/mapping:ro" in text
    assert "mosquitto.conf:/mosquitto" not in text
    assert "restart: unless-stopped" in text


# --- switching on and off -----------------------------------------------------


def test_the_first_switch_on_creates_the_shared_account_and_starts_the_broker(install):
    broker = _broker(install)

    result = broker.enable()

    assert result["ok"] and result["created_account"] == "devices"
    assert broker.compose.calls == [("up", (BROKER_SERVICE,), False)]
    settings = _settings_file(install)
    assert settings["enabled"] is True and settings["anonymous"] is False
    assert [account["name"] for account in settings["accounts"]] == ["devices"]
    assert broker.reveal_password("devices")
    workspace = install / "mqtt"
    for name in ("broker/acl", "broker/passwd", "mapping/mapping-credentials.json"):
        assert stat.S_IMODE(os.stat(workspace / name).st_mode) == 0o600
    passwd = (workspace / "broker" / "passwd").read_text()
    assert {line.split(":")[0] for line in passwd.splitlines()} == {"devices", "ems", "ems-mapping", "ems-admin"}
    assert broker.reveal_password("devices") not in passwd
    credentials = json.loads((workspace / "mapping" / "mapping-credentials.json").read_text())
    assert credentials["brokers"][BUILTIN_PROFILE_REF]["host"] == BROKER_SERVICE
    assert credentials["brokers"][BUILTIN_PROFILE_REF]["username"] == "ems-mapping"


def test_a_taken_port_keeps_the_broker_off_and_starts_nothing(install):
    broker = _broker(install, port_free=False)

    result = broker.enable()

    assert not result["ok"] and result["status"] == "mqtt_broker_port_in_use"
    assert broker.compose.calls == []
    assert not (install / "config" / "mqtt" / "broker.json").exists()


def test_a_broker_that_never_answers_is_stopped_again_and_stays_off(install):
    _write_settings(install)
    broker = _broker(install, answers=False)
    broker.set_password("devices", "a-long-password")

    result = broker.enable()

    assert result["status"] == "mqtt_broker_not_ready"
    assert broker.compose.calls == [("up", (BROKER_SERVICE,), False), ("stop", (BROKER_SERVICE,))]
    assert _settings_file(install)["enabled"] is False


def test_a_failed_pull_keeps_the_switch_off(install):
    broker = _broker(install, compose=FakeCompose(fail={"up": "docker_pull_failed"}))

    result = broker.enable()

    assert result["status"] == "docker_pull_failed"
    assert not (install / "config" / "mqtt" / "broker.json").exists()


def test_an_account_without_a_password_after_a_restore_blocks_the_start(install):
    _write_settings(install)
    broker = _broker(install)

    result = broker.enable()

    assert result["status"] == "render_failed"
    assert [issue["code"] for issue in result["issues"]] == ["mqtt_broker_account_password_missing"]
    assert broker.compose.calls == []


def test_stopping_is_always_possible_and_says_who_is_affected(install):
    config = {
        "devices": [
            {"name": "Balkon", "type": "external_mqtt", "mqtt": {"broker_ref": BUILTIN_PROFILE_REF}},
            {"name": "Keller", "type": "zendure_mqtt", "mqtt": {"broker_ref": BUILTIN_PROFILE_REF}},
            {"name": "Other", "type": "zendure_mqtt", "mqtt": {"broker_ref": "local"}},
        ],
        "zendure_mqtt": {"brokers": {BUILTIN_PROFILE_REF: {"source": "local_mqtt", "host": "192.0.2.10"}}},
    }
    broker = _broker(install, config=config)
    broker.enable()
    broker.compose.calls.clear()

    impact = broker.stop_impact({"devices": {"Keller": {"output_limit_w": 600}}})
    result = broker.stop()

    assert impact == {
        "external_devices": [{"name": "Balkon"}],
        "zendure_devices": [{"name": "Keller", "held_output_limit_w": 600}],
        "grid_meter": False,
    }
    assert result["ok"] and result["status"] == "stopped"
    assert broker.compose.calls == [("stop", (BROKER_SERVICE,))]
    assert _settings_file(install)["enabled"] is False
    assert broker.reveal_password("devices")


def test_a_new_port_recreates_the_broker_and_other_changes_reload_it(install):
    broker = _broker(install)
    broker.enable()
    broker.compose.calls.clear()

    assert broker.update({"anonymous": True})["ok"]
    assert broker.update({"port": 1884})["ok"]

    assert broker.compose.calls == [("reload", BROKER_SERVICE), ("up", (BROKER_SERVICE,), True)]
    assert 'ports: ["1884:1883"]' in (install / "mqtt" / "compose.yml").read_text()
    assert "allow_anonymous true" in (install / "mqtt" / "broker" / "mosquitto.conf").read_text()


def test_a_settings_change_while_off_is_only_saved(install):
    _write_settings(install)
    broker = _broker(install)

    assert broker.update({"anonymous": True})["ok"]

    assert broker.compose.calls == []
    assert _settings_file(install)["anonymous"] is True


def test_a_new_account_gets_a_password_and_a_removed_one_loses_it(install):
    broker = _broker(install)
    broker.enable()

    broker.update({"accounts": [{"name": "devices"}, {"name": "zendure", "zendure": True}]})
    assert broker.reveal_password("zendure")
    assert "user zendure" in (install / "mqtt" / "broker" / "acl").read_text()

    broker.update({"accounts": [{"name": "zendure", "zendure": True}]})
    assert broker.store.load_mqtt_broker_secret(credentials_ref_for("devices")) is None
    assert broker.reveal_password("devices") is None


def test_the_last_account_cannot_go_while_anonymous_access_is_off(install):
    broker = _broker(install)
    broker.enable()

    result = broker.update({"accounts": []})

    assert [issue["code"] for issue in result["issues"]] == ["mqtt_broker_no_access"]
    assert broker.reveal_password("devices")


def test_a_password_is_set_or_generated_and_can_be_shown_again(install):
    broker = _broker(install)
    broker.enable()
    broker.compose.calls.clear()

    assert broker.set_password("devices", "my-own-password")["password"] == "my-own-password"
    assert broker.reveal_password("devices") == "my-own-password"
    generated = broker.set_password("devices")["password"]
    assert broker.reveal_password("devices") == generated
    assert broker.compose.calls == [("reload", BROKER_SERVICE), ("reload", BROKER_SERVICE)]


@pytest.mark.parametrize("password", ["short", "x" * 129, "with:colon", "line\nbreak"])
def test_a_password_mosquitto_cannot_take_is_refused(install, password):
    broker = _broker(install)
    broker.enable()

    result = broker.set_password("devices", password)

    assert result["status"] == "mqtt_broker_account_password_invalid"
    assert broker.reveal_password("devices") != password


def test_an_unknown_account_has_no_password(install):
    broker = _broker(install)
    broker.enable()

    assert broker.set_password("nobody", "a-long-password")["status"] == "mqtt_broker_account_unknown"
    assert broker.reveal_password("nobody") is None


def test_a_hand_made_profile_with_the_reserved_name_is_reported(install):
    config = {"zendure_mqtt": {"brokers": {BUILTIN_PROFILE_REF: {"source": "local_mqtt", "host": "192.0.2.5"}}}}
    broker = _broker(install, config=config)

    assert [issue["code"] for issue in broker.view()["issues"]] == ["mqtt_broker_profile_name_reserved"]


def test_the_mapping_container_runs_for_mappings_on_another_broker_while_the_broker_is_off(install):
    config = {"zendure_mqtt": {"brokers": {"iobroker": {"source": "local_mqtt", "host": "192.0.2.20", "port": 1883}}}}
    mappings = {
        "schema_version": 1,
        "devices": [
            {
                "id": "akku",
                "broker_ref": "iobroker",
                "values": {"inverterPower": {"sources": [{"topic": "akku/ac", "format": "number"}]}},
            }
        ],
    }
    _write_settings(install)
    (install / "config" / "mqtt" / "mappings.json").write_text(json.dumps(mappings), encoding="utf-8")
    broker = _broker(install, config=config)
    broker.set_password("devices", "a-long-password")

    result = broker.sync_mapping_container()

    assert result.ok and result.status == "running"
    assert broker.compose.calls == [("up", (MAPPING_SERVICE,), False)]
    credentials = json.loads((install / "mqtt" / "mapping" / "mapping-credentials.json").read_text())
    assert credentials["brokers"]["iobroker"] == {"host": "192.0.2.20", "port": 1883, "tls": False, "tls_insecure": False}
    assert broker.mapping_broker_refs() == {BUILTIN_PROFILE_REF, "iobroker"}


def test_the_mapping_container_stops_when_no_mapping_is_left(install):
    broker = _broker(install)
    broker.enable()
    broker.compose.states[MAPPING_SERVICE] = "running"
    broker.compose.calls.clear()

    broker.sync_mapping_container()

    assert broker.compose.calls == [("stop", (MAPPING_SERVICE,))]


def test_a_grid_meter_on_the_broker_gets_its_topic_protected(install):
    config = {"grid_meter": {"type": "mqtt", "mqtt": {"broker_ref": BUILTIN_PROFILE_REF, "topic": "meter/power"}}}
    broker = _broker(install, config=config)
    broker.enable()

    acl = (install / "mqtt" / "broker" / "acl").read_text()

    assert "topic deny meter/power" in acl and "topic read meter/power" in acl
    assert broker.view()["grid_meter_topic"] == "meter/power"


def test_an_unprotectable_grid_meter_topic_keeps_the_broker_off(install):
    config = {"grid_meter": {"type": "mqtt", "mqtt": {"broker_ref": BUILTIN_PROFILE_REF, "topic": "meter/+/power"}}}
    broker = _broker(install, config=config)

    result = broker.enable()

    assert [issue["code"] for issue in result["issues"]] == ["mqtt_broker_grid_meter_topic_unprotectable"]
    assert broker.compose.calls == []


def test_the_view_holds_no_password(install):
    broker = _broker(install)
    broker.enable()

    view = broker.view()

    assert view["running"] is True and view["accounts"][0]["password_saved"] is True
    assert broker.reveal_password("devices") not in json.dumps(view)


def test_a_second_action_while_one_runs_is_refused_as_busy(install):
    broker = _broker(install)
    BuiltinBroker._lock.acquire()
    try:
        assert broker.enable()["status"] == "busy"
    finally:
        BuiltinBroker._lock.release()
