# Common Commands

Run Docker commands from the directory that contains `docker-compose.yml`.

## Docker

Start:

```bash
docker compose up -d
```

Stop:

```bash
docker compose down
```

Restart:

```bash
docker compose restart
```

Logs:

```bash
docker compose logs -f
```

Status:

```bash
docker compose ps
```

Diagnose:

```bash
docker compose exec ems python3 emsctl.py diagnose
```

Deeper diagnostics:

```bash
docker compose exec ems python3 emsctl.py diagnose --deep
```

Use this when normal diagnostics do not explain the issue or before opening a
detailed bug report.

Hardware read-only check:

```bash
docker compose exec ems python3 emsctl.py diagnose --hardware
```

Support bundle:

```bash
docker compose exec ems python3 emsctl.py diagnose --support-bundle
```

Backup:

```bash
docker compose exec ems python3 emsctl.py backup create
```

Config init:

```bash
docker compose exec ems python3 emsctl.py config init
```

Config upgrade:

```bash
docker compose exec ems python3 emsctl.py config upgrade --dry-run
docker compose exec ems python3 emsctl.py config upgrade
```

## Native Python

Run native commands from the repository checkout, usually with your virtual
environment active.

```bash
python3 emsctl.py diagnose
python3 emsctl.py diagnose --deep
python3 emsctl.py diagnose --hardware
python3 emsctl.py diagnose --support-bundle
python3 emsctl.py backup create
python3 emsctl.py config init
python3 emsctl.py config upgrade --dry-run
python3 emsctl.py config upgrade
python3 -B ems-solarflow-api-control.py --dry-run --no-ha --once
python3 -B ems-solarflow-api-control.py --duration 120
```

More detail: [cli.md](cli.md).
