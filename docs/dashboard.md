# Read-Only Live Dashboard

The EMS includes an optional standalone dashboard at:

```text
http://<ems-host>:8080
```

It is read-only. The dashboard exposes only telemetry endpoints and does not
provide control, configuration, or Zendure write routes.

## Control Explain View

The Control view shows the detailed calculation flow from measurements to final
handoff, so the control decision can be followed step by step.

![Control Explain demo screenshot](assets/control-explain-demo.jpg)

## Energy Statistics View

The Energy tab shows historical inverter output totals and savings estimates
from the local SQLite aggregates.

![Energy statistics demo screenshot](assets/preview-energy.jpg)

## Configuration

The dashboard section in `config.json` controls startup:

```json
{
  "dashboard": {
    "enabled": true,
    "host": "0.0.0.0",
    "port": 8080,
    "database_path": "data/ems_dashboard.sqlite",
    "history_hours": 48,
    "write_interval_seconds": 5
  },
  "energy_savings": {
    "enabled": true,
    "price_per_kwh": 0.0,
    "currency": "EUR",
    "max_sample_delta_seconds": 60
  }
}
```

`database_path` is relative to the project directory unless an absolute path is
used. The SQLite store is local and only keeps short-term dashboard history.
`write_interval_seconds` keeps database writes low even when the EMS loop runs
with a short interval.

Energy statistics are stored in the same SQLite database as daily aggregates.
They integrate measured inverter AC output over real elapsed time and skip
intervals longer than `max_sample_delta_seconds`.

## API

Live snapshot:

```text
GET /api/live
```

The live snapshot includes `energy_stats` with today, rolling windows, best
day, current-year monthly totals, yearly totals, and lifetime totals.

Energy statistics only:

```text
GET /api/energy-stats
```

Short-term history:

```text
GET /api/history?range=6h
```

Supported ranges are `1h`, `6h`, `12h`, and `24h`.

Live updates:

```text
GET /api/events
```

The event stream uses Server-Sent Events and emits `telemetry` events.
