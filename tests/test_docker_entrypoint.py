# SPDX-License-Identifier: AGPL-3.0-or-later
import os
import stat
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
ENTRYPOINT = ROOT / "docker-entrypoint.sh"
ROOT_PERMISSION_SKIP = pytest.mark.skipif(
    os.geteuid() == 0,
    reason="Permission simulation is not reliable when tests run as root",
)


def run_entrypoint(
    tmp_path,
    config_path,
    template_path,
    command=None,
    data_dir=None,
):
    data_dir = data_dir or tmp_path / "data"
    env = {
        **os.environ,
        "EMS_CONFIG_FILE": str(config_path),
        "EMS_TEMPLATE_FILE": str(template_path),
        "EMS_DATA_DIR": str(data_dir),
        "EMS_SKIP_PRIVILEGE_DROP": "1",
    }
    command = command or ["sh", "-c", 'printf ok > "$EMS_DATA_DIR/probe.txt"']
    return subprocess.run(
        [str(ENTRYPOINT), *command],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def test_docker_entrypoint_creates_config_and_writable_data_dir(tmp_path):
    template_path = tmp_path / "config.template.json"
    config_path = tmp_path / "config" / "config.json"
    template_path.write_text(
        '{"system": {"runtime_state_path": "data/runtime-state.json"}}'
    )

    result = run_entrypoint(tmp_path, config_path, template_path)

    assert result.returncode == 0, result.stderr
    assert config_path.read_text() == template_path.read_text()
    assert (tmp_path / "data" / "probe.txt").read_text() == "ok"
    assert "No config.json found." in result.stderr
    assert "Created /app/config/config.json from config.template.json." in result.stderr
    assert "Please review and edit ./config/config.json" in result.stderr


def test_docker_entrypoint_does_not_overwrite_existing_config(tmp_path):
    template_path = tmp_path / "config.template.json"
    config_path = tmp_path / "config" / "config.json"
    template_path.write_text('{"template": true}')
    config_path.parent.mkdir()
    existing_config = (
        '{\n'
        '  "custom": true,\n'
        '  "system": {\n'
        '    "runtime_state_path": "runtime-state.json"\n'
        '  }\n'
        '}\n'
    )
    config_path.write_text(existing_config)
    config_path.chmod(0o640)
    before_stat = config_path.stat()

    result = run_entrypoint(tmp_path, config_path, template_path)
    after_stat = config_path.stat()

    assert result.returncode == 0, result.stderr
    assert config_path.read_text() == existing_config
    assert stat.S_IMODE(after_stat.st_mode) == stat.S_IMODE(before_stat.st_mode)
    assert (after_stat.st_uid, after_stat.st_gid) == (
        before_stat.st_uid,
        before_stat.st_gid,
    )
    assert "No config.json found." not in result.stderr
    assert "still matches the shipped template" not in result.stderr
    docker_only_flag = "_config" + "_initialized"
    assert docker_only_flag not in config_path.read_text()


def test_docker_entrypoint_does_not_recursively_chown_bind_mounts():
    script = ENTRYPOINT.read_text()

    assert "chown -R" not in script


def test_docker_entrypoint_uses_shared_config_template(tmp_path):
    template_path = ROOT / "config.template.json"
    config_path = tmp_path / "config" / "config.json"

    result = run_entrypoint(tmp_path, config_path, template_path)

    assert result.returncode == 0, result.stderr
    assert config_path.read_bytes() == template_path.read_bytes()
    assert "data/runtime-state.json" in config_path.read_text()


def test_docker_entrypoint_warns_when_config_matches_template(tmp_path):
    template_path = tmp_path / "config.template.json"
    config_path = tmp_path / "config" / "config.json"
    template_path.write_text('{"template": true}')
    config_path.parent.mkdir()
    config_path.write_text(template_path.read_text())

    result = run_entrypoint(tmp_path, config_path, template_path)

    assert result.returncode == 0, result.stderr
    assert "WARNING: config.json still matches the shipped template." in result.stderr
    assert "Startup continues, but device settings may be incomplete." in result.stderr


def test_docker_entrypoint_skips_template_warning_after_edit(tmp_path):
    template_path = tmp_path / "config.template.json"
    config_path = tmp_path / "config" / "config.json"
    template_path.write_text('{"template": true}')
    config_path.parent.mkdir()
    config_path.write_text('{"template": false}')

    result = run_entrypoint(tmp_path, config_path, template_path)

    assert result.returncode == 0, result.stderr
    assert "still matches the shipped template" not in result.stderr


@ROOT_PERMISSION_SKIP
def test_docker_entrypoint_reports_unwritable_config_dir(tmp_path):
    template_path = tmp_path / "config.template.json"
    config_dir = tmp_path / "config"
    config_path = config_dir / "config.json"
    template_path.write_text('{"template": true}')
    config_dir.mkdir()
    config_dir.chmod(0o500)

    try:
        result = run_entrypoint(tmp_path, config_path, template_path)
    finally:
        config_dir.chmod(0o700)

    assert result.returncode != 0
    assert "Unable to create /app/config/config.json." in result.stderr
    assert "mounted ./config directory" in result.stderr


@ROOT_PERMISSION_SKIP
def test_docker_entrypoint_reports_unwritable_data_dir(tmp_path):
    template_path = tmp_path / "config.template.json"
    config_path = tmp_path / "config" / "config.json"
    data_dir = tmp_path / "data"
    template_path.write_text('{"template": true}')
    data_dir.mkdir()
    data_dir.chmod(0o500)

    try:
        result = run_entrypoint(
            tmp_path,
            config_path,
            template_path,
            data_dir=data_dir,
        )
    finally:
        data_dir.chmod(0o700)

    assert result.returncode != 0
    assert "Unable to write to /app/data." in result.stderr
    assert "mounted ./data directory" in result.stderr
