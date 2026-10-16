# SPDX-License-Identifier: AGPL-3.0-or-later
"""The History chart and the runtime state apply their answers in request order (FR21).

A 30-s refresh, a range or device pick, a save's reload: each starts a request,
and the answers land in whatever order the server sends them. ``loadHistory``
and ``loadRuntimeState`` applied each answer as it arrived, so an older one
landing after a newer one replaced it -- the chart for a range no longer
picked, the runtime values from before a save. Both now keep the rule
``loadAnalytics`` has since review 4: an answer for a selection no longer on
screen, or one older than the data on screen, is dropped, and a failure does
not count as applied data. Every fetch here is settled by hand, in the order
each test names.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = [
    pytest.mark.contract,
]


ROOT = Path(__file__).resolve().parents[1]
APP_JS = ROOT / "dashboard" / "static" / "app.js"


def run_node(script):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for executable dashboard frontend test")
    result = subprocess.run(
        [node, "-e", script],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def _history(points):
    return {"time": [0, 1800], "series": {"pv": [points, points]}, "meta": {}}


def _history_in_turn(events, answers):
    """History requests started and settled by hand.

    ``events`` holds ``["load", range]`` to pick ``range`` and start a
    request (``"refresh"`` starts one for the range on screen), and
    ``["answer", n]``, ``["503", n]`` or ``["fail", n]`` to settle request
    ``n`` with ``answers[n]``. After each settlement the PV value held and
    whether the loading note shows are recorded.
    """

    script = f"""
const app = require({json.dumps(str(APP_JS))});
const nodes = {{ historyLoading: {{ hidden: true }} }};
global.document = {{ hidden: false, getElementById: (id) => nodes[id] || null, querySelectorAll: () => [] }};
const pending = [];
global.fetch = (url) => new Promise((resolve, reject) => pending.push({{ url, resolve, reject }}));
const answers = {json.dumps(answers)};
app.state.history.range = "24h";
(async () => {{
  const loads = [];
  const steps = [];
  for (const [kind, n] of {json.dumps(events)}) {{
    if (kind === "load") {{
      if (n !== "refresh") app.state.history.range = n;
      loads.push(app.loadHistory(n !== "refresh"));
      continue;
    }}
    if (kind === "answer") pending[n].resolve({{ ok: true, json: async () => answers[n] }});
    if (kind === "503") pending[n].resolve({{ ok: false, json: async () => ({{ error: "history_unavailable" }}) }});
    if (kind === "fail") pending[n].reject(new Error("network"));
    await loads[n];
    const data = app.state.history.data;
    steps.push({{
      settled: `${{kind}} ${{n}}`,
      pv: data && data.series ? data.series.pv[0] : null,
      loading: !nodes.historyLoading.hidden,
    }});
  }}
  console.log(JSON.stringify({{ urls: pending.map((request) => request.url), steps }}));
}})();
"""
    return run_node(script)


def test_an_older_history_answer_landing_after_a_newer_one_does_not_replace_it():
    result = _history_in_turn(
        [["load", "refresh"], ["load", "refresh"], ["answer", 1], ["answer", 0]],
        [_history(100), _history(200)],
    )
    assert result["steps"] == [
        {"settled": "answer 1", "pv": 200, "loading": False},
        {"settled": "answer 0", "pv": 200, "loading": False},
    ]


def test_a_history_answer_for_a_range_no_longer_picked_is_not_shown():
    """The 24-h refresh lands while the 7-day pick loads, and after it."""

    first = _history_in_turn(
        [["load", "refresh"], ["load", "7d"], ["answer", 0], ["answer", 1]],
        [_history(100), _history(700)],
    )
    assert "range=24h" in first["urls"][0]
    assert "range=7d" in first["urls"][1]
    assert first["steps"] == [
        {"settled": "answer 0", "pv": None, "loading": True},
        {"settled": "answer 1", "pv": 700, "loading": False},
    ]

    last = _history_in_turn(
        [["load", "refresh"], ["load", "7d"], ["answer", 1], ["answer", 0]],
        [_history(100), _history(700)],
    )
    assert last["steps"][-1] == {"settled": "answer 0", "pv": 700, "loading": False}


def test_an_older_history_failure_landing_after_a_newer_answer_keeps_the_chart():
    for failure in ("503", "fail"):
        result = _history_in_turn(
            [["load", "refresh"], ["load", "refresh"], ["answer", 1], [failure, 0]],
            [_history(100), _history(200)],
        )
        assert result["steps"][-1] == {"settled": f"{failure} 0", "pv": 200, "loading": False}


def test_a_newer_history_failure_does_not_hold_back_an_older_answer():
    result = _history_in_turn(
        [["load", "refresh"], ["load", "refresh"], ["503", 1], ["answer", 0]],
        [_history(100), _history(200)],
    )
    assert result["steps"] == [
        {"settled": "503 1", "pv": None, "loading": False},
        {"settled": "answer 0", "pv": 100, "loading": False},
    ]


def test_a_newer_history_answer_without_a_database_does_not_hold_back_older_data():
    unavailable = {"time": [], "series": {"pv": []}, "meta": {"unavailable": True}}
    result = _history_in_turn(
        [["load", "refresh"], ["load", "refresh"], ["answer", 1], ["answer", 0]],
        [_history(100), unavailable],
    )
    assert result["steps"][-1] == {"settled": "answer 0", "pv": 100, "loading": False}


def _runtime_in_turn(events, answers, *, dirty=False):
    """Runtime-state loads started and settled by hand.

    ``events`` holds ``["refresh"]`` for a plain reload, ``["save"]`` for the
    forced reload after a save or a login, and ``["answer", n]`` or
    ``["fail", n]`` to settle load ``n`` with ``answers[n]``. After each
    settlement the runtime held, whether it reads unavailable, the retries
    scheduled and whether the editor still counts as being edited are
    recorded.
    """

    script = f"""
const app = require({json.dumps(str(APP_JS))});
const nodes = {{ writeModeState: {{ textContent: "", className: "" }} }};
global.document = {{ hidden: false, getElementById: (id) => nodes[id] || null, querySelectorAll: () => [] }};
const timers = [];
global.setTimeout = (fn, ms) => {{ timers.push(ms); return timers.length; }};
global.clearTimeout = () => {{}};
const pending = [];
global.fetch = (url) => new Promise((resolve, reject) => pending.push({{ url, resolve, reject }}));
const answers = {json.dumps(answers)};
app.state.demoMode = false;
app.state.auth = {{ configured: true, authenticated: true, csrfToken: "t" }};
app.state.runtimeEditorDirty = {json.dumps(dirty)};
(async () => {{
  const loads = [];
  const steps = [];
  for (const [kind, n] of {json.dumps(events)}) {{
    if (kind === "refresh") {{ loads.push(app.loadRuntimeState()); continue; }}
    if (kind === "save") {{ loads.push(app.loadRuntimeState({{ forceRuntimeEditor: true }})); continue; }}
    if (kind === "answer") pending[n].resolve({{ ok: true, status: 200, json: async () => answers[n] }});
    if (kind === "fail") pending[n].reject(new Error("network"));
    await loads[n];
    steps.push({{
      settled: `${{kind}} ${{n}}`,
      runtime: app.state.runtime,
      unavailable: app.state.runtimeUnavailable,
      retries: timers.length,
      dirty: app.state.runtimeEditorDirty,
    }});
  }}
  console.log(JSON.stringify(steps));
}})();
"""
    return run_node(script)


BEFORE_SAVE = {"system": {"max_total_power": 600}}
AFTER_SAVE = {"system": {"max_total_power": 800}}


def test_a_refresh_from_before_a_save_landing_after_its_reload_does_not_bring_back_the_old_values():
    steps = _runtime_in_turn(
        [["refresh"], ["save"], ["answer", 1], ["answer", 0]],
        [BEFORE_SAVE, AFTER_SAVE],
    )
    assert steps[0]["runtime"] == AFTER_SAVE
    assert steps[1]["runtime"] == AFTER_SAVE


def test_an_older_runtime_failure_landing_after_a_newer_answer_keeps_the_runtime():
    steps = _runtime_in_turn(
        [["refresh"], ["refresh"], ["answer", 1], ["fail", 0]],
        [BEFORE_SAVE, AFTER_SAVE],
    )
    assert steps[-1] == {
        "settled": "fail 0",
        "runtime": AFTER_SAVE,
        "unavailable": False,
        "retries": 0,
        "dirty": False,
    }


def test_a_newer_runtime_failure_does_not_hold_back_an_older_answer():
    steps = _runtime_in_turn(
        [["refresh"], ["refresh"], ["fail", 1], ["answer", 0]],
        [BEFORE_SAVE, AFTER_SAVE],
    )
    assert steps[0]["runtime"] is None
    assert steps[0]["unavailable"] is True
    assert steps[-1]["runtime"] == BEFORE_SAVE
    assert steps[-1]["unavailable"] is False


def test_a_save_reload_landing_after_a_newer_one_still_resets_the_editor():
    """Its answer is older than the data on screen, which already holds the save."""

    steps = _runtime_in_turn(
        [["save"], ["refresh"], ["answer", 1], ["answer", 0]],
        [AFTER_SAVE, AFTER_SAVE],
        dirty=True,
    )
    assert steps[0]["dirty"] is True
    assert steps[1]["runtime"] == AFTER_SAVE
    assert steps[1]["dirty"] is False
