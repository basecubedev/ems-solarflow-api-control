# SPDX-License-Identifier: AGPL-3.0-or-later
"""A local-HTTP report number no float can hold is a missing value, not a crash.

``ZendureClient.fetch`` handed the device's own report to ``parse_device``
unfiltered. A ``socLimit`` of 1e19, an ``acStatus`` of 2**63 or a ``packNum``
just below 2**63 (rounded up through a float) reached the full-charge state
store, which keeps them as SQLite integers ending at 2**63, and raised
``OverflowError`` there. Since K8 a failing cycle no longer ends the process,
but this one failed at the same point every loop, before any device was
written: every device held its last limit for as long as the report said so.
The MQTT path already reads such a value as missing (``observable_metrics``);
the HTTP path now reads its report through the same rule.
"""

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from ems.clients import ZendureClient
from ems.controller import EMSController
from ems.state_store import BatteryFullChargeStateStore
from test_write_gates import RuntimeStateStub, ShellyStub

pytestmark = [
    pytest.mark.unit,
    pytest.mark.simulation,
    pytest.mark.power_control,
]

BEYOND_FLOAT = int("9" * 400)
REPORT = {
    "electricLevel": 60,
    "minSoc": 150,
    "socSet": 1000,
    "solarInputPower": 500,
    "outputHomePower": 0,
    "outputLimit": 200,
    "packNum": 1,
    "acMode": 2,
    "acStatus": 1,
    "dcStatus": 1,
    "packState": 2,
    "socLimit": 0,
}


class _Device:
    """The device's local API: one report, and every write it was sent."""

    def __init__(self, properties):
        self.properties = properties
        self.posts = []

    def get(self, url, **_kwargs):
        return SimpleNamespace(status_code=200, json=lambda: {"properties": dict(self.properties)})

    def post(self, url, json=None, **_kwargs):
        self.posts.append(json)
        return SimpleNamespace(status_code=200, json=lambda: {})


def _client(properties):
    return ZendureClient(
        "WR1",
        "192.0.2.10",
        "WR1-SN",
        _Device(properties),
        min_soc=15,
        max_soc=100,
        smart_mode=1,
        grid_off_mode=None,
        max_power=800,
        hardware_profile="solarflow_800_pro_2",
    )


OUT_OF_RANGE = [
    ("socLimit", 1e19),
    ("acStatus", 2**63),
    ("socStatus", 1e19),
    ("batCalTime", 10**20),
    ("packNum", 1e300),
    ("acMode", -1e19),
    ("packNum", 2**63 - 1),
    ("packNum", 2**53),
    ("outputLimit", BEYOND_FLOAT),
    ("electricLevel", float("nan")),
]


@pytest.mark.parametrize(("field", "value"), OUT_OF_RANGE)
def test_a_number_of_2_53_or_more_reads_as_missing(tmp_path, field, value):
    """Read as if the report had not carried it, and the store keeps the state."""

    dev = _client({**REPORT, field: value})
    without = _client({key: item for key, item in REPORT.items() if key != field})

    state = dev.fetch()
    BatteryFullChargeStateStore(str(tmp_path / "assist.sqlite")).record_observation(
        "WR1", state, True, datetime(2026, 1, 1), 30
    )

    assert state == without.fetch()


def test_a_dropped_report_value_is_named_once(caplog):
    """Dropped, the value would read as missing with a healthy read and no word of it."""

    dev = _client({**REPORT, "socLimit": 1e19, "minSoc": "abc", "inventedField": 1e19})

    with caplog.at_level("INFO"):
        for _ in range(2):
            assert dev.fetch() is not None

    named = [record.getMessage() for record in caplog.records if "http_report_value_ignored" in record.getMessage()]
    assert len(named) == 2
    assert "metric=minSoc" in named[0] and "metric=socLimit" in named[1]


def test_every_number_below_2_53_is_read_as_it_came():
    """The other side of the bound: nothing a device can mean is dropped."""

    state = _client({**REPORT, "electricLevel": 52.5, "batCalTime": 2**53 - 1}).fetch()

    assert (state.soc, state.battery_calibration_time, state.output_limit) == (52.5, 2**53 - 1, 200)


def test_a_pack_count_of_zero_beside_pack_data_in_the_report_is_unknown():
    """The pack list the HTTP report carries is still weighed against a zero."""

    state = _client({**REPORT, "packNum": 0, "packData": [{"sn": "P1"}]}).fetch()

    assert state.pack_num is None


@pytest.mark.parametrize(("field", "value"), [("socLimit", 1e19), ("packNum", 2**63 - 1)])
def test_a_report_the_store_cannot_hold_leaves_every_cycle_running(tmp_path, field, value):
    """Before the fix each cycle failed in the store and wrote no device at all."""

    dev = _client({**REPORT, field: value})
    controller = EMSController(
        [dev],
        ShellyStub(300),
        sleep_enabled=False,
        runtime_state=RuntimeStateStub(),
        battery_full_charge_store=BatteryFullChargeStateStore(str(tmp_path / "assist.sqlite")),
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
    ):
        results = [controller.run_guarded() for _ in range(3)]

    assert results == [True, True, True]
    assert controller.cycle_failures == 0
    assert controller.set_output_limit.call_count >= 1
