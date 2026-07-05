# Developer Setup

Only for development, debugging and contributing from a Git checkout with a local
Python environment. This is not the normal user path — for a home install, use
the [Admin Console](admin-setup.md) or [Docker Bootstrap](docker-bootstrap.md).

## Layout

```text
Config:  ./config/config.json
Data:    ./data/
```

New developer setups use the standard `config/config.json` layout. Older
checkouts may still use a root `config.json`; that is supported as a fallback,
but new setups should use `config/config.json`.

## Steps

```bash
python3 -m venv .venv && source .venv/bin/activate
python -m pip install -r requirements.txt -r requirements-dev.txt

mkdir -p config data
cp config/config.template.json config/config.json
nano config/config.json
```

Or run the guided assistant, which writes `config/config.json`:

```bash
python3 emsctl.py config init
```

Then validate:

```bash
python3 emsctl.py diagnose
python3 -B ems-solarflow-api-control.py --dry-run --no-ha --once
```

Full detail: [../native-python.md](../native-python.md) and
[../development.md](../development.md). Layout and legacy migration:
[config-layout.md](config-layout.md).

## Build the Admin Console from source

Normal users install the Admin Console from the published image with
`install-admin-console.sh` (see [admin-setup.md](admin-setup.md)); no Git is
required. Contributors can instead build and run the Admin Console image from a
Git checkout:

```bash
git clone https://github.com/basecubedev/ems-solarflow-api-control.git
cd ems-solarflow-api-control
deploy/admin/start-admin-setup.sh
```

Then open `http://127.0.0.1:8090`. Add `--hostnet` for LAN discovery or
`--discovery-only` for the restricted, no-Docker-socket preview. This launcher
builds the image locally from the checkout; the installer path uses the published
`ghcr.io/basecubedev/ems-solarflow-admin` image. Full technical reference:
[../admin-discovery.md](../admin-discovery.md).
