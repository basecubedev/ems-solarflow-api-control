# SPDX-License-Identifier: AGPL-3.0-or-later
"""Consumers: devices that only say what they draw.

A consumer comes in over the ``ems-consumer`` tree of the external catalog.
It is a part of the house load the grid meter already measured, so it joins no
inverter total, no validity flag and no control path: the snapshot names it,
the store integrates and prices it by the share of the house the grid supplied
at the time, and the history and InfluxDB carry its power -- all under its
history key, its device id unless ``history_id`` names another, so a rename
keeps what it drew.
"""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from urllib.parse import quote

import pytest

from dashboard.sqlite_store import DashboardStore, consumer_histories
from dashboard.telemetry import build_dashboard_snapshot, consumer_readings
from ems import config as cfg
from ems.energy_channels import home_grid_share
from ems.history.influx_provider import InfluxHistoryProvider
from ems.history.influx_writer import build_telemetry_lines
from ems.history.provider import SqliteHistoryProvider, consumer_of_series, normalize_series
from ems.zendure_mqtt.config_entries import (
    consumer_history_key,
    consumer_history_keys,
    validate_external_mqtt_device_config,
)
from ems.zendure_mqtt.runtime import classify_zendure_mqtt_devices
from ems.zendure_mqtt.snapshot import ZendureMqttAggregator
from mqtt_mapping.config import parse_mappings
from mqtt_mapping.mapping import Received, evaluate_device

pytestmark = [
    pytest.mark.contract,
    pytest.mark.mqtt,
]


class _Runtime:
    def __init__(self, summaries, snapshots):
        self._summaries = summaries
        self._snapshots = snapshots

    def device_summaries(self):
        return self._summaries

    def snapshots(self):
        return self._snapshots


def _entries(entries):
    return [
        {"name": name, "type": "external_mqtt", "mqtt": {"topic_family": family, "device_id": device_id}}
        for name, family, device_id, _messages, _status in entries
    ]


@pytest.fixture(autouse=True)
def _configured(monkeypatch):
    """The devices the dashboard looks its consumers' history keys up in."""

    monkeypatch.setattr(cfg, "ZENDURE_CONFIG", [])
    return lambda devices: monkeypatch.setattr(cfg, "ZENDURE_CONFIG", devices)


def _runtime(entries):
    """``entries``: ``(name, family, device_id, [(key, payload)], status)`` through the real aggregator."""

    if not cfg.ZENDURE_CONFIG:
        cfg.ZENDURE_CONFIG = _entries(entries)
    devices, invalid = classify_zendure_mqtt_devices(_entries(entries))
    assert invalid == []
    aggregator = ZendureMqttAggregator()
    prefixes = {"ems_solarflow": "ems-solarflow", "ems_consumer": "ems-consumer"}
    for _name, family, device_id, messages, _status in entries:
        for key, payload in messages:
            aggregator.observe(f"{prefixes[family]}/{device_id}/{key}", payload)
    return _Runtime(
        [
            {"name": entry[0], "identifier": device.identifier, "status": entry[4]}
            for entry, device in zip(entries, devices)
        ],
        {snap.device_id: snap for snap in aggregator.snapshots()},
    )


def _controller(runtime):
    return SimpleNamespace(
        devices=[SimpleNamespace(name="WR1")],
        runtime_state=None,
        device_online={"WR1": True},
        commanded_total_w=0,
        filtered_load_w=0,
        _dashboard_capabilities=[],
        zendure_mqtt_runtime=runtime,
    )


def _dashboard(runtime, load_w=400):
    return build_dashboard_snapshot(
        _controller(runtime),
        load_w=load_w,
        states=[SimpleNamespace(solar=700, output=600, pack_out=0, pack_in=100, soc=60)],
        targets=[600],
        effective_targets=[600],
        allocated_total_w=600,
        effective_total_w=600,
        enabled=True,
        max_total_power=1600,
        min_output_limit=35,
    )


WASHER = ("Washer", "ems_consumer", "washer", [("power", b"450")], "online")


def test_a_consumer_is_named_in_the_snapshot_and_joins_no_inverter_total():
    without = _dashboard(None)
    snapshot = _dashboard(_runtime([WASHER]))

    assert "Washer" not in snapshot["devices"]
    assert snapshot["consumers"] == {"washer": {"name": "Washer", "online": True, "power_w": 450}}
    for key in ("home_load_w", "pv_total_w", "inverter_output_w", "battery_power_w", "device_power_valid"):
        assert snapshot[key] == without[key], key
    assert snapshot["consumer_total_w"] == 450
    assert snapshot["home_other_w"] == snapshot["home_load_w"] - 450


def test_a_silent_consumer_reads_nothing_and_leaves_the_statistics_measured():
    snapshot = _dashboard(_runtime([("Washer", "ems_consumer", "washer", [("power", b"450")], "stale")]))

    assert snapshot["consumers"] == {"washer": {"name": "Washer", "online": False, "power_w": None}}
    assert snapshot["consumer_total_w"] == 0
    assert snapshot["device_power_valid"] is True


def test_consumers_summing_past_the_house_leave_no_negative_other():
    snapshot = _dashboard(_runtime([("Heat pump", "ems_consumer", "hp", [("power", b"5000")], "online")]))

    assert snapshot["home_other_w"] == 0


def test_an_inverter_and_a_consumer_of_one_id_stay_apart():
    snapshot = _dashboard(
        _runtime([
            ("Garage", "ems_solarflow", "garage", [("inverterPower", b"300")], "online"),
            ("Garage plug", "ems_consumer", "garage", [("power", b"80")], "online"),
        ])
    )

    assert snapshot["devices"]["Garage"]["output_w"] == 300
    assert snapshot["consumers"]["garage"] == {"name": "Garage plug", "online": True, "power_w": 80}


def test_an_inverter_and_a_consumer_of_one_id_are_watched_apart_on_another_broker():
    from ems.zendure_mqtt.config_entries import external_devices_elsewhere

    devices = [
        {"name": name, "type": "external_mqtt", "mqtt": {"broker_ref": "home", "topic_family": family, "device_id": "garage"}}
        for name, family in (("Garage", "ems_solarflow"), ("Garage plug", "ems_consumer"))
    ]

    watched = external_devices_elsewhere(devices, "other")

    assert watched == {
        "ems-solarflow/garage": ("home", "ems-solarflow/garage/+", "garage"),
        "ems-consumer/garage": ("home", "ems-consumer/garage/+", "garage"),
    }


def test_the_influx_path_reads_the_same_consumers():
    runtime = _runtime([WASHER, ("Dryer", "ems_consumer", "dryer", [("power", b"10")], "stale")])

    assert consumer_readings(_controller(runtime)) == {"washer": 450, "dryer": None}


def test_a_renamed_consumer_keeps_its_key_and_says_its_new_name(_configured):
    _configured(_entries([("Waschmaschine", "ems_consumer", "washer", [], "online")]))
    runtime = _runtime([("Waschmaschine", "ems_consumer", "washer", [("power", b"450")], "online")])

    assert _dashboard(runtime)["consumers"] == {"washer": {"name": "Waschmaschine", "online": True, "power_w": 450}}
    assert consumer_readings(_controller(runtime)) == {"washer": 450}


def _consumer(name, device_id, broker="home", **extra):
    return {
        "name": name,
        "type": "external_mqtt",
        "mqtt": {"broker_ref": broker, "topic_family": "ems_consumer", "device_id": device_id},
        **extra,
    }


def test_a_history_id_records_the_consumer_under_another_key():
    assert consumer_history_key(_consumer("Washer", "washer")) == "washer"
    assert consumer_history_key(_consumer("Washer", "washer", history_id="washer-2")) == "washer-2"
    assert consumer_history_key({**_consumer("Inverter", "inv"), "mqtt": {"topic_family": "ems_solarflow", "device_id": "inv"}}) is None


def test_two_consumers_given_one_key_never_share_a_history():
    keys = consumer_history_keys([
        _consumer("Washer", "plug"),
        _consumer("Dryer", "dryer-plug", broker="cellar", history_id="plug"),
        {"name": "WR1"},
    ])

    assert keys == {"Washer": "plug", "Dryer": "plug@cellar"}


def test_a_disabled_consumer_holds_no_key_and_no_name():
    keys = consumer_history_keys([
        _consumer("Washer", "plug", enabled=False),
        _consumer("Washer", "other-plug"),
        _consumer("Dryer", "plug"),
    ])

    assert keys == {"Washer": "other-plug", "Dryer": "plug"}


@pytest.mark.parametrize(
    ("entry", "refused"),
    [
        (_consumer("Washer", "washer", history_id="washer-2"), False),
        (_consumer("Washer", "washer", history_id="plug@cellar"), True),
        (_consumer("Washer", "washer", history_id="washer 2"), True),
        (_consumer("Washer", "washer", history_id=""), True),
        ({**_consumer("WR", "wr", history_id="wr"), "mqtt": {"topic_family": "ems_solarflow", "device_id": "wr"}}, True),
    ],
)
def test_a_history_id_is_a_device_id_on_a_consumer_only(entry, refused):
    codes = {issue["code"] for issue in validate_external_mqtt_device_config(entry)}

    assert ("external_mqtt_history_id_invalid" in codes) is refused


def test_a_mapped_consumer_publishes_its_power_on_its_own_tree():
    mappings, issues = parse_mappings({
        "schema_version": 1,
        "devices": [{
            "id": "washer",
            "family": "ems_consumer",
            "broker_ref": "ems-mqtt",
            "values": {"power": {"sources": [{"topic": "plug/status", "format": "json", "path": "apower"}]}},
        }],
    })
    assert issues == []
    (device,) = mappings.devices

    published = evaluate_device(device, {"plug/status": Received(b'{"apower": 612.3}', at=0.0)}, now=1.0)
    refused = evaluate_device(device, {"plug/status": Received(b'{"apower": -5}', at=0.0)}, now=1.0)

    assert device.output_topic == "ems-consumer/washer/state"
    assert published.state == {"power": 612.3}
    assert refused.state is None and refused.reason == "catalog_refused"


@pytest.mark.parametrize(
    ("home", "grid", "share"),
    [(1000, 400, 0.4), (1000, 1500, 1.0), (1000, -200, 0.0), (0, 50, 1.0), (0, 0, 0.0)],
)
def test_the_grid_share_of_the_house(home, grid, share):
    assert home_grid_share({"home_load_w": home, "grid_power_w": grid}) == pytest.approx(share)


def _sample(timestamp, consumers, output=600, grid=400):
    return {
        "timestamp": timestamp,
        "devices": {},
        "grid_power_w": grid,
        "home_load_w": output + grid,
        "pv_total_w": output,
        "inverter_output_w": output,
        "inverter_charge_w": 0,
        "battery_power_w": 0,
        "average_soc": 0,
        "consumers": consumers,
        "consumer_total_w": sum(item["power_w"] or 0 for item in consumers.values()),
        "home_other_w": max(0, output + grid - sum(item["power_w"] or 0 for item in consumers.values())),
        "controller": {"commanded_total_w": 0},
        "rules": {},
        "control_explain": None,
    }


def test_a_consumer_is_priced_by_the_grid_share_of_the_house(tmp_path):
    store = DashboardStore(
        tmp_path / "dashboard.sqlite",
        energy_savings={"enabled": True, "price_per_kwh": 0.3, "max_sample_delta_seconds": 3600},
    )
    first = datetime(2026, 6, 1, 8, 0, tzinfo=timezone.utc)
    second = first + timedelta(hours=1)
    consumers = {
        "washer": {"name": "Washer", "online": True, "power_w": 600},
        "dryer": {"name": "Dryer", "online": False, "power_w": None},
    }

    store.record(_sample(first.isoformat(), consumers))
    store.record(_sample(second.isoformat(), consumers))
    summary = store.energy_summary(now=second.isoformat())

    assert summary["today"]["consumers"] == {
        "washer": {
            "name": "Washer", "last_date": "2026-06-01",
            "kwh": 0.6, "grid_kwh": 0.24, "own_kwh": 0.36, "cost": 0.072, "saved": 0.108,
        }
    }
    assert summary["lifetime"]["consumers"]["washer"]["kwh"] == 0.6
    assert summary["yesterday"]["consumers"] == {}


def test_a_renamed_consumer_keeps_what_it_drew_under_its_new_name(tmp_path):
    path = tmp_path / "dashboard.sqlite"
    store = DashboardStore(path, energy_savings={"enabled": True, "max_sample_delta_seconds": 86400 * 3})
    day = datetime(2026, 6, 1, 8, 0, tzinfo=timezone.utc)
    samples = [
        (day, "Washer", "washer"),
        (day + timedelta(hours=1), "Washer", "washer"),
        (day + timedelta(days=1), "Waschmaschine", "washer"),
        (day + timedelta(days=1, hours=1), "Freezer", "freezer"),
    ]
    for at, name, key in samples:
        store.record(_sample(at.isoformat(), {key: {"name": name, "online": True, "power_w": 100}}))

    lifetime = store.energy_summary(now=(day + timedelta(days=1, hours=1)).isoformat())["lifetime"]["consumers"]

    assert {key: (value["name"], value["last_date"]) for key, value in lifetime.items()} == {
        "washer": ("Waschmaschine", "2026-06-02"),
        "freezer": ("Freezer", "2026-06-02"),
    }
    assert lifetime["washer"]["kwh"] == pytest.approx(0.1 + 2.3, abs=0.01)
    assert consumer_histories(str(path)) == {
        "washer": {"name": "Waschmaschine", "last_date": "2026-06-02", "kwh": lifetime["washer"]["kwh"]},
        "freezer": {"name": "Freezer", "last_date": "2026-06-02", "kwh": lifetime["freezer"]["kwh"]},
    }


def test_each_month_and_year_names_what_each_consumer_drew_and_the_peak_day_is_theirs(tmp_path):
    store = DashboardStore(
        tmp_path / "dashboard.sqlite",
        energy_savings={"enabled": True, "price_per_kwh": 0.3, "max_sample_delta_seconds": 86400 * 40},
    )
    samples = [
        (datetime(2025, 12, 20, 8, tzinfo=timezone.utc), {"washer": 100}),
        (datetime(2025, 12, 20, 9, tzinfo=timezone.utc), {"washer": 100}),
        (datetime(2026, 1, 10, 8, tzinfo=timezone.utc), {"washer": 0, "dryer": 0}),
        (datetime(2026, 1, 10, 10, tzinfo=timezone.utc), {"washer": 500, "dryer": 1000}),
        (datetime(2026, 2, 3, 8, tzinfo=timezone.utc), {"washer": 0}),
        (datetime(2026, 2, 3, 9, tzinfo=timezone.utc), {"washer": 200}),
    ]
    for at, powers in samples:
        store.record(_sample(at.isoformat(), {key: {"name": key.title(), "online": True, "power_w": watts} for key, watts in powers.items()}))

    summary = store.energy_summary(now=datetime(2026, 2, 3, 12, tzinfo=timezone.utc).isoformat())
    months = {item["month"]: item["consumers"] for item in summary["monthly_current_year"]}
    years = {item["year"]: item["consumers"] for item in summary["yearly"]}

    assert {key: value["kwh"] for key, value in months[1].items()} == {"washer": 1.0, "dryer": 2.0}
    assert {key: value["kwh"] for key, value in months[2].items()} == {"washer": 0.2}
    assert months[3] == {}
    assert {key: value["kwh"] for key, value in years[2025].items()} == {"washer": 0.1}
    assert years[2026]["washer"]["kwh"] == pytest.approx(1.2)
    assert summary["consumer_peak_day"]["date"] == "2026-01-10"
    assert set(summary["consumer_peak_day"]["consumers"]) == {"washer", "dryer"}


def test_the_months_a_summary_keeps_follow_a_new_month_a_restored_file_and_a_late_sample(tmp_path):
    import sqlite3

    path = tmp_path / "dashboard.sqlite"
    store = DashboardStore(path, energy_savings={"enabled": True, "max_sample_delta_seconds": 86400 * 40})
    for at, watts in ((datetime(2026, 1, 10, 8, tzinfo=timezone.utc), 0), (datetime(2026, 1, 10, 9, tzinfo=timezone.utc), 100)):
        store.record(_sample(at.isoformat(), {"washer": {"name": "Washer", "online": True, "power_w": watts}}))
    january = store.energy_summary(now=datetime(2026, 1, 20, tzinfo=timezone.utc).isoformat())
    store.record(_sample(datetime(2026, 2, 2, 9, tzinfo=timezone.utc).isoformat(), {"washer": {"name": "Waschmaschine", "online": True, "power_w": 0}}))
    store.record(_sample(datetime(2026, 2, 2, 10, tzinfo=timezone.utc).isoformat(), {"washer": {"name": "Waschmaschine", "online": True, "power_w": 200}}))
    february = store.energy_summary(now=datetime(2026, 2, 3, tzinfo=timezone.utc).isoformat())

    with store.maintenance_pause():
        con = sqlite3.connect(path)
        con.execute("UPDATE daily_consumer_energy SET energy_wh = 500 WHERE date = '2026-01-10'")
        con.commit()
        con.close()
    restored = store.energy_summary(now=datetime(2026, 2, 3, tzinfo=timezone.utc).isoformat())

    con = sqlite3.connect(path)
    con.execute(
        "INSERT INTO daily_consumer_energy(date, consumer, name, energy_wh, updated_at) VALUES('2026-01-11', 'dryer', 'Dryer', 700, 'x')"
    )
    con.commit()
    con.close()
    written_elsewhere = store.energy_summary(now=datetime(2026, 2, 3, tzinfo=timezone.utc).isoformat())
    later = tmp_path / "later.sqlite"
    store = DashboardStore(later, energy_savings={"enabled": True, "timezone": "UTC"})
    for second, watts in ((40, 0), (50, 1000)):
        store.record(_sample(datetime(2026, 1, 31, 23, 59, second, tzinfo=timezone.utc).isoformat(), {"washer": {"name": "Washer", "online": True, "power_w": watts}}))
    store.energy_summary(now=datetime(2026, 2, 1, 0, 0, 1, tzinfo=timezone.utc).isoformat())
    store.record(_sample(datetime(2026, 1, 31, 23, 59, 59, tzinfo=timezone.utc).isoformat(), {"washer": {"name": "Washer", "online": True, "power_w": 4000}}))
    late = store.energy_summary(now=datetime(2026, 2, 1, 0, 0, 2, tzinfo=timezone.utc).isoformat())

    assert january["lifetime"]["consumers"]["washer"]["kwh"] == 0.1
    assert [item["consumers"].get("washer", {}).get("kwh") for item in february["monthly_current_year"][:2]] == [0.1, 0.2]
    assert february["lifetime"]["consumers"]["washer"] == {**february["lifetime"]["consumers"]["washer"], "name": "Waschmaschine", "kwh": 0.3}
    assert restored["monthly_current_year"][0]["consumers"]["washer"]["kwh"] == 0.5
    assert written_elsewhere["monthly_current_year"][0]["consumers"]["dryer"]["kwh"] == 0.7
    assert late["monthly_current_year"][0]["consumers"]["washer"]["kwh"] == 0.013


def test_a_consumer_table_without_names_gains_the_column_and_keeps_its_values(tmp_path):
    import sqlite3

    path = tmp_path / "dashboard.sqlite"
    con = sqlite3.connect(path)
    con.execute(
        "CREATE TABLE daily_consumer_energy (date TEXT NOT NULL, consumer TEXT NOT NULL, "
        "energy_wh REAL NOT NULL DEFAULT 0, grid_wh REAL NOT NULL DEFAULT 0, cost_value REAL NOT NULL DEFAULT 0, "
        "savings_value REAL NOT NULL DEFAULT 0, updated_at TEXT NOT NULL, PRIMARY KEY(date, consumer))"
    )
    con.execute("INSERT INTO daily_consumer_energy VALUES ('2026-06-01', 'washer', 500, 0, 0, 0, 'x')")
    con.commit()
    con.close()

    store = DashboardStore(path, energy_savings={"enabled": True})
    start = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(minutes=1)
    for second in (0, 10):
        store.record(_sample((start + timedelta(seconds=second)).isoformat(), {"washer": {"name": "Washer", "online": True, "power_w": 360}}))

    assert consumer_histories(str(path))["washer"]["name"] == "Washer"
    assert consumer_histories(str(path))["washer"]["kwh"] == pytest.approx(0.501, abs=0.001)


def test_a_database_that_is_not_there_has_no_consumer_history(tmp_path):
    assert consumer_histories(str(tmp_path / "missing.sqlite")) == {}
    assert consumer_histories("") == {}
    (tmp_path / "empty.sqlite").write_bytes(b"")
    assert consumer_histories(str(tmp_path / "empty.sqlite")) == {}


def test_a_consumer_draws_its_own_line_from_the_history(tmp_path):
    path = tmp_path / "dashboard.sqlite"
    store = DashboardStore(path, energy_savings={"enabled": True})
    start = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(minutes=10)
    for minute, power in enumerate((100, 200, None)):
        consumers = {"Wash machine": {"name": "Wash machine", "online": power is not None, "power_w": power}}
        store.record(_sample((start + timedelta(minutes=minute)).isoformat(), consumers))

    result = SqliteHistoryProvider(str(path)).query(
        start, start + timedelta(minutes=5), series=["consumer:Wash machine", "home_other", "consumers"]
    )

    assert result.series["consumer:Wash machine"] == [100, 200, None]
    assert result.series["consumers"] == [100, 200, 0]
    assert result.series["home_other"] == [900, 800, 1000]


def test_a_consumer_series_names_its_consumer_percent_encoded():
    assert consumer_of_series("consumer:Heat%20pump%2C%20cellar") == "Heat pump, cellar"
    assert consumer_of_series('consumer:12%22%20fan') == '12" fan'
    assert consumer_of_series("consumer:") is None
    assert consumer_of_series("consumer:" + "x" * 257) is None
    assert normalize_series(["consumer:Washer", "home_other", "consumer:Washer"]) == ["consumer:Washer", "home_other"]


def test_any_consumer_name_draws_its_line(tmp_path):
    path = tmp_path / "dashboard.sqlite"
    store = DashboardStore(path, energy_savings={"enabled": True})
    start = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(minutes=10)
    names = ['12" fan', "Heat pump, cellar", "a.b[0]", "back\\slash"]
    store.record(_sample(start.isoformat(), {name: {"online": True, "power_w": 10 + index} for index, name in enumerate(names)}))

    ids = ["consumer:" + quote(name, safe="") for name in names]
    result = SqliteHistoryProvider(str(path)).query(start, start + timedelta(minutes=5), series=ids)

    assert [result.series[series_id] for series_id in ids] == [[10], [11], [12], [13]]


def test_influx_carries_each_consumer_and_the_unmeasured_rest():
    lines = build_telemetry_lines(
        [], [], {}, 400, timestamp_ns=1, consumers={"Washer": 300.0, "Dryer": None}
    )

    assert "consumer,device=Washer,source=mqtt available=true,power=300.0 1" in lines
    assert "consumer,device=Dryer,source=mqtt available=false 1" in lines
    meter = next(line for line in lines if line.startswith("shelly_meter"))
    assert "house_load=400.0" in meter and "house_other=100.0" in meter


class _Client:
    def __init__(self):
        self.queries = []

    def query_raw(self, flux):
        self.queries.append(flux)
        return ""


def test_influx_reads_one_consumer_by_its_device_tag():
    client = _Client()
    provider = InfluxHistoryProvider(
        {"enabled": True, "bucket_prefix": "ems", "query_profiles": []}, client=client
    )
    start = datetime(2026, 6, 1, 8, 0, tzinfo=timezone.utc)

    provider.query(start, start + timedelta(hours=1), devices=["WR1"], series=["consumer:Washer", "home_other"])

    consumer, other = client.queries
    assert 'r._measurement == "consumer"' in consumer and 'r.device == "Washer"' in consumer
    assert "WR1" not in consumer
    assert 'r._field == "house_other"' in other
