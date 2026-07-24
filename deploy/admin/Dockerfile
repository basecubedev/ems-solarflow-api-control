# SPDX-License-Identifier: AGPL-3.0-or-later
# EMS SolarFlow Admin device-discovery container. Stdlib http.server,
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
# the EMS config upgrade/render helpers. The Admin backup/restore workflow drives
# the EMS backup core directly against the mounted standard layout (it never
# invents a new archive format), so the EMS backup module and its import chain are
# copied too. Still not the whole EMS core (no controller/target-control/MQTT).
COPY ems/__init__.py ./ems/__init__.py
COPY ems/paths.py ./ems/paths.py
COPY ems/config_catalog.py ./ems/config_catalog.py
COPY ems/config.py ./ems/config.py
COPY ems/backup.py ./ems/backup.py
COPY ems/backup_crypto.py ./ems/backup_crypto.py
COPY ems/influx_setup.py ./ems/influx_setup.py
COPY ems/build_info.py ./ems/build_info.py
COPY ems/diagnostics.py ./ems/diagnostics.py
COPY ems/health.py ./ems/health.py
COPY ems/clients.py ./ems/clients.py
COPY ems/models.py ./ems/models.py
COPY ems/logging_utils.py ./ems/logging_utils.py
# Offline Zendure MQTT setup proposals + config-derived runtime status: Admin
# turns discovery telemetry into preview-only config fragments and builds the
# Maintenance telemetry status view with the EMS code instead of duplicating
# its rules. Only the parsing/status subset is shipped — service/runtime import
# the paho-backed client lazily (factory/start only), so no paho-mqtt or
# network client module is pulled into the Admin image and nothing here can
# open a broker connection.
COPY ems/zendure_mqtt/__init__.py ./ems/zendure_mqtt/__init__.py
COPY ems/zendure_mqtt/topics.py ./ems/zendure_mqtt/topics.py
COPY ems/zendure_mqtt/payloads.py ./ems/zendure_mqtt/payloads.py
COPY ems/zendure_mqtt/snapshot.py ./ems/zendure_mqtt/snapshot.py
COPY ems/zendure_mqtt/write_protocols.py ./ems/zendure_mqtt/write_protocols.py
COPY ems/zendure_mqtt/capability.py ./ems/zendure_mqtt/capability.py
COPY ems/zendure_mqtt/config_mapping.py ./ems/zendure_mqtt/config_mapping.py
COPY ems/zendure_mqtt/config_entries.py ./ems/zendure_mqtt/config_entries.py
# EMS-owned config migration. Admin's migration review/apply and the Guided
# Upgrade preflight orchestrate this module; they never reimplement it.
COPY ems/zendure_mqtt/migration.py ./ems/zendure_mqtt/migration.py
COPY ems/zendure_mqtt/config.py ./ems/zendure_mqtt/config.py
COPY ems/zendure_mqtt/service.py ./ems/zendure_mqtt/service.py
COPY ems/zendure_mqtt/runtime.py ./ems/zendure_mqtt/runtime.py
# Model-aware power-control registry + capability authority. Admin's config
# mapping/validation resolves hardware profiles and write capability from here.
COPY ems/mqtt_control/__init__.py ./ems/mqtt_control/__init__.py
COPY ems/mqtt_control/topic_families.py ./ems/mqtt_control/topic_families.py
COPY ems/mqtt_control/zendure_profiles.py ./ems/mqtt_control/zendure_profiles.py
COPY ems/mqtt_control/power_capability.py ./ems/mqtt_control/power_capability.py
COPY ems/mqtt_credentials.py ./ems/mqtt_credentials.py
COPY dashboard/auth.py ./dashboard/auth.py
# Shared HTTPS helper: the optional Admin HTTPS listener reuses the Dashboard's
# certificate generation and SSLContext code instead of duplicating it.
COPY dashboard/https.py ./dashboard/https.py

# --- Paired system-build identity + embedded Setup resources -------------
# CI passes the SAME values as build args to the Admin AND EMS image builds, so
# the two images form one verifiable system build (matching revision/build_id/
# channel/release_tag). The embedded bundle lets Fresh/Automated Setup and Guided
# Upgrade run with no GitHub access once Admin has aligned to the selected build.
# Direct source builds have a deterministic, internally-consistent fallback.
# The developer launcher replaces these with the repository's real Git identity.
ARG EMS_REVISION=0000000000000000000000000000000000000000
ARG EMS_BUILD_ID=local-0000000
ARG EMS_CHANNEL=development
ARG EMS_RELEASE_TAG=local
ARG EMS_SYSTEM_TAG=local
ARG ADMIN_IMAGE_REPO=ghcr.io/basecubedev/ems-solarflow-admin
ARG EMS_IMAGE_REPO=ghcr.io/basecubedev/ems-solarflow-api-control

# Stage the Setup resources at their bundle paths, then generate the
# hash-verified bundle + descriptors from the same build args as the labels.
COPY config/config.template.json /tmp/release-src/config.template.json
COPY docker-compose.example.yml /tmp/release-src/docker-compose.example.yml
COPY install-docker.sh /tmp/release-src/install-docker.sh
COPY install-docker.ps1 /tmp/release-src/install-docker.ps1
COPY deploy/docker/ /tmp/release-src/deploy/docker/
COPY scripts/generate_release_resources.py ./scripts/generate_release_resources.py
RUN python scripts/generate_release_resources.py \
      --source-root /tmp/release-src \
      --output-dir /app/release-resources \
      --system-tag "${EMS_SYSTEM_TAG}" \
      --channel "${EMS_CHANNEL}" \
      --revision "${EMS_REVISION}" \
      --build-id "${EMS_BUILD_ID}" \
      --release-tag "${EMS_RELEASE_TAG}" \
      --admin-image "${ADMIN_IMAGE_REPO}:${EMS_SYSTEM_TAG}" \
      --ems-image "${EMS_IMAGE_REPO}:${EMS_SYSTEM_TAG}" \
    && rm -rf /tmp/release-src \
    && chown -R admin:admin /app/release-resources

# OCI + EMS build-identity labels; identical stamping is applied to the EMS
# image so a paired system build validates by identity, not tag name.
LABEL org.opencontainers.image.version="${EMS_SYSTEM_TAG}" \
      org.opencontainers.image.revision="${EMS_REVISION}" \
      de.basecubedev.ems.build_id="${EMS_BUILD_ID}" \
      de.basecubedev.ems.channel="${EMS_CHANNEL}" \
      de.basecubedev.ems.release_tag="${EMS_RELEASE_TAG}"

USER admin

# 8090 HTTP always; 8091 optional HTTPS (started only when EMS_ADMIN_HTTPS_ENABLED).
EXPOSE 8090 8091

HEALTHCHECK --interval=10s --timeout=3s --start-period=10s --retries=6 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8090/api/admin/auth/status', timeout=2).read()"

# Bind inside the container; Compose limits the bridge preview to host loopback.
CMD ["python", "-B", "-m", "admin", "--host", "0.0.0.0", "--port", "8090"]
