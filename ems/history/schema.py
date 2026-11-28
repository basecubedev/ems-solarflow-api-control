# SPDX-FileCopyrightText: 2026 basecubedev
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Config-driven, idempotent InfluxDB schema reconciler.

Configuration is the source of truth. ``sync`` reconciles the live InfluxDB
instance to match the ``influxdb`` config block:

- create missing buckets (named ``{bucket_prefix}_{key}``),
- align bucket retention with ``retention.*_days``,
- create/update downsampling tasks for each ``downsampling`` entry,
- disable downsampling tasks that are no longer configured.

All operations are idempotent: running ``sync`` twice with unchanged config
performs no writes the second time. ``status`` reads the live state for the
``emsctl influx status`` command and the dashboard.

Import-side-effect-free; takes an injected client so it is unit-testable
without a live InfluxDB.
"""

from ems.config import INFLUXDB_RETENTION_KEY_BY_BUCKET
from ems.history.provider import PACK_CELL_REDUCERS

# Fields carried through downsampling. Numeric fields are averaged over the
# window (right aggregate for power/SoC charts); state fields keep the last
# value (averaging booleans/enum codes would be meaningless). Mirrors the
# developer Flux tasks in develop/influxdb/tasks/.
NUMERIC_FIELDS = [
    "soc",
    "min_soc",
    "max_soc",
    "solar",
    "solar1",
    "solar2",
    "solar3",
    "solar4",
    "output",
    "output_limit",
    "grid_input",
    "pack_in",
    "pack_out",
    "voltage",
    "temp",
    "remain_minutes",
    "house_load",
    "house_other",
    "grid_power",
    "target_output",
    "power",
    "surplus_export_w",
]

STATE_FIELDS = [
    "soc_limit",
    "pack_state",
    "fault_level",
    "smart_mode",
    "grid_off_mode",
    "ac_mode",
    "ac_status",
    "dc_status",
    "grid_state",
    "available",
    "pv_present",
    "output_active",
    "fault_active",
    "pass_state",
    "reverse_state",
    "grid_reverse",
    "surplus_export",
    "ac_charging",
]

MEASUREMENT_FILTER = (
    'r._measurement == "zendure_device" or '
    'r._measurement == "shelly_meter" or '
    'r._measurement == "ems_runtime" or '
    'r._measurement == "consumer"'
)

TASK_NAME_INFIX = "-downsample-"
CELL_TASK_SUFFIX = ":cells"
CELL_MEASUREMENT = "battery_pack"
BATTERY_TASK_SUFFIX = ":battery"
BATTERY_DOWNSAMPLED = (
    ("battery_pack", "temp_c", "mean"),
    ("battery_pack", "soc", "mean"),
    ("battery_pack", "cell_spread_v", "max"),
    ("battery_pack", "voltage_v", "mean"),
    ("battery_pack", "current_a", "mean"),
    ("battery_pack", "flow_w", "mean"),
    ("zendure_device", "heat_state", "max"),
    ("zendure_device", "low_temp", "max"),
    ("zendure_device", "error_flag", "max"),
    ("zendure_device", "derate", "max"),
    ("zendure_device", "winter_hold", "max"),
    ("zendure_device", "ac_input", "max"),
    ("zendure_device", "charge_avail_w", "mean"),
    ("zendure_device", "discharge_avail_w", "mean"),
    ("zendure_device", "input_limit_w", "mean"),
    ("zendure_device", "winter_target", "last"),
)


def bucket_name(prefix, key):
    """Resolve a bucket key (raw/1m/5m/1h/...) to its full bucket name."""
    return f"{prefix}_{key}"


def task_name(prefix, target):
    """Stable task name for the downsampling task writing into ``target``."""
    return f"{prefix}{TASK_NAME_INFIX}{target}"


def cell_task_name(prefix, target):
    """The task that carries the battery cell voltages of one downsampling step.

    A target may hold ``-`` but never ``:``, so no target's main task can take
    this name.
    """
    return f"{task_name(prefix, target)}{CELL_TASK_SUFFIX}"


def battery_task_name(prefix, target):
    """The task that carries the battery's condition of one downsampling step (``BATTERY_DOWNSAMPLED``)."""
    return f"{task_name(prefix, target)}{BATTERY_TASK_SUFFIX}"


def _planned_tasks(influx_config):
    """``(name, flux builder, entry)`` for every task the downsampling asks for.

    The cell voltages and the battery's condition have a task each beside each
    step's main one: the safe sync creates a task that is missing but never
    changes one that exists, so a task of their own reaches an upgraded
    installation by itself.
    """

    prefix = influx_config["bucket_prefix"]
    for entry in influx_config.get("downsampling", []):
        target = entry.get("target")
        if not target:
            continue
        yield task_name(prefix, target), build_downsample_flux, entry
        yield cell_task_name(prefix, target), build_cell_downsample_flux, entry
        yield battery_task_name(prefix, target), build_battery_downsample_flux, entry


def retention_seconds_for_bucket(influx_config, key):
    """Retention (seconds) for a bucket key; 0 means infinite/no expiry."""
    retention = influx_config.get("retention", {})
    retention_key = INFLUXDB_RETENTION_KEY_BY_BUCKET.get(key)
    if retention_key is None:
        return 0
    return int(retention.get(retention_key, 0) or 0) * 86400


def unattended_sync_wanted(influx_config):
    """Whether this config lets the EMS reconcile the schema on its own."""

    if not isinstance(influx_config, dict) or not influx_config.get("enabled"):
        return False
    return influx_config.get("auto_sync") is not False


def planned_buckets(influx_config):
    """Ordered {bucket_key: retention_seconds} derived from the config.

    Always includes ``raw`` plus every downsampling source and target. Order
    keeps ``raw`` first, then targets in pipeline order, so creation respects
    the dependency chain.
    """
    keys = ["raw"]
    for entry in influx_config.get("downsampling", []):
        for key in (entry.get("source"), entry.get("target")):
            if key and key not in keys:
                keys.append(key)

    return {key: retention_seconds_for_bucket(influx_config, key) for key in keys}


def planned_bucket_names(influx_config):
    """The planned buckets by name, in pipeline order.

    One source for everyone who asks whether the schema is complete: the
    provider diagnosing a failed query and the writer reporting the gap at
    startup must not disagree about which buckets were supposed to exist.
    """

    prefix = influx_config["bucket_prefix"]

    return [bucket_name(prefix, key) for key in planned_buckets(influx_config)]


def query_profile_bucket_names(influx_config):
    """The buckets the query profiles read, in profile order.

    Not the same set as the planned buckets, and nothing cross-checks the two
    halves of the config: a profile naming a bucket that no downsampling entry
    produces names one that no schema sync will ever create.
    """

    prefix = influx_config["bucket_prefix"]
    names = []
    for profile in influx_config.get("query_profiles", []):
        key = profile.get("bucket")
        if not key:
            continue
        name = bucket_name(prefix, key)
        if name not in names:
            names.append(name)

    return names


def planned_task_names(influx_config):
    """The downsampling tasks by name, in pipeline order.

    Buckets alone do not say the schema is complete. ``sync`` creates them
    first, so a run that stops in between leaves every bucket in place with
    nothing filling them -- an empty chart that reads as healthy everywhere.
    """

    names = []
    for name, _build, _entry in _planned_tasks(influx_config):
        if name not in names:
            names.append(name)

    return names


def build_downsample_flux(influx_config, entry):
    """Render the Flux body (with ``option task``) for one downsampling entry."""
    prefix = influx_config["bucket_prefix"]
    org = influx_config["org"]
    window = entry["window"]
    source_bucket = bucket_name(prefix, entry["source"])
    target_bucket = bucket_name(prefix, entry["target"])
    name = task_name(prefix, entry["target"])

    numeric = ",\n  ".join(f'"{field}"' for field in NUMERIC_FIELDS)
    state = ",\n  ".join(f'"{field}"' for field in STATE_FIELDS)

    return f'''option task = {{name: "{name}", every: {window}}}

numeric_fields = [
  {numeric},
]

state_fields = [
  {state},
]

from(bucket: "{source_bucket}")
  |> range(start: -task.every)
  |> filter(fn: (r) => {MEASUREMENT_FILTER})
  |> filter(fn: (r) => contains(value: r._field, set: numeric_fields))
  |> aggregateWindow(every: {window}, fn: mean, createEmpty: false)
  |> to(bucket: "{target_bucket}", org: "{org}")

from(bucket: "{source_bucket}")
  |> range(start: -task.every)
  |> filter(fn: (r) => {MEASUREMENT_FILTER})
  |> filter(fn: (r) => contains(value: r._field, set: state_fields))
  |> aggregateWindow(every: {window}, fn: last, createEmpty: false)
  |> to(bucket: "{target_bucket}", org: "{org}")
'''


def build_cell_downsample_flux(influx_config, entry):
    """Render the Flux body of the task that carries one step's cell voltages.

    Each field keeps the window's extreme rather than its mean, so a short dip
    of the lowest cell survives into the coarser buckets.
    """
    prefix = influx_config["bucket_prefix"]
    org = influx_config["org"]
    window = entry["window"]
    source_bucket = bucket_name(prefix, entry["source"])
    target_bucket = bucket_name(prefix, entry["target"])
    name = cell_task_name(prefix, entry["target"])

    pipelines = "".join(
        f'''
from(bucket: "{source_bucket}")
  |> range(start: -task.every)
  |> filter(fn: (r) => r._measurement == "{CELL_MEASUREMENT}" and r._field == "{field}")
  |> aggregateWindow(every: {window}, fn: {reducer}, createEmpty: false)
  |> to(bucket: "{target_bucket}", org: "{org}")
'''
        for field, reducer in PACK_CELL_REDUCERS.items()
    )
    return f'''option task = {{name: "{name}", every: {window}}}
{pipelines}'''


def build_battery_downsample_flux(influx_config, entry):
    """Render the Flux body of the task that carries one step's battery condition.

    The pack temperatures and charges and what the battery accepts keep the window's mean;
    a state such as the heating keeps its maximum, so a window in which it was
    on at all shows it on; the winter target keeps its last value. One
    pipeline per reducer.
    """
    prefix = influx_config["bucket_prefix"]
    org = influx_config["org"]
    window = entry["window"]
    source_bucket = bucket_name(prefix, entry["source"])
    target_bucket = bucket_name(prefix, entry["target"])
    name = battery_task_name(prefix, entry["target"])

    pipelines = "".join(
        f'''
from(bucket: "{source_bucket}")
  |> range(start: -task.every)
  |> filter(fn: (r) => {_battery_predicate(reducer)})
  |> aggregateWindow(every: {window}, fn: {reducer}, createEmpty: false)
  |> to(bucket: "{target_bucket}", org: "{org}")
'''
        for reducer in dict.fromkeys(reducer for _measurement, _field, reducer in BATTERY_DOWNSAMPLED)
    )
    return f'''option task = {{name: "{name}", every: {window}}}
{pipelines}'''


def _battery_predicate(reducer):
    """The filter of one battery pipeline: every field the reducer keeps, by measurement.

    One pipeline per reducer reads the source bucket once for all of its fields
    rather than once per field, which a small host running InfluxDB notices.
    """

    fields = {}
    for measurement, field, kept_by in BATTERY_DOWNSAMPLED:
        if kept_by == reducer:
            fields.setdefault(measurement, []).append(field)
    clauses = []
    for measurement, names in fields.items():
        any_field = " or ".join(f'r._field == "{field}"' for field in names)
        clauses.append(f'(r._measurement == "{measurement}" and ({any_field}))')
    return " or ".join(clauses)


def _normalize_flux(text):
    """Whitespace-insensitive comparison so cosmetic diffs don't trigger updates."""
    return "\n".join(line.rstrip() for line in str(text or "").strip().splitlines())


def _survivor_rank(task):
    """Order duplicates of one name so the same task survives every sync.

    A task the API returned without a usable id comes last whatever its status,
    because the only thing that could be done with it is a write it cannot
    receive. Among the rest an active task outranks a disabled copy: InfluxDB
    resumes an activated task from its last completion, so keeping the stale one
    would make it backfill every window it slept through.
    """

    task_id = task.get("id")

    return (not task_id, task.get("status") != "active", str(task_id or ""))


def _task_groups_by_name(client):
    """Live tasks grouped by name, each group best-survivor first.

    InfluxDB does not make task names unique: two syncs running at once can both
    find a task missing and both create it. A name-keyed lookup sees only one of
    the two and leaves the other writing the same window.
    """

    groups = {}
    for task in client.list_tasks():
        groups.setdefault(task.get("name"), []).append(task)
    for group in groups.values():
        group.sort(key=_survivor_rank)
    return groups


def _configured_task_names(influx_config):
    """``(desired, repeated)`` task names the config asks for.

    The config is a list, so it can name one target twice. Both entries want the
    same task, and the operator has to be told that rather than being shown a
    duplicate that looks like the outcome of a race.
    """

    configured = [name for name, _build, _entry in _planned_tasks(influx_config)]
    desired = set(configured)
    return desired, {name for name in desired if configured.count(name) > 1}


def _retired_tasks(groups, influx_config):
    """Yield ``(name, task, reason)`` for each owned task a sync retires.

    Obsolete tasks owned by this prefix, plus any duplicate of one that is still
    wanted. Sync disables exactly this set and prune deletes from exactly this
    set, so the rule lives here instead of in both: anything sync keeps can then
    never become a delete candidate. A task the API returned without a usable id
    is skipped, because nothing can be addressed to it.
    """

    prefix = influx_config["bucket_prefix"]
    owned_prefix = f"{prefix}{TASK_NAME_INFIX}"
    desired_names, repeated_in_config = _configured_task_names(influx_config)

    for name, group in groups.items():
        if not name or not name.startswith(owned_prefix):
            continue
        wanted = name in desired_names
        if not wanted:
            reason = "not_configured"
        elif name in repeated_in_config:
            reason = "duplicate_target"
        else:
            reason = "duplicate"
        for task in (group[1:] if wanted else group):
            if not task.get("id"):
                continue
            yield name, task, reason


def prune(client, influx_config, dry_run=False):
    """Delete the downsampling tasks a sync retired. Returns a report dict.

    Sync disables an obsolete task rather than deleting it, which is right --
    disabling is reversible and a delete is not -- but nothing ever removed the
    disabled ones, so every later sync reported the same growing list. Prune is
    that missing step, and it removes only what sync already stopped: an
    obsolete task still running is reported and left alone, so stopping a task
    and removing it stay two separate decisions.
    """

    report = {"tasks": []}
    groups = _task_groups_by_name(client)
    for name, task, reason in _retired_tasks(groups, influx_config):
        if task.get("status") != "inactive":
            report["tasks"].append(
                {"name": name, "action": "kept", "reason": "still_active"}
            )
            continue
        if dry_run:
            report["tasks"].append(
                {"name": name, "action": "would_delete", "reason": reason}
            )
            continue
        client.delete_task(task.get("id"))
        report["tasks"].append(
            {"name": name, "action": "deleted", "reason": reason}
        )
    return report


def _retention_blocker(client, name, current, wanted):
    """Why moving a bucket from ``current`` to ``wanted`` is not safe, or None.

    0 means kept forever. ``older_data``: points would fall out. A probe that
    fails is ``probe_failed``: the safe sync leaves the retention as it is
    rather than guess, and asks again next time.
    """

    if wanted == 0 or (current != 0 and wanted >= current):
        return None
    try:
        return "older_data" if client.has_points_before(name, wanted) else None
    except Exception:
        return "probe_failed"


def _refused_by_influxdb(exc):
    """Whether InfluxDB answered a request with a refusal of the request itself.

    A token it does not accept (401/403) is the whole sync's problem, not one
    bucket's, and so is everything that is not an answer.
    """

    status = getattr(getattr(exc, "response", None), "status_code", None)
    return isinstance(status, int) and 400 <= status < 500 and status not in (401, 403)


def _safe_bucket(client, name, retention, known_kept=(), confirmed=None):
    """Create a missing bucket; correct a retention only where that deletes nothing.

    A bucket in ``known_kept`` was found holding older data before and is not
    asked again: its answer does not change while its retention does not. A
    retention ``confirmed`` names for the bucket was confirmed by an operator
    who knew it may delete older data, and is applied without asking; one
    InfluxDB refuses is reported ``kept`` with reason ``refused``, and the
    rest of the schema is still reconciled.
    """

    wanted = int(retention or 0)
    live = client.find_bucket(name)
    if live is None:
        _bucket, created = client.ensure_bucket(name, wanted)
        action = "created" if created else "unchanged"
        return {"name": name, "action": action, "retention_seconds": wanted}
    current = client.bucket_retention_seconds(live)
    if current == wanted:
        return {"name": name, "action": "unchanged", "retention_seconds": wanted}
    if (confirmed or {}).get(name) == wanted:
        try:
            client.update_bucket_retention(live.get("id"), wanted)
        except Exception as exc:
            if not _refused_by_influxdb(exc):
                raise
            return {
                "name": name,
                "action": "kept",
                "reason": "refused",
                "error": str(exc),
                "retention_seconds": wanted,
                "live_retention_seconds": current,
            }
        return {"name": name, "action": "updated", "retention_seconds": wanted, "confirmed": True}
    reason = "older_data" if name in known_kept else _retention_blocker(client, name, current, wanted)
    if reason is None:
        client.update_bucket_retention(live.get("id"), wanted)
        return {"name": name, "action": "updated", "retention_seconds": wanted}
    return {
        "name": name,
        "action": "kept",
        "reason": reason,
        "retention_seconds": wanted,
        "live_retention_seconds": current,
    }


def sync(client, influx_config, safe=False, known_kept=(), confirmed=None):
    """Reconcile InfluxDB to the config. Returns a structured report dict.

    ``safe`` is the sync that runs without anyone watching: the EMS's own and
    the Admin's repair. It creates what is missing and changes nothing an
    operator may have meant -- no task is updated, re-activated or disabled --
    and no data is deleted unasked: a retention is shortened only when no point
    is old enough to fall out of it, or when ``confirmed`` maps the bucket to exactly
    that retention, which an operator confirmed knowing it deletes older data
    (:mod:`ems.history.confirmed_retention`). A bucket or task it leaves alone
    is reported ``kept``; ``known_kept`` names buckets a caller already knows
    hold older data, so they are not probed again.
    """
    prefix = influx_config["bucket_prefix"]
    org_id = client.get_org_id()

    report = {"buckets": [], "tasks": [], "disabled_tasks": []}

    # 1) Buckets + retention.
    for key, retention in planned_buckets(influx_config).items():
        name = bucket_name(prefix, key)
        if safe:
            report["buckets"].append(_safe_bucket(client, name, retention, known_kept, confirmed))
            continue
        _bucket, action = client.ensure_bucket_retention(name, retention)
        report["buckets"].append(
            {"name": name, "action": action, "retention_seconds": retention}
        )

    # 2) Downsampling tasks (create/update).
    tasks_by_name = _task_groups_by_name(client)
    # A name whose every task lacks an id counts as missing, so the sync creates
    # one that can be written to instead of reconciling one that cannot.
    existing_tasks = {
        name: group[0]
        for name, group in tasks_by_name.items()
        if group[0].get("id")
    }

    for name, build, entry in _planned_tasks(influx_config):
        flux = build(influx_config, entry)
        existing = existing_tasks.get(name)

        if existing is None:
            created = client.create_task(flux, status="active", org_id=org_id)
            report["tasks"].append({"name": name, "action": "created"})
            # A target the config names twice must not become two tasks: the
            # second entry reconciles the one just created instead.
            if isinstance(created, dict) and created.get("id"):
                existing_tasks[name] = created
            continue

        needs_flux = _normalize_flux(existing.get("flux")) != _normalize_flux(flux)
        needs_status = existing.get("status") != "active"

        if safe:
            action = "kept" if needs_flux or needs_status else "unchanged"
            report["tasks"].append({"name": name, "action": action})
        elif needs_flux or needs_status:
            client.update_task(
                existing.get("id"),
                flux=flux if needs_flux else None,
                status="active" if needs_status else None,
            )
            report["tasks"].append({"name": name, "action": "updated"})
        else:
            report["tasks"].append({"name": name, "action": "unchanged"})

    # 3) Retire what the config no longer wants. The reason travels with the
    #    entry because a duplicate's name is also in ``tasks``, and without it
    #    the two report the same name saying opposite things.
    if safe:
        return report
    for name, task, reason in _retired_tasks(tasks_by_name, influx_config):
        if task.get("status") == "inactive":
            report["disabled_tasks"].append(
                {"name": name, "action": "unchanged", "reason": reason}
            )
            continue
        client.update_task(task.get("id"), status="inactive")
        report["disabled_tasks"].append(
            {"name": name, "action": "disabled", "reason": reason}
        )

    return report


# How far back `status` looks for the newest raw point. The EMS writes every
# cycle, so a quarter of an hour without one means nothing is arriving -- or
# three write intervals, where `raw_write_interval_seconds` throttles it more.
RAW_FRESHNESS_SECONDS = 15 * 60


def raw_freshness_window(influx_config):
    try:
        interval = float(influx_config.get("raw_write_interval_seconds") or 0)
    except (TypeError, ValueError):
        interval = 0.0
    return int(max(RAW_FRESHNESS_SECONDS, 3 * interval))


def _raw_freshness(client, name, exists, window):
    """``(newest point or None, whether it could be asked)`` for the raw bucket."""

    if not exists:
        return None, False
    try:
        return client.latest_point_time(name, window), True
    except Exception:
        return None, False


def status(client, influx_config, confirmed=None):
    """Read live bucket/task state for diagnostics. Returns a report dict.

    Beside the schema it reports whether telemetry is arriving: a complete
    schema says nothing about writes InfluxDB refuses. A bucket whose configured
    retention ``confirmed`` names, and which does not carry it yet, says so as
    ``confirmed_retention``: the EMS applies it by itself.
    """
    prefix = influx_config["bucket_prefix"]
    owned_bucket_prefix = f"{prefix}_"
    owned_task_prefix = f"{prefix}{TASK_NAME_INFIX}"

    buckets = []
    for key, retention in planned_buckets(influx_config).items():
        name = bucket_name(prefix, key)
        live = client.find_bucket(name)
        live_retention = client.bucket_retention_seconds(live) if live else None
        buckets.append(
            {
                "name": name,
                "exists": live is not None,
                "retention_seconds": live_retention,
                "expected_retention_seconds": retention,
                "confirmed_retention": (
                    live is not None
                    and live_retention != retention
                    and (confirmed or {}).get(name) == retention
                ),
            }
        )

    tasks = []
    healthy = True
    for task in client.list_tasks():
        name = task.get("name", "")
        if not name.startswith(owned_task_prefix):
            continue
        last_run_status = task.get("lastRunStatus")
        task_active = task.get("status") == "active"
        if task_active and last_run_status not in (None, "success"):
            healthy = False
        tasks.append(
            {
                "name": name,
                "status": task.get("status"),
                "last_run_status": last_run_status,
                "latest_completed": task.get("latestCompleted"),
                "every": task.get("every"),
            }
        )

    missing_buckets = [b["name"] for b in buckets if not b["exists"]]
    active = {task["name"] for task in tasks if task["status"] == "active"}
    missing_tasks = [name for name in planned_task_names(influx_config) if name not in active]
    healthy = healthy and not missing_buckets and not missing_tasks
    raw = bucket_name(prefix, "raw")
    window = raw_freshness_window(influx_config)
    raw_latest, raw_checked = _raw_freshness(
        client, raw, any(b["name"] == raw and b["exists"] for b in buckets), window
    )

    return {
        "bucket_prefix": prefix,
        "owned_bucket_prefix": owned_bucket_prefix,
        "buckets": buckets,
        "tasks": tasks,
        "healthy": healthy,
        "missing_buckets": missing_buckets,
        "missing_tasks": missing_tasks,
        "raw_bucket": raw,
        "raw_window_seconds": window,
        "raw_checked": raw_checked,
        "raw_latest": raw_latest,
    }
