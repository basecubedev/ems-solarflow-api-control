# SPDX-License-Identifier: AGPL-3.0-or-later
"""Every ``minSoc`` writer follows the raise rule winter mode follows (K15).

The configured ``min_soc`` was written without a look at the SoC: with winter
mode switched off, for a device whose winter policy takes no step, and by the
full-charge assist at its start, on every loop while it ran and on its
restore. A raise far above the battery makes the firmware charge from the grid
at full power (field incident 2026-10-03); one onto the SoC makes it draw about
1.2 kW for a few seconds (2026-10-07/08). Assist and winter also wrote
``minSoc`` against each other while an assist ran.

The configured ``min_soc`` is now held to ``cfg.winter_min_soc_raise_limit``
as every winter target is, nothing is written over a ``minSoc`` the report does
not carry, and the assist writes ``socSet`` alone: ``minSoc`` stays with the
SoC reconcile while it runs.
"""

import logging
from contextlib import contextmanager
from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from ems import config as cfg
from ems.clients import ZendureClient, parse_device
from ems.controller import EMSController
from ems.state_store import BatteryFullChargeStateStore
from tests.test_battery_full_charge_assist import configure_assist
from tests.test_off_means_off import Runtime
from tests.test_write_gates import ShellyStub

pytestmark = [
    pytest.mark.unit,
    pytest.mark.simulation,
    pytest.mark.power_control,
]

WINTER_MORNING = datetime(2026, 11, 3, 9, 0)
SUMMER_MORNING = datetime(2026, 7, 2, 9, 0)
DUE = datetime(2026, 11, 3, 9, 0, tzinfo=timezone.utc)


def report(soc, min_soc, max_soc=100, pv=0):
    """A local-API report; ``None`` leaves the field out, as a device may."""

    properties = {
        "socSet": max_soc * 10,
        "solarInputPower": pv,
        "outputLimit": 0,
        "packNum": 1,
        "acMode": 2,
        "acStatus": 1,
        "smartMode": 1,
    }
    if soc is not None:
        properties["electricLevel"] = soc
    if min_soc is not None:
        properties["minSoc"] = min_soc * 10
    return parse_device({"properties": properties})


class _Reply:
    status_code = 200
    text = ""


class SocWindowHardware:
    """A device that takes its SoC window from the writes it is sent."""

    def __init__(self, item):
        self.state = item
        self.writes = []

    def post(self, url, json=None, **_kwargs):
        properties = dict(json["properties"])
        self.writes.append(properties)
        if "minSoc" in properties:
            self.state.min_soc = properties["minSoc"] / 10
            self.state.min_soc_reported = True
        if "socSet" in properties:
            self.state.max_soc = properties["socSet"] / 10
        return _Reply()


CONTEXTS = {
    "winter off in the config": ({"enabled": False}, None, None, WINTER_MORNING),
    "winter off at runtime": ({}, {"enabled": False}, None, WINTER_MORNING),
    "policy none in winter": ({}, None, "none", WINTER_MORNING),
    "policy none in summer": ({}, None, "none", SUMMER_MORNING),
}
SETTINGS = {**CONTEXTS, "winter on": ({}, None, None, WINTER_MORNING)}


class Rig:
    """One local-API device behind the real controller and its write gates."""

    def __init__(
        self,
        *,
        configured_min_soc,
        item,
        context="winter off in the config",
        configured_max_soc=100,
        store=None,
    ):
        winter, runtime_winter, policy, self.now = SETTINGS[context]
        self.winter = {**cfg.WINTER_DEFAULTS, "enabled": True, **winter}
        self.entry = {"name": "WR1", **({"winter_policy": policy} if policy else {})}
        runtime = Runtime()
        if runtime_winter is not None:
            runtime.data["winter"] = runtime_winter
        self.hardware = SocWindowHardware(item)
        self.dev = ZendureClient(
            "WR1", "192.0.2.10", "WR1-SN", self.hardware,
            configured_min_soc, configured_max_soc, 1, None, 800,
            hardware_profile="solarflow_800_pro_2",
        )
        self.controller = EMSController(
            devices=[self.dev],
            shelly=ShellyStub(0),
            sleep_enabled=False,
            runtime_state=runtime,
            battery_full_charge_store=store,
        )

    @property
    def state(self):
        return self.hardware.state

    @contextmanager
    def configured(self):
        with (
            patch.object(cfg, "WINTER_CONFIG", self.winter),
            patch.object(cfg, "ZENDURE_CONFIG", [self.entry]),
            patch.object(cfg, "state_reconciliation_writes_allowed", lambda gate: True),
            configure_assist(assist_start_soc=0, enable_ac_charge_mode=False),
        ):
            yield

    def reconcile(self):
        with self.configured():
            self.controller.reconcile_device_state_limits(
                self.dev,
                self.state,
                cfg.winter_mode_active(self.now, self.controller.runtime_state),
                self.now,
            )

    def assist(self, now=DUE):
        with self.configured():
            self.controller.process_battery_full_charge_assist(
                self.dev,
                self.state,
                now,
                self.controller.full_charge_assist_feature_transition(now),
            )

    def min_soc_written(self):
        return [write["minSoc"] for write in self.hardware.writes if "minSoc" in write]


# --- the configured min_soc where winter mode plans nothing ---------------------


@pytest.mark.parametrize("context", CONTEXTS)
def test_a_configured_raise_far_above_the_soc_waits_for_the_battery(context):
    rig = Rig(configured_min_soc=30, item=report(soc=20, min_soc=15), context=context)

    rig.reconcile()
    assert rig.min_soc_written() == []

    rig.state.soc = 27
    rig.reconcile()
    assert rig.min_soc_written() == [300]


@pytest.mark.parametrize("context", CONTEXTS)
def test_a_configured_raise_onto_the_soc_waits_until_the_soc_moves(context):
    rig = Rig(configured_min_soc=20, item=report(soc=20, min_soc=15), context=context)

    rig.reconcile()
    assert rig.min_soc_written() == []

    rig.state.soc = 21
    rig.reconcile()
    assert rig.min_soc_written() == [200]


@pytest.mark.parametrize("context", CONTEXTS)
def test_a_configured_raise_the_battery_nearly_holds_is_written_whole(context):
    rig = Rig(configured_min_soc=22, item=report(soc=20, min_soc=15), context=context)

    rig.reconcile()

    assert rig.min_soc_written() == [220]


@pytest.mark.parametrize("context", CONTEXTS)
@pytest.mark.parametrize("lead", [4, 5, 6])
def test_a_configured_raise_leading_the_soc_by_more_than_three_points_waits(context, lead):
    """The 800 Pro 2 latches a grid charge between four and five points above the SoC."""
    rig = Rig(configured_min_soc=20 + lead, item=report(soc=20, min_soc=15), context=context)

    rig.reconcile()

    assert rig.min_soc_written() == []


@pytest.mark.parametrize("context", CONTEXTS)
def test_a_configured_raise_three_points_above_the_soc_is_written(context):
    rig = Rig(configured_min_soc=23, item=report(soc=20, min_soc=15), context=context)

    rig.reconcile()

    assert rig.min_soc_written() == [230]


@pytest.mark.parametrize("context", CONTEXTS)
@pytest.mark.parametrize("soc", [20, None], ids=["soc 20", "no soc"])
def test_a_configured_lowering_is_written_at_once(context, soc):
    rig = Rig(configured_min_soc=15, item=report(soc=soc, min_soc=30), context=context)

    rig.reconcile()

    assert rig.min_soc_written() == [150]


@pytest.mark.parametrize("context", CONTEXTS)
def test_a_report_without_a_soc_gets_no_configured_raise(context):
    rig = Rig(configured_min_soc=30, item=report(soc=None, min_soc=15), context=context)

    rig.reconcile()

    assert rig.min_soc_written() == []


@pytest.mark.parametrize("context", CONTEXTS)
def test_nothing_is_written_over_a_min_soc_the_report_does_not_carry(context):
    rig = Rig(
        configured_min_soc=15,
        configured_max_soc=100,
        item=report(soc=50, min_soc=None, max_soc=90),
        context=context,
    )

    rig.reconcile()

    assert rig.hardware.writes == [{"socSet": 1000}]


def test_a_configured_raise_that_waits_is_said_once_until_it_is_written(caplog):
    rig = Rig(configured_min_soc=30, item=report(soc=20, min_soc=15))

    with caplog.at_level(logging.DEBUG):
        rig.reconcile()
        rig.reconcile()
        rig.state.soc = 27
        rig.reconcile()
        rig.state.min_soc = 15
        rig.state.soc = 20
        rig.reconcile()

    said = [
        record.levelno
        for record in caplog.records
        if "event=min_soc_raise_waits_for_battery" in record.getMessage()
    ]
    assert said == [logging.INFO, logging.DEBUG, logging.INFO]


# --- the full-charge assist writes socSet alone ---------------------------------


def assist_due(tmp_path):
    store = BatteryFullChargeStateStore(str(tmp_path / "ems_state.sqlite"))
    store.set_full_charge_feature_enabled_state(True, DUE)
    store.update_device_state("WR1", DUE, has_battery=True, next_due_at=DUE.isoformat())
    return store


def assist_running(tmp_path):
    store = assist_due(tmp_path)
    store.mark_assist_started("WR1", DUE, False)
    store.update_device_state("WR1", DUE, max_soc_request_pending=False)
    return store


def assist_restoring(tmp_path):
    store = assist_running(tmp_path)
    store.mark_assist_completed("WR1", DUE, 28)
    return store


@pytest.mark.parametrize("min_soc", [15, None], ids=["reported", "not reported"])
def test_the_assist_start_writes_no_min_soc(tmp_path, min_soc):
    rig = Rig(
        configured_min_soc=30,
        configured_max_soc=90,
        item=report(soc=20, min_soc=min_soc, max_soc=90),
        store=assist_due(tmp_path),
    )

    rig.assist()

    assert rig.controller.battery_full_charge_store.get_device_state("WR1")[
        "full_charge_assist_active"
    ]
    assert rig.hardware.writes == [{"socSet": 1000}]


def test_the_running_assist_writes_no_min_soc(tmp_path):
    rig = Rig(
        configured_min_soc=30,
        configured_max_soc=90,
        item=report(soc=20, min_soc=15, max_soc=90),
        store=assist_running(tmp_path),
    )

    rig.assist()
    rig.assist()

    assert rig.hardware.writes == [{"socSet": 1000}]


def test_the_assist_restore_writes_no_min_soc(tmp_path):
    rig = Rig(
        configured_min_soc=30,
        configured_max_soc=90,
        item=report(soc=20, min_soc=15, max_soc=100),
        store=assist_restoring(tmp_path),
    )

    rig.assist()

    assert rig.hardware.writes == [{"socSet": 900}]
    assert not rig.controller.battery_full_charge_store.get_device_state("WR1")[
        "restore_pending"
    ]


def test_assist_and_winter_do_not_write_min_soc_against_each_other(tmp_path):
    """The live order: the assist every cycle, then the SoC reconcile.

    The assist wrote the configured 15, the reconcile winter's 30 back, once
    each per cycle for as long as the assist ran.
    """

    rig = Rig(
        configured_min_soc=15,
        configured_max_soc=90,
        item=report(soc=40, min_soc=30, max_soc=100),
        context="winter on",
        store=assist_running(tmp_path),
    )
    remembered = rig.controller.winter.device("WR1")
    remembered.target = 30
    remembered.step_date = "2026-11-02"

    for _ in range(4):
        rig.assist()
        rig.reconcile()

    assert rig.min_soc_written() == []
    assert rig.state.min_soc == 30


def test_during_an_assist_the_configured_min_soc_still_follows_the_rule(tmp_path):
    rig = Rig(
        configured_min_soc=30,
        configured_max_soc=90,
        item=report(soc=20, min_soc=15, max_soc=100),
        store=assist_running(tmp_path),
    )

    rig.assist()
    rig.reconcile()
    assert rig.min_soc_written() == []

    rig.state.soc = 28
    rig.assist()
    rig.reconcile()
    assert rig.min_soc_written() == [300]
