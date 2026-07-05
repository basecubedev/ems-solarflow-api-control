# SPDX-License-Identifier: AGPL-3.0-or-later
import os
import stat
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
ENTRYPOINT = ROOT / "docker-entrypoint.sh"
COMPOSE_EXAMPLE = ROOT / "docker-compose.example.yml"
NON_ROOT_VALIDATION_SCRIPT = ROOT / "scripts" / "validate_docker_non_root.sh"
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
    assert "chown -R /app/config" not in script
    assert "chown -R /app/data" not in script


def test_docker_entrypoint_supports_numeric_puid_pgid_selection():
    script = ENTRYPOINT.read_text()

    assert "PUID" in script
    assert "PGID" in script
    assert '[ -n "${PUID:-}" ] || [ -n "${PGID:-}" ]' in script
    assert 'is_positive_id "${PUID:-}"' in script
    assert 'is_positive_id "${PGID:-}"' in script
    assert "--reuid=\"$RUNTIME_UID\"" in script
    assert "--regid=\"$RUNTIME_GID\"" in script
    assert "setpriv" in script


def test_docker_entrypoint_has_no_unconditional_root_fallback():
    script = ENTRYPOINT.read_text()
    final_exec = 'exec "$@"'
    final_exec_index = script.rfind(final_exec)
    root_refusal_index = script.rfind("root_refusal")

    assert final_exec_index != -1
    assert root_refusal_index != -1
    assert root_refusal_index < final_exec_index
    assert "EMS_SKIP_PRIVILEGE_DROP" in script
    assert 'if [ "$(id -u)" = "0" ] && [ "${EMS_SKIP_PRIVILEGE_DROP:-0}" != "1" ]; then\n    root_refusal\nfi' in script


def test_docker_entrypoint_bootstrap_runs_after_privilege_drop_path():
    script = ENTRYPOINT.read_text()

    assert script.index('exec setpriv') < script.index('if [ ! -f "$CONFIG_FILE" ]; then')
    assert 'EMS_PRIVILEGE_DROPPED=1 exec setpriv' in script
    assert 'cp "$TEMPLATE_FILE" "$CONFIG_FILE"' in script


def test_docker_entrypoint_checks_existing_config_writable_by_runtime_user():
    script = ENTRYPOINT.read_text()

    assert 'if [ -f "$CONFIG_FILE" ] && ! test_as_runtime_user test -w "$CONFIG_FILE"; then' in script
    assert 'if [ -f "$CONFIG_FILE" ] && [ ! -w "$CONFIG_FILE" ]; then' in script
    assert "Unable to write to /app/config/config.json." in script


def test_docker_entrypoint_refuses_root_owned_mount_directories():
    script = ENTRYPOINT.read_text()

    assert 'DATA_UID="$(path_uid "$DATA_DIR")"' in script
    assert 'CONFIG_UID="$(path_uid "$CONFIG_DIR")"' in script
    assert 'if [ "$DATA_UID" = "0" ] || [ "$CONFIG_UID" = "0" ]; then\n        root_refusal\n    fi' in script


def test_docker_entrypoint_validates_puid_pgid_before_root_owned_mount_refusal():
    script = ENTRYPOINT.read_text()
    explicit_env_index = script.index('if [ -n "${PUID:-}" ] || [ -n "${PGID:-}" ]; then')
    invalid_index = script.index('invalid_uid_gid', explicit_env_index)
    root_refusal_index = script.index('root_refusal', invalid_index)

    assert invalid_index < root_refusal_index


def test_docker_compose_example_does_not_hard_default_user_ids():
    compose = COMPOSE_EXAMPLE.read_text()

    assert 'PUID: "${PUID:-}"' in compose
    assert 'PGID: "${PGID:-}"' in compose
    assert "${PUID:-1000}" not in compose
    assert "${PGID:-1000}" not in compose


def test_docker_non_root_validation_script_covers_manual_checks():
    script = NON_ROOT_VALIDATION_SCRIPT.read_text()

    assert "/proc/1/status" in script
    assert "test \"$uid\" != \"0\"" in script
    assert "/app/config/non-root-config-write-test" in script
    assert "/app/data/non-root-data-write-test" in script
    assert "chown 0:0 /case/config /case/data" in script
    assert "EMS refuses to start as root." in script


def test_docker_entrypoint_root_escape_hatch_is_explicit_only():
    script = ENTRYPOINT.read_text()

    assert script.count("EMS_SKIP_PRIVILEGE_DROP") == 2
    assert "RUN_AS_USER" in script
    assert "root_refusal" in script


def test_docker_entrypoint_has_clear_root_refusal_message():
    script = ENTRYPOINT.read_text()

    assert "EMS refuses to start as root." in script
    assert "PUID=$(id -u) PGID=$(id -g) docker compose up -d" in script
    assert "mounted /app/data or /app/config directory" in script


def test_docker_entrypoint_uses_shared_config_template(tmp_path):
    template_path = ROOT / "config" / "config.template.json"
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
    assert "Startup continues in safe mode until required placeholders are replaced." in result.stderr
    assert "Hardware writes are disabled while template placeholders remain." in result.stderr


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
