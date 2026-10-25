# SPDX-License-Identifier: AGPL-3.0-or-later
"""Local docs-preview server for the Admin Console.

Serves the real admin static assets (``admin/static/``) as the Admin server
does -- Modern at the root, the deprecated Classic under ``/classic/`` -- with deterministic,
non-secret demo API responses loaded from ``tests/fixtures/admin_docs/`` so the
Admin Console screens can be screenshotted for the docs without real hardware,
Docker, discovery, MQTT, config.json, passwords, or a running EMS.

The Modern console is the one the documentation screenshots show
(``scripts/capture_admin_docs.mjs``). A page names the installation it
shows with ``?scenario=<name>`` in its own address -- a fresh host, a signed-out
browser, an update that runs, and so on (``SCENARIOS``); without one it shows
the installed demo system. The settings page, its check and the telemetry view
are answered by the shipped Admin builders from the demo installation's
``config.json`` (``demo_installation.json`` laid over the shipped template), so
they can never describe a settings layout the product no longer has. A fixed,
non-secret ``DEMO_IDENTITY_KEY`` signs the device identities of the settings
and of the network search alike, as the Admin server's own key does, so both
agree on which device is which.

The Classic console keeps a small drive script (``admin_docs_preview.js``,
injected before ``</body>`` of the Classic page) for the demo videos
(``scripts/render_admin_docs_video.py``): it reads a ``?screen=`` query
parameter and navigates the already-authenticated Classic SPA to that screen.

Usage::

    python3 scripts/serve_admin_docs_preview.py
    python3 scripts/serve_admin_docs_preview.py --host 127.0.0.1 --port 8092

Open, for example::

    http://127.0.0.1:8092/#/overview
    http://127.0.0.1:8092/?scenario=fresh#/assistant
    http://127.0.0.1:8092/?scenario=upgrade-running#/updates

Safety: the preview never contacts real Zendure/Shelly/MQTT/Docker/InfluxDB
endpoints, never reads secrets from config.json, and never writes runtime-state,
auth files, config or the dashboard database. Its only write is the demo
``config.json`` in a temporary directory of its own, removed when it stops. It
binds to loopback by default.
"""

import argparse
import base64
import copy
import json
import os
import shutil
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(SCRIPT_DIR)
STATIC_DIR = os.path.join(ROOT, "admin", "static")
CLASSIC_FILES = {
    "/classic/admin.js": ("admin.js", "application/javascript"),
    "/classic/admin.css": ("admin.css", "text/css"),
    "/classic/admin-theme.js": ("admin-theme.js", "application/javascript"),
}
FIXTURE_DIR = os.path.join(ROOT, "tests", "fixtures", "admin_docs")
TEMPLATE_PATH = os.path.join(ROOT, "config", "config.template.json")
DRIVE_SCRIPT = os.path.join(SCRIPT_DIR, "admin_docs_preview.js")

DEFAULT_HOST = "127.0.0.1"
DEMO_IDENTITY_KEY = b"ems-admin-docs-preview-demo-key!"
DEFAULT_PORT = 8092
DISPLAY_CONFIG_PATH = "/opt/ems/config/config.json"

SCENARIOS = (
    "installed",
    "no-backups",
    "settings-saved",
    "fresh",
    "signed-out",
    "first-start",
    "setup",
    "upgrade-running",
    "upgrade-done",
    "upgrade-reconnect",
    "recovery",
    "mqtt",
)

# Classic screens of the demo videos that need an active, server-confirmed
# Fresh Setup transition to be reachable at all (Guided Setup steps 02-05).
SETUP_STEP_SCREENS = frozenset({"discovery", "config-preview", "setup-start-done"})

# Firefox ``--screenshot`` captures at the ``load`` event and does not wait for
# post-load fetches. A hidden image pointed at ``/__hold`` deliberately delays
# ``load`` so the Classic SPA has time to authenticate, fetch its demo data,
# navigate to the requested screen and render before a video frame is taken.
HOLD_SECONDS = 5.0
# 1x1 transparent PNG.
_HOLD_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)


def _load_fixture(name):
    with open(os.path.join(FIXTURE_DIR, name), encoding="utf-8") as handle:
        return json.load(handle)


def _on_path():
    if ROOT not in sys.path:
        sys.path.insert(0, ROOT)


def _overlay(base, changes):
    """``changes`` laid over ``base``: objects key by key, lists item by item."""

    if isinstance(base, dict) and isinstance(changes, dict):
        merged = copy.deepcopy(base)
        for key, value in changes.items():
            merged[key] = _overlay(base.get(key), value) if key in base else copy.deepcopy(value)
        return merged
    if isinstance(base, list) and isinstance(changes, list):
        merged = [copy.deepcopy(item) for item in base[: len(changes)]]
        merged += [None] * (len(changes) - len(merged))
        return [_overlay(merged[index], value) for index, value in enumerate(changes)]
    return copy.deepcopy(changes)


def demo_config():
    """The demo installation's config.json: its own values over the shipped template."""

    with open(TEMPLATE_PATH, encoding="utf-8") as handle:
        template = json.load(handle)
    demo = _load_fixture("demo_installation.json")
    config = _overlay(template, demo["config"])
    for dotted in demo.get("without", []):
        *parents, leaf = dotted.split(".")
        node = config
        for part in parents:
            node = node.get(part, {})
        node.pop(leaf, None)
    return config


DEMO_ISOLATED_ENV = ("EMS_CONFIG_FILE", "EMS_CONFIG_DIR", "EMS_INSTALL_DIR", "EMS_TEMPLATE_FILE", "EMS_ADMIN_DATA_DIR")


class DemoInstallation:
    """A throwaway install root holding the demo config.json, read by the real builders.

    The variables that point the builders at a real installation are cleared
    first, so neither the demo write nor any read can reach one: the
    preview's only write lands in its own temporary directory.
    """

    def __init__(self):
        for name in DEMO_ISOLATED_ENV:
            os.environ.pop(name, None)
        _on_path()
        from ems import paths

        self.root = tempfile.mkdtemp(prefix="ems-admin-docs-")
        config_path = paths.standard_config_path(self.root)
        os.makedirs(os.path.dirname(config_path), exist_ok=True)
        with open(config_path, "w", encoding="utf-8") as handle:
            json.dump(demo_config(), handle, indent=2)
            handle.write("\n")

    def config(self):
        from admin.maintenance_config import load_maintenance_config

        payload = load_maintenance_config(base_dir=self.root, identity_token_key=DEMO_IDENTITY_KEY)
        payload["config_path"] = DISPLAY_CONFIG_PATH
        return payload

    def preview(self, draft):
        from admin.maintenance_config import preview_maintenance_config

        payload = preview_maintenance_config(draft, base_dir=self.root, identity_token_key=DEMO_IDENTITY_KEY)
        payload["config_path"] = DISPLAY_CONFIG_PATH
        return payload

    def telemetry(self):
        from admin.zendure_mqtt_runtime_status import build_runtime_status_view

        return build_runtime_status_view(base_dir=self.root)

    def remove(self):
        shutil.rmtree(self.root, ignore_errors=True)


def _tokens(value):
    return tuple(entry for entry in value if isinstance(entry, str) and entry.strip()) if isinstance(value, list) else ()


class DemoDiscovery:
    """The demo host's discovery sources, in the shape the shipped discovery builders read.

    One object stands in for the scan registry, mDNS, the local MQTT brokers
    and the Zendure cloud: mDNS has found the demo devices, nothing else has
    found anything. The guided setup's device search and device plan are then
    answered by ``run_discovery`` and ``build_setup_plan`` themselves.
    """

    def __init__(self, devices):
        self._devices = devices

    def all_devices(self):
        return []

    def devices(self):
        return copy.deepcopy(self._devices)

    def candidates(self):
        return []

    def settings(self):
        return {"token_saved": False, "tls_mode": "system_ca", "last_broker": "", "last_status": ""}

    def ensure_device_list_candidates(self):
        return None

    def refresh(self):
        return {"ok": True}

    def run(self, refresh):
        _on_path()
        from admin.discovery_preparation import default_preparation
        from admin.discovery_run import run_discovery

        return run_discovery(
            default_preparation(),
            refresh=refresh,
            registry=self,
            mdns_provider=self,
            mqtt_discovery=self,
            zendure_cloud_discovery=self,
            identity_token_key=DEMO_IDENTITY_KEY,
        )

    def plan(self, body):
        _on_path()
        from admin.connection_capability import connection_output_control
        from admin.discovery_preparation import default_preparation
        from admin.observation_identity import stamp_observations
        from admin.setup_planner import OBSERVATION_REF_FIELD, build_setup_plan

        observations = stamp_observations(self.devices(), key=DEMO_IDENTITY_KEY)
        by_id = {device["observation_id"]: device for device in observations}
        submitted = (body.get("candidates") or {}).get("observations") or []
        for reference in submitted:
            if isinstance(reference, dict) and reference.get(OBSERVATION_REF_FIELD):
                device = by_id.get(str(reference.get("observation_id") or ""))
                if device is not None:
                    device[OBSERVATION_REF_FIELD] = str(reference[OBSERVATION_REF_FIELD])
        preparation = default_preparation()
        return build_setup_plan(
            body.get("state") if isinstance(body.get("state"), dict) else {},
            observations=observations,
            proposals=[],
            priority=preparation["discovery_priority"],
            enabled_sources={source: entry["enabled"] for source, entry in preparation["sources"].items()},
            identity_token_key=DEMO_IDENTITY_KEY,
            control_supported=connection_output_control,
            confirmed_switches=_tokens(body.get("confirmed_switches")),
            declined_switches=_tokens(body.get("declined_switches")),
            unresolved_references=[],
        )


def _with_live_health(overview_payload):
    """Derive the status page's findings list instead of freezing one.

    ``health`` is a projection of the rest of the overview, so a fixture copy of
    it would be a second answer that drifts. Deriving it here keeps the
    screenshot honest about what the shipped synthesis says for this demo
    installation.
    """

    _on_path()
    from admin.maintenance_health import build_maintenance_health

    payload = copy.deepcopy(overview_payload)
    payload["health"] = build_maintenance_health(payload)
    return payload


def _settings_saved(overview, plan, change):
    """The installed system right after a settings change EMS has not read yet."""

    overview = copy.deepcopy(overview)
    overview["paths"]["config"]["modified_at"] = change["config_modified_at"]
    plan = copy.deepcopy(plan)
    for key in ("config_state", "actions", "summary"):
        plan[key] = copy.deepcopy(change[key])
    return _with_live_health(overview), plan


def _after_upgrade(overview, tag):
    """The installed system once the Guided Upgrade to ``tag`` has finished."""

    overview = copy.deepcopy(overview)
    image = f"ghcr.io/example/ems-solarflow-api-control:{tag}"
    overview["containers"]["ems"].update(image=image, tag=tag, started_at="2026-07-05T13:29:40+00:00")
    overview["components"]["ems"].update(image=image, tag=tag)
    overview["components"]["admin"].update(image=f"ghcr.io/example/ems-admin:{tag}", tag=tag)
    return _with_live_health(overview)


def _admin_replaced(overview, tag):
    """Midway through the Guided Upgrade to ``tag``: the new Admin console answers, EMS is not replaced yet."""

    overview = copy.deepcopy(overview)
    overview["components"]["admin"].update(image=f"ghcr.io/example/ems-admin:{tag}", tag=tag)
    return _with_live_health(overview)


def build_routes():
    """The installed demo system: (method, api-path) to the payload the console expects."""

    common = _load_fixture("common.json")
    overview = _load_fixture("maintenance_overview_demo.json")
    backups = _load_fixture("backup_restore_demo.json")
    upgrade = _load_fixture("guided_upgrade_demo.json")
    setup = _load_fixture("guided_setup_demo.json")
    diagnostics = _load_fixture("diagnostics_demo.json")

    get_routes = {
        "/api/admin/auth/status": common["auth_status"],
        "/api/admin/install-state": common["install_state"],
        "/api/discovery/mdns/status": common["mdns_status"],
        "/api/discovery/devices": common["mdns_devices"],
        "/api/discovery/mqtt-brokers": common["mqtt_brokers"],
        "/api/discovery/result/" + common["scan_started"]["scan_id"]: common["scan_result"],
        "/api/admin/maintenance/admin-update/resume": common["admin_update_resume"],
        "/api/admin/maintenance/overview": _with_live_health(overview["overview"]),
        "/api/admin/maintenance/containers/plan": overview["containers_plan"],
        "/api/admin/maintenance/backups": backups["backups_list"],
        # Nothing to migrate: the card is expected to be absent.
        "/api/admin/maintenance/zendure-mqtt/migration-review": overview["migration_review_none"],
        "/api/admin/maintenance/admin-update/status": upgrade["admin_update_status"],
        "/api/admin/system-alignment/status": upgrade["alignment_idle"],
        "/api/admin/workflow-lifecycle": overview["recovery_preview_idle"]["lifecycle"],
        "/api/setup/config-template": setup["config_template"],
        "/api/setup/config/catalog": setup["config_catalog"],
        "/api/discovery/networks": setup["networks"],
        "/api/setup/deployment/plan": setup["deployment_plan"],
        "/api/setup/deployment/status": setup["deployment_status"],
        # Without this the generic empty-payload fallback would clear
        # generated_ready and re-lock the deployment/start steps.
        "/api/setup/config/status": setup["config_status"],
    }
    post_routes = {
        "/api/discovery/scan": common["scan_started"],
        "/api/setup/discovery/scan": common["scan_started"],
        "/api/setup/config-preview": setup["config_preview"],
        "/api/admin/maintenance/admin-update/plan": upgrade["admin_update_plan"],
        "/api/admin/maintenance/upgrade/validate": upgrade["upgrade_validate"],
        "/api/admin/maintenance/diagnostics/run": diagnostics["diagnostics_run"],
        "/api/admin/maintenance/backups/restore/preview": backups["restore_preview"],
        "/api/admin/workflow-lifecycle/recovery/preview": overview["recovery_preview_idle"],
        "/api/admin/system-alignment/validate": setup["system_build_validate"],
    }
    # /api/setup/releases branches on the ?flow= query parameter.
    releases = {
        "upgrade": upgrade["releases_upgrade"],
        "setup": setup["releases_setup"],
    }
    return get_routes, post_routes, releases


def build_scenarios():
    """Each scenario's answers that differ from the installed demo system, by method and path."""

    common = _load_fixture("common.json")
    overview = _load_fixture("maintenance_overview_demo.json")
    backups = _load_fixture("backup_restore_demo.json")
    upgrade = _load_fixture("guided_upgrade_demo.json")
    setup = _load_fixture("guided_setup_demo.json")
    mqtt = _load_fixture("zendure_mqtt_demo.json")

    def alignment(transition, pair_state="transition_active"):
        pair = {**upgrade["alignment_idle"]["running_pair"], "state": pair_state}
        return {**upgrade["alignment_idle"], "active": True, "transition": transition, "running_pair": pair}

    jobs = "/api/admin/maintenance/upgrade/jobs/" + upgrade["upgrade_resume"]["job_id"]
    scan = "/api/discovery/result/" + common["scan_started"]["scan_id"]
    running = {
        "GET": {
            "/api/admin/system-alignment/status": alignment(upgrade["upgrade_transitions"]["running"]),
            "/api/admin/workflow-lifecycle": upgrade["upgrade_lifecycle"],
            "/api/admin/maintenance/overview": _admin_replaced(overview["overview"], upgrade["upgrade_validate"]["target_release"]),
            jobs: upgrade["upgrade_job_running"],
        },
        "POST": {"/api/admin/maintenance/upgrade/resume": upgrade["upgrade_resume"]},
    }
    saved_overview, saved_plan = _settings_saved(
        overview["overview"], overview["containers_plan"], overview["settings_saved"]
    )
    return {
        "installed": {},
        "no-backups": {"GET": {"/api/admin/maintenance/backups": backups["backups_list_empty"]}},
        "settings-saved": {
            "GET": {
                "/api/admin/maintenance/overview": saved_overview,
                "/api/admin/maintenance/containers/plan": saved_plan,
            }
        },
        "fresh": {
            "GET": {
                "/api/admin/maintenance/overview": _with_live_health(overview["overview_fresh"]),
                "/api/admin/maintenance/config": overview["config_missing"],
                "/api/admin/maintenance/backups": backups["backups_list_empty"],
            }
        },
        "signed-out": {"GET": {"/api/admin/auth/status": common["auth_status_signed_out"]}},
        "first-start": {"GET": {"/api/admin/auth/status": common["auth_status_first_start"]}},
        "setup": {
            "GET": {
                scan: {**common["scan_result"], "devices": common["mdns_devices"]["devices"]},
                "/api/admin/maintenance/overview": _with_live_health(overview["overview_fresh"]),
                "/api/admin/maintenance/config": overview["config_missing"],
                "/api/admin/system-alignment/status": setup["system_alignment_status"],
                "/api/admin/workflow-lifecycle": setup["setup_lifecycle"],
                "/api/setup/workflow": setup["setup_workflow"],
            }
        },
        "upgrade-running": running,
        "upgrade-done": {
            "GET": {
                **running["GET"],
                jobs: upgrade["upgrade_job_done"],
                "/api/admin/system-alignment/status": alignment(upgrade["upgrade_transitions"]["running"], "current"),
                "/api/admin/maintenance/overview": _after_upgrade(overview["overview"], upgrade["upgrade_validate"]["target_release"]),
            },
            "POST": running["POST"],
        },
        "upgrade-reconnect": {
            "GET": {
                "/api/admin/system-alignment/status": alignment(upgrade["upgrade_transitions"]["reconnect"]),
                "/api/admin/workflow-lifecycle": upgrade["upgrade_lifecycle"],
            }
        },
        "recovery": {
            "GET": {"/api/admin/workflow-lifecycle": overview["recovery_preview_blocked"]["lifecycle"]},
            "POST": {"/api/admin/workflow-lifecycle/recovery/preview": overview["recovery_preview_blocked"]},
        },
        "mqtt": {"GET": {"/api/admin/maintenance/zendure-mqtt/runtime-status": mqtt["runtime_status"]}},
    }


class DocsPreviewHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # keep the preview quiet
        pass

    def _referer_query(self):
        """The query of the page that issued this API call.

        Same-origin requests carry the full page address in Referer, which is
        the only way a page's scenario reaches the API calls it makes: the
        console's own fetches cannot carry it.
        """

        referer = self.headers.get("Referer") or ""
        return parse_qs(urlparse(referer).query)

    def _scenario(self):
        name = self._referer_query().get("scenario", ["installed"])[0]
        return name if name in self.server.scenarios else "installed"

    def _classic_screen(self):
        return self._referer_query().get("screen", [""])[0]

    def _scenario_answer(self, method, path):
        return self.server.scenarios[self._scenario()].get(method, {}).get(path)

    def _send_json(self, payload, status=200):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_bytes(self, body, content_type):
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_static(self, filename, content_type):
        try:
            with open(os.path.join(STATIC_DIR, filename), "rb") as handle:
                body = handle.read()
        except OSError:
            self.send_error(404)
            return
        self._send_bytes(body, content_type)

    def _send_modern(self, path):
        """The Modern console's build output, at the root as the Admin server serves it."""

        _on_path()
        from dashboard.static_files import build_static_asset_index, lookup_static_asset, modern_asset_path

        asset_path = modern_asset_path(path)
        asset = lookup_static_asset(build_static_asset_index(STATIC_DIR), asset_path) if asset_path else None
        if asset is None:
            self.send_error(404)
            return
        full_path, content_type = asset
        with open(full_path, "rb") as handle:
            self._send_bytes(handle.read(), content_type)

    def _send_index(self):
        try:
            with open(os.path.join(STATIC_DIR, "index.html"), encoding="utf-8") as handle:
                html = handle.read()
        except OSError:
            self.send_error(404)
            return
        inject = (
            '  <img src="/__hold" alt="" width="1" height="1"'
            ' style="position:absolute;left:-9999px;opacity:0" aria-hidden="true">\n'
            '  <script src="/_docs_preview.js"></script>\n</body>'
        )
        html = html.replace("</body>", inject, 1)
        self._send_bytes(html.encode("utf-8"), "text/html; charset=utf-8")

    def _stamped(self, path, payload):
        """A device list with the observation identity the Admin server attaches to every one."""

        if not path.startswith(("/api/discovery/devices", "/api/discovery/result/")):
            return payload
        from admin.observation_identity import stamp_observations

        payload = copy.deepcopy(payload)
        stamp_observations(payload["devices"], key=DEMO_IDENTITY_KEY)
        return payload

    def _read_body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        try:
            body = json.loads(self.rfile.read(length).decode("utf-8"))
        except ValueError:
            return {}
        return body if isinstance(body, dict) else {}

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        if path in ("/classic/", "/classic/index.html"):
            self._send_index()
            return
        if path in CLASSIC_FILES:
            self._send_static(*CLASSIC_FILES[path])
            return
        if path == "/__hold":
            # Slow response that holds the load event open (see HOLD_SECONDS).
            time.sleep(HOLD_SECONDS)
            self._send_bytes(_HOLD_PNG, "image/png")
            return
        if path == "/_docs_preview.js":
            try:
                with open(DRIVE_SCRIPT, "rb") as handle:
                    body = handle.read()
            except OSError:
                self.send_error(404)
                return
            self._send_bytes(body, "application/javascript")
            return
        if not path.startswith("/api/"):
            self._send_modern(path)
            return
        answer = self._scenario_answer("GET", path)
        if answer is not None:
            self._send_json(self._stamped(path, answer))
        elif path == "/api/setup/releases":
            flow = parse_qs(parsed.query).get("flow", ["setup"])[0]
            self._send_json(self.server.releases.get(flow, self.server.releases["setup"]))
        elif path == "/api/admin/system-alignment/status" and self._classic_screen() in SETUP_STEP_SCREENS:
            self._send_json(self.server.classic_setup_alignment)
        elif path == "/api/admin/maintenance/config":
            self._send_json(self.server.installation.config())
        elif path == "/api/admin/maintenance/zendure-mqtt/runtime-status":
            self._send_json(self.server.installation.telemetry())
        elif path in self.server.get_routes:
            self._send_json(self._stamped(path, self.server.get_routes[path]))
        else:
            # Any other read the console attempts degrades to a benign empty payload.
            self._send_json({"ok": True})

    def do_POST(self):
        path = urlparse(self.path).path
        body = self._read_body()
        if not path.startswith("/api/"):
            self.send_error(404)
            return
        answer = self._scenario_answer("POST", path)
        if answer is not None:
            self._send_json(answer)
        elif path == "/api/admin/auth/refresh":
            status = self._scenario_answer("GET", "/api/admin/auth/status")
            self._send_json(status or self.server.get_routes["/api/admin/auth/status"])
        elif path == "/api/setup/discovery/run":
            self._send_json(self.server.discovery.run(refresh=bool(body.get("refresh"))))
        elif path == "/api/setup/device-plan":
            self._send_json(self.server.discovery.plan(body))
        elif path == "/api/admin/maintenance/config/preview":
            draft = body.get("draft") if isinstance(body.get("draft"), dict) else body
            self._send_json(self.server.installation.preview(draft))
        elif path in self.server.post_routes:
            self._send_json(self.server.post_routes[path])
        else:
            self._send_json({"ok": True})


class DocsPreviewServer(ThreadingHTTPServer):
    """The preview, which removes its demo installation when it closes."""

    def server_close(self):
        super().server_close()
        self.installation.remove()


def start_server(host, port):
    get_routes, post_routes, releases = build_routes()
    server = DocsPreviewServer((host, port), DocsPreviewHandler)
    server.get_routes = get_routes
    server.post_routes = post_routes
    server.releases = releases
    server.scenarios = build_scenarios()
    server.classic_setup_alignment = _load_fixture("guided_setup_demo.json")["system_alignment_status"]
    server.installation = DemoInstallation()
    server.discovery = DemoDiscovery(_load_fixture("common.json")["mdns_devices"]["devices"])
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    server = start_server(args.host, args.port)
    print(f"Admin docs preview on http://{args.host}:{args.port}/#/overview")
    print(f"Scenarios (?scenario=): {', '.join(SCENARIOS)}")
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    sys.exit(main())
