# SPDX-License-Identifier: AGPL-3.0-or-later
FROM python:3.14-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt \
    && addgroup --system ems \
    && adduser --system --ingroup ems --home /app --no-create-home ems

COPY ems/ ./ems/
COPY dashboard/ ./dashboard/
COPY ems-solarflow-api-control.py emsctl.py config.template.json README.md ./
COPY LICENSE THIRD_PARTY_LICENSES.md ./
COPY docs/ ./docs/

RUN mkdir -p /app/config /app/data \
    && chown -R ems:ems /app

USER ems

EXPOSE 8080
VOLUME ["/app/config", "/app/data"]

CMD ["python", "-B", "ems-solarflow-api-control.py", "--config", "/app/config/config.json"]
