#!/usr/bin/env bash
set -euo pipefail

: "${ADMIN_REPLACEMENT_RUNTIME:?ADMIN_REPLACEMENT_RUNTIME is required}"
: "${ADMIN_REPLACEMENT_EVENTS:?ADMIN_REPLACEMENT_EVENTS is required}"
: "${CANARY_SOURCE_TAG:?CANARY_SOURCE_TAG is required}"
: "${CANARY_SOURCE_REVISION:?CANARY_SOURCE_REVISION is required}"
: "${CANARY_SOURCE_BUILD_ID:?CANARY_SOURCE_BUILD_ID is required}"
: "${CANARY_SOURCE_ADMIN_DIGEST:?CANARY_SOURCE_ADMIN_DIGEST is required}"
: "${CANARY_TAG:?CANARY_TAG is required}"
: "${CANARY_REVISION:?CANARY_REVISION is required}"
: "${CANARY_BUILD_ID:?CANARY_BUILD_ID is required}"
: "${CANARY_ADMIN_DIGEST:?CANARY_ADMIN_DIGEST is required}"
: "${CANARY_EMS_DIGEST:?CANARY_EMS_DIGEST is required}"

E2E_PORT="${EMS_ADMIN_REPLACEMENT_E2E_PORT:-8126}"
RUNTIME_DIR="$ADMIN_REPLACEMENT_RUNTIME"
INSTALL_DIR="$RUNTIME_DIR/install"
COMPOSE_FILE="$INSTALL_DIR/docker-compose.admin.yml"
CONTAINER_NAME="ems-solarflow-admin"
ADMIN_REPO="ghcr.io/basecubedev/ems-solarflow-admin"
EMS_REPO="ghcr.io/basecubedev/ems-solarflow-api-control"
SOURCE_ADMIN="${ADMIN_REPO}@${CANARY_SOURCE_ADMIN_DIGEST}"
TARGET_ADMIN="${ADMIN_REPO}@${CANARY_ADMIN_DIGEST}"
TARGET_EMS="${EMS_REPO}@${CANARY_EMS_DIGEST}"

require_digest() {
  case "$2" in
    sha256:*) ;;
    *) echo "$1 must be an immutable sha256: digest, got: $2" >&2; exit 1 ;;
  esac
}
require_digest CANARY_SOURCE_ADMIN_DIGEST "$CANARY_SOURCE_ADMIN_DIGEST"
require_digest CANARY_ADMIN_DIGEST "$CANARY_ADMIN_DIGEST"
require_digest CANARY_EMS_DIGEST "$CANARY_EMS_DIGEST"
if [[ "$CANARY_SOURCE_ADMIN_DIGEST" == "$CANARY_ADMIN_DIGEST" ]]; then
  echo "source and target Admin digests are identical; the replacement would assert nothing" >&2
  exit 1
fi

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

docker pull "$SOURCE_ADMIN"
docker pull "$TARGET_ADMIN"
docker pull "$TARGET_EMS"

# The shared page objects address one data-testid contract. Both published
# Admins must serve it, and a gap has to be named here rather than surfacing as
# a locator timeout deep inside the journey.
python3 scripts/admin_test_contract.py --role source --image "$SOURCE_ADMIN"
python3 scripts/admin_test_contract.py --role target --image "$TARGET_ADMIN"

# Compose addresses the source by tag; bind that tag to the resolved digest so
# the container that starts is the exact published image, not whatever the
# registry currently serves for the tag.
docker tag "$SOURCE_ADMIN" "${ADMIN_REPO}:${CANARY_SOURCE_TAG}"

sh deploy/admin/install-admin-console.sh \
  --tag "$CANARY_SOURCE_TAG" --bridge --bind 127.0.0.1 --port "$E2E_PORT" \
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
