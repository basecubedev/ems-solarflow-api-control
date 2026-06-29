#!/bin/sh
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# Docker-first installer for EMS SolarFlow API control.
#
# Run from an empty folder, no repository checkout required:
#   sh install-docker.sh                 # EMS only
#   sh install-docker.sh --analytics     # EMS + Analytics (bundled InfluxDB)
#
# It writes docker-compose.yml, config/ and data/, creates config/config.json
# through the supported config-init flow, and (with --analytics) generates local
# bundled InfluxDB secrets in config/influxdb.env and starts the stack.

set -eu

IMAGE="ghcr.io/basecubedev/ems-solarflow-api-control"
TAG="latest"
ANALYTICS=0
START=1
DRY_RUN=0
FORCE=0

# The single docker-compose.yml uses `env_file: required: false`, added in
# Docker Compose v2.24.0.
MIN_COMPOSE_VERSION="2.24.0"

log() { printf '%s\n' "$*"; }
err() { printf 'error: %s\n' "$*" >&2; }
warn() { printf 'warning: %s\n' "$*" >&2; }

usage() {
    cat <<'EOF'
EMS SolarFlow Docker-first installer

Usage:
  sh install-docker.sh [options]

Options:
  --analytics       Enable Analytics (bundled InfluxDB) alongside EMS.
  --tag <tag>       Container image tag to use (default: latest).
  --no-start        Set everything up but do not start the stack.
  --dry-run         Show what would happen without writing or starting anything.
  --force           Overwrite an existing docker-compose.yml / config.json.
  --help            Show this help.

Examples:
  sh install-docker.sh
  sh install-docker.sh --analytics
  sh install-docker.sh --tag v0.6.0
  sh install-docker.sh --dry-run

After install:
  docker compose ps
  docker compose logs -f
  docker compose exec ems python3 emsctl.py diagnose
  docker compose exec ems python3 emsctl.py influx status   # with --analytics
EOF
}

while [ $# -gt 0 ]; do
    case "$1" in
        --analytics|-Analytics) ANALYTICS=1 ;;
        --no-analytics) ANALYTICS=0 ;;
        --tag) shift; [ $# -gt 0 ] || { err "--tag needs a value"; exit 2; }; TAG="$1" ;;
        --tag=*) TAG="${1#--tag=}" ;;
        --no-start|-NoStart) START=0 ;;
        --dry-run|-DryRun) DRY_RUN=1 ;;
        --force|-Force) FORCE=1 ;;
        --help|-Help|-h) usage; exit 0 ;;
        *) err "unknown option: $1"; usage >&2; exit 2 ;;
    esac
    shift
done

run() {
    if [ "$DRY_RUN" -eq 1 ]; then
        log "DRY-RUN: $*"
        return 0
    fi
    "$@"
}

# Report a missing/unsupported prerequisite. Fatal for a real install; in
# --dry-run it is only a warning, since no Docker command is executed.
prereq_problem() {
    if [ "$DRY_RUN" -eq 1 ]; then
        warn "$1"
        log "Dry-run continues because no Docker command will be executed."
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
        prereq_problem "Docker Compose v$MIN_COMPOSE_VERSION or newer is required because this setup uses optional env_file.required:false (found v$version)."
        return 0
    fi

    # Daemon reachability only matters for a real install.
    if [ "$DRY_RUN" -ne 1 ] && ! docker info >/dev/null 2>&1; then
        err "Cannot talk to the Docker daemon. Is it running and can this user access it?"
        exit 1
    fi
}

write_compose() {
    if [ -f docker-compose.yml ] && [ "$FORCE" -ne 1 ]; then
        log "Keeping existing docker-compose.yml (use --force to overwrite)."
        return 0
    fi
    if [ "$DRY_RUN" -eq 1 ]; then
        log "DRY-RUN: write docker-compose.yml (image tag: $TAG)"
        return 0
    fi
    cat > docker-compose.yml <<'YAML'
# EMS SolarFlow API control — Docker-first setup.
#
# EMS only:
#   docker compose up -d
#
# EMS + Analytics (bundled InfluxDB):
#   docker compose --profile with-analytics up -d
services:
  ems:
    image: ghcr.io/basecubedev/ems-solarflow-api-control:latest
    container_name: ems-solarflow-api-control
    restart: unless-stopped
    environment:
      PUID: "${PUID:-}"
      PGID: "${PGID:-}"
      EMS_IN_CONTAINER: "1"
    env_file:
      - path: ./config/influxdb.env
        required: false
    ports:
      - "8080:8080"
    volumes:
      - ./config:/app/config
      - ./data:/app/data

  influxdb:
    image: influxdb:2.7
    container_name: ems-influxdb
    profiles:
      - with-analytics
    ports:
      - "8086:8086"
    env_file:
      - ./config/influxdb.env
    volumes:
      - ./data/influxdb:/var/lib/influxdb2
    restart: unless-stopped
YAML
    if [ "$TAG" != "latest" ]; then
        sed "s|$IMAGE:latest|$IMAGE:$TAG|" docker-compose.yml > docker-compose.yml.tmp \
            && mv docker-compose.yml.tmp docker-compose.yml
    fi
    log "Wrote docker-compose.yml (image tag: $TAG)."
}

write_env() {
    # Local .env runs EMS as the invoking user. The Analytics profile is added
    # later by enable_analytics_profile, only after config/influxdb.env exists —
    # activating it earlier would make every `docker compose run` pull the
    # bundled InfluxDB service (required env_file) into scope before its secret
    # file is generated.
    uid=$(id -u 2>/dev/null || echo "")
    gid=$(id -g 2>/dev/null || echo "")
    if [ "$DRY_RUN" -eq 1 ]; then
        log "DRY-RUN: write .env (PUID/PGID)"
        return 0
    fi
    {
        if [ -n "$uid" ] && [ "$uid" != "0" ] && [ -n "$gid" ] && [ "$gid" != "0" ]; then
            printf 'PUID=%s\n' "$uid"
            printf 'PGID=%s\n' "$gid"
        fi
    } > .env
}

# Default plain `docker compose up -d` to the Analytics profile. Called only
# after config/influxdb.env exists so the bundled InfluxDB service can load it.
enable_analytics_profile() {
    if [ "$DRY_RUN" -eq 1 ]; then
        log "DRY-RUN: append COMPOSE_PROFILES=with-analytics to .env"
        return 0
    fi
    printf 'COMPOSE_PROFILES=with-analytics\n' >> .env
}

compose() {
    run docker compose "$@"
}

main() {
    require_docker

    run mkdir -p config data
    if [ "$ANALYTICS" -eq 1 ]; then
        run mkdir -p data/influxdb
    fi

    write_compose
    write_env

    # For Analytics we enable bundled InfluxDB in config.json up front via the
    # supported config-init flow, then generate the local secret file. EMS-only
    # leaves config.json to the container's first-start template seeding.
    if [ "$ANALYTICS" -eq 1 ]; then
        if [ -f config/config.json ] && [ "$FORCE" -ne 1 ]; then
            log "Keeping existing config/config.json (use --force to re-run config init)."
        else
            compose run --rm ems python3 emsctl.py config init --analytics --yes --no-backup
        fi
        compose run --rm ems python3 emsctl.py influx init --no-start
        enable_analytics_profile
    fi

    if [ "$START" -eq 0 ] || [ "$DRY_RUN" -eq 1 ]; then
        log ""
        log "Setup prepared. Start it with:"
        if [ "$ANALYTICS" -eq 1 ]; then
            log "  docker compose --profile with-analytics up -d"
        else
            log "  docker compose up -d"
            log "(config/config.json is created from the template on first start.)"
        fi
        return 0
    fi

    compose up -d

    if [ "$ANALYTICS" -eq 1 ]; then
        compose exec -T ems python3 emsctl.py influx sync || \
            log "Analytics sync will retry on next run; check 'docker compose logs -f influxdb'."
        compose exec -T ems python3 emsctl.py influx status || true
    fi

    log ""
    log "EMS is starting."
    log "  dashboard:  http://localhost:8080"
    if [ "$ANALYTICS" -eq 1 ]; then
        log "  analytics:  http://localhost:8086 (bundled InfluxDB)"
    fi
    log ""
    log "Useful commands:"
    log "  docker compose ps"
    log "  docker compose logs -f"
    log "  docker compose exec ems python3 emsctl.py diagnose"
    if [ "$ANALYTICS" -eq 1 ]; then
        log "  docker compose exec ems python3 emsctl.py influx status"
    fi
    log ""
    log "Next: choose your grid meter and set your devices in config/config.json,"
    log "or run the guided setup assistant:"
    log "  docker compose exec ems python3 emsctl.py config init"
    log "For Zendure SmartMeter D0, select \"Zendure SmartMeter D0 via MQTT\"."
    log "Then restart EMS with: docker compose restart"
}

main
