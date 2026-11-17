# SPDX-FileCopyrightText: 2026 basecubedev
# SPDX-License-Identifier: AGPL-3.0-or-later
"""A retention an operator set in the Admin and confirmed (ems.history.confirmed_retention).

The unattended schema sync never shortens a bucket past data it holds. These
tests hold the record that lets it do so to what the operator confirmed: only
the buckets a confirmed change moved, only the retention it set, and only while
nothing replaced it.
"""

import contextlib
import json
import threading

import pytest

from ems.history import confirmed_retention
from ems.config import resolve_influxdb_config
from ems.history.confirmed_retention import (
    Confirmation,
    confirm_change,
    confirmations,
    confirmed_path,
    discard,
    for_settings,
    retention_changes,
    target_of,
)

pytestmark = [
    pytest.mark.unit,
]

DAY = 86400


def config(enabled=True, **retention):
    return {"influxdb": {"enabled": enabled, "retention": retention}}


def at(moment):
    return lambda: moment


TARGET = target_of(resolve_influxdb_config(config()))


def confirmed_retentions(path):
    return {name: entry.retention_seconds for name, entry in confirmations(path).items()}


def test_a_shorter_raw_retention_is_a_change():
    assert retention_changes(config(raw_days=14), config(raw_days=7)) == {"ems_raw": 7 * DAY}


def test_the_release_default_counts_as_the_retention_before():
    assert retention_changes(config(), config(raw_days=7)) == {"ems_raw": 7 * DAY}


def test_a_longer_retention_is_a_change_too():
    """InfluxDB's own setup leaves the raw bucket keeping data forever while the
    settings say 14 days; 30 confirmed there must reach the bucket as well."""

    assert retention_changes(config(raw_days=14), config(raw_days=30)) == {"ems_raw": 30 * DAY}


def test_an_unchanged_retention_is_no_change():
    assert retention_changes(config(raw_days=7), config(raw_days=7)) == {}


def test_every_moved_bucket_is_named():
    changes = retention_changes(
        config(raw_days=14, one_minute_days=90, one_hour_days=1825),
        config(raw_days=7, one_minute_days=30, one_hour_days=1825),
    )

    assert changes == {"ems_raw": 7 * DAY, "ems_1m": 30 * DAY}


def test_a_renamed_bucket_prefix_names_no_bucket():
    after = {"influxdb": {"enabled": True, "bucket_prefix": "home", "retention": {"raw_days": 7}}}

    assert retention_changes(config(raw_days=14), after) == {}


def test_analytics_switched_on_with_a_new_retention_is_a_change():
    assert retention_changes(config(False, raw_days=14), config(raw_days=7)) == {"ems_raw": 7 * DAY}


@pytest.mark.parametrize(
    "after",
    [
        config(False, raw_days=7),
        {"influxdb": {"enabled": True, "auto_sync": False, "retention": {"raw_days": 7}}},
    ],
    ids=["analytics-off", "auto-sync-off"],
)
def test_no_change_counts_where_no_keeper_would_apply_it(after):
    assert retention_changes(config(raw_days=14), after) == {}


def test_another_influxdb_names_no_bucket():
    after = {"influxdb": {"enabled": True, "url": "http://elsewhere:8086", "retention": {"raw_days": 7}}}

    assert retention_changes(config(raw_days=14), after) == {}


def test_settings_the_ems_kept_no_schema_under_before_start_the_record_over(tmp_path):
    """An entry from before analytics was switched off by hand must not survive
    switching it back on here, even when no retention moves."""

    path = confirmed_path(tmp_path)
    confirm_change(path, config(raw_days=30), config(raw_days=14), now=at(1000.0))

    assert confirm_change(path, config(False, raw_days=14), config(raw_days=14), now=at(2000.0)) == []
    assert not path.exists()


def test_only_entries_for_the_running_influxdb_and_retention_count(tmp_path):
    path = confirmed_path(tmp_path)
    confirm_change(path, config(raw_days=30, one_minute_days=90), config(raw_days=14, one_minute_days=30), now=at(1000.0))
    running = resolve_influxdb_config(config(raw_days=14, one_minute_days=60))

    assert set(for_settings(confirmations(path), running)) == {"ems_raw"}
    assert for_settings(confirmations(path), {**running, "org": "other"}) == {}
    assert for_settings(confirmations(path), {**running, "auto_sync": False}) == {}


def test_the_record_lives_in_the_data_directory(tmp_path, isolated_install_root):
    assert confirmed_path() == isolated_install_root / "data" / "influx-retention-confirmed.json"
    assert confirmed_path(tmp_path) == tmp_path / "influx-retention-confirmed.json"


def test_a_confirmed_change_is_recorded_with_its_moment(tmp_path):
    path = confirmed_path(tmp_path)

    names = confirm_change(
        path, config(raw_days=14, one_minute_days=90), config(raw_days=7, one_minute_days=30), now=at(1000.0)
    )

    assert names == ["ems_1m", "ems_raw"]
    assert confirmations(path) == {
        "ems_raw": Confirmation(7 * DAY, 1000.0, TARGET),
        "ems_1m": Confirmation(30 * DAY, 1000.0, TARGET),
    }
    assert confirmed_retentions(path) == {"ems_raw": 7 * DAY, "ems_1m": 30 * DAY}


def test_a_later_confirmation_for_the_same_bucket_replaces_the_earlier(tmp_path):
    path = confirmed_path(tmp_path)
    confirm_change(path, config(raw_days=30), config(raw_days=7), now=at(1000.0))

    confirm_change(path, config(raw_days=7), config(raw_days=10), now=at(2000.0))

    assert confirmations(path) == {"ems_raw": Confirmation(10 * DAY, 2000.0, TARGET)}


def test_a_bucket_set_to_keep_its_data_forever_loses_its_entry(tmp_path):
    path = confirmed_path(tmp_path)
    confirm_change(path, config(raw_days=14), config(raw_days=7), now=at(1000.0))

    names = confirm_change(path, config(raw_days=7), config(raw_days=0), now=at(2000.0))

    assert names == []
    assert not path.exists()


def test_a_confirmed_change_leaves_the_other_buckets_entries(tmp_path):
    path = confirmed_path(tmp_path)
    confirm_change(path, config(one_minute_days=90), config(one_minute_days=30), now=at(1000.0))

    confirm_change(path, config(raw_days=7), config(raw_days=14), now=at(2000.0))

    assert confirmed_retentions(path) == {"ems_1m": 30 * DAY, "ems_raw": 14 * DAY}


@pytest.mark.parametrize(
    "after",
    [
        config(False, raw_days=7),
        {"influxdb": {"enabled": True, "auto_sync": False, "retention": {"raw_days": 7}}},
    ],
    ids=["analytics-off", "auto-sync-off"],
)
def test_settings_under_which_the_ems_keeps_no_schema_clear_the_record(tmp_path, after):
    """Whoever manages the schema from now on has not confirmed what is in the
    record; switched back on later, it must not apply an old entry."""

    path = confirmed_path(tmp_path)
    confirm_change(path, config(raw_days=14), config(raw_days=7), now=at(1000.0))

    assert confirm_change(path, config(raw_days=7), after, now=at(2000.0)) == []
    assert not path.exists()


def test_a_change_that_moves_no_retention_writes_no_file(tmp_path):
    path = confirmed_path(tmp_path)

    assert confirm_change(path, config(raw_days=7), config(raw_days=7)) == []
    assert not path.exists()


@pytest.mark.parametrize(
    "content",
    [
        "",
        "not json",
        "[]",
        json.dumps({"version": 2, "buckets": {"ems_raw": {"retention_seconds": 7 * DAY, "confirmed_at": 1.0}}}),
        json.dumps({"version": 3, "buckets": []}),
        json.dumps(
            {
                "version": 3,
                "buckets": {
                    "ems_raw": {"retention_seconds": "604800", "confirmed_at": 1.0, "target": TARGET},
                    "ems_1m": {"retention_seconds": True, "confirmed_at": 1.0, "target": TARGET},
                    "ems_5m": {"retention_seconds": 0, "confirmed_at": 1.0, "target": TARGET},
                    "ems_1h": {"retention_seconds": 86400, "confirmed_at": "yesterday", "target": TARGET},
                    "ems_x": {"retention_seconds": 86400, "target": TARGET},
                    "ems_y": 86400,
                    "ems_z": {"retention_seconds": 86400, "confirmed_at": 1.0},
                },
            }
        ),
    ],
    ids=["empty", "garbage", "list", "older-version", "no-mapping", "bad-entries"],
)
def test_a_record_that_cannot_be_trusted_confirms_nothing(tmp_path, content):
    path = confirmed_path(tmp_path)
    path.write_text(content, encoding="utf-8")

    assert confirmations(path) == {}


def test_a_moment_too_large_for_a_float_confirms_nothing_and_raises_nothing(tmp_path):
    path = confirmed_path(tmp_path)
    path.write_text(
        '{"version": 3, "buckets": {"ems_raw": {"retention_seconds": 604800, "target": "x", "confirmed_at": 1'
        + "0" * 400
        + '}}}',
        encoding="utf-8",
    )

    assert confirmations(path) == {}


def test_a_missing_record_confirms_nothing(tmp_path):
    assert confirmations(confirmed_path(tmp_path)) == {}


def test_a_discarded_entry_is_removed_and_the_others_stay(tmp_path):
    path = confirmed_path(tmp_path)
    confirm_change(path, config(raw_days=14, one_minute_days=90), config(raw_days=7, one_minute_days=30), now=at(1000.0))

    discard(path, {"ems_raw": Confirmation(7 * DAY, 1000.0, TARGET)})

    assert confirmed_retentions(path) == {"ems_1m": 30 * DAY}


def test_an_entry_confirmed_again_since_it_was_read_is_not_discarded(tmp_path):
    """The keeper read the entry before the Admin recorded a newer confirmation
    for the same bucket; that newer one is not the keeper's to remove."""

    path = confirmed_path(tmp_path)
    confirm_change(path, config(raw_days=14), config(raw_days=7), now=at(1000.0))
    read = confirmations(path)
    confirm_change(path, config(raw_days=7), config(raw_days=3), now=at(2000.0))

    discard(path, read)

    assert confirmations(path) == {"ems_raw": Confirmation(3 * DAY, 2000.0, TARGET)}


def test_the_last_discarded_entry_removes_the_record(tmp_path):
    path = confirmed_path(tmp_path)
    confirm_change(path, config(raw_days=14), config(raw_days=7), now=at(1000.0))

    discard(path, confirmations(path))

    assert not path.exists()


def test_a_confirmation_recorded_while_another_is_discarded_is_not_lost(tmp_path, monkeypatch):
    """Interleaving: the keeper has read the record to discard ``ems_raw`` when
    the Admin comes to record ``ems_1m``. The Admin finds the record's lock held
    and waits; once the keeper has written and let go, the Admin adds to what
    the keeper left. The keeper resumes only when the Admin is blocked on the
    lock, or has finished without taking it, which a missing lock would do."""

    path = confirmed_path(tmp_path)
    confirm_change(path, config(raw_days=14), config(raw_days=7), now=at(1000.0))
    locks = {}
    keeper_read = threading.Event()
    admin_blocked_or_done = threading.Event()
    real_read = confirmed_retention._read

    @contextlib.contextmanager
    def lock(target, timeout=None):
        held = locks.setdefault(target, threading.Lock())
        if not held.acquire(blocking=False):
            if threading.current_thread().name == "admin":
                admin_blocked_or_done.set()
            held.acquire()
        try:
            yield
        finally:
            held.release()

    def read(target):
        result = real_read(target)
        if threading.current_thread().name == "keeper":
            keeper_read.set()
            assert admin_blocked_or_done.wait(5)
        return result

    def admin():
        try:
            confirm_change(path, config(one_minute_days=90), config(one_minute_days=30), now=at(2000.0))
        finally:
            admin_blocked_or_done.set()

    monkeypatch.setattr(confirmed_retention, "exclusive_file_lock", lock)
    monkeypatch.setattr(confirmed_retention, "_read", read)
    keeper = threading.Thread(
        target=discard, args=(path, {"ems_raw": Confirmation(7 * DAY, 1000.0, TARGET)}), name="keeper"
    )
    admin_thread = threading.Thread(target=admin, name="admin")
    keeper.start()
    assert keeper_read.wait(5)
    admin_thread.start()
    keeper.join(10)
    admin_thread.join(10)

    assert confirmations(path) == {"ems_1m": Confirmation(30 * DAY, 2000.0, TARGET)}
