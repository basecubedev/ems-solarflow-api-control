# SPDX-FileCopyrightText: 2026 basecubedev
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Bucket retentions an operator set in the Admin and confirmed there.

The schema sync that runs unattended never shortens a bucket's retention past
data the bucket holds: nobody asked for that data to go. An operator who changes
a retention in the Admin's settings and confirms the change, under its warning
that stored data can be discarded, did ask. The Admin records the retention each
changed bucket was confirmed for, on which InfluxDB, and the EMS's schema keeper
applies exactly that retention there, older data and all, and then removes the
entry.

An entry counts only for the bucket, the InfluxDB and the retention it names,
and only for the settings it was confirmed with. An EMS running settings that
name another retention or InfluxDB leaves it for the restart. At its start, the
EMS removes
every entry confirmed before it read settings that do not ask for it -- a backup
restored, the file edited by hand, analytics switched off -- so an entry never
outlives the settings it was confirmed with and cannot come back to life later.

The Admin writes the record and the EMS removes from it, from two processes, so
both hold its lock while they read and write it. Import-side-effect-free.
"""

import json
import math
import os
import time
from dataclasses import dataclass
from pathlib import Path

from ems.config import resolve_influxdb_config
from ems.file_io import atomic_write_bytes, exclusive_file_lock
from ems.history import schema
from ems.paths import resolve_data_dir

CONFIRMED_FILENAME = "influx-retention-confirmed.json"
FORMAT_VERSION = 3


@dataclass(frozen=True)
class Confirmation:
    """The retention a bucket was confirmed for, on which InfluxDB, and when (epoch seconds)."""

    retention_seconds: int
    confirmed_at: float
    target: str


def confirmed_path(data_dir=None):
    """The record in the EMS data directory, or in ``data_dir`` when given."""

    return resolve_data_dir(data_dir) / CONFIRMED_FILENAME


def target_of(influx_config):
    """The InfluxDB a normalized ``influxdb`` block writes to.

    Every setting the EMS picks its server by (``influx_setup.runtime_influx_url``)
    and the organisation: a change of any of them may point at another server.
    """

    return json.dumps(
        [
            influx_config.get(key)
            for key in ("mode", "url", "host_url", "org")
        ]
    )


def planned_retentions(influx_config):
    """``{bucket name: retention seconds}`` a normalized ``influxdb`` block asks for."""

    prefix = influx_config["bucket_prefix"]
    return {
        schema.bucket_name(prefix, key): int(seconds or 0)
        for key, seconds in schema.planned_buckets(influx_config).items()
    }


def retention_changes(previous, merged):
    """``{bucket name: retention seconds after}`` for every retention the change moves.

    Both are whole configs, read the way the EMS reads them. Only a bucket both
    name on the same InfluxDB is counted: a new one is created with its
    retention. Nothing counts while the EMS would not keep the schema itself.
    """

    before = resolve_influxdb_config(previous)
    after = resolve_influxdb_config(merged)
    if not schema.unattended_sync_wanted(after) or target_of(before) != target_of(after):
        return {}
    was = planned_retentions(before)
    return {
        name: seconds
        for name, seconds in planned_retentions(after).items()
        if name in was and was[name] != seconds
    }


def confirm_change(path, previous, merged, *, now=time.time):
    """Record the retentions a confirmed change from ``previous`` to ``merged`` set.

    A bucket set to a retention is recorded with it. One set to keep its data
    forever loses an entry it may still have: the unattended sync applies that
    by itself. Settings under which the EMS does not keep the schema itself, now
    or before the change, leave no entry from earlier: nobody confirmed those
    entries for what is saved now. Returns the recorded buckets' names.
    """

    path = Path(path)
    after = resolve_influxdb_config(merged)
    keeps_now = schema.unattended_sync_wanted(after)
    kept_before = schema.unattended_sync_wanted(resolve_influxdb_config(previous))
    changes = retention_changes(previous, merged)
    start_over = not (keeps_now and kept_before)
    if not changes and not (start_over and path.exists()):
        return []
    recorded = {name: seconds for name, seconds in changes.items() if seconds > 0}
    os.makedirs(path.parent, exist_ok=True)
    with exclusive_file_lock(_lock_path(path)):
        entries = {} if start_over else _read(path)
        for name in changes:
            entries.pop(name, None)
        at = float(now())
        target = target_of(after)
        entries.update({name: Confirmation(seconds, at, target) for name, seconds in recorded.items()})
        _write(path, entries)
    return sorted(recorded)


def confirmations(path):
    """``{bucket name: Confirmation}``; none when the record is missing or cannot be trusted."""

    return _read(Path(path))


def for_settings(entries, influx_config):
    """The ``entries`` that name the InfluxDB and the retention ``influx_config`` asks for."""

    if not schema.unattended_sync_wanted(influx_config):
        return {}
    target = target_of(influx_config)
    planned = planned_retentions(influx_config)
    return {
        name: entry
        for name, entry in entries.items()
        if entry.target == target and planned.get(name) == entry.retention_seconds
    }


def drop_replaced(path, influx_config, settings_read_at):
    """Remove the entries confirmed before ``settings_read_at`` that those settings do not ask for.

    Called once as the EMS starts, with the normalized ``influxdb`` block it
    runs and the moment it read it. Returns the removed ``{name: Confirmation}``.
    """

    path = Path(path)
    if settings_read_at is None or not path.exists():
        return {}
    with exclusive_file_lock(_lock_path(path)):
        entries = _read(path)
        wanted = for_settings(entries, influx_config) if isinstance(influx_config, dict) else {}
        replaced = {
            name: entry
            for name, entry in entries.items()
            if entry.confirmed_at < settings_read_at and name not in wanted
        }
        if replaced:
            _write(path, {name: entry for name, entry in entries.items() if name not in replaced})
    return replaced


def discard(path, done):
    """Remove the entries in ``done`` that the record still holds unchanged.

    ``done`` maps bucket names to the Confirmation read earlier; an entry the
    Admin replaced since is a new confirmation and stays.
    """

    path = Path(path)
    with exclusive_file_lock(_lock_path(path)):
        entries = _read(path)
        gone = [name for name, entry in done.items() if entries.get(name) == entry]
        if not gone:
            return
        for name in gone:
            del entries[name]
        _write(path, entries)


def _lock_path(path):
    return str(path.with_name(f"{path.name}.lock"))


def _read(path):
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict) or data.get("version") != FORMAT_VERSION:
        return {}
    buckets = data.get("buckets")
    if not isinstance(buckets, dict):
        return {}
    entries = {}
    for name, entry in buckets.items():
        if not isinstance(name, str) or not isinstance(entry, dict):
            continue
        seconds = entry.get("retention_seconds")
        at = entry.get("confirmed_at")
        target = entry.get("target")
        if type(seconds) is not int or seconds <= 0 or type(at) not in (int, float):
            continue
        if not isinstance(target, str):
            continue
        try:
            at = float(at)
        except OverflowError:
            continue
        if math.isfinite(at):
            entries[name] = Confirmation(seconds, at, target)
    return entries


def _write(path, entries):
    if not entries:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        return
    buckets = {
        name: {
            "retention_seconds": entry.retention_seconds,
            "confirmed_at": entry.confirmed_at,
            "target": entry.target,
        }
        for name, entry in entries.items()
    }
    raw = json.dumps({"version": FORMAT_VERSION, "buckets": buckets}, indent=2, sort_keys=True)
    atomic_write_bytes(str(path), (raw + "\n").encode("utf-8"))
