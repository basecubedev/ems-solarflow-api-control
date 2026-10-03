# SPDX-License-Identifier: AGPL-3.0-or-later
"""A report number no float can hold is a missing value, not a reading.

JSON and scalar MQTT payloads carry integers of any length. Python keeps them
exact, but every comparison downstream (confirmation, foreign-writer detection,
the aggregator's SoC normalisation) works in floats and raised
``OverflowError`` on such a value. These tests feed the report through the real
control client, aggregator and device client.
"""

import time
from datetime import datetime

import pytest

from ems.mqtt_control.dispatch import WriteDispatchStatus
from ems.state_store import BatteryFullChargeStateStore
from ems.target_control import detect_capabilities
from ems.zendure_mqtt.device_client import ZendureMqttDeviceClient
from ems.zendure_mqtt.payloads import coerce_scalar, parse_report_payload
from ems.zendure_mqtt.runtime import build_zendure_mqtt_runtime
from ems.zendure_mqtt.service import ZendureMqttRuntimeConfig
from ems.zendure_mqtt.snapshot import ZendureMqttAggregator, observable_metrics
from ems.zendure_mqtt.topics import FAMILY_LEGACY_JSON
from tests.helpers.fake_mqtt import FakeClock, FakeMqttNetwork

pytestmark = [
    pytest.mark.mqtt,
    pytest.mark.unit,
    pytest.mark.simulation,
    pytest.mark.power_control,
]

BEYOND_FLOAT = "9" * 400
REPORT_TOPIC = "iot/PK/DEV/properties/report"
APPLIED = {"outputLimit": 300, "acMode": 2, "smartMode": 1, "inputLimit": 0}


@pytest.fixture
def clock(monkeypatch):
    fake = FakeClock()
    monkeypatch.setattr(time, "monotonic", fake.monotonic)
    return fake


@pytest.fixture
def network(clock):
    return FakeMqttNetwork(clock)


@pytest.fixture
def service(network):
    config = ZendureMqttRuntimeConfig.from_dict(
        {"host": "10.0.0.10"}, broker_ref="local", source="local_mqtt"
    )
    started = network.control_service_factory()(config)
    started.start()
    yield started
    started.stop()


def _device(service):
    return ZendureMqttDeviceClient(
        "INV",
        service,
        device_id="DEV",
        topic_family=FAMILY_LEGACY_JSON,
        source="local_mqtt",
        broker_ref="local",
        product_key="PK",
        hardware_profile="solarflow_800_pro_2",
        max_power=2000,
    )


def _inject(network, properties, topic=REPORT_TOPIC):
    body = ", ".join(f'"{key}": {value}' for key, value in properties.items())
    assert network.broker("local").inject(topic, f'{{"properties": {{{body}}}}}'.encode())


def _confirmed_device(service, network, clock):
    dev = _device(service)
    assert dev.dispatch_output_limit(300).status is WriteDispatchStatus.PUBLISHED
    clock.advance(1.0)
    _inject(network, APPLIED)
    assert dev.fetch() is not None
    assert dev.describe()["last_confirmed_target_w"] == 300
    return dev


@pytest.mark.parametrize(
    "literal", [BEYOND_FLOAT, "-" + BEYOND_FLOAT], ids=["positive", "negative"]
)
def test_parser_reads_an_integer_beyond_float_range_as_missing(literal):
    report = parse_report_payload(
        ('{"properties": {"outputLimit": %s, "electricLevel": 55}}' % literal).encode()
    )
    assert report.properties["outputLimit"] is None
    assert report.properties["electricLevel"] == 55


def test_scalar_integer_beyond_float_range_stays_text():
    assert coerce_scalar(BEYOND_FLOAT.encode()) == BEYOND_FLOAT
    assert coerce_scalar(("-" + BEYOND_FLOAT).encode()) == "-" + BEYOND_FLOAT
    assert coerce_scalar(b"430") == 430


def test_scalar_soc_beyond_float_range_leaves_the_snapshots_readable():
    aggregator = ZendureMqttAggregator()
    aggregator.observe("Zendure/sensor/SN1/minSoc", BEYOND_FLOAT.encode())
    aggregator.observe("Zendure/sensor/SN1/electricLevel", b"55")

    [snapshot] = aggregator.snapshots()

    assert snapshot.metrics["electricLevel"] == 55
    assert "min_soc_percent" not in snapshot.metrics


def test_output_limit_beyond_float_range_is_neither_a_crash_nor_foreign_control(
    service, network, clock
):
    dev = _confirmed_device(service, network, clock)

    for _report in range(2):
        clock.advance(1.0)
        _inject(network, {"outputLimit": BEYOND_FLOAT})
        assert dev.fetch() is not None

    described = dev.describe()
    assert described["external_control_suspected"] is False
    assert dev.read_health.consecutive_failures == 0


def test_output_limit_beyond_float_range_never_confirms_the_command_in_flight(
    service, network, clock
):
    dev = _device(service)
    assert dev.dispatch_output_limit(300).status is WriteDispatchStatus.PUBLISHED
    record = dev._active_command
    clock.advance(1.0)
    _inject(network, dict(APPLIED, outputLimit=BEYOND_FLOAT))

    assert dev.fetch() is not None

    assert record.state == "published"
    assert dev.describe()["active_command"]["state"] == "published"


def test_another_devices_soc_beyond_float_range_does_not_blind_this_device(
    service, network, clock
):
    dev = _confirmed_device(service, network, clock)
    clock.advance(1.0)
    _inject(
        network, {"minSoc": BEYOND_FLOAT}, topic="iot/PK/OTHER/properties/report"
    )

    assert dev.fetch() is not None
    assert dev.describe()["state"] == "fresh"


def test_soc_beyond_float_range_keeps_the_status_file_writable(network, tmp_path):
    runtime = build_zendure_mqtt_runtime(
        {
            "zendure_mqtt": {"host": "10.0.0.10"},
            "devices": [
                {
                    "type": "zendure_mqtt",
                    "name": "Battery",
                    "mqtt": {"topic_family": "legacy_json", "device_id": "DEV"},
                }
            ],
        },
        service_factory=network.telemetry_service_factory(),
    )
    runtime.start()
    try:
        report = '{"properties": {"electricLevel": 55, "minSoc": %s}}' % BEYOND_FLOAT
        assert network.broker("default").inject(REPORT_TOPIC, report.encode())

        assert runtime.write_status_file(tmp_path / "status.json") is True
    finally:
        runtime.stop()


NUMERIC_FIELDS = ("soc", "min_soc", "max_soc", "solar", "output", "output_limit", "temp", "voltage")


@pytest.mark.parametrize("payload", [BEYOND_FLOAT, "abc", "1e400", "nan"])
@pytest.mark.parametrize(
    "metric",
    [
        "outputLimit",
        "solarInputPower",
        "outputHomePower",
        "packInputPower",
        "electricLevel",
        "minSoc",
        "socSet",
        "hyperTmp",
        "BatVolt",
        "remainOutTime",
    ],
)
def test_scalar_text_reaches_the_controller_as_a_missing_value(service, network, metric, payload):
    """Any client on the broker can publish a scalar topic, and the aggregator
    keeps text as it came. Handed to ``parse_device`` it reached the control
    maths: ``run_once`` raised and the EMS process ended, or ``fetch`` raised
    and the device went offline without a read failure.
    """

    dev = _device(service)
    _inject(network, {"electricLevel": 60, "outputLimit": 200, "packNum": 1, "minSoc": 150})
    assert network.broker("local").inject(f"Zendure/sensor/DEV/{metric}", payload.encode())

    state = dev.fetch()

    assert state is not None
    assert all(
        isinstance(getattr(state, field), (int, float)) and not isinstance(getattr(state, field), bool)
        for field in NUMERIC_FIELDS
    )
    detect_capabilities(state)


def test_a_dropped_report_value_leaves_a_trace(service, network, caplog):
    """Dropped silently, text read as a missing value with a healthy read."""

    dev = _device(service)
    _inject(network, {"electricLevel": 60, "outputLimit": 200, "packNum": 1})
    assert network.broker("local").inject("Zendure/sensor/DEV/minSoc", b"abc")

    with caplog.at_level("INFO"):
        for _ in range(2):
            assert dev.fetch() is not None

    assert dev.describe()["ignored_report_values"] == ["minSoc"]
    assert caplog.text.count("event=mqtt_report_value_ignored") == 1
    assert "reason=not_a_usable_number" in caplog.text


class _RecordingProperties(dict):
    """Properties that remember every field asked for, holding nothing or 1 everywhere."""

    def __init__(self, value=None):
        super().__init__()
        self.read = set()
        self._value = value

    def get(self, key, default=None):
        self.read.add(key)
        return default if self._value is None else self._value

    def __getitem__(self, key):
        self.read.add(key)
        if self._value is None:
            raise KeyError(key)
        return self._value

    def __contains__(self, key):
        self.read.add(key)
        return self._value is not None

    def _unnamed(self, *args, **kwargs):
        raise AssertionError("parse_device reads report fields by name only")

    __iter__ = keys = values = items = copy = setdefault = pop = popitem = update = _unnamed


def test_the_traced_report_values_are_the_ones_parse_device_reads():
    """Two lists that must agree: the trace's field set and what parse_device reads.

    Read with nothing and with several numbers everywhere, so a field read only
    when another one is set, is zero or is a given mode is found too. A field
    read in a way that does not name it cannot be traced, so that is refused.
    """

    from ems.clients import DEVICE_STATE_PROPERTIES, parse_device

    read = set()
    for value in (None, 0, 1, 2, 100_000):
        props = _RecordingProperties(value)
        parse_device({"properties": props})
        read |= props.read

    assert read - {"packData"} == DEVICE_STATE_PROPERTIES


def test_a_number_a_report_sends_as_text_is_read_as_that_number(service, network):
    """A JSON report may quote a number; dropped as text, a pack count read as unknown."""

    dev = _device(service)
    _inject(network, {"electricLevel": 60, "outputLimit": 200, "packNum": '"2"', "batCalTime": '"1700000000"'})

    state = dev.fetch()

    assert (state.pack_num, state.battery_calibration_time) == (2, 1700000000)
    assert dev.describe()["ignored_report_values"] == []


def test_derived_invented_and_textual_values_are_not_traced(service, network, caplog):
    """Traced over every key, the aggregator's derived fields read as dropped on
    every PV device, and invented names flooded the log one INFO line each."""

    dev = _device(service)
    invented = ", ".join(f'"made_up_{n}": "x"' for n in range(200))
    assert network.broker("local").inject(
        REPORT_TOPIC,
        (
            '{"properties": {"electricLevel": 60, "outputLimit": 200, "packNum": 1, '
            '"solarPower1": 120, "solarPower2": 80, "fanSwitch": 1, "packData": [{"sn": "P1"}], '
            + invented + "}}"
        ).encode(),
    )

    with caplog.at_level("INFO"):
        assert dev.fetch() is not None

    assert dev.describe()["ignored_report_values"] == []
    assert "mqtt_report_value_ignored" not in caplog.text


def test_a_quoted_number_is_held_as_that_number_from_the_moment_it_arrives():
    """Read as text, confirmation never saw a quoted applied state. Parsed at
    every fetch instead, text no number can hold is parsed again each cycle
    under the device lock: ten scalar texts of 10 MB cost two seconds."""

    aggregator = ZendureMqttAggregator()
    aggregator.observe(REPORT_TOPIC, b'{"properties": {"packNum": "2", "outputLimit": " 300 ", "name": "x"}}')

    (snapshot,) = aggregator.snapshots()

    assert (snapshot.metrics["packNum"], snapshot.metrics["outputLimit"]) == (2, 300)
    assert snapshot.metrics["name"] == "x"
    assert observable_metrics({"packNum": "2"}) == {}


def test_a_quoted_applied_state_confirms_the_command(service, network, clock):
    dev = _device(service)
    assert dev.dispatch_output_limit(300).status is WriteDispatchStatus.PUBLISHED
    record = dev._active_command
    clock.advance(1.0)
    _inject(network, {key: f'"{value}"' for key, value in APPLIED.items()})

    dev.fetch()

    assert record.state == "telemetry_confirmed"


def test_a_quoted_foreign_target_is_suspected(service, network, clock):
    dev = _confirmed_device(service, network, clock)
    for _ in range(3):
        clock.advance(5.0)
        _inject(network, {"outputLimit": '"700"'})
        dev.fetch()

    assert dev.describe()["external_control_suspected"] is True


@pytest.mark.parametrize(
    ("field", "literal"),
    [
        ("socLimit", "1e19"),
        ("acStatus", "9223372036854775808"),
        ("socStatus", '"1e19"'),
        ("batCalTime", '"100000000000000000000"'),
        ("packNum", "1e300"),
        ("acMode", "-1e19"),
        ("packNum", "9223372036854775807"),
        ("packNum", "9007199254740992"),
    ],
)
def test_a_number_of_2_53_or_more_reads_as_missing(service, network, tmp_path, field, literal):
    """Past 2**53 a float no longer holds every integer, and the pack count is
    read through one; the full-charge store keeps these fields as SQLite
    integers, and one such report from any broker client ended run_once."""

    dev = _device(service)
    _inject(network, {"electricLevel": 60, "outputLimit": 200, "packNum": 1, field: literal})

    state = dev.fetch()
    BatteryFullChargeStateStore(str(tmp_path / "assist.sqlite")).record_observation(
        "INV", state, True, datetime(2026, 1, 1), 30
    )

    assert dev.describe()["ignored_report_values"] == [field]


def test_a_report_boolean_is_no_number(service, network):
    dev = _device(service)
    _inject(network, {"electricLevel": 60, "outputLimit": 200, "packNum": 1, "acMode": "true"})

    dev.fetch()

    assert dev.describe()["ignored_report_values"] == ["acMode"]


def test_a_value_read_again_as_a_number_is_no_longer_listed_as_ignored(service, network):
    dev = _device(service)
    _inject(network, {"electricLevel": 60, "outputLimit": 200, "packNum": 1, "minSoc": '"abc"'})
    dev.fetch()
    assert dev.describe()["ignored_report_values"] == ["minSoc"]

    _inject(network, {"minSoc": 100})
    dev.fetch()

    assert dev.describe()["ignored_report_values"] == []


def test_every_number_below_2_53_is_read_as_it_came(service, network):
    """The other side of the bound: nothing a device can mean is dropped."""

    dev = _device(service)
    _inject(network, {"electricLevel": 52.5, "outputLimit": "300.0", "packNum": 1, "batCalTime": 2**53 - 1})

    state = dev.fetch()

    assert (state.soc, state.output_limit, state.battery_calibration_time) == (52.5, 300.0, 2**53 - 1)
    assert dev.describe()["ignored_report_values"] == []
    assert observable_metrics({"a": -(2**53 - 1), "b": -52.5}) == {"a": -(2**53 - 1), "b": -52.5}


@pytest.mark.parametrize("pack_data", ['"P1"', '["P1"]', '{"sn": "P1"}'], ids=["text", "list", "object"])
def test_a_pack_count_of_zero_beside_any_pack_data_is_unknown(service, network, pack_data):
    """Main weighed whatever the device sent as packData; with the properties
    filtered to numbers, a non-list packData vanished and 0 read as a battery
    confirmed absent."""

    dev = _device(service)
    _inject(network, {"electricLevel": 60, "outputLimit": 200, "packNum": 0, "packData": pack_data})

    state = dev.fetch()

    assert state.pack_num is None


def test_a_pack_count_of_zero_beside_top_level_pack_data_is_unknown(service, network):
    """The form the real firmware sends: packData beside properties, not in them."""

    dev = _device(service)
    assert network.broker("local").inject(
        REPORT_TOPIC,
        b'{"properties": {"electricLevel": 60, "outputLimit": 200, "packNum": 0}, '
        b'"packData": [{"sn": "P1"}]}',
    )

    assert dev.fetch().pack_num is None


@pytest.mark.parametrize("pack_data", [None, "[]", '""', "0", '"0"'], ids=["missing", "empty-list", "empty-text", "zero", "zero-text"])
def test_a_pack_count_of_zero_without_pack_data_is_a_confirmed_absence(service, network, pack_data):
    """The other direction: an empty packData is no witness. A zero sent as text
    is read as the number it is when the report arrives, as on a scalar topic."""

    dev = _device(service)
    properties = {"electricLevel": 60, "outputLimit": 200, "packNum": 0}
    if pack_data is not None:
        properties["packData"] = pack_data
    _inject(network, properties)

    assert dev.fetch().pack_num == 0


@pytest.mark.parametrize(("pack_data", "pack_num"), [('"P1"', None), (None, 0)], ids=["text", "missing"])
def test_the_dashboard_tile_weighs_the_same_pack_witness(pack_data, pack_num):
    """Two readers of one aggregator: the telemetry-only tile and fetch()."""

    from types import SimpleNamespace

    from dashboard.telemetry import _telemetry_only_tiles

    properties = '{"electricLevel": 60, "packNum": 0' + (f', "packData": {pack_data}' if pack_data else "") + "}"
    aggregator = ZendureMqttAggregator()
    aggregator.observe(REPORT_TOPIC, ('{"properties": ' + properties + "}").encode())
    (snapshot,) = aggregator.snapshots()
    runtime = SimpleNamespace(
        device_summaries=lambda: [{"name": "T", "identifier": "DEV", "status": "online"}],
        snapshots=lambda: {"DEV": snapshot},
    )

    (tile,) = _telemetry_only_tiles(SimpleNamespace(zendure_mqtt_runtime=runtime))

    assert tile["state"].pack_num == pack_num
