# SPDX-License-Identifier: AGPL-3.0-or-later
"""The real-Mosquitto release gate fails closed instead of silently skipping.

Locally the broker/protocol tests may skip without Docker. With
``EMS_REQUIRE_REAL_MQTT_TESTS=1`` (set by the release workflow) a missing
Docker CLI, an unreachable daemon, or a broker that cannot start must produce
a non-zero pytest result — a release gate must never go green by skipping.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests.helpers.mosquitto import docker_available

pytestmark = [
    pytest.mark.mqtt,
    pytest.mark.e2e,
    pytest.mark.simulation,
]

ROOT = Path(__file__).resolve().parents[1]
# The documented real-broker release contract. The publish workflow runs exactly
# this set; tests/test_docker_feature_publish_workflow.py pins them together.
GATE_FILES = (
    "tests/test_zendure_mqtt_broker_mosquitto.py",
    "tests/test_mqtt_real_mosquitto.py",
    "tests/test_mqtt_real_mosquitto_acl.py",
    "tests/test_mqtt_real_mosquitto_tls.py",
    "tests/test_mqtt_real_legacy_flow.py",
)
LIFECYCLE_TEST = (
    "tests/test_zendure_mqtt_broker_mosquitto.py"
    "::test_real_mosquitto_publish_to_reply_routing_acknowledges"
)

DAEMON_DOWN_STUB = "#!/bin/sh\nexit 1\n"
BROKER_START_FAILS_STUB = (
    "#!/bin/sh\n"
    'case "$1" in\n'
    "  info|version) exit 0 ;;\n"
    '  *) echo "docker $1 is unavailable" >&2; exit 1 ;;\n'
    "esac\n"
)


def _run_gate(tmp_path, *, docker_stub=None, require=False, files=GATE_FILES):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    if docker_stub is not None:
        stub = bin_dir / "docker"
        stub.write_text(docker_stub, encoding="utf-8")
        stub.chmod(0o755)
    env = {**os.environ, "PATH": str(bin_dir)}
    env.pop("EMS_REQUIRE_REAL_MQTT_TESTS", None)
    if require:
        env["EMS_REQUIRE_REAL_MQTT_TESTS"] = "1"
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q", *files],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
    )


def test_local_docker_absence_skips_the_gate(tmp_path):
    result = _run_gate(tmp_path, docker_stub=None, require=False)
    # Exit code 5 is "no tests ran": both modules skipped at collection.
    assert result.returncode in (0, 5), result.stdout + result.stderr
    assert "skipped" in result.stdout
    assert "passed" not in result.stdout
    assert "failed" not in result.stdout
    assert "error" not in result.stdout.lower()


def test_required_ci_docker_absence_fails_the_gate(tmp_path):
    result = _run_gate(tmp_path, docker_stub=None, require=True)
    assert result.returncode != 0
    output = result.stdout + result.stderr
    assert "EMS_REQUIRE_REAL_MQTT_TESTS" in output
    assert "Docker CLI" in output


@pytest.mark.parametrize("gate_file", GATE_FILES)
def test_every_gate_file_fails_closed_on_its_own(tmp_path, gate_file):
    # Each module must abort by itself. Asserting only on the whole selection
    # would let a module that skips silently hide behind its passing siblings
    # while the publish gate still sees "N passed".
    result = _run_gate(tmp_path, docker_stub=None, require=True, files=(gate_file,))
    assert result.returncode != 0, gate_file
    output = result.stdout + result.stderr
    assert "EMS_REQUIRE_REAL_MQTT_TESTS" in output, gate_file
    assert "skipped" not in output, gate_file


def test_required_ci_docker_daemon_down_fails_the_gate(tmp_path):
    result = _run_gate(tmp_path, docker_stub=DAEMON_DOWN_STUB, require=True)
    assert result.returncode != 0
    output = result.stdout + result.stderr
    assert "EMS_REQUIRE_REAL_MQTT_TESTS" in output
    assert "daemon" in output


def test_required_ci_broker_startup_failure_fails_the_gate(tmp_path):
    result = _run_gate(
        tmp_path,
        docker_stub=BROKER_START_FAILS_STUB,
        require=True,
        files=(LIFECYCLE_TEST,),
    )
    assert result.returncode != 0
    assert "failed to start mosquitto" in result.stdout + result.stderr


@pytest.mark.docker
def test_required_ci_successful_run_executes_a_lifecycle_test():
    if not docker_available():
        pytest.skip("Docker is not available")
    env = {**os.environ, "EMS_REQUIRE_REAL_MQTT_TESTS": "1"}
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", LIFECYCLE_TEST],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "1 passed" in result.stdout
