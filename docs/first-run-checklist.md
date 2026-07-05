# First-Run Checklist

Use this after the first config edit and before unattended operation.

## Docker

1. Replace all placeholders in `config/config.json`, including example IPs and
   `YOUR_SN`.
2. Restart EMS:

```bash
docker compose restart
```

3. Run diagnose:

```bash
docker compose exec ems python3 emsctl.py diagnose
```

4. Run read-only hardware checks:

```bash
docker compose exec ems python3 emsctl.py diagnose --hardware
```

5. Open the dashboard:

```text
http://<host-ip>:8080
```

6. Confirm grid meter direction/sign. Import and export should match your real
   meter behavior.
7. Confirm each Zendure device is reachable.
8. Confirm SOC and power limits look reasonable.
9. Optionally use dry-run or bounded validation before normal operation.
10. Monitor the first live run.
11. Do not enable unattended operation until behavior looks correct.

Backups are stored in `data/backups/` by default.

If you enabled Analytics (`sh install-docker.sh --analytics`), confirm the
bundled InfluxDB is reachable:

```bash
docker compose exec ems python3 emsctl.py influx status
```

## Native Python

```bash
python3 emsctl.py diagnose
python3 emsctl.py diagnose --hardware
python3 -B ems-solarflow-api-control.py --dry-run --no-ha --once
python3 -B ems-solarflow-api-control.py --duration 120
```

Backups are stored in `data/backups/` by default.

## What Not To Do

- Do not run EMS in parallel with another writer/controller.
- Do not leave `YOUR_SN` or example IPs in config.
- Do not assume InfluxDB is required.
- Do not expose the dashboard publicly without a reverse proxy/auth design.
- Do not skip first-run diagnose.

More detail: [safety.md](user/safety.md) and [troubleshooting.md](user/troubleshooting.md).
