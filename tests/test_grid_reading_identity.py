# SPDX-FileCopyrightText: 2026 basecubedev
# SPDX-License-Identifier: AGPL-3.0-or-later
"""How a grid meter client tells a new reading from one it repeats.

The control loop regulates once per reading (see ``tests/test_grid_reading_once.py``).
That only works for a meter whose client says which reading is which, so every
meter type the catalogue offers is classified here: either its client can repeat
a reading and names each one, or it has no way to tell its readings apart and
every read counts as a new one.
"""

import functools
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from ems.clients import (
    DsmrTcpClient,
    E3dcModbusGridMeterClient,
    EcoTrackerClient,
    HomeWizardP1Client,
    ModbusMeterClient,
    MqttGridMeterClient,
    PowerfoxClient,
    Shelly3EMGen1Client,
    ShellyClient,
    SunSpecMeterClient,
    TasmotaHttpClient,
    ZendureGridMeterHttpClient,
    close_grid_meter_client,
    create_grid_meter_client,
)
from ems import tibber_pulse
from ems.config_catalog import grid_meter_types
from ems.grid_reading import GridReading, ReadingTracker, read_grid_meter
from ems.tibber_pulse import TibberPulseGridMeterClient

pytestmark = [
    pytest.mark.unit,
    pytest.mark.power_control,
]


NAMES_ITS_READINGS = {
    MqttGridMeterClient: "keeps the last message until the next one arrives",
    TibberPulseGridMeterClient: "keeps the last frame of a stream or a poll",
    DsmrTcpClient: "keeps the last telegram; a DSMR 4 meter sends every 10 s",
    HomeWizardP1Client: "the P1 meter repeats a telegram until the next; its time stamp tells",
    PowerfoxClient: "the poweropti repeats a value until the next; its time stamp tells",
}

NO_READING_IDENTITY = {
    ShellyClient: "measures its present power on every read",
    Shelly3EMGen1Client: "measures its present power on every read",
    EcoTrackerClient: "serves its reading head's last datagram, with no time stamp",
    TasmotaHttpClient: "serves its reading head's last datagram, with no time stamp",
    ZendureGridMeterHttpClient: "serves its last datagram, with no time stamp",
    ModbusMeterClient: "reads the meter's power registers",
    SunSpecMeterClient: "reads the meter's power registers",
    E3dcModbusGridMeterClient: "reading_for_cycle never hands the meter the same reading twice",
}

MINIMAL_SETTINGS = {
    "ip": "192.0.2.1",
    "port": 23,
    "unit_id": 1,
    "serial": "0123456789ab",
    "token": "0123456789ABCDEF0123456789ABCDEF",
    "node_id": 1,
    "power_path": "StatusSNS.SM.Power",
}


def build_client(meter_type, monkeypatch):
    monkeypatch.setattr(
        tibber_pulse,
        "TibberPulseGridMeterClient",
        functools.partial(TibberPulseGridMeterClient, start=False),
    )
    settings = {
        **MINIMAL_SETTINGS,
        "type": meter_type,
        "mqtt": {"host": "192.0.2.1", "topic": "meter/power", "_mqtt_client_factory": MagicMock},
    }
    if meter_type == "dsmr_p1_tcp":
        settings["port"] = 23
    else:
        settings.pop("port")
    return create_grid_meter_client(settings, MagicMock())


@pytest.mark.parametrize("meter_type", sorted(grid_meter_types()))
def test_every_meter_type_says_whether_it_repeats_readings(meter_type, monkeypatch):
    client = build_client(meter_type, monkeypatch)
    try:
        kind = type(client)
        assert (kind in NAMES_ITS_READINGS) != (kind in NO_READING_IDENTITY), (
            f"{meter_type} builds a {kind.__name__}, which is classified in neither or both lists"
        )
        if kind in NAMES_ITS_READINGS:
            assert callable(getattr(client, "get_reading", None))
        else:
            assert not hasattr(client, "get_reading")
    finally:
        close_grid_meter_client(client)


def test_every_classified_client_is_built_by_some_meter_type(monkeypatch):
    built = set()
    for meter_type in grid_meter_types():
        client = build_client(meter_type, monkeypatch)
        built.add(type(client))
        close_grid_meter_client(client)

    assert set(NAMES_ITS_READINGS) | set(NO_READING_IDENTITY) == built


class Message:
    topic = "meter/power"

    def __init__(self, payload):
        self.payload = payload


def mqtt_meter():
    return MqttGridMeterClient(
        "192.0.2.70", 1883, "meter/power", client_factory=MagicMock
    )


def test_each_mqtt_message_is_one_reading_even_with_the_same_value():
    meter = mqtt_meter()
    meter._on_message(None, None, Message(b"250"))
    first = meter.get_reading()
    repeated = meter.get_reading()

    meter._on_message(None, None, Message(b"250"))
    second = meter.get_reading()

    assert first == repeated == GridReading(250.0, first.sequence)
    assert second.power_w == 250.0
    assert second.sequence != first.sequence


def test_an_unreadable_mqtt_message_is_no_new_reading():
    meter = mqtt_meter()
    meter._on_message(None, None, Message(b"250"))
    first = meter.get_reading()

    meter._on_message(None, None, Message(b"not a number"))

    assert meter.get_reading() == first


def test_the_mqtt_power_is_the_reading_s_power():
    meter = mqtt_meter()
    meter._on_message(None, None, Message(b"-420"))

    assert meter.get_power() == meter.get_reading().power_w == -420.0


def test_a_client_without_reading_identity_gives_a_new_reading_on_every_read():
    client = SimpleNamespace(get_power=lambda: 75)

    assert read_grid_meter(client) == GridReading(75, None)


def test_a_client_with_reading_identity_is_asked_for_its_reading():
    reading = GridReading(75, 3)
    client = SimpleNamespace(get_power=lambda: 0, get_reading=lambda: reading)

    assert read_grid_meter(client) is reading


def test_the_tracker_tells_a_repeated_reading_from_a_new_one():
    tracker = ReadingTracker()
    client = object()

    assert tracker.is_new(client, GridReading(10, 1)) is True
    assert tracker.is_new(client, GridReading(10, 1)) is False
    assert tracker.is_new(client, GridReading(10, 2)) is True
    assert tracker.is_new(client, GridReading(12, 2)) is False


def test_a_reading_without_identity_is_always_new_and_starts_over():
    tracker = ReadingTracker()
    client = object()
    tracker.is_new(client, GridReading(10, 1))

    assert tracker.is_new(client, GridReading(10, None)) is True
    assert tracker.is_new(client, GridReading(10, None)) is True
    assert tracker.is_new(client, GridReading(10, 1)) is True


def test_another_client_s_reading_is_new_whatever_its_sequence():
    tracker = ReadingTracker()
    tracker.is_new(object(), GridReading(10, 1))

    assert tracker.is_new(object(), GridReading(10, 1)) is True


def dated_mqtt():
    from unittest.mock import patch

    meter = mqtt_meter()
    clock = SimpleNamespace(now=12.0)
    with patch("time.monotonic", lambda: clock.now):
        meter._on_message(None, None, Message(b"250"))
        clock.now = 13.5
        first = meter.get_reading()
        repeated = meter.get_reading()
    return first, repeated, 12.0


def dated_tibber():
    from tests.test_tibber_pulse_grid_meter import ISKRA, _client

    client, clock = _client(ISKRA)
    clock.now = 1012.0
    client.poll_once()
    clock.now = 1013.5
    first = client.get_reading()
    repeated = client.get_reading()
    return first, repeated, 1012.0


def dated_dsmr():
    from tests.test_dsmr_p1_tcp_grid_meter import Clock, Reader, telegram

    clock = Clock()
    client = DsmrTcpClient(Reader([telegram("00.400")], []), clock=clock)
    first = client.get_reading()
    clock.now += 3
    repeated = client.get_reading()
    return first, repeated, 100.0


def dated_homewizard():
    from tests.test_homewizard_p1_grid_meter import SINGLE_PHASE_EXPORT, Clock, FakeApi

    clock = Clock()
    still = {**SINGLE_PHASE_EXPORT, "power_w": 300}
    client = HomeWizardP1Client(FakeApi(still, still), clock=clock)
    first = client.get_reading()
    clock.now += 3
    repeated = client.get_reading()
    return first, repeated, 1000.0


def dated_powerfox():
    from tests.test_powerfox_grid_meter import KEY, Clock, Response, Session, _value

    clock = Clock()
    session = Session(Response(_value(100, 5)), Response(_value(100, 5)))
    client = PowerfoxClient("192.0.2.50", session, KEY, clock=clock)
    first = client.get_reading()
    clock.now += 3
    repeated = client.get_reading()
    return first, repeated, 1000.0


DATED_BY = {
    MqttGridMeterClient: dated_mqtt,
    TibberPulseGridMeterClient: dated_tibber,
    DsmrTcpClient: dated_dsmr,
    HomeWizardP1Client: dated_homewizard,
    PowerfoxClient: dated_powerfox,
}


def test_every_client_that_names_its_readings_dates_them():
    assert set(DATED_BY) == set(NAMES_ITS_READINGS)


@pytest.mark.parametrize("kind", sorted(DATED_BY, key=lambda kind: kind.__name__), ids=lambda kind: kind.__name__)
def test_a_reading_is_dated_when_it_reached_the_ems_and_keeps_that_date(kind):
    """A message or a frame by its arrival, a polled reading by the read that first returned it."""

    first, repeated, received_at = DATED_BY[kind]()

    assert first.received_at == received_at
    assert repeated.sequence == first.sequence
    assert repeated.received_at == received_at


def test_a_client_without_reading_identity_is_dated_when_the_read_went_out():
    from unittest.mock import patch

    clock = SimpleNamespace(now=5.0)

    def get_power():
        clock.now = 6.0
        return 75

    with patch("time.monotonic", lambda: clock.now):
        reading = read_grid_meter(SimpleNamespace(get_power=get_power))

    assert reading == GridReading(75, None)
    assert reading.received_at == 5.0


def test_a_reading_s_date_is_no_part_of_its_identity():
    assert GridReading(75, 3, received_at=1.0) == GridReading(75, 3, received_at=2.0)


def test_a_polled_meter_s_next_reading_is_dated_by_its_own_read():
    from tests.test_dsmr_p1_tcp_grid_meter import Clock as DsmrClock, Reader, telegram
    from tests.test_homewizard_p1_grid_meter import SINGLE_PHASE_EXPORT, Clock, FakeApi

    clock = Clock()
    still = {**SINGLE_PHASE_EXPORT, "power_w": 300}
    moved = {**still, "timestamp": "2026-11-12T03:14:37"}
    homewizard = HomeWizardP1Client(FakeApi(still, still, moved), clock=clock)
    dsmr_clock = DsmrClock()
    dsmr = DsmrTcpClient(
        Reader([telegram("00.400")], [], [telegram("00.400", stamp="261112050010W")]),
        clock=dsmr_clock,
    )
    for _ in range(2):
        homewizard.get_reading()
        dsmr.get_reading()
        clock.now += 3
        dsmr_clock.now += 3

    assert homewizard.get_reading().received_at == 1006.0
    assert dsmr.get_reading().received_at == 106.0
