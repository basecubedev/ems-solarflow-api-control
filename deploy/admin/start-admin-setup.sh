#!/bin/sh
# SPDX-License-Identifier: AGPL-3.0-or-later
# Start the EMS SolarFlow Admin Console from a source checkout (build from
# source). For normal users, use install-admin-console.sh instead — it runs the
# published image with no Git checkout required.
#
# Default mode is deployment-capable: the Admin container controls the host
# Docker engine through /var/run/docker.sock. No Docker daemon runs inside the
# Admin container.
#
# Usage:
#   deploy/admin/start-admin-setup.sh                    # deployment-capable
#   deploy/admin/start-admin-setup.sh --hostnet          # + host net for LAN discovery
#   deploy/admin/start-admin-setup.sh --discovery-only   # restricted, no Docker socket
#
# SECURITY: the default mode mounts /var/run/docker.sock, which grants
# effectively root-equivalent control of the host. Run it only on a trusted
# local machine and never expose the Admin UI to the internet.
set -eu

here="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
project_root="$(CDPATH= cd -- "$here/../.." && pwd)"
admin_data_dir="${EMS_ADMIN_DATA_DIR:-$project_root/data/admin}"

# Same-path mounting: the Admin container drives the host Docker daemon, so bind
# mount sources it forwards must be valid host paths. Exporting the real install
# root lets ems.paths resolve config/data/compose under it instead of /app.
export EMS_INSTALL_DIR="$project_root"
export EMS_ADMIN_DATA_DIR="$admin_data_dir"

mode="deployment"
hostnet=""
for arg in "$@"; do
  case "$arg" in
    --discovery-only) mode="discovery" ;;
    --hostnet) hostnet="1" ;;
    -h | --help)
      sed -n '3,19p' "$0"
      exit 0
      ;;
    *)
      echo "unknown option: $arg" >&2
      exit 2
      ;;
  esac
done

resolve_local_system_build_identity() {
  if ! command -v git >/dev/null 2>&1; then
    echo "Cannot derive local System Build metadata: git is not installed." >&2
    exit 1
  fi

  if ! revision="$(git -C "$project_root" rev-parse --verify HEAD 2>/dev/null)"; then
    echo "Cannot derive local System Build revision from the repository Git metadata." >&2
    exit 1
  fi
  case "$revision" in
    *[!0-9a-f]*|'')
      echo "Local System Build revision is invalid: expected a full lowercase Git SHA." >&2
      exit 1
      ;;
  esac
  if [ "${#revision}" -ne 40 ]; then
    echo "Local System Build revision is invalid: expected a full 40-character Git SHA." >&2
    exit 1
  fi

  if ! git_status="$(git -C "$project_root" status --porcelain --untracked-files=normal 2>/dev/null)"; then
    echo "Cannot determine whether the local repository is dirty." >&2
    exit 1
  fi
  short_revision="$(printf '%s' "$revision" | cut -c1-7)"
  short_revision_12="$(printf '%s' "$revision" | cut -c1-12)"

  SYSTEM_TAG="local"
  SYSTEM_CHANNEL="development"
  SYSTEM_REVISION="$revision"
  SYSTEM_BUILD_ID="local-$short_revision"
  SYSTEM_GIT_DIRTY="false"
  if [ -n "$git_status" ]; then
    SYSTEM_BUILD_ID="$SYSTEM_BUILD_ID-dirty"
    SYSTEM_GIT_DIRTY="true"
  fi
  SYSTEM_RELEASE_TAG="local"
  SYSTEM_REVISION_SHORT="$short_revision_12"
  export SYSTEM_TAG SYSTEM_CHANNEL SYSTEM_REVISION SYSTEM_REVISION_SHORT
  export SYSTEM_BUILD_ID SYSTEM_GIT_DIRTY SYSTEM_RELEASE_TAG
}

is_positive_id() {
  case "${1:-}" in
    ''|*[!0-9]*|0) return 1 ;;
    *) return 0 ;;
  esac
}

id_pair_or_fail() {
  PUID="${PUID:-$(id -u)}"
  PGID="${PGID:-$(id -g)}"
  if ! is_positive_id "$PUID" || ! is_positive_id "$PGID"; then
    echo "Admin Setup requires non-root numeric PUID and PGID." >&2
    exit 1
  fi
  export PUID PGID
}

perm_has_exec() {
  case "$1" in
    x|s|t) return 0 ;;
    *) return 1 ;;
  esac
}

runtime_can_write_dir() {
  path="$1"
  dir_uid="$(stat -c '%u' "$path" 2>/dev/null || printf '')"
  dir_gid="$(stat -c '%g' "$path" 2>/dev/null || printf '')"
  dir_perm="$(stat -c '%A' "$path" 2>/dev/null || printf '')"

  owner_w="$(printf '%s' "$dir_perm" | cut -c3)"
  owner_x="$(printf '%s' "$dir_perm" | cut -c4)"
  group_w="$(printf '%s' "$dir_perm" | cut -c6)"
  group_x="$(printf '%s' "$dir_perm" | cut -c7)"
  other_w="$(printf '%s' "$dir_perm" | cut -c9)"
  other_x="$(printf '%s' "$dir_perm" | cut -c10)"

  if [ "$dir_uid" = "$PUID" ] && [ "$owner_w" = "w" ] && perm_has_exec "$owner_x"; then
    return 0
  fi
  if [ "$dir_gid" = "$PGID" ] && [ "$group_w" = "w" ] && perm_has_exec "$group_x"; then
    return 0
  fi
  if [ "$other_w" = "w" ] && perm_has_exec "$other_x"; then
    return 0
  fi
  return 1
}

require_runtime_writable_dir() {
  path="$1"
  if runtime_can_write_dir "$path"; then
    return 0
  fi

  echo "Admin data directory is not writable for PUID=${PUID} PGID=${PGID}: $path" >&2
  echo "Fix the host ownership and retry:" >&2
  echo "  sudo chown -R ${PUID}:${PGID} '$admin_data_dir'" >&2
  exit 1
}

# Admin-owned state, staging, release cache, and backups live under data/admin/.
# The live EMS runtime (config/, data/, docker-compose.yml) lives in the install
# root, not here.
prepare_admin_data_dir() {
  if ! mkdir -p \
    "$admin_data_dir" \
    "$admin_data_dir/releases" \
    "$admin_data_dir/state" \
    "$admin_data_dir/staging" \
    "$admin_data_dir/backups"
  then
    echo "Cannot create Admin data directory: $admin_data_dir" >&2
    echo "Fix the host ownership and retry:" >&2
    echo "  sudo mkdir -p '$admin_data_dir'" >&2
    echo "  sudo chown -R ${PUID}:${PGID} '$project_root/data'" >&2
    exit 1
  fi
  require_runtime_writable_dir "$admin_data_dir"
  require_runtime_writable_dir "$admin_data_dir/releases"
  require_runtime_writable_dir "$admin_data_dir/state"
  require_runtime_writable_dir "$admin_data_dir/staging"
  require_runtime_writable_dir "$admin_data_dir/backups"
}

resolve_local_system_build_identity
id_pair_or_fail
prepare_admin_data_dir

echo "Using PUID=${PUID} PGID=${PGID} for the Admin and EMS deployment workspace." >&2
echo "Using admin data directory ${admin_data_dir}." >&2
echo "Using local System Build ${SYSTEM_BUILD_ID} (${SYSTEM_REVISION})." >&2

socket="${EMS_ADMIN_DOCKER_SOCKET:-/var/run/docker.sock}"

if [ "$mode" = "discovery" ]; then
  files="-f $here/docker-compose.discovery-only.yml"
else
  files="-f $here/docker-compose.yml"
  DOCKER_GID="$(stat -c '%g' "$socket" 2>/dev/null || true)"
  if [ -z "${DOCKER_GID}" ]; then
    DOCKER_GID="$(getent group docker 2>/dev/null | cut -d: -f3 || true)"
  fi
  export DOCKER_GID
  echo "Using DOCKER_GID=${DOCKER_GID:-<unknown>} for host Docker socket ${socket}." >&2
fi

if [ -n "$hostnet" ]; then
  files="$files -f $here/docker-compose.hostnet.yml"
fi

# Build both sides of the local System Build from the same Git-derived identity.
# Compose owns the Admin build configuration; the additional fixed tag is the
# server-side repository reference embedded in the Admin release resources.
# The EMS image is built directly because it is intentionally not a service in
# the Admin Setup Compose project.
# shellcheck disable=SC2086
docker compose $files build
docker image tag \
  ems-solarflow-control-admin:local \
  ghcr.io/basecubedev/ems-solarflow-admin:local

docker build \
  --build-arg EMS_RELEASE_TAG="$SYSTEM_RELEASE_TAG" \
  --build-arg EMS_GIT_COMMIT="$SYSTEM_REVISION" \
  --build-arg EMS_GIT_COMMIT_SHORT="$SYSTEM_REVISION_SHORT" \
  --build-arg EMS_GIT_DESCRIBE="$SYSTEM_BUILD_ID" \
  --build-arg EMS_GIT_BRANCH=local \
  --build-arg EMS_GIT_DIRTY="$SYSTEM_GIT_DIRTY" \
  --build-arg EMS_BUILD_ID="$SYSTEM_BUILD_ID" \
  --build-arg EMS_BUILD_SERIAL=0 \
  --build-arg EMS_CHANNEL="$SYSTEM_CHANNEL" \
  --tag ghcr.io/basecubedev/ems-solarflow-api-control:local \
  "$project_root"

# shellcheck disable=SC2086
exec docker compose $files up
