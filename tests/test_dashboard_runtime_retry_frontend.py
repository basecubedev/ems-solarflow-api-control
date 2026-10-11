# SPDX-License-Identifier: AGPL-3.0-or-later
"""A login that cannot load the runtime state says so and tries again instead
of leaving the tiles locked while the header reads "Write mode"."""

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
        pytest.skip("node is required for executable dashboard rendering test")
    result = subprocess.run(
        [node, "-e", script],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


FAKE_DOM = """
const nodes = {
  authButton: { hidden: false, textContent: "" },
  writeModeState: { textContent: "", className: "" },
};
global.document = {
  hidden: false,
  getElementById: (id) => nodes[id] || null,
  querySelectorAll: () => [],
};
"""


def test_a_failed_runtime_load_after_login_is_shown_and_retried():
    script = f"""
const app = require({json.dumps(str(APP_JS))});
{FAKE_DOM}
const timers = [];
global.setTimeout = (fn, ms) => {{ timers.push({{ fn, ms }}); return timers.length; }};
global.clearTimeout = () => {{}};
let reply = {{ ok: false, status: 502, json: async () => {{ throw new Error("not json"); }} }};
global.fetch = async () => reply;

(async () => {{
  app.state.demoMode = false;
  app.state.auth = {{ configured: true, authenticated: true, csrfToken: "t" }};
  app.state.runtime = null;
  await app.loadRuntimeState({{ forceRuntimeEditor: true }});
  const failed = {{
    runtime: app.state.runtime,
    pill: nodes.writeModeState.textContent,
    retries: timers.length,
  }};

  reply = {{ ok: false, status: 503, json: async () => ({{ error: "starting" }}) }};
  await timers.shift().fn();
  const errorBody = {{ runtime: app.state.runtime, retries: timers.length }};

  reply = {{ ok: true, status: 200, json: async () => ({{ system: {{ enabled: true }} }}) }};
  await timers.shift().fn();
  const recovered = {{
    runtime: app.state.runtime,
    pill: nodes.writeModeState.textContent,
    retries: timers.length,
  }};
  console.log(JSON.stringify({{ failed, errorBody, recovered }}));
}})();
"""
    out = run_node(script)

    assert out["failed"]["runtime"] is None
    assert "unavailable" in out["failed"]["pill"]
    assert out["failed"]["retries"] == 1
    assert out["errorBody"] == {"runtime": None, "retries": 1}
    assert out["recovered"]["runtime"] == {"system": {"enabled": True}}
    assert out["recovered"]["pill"] == "Write mode"
    assert out["recovered"]["retries"] == 0


def test_a_failed_runtime_load_while_read_only_is_not_retried_early():
    script = f"""
const app = require({json.dumps(str(APP_JS))});
{FAKE_DOM}
const timers = [];
global.setTimeout = (fn, ms) => {{ timers.push(ms); return timers.length; }};
global.fetch = async () => {{ throw new Error("offline"); }};
(async () => {{
  app.state.demoMode = false;
  app.state.auth = {{ configured: true, authenticated: false, csrfToken: null }};
  await app.loadRuntimeState();
  console.log(JSON.stringify({{ retries: timers.length, pill: nodes.writeModeState.textContent }}));
}})();
"""
    out = run_node(script)

    assert out["retries"] == 0
