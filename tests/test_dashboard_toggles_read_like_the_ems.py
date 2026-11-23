# SPDX-FileCopyrightText: 2026 basecubedev
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The dashboard reads a stored runtime switch with the words the EMS reads it with.

``runtime-state.json`` is edited by hand now and then. The EMS reads a switch
through ``safe_bool``, which knows a fixed set of on and off words; the runtime
editor showed every non-empty string as on, so a stored ``"off"`` showed a device
switched on that the EMS kept off. The editor now keeps its own copy of the words,
and this test is what makes the two lists one.
"""

import ast
import inspect
import json
import re
import textwrap
from pathlib import Path

import pytest

from ems import config as cfg
from ems.runtime_commands import attach_limits
from ems.runtime_state import build_runtime_defaults

pytestmark = [pytest.mark.contract, pytest.mark.config]

RUNTIME_JS = Path(__file__).resolve().parents[1] / "frontend" / "dashboard-modern" / "src" / "lib" / "runtime.js"
WORDS = re.compile(r'export const (ON|OFF)_WORDS = Object\.freeze\(\[([^\]]*)\]\);')


def dashboard_words():
    found = {kind: re.findall(r'"([^"]*)"', body) for kind, body in WORDS.findall(RUNTIME_JS.read_text(encoding="utf-8"))}
    assert set(found) == {"ON", "OFF"}, "runtime.js no longer declares ON_WORDS and OFF_WORDS"
    return found


def safe_bool_words():
    """Every word ``safe_bool`` compares a stored value against."""

    tree = ast.parse(textwrap.dedent(inspect.getsource(cfg.safe_bool)))
    return {
        item.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Compare) and any(isinstance(op, ast.In) for op in node.ops)
        for comparator in node.comparators
        if isinstance(comparator, ast.Tuple)
        for item in comparator.elts
        if isinstance(item, ast.Constant) and isinstance(item.value, str)
    }


def test_the_dashboard_knows_exactly_the_words_safe_bool_knows():
    words = dashboard_words()

    assert set(words["ON"]) | set(words["OFF"]) == safe_bool_words()
    assert [cfg.safe_bool(word, None) for word in words["ON"]] == [True] * len(words["ON"])
    assert [cfg.safe_bool(word, None) for word in words["OFF"]] == [False] * len(words["OFF"])


def test_a_switch_config_decides_reaches_the_editor_as_configured(monkeypatch):
    """For an unreadable stored value the EMS falls back to ``cfg.SYSTEM_ENABLED`` and to
    a device's ``ac_charge_enabled``, the values ``build_runtime_defaults`` seeds from config."""

    from types import SimpleNamespace

    monkeypatch.setattr(cfg, "SYSTEM_ENABLED", False)
    devices = [SimpleNamespace(name="WR1", ac_charge_enabled=False), SimpleNamespace(name="WR2")]

    limits = attach_limits({}, {}, defaults=build_runtime_defaults(devices))["_limits"]

    assert limits["switch_defaults"] == {
        "system": {"enabled": False},
        "devices": {"WR1": {"ac_charge_enabled": False}, "WR2": {"ac_charge_enabled": True}},
    }
    assert attach_limits({}, {})["_limits"]["switch_defaults"] == {"system": {"enabled": True}, "devices": {}}


def test_the_dashboard_hands_the_runtime_defaults_to_the_editor():
    from tests.test_dashboard_server import RuntimeStateStub, direct_handler

    runtime_state = RuntimeStateStub()
    runtime_state.defaults = {"system": {"enabled": False}, "devices": {"WR1": {"ac_charge_enabled": False}}}

    status, _, body = direct_handler("/api/runtime", runtime_state=runtime_state)

    assert status == 200
    assert json.loads(body)["_limits"]["switch_defaults"] == {
        "system": {"enabled": False},
        "devices": {"WR1": {"ac_charge_enabled": False}},
    }
