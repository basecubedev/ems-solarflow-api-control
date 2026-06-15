# SPDX-License-Identifier: AGPL-3.0-or-later
import json
import shutil
import subprocess
from pathlib import Path

import pytest


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


def test_render_diagnose_report_escapes_dynamic_text():
    malicious = "<script>alert(1)</script>"
    script = f"""
const app = require({json.dumps(str(APP_JS))});
const report = {{
  profile: "install",
  diagnosis: {{
    status: "warning",
    sections: [
      {{ id: "config", title: {json.dumps(malicious)}, status: "warning",
         warnings: [{json.dumps(malicious)}], errors: [] }}
    ],
    root_causes: [
      {{ severity: "error", title: {json.dumps(malicious)},
         message: {json.dumps(malicious)}, suggested_next_check: {json.dumps(malicious)} }}
    ]
  }}
}};
console.log(JSON.stringify({{ html: app.renderDiagnoseReport(report) }}));
"""
    output = run_node(script)
    html = output["html"]
    assert malicious not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    # status + severity pills rendered with reused tone classes
    assert "tone-warn" in html
    assert "tone-blocked" in html


def test_render_diagnose_report_handles_empty_report():
    script = f"""
const app = require({json.dumps(str(APP_JS))});
console.log(JSON.stringify({{
  none: app.renderDiagnoseReport(null),
  empty: app.renderDiagnoseReport({{ profile: "deep", diagnosis: {{ status: "ok", sections: [] }} }})
}}));
"""
    output = run_node(script)
    assert "No diagnosis available." in output["none"]
    assert "No sections reported." in output["empty"]
    assert "tone-send" in output["empty"]  # ok status pill


def test_render_diagnose_report_shows_global_metrics_warnings_and_errors():
    malicious = "<svg onload=alert(1)>"
    script = f"""
const app = require({json.dumps(str(APP_JS))});
const report = {{
  profile: "deep",
  diagnosis: {{
    status: "error",
    metrics: {{
      total_checks: 7,
      {json.dumps(malicious)}: {json.dumps(malicious)}
    }},
    warnings: ["global warning", {json.dumps(malicious)}],
    errors: ["global error", {json.dumps(malicious)}],
    sections: [],
    root_causes: []
  }}
}};
console.log(JSON.stringify({{ html: app.renderDiagnoseReport(report) }}));
"""
    output = run_node(script)
    html = output["html"]
    assert "total checks" in html
    assert "7" in html
    assert "Global warnings" in html
    assert "global warning" in html
    assert "Global errors" in html
    assert "global error" in html
    assert malicious not in html
    assert "&lt;svg onload=alert(1)&gt;" in html
    assert "diagnose-metrics" in html
    assert "diagnose-global-warning" in html
    assert "diagnose-global-error" in html


def test_diagnose_auth_state_messages():
    script = f"""
const app = require({json.dumps(str(APP_JS))});
const out = {{}};
app.state.auth = {{ configured: false, authenticated: false, csrfToken: null }};
out.notConfigured = app.diagnoseAuthState();
app.state.auth = {{ configured: true, authenticated: false, csrfToken: null }};
out.notAuthenticated = app.diagnoseAuthState();
app.state.auth = {{ configured: true, authenticated: true, csrfToken: "t" }};
out.authenticated = app.diagnoseAuthState();
console.log(JSON.stringify(out));
"""
    output = run_node(script)
    assert "Configure a dashboard password" in output["notConfigured"]
    assert "Login required" in output["notAuthenticated"]
    assert output["authenticated"] == ""


def test_set_flow_view_toggles_diagnose_view():
    script = f"""
const app = require({json.dumps(str(APP_JS))});

class FakeElement {{
  constructor(id) {{ this.id = id; this.hidden = false; this.dataset = {{}};
    this.classList = {{ _s: new Set(),
      toggle(c, on) {{ if (on) this._s.add(c); else this._s.delete(c); }},
      add(c) {{ this._s.add(c); }}, remove(c) {{ this._s.delete(c); }},
      contains(c) {{ return this._s.has(c); }} }}; }}
  setAttribute() {{}}
}}

const ids = ["flowSvg","deviceFlowView","controlExplainView","energyStatsView","diagnoseView","diagnoseResults","diagnoseStatus","diagnoseCopy"];
const elements = new Map(ids.map((id) => [id, new FakeElement(id)]));
const wrap = new FakeElement("wrap");

global.window = {{ localStorage: {{ getItem() {{ return null; }}, setItem() {{}} }} }};
global.document = {{
  getElementById(id) {{ return elements.get(id) || null; }},
  querySelector(sel) {{ return sel === ".flow-wrap" ? wrap : null; }},
  querySelectorAll() {{ return []; }},
}};

app.state.auth = {{ configured: true, authenticated: true, csrfToken: "t" }};
app.setFlowView("diagnose", false);
const r1 = {{
  diagnoseHidden: elements.get("diagnoseView").hidden,
  svgHidden: elements.get("flowSvg").hidden,
  energyHidden: elements.get("energyStatsView").hidden,
  wrapHasDiagnose: wrap.classList.contains("view-diagnose"),
}};
app.setFlowView("aggregated", false);
const r2 = {{
  diagnoseHidden: elements.get("diagnoseView").hidden,
  svgHidden: elements.get("flowSvg").hidden,
}};
console.log(JSON.stringify({{ r1, r2 }}));
"""
    output = run_node(script)
    assert output["r1"]["diagnoseHidden"] is False
    assert output["r1"]["svgHidden"] is True
    assert output["r1"]["energyHidden"] is True
    assert output["r1"]["wrapHasDiagnose"] is True
    # switching away hides the diagnose view again and restores aggregated
    assert output["r2"]["diagnoseHidden"] is True
    assert output["r2"]["svgHidden"] is False
