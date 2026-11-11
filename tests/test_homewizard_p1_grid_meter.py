# SPDX-FileCopyrightText: 2026 basecubedev
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The HomeWizard P1 Meter read over its local API v2.

Payloads follow HomeWizard's published API documentation; no test reaches a
real meter, and the API itself is a fake here (the TLS path has its own tests
in ``test_homewizard_api.py``). The sign is the meter's own: import positive,
export negative.
"""

import json
import logging

import pytest

from ems import config as cfg
from ems import diagnostics
from ems import homewizard
from ems.clients import (
    HOMEWIZARD_FROZEN_AFTER_S,
    HomeWizardP1Client,
    _parse_homewizard_p1_measurement,
    create_grid_meter_client,
)
from ems.config_catalog import (
    TEMPLATE_GRID_METER_HOST,
    grid_meter_port_carries_over,
    grid_meter_types,
    grid_meter_variant_field_spec,
    http_grid_meter_types,
)
from ems.template_addresses import template_address

pytestmark = [
    pytest.mark.power_control,
    pytest.mark.unit,
]

SERIAL = "0a1b2c3d4e5f"
TOKEN = "0123456789ABCDEF0123456789ABCDEF"
NAME = "local/ems_solarflow#3f9a1c"
PAIRED = {"type": "homewizard_p1", "ip": "192.0.2.40", "serial": SERIAL, "token": TOKEN, "pairing_name": NAME}
SINGLE_PHASE_EXPORT = {
    "protocol_version": 50,
    "meter_model": "ISKRA  2M550T-101",
    "unique_id": "00112233445566778899AABBCCDDEEFF",
    "timestamp": "2026-11-12T03:14:27",
    "energy_import_t1_kwh": 10830.511,
    "energy_import_t2_kwh": 2948.827,
    "energy_export_t1_kwh": 1285.951,
    "energy_export_t2_kwh": 2876.51,
    "power_w": -678,
    "power_l1_w": -676,
}
THREE_PHASE_IMPORT = {
    "protocol_version": 50,
    "timestamp": "2026-11-12T03:14:28",
    "power_w": 7100.278,
    "power_l1_w": 2366.759,
    "power_l2_w": 2366.759,
    "power_l3_w": 2366.76,
}


class FakeApi:
    """Answers queued measurements or errors, as ``HomeWizardP1Api`` would."""

    def __init__(self, *answers, host="192.0.2.40"):
        self.host = host
        self.answers = list(answers)
        self.closed = False

    def measurement(self):
        answer = self.answers.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        return answer

    def close(self):
        self.closed = True


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


# --- payload --------------------------------------------------------------------


@pytest.mark.parametrize(
    "payload, power, timestamp",
    [
        (SINGLE_PHASE_EXPORT, -678.0, "2026-11-12T03:14:27"),
        (THREE_PHASE_IMPORT, 7100.278, "2026-11-12T03:14:28"),
        ({"power_w": 0}, 0.0, None),
    ],
)
def test_the_total_power_and_the_meter_time_stamp_are_read(payload, power, timestamp):
    assert _parse_homewizard_p1_measurement(payload) == (power, timestamp)


@pytest.mark.parametrize(
    "payload",
    [
        {"power_l1_w": 120},
        {"power_w": "120"},
        {"power_w": None},
        {"power_w": True},
        {"active_power_w": 120},
        {"error": "user:unauthorized"},
        [],
        None,
    ],
)
def test_a_payload_without_a_numeric_total_is_refused(payload):
    with pytest.raises(ValueError, match="HomeWizard payload"):
        _parse_homewizard_p1_measurement(payload)


@pytest.mark.parametrize("literal", ["NaN", "Infinity", "-Infinity"])
def test_a_non_finite_total_is_refused(literal):
    with pytest.raises(ValueError):
        _parse_homewizard_p1_measurement(json.loads('{"power_w": %s}' % literal))


# --- client ---------------------------------------------------------------------


def test_a_failed_read_keeps_the_last_value_and_counts_as_stale(caplog):
    unreachable = homewizard.HomeWizardError(homewizard.REASON_UNREACHABLE, "cannot reach the P1 Meter")
    client = HomeWizardP1Client(FakeApi({**SINGLE_PHASE_EXPORT, "power_w": 250.04}, unreachable))
    assert client.get_power() == 250.0

    with caplog.at_level(logging.WARNING):
        assert client.get_power() == 250.0

    snapshot = client.health.snapshot()
    assert snapshot["failure_count"] == 1
    assert snapshot["stale_used"] is True
    assert "event=homewizard_p1_read_error" in caplog.text
    assert "reason=unreachable" in caplog.text
    assert "stale_value=250.0" in caplog.text


def test_a_time_stamp_that_stops_moving_makes_the_reading_fail():
    clock = Clock()
    still = {**SINGLE_PHASE_EXPORT, "power_w": 300}
    client = HomeWizardP1Client(FakeApi(still, still, still, {**still, "timestamp": "2026-11-12T03:15:00"}), clock=clock)

    assert client.get_power() == 300.0
    clock.now += HOMEWIZARD_FROZEN_AFTER_S
    assert client.get_power() == 300.0
    assert client.health.snapshot()["failure_count"] == 0

    clock.now += 1
    client.get_power()
    snapshot = client.health.snapshot()
    assert snapshot["failure_count"] == 1
    assert "no new reading for 31 s" in snapshot["last_error"]

    assert client.get_power() == 300.0
    assert client.health.snapshot()["consecutive_failures"] == 0


def test_a_meter_without_a_time_stamp_is_never_called_frozen():
    clock = Clock()
    answer = {"power_w": 120}
    client = HomeWizardP1Client(FakeApi(answer, answer), clock=clock)

    client.get_power()
    clock.now += 10 * HOMEWIZARD_FROZEN_AFTER_S
    client.get_power()

    assert client.health.snapshot()["failure_count"] == 0


def test_closing_the_client_closes_the_connection():
    api = FakeApi()
    HomeWizardP1Client(api).close()

    assert api.closed is True


def test_the_factory_builds_a_client_for_the_paired_meter():
    client = create_grid_meter_client(PAIRED, session=None)

    assert isinstance(client, HomeWizardP1Client)
    assert (client.api.host, client.api.port, client.api.serial, client.api.token) == ("192.0.2.40", 443, SERIAL, TOKEN)
    client.close()


@pytest.mark.parametrize(
    "grid_meter, message",
    [
        ({**PAIRED, "ip": ""}, "requires grid_meter.ip"),
        ({**PAIRED, "token": ""}, "not paired"),
        ({**PAIRED, "token": "abc"}, "not paired"),
        ({**PAIRED, "serial": "0a1b2c3d4e"}, "not paired"),
        ({**PAIRED, "port": 0}, "grid_meter.port"),
    ],
)
def test_an_unpaired_or_invalid_meter_is_refused_not_guessed(grid_meter, message):
    with pytest.raises(ValueError, match=message):
        cfg.homewizard_p1_grid_meter_settings(grid_meter)


@pytest.mark.parametrize("name", ["no-prefix", "local/" + "x" * 41, "my meter"])
def test_a_malformed_pairing_name_is_refused_by_the_editors_but_never_stops_reading(name):
    grid_meter = {**PAIRED, "pairing_name": name}

    with pytest.raises(ValueError, match="grid_meter.pairing_name"):
        cfg.check_homewizard_p1_pairing_name(grid_meter)
    assert cfg.homewizard_p1_grid_meter_settings(grid_meter)["token"] == TOKEN
    client = create_grid_meter_client(grid_meter, session=None)
    assert isinstance(client, HomeWizardP1Client)
    client.close()
    checks = []
    diagnostics.diagnose_grid_meter_config(checks, {"grid_meter": grid_meter})
    assert _codes(checks)["grid_meter_homewizard_p1_invalid"]["level"] == "error"


def test_config_init_treats_a_malformed_name_as_absent_and_keeps_the_pairing(monkeypatch):
    from ems import config_init

    assert config_init.ask_grid_meter({**PAIRED, "pairing_name": "my meter"}, noninteractive=True)["token"] == TOKEN


def test_a_meter_paired_before_names_were_stored_is_still_read():
    legacy = {key: value for key, value in PAIRED.items() if key != "pairing_name"}

    assert cfg.homewizard_p1_grid_meter_settings(legacy)["serial"] == SERIAL


def test_the_serial_is_compared_in_lower_case():
    assert cfg.homewizard_p1_grid_meter_settings({**PAIRED, "serial": SERIAL.upper()})["serial"] == SERIAL


# --- config, catalogue and template ----------------------------------------------


def test_the_type_is_a_catalogue_variant_reached_at_an_address():
    assert cfg.HOMEWIZARD_P1_GRID_METER_TYPE in grid_meter_types()
    assert cfg.HOMEWIZARD_P1_GRID_METER_TYPE in http_grid_meter_types()
    assert grid_meter_variant_field_spec("homewizard_p1")["keys"] == frozenset({"type", "ip", "port", "serial", "token", "pairing_name"})


def test_the_token_is_a_secret_catalogue_field():
    from ems.config_catalog import get_config_feature_field_index, is_secret_catalog_field

    fields = get_config_feature_field_index()
    assert is_secret_catalog_field(fields["grid_meter.token"])
    assert not is_secret_catalog_field(fields["grid_meter.serial"])


def test_model_and_transport_name_the_local_http_api():
    assert cfg.grid_meter_model_transport("homewizard_p1") == ("HomeWizard P1 Meter", "local HTTP API")


def test_a_port_carries_over_only_between_meters_on_the_same_port():
    assert grid_meter_port_carries_over("ecotracker", "homewizard_p1") is False
    assert grid_meter_port_carries_over("homewizard_p1", "homewizard_p1") is True


def test_a_template_placeholder_address_keeps_the_ems_in_no_write_mode():
    config = {
        "devices": [{"name": "WR1", "ip": "192.0.2.1", "sn": "SN1"}],
        "grid_meter": {**PAIRED, "ip": template_address(TEMPLATE_GRID_METER_HOST)},
    }
    assert "grid_meter.ip" in cfg.template_placeholder_paths(config)


# --- diagnostics ------------------------------------------------------------------


def _codes(checks):
    return {check["code"]: check for check in checks}


def test_diagnostics_names_the_same_provider_as_the_meter():
    assert diagnostics.GRID_METER_PROVIDERS["homewizard_p1"] == HomeWizardP1Client.provider


@pytest.mark.parametrize(
    "grid_meter, code, level",
    [
        (PAIRED, "grid_meter_homewizard_p1_paired", "ok"),
        ({**PAIRED, "token": ""}, "grid_meter_homewizard_p1_invalid", "error"),
        ({**PAIRED, "ip": ""}, "grid_meter_homewizard_p1_invalid", "error"),
    ],
)
def test_diagnose_config_requires_address_and_pairing(grid_meter, code, level):
    checks = []
    diagnostics.diagnose_grid_meter_config(checks, {"grid_meter": grid_meter})

    codes = _codes(checks)
    assert codes[code]["level"] == level
    assert codes["grid_meter_type"]["details"]["model"] == "HomeWizard P1 Meter"


def _diagnose_hardware(monkeypatch, answer, system=None, runtime=None, grid_meter=PAIRED):
    built = []

    class Api(FakeApi):
        def __init__(self, host, **kwargs):
            super().__init__(answer, host=host)
            built.append((host, kwargs))

    monkeypatch.setattr(homewizard, "HomeWizardP1Api", Api)
    checks = []
    config = {"grid_meter": grid_meter, "devices": []}
    if system is not None:
        config["system"] = system
    health = diagnostics.diagnose_hardware(checks, config, runtime)
    return _codes(checks), health, built


def test_diagnose_reads_the_paired_meter_once_and_records_the_read(monkeypatch):
    codes, health, built = _diagnose_hardware(monkeypatch, SINGLE_PHASE_EXPORT)

    assert built == [("192.0.2.40", {"serial": SERIAL, "token": TOKEN, "port": 443})]
    read = codes["homewizard_p1_read_ok"]["details"]
    assert (read["power_w"], read["protocol_version"], read["timestamp"]) == (-678.0, 50, "2026-11-12T03:14:27")
    assert TOKEN not in json.dumps(codes)
    assert "homewizard_p1_slow_meter" not in codes
    assert health["grid_meter"]["provider"] == "HomeWizard P1"
    assert health["grid_meter"]["success_count"] == 1


def test_diagnose_skips_an_unpaired_meter_and_says_how_to_pair(monkeypatch):
    codes, health, built = _diagnose_hardware(monkeypatch, SINGLE_PHASE_EXPORT, grid_meter={**PAIRED, "token": ""})

    assert built == []
    assert codes["homewizard_p1_unpaired"]["level"] == "warning"
    assert "emsctl.py config init" in codes["homewizard_p1_unpaired"]["hint"]
    assert health["grid_meter"]["failure_count"] == 1


@pytest.mark.parametrize(
    "reason, hint",
    [
        (homewizard.REASON_UNAUTHORIZED, "no longer accepts the token"),
        (homewizard.REASON_OTHER_METER, "Another P1 Meter now has this address"),
        (homewizard.REASON_NOT_A_P1_METER, "not a HomeWizard P1 Meter"),
        (homewizard.REASON_UNREACHABLE, None),
    ],
)
def test_diagnose_names_what_to_do_for_each_refusal(monkeypatch, reason, hint):
    codes, health, _built = _diagnose_hardware(monkeypatch, homewizard.HomeWizardError(reason, "refused"))

    failed = codes["homewizard_p1_read_failed"]
    assert failed["level"] == "warning"
    assert failed["details"]["reason"] == reason
    if hint is None:
        assert "hint" not in failed
    else:
        assert hint in failed["hint"]
    assert health["grid_meter"]["failure_count"] == 1


@pytest.mark.parametrize("system", [None, {"loop_interval": 10}])
def test_diagnose_warns_when_a_dsmr4_reading_reaches_two_control_cycles(monkeypatch, system):
    codes, _health, _built = _diagnose_hardware(monkeypatch, {**SINGLE_PHASE_EXPORT, "protocol_version": 42}, system)

    slow = codes["homewizard_p1_slow_meter"]
    assert slow["level"] == "warning"
    assert "DSMR 4.2" in slow["message"]
    assert "every 10 s" in slow["message"]
    assert "counts twice" in slow["message"]
    assert "loop-interval 11" in slow["hint"]
    assert "telemetry_max_age_seconds" in slow["hint"]


def test_diagnose_judges_the_loop_interval_the_ems_uses_not_the_one_in_the_file(monkeypatch):
    dsmr4 = {**SINGLE_PHASE_EXPORT, "protocol_version": 42}

    edited_by_hand, _health, _built = _diagnose_hardware(
        monkeypatch, dsmr4, {"loop_interval": 11}, {"system": {"loop_interval": 5}}
    )
    set_at_runtime, _health, _built = _diagnose_hardware(
        monkeypatch,
        dsmr4,
        {"loop_interval": 5, "output_control": {"telemetry_max_age_seconds": 24}},
        {"system": {"loop_interval": 12}},
    )

    assert edited_by_hand["homewizard_p1_slow_meter"]["level"] == "warning"
    assert "loop interval of 5 s" in edited_by_hand["homewizard_p1_slow_meter"]["message"]
    assert set_at_runtime["homewizard_p1_slow_meter"]["level"] == "info"


def test_diagnose_only_names_a_dsmr4_meter_when_loop_and_window_fit_it(monkeypatch):
    codes, _health, _built = _diagnose_hardware(
        monkeypatch,
        {**SINGLE_PHASE_EXPORT, "protocol_version": 40},
        {"loop_interval": 11, "output_control": {"telemetry_max_age_seconds": 22}},
    )

    slow = codes["homewizard_p1_slow_meter"]
    assert slow["level"] == "info"
    assert "DSMR 4 " in slow["message"]
    assert "reads each reading once" in slow["message"]
    assert "hint" not in slow


def test_diagnose_warns_when_the_telemetry_window_is_short_for_a_long_loop_set_at_runtime(monkeypatch):
    codes, _health, _built = _diagnose_hardware(
        monkeypatch,
        {**SINGLE_PHASE_EXPORT, "protocol_version": 42},
        {"loop_interval": 5},
        {"system": {"loop_interval": 11}},
    )

    slow = codes["homewizard_p1_slow_meter"]
    assert slow["level"] == "warning"
    assert "telemetry_max_age_seconds at 10 s" in slow["message"]
    assert "counts twice" not in slow["message"]
    assert "telemetry_max_age_seconds" in slow["hint"]


@pytest.mark.parametrize("telemetry_window, warned", [(10, True), (22, False)])
def test_the_recommended_loop_needs_the_recommended_telemetry_window(telemetry_window, warned):
    checks = []
    diagnostics.diagnose_config_plausibility(
        checks,
        None,
        {
            "system": {"loop_interval": 11, "output_control": {"telemetry_max_age_seconds": telemetry_window}},
            "energy_savings": {"enabled": True},
        },
    )

    window_warnings = [
        check["code"] for check in checks
        if check["code"].startswith("energy_telemetry_window") and check["level"] != "ok"
    ]
    assert window_warnings == (["energy_telemetry_window_below_loop_interval"] if warned else [])


# --- the CLI setup assistant ------------------------------------------------------


def _menu_number(config_init):
    return str([choice for choice, _ in config_init.GRID_METER_CHOICES].index("homewizard_p1") + 1)


def test_config_init_pairs_a_new_meter_and_drops_foreign_keys(monkeypatch):
    from ems import config_init

    paired = []
    answers = iter([_menu_number(config_init), "192.0.2.40"])
    monkeypatch.setattr("builtins.input", lambda _prompt="": next(answers))
    monkeypatch.setattr(
        config_init,
        "pair_homewizard_p1",
        lambda host, port=None, name=None: paired.append((host, port, name))
        or {"serial": SERIAL, "token": TOKEN, "pairing_name": NAME},
    )

    result = config_init.ask_grid_meter(
        {"type": "tasmota_http", "url": "http://192.0.2.9/cm", "power_path": "StatusSNS.Power", "port": 80}
    )

    assert paired == [("192.0.2.40", None, None)]
    assert result == {"type": "homewizard_p1", "ip": "192.0.2.40", "serial": SERIAL, "token": TOKEN, "pairing_name": NAME}
    assert config_init._meter_summary(result) == f"HomeWizard P1 Meter at 192.0.2.40, serial {SERIAL}"


@pytest.mark.parametrize("again, pairings", [("", 0), ("y", 1)])
def test_config_init_keeps_a_paired_meter_unless_asked_to_pair_again(monkeypatch, again, pairings):
    from ems import config_init

    paired = []
    answers = iter([_menu_number(config_init), "", again])
    monkeypatch.setattr("builtins.input", lambda _prompt="": next(answers))
    monkeypatch.setattr(
        config_init,
        "pair_homewizard_p1",
        lambda host, port=None, name=None: paired.append(name)
        or {"serial": "ffeeddccbbaa", "token": "F" * 32, "pairing_name": name},
    )

    result = config_init.ask_grid_meter(dict(PAIRED))

    assert paired == [NAME] * pairings
    assert result["serial"] == (SERIAL if pairings == 0 else "ffeeddccbbaa")
    assert result["pairing_name"] == NAME


def test_config_init_without_questions_keeps_a_pairing_and_refuses_none(monkeypatch):
    from ems import config_init

    assert config_init.ask_grid_meter(dict(PAIRED), noninteractive=True) == PAIRED
    with pytest.raises(config_init.ConfigInitError, match="not paired"):
        config_init.ask_grid_meter({**PAIRED, "token": ""}, noninteractive=True)


def test_switching_away_from_the_meter_drops_its_pairing(monkeypatch):
    from ems import config_init

    answers = iter(["1", "192.0.2.20"])
    monkeypatch.setattr("builtins.input", lambda _prompt="": next(answers))

    assert config_init.ask_grid_meter(dict(PAIRED)) == {"type": "shelly", "ip": "192.0.2.20"}


def test_pairing_keeps_the_installations_name_and_makes_one_up_only_when_there_is_none():
    first, second = homewizard.pairing_name(), homewizard.pairing_name(None)

    assert first != second
    for made in (first, second):
        assert made.startswith(homewizard.PAIRING_NAME_PREFIX) and len(made) == len(homewizard.PAIRING_NAME_PREFIX) + 6
    assert homewizard.pairing_name(NAME) == NAME
    assert homewizard.pairing_name(" cloud/someone ").startswith(homewizard.PAIRING_NAME_PREFIX)
    assert homewizard.pairing_name("local/" + "x" * 41).startswith(homewizard.PAIRING_NAME_PREFIX)


def test_config_init_pairs_under_the_stored_name_and_returns_it(monkeypatch):
    from ems import config_init

    asked = []

    class Api:
        connected_serial = SERIAL

        def __init__(self, host, **_kwargs):
            self.host = host

        def pair(self, name):
            asked.append(name)
            return TOKEN

        def close(self):
            return None

    monkeypatch.setattr(homewizard, "HomeWizardP1Api", Api)

    assert config_init.pair_homewizard_p1("192.0.2.40", name=NAME) == {"serial": SERIAL, "token": TOKEN, "pairing_name": NAME}
    fresh = config_init.pair_homewizard_p1("192.0.2.40")
    assert asked == [NAME, fresh["pairing_name"]]
    assert fresh["pairing_name"] != NAME


def test_config_init_pairing_failure_is_a_setup_error(monkeypatch):
    from ems import config_init

    class Api:
        connected_serial = None

        def __init__(self, host, **_kwargs):
            self.host = host

        def pair(self, name):
            raise homewizard.HomeWizardError(homewizard.REASON_UNREACHABLE, "cannot reach the P1 Meter")

        def close(self):
            return None

    monkeypatch.setattr(homewizard, "HomeWizardP1Api", Api)

    with pytest.raises(config_init.ConfigInitError, match="pairing failed: cannot reach"):
        config_init.pair_homewizard_p1("192.0.2.40")
