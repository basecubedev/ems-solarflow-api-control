# Docker Bootstrap

Best for shell users who want a copy/paste Docker install without the browser
wizard. For a browser-guided setup instead, use the
[Admin Console](admin-setup.md).

## Layout

```text
Config:  ./config/config.json
Data:    ./data/
Compose: ./docker-compose.yml
```

## Steps

Linux/macOS — EMS only:

```bash
mkdir -p ems-solarflow-api-control && cd ems-solarflow-api-control
curl -fsSLo install-docker.sh https://raw.githubusercontent.com/basecubedev/ems-solarflow-api-control/main/install-docker.sh
sh install-docker.sh
```

The installer writes `docker-compose.yml`, creates `config/` and `data/`, and
starts EMS. For EMS-only installs `config/config.json` is created on first
container start; with `--analytics` the installer creates it during setup by
running `config init --analytics`. Existing `config/config.json` files are not
overwritten.

Then configure and verify:

```bash
docker compose exec ems python3 emsctl.py config init
docker compose restart
docker compose exec ems python3 emsctl.py diagnose
```

Full detail: [../quickstart.md](../quickstart.md) and [../docker.md](../docker.md).
Layout and legacy migration: [config-layout.md](config-layout.md).
