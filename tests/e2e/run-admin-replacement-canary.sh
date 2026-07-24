#!/usr/bin/env bash
set -euo pipefail

: "${ADMIN_REPLACEMENT_RUNTIME:?ADMIN_REPLACEMENT_RUNTIME is required}"
: "${ADMIN_REPLACEMENT_EVENTS:?ADMIN_REPLACEMENT_EVENTS is required}"
: "${CANARY_TAG:?CANARY_TAG is required}"
: "${CANARY_ADMIN_DIGEST:?CANARY_ADMIN_DIGEST is required}"
: "${CANARY_EMS_DIGEST:?CANARY_EMS_DIGEST is required}"

E2E_PORT="${EMS_ADMIN_REPLACEMENT_E2E_PORT:-8126}"
RUNTIME_DIR="$ADMIN_REPLACEMENT_RUNTIME"
INSTALL_DIR="$RUNTIME_DIR/install"
COMPOSE_FILE="$INSTALL_DIR/docker-compose.admin.yml"
CONTAINER_NAME="ems-solarflow-admin"

case "$(basename "$RUNTIME_DIR")" in
  ems-admin-replacement-*) ;;
  *) echo "replacement runtime must use an ems-admin-replacement-* directory" >&2; exit 1 ;;
esac
if [[ -e "$RUNTIME_DIR" && -n "$(find "$RUNTIME_DIR" -mindepth 1 -maxdepth 1 -print -quit)" ]]; then
  echo "replacement runtime is not empty: $RUNTIME_DIR" >&2
  exit 1
fi
mkdir -p "$RUNTIME_DIR"
[[ -S /var/run/docker.sock ]] || { echo "Docker socket is unavailable" >&2; exit 1; }
docker info >/dev/null

EVENTS_PID=""
LOG_PID=""
cleanup_replacement_canary() {
  [[ -z "$LOG_PID" ]] || kill "$LOG_PID" >/dev/null 2>&1 || true
  [[ -z "$EVENTS_PID" ]] || kill "$EVENTS_PID" >/dev/null 2>&1 || true
  if [[ -f "$COMPOSE_FILE" ]]; then
    docker compose -f "$COMPOSE_FILE" down --remove-orphans >/dev/null 2>&1 || true
  fi
  docker ps -aq --filter 'name=^/ems-admin-updater-' \
    | xargs -r docker rm -f >/dev/null 2>&1 || true
}
trap cleanup_replacement_canary EXIT INT TERM

docker pull ghcr.io/basecubedev/ems-solarflow-admin:latest
docker pull "ghcr.io/basecubedev/ems-solarflow-admin@${CANARY_ADMIN_DIGEST}"
docker pull "ghcr.io/basecubedev/ems-solarflow-api-control@${CANARY_EMS_DIGEST}"

sh deploy/admin/install-admin-console.sh \
  --tag latest --bridge --bind 127.0.0.1 --port "$E2E_PORT" \
  --install-dir "$INSTALL_DIR" --no-start

docker rm -f "$CONTAINER_NAME" >/dev/null 2>&1 || true
: > "$ADMIN_REPLACEMENT_EVENTS"
docker events \
  --filter "container=${CONTAINER_NAME}" \
  --filter event=destroy \
  --format '{{.Action}} {{.Actor.ID}}' \
  > "$ADMIN_REPLACEMENT_EVENTS" &
EVENTS_PID=$!

docker compose -f "$COMPOSE_FILE" up -d
docker logs --follow "$CONTAINER_NAME" &
LOG_PID=$!

while true; do
  sleep 10
done
