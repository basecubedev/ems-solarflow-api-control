# SPDX-License-Identifier: AGPL-3.0-or-later
# EMS SolarFlow Control Admin device-discovery container. Stdlib http.server,
# requests, and zeroconf: no Node, no build toolchain, no database.
# Copies just the files needed for discovery so the image stays small enough for
# a Raspberry Pi. mDNS needs host networking to see LAN devices.
#
# Docker-out-of-Docker: the Admin container controls the *host* Docker engine
# through the mounted /var/run/docker.sock (see docker-compose.deployment.yml).
# It ships only the Docker CLI and the Compose plugin — never a daemon: no
# dockerd, no Docker-in-Docker, no privileged mode. In discovery-only preview
# mode the socket is simply not mounted and the CLI reports it as unavailable.
FROM python:3.14-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV EMS_ADMIN_DATA_DIR=/data

# BuildKit provides TARGETARCH; pin the client versions for reproducible builds.
ARG TARGETARCH
ARG DOCKER_CLI_VERSION=27.5.1
ARG DOCKER_COMPOSE_VERSION=2.32.4

WORKDIR /app

# Docker client (CLI + Compose plugin) as static binaries — no daemon package.
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends ca-certificates curl; \
    case "${TARGETARCH:-amd64}" in \
      amd64) dockerArch=x86_64; composeArch=x86_64 ;; \
      arm64) dockerArch=aarch64; composeArch=aarch64 ;; \
      arm) dockerArch=armhf; composeArch=armv7 ;; \
      *) echo "unsupported TARGETARCH: ${TARGETARCH}" >&2; exit 1 ;; \
    esac; \
    curl -fsSL "https://download.docker.com/linux/static/stable/${dockerArch}/docker-${DOCKER_CLI_VERSION}.tgz" -o /tmp/docker.tgz; \
    tar -xzf /tmp/docker.tgz -C /tmp docker/docker; \
    install -m 0755 /tmp/docker/docker /usr/local/bin/docker; \
    mkdir -p /usr/local/lib/docker/cli-plugins; \
    curl -fsSL "https://github.com/docker/compose/releases/download/v${DOCKER_COMPOSE_VERSION}/docker-compose-linux-${composeArch}" \
      -o /usr/local/lib/docker/cli-plugins/docker-compose; \
    chmod 0755 /usr/local/lib/docker/cli-plugins/docker-compose; \
    apt-get purge -y curl; apt-get autoremove -y; \
    rm -rf /tmp/docker.tgz /tmp/docker /var/lib/apt/lists/*; \
    docker --version; docker compose version

COPY deploy/admin/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir --no-compile -r requirements.txt \
    && addgroup --system admin \
    && adduser --system --ingroup admin --home /app --no-create-home admin \
    && mkdir -p /data/releases /data/state \
    && chown -R admin:admin /data

COPY admin/ ./admin/
COPY dashboard/__init__.py ./dashboard/__init__.py
COPY dashboard/static_files.py ./dashboard/static_files.py
# Admin install-context detection reuses the central EMS path resolver, the
# setup config UI reads the central config catalog, and the guided upgrade reuses
# the EMS config upgrade/render helpers; copy just those (not the whole EMS core,
# and never the crypto-backed backup module) so the orchestration image stays
# small. The EMS-format backup runs inside the EMS container via emsctl instead.
COPY ems/__init__.py ./ems/__init__.py
COPY ems/paths.py ./ems/paths.py
COPY ems/config_catalog.py ./ems/config_catalog.py
COPY ems/config.py ./ems/config.py

USER admin

EXPOSE 8090

# Bind inside the container; Compose limits the bridge preview to host loopback.
CMD ["python", "-B", "-m", "admin", "--host", "0.0.0.0", "--port", "8090"]
