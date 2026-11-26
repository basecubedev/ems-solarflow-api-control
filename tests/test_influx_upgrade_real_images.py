# SPDX-FileCopyrightText: 2026 basecubedev
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The Admin's InfluxDB upgrade and its way back, against real images and a real Docker.

The installation is the one the Docker-first installer writes, reduced to the
``influxdb`` service. It starts on the oldest version installations still run
(2.7) and, where ``EMS_INFLUX_UPGRADE_FROM`` names it, on the image the base
branch pinned, the one most installations run when a bump arrives; CI sets it
from the pull request's base, or from the commit a push to main replaced. A
pin that is no upgrade from it, as in a revert of a bump, starts from 2.7
alone. The upgrade goes to the pinned image, so a
Dependabot bump of the pin is proven here before it merges: the converted
data keeps every point, bucket, task and the EMS token. The way back proves
the other half: the upgrade's backup restored on the previous image brings
back exactly what InfluxDB held at the backup, and 2.7 refuses what the new
image converted.

The backup and restore are the official ``influx backup`` and ``influx
restore --full`` the EMS runs (ems/influx_setup.py), issued in the InfluxDB
container because no EMS image is built here; tests/test_docker_first_e2e.py
covers them through the EMS.
"""

import json
import os
import subprocess
import uuid

import pytest

from docker_e2e_utils import docker_compose_ready

from admin.deployment import DockerCli, DockerCompose
from admin.ems_tool import BACKUP_RESTORE_TIMEOUT
from admin.influx_upgrade import ROLLED_BACK, UPGRADED, InfluxUpgradeService
from admin.install_context import detect_install_context
from admin.maintenance import run_maintenance_overview
from ems.influx_image import is_upgrade, pinned_image
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OLDEST = "influxdb:2.7.12"
START_VARIABLE = "EMS_INFLUX_UPGRADE_FROM"


def start_images():
    """Each image an upgrade is proven from: the oldest, and the base branch's pin where the pin upgrades it."""

    images = [OLDEST]
    previous = os.environ.get(START_VARIABLE, "").strip()
    if previous and previous not in images and is_upgrade(previous, pinned_image(ROOT)):
        images.append(previous)
    return images
TOKEN = "e2e-upgrade-token-0123456789abcdef"
POINTS = 5000


pytestmark = [
    pytest.mark.e2e,
    pytest.mark.docker,
    pytest.mark.slow,
    pytest.mark.maintenance,
    pytest.mark.skipif(not hasattr(os, "getuid"), reason="POSIX-only Docker e2e"),
    pytest.mark.skipif(not docker_compose_ready(), reason="Docker is not available"),
]


def _compose(container, start):
    return f"""\
services:
  influxdb:
    image: {start}
    container_name: {container}
    profiles:
      - with-analytics
    env_file:
      - ./config/influxdb.env
    volumes:
      - ./data/influxdb:/var/lib/influxdb2
      - ./backups:/backups
"""


class InfluxBackups:
    """The backup service's part, done with the influx CLI the EMS uses."""

    def __init__(self, root, container):
        self.root = root
        self.container = container
        self.archives = set()
        self.refuse_restore = False

    def _influx(self, script):
        """A backup or a restore, given as long as the Admin gives the EMS one: on a slowed Pi-shaped VM a restore took over five minutes."""

        return subprocess.run(
            ["docker", "exec", self.container, "sh", "-c", script],
            capture_output=True, text=True, timeout=BACKUP_RESTORE_TIMEOUT,
        )

    def create_backup(self, request):
        assert request == {"scope": "influxdb"}
        name = f"ems-influxdb-manual-{uuid.uuid4().hex[:8]}"
        result = self._influx(f'influx backup -t "$INFLUXDB_TOKEN" /backups/{name}')
        if result.returncode != 0:
            return {"ok": False, "message": result.stderr}
        self.archives.add(name)
        return {"ok": True, "archives": [{"name": name, "verified": True}]}

    def has_archive(self, name):
        return name in self.archives

    def check_influx_restore(self, name):
        """The archive's size, read on disk as admin/backup_restore.py checks it: a way back runs while InfluxDB is down."""

        assert name in self.archives
        archive = Path(self.root) / "backups" / name
        assert archive.is_dir(), archive
        return sum(path.stat().st_size for path in archive.rglob("*") if path.is_file())

    def restore_influx_archive(self, name):
        if self.refuse_restore:
            raise RuntimeError("influx restore failed: simulated")
        result = self._influx(f'influx restore --full -t "$INFLUXDB_TOKEN" /backups/{name}')
        assert result.returncode == 0, result.stderr


def _query(container, flux):
    result = subprocess.run(
        ["docker", "exec", container, "sh", "-c",
         'influx query --org ems --raw -t "$INFLUXDB_TOKEN" "$0"', flux],
        capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


def _count(container):
    out = _query(
        container,
        'from(bucket:"ems_raw") |> range(start: 2026-01-01T00:00:00Z) '
        '|> filter(fn:(r)=> r._field=="value") |> count()',
    )
    header = None
    for line in out.splitlines():
        cells = line.strip().split(",")
        if "_value" in cells:
            header = cells
        elif header and len(cells) == len(header) and cells[header.index("_value")].isdigit():
            return int(cells[header.index("_value")])
    return 0


def _api(container, method, path, body=None):
    command = ["curl", "-sf", "-X", method, "-H", f"Authorization: Token {TOKEN}",
               "-H", "Content-Type: application/json", f"http://localhost:8086{path}"]
    if body is not None:
        command += ["--data-binary", json.dumps(body) if not isinstance(body, str) else body]
    result = subprocess.run(["docker", "exec", container, *command], capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, (path, result.stdout, result.stderr)
    return json.loads(result.stdout) if result.stdout.strip() else None


def _write(container, start, points):
    lines = "\n".join(f"ems_device,device=WR1 value={i % 800}i {start + i * 5}000000000" for i in range(points))
    result = subprocess.run(
        ["docker", "exec", "-i", container, "sh", "-c",
         'influx write --org ems --bucket ems_raw -t "$INFLUXDB_TOKEN" --precision ns'],
        input=lines, capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, result.stderr


def _logs(container):
    result = subprocess.run(["docker", "logs", "--tail", "40", container], capture_output=True, text=True, timeout=60)
    return (result.stdout + result.stderr)[-4000:]


def _as_root(root, script):
    subprocess.run(
        ["docker", "run", "--rm", "--user", "0:0", "--entrypoint", "sh",
         "--mount", f"type=bind,src={root},dst=/w", OLDEST, "-c", script],
        capture_output=True, text=True, timeout=120,
    )


def _volumes_of(container):
    listed = subprocess.run(
        ["docker", "inspect", "-f", '{{range .Mounts}}{{if eq .Type "volume"}}{{.Name}} {{end}}{{end}}', container],
        capture_output=True, text=True, timeout=60,
    )
    return set(listed.stdout.split()) if listed.returncode == 0 else set()


class TrackingCompose(DockerCompose):
    """Compose that notes every volume the test's container was given, so teardown removes exactly those."""

    def __init__(self, container, seen):
        super().__init__()
        self.container = container
        self.seen = seen

    def up(self, *args, **kwargs):
        try:
            return super().up(*args, **kwargs)
        finally:
            self.seen.update(_volumes_of(self.container))


@pytest.fixture(params=start_images())
def installation(tmp_path, monkeypatch, request):
    monkeypatch.setenv("COMPOSE_PROJECT_NAME", f"ems-influx-upgrade-e2e-{uuid.uuid4().hex[:10]}")
    root = tmp_path / "install"
    for name in ("config", "data/influxdb", "backups"):
        (root / name).mkdir(parents=True)
    container = f"ems-influxdb-e2e-{uuid.uuid4().hex[:10]}"
    start = request.param
    (root / "docker-compose.yml").write_text(_compose(container, start), encoding="utf-8")
    (root / "config" / "config.json").write_text(
        json.dumps({"influxdb": {"enabled": True, "mode": "bundled", "org": "ems", "bucket_prefix": "ems"}}),
        encoding="utf-8",
    )
    (root / "config" / "influxdb.env").write_text(
        f"INFLUXDB_ORG=ems\nINFLUXDB_TOKEN={TOKEN}\nDOCKER_INFLUXDB_INIT_MODE=setup\n"
        "DOCKER_INFLUXDB_INIT_USERNAME=admin\nDOCKER_INFLUXDB_INIT_PASSWORD=e2e-password-123\n"
        "DOCKER_INFLUXDB_INIT_ORG=ems\nDOCKER_INFLUXDB_INIT_BUCKET=ems_raw\n"
        f"DOCKER_INFLUXDB_INIT_ADMIN_TOKEN={TOKEN}\n",
        encoding="utf-8",
    )
    for image in (start, pinned_image(ROOT)):
        pulled = subprocess.run(["docker", "pull", "-q", image], capture_output=True, text=True, timeout=600)
        assert pulled.returncode == 0, pulled.stderr
    seen = set()
    try:
        yield root, container, TrackingCompose(container, seen), start
    finally:
        subprocess.run(
            ["docker", "compose", "--profile", "with-analytics", "down", "-v", "--remove-orphans"],
            cwd=root, capture_output=True, timeout=120,
        )
        subprocess.run(["docker", "rm", "-f", "-v", container], capture_output=True, timeout=60)
        _as_root(root, "rm -rf /w/data /w/backups")
        if seen:
            subprocess.run(["docker", "volume", "rm", *sorted(seen)], capture_output=True, timeout=60)


def _service(root, container, backups, compose):
    return InfluxUpgradeService(
        record_path=root / "admin" / "influxdb-upgrade.json",
        backups=backups,
        context_provider=lambda: detect_install_context(base_dir=str(root)),
        docker=DockerCli(),
        compose=compose,
        overview_provider=lambda: run_maintenance_overview(base_dir=str(root)),
        target_image=lambda: pinned_image(ROOT),
    )


def _upgraded(installation):
    """An installation on its start image with buckets, a task and points, upgraded to the pin, plus 100 points after."""

    root, container, compose, start = installation
    target = pinned_image(ROOT)
    compose.up(root, profiles=("with-analytics",), services=("influxdb",))
    backups = InfluxBackups(root, container)
    service = _service(root, container, backups, compose)
    assert service._wait_until_answering(container) is None
    org_id = _api(container, "GET", "/api/v2/orgs?org=ems")["orgs"][0]["id"]
    for name, seconds in (("ems_1m", 7776000), ("ems_5m", 31536000)):
        _api(container, "POST", "/api/v2/buckets", {
            "orgID": org_id, "name": name,
            "retentionRules": [{"type": "expire", "everySeconds": seconds, "shardGroupDurationSeconds": 86400}],
        })
    _api(container, "POST", "/api/v2/tasks", {
        "org": "ems",
        "flux": 'option task = {name: "ems-downsample-1m", every: 1m}\n'
                'from(bucket: "ems_raw") |> range(start: -2m) |> aggregateWindow(every: 1m, fn: mean) |> to(bucket: "ems_1m")',
    })
    _write(container, 1767225600, POINTS)
    assert _count(container) == POINTS

    plan = service.plan()
    assert plan["state"] == "available", plan
    upgraded = service.upgrade(target)

    assert upgraded["ok"] is True, (upgraded, _logs(container))
    assert service.store.read()["state"] == UPGRADED
    assert f"image: {target}" in (root / "docker-compose.yml").read_text(encoding="utf-8")
    assert _count(container) == POINTS
    _write(container, 1767225600 + POINTS * 5, 100)
    assert _count(container) == POINTS + 100
    return root, container, service, backups, start


def test_an_upgrade_keeps_every_point_and_the_way_back_restores_the_backup(installation):
    root, container, service, _backups, start = _upgraded(installation)

    rollback = service.plan()["rollback"]
    assert rollback["available"] is True and rollback["to_image"] == start
    back = service.rollback(start)

    assert back["ok"] is True, (back, _logs(container))
    record = service.store.read()
    assert record["state"] == ROLLED_BACK
    assert f"image: {start}" in (root / "docker-compose.yml").read_text(encoding="utf-8")
    assert _count(container) == POINTS
    assert {"ems_1m", "ems_5m", "ems_raw"} <= set(record["buckets"])


@pytest.mark.parametrize("installation", [OLDEST], indirect=True)
def test_the_oldest_image_refuses_what_the_pinned_one_converted(installation):
    """Why the way back restores a backup instead of starting the old image on the converted data."""

    root, container, service, _backups, start = _upgraded(installation)
    assert service.rollback(start)["ok"] is True, _logs(container)
    record = service.store.read()

    aside = root / "data" / record["set_aside"]
    refused = subprocess.run(
        ["docker", "run", "--rm", "--mount", f"type=bind,src={aside},dst=/var/lib/influxdb2", OLDEST, "influxd"],
        capture_output=True, text=True, timeout=120,
    )
    assert refused.returncode != 0
    assert "unknown migration" in refused.stdout + refused.stderr


def test_a_way_back_whose_restore_fails_puts_the_upgraded_influxdb_back(installation):
    root, container, service, backups, start = _upgraded(installation)
    backups.refuse_restore = True

    back = service.rollback(start)

    assert back["ok"] is False and "Roll back can be tried again" in back["message"], (back, _logs(container))
    assert service.store.read()["state"] == UPGRADED
    assert f"image: {pinned_image(ROOT)}" in (root / "docker-compose.yml").read_text(encoding="utf-8")
    assert _count(container) == POINTS + 100
    assert [path.name for path in (root / "data").iterdir() if "failed-way-back" in path.name]
    assert service.plan()["rollback"]["available"] is True
