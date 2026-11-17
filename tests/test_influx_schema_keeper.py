# SPDX-FileCopyrightText: 2026 basecubedev
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The EMS keeps the analytics schema in place itself (ems.history.schema_keeper).

A fresh appliance install starts InfluxDB and the EMS together and never runs
``influx sync``: InfluxDB's own setup creates the raw bucket, and the
downsampled buckets and their tasks are never made. These tests hold the EMS to
making them, to asking again until InfluxDB answers, and to noticing later when
one goes missing.
"""

import contextlib
import logging
import re
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from ems.config import normalize_influxdb_config
from ems.file_io import FileLockTimeout
from ems.history import schema, schema_keeper
from ems.history.confirmed_retention import confirm_change, confirmations, confirmed_path
from ems.history.schema_keeper import (
    drop_replaced_confirmations,
    RECHECK_SECONDS,
    RETRY_SECONDS,
    InfluxSchemaKeeper,
    start_schema_keeper,
)
from tests.test_history_schema import FakeInfluxClient

pytestmark = [
    pytest.mark.unit,
]

ROOT = Path(__file__).resolve().parents[1]
CONFIG = normalize_influxdb_config({"enabled": True, "bucket_prefix": "ems"})
DAY = 86400


def fresh_install():
    """What InfluxDB's setup mode leaves: the raw bucket, kept forever, nothing else."""

    client = FakeInfluxClient()
    client.buckets["ems_raw"] = 0
    return client


class Unreachable(FakeInfluxClient):
    """An InfluxDB that does not answer until ``up`` is set."""

    def __init__(self):
        super().__init__()
        self.up = False

    def get_org_id(self):
        if not self.up:
            raise ConnectionError("connection refused")
        return super().get_org_id()


def refusal(status):
    error = OSError(f"HTTP {status}")
    error.response = SimpleNamespace(status_code=status)
    return error


class Refusing(FakeInfluxClient):
    def get_org_id(self):
        raise refusal(403)


def no_lock(timeout):
    return contextlib.nullcontext()


def keeper_for(client, config=CONFIG, lock=no_lock, confirmed=None):
    return InfluxSchemaKeeper(
        config, client_factory=lambda: client, lock=lock, confirmed_path=confirmed
    )


def confirmed_retentions(path):
    return {name: entry.retention_seconds for name, entry in confirmations(path).items()}


def confirm_raw(path, before_days, after_days, moment=1000.0):
    """What the Admin records when the raw retention is changed and confirmed at ``moment``."""

    confirm_change(
        path,
        {"influxdb": {"enabled": True, "retention": {"raw_days": before_days}}},
        {"influxdb": {"enabled": True, "retention": {"raw_days": after_days}}},
        now=lambda: moment,
    )


def test_a_fresh_install_gets_its_downsampled_buckets_and_tasks_without_anyone_asking():
    client = fresh_install()

    delay = keeper_for(client).reconcile()

    assert sorted(client.buckets) == ["ems_1h", "ems_1m", "ems_5m", "ems_raw"]
    assert sorted(task["name"] for task in client.tasks.values()) == [
        "ems-downsample-1h",
        "ems-downsample-1h:cells",
        "ems-downsample-1m",
        "ems-downsample-1m:cells",
        "ems-downsample-5m",
        "ems-downsample-5m:cells",
    ]
    assert all(task["status"] == "active" for task in client.tasks.values())
    assert client.buckets["ems_raw"] == 14 * DAY
    assert delay == RECHECK_SECONDS


def test_an_influxdb_still_starting_is_asked_again_and_again_until_it_answers():
    client = Unreachable()
    keeper = keeper_for(client)

    delays = [keeper.reconcile() for _ in range(12)]

    assert delays[: len(RETRY_SECONDS)] == list(RETRY_SECONDS)
    assert set(delays[len(RETRY_SECONDS):]) == {RETRY_SECONDS[-1]}
    assert client.buckets == {}

    client.up = True
    assert keeper.reconcile() == RECHECK_SECONDS
    assert "ems_1h" in client.buckets
    assert keeper.reconcile() == RECHECK_SECONDS


def test_after_a_success_the_retry_ladder_starts_from_the_bottom_again():
    client = Unreachable()
    keeper = keeper_for(client)
    keeper.reconcile()
    keeper.reconcile()
    client.up = True
    keeper.reconcile()

    client.up = False

    assert keeper.reconcile() == RETRY_SECONDS[0]


def test_a_bucket_that_goes_missing_later_comes_back_on_the_next_round():
    client = fresh_install()
    keeper = keeper_for(client)
    keeper.reconcile()

    del client.buckets["ems_5m"]
    client.tasks.clear()
    keeper.reconcile()

    assert "ems_5m" in client.buckets
    assert len(client.tasks) == 6


def test_a_schema_in_place_is_left_alone_and_nothing_is_logged(caplog):
    client = fresh_install()
    keeper = keeper_for(client)
    keeper.reconcile()
    client.calls.clear()

    with caplog.at_level(logging.INFO):
        keeper.reconcile()

    assert client.calls == []
    assert "influx_schema" not in caplog.text


def test_what_the_keeper_created_is_logged_once(caplog):
    with caplog.at_level(logging.INFO):
        keeper_for(fresh_install()).reconcile()

    assert "event=influx_schema_synced" in caplog.text
    assert "created_buckets=ems_1m,ems_5m,ems_1h" in caplog.text
    assert (
        "created_tasks=ems-downsample-1m,ems-downsample-1m:cells,ems-downsample-5m,"
        "ems-downsample-5m:cells,ems-downsample-1h,ems-downsample-1h:cells"
    ) in caplog.text
    assert "updated_buckets=ems_raw" in caplog.text


def test_the_first_failures_while_influxdb_starts_are_not_warnings(caplog):
    keeper = keeper_for(Unreachable())

    with caplog.at_level(logging.INFO):
        for _ in range(schema_keeper.QUIET_FAILURES):
            keeper.reconcile()
    quiet = [record for record in caplog.records if "influx_schema_sync_failed" in record.getMessage()]
    assert quiet and all(record.levelno == logging.INFO for record in quiet)

    caplog.clear()
    with caplog.at_level(logging.INFO):
        keeper.reconcile()
    loud = [record for record in caplog.records if "influx_schema_sync_failed" in record.getMessage()]
    assert [record.levelno for record in loud] == [logging.WARNING]
    assert "retry_in_s=" in loud[0].getMessage()


def test_a_long_outage_is_not_logged_every_round(caplog):
    keeper = keeper_for(Unreachable())

    with caplog.at_level(logging.INFO):
        for _ in range(60):
            keeper.reconcile()

    lines = [record for record in caplog.records if "influx_schema_sync_failed" in record.getMessage()]
    assert 5 < len(lines) < 20


def test_a_refusing_token_is_named_once_and_asked_again_only_rarely(caplog):
    keeper = keeper_for(Refusing())

    with caplog.at_level(logging.INFO):
        delays = [keeper.reconcile() for _ in range(4)]

    assert caplog.text.count("event=influx_schema_sync_not_permitted") == 1
    assert "status=403" in caplog.text
    assert "write access" in caplog.text
    assert "influx_schema_sync_failed" not in caplog.text
    assert set(delays) == {RETRY_SECONDS[-1]}


def test_an_unknown_token_is_told_apart_from_a_token_without_rights(caplog):
    class Unknown(FakeInfluxClient):
        def get_org_id(self):
            raise refusal(401)

    with caplog.at_level(logging.INFO):
        keeper_for(Unknown()).reconcile()

    assert "status=401" in caplog.text
    assert "does not accept the token" in caplog.text


def test_a_refusal_is_not_repeated_after_an_outage_in_between(caplog):
    class Flaky(FakeInfluxClient):
        answer = "refuse"

        def get_org_id(self):
            if self.answer == "refuse":
                raise refusal(403)
            raise ConnectionError("connection refused")

    client = Flaky()
    keeper = keeper_for(client)
    with caplog.at_level(logging.INFO):
        keeper.reconcile()
        client.answer = "down"
        keeper.reconcile()
        client.answer = "refuse"
        keeper.reconcile()

    assert caplog.text.count("event=influx_schema_sync_not_permitted") == 1


def test_months_of_raw_telemetry_are_never_deleted_by_the_keeper(caplog):
    """The raw bucket InfluxDB's setup made keeps data forever. On an appliance
    that never had its schema synced it is the only history there is, and the
    downsampling tasks only aggregate forward from the moment they exist."""

    client = fresh_install()
    client.old_points.add("ems_raw")
    keeper = keeper_for(client)

    with caplog.at_level(logging.INFO):
        keeper.reconcile()
        keeper.reconcile()

    assert client.buckets["ems_raw"] == 0
    assert "ems_1h" in client.buckets
    assert caplog.text.count("event=influx_schema_retention_kept") == 1
    assert "bucket=ems_raw" in caplog.text


def test_a_task_switched_off_by_hand_stays_off_and_is_named_once(caplog):
    client = fresh_install()
    keeper = keeper_for(client)
    keeper.reconcile()
    task = next(iter(client.tasks.values()))
    task["status"] = "inactive"

    with caplog.at_level(logging.INFO):
        keeper.reconcile()
        keeper.reconcile()

    assert task["status"] == "inactive"
    assert len(client.tasks) == 6
    assert caplog.text.count("event=influx_schema_task_kept") == 1
    assert f"tasks={task['name']}" in caplog.text


def test_a_bucket_found_holding_older_data_is_not_probed_again():
    """The probe is a query over the bucket's history; its answer cannot change
    while the retention stays, so the keeper asks once per process."""

    client = fresh_install()
    client.old_points.add("ems_raw")
    keeper = keeper_for(client)
    keeper.reconcile()
    keeper.reconcile()
    keeper.reconcile()

    probes = [call for call in client.calls if call[0] == "has_points_before"]
    assert probes == [("has_points_before", "ems_raw", 14 * DAY)]


def test_a_retention_confirmed_in_the_admin_is_applied_over_older_data(tmp_path, caplog):
    """The operator set the raw retention in the Admin and confirmed it; the
    EMS restarted with those settings applies it, older data and all."""

    client = fresh_install()
    client.old_points.add("ems_raw")
    path = confirmed_path(tmp_path)
    confirm_raw(path, 30, 14)

    with caplog.at_level(logging.INFO):
        keeper_for(client, confirmed=path).reconcile()

    assert client.buckets["ems_raw"] == 14 * DAY
    assert not path.exists()
    assert "event=influx_schema_confirmed_retention_applied" in caplog.text
    assert "bucket=ems_raw" in caplog.text
    assert "updated_now=True" in caplog.text
    assert "event=influx_schema_retention_kept" not in caplog.text


def test_a_confirmed_retention_applied_in_a_round_that_failed_later_is_still_named(tmp_path, caplog):
    """The update went through, then a later lookup timed out and the round
    failed; the next round finds the bucket carrying the retention and must
    still say that the confirmed retention was applied."""

    class FailingOnce(FakeInfluxClient):
        failed = False

        def find_bucket(self, name):
            if name == "ems_1m" and not self.failed:
                self.failed = True
                raise TimeoutError("lookup timed out")
            return super().find_bucket(name)

    client = FailingOnce()
    client.buckets["ems_raw"] = 0
    client.old_points.add("ems_raw")
    path = confirmed_path(tmp_path)
    confirm_raw(path, 30, 14)
    keeper = keeper_for(client, confirmed=path)

    with caplog.at_level(logging.INFO):
        keeper.reconcile()
        keeper.reconcile()

    assert client.buckets["ems_raw"] == 14 * DAY
    assert not path.exists()
    assert "event=influx_schema_confirmed_retention_applied" in caplog.text
    assert "updated_now=False" in caplog.text


def test_a_confirmation_for_the_settings_the_ems_restarts_with_waits_for_it(tmp_path):
    """Protected interleaving: the Admin records the confirmation when it saves
    the settings, after the EMS read the old ones -- long before, or while it
    was still starting. Neither the start nor the keeper touch the entry; the
    keeper keeps the safe rule for its own retention."""

    client = fresh_install()
    client.old_points.add("ems_raw")
    path = confirmed_path(tmp_path)
    confirm_raw(path, 14, 7, moment=3000.0)

    drop_replaced_confirmations(CONFIG, 2000.0, path=path)
    keeper_for(client, confirmed=path).reconcile()

    assert client.buckets["ems_raw"] == 0
    assert confirmed_retentions(path) == {"ems_raw": 7 * DAY}


def test_a_confirmation_replaced_before_the_ems_read_its_settings_is_dropped_at_start(tmp_path, caplog):
    """Raw shortened to 7 days and confirmed, then the backup with 14 days
    restored before the restart. The entry must not wait for a 7 that some
    later hand sets without confirming it."""

    path = confirmed_path(tmp_path)
    confirm_raw(path, 14, 7, moment=1000.0)

    with caplog.at_level(logging.INFO):
        drop_replaced_confirmations(CONFIG, 2000.0, path=path)

    assert not path.exists()
    assert "event=influx_schema_confirmed_retention_dropped" in caplog.text
    assert "bucket=ems_raw" in caplog.text


@pytest.mark.parametrize(
    "settings",
    [
        None,
        {**CONFIG, "enabled": False},
        {**CONFIG, "auto_sync": False},
        {**CONFIG, "url": "http://elsewhere:8086"},
        {**CONFIG, "host_url": "http://127.0.0.1:9086"},
        {**CONFIG, "mode": "external" if CONFIG["mode"] != "external" else "bundled"},
        {**CONFIG, "org": "other"},
        normalize_influxdb_config({"enabled": True, "bucket_prefix": "home"}),
    ],
    ids=[
        "no-influxdb-block",
        "analytics-off",
        "auto-sync-off",
        "other-url",
        "other-host-url",
        "other-mode",
        "other-org",
        "other-prefix",
    ],
)
def test_settings_that_do_not_run_the_confirmed_schema_drop_it_at_start(tmp_path, settings):
    """Analytics off, the schema managed by hand, another InfluxDB: the EMS that
    starts with such settings has not run what was confirmed, and a later start
    with the confirmed value again must not find the entry waiting."""

    path = confirmed_path(tmp_path)
    confirm_raw(path, 30, 14, moment=1000.0)

    drop_replaced_confirmations(settings, 2000.0, path=path)

    assert not path.exists()


def test_the_start_keeps_a_confirmation_its_settings_ask_for(tmp_path):
    path = confirmed_path(tmp_path)
    confirm_raw(path, 30, 14, moment=1000.0)

    drop_replaced_confirmations(CONFIG, 2000.0, path=path)

    assert confirmed_retentions(path) == {"ems_raw": 14 * DAY}


def test_a_confirmation_for_another_influxdb_is_not_applied(tmp_path):
    client = fresh_install()
    client.old_points.add("ems_raw")
    path = confirmed_path(tmp_path)
    confirm_raw(path, 30, 14)

    keeper_for(client, config={**CONFIG, "url": "http://elsewhere:8086"}, confirmed=path).reconcile()

    assert client.buckets["ems_raw"] == 0


def test_a_start_that_cannot_read_or_clean_the_record_never_raises(tmp_path, caplog, monkeypatch):
    path = confirmed_path(tmp_path)
    confirm_raw(path, 30, 14, moment=1000.0)

    def refused(*args):
        raise PermissionError("read-only data directory")

    monkeypatch.setattr(schema_keeper.confirmed_retention, "drop_replaced", refused)
    with caplog.at_level(logging.INFO):
        drop_replaced_confirmations(CONFIG, 2000.0, path=path)

    assert "event=influx_schema_confirmed_retention_unsettled" in caplog.text


def test_a_start_whose_settings_were_never_read_drops_nothing(tmp_path):
    path = confirmed_path(tmp_path)
    confirm_raw(path, 30, 14, moment=1000.0)

    drop_replaced_confirmations(None, None, path=path)

    assert confirmed_retentions(path) == {"ems_raw": 14 * DAY}


def test_a_settlement_that_fails_unexpectedly_never_ends_the_keeper(tmp_path, caplog, monkeypatch):
    client = fresh_install()
    path = confirmed_path(tmp_path)
    confirm_raw(path, 30, 14)
    keeper = keeper_for(client, confirmed=path)

    def broken(report, entries):
        raise RuntimeError("unexpected")

    monkeypatch.setattr(keeper, "_settle_confirmed", broken)
    with caplog.at_level(logging.INFO):
        delay = keeper.reconcile()

    assert delay == RECHECK_SECONDS
    assert "event=influx_schema_confirmed_retention_unsettled" in caplog.text


def test_a_confirmed_retention_influxdb_refuses_is_named_once_and_kept_for_later(tmp_path, caplog):
    client = fresh_install()
    client.old_points.add("ems_raw")
    path = confirmed_path(tmp_path)
    confirm_raw(path, 30, 14)

    def refusing(bucket_id, retention_seconds):
        error = OSError("422 shard-group duration")
        error.response = SimpleNamespace(status_code=422)
        raise error

    client.update_bucket_retention = refusing
    keeper = keeper_for(client, confirmed=path)
    with caplog.at_level(logging.INFO):
        assert keeper.reconcile() == RECHECK_SECONDS
        keeper.reconcile()

    assert caplog.text.count("event=influx_schema_confirmed_retention_refused") == 1
    assert "ems_1h" in client.buckets
    assert confirmed_retentions(path) == {"ems_raw": 14 * DAY}


def test_a_refusal_is_named_for_a_bucket_found_holding_older_data_before(tmp_path, caplog):
    client = fresh_install()
    client.old_points.add("ems_raw")
    keeper = keeper_for(client, confirmed=confirmed_path(tmp_path))
    keeper.reconcile()
    confirm_raw(confirmed_path(tmp_path), 30, 14)

    def refusing(bucket_id, retention_seconds):
        error = OSError("422 shard-group duration")
        error.response = SimpleNamespace(status_code=422)
        raise error

    client.update_bucket_retention = refusing
    with caplog.at_level(logging.INFO):
        keeper.reconcile()

    assert "event=influx_schema_confirmed_retention_refused" in caplog.text


def test_a_confirmation_the_bucket_already_carries_is_settled(tmp_path):
    client = FakeInfluxClient()
    schema.sync(client, CONFIG)
    path = confirmed_path(tmp_path)
    confirm_raw(path, 30, 14)

    keeper_for(client, confirmed=path).reconcile()

    assert not path.exists()


def test_a_record_that_cannot_be_read_keeps_the_safe_rule(tmp_path, caplog):
    client = fresh_install()
    client.old_points.add("ems_raw")
    path = tmp_path / "unreadable"
    path.mkdir()

    with caplog.at_level(logging.INFO):
        delay = keeper_for(client, confirmed=path).reconcile()

    assert delay == RECHECK_SECONDS
    assert client.buckets["ems_raw"] == 0
    assert "event=influx_schema_retention_kept" in caplog.text


def test_a_confirmation_that_cannot_be_settled_is_said_once_and_the_rounds_go_on(tmp_path, caplog, monkeypatch):
    client = fresh_install()
    client.old_points.add("ems_raw")
    path = confirmed_path(tmp_path)
    confirm_raw(path, 30, 14)

    def refused(target, done):
        raise PermissionError("read-only data directory")

    monkeypatch.setattr(schema_keeper.confirmed_retention, "discard", refused)
    keeper = keeper_for(client, confirmed=path)
    with caplog.at_level(logging.INFO):
        delay = keeper.reconcile()
        keeper.reconcile()

    assert delay == RECHECK_SECONDS
    assert client.buckets["ems_raw"] == 14 * DAY
    assert caplog.text.count("event=influx_schema_confirmed_retention_unsettled") == 1
    assert caplog.text.count("event=influx_schema_confirmed_retention_applied") == 1


def test_the_ems_keeper_reads_confirmations_from_the_data_directory(isolated_install_root, monkeypatch):
    monkeypatch.setattr(InfluxSchemaKeeper, "start", lambda self: None)

    keeper = start_schema_keeper(CONFIG)

    assert keeper.confirmed_path == isolated_install_root / "data" / "influx-retention-confirmed.json"


def test_the_moment_the_settings_were_read_is_taken_before_reading_them(tmp_path, monkeypatch):
    import time

    from ems import config as cfg

    seen = {}
    real_read = cfg._read_raw_config

    def reading(path):
        seen["read_at"] = cfg.SETTINGS_READ_AT
        seen["reading_at"] = time.time()
        return real_read(path)

    (tmp_path / "config.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(cfg, "_read_raw_config", reading)
    monkeypatch.setattr(cfg, "SETTINGS_READ_AT", None)
    monkeypatch.setattr(cfg, "LOADED_DEVICES", [])
    args = SimpleNamespace(config=str(tmp_path / "config.json"), simulate=False, replay=None, self_test=False)
    before = time.time()

    try:
        cfg.load_config(args, base_dir=str(tmp_path))
    except SystemExit:
        pass

    assert before <= seen["read_at"] <= seen["reading_at"]


def test_the_sync_runs_inside_the_schema_lock():
    held = {"now": False, "during_sync": None}

    @contextlib.contextmanager
    def recording(timeout):
        assert timeout == 0
        held["now"] = True
        try:
            yield
        finally:
            held["now"] = False

    class Watching(FakeInfluxClient):
        def get_org_id(self):
            held["during_sync"] = held["now"]
            return super().get_org_id()

    keeper_for(Watching(), lock=recording).reconcile()

    assert held["during_sync"] is True


def test_a_sync_already_running_elsewhere_is_left_to_finish():
    """Protected interleaving: an `emsctl influx sync` holds the schema lock when
    the keeper's round comes. The keeper does not wait and does not write; it
    asks again shortly, and that round is not counted as a failure."""

    def busy(timeout):
        raise FileLockTimeout("held by another process")

    client = fresh_install()
    keeper = keeper_for(client, lock=busy)

    assert keeper.reconcile() == RETRY_SECONDS[0]
    assert keeper.reconcile() == RETRY_SECONDS[0]
    assert client.calls == []
    assert sorted(client.buckets) == ["ems_raw"]


def test_no_usable_token_is_a_retry_not_a_crash():
    keeper = InfluxSchemaKeeper(CONFIG, client_factory=lambda: None)

    assert keeper.reconcile() == RETRY_SECONDS[0]


def test_a_client_that_cannot_be_built_is_a_retry_not_a_crash():
    def broken():
        raise RuntimeError("bad url")

    keeper = InfluxSchemaKeeper(CONFIG, client_factory=broken)

    assert keeper.reconcile() == RETRY_SECONDS[0]


@pytest.mark.parametrize(
    "config",
    [
        None,
        {**CONFIG, "enabled": False},
        {**CONFIG, "auto_sync": False},
    ],
    ids=["no-influxdb-block", "analytics-off", "auto-sync-off"],
)
def test_no_keeper_runs_where_nothing_may_be_synced(config):
    assert start_schema_keeper(config) is None


def test_the_keeper_works_on_its_own_thread_and_stops_between_rounds():
    """Protected interleaving: the first round runs on the keeper's thread while
    the caller does not wait for it, and ``stop`` ends the wait for the next
    round instead of sleeping it out."""

    reached = threading.Event()

    class Signalling(FakeInfluxClient):
        def get_org_id(self):
            reached.set()
            return super().get_org_id()

    client = Signalling()
    keeper = InfluxSchemaKeeper(CONFIG, client_factory=lambda: client, lock=no_lock, first_attempt_s=0)
    keeper.start()
    try:
        assert reached.wait(5)
    finally:
        keeper.stop(timeout=5)

    assert not keeper.running
    assert "ems_1h" in client.buckets


def test_the_ems_starts_one_keeper_beside_the_writer_and_stops_it():
    """The keeper starts inside the writer's own gate -- analytics on, real
    hardware, no simulation, no replay -- and stops where the writer stops."""

    source = (ROOT / "ems-solarflow-api-control.py").read_text(encoding="utf-8")
    gate = source.index("        and not cfg.SIMULATION_MODE\n        and not getattr(args, \"replay\", None)\n    ):")
    end = re.search(r"\n    [^\s)]", source[gate:]).start() + gate
    block = source[gate:end]

    assert source.count("start_schema_keeper(") == 1
    assert "influx_writer.start()" in block
    assert "influx_schema_keeper = start_schema_keeper(cfg.INFLUXDB_CONFIG)" in block
    assert source.count("drop_replaced_confirmations(") == 1
    before_gate = source[source.index("    influx_schema_keeper = None\n"):gate]
    assert (
        "    if not cfg.SIMULATION_MODE and not getattr(args, \"replay\", None):\n"
        "        try:\n"
        "            from ems.history.schema_keeper import drop_replaced_confirmations\n"
        "\n"
        "            drop_replaced_confirmations(cfg.INFLUXDB_CONFIG, cfg.SETTINGS_READ_AT)\n"
    ) in before_gate
    assert (
        "        if influx_writer:\n"
        "            influx_writer.stop()\n"
        "        if influx_schema_keeper is not None:\n"
        "            influx_schema_keeper.stop()\n"
    ) in source


def test_the_schema_lock_lives_in_the_data_directory(isolated_install_root):
    assert schema_keeper.schema_lock_path() == isolated_install_root / "data" / "influx-schema.lock"


class _PatchSession:
    def __init__(self):
        self.bodies = []

    def patch(self, url, **kwargs):
        self.bodies.append(kwargs["json"])
        return SimpleNamespace(raise_for_status=lambda: None, json=lambda: {})


@pytest.mark.parametrize(
    ("days", "shard_seconds"),
    [(1, 3600), (3, DAY), (179, DAY), (180, 7 * DAY), (1825, 7 * DAY)],
)
def test_a_retention_update_asks_for_the_shard_groups_influxdb_would_pick(days, shard_seconds):
    """InfluxDB refuses a retention shorter than the bucket's shard groups, and
    the raw bucket its setup made to keep data forever has week-long ones: a
    retention below a week only goes through with shorter shard groups."""

    from ems.history.influx_client import HistoryInfluxClient

    session = _PatchSession()
    client = HistoryInfluxClient("http://influxdb:8086", "ems", "t", session=session)

    client.update_bucket_retention("b-1", days * DAY)

    assert session.bodies == [
        {
            "retentionRules": [
                {"type": "expire", "everySeconds": days * DAY, "shardGroupDurationSeconds": shard_seconds}
            ]
        }
    ]


def test_a_bucket_set_to_keep_its_data_forever_asks_for_an_expiry_of_zero():
    """InfluxDB 2.7 and 2.9 answer an empty rule list with 200 and keep the old
    retention; an expiry of 0 is what makes a bucket keep its data forever."""

    from ems.history.influx_client import HistoryInfluxClient

    session = _PatchSession()
    HistoryInfluxClient("http://influxdb:8086", "ems", "t", session=session).update_bucket_retention("b-1", 0)

    assert session.bodies == [{"retentionRules": [{"type": "expire", "everySeconds": 0}]}]


class _CsvSession:
    def __init__(self, text):
        self.text = text
        self.queries = []
        self.timeouts = []

    def post(self, url, **kwargs):
        self.queries.append(kwargs["json"]["query"])
        self.timeouts.append(kwargs["timeout"])
        return SimpleNamespace(text=self.text, raise_for_status=lambda: None)


@pytest.mark.parametrize(
    ("csv", "expected"),
    [
        (",result,table,_time\r\n,_result,0,2026-01-01T00:00:00Z\r\n", True),
        ("\r\n", False),
    ],
    ids=["an-old-point", "nothing-that-old"],
)
def test_the_probe_asks_for_one_point_older_than_the_retention(csv, expected):
    from ems.history.influx_client import HistoryInfluxClient

    session = _CsvSession(csv)
    client = HistoryInfluxClient("http://influxdb:8086", "ems", "t", session=session)

    assert client.has_points_before("ems_raw", 14 * DAY) is expected
    [query] = session.queries
    assert query.startswith('from(bucket: "ems_raw") |> range(start: 0, stop: -1209600s) |> first()')
    assert session.timeouts == [10]


def test_a_probe_that_failed_is_asked_again_and_not_called_older_data(caplog):
    """A probe that times out while a fresh InfluxDB settles says nothing about
    the data. Remembering it as "holds older data" would leave the raw bucket
    growing forever on exactly the installation the keeper exists for."""

    client = fresh_install()
    answers = iter([ConnectionError("read timed out")])

    def probe(name, seconds_ago):
        client.calls.append(("has_points_before", name, seconds_ago))
        for answer in answers:
            raise answer
        return False

    client.has_points_before = probe
    keeper = keeper_for(client)

    with caplog.at_level(logging.INFO):
        keeper.reconcile()
        assert client.buckets["ems_raw"] == 0
        keeper.reconcile()

    assert client.buckets["ems_raw"] == 14 * DAY
    assert "influx_schema_retention_kept" not in caplog.text
    assert caplog.text.count("event=influx_schema_retention_unchecked") == 1


@pytest.mark.parametrize(
    ("csv", "expected"),
    [
        (",result,table,_time\r\n,_result,0,2026-10-20T18:00:00Z\r\n", "2026-10-20T18:00:00Z"),
        ("\r\n", None),
    ],
    ids=["a-point", "nothing-in-the-window"],
)
def test_the_freshness_query_asks_for_the_newest_point_in_its_window(csv, expected):
    from ems.history.influx_client import HistoryInfluxClient

    session = _CsvSession(csv)
    client = HistoryInfluxClient("http://influxdb:8086", "ems", "t", session=session)

    assert client.latest_point_time("ems_raw", 900) == expected
    [query] = session.queries
    assert query.startswith('from(bucket: "ems_raw") |> range(start: -900s) |> last()')
