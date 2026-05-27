# Zendure Development Grafana

This workspace provides a dedicated local Grafana instance for telemetry
analysis, experiments, and dashboard work. It is separate from any normal
Grafana system and is intended for local development only.

It is not part of the EMS control loop and does not make production
assumptions.

## Setup

1. Copy the environment template:

   ```bash
   cp develop/grafana/.env.example develop/grafana/.env
   ```

2. Edit `develop/grafana/.env`.

   Set at least:

   - `GRAFANA_ADMIN_PASSWORD`
   - `GRAFANA_INFLUXDB_URL`
   - `GRAFANA_INFLUXDB_ORG`
   - `GRAFANA_INFLUXDB_BUCKET`
   - `GRAFANA_INFLUXDB_TOKEN`

   The datasource token should match the development InfluxDB token from
   `develop/influxdb/.env`. For the simple local setup, this is usually the same
   value as `INFLUXDB_ADMIN_TOKEN`.

3. Start Grafana:

   ```bash
   docker compose -f develop/grafana/docker-compose.yml up -d
   ```

4. Open Grafana:

   ```text
   http://localhost:3001
   ```

5. Log in with:

   ```text
   admin / value from GRAFANA_ADMIN_PASSWORD
   ```

## Datasource

Grafana provisions the InfluxDB2 datasource automatically on container startup.

- name: `Zendure Dev InfluxDB2`
- query language: Flux
- default bucket: value from `GRAFANA_INFLUXDB_BUCKET`
- URL: value from `GRAFANA_INFLUXDB_URL`

The datasource is editable in the Grafana UI for development convenience.

## InfluxDB Network Access

This Grafana stack is intentionally separate from `develop/influxdb`. Keep the
InfluxDB URL configurable instead of assuming one Docker network model.

The default value is:

```env
GRAFANA_INFLUXDB_URL=http://host.docker.internal:8086
```

On Linux Docker, `host.docker.internal` may require an explicit host gateway
entry. The compose file includes:

```yaml
extra_hosts:
  - "host.docker.internal:host-gateway"
```

Alternative values can be used when InfluxDB is reachable through a specific
host or LAN address:

```env
GRAFANA_INFLUXDB_URL=http://192.168.1.110:8086
GRAFANA_INFLUXDB_URL=http://192.168.20.45:8086
GRAFANA_INFLUXDB_URL=http://host.docker.internal:8086
```

## Dashboards

Dashboard provisioning is prepared for later use. Drop dashboard JSON files into
`develop/grafana/dashboards/`; Grafana reads them from the `Zendure Dev` folder.

No dashboards are included by default.

## Reset

To reset local Grafana state:

```bash
docker compose -f develop/grafana/docker-compose.yml down
rm -rf develop/grafana/data
docker compose -f develop/grafana/docker-compose.yml up -d
```

This deletes local Grafana dashboards, users, settings, and other state stored
in `develop/grafana/data/`.
