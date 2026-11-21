# SPDX-FileCopyrightText: 2026 basecubedev
# SPDX-License-Identifier: AGPL-3.0-or-later
"""A compose file's former port entries are brought to today's layout, and nothing else.

The installers publish the EMS's port as ``.env`` may move it and InfluxDB's
on this host's loopback only; a compose file written earlier publishes both on
every address. What these tests hold:

- the two entries the installers wrote, quoted or not, become the installers'
  current entries, and the installers write exactly those;
- only in the service the installers named and gave their container name: an
  operator's own InfluxDB, a port an operator chose, and every other line
  stay as they are;
- the services whose entry moved are named; applying it twice changes nothing.
"""

from pathlib import Path

import pytest

from admin.compose_ports import current_publishes

pytestmark = [pytest.mark.unit, pytest.mark.admin, pytest.mark.system_build]

ROOT = Path(__file__).resolve().parents[1]
DASHBOARD = '"${EMS_DASHBOARD_PUBLISH:-8080}:8080"'
INFLUX = '"${EMS_INFLUX_PUBLISH:-127.0.0.1:8086}:8086"'

FORMER = """services:
  ems:
    image: ghcr.io/basecubedev/ems-solarflow-api-control@sha256:abc
    container_name: ems-solarflow-api-control
    ports:
      - "8080:8080"

  influxdb:
    image: influxdb:2.9.1
    container_name: ems-influxdb
    ports:
      - 8086:8086
"""


def test_the_installers_former_entries_take_today_s_layout():
    current, moved = current_publishes(FORMER)
    assert current == FORMER.replace('"8080:8080"', DASHBOARD).replace("8086:8086", INFLUX)
    assert moved == ("ems", "influxdb")
    assert current_publishes(current) == (current, ())


@pytest.mark.parametrize(
    "entry",
    ['"18080:8080"', '"127.0.0.1:8080:8080"', '"8086:8086/tcp"', '"0.0.0.0:8086:8086"', '"8080:8080" # mine'],
)
def test_a_port_an_operator_chose_stays(entry):
    text = f"services:\n  ems:\n    container_name: ems-solarflow-api-control\n    ports:\n      - {entry}\n"
    assert current_publishes(text) == (text, ())


@pytest.mark.parametrize(
    "text",
    [
        '# publishes "8080:8080" on every address\nservices: {}\n',
        'services:\n  influxdb:\n    image: influxdb\n    ports:\n      - "8086:8086"\n',
        'services:\n  grafana-db:\n    container_name: ems-influxdb\n    ports:\n      - "8086:8086"\n',
        'services:\n  ems:\n    container_name: ems-solarflow-api-control\nx-ports:\n  - "8080:8080"\n',
    ],
)
def test_an_entry_outside_the_installers_services_stays(text):
    assert current_publishes(text) == (text, ())


@pytest.mark.parametrize("source", ["install-docker.sh", "install-docker.ps1", "docker-compose.yml"])
def test_the_installers_write_exactly_today_s_entries(source):
    text = (ROOT / source).read_text(encoding="utf-8")
    assert f"      - {DASHBOARD}\n" in text
    assert f"      - {INFLUX}\n" in text
    assert current_publishes(text) == (text, ())
