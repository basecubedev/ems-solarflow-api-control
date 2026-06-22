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
docker compose exec ems python3 emsctl.py backup create --type config --password
docker compose exec ems python3 emsctl.py backup create --type databases --password
```

Backups are stored in `data/backups/` by default. Docker users see the same
folder on the host via the existing `./data:/app/data` mount; no separate
backup volume is needed. Password-protected backups are recommended; without
the password, encrypted backups cannot be restored.

Unencrypted backup variant:

```bash
docker compose exec ems python3 emsctl.py backup create --type config
docker compose exec ems python3 emsctl.py backup create --type databases
```

Config init:

```bash
docker compose exec ems python3 emsctl.py config init
```

Config upgrade:

```bash
docker compose exec ems python3 emsctl.py config upgrade --dry-run
docker compose exec ems python3 emsctl.py config upgrade --yes --backup
```

Safe update:

```bash
docker compose exec ems python3 emsctl.py backup create --type config --password
docker compose exec ems python3 emsctl.py backup create --type databases --password
docker compose pull
docker compose up -d
docker compose exec ems python3 emsctl.py config upgrade --dry-run
docker compose exec ems python3 emsctl.py config upgrade --yes --backup
docker compose exec ems python3 emsctl.py diagnose
```

## Native Python

Run native commands from the repository checkout, usually with your virtual
environment active.

```bash
python3 emsctl.py diagnose
python3 emsctl.py diagnose --deep
python3 emsctl.py diagnose --hardware
python3 emsctl.py diagnose --support-bundle
python3 emsctl.py backup create --type config --password
python3 emsctl.py config init
python3 emsctl.py config upgrade --dry-run
python3 emsctl.py config upgrade
python3 -B ems-solarflow-api-control.py --dry-run --no-ha --once
python3 -B ems-solarflow-api-control.py --duration 120
```

More detail: [cli.md](cli.md).
