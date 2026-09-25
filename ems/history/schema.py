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
    "pack_in",
    "pack_out",
    "voltage",
    "temp",
    "remain_minutes",
    "house_load",
    "grid_power",
    "target_output",
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
]

MEASUREMENT_FILTER = (
    'r._measurement == "zendure_device" or '
    'r._measurement == "shelly_meter" or '
    'r._measurement == "ems_runtime"'
)

TASK_NAME_INFIX = "-downsample-"


def bucket_name(prefix, key):
    """Resolve a bucket key (raw/1m/5m/1h/...) to its full bucket name."""
    return f"{prefix}_{key}"


def task_name(prefix, target):
    """Stable task name for the downsampling task writing into ``target``."""
    return f"{prefix}{TASK_NAME_INFIX}{target}"


def retention_seconds_for_bucket(influx_config, key):
    """Retention (seconds) for a bucket key; 0 means infinite/no expiry."""
    retention = influx_config.get("retention", {})
    retention_key = INFLUXDB_RETENTION_KEY_BY_BUCKET.get(key)
    if retention_key is None:
        return 0
    return int(retention.get(retention_key, 0) or 0) * 86400


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

    prefix = influx_config["bucket_prefix"]
    names = []
    for entry in influx_config.get("downsampling", []):
        target = entry.get("target")
        if not target:
            continue
        name = task_name(prefix, target)
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

    prefix = influx_config["bucket_prefix"]
    configured = [
        task_name(prefix, entry["target"])
        for entry in influx_config.get("downsampling", [])
    ]
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


def sync(client, influx_config):
    """Reconcile InfluxDB to the config. Returns a structured report dict."""
    prefix = influx_config["bucket_prefix"]
    org_id = client.get_org_id()

    report = {"buckets": [], "tasks": [], "disabled_tasks": []}

    # 1) Buckets + retention.
    for key, retention in planned_buckets(influx_config).items():
        name = bucket_name(prefix, key)
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

    for entry in influx_config.get("downsampling", []):
        name = task_name(prefix, entry["target"])
        flux = build_downsample_flux(influx_config, entry)
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

        if needs_flux or needs_status:
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


def status(client, influx_config):
    """Read live bucket/task state for diagnostics. Returns a report dict."""
    prefix = influx_config["bucket_prefix"]
    owned_bucket_prefix = f"{prefix}_"
    owned_task_prefix = f"{prefix}{TASK_NAME_INFIX}"

    buckets = []
    for key, retention in planned_buckets(influx_config).items():
        name = bucket_name(prefix, key)
        live = client.find_bucket(name)
        buckets.append(
            {
                "name": name,
                "exists": live is not None,
                "retention_seconds": (
                    client.bucket_retention_seconds(live) if live else None
                ),
                "expected_retention_seconds": retention,
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

    return {
        "bucket_prefix": prefix,
        "owned_bucket_prefix": owned_bucket_prefix,
        "buckets": buckets,
        "tasks": tasks,
        "healthy": healthy,
        "missing_buckets": [b["name"] for b in buckets if not b["exists"]],
    }
