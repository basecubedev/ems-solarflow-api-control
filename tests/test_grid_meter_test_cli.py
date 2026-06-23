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


def test_grid_meter_test_rejects_unknown_action():
    rc = emsctl.handle_grid_meter_command(
        SimpleNamespace(action="bogus"), {"grid_meter": {}}
    )
    assert rc == 2
