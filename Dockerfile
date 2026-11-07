# SPDX-FileCopyrightText: 2026 basecubedev
# SPDX-License-Identifier: AGPL-3.0-or-later
# Provides the official ``influx`` CLI so bundled InfluxDB backup/restore works
# from inside this container without the Docker CLI or socket.
FROM influxdb:2.9 AS influxdb-cli

FROM python:3.14-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

COPY --from=influxdb-cli /usr/local/bin/influx /usr/local/bin/influx
COPY third_party/influx-cli/ /usr/share/doc/influx-cli/
COPY scripts/binary_notices.py /tmp/binary_notices.py
RUN python3 /tmp/binary_notices.py check --component influx-cli \
      --binary /usr/local/bin/influx --notices /usr/share/doc/influx-cli/THIRD_PARTY_NOTICES \
    && rm /tmp/binary_notices.py

# Mark the image as a container runtime so existing users get persistent
# backups under /app/data/backups after a plain image pull, without editing
# their local compose file. EMS_CONFIG_FILE mirrors the entrypoint default.
ENV EMS_IN_CONTAINER=1
ENV EMS_CONFIG_FILE=/app/config/config.json

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt \
    && addgroup --system ems \
    && adduser --system --ingroup ems --home /app --no-create-home ems

COPY ems/ ./ems/
COPY dashboard/ ./dashboard/
# Runtime dependency of ems/history (Analytics influx sync/status); the rest of
# scripts/ is dev tooling and intentionally not shipped.
COPY scripts/influx_utils.py ./scripts/influx_utils.py
COPY scripts/mqtt_write_latency_probe.py ./scripts/mqtt_write_latency_probe.py
COPY ems-solarflow-api-control.py emsctl.py README.md ./
# Runtime and entrypoint expect the template at /app/config.template.json; the
# canonical source lives in config/ and is copied to that image path here.
COPY config/config.template.json /app/config.template.json
COPY docker-entrypoint.sh ./
COPY LICENSE THIRD_PARTY_LICENSES.md ./
COPY docs/ ./docs/

RUN mkdir -p /app/config /app/data \
    && chmod +x /app/docker-entrypoint.sh \
    && chown -R ems:ems /app

# Runtime-visible build identity. Python cannot reliably read its own image's OCI
# labels, so CI passes the same identity in as build args (see docker-publish.yml)
# and it is exported as env for ems.build_info. Kept last so changing per-build
# metadata never invalidates the dependency layer above. A plain source build
# uses an explicit valid local fallback; CI replaces every value with the real
# repository/workflow identity.
ARG EMS_RELEASE_TAG=local
ARG EMS_GIT_COMMIT=0000000000000000000000000000000000000000
ARG EMS_GIT_COMMIT_SHORT=000000000000
ARG EMS_GIT_DESCRIBE=local
ARG EMS_GIT_BRANCH=local
ARG EMS_GIT_DIRTY=false
ARG EMS_BUILD_ID=local-0000000
ARG EMS_BUILD_SERIAL=0
ARG EMS_CHANNEL=development

ENV EMS_RELEASE_TAG=$EMS_RELEASE_TAG
ENV EMS_GIT_COMMIT=$EMS_GIT_COMMIT
ENV EMS_GIT_COMMIT_SHORT=$EMS_GIT_COMMIT_SHORT
ENV EMS_GIT_DESCRIBE=$EMS_GIT_DESCRIBE
ENV EMS_GIT_BRANCH=$EMS_GIT_BRANCH
ENV EMS_GIT_DIRTY=$EMS_GIT_DIRTY
ENV EMS_BUILD_ID=$EMS_BUILD_ID
ENV EMS_BUILD_SERIAL=$EMS_BUILD_SERIAL
ENV EMS_CHANNEL=$EMS_CHANNEL
RUN python3 -c 'import json, os; json.dump({"version": os.environ["EMS_RELEASE_TAG"], "revision": os.environ["EMS_GIT_COMMIT"], "build_id": os.environ["EMS_BUILD_ID"], "channel": os.environ["EMS_CHANNEL"]}, open("/app/dashboard/static/modern/build.json", "w"))'

# The same OCI identity contract as the paired Admin image. CI and the local
# Docker contract pass one revision/build/channel/release tuple to both images.
LABEL org.opencontainers.image.version=$EMS_RELEASE_TAG \
      org.opencontainers.image.revision=$EMS_GIT_COMMIT \
      de.basecubedev.ems.build_id=$EMS_BUILD_ID \
      de.basecubedev.ems.channel=$EMS_CHANNEL \
      de.basecubedev.ems.release_tag=$EMS_RELEASE_TAG

EXPOSE 8080
VOLUME ["/app/config", "/app/data"]

ENTRYPOINT ["/app/docker-entrypoint.sh"]
CMD ["python", "-B", "ems-solarflow-api-control.py", "--config", "/app/config/config.json"]
