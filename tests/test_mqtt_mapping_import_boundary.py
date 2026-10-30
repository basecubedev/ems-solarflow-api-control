# SPDX-License-Identifier: AGPL-3.0-or-later
"""The mapping service reads only the rules it shares with the EMS.

It runs in a container of its own beside the EMS and must keep working while the
EMS restarts or updates, so it imports no controller, no client and nothing of
the Admin: only the number rule, the external catalog, the shared file and
logging primitives and the one TLS helper every MQTT client here uses.
"""

import ast
import sys
from pathlib import Path

import pytest

pytestmark = [
    pytest.mark.contract,
    pytest.mark.mqtt,
]

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "mqtt_mapping"
ALLOWED_PROJECT_MODULES = frozenset(
    {
        "ems.config",
        "ems.file_io",
        "ems.logging_utils",
        "ems.zendure_mqtt.external_catalog",
        "ems.zendure_mqtt.payload_values",
    }
)
ALLOWED_THIRD_PARTY = frozenset({"paho"})


def _imports(path):
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            yield node.module


def test_the_package_imports_only_what_it_shares_with_the_ems():
    offending = []
    for path in sorted(PACKAGE.glob("*.py")):
        for module in _imports(path):
            root = module.split(".")[0]
            if root == "mqtt_mapping" or root in sys.stdlib_module_names or root in ALLOWED_THIRD_PARTY:
                continue
            if module not in ALLOWED_PROJECT_MODULES:
                offending.append(f"{path.name}: {module}")

    assert offending == []


def test_the_allowed_modules_exist():
    for module in ALLOWED_PROJECT_MODULES:
        assert (ROOT / (module.replace(".", "/") + ".py")).is_file(), module
