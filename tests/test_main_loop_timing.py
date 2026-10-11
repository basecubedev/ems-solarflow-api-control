# SPDX-License-Identifier: AGPL-3.0-or-later
import ast
import unittest
from pathlib import Path

import pytest

pytestmark = [
    pytest.mark.power_control,
    pytest.mark.e2e,
]


ENTRYPOINT = (
    Path(__file__).resolve().parents[1]
    / "ems-solarflow-api-control.py"
)


def is_call(node, object_name, method_name):
    return (
        isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Attribute)
        and node.value.func.attr == method_name
        and isinstance(node.value.func.value, ast.Name)
        and node.value.func.value.id == object_name
    )


def runs_a_cycle(statement):
    """Whether a loop statement runs the control cycle, guarded or bounded."""

    return any(
        is_call(node, "ems", name)
        for node in ast.walk(statement)
        for name in ("run_once", "run_guarded")
    )


class MainLoopTimingTest(unittest.TestCase):
    def test_live_main_loop_does_not_sleep_after_run_once(self):
        tree = ast.parse(ENTRYPOINT.read_text())
        main_function = next(
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "main"
        )
        live_loop = next(
            node
            for node in ast.walk(main_function)
            if isinstance(node, ast.While) and any(runs_a_cycle(s) for s in node.body)
        )
        cycle_index = next(
            index
            for index, statement in enumerate(live_loop.body)
            if runs_a_cycle(statement)
        )
        sleep_calls_after_run_once = [
            node
            for statement in live_loop.body[cycle_index + 1:]
            for node in ast.walk(statement)
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "sleep"
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "time"
            )
        ]

        self.assertEqual(sleep_calls_after_run_once, [])


if __name__ == "__main__":
    unittest.main()
