# SPDX-License-Identifier: AGPL-3.0-or-later
"""The one flow the aggregated diagram could not draw: grid into the fleet.

A charging device reports no output, so before this the diagram showed an idle
inverter next to a grid that was importing hundreds of watts for it. These tests
pin the two rules that make the picture add up.
"""

import json
import shutil
import subprocess
from html.parser import HTMLParser
from pathlib import Path

import pytest

pytestmark = [
    pytest.mark.contract,
]

ROOT = Path(__file__).resolve().parents[1]
APP_JS = ROOT / "dashboard" / "static" / "app.js"
INDEX_HTML = ROOT / "dashboard" / "static" / "index.html"


def run_node(script):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for executable dashboard flow tests")
    result = subprocess.run(
        [node, "-e", script],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


class PipeParser(HTMLParser):
    """Collect the ids and classes of the SVG flow pipes."""

    def __init__(self):
        super().__init__()
        self.pipes = {}

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        classes = attrs.get("class", "").split()
        if tag == "g" and "energy-pipe" in classes and attrs.get("id"):
            self.pipes[attrs["id"]] = classes


def test_the_diagram_has_a_grid_to_inverter_pipe():
    parser = PipeParser()
    parser.feed(INDEX_HTML.read_text(encoding="utf-8"))

    assert "pipeGridInverter" in parser.pipes
    # It reuses the grid colour family rather than introducing a new one.
    assert "grid" in parser.pipes["pipeGridInverter"]
    # The node it feeds gained a state line, like the battery and grid nodes.
    assert 'id="flowInverterState"' in INDEX_HTML.read_text(encoding="utf-8")


def test_the_charge_is_not_drawn_twice_on_the_way_from_the_grid():
    """At night the whole import is the charge, and it has its own pipe now."""

    out = run_node(
        f"""
const app = require({json.dumps(str(APP_JS))});
console.log(JSON.stringify({{
  night: app.gridHomePipeWatts(600, 600),
  import_without_charge: app.gridHomePipeWatts(600, 0),
  export_untouched: app.gridHomePipeWatts(-900, 0),
  export_ignores_charge: app.gridHomePipeWatts(-900, 400),
  charge_exceeds_import: app.gridHomePipeWatts(100, 600),
  no_meter: app.gridHomePipeWatts(0, 0),
}}));
"""
    )

    assert out["night"] == 0
    assert out["import_without_charge"] == 600
    assert out["export_untouched"] == 900
    assert out["export_ignores_charge"] == 900
    # Never negative: the pipe falls idle instead of reversing.
    assert out["charge_exceeds_import"] == 0
    assert out["no_meter"] == 0


def test_the_inverter_keeps_its_output_and_states_the_charge_separately():
    """Both directions run at once in a mixed fleet, so one number cannot
    stand for both. The value stays the output; the charge gets its own line."""

    out = run_node(
        f"""
const app = require({json.dumps(str(APP_JS))});
console.log(JSON.stringify({{
  charging: app.inverterChargeLabel(600),
  idle: app.inverterChargeLabel(0),
  below_threshold: app.inverterChargeLabel(3),
}}));
"""
    )

    assert out["charging"] == "Charging 600 W"
    assert out["idle"] == ""
    assert out["below_threshold"] == ""
