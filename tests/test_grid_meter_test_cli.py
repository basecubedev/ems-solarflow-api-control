# SPDX-License-Identifier: AGPL-3.0-or-later
from types import SimpleNamespace

import emsctl
from ems import clients as clients_mod
from ems.health import CommHealth


class FakeMeterClient:
    provider = "Shelly"
    ip = "192.0.2.99"

    def __init__(self):
        self.health = CommHealth("Shelly", kind="read")
        self.calls = 0

    def get_power(self):
        self.calls += 1
        if self.calls == 1:
            self.health.record_failure(
                error=TimeoutError("Read timed out"),
                latency_ms=3000,
                stale_used=True,
            )
        else:
            self.health.record_success(latency_ms=40)
        return 0


def test_grid_meter_test_reports_latency_summary(monkeypatch, capsys):
    fake = FakeMeterClient()
    monkeypatch.setattr(
        clients_mod, "create_grid_meter_client", lambda config, session: fake
    )
    monkeypatch.setattr(clients_mod, "create_session", lambda: object())

    args = SimpleNamespace(action="test", duration=1, interval=0.0)
    rc = emsctl.handle_grid_meter_command(
        args, {"grid_meter": {"type": "shelly", "ip": "192.0.2.99"}}
    )

    out = capsys.readouterr().out
    assert "Grid meter read test: Shelly 192.0.2.99" in out
    assert "Duration: 1s" in out
    assert "Reads:" in out
    assert "OK:" in out
    assert "Failed:" in out
    assert "p95 latency:" in out
    # At least one read failed (first probe), so a non-zero exit is expected.
    assert rc == 1


def test_grid_meter_test_reports_missing_mqtt_value(monkeypatch, capsys):
    fake = FakeMeterClient()
    fake.provider = "MQTT"
    fake.ip = ""
    fake.endpoint = "mqtt.local:1883 meter/grid"
    fake.get_power = lambda: fake.health.record_failure(
        error="no MQTT message received yet",
        latency_ms=0,
        stale_used=True,
    ) or 0
    monkeypatch.setattr(
        clients_mod, "create_grid_meter_client", lambda config, session: fake
    )
    monkeypatch.setattr(clients_mod, "create_session", lambda: object())

    args = SimpleNamespace(action="test", duration=1, interval=0.0)
    rc = emsctl.handle_grid_meter_command(
        args, {"grid_meter": {"type": "mqtt", "host": "mqtt.local", "topic": "meter/grid"}}
    )

    out = capsys.readouterr().out
    assert "Grid meter read test: MQTT mqtt.local:1883 meter/grid" in out
    assert "Latest power: unavailable (no fresh MQTT value received)" in out
    assert rc == 1


def test_grid_meter_test_displays_zendure_smartmeter_d0(monkeypatch, capsys):
    fake = FakeMeterClient()
    fake.provider = "Zendure SmartMeter D0"
    fake.transport = "mqtt"
    fake.ip = ""
    fake.endpoint = "mqtt.local:1883 Zendure/sensor/SN/totalPower"
    fake.get_power = lambda: fake.health.record_failure(
        error="no MQTT message received yet",
        latency_ms=0,
        stale_used=True,
    ) or 0
    monkeypatch.setattr(
        clients_mod, "create_grid_meter_client", lambda config, session: fake
    )
    monkeypatch.setattr(clients_mod, "create_session", lambda: object())

    args = SimpleNamespace(action="test", duration=1, interval=0.0)
    rc = emsctl.handle_grid_meter_command(
        args,
        {
            "grid_meter": {
                "type": "zendure_smartmeter_d0",
                "mqtt": {
                    "host": "mqtt.local",
                    "topic": "Zendure/sensor/SN/totalPower",
                },
            }
        },
    )

    out = capsys.readouterr().out
    assert (
        "Grid meter read test: Zendure SmartMeter D0 "
        "mqtt.local:1883 Zendure/sensor/SN/totalPower"
    ) in out
    assert "Latest power: unavailable (no fresh MQTT value received)" in out
    assert rc == 1


def test_grid_meter_test_rejects_unknown_action():
    rc = emsctl.handle_grid_meter_command(
        SimpleNamespace(action="bogus"), {"grid_meter": {}}
    )
    assert rc == 2


class RecordingMeterClient:
    provider = "Zendure SmartMeter D0"
    transport = "mqtt"

    def __init__(self):
        self.health = CommHealth("Zendure SmartMeter D0", kind="read")

    def get_power(self):
        self.health.record_success(latency_ms=5)
        return -43

    endpoint = "10.0.0.9:8883 Zendure/sensor/SN/totalPower"

    def close(self):
        pass


def test_grid_meter_test_resolves_broker_ref_config(monkeypatch, capsys):
    captured = {}

    def _capture(config, session):
        captured["config"] = config
        return RecordingMeterClient()

    monkeypatch.setattr(clients_mod, "create_grid_meter_client", _capture)
    monkeypatch.setattr(clients_mod, "create_session", lambda: object())

    config = {
        "grid_meter": {
            "type": "zendure_smartmeter_d0",
            "mqtt": {
                "broker_ref": "local_mqtt",
                "topic": "Zendure/sensor/SN/totalPower",
                "payload_format": "number",
            },
        },
        "zendure_mqtt": {
            "enabled": True,
            "brokers": {
                "local_mqtt": {
                    "enabled": True,
                    "source": "local_mqtt",
                    "host": "10.0.0.9",
                    "port": 8883,
                    "tls": True,
                    "username": "user",
                    "password": "secret",
                }
            },
        },
    }
    args = SimpleNamespace(action="test", duration=1, interval=0.0)
    rc = emsctl.handle_grid_meter_command(args, config)

    resolved = captured["config"]["mqtt"]
    assert resolved["host"] == "10.0.0.9"
    assert resolved["port"] == 8883
    assert resolved["tls"] is True
    assert resolved["topic"] == "Zendure/sensor/SN/totalPower"
    out = capsys.readouterr().out
    # The endpoint is shown without exposing credentials.
    assert "secret" not in out
    assert rc == 0


def test_grid_meter_test_reports_unknown_broker_ref(monkeypatch, capsys):
    monkeypatch.setattr(clients_mod, "create_session", lambda: object())
    config = {
        "grid_meter": {
            "type": "zendure_smartmeter_d0",
            "mqtt": {
                "broker_ref": "missing",
                "topic": "Zendure/sensor/SN/totalPower",
            },
        },
        "zendure_mqtt": {"enabled": True, "brokers": {}},
    }
    args = SimpleNamespace(action="test", duration=1, interval=0.0)
    rc = emsctl.handle_grid_meter_command(args, config)
    assert rc == 2
