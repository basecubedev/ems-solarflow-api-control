# SPDX-License-Identifier: AGPL-3.0-or-later
# Provides the official ``influx`` CLI so bundled InfluxDB backup/restore works
# from inside this container without the Docker CLI or socket.
FROM influxdb:2.9 AS influxdb-cli

FROM python:3.14-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

COPY --from=influxdb-cli /usr/local/bin/influx /usr/local/bin/influx

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

EXPOSE 8080
VOLUME ["/app/config", "/app/data"]

ENTRYPOINT ["/app/docker-entrypoint.sh"]
CMD ["python", "-B", "ems-solarflow-api-control.py", "--config", "/app/config/config.json"]
