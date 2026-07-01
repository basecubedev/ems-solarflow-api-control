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


# A small DOM shim usable across the maintenance render/flow tests.
DOM_SHIM = """
const elements = new Map();
function fake(id) { return { id, innerHTML: "", textContent: "", value: "", disabled: false }; }
function ensure(id) { if (!elements.has(id)) elements.set(id, fake(id)); return elements.get(id); }
["maintenanceResults","maintenanceStatus","restorePlanArea","restoreConfirm",
 "restorePreview","restoreFile","restorePassword","maintenanceBackupDetail"].forEach(ensure);
global.document = { getElementById: (id) => elements.get(id) || null };
"""


def test_maintenance_auth_state_messages():
    script = f"""
const app = require({json.dumps(str(APP_JS))});
const out = {{}};
app.state.auth = {{ configured: false, authenticated: false, csrfToken: null }};
out.notConfigured = app.maintenanceAuthState();
app.state.auth = {{ configured: true, authenticated: false, csrfToken: null }};
out.notAuthenticated = app.maintenanceAuthState();
app.state.auth = {{ configured: true, authenticated: true, csrfToken: "t" }};
out.authenticated = app.maintenanceAuthState();
console.log(JSON.stringify(out));
"""
    output = run_node(script)
    assert "Configure a dashboard password" in output["notConfigured"]
    assert "Login required" in output["notAuthenticated"]
    assert output["authenticated"] == ""


def test_maintenance_view_escapes_dynamic_values_and_has_five_cards():
    malicious = "<img src=x onerror=alert(1)>"
    script = f"""
const app = require({json.dumps(str(APP_JS))});
{DOM_SHIM}
app.state.auth = {{ configured: true, authenticated: true, csrfToken: "t" }};
app.state.maintenance.status = {{
  config_path: {json.dumps(malicious)}, backup_dir: "data/backups",
  backup_types: ["config"],
  influxdb: {{ enabled: true, mode: {json.dumps(malicious)}, backup_supported: true, restore_supported: true }},
  restore_available_in_dashboard: true,
}};
app.state.maintenance.backups = [{{
  name: {json.dumps(malicious)}, backup_type: {json.dumps(malicious)},
  modified_at: "2026-06-29T20:15:00Z", size_bytes: 2048, encrypted: false,
  ems_version: {json.dumps(malicious)},
}}];
app.state.maintenance.configUpgrade = {{ changed: true, add_count: 3, comment_add_count: 4, comment_refresh_count: 21 }};
app.state.maintenance.restore.file = {json.dumps(malicious)};
app.state.maintenance.restore.previewed = true;
app.state.maintenance.restore.plan = {{
  file: {json.dumps(malicious)}, backup_type: "config",
  actions: [{{ action: "would_replace_conflict", path: {json.dumps(malicious)} }}],
  warnings: [{json.dumps(malicious)}], requires_restart: true, requires_relogin: true,
}};
app.renderMaintenanceView();
const html = elements.get("maintenanceResults").innerHTML;
console.log(JSON.stringify({{
  hasRaw: html.includes({json.dumps(malicious)}),
  escaped: html.includes("&lt;img src=x onerror=alert(1)&gt;"),
  cards: (html.match(/maintenance-stage-number/g) || []).length,
  hasSafety: html.includes("Safety Notes"),
  hasRestoreSelect: html.includes("data-maintenance-restore-file"),
  hasPasswordField: html.includes("restorePassword"),
  backupActionsAfterList:
    html.indexOf('data-maintenance-action="backup-config"') >
    html.indexOf("maintenance-backup-list"),
}}));
"""
    output = run_node(script)
    assert output["hasRaw"] is False
    assert output["escaped"] is True
    assert output["cards"] == 5
    assert output["hasSafety"] is True
    assert output["hasRestoreSelect"] is True
    assert output["hasPasswordField"] is True
    assert output["backupActionsAfterList"] is True


def test_maintenance_auth_gate_renders_empty_state():
    script = f"""
const app = require({json.dumps(str(APP_JS))});
{DOM_SHIM}
app.state.auth = {{ configured: true, authenticated: false, csrfToken: null }};
app.renderMaintenanceView();
console.log(JSON.stringify({{ html: elements.get("maintenanceResults").innerHTML }}));
"""
    output = run_node(script)
    assert "Login required to use maintenance tools" in output["html"]


def test_maintenance_tab_toggling_hides_other_panels():
    script = f"""
const app = require({json.dumps(str(APP_JS))});

class FakeElement {{
  constructor(id) {{ this.id = id; this.hidden = false; this.dataset = {{}};
    this.innerHTML = ""; this.textContent = ""; this.value = "";
    this.classList = {{ _s: new Set(),
      toggle(c, on) {{ if (on) this._s.add(c); else this._s.delete(c); }},
      add(c) {{ this._s.add(c); }}, remove(c) {{ this._s.delete(c); }},
      contains(c) {{ return this._s.has(c); }} }}; }}
  setAttribute() {{}}
}}

const ids = ["flowSvg","deviceFlowView","controlExplainView","energyStatsView",
  "diagnoseView","logsView","maintenanceView","analyticsView","maintenanceResults","maintenanceStatus"];
const elements = new Map(ids.map((id) => [id, new FakeElement(id)]));
const wrap = new FakeElement("wrap");
const shell = new FakeElement("shell");

global.window = {{ localStorage: {{ getItem() {{ return null; }}, setItem() {{}} }} }};
global.fetch = () => Promise.reject(new Error("no network in test"));
global.document = {{
  getElementById(id) {{ return elements.get(id) || null; }},
  querySelector(sel) {{ if (sel === ".flow-wrap") return wrap; if (sel === ".shell") return shell; return null; }},
  querySelectorAll() {{ return []; }},
}};

app.state.auth = {{ configured: true, authenticated: true, csrfToken: "t" }};
app.setFlowView("maintenance", false);
const r1 = {{
  maintenanceHidden: elements.get("maintenanceView").hidden,
  svgHidden: elements.get("flowSvg").hidden,
  logsHidden: elements.get("logsView").hidden,
  wrapHasMaintenance: wrap.classList.contains("view-maintenance"),
  shellHasMaintenance: shell.classList.contains("view-maintenance"),
}};
app.setFlowView("aggregated", false);
const r2 = {{ maintenanceHidden: elements.get("maintenanceView").hidden }};
console.log(JSON.stringify({{ r1, r2 }}));
"""
    output = run_node(script)
    assert output["r1"]["maintenanceHidden"] is False
    assert output["r1"]["svgHidden"] is True
    assert output["r1"]["logsHidden"] is True
    assert output["r1"]["wrapHasMaintenance"] is True
    assert output["r1"]["shellHasMaintenance"] is True
    assert output["r2"]["maintenanceHidden"] is True


def test_create_backup_posts_with_csrf():
    script = f"""
const app = require({json.dumps(str(APP_JS))});
{DOM_SHIM}
const calls = [];
global.fetch = (url, opts) => {{
  calls.push({{ url, opts }});
  return Promise.resolve({{ ok: true, json: () => Promise.resolve({{ created: true, backup: {{ name: "ems-config-x.tar.gz" }} }}) }});
}};
app.state.auth = {{ configured: true, authenticated: true, csrfToken: "csrf-123" }};
app.createMaintenanceBackup("config").then(() => {{
  const create = calls.find((c) => c.url === "/api/maintenance/backups/create");
  console.log(JSON.stringify({{
    method: create.opts.method, csrf: create.opts.headers["X-CSRF-Token"],
    body: JSON.parse(create.opts.body),
  }}));
}});
"""
    output = run_node(script)
    assert output["method"] == "POST"
    assert output["csrf"] == "csrf-123"
    assert output["body"] == {"type": "config"}


def test_restore_confirm_disabled_until_preview():
    script = f"""
const app = require({json.dumps(str(APP_JS))});
{DOM_SHIM}
app.state.auth = {{ configured: true, authenticated: true, csrfToken: "t" }};
app.state.maintenance.status = {{ backup_types: ["config"], influxdb: {{ enabled: false }}, restore_available_in_dashboard: true }};
app.state.maintenance.backups = [{{ name: "ems-config-manual-x.tar.gz", backup_type: "config", modified_at: "t", size_bytes: 10, encrypted: false }}];
// No preview yet, but a file is selected.
app.state.maintenance.restore.file = "ems-config-manual-x.tar.gz";
app.state.maintenance.restore.previewed = false;
const before = app.maintenanceRestoreStage();
app.state.maintenance.restore.previewed = true;
const after = app.maintenanceRestoreStage();
function confirmDisabled(html) {{
  const i = html.indexOf('id="restoreConfirm"');
  const tag = html.slice(html.lastIndexOf('<button', i), html.indexOf('>', i) + 1);
  return tag.includes('disabled');
}}
console.log(JSON.stringify({{ before: confirmDisabled(before), after: confirmDisabled(after) }}));
"""
    output = run_node(script)
    assert output["before"] is True
    assert output["after"] is False


def test_preview_restore_posts_with_csrf_and_password():
    script = f"""
const app = require({json.dumps(str(APP_JS))});
{DOM_SHIM}
elements.get("restoreFile").value = "ems-config-manual-x.tar.gz";
elements.get("restorePassword").value = "topsecret";
const calls = [];
global.fetch = (url, opts) => {{
  calls.push({{ url, opts }});
  return Promise.resolve({{ ok: true, json: () => Promise.resolve({{ file: "x", backup_type: "config", actions: [], warnings: [] }}) }});
}};
app.state.auth = {{ configured: true, authenticated: true, csrfToken: "csrf-9" }};
app.previewRestore().then(() => {{
  const call = calls.find((c) => c.url === "/api/maintenance/backups/restore-plan");
  const body = JSON.parse(call.opts.body);
  console.log(JSON.stringify({{
    method: call.opts.method, csrf: call.opts.headers["X-CSRF-Token"],
    file: body.file, password: body.password,
    previewed: app.state.maintenance.restore.previewed,
  }}));
}});
"""
    output = run_node(script)
    assert output["method"] == "POST"
    assert output["csrf"] == "csrf-9"
    assert output["file"] == "ems-config-manual-x.tar.gz"
    assert output["password"] == "topsecret"
    assert output["previewed"] is True


def test_confirm_restore_clears_password_and_state_after_request():
    script = f"""
const app = require({json.dumps(str(APP_JS))});
{DOM_SHIM}
elements.get("restorePassword").value = "topsecret";
const calls = [];
global.fetch = (url, opts) => {{
  calls.push({{ url, opts }});
  return Promise.resolve({{ ok: true, json: () => Promise.resolve({{ restored: true, message: "done", requires_restart: true }}) }});
}};
global.fetch = ((orig) => (url, opts) => {{
  calls.push({{ url, opts }});
  if (url === "/api/maintenance/backups") return Promise.resolve({{ ok: true, json: () => Promise.resolve({{ items: [] }}) }});
  return Promise.resolve({{ ok: true, json: () => Promise.resolve({{ restored: true, message: "done", requires_restart: true }}) }});
}})();
app.state.auth = {{ configured: true, authenticated: true, csrfToken: "t" }};
app.state.maintenance.restore.file = "ems-config-manual-x.tar.gz";
app.state.maintenance.restore.previewed = true;
app.confirmRestore().then(() => {{
  const restoreCall = calls.find((c) => c.url === "/api/maintenance/backups/restore");
  const body = JSON.parse(restoreCall.opts.body);
  const html = elements.get("maintenanceResults").innerHTML;
  console.log(JSON.stringify({{
    sentPassword: body.password,
    confirms: [body.confirm_preview, body.confirm_restore, body.confirm_replace],
    passwordInputCleared: elements.get("restorePassword").value === "",
    previewedReset: app.state.maintenance.restore.previewed,
    htmlHasSecret: html.includes("topsecret"),
  }}));
}});
"""
    output = run_node(script)
    assert output["sentPassword"] == "topsecret"
    assert output["confirms"] == [True, True, True]
    assert output["passwordInputCleared"] is True
    assert output["previewedReset"] is False
    assert output["htmlHasSecret"] is False


def test_status_card_uses_friendly_backup_type_labels():
    script = f"""
const app = require({json.dumps(str(APP_JS))});
{DOM_SHIM}
app.state.auth = {{ configured: true, authenticated: true, csrfToken: "t" }};
app.state.maintenance.status = {{
  config_path: "config/config.json", backup_dir: "data/backups",
  backup_types: ["config", "databases", "influxdb"],
  influxdb: {{ enabled: true, mode: "bundled", backup_supported: true, restore_supported: true }},
  restore_available_in_dashboard: true,
}};
app.renderMaintenanceView();
const html = elements.get("maintenanceResults").innerHTML;
console.log(JSON.stringify({{
  hasFriendly: html.includes("Local SQLite DBs") && html.includes("Analytics (InfluxDB)"),
  hasRawList: html.includes("config, databases, influxdb"),
  labelHelper: [app.maintenanceBackupTypeLabel("databases"), app.maintenanceBackupTypeLabel("influxdb")],
}}));
"""
    output = run_node(script)
    assert output["hasFriendly"] is True
    assert output["hasRawList"] is False
    assert output["labelHelper"] == ["Local SQLite DBs", "Analytics (InfluxDB)"]


def test_maintenance_visible_and_auth_gated_before_login():
    script = f"""
const app = require({json.dumps(str(APP_JS))});

class FakeElement {{
  constructor(id) {{ this.id = id; this.hidden = false; this.dataset = {{}};
    this.innerHTML = ""; this.textContent = ""; this.value = "";
    this.classList = {{ _s: new Set(), toggle(c, on) {{ if (on) this._s.add(c); else this._s.delete(c); }},
      add(c) {{ this._s.add(c); }}, remove(c) {{ this._s.delete(c); }}, contains(c) {{ return this._s.has(c); }} }}; }}
  setAttribute() {{}}
}}
const ids = ["flowSvg","deviceFlowView","controlExplainView","energyStatsView",
  "diagnoseView","logsView","maintenanceView","analyticsView","maintenanceResults","maintenanceStatus","writeModeState","authButton"];
const elements = new Map(ids.map((id) => [id, new FakeElement(id)]));
const wrap = new FakeElement("wrap");
const shell = new FakeElement("shell");
const maintTab = new FakeElement("maintTab");
global.window = {{ localStorage: {{ getItem() {{ return null; }}, setItem() {{}} }} }};
const calls = [];
global.fetch = (url) => {{
  calls.push(url);
  return Promise.reject(new Error("no network"));
}};
global.document = {{
  getElementById(id) {{ return elements.get(id) || null; }},
  querySelector(sel) {{
    if (sel === ".flow-wrap") return wrap;
    if (sel === ".shell") return shell;
    if (sel === '[data-flow-view="maintenance"]') return maintTab;
    return null;
  }},
  querySelectorAll() {{ return []; }},
}};

const out = {{}};
app.state.auth = {{ configured: true, authenticated: false, csrfToken: null }};
app.setFlowView("maintenance", false);
out.viewWhenLoggedOut = app.state.flowView;
out.maintenanceHiddenLoggedOut = elements.get("maintenanceView").hidden;
out.htmlWhenLoggedOut = elements.get("maintenanceResults").innerHTML;
app.renderAuthState();
out.tabHiddenLoggedOut = maintTab.hidden;
out.maintenanceCalls = calls.filter((url) => String(url).startsWith("/api/maintenance/"));
console.log(JSON.stringify(out));
"""
    output = run_node(script)
    assert output["viewWhenLoggedOut"] == "maintenance"
    assert output["maintenanceHiddenLoggedOut"] is False
    assert output["tabHiddenLoggedOut"] is False
    assert "Login required to use maintenance tools" in output["htmlWhenLoggedOut"]
    assert "data-maintenance-action" not in output["htmlWhenLoggedOut"]
    assert "data-maintenance-restore-file" not in output["htmlWhenLoggedOut"]
    assert output["maintenanceCalls"] == []


def test_maintenance_tab_is_not_hidden_in_initial_markup():
    html = (ROOT / "dashboard" / "static" / "index.html").read_text()
    marker = 'data-flow-view="maintenance"'
    start = html.index("<button", html.index(marker) - len("<button"))
    end = html.index(">", html.index(marker))
    assert " hidden" not in html[start:end]


def test_maintenance_loads_status_backups_and_upgrade_after_login():
    script = f"""
const app = require({json.dumps(str(APP_JS))});
{DOM_SHIM}
const calls = [];
global.fetch = (url) => {{
  calls.push(url);
  const payloads = {{
    "/api/maintenance/status": {{
      config_path: "config/config.json", backup_dir: "data/backups",
      backup_types: ["config"], influxdb: {{ enabled: false }},
      restore_available_in_dashboard: true,
    }},
    "/api/maintenance/backups": {{ items: [{{
      name: "ems-config-manual-x.tar.gz", backup_type: "config",
      modified_at: "2026-06-30T10:00:00Z", size_bytes: 1024,
      encrypted: false, ems_version: "0.6.0",
    }}] }},
    "/api/maintenance/config-upgrade": {{
      changed: true, add_count: 2, comment_add_count: 1,
      comment_refresh_count: 0, plan_id: "plan-1", apply_available: true,
    }},
  }};
  return Promise.resolve({{ ok: true, json: () => Promise.resolve(payloads[url]) }});
}};
app.state.auth = {{ configured: true, authenticated: true, csrfToken: "csrf" }};
app.enterMaintenanceView();
setTimeout(() => {{
  const html = elements.get("maintenanceResults").innerHTML;
  console.log(JSON.stringify({{
    calls,
    hasStatus: html.includes("config/config.json"),
    hasBackup: html.includes("ems-config-manual-x.tar.gz"),
    hasUpgrade: html.includes("2 config value changes planned"),
  }}));
}}, 0);
"""
    output = run_node(script)
    assert output["calls"] == [
        "/api/maintenance/status",
        "/api/maintenance/backups",
        "/api/maintenance/config-upgrade",
    ]
    assert output["hasStatus"] is True
    assert output["hasBackup"] is True
    assert output["hasUpgrade"] is True


def test_backup_details_show_provenance_and_escape():
    malicious = "<b>x</b>"
    script = f"""
const app = require({json.dumps(str(APP_JS))});
const payload = {{ manifest_available: true, encrypted: false, manifest: {{
  backup_type: "config", backup_purpose: "rollback", backup_format: 1,
  created_at: "2026-06-29T20:15:00Z", ems_version: "0.6.0",
  git_commit_short: "b87add4", git_branch: "main", encrypted: false,
  rollback_for: "ems-config-manual-x.tar.gz", skipped_count: 1,
  files: [
    {{ path: "config.json", kind: "config", sensitive: true }},
    {{ path: "runtime-state.json", kind: "runtime_state" }},
    {{ path: {json.dumps(malicious)}, kind: "dashboard_auth", sensitive: true, privacy_relevant: true }},
  ],
}} }};
const html = app.renderBackupManifest("ems-config-rollback-x.tar.gz", payload);
const enc = app.renderBackupManifest("x.enc", {{ manifest_available: false, encrypted: true }});
console.log(JSON.stringify({{
  hasRaw: html.includes({json.dumps(malicious)}),
  escaped: html.includes("&lt;b&gt;x&lt;/b&gt;"),
  hasProvenance: html.includes("Source EMS") && html.includes("Source revision"),
  hasRollback: html.includes("Rollback of"),
  readableKinds: html.includes("Runtime state") && html.includes("Dashboard auth"),
  badges: html.includes("badge-secret") && html.includes("badge-privacy"),
  encryptedHint: enc.includes("restore preview with the backup password")
    && enc.includes("emsctl with the password"),
}}));
"""
    output = run_node(script)
    assert output["hasRaw"] is False
    assert output["escaped"] is True
    assert output["hasProvenance"] is True
    assert output["hasRollback"] is True
    assert output["readableKinds"] is True
    assert output["badges"] is True
    assert output["encryptedHint"] is True


def test_running_state_disables_action_buttons():
    script = f"""
const app = require({json.dumps(str(APP_JS))});
{DOM_SHIM}
app.state.auth = {{ configured: true, authenticated: true, csrfToken: "t" }};
app.state.maintenance.status = {{ backup_types: ["config"], influxdb: {{ enabled: false }}, restore_available_in_dashboard: true }};
app.state.maintenance.running = true;
app.renderMaintenanceView();
const html = elements.get("maintenanceResults").innerHTML;
const buttonCount = (html.match(/data-maintenance-action/g) || []).length;
const disabledCount = (html.match(/disabled/g) || []).length;
console.log(JSON.stringify({{ buttonCount, disabledCount }}));
"""
    output = run_node(script)
    assert output["buttonCount"] > 0
    assert output["disabledCount"] >= output["buttonCount"]


def test_config_upgrade_preview_renders_concrete_escaped_sections():
    malicious = "</code><img src=x onerror=alert(1)>"
    script = f"""
const app = require({json.dumps(str(APP_JS))});
{DOM_SHIM}
app.state.auth = {{ configured: true, authenticated: true, csrfToken: "t" }};
app.state.maintenance.configUpgrade = {{
  changed: true, apply_available: true, plan_id: "plan-1",
  add_count: 1, migration_count: 1, comment_add_count: 1,
  comment_refresh_count: 1, format_changed: true,
  items: [
    {{ kind: "add", path: "config_upgrade.on_startup", value: "check" }},
    {{ kind: "migrate", path: "system.mode", old_value: "old", value: "new" }},
    {{ kind: "comment_add", path: "system._comment_mode" }},
    {{ kind: "comment_refresh", path: {json.dumps(malicious)} }},
  ],
}};
const html = app.maintenanceConfigUpgradeStage();
console.log(JSON.stringify({{
  hasRaw: html.includes({json.dumps(malicious)}),
  hasAdded: html.includes("Added keys") && html.includes("config_upgrade.on_startup"),
  hasMigration: html.includes("Migrated values") && html.includes("old") && html.includes("new"),
  hasComments: html.includes("New explanatory comments") && html.includes("Comment refresh"),
  hasFormat: html.includes("template layout"),
  escaped: html.includes("&lt;/code&gt;&lt;img"),
}}));
"""
    output = run_node(script)
    assert output == {
        "hasRaw": False,
        "hasAdded": True,
        "hasMigration": True,
        "hasComments": True,
        "hasFormat": True,
        "escaped": True,
    }


def test_config_upgrade_comment_only_state_has_explicit_action():
    script = f"""
const app = require({json.dumps(str(APP_JS))});
{DOM_SHIM}
app.state.auth = {{ configured: true, authenticated: true, csrfToken: "t" }};
app.state.maintenance.configUpgrade = {{
  changed: false, apply_available: true, plan_id: "plan-2",
  add_count: 0, migration_count: 0, comment_add_count: 0,
  comment_refresh_count: 21, format_changed: false,
  items: [{{ kind: "comment_refresh", path: "system._comment" }}],
}};
const html = app.maintenanceConfigUpgradeStage();
console.log(JSON.stringify({{
  valuesCurrent: html.includes("Config values are up to date."),
  refreshCount: html.includes("21 explanatory comments can be refreshed"),
  action: html.includes("Refresh comments with backup"),
  misleading: html.includes("Config is already up to date."),
}}));
"""
    output = run_node(script)
    assert output["valuesCurrent"] is True
    assert output["refreshCount"] is True
    assert output["action"] is True
    assert output["misleading"] is False


def test_apply_config_upgrade_confirms_and_sends_preview_contract():
    script = f"""
const app = require({json.dumps(str(APP_JS))});
{DOM_SHIM}
const calls = [];
global.window = {{ confirm: () => true }};
global.fetch = (url, opts = {{}}) => {{
  calls.push({{ url, opts }});
  if (url === "/api/maintenance/config-upgrade/apply") {{
    return Promise.resolve({{ ok: true, json: () => Promise.resolve({{
      changed: true, backup_name: "ems-config-manual-x.tar.gz",
      requires_restart: true, message: "Config upgraded.",
    }}) }});
  }}
  if (url === "/api/maintenance/config-upgrade") {{
    return Promise.resolve({{ ok: true, json: () => Promise.resolve({{ apply_available: false }}) }});
  }}
  return Promise.resolve({{ ok: true, json: () => Promise.resolve({{ items: [] }}) }});
}};
app.state.auth = {{ configured: true, authenticated: true, csrfToken: "csrf-1" }};
app.state.maintenance.configUpgrade = {{
  changed: true, apply_available: true, plan_id: "preview-123",
  add_count: 1, migration_count: 0, comment_add_count: 0,
  comment_refresh_count: 0, items: [],
}};
app.applyConfigUpgrade().then(() => {{
  const call = calls.find((item) => item.url === "/api/maintenance/config-upgrade/apply");
  console.log(JSON.stringify({{
    csrf: call.opts.headers["X-CSRF-Token"],
    body: JSON.parse(call.opts.body),
    message: app.state.maintenance.message,
  }}));
}});
"""
    output = run_node(script)
    assert output["csrf"] == "csrf-1"
    assert output["body"] == {
        "refresh_comments": True,
        "confirm_apply": True,
        "plan_id": "preview-123",
    }
    assert "ems-config-manual-x.tar.gz" in output["message"]
    assert "Restart EMS" in output["message"]


def test_backup_headers_are_explicitly_left_aligned():
    css = (ROOT / "dashboard" / "static" / "styles.css").read_text()
    header_rule = css.split(".maintenance-backup-head > span {", 1)[1].split("}", 1)[0]
    row_rule = css.split(".maintenance-backup-row {", 1)[1].split("}", 1)[0]
    assert "text-align: left;" in header_rule
    assert "justify-self: start;" in header_rule
    assert "minmax(76px, .75fr)" in row_rule
