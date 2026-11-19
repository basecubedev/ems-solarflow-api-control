# SPDX-FileCopyrightText: 2026 basecubedev
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Real Docker end-to-end tests for the Docker-first end-user setup.

These run the actual `install-docker.sh` flow against a locally built image and
a real Docker daemon, covering the two documented quickstarts:

  A. EMS only        -> sh install-docker.sh --no-start; docker compose up -d
  B. EMS + Analytics -> sh install-docker.sh --analytics --no-start;
                        docker compose --profile with-analytics up -d

They skip cleanly without Docker and always clean up the test project. A local
override pins the freshly built image and drops published host ports so the
tests do not collide with anything already bound on 8080/8086.
"""
import json
import os
import re
import subprocess
import tarfile
import time
import uuid
from pathlib import Path

import pytest

from docker_e2e_utils import assert_no_root_owned_files, compose_env
from ems.influx_image import pinned_image

ROOT = Path(__file__).resolve().parents[1]
INSTALL_SH = ROOT / "install-docker.sh"
IMAGE_TAG = "ems-solarflow-api-control:first-e2e"

# Backup password is passed via this env var (never argv) so it cannot leak into
# process arguments or test diagnostics.
BACKUP_PASSWORD = "e2e-influx-backup-pw"

# Exports the bundled InfluxDB host/token for the in-container influx CLI. The
# token is read from the mounted secret file into the environment so it never
# appears in argv.
_INFLUX_CLI_ENV = (
    "export INFLUX_HOST=http://influxdb:8086; "
    "export INFLUX_TOKEN=\"$(sed -n 's/^INFLUXDB_TOKEN=//p' config/influxdb.env)\"; "
)


def docker_available():
    from shutil import which

    if not which("docker"):
        return False
    if subprocess.run(["docker", "info"], capture_output=True).returncode != 0:
        return False
    return subprocess.run(["docker", "compose", "version"], capture_output=True).returncode == 0


pytestmark = [
    pytest.mark.setup,
    pytest.mark.e2e,
    pytest.mark.docker,
    pytest.mark.skipif(not hasattr(os, "getuid"), reason="POSIX-only Docker e2e"),
    pytest.mark.skipif(not docker_available(), reason="Docker is not available"),
]


_OVERRIDE = """\
services:
  ems:
    image: {image}
    pull_policy: never
    container_name: ${{COMPOSE_PROJECT_NAME}}-ems
    ports: !override []
  influxdb:
{influx_image_line}    container_name: ${{COMPOSE_PROJECT_NAME}}-influxdb
    ports: !override []
"""


@pytest.fixture(scope="session")
def built_image():
    build = subprocess.run(
        ["docker", "build", "-t", IMAGE_TAG, str(ROOT)],
        capture_output=True,
        text=True,
    )
    if build.returncode != 0:
        pytest.fail("docker build failed:\n" + build.stderr)
    return IMAGE_TAG


_SECRET_KEY = re.compile(r"(?i)(password|token|secret)")
_SENSITIVE_CONFIG_KEYS = {"sn", "ip", "url", "host_url", "token"}


def _secret_shape(value):
    """Describe a secret's shape without revealing it.

    The InfluxDB entrypoint passes these values to ``influx setup`` on a
    command line, where an empty value and one that reads as a flag both shift
    every following argument by one and make the container crash-loop. A dump
    that prints only ``***REDACTED***`` hides exactly that, so record the two
    shapes that break it — never the value.
    """

    if not value:
        return "***EMPTY***"
    flag_like = " flag-like" if value.startswith("-") else ""
    return f"***REDACTED len={len(value)}{flag_like}***"


def _redact_env(text):
    lines = []
    for line in text.splitlines():
        key, separator, value = line.partition("=")
        if separator and _SECRET_KEY.search(key):
            lines.append(f"{key}={_secret_shape(value)}")
        else:
            lines.append(line)
    return "\n".join(lines)


def _redact_config(text):
    try:
        data = json.loads(text)
    except ValueError:
        return "***unparseable config redacted***"

    def walk(obj):
        if isinstance(obj, dict):
            return {
                k: ("***REDACTED***" if _SECRET_KEY.search(k) or k in _SENSITIVE_CONFIG_KEYS else walk(v))
                for k, v in obj.items()
            }
        if isinstance(obj, list):
            return [walk(v) for v in obj]
        return obj

    return json.dumps(walk(data), indent=2)


class Project:
    def __init__(self, path, image, influx_image=None):
        """``influx_image`` False leaves the InfluxDB image to docker-compose.yml."""

        self.path = path
        self.name = "ems-first-e2e-" + uuid.uuid4().hex[:10]
        self.env = compose_env(
            COMPOSE_PROJECT_NAME=self.name,
            PUID=str(os.getuid()),
            PGID=str(os.getgid()),
        )
        if influx_image is False:
            line = ""
        else:
            line = f"    image: {influx_image or pinned_image(ROOT)}\n    pull_policy: missing\n"
        (path / "docker-compose.override.yml").write_text(_OVERRIDE.format(image=image, influx_image_line=line))

    def install(self, *args):
        return subprocess.run(
            ["sh", str(INSTALL_SH), *args],
            cwd=self.path,
            env=self.env,
            capture_output=True,
            text=True,
        )

    def compose(self, *args, profile=False):
        prefix = ["docker", "compose"]
        if profile:
            prefix += ["--profile", "with-analytics"]
        return subprocess.run(
            [*prefix, *args],
            cwd=self.path,
            env=self.env,
            capture_output=True,
            text=True,
        )

    def container_exists(self, suffix):
        return subprocess.run(
            ["docker", "inspect", f"{self.name}-{suffix}"],
            capture_output=True,
        ).returncode == 0

    def down(self):
        self.compose("down", "-v", "--remove-orphans", profile=True)

    def diagnostics(self, failed_command):
        out = [f"FAILED COMMAND: {failed_command}", "", "docker compose ps:"]
        out.append(self.compose("ps", "--all", profile=True).stdout)
        out.append("\ndocker compose logs:")
        out.append(self.compose("logs", "--no-color", profile=True).stdout)
        env_file = self.path / ".env"
        if env_file.exists():
            out.append("\n.env (redacted):\n" + _redact_env(env_file.read_text()))
        influx_env = self.path / "config" / "influxdb.env"
        if influx_env.exists():
            out.append(
                "\nconfig/influxdb.env (redacted):\n"
                + _redact_env(influx_env.read_text())
            )
        config_file = self.path / "config" / "config.json"
        if config_file.exists():
            out.append("\nconfig/config.json (redacted):\n" + _redact_config(config_file.read_text()))
        return "\n".join(out)


def _wait(predicate, timeout=30, interval=1.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


def test_the_failure_dump_shows_secret_shape_but_never_the_secret():
    """Expose the shapes that crash the container, never a value.

    An empty or flag-like init value shifts every following ``influx setup``
    argument by one and leaves the container crash-looping, so a dump that
    prints only ``***REDACTED***`` cannot tell that story. The secrets
    themselves must still never reach a CI log.
    """

    secret = "s3cr3t-value-that-must-not-leak"
    flag_like = "-leading-dash-token"
    rendered = _redact_env(
        "\n".join(
            (
                "# a comment naming TOKEN but carrying no value",
                "DOCKER_INFLUXDB_INIT_ORG=ems",
                f"DOCKER_INFLUXDB_INIT_PASSWORD={secret}",
                "DOCKER_INFLUXDB_INIT_ADMIN_TOKEN=",
                f"INFLUXDB_TOKEN={flag_like}",
            )
        )
    )

    assert secret not in rendered
    assert flag_like not in rendered
    assert "# a comment naming TOKEN but carrying no value" in rendered
    assert "DOCKER_INFLUXDB_INIT_ORG=ems" in rendered
    assert "DOCKER_INFLUXDB_INIT_ADMIN_TOKEN=***EMPTY***" in rendered
    assert (
        f"DOCKER_INFLUXDB_INIT_PASSWORD=***REDACTED len={len(secret)}***" in rendered
    )
    assert (
        f"INFLUXDB_TOKEN=***REDACTED len={len(flag_like)} flag-like***" in rendered
    )


def test_ems_only_quickstart(built_image, tmp_path):
    project = Project(tmp_path, built_image)
    failed = "setup"
    try:
        install = project.install("--no-start")
        assert install.returncode == 0, install.stderr

        assert (project.path / "docker-compose.yml").is_file()
        assert not (project.path / "config" / "influxdb.env").exists()
        assert not (project.path / "data" / "influxdb").exists()
        assert "docker.sock" not in (project.path / "docker-compose.yml").read_text()

        failed = "docker compose up -d"
        up = project.compose("up", "-d")
        assert up.returncode == 0, up.stderr

        config = project.path / "config" / "config.json"
        assert _wait(config.is_file, timeout=30), "container did not seed config.json"

        failed = "docker compose ps"
        ps = project.compose("ps")
        assert f"{project.name}-ems" in ps.stdout
        assert "Up" in ps.stdout

        failed = "docker compose logs ems"
        logs = project.compose("logs", "--no-color", "ems")
        assert "Traceback" not in logs.stdout + logs.stderr

        failed = "config validate (config upgrade --dry-run)"
        validate = project.compose("exec", "-T", "ems", "python3", "emsctl.py", "config", "upgrade", "--dry-run")
        assert validate.returncode == 0, validate.stdout + validate.stderr

        failed = "emsctl status"
        status = project.compose("exec", "-T", "ems", "python3", "emsctl.py", "status")
        assert status.returncode == 0, status.stdout + status.stderr

        # EMS-only must not start or require the bundled InfluxDB.
        assert not project.container_exists("influxdb")
        assert not (project.path / "config" / "influxdb.env").exists()
    except Exception:
        print(project.diagnostics(failed))
        raise
    finally:
        project.down()


def test_analytics_quickstart(built_image, tmp_path):
    project = Project(tmp_path, built_image)
    failed = "setup"
    try:
        install = project.install("--analytics", "--no-start")
        assert install.returncode == 0, install.stdout + install.stderr

        assert (project.path / "config" / "influxdb.env").is_file()
        assert (project.path / "data" / "influxdb").is_dir()
        assert not (project.path / "docker-compose.yml").read_text().count("docker.sock")

        influx = json.loads((project.path / "config" / "config.json").read_text())["influxdb"]
        assert influx["enabled"] is True
        assert influx["mode"] == "bundled"
        assert influx["secret_file"] == "config/influxdb.env"

        env_keys = {
            line.split("=", 1)[0]
            for line in (project.path / "config" / "influxdb.env").read_text().splitlines()
            if "=" in line and not line.lstrip().startswith("#")
        }
        assert {
            "INFLUXDB_ORG",
            "INFLUXDB_TOKEN",
            "DOCKER_INFLUXDB_INIT_MODE",
            "DOCKER_INFLUXDB_INIT_USERNAME",
            "DOCKER_INFLUXDB_INIT_PASSWORD",
            "DOCKER_INFLUXDB_INIT_ORG",
            "DOCKER_INFLUXDB_INIT_BUCKET",
            "DOCKER_INFLUXDB_INIT_ADMIN_TOKEN",
            "DOCKER_INFLUXDB_INIT_CLI_CONFIG_NAME",
        } <= env_keys

        failed = "docker compose --profile with-analytics up -d"
        up = project.compose("up", "-d", profile=True)
        assert up.returncode == 0, up.stderr

        assert project.container_exists("ems")
        assert project.container_exists("influxdb")

        # InfluxDB needs a moment to finish first-run setup; influx sync is the
        # documented readiness/verification command, so poll it.
        failed = "influx sync"
        sync = None

        def synced():
            nonlocal sync
            sync = project.compose("exec", "-T", "ems", "python3", "emsctl.py", "influx", "sync")
            return sync.returncode == 0

        assert _wait(synced, timeout=90, interval=3), (sync.stdout + sync.stderr if sync else "no sync output")

        failed = "influx status"
        status = project.compose("exec", "-T", "ems", "python3", "emsctl.py", "influx", "status")
        assert status.returncode == 0, status.stdout + status.stderr
        assert "healthy: yes" in status.stdout

        org = influx["org"]
        project.env["EMS_TEST_BACKUP_PASSWORD"] = BACKUP_PASSWORD

        # Seed a real data point in a dedicated bucket so the restore check below
        # proves data (not just metadata) survives the round trip.
        failed = "seed influx data point"
        seed = project.compose(
            "exec", "-T", "ems", "sh", "-lc",
            _INFLUX_CLI_ENV
            + f"influx bucket create -n e2e_probe -o {org} && "
            + "influx write -b e2e_probe -o " + org
            + " --precision s 'e2e_probe value=42 1700000000'",
        )
        assert seed.returncode == 0, seed.stdout + seed.stderr

        # Encrypted bundled InfluxDB backup must work from inside the EMS
        # container without the Docker CLI/socket (Docker-first end-user path).
        failed = "backup create --type influxdb --password"
        create = project.compose(
            "exec", "-T", "-e", "EMS_TEST_BACKUP_PASSWORD", "ems", "sh", "-lc",
            'printf "%s\\n%s\\n" "$EMS_TEST_BACKUP_PASSWORD" '
            '"$EMS_TEST_BACKUP_PASSWORD" | '
            "python3 emsctl.py backup create --type influxdb --password",
        )
        assert create.returncode == 0, create.stdout + create.stderr
        assert BACKUP_PASSWORD not in create.stdout + create.stderr

        archives = sorted(
            (project.path / "data" / "backups").glob("ems-influxdb-manual-*.tar.gz.enc")
        )
        assert len(archives) == 1, [p.name for p in archives]
        archive = archives[0]
        in_container = f"data/backups/{archive.name}"

        # Encrypted archive must not be openable as a plain gzip tar.
        with pytest.raises(tarfile.TarError):
            tarfile.open(archive, "r:gz")

        failed = "backup inspect encrypted (no password)"
        inspect_nopw = project.compose(
            "exec", "-T", "ems", "sh", "-lc",
            f"python3 emsctl.py backup inspect {in_container} < /dev/null",
        )
        assert inspect_nopw.returncode == 0, inspect_nopw.stderr
        assert "password required" in inspect_nopw.stdout

        failed = "backup inspect encrypted (with password)"
        inspect_pw = project.compose(
            "exec", "-T", "-e", "EMS_TEST_BACKUP_PASSWORD", "ems", "sh", "-lc",
            'printf "%s\\n" "$EMS_TEST_BACKUP_PASSWORD" | '
            f"python3 emsctl.py backup inspect {in_container}",
        )
        assert inspect_pw.returncode == 0, inspect_pw.stderr
        assert "encrypted:  True" in inspect_pw.stdout
        assert "type:       influxdb" in inspect_pw.stdout
        assert "mode:          bundled" in inspect_pw.stdout
        assert BACKUP_PASSWORD not in inspect_pw.stdout + inspect_pw.stderr

        # Drop the bucket so the restore has something real to bring back.
        failed = "delete seeded bucket"
        drop = project.compose(
            "exec", "-T", "ems", "sh", "-lc",
            _INFLUX_CLI_ENV + f"influx bucket delete -n e2e_probe -o {org}",
        )
        assert drop.returncode == 0, drop.stdout + drop.stderr

        failed = "wrong-password restore must fail cleanly"
        wrong = project.compose(
            "exec", "-T", "ems", "sh", "-lc",
            'printf "%s\\n" "wrong-password" | '
            f"python3 emsctl.py backup restore {in_container} "
            "--on-conflict replace --no-rollback",
        )
        assert wrong.returncode != 0
        assert "Traceback" not in wrong.stdout + wrong.stderr
        assert BACKUP_PASSWORD not in wrong.stdout + wrong.stderr

        failed = "dry-run restore"
        dry = project.compose(
            "exec", "-T", "-e", "EMS_TEST_BACKUP_PASSWORD", "ems", "sh", "-lc",
            'printf "%s\\n" "$EMS_TEST_BACKUP_PASSWORD" | '
            f"python3 emsctl.py backup restore {in_container} "
            "--dry-run --on-conflict replace --no-rollback",
        )
        assert dry.returncode == 0, dry.stdout + dry.stderr
        assert BACKUP_PASSWORD not in dry.stdout + dry.stderr

        failed = "real restore"
        restore = project.compose(
            "exec", "-T", "-e", "EMS_TEST_BACKUP_PASSWORD", "ems", "sh", "-lc",
            'printf "%s\\n" "$EMS_TEST_BACKUP_PASSWORD" | '
            f"python3 emsctl.py backup restore {in_container} "
            "--on-conflict replace --no-rollback",
        )
        assert restore.returncode == 0, restore.stdout + restore.stderr
        assert BACKUP_PASSWORD not in restore.stdout + restore.stderr

        failed = "influx status after restore"

        def healthy_again():
            res = project.compose(
                "exec", "-T", "ems", "python3", "emsctl.py", "influx", "status"
            )
            return res.returncode == 0 and "healthy: yes" in res.stdout

        assert _wait(healthy_again, timeout=60, interval=3)

        failed = "seeded data point survives restore"

        def probe_restored():
            res = project.compose(
                "exec", "-T", "ems", "sh", "-lc",
                _INFLUX_CLI_ENV
                + "influx query -o " + org + " '"
                + 'from(bucket:"e2e_probe") '
                + "|> range(start: 1970-01-01T00:00:00Z) "
                + '|> filter(fn: (r) => r._measurement == "e2e_probe")\'',
            )
            return res.returncode == 0 and "42" in res.stdout

        assert _wait(probe_restored, timeout=30, interval=3)

        failed = "no password in docker logs"
        logs = project.compose("logs", "--no-color", "ems", "influxdb", profile=True)
        assert BACKUP_PASSWORD not in logs.stdout + logs.stderr

        # Every emsctl write above (backup create, restore staging) must land as
        # the host runtime user. The bundled InfluxDB data dir belongs to the
        # separate influxdb container, so it is excluded from the EMS check.
        failed = "no root-owned files in mounts"
        assert_no_root_owned_files(
            project.path / "config",
            project.path / "data",
            exclude=[project.path / "data" / "influxdb"],
        )
    except Exception:
        print(project.diagnostics(failed))
        raise
    finally:
        project.down()


def _influx_points(project, org):
    res = project.compose(
        "exec", "-T", "ems", "sh", "-lc",
        _INFLUX_CLI_ENV + "influx query --raw -o " + org + " '"
        + 'from(bucket:"ems_raw") |> range(start: -1d) '
        + '|> filter(fn: (r) => r._measurement == "e2e_upgrade") |> count()\'',
    )
    assert res.returncode == 0, res.stdout + res.stderr
    header = None
    for line in res.stdout.splitlines():
        cells = line.strip().split(",")
        if "_value" in cells:
            header = cells
        elif header and len(cells) == len(header) and cells[header.index("_value")].isdigit():
            return int(cells[header.index("_value")])
    return 0


def test_the_admin_upgrades_the_bundled_influxdb_through_the_ems_and_back(built_image, tmp_path, monkeypatch):
    """An installation of an older release, upgraded and rolled back by the Admin's own code.

    Everything is the production path but the Admin's container: the EMS
    makes and restores the backups (``emsctl backup`` in a one-off EMS
    container), Docker and Compose run for real, and the installation starts
    on the oldest InfluxDB the installations still run.
    """

    from admin.backup_restore import BackupRestoreService
    from admin.deployment import DockerCli, DockerCompose
    from admin.ems_tool import EmsToolRunner
    from admin.influx_upgrade import ROLLED_BACK, UPGRADED, InfluxUpgradeService
    from admin.install_context import detect_install_context
    from ems.influx_image import replace_image

    oldest = "influxdb:2.7.12"
    target = pinned_image(ROOT)
    project = Project(tmp_path, built_image, influx_image=False)
    seen = set()
    for key in ("COMPOSE_PROJECT_NAME", "PUID", "PGID"):
        monkeypatch.setenv(key, project.env[key])
    failed = "setup"
    try:
        install = project.install("--analytics", "--no-start")
        assert install.returncode == 0, install.stdout + install.stderr
        compose_file = project.path / "docker-compose.yml"
        compose_file.write_text(replace_image(compose_file.read_text(), oldest))

        failed = "docker compose --profile with-analytics up -d"
        up = project.compose("up", "-d", profile=True)
        assert up.returncode == 0, up.stderr
        seen |= _volumes_of(f"{project.name}-influxdb")
        org = json.loads((project.path / "config" / "config.json").read_text())["influxdb"]["org"]

        failed = "influx sync"
        assert _wait(
            lambda: project.compose("exec", "-T", "ems", "python3", "emsctl.py", "influx", "sync").returncode == 0,
            timeout=90, interval=3,
        )
        failed = "seed points"
        seed = project.compose(
            "exec", "-T", "ems", "sh", "-lc",
            _INFLUX_CLI_ENV + "now=$(date +%s); for i in $(seq 1 200); do echo \"e2e_upgrade value=$i $((now - 3600 + i))\"; done "
            "| influx write -b ems_raw -o " + org + " --precision s",
        )
        assert seed.returncode == 0, seed.stdout + seed.stderr
        assert _influx_points(project, org) == 200

        root = str(project.path)
        influx_name = f"{project.name}-influxdb"
        docker = DockerCli()

        def overview():
            row = docker.inspect_container(influx_name)
            return {
                "docker": {"available": True},
                "containers": {"influxdb": {
                    "name": influx_name,
                    "running": bool(row) and row["status"] == "running",
                    "image": row["image"] if row else None,
                }},
            }

        class TrackingCompose(DockerCompose):
            """Compose that notes every volume the InfluxDB container was given."""

            def up(self, *args, **kwargs):
                try:
                    return super().up(*args, **kwargs)
                finally:
                    seen.update(_volumes_of(influx_name))

        context = lambda: detect_install_context(base_dir=root)  # noqa: E731
        service = InfluxUpgradeService(
            record_path=project.path / "admin-data" / "influxdb-upgrade.json",
            backups=BackupRestoreService(context_provider=context, ems_tool=EmsToolRunner()),
            context_provider=context,
            docker=docker,
            compose=TrackingCompose(),
            overview_provider=overview,
            target_image=lambda: target,
        )

        failed = "upgrade"
        assert service.plan()["state"] == "available"
        upgraded = service.upgrade(target)
        assert upgraded["ok"] is True, upgraded
        assert service.store.read()["state"] == UPGRADED
        assert docker.inspect_container(influx_name)["image"] == target
        assert _influx_points(project, org) == 200
        status = project.compose("exec", "-T", "ems", "python3", "emsctl.py", "influx", "status")
        assert status.returncode == 0 and "healthy: yes" in status.stdout, status.stdout + status.stderr

        failed = "way back"
        back = service.rollback(oldest)
        assert back["ok"] is True, back
        assert service.store.read()["state"] == ROLLED_BACK
        assert docker.inspect_container(influx_name)["image"] == oldest
        assert _influx_points(project, org) == 200
    except Exception:
        print(project.diagnostics(failed))
        raise
    finally:
        project.down()
        subprocess.run(
            ["docker", "run", "--rm", "--user", "0:0", "--entrypoint", "sh",
             "--mount", f"type=bind,src={project.path / 'data'},dst=/d", "influxdb:2.7.12",
             "-c", "rm -rf /d/influxdb*"],
            capture_output=True, timeout=120,
        )
        # The way back starts InfluxDB with fresh anonymous volumes, which
        # leaves the old ones unattached; the ones its container had go.
        if seen:
            subprocess.run(["docker", "volume", "rm", *sorted(seen)], capture_output=True, timeout=60)


def _volumes_of(container):
    listed = subprocess.run(
        ["docker", "inspect", "-f", '{{range .Mounts}}{{if eq .Type "volume"}}{{.Name}} {{end}}{{end}}', container],
        capture_output=True, text=True, timeout=60,
    )
    return set(listed.stdout.split()) if listed.returncode == 0 else set()



def test_the_installer_reads_no_host_zone_inside_a_container(tmp_path):
    """Inside the Admin container the "host" zone is the container's own UTC.

    Pinning that into .env is what made a later Admin, which does know the
    zone, unable to fill it in.
    """

    result = subprocess.run(
        [
            "docker", "run", "--rm", "--network", "none",
            "-v", f"{INSTALL_SH}:/install-docker.sh:ro",
            "-w", "/tmp",
            "python:3.14-slim",
            "sh", "/install-docker.sh", "--dry-run",
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "DRY-RUN: write .env (PUID/PGID, TZ=unset)" in result.stdout
    assert "No time zone could be read here" in result.stderr
