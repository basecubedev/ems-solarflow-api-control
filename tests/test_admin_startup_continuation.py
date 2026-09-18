# SPDX-License-Identifier: AGPL-3.0-or-later
"""The Admin entry point continues a Guided Upgrade that replaced it.

A browser is not part of the durable upgrade contract, and after the Admin
container is swapped it is the only thing that used to continue the operation.
"""

import ast
from pathlib import Path

import pytest

pytestmark = [
    pytest.mark.admin,
    pytest.mark.system_build,
    pytest.mark.workflow,
    pytest.mark.contract,
    pytest.mark.simulation,
]


def _main_source():
    return Path("admin/__main__.py").read_text(encoding="utf-8")


def test_the_entry_point_continues_a_pending_guided_upgrade():
    source = _main_source()
    assert "resume_pending_guided_upgrade" in source
    # It must not block the listeners: a continuation pulls an image.
    tree = ast.parse(source)
    threaded = any(
        isinstance(node, ast.keyword)
        and node.arg == "target"
        and isinstance(node.value, ast.Name)
        and node.value.id == "_continue_pending_upgrade"
        for node in ast.walk(tree)
    )
    assert threaded, "the continuation must run off the serving threads"
