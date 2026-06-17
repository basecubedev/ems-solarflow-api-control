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
    existing_tasks = {task.get("name"): task for task in client.list_tasks()}
    desired_names = set()

    for entry in influx_config.get("downsampling", []):
        name = task_name(prefix, entry["target"])
        desired_names.add(name)
        flux = build_downsample_flux(influx_config, entry)
        existing = existing_tasks.get(name)

        if existing is None:
            client.create_task(flux, status="active", org_id=org_id)
            report["tasks"].append({"name": name, "action": "created"})
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

    # 3) Disable obsolete downsampling tasks owned by this prefix.
    owned_prefix = f"{prefix}{TASK_NAME_INFIX}"
    for name, task in existing_tasks.items():
        if not name or not name.startswith(owned_prefix):
            continue
        if name in desired_names:
            continue
        if task.get("status") == "inactive":
            report["disabled_tasks"].append({"name": name, "action": "unchanged"})
            continue
        client.update_task(task.get("id"), status="inactive")
        report["disabled_tasks"].append({"name": name, "action": "disabled"})

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
