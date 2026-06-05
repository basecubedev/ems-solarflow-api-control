import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
APP_JS = ROOT / "dashboard" / "static" / "app.js"
INDEX_HTML = ROOT / "dashboard" / "static" / "index.html"
STYLES_CSS = ROOT / "dashboard" / "static" / "styles.css"


def run_node(script):
    result = subprocess.run(
        ["node", "-e", script],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_frontend_dynamic_html_escapes_malicious_values():
    malicious = "<img src=x onerror=alert(1)>"
    script = f"""
const app = require({json.dumps(str(APP_JS))});
const malicious = {json.dumps(malicious)};
const output = {{
  deviceValue: app.deviceValue("Name", malicious),
  runtimeDeviceForm: app.runtimeDeviceForm(malicious, {{
    enabled: true,
    max_power: 800,
    offgrid_socket_mode: "off",
    pv_priority_factor: 1.0
  }}, 800)
}};
console.log(JSON.stringify(output));
"""
    output = run_node(script)

    assert malicious not in output["deviceValue"]
    assert "&lt;img src=x onerror=alert(1)&gt;" in output["deviceValue"]
    assert malicious not in output["runtimeDeviceForm"]
    assert "&lt;img src=x onerror=alert(1)&gt;" in output["runtimeDeviceForm"]


def test_frontend_error_feedback_uses_text_content_not_html():
    malicious = "\"'><svg onload=alert(1)>"
    script = f"""
const app = require({json.dumps(str(APP_JS))});
const element = {{}};
global.document = {{
  getElementById(id) {{
    return id === "runtimeWriteFeedback" ? element : null;
  }}
}};
app.setRuntimeFeedback({json.dumps(malicious)}, true);
console.log(JSON.stringify({{
  textContent: element.textContent,
  innerHTML: element.innerHTML || null,
  className: element.className
}}));
"""
    output = run_node(script)

    assert output["textContent"] == malicious
    assert output["innerHTML"] is None
    assert output["className"] == "runtime-feedback error"


def test_login_modal_is_hidden_initially():
    html = INDEX_HTML.read_text()
    css = STYLES_CSS.read_text()
    assert '<div id="loginModal" class="modal-backdrop" hidden>' in html
    assert "[hidden] { display: none !important; }" in css


def test_battery_fill_uses_transform_animation_basis():
    html = INDEX_HTML.read_text()
    css = STYLES_CSS.read_text()

    assert (
        '<rect id="flowBatteryFill" class="battery-fill" x="29" y="32" '
        'width="42" height="13" rx="4"></rect>'
    ) in html
    assert "transition: transform .42s ease, fill .25s ease;" in css
    assert "transform: scaleX(0);" in css
    assert "transform-box: fill-box;" in css
    assert "transform-origin: left center;" in css


def test_dashboard_generated_templates_do_not_use_inline_style_attributes():
    js = APP_JS.read_text()

    assert 'style="' not in js


def test_runtime_control_panel_orders_all_devices_before_winter_and_ha():
    script = f"""
const app = require({json.dumps(str(APP_JS))});
app.state.auth = {{ configured: true, authenticated: true, csrfToken: "token" }};
app.state.runtime = {{
  system: {{ enabled: true, max_total_power: 1200, min_output_limit: 35, loop_interval: 5 }},
  devices: {{
    WR1: {{ enabled: true, max_power: 800, offgrid_socket_mode: "off", pv_priority_factor: 1.0 }},
    WR2: {{ enabled: true, max_power: 800, offgrid_socket_mode: "eco", pv_priority_factor: 1.1 }},
    WR3: {{ enabled: false, max_power: 600, offgrid_socket_mode: "standard", pv_priority_factor: 0.9 }}
  }},
  winter: {{ enabled: false }},
  ha: {{ enabled: true, control_enabled: false }},
  _limits: {{
    system: {{ max_total_power: 1200, min_output_limit: 1200 }},
    devices: {{ WR1: 800, WR2: 800, WR3: 600 }},
    fallback_device_max_power: 800
  }}
}};
const html = app.runtimeControlPanel();
console.log(JSON.stringify({{
  ems: html.indexOf("EMS / System"),
  wr1: html.indexOf(">WR1<"),
  wr2: html.indexOf(">WR2<"),
  wr3: html.indexOf(">WR3<"),
  winter: html.indexOf("Winter Mode"),
  ha: html.indexOf("Home Assistant"),
  wr3Present: html.includes(">WR3<")
}}));
"""
    output = run_node(script)

    assert output["wr3Present"] is True
    assert output["ems"] < output["wr1"] < output["wr2"] < output["wr3"]
    assert output["wr3"] < output["winter"] < output["ha"]


def test_runtime_control_panel_uses_stable_number_limits_after_lowered_values():
    script = f"""
const app = require({json.dumps(str(APP_JS))});
app.state.auth = {{ configured: true, authenticated: true, csrfToken: "token" }};
app.state.runtime = {{
  system: {{ enabled: true, max_total_power: 800, min_output_limit: 35, loop_interval: 5 }},
  devices: {{
    WR1: {{ enabled: true, max_power: 400, offgrid_socket_mode: "off", pv_priority_factor: 0.5 }}
  }},
  winter: {{ enabled: false }},
  ha: {{ enabled: true, control_enabled: false }},
  _limits: {{
    system: {{ max_total_power: 5000, min_output_limit: 5000 }},
    devices: {{ WR1: 800 }},
    fallback_device_max_power: 800
  }}
}};
const html = app.runtimeControlPanel();
console.log(JSON.stringify({{
  maxTotalPowerInput: html.match(/name="max_total_power"[^>]+>/)[0],
  minOutputLimitInput: html.match(/name="min_output_limit"[^>]+>/)[0],
  loopIntervalInput: html.match(/name="loop_interval"[^>]+>/)[0],
  deviceMaxPowerInput: html.match(/name="max_power"[^>]+>/)[0],
  pvPriorityInput: html.match(/name="pv_priority_factor"[^>]+>/)[0]
}}));
"""
    output = run_node(script)

    assert 'value="800"' in output["maxTotalPowerInput"]
    assert 'max="5000"' in output["maxTotalPowerInput"]
    assert 'step="50"' in output["maxTotalPowerInput"]
    assert 'max="800"' not in output["maxTotalPowerInput"]
    assert 'max="5000"' in output["minOutputLimitInput"]
    assert 'step="5"' in output["minOutputLimitInput"]
    assert 'max="3600"' in output["loopIntervalInput"]
    assert 'step="1"' in output["loopIntervalInput"]
    assert 'value="400"' in output["deviceMaxPowerInput"]
    assert 'max="800"' in output["deviceMaxPowerInput"]
    assert 'step="50"' in output["deviceMaxPowerInput"]
    assert 'max="400"' not in output["deviceMaxPowerInput"]
    assert 'max="100"' in output["pvPriorityInput"]
    assert 'step="0.01"' in output["pvPriorityInput"]


def test_runtime_editor_keeps_dirty_input_across_live_refresh():
    script = f"""
const app = require({json.dumps(str(APP_JS))});

const elements = new Map();
let runtimeRenderCount = 0;

class FakeElement {{
  constructor(id = "", className = "") {{
    this.id = id;
    this.className = className;
    this.parent = null;
    this.dataset = {{}};
    this.listeners = {{}};
    this.elements = [];
    this.name = "";
    this.type = "";
    this.value = "";
    this._innerHTML = "";
    this.textContent = "";
  }}
  set innerHTML(value) {{
    this._innerHTML = value;
    if (this.id === "controlExplainView" && value.includes('id="runtimeEditorMount"')) {{
      const runtimeMount = new FakeElement("runtimeEditorMount");
      runtimeMount.parent = this;
      elements.set("runtimeEditorMount", runtimeMount);
      const explainMount = new FakeElement("controlExplainMount");
      explainMount.parent = this;
      elements.set("controlExplainMount", explainMount);
    }}
    if (this.id === "runtimeEditorMount") {{
      runtimeRenderCount += 1;
      ["runtimePanel", "runtimeForm", "maxTotalPowerInput", "runtimeWriteFeedback"].forEach((id) => {{
        const old = elements.get(id);
        if (old) old.parent = null;
      }});
      elements.delete("runtimePanel");
      elements.delete("runtimeForm");
      elements.delete("maxTotalPowerInput");
      elements.delete("runtimeWriteFeedback");
      if (!value.includes("runtime-form")) return;
      const panel = new FakeElement("runtimePanel", "runtime-editor-panel control-stage-row");
      const form = new FakeElement("runtimeForm", "runtime-form control-pipeline-stage");
      const feedback = new FakeElement("runtimeWriteFeedback", "runtime-feedback");
      const input = new FakeElement("maxTotalPowerInput");
      const match = value.match(/name="max_total_power" value="([^"]*)"/);
      panel.parent = this;
      form.parent = panel;
      feedback.parent = panel;
      input.parent = form;
      input.name = "max_total_power";
      input.type = "number";
      input.value = match ? match[1] : "";
      form.dataset.runtimeEndpoint = "/api/runtime/system";
      form.elements = [input];
      elements.set("runtimePanel", panel);
      elements.set("runtimeForm", form);
      elements.set("runtimeWriteFeedback", feedback);
      elements.set("maxTotalPowerInput", input);
    }}
  }}
  get innerHTML() {{ return this._innerHTML; }}
  addEventListener(type, handler) {{ this.listeners[type] = handler; }}
  matches(selector) {{
    return selector === ".runtime-form" && this.className.split(" ").includes("runtime-form");
  }}
  closest(selector) {{
    const selectors = selector.split(",").map((item) => item.trim());
    let current = this;
    while (current) {{
      for (const item of selectors) {{
        if (item.startsWith(".") && current.className.split(" ").includes(item.slice(1))) {{
          return current;
        }}
      }}
      current = current.parent;
    }}
    return null;
  }}
  contains(node) {{
    let current = node;
    while (current) {{
      if (current === this) return true;
      current = current.parent;
    }}
    return false;
  }}
  querySelector(selector) {{
    if (selector === "#runtimeEditorMount") return elements.get("runtimeEditorMount") || null;
    if (selector === "#controlExplainMount") return elements.get("controlExplainMount") || null;
    if (selector === ".runtime-editor-panel") return elements.get("runtimePanel") || null;
    if (selector === ".runtime-form") return elements.get("runtimeForm") || null;
    if (selector === 'input[name="max_total_power"]') return elements.get("maxTotalPowerInput") || null;
    return null;
  }}
}}

const controlExplainView = new FakeElement("controlExplainView");
elements.set("controlExplainView", controlExplainView);
global.document = {{
  activeElement: null,
  getElementById(id) {{ return elements.get(id) || null; }}
}};

const snapshot = {{ control_explain: null }};
app.state.auth = {{ configured: true, authenticated: true, csrfToken: "token" }};
app.state.runtime = {{
  system: {{ enabled: true, max_total_power: 1600, min_output_limit: 35, loop_interval: 5 }},
  devices: {{}},
  winter: {{ enabled: false }},
  ha: {{ enabled: true, control_enabled: false }},
  _limits: {{ system: {{ max_total_power: 5000, min_output_limit: 5000 }}, devices: {{}}, fallback_device_max_power: 800 }}
}};
app.renderControlExplain(snapshot, {{ forceRuntimeEditor: true }});
app.initRuntimeForms();

const firstInput = elements.get("maxTotalPowerInput");
firstInput.value = "160";
global.document.activeElement = firstInput;
controlExplainView.listeners.input({{ target: firstInput }});
const beforeRenderCount = runtimeRenderCount;
const beforeMountHtml = elements.get("runtimeEditorMount").innerHTML;

app.renderControlExplain({{ control_explain: null }});

const afterInput = elements.get("maxTotalPowerInput");
console.log(JSON.stringify({{
  sameInput: afterInput === firstInput,
  value: afterInput.value,
  renderCountUnchanged: runtimeRenderCount === beforeRenderCount,
  runtimeMountHtmlStable: elements.get("runtimeEditorMount").innerHTML === beforeMountHtml,
  editing: app.isRuntimeEditorEditing(),
  explainUpdated: elements.get("controlExplainMount").innerHTML.includes("No control explanation data available yet.")
}}));
"""
    output = run_node(script)

    assert output == {
        "sameInput": True,
        "value": "160",
        "renderCountUnchanged": True,
        "runtimeMountHtmlStable": True,
        "editing": True,
        "explainUpdated": True,
    }


def test_runtime_submit_forces_reload_and_keeps_saved_feedback():
    script = f"""
const app = require({json.dumps(str(APP_JS))});

const elements = new Map();

class FakeElement {{
  constructor(id = "", className = "") {{
    this.id = id;
    this.className = className;
    this.parent = null;
    this.dataset = {{}};
    this.elements = [];
    this.name = "";
    this.type = "";
    this.value = "";
    this._innerHTML = "";
    this.textContent = "";
  }}
  set innerHTML(value) {{
    this._innerHTML = value;
    if (this.id === "controlExplainView" && value.includes('id="runtimeEditorMount"')) {{
      const runtimeMount = new FakeElement("runtimeEditorMount");
      runtimeMount.parent = this;
      elements.set("runtimeEditorMount", runtimeMount);
      const explainMount = new FakeElement("controlExplainMount");
      explainMount.parent = this;
      elements.set("controlExplainMount", explainMount);
    }}
    if (this.id === "runtimeEditorMount") {{
      ["runtimePanel", "runtimeForm", "maxTotalPowerInput", "runtimeWriteFeedback"].forEach((id) => {{
        const old = elements.get(id);
        if (old) old.parent = null;
      }});
      elements.delete("runtimePanel");
      elements.delete("runtimeForm");
      elements.delete("maxTotalPowerInput");
      elements.delete("runtimeWriteFeedback");
      if (!value.includes("runtime-form")) return;
      const panel = new FakeElement("runtimePanel", "runtime-editor-panel control-stage-row");
      const form = new FakeElement("runtimeForm", "runtime-form control-pipeline-stage");
      const feedback = new FakeElement("runtimeWriteFeedback", "runtime-feedback");
      const input = new FakeElement("maxTotalPowerInput");
      const match = value.match(/name="max_total_power" value="([^"]*)"/);
      panel.parent = this;
      form.parent = panel;
      feedback.parent = panel;
      input.parent = form;
      input.name = "max_total_power";
      input.type = "number";
      input.value = match ? match[1] : "";
      form.dataset.runtimeEndpoint = "/api/runtime/system";
      form.elements = [input];
      elements.set("runtimePanel", panel);
      elements.set("runtimeForm", form);
      elements.set("runtimeWriteFeedback", feedback);
      elements.set("maxTotalPowerInput", input);
    }}
  }}
  get innerHTML() {{ return this._innerHTML; }}
  matches(selector) {{
    return selector === ".runtime-form" && this.className.split(" ").includes("runtime-form");
  }}
  closest(selector) {{
    const selectors = selector.split(",").map((item) => item.trim());
    let current = this;
    while (current) {{
      for (const item of selectors) {{
        if (item.startsWith(".") && current.className.split(" ").includes(item.slice(1))) {{
          return current;
        }}
      }}
      current = current.parent;
    }}
    return null;
  }}
  contains(node) {{
    let current = node;
    while (current) {{
      if (current === this) return true;
      current = current.parent;
    }}
    return false;
  }}
  querySelector(selector) {{
    if (selector === "#runtimeEditorMount") return elements.get("runtimeEditorMount") || null;
    if (selector === "#controlExplainMount") return elements.get("controlExplainMount") || null;
    if (selector === ".runtime-editor-panel") return elements.get("runtimePanel") || null;
    return null;
  }}
}}

elements.set("controlExplainView", new FakeElement("controlExplainView"));
global.document = {{
  activeElement: null,
  getElementById(id) {{ return elements.get(id) || null; }}
}};

app.state.auth = {{ configured: true, authenticated: true, csrfToken: "token" }};
app.state.snapshot = {{ control_explain: null }};
app.state.runtime = {{
  system: {{ enabled: true, max_total_power: 1600, min_output_limit: 35, loop_interval: 5 }},
  devices: {{}},
  winter: {{ enabled: false }},
  ha: {{ enabled: true, control_enabled: false }},
  _limits: {{ system: {{ max_total_power: 5000, min_output_limit: 5000 }}, devices: {{}}, fallback_device_max_power: 800 }}
}};
app.renderControlExplain(app.state.snapshot, {{ forceRuntimeEditor: true }});

const form = elements.get("runtimeForm");
const input = elements.get("maxTotalPowerInput");
input.value = "160";
global.document.activeElement = input;
app.state.runtimeEditorDirty = true;

const calls = [];
global.fetch = async (url, options = {{}}) => {{
  calls.push({{ url, method: options.method || "GET", body: options.body || null }});
  if (url === "/api/runtime/system") {{
    return {{ ok: true, status: 200, json: async () => ({{ ok: true }}) }};
  }}
  if (url === "/api/runtime") {{
    return {{
      ok: true,
      status: 200,
      json: async () => ({{
        system: {{ enabled: true, max_total_power: 160, min_output_limit: 35, loop_interval: 5 }},
        devices: {{}},
        winter: {{ enabled: false }},
        ha: {{ enabled: true, control_enabled: false }},
        _limits: {{ system: {{ max_total_power: 5000, min_output_limit: 5000 }}, devices: {{}}, fallback_device_max_power: 800 }}
      }})
    }};
  }}
  throw new Error(`unexpected fetch ${{url}}`);
}};

(async () => {{
  await app.submitRuntimeForm(form);
  console.log(JSON.stringify({{
    calls,
    value: elements.get("maxTotalPowerInput").value,
    dirty: app.state.runtimeEditorDirty,
    editing: app.isRuntimeEditorEditing(),
    feedback: elements.get("runtimeWriteFeedback").textContent,
    feedbackClass: elements.get("runtimeWriteFeedback").className
  }}));
}})().catch((error) => {{
  console.error(error);
  process.exit(1);
}});
"""
    output = run_node(script)

    assert output["calls"] == [
        {
            "url": "/api/runtime/system",
            "method": "PATCH",
            "body": '{"max_total_power":160}',
        },
        {"url": "/api/runtime", "method": "GET", "body": None},
    ]
    assert output["value"] == "160"
    assert output["dirty"] is False
    assert output["editing"] is False
    assert output["feedback"] == "Saved."
    assert output["feedbackClass"] == "runtime-feedback ok"


def test_runtime_editor_force_refresh_replaces_write_controls_after_logout():
    script = f"""
const app = require({json.dumps(str(APP_JS))});

const elements = new Map();
class FakeElement {{
  constructor(id = "") {{
    this.id = id;
    this.parent = null;
    this._innerHTML = "";
  }}
  set innerHTML(value) {{
    this._innerHTML = value;
    if (this.id === "controlExplainView" && value.includes('id="runtimeEditorMount"')) {{
      const runtimeMount = new FakeElement("runtimeEditorMount");
      runtimeMount.parent = this;
      elements.set("runtimeEditorMount", runtimeMount);
      const explainMount = new FakeElement("controlExplainMount");
      explainMount.parent = this;
      elements.set("controlExplainMount", explainMount);
    }}
  }}
  get innerHTML() {{ return this._innerHTML; }}
  querySelector(selector) {{
    if (selector === "#runtimeEditorMount") return elements.get("runtimeEditorMount") || null;
    if (selector === "#controlExplainMount") return elements.get("controlExplainMount") || null;
    return null;
  }}
  contains(node) {{
    let current = node;
    while (current) {{
      if (current === this) return true;
      current = current.parent;
    }}
    return false;
  }}
}}
elements.set("controlExplainView", new FakeElement("controlExplainView"));
global.document = {{
  activeElement: {{ closest: () => ({{}}) }},
  getElementById(id) {{ return elements.get(id) || null; }}
}};

app.state.auth = {{ configured: true, authenticated: true, csrfToken: "token" }};
app.state.runtime = {{
  system: {{ enabled: true, max_total_power: 1600, min_output_limit: 35, loop_interval: 5 }},
  devices: {{}},
  winter: {{ enabled: false }},
  ha: {{ enabled: true, control_enabled: false }},
  _limits: {{ system: {{ max_total_power: 5000, min_output_limit: 5000 }}, devices: {{}}, fallback_device_max_power: 800 }}
}};
app.renderControlExplain({{ control_explain: null }}, {{ forceRuntimeEditor: true }});
const authenticatedHtml = elements.get("runtimeEditorMount").innerHTML;

app.state.auth = {{ configured: true, authenticated: false, csrfToken: null }};
app.state.runtimeEditorDirty = true;
app.renderControlExplain({{ control_explain: null }}, {{ forceRuntimeEditor: true }});
const loggedOutHtml = elements.get("runtimeEditorMount").innerHTML;

console.log(JSON.stringify({{
  hadForm: authenticatedHtml.includes("runtime-form"),
  loginRequired: loggedOutHtml.includes("Login required"),
  hasFormAfterLogout: loggedOutHtml.includes("runtime-form")
}}));
"""
    output = run_node(script)

    assert output == {
        "hadForm": True,
        "loginRequired": True,
        "hasFormAfterLogout": False,
    }


def test_battery_fill_helper_clamps_and_sets_transform_scale():
    script = f"""
const app = require({json.dumps(str(APP_JS))});
const classes = new Set(["battery-fill"]);
const element = {{
  attributes: {{}},
  style: {{}},
  classList: {{
    toggle(name, enabled) {{
      if (enabled) classes.add(name);
      else classes.delete(name);
    }}
  }},
  setAttribute(name, value) {{
    this.attributes[name] = value;
  }},
  getAttribute(name) {{
    return this.attributes[name];
  }}
}};
global.document = {{
  getElementById(id) {{
    return id === "battery" ? element : null;
  }}
}};

app.setBatteryFill("battery", 125);
const high = {{
  width: element.attributes.width,
  transform: element.style.transform,
  low: classes.has("low"),
  full: classes.has("full")
}};

app.setBatteryFill("battery", -5);
const low = {{
  width: element.attributes.width,
  transform: element.style.transform,
  low: classes.has("low"),
  full: classes.has("full")
}};

console.log(JSON.stringify({{ high, low }}));
"""
    output = run_node(script)

    assert output["high"] == {
        "width": "42",
        "transform": "scaleX(1)",
        "low": False,
        "full": True,
    }
    assert output["low"] == {
        "width": "42",
        "transform": "scaleX(0)",
        "low": True,
        "full": False,
    }


def test_battery_fill_helper_animates_only_real_soc_changes():
    script = f"""
const app = require({json.dumps(str(APP_JS))});
const callbacks = [];
const classes = new Set(["battery-fill"]);
const element = {{
  attributes: {{}},
  style: {{}},
  classList: {{
    toggle(name, enabled) {{
      if (enabled) classes.add(name);
      else classes.delete(name);
    }}
  }},
  setAttribute(name, value) {{
    this.attributes[name] = value;
  }},
  getAttribute(name) {{
    return this.attributes[name];
  }}
}};
global.window = {{
  requestAnimationFrame(callback) {{
    callbacks.push(callback);
  }}
}};
global.document = {{
  getElementById(id) {{
    return id === "battery" ? element : null;
  }}
}};

app.setBatteryFill("battery", 60);
const firstRender = element.style.transform || null;
while (callbacks.length) callbacks.shift()();

app.setBatteryFill("battery", 60);
const unchanged = element.style.transform || null;
while (callbacks.length) callbacks.shift()();

app.setBatteryFill("battery", 80);
const beforeChangeFrames = element.style.transform || null;
callbacks.shift()();
const afterFirstChangeFrame = element.style.transform || null;
callbacks.shift()();
const afterSecondChangeFrame = element.style.transform || null;

console.log(JSON.stringify({{
  firstRender,
  unchanged,
  beforeChangeFrames,
  afterFirstChangeFrame,
  afterSecondChangeFrame,
  target: element.attributes["data-soc-target"]
}}));
"""
    output = run_node(script)

    assert output["firstRender"] == "scaleX(0.6)"
    assert output["unchanged"] == "scaleX(0.6)"
    assert output["beforeChangeFrames"] == "scaleX(0.6)"
    assert output["afterFirstChangeFrame"] == "scaleX(0.6)"
    assert output["afterSecondChangeFrame"] == "scaleX(0.8)"
    assert output["target"] == "80"


def test_device_cards_render_soc_fill_start_and_target_values():
    script = f"""
const app = require({json.dumps(str(APP_JS))});
const cards = [];
const grid = {{
  innerHTML: "",
  querySelectorAll() {{
    return [];
  }},
  appendChild(card) {{
    cards.push(card);
  }}
}};
global.document = {{
  getElementById(id) {{
    return id === "deviceGrid" ? grid : null;
  }},
  createElement() {{
    return {{}};
  }}
}};

app.renderDevices({{
  "WR<&1": {{
    online: true,
    soc: 72,
    battery_power_w: 100,
    pv_input_w: 500,
    output_w: 300,
    target_w: 300,
    output_limit_w: 700,
    mode: "solar"
  }}
}});
const firstHtml = cards[0].innerHTML;

app.renderDevices({{
  "WR<&1": {{
    online: true,
    soc: 72,
    battery_power_w: 100,
    pv_input_w: 500,
    output_w: 300,
    target_w: 300,
    output_limit_w: 700,
    mode: "solar"
  }}
}});
const unchangedFirstHtml = cards[1].innerHTML;

app.renderDevices({{
  "WR<&1": {{
    online: true,
    soc: 75,
    battery_power_w: 100,
    pv_input_w: 500,
    output_w: 300,
    target_w: 300,
    output_limit_w: 700,
    mode: "solar"
  }}
}});
const changedHtml = cards[2].innerHTML;

app.renderDevices({{
  "WR<&1": {{
    online: true,
    soc: 75,
    battery_power_w: 100,
    pv_input_w: 500,
    output_w: 300,
    target_w: 300,
    output_limit_w: 700,
    mode: "solar"
  }}
}});
const unchangedAfterChangeHtml = cards[3].innerHTML;

console.log(JSON.stringify({{
  firstHtml,
  unchangedFirstHtml,
  changedHtml,
  unchangedAfterChangeHtml
}}));
"""
    output = run_node(script)

    assert 'data-device-soc-fill="WR&lt;&amp;1"' in output["firstHtml"]
    assert 'data-soc-start="72"' in output["firstHtml"]
    assert 'data-soc-target="72"' in output["firstHtml"]
    assert 'data-soc-animate="false"' in output["firstHtml"]
    assert 'style="' not in output["firstHtml"]
    assert ">72%</strong>" in output["firstHtml"]

    assert 'data-soc-start="72"' in output["unchangedFirstHtml"]
    assert 'data-soc-target="72"' in output["unchangedFirstHtml"]
    assert 'data-soc-animate="false"' in output["unchangedFirstHtml"]
    assert 'style="' not in output["unchangedFirstHtml"]

    assert 'data-soc-start="72"' in output["changedHtml"]
    assert 'data-soc-target="75"' in output["changedHtml"]
    assert 'data-soc-animate="true"' in output["changedHtml"]
    assert 'style="' not in output["changedHtml"]
    assert ">75%</strong>" in output["changedHtml"]

    assert 'data-soc-start="75"' in output["unchangedAfterChangeHtml"]
    assert 'data-soc-target="75"' in output["unchangedAfterChangeHtml"]
    assert 'data-soc-animate="false"' in output["unchangedAfterChangeHtml"]
    assert 'style="' not in output["unchangedAfterChangeHtml"]


def test_device_battery_visual_clamps_soc_and_renders_transform_fill():
    script = f"""
const app = require({json.dumps(str(APP_JS))});
const output = {{
  low: app.deviceBatteryVisual(0, 0, "Idle", "0 W", -5, false, "idle"),
  high: app.deviceBatteryVisual(0, 0, "Idle", "0 W", 125, false, "idle"),
  unsafeIndex: app.deviceBatteryVisual(0, 0, "Idle", "0 W", 50, false, "idle", "1\\" onclick=\\"alert(1)")
}};
console.log(JSON.stringify(output));
"""
    output = run_node(script)

    assert 'width="42"' in output["low"]
    assert 'data-battery-fill-start="0"' in output["low"]
    assert 'data-battery-fill-target="0"' in output["low"]
    assert 'style="' not in output["low"]
    assert ">0%</text>" in output["low"]
    assert 'class="battery-fill low"' in output["low"]

    assert 'width="42"' in output["high"]
    assert 'data-battery-fill-start="1"' in output["high"]
    assert 'data-battery-fill-target="1"' in output["high"]
    assert 'style="' not in output["high"]
    assert ">100%</text>" in output["high"]
    assert 'class="battery-fill full"' in output["high"]
    assert 'data-device-battery-fill="0"' in output["unsafeIndex"]
    assert "onclick" not in output["unsafeIndex"]


def test_dashboard_start_events_loads_live_once_and_prefers_sse():
    script = f"""
(async () => {{
const app = require({json.dumps(str(APP_JS))});
const connection = {{}};
const fetchCalls = [];
const timers = [];
const intervals = [];

class FakeEventSource {{
  static instances = [];

  constructor(url) {{
    this.url = url;
    this.closed = false;
    this.listeners = {{}};
    FakeEventSource.instances.push(this);
  }}

  addEventListener(name, callback) {{
    this.listeners[name] = callback;
  }}

  close() {{
    this.closed = true;
  }}
}}

global.document = {{
  getElementById(id) {{
    return id === "connectionState" ? connection : null;
  }}
}};
global.window = {{
  EventSource: FakeEventSource,
  setTimeout(callback, ms) {{
    timers.push({{ callback, ms }});
    return timers.length;
  }}
}};
global.EventSource = FakeEventSource;
global.fetch = async (url) => {{
  fetchCalls.push(url);
  return {{
    ok: false,
    status: 503,
    async json() {{
      return {{}};
    }}
  }};
}};
global.setInterval = (callback, ms) => {{
  intervals.push({{ callback, ms }});
  return intervals.length;
}};

app.resetLiveTransportForTests();
app.startEvents();
await Promise.resolve();

console.log(JSON.stringify({{
  fetchCalls,
  eventSourceUrl: FakeEventSource.instances[0].url,
  timerMs: timers.map((timer) => timer.ms),
  intervalCount: intervals.length
}}));
}})();
"""
    output = run_node(script)

    assert output["fetchCalls"] == ["/api/live"]
    assert output["eventSourceUrl"] == "/api/events"
    assert output["timerMs"] == [3000]
    assert output["intervalCount"] == 0


def test_dashboard_sse_early_error_falls_back_to_one_polling_interval():
    script = f"""
(async () => {{
const app = require({json.dumps(str(APP_JS))});
const connection = {{}};
const intervals = [];

class FakeEventSource {{
  static instances = [];

  constructor(url) {{
    this.url = url;
    this.closed = false;
    this.listeners = {{}};
    FakeEventSource.instances.push(this);
  }}

  addEventListener(name, callback) {{
    this.listeners[name] = callback;
  }}

  close() {{
    this.closed = true;
  }}
}}

global.document = {{
  getElementById(id) {{
    return id === "connectionState" ? connection : null;
  }}
}};
global.window = {{
  EventSource: FakeEventSource,
  setTimeout() {{
    return 1;
  }}
}};
global.EventSource = FakeEventSource;
global.fetch = async () => {{
  throw new Error("offline");
}};
global.setInterval = (callback, ms) => {{
  intervals.push({{ callback, ms }});
  return intervals.length;
}};

app.resetLiveTransportForTests();
app.startEvents();
const source = FakeEventSource.instances[0];
source.onerror();
source.onerror();
await Promise.resolve();

console.log(JSON.stringify({{
  closed: source.closed,
  intervalCount: intervals.length,
  intervalMs: intervals.map((interval) => interval.ms),
  transport: app.state.liveTransport
}}));
}})();
"""
    output = run_node(script)

    assert output["closed"] is True
    assert output["intervalCount"] == 1
    assert output["intervalMs"] == [2000]
    assert output["transport"] == "polling"
