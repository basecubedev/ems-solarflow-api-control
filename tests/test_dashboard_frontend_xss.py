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
