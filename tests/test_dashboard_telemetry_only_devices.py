# SPDX-FileCopyrightText: 2026 basecubedev
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Telemetry-only Zendure MQTT devices are visible read-only in the dashboard.

A Zendure MQTT device that streams telemetry but is not write-enabled
(``capabilities.write_output_limit`` unset) is excluded from the control loop
and never joins ``controller.devices``. It must still appear in the dashboard as
a read-only tile so a healthy but uncontrolled inverter is never invisible. The
tile carries live telemetry, is flagged ``read_only``, contributes to the
aggregate totals, and never gains a control target.
"""

import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from dashboard.telemetry import build_dashboard_snapshot

pytestmark = [
    pytest.mark.contract,
    pytest.mark.simulation,
]


class _FakeTelemetryRuntime:
    def __init__(self, summaries, snapshots):
        self._summaries = summaries
        self._snapshots = snapshots

    def device_summaries(self):
        return self._summaries

    def snapshots(self):
        return self._snapshots


def _snapshot(metrics):
    return SimpleNamespace(metrics=metrics)


def _controller(devices, online, runtime=None):
    return SimpleNamespace(
        devices=[SimpleNamespace(name=name) for name in devices],
        runtime_state=None,
        device_online=online,
        commanded_total_w=0,
        filtered_load_w=0,
        _dashboard_capabilities=[],
        zendure_mqtt_runtime=runtime,
    )


def _control_state():
    return SimpleNamespace(solar=400, output=300, pack_out=110, pack_in=10, soc=60)


def test_telemetry_only_device_appears_as_read_only_tile_with_live_values():
    runtime = _FakeTelemetryRuntime(
        summaries=[{"name": "INV_2", "identifier": "ID2", "status": "online"}],
        snapshots={
            "ID2": _snapshot(
                {
                    "electricLevel": 88,
                    "solarInputPower": 315,
                    "outputHomePower": 280,
                    "outputPackPower": 0,
                    "packInputPower": 0,
                    "outputLimit": 300,
                }
            )
        },
    )
    controller = _controller(["WR1"], {"WR1": True}, runtime=runtime)

    snapshot = build_dashboard_snapshot(
        controller,
        load_w=0,
        states=[_control_state()],
        targets=[300],
        effective_targets=[300],
        allocated_total_w=300,
        effective_total_w=300,
        enabled=True,
        max_total_power=1600,
        min_output_limit=35,
    )

    assert set(snapshot["devices"]) == {"WR1", "INV_2"}
    tile = snapshot["devices"]["INV_2"]
    assert tile["read_only"] is True
    assert tile["online"] is True
    assert tile["soc"] == 88
    assert tile["pv_input_w"] == 315
    assert tile["output_w"] == 280
    assert tile["output_limit_w"] == 300
    assert tile["target_w"] == 0
    assert tile["allocated_target_w"] == 0
    assert tile["capability"] is None
    assert tile["battery_full_charge_assist"]["status"] == "unknown"

    assert "read_only" not in snapshot["devices"]["WR1"]
    assert snapshot["pv_total_w"] == 715
    assert snapshot["inverter_output_w"] == 580
    assert snapshot["average_soc"] == 74.0


def test_no_telemetry_runtime_leaves_control_only_snapshot_unchanged():
    controller = _controller(["WR1"], {"WR1": True}, runtime=None)

    snapshot = build_dashboard_snapshot(
        controller,
        load_w=0,
        states=[_control_state()],
        targets=[300],
        effective_targets=[300],
        allocated_total_w=300,
        effective_total_w=300,
        enabled=True,
        max_total_power=1600,
        min_output_limit=35,
    )

    assert set(snapshot["devices"]) == {"WR1"}
    assert snapshot["pv_total_w"] == 400


def test_stale_telemetry_device_is_shown_offline_and_unseen_is_skipped():
    runtime = _FakeTelemetryRuntime(
        summaries=[
            {"name": "STALE", "identifier": "S1", "status": "stale"},
            {"name": "UNSEEN", "identifier": "U1", "status": "unseen"},
            {"name": "INVALID", "identifier": None, "status": "invalid"},
        ],
        snapshots={
            "S1": _snapshot({"electricLevel": 50, "solarInputPower": 100, "outputHomePower": 90}),
        },
    )
    controller = _controller(["WR1"], {"WR1": True}, runtime=runtime)

    snapshot = build_dashboard_snapshot(
        controller,
        load_w=0,
        states=[_control_state()],
        targets=[300],
        effective_targets=[300],
        allocated_total_w=300,
        effective_total_w=300,
        enabled=True,
        max_total_power=1600,
        min_output_limit=35,
    )

    assert set(snapshot["devices"]) == {"WR1", "STALE"}
    assert snapshot["devices"]["STALE"]["online"] is False
    assert snapshot["devices"]["STALE"]["read_only"] is True
    assert snapshot["rules"]["offline_devices"]["active"] is True


def test_telemetry_device_never_shadows_a_control_device_of_the_same_name():
    runtime = _FakeTelemetryRuntime(
        summaries=[{"name": "WR1", "identifier": "ID1", "status": "online"}],
        snapshots={"ID1": _snapshot({"electricLevel": 1, "outputHomePower": 9999})},
    )
    controller = _controller(["WR1"], {"WR1": True}, runtime=runtime)

    snapshot = build_dashboard_snapshot(
        controller,
        load_w=0,
        states=[_control_state()],
        targets=[300],
        effective_targets=[300],
        allocated_total_w=300,
        effective_total_w=300,
        enabled=True,
        max_total_power=1600,
        min_output_limit=35,
    )

    assert set(snapshot["devices"]) == {"WR1"}
    assert "read_only" not in snapshot["devices"]["WR1"]
    assert snapshot["devices"]["WR1"]["output_w"] == 300


def test_read_only_flag_survives_the_json_snapshot_roundtrip():
    runtime = _FakeTelemetryRuntime(
        summaries=[{"name": "INV_2", "identifier": "ID2", "status": "online"}],
        snapshots={"ID2": _snapshot({"electricLevel": 88, "outputHomePower": 280})},
    )
    controller = _controller(["WR1"], {"WR1": True}, runtime=runtime)

    snapshot = build_dashboard_snapshot(
        controller,
        load_w=0,
        states=[_control_state()],
        targets=[300],
        effective_targets=[300],
        allocated_total_w=300,
        effective_total_w=300,
        enabled=True,
        max_total_power=1600,
        min_output_limit=35,
    )
    roundtrip = json.loads(json.dumps(snapshot, sort_keys=True))
    assert roundtrip["devices"]["INV_2"]["read_only"] is True


def _namespace_runtime(name, messages, *, device_id="garage-inverter", status="online"):
    """A telemetry runtime fed through the real aggregator, topic by topic."""

    from ems.zendure_mqtt.runtime import classify_zendure_mqtt_devices
    from ems.zendure_mqtt.snapshot import ZendureMqttAggregator

    (device,), invalid = classify_zendure_mqtt_devices(
        [
            {
                "name": name,
                "type": "external_mqtt",
                "mqtt": {"topic_family": "ems_solarflow", "device_id": device_id},
            }
        ]
    )
    assert invalid == []
    aggregator = ZendureMqttAggregator()
    for key, payload in messages:
        aggregator.observe(f"ems-solarflow/{device_id}/{key}", payload)
    snapshots = {snap.device_id: snap for snap in aggregator.snapshots()}
    return _FakeTelemetryRuntime(
        summaries=[{"name": name, "identifier": device.identifier, "status": status}],
        snapshots=snapshots,
    )


def _dashboard(runtime, states=None):
    controller = _controller(["WR1"], {"WR1": True}, runtime=runtime)
    return build_dashboard_snapshot(
        controller,
        load_w=0,
        states=states or [_control_state()],
        targets=[300],
        effective_targets=[300],
        allocated_total_w=300,
        effective_total_w=300,
        enabled=True,
        max_total_power=1600,
        min_output_limit=35,
    )


def test_an_external_device_is_a_read_only_tile_like_any_other():
    """A device with no local API at all -- read over MQTT from whatever a
    home-automation system publishes -- reaches the cockpit through the very
    same runtime as a telemetry-only Zendure device. One kind of read-only
    tile, and one path to it."""

    snapshot = _dashboard(
        _namespace_runtime("Garage inverter", [("inverterPower", b"1234")])
    )

    tile = snapshot["devices"]["Garage inverter"]
    assert tile["read_only"] is True
    assert tile["online"] is True
    assert tile["output_w"] == 1234
    assert tile["target_w"] == 0
    assert tile["capability"] is None
    assert snapshot["inverter_output_w"] == 300 + 1234


def test_every_key_reaches_its_tile_and_the_totals():
    """Topic in, tile and totals out, through the real aggregator."""

    snapshot = _dashboard(
        _namespace_runtime(
            "Garage battery",
            [
                (
                    "state",
                    b'{"inverterPower": 400, "solarPower": 520, '
                    b'"batteryPower": 120, "batterySoc": 40}',
                ),
            ],
        )
    )

    tile = snapshot["devices"]["Garage battery"]
    assert tile["output_w"] == 400
    assert tile["pv_input_w"] == 520
    assert tile["battery_power_w"] == 120
    assert (tile["pack_output_w"], tile["pack_input_w"]) == (120, 0)
    assert tile["soc"] == 40
    assert tile["soc_reported"] is True
    assert snapshot["pv_total_w"] == 400 + 520
    assert snapshot["inverter_output_w"] == 300 + 400
    assert snapshot["battery_power_w"] == 100 + 120
    assert snapshot["average_soc"] == (60 + 40) / 2


def test_a_discharging_battery_counts_as_discharge():
    """The namespace's sign is the dashboard's: negative is discharge."""

    snapshot = _dashboard(
        _namespace_runtime(
            "Garage battery",
            [("batteryPower", b"-250"), ("inverterPower", b"250"), ("batterySoc", b"55")],
        )
    )

    tile = snapshot["devices"]["Garage battery"]
    assert tile["battery_power_w"] == -250
    assert (tile["pack_output_w"], tile["pack_input_w"]) == (0, 250)
    assert snapshot["battery_power_w"] == 100 - 250


def test_a_device_without_a_charge_level_is_left_out_of_the_average():
    """Counting it at zero would halve the average of a single full battery."""

    snapshot = _dashboard(
        _namespace_runtime("Garage inverter", [("inverterPower", b"765")])
    )

    tile = snapshot["devices"]["Garage inverter"]
    assert tile["soc_reported"] is False
    assert snapshot["average_soc"] == 60


def test_a_telemetry_only_zendure_device_without_a_charge_level_is_left_out_too():
    runtime = _FakeTelemetryRuntime(
        summaries=[{"name": "INV_2", "identifier": "ID2", "status": "online"}],
        snapshots={"ID2": _snapshot({"outputHomePower": 280})},
    )

    snapshot = _dashboard(runtime)

    assert snapshot["devices"]["INV_2"]["soc_reported"] is False
    assert snapshot["average_soc"] == 60


def test_a_telemetry_only_device_without_packs_is_left_out_of_the_average():
    runtime = _FakeTelemetryRuntime(
        summaries=[{"name": "INV_2", "identifier": "ID2", "status": "online"}],
        snapshots={"ID2": _snapshot({"packNum": 0, "electricLevel": 0, "outputHomePower": 280})},
    )

    snapshot = _dashboard(runtime)

    assert snapshot["devices"]["INV_2"]["soc_reported"] is False
    assert snapshot["average_soc"] == 60


@pytest.mark.parametrize("payload", ["9" * 400, "abc", 2**60])
def test_a_charge_level_no_reading_can_hold_is_not_reported(payload):
    """The tile reads the charge level through the filter the state does, or a
    text value would count as a battery at zero and halve the average."""

    runtime = _FakeTelemetryRuntime(
        summaries=[{"name": "INV_2", "identifier": "ID2", "status": "online"}],
        snapshots={"ID2": _snapshot({"outputHomePower": 280, "electricLevel": payload})},
    )

    snapshot = _dashboard(runtime)

    assert snapshot["devices"]["INV_2"]["soc_reported"] is False
    assert snapshot["average_soc"] == 60


@pytest.mark.parametrize("payload", ["9" * 400, "abc"])
@pytest.mark.parametrize("metric", ["minSoc", "socSet", "hyperTmp", "BatVolt", "remainOutTime"])
def test_text_in_a_telemetry_only_report_costs_no_snapshot(metric, payload):
    """The control path reads the aggregator through one filter; the tiles read
    the same aggregator and must weigh it alike, or one text value loses every
    device's dashboard snapshot."""

    metrics = {"electricLevel": 88, "solarInputPower": 315, "outputHomePower": 280, metric: payload}
    runtime = _FakeTelemetryRuntime(
        summaries=[{"name": "INV_2", "identifier": "ID2", "status": "online"}],
        snapshots={"ID2": _snapshot(metrics)},
    )
    controller = _controller(["WR1"], {"WR1": True}, runtime=runtime)

    snapshot = build_dashboard_snapshot(
        controller,
        load_w=0,
        states=[_control_state()],
        targets=[300],
        effective_targets=[300],
        allocated_total_w=300,
        effective_total_w=300,
        enabled=True,
        max_total_power=1600,
        min_output_limit=35,
    )

    assert snapshot["devices"]["INV_2"]["soc"] == 88


def test_a_charge_level_of_zero_is_a_reading_and_counts():
    snapshot = _dashboard(
        _namespace_runtime(
            "Garage battery", [("inverterPower", b"0"), ("batterySoc", b"0")]
        )
    )

    assert snapshot["devices"]["Garage battery"]["soc_reported"] is True
    assert snapshot["average_soc"] == 30


@pytest.mark.parametrize(
    ("messages", "hardware"),
    [
        ([("inverterPower", b"765")], {"pv": False, "battery": False}),
        ([("inverterPower", b"0"), ("solarPower", b"0")], {"pv": True, "battery": False}),
        ([("inverterPower", b"250"), ("batteryPower", b"-250")], {"pv": False, "battery": True}),
        ([("inverterPower", b"0"), ("batterySoc", b"40")], {"pv": False, "battery": True}),
        (
            [("state", b'{"inverterPower": 400, "solarPower": 520, "batteryPower": 120, "batterySoc": 40}')],
            {"pv": True, "battery": True},
        ),
    ],
)
def test_an_external_device_has_the_parts_its_bridge_publishes(messages, hardware):
    """An external device has no pack count and no ``pv_kwp``: what it has is
    what its bridge publishes. A key the bridge sends at 0 W is a part that
    is idle; a key it never sends is a part the device does not have."""

    snapshot = _dashboard(_namespace_runtime("Garage", messages))

    assert snapshot["devices"]["Garage"]["hardware"] == hardware


def test_a_telemetry_only_zendure_device_without_packs_has_no_battery():
    runtime = _FakeTelemetryRuntime(
        summaries=[{"name": "INV_2", "identifier": "ID2", "status": "online"}],
        snapshots={"ID2": _snapshot({"packNum": 0, "solarInputPower": 315, "outputHomePower": 280})},
    )

    snapshot = _dashboard(runtime)

    assert snapshot["devices"]["INV_2"]["hardware"] == {"pv": True, "battery": False}


@pytest.mark.parametrize(
    ("observed", "config_entries", "hardware"),
    [
        ({"product": "SolarFlow 2400 AC"}, [], {"pv": False, "battery": True}),
        ({}, [{"name": "INV_2", "type": "zendure_mqtt", "hardware_profile": "solarflow_2400_ac_plus"}], {"pv": False, "battery": True}),
        ({"product": "SolarFlow 800 Pro"}, [], {"pv": True, "battery": True}),
        ({}, [], {"pv": True, "battery": True}),
    ],
    ids=["model-from-report", "model-from-config", "model-with-pv", "model-unknown"],
)
def test_a_telemetry_only_zendure_device_has_what_its_model_can_have(observed, config_entries, hardware):
    """A Zendure device read over MQTT is known by the product its report names,
    or by the model its config entry pins, as a local device is."""

    from ems import config as cfg

    snapshot_of_device = SimpleNamespace(
        metrics={"packNum": 1, "electricLevel": 50, "outputHomePower": 280}, **observed
    )
    runtime = _FakeTelemetryRuntime(
        summaries=[{"name": "INV_2", "identifier": "ID2", "status": "online"}],
        snapshots={"ID2": snapshot_of_device},
    )
    with patch.object(cfg, "ZENDURE_CONFIG", config_entries):
        snapshot = _dashboard(runtime)

    assert snapshot["devices"]["INV_2"]["hardware"] == hardware


def test_both_kinds_of_read_only_device_can_be_present_at_once():
    # One runtime, two kinds of device: a Zendure device with no write method
    # and an external device with no command path at all.
    combined = _FakeTelemetryRuntime(
        summaries=[
            {"name": "INV_2", "identifier": "ID2", "status": "online"},
            {"name": "Garage inverter", "identifier": "EXAMPLE0000001", "status": "stale"},
        ],
        snapshots={
            "ID2": _snapshot({"outputHomePower": 280, "solarInputPower": 315}),
            "EXAMPLE0000001": _snapshot({"outputHomePower": 1234}),
        },
    )

    snapshot = _dashboard(combined)

    assert set(snapshot["devices"]) == {"WR1", "INV_2", "Garage inverter"}
    # A stale reading is shown as offline but still counted: the power is real,
    # only the reading is old.
    assert snapshot["devices"]["Garage inverter"]["online"] is False
    assert snapshot["inverter_output_w"] == 300 + 280 + 1234
    # And it stops the sample from counting as a measurement.
    assert snapshot["device_power_valid"] is False
