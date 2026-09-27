# SPDX-License-Identifier: AGPL-3.0-or-later
"""Contracts for the energy board's detail switch and channel rows.

The board renders channel labels from the payload's ``channel_meta``; the only
thing the browser owns is the icon and tone per channel. That pairing is walked
here, because two lists that must agree and are never compared are how this
project has shipped six defects of the same shape.
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from ems.energy_channels import ENERGY_CHANNELS

pytestmark = [
    pytest.mark.contract,
]


ROOT = Path(__file__).resolve().parents[1]
APP_JS = ROOT / "dashboard" / "static" / "app.js"
INDEX_HTML = ROOT / "dashboard" / "static" / "index.html"


def run_node(script):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for the executable energy frontend test")
    result = subprocess.run(
        [node, "-e", script],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def render_period(detail, values, meta=None):
    """Render one period card at the given detail level and return its HTML."""

    script = f"""
const app = require({json.dumps(str(APP_JS))});
app.state.energyDetail = {json.dumps(detail)};
const html = app.energyPeriodStage(
  "Today",
  {json.dumps(values)},
  "EUR",
  {{ kind: "today", subtitle: "Current day output", meta: {json.dumps(meta or [])} }},
);
console.log(JSON.stringify({{ html }}));
"""
    return run_node(script)["html"]


def payload(channels=None, coverage=None, ratios=None, **overrides):
    values = {
        "inverter_output_wh": 4000.0,
        "inverter_output_kwh": 4.0,
        "savings_value": 1.4,
        "channels": {
            channel.id: {"wh": 0.0, "kwh": 0.0} for channel in ENERGY_CHANNELS
        },
        "coverage": coverage or {},
        "ratios": ratios or {"self_sufficiency": None, "self_consumption": None},
    }
    if channels:
        for channel_id, kwh in channels.items():
            values["channels"][channel_id] = {"wh": kwh * 1000, "kwh": kwh}
    values.update(overrides)
    return values


def meta_for(*channel_ids, since="2026-09-12", until="2026-09-26"):
    ids = channel_ids or [channel.id for channel in ENERGY_CHANNELS]
    labels = {channel.id: channel.label for channel in ENERGY_CHANNELS}
    return [
        {
            "id": channel_id,
            "label": labels[channel_id],
            "unit": "Wh",
            "since": since,
            "until": until,
        }
        for channel_id in ids
    ]


def test_detail_switch_is_in_the_energy_heading():
    html = INDEX_HTML.read_text(encoding="utf-8")

    assert 'data-energy-detail="basic"' in html
    assert 'data-energy-detail="expert"' in html
    # It reuses the existing segmented-control class instead of inventing one.
    assert 'class="analytics-tabs" role="tablist" aria-label="Energy detail level"' in html
    assert 'id="energyStatsSubtitle"' in html


def test_channel_presentation_covers_every_backend_channel():
    script = f"""
const app = require({json.dumps(str(APP_JS))});
console.log(JSON.stringify({{ ids: Object.keys(app.ENERGY_CHANNEL_PRESENTATION) }}));
"""
    ids = run_node(script)["ids"]

    assert ids == [channel.id for channel in ENERGY_CHANNELS]


def test_basic_card_keeps_the_board_as_it_was():
    html = render_period("basic", payload(), meta_for())

    assert "Energy" in html
    assert "Savings" in html
    for channel in ENERGY_CHANNELS:
        assert channel.label not in html


def test_expert_card_lists_every_channel_from_the_payload():
    html = render_period(
        "expert",
        payload(channels={"grid_import": 2.5, "battery_charge": 1.8}),
        meta_for(),
    )

    for channel in ENERGY_CHANNELS:
        assert channel.label in html
    assert "2.5 kWh" in html
    assert "1.8 kWh" in html


def test_a_period_nobody_measured_says_so_once():
    html = render_period(
        "expert",
        payload(coverage={channel.id: "none" for channel in ENERGY_CHANNELS}),
        meta_for(),
    )

    assert "0.0 kWh" not in html
    assert "not measured" in html
    # Six identical blanks read as a broken card, so the channel rows collapse
    # into the single statement above.
    for channel in ENERGY_CHANNELS:
        assert channel.label not in html


def test_a_partly_measured_period_still_lists_every_channel():
    html = render_period(
        "expert",
        payload(
            channels={"grid_import": 28.9},
            coverage={channel.id: "partial" for channel in ENERGY_CHANNELS},
        ),
        meta_for(),
    )

    for channel in ENERGY_CHANNELS:
        assert channel.label in html
    assert "0.0 kWh \u25e6" in html


def test_a_partly_measured_channel_carries_the_mark():
    html = render_period(
        "expert",
        payload(
            channels={"grid_import": 28.9},
            coverage={"grid_import": "partial"},
        ),
        meta_for(),
    )

    assert "28.9 kWh ◦" in html
    assert "since 2026-09-12" in html


def test_self_sufficiency_is_absent_in_basic_and_explicit_in_expert():
    unknown = payload()
    known = payload(ratios={"self_sufficiency": 0.54, "self_consumption": 0.97})

    assert "Self-Sufficiency" not in render_period("basic", unknown, meta_for())
    assert "Self-Sufficiency" in render_period("expert", unknown, meta_for())
    assert "54%" in render_period("basic", known, meta_for())


def test_peak_output_is_an_expert_row_only():
    values = payload(peak_output_w=742.0)

    assert "Peak Output" not in render_period("basic", values, meta_for())
    assert "742 W" in render_period("expert", values, meta_for())


def test_detail_switch_stores_and_restores_the_choice():
    script = f"""
const app = require({json.dumps(str(APP_JS))});
const stored = {{}};
const buttons = [
  {{ dataset: {{ energyDetail: "basic" }}, classList: {{ toggle() {{}} }}, setAttribute() {{}} }},
  {{ dataset: {{ energyDetail: "expert" }}, classList: {{ toggle() {{}} }}, setAttribute() {{}} }},
];
const subtitle = {{ textContent: "" }};
global.window = {{ localStorage: {{
  getItem: (key) => (key in stored ? stored[key] : null),
  setItem: (key, value) => {{ stored[key] = value; }},
}} }};
global.document = {{
  querySelectorAll: () => buttons,
  getElementById: (id) => (id === "energyStatsSubtitle" ? subtitle : null),
}};

app.setEnergyDetail("expert");
const afterExpert = {{ detail: app.state.energyDetail, stored: stored["dashboard.energyDetail"], subtitle: subtitle.textContent }};
app.setEnergyDetail("nonsense");
const afterNonsense = {{ detail: app.state.energyDetail, subtitle: subtitle.textContent }};
console.log(JSON.stringify({{ afterExpert, afterNonsense }}));
"""
    out = run_node(script)

    assert out["afterExpert"]["detail"] == "expert"
    assert out["afterExpert"]["stored"] == "expert"
    assert "◦" in out["afterExpert"]["subtitle"]
    # An unknown level falls back instead of leaving the board in limbo.
    assert out["afterNonsense"]["detail"] == "basic"
    assert out["afterNonsense"]["subtitle"] == "Based on measured inverter output."
