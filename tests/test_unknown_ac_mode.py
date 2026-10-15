# SPDX-License-Identifier: AGPL-3.0-or-later
"""A missing ``acMode`` is unknown, not 0 (K10, owner decision 2026-10-12).

``parse_device`` read a report without ``acMode`` as 0, and the per-cycle
default ``ac_output`` claim wrote ``acMode = 2`` over it on every cycle: the
reconciler skipped an unknown mode except 0, and only for the startup path,
which nothing called. ``acMode`` is now written only from a known mode (1 or
2) or for a role the operator set; the default claim over an unknown mode
writes nothing and says so once. Evidence: 14 days of raw samples from two
800 Pro 2 carry ``acMode`` only as 1 or 2, so a device that reports it keeps
working as before.
"""

import json
import logging
import shutil
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from ems import config as cfg
from ems.clients import ZendureClient, parse_device, zero_device_state
from ems.controller import EMSController
from ems.runtime_intents import ac_input_intent, ac_output_intent
from ems.simulation import state_from_trace_device
from test_write_gates import RuntimeStateStub, ShellyStub, device, state

pytestmark = [
    pytest.mark.unit,
    pytest.mark.simulation,
    pytest.mark.power_control,
]

REPORT = {
    "electricLevel": 60,
    "minSoc": 150,
    "socSet": 1000,
    "solarInputPower": 500,
    "outputLimit": 200,
    "packNum": 1,
    "acStatus": 1,
    "dcStatus": 1,
    "packState": 2,
    "smartMode": 1,
}


def test_a_report_without_ac_mode_says_so():
    assert parse_device({"properties": {**REPORT, "acMode": 2}}).ac_mode_reported is True
    assert parse_device({"properties": {**REPORT, "acMode": 0}}).ac_mode_reported is True
    assert parse_device({"properties": REPORT}).ac_mode_reported is False
    assert parse_device({"properties": {**REPORT, "acMode": None}}).ac_mode_reported is False


def test_states_built_without_a_report_carry_no_ac_mode():
    assert zero_device_state().ac_mode_reported is False
    assert state_from_trace_device({"soc": 50}).ac_mode_reported is False
    assert state_from_trace_device({"soc": 50, "acMode": 1}).ac_mode_reported is True
    assert state_from_trace_device({"soc": 50, "ac_mode": 2}).ac_mode_reported is True


def _controller(runtime_state=None):
    controlled = device("WR1")
    controlled.session.post.return_value = SimpleNamespace(status_code=200)
    controller = EMSController(
        devices=[controlled],
        shelly=ShellyStub(0),
        sleep_enabled=False,
        runtime_state=runtime_state or RuntimeStateStub(),
    )
    return controller, controlled


def _written(controlled):
    return [call.kwargs["json"]["properties"] for call in controlled.session.post.call_args_list]


def _unreported():
    return parse_device({"properties": REPORT})


@pytest.mark.parametrize(
    "current",
    [_unreported(), state(ac_mode=0), state(ac_mode=3)],
    ids=["missing", "zero", "unsupported"],
)
def test_the_default_claim_writes_nothing_over_an_unknown_mode(current):
    controller, controlled = _controller()

    with patch("ems.controller.cfg.state_reconciliation_writes_allowed", return_value=True):
        ok = controller.reconcile_ac_mode_intent(controlled, current, ac_output_intent("WR1"))

    assert ok is False
    assert _written(controlled) == []


def test_an_unknown_mode_is_said_once_until_it_is_known_again(caplog):
    controller, controlled = _controller()

    with patch(
        "ems.controller.cfg.state_reconciliation_writes_allowed", return_value=True
    ), caplog.at_level(logging.DEBUG):
        for current in (_unreported(), _unreported(), _unreported(), state(ac_mode=2), _unreported()):
            controller.reconcile_ac_mode_intent(controlled, current, ac_output_intent("WR1"))

    said = [record for record in caplog.records if "event=unknown_ac_mode" in record.getMessage()]
    assert [record.levelno for record in said] == [logging.WARNING, logging.WARNING]
    assert "current_ac_mode=None" in said[0].getMessage()
    assert _written(controlled) == []


@pytest.mark.parametrize(("role", "written"), [("ac_output", 2), ("ac_input", 1)])
def test_a_role_the_operator_set_is_written_over_an_unknown_mode(role, written):
    controller, controlled = _controller(
        RuntimeStateStub(devices={"WR1": {"runtime_role": role, "runtime_role_reason": "emsctl"}})
    )
    intent = controller.get_device_runtime_intent(controlled, _unreported())

    with patch("ems.controller.cfg.state_reconciliation_writes_allowed", return_value=True):
        controller.reconcile_ac_mode_intent(controlled, _unreported(), intent)

    assert intent.operator_role is True
    assert _written(controlled) == [{"acMode": written}]


def test_the_default_role_is_not_the_operator_s():
    controller, controlled = _controller()

    assert controller.get_device_runtime_intent(controlled, _unreported()).operator_role is False


def test_an_invalid_stored_role_is_not_the_operator_s():
    controller, controlled = _controller(
        RuntimeStateStub(devices={"WR1": {"runtime_role": "sideways"}})
    )

    assert controller.get_device_runtime_intent(controlled, _unreported()).operator_role is False


def test_the_full_charge_assist_writes_no_mode_over_an_unknown_one():
    """The assist is the EMS's own claim, not a role the operator set."""

    controller, controlled = _controller()

    with patch("ems.controller.cfg.state_reconciliation_writes_allowed", return_value=True):
        ok = controller.reconcile_ac_mode_intent(
            controlled,
            _unreported(),
            ac_input_intent("WR1", "battery_full_charge_assist", setpoint_w=600, priority=200),
        )

    assert ok is False
    assert _written(controlled) == []


def test_a_known_mode_is_still_brought_back_to_output():
    controller, controlled = _controller()

    with patch("ems.controller.cfg.state_reconciliation_writes_allowed", return_value=True):
        controller.reconcile_ac_mode_intent(controlled, state(ac_mode=1, soc=60), ac_output_intent("WR1"))

    assert _written(controlled) == [{"acMode": 2}]


class _Device:
    """The device's local API: one report, and every write it was sent."""

    def __init__(self, properties):
        self.properties = properties
        self.posts = []

    def get(self, url, **_kwargs):
        return SimpleNamespace(status_code=200, json=lambda: {"properties": dict(self.properties)})

    def post(self, url, json=None, **_kwargs):
        self.posts.append(json["properties"])
        return SimpleNamespace(status_code=200, json=lambda: {})


def test_a_device_that_reports_no_ac_mode_is_not_written_one_every_cycle():
    """The reported defect, through the real fetch: acMode=2 went out each cycle."""

    hardware = _Device(REPORT)
    dev = ZendureClient(
        "WR1", "192.0.2.10", "WR1-SN", hardware, min_soc=15, max_soc=100,
        smart_mode=1, grid_off_mode=None, max_power=800,
        hardware_profile="solarflow_800_pro_2",
    )
    controller = EMSController(
        [dev], ShellyStub(300), sleep_enabled=False, runtime_state=RuntimeStateStub()
    )
    controller.set_output_limit = Mock(return_value=True)

    with patch.multiple(
        "ems.controller.cfg",
        SYSTEM_ENABLED=True,
        MAX_TOTAL_POWER=800,
        MAX_DEVICE_POWER=800,
        MIN_OUTPUT_LIMIT=0,
        DEADBAND=10,
        SOC_RECONCILE_INTERVAL=0,
        DRY_RUN=False,
        SIMULATION_MODE=False,
        ARGS=SimpleNamespace(replay=False),
        ALLOW_HARDWARE_WRITES=True,
        ALLOW_STATE_RECONCILIATION_WRITES=True,
    ):
        for _ in range(3):
            assert controller.run_guarded() is True

    assert [written for written in hardware.posts if "acMode" in written] == []
    assert controller.set_output_limit.call_count >= 1


RETIRED = "reconcile_ac_mode_on_start"


def test_the_retired_startup_key_is_gone_from_the_catalog_and_the_template():
    from ems.config_catalog import get_config_feature_field_index

    template = json.loads(open("config/config.template.json", encoding="utf-8").read())

    assert f"system.{RETIRED}" not in get_config_feature_field_index()
    assert RETIRED not in template["system"]
    assert RETIRED not in cfg.default_runtime_config()["system"]
    assert not hasattr(cfg, "RECONCILE_AC_MODE_ON_START")
    assert not hasattr(EMSController, "run_startup_ac_mode_reconcile_once")


@pytest.mark.parametrize("value", [True, False, "yes", None])
def test_a_config_that_still_carries_the_retired_key_loads_and_starts_unchanged(
    tmp_path, capsys, caplog, value
):
    """Any value, even one that is not a boolean, is left alone and changes nothing."""

    def loaded(with_key):
        config = cfg.default_runtime_config()
        config["system"].update(
            {"dry_run": False, "simulation_mode": False, "allow_hardware_writes": True,
             "allow_state_reconciliation_writes": True}
        )
        config["system"].pop(RETIRED, None)
        if with_key:
            config["system"][RETIRED] = value
        config["config_upgrade"] = {"on_startup": "check"}
        (tmp_path / "config").mkdir(exist_ok=True)
        shutil.copy("config/config.template.json", tmp_path / "config" / "config.template.json")
        path = tmp_path / ("with.json" if with_key else "without.json")
        path.write_text(json.dumps(config))
        before = path.read_bytes()
        args = SimpleNamespace(
            config=str(path), dry_run=False, simulate=False, replay=None,
            self_test=False,
        )
        snapshot = {name: getattr(cfg, name) for name in dir(cfg) if name.isupper()}
        try:
            cfg.initialize(args, str(tmp_path))
            result = {
                name: getattr(cfg, name)
                for name in ("DRY_RUN", "SIMULATION_MODE", "ALLOW_HARDWARE_WRITES",
                             "ALLOW_STATE_RECONCILIATION_WRITES", "RECONCILE_SMART_MODE")
            }
        finally:
            for name, saved in snapshot.items():
                setattr(cfg, name, saved)
        assert path.read_bytes() == before
        return result

    with caplog.at_level(logging.DEBUG):
        assert loaded(True) == loaded(False)

    assert RETIRED not in capsys.readouterr().err
    assert RETIRED not in caplog.text
