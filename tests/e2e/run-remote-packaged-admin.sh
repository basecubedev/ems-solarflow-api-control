#!/usr/bin/env bash
set -euo pipefail

: "${REMOTE_ADMIN_IMAGE:?REMOTE_ADMIN_IMAGE is required}"
: "${REMOTE_CATALOGUE_SOURCE:?REMOTE_CATALOGUE_SOURCE is required}"
: "${CANARY_TAG:?CANARY_TAG is required}"

E2E_PORT="${EMS_ADMIN_REMOTE_E2E_PORT:-8125}"
CONTAINER_NAME="ems-admin-remote-system-build-${E2E_PORT}"
RUNTIME_DIR="$(mktemp -d -t ems-remote-admin-XXXXXX)"
mkdir -p "$RUNTIME_DIR/admin" "$RUNTIME_DIR/install"
chmod 0777 "$RUNTIME_DIR" "$RUNTIME_DIR/admin" "$RUNTIME_DIR/install"

cleanup_remote_admin() {
  docker rm -f "$CONTAINER_NAME" >/dev/null 2>&1 || true
  rm -rf "$RUNTIME_DIR"
}
trap cleanup_remote_admin EXIT INT TERM
cleanup_container() {
  docker rm -f "$CONTAINER_NAME" >/dev/null 2>&1 || true
}
cleanup_container

DOCKER_GID="$(stat -c %g /var/run/docker.sock)"
CATALOGUE_SOURCE="$REMOTE_CATALOGUE_SOURCE"
CATALOGUE_MOUNT=()
if [[ -f "$REMOTE_CATALOGUE_SOURCE" ]]; then
  CATALOGUE_SOURCE="/canary/development-builds.json"
  CATALOGUE_MOUNT=(
    --mount "type=bind,source=${REMOTE_CATALOGUE_SOURCE},target=${CATALOGUE_SOURCE},readonly"
  )
fi
docker run --rm --detach \
  --name "$CONTAINER_NAME" \
  --publish "127.0.0.1:${E2E_PORT}:8090" \
  --group-add "$DOCKER_GID" \
  --mount type=bind,source=/var/run/docker.sock,target=/var/run/docker.sock \
  --mount "type=bind,source=${RUNTIME_DIR},target=${RUNTIME_DIR}" \
  "${CATALOGUE_MOUNT[@]}" \
  --env "EMS_ADMIN_DEVELOPMENT_CATALOGUE=${CATALOGUE_SOURCE}" \
  --env "EMS_ADMIN_DATA_DIR=${RUNTIME_DIR}/admin" \
  --env "EMS_INSTALL_DIR=${RUNTIME_DIR}/install" \
  --env "EMS_ADMIN_CONTAINER_NAME=${CONTAINER_NAME}" \
  --env "EMS_ADMIN_IMAGE=ghcr.io/basecubedev/ems-solarflow-admin" \
  --env "EMS_ADMIN_TAG=${CANARY_TAG}" \
  --env DOCKER_CONFIG=/tmp/docker \
  "${REMOTE_ADMIN_IMAGE}" \
  python -B -m admin --host 0.0.0.0 --port 8090

docker logs --follow "$CONTAINER_NAME" &
REMOTE_LOG_PID=$!
wait "$REMOTE_LOG_PID"
