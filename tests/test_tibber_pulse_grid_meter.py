# SPDX-FileCopyrightText: 2026 basecubedev
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The Tibber Pulse bridge as a grid meter: config, password, reader, cache."""

import base64
import functools
import json
import logging
import threading
from pathlib import Path

import pytest
import requests

import ems.config as cfg
from ems import clients, config_catalog, diagnostics, simulation, sml, tibber_pulse
from ems.mqtt_credentials import FileBridgePasswordResolver, MqttCredentialError
from ems.tibber_pulse import (
    CRC_RELEASE_AFTER,
    MAX_AGE_SECONDS,
    MAX_PLAUSIBLE_POWER_W,
    RETRY_CAP_SECONDS,
    WARNING_REPEAT_SECONDS,
    BridgeError,
    TibberPulseBridge,
    TibberPulseGridMeterClient,
)
from tests.test_sml import _entry, _frame, _get_list, _int

pytestmark = [pytest.mark.unit, pytest.mark.power_control]

TYPE = cfg.TIBBER_PULSE_GRID_METER_TYPE
FIXTURES = json.loads((Path(__file__).parent / "fixtures" / "sml" / "telegrams.json").read_text())
TELEGRAMS = {t["id"]: bytes.fromhex(t["frame"]) for t in FIXTURES["telegrams"] if "frame" in t}
ISKRA = TELEGRAMS["bridge-iskra"]
HOLLEY = TELEGRAMS["bridge-holley"]


def test_the_settings_fill_port_and_node_and_keep_the_reference():
    settings = cfg.tibber_pulse_grid_meter_settings({"type": TYPE, "ip": "192.0.2.7", "credentials_ref": "bridge-1"})

    assert settings == {"host": "192.0.2.7", "port": 80, "node_id": 1, "credentials_ref": "bridge-1"}


@pytest.mark.parametrize(
    "block, message",
    [
        ({"credentials_ref": "bridge"}, "requires grid_meter.ip"),
        ({"ip": "192.0.2.7"}, "requires grid_meter.credentials_ref"),
        ({"ip": "192.0.2.7", "credentials_ref": "Bridge One"}, "not a canonical reference"),
        ({"ip": "192.0.2.7", "credentials_ref": "bridge", "node_id": 0}, "grid_meter.node_id"),
        ({"ip": "192.0.2.7", "credentials_ref": "bridge", "node_id": 1.5}, "grid_meter.node_id"),
        ({"ip": "192.0.2.7", "credentials_ref": "bridge", "port": 70000}, "grid_meter.port"),
    ],
)
def test_invalid_settings_are_refused(block, message):
    with pytest.raises(ValueError, match=message):
        cfg.tibber_pulse_grid_meter_settings({"type": TYPE, **block})


def test_the_catalog_offers_the_type_with_its_own_fields():
    variant = config_catalog.GRID_METER_VARIANTS[TYPE]
    spec = config_catalog.grid_meter_variant_field_spec(TYPE)

    assert variant["fields"] == ("grid_meter.ip", "grid_meter.node_id", "grid_meter.credentials_ref")
    assert spec["keys"] == {"type", "ip", "port", "node_id", "credentials_ref"}
    assert TYPE in config_catalog.http_grid_meter_types()
    assert cfg.grid_meter_model_transport(TYPE) == ("Tibber Pulse IR bridge", "local HTTP API")


def test_a_missing_password_reference_keeps_the_install_in_no_write_mode():
    config = {
        "devices": [{"name": "WR1", "ip": "192.0.2.10", "sn": "HOA1"}],
        "grid_meter": {"type": TYPE, "ip": "192.0.2.7"},
    }

    assert "grid_meter.credentials_ref" in cfg.template_placeholder_paths(config)
    config["grid_meter"]["credentials_ref"] = "bridge"
    assert cfg.template_placeholder_paths(config) == []


def _write_record(secrets, ref, password, *, encrypted):
    record = {"version": 1, "ref": ref}
    if encrypted:
        from cryptography.fernet import Fernet

        key = Fernet.generate_key()
        (secrets / ".secret-key").write_bytes(key)
        record["password"] = Fernet(key).encrypt(password.encode()).decode("ascii")
    else:
        record["password"] = base64.b64encode(password.encode()).decode("ascii")
    record["password_encrypted"] = encrypted
    (secrets / f"bridge-{ref}.json").write_text(json.dumps(record))


@pytest.mark.parametrize("encrypted", [True, False])
def test_the_core_reads_the_bridge_password_from_the_secret_store(tmp_path, encrypted):
    _write_record(tmp_path, "bridge", "AB12-CD34", encrypted=encrypted)

    assert FileBridgePasswordResolver(tmp_path).resolve("bridge") == "AB12-CD34"


def test_a_missing_or_foreign_bridge_record_is_an_error_without_the_secret(tmp_path):
    resolver = FileBridgePasswordResolver(tmp_path)
    with pytest.raises(MqttCredentialError, match="was not found"):
        resolver.resolve("bridge")

    (tmp_path / "bridge-bridge.json").write_text(json.dumps({"ref": "other", "password": "eA==", "password_encrypted": False}))
    with pytest.raises(MqttCredentialError, match="malformed"):
        resolver.resolve("bridge")


def test_an_mqtt_record_is_not_a_bridge_password(tmp_path):
    (tmp_path / "mqtt-bridge.json").write_text(json.dumps({"ref": "bridge", "password": "eA==", "password_encrypted": False}))

    with pytest.raises(MqttCredentialError, match="was not found"):
        FileBridgePasswordResolver(tmp_path).resolve("bridge")


class _Response:
    def __init__(self, status, content=b""):
        self.status_code = status
        self.content = content


class _Session:
    def __init__(self, answers):
        self.answers = answers
        self.calls = []
        self.closed = False

    def get(self, url, *, params, auth, timeout):
        self.calls.append((url, params, auth))
        return self.answers[url.rsplit("/", 1)[1]]

    def close(self):
        self.closed = True


def test_the_bridge_is_read_with_basic_auth_and_the_node_number():
    session = _Session({"node_data.json": _Response(200, ISKRA)})
    bridge = TibberPulseBridge("192.0.2.7", "AB12-CD34", node_id=2, session=session)

    assert bridge.read_telegram() == ISKRA
    assert session.calls == [("http://192.0.2.7/node_data.json", {"node_id": 2}, ("admin", "AB12-CD34"))]


def test_older_firmware_falls_back_to_data_json_and_keeps_that_path():
    session = _Session({"node_data.json": _Response(404), "data.json": _Response(200, ISKRA)})
    bridge = TibberPulseBridge("192.0.2.7", "pw", port=8080, session=session)

    bridge.read_telegram()
    bridge.read_telegram()

    assert [call[0] for call in session.calls] == [
        "http://192.0.2.7:8080/node_data.json",
        "http://192.0.2.7:8080/data.json",
        "http://192.0.2.7:8080/data.json",
    ]


@pytest.mark.parametrize(
    "answers, reason",
    [
        ({"node_data.json": _Response(401)}, "unauthorized"),
        ({"node_data.json": _Response(500)}, "http_error"),
        ({"node_data.json": _Response(404), "data.json": _Response(404)}, "no_data_path"),
    ],
)
def test_a_refused_request_names_its_reason(answers, reason):
    bridge = TibberPulseBridge("192.0.2.7", "pw", session=_Session(answers))

    with pytest.raises(BridgeError) as raised:
        bridge.read_telegram()
    assert raised.value.reason == reason


class _OnePasswordSession(_Session):
    """A bridge that accepts one password and refuses every other with 401."""

    def __init__(self, accepted):
        super().__init__({"node_data.json": _Response(200, ISKRA)})
        self.accepted = accepted

    def get(self, url, *, params, auth, timeout):
        if auth[1] != self.accepted:
            self.calls.append((url, params, auth))
            return _Response(401)
        return super().get(url, params=params, auth=auth, timeout=timeout)


def test_a_password_saved_after_the_start_is_used_without_a_restart():
    saved = []

    def provider():
        if not saved:
            raise MqttCredentialError("Bridge password reference 'basement' was not found")
        return saved[0]

    session = _OnePasswordSession("AB12-CD34")
    bridge = TibberPulseBridge("192.0.2.7", session=session, password_provider=provider)

    with pytest.raises(BridgeError) as raised:
        bridge.read_telegram()
    assert raised.value.reason == "no_password" and session.calls == []
    saved.append("AB12-CD34")
    assert bridge.read_telegram() == ISKRA


def test_a_refused_password_is_read_again_from_the_store_on_the_next_request():
    saved = ["old-pass"]
    session = _OnePasswordSession("AB12-CD34")
    bridge = TibberPulseBridge("192.0.2.7", session=session, password_provider=lambda: saved[0])

    with pytest.raises(BridgeError, match="unauthorized"):
        bridge.read_telegram()
    saved[0] = "AB12-CD34"

    assert bridge.read_telegram() == ISKRA
    assert [call[2][1] for call in session.calls] == ["old-pass", "AB12-CD34"]


def test_a_password_given_directly_is_kept_after_a_refusal():
    session = _OnePasswordSession("AB12-CD34")
    bridge = TibberPulseBridge("192.0.2.7", "nope", session=session)

    for _ in range(2):
        with pytest.raises(BridgeError, match="unauthorized"):
            bridge.read_telegram()
    assert [call[2][1] for call in session.calls] == ["nope", "nope"]


class _Bridge:
    base_url = "http://192.0.2.7"
    node_id = 1

    def __init__(self, *telegrams):
        self.telegrams = list(telegrams)
        self.closed = False

    def read_telegram(self):
        item = self.telegrams.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def close(self):
        self.closed = True


class _Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def _client(*telegrams):
    """A client past its baseline frame, so the given telegrams are compared with it."""

    clock = _Clock()
    client = TibberPulseGridMeterClient(_Bridge(_power_frame(1), *telegrams), clock=clock, start=False)
    client.poll_once()
    return client, clock


def test_the_loop_gets_the_cached_reading_without_waiting_on_the_bridge():
    client, _ = _client(ISKRA)

    assert client.poll_once() is True
    assert client.get_power() == 130
    assert client.health.success_count == 1


def test_a_reading_older_than_the_maximum_age_counts_as_a_failure():
    client, clock = _client(ISKRA)
    client.poll_once()
    clock.now += MAX_AGE_SECONDS + 0.1

    assert client.get_power() == 130
    assert client.health.consecutive_failures == 1
    assert client.health.stale_used


def test_before_the_first_reading_the_meter_reports_a_failure():
    client, _ = _client()

    assert client.get_power() == 0
    assert client.health.consecutive_failures == 1


def test_the_same_meter_second_is_not_a_new_reading():
    client, clock = _client(ISKRA, ISKRA)
    client.poll_once()
    clock.now += MAX_AGE_SECONDS - 1

    assert client.poll_once() is False
    clock.now += 2
    client.get_power()
    assert client.health.consecutive_failures == 1


def test_a_changed_value_without_a_meter_clock_is_a_new_reading():
    first = _frame(_get_list(_entry(sml.POWER_TOTAL, _int(-400, 4))))
    second = _frame(_get_list(_entry(sml.POWER_TOTAL, _int(-380, 4))))
    client, _ = _client(first, second)
    client.poll_once()

    assert client.poll_once() is True
    assert client.get_power() == -380


def test_without_a_meter_clock_an_unchanged_telegram_is_not_a_new_reading():
    client, _ = _client(HOLLEY, HOLLEY)
    client.poll_once()

    assert client.poll_once() is False


@pytest.mark.parametrize(
    "failure, reason",
    [
        (TELEGRAMS["bridge-emh-corrupt"], "crc_mismatch"),
        (TELEGRAMS["bridge-truncated"], "incomplete_frame"),
        (BridgeError("unauthorized"), "unauthorized"),
        (requests.ConnectTimeout("bridge gone"), "ConnectTimeout"),
    ],
)
def test_a_failed_poll_keeps_the_last_good_reading_and_names_the_reason(failure, reason):
    client, clock = _client(ISKRA, failure)
    client.poll_once()

    assert client.poll_once() is False
    assert client.get_power() == 130
    clock.now += MAX_AGE_SECONDS + 1
    client.get_power()
    assert reason in client.health.last_error


def _read_warnings(caplog):
    return [r.getMessage() for r in caplog.records if "tibber_pulse_read_error" in r.getMessage()]


def test_a_lasting_failure_warns_once_a_minute_and_again_when_its_reason_changes(caplog):
    client, clock = _client(BridgeError("unauthorized"), BridgeError("no_password"))
    client.poll_once()

    with caplog.at_level(logging.WARNING):
        client.get_power()
        client.get_power()
        clock.now += WARNING_REPEAT_SECONDS
        client.get_power()
        client.poll_once()
        client.get_power()

    assert [message.split("reason=")[1].split()[0] for message in _read_warnings(caplog)] == [
        "unauthorized",
        "unauthorized",
        "no_password",
    ]
    assert client.health.consecutive_failures == 4


def test_an_implausible_power_is_never_cached(monkeypatch):
    client, _ = _client(ISKRA)
    monkeypatch.setattr(sml, "grid_power_w", lambda telegram: MAX_PLAUSIBLE_POWER_W + 1)

    assert client.poll_once() is False
    assert client.get_power() == 0


class _BlockingBridge(_Bridge):
    """Lets the test hold the worker inside a read and observe that it ran."""

    def __init__(self):
        super().__init__()
        self.reading = threading.Event()

    def read_telegram(self):
        self.reading.set()
        return ISKRA


def test_close_stops_the_worker_between_polls():
    """Interleaving: the worker finishes one poll and waits for its next; close
    must end that wait instead of letting the worker poll again."""
    bridge = _BlockingBridge()
    client = TibberPulseGridMeterClient(bridge, poll_interval=3600)
    assert bridge.reading.wait(5)

    client.close()

    assert not client._thread.is_alive()
    assert bridge.closed


def _client_without_worker(monkeypatch):
    """The factory's client with no background thread, so a test polls alone."""

    monkeypatch.setattr(tibber_pulse, "TibberPulseGridMeterClient", functools.partial(TibberPulseGridMeterClient, start=False))


def test_the_factory_builds_the_client_from_the_config_and_the_store(monkeypatch):
    class Resolver:
        def resolve(self, ref):
            assert ref == "bridge"
            return "AB12-CD34"

    seen = {}

    def read(self):
        seen["password"] = self._current_password()
        seen["url"] = self.base_url
        return ISKRA

    monkeypatch.setattr(TibberPulseBridge, "read_telegram", read)
    _client_without_worker(monkeypatch)
    client = clients.create_grid_meter_client(
        {"type": TYPE, "ip": "192.0.2.7", "node_id": 2, "credentials_ref": "bridge"},
        None,
        bridge_password_resolver=Resolver(),
    )
    try:
        assert isinstance(client, TibberPulseGridMeterClient)
        assert client.bridge.node_id == 2
        client.poll_once()
    finally:
        clients.close_grid_meter_client(client)
    assert seen == {"password": "AB12-CD34", "url": "http://192.0.2.7"}


@pytest.mark.parametrize(
    "block, resolver_error",
    [
        ({"credentials_ref": "basement"}, MqttCredentialError("Bridge password reference 'basement' was not found")),
        ({}, None),
    ],
)
def test_a_meter_without_a_usable_password_starts_and_fails_every_read_without_a_request(block, resolver_error, monkeypatch):
    class Resolver:
        def resolve(self, ref):
            raise resolver_error

    _client_without_worker(monkeypatch)
    client = clients.create_grid_meter_client(
        {"type": TYPE, "ip": "192.0.2.7", **block}, None, bridge_password_resolver=Resolver()
    )
    try:
        client.poll_once()
        assert client.get_power() == 0
        assert "no_password" in client.health.last_error
    finally:
        clients.close_grid_meter_client(client)


def test_a_structurally_invalid_meter_is_refused():
    with pytest.raises(ValueError, match="grid_meter.ip"):
        clients.create_grid_meter_client({"type": TYPE, "credentials_ref": "basement"}, None)


def _levels(checks):
    return {check["code"]: check["level"] for check in checks}


def test_diagnose_names_the_meter_experimental_and_checks_its_settings():
    checks = []
    diagnostics.diagnose_grid_meter_config(checks, {"grid_meter": {"type": TYPE, "ip": "192.0.2.7", "credentials_ref": "bridge"}})
    levels = _levels(checks)

    assert levels["grid_meter_tibber_pulse_configured"] == "ok"
    assert levels["grid_meter_tibber_pulse_experimental"] == "info"

    checks = []
    diagnostics.diagnose_grid_meter_config(checks, {"grid_meter": {"type": TYPE, "ip": "192.0.2.7"}})
    assert _levels(checks)["grid_meter_tibber_pulse_invalid"] == "error"


def test_the_hardware_probe_reads_one_telegram(monkeypatch):
    monkeypatch.setattr(FileBridgePasswordResolver, "resolve", lambda self, ref: "pw")
    monkeypatch.setattr(TibberPulseBridge, "read_telegram", lambda self: ISKRA)
    checks = []
    health = diagnostics.diagnose_hardware(checks, {"grid_meter": {"type": TYPE, "ip": "192.0.2.7", "credentials_ref": "bridge"}})

    probe = next(check for check in checks if check["code"] == "tibber_pulse_read_ok")
    assert probe["details"]["power_w"] == 130
    assert health["grid_meter"]["provider"] == "Tibber Pulse"


def test_the_hardware_probe_reports_a_damaged_telegram(monkeypatch):
    monkeypatch.setattr(FileBridgePasswordResolver, "resolve", lambda self, ref: "pw")
    monkeypatch.setattr(TibberPulseBridge, "read_telegram", lambda self: TELEGRAMS["bridge-emh-corrupt"])
    checks = []
    diagnostics.diagnose_hardware(checks, {"grid_meter": {"type": TYPE, "ip": "192.0.2.7", "credentials_ref": "bridge"}})

    assert _levels(checks)["tibber_pulse_read_failed"] == "warning"


def test_config_init_keeps_an_existing_tibber_meter_and_its_password_reference():
    from ems import config_init

    existing = {"type": TYPE, "ip": "192.0.2.7", "credentials_ref": "basement", "node_id": 2, "power_path": "stale"}

    result = config_init.ask_grid_meter(existing, noninteractive=True)

    assert result == {"type": TYPE, "ip": "192.0.2.7", "credentials_ref": "basement", "node_id": 2}
    assert TYPE in config_init.SUPPORTED_GRID_METER_TYPES


@pytest.mark.parametrize(
    "existing",
    [
        {"type": "shelly", "ip": "192.0.2.8", "credentials_ref": "basement", "node_id": 2},
        {"type": "tasmota_http", "ip": "192.0.2.9", "credentials_ref": "basement", "node_id": 2},
    ],
)
def test_config_init_drops_the_bridge_keys_when_the_meter_is_no_bridge(existing):
    from ems import config_init

    result = config_init.ask_grid_meter(existing, noninteractive=True)

    assert "credentials_ref" not in result and "node_id" not in result


def test_config_init_refuses_a_tibber_meter_without_a_password_reference():
    from ems import config_init

    with pytest.raises(config_init.ConfigInitError):
        config_init.ask_grid_meter({"type": TYPE, "ip": "192.0.2.7"}, noninteractive=True)


class _CrashThenTelegram(_Bridge):
    """Raises an unexpected error once, then serves a telegram."""

    def __init__(self):
        super().__init__()
        self.calls = 0
        self.served = threading.Event()

    def read_telegram(self):
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("unexpected")
        self.served.set()
        return ISKRA


def test_an_unexpected_error_does_not_end_the_worker():
    """Interleaving: the first poll raises an error nobody expects; the worker
    must log it and poll again rather than die with the meter frozen."""
    bridge = _CrashThenTelegram()
    client = TibberPulseGridMeterClient(bridge, poll_interval=0)
    try:
        assert bridge.served.wait(5)
    finally:
        client.close()
    assert bridge.calls >= 2


def _power_frame(watts, **frame):
    return _frame(_get_list(_entry(sml.POWER_TOTAL, _int(watts, 4))), **frame)


def test_three_different_intact_telegrams_pin_the_meters_crc_variant():
    client, _ = _client(*(_power_frame(w) for w in (100, 110, 120)))
    for _ in range(3):
        client.poll_once()

    assert client._crc_variants == (("x25", "big"),)


def test_a_telegram_the_bridge_repeats_counts_once_toward_the_pin():
    client, _ = _client(ISKRA, ISKRA, ISKRA)
    for _ in range(3):
        client.poll_once()

    assert client._crc_variants == sml.CRC_VARIANTS


def test_a_pin_every_later_telegram_misses_is_released(caplog):
    other = [_power_frame(w, crc=sml.crc16_kermit, byteorder="little") for w in range(200, 200 + CRC_RELEASE_AFTER + 1)]
    client, _ = _client(*(_power_frame(w) for w in (100, 110, 120)), *other)
    for _ in range(3):
        client.poll_once()

    with caplog.at_level(logging.WARNING):
        results = [client.poll_once() for _ in other]

    assert results == [False] * CRC_RELEASE_AFTER + [True]
    assert client.get_power() == 200 + CRC_RELEASE_AFTER
    assert any("tibber_pulse_crc_released" in r.getMessage() for r in caplog.records)


def test_a_reading_handed_out_every_cycle_adds_its_round_trip_once():
    client, clock = _client(ISKRA)
    client.poll_once()
    for _ in range(3):
        client.get_power()
        clock.now += 1

    assert client.health.success_count == 3
    assert len(client.health._latency_samples) == 1


def test_a_reading_that_aged_without_an_error_says_no_new_telegram_came():
    client, clock = _client(ISKRA, ISKRA)
    client.poll_once()
    clock.now += MAX_AGE_SECONDS + 1
    client.poll_once()

    client.get_power()
    assert client.health.last_error.startswith("no new telegram for")


def test_the_live_preflight_waits_for_the_first_bridge_reading(monkeypatch):
    monkeypatch.setattr(cfg, "hardware_writes_allowed", lambda: False)
    order = []

    class Meter:
        def wait_until_ready(self, timeout):
            order.append(("wait", timeout))
            return True

        def get_power(self):
            order.append(("read", None))
            return 130

    assert simulation.run_live_preflight([], Meter()) is True
    assert order == [("wait", clients.GRID_METER_READY_SECONDS), ("read", None)]


def test_the_live_preflight_stops_when_no_good_reading_came(monkeypatch):
    monkeypatch.setattr(cfg, "hardware_writes_allowed", lambda: False)
    client, _ = _client(BridgeError("no_password"))
    client.poll_once()
    monkeypatch.setattr(client, "wait_until_ready", lambda timeout: client._ready.is_set())

    assert simulation.run_live_preflight([], client) is False


@pytest.mark.parametrize("host", ["http://192.0.2.7", "192.0.2.7:80", "192.0.2.7/data.json", "admin@192.0.2.7", "192.0.2 .7", "192.0.2.7#x", "192.0.2.7?a"])
def test_an_address_with_scheme_port_or_path_is_refused(host):
    with pytest.raises(ValueError, match="grid_meter.ip"):
        cfg.tibber_pulse_grid_meter_settings({"type": TYPE, "ip": host, "credentials_ref": "basement"})


def test_the_hardware_probe_refuses_an_implausible_power(monkeypatch):
    monkeypatch.setattr(FileBridgePasswordResolver, "resolve", lambda self, ref: "pw")
    monkeypatch.setattr(TibberPulseBridge, "read_telegram", lambda self: ISKRA)
    monkeypatch.setattr(sml, "grid_power_w", lambda telegram: MAX_PLAUSIBLE_POWER_W + 1)
    checks = []
    diagnostics.diagnose_hardware(checks, {"grid_meter": {"type": TYPE, "ip": "192.0.2.7", "credentials_ref": "bridge"}})

    failed = next(check for check in checks if check["code"] == "tibber_pulse_read_failed")
    assert failed["details"]["reason"] == "implausible_power"


def test_a_preflight_meter_without_a_background_reader_keeps_its_old_check(monkeypatch):
    monkeypatch.setattr(cfg, "hardware_writes_allowed", lambda: False)

    class MqttLikeMeter:
        class health:
            consecutive_failures = 1
            last_error = "no MQTT message received yet"

        def get_power(self):
            return 0

    assert simulation.run_live_preflight([], MqttLikeMeter()) is True


def test_the_health_age_is_the_reading_s_age_not_the_last_hand_out():
    client, clock = _client(ISKRA)
    client.poll_once()
    clock.now += 4

    client.get_power()

    assert 4 <= client.health.age_seconds() < 5


def test_bytes_around_an_unchanged_frame_do_not_make_a_new_reading():
    client, _ = _client(ISKRA + b"tail-1", ISKRA + b"tail-2")
    client.poll_once()

    assert client.poll_once() is False


def test_a_login_that_cannot_work_is_tried_again_only_every_half_minute():
    client, _ = _client(BridgeError("unauthorized"), BridgeError("no_password"), BridgeError("http_error"))

    client.poll_once()
    assert client._pause() == RETRY_CAP_SECONDS
    client.poll_once()
    assert client._pause() == RETRY_CAP_SECONDS
    client.poll_once()
    assert client._pause() == client.poll_interval


def test_a_bridge_that_does_not_answer_gets_a_longer_pause_each_time():
    timeouts = [requests.ReadTimeout("hung")] * 5
    client, _ = _client(*timeouts, ISKRA)

    pauses = []
    for _ in timeouts:
        client.poll_once()
        pauses.append(client._pause())
    client.poll_once()

    assert pauses == [6.0, 12.0, 24.0, RETRY_CAP_SECONDS, RETRY_CAP_SECONDS]
    assert client._pause() == client.poll_interval


def test_ready_means_a_good_reading_not_just_a_finished_poll():
    client, _ = _client(TELEGRAMS["bridge-emh-corrupt"], ISKRA)

    client.poll_once()
    assert client.wait_until_ready(0) is False
    client.poll_once()
    assert client.wait_until_ready(0) is True


def test_a_password_an_http_login_cannot_carry_is_refused_without_a_request():
    session = _OnePasswordSession("AB12-CD34")
    bridge = TibberPulseBridge("192.0.2.7", "AB12-CD3€", session=session)

    with pytest.raises(BridgeError) as raised:
        bridge.read_telegram()
    assert raised.value.reason == "bad_password" and session.calls == []


def test_a_damaged_telegram_the_bridge_repeats_is_one_miss():
    damaged = _power_frame(300, crc=sml.crc16_kermit, byteorder="little")
    client, _ = _client(*(_power_frame(w) for w in (100, 110, 120)), *([damaged] * (CRC_RELEASE_AFTER + 2)))
    for _ in range(3 + CRC_RELEASE_AFTER + 2):
        client.poll_once()

    assert client._crc_variants == (("x25", "big"),)


def test_the_factory_holds_the_reading_to_the_configured_telemetry_age(monkeypatch):
    monkeypatch.setitem(cfg.OUTPUT_CONTROL_CONFIG, "telemetry_max_age_seconds", 4)
    _client_without_worker(monkeypatch)
    client = clients.create_grid_meter_client({"type": TYPE, "ip": "192.0.2.7", "credentials_ref": "bridge"}, None)
    try:
        assert client.max_age_seconds == 4
    finally:
        clients.close_grid_meter_client(client)


def test_the_hardware_probe_reads_again_after_one_damaged_telegram(monkeypatch):
    monkeypatch.setattr(FileBridgePasswordResolver, "resolve", lambda self, ref: "pw")
    answers = iter([TELEGRAMS["bridge-emh-corrupt"], ISKRA])
    monkeypatch.setattr(TibberPulseBridge, "read_telegram", lambda self: next(answers))
    checks = []
    diagnostics.diagnose_hardware(checks, {"grid_meter": {"type": TYPE, "ip": "192.0.2.7", "credentials_ref": "bridge"}})

    assert _levels(checks)["tibber_pulse_read_ok"] == "ok"


def test_config_init_drops_an_e3dc_port_when_the_meter_becomes_the_bridge(monkeypatch):
    from ems import config_init

    number = str([choice for choice, _ in config_init.GRID_METER_CHOICES].index(TYPE) + 1)
    answers = iter([number, "192.0.2.7", "basement", ""])
    monkeypatch.setattr("builtins.input", lambda _prompt="": next(answers))

    result = config_init.ask_grid_meter({"type": "e3dc_modbus", "ip": "192.0.2.7", "port": 502, "unit_id": 1})

    assert result == {"type": TYPE, "ip": "192.0.2.7", "credentials_ref": "basement", "node_id": 1}


class _GoneBridge(_Bridge):
    """A bridge that never answers again."""

    def read_telegram(self):
        raise requests.ConnectTimeout("bridge gone")


def test_a_bridge_gone_for_hours_keeps_a_bounded_pause():
    client = TibberPulseGridMeterClient(_GoneBridge(), clock=_Clock(), start=False)

    for _ in range(2000):
        client.poll_once()

    assert client._pause() == RETRY_CAP_SECONDS


def test_a_server_error_slows_the_polling_like_no_answer():
    client, _ = _client(BridgeError("http_error", "HTTP 503 from /node_data.json", 503))

    client.poll_once()

    assert client._pause() == 2 * client.poll_interval


def test_a_password_corrected_after_a_bad_character_is_read_again():
    saved = ["AB12-CD3\u20ac"]
    session = _OnePasswordSession("AB12-CD34")
    bridge = TibberPulseBridge("192.0.2.7", session=session, password_provider=lambda: saved[0])

    with pytest.raises(BridgeError, match="bad_password"):
        bridge.read_telegram()
    saved[0] = "AB12-CD34"

    assert bridge.read_telegram() == ISKRA


def test_the_factory_takes_the_age_limit_it_is_given(monkeypatch):
    _client_without_worker(monkeypatch)
    client = clients.create_grid_meter_client(
        {"type": TYPE, "ip": "192.0.2.7", "credentials_ref": "bridge"}, None, max_age_seconds=7
    )
    try:
        assert client.max_age_seconds == 7
    finally:
        clients.close_grid_meter_client(client)


def test_the_first_frame_is_only_the_baseline():
    client = TibberPulseGridMeterClient(_Bridge(ISKRA, ISKRA), clock=_Clock(), start=False)

    assert client.poll_once() is False
    assert client.poll_once() is False
    assert client.wait_until_ready(0) is False
    assert client.get_power() == 0


def test_the_hardware_probe_reads_once_for_a_telegram_that_is_whole_but_unusable(monkeypatch):
    monkeypatch.setattr(FileBridgePasswordResolver, "resolve", lambda self, ref: "pw")
    reads = []
    monkeypatch.setattr(TibberPulseBridge, "read_telegram", lambda self: reads.append(1) or TELEGRAMS["emh-no-power"])
    checks = []
    diagnostics.diagnose_hardware(checks, {"grid_meter": {"type": TYPE, "ip": "192.0.2.7", "credentials_ref": "bridge"}})

    assert _levels(checks)["tibber_pulse_read_failed"] == "warning"
    assert reads == [1]


def test_a_flapping_reading_still_warns_once_a_minute(caplog):
    client, clock = _client(*(_power_frame(w) for w in (200, 210, 220, 230, 240)))
    client.poll_once()

    with caplog.at_level(logging.WARNING):
        for _ in range(4):
            client.get_power()
            clock.now += MAX_AGE_SECONDS + 1
            client.get_power()
            client.poll_once()

    assert len(_read_warnings(caplog)) == 1
