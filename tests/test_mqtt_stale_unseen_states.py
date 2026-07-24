# SPDX-License-Identifier: AGPL-3.0-or-later
"""Fresh, stale and unseen are distinct runtime states, driven by an injected clock.

Staleness is exercised without a real sleep: telemetry aggregators stamp
snapshots with a :class:`FakeClock`, ``patch_snapshot_clock`` points the runtime's
staleness math at the same clock, and ``clock.advance`` ages a device past
``stale_after_seconds``. The production safety contract is asserted directly:
a stale or unseen device reads as unavailable (``None``) and receives no unsafe
write, while a fresh message restores availability.
"""

import contextlib
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from ems.zendure_mqtt.service import SNAPSHOT_FRESH, SNAPSHOT_STALE, SNAPSHOT_UNSEEN
from tests.helpers import payloads
from tests.helpers.controller import patch_snapshot_clock, run_installation_cycle
from tests.helpers.fake_mqtt import FakeClock, FakeMqttNetwork
from tests.helpers.mqtt_scenarios import (
    PAYLOAD_HTTP_ZENSDK,
    PAYLOAD_LEGACY_JSON,
    ROLE_INVERTER_CONTROL,
    STATE_STALE,
    STATE_UNSEEN,
    TRANSPORT_API_HTTP,
    TRANSPORT_GRID_METER_HTTP,
    TRANSPORT_GRID_METER_MQTT,
    TRANSPORT_LOCAL_MQTT_A,
    BrokerSpec,
    DeviceSpec,
    GridMeterSpec,
    Scenario,
    build_installation,
)

pytestmark = [pytest.mark.simulation, pytest.mark.power_control]

_STALE_AFTER = 60.0  # ZendureMqttRuntimeConfig default
LOCAL_A = BrokerSpec(ref="local_a", source="local_mqtt", host="10.0.0.10")
HTTP_METER = GridMeterSpec(
    meter_type="shelly", transport=TRANSPORT_GRID_METER_HTTP, power_w=2000.0
)


def _legacy(name="LA", *, state=STATE_STALE):
    return DeviceSpec(name, ROLE_INVERTER_CONTROL, TRANSPORT_LOCAL_MQTT_A,
                      PAYLOAD_LEGACY_JSON, broker_ref="local_a",
                      device_id=f"DEV{name}", product_key=f"PK{name}",
                      control_enabled=True, state=state)


def _report(name="LA"):
    return payloads.legacy_json_report(device_id=f"DEV{name}", serial=f"DEV{name}")


@contextlib.contextmanager
def _frozen_client_monotonic(value):
    """Freeze ``ems.clients`` monotonic time so grid-meter staleness is testable.

    ``get_power`` reads only ``time.monotonic``; swapping the module reference for
    that call is enough and needs no real wait.
    """

    import ems.clients as clients

    with patch.object(clients, "time", SimpleNamespace(monotonic=lambda: value)):
        yield


def _build(scenario, clock):
    network = FakeMqttNetwork(clock=clock)
    return network, build_installation(scenario, network)


# --- inverter telemetry: fresh / stale / unseen -----------------------------
def test_fresh_message_reads_available():
    clock = FakeClock()
    _network, installation = _build(
        Scenario(name="fresh", brokers=(LOCAL_A,), devices=(_legacy(),),
                 grid_meter=HTTP_METER),
        clock,
    )
    try:
        device = installation.control_runtime.devices[0]
        with patch_snapshot_clock(clock):
            assert device.fetch() is not None
    finally:
        installation.stop()


def test_fresh_to_stale_transition_reads_unavailable():
    clock = FakeClock()
    _network, installation = _build(
        Scenario(name="fresh_then_stale", brokers=(LOCAL_A,), devices=(_legacy(),),
                 grid_meter=HTTP_METER),
        clock,
    )
    try:
        device = installation.control_runtime.devices[0]
        with patch_snapshot_clock(clock):
            assert device.fetch() is not None  # fresh
            clock.advance(_STALE_AFTER + 5)
            assert device.fetch() is None  # aged past the threshold
    finally:
        installation.stop()


def test_unseen_device_reads_unavailable_distinctly():
    clock = FakeClock()
    _network, installation = _build(
        Scenario(name="unseen", brokers=(LOCAL_A,),
                 devices=(_legacy(state=STATE_UNSEEN),), grid_meter=HTTP_METER),
        clock,
    )
    try:
        device = installation.control_runtime.devices[0]
        with patch_snapshot_clock(clock):
            # No message was ever injected: unseen, not merely stale.
            status = device._service.snapshot_status("DEVLA", now_monotonic=clock.monotonic())
            assert status.state == SNAPSHOT_UNSEEN
            assert device.fetch() is None
    finally:
        installation.stop()


def test_recovery_from_stale_to_fresh():
    clock = FakeClock()
    network, installation = _build(
        Scenario(name="recovery", brokers=(LOCAL_A,), devices=(_legacy(),),
                 grid_meter=HTTP_METER),
        clock,
    )
    try:
        device = installation.control_runtime.devices[0]
        with patch_snapshot_clock(clock):
            clock.advance(_STALE_AFTER + 5)
            assert device.fetch() is None  # stale
            network.broker("local_a").inject(
                "iot/PKLA/DEVLA/properties/report", _report()
            )
            assert device.fetch() is not None  # a fresh message restores it
    finally:
        installation.stop()


def test_stale_and_unseen_are_classified_differently():
    clock = FakeClock()
    network, installation = _build(
        Scenario(name="stale_vs_unseen", brokers=(LOCAL_A,), devices=(_legacy(),),
                 grid_meter=HTTP_METER),
        clock,
    )
    try:
        service = installation.control_runtime.devices[0]._service
        # Seen then aged -> stale; never seen -> unseen. Two distinct states.
        assert service.snapshot_status(
            "DEVLA", now_monotonic=clock.monotonic()
        ).state == SNAPSHOT_FRESH
        assert service.snapshot_status(
            "DEVLA", now_monotonic=clock.monotonic() + _STALE_AFTER + 5
        ).state == SNAPSHOT_STALE
        assert service.snapshot_status(
            "NEVER_SEEN", now_monotonic=clock.monotonic()
        ).state == SNAPSHOT_UNSEEN
    finally:
        installation.stop()


def test_stale_device_gets_no_unsafe_write_through_real_fetch():
    clock = FakeClock()
    network, installation = _build(
        Scenario(
            name="stale_no_write",
            brokers=(LOCAL_A,),
            devices=(
                DeviceSpec("API", ROLE_INVERTER_CONTROL, TRANSPORT_API_HTTP,
                           PAYLOAD_HTTP_ZENSDK, serial="API"),
                _legacy("LA"),
            ),
            grid_meter=HTTP_METER,
        ),
        clock,
    )
    try:
        clock.advance(_STALE_AFTER + 5)  # the MQTT device is now stale
        controller = run_installation_cycle(installation, clock=clock)
        explanation = controller.last_control_explanation
        # The healthy API device is controlled.
        assert explanation.devices["API"].online is True
        # The stale MQTT device is marked offline and skipped, never written.
        assert explanation.devices["LA"].online is False
        assert network.broker("local_a").publish_calls == []
    finally:
        installation.stop()


def test_unseen_device_gets_no_write_through_real_fetch():
    clock = FakeClock()
    network, installation = _build(
        Scenario(
            name="unseen_no_write",
            brokers=(LOCAL_A,),
            devices=(_legacy(state=STATE_UNSEEN),),
            grid_meter=HTTP_METER,
        ),
        clock,
    )
    try:
        controller = run_installation_cycle(installation, clock=clock)
        assert controller.last_control_explanation.devices["LA"].online is False
        assert network.broker("local_a").publish_calls == []
    finally:
        installation.stop()


# --- D0 grid meter: fresh / stale / unseen ----------------------------------
def _d0_scenario(name, *, state):
    return Scenario(
        name=name,
        brokers=(LOCAL_A,),
        devices=(),
        grid_meter=GridMeterSpec(
            meter_type="zendure_smartmeter_d0", transport=TRANSPORT_GRID_METER_MQTT,
            broker_ref="local_a", serial="D0X",
            topic="Zendure/sensor/D0X/totalPower", power_w=1800.0, state=state,
        ),
    )


def test_stale_d0_grid_meter_reports_stale_not_unseen():
    network = FakeMqttNetwork()
    installation = build_installation(_d0_scenario("d0_stale", state=STATE_STALE), network)
    meter = installation.grid_meter
    try:
        # A valid reading was injected: fresh and healthy.
        assert meter.get_power() == 1800.0
        assert meter.health.stale_used is False
        seen_at = meter.last_message_monotonic
        assert seen_at is not None  # distinctly not unseen

        # Age the reading past max_age; the meter reports stale (documented safe
        # behavior: it holds its last value rather than inventing a fresh one).
        with _frozen_client_monotonic(seen_at + meter.max_age_seconds + 5):
            assert meter.get_power() == 1800.0
            assert meter.health.stale_used is True
    finally:
        installation.stop()


def test_unseen_d0_grid_meter_is_distinct_from_stale():
    network = FakeMqttNetwork()
    installation = build_installation(_d0_scenario("d0_unseen", state=STATE_UNSEEN), network)
    meter = installation.grid_meter
    try:
        # No message was ever received: the safe fallback is 0, and it is unseen
        # (no last-message timestamp), never a stale held value.
        assert meter.get_power() == 0
        assert meter.last_message_monotonic is None
    finally:
        installation.stop()
