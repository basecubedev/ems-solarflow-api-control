# SPDX-License-Identifier: AGPL-3.0-or-later
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


def test_render_log_rows_escapes_message_and_sets_level_tone():
    malicious = "<img src=x onerror=alert(1)>"
    script = f"""
const app = require({json.dumps(str(APP_JS))});
const html = app.renderLogRows([
  {{ ts: 1718452800, level: "ERROR", message: {json.dumps(malicious)} }},
  {{ ts: 1718452801, level: "INFO", message: "ok" }},
]);
console.log(JSON.stringify({{ html }}));
"""
    output = run_node(script)
    html = output["html"]
    assert malicious not in html
    assert "&lt;img src=x onerror=alert(1)&gt;" in html
    assert "log-error" in html
    assert "log-info" in html


def test_trim_log_rows_keeps_bounded_window():
    script = f"""
const app = require({json.dumps(str(APP_JS))});
const existing = Array.from({{ length: 990 }}, (_, i) => ({{ seq: i, message: "x" }}));
const incoming = Array.from({{ length: 50 }}, (_, i) => ({{ seq: 1000 + i, message: "y" }}));
const trimmed = app.trimLogRows(existing, incoming, 1000);
console.log(JSON.stringify({{
  length: trimmed.length,
  firstSeq: trimmed[0].seq,
  lastSeq: trimmed[trimmed.length - 1].seq,
}}));
"""
    output = run_node(script)
    assert output["length"] == 1000  # bounded to max
    assert output["lastSeq"] == 1049  # newest kept
    assert output["firstSeq"] == 40  # oldest 40 evicted


def test_logs_auth_state_messages():
    script = f"""
const app = require({json.dumps(str(APP_JS))});
const out = {{}};
app.state.auth = {{ configured: false, authenticated: false, csrfToken: null }};
out.notConfigured = app.logsAuthState();
app.state.auth = {{ configured: true, authenticated: false, csrfToken: null }};
out.notAuthenticated = app.logsAuthState();
app.state.auth = {{ configured: true, authenticated: true, csrfToken: "t" }};
out.authenticated = app.logsAuthState();
console.log(JSON.stringify(out));
"""
    output = run_node(script)
    assert "Configure a dashboard password" in output["notConfigured"]
    assert "Login required" in output["notAuthenticated"]
    assert output["authenticated"] == ""


def test_set_flow_view_toggles_logs_view():
    script = f"""
const app = require({json.dumps(str(APP_JS))});

class FakeElement {{
  constructor(id) {{ this.id = id; this.hidden = false; this.dataset = {{}};
    this._html = ""; this.scrollTop = 0; this.scrollHeight = 0;
    this.classList = {{ _s: new Set(),
      toggle(c, on) {{ if (on) this._s.add(c); else this._s.delete(c); }},
      add(c) {{ this._s.add(c); }}, remove(c) {{ this._s.delete(c); }},
      contains(c) {{ return this._s.has(c); }} }}; }}
  set innerHTML(v) {{ this._html = v; }}
  get innerHTML() {{ return this._html; }}
  setAttribute() {{}}
}}

const ids = ["flowSvg","deviceFlowView","controlExplainView","energyStatsView","diagnoseView","logsView","logsOutput","logsStatus"];
const elements = new Map(ids.map((id) => [id, new FakeElement(id)]));
const wrap = new FakeElement("wrap");

global.window = {{ localStorage: {{ getItem() {{ return null; }}, setItem() {{}} }} }};
global.document = {{
  getElementById(id) {{ return elements.get(id) || null; }},
  querySelector(sel) {{ return sel === ".flow-wrap" ? wrap : null; }},
  querySelectorAll() {{ return []; }},
}};

// Unauthenticated: switching to logs shows the empty state and starts no timer.
app.state.auth = {{ configured: true, authenticated: false, csrfToken: null }};
app.setFlowView("logs", false);
const r1 = {{
  logsHidden: elements.get("logsView").hidden,
  svgHidden: elements.get("flowSvg").hidden,
  wrapHasLogs: wrap.classList.contains("view-logs"),
  output: elements.get("logsOutput").innerHTML,
  timer: app.state.logs.timerId,
}};
app.setFlowView("aggregated", false);
const r2 = {{ logsHidden: elements.get("logsView").hidden }};
console.log(JSON.stringify({{ r1, r2 }}));
"""
    output = run_node(script)
    assert output["r1"]["logsHidden"] is False
    assert output["r1"]["svgHidden"] is True
    assert output["r1"]["wrapHasLogs"] is True
    assert "Login required" in output["r1"]["output"]
    assert output["r1"]["timer"] is None  # no poll loop while unauthenticated
    assert output["r2"]["logsHidden"] is True


def test_set_service_log_level_posts_with_csrf():
    script = f"""
const app = require({json.dumps(str(APP_JS))});
const calls = [];
global.fetch = async (url, opts) => {{ calls.push({{ url, opts }});
  return {{ ok: true, status: 200, json: async () => ({{ service_level: "DEBUG" }}) }}; }};
global.document = {{ getElementById() {{ return null; }} }};

(async () => {{
  // unauthenticated -> no request
  app.state.auth = {{ configured: true, authenticated: false, csrfToken: null }};
  await app.setServiceLogLevel("DEBUG");
  const afterUnauth = calls.length;

  app.state.auth = {{ configured: true, authenticated: true, csrfToken: "tok-9" }};
  await app.setServiceLogLevel("DEBUG");
  const c = calls[calls.length - 1];
  console.log(JSON.stringify({{
    afterUnauth,
    url: c ? c.url : null,
    method: c ? c.opts.method : null,
    csrf: c ? c.opts.headers["X-CSRF-Token"] : null,
    body: c ? JSON.parse(c.opts.body) : null,
    serviceLevel: app.state.logs.serviceLevel,
  }}));
}})();
"""
    output = run_node(script)
    assert output["afterUnauth"] == 0
    assert output["url"] == "/api/logs/level"
    assert output["method"] == "POST"
    assert output["csrf"] == "tok-9"
    assert output["body"] == {"level": "DEBUG"}
    assert output["serviceLevel"] == "DEBUG"
