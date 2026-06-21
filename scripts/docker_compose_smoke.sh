#!/bin/sh
# SPDX-License-Identifier: AGPL-3.0-or-later
set -eu

ROOT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
IMAGE="${EMS_DOCKER_SMOKE_IMAGE:-ems-solarflow-api-control:ci-smoke}"
BUILD_IMAGE="${EMS_DOCKER_SMOKE_BUILD:-1}"

if ! docker compose version >/dev/null 2>&1; then
    echo "docker compose is not available; skipping compose smoke test." >&2
    exit 0
fi

if [ "$BUILD_IMAGE" != "0" ]; then
    docker build -t "$IMAGE" "$ROOT_DIR"
fi

TMPDIR="$(mktemp -d)"
cleanup() {
    cd "$ROOT_DIR"
    if [ -f "$TMPDIR/docker-compose.yml" ]; then
        docker compose -f "$TMPDIR/docker-compose.yml" \
            --project-name ems-compose-smoke down -v >/dev/null 2>&1 || true
    fi
    rm -rf "$TMPDIR"
}
trap cleanup EXIT INT TERM

cp "$ROOT_DIR/docker-compose.example.yml" "$TMPDIR/docker-compose.yml"
python3 - "$TMPDIR/docker-compose.yml" "$IMAGE" <<'PY'
from pathlib import Path
import sys

path = Path(sys.argv[1])
image = sys.argv[2]
text = path.read_text()
text = text.replace(
    "image: ghcr.io/basecubedev/ems-solarflow-api-control:latest",
    f"image: {image}",
)
text = text.replace("    container_name: ems-solarflow-api-control\n", "")
path.write_text(text)
PY

cd "$TMPDIR"
mkdir -p config data
export PUID="${PUID:-$(id -u)}"
export PGID="${PGID:-$(id -g)}"
if [ "$(id -u)" = "0" ]; then
    chown "$PUID:$PGID" config data
fi

docker compose --project-name ems-compose-smoke up -d
created_config=0
for _ in 1 2 3 4 5 6 7 8 9 10; do
    if [ -f config/config.json ]; then
        created_config=1
        break
    fi
    sleep 1
done
if [ "$created_config" != "1" ]; then
    docker compose --project-name ems-compose-smoke logs ems >&2 || true
    echo "config/config.json was not created by the container." >&2
    exit 1
fi

docker compose --project-name ems-compose-smoke exec -T ems \
    test -f /app/config/config.json
docker compose --project-name ems-compose-smoke exec -T ems \
    test -w /app/config
docker compose --project-name ems-compose-smoke exec -T ems \
    test -w /app/data
docker compose --project-name ems-compose-smoke exec -T ems \
    python3 emsctl.py config init --dry-run >/dev/null
docker compose --project-name ems-compose-smoke exec -T ems \
    python3 emsctl.py diagnose >/dev/null || true

# Backups must land on the persistent ./data mount (host data/backups/) without
# the user adding a separate backup volume. The documented exec command must run
# as the runtime user so the archive is host-deletable (no root-owned files).
docker compose --project-name ems-compose-smoke exec -T ems \
    python3 emsctl.py backup create >/dev/null
test -d data/backups
if [ "$(find data/backups -name '*.tar.gz' -o -name '*.tar.gz.enc' | wc -l)" -lt 1 ]; then
    echo "no backup archive was created under data/backups/." >&2
    exit 1
fi

# emsctl exec commands must not leave root-owned files in the bind mounts.
root_owned="$(find config data -maxdepth 5 -uid 0 -print -quit 2>/dev/null || true)"
if [ -n "$root_owned" ]; then
    echo "root-owned file left in bind mount: $root_owned" >&2
    ls -lan "$root_owned" >&2 || true
    exit 1
fi

# The host user must be able to delete the generated backup archive.
backup_archive="$(find data/backups -name '*.tar.gz' -o -name '*.tar.gz.enc' | head -n 1)"
if [ -n "$backup_archive" ] && ! rm -f "$backup_archive"; then
    echo "host user cannot delete backup archive: $backup_archive" >&2
    exit 1
fi

docker compose --project-name ems-compose-smoke down -v
