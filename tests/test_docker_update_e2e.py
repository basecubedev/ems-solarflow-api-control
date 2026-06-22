# SPDX-License-Identifier: AGPL-3.0-or-later
import json
import os
import shutil
import subprocess
import tarfile
import textwrap
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
PASSWORD = "test-restore-password"


def docker_compose_available():
    if not shutil.which("docker"):
        return False
    if subprocess.run(
        ["docker", "info"],
        text=True,
        capture_output=True,
        check=False,
    ).returncode != 0:
        return False
    return subprocess.run(
        ["docker", "compose", "version"],
        text=True,
        capture_output=True,
        check=False,
    ).returncode == 0


pytestmark = [
    pytest.mark.docker,
    pytest.mark.skipif(
        not docker_compose_available(),
        reason="Docker Compose is not available",
    ),
]


def run(project_dir, *args, input_text=None, env=None):
    return subprocess.run(
        [*args],
        cwd=project_dir,
        text=True,
        input=input_text,
        capture_output=True,
        check=False,
        env={**os.environ, **(env or {})},
    )


def project_name(project_dir):
    raw = project_dir.parent.name.lower()
    safe = "".join(char if char.isalnum() else "-" for char in raw).strip("-")
    return f"ems-rc4-e2e-{safe[:36] or 'test'}"


def compose(project_dir, *args, input_text=None, env=None):
    return run(
        project_dir,
        "docker",
        "compose",
        "-p",
        project_name(project_dir),
        *args,
        input_text=input_text,
        env=env,
    )


def write_old_user_config(path):
    path.write_text(json.dumps({
        "ha": {
            "enabled": True,
            "url": "http://homeassistant.local:8123",
            "token": "user-token-must-survive",
        },
        "system": {
            "enabled": True,
            "dry_run": True,
            "allow_hardware_writes": False,
            "max_total_power": 777,
            "max_device_power": 444,
            "deadband": 12,
            "runtime_state_path": "data/runtime-state.json",
        },
        "dashboard": {
            "enabled": True,
            "host": "0.0.0.0",
            "port": 8080,
            "database_path": "data/ems_dashboard.sqlite",
        },
        "grid_meter": {
            "type": "ha",
        },
        "devices": [
            {
                "name": "WR1",
                "ip": "192.0.2.10",
                "sn": "USER_SN_1",
                "max_power": 444,
            }
        ],
        "custom_user_key": {"preserve": True},
    }))


@pytest.fixture
def docker_install(tmp_path):
    install = tmp_path / "install"
    install.mkdir()
    (install / "config").mkdir()
    (install / "data").mkdir()
    shutil.copy(ROOT / "docker-compose.example.yml", install / "docker-compose.yml")
    (install / "docker-compose.override.yml").write_text(textwrap.dedent(f"""\
        services:
          ems:
            container_name: {project_name(install)}-ems
            build:
              context: {ROOT}
            image: ems-solarflow-api-control:pytest-update-e2e
            command: ["sh", "-c", "sleep 3600"]
        """))
    write_old_user_config(install / "config" / "config.json")
    (install / "data" / "runtime-state.json").write_text(json.dumps({
        "timestamp": "2026-06-22T00:00:00+00:00",
        "controller": {"enabled": False},
        "system": {"enabled": True, "dry_run": True},
        "devices": {},
    }))

    try:
        yield install
    finally:
        compose(install, "down", "-v")


def backup_names(install, suffix):
    return sorted(path.name for path in (install / "data" / "backups").glob(suffix))


def assert_no_root_owned_files(install):
    for base in (install / "config", install / "data"):
        for path in base.rglob("*"):
            stat = path.stat()
            assert stat.st_uid != 0, path
            assert stat.st_gid != 0, path


def test_docker_update_flow_preserves_config_and_creates_backups(docker_install):
    up = compose(docker_install, "up", "-d", "--build")
    assert up.returncode == 0, up.stderr

    ps = compose(docker_install, "ps", "--status", "running", "ems")
    assert ps.returncode == 0, ps.stderr
    assert "ems" in ps.stdout

    backup = compose(
        docker_install,
        "exec",
        "-T",
        "ems",
        "python3",
        "emsctl.py",
        "backup",
        "create",
        "--type",
        "config",
    )
    assert backup.returncode == 0, backup.stderr
    assert backup_names(docker_install, "ems-config-manual-*.tar.gz")

    dry_run = compose(
        docker_install,
        "exec",
        "-T",
        "ems",
        "python3",
        "emsctl.py",
        "config",
        "upgrade",
        "--dry-run",
    )
    assert dry_run.returncode == 0, dry_run.stderr
    assert "Config upgrade plan:" in dry_run.stdout

    before_upgrade_backups = set(backup_names(docker_install, "ems-config-manual-*.tar.gz"))
    apply = compose(
        docker_install,
        "exec",
        "-T",
        "ems",
        "python3",
        "emsctl.py",
        "config",
        "upgrade",
        "--yes",
        "--backup",
    )
    assert apply.returncode == 0, apply.stderr
    after_upgrade_backups = set(backup_names(docker_install, "ems-config-manual-*.tar.gz"))
    assert len(after_upgrade_backups) > len(before_upgrade_backups)

    upgraded = json.loads((docker_install / "config" / "config.json").read_text())
    assert upgraded["system"]["max_total_power"] == 777
    assert upgraded["ha"]["token"] == "user-token-must-survive"
    assert upgraded["devices"][0]["sn"] == "USER_SN_1"
    assert upgraded["custom_user_key"] == {"preserve": True}
    assert "config_upgrade" in upgraded
    assert "influxdb" in upgraded
    assert upgraded["dashboard"]["animation_mode"] == "normal"

    diagnose = compose(
        docker_install,
        "exec",
        "-T",
        "ems",
        "python3",
        "emsctl.py",
        "diagnose",
    )
    assert diagnose.returncode == 0, diagnose.stdout + diagnose.stderr
    assert "Traceback" not in diagnose.stdout + diagnose.stderr

    logs = compose(docker_install, "logs", "ems")
    assert logs.returncode == 0, logs.stderr
    assert "No config.json found." not in logs.stdout + logs.stderr

    assert (docker_install / "config" / "config.json").is_file()
    assert_no_root_owned_files(docker_install)


def test_docker_encrypted_backup_inspect_and_restore_dry_run(docker_install):
    up = compose(docker_install, "up", "-d", "--build")
    assert up.returncode == 0, up.stderr

    create = compose(
        docker_install,
        "exec",
        "-T",
        "-e",
        "EMS_TEST_BACKUP_PASSWORD",
        "ems",
        "sh",
        "-lc",
        'printf "%s\\n%s\\n" "$EMS_TEST_BACKUP_PASSWORD" "$EMS_TEST_BACKUP_PASSWORD" | python3 emsctl.py backup create --type config --password',
        env={"EMS_TEST_BACKUP_PASSWORD": PASSWORD},
    )
    assert create.returncode == 0, create.stderr
    assert PASSWORD not in create.stdout + create.stderr
    archives = list((docker_install / "data" / "backups").glob("*.tar.gz.enc"))
    assert len(archives) == 1
    archive = archives[0]

    with pytest.raises(tarfile.TarError):
        tarfile.open(archive, "r:gz")

    in_container_archive = f"data/backups/{archive.name}"
    inspect_without_password = compose(
        docker_install,
        "exec",
        "-T",
        "ems",
        "python3",
        "emsctl.py",
        "backup",
        "inspect",
        in_container_archive,
        input_text="",
    )
    assert inspect_without_password.returncode == 0, inspect_without_password.stderr
    assert "password required" in inspect_without_password.stdout

    inspect = compose(
        docker_install,
        "exec",
        "-T",
        "-e",
        "EMS_TEST_BACKUP_PASSWORD",
        "ems",
        "sh",
        "-lc",
        f'printf "%s\\n" "$EMS_TEST_BACKUP_PASSWORD" | python3 emsctl.py backup inspect {in_container_archive}',
        env={"EMS_TEST_BACKUP_PASSWORD": PASSWORD},
    )
    assert inspect.returncode == 0, inspect.stderr
    assert "encrypted:  True" in inspect.stdout
    assert PASSWORD not in inspect.stdout + inspect.stderr

    config_path = docker_install / "config" / "config.json"
    config_before = config_path.read_text()
    restore = compose(
        docker_install,
        "exec",
        "-T",
        "-e",
        "EMS_TEST_BACKUP_PASSWORD",
        "ems",
        "sh",
        "-lc",
        f'printf "%s\\n" "$EMS_TEST_BACKUP_PASSWORD" | python3 emsctl.py backup restore {in_container_archive} --dry-run --on-conflict keep --no-rollback',
        env={"EMS_TEST_BACKUP_PASSWORD": PASSWORD},
    )
    assert restore.returncode == 0, restore.stderr
    assert "Dry run: no files were changed" in restore.stdout
    assert config_path.read_text() == config_before
    assert PASSWORD not in restore.stdout + restore.stderr

    changed = json.loads(config_path.read_text())
    changed["custom_user_key"]["restore_probe"] = "changed-after-backup"
    config_path.write_text(json.dumps(changed))
    changed_after_backup = config_path.read_text()
    assert "changed-after-backup" in changed_after_backup

    wrong = compose(
        docker_install,
        "exec",
        "-T",
        "ems",
        "sh",
        "-lc",
        f'printf "%s\\n" "wrong-password" | python3 emsctl.py backup restore {in_container_archive} --on-conflict replace --no-rollback',
    )
    assert wrong.returncode != 0
    assert "incorrect password or corrupted backup" in wrong.stderr
    assert PASSWORD not in wrong.stdout + wrong.stderr
    assert config_path.read_text() == changed_after_backup

    real_restore = compose(
        docker_install,
        "exec",
        "-T",
        "-e",
        "EMS_TEST_BACKUP_PASSWORD",
        "ems",
        "sh",
        "-lc",
        f'printf "%s\\n" "$EMS_TEST_BACKUP_PASSWORD" | python3 emsctl.py backup restore {in_container_archive} --on-conflict replace --no-rollback',
        env={"EMS_TEST_BACKUP_PASSWORD": PASSWORD},
    )
    assert real_restore.returncode == 0, real_restore.stderr
    assert "Restore completed." in real_restore.stdout
    assert PASSWORD not in real_restore.stdout + real_restore.stderr
    assert config_path.read_text() == config_before

    logs = compose(docker_install, "logs", "ems")
    assert logs.returncode == 0, logs.stderr
    assert PASSWORD not in logs.stdout + logs.stderr
