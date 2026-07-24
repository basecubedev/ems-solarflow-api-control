# SPDX-License-Identifier: AGPL-3.0-or-later
"""The capability modules must import in any order (no import cycle).

The power-write capability authority lives in :mod:`ems.mqtt_control` and must
never reach back into the :mod:`ems.zendure_mqtt` package to obtain the transport
family identifiers it reasons about. Otherwise importing
``ems.mqtt_control.power_capability`` first triggers the package initializer of
``ems.zendure_mqtt`` (which eagerly imports :mod:`ems.zendure_mqtt.capability`),
which imports the still-partially-initialized ``power_capability`` — a circular
import.

These tests each spawn a *fresh interpreter* and import one module first, so they
are independent of pytest collection order (the real-world failure mode).
"""

import subprocess
import sys

import pytest

pytestmark = pytest.mark.simulation


def _import_first(module: str) -> subprocess.CompletedProcess:
    """Import ``module`` as the very first thing in a clean interpreter."""

    return subprocess.run(
        [sys.executable, "-c", f"import {module}"],
        capture_output=True,
        text=True,
    )


def _assert_clean(module: str) -> None:
    result = _import_first(module)
    assert result.returncode == 0, (
        f"importing {module} first failed:\n{result.stderr}"
    )


def test_power_capability_imports_in_fresh_interpreter():
    _assert_clean("ems.mqtt_control.power_capability")


def test_zendure_capability_imports_before_power_capability():
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import ems.zendure_mqtt.capability\n"
            "import ems.mqtt_control.power_capability",
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_power_capability_imports_before_zendure_package():
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import ems.mqtt_control.power_capability\n"
            "import ems.zendure_mqtt",
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_zendure_topics_imports_in_fresh_interpreter():
    _assert_clean("ems.zendure_mqtt.topics")


def test_zendure_profiles_imports_in_fresh_interpreter():
    _assert_clean("ems.mqtt_control.zendure_profiles")


def test_capability_tests_pass_in_isolation():
    """The capability test module must collect+pass when run entirely alone."""

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:cacheprovider",
            "tests/test_zendure_power_capability.py",
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
