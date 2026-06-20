# SPDX-License-Identifier: AGPL-3.0-or-later
FROM python:3.14-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

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
COPY ems-solarflow-api-control.py emsctl.py config.template.json README.md ./
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
