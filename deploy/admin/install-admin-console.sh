#!/bin/sh
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# EMS SolarFlow Admin Console installer (no Git checkout required).
#
# Installs and starts the Admin Console from the published GHCR image. Run it
# from the directory that should hold your EMS install:
#
#   sh install-admin-console.sh                 # host networking, deployment-capable
#   sh install-admin-console.sh --bridge        # Docker bridge networking instead
#   sh install-admin-console.sh --discovery-only # restricted, no Docker socket
#
# It writes a self-contained docker-compose.admin.yml (published image, no build,
# no repository checkout), creates config/ and data/admin/, and starts the
# Admin Console on http://127.0.0.1:8090.
#
# EMS SolarFlow is a local LAN appliance, so the default uses host networking:
# device discovery sees the real LAN like a local host process. Bridge networking
# is available with --bridge for restricted environments; discovery is then less
# reliable.
#
# SECURITY: the default mode mounts /var/run/docker.sock, which grants
# effectively root-equivalent control of the host Docker engine. Run it only on a
# trusted local machine or trusted LAN and never expose the Admin UI to the
# internet.

set -eu

IMAGE="ghcr.io/basecubedev/ems-solarflow-admin"
TAG="latest"
CONTAINER_NAME="ems-solarflow-admin" # stable name so Admin can update itself
COMPOSE_SERVICE="ems-solarflow-admin" # compose service name for `docker compose up`
MODE="deployment" # deployment | discovery
NETWORK="host"     # host | bridge
BIND="127.0.0.1"   # bridge-mode publish address
PORT="8090"        # bridge-mode publish port
HTTPS=0            # optional parallel HTTPS listener
HTTPS_PORT="8091"  # HTTPS listener / bridge-mode publish port
HTTPS_BIND=""      # bridge-mode HTTPS publish address (defaults to --bind)
HTTPS_AUTO_GENERATE="true"
START=1
DRY_RUN=0
FORCE=0
INSTALL_DIR=""
COMPOSE_FILE="docker-compose.admin.yml"
ENV_FILE=".env.admin"

# `env_file: required: false` is not used here, but keep the same Compose v2
# baseline as the Docker Bootstrap installer so users need only one Docker.
MIN_COMPOSE_VERSION="2.24.0"

log() { printf '%s\n' "$*"; }
err() { printf 'error: %s\n' "$*" >&2; }
warn() { printf 'warning: %s\n' "$*" >&2; }

usage() {
    cat <<'EOF'
EMS SolarFlow Admin Console installer

Usage:
  sh install-admin-console.sh [options]

The default uses host networking so device discovery can see your LAN; this is
normal local-appliance behaviour. Use --bridge only when you need Docker bridge
networking instead.

Options:
  --tag <tag>          Admin image tag to use (default: latest).
  --bridge             Use Docker bridge networking instead of host networking.
  --bind <host>        Bridge-mode publish address (default: 127.0.0.1).
  --port <port>        UI port (default: 8090). Publish port in bridge mode.
  --https              Also start an optional HTTPS listener (HTTP stays on 8090).
  --https-port <port>  HTTPS port (default: 8091).
  --https-bind <addr>  Bridge-mode HTTPS publish address (default: follows --bind).
  --no-https-auto-generate
                       Do not auto-generate a self-signed Admin certificate.
  --discovery-only     Do not mount the Docker socket (deployment disabled).
  --install-dir <path> Install location (default: current directory).
  --no-start           Write files but do not start the Admin Console.
  --dry-run            Show what would happen without writing or starting.
  --force              Overwrite an existing docker-compose.admin.yml / env.
  --help               Show this help.

Examples:
  sh install-admin-console.sh
  sh install-admin-console.sh --bridge
  sh install-admin-console.sh --tag v0.6.0
  sh install-admin-console.sh --discovery-only

After install:
  docker compose -f docker-compose.admin.yml ps
  docker compose -f docker-compose.admin.yml logs -f
  docker compose -f docker-compose.admin.yml down
EOF
}

while [ $# -gt 0 ]; do
    case "$1" in
        --tag) shift; [ $# -gt 0 ] || { err "--tag needs a value"; exit 2; }; TAG="$1" ;;
        --tag=*) TAG="${1#--tag=}" ;;
        --bridge) NETWORK="bridge" ;;
        --bind) shift; [ $# -gt 0 ] || { err "--bind needs a value"; exit 2; }; BIND="$1" ;;
        --bind=*) BIND="${1#--bind=}" ;;
        --port) shift; [ $# -gt 0 ] || { err "--port needs a value"; exit 2; }; PORT="$1" ;;
        --port=*) PORT="${1#--port=}" ;;
        --https) HTTPS=1 ;;
        --https-port) shift; [ $# -gt 0 ] || { err "--https-port needs a value"; exit 2; }; HTTPS_PORT="$1" ;;
        --https-port=*) HTTPS_PORT="${1#--https-port=}" ;;
        --https-bind) shift; [ $# -gt 0 ] || { err "--https-bind needs a value"; exit 2; }; HTTPS_BIND="$1" ;;
        --https-bind=*) HTTPS_BIND="${1#--https-bind=}" ;;
        --no-https-auto-generate) HTTPS_AUTO_GENERATE="false" ;;
        # Host networking is already the default; accept the old flag as a no-op.
        --hostnet) log "Host networking is already the default." ;;
        --discovery-only) MODE="discovery" ;;
        --install-dir) shift; [ $# -gt 0 ] || { err "--install-dir needs a value"; exit 2; }; INSTALL_DIR="$1" ;;
        --install-dir=*) INSTALL_DIR="${1#--install-dir=}" ;;
        --no-start) START=0 ;;
        --dry-run) DRY_RUN=1 ;;
        --force) FORCE=1 ;;
        --help|-h) usage; exit 0 ;;
        *) err "unknown option: $1"; usage >&2; exit 2 ;;
    esac
    shift
done

# --bind / --port only shape the bridge-mode published port. With host
# networking the server binds 8090 on every host address directly.
if [ "$NETWORK" = "host" ] && { [ "$BIND" != "127.0.0.1" ] || [ "$PORT" != "8090" ]; }; then
    warn "--bind/--port apply to bridge mode only; host networking always serves on 8090."
fi

# Bridge-mode HTTPS publish address follows --bind unless set explicitly.
HTTPS_BIND="${HTTPS_BIND:-$BIND}"

run() {
    if [ "$DRY_RUN" -eq 1 ]; then
        log "DRY-RUN: $*"
        return 0
    fi
    "$@"
}

# Missing/unsupported prerequisite: fatal for a real install (starts a container),
# only a warning when no container is started now (--dry-run / --no-start).
prereq_problem() {
    if [ "$DRY_RUN" -eq 1 ] || [ "$START" -eq 0 ]; then
        warn "$1"
        log "Continuing because the Admin Console will not be started now."
        return 0
    fi
    err "$1"
    exit 1
}

# True when dotted version $1 >= $2 (numeric per-component compare).
version_ge() {
    awk -v a="$1" -v b="$2" 'BEGIN {
        n = split(a, x, "."); m = split(b, y, ".");
        for (i = 1; i <= 3; i++) {
            xi = (i <= n) ? x[i] + 0 : 0;
            yi = (i <= m) ? y[i] + 0 : 0;
            if (xi > yi) exit 0;
            if (xi < yi) exit 1;
        }
        exit 0;
    }'
}

is_positive_id() {
    case "${1:-}" in
        ''|*[!0-9]*|0) return 1 ;;
        *) return 0 ;;
    esac
}

require_docker() {
    if ! command -v docker >/dev/null 2>&1; then
        prereq_problem "Docker is not installed. See https://docs.docker.com/engine/install/"
        return 0
    fi
    if ! docker compose version >/dev/null 2>&1; then
        prereq_problem "'docker compose' (Compose v2) is not available. Update Docker."
        return 0
    fi
    version=$(docker compose version --short 2>/dev/null | sed 's/^v//')
    if [ -n "$version" ] && ! version_ge "$version" "$MIN_COMPOSE_VERSION"; then
        prereq_problem "Docker Compose v$MIN_COMPOSE_VERSION or newer is required (found v$version)."
        return 0
    fi
    # A live daemon is only needed to actually start the container. --dry-run and
    # --no-start write files without touching the daemon.
    if [ "$START" -eq 1 ] && [ "$DRY_RUN" -ne 1 ] && ! docker info >/dev/null 2>&1; then
        err "Cannot talk to the Docker daemon. Is it running and can this user access it?"
        exit 1
    fi
}

# The container runs non-root and same-path mounts share the invoking user's
# identity, so a real non-root PUID/PGID is required to start. When only writing
# files (--dry-run / --no-start) this is a warning so config can be generated in
# CI/root sandboxes; a real start still refuses root.
resolve_ids() {
    PUID="${PUID:-$(id -u)}"
    PGID="${PGID:-$(id -g)}"
    if is_positive_id "$PUID" && is_positive_id "$PGID"; then
        return 0
    fi
    if [ "$START" -eq 1 ] && [ "$DRY_RUN" -ne 1 ]; then
        err "The Admin Console needs a non-root numeric user. Run it as a normal user, not root."
        exit 1
    fi
    warn "Non-root PUID/PGID are required before starting; run as a normal user before 'docker compose up'."
}

# Deployment mode joins the host Docker socket group; discovery-only never
# touches the socket, so DOCKER_GID stays unset there.
resolve_docker_gid() {
    socket="${EMS_ADMIN_DOCKER_SOCKET:-/var/run/docker.sock}"
    DOCKER_GID="$(stat -c '%g' "$socket" 2>/dev/null || true)"
    if [ -z "$DOCKER_GID" ]; then
        DOCKER_GID="$(getent group docker 2>/dev/null | cut -d: -f3 || true)"
    fi
    if [ -z "$DOCKER_GID" ]; then
        DOCKER_GID="999"
        warn "Could not detect the Docker socket group; using DOCKER_GID=999."
    fi
}

make_dirs() {
    run mkdir -p \
        config \
        data \
        data/admin \
        data/admin/releases \
        data/admin/state \
        data/admin/staging \
        data/admin/backups
}

require_writable_admin_dir() {
    [ "$DRY_RUN" -eq 1 ] && return 0
    probe="data/admin/.write-test.$$"
    if ( : > "$probe" ) 2>/dev/null; then
        rm -f "$probe"
        return 0
    fi
    err "Admin data directory is not writable: $install_dir/data/admin"
    err "Fix the host ownership and retry:"
    err "  sudo chown -R \"\$(id -u):\$(id -g)\" data"
    exit 1
}

# Emit the self-contained compose file to stdout. Values are baked in so the
# file needs no .env and no repository checkout to run.
emit_compose() {
    if [ "$HTTPS" -eq 1 ]; then
        expose_ports="ports 8090/${HTTPS_PORT}"
    else
        expose_ports="port 8090"
    fi
    cat <<EOF
# SPDX-License-Identifier: AGPL-3.0-or-later
# Generated by install-admin-console.sh — EMS SolarFlow Admin Console runtime.
# Self-contained: published image, resolved host paths, no build, no checkout.
#
# SECURITY: run only on a trusted local machine or trusted LAN; never expose
# ${expose_ports} to the internet.
EOF
    if [ "$NETWORK" = "host" ]; then
        cat <<EOF
# Host networking: the UI is reachable on every LAN address of this host and
# discovery sees the real LAN.
EOF
        if [ "$HTTPS" -eq 1 ]; then
            cat <<EOF
# Optional HTTPS also listens on ${HTTPS_PORT} (HTTP on 8090 stays available).
EOF
        fi
    else
        cat <<EOF
# Bridge networking: the UI is published on ${BIND}:${PORT}; LAN discovery may be
# less reliable.
EOF
    fi
    if [ "$MODE" = "deployment" ]; then
        cat <<EOF
# This mode mounts /var/run/docker.sock (root-equivalent host control).
EOF
    else
        cat <<EOF
# Discovery-only: no Docker socket is mounted; deployment actions are disabled.
EOF
    fi
    cat <<EOF
services:
  ems-solarflow-admin:
    image: ${IMAGE}:${TAG}
    container_name: ${CONTAINER_NAME}
    user: "${PUID}:${PGID}"
EOF
    if [ "$MODE" = "deployment" ]; then
        cat <<EOF
    group_add:
      - "${DOCKER_GID}"
EOF
    fi
    if [ "$NETWORK" = "host" ]; then
        # Host networking binds the UI to every host address on 8090 and lets
        # discovery see the real LAN; no Docker port mapping applies.
        cat <<EOF
    network_mode: host
EOF
    else
        # Bridge networking: publish the UI on the chosen host address and port.
        cat <<EOF
    ports:
      - "${BIND}:${PORT}:8090"
EOF
        # Publish the optional HTTPS port only when enabled; loopback by default.
        if [ "$HTTPS" -eq 1 ]; then
            cat <<EOF
      - "${HTTPS_BIND}:${HTTPS_PORT}:8091"
EOF
        fi
    fi
    cat <<EOF
    environment:
      EMS_INSTALL_DIR: "${install_dir}"
      EMS_ADMIN_DATA_DIR: "${admin_data_dir}"
      PUID: "${PUID}"
      PGID: "${PGID}"
      # Non-secret Admin identity so the Admin Console can update itself before a
      # Guided EMS Upgrade (target image derived from a trusted release tag).
      EMS_ADMIN_IMAGE: "${IMAGE}"
      EMS_ADMIN_TAG: "${TAG}"
      EMS_ADMIN_COMPOSE_FILE: "${install_dir}/${COMPOSE_FILE}"
      # Compose service name (docker compose up) is separate from the container
      # name (Docker inspect identity); they default to the same value.
      EMS_ADMIN_COMPOSE_SERVICE: "${COMPOSE_SERVICE}"
      EMS_ADMIN_CONTAINER_NAME: "${CONTAINER_NAME}"
EOF
    if [ "$MODE" = "deployment" ]; then
        cat <<EOF
      DOCKER_CONFIG: /tmp/docker
EOF
    fi
    # Optional parallel HTTPS listener; HTTP on 8090 always stays available.
    if [ "$HTTPS" -eq 1 ]; then
        https_enabled="true"
    else
        https_enabled="false"
    fi
    cat <<EOF
      EMS_ADMIN_HTTPS_ENABLED: "${https_enabled}"
      EMS_ADMIN_HTTPS_PORT: "${HTTPS_PORT}"
      EMS_ADMIN_HTTPS_CERT_FILE: "config/admin.crt"
      EMS_ADMIN_HTTPS_KEY_FILE: "config/admin.key"
      EMS_ADMIN_HTTPS_AUTO_GENERATE: "${HTTPS_AUTO_GENERATE}"
EOF
    cat <<EOF
    volumes:
      - "${install_dir}:${install_dir}"
      - "${admin_data_dir}:${admin_data_dir}"
EOF
    if [ "$MODE" = "deployment" ]; then
        cat <<EOF
      - /var/run/docker.sock:/var/run/docker.sock
EOF
    fi
    cat <<EOF
    read_only: true
    tmpfs:
      - /tmp:size=16m,mode=1777
    cap_drop:
      - ALL
    security_opt:
      - no-new-privileges:true
    restart: "no"
EOF
}

write_compose() {
    if [ -f "$COMPOSE_FILE" ] && [ "$FORCE" -ne 1 ]; then
        log "Keeping existing $COMPOSE_FILE (use --force to overwrite)."
        return 0
    fi
    if [ "$DRY_RUN" -eq 1 ]; then
        log "DRY-RUN: write $COMPOSE_FILE (image ${IMAGE}:${TAG}, mode ${MODE})"
        return 0
    fi
    emit_compose > "$COMPOSE_FILE"
    log "Wrote $COMPOSE_FILE (image ${IMAGE}:${TAG}, mode ${MODE})."
}

# .env.admin records the resolved identity for reference; the values are already
# baked into the compose file above.
write_env() {
    if [ "$DRY_RUN" -eq 1 ]; then
        log "DRY-RUN: write $ENV_FILE (EMS_INSTALL_DIR, EMS_ADMIN_DATA_DIR, DOCKER_GID)"
        return 0
    fi
    if [ -f "$ENV_FILE" ] && [ "$FORCE" -ne 1 ]; then
        log "Keeping existing $ENV_FILE (use --force to overwrite)."
        return 0
    fi
    {
        printf '# Resolved Admin Console identity (baked into %s).\n' "$COMPOSE_FILE"
        printf 'EMS_INSTALL_DIR=%s\n' "$install_dir"
        printf 'EMS_ADMIN_DATA_DIR=%s\n' "$admin_data_dir"
        printf 'EMS_ADMIN_IMAGE=%s\n' "$IMAGE"
        printf 'EMS_ADMIN_TAG=%s\n' "$TAG"
        printf 'EMS_ADMIN_COMPOSE_SERVICE=%s\n' "$COMPOSE_SERVICE"
        printf 'EMS_ADMIN_CONTAINER_NAME=%s\n' "$CONTAINER_NAME"
        printf 'PUID=%s\n' "$PUID"
        printf 'PGID=%s\n' "$PGID"
        if [ "$MODE" = "deployment" ]; then
            printf 'DOCKER_GID=%s\n' "$DOCKER_GID"
        fi
        if [ "$NETWORK" = "bridge" ]; then
            printf 'EMS_ADMIN_BIND=%s\n' "$BIND"
            printf 'EMS_ADMIN_PORT=%s\n' "$PORT"
        fi
    } > "$ENV_FILE"
}

# Host networking always serves 8090; bridge mode uses the chosen publish port.
ui_port() {
    if [ "$NETWORK" = "host" ]; then
        printf '8090'
    else
        printf '%s' "$PORT"
    fi
}

print_open_urls() {
    port="$(ui_port)"
    log "Open:"
    log "  http://127.0.0.1:${port}"
    if [ "$HTTPS" -eq 1 ]; then
        log "  https://127.0.0.1:${HTTPS_PORT}"
        log ""
        log "Your browser may show a certificate warning for the generated local"
        log "certificate. This is expected for self-signed certificates."
    fi
    if [ "$NETWORK" = "host" ]; then
        log ""
        log "If this runs on a headless LAN host, open:"
        log "  http://<host-ip>:${port}"
    fi
}

print_security_block() {
    port="$(ui_port)"
    log ""
    log "Security:"
    log "  Use only on a trusted local machine or trusted LAN."
    if [ "$HTTPS" -eq 1 ]; then
        log "  Do not expose ports ${port}/${HTTPS_PORT} to the internet."
    else
        log "  Do not expose port ${port} to the internet."
    fi
    if [ "$MODE" = "deployment" ]; then
        log "  Deployment mode mounts /var/run/docker.sock so Admin can manage local EMS containers."
    else
        log "  Discovery-only mode mounts no Docker socket; deployment actions are disabled."
    fi
}

print_next_steps() {
    log ""
    if [ "$NETWORK" = "bridge" ]; then
        log "Bridge mode started."
        log "Automatic LAN discovery may be less reliable in this mode."
    else
        log "Admin Console started."
    fi
    if [ "$MODE" = "discovery" ]; then
        log "Discovery-only mode: Docker deployment actions are disabled."
    fi
    log ""
    print_open_urls
    log ""
    log "Useful checks:"
    log "  docker compose -f $COMPOSE_FILE ps"
    log "  docker compose -f $COMPOSE_FILE logs -f"
    log ""
    log "Stop:"
    log "  docker compose -f $COMPOSE_FILE down"
    print_security_block
}

print_no_start() {
    log ""
    if [ "$DRY_RUN" -eq 1 ]; then
        log "Dry-run complete. No files were written and nothing was started."
    else
        log "Admin Console files written."
    fi
    log "Start later:"
    log "  docker compose -f $COMPOSE_FILE up -d"
    log ""
    log "Then open http://127.0.0.1:$(ui_port)"
    if [ "$HTTPS" -eq 1 ]; then
        log "  or https://127.0.0.1:${HTTPS_PORT}"
        log ""
        log "Your browser may show a certificate warning for the generated local"
        log "certificate. This is expected for self-signed certificates."
    fi
    print_security_block
}

main() {
    if [ -n "$INSTALL_DIR" ]; then
        run mkdir -p "$INSTALL_DIR"
        if [ "$DRY_RUN" -ne 1 ]; then
            cd "$INSTALL_DIR" || { err "cannot enter install dir: $INSTALL_DIR"; exit 1; }
        fi
    fi
    # Absolute host paths: the Admin container forwards these as bind mounts to
    # the host Docker daemon, so they must be valid host paths (same-path mount).
    install_dir="$(pwd -P)"
    admin_data_dir="$install_dir/data/admin"

    require_docker
    resolve_ids
    if [ "$MODE" = "deployment" ]; then
        resolve_docker_gid
    fi

    make_dirs
    require_writable_admin_dir
    write_compose
    write_env

    log ""
    log "Install dir:   $install_dir"
    log "Admin data:    $admin_data_dir"
    log "Image:         ${IMAGE}:${TAG}"
    log "Mode:          $MODE"
    log "Networking:    $NETWORK$( [ "$NETWORK" = "bridge" ] && printf ' (%s:%s)' "$BIND" "$PORT" || true )"
    if [ "$HTTPS" -eq 1 ]; then
        log "HTTPS:         enabled (port ${HTTPS_PORT})"
    else
        log "HTTPS:         disabled"
    fi
    if [ "$MODE" = "deployment" ]; then
        log "DOCKER_GID:    ${DOCKER_GID}"
    fi

    if [ "$START" -eq 0 ] || [ "$DRY_RUN" -eq 1 ]; then
        print_no_start
        return 0
    fi

    run docker compose -f "$COMPOSE_FILE" up -d

    print_next_steps
}

main
