# SPDX-License-Identifier: AGPL-3.0-or-later
"""A device's ``min_soc``/``max_soc`` are validated once, when it is built (K14).

Only ``apply_soc_limits`` read them through ``cfg.safe_soc_limit``; winter
mode's floor and the remaining-time estimate read the raw config value, so a
``"15"``, a ``null`` or any other value that is not a number raised in every
winter reconcile, and the cycle failed. Both device classes now read the value
the SoC writer always used: the whole percent, or 0 (unmanaged) for a value
that is no percent from 0 to 100, named once at load. A value that is a valid
number loads unchanged.
"""

import logging
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from ems.clients import ZendureClient
from ems.controller import EMSController
from ems.target_control import calculate_remaining_time_hours
from ems.zendure_mqtt.device_client import ZendureMqttDeviceClient
from test_unknown_ac_mode import _Device
from test_write_gates import RuntimeStateStub, ShellyStub, state

pytestmark = [
    pytest.mark.unit,
    pytest.mark.power_control,
]

REPORT = {
    "electricLevel": 10,
    "minSoc": 150,
    "socSet": 1000,
    "solarInputPower": 0,
    "outputLimit": 0,
    "packNum": 1,
    "acStatus": 1,
    "dcStatus": 1,
    "packState": 2,
    "acMode": 2,
    "smartMode": 1,
}

READ_AS = [
    (15, 15),
    (100, 100),
    (0, 0),
    (15.0, 15),
    ("15", 15),
    ("abc", 0),
    (None, 0),
    (True, 0),
    (-5, 0),
    (150, 0),
    (float("nan"), 0),
]


def _http(min_soc, max_soc, session=None):
    return ZendureClient(
        "WR1", "192.0.2.10", "WR1-SN", session or Mock(), min_soc=min_soc,
        max_soc=max_soc, smart_mode=1, grid_off_mode=None, max_power=800,
        hardware_profile="solarflow_800_pro_2",
    )


def _mqtt(min_soc, max_soc):
    return ZendureMqttDeviceClient(
        "WR1", SimpleNamespace(), device_id="DEVICE_ID", topic_family="legacy_json",
        source="local_mqtt", product_key="PK", hardware_profile="hyper_2000",
        max_power=1200, min_soc=min_soc, max_soc=max_soc,
    )


@pytest.mark.parametrize("build", [_http, _mqtt], ids=["http", "mqtt"])
@pytest.mark.parametrize(("configured", "read"), READ_AS)
def test_a_device_reads_its_soc_bounds_as_the_soc_writer_does(build, configured, read):
    dev = build(configured, configured)

    assert dev.min_soc == read
    assert dev.max_soc == read


def test_a_valid_whole_number_loads_unchanged():
    dev = _http(15, 100)

    assert (dev.min_soc, dev.max_soc) == (15, 100)
    assert type(dev.min_soc) is int and type(dev.max_soc) is int


def test_a_value_left_unmanaged_is_named_at_load(caplog):
    with caplog.at_level(logging.DEBUG):
        _http("abc", 150)
        _http(15, 100)
        _http(0, None)

    said = [
        record.getMessage() for record in caplog.records
        if "event=device_soc_limit_invalid" in record.getMessage()
    ]
    assert len(said) == 2
    assert "device=WR1" in said[0] and "key=min_soc" in said[0]
    assert "key=max_soc" in said[1]
    assert all(record.levelno == logging.WARNING for record in caplog.records
               if "event=device_soc_limit_invalid" in record.getMessage())


def test_the_remaining_time_reads_the_validated_bound():
    dev = _http("100", "abc")

    assert calculate_remaining_time_hours(state(soc=50), SimpleNamespace(
        battery_kwh=1.0, min_soc=dev.min_soc, max_soc=dev.max_soc), 500) == 1.0


def _winter_cycles(min_soc, max_soc):
    hardware = _Device(REPORT)
    dev = _http(min_soc, max_soc, session=hardware)
    runtime = RuntimeStateStub()
    runtime.data = {}
    controller = EMSController(
        [dev], ShellyStub(300), sleep_enabled=False, runtime_state=runtime
    )
    controller.set_output_limit = Mock(return_value=True)
    results = []
    with patch.multiple(
        "ems.controller.cfg",
        SYSTEM_ENABLED=True,
        MAX_TOTAL_POWER=800,
        MAX_DEVICE_POWER=800,
        MIN_OUTPUT_LIMIT=35,
        DEADBAND=10,
        SOC_RECONCILE_INTERVAL=1,
        DRY_RUN=False,
        SIMULATION_MODE=False,
        ARGS=SimpleNamespace(replay=False),
        ALLOW_HARDWARE_WRITES=True,
        ALLOW_STATE_RECONCILIATION_WRITES=True,
    ), patch("ems.controller.cfg.winter_mode_active", return_value=True):
        for _ in range(2):
            results.append(controller.run_guarded())
    return results, hardware.posts


def test_a_string_bound_no_longer_fails_the_winter_reconcile():
    """The reported defect: ``"15"`` raised in winter's floor on every reconcile."""

    results, written = _winter_cycles("15", "100")

    assert results == [True, True]
    assert written == _winter_cycles(15, 100)[1]
