#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
E2E_PORT="${EMS_ADMIN_PACKAGED_E2E_PORT:-8124}"
IMAGE_NAME="ems-solarflow-admin:system-build-browser-gate"
CONTAINER_NAME="ems-admin-system-build-browser-gate-${E2E_PORT}"
DEVELOPMENT_TAG="dev-development-deadbee-100-1"

cleanup_packaged_admin() {
  docker rm -f "$CONTAINER_NAME" >/dev/null 2>&1 || true
}
trap cleanup_packaged_admin EXIT INT TERM
cleanup_packaged_admin

cd "$PROJECT_ROOT"
# CI can prebuild "$IMAGE_NAME" with a cache and set EMS_ADMIN_PACKAGED_SKIP_BUILD;
# local runs build normally with plain Docker and no cache dependency.
if [ -z "${EMS_ADMIN_PACKAGED_SKIP_BUILD:-}" ]; then
  docker build -f deploy/admin/Dockerfile \
    --build-arg EMS_REVISION=deadbee1234567890abcdef1234567890abcdef1 \
    --build-arg EMS_BUILD_ID="$DEVELOPMENT_TAG" \
    --build-arg EMS_CHANNEL=development \
    --build-arg EMS_RELEASE_TAG="$DEVELOPMENT_TAG" \
    --build-arg EMS_SYSTEM_TAG="$DEVELOPMENT_TAG" \
    -t "$IMAGE_NAME" .
fi

docker run --rm --detach \
  --name "$CONTAINER_NAME" \
  --publish "127.0.0.1:${E2E_PORT}:8090" \
  --env EMS_ADMIN_TEST_MODE=1 \
  --env EMS_ADMIN_TEST_PACKAGED_RESOURCES=1 \
  --env EMS_INSTALL_DIR=/data/install \
  "$IMAGE_NAME" \
  sh -c 'mkdir -p /data/install && exec python -B -m admin --host 0.0.0.0 --port 8090'

docker logs --follow "$CONTAINER_NAME" &
PACKAGED_LOG_PID=$!
wait "$PACKAGED_LOG_PID"
