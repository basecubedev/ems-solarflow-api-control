# SPDX-License-Identifier: AGPL-3.0-or-later
"""Static admin frontend smoke checks."""

import json
import os
import shutil
import subprocess

import pytest

pytestmark = pytest.mark.simulation

STATIC_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "admin", "static"
)


def _read(name):
    with open(os.path.join(STATIC_DIR, name), encoding="utf-8") as handle:
        return handle.read()


def test_index_has_cidr_input():
    html = _read("index.html")
    assert 'id="cidr-input"' in html
    assert 'placeholder="192.168.178.0/24"' in html


def test_index_has_start_scan_button():
    html = _read("index.html")
    assert 'id="scan-button"' in html
    assert "Scan manually" in html


def test_index_references_admin_assets():
    html = _read("index.html")
    assert "/admin.css" in html
    assert "/admin.js" in html


def test_index_marks_active_list_as_planned():
    html = _read("index.html")
    assert "Planned for next phase" in html


def test_js_defines_escape_helper_before_rendering():
    js = _read("admin.js")
    escape_at = js.find("function escapeHtml")
    render_at = js.find("function renderDeviceCard")
    assert escape_at != -1, "escapeHtml helper missing"
    assert render_at != -1, "renderDeviceCard missing"
    assert escape_at < render_at, "escape helper must be defined before rendering"
    assert "escapeHtml(device" in js, "device values must pass through escapeHtml"


def test_css_reuses_dashboard_tokens():
    css = _read("admin.css")
    for token in ("--output", "--accent2", "--muted", "--danger"):
        assert token in css


def test_release_select_stays_within_its_panel():
    css = _read("admin.css")
    field = css.split(".field {", 1)[1].split("}", 1)[0]
    select = css.split(".scan-form select {", 1)[1].split("}", 1)[0]

    assert "min-width: 0" in field
    assert "width: 100%" in select
    assert "max-width: 100%" in select
    assert "box-sizing: border-box" in select
    assert "text-overflow: ellipsis" in select


def test_scanning_empty_state_asks_user_to_wait_for_results():
    js = _read("admin.js")

    assert (
        "Please wait for scanning to finish. Scan results will appear here." in js
    )
    assert "Scanning… devices will appear here." not in js


def test_index_has_network_suggestion_section():
    html = _read("index.html")
    assert 'id="networks-list"' in html
    assert 'id="networks-refresh"' in html
    assert "Detected networks" in html


def test_index_keeps_manual_cidr_input():
    html = _read("index.html")
    assert 'id="cidr-input"' in html
    assert "Manual CIDR" in html


def test_js_fetches_networks_endpoint():
    js = _read("admin.js")
    assert "/api/discovery/networks" in js
    assert "loadNetworks" in js


def test_js_renders_networks_through_escape_helper():
    js = _read("admin.js")
    escape_at = js.find("function escapeHtml")
    render_at = js.find("function renderNetworkRow")
    assert render_at != -1, "renderNetworkRow missing"
    assert escape_at < render_at, "escape helper must be defined before rendering"
    assert "escapeHtml(net" in js, "network values must pass through escapeHtml"


def test_js_network_scan_button_starts_scan():
    js = _read("admin.js")
    assert "network-scan" in js
    assert "triggerScan" in js
    assert "runScans" in js


def test_index_has_no_network_multi_select():
    html = _read("index.html")
    # The compact networks tile lists networks side by side with no selection UI.
    assert 'id="networks-select-all"' not in html
    assert 'id="networks-scan-selected"' not in html
    assert "Scan selected" not in html


def test_js_networks_render_as_compact_chips_without_selection():
    js = _read("admin.js")
    # Each detected network is a compact chip; there is no checkbox / select-all
    # / scan-selected machinery.
    assert "network-chip" in js
    assert "network-select" not in js
    assert "selectedCidrs" not in js
    assert "updateSelectionState" not in js
    # Scanning still runs through the shared runner.
    assert "triggerScan" in js
    assert "runScans" in js


def test_index_has_scan_all_button_and_default_keep_results():
    html = _read("index.html")
    assert 'id="networks-scan-all"' in html
    assert "Scan all" in html
    # Keep previous results is enabled by default.
    assert '<input id="results-accumulate" type="checkbox" checked>' in html


def test_js_scan_all_scans_every_lan_network():
    js = _read("admin.js")
    assert "function lanCidrs" in js
    # The header button scans all detected LAN networks in one run.
    assert "runScans(lanCidrs())" in js
    # Docker networks are excluded from "Scan all".
    fn = js.split("function lanCidrs", 1)[1].split("\nfunction ", 1)[0]
    assert "!net.is_docker_like" in fn


def test_js_auto_scans_all_networks_after_discovery():
    js = _read("admin.js")
    assert "function runInitialScan" in js
    # Entering the Devices step chains discovery into an automatic scan of all
    # LAN networks (once per session).
    assert "loadNetworks().then(runInitialScan)" in js
    fn = js.split("function runInitialScan", 1)[1].split("\nfunction ", 1)[0]
    assert "runScans(cidrs)" in fn
    # It never double-starts while a scan is already running.
    assert "if (scanning ||" in fn
    # The same CIDR is not auto-scanned twice.
    assert "autoScannedCidrs" in fn


def test_js_defers_discovery_to_devices_step():
    js = _read("admin.js")
    assert "function enterDevicesStep" in js
    # Discovery only starts when the Devices step is first opened, once per session.
    fn = js.split("function enterDevicesStep", 1)[1].split("\nfunction ", 1)[0]
    assert "devicesDiscoveryStarted" in fn
    assert "loadNetworks().then(runInitialScan)" in fn
    # The old unconditional startup scan is gone.
    assert "\nloadNetworks().then(runInitialScan);" not in js


def test_scan_buttons_show_busy_state_during_scan():
    js = _read("admin.js")
    css = _read("admin.css")
    # Both the manual and "Scan all" buttons flag a visible busy state.
    assert js.count('classList.toggle("is-scanning", scanning)') >= 2
    assert '"Scanning…"' in js
    # The busy state is backed by a spinner animation.
    assert ".primary-button.is-scanning" in css
    assert "@keyframes admin-spin" in css


def test_js_lan_chips_have_no_button_but_docker_chips_do():
    js = _read("admin.js")
    # LAN chips are info-only; Docker chips keep an opt-in per-chip Scan button.
    assert "renderNetworkRow(net, false)" in js
    assert "renderNetworkRow(net, true)" in js
    # The per-chip Scan button is gated on the withScanButton flag.
    assert "withScanButton" in js


def test_index_has_no_manual_gateway_button():
    html = _read("index.html")
    # The gateway probe now runs automatically; no manual button/section exists.
    assert 'id="gateway-probe-button"' not in html
    assert "Find common gateway networks" not in html
    assert "gateway-noresponse" not in html
    assert 'id="gateway-list"' not in html
    # A status line still explains the automatic probe.
    assert 'id="gateway-probe-status"' in html


def test_index_has_docker_advanced_section():
    html = _read("index.html")
    assert 'id="networks-docker-details"' in html
    assert "Advanced: Docker/container networks" in html


def test_js_gateway_probe_runs_automatically_with_networks():
    js = _read("admin.js")
    assert "/api/discovery/gateway-probe" in js
    # Gateway probing is invoked from the automatic network load, not a button.
    assert "loadGatewayNetworks" in js
    assert "await loadGatewayNetworks()" in js
    # Reachable gateway candidates become normal selectable network entries.
    assert "normalizeGatewayCandidate" in js
    assert "gatewayNetworks" in js
    assert "renderNetworkList" in js


def test_js_gateway_values_pass_through_escape_helper():
    js = _read("admin.js")
    escape_at = js.find("function escapeHtml")
    render_at = js.find("function renderNetworkRow")
    assert render_at != -1, "renderNetworkRow missing"
    assert escape_at < render_at, "escape helper must be defined before rendering"
    # Gateway rows render through renderNetworkRow, which escapes the gateway IP.
    assert "escapeHtml(net.gateway_candidate)" in js


def test_js_splits_docker_networks_into_advanced():
    js = _read("admin.js")
    assert "is_docker_like" in js
    assert "networksDockerList" in js


def test_index_has_keep_results_toggle():
    html = _read("index.html")
    assert 'id="results-accumulate"' in html
    assert "Keep previous results" in html


def test_js_keeps_devices_when_accumulate_enabled():
    js = _read("admin.js")
    assert "keptDevices" in js
    assert "commitDevices" in js
    # Fresh results only when the toggle is off.
    assert "keptDevices.clear()" in js


def test_index_has_global_mdns_discovery_control():
    html = _read("index.html")
    assert 'id="mdns-state"' in html
    assert 'id="mdns-message"' in html
    assert 'id="mdns-count"' in html
    assert 'id="mdns-toggle"' in html
    assert 'id="mdns-refresh"' in html
    assert "Automatic mDNS discovery" in html
    assert "Automatic Zendure discovery" not in html
    assert "Refresh mDNS now" in html
    assert "_zendure._tcp" not in html
    assert "_http._tcp" not in html
    assert "Last event" not in html


def test_js_polls_mdns_status_endpoint():
    js = _read("admin.js")
    assert "/api/discovery/mdns/status" in js
    assert "/api/discovery/devices" in js
    assert "pollMdns" in js
    assert "setInterval(pollMdns" in js


def test_js_mdns_devices_merge_and_survive_manual_scan_clear():
    js = _read("admin.js")
    # mDNS devices are their own map, merged in aggregation.
    assert "mdnsDevices" in js
    assert "for (const device of mdnsDevices.values()) mergeDevice(seen, device)" in js
    # runScans clears kept devices but never the mDNS map.
    assert "mdnsDevices.clear()" not in js.split("async function runScans")[1].split(
        "async function"
    )[0]


def test_js_renders_source_badges_through_escape_helper():
    js = _read("admin.js")
    assert "sourceBadges" in js
    assert "source-mdns" in js
    # Source labels are escaped before insertion into the DOM.
    assert "escapeHtml(label)" in js


def test_js_mdns_status_values_pass_through_escape_helper():
    js = _read("admin.js")
    # Untrusted mDNS status is written via textContent (not innerHTML).
    assert "els.mdnsMessage.textContent" in js
    assert "els.mdnsState.textContent" in js


def test_js_mdns_toggle_uses_only_enable_disable_endpoints():
    js = _read("admin.js")
    assert "/api/discovery/mdns/enable" in js
    assert "/api/discovery/mdns/disable" in js
    assert "_zendure._tcp" not in js
    assert "_http._tcp" not in js
    toggle = js.split("async function toggleMdns", 1)[1]
    assert "results-accumulate" not in toggle


def test_ignored_devices_are_collapsed_and_rendered_safely():
    html = _read("index.html")
    js = _read("admin.js")
    assert 'id="ignored-devices"' in html
    assert '<details id="ignored-devices"' in html
    assert "<details id=\"ignored-devices\" class=\"ignored-devices\" hidden>" in html
    assert "Ignored devices (" in js
    assert "escapeHtml(device.reason" in js
    assert "device.last_verify_attempt" in js


def test_js_can_actively_refresh_mdns():
    js = _read("admin.js")
    assert "/api/discovery/mdns/refresh" in js
    assert "Refreshing mDNS discovery…" in js


def test_ui_has_separate_mqtt_broker_candidates_section():
    html = _read("index.html")
    js = _read("admin.js")
    assert "MQTT broker candidates" in html
    assert 'id="mqtt-list"' in html
    assert 'id="mqtt-refresh"' in html
    assert 'id="mqtt-probe"' not in html
    assert "checked automatically when you scan a" in html
    assert "/api/discovery/mqtt-brokers" in js
    assert "probeMqttNetworks(unique)" in js
    assert "escapeHtml(broker" in js


def _setup_panel(html):
    return html.split('id="view-setup"', 1)[1].split('id="view-advanced"', 1)[0]


def _config_section(html):
    return _setup_panel(html).split('aria-label="Config"', 1)[1]


def _devices_section(html):
    return _setup_panel(html).split('aria-label="Devices"', 1)[1].split(
        'aria-label="Config"', 1
    )[0]


def _advanced_panel(html):
    return html.split('id="view-advanced"', 1)[1]


def test_index_has_no_top_level_tab_bar():
    html = _read("index.html")
    # The start gate's two choices are the only router; the redundant top-level
    # tab strip (Setup / Maintenance / Diagnostics) has been removed.
    assert '<nav class="admin-view-tabs"' not in html
    assert 'role="tablist" aria-label="Admin sections"' not in html
    # The old five-tab layout is gone: Discovery and Config are not primary tabs.
    assert 'data-admin-view="discovery"' not in html
    assert 'data-admin-view="config"' not in html


def test_start_gate_is_the_default_screen():
    html = _read("index.html")
    # The router/start screen is shown first; the workspace panels stay hidden
    # until the user chooses a path so the setup wizard never auto-runs.
    assert 'id="view-start"' in html
    assert (
        '<div class="admin-view" id="view-setup" data-admin-view-panel="setup" hidden>'
        in html
    )
    assert 'data-admin-view-panel="maintenance" hidden' in html
    assert 'data-admin-view-panel="advanced" hidden' in html


def test_start_gate_has_exactly_two_choices():
    html = _read("index.html")
    assert html.count('name="start-path"') == 2
    assert 'value="setup_new"' in html
    assert 'value="manage_existing"' in html
    assert "Guided setup" in html
    assert "Maintenance" in html
    # Guided setup is the primary, first option.
    assert html.index('value="setup_new"') < html.index('value="manage_existing"')
    assert html.index("Guided setup") < html.index(
        '<span class="start-choice-title">Maintenance</span>'
    )
    # Docker bootstrap / developer setup stay documentation-only paths.
    assert "Docker bootstrap" not in html
    assert "Developer setup" not in html


def test_setup_tab_has_release_devices_config_stages():
    html = _read("index.html")
    setup = _setup_panel(html)
    for label in ("Release", "Devices", "Config"):
        assert 'aria-label="' + label + '"' in setup
    # Numbered stages establish the 01/02/03 setup flow.
    for step in ("01", "02", "03"):
        assert ">" + step + "<" in setup


def test_setup_release_stage_uses_downloaded_release_resources():
    html = _read("index.html")
    setup = _setup_panel(html)
    assert "Selected release" in setup
    assert "Release resource download not implemented yet" not in setup
    assert "Config template" in setup
    assert "Docker installers" in setup


def test_setup_devices_stage_shows_compact_summary_counts():
    html = _read("index.html")
    devices = _devices_section(html)
    for marker in (
        'id="setup-summary-devices"',
        'id="setup-summary-networks"',
        'id="setup-summary-mdns"',
        'id="setup-summary-mqtt"',
    ):
        assert marker in devices


def test_setup_devices_stage_owns_discovery_controls():
    html = _read("index.html")
    devices = _devices_section(html)
    for marker in (
        'id="cidr-input"',
        'id="networks-list"',
        'id="networks-refresh"',
        'id="mdns-refresh"',
        'id="ignored-devices"',
        'id="mqtt-list"',
    ):
        assert marker in devices


def test_setup_manual_scan_is_collapsed_advanced():
    html = _read("index.html")
    devices = _devices_section(html)
    # The manual CIDR scan remains reachable but lives in a collapsed details block.
    manual = devices.split('id="manual-scan-details"', 1)
    assert len(manual) == 2, "manual scan details block missing"
    assert manual[0].rstrip().endswith("<details class=\"advanced-details\"")
    assert 'id="cidr-input"' in manual[1]
    assert "Advanced manual scan" in devices


def test_advanced_panel_preserves_deployment_system_network():
    html = _read("index.html")
    advanced = _advanced_panel(html)
    assert "Deployment" in advanced
    assert "bootstrap" in advanced
    assert "System" in advanced
    assert "diagnostics" in advanced and "support" in advanced and "logs" in advanced
    assert "Planned for next phase" in advanced


def test_advanced_panel_states_wifi_unavailable_in_docker():
    html = _read("index.html")
    network = _advanced_panel(html).split('id="advanced-network"', 1)[1]
    assert "WiFi configuration is not available in local Docker mode" in network
    assert "Raspberry Pi appliance mode" in network


def test_config_stage_shows_draft_and_preview():
    html = _read("index.html")
    config = _config_section(html)
    # The Config stage owns the auto-selected draft, an add-more list, and a preview.
    assert 'id="config-draft-list"' in config
    assert 'id="config-available-list"' in config
    assert 'id="config-preview"' in config


def test_config_export_is_labelled_as_not_deployment():
    html = _read("index.html")
    config = _config_section(html)
    assert "no deployment" in config


def test_config_stage_offers_apply_to_ems_with_target_and_status():
    html = _read("index.html")
    config = _config_section(html)
    assert 'id="config-apply"' in config
    assert "Apply to EMS installation" in config
    assert 'id="config-apply-target"' in config
    assert "./config/config.json" in config
    assert 'id="config-apply-status"' in config


def test_config_apply_js_posts_to_apply_endpoint_and_reports_result():
    js = _read("admin.js")
    assert "/api/setup/config/apply" in js
    assert "applyGeneratedConfig" in js
    assert "backup_path" in js


def test_setup_has_deployment_step_panel_and_status():
    html = _read("index.html")
    setup = _setup_panel(html)
    # Stepper gains a fourth Deployment step.
    assert 'data-setup-step="deployment"' in setup
    assert ">04<" in setup
    assert 'id="step-status-deployment"' in setup
    # The Deployment panel shows the saved generated config path/status.
    assert 'data-setup-step-panel="deployment" hidden' in setup
    deployment = setup.split('data-setup-step-panel="deployment"', 1)[1]
    assert 'id="deployment-config-state"' in deployment
    assert 'id="deployment-config-path"' in deployment
    assert "/data/generated/config.json" in deployment


def _deployment_section(html):
    return _setup_panel(html).split(
        'data-setup-step-panel="deployment"', 1
    )[1].split('data-setup-step-panel="start"', 1)[0]


def _start_section(html):
    return _setup_panel(html).split(
        'data-setup-step-panel="start"', 1
    )[1].split("</div>\n\n      <div class=\"setup-nav\"", 1)[0]


def test_step_04_is_renamed_prepare_deployment():
    html = _read("index.html")
    setup = _setup_panel(html)
    # The stepper label and the panel heading both say "Prepare deployment".
    assert '<span class="setup-step-label">Prepare deployment</span>' in setup
    deployment = _deployment_section(html)
    assert "Prepare deployment" in deployment
    assert "EMS is not started yet" in deployment


def test_step_04_shows_plan_summary_and_images():
    html = _read("index.html")
    deployment = _deployment_section(html)
    for marker in (
        'id="deployment-workspace"',
        'id="deployment-bootstrap-source"',
        'id="deployment-images"',
        'id="deployment-images-empty"',
        'id="deployment-progress"',
        'id="deployment-ready-summary"',
    ):
        assert marker in deployment
    assert "Images to download" in deployment
    retry_button = deployment.split('id="deployment-prepare"', 1)[1].split(
        "</button>", 1
    )[0]
    assert "Retry preparation" in retry_button
    assert "hidden" in retry_button.split(">", 1)[0]
    assert "Prepared</span>" not in deployment


def test_step_04_requires_confirmation_to_replace_existing_install():
    html = _read("index.html")
    deployment = _deployment_section(html)
    assert 'id="deployment-existing-install"' in deployment
    assert "Existing EMS installation detected" in deployment
    assert "will replace config/config.json and docker-compose.yml" in deployment
    assert "backup" in deployment.lower()
    assert "data/ will not be deleted" in deployment
    block = deployment.split('id="deployment-existing-install"', 1)[1]
    assert "hidden" in block.split(">", 1)[0]
    assert 'id="deployment-existing-replace"' in block


def test_js_existing_install_conflict_requires_explicit_confirmation():
    js = _read("admin.js")
    # The existing-install conflict is handled distinctly and never auto-retried.
    assert 'data.reason === "existing_install_conflict"' in js
    fn = js.split("async function prepareDeployment", 1)[1].split(
        "\nfunction ", 1
    )[0]
    assert "existing_conflict = data" in fn
    # Auto-prepare must stop when an existing-install conflict is pending.
    auto = js.split("function autoPrepareDeploymentIfNeeded", 1)[1].split(
        "\nasync function ", 1
    )[0]
    assert "dep.existing_conflict" in auto
    # The explicit replace button retries with overwrite=true.
    assert "deploymentExistingReplace" in js
    assert "prepareDeployment(true)" in js


def test_js_deployment_plan_is_read_only_and_images_from_server():
    js = _read("admin.js")
    # The plan endpoint is read-only; the frontend never invents image names.
    assert "/api/setup/deployment/plan" in js
    fn = js.split("async function loadDeploymentPlan", 1)[1].split(
        "\nasync function ", 1
    )[0]
    assert "data.images" in fn
    # Images are rendered from server plan state, not a hardcoded list.
    render = js.split("function renderDeploymentImages", 1)[1].split(
        "\nfunction ", 1
    )[0]
    assert "setupState.deployment.images" in render
    assert "influxdb:" not in render
    assert "ghcr.io" not in render


def test_js_deployment_prepare_starts_job_and_polls_progress():
    js = _read("admin.js")
    assert "/api/setup/deployment/prepare" in js
    prepare = js.split("async function prepareDeployment", 1)[1].split(
        "\nfunction ", 1
    )[0]
    assert "overwrite" in prepare
    assert "pollDeploymentJob" in prepare
    poll = js.split("function pollDeploymentJob", 1)[1].split("\n\n", 1)[0]
    assert "/api/setup/deployment/jobs/" in poll
    # No container-start command is ever issued from the frontend.
    assert "compose up" not in js
    assert "stack up" not in js


def test_js_deployment_auto_prepares_once_and_gates_next_on_ready():
    js = _read("admin.js")
    auto = js.split("function autoPrepareDeploymentIfNeeded", 1)[1].split(
        "\nasync function ", 1
    )[0]
    assert "auto_prepare_attempted" in auto
    assert "deploymentReady()" in auto
    assert "dep.status === \"failed\"" in auto
    assert "dep.conflict" in auto
    assert "prepareDeployment(false)" in auto
    plan = js.split("async function loadDeploymentPlan", 1)[1].split(
        "\nfunction ", 1
    )[0]
    assert "autoPrepareDeploymentIfNeeded()" in plan
    nav = js.split("function renderSetupNav", 1)[1].split("\nfunction ", 1)[0]
    assert "deploymentReady()" in nav


def test_step_04_has_docker_access_card():
    html = _read("index.html")
    deployment = _deployment_section(html)
    for marker in (
        'id="deployment-docker"',
        'id="deployment-docker-state"',
        'id="deployment-docker-mode"',
        'id="deployment-docker-note"',
        'id="deployment-docker-recheck"',
    ):
        assert marker in deployment
    assert "Docker access" in deployment
    # User-friendly framing, never Docker-in-Docker.
    assert "run as normal containers next to this Admin container" in deployment
    assert "Docker-in-Docker" not in html
    # A clear security note: mounting the socket grants host Docker control.
    collapsed = " ".join(deployment.split())
    assert "Only run it on a trusted local machine" in collapsed
    assert "do not expose the Admin UI to the internet" in collapsed


def test_js_deployment_reflects_docker_status_from_plan():
    js = _read("admin.js")
    plan = js.split("async function loadDeploymentPlan", 1)[1].split(
        "\nasync function ", 1
    )[0]
    # Docker access state is derived server-side and stored from the plan.
    assert "data.docker" in plan
    render = js.split("function renderDeploymentDocker", 1)[1].split(
        "\nfunction ", 1
    )[0]
    assert "textContent" in render
    assert "innerHTML" not in render
    # Distinct discovery-only vs missing-client wording exists in the mapping.
    assert "socket_missing" in js
    assert "client_missing" in js
    # Prepare is blocked when Docker is not ready.
    controls = js.split("function renderDeploymentControls", 1)[1].split(
        "\nfunction ", 1
    )[0]
    assert "dockerReady()" in controls


def test_js_deployment_conflict_offers_overwrite():
    js = _read("admin.js")
    prepare = js.split("async function prepareDeployment", 1)[1].split(
        "\nfunction ", 1
    )[0]
    assert "workspace_conflict" in prepare
    assert "prepareDeployment(true)" in js


def test_js_deployment_values_use_safe_dom_text():
    js = _read("admin.js")
    images = js.split("function renderDeploymentImages", 1)[1].split(
        "\nfunction ", 1
    )[0]
    # Image refs and step labels are written via textContent, never innerHTML.
    assert "textContent" in images
    assert "innerHTML" not in images
    steps = js.split("function renderDeploymentSteps", 1)[1].split(
        "\nfunction ", 1
    )[0]
    assert "textContent" in steps
    assert "innerHTML" not in steps


def test_js_deployment_reads_generated_config_status():
    js = _read("admin.js")
    assert "SETUP_STEPS = [" in js
    steps = js.split("SETUP_STEPS = [", 1)[1].split("]", 1)[0]
    assert '"deployment"' in steps
    # Deployment status comes from the generated-config status endpoint.
    assert "/api/setup/config/status" in js
    fn = js.split("async function refreshDeploymentStatus", 1)[1].split(
        "\nfunction ", 1
    )[0]
    assert "data.exists" in fn
    # Deployment stays locked until a generated config has been saved.
    locked = js.split("function stepLocked", 1)[1].split("\nfunction ", 1)[0]
    assert "generated_ready" in locked


def test_setup_has_blocked_step_05_start_ems_panel():
    html = _read("index.html")
    setup = _setup_panel(html)
    start = _start_section(html)

    assert 'data-setup-step="start"' in setup
    assert ">05<" in setup
    assert '<span class="setup-step-label">Start EMS</span>' in setup
    assert 'id="step-status-start">Locked</span>' in setup
    assert 'data-setup-step-panel="start" hidden' in setup
    assert "Prepare deployment first before starting EMS." in start
    for marker in (
        'id="start-workspace"',
        'id="start-prepared"',
        'id="start-release"',
        'id="start-docker"',
        'id="start-services"',
        'id="start-progress"',
        'id="start-button"',
        'id="start-recheck"',
    ):
        assert marker in start


def test_js_step_05_unlocks_only_after_prepare_completes():
    js = _read("admin.js")
    steps = js.split("SETUP_STEPS = [", 1)[1].split("]", 1)[0]
    assert '"start"' in steps
    locked = js.split("function stepLocked", 1)[1].split("\nfunction ", 1)[0]
    assert 'step === "start"' in locked
    assert "!deploymentReady()" in locked
    render = js.split("function renderStepper", 1)[1].split("\nfunction ", 1)[0]
    assert "setupEls.stepStatus.start" in render


def test_js_step_05_starts_and_polls_backend_job():
    js = _read("admin.js")
    start = js.split("async function startDeployment", 1)[1].split(
        "\nfunction ", 1
    )[0]
    assert "/api/setup/deployment/start" in start
    assert "pollStartJob" in start
    poll = js.split("function pollStartJob", 1)[1].split("\nfunction ", 1)[0]
    assert "/api/setup/deployment/start/jobs/" in poll
    assert "refreshStartStatus()" in poll
    assert 'startButton.addEventListener("click", startDeployment)' in js
    assert 'startRecheck.addEventListener("click", refreshStartStatus)' in js


def test_step_05_renders_success_dashboard_link_and_failures():
    html = _read("index.html")
    js = _read("admin.js")
    start = _start_section(html)

    assert 'id="start-success"' in start
    assert 'id="start-dashboard-link"' in start
    assert "Open EMS Dashboard" in start
    assert 'id="start-error"' in start
    success = js.split("function renderStartSuccess", 1)[1].split(
        "\nfunction ", 1
    )[0]
    assert "start.running" in success
    assert "startDashboardHref()" in success
    apply = js.split("function applyStartJob", 1)[1].split("\nfunction ", 1)[0]
    assert 'job.status === "failed"' in apply
    assert "job.error.message" in apply
    assert "job.error.detail" in apply


def test_step_05_renders_workspace_permission_repair_action():
    html = _read("index.html")
    js = _read("admin.js")
    start = _start_section(html)

    assert 'id="start-permission-error"' in start
    assert "Deployment workspace is not writable by EMS" in start
    assert "Repair permissions and continue" in start
    assert "/api/setup/deployment/repair-permissions" in js
    repair = js.split("async function repairWorkspacePermissions", 1)[1].split(
        "\nasync function ", 1
    )[0]
    assert "workspace_permission_denied" in repair
    assert "await startDeployment()" in repair


def test_js_step_05_status_refresh_preserves_failed_job():
    js = _read("admin.js")
    refresh = js.split("async function refreshStartStatus", 1)[1].split(
        "\nasync function ", 1
    )[0]
    assert 'start.status !== "failed"' in refresh
    assert "if (start.running)" in refresh
    assert "if (data.conflict)" in refresh


def test_step_05_renders_container_conflict_action_and_safe_running_behavior():
    html = _read("index.html")
    js = _read("admin.js")
    start = _start_section(html)

    assert 'id="start-container-conflict"' in start
    assert "Existing EMS container found" in start
    assert 'id="start-conflict-resolve"' in start
    render = js.split("function renderContainerConflict", 1)[1].split(
        "\nfunction ", 1
    )[0]
    assert "safe_fix_available" in render
    assert "replace_available" in render
    assert "EMS is running with a different image" in render
    assert "Replace running EMS and continue" in render
    assert "startConflictResolve.hidden = !safe && !replace" in render
    resolve = js.split("async function resolveContainerConflict", 1)[1].split(
        "\nfunction ", 1
    )[0]
    assert "/api/setup/deployment/resolve-container-conflict" in resolve
    assert "remove_stopped_and_continue" in resolve
    assert "replace_running_and_continue" in resolve
    assert "await startDeployment()" in resolve

    success = js.split("function renderStartSuccess", 1)[1].split(
        "\nfunction ", 1
    )[0]
    assert "start.running && !start.conflict" in success


def test_step_05_conflict_keeps_raw_detail_and_blocks_start():
    html = _read("index.html")
    js = _read("admin.js")
    start = _start_section(html)

    assert 'id="start-error-detail"' in start
    controls = js.split("function renderStartControls", 1)[1].split(
        "\nfunction ", 1
    )[0]
    assert "Boolean(start.conflict)" in controls
    apply = js.split("function applyStartJob", 1)[1].split(
        "\nfunction ", 1
    )[0]
    assert "job.error.detail" in apply
    assert "job.conflict" in apply


def test_js_step_05_service_values_use_safe_dom_text():
    js = _read("admin.js")
    render = js.split("function renderStartServices", 1)[1].split(
        "\nfunction ", 1
    )[0]
    assert "document.createElement" in render
    assert "textContent" in render
    assert "innerHTML" not in render


def test_admin_header_copy_describes_docker_deployment():
    html = _read("index.html")
    header = html.split('<header class="admin-header">', 1)[1].split(
        "</header>", 1
    )[0]
    assert "deploy EMS with Docker" in header
    assert "Read-only" not in header


def test_config_preview_uses_backend_generation_and_compact_summary():
    html = _read("index.html")
    js = _read("admin.js")
    config = _config_section(html)
    assert 'id="config-preview-ready"' in config
    assert 'id="config-preview-devices"' in config
    assert 'id="config-preview-release"' in config
    assert "Advanced: generated config.json preview" in config
    assert 'fetch("/api/setup/config-preview"' in js
    assert "supported_grid_meter_count" in js


def test_config_validation_is_a_distinct_status_card():
    html = _read("index.html")
    js = _read("admin.js")
    css = _read("admin.css")
    config = _config_section(html)

    assert 'id="config-validation-card"' in config
    assert 'class="config-validation-card"' in config
    assert "<h3>Config validation</h3>" in config
    assert 'id="config-validation-state"' not in config
    assert 'id="config-preview-ready" class="config-validation-state"' in config
    assert "config-validation-item" in js
    assert "config-validation-icon" in js
    assert 'validationCard.dataset.tone' in js
    assert ".config-validation-card[data-tone=\"ready\"]" in css
    assert config.index('id="config-validation-card"') < config.index(
        'id="config-available-details"'
    )


def test_config_inverters_use_shared_hardware_cards():
    js = _read("admin.js")
    row = js.split("function renderInverterDraftRow", 1)[1].split(
        "\nfunction ", 1
    )[0]

    assert "renderHardwareCard({" in row
    assert 'kind: "inverter"' in row
    assert 'removeClass: "config-draft-remove"' in row
    assert "inverterModelText(item)" in row
    assert "data-inverter-toggle" in row
    assert '"Inverter " + (index + 1)' in row
    # Enabled is edited in the body, not through a leading header checkbox.
    assert "data-inverter-enable" not in row
    assert "feature-enable" not in row
    assert "function renderConfigDraftCard" not in js
    assert "config-draft-card" not in js
    assert "config-draft-fields" not in js


def test_config_inverter_rows_use_device_catalog_fields():
    js = _read("admin.js")
    section = js.split("function deviceCatalogSection", 1)[1].split(
        "\nfunction ", 1
    )[0]
    assert 'section.id === "devices"' in section
    fields = js.split("function renderInverterFields", 1)[1].split(
        "\nfunction ", 1
    )[0]
    assert "deviceCatalogFields()" in fields
    # Advanced/expert device fields stay in nested collapsed areas.
    assert "<summary>Advanced settings</summary>" in fields
    assert "Developer / expert settings" in fields
    # Each device field renders as a compact settings row, not a stacked tile.
    field = js.split("function renderDeviceField", 1)[1].split("\nfunction ", 1)[0]
    assert 'class="feature-field-row"' in field
    assert 'class="feature-field-label"' in field
    assert 'class="feature-field-control"' in field


def test_config_inverter_maps_identity_fields_and_stores_overrides():
    js = _read("admin.js")
    mapped = js.split("DEVICE_MAPPED_FIELD_KEYS = {", 1)[1].split("}", 1)[0]
    assert 'name: "config_name"' in mapped
    assert 'ip: "ip"' in mapped
    assert 'sn: "serial_number"' in mapped
    update = js.split("function updateDraftDeviceField", 1)[1].split(
        "\nfunction ", 1
    )[0]
    # Non-identity device values are stored as per-device overrides.
    assert "item.config_values" in update


def test_config_inverter_summary_shows_key_facts():
    js = _read("admin.js")
    fn = js.split("function inverterSummaryText", 1)[1].split("\nfunction ", 1)[0]
    assert "item.config_name" in fn
    assert "item.ip" in fn
    assert "Serial missing" in fn
    # The per-device output limit is surfaced in the collapsed summary.
    assert '"devices[].max_power"' in fn
    assert '" W"' in fn


def test_config_inverter_list_excludes_grid_meter_items():
    js = _read("admin.js")
    fn = js.split("function renderInverterList", 1)[1].split("\nfunction ", 1)[0]
    # Only inverters render as rows; the grid meter is a separate hardware concept.
    assert "inverterItems()" in fn
    draft = js.split("function renderConfigDraft(", 1)[1].split(
        "\nfunction ", 1
    )[0]
    assert "renderInverterList()" in draft
    # A selected grid meter keeps a compact change/remove path in its own area.
    selected = js.split("function renderSelectedGridMeter", 1)[1].split(
        "\nfunction ", 1
    )[0]
    assert "config-grid-remove" in selected
    assert "config-auto-badge" in selected


def test_config_advanced_rows_use_secondary_caret_style():
    html = _read("index.html")
    css = _read("admin.css")
    config = _config_section(html)
    for details_id in (
        "config-available-details",
        "config-preview-details",
        "config-template-details",
    ):
        assert f'class="advanced-details" id="{details_id}"' in config
        assert f'id="{details_id}" open' not in config
    assert ".advanced-details > summary::marker" in css
    assert '.advanced-details[open] > summary::marker' in css


def test_config_step_has_no_prominent_download_or_save_actions():
    html = _read("index.html")
    config = _config_section(html)

    # The main flow no longer offers manual save/download; the wizard saves on
    # Continue. No "Save config draft" action and no separate actions section.
    assert 'class="config-export-actions"' not in config
    assert 'id="config-save"' not in config
    assert "Save config draft" not in config
    assert 'id="config-overwrite-confirm"' not in config
    # The debug-only download stays inside the collapsed preview accordion.
    preview = config.split('id="config-preview-details"', 1)[1]
    assert 'id="config-download"' in preview
    assert "Download config.json" in preview
    assert 'id="config-export-status"' in preview


def test_config_continue_saves_generated_config_via_write_endpoint():
    js = _read("admin.js")

    # The debug download still uses the download endpoint.
    assert "/api/setup/config/download" in js
    assert 'link.download = "config.json"' in js
    # Continue rebuilds/validates and writes the generated config (overwrite),
    # then advances to the deployment step.
    fn = js.split("async function continueFromConfig", 1)[1].split(
        "\nfunction ", 1
    )[0]
    assert "/api/setup/config/write" in fn
    assert "configExportBody(true)" in fn
    assert 'setActiveStep("deployment")' in fn


def test_config_invalid_blocks_continue_and_shows_error():
    js = _read("admin.js")

    fn = js.split("async function continueFromConfig", 1)[1].split(
        "\nfunction ", 1
    )[0]
    # Invalid config keeps the user on Config with a visible error and no write.
    assert "!latestConfigPreview || !latestConfigPreview.ready" in fn
    assert "showSetupNavError" in fn
    # The Continue button itself is gated on preview readiness.
    nav = js.split("function renderSetupNav", 1)[1].split("\nfunction ", 1)[0]
    assert "latestConfigPreview && latestConfigPreview.ready" in nav
    assert "Continue to deployment" in nav


def test_config_tab_has_clear_draft_button():
    html = _read("index.html")
    assert 'id="config-clear-draft"' in html
    assert "Clear draft" in html


def test_js_config_available_only_offers_supported_roles():
    js = _read("admin.js")
    assert "isConfigCandidate" in js
    # Only inverter/grid_meter candidates reach the config available list.
    assert 'role === "inverter" || role === "grid_meter"' in js
    assert "availableConfigDevices" in js


def test_js_config_generates_sequential_inverter_names():
    js = _read("admin.js")
    assert "nextInverterName" in js
    assert '"inverter_" + index' in js
    # Grid meter default name.
    assert 'config_name: "grid_meter"' in js


def test_js_config_prevents_duplicate_add():
    js = _read("admin.js")
    assert "draftHasSource" in js
    # The add path bails out when the source id is already in the draft.
    assert "if (draftHasSource(sourceId)) return;" in js
    # Added cards disable their button.
    assert ">Added</button>" in js


def test_js_config_validates_duplicate_and_empty_names():
    js = _read("admin.js")
    assert "configValidationHints" in js
    assert "Duplicate config name" in js
    assert "must be non-empty" in js
    assert "At least one inverter is recommended" in js


def test_js_config_single_grid_meter_replaces_existing():
    js = _read("admin.js")
    # A manual grid-meter pick always replaces any existing one, so the draft
    # can never hold two grid meters.
    assert "function selectGridMeter" in js
    assert 'item.role !== "grid_meter"' in js


def test_js_config_preview_patches_real_template_fields():
    js = _read("admin.js")
    assert "configDraftPreview" in js
    assert "activeConfigTemplate" in js
    assert "cloneConfigValue(activeConfigTemplate)" in js
    assert "device.ip = item.ip" in js
    assert "device.sn = item.serial_number" in js
    assert "gridTemplate.ip = meter.ip" in js
    # Preview is written via textContent so JSON never touches innerHTML.
    assert "configEls.preview.textContent" in js


def test_config_step_loads_and_identifies_active_release_template():
    html = _read("index.html")
    js = _read("admin.js")
    config = _config_section(html)

    assert 'id="config-template-status"' in config
    assert 'id="config-template-details"' in config
    assert 'id="config-template-preview"' in config
    assert "/api/setup/config-template" in js
    assert "Using config template from " in js
    assert "templatePreview.textContent" in js


def test_release_step_shows_template_and_planned_image_state():
    html = _read("index.html")
    js = _read("admin.js")
    release = _setup_panel(html).split('aria-label="Release"', 1)[1].split(
        'aria-label="Devices"', 1
    )[0]

    assert "✓ Release resources prepared" in release
    assert "✓ config.template.json loaded" in release
    assert "✓ Docker install resources found" in release
    assert 'id="release-docker-image"' in release
    assert "data.docker_image" in js


def test_js_config_escapes_dynamic_device_values():
    js = _read("admin.js")
    config = js.split("renderConfigAvailableCard", 1)[1]
    # Serial, source id, role, and endpoint all pass through escapeHtml.
    assert "escapeHtml(device.serial_number)" in config
    assert "escapeHtml(sourceId)" in config
    assert "escapeHtml(device.ip)" in js
    inverter = js.split("function renderInverterDraftRow", 1)[1].split(
        "\nfunction ", 1
    )[0]
    assert "renderHardwareCard({" in inverter
    hardware = js.split("function renderHardwareCard", 1)[1].split(
        "\nfunction ", 1
    )[0]
    assert "escapeHtml(card.sourceId)" in hardware
    assert "escapeHtml(card.model)" in hardware
    assert "escapeHtml(card.meta)" in hardware
    assert "meta: inverterSummaryText(item)" in inverter
    control = js.split("function renderDeviceControl", 1)[1].split(
        "\nfunction ", 1
    )[0]
    assert "escapeHtml(formatFeatureValue(value))" in control


def test_js_scan_confirmed_devices_are_not_stale():
    js = _read("admin.js")
    # A device an active scan reaches is online; the merge clears any leaked
    # mDNS stale marker for kept + current-session hits.
    assert "function mergeDevice(map, device, fresh)" in js
    assert "if (fresh) merged.stale = false" in js
    agg = js.split("function aggregateDevices", 1)[1].split("\nfunction ", 1)[0]
    assert "mergeDevice(seen, device, true)" in agg


def test_index_has_manual_device_entry_under_config():
    html = _read("index.html")
    config = _config_section(html)
    assert 'id="config-manual-form"' in config
    assert 'id="config-manual-host"' in config
    assert 'id="config-manual-role"' in config
    assert "Add a device manually" in config


def test_js_manual_device_adds_to_draft():
    js = _read("admin.js")
    assert "function addManualDevice" in js
    # Manual entries are flagged so auto-config and staleness skip them.
    assert "manual: true" in js
    assert 'discovery_source: "manual"' in js
    fn = js.split("function addManualDevice", 1)[1].split("\nfunction ", 1)[0]
    # A manual grid meter still replaces any existing single grid meter.
    assert 'item.role !== "grid_meter"' in fn
    # Manual meters are never treated as stale (no discovery record).
    stale = js.split("function selectedGridMeterStale", 1)[1].split("\nfunction ", 1)[0]
    assert "meter.manual" in stale


def test_js_config_draft_persists_in_localstorage():
    js = _read("admin.js")
    assert "CONFIG_DRAFT_STORAGE_KEY" in js
    assert "localStorage.setItem" in js
    assert "localStorage.getItem" in js
    # Clear draft removes the stored draft.
    assert "localStorage.removeItem" in js


def test_js_config_available_refreshes_with_discovery():
    js = _read("admin.js")
    # The same aggregated device data feeds the Config tab and drives auto-config.
    assert "renderConfigAvailable" in js
    aggregate = js.split("function renderAggregate", 1)[1].split("function", 2)[0]
    assert "syncConfigFromDiscovery()" in aggregate


def test_js_config_renders_on_setup_switch():
    js = _read("admin.js")
    switch = js.split("function setAdminView", 1)[1].split("function", 1)[0]
    assert 'next === "setup"' in switch
    # Switching to the Setup tab refreshes available devices, re-runs
    # auto-config, and redraws the draft in one call.
    assert "syncConfigFromDiscovery()" in switch


def test_js_config_auto_adds_verified_inverters():
    js = _read("admin.js")
    assert "function autoAddInverters" in js
    assert "function isAutoConfigReady" in js
    # Auto-add skips devices already in the draft or removed by the user.
    assert "draftHasSource(sourceId) || configDismissed.has(sourceId)" in js
    # Sequential inverter naming is reused for auto-added inverters.
    assert 'draftItemFromDevice(device, "inverter")' in js


def test_js_config_zero_grid_meters_not_auto_selected():
    js = _read("admin.js")
    fn = js.split("function autoSelectGridMeter", 1)[1].split("\nfunction ", 1)[0]
    # Only exactly one supported meter is auto-selected; 0 (or 2+) is skipped.
    assert "meters.length !== 1" in fn
    assert "return false" in fn


def test_js_config_one_grid_meter_auto_selected_with_badge():
    js = _read("admin.js")
    assert "function autoSelectGridMeter" in js
    assert "item.auto_selected = true" in js
    # The auto-selected meter carries an "Auto-selected" badge in its draft card.
    assert "Auto-selected" in js
    assert "config-auto-badge" in js


def test_js_config_two_grid_meters_show_selection_needed():
    js = _read("admin.js")
    assert "function renderGridMeterSelection" in js
    assert "Grid meter selection needed" in js
    assert "config-grid-use" in js
    assert ">Use this<" in js
    # Zero supported meters surfaces a compact hint instead of a picker.
    assert "No supported grid meter found yet" in js


def test_js_config_auto_select_respects_existing_selection():
    js = _read("admin.js")
    fn = js.split("function autoSelectGridMeter", 1)[1].split("\nfunction ", 1)[0]
    # A grid meter already in the draft (auto or manual) is never replaced by
    # a later rediscovery.
    assert "if (gridMeterItem()) return false;" in fn


def test_js_config_grid_meter_use_action_selects_manually():
    js = _read("admin.js")
    assert "config-grid-use" in js
    assert "selectGridMeter(use.getAttribute" in js
    # A manual pick clears the auto flag and replaces any existing meter.
    assert "item.auto_selected = false" in js


def test_js_config_supported_grid_meters_exclude_unverified():
    js = _read("admin.js")
    fn = js.split("function supportedGridMeters", 1)[1].split("\nfunction ", 1)[0]
    # Only verified, config-ready grid meters are selectable; unsupported Shelly
    # devices are unverified and never reach this list.
    assert 'String(device.role_suggestion) === "grid_meter"' in fn
    assert "isAutoConfigReady(device)" in fn
    ready = js.split("function isAutoConfigReady", 1)[1].split("\nfunction ", 1)[0]
    assert "device.verified !== false" in ready


def test_js_config_grid_meter_validation_hint_codes():
    js = _read("admin.js")
    for code in (
        "missing_grid_meter",
        "grid_meter_selection_needed",
        "selected_grid_meter_stale",
        "duplicate_grid_meter",
    ):
        assert code in js


def test_js_config_stale_selected_grid_meter_kept_with_hint():
    js = _read("admin.js")
    assert "function selectedGridMeterStale" in js
    # Staleness never removes the meter; it only drives a badge/hint.
    fn = js.split("function selectedGridMeterStale", 1)[1].split("\nfunction ", 1)[0]
    assert "return !device || Boolean(device.stale)" in fn


def test_js_config_manual_removal_is_not_auto_readded():
    js = _read("admin.js")
    # Removal and clear-draft record dismissed source ids that auto-config skips.
    assert "configDismissed" in js
    assert "CONFIG_DISMISSED_STORAGE_KEY" in js
    assert "configDismissed.add(sourceId)" in js


def test_css_hides_inactive_admin_view_panels():
    css = _read("admin.css")
    # The hidden attribute must beat the .admin-view flex layout, otherwise
    # every tab panel renders simultaneously and the tabs look non-functional.
    assert ".admin-view[hidden]" in css
    after = css.split(".admin-view[hidden]", 1)[1][:40]
    assert "display: none" in after


def test_js_tab_switching_supports_hash_navigation():
    js = _read("admin.js")
    assert "setAdminView" in js
    assert "data-admin-view" in js
    assert "data-admin-view-panel" in js
    assert 'window.addEventListener("hashchange"' in js
    # Setup is the fallback when the hash is empty or unknown.
    assert 'ADMIN_VIEWS.includes(view) ? view : "setup"' in js


# --- setup wizard --------------------------------------------------------


def test_setup_has_wizard_stepper():
    html = _read("index.html")
    setup = _setup_panel(html)
    assert 'class="setup-stepper"' in setup
    for step in ("release", "devices", "config"):
        assert 'data-setup-step="' + step + '"' in setup
    # Compact per-step status lives in the stepper header, not as full cards.
    for marker in (
        'id="step-status-release"',
        'id="step-status-devices"',
        'id="step-status-config"',
    ):
        assert marker in setup


def test_setup_steps_are_separate_panels_hidden_by_default():
    html = _read("index.html")
    setup = _setup_panel(html)
    for step in ("release", "devices", "config"):
        assert 'data-setup-step-panel="' + step + '"' in setup
    # Only the release panel is visible on load; devices/config start hidden.
    assert 'data-setup-step-panel="devices" hidden' in setup
    assert 'data-setup-step-panel="config" hidden' in setup
    assert 'data-setup-step-panel="release" hidden' not in setup


def test_css_hides_inactive_setup_step_panels():
    css = _read("admin.css")
    assert ".setup-step-panel[hidden]" in css
    after = css.split(".setup-step-panel[hidden]", 1)[1][:40]
    assert "display: none" in after


def test_setup_release_step_has_version_controls():
    html = _read("index.html")
    release = _setup_panel(html).split('aria-label="Release"', 1)[1].split(
        'aria-label="Devices"', 1
    )[0]
    assert 'id="release-select"' in release
    assert "Loading releases" in release
    assert "Custom tag" not in release
    assert 'id="release-download"' in release
    assert 'id="release-status"' in release
    assert 'id="release-error"' in release
    assert "Docker-based EMS installations" in release
    assert "from v0.6.0 onward" in release
    assert 'id="release-badges"' in release


def test_setup_has_back_and_next_nav_buttons():
    html = _read("index.html")
    setup = _setup_panel(html)
    assert 'id="setup-back"' in setup
    assert 'id="setup-next"' in setup
    # Next starts disabled until the release step is ready.
    assert 'id="setup-next" type="button" class="primary-button compact" disabled' in setup


def test_js_defines_setup_state_model():
    js = _read("admin.js")
    state = js.split("const setupState =", 1)[1].split("};", 1)[0]
    assert "activeStep" in state
    assert "release" in state and "devices" in state and "config" in state
    for key in ("status", "supported_count", "auto_added_count"):
        assert key in state


def test_js_release_gates_next_until_ready():
    js = _read("admin.js")
    assert "function releaseReady" in js
    ready = js.split("function releaseReady", 1)[1].split("\nfunction ", 1)[0]
    assert 'setupState.release.status === "ready"' in ready
    # Devices and Config are locked until the release step is ready.
    locked = js.split("function stepLocked", 1)[1].split("\nfunction ", 1)[0]
    assert '"devices"' in locked and '"config"' in locked
    assert "!releaseReady()" in locked
    # The Next button is disabled while the following step is locked.
    nav = js.split("function renderSetupNav", 1)[1].split("\nfunction ", 1)[0]
    assert "stepLocked(SETUP_STEPS[index + 1])" in nav
    assert "setupEls.next.disabled" in nav


def test_cached_release_selection_must_update_backend_pointer_before_ready():
    js = _read("admin.js")
    select = js.split("function onReleaseSelectChange", 1)[1].split(
        "\nfunction ", 1
    )[0]
    assert "setupState.release.current === release.tag" in select
    prepare = js.split("async function prepareRelease", 1)[1].split(
        "\ndocument.querySelectorAll", 1
    )[0]
    assert "setupState.release.current = data.tag" in prepare


def test_js_only_active_step_panel_is_shown():
    js = _read("admin.js")
    fn = js.split("function setActiveStep", 1)[1].split("\nfunction ", 1)[0]
    assert "panel.dataset.setupStepPanel !== next" in fn
    assert "panel.hidden" in fn


def test_js_release_preparation_uses_backend_api():
    js = _read("admin.js")
    assert "/api/setup/releases" in js
    assert "/api/setup/releases/prepare" in js
    assert "function prepareRelease" in js
    for status in ("not_started", "downloading", "ready", "failed"):
        assert status in js
    # A failed preparation surfaces an error and a Retry button.
    setter = js.split("function setReleaseStatus", 1)[1].split("\nfunction ", 1)[0]
    assert "Retry" in setter
    assert "releaseError" in setter
    assert "setTimeout(() => setReleaseStatus(\"ready\")" not in js


def test_release_options_show_classification_and_disable_unsupported_refs():
    js = _read("admin.js")
    labels = js.split("function releaseOptionLabel", 1)[1].split(
        "\nfunction ", 1
    )[0]
    for badge in ("stable", "latest", "rc", "docker", "unsupported"):
        assert f'"{badge}"' in labels
    assert "release.reason" in labels
    assert "option.disabled = release.selectable === false" in js


def test_release_default_uses_backend_supported_stable_choice():
    js = _read("admin.js")
    load = js.split("async function loadReleases", 1)[1].split(
        "\nasync function ", 1
    )[0]
    assert "data.default_release" in load
    assert "data.prepared_release" in load


def test_release_badges_use_safe_dom_text():
    js = _read("admin.js")
    render = js.split("function renderReleaseBadges", 1)[1].split(
        "\nfunction ", 1
    )[0]
    assert "document.createElement" in render
    assert "span.textContent" in render
    assert "innerHTML" not in render


def test_release_resources_are_kept_under_advanced_details():
    html = _read("index.html")
    release = _setup_panel(html).split('aria-label="Release"', 1)[1].split(
        'aria-label="Devices"', 1
    )[0]
    assert 'id="release-resource-details"' in release
    assert "<summary>Advanced details</summary>" in release
    for resource in ("config", "install", "compose", "manifest"):
        assert f'id="release-resource-{resource}"' in release


def test_js_release_status_values_written_via_textcontent():
    js = _read("admin.js")
    setter = js.split("function setReleaseStatus", 1)[1].split("\nfunction ", 1)[0]
    # Untrusted release messages/errors are written with textContent, not innerHTML.
    assert "setupEls.releaseStatus.textContent" in setter
    assert "setupEls.releaseError.textContent" in setter


def _extract_fn(js, name):
    marker = "function " + name
    body = js.split(marker, 1)[1].split("\nfunction ", 1)[0]
    return marker + body


def test_js_aggregate_gates_config_sync_on_discovery_signature():
    js = _read("admin.js")
    aggregate = _extract_fn(js, "renderAggregate")
    # Config sync only runs when the discovery signature actually changed, so an
    # unchanged mDNS poll never re-renders the draft the user is editing.
    assert "buildDiscoverySignature(" in aggregate
    assert "lastDiscoverySignature" in aggregate
    assert "if (signature !== lastDiscoverySignature)" in aggregate
    assert "syncConfigFromDiscovery()" in aggregate


def test_js_discovery_signature_excludes_volatile_timestamps():
    js = _read("admin.js")
    signature = _extract_fn(js, "buildDiscoverySignature")
    # Volatile fields must not appear in the signature or every poll would look
    # like a change and reset the Config draft.
    assert "last_seen" not in signature
    assert "last_verify_attempt" not in signature
    assert "confidence" not in signature
    # The fields that do matter for the rendered draft are included.
    for field in ("role", "ip", "port", "serial_number", "api_family"):
        assert field in signature


def _run_signature_node(setup):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for the discovery signature behavior test")
    js = _read("admin.js")
    helpers = "\n".join(
        _extract_fn(js, name)
        for name in ("deviceKey", "isAutoConfigReady", "buildDiscoverySignature")
    )
    script = helpers + "\n" + setup
    result = subprocess.run(
        [node, "-e", script],
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


BASE_DEVICES_JS = """
const inverter = {
  serial_number: "SN-1", api_family: "zendure", role_suggestion: "inverter",
  ip: "10.0.0.5", port: 8080, device_type: "solarflow", verified: true,
  usable_for_config: true, last_seen: "2026-07-01T10:00:00Z",
};
const meter = {
  serial_number: "SN-2", api_family: "shelly", role_suggestion: "grid_meter",
  ip: "10.0.0.6", port: 80, device_type: "pro3em", verified: true,
  usable_for_config: true, last_seen: "2026-07-01T10:00:00Z",
};
"""


def test_signature_stable_across_identical_and_volatile_polls():
    out = _run_signature_node(
        BASE_DEVICES_JS
        + """
const first = buildDiscoverySignature([inverter, meter], []);
// Same devices, only volatile timestamps and order changed.
const second = buildDiscoverySignature(
  [
    Object.assign({}, meter, { last_seen: "2026-07-01T10:05:00Z" }),
    Object.assign({}, inverter, { last_seen: "2026-07-01T10:05:00Z" }),
  ],
  []
);
console.log(JSON.stringify({ first, second }));
"""
    )
    assert out["first"] == out["second"]


def test_signature_changes_when_new_device_appears():
    out = _run_signature_node(
        BASE_DEVICES_JS
        + """
const before = buildDiscoverySignature([inverter], []);
const extra = Object.assign({}, inverter, { serial_number: "SN-9" });
const after = buildDiscoverySignature([inverter, extra], []);
console.log(JSON.stringify({ before, after }));
"""
    )
    assert out["before"] != out["after"]


def test_signature_changes_when_usable_status_flips():
    out = _run_signature_node(
        BASE_DEVICES_JS
        + """
const notReady = Object.assign({}, meter, { usable_for_config: false });
const before = buildDiscoverySignature([notReady], []);
const after = buildDiscoverySignature([meter], []);
console.log(JSON.stringify({ before, after }));
"""
    )
    assert out["before"] != out["after"]


# --- catalog-driven feature settings -------------------------------------


def test_config_stage_has_grouped_setup_settings():
    html = _read("index.html")
    config = _config_section(html)
    assert 'id="config-feature-settings"' in config
    # Setup settings are split into Hardware, Features and Advanced groups.
    for group in ("hardware", "features", "advanced"):
        assert 'data-setup-group="' + group + '"' in config
    for group in ("features", "advanced"):
        assert 'id="config-feature-list-' + group + '"' in config
    assert ">Hardware<" in config
    assert ">Features<" in config


def test_config_stage_renders_hardware_group_before_features():
    html = _read("index.html")
    config = _config_section(html)
    hardware = config.index('data-setup-group="hardware"')
    features = config.index('data-setup-group="features"')
    advanced = config.index('data-setup-group="advanced"')
    assert hardware < features < advanced


def test_config_hardware_group_contains_grid_meter_and_devices():
    html = _read("index.html")
    config = _config_section(html)
    hardware = config.split('data-setup-group="hardware"', 1)[1].split(
        'data-setup-group="features"', 1
    )[0]
    grid_heading = hardware.index(">Grid meter<")
    grid_card = hardware.index('id="config-grid-meter-selection"')
    inverter_heading = hardware.index(">Inverters / devices<")
    assert grid_heading < grid_card < inverter_heading
    assert "Inverters / devices" in hardware
    assert 'id="config-draft-list"' in hardware
    assert 'id="config-available-list"' in hardware


def test_config_advanced_group_is_collapsed_by_default():
    html = _read("index.html")
    config = _config_section(html)
    assert 'class="advanced-details setup-group" data-setup-group="advanced"' in config
    assert 'id="config-advanced-settings" open' not in config


def test_js_fetches_setup_config_catalog_endpoint():
    js = _read("admin.js")
    assert "/api/setup/config/catalog" in js
    assert "loadSetupCatalog" in js
    assert "renderFeatureSettings" in js


def test_js_renders_feature_rows_into_group_lists():
    js = _read("admin.js")
    render = js.split("function renderFeatureSettings", 1)[1].split(
        "\nfunction ", 1
    )[0]
    # Sections are distributed into per-group list containers by setup_group.
    assert "configEls.featureLists" in render
    assert "sectionsForGroup" in render
    group_fn = js.split("function sectionsForGroup", 1)[1].split("\nfunction ", 1)[0]
    assert "section.setup_group" in group_fn
    # Hardware renders first, then features, then advanced.
    order = js.split("SETUP_GROUP_ORDER = [", 1)[1].split("]", 1)[0]
    assert '"hardware"' in order
    assert order.index('"hardware"') < order.index('"features"') < order.index(
        '"advanced"'
    )


def test_js_renders_feature_rows_with_title_description_status_and_toggle():
    js = _read("admin.js")
    row = js.split("function renderFeatureRow", 1)[1].split("\nfunction ", 1)[0]
    # Collapsed rows expose title, short description, status and expand affordance.
    assert "feature-title" in row
    assert "feature-desc" in row
    assert "feature-status" in row
    assert "data-feature-toggle" in row
    assert 'aria-expanded="' in row
    # Feature activation uses a real checkbox control in the row.
    assert "data-feature-enable" in row
    assert 'type="checkbox"' in row


def test_js_only_renders_fields_when_feature_row_is_open():
    js = _read("admin.js")
    row = js.split("function renderFeatureRow", 1)[1].split("\nfunction ", 1)[0]
    # The body is only populated when the row is in the open set.
    assert "openFeatures.has(section.id)" in js
    assert "open ? renderFeatureBody(section)" in row


def test_js_separates_normal_advanced_and_expert_field_levels():
    js = _read("admin.js")
    body = js.split("function renderFeatureBody", 1)[1].split("\nfunction ", 1)[0]
    # Normal fields render first; advanced and expert sit in nested collapsed
    # <details> areas that stay closed by default.
    assert "feature-advanced" in body
    assert "feature-expert" in body
    assert "<summary>Advanced settings</summary>" in body
    assert "Developer / expert settings" in body
    assert "control stability" in body


def test_js_feature_fields_render_as_compact_rows_not_tiles():
    js = _read("admin.js")
    field = js.split("function renderFeatureField", 1)[1].split("\nfunction ", 1)[0]
    # Each field is a single settings row (label | control | description), not a
    # stacked card/tile.
    assert 'class="feature-field-row"' in field
    assert 'class="feature-field-label"' in field
    assert 'class="feature-field-control"' in field
    assert "feature-field field" not in field  # old tile class is gone


def test_css_feature_fields_use_row_grid_with_mobile_stack():
    css = _read("admin.css")
    row = css.split(".feature-field-row {", 1)[1].split("}", 1)[0]
    # Desktop: three-column grid so labels, controls and descriptions align.
    assert "display: grid" in row
    assert "grid-template-columns" in row
    # Narrow viewports collapse the row into a single stacked column.
    mobile = css.split("@media (max-width: 760px)", 1)[1]
    assert ".feature-field-row { grid-template-columns: 1fr" in mobile


def test_js_feature_values_and_dynamic_text_pass_through_escape_helper():
    js = _read("admin.js")
    field = js.split("function renderFeatureControl", 1)[1].split("\nfunction ", 1)[0]
    assert "escapeHtml(" in field
    rowfn = js.split("function renderFeatureRow", 1)[1].split("\nfunction ", 1)[0]
    assert "escapeHtml(section.title)" in rowfn


def test_js_grid_meter_variant_fields_change_with_selected_type():
    js = _read("admin.js")
    assert "gridVariantFields" in js
    assert "selectedGridMeterType" in js
    assert "data-feature-variant-select" in js
    # Changing the meter type re-renders so variant-specific fields appear.
    change = js.split("function handleFeatureListChange", 1)[1].split(
        "\nfunction ", 1
    )[0]
    assert "data-feature-variant-select" in change
    assert "renderFeatureSettings()" in change


def test_js_feature_values_flow_into_preview_and_export():
    js = _read("admin.js")
    assert "features: featureValues" in js


def test_js_deprecated_levels_are_hidden_from_setup_features():
    js = _read("admin.js")
    assert 'FEATURE_LEVELS_HIDDEN' in js
    const = js.split("const FEATURE_LEVELS_HIDDEN", 1)[1].split("\n", 1)[0]
    assert '"deprecated"' in const


def test_manual_form_offers_role_specific_type_selection():
    html = _read("index.html")
    config = _config_section(html)
    assert 'id="config-manual-type"' in config
    assert 'id="config-manual-type-description"' not in config
    assert ">Type<" in config
    assert ">Meter type<" not in config


def test_js_manual_type_is_role_specific_and_stored():
    js = _read("admin.js")
    fn = js.split("function addManualDevice", 1)[1].split("\nfunction ", 1)[0]
    assert "item.grid_meter_type = selectedType" in fn
    assert "item.connection_type = selectedType" in fn
    assert "syncGridMeterFeatureValues(item)" in fn
    variants = js.split("function manualHardwareVariants", 1)[1].split(
        "\nfunction ", 1
    )[0]
    assert "setupCatalog.hardware_variants[role]" in variants
    reset = js.split("function resetManualTypeForRole", 1)[1].split(
        "\nfunction ", 1
    )[0]
    assert "populateManualTypes(true)" in reset


def test_js_grid_meter_type_prefers_explicit_field():
    js = _read("admin.js")
    fn = js.split("function gridMeterType", 1)[1].split("\nfunction ", 1)[0]
    # Explicit type wins before discovery inference.
    assert "item.grid_meter_type" in fn
    assert "GRID_METER_TYPE_CHOICES.has(explicit)" in fn


def test_js_inverter_body_exposes_enabled_field():
    js = _read("admin.js")
    body = js.split("function renderInverterBody", 1)[1].split("\nfunction ", 1)[0]
    # Enabled is an explicit hardware field in the opened item, not only the
    # header checkbox.
    assert "renderHardwareEnabledRow" in body
    assert '"data-inverter-enable"' in body
    assert "Include this inverter" in body
    row = js.split("function renderInverterDraftRow", 1)[1].split(
        "\nfunction ", 1
    )[0]
    assert 'removeClass: "config-draft-remove"' in row


def test_js_grid_meter_card_uses_shared_hardware_controls():
    js = _read("admin.js")
    fn = js.split("function renderSelectedGridMeter", 1)[1].split(
        "\nfunction ", 1
    )[0]
    assert "renderHardwareCard({" in fn
    assert 'kind: "grid-meter"' in fn
    assert "gridMeterModelText(meter)" in fn
    assert 'removeClass: "config-grid-remove"' in fn
    body = js.split("function renderGridMeterBody", 1)[1].split(
        "\nfunction ", 1
    )[0]
    assert "renderHardwareEnabledRow" in body
    assert '"data-grid-enable"' in body


def test_js_shared_hardware_card_has_common_header_contract():
    js = _read("admin.js")
    card = js.split("function renderHardwareCard", 1)[1].split(
        "\nfunction ", 1
    )[0]
    for class_name in (
        "hardware-card-title",
        "hardware-card-model",
        "hardware-card-meta",
        "hardware-card-status",
        "hardware-card-remove",
        "hardware-card-toggle",
    ):
        assert class_name in card
    assert ">Remove<" in card


def test_js_hardware_remove_uses_shared_draft_action():
    js = _read("admin.js")
    # Both hardware item types remove through the same removeDraftItem path so the
    # dismissed-source behavior stays consistent.
    grid = js.split("configEls.gridMeterSelection.addEventListener", 1)[1].split(
        "});", 1
    )[0]
    assert "removeDraftItem(" in grid
    draft = js.split("configEls.draftList.addEventListener", 1)[1].split(
        "renderConfigPreview", 1
    )[0]
    assert "removeDraftItem(sourceId)" in draft
    fn = js.split("function removeDraftItem", 1)[1].split("\nfunction ", 1)[0]
    # Removal still dismisses the source so discovery does not re-add it.
    assert "configDismissed.add(sourceId)" in fn


def test_css_hardware_role_accents_are_grid_and_output():
    css = _read("admin.css")
    grid = css.split(".hardware-card-grid-meter {", 1)[1].split("}", 1)[0]
    inverter = css.split(".hardware-card-inverter {", 1)[1].split("}", 1)[0]
    assert "var(--grid)" in grid
    assert "var(--pv)" not in grid
    assert "var(--output)" in inverter


# --- maintenance overview (read-only) ------------------------------------


def _maintenance_section(html):
    return html.split('id="view-maintenance"', 1)[1].split(
        'id="view-advanced"', 1
    )[0]


def test_index_has_maintenance_view_panel():
    html = _read("index.html")
    assert 'id="view-maintenance"' in html
    assert 'data-admin-view-panel="maintenance" hidden' in html
    # The maintenance view is reached from the start gate, not a top-level tab.
    assert 'data-admin-view="maintenance"' not in html


def test_maintenance_opens_hub_before_manual_editor():
    html = _read("index.html")
    maintenance = _maintenance_section(html)
    # The hub is shown first; the detailed editor lives in a hidden nested panel.
    assert 'id="maintenance-hub"' in maintenance
    assert 'id="maintenance-manual-panel" hidden' in maintenance
    assert maintenance.index('id="maintenance-hub"') < maintenance.index(
        'id="maintenance-manual-panel"'
    )


def test_maintenance_hub_exposes_three_user_paths():
    html = _read("index.html")
    hub = _read("index.html").split('id="maintenance-hub"', 1)[1].split(
        'id="maintenance-manual-panel"', 1
    )[0]
    for path, label in (
        ("manual", "Manual configuration / existing system"),
        ("upgrade", "Guided upgrade"),
        ("backup", "Backup / restore"),
    ):
        assert 'data-maintenance-path="' + path + '"' in hub
        assert label in hub
    # The manual path is a full-page navigation button (drills into its own page),
    # not an inline "open" toggle.
    assert 'id="maintenance-open-manual"' in hub
    open_tag = hub.split('id="maintenance-open-manual"', 1)[0].rsplit("<", 1)[1]
    assert open_tag.startswith("button")
    assert "maintenance-path-nav" in hub
    assert "Open manual maintenance" not in hub
    # Guided upgrade is the recommended default path; only backup stays "Planned".
    assert hub.count('class="source-badge">Planned') == 1
    assert "/api/admin/" not in hub


def test_maintenance_hub_orders_guided_upgrade_first():
    html = _read("index.html")
    hub = html.split('id="maintenance-hub"', 1)[1].split(
        'id="maintenance-manual-panel"', 1
    )[0]
    # DOM order (not CSS): guided upgrade, then manual config, then backup.
    order = [
        hub.index('data-maintenance-path="upgrade"'),
        hub.index('data-maintenance-path="manual"'),
        hub.index('data-maintenance-path="backup"'),
    ]
    assert order == sorted(order)
    # Step numbers follow the order.
    for path, step in (("upgrade", "01"), ("manual", "02"), ("backup", "03")):
        card = hub.split('data-maintenance-path="' + path + '"', 1)[1]
        assert card.split("control-stage-step\">", 1)[1].startswith(step)
    # Guided upgrade is a navigation button with the recommended/primary treatment.
    upgrade_tag = hub.split('data-maintenance-path="upgrade"', 1)[0].rsplit("<", 1)[1]
    assert upgrade_tag.startswith("button")
    assert "is-primary" in upgrade_tag
    upgrade = hub.split('data-maintenance-path="upgrade"', 1)[1].split(
        'data-maintenance-path="manual"', 1
    )[0]
    assert "Recommended path" in upgrade


def test_workspace_pages_have_back_navigation():
    html = _read("index.html")
    # Setup and the maintenance hub go back to the landing gate; the manual page
    # goes back to the maintenance hub.
    setup = _setup_panel(html)
    assert 'data-back="landing"' in setup
    hub = html.split('id="maintenance-hub"', 1)[1].split(
        'id="maintenance-manual-panel"', 1
    )[0]
    assert 'data-back="landing"' in hub
    manual = html.split('id="maintenance-manual-panel"', 1)[1].split(
        'id="view-advanced"', 1
    )[0]
    assert 'data-back="maintenance-hub"' in manual
    assert 'id="maintenance-back-hub"' in manual
    # Back controls are real buttons, never clickable divs.
    for segment in (setup, hub, manual):
        marker = segment.split('data-back="', 1)[0].rsplit("<", 1)[1]
        assert marker.startswith("button")


def test_js_back_navigation_returns_to_landing_and_hub():
    js = _read("admin.js")
    nav = js.split("function navigateBack", 1)[1].split("\n}", 1)[0]
    assert 'target === "maintenance-hub"' in nav
    assert "showLanding()" in nav
    landing = js.split("function showLanding", 1)[1].split("\n}", 1)[0]
    # Returning to landing re-shows the gate and drops workspace routing.
    assert "startEls.gate" in landing
    assert "workspaceRevealed = false" in landing
    # Hash routing is inert while the landing gate is showing.
    route = js.split("function applyHashRoute", 1)[1].split("\n}", 1)[0]
    assert "if (!workspaceRevealed) return" in route
    # The back controls are wired from the shared data-back attribute.
    assert '[data-back]' in js


def test_maintenance_manual_panel_keeps_detailed_markup():
    html = _read("index.html")
    manual = html.split('id="maintenance-manual-panel"', 1)[1].split(
        'id="view-advanced"', 1
    )[0]
    for marker in (
        'id="maintenance-refresh"',
        'id="maintenance-config"',
        'id="maintenance-ems"',
        'id="maintenance-diagnostics"',
        'id="maintenance-config-card"',
        'id="maintenance-back-hub"',
    ):
        assert marker in manual


def test_maintenance_endpoints_are_unchanged():
    js = _read("admin.js")
    for endpoint in (
        "/api/admin/maintenance/overview",
        "/api/admin/maintenance/diagnostics/run",
        "/api/admin/maintenance/config",
        "/api/admin/maintenance/config/preview",
        "/api/admin/maintenance/config/apply",
        "/api/admin/maintenance/containers/plan",
        "/api/admin/maintenance/containers/sync",
    ):
        assert endpoint in js


def test_js_maintenance_path_helper_only_manual_loads_overview():
    js = _read("admin.js")
    assert 'const MAINTENANCE_PATHS = ["hub", "manual", "upgrade", "backup"]' in js
    # Every path maps to exactly one panel via a registry, not manual-only toggling.
    registry = js.split("const MAINTENANCE_PANEL_IDS = {", 1)[1].split("};", 1)[0]
    for key, panel in (
        ("hub", "maintenance-hub"),
        ("manual", "maintenance-manual-panel"),
        ("upgrade", "maintenance-upgrade-panel"),
        ("backup", "maintenance-backup-panel"),
    ):
        assert key + ': "' + panel + '"' in registry
    path = js.split("function setMaintenancePath", 1)[1].split("\nfunction ", 1)[0]
    # The helper drives panels from the registry; only manual loads the overview.
    assert "MAINTENANCE_PANEL_IDS" in path
    assert 'next === "manual"' in path
    assert "loadMaintenanceOverview()" in path


def test_js_maintenance_cards_use_generic_open_navigation():
    js = _read("admin.js")
    # Card navigation is wired from a shared data attribute, not a manual-only id.
    assert "[data-open-maintenance-path]" in js
    handler = js.split("[data-open-maintenance-path]", 1)[1].split("});", 1)[0]
    assert "openMaintenancePath" in handler
    assert 'window.location.hash = "maintenance-" + path' in handler


def test_maintenance_subpages_are_full_page_siblings_of_hub():
    html = _read("index.html")
    maintenance = _maintenance_section(html)
    # Each path has its own full-page panel, all hidden by default and siblings
    # of the hub under view-maintenance (never rendered below the hub).
    for panel in (
        "maintenance-manual-panel",
        "maintenance-upgrade-panel",
        "maintenance-backup-panel",
    ):
        assert 'id="' + panel + '" hidden' in maintenance
    # Every subpage carries a back button to the maintenance hub and stays a
    # non-mutating placeholder (no backend calls).
    for panel in ("maintenance-upgrade-panel", "maintenance-backup-panel"):
        head = maintenance.split('id="' + panel + '"', 1)[1].split("</header>", 1)[0]
        assert 'data-back="maintenance-hub"' in head
        assert "/api/admin/" not in maintenance.split('id="' + panel + '"', 1)[1]


def test_css_hidden_maintenance_panels_beat_hub_flex_layout():
    css = _read("admin.css")
    # The hub sets display:flex, which would override the UA [hidden] rule and
    # leave the hub rendered above the selected subpage. A targeted rule must
    # restore hidden semantics for the hub and each full-page panel.
    for selector in (
        ".maintenance-hub[hidden]",
        "#maintenance-manual-panel[hidden]",
        "#maintenance-upgrade-panel[hidden]",
        "#maintenance-backup-panel[hidden]",
    ):
        assert selector in css
    block = css.split(".maintenance-hub[hidden]", 1)[1].split("}", 1)[0]
    assert "display: none" in block


def _upgrade_panel(html):
    maintenance = _maintenance_section(html)
    return maintenance.split('id="maintenance-upgrade-panel"', 1)[1].split(
        'id="maintenance-backup-panel"', 1
    )[0]


def test_guided_upgrade_planning_has_three_numbered_stages():
    html = _read("index.html")
    panel = _upgrade_panel(html)
    for label in ("EMS release", "Upgrade options", "Upgrade validation"):
        assert 'aria-label="' + label + '"' in panel
    for step in ("01", "02", "03"):
        assert ">" + step + "<" in panel
    # Current version + a target selector are shown.
    assert 'id="upgrade-current-version"' in panel
    assert 'id="upgrade-release-select"' in panel
    assert 'id="upgrade-prepare-btn"' in panel


def test_guided_upgrade_uses_clean_stage_style_not_maintenance_card():
    html = _read("index.html")
    panel = _upgrade_panel(html)
    # The guided workflow must not reuse the collapsible maintenance/config card
    # language (which draws the blue accent line and form-style option rows).
    for cls in ("maintenance-card", "maintenance-card-head", "mconfig-backup-choice"):
        assert cls not in panel
    # Its stages are plain control-pipeline stages carrying the guided marker.
    assert panel.count("control-pipeline-stage guided-upgrade-stage") == 3
    # Options reuse the shared settings-list rows instead of inline checkboxes.
    assert "feature-fields upgrade-options" in panel
    assert "feature-field-row" in panel


def test_guided_upgrade_options_default_on_with_backup():
    html = _read("index.html")
    panel = _upgrade_panel(html)
    for key in (
        "backup",
        "config_check",
        "config_add_keys",
        "config_comments",
        "pull_image",
        "recreate",
        "diagnostics",
    ):
        marker = 'data-upgrade-option="' + key + '"'
        assert marker in panel
        # Each option box ships checked by default.
        box = panel.split(marker, 1)[1].split(">", 1)[0]
        prefix = panel.split(marker, 1)[0].rsplit("<input", 1)[1]
        assert "checked" in prefix + box


def test_guided_upgrade_validation_reuses_setup_card_and_has_execute_action():
    html = _read("index.html")
    panel = _upgrade_panel(html)
    # Validation reuses the Setup validation card style.
    assert 'id="upgrade-validation-card" class="config-validation-card"' in panel
    assert 'id="upgrade-validation"' in panel and "config-validation-list" in panel
    assert 'id="upgrade-plan-btn"' in panel
    # The execute button exists but ships disabled; the JS enables it only once a
    # target is selected, prepared, and planned.
    assert 'id="upgrade-execute-btn"' in panel
    execute = panel.split('id="upgrade-execute-btn"', 1)[1].split(">", 1)[0]
    prefix = panel.split('id="upgrade-execute-btn"', 1)[0].rsplit("<button", 1)[1]
    assert "disabled" in prefix + execute


def test_guided_upgrade_js_planning_and_guarded_execute():
    js = _read("admin.js")
    # Entering the upgrade path loads its own read-only planning data.
    path = js.split("function setMaintenancePath", 1)[1].split("\nfunction ", 1)[0]
    assert 'next === "upgrade"' in path
    assert "loadUpgradePlanning()" in path
    upgrade = js.split("Guided upgrade planning", 1)[1].split(
        "// --- EMS diagnostics", 1
    )[0]
    # Planning consumes read-only / existing preparation endpoints.
    assert "/api/admin/maintenance/overview" in upgrade
    assert "/api/setup/releases" in upgrade
    # The only mutating call is the explicit, confirmed upgrade executor.
    assert "/api/admin/maintenance/upgrade/execute" in upgrade
    assert "confirm: true" in upgrade
    # No other destructive lifecycle calls are made from the module.
    for forbidden in ("config/apply", "containers/sync", "docker rm", "compose up"):
        assert forbidden not in upgrade


def test_guided_upgrade_js_polls_live_step_progress():
    js = _read("admin.js")
    upgrade = js.split("Guided upgrade planning", 1)[1].split(
        "// --- EMS diagnostics", 1
    )[0]
    # Execute kicks off a job and the UI polls it for live step states.
    assert "/api/admin/maintenance/upgrade/jobs/" in upgrade
    assert "job_id" in upgrade
    assert "pollUpgradeJob" in upgrade
    assert "renderUpgradeSteps" in upgrade
    # Polling loops on a timer and stops on a terminal status.
    assert "setTimeout" in upgrade
    assert "stopUpgradePolling" in upgrade
    assert '"succeeded"' in upgrade and '"failed"' in upgrade
    # Live steps render every job step state, including running/pending.
    for state in ("done", "running", "pending", "failed"):
        assert state in upgrade


def test_guided_upgrade_execute_button_enabled_only_when_ready():
    js = _read("admin.js")
    fn = js.split("function updateExecuteButton", 1)[1].split("\nfunction ", 1)[0]
    # Execution is gated on a generated plan, a selected + prepared target, and no
    # in-flight run; it starts disabled in the HTML until this logic enables it.
    assert "upgradeState.planned" in fn
    assert "upgradeTargetPrepared()" in fn
    assert "upgradeState.selected" in fn
    assert "upgradeState.running" in fn
    assert "executeBtn.disabled = !allowed" in fn


def test_maintenance_view_has_three_numbered_sections():
    html = _read("index.html")
    maintenance = _maintenance_section(html)
    for label in ("Installation layout", "Runtime containers", "Versions and links"):
        assert 'aria-label="' + label + '"' in maintenance
        assert label in maintenance
    for step in ("01", "02", "03"):
        assert ">" + step + "<" in maintenance
    assert "Existing EMS installation overview" in maintenance


def test_maintenance_view_has_layout_container_and_version_facts():
    html = _read("index.html")
    maintenance = _maintenance_section(html)
    for marker in (
        'id="maintenance-config"',
        'id="maintenance-data"',
        'id="maintenance-compose"',
        'id="maintenance-state"',
        'id="maintenance-ems"',
        'id="maintenance-influx"',
        'id="maintenance-docker"',
        'id="maintenance-ems-image"',
        'id="maintenance-influx-image"',
        'id="maintenance-dashboard"',
        'id="maintenance-warnings"',
    ):
        assert marker in maintenance


def test_maintenance_exposes_no_mutating_actions():
    html = _read("index.html")
    js = _read("admin.js")
    maintenance = _maintenance_section(html)
    # The detailed controls live in the manual panel; the hub only holds planned
    # placeholders (verified separately to carry no endpoints), whose descriptive
    # text mentions backup/restore/upgrade without exposing any action.
    manual = maintenance.split('id="maintenance-manual-panel"', 1)[1].split(
        'id="maintenance-upgrade-panel"', 1
    )[0]
    # The guided container-sync workflow is the one sanctioned mutating action
    # (explicit confirm, no delete). Exclude its label before asserting that no
    # other arbitrary EMS lifecycle controls exist.
    guarded = manual.replace("Restart / sync containers", "")
    for forbidden in (
        "Update",
        "Restart",
        "Backup",
        "Restore",
        "Stop EMS",
        "Start EMS",
    ):
        assert forbidden not in guarded
    # The only maintenance control is a read-only refresh.
    assert 'id="maintenance-refresh"' in maintenance
    # No mutating Docker/compose calls are wired from the maintenance renderer.
    render = js.split("function renderMaintenance", 1)[1].split(
        "\nfunction renderMaintenanceError", 1
    )[0]
    for mutating in ("compose up", "docker rm", "docker stop", "/prepare", "/start"):
        assert mutating not in render


def test_js_defines_maintenance_overview_renderer():
    js = _read("admin.js")
    assert "function renderMaintenance" in js
    assert "async function loadMaintenanceOverview" in js
    assert "/api/admin/maintenance/overview" in js


def test_js_maintenance_routes_to_hub_not_overview():
    js = _read("admin.js")
    fn = js.split("function enterMaintenance", 1)[1].split("\n}", 1)[0]
    assert 'setAdminView("maintenance")' in fn
    # Entering maintenance shows the hub; it must not load the overview.
    assert 'setMaintenancePath("hub")' in fn
    assert "loadMaintenanceOverview()" not in fn
    # The admin view registry includes maintenance so hash routing works.
    assert '"maintenance"' in js.split("const ADMIN_VIEWS =", 1)[1].split("]", 1)[0]
    # Switching the admin panel no longer auto-loads the overview.
    switch = js.split("function setAdminView", 1)[1].split("\nfunction ", 1)[0]
    assert "loadMaintenanceOverview()" not in switch
    # Only opening the manual path loads the detailed overview.
    path = js.split("function setMaintenancePath", 1)[1].split("\nfunction ", 1)[0]
    assert 'next === "manual"' in path
    assert "loadMaintenanceOverview()" in path


def test_js_maintenance_dynamic_values_are_escaped_or_text_only():
    js = _read("admin.js")
    render = js.split("function renderMaintenance", 1)[1].split(
        "\nfunction renderMaintenanceError", 1
    )[0]
    # Fact values (image refs, dashboard URL, state labels) are written via
    # textContent through the shared setter, never innerHTML.
    assert "setMaintenanceFact" in render
    setter = js.split("function setMaintenanceFact", 1)[1].split("\nfunction ", 1)[0]
    assert "el.textContent = text" in setter
    assert "innerHTML" not in setter
    # Warnings are the only innerHTML path and pass through escapeHtml.
    warnings = js.split("function renderMaintenanceWarnings", 1)[1].split(
        "\nfunction ", 1
    )[0]
    assert "escapeHtml(note)" in warnings


def test_js_maintenance_card_tone_helper_uses_dataset():
    js = _read("admin.js")
    helper = js.split("function setMaintenanceCardTone", 1)[1].split(
        "\nfunction ", 1
    )[0]
    # Tone is applied via data-tone and cleared when empty, never via innerHTML.
    assert "card.dataset.tone = tone" in helper
    assert "delete card.dataset.tone" in helper
    assert "innerHTML" not in helper


def test_js_maintenance_summaries_set_card_tones():
    js = _read("admin.js")
    fn = js.split("function renderMaintenanceSummaries", 1)[1].split(
        "\nfunction ", 1
    )[0]
    assert 'setMaintenanceCardTone("maintenance-layout", layoutOk ? "ok" : "warn")' in fn
    assert 'setMaintenanceCardTone("maintenance-versions", dashboard ? "info" : "warn")' in fn
    # The Runtime containers summary + tone are owned by the plan renderer, which
    # knows the config feature-state; the overview summary no longer derives them.
    assert "maintenance-containers" not in fn
    plan = js.split("function renderRuntimeServiceStatus", 1)[1].split(
        "\nfunction ", 1
    )[0]
    assert 'setMaintenanceCardTone("maintenance-containers", tone)' in plan
    assert "plan.status_summary" in plan


def test_js_config_apply_sets_action_tone_only_when_changed():
    js = _read("admin.js")
    # The changed branch flags the config card for the follow-up container sync.
    changed = js.split("Config updated · container sync recommended", 1)[1].split(
        "await loadMaintenanceContainerPlan", 1
    )[0]
    assert 'setMaintenanceCardTone("maintenance-config-card", "action")' in changed
    # A no-op apply (changed === false) must not raise the action tone.
    noop = js.split("if (data.changed === false) {", 1)[1].split("} else {", 1)[0]
    assert "setMaintenanceCardTone" not in noop


def test_maintenance_card_has_tone_accent_styles():
    css = _read("admin.css")
    assert ".maintenance-card::before" in css
    for tone in ("ok", "warn", "info", "action"):
        assert '.maintenance-card[data-tone="' + tone + '"]::before' in css


def test_maintenance_cards_are_collapsed_by_default_with_summaries():
    html = _read("index.html")
    maintenance = _maintenance_section(html)
    # Each card starts collapsed: closed state + hidden body + a toggle button
    # carrying a one-line summary in the header.
    assert maintenance.count('data-open="false"') == 5
    for card in (
        "maintenance-layout",
        "maintenance-containers",
        "maintenance-versions",
        "maintenance-diagnostics",
    ):
        assert 'data-maintenance-toggle="' + card + '"' in maintenance
        assert 'id="' + card + '-body"' in maintenance
    for summary in (
        'id="maintenance-system-status"',
        'id="maintenance-layout-summary"',
        'id="maintenance-containers-summary"',
        'id="maintenance-versions-summary"',
    ):
        assert summary in maintenance
    # The detailed bodies stay hidden until expanded.
    assert 'id="maintenance-layout-body" hidden' in maintenance


def test_maintenance_expanded_body_carries_resolved_paths_and_names():
    html = _read("index.html")
    maintenance = _maintenance_section(html)
    for marker in (
        'id="maintenance-config-path"',
        'id="maintenance-data-path"',
        'id="maintenance-compose-path"',
        'id="maintenance-ems-name"',
        'id="maintenance-influx-name"',
        'id="maintenance-docker-server"',
    ):
        assert marker in maintenance


def test_maintenance_dashboard_link_is_a_real_anchor():
    html = _read("index.html")
    js = _read("admin.js")
    maintenance = _maintenance_section(html)
    assert 'id="maintenance-dashboard-link"' in maintenance
    assert 'target="_blank"' in maintenance
    assert 'rel="noopener"' in maintenance
    # href is set through the DOM property, never via innerHTML/markup.
    fn = js.split("function renderMaintenanceDashboard", 1)[1].split(
        "\nfunction ", 1
    )[0]
    assert "link.href = url" in fn
    assert "innerHTML" not in fn


def test_maintenance_toggle_expands_and_collapses_card():
    js = _read("admin.js")
    fn = js.split("function toggleMaintenanceCard", 1)[1].split("\nfunction ", 1)[0]
    assert 'setAttribute("data-open"' in fn
    assert "body.hidden = !open" in fn
    assert 'setAttribute("aria-expanded"' in fn
    # A delegated click handler drives the collapse/expand.
    assert "[data-maintenance-toggle]" in js


# --- EMS diagnostics card ------------------------------------------------


def test_maintenance_has_collapsed_diagnostics_card():
    html = _read("index.html")
    maintenance = _maintenance_section(html)
    card = maintenance.split('id="maintenance-diagnostics"', 1)
    assert len(card) == 2, "diagnostics card missing"
    # It is the fourth numbered stage and starts collapsed.
    assert 'aria-label="EMS diagnostics"' in maintenance
    assert ">04<" in maintenance
    assert 'data-open="false"' in card[1].split(">", 1)[0]
    body = maintenance.split('id="maintenance-diagnostics-body"', 1)[1].split(">", 1)[0]
    assert "hidden" in body
    # The collapsed header carries a useful default summary.
    assert "Diagnostics have not been run yet." in maintenance
    assert "Read-only EMS checks from the installed system" in maintenance


def test_maintenance_diagnostics_has_run_button_and_no_forbidden_actions():
    html = _read("index.html")
    maintenance = _maintenance_section(html)
    assert 'id="maintenance-diagnostics-run"' in maintenance
    assert "Run diagnostics" in maintenance
    # Dry-run framing is explicit; no config is written.
    assert "Config upgrade is checked in dry-run mode only" in maintenance
    # No mutating maintenance actions are introduced by the diagnostics card
    # itself (scope to its own section, not the later config/sync cards).
    diagnostics = maintenance.split('id="maintenance-diagnostics"', 1)[1].split(
        "</section>", 1
    )[0]
    for forbidden in ("Restart", "Restore", "Update", "Upgrade EMS", "Apply config", "Backup"):
        assert forbidden not in diagnostics


def test_js_diagnostics_posts_to_run_endpoint_without_command_input():
    js = _read("admin.js")
    fn = js.split("async function runDiagnostics", 1)[1].split(
        "\nfunction ", 1
    )[0]
    assert '"/api/admin/maintenance/diagnostics/run"' in fn
    assert 'method: "POST"' in fn
    # The button is disabled and relabelled while running; no raw command is sent.
    assert "button.disabled = true" in fn
    assert "Running…" in fn


def test_js_diagnostics_renders_output_with_safe_dom_text():
    js = _read("admin.js")
    render = js.split("function renderDiagnosticsCheck", 1)[1].split(
        "\nfunction ", 1
    )[0]
    # Every dynamic value goes through createElement/textContent, never innerHTML.
    assert "document.createElement" in render
    assert "textContent" in render
    assert "innerHTML" not in render
    # Raw output lives in a collapsed <details> drawer, not dumped inline.
    assert 'document.createElement("details")' in render
    assert 'document.createElement("pre")' in render


def test_js_diagnostics_summary_reflects_available_and_unavailable_states():
    js = _read("admin.js")
    fn = js.split("function diagnosticsSummaryLine", 1)[1].split(
        "\nfunction ", 1
    )[0]
    assert "EMS CLI available" in fn
    assert "EMS CLI unavailable" in fn
    assert "ok" in fn and "warning" in fn and "failed" in fn
    # A disabled subsystem is surfaced in the summary but keeps the ok tone.
    assert "summary.disabled" in fn


def test_js_diagnostics_disabled_status_is_neutral_not_warning():
    js = _read("admin.js")
    tones = js.split("const DIAGNOSTICS_STATUS_TONE", 1)[1].split("};", 1)[0]
    # Disabled-by-config is neutral (info), never warn/error.
    assert "disabled:" in tones
    assert 'disabled: "info"' in tones
    assert 'disabled: "warn"' not in tones
    assert 'disabled: "error"' not in tones


def test_js_diagnostics_prefers_backend_message_over_raw_stderr():
    js = _read("admin.js")
    fn = js.split("function diagnosticsCheckMessage", 1)[1].split(
        "\nfunction ", 1
    )[0]
    # The friendly backend message (e.g. disabled InfluxDB) is the visible text;
    # raw stderr stays in the raw-output drawer.
    assert "check.message" in fn


def test_css_diagnostics_info_tone_is_muted():
    css = _read("admin.css")
    assert '.maintenance-check-pill[data-tone="info"]' in css


def test_js_diagnostics_is_not_auto_run_on_view_switch():
    js = _read("admin.js")
    # Opening the manual maintenance path loads the read-only overview; diagnostics
    # are user-triggered via the Run button, never auto-run.
    path = js.split("function setMaintenancePath", 1)[1].split("\nfunction ", 1)[0]
    assert "loadMaintenanceOverview()" in path
    assert "runDiagnostics()" not in path
    switch = js.split("function setAdminView", 1)[1].split("\nfunction ", 1)[0]
    assert "runDiagnostics()" not in switch
    assert 'diagnosticsEls.run.addEventListener("click", runDiagnostics)' in js


def test_index_has_config_and_hardware_card_collapsed_by_default():
    html = _read("index.html")
    assert 'id="maintenance-config-card"' in html
    assert "Configuration &amp; hardware" in html
    # collapsed by default: the card body is hidden and the toggle is not expanded
    card = html.split('id="maintenance-config-card"', 1)[1].split("</section>", 1)[0]
    assert 'data-open="false"' in card
    assert 'id="maintenance-config-card-body"' in card
    assert 'aria-expanded="false"' in card
    body_tag = card.split('id="maintenance-config-card-body"', 1)[1].split(">", 1)[0]
    assert "hidden" in body_tag


def test_index_config_card_shows_safe_preview_and_apply_actions():
    html = _read("index.html")
    card = html.split('id="maintenance-config-card"', 1)[1].split("</section>", 1)[0]
    assert 'id="maintenance-config-source"' in card
    assert "Preview changes" in card
    assert "Reset draft" in card
    assert "Apply reviewed draft" in card
    assert "Create a backup before applying (recommended)" in card
    for banned in (">Save<", ">Restart<", ">Restore<", ">Upgrade<"):
        assert banned not in card, f"unexpected write control {banned}"


def test_maintenance_config_uses_setup_hardware_and_feature_groups():
    html = _read("index.html")
    card = html.split('id="maintenance-config-card"', 1)[1].split("</section>", 1)[0]
    assert 'id="maintenance-config-hardware"' in card
    assert 'class="mconfig-hardware-list"' in card
    assert "Discover &amp; add hardware" in card
    assert "Start discovery" in card
    assert "Add manually" in card
    assert 'id="maintenance-config-features"' in card
    assert 'class="feature-list"' in card
    assert "Advanced / System settings" in card
    advanced = card.split('id="maintenance-config-advanced-section"', 1)[0]
    assert " open" not in advanced.rsplit("<details", 1)[-1]
    assert "maintenance-config-apply-hint" not in card


def test_js_maintenance_config_renders_setup_style_cards():
    js = _read("admin.js")
    hardware = js.split("function mconfigHardwareCard", 1)[1].split(
        "\nfunction ", 1
    )[0]
    assert "hardware-card hardware-card-" in hardware
    assert "hardware-card-status" in hardware
    assert "hardware-card-model" in hardware
    expanded = js.split("function mconfigSetExpanded", 1)[1].split("\nfunction ", 1)[0]
    assert "aria-expanded" in expanded
    feature = js.split("function renderMaintenanceFeatureSection", 1)[1].split(
        "\nfunction ", 1
    )[0]
    assert "feature-row mconfig-feature" in feature
    assert "feature-enable" in feature
    assert "feature-status" in feature
    assert "feature-desc" in feature
    assert "aria-controls" in feature


def test_hardware_and_feature_card_bodies_respect_hidden_state():
    css = _read("admin.css")
    assert ".hardware-card-body[hidden]" in css
    assert ".feature-body[hidden]" in css
    rule = css.split(".hardware-card-body[hidden]", 1)[1].split("}", 1)[0]
    assert "display: none" in rule


def test_maintenance_grid_meter_has_draft_only_remove_action():
    js = _read("admin.js")
    grid = js.split("function renderMaintenanceGridMeter", 1)[1].split(
        "\nfunction ", 1
    )[0]
    assert "onRemove:" in grid
    assert "present: false" in grid
    assert "renderMaintenanceGridMeter()" in grid
    assert "/api/" not in grid


def test_js_config_editor_uses_safe_dom_and_no_innerhtml():
    js = _read("admin.js")
    fn = js.split("function renderMaintenanceConfig", 1)[1].split(
        "\n// --- ", 1
    )[0]
    assert "innerHTML" not in fn
    # dynamic labels/paths/values go through textContent
    assert "textContent" in js.split("function renderMaintenanceConfigChange", 1)[1][:600]


def test_js_config_preview_posts_no_custom_path():
    js = _read("admin.js")
    fn = js.split("async function previewMaintenanceConfig", 1)[1].split(
        "\nfunction ", 1
    )[0]
    assert "/api/admin/maintenance/config/preview" in fn
    assert "draft: mconfigState.draft" in fn
    assert "path:" not in fn


def test_js_config_reset_restores_pristine_draft():
    js = _read("admin.js")
    fn = js.split("function resetMaintenanceConfigDraft", 1)[1].split(
        "\nif (", 1
    )[0]
    assert "mconfigState.pristine" in fn
    assert "renderMaintenanceInverters()" in fn


def test_maintenance_discovery_is_first_class_review_workflow():
    html = _read("index.html")
    card = html.split('id="maintenance-config-card"', 1)[1].split("</section>", 1)[0]
    assert 'id="maintenance-config-discovery"' in card
    assert 'id="maintenance-discovery-results"' in card
    assert "Close result" in card
    assert ">Cancel</button>" not in card
    assert "Nothing is written until you review and apply the draft." in card
    js = _read("admin.js")
    start = js.split("async function startMaintenanceDiscovery", 1)[1].split(
        "\nasync function ", 1
    )[0]
    assert "if (!mconfigState.loaded)" in start
    assert "await loadMaintenanceConfig()" in start
    assert 'fetch("/api/discovery/mdns/refresh"' in start
    assert 'fetch("/api/discovery/networks"' in start
    assert 'fetch("/api/discovery/scan"' in js
    assert '"/api/discovery/result/"' in js
    assert "buildMaintenanceDiscoveryReview" in start
    assert "/api/admin/maintenance/config/apply" not in start

    close = js.split("function closeMaintenanceDiscovery", 1)[1].split(
        "\nasync function ", 1
    )[0]
    assert "loadMaintenanceConfig" not in close

    manual = js.split("async function addManualMaintenanceInverter", 1)[1].split(
        "\n// --- ", 1
    )[0]
    assert "if (!mconfigState.loaded)" in manual
    assert "await loadMaintenanceConfig()" in manual


def test_maintenance_discovery_matches_serial_before_ip_and_keeps_missing():
    js = _read("admin.js")
    match = js.split("function mconfigFindInverterMatch", 1)[1].split(
        "\nfunction ", 1
    )[0]
    assert match.index("serial_number") < match.index("configured.ip")
    review = js.split("function buildMaintenanceDiscoveryReview", 1)[1].split(
        "\nfunction ", 1
    )[0]
    assert "mconfigState.draft" in review
    assert "mconfigState.pristine" not in review
    assert 'state: "missing"' in review
    assert 'state: ipChanged ? "conflict" : "found"' in review
    add = js.split("function mconfigAddDiscovered", 1)[1].split(
        "\nfunction ", 1
    )[0]
    assert "mconfigIdentity(device.sn) === serial" in add
    assert "port: found.port ||" not in add
    assert 'gridMeter.port = found.port' in add


def test_maintenance_discovery_always_renders_summary_and_pending_status():
    js = _read("admin.js")
    render = js.split("function renderMaintenanceDiscoveryReview", 1)[1].split(
        "\nlet mconfigDiscovering", 1
    )[0]
    card = js.split("function renderMaintenanceDiscoveryCard", 1)[1].split(
        "\nfunction renderMaintenanceDiscoveryReview", 1
    )[0]
    assert "Discovery completed" in render
    assert "Configured:" in render
    assert "No supported devices found." in render
    assert "No new devices found." in render
    assert "Configured devices were kept in the draft." in render
    assert "textContent" in render
    assert "innerHTML" not in render

    add = js.split("function mconfigAddDiscovered", 1)[1].split(
        "\nfunction ", 1
    )[0]
    review = js.split("function renderMaintenanceDiscoveryReview", 1)[1].split(
        "\nlet mconfigDiscovering", 1
    )[0]
    assert "mconfigMarkDraftChanged" in add
    assert "mconfigMarkDraftChanged" in card


def test_maintenance_config_global_preview_button_remains_bottom_action():
    html = _read("index.html")
    card = html.split('id="maintenance-config-card"', 1)[1].split("</section>", 1)[0]
    assert 'id="maintenance-config-preview-btn"' in card
    assert "Preview changes" in card


def test_maintenance_discovery_cards_keep_add_actions_not_local_preview():
    js = _read("admin.js")
    card = js.split("function renderMaintenanceDiscoveryCard", 1)[1].split(
        "\nfunction renderMaintenanceDiscoveryReview", 1
    )[0]
    review = js.split("function renderMaintenanceDiscoveryReview", 1)[1].split(
        "\nlet mconfigDiscovering", 1
    )[0]

    assert '"device-card mconfig-discovery-device-card"' in card
    assert '"device-role " + discoveryRoleClass(role)' in card
    assert '"mconfig-discovery-add-button " + actionState.cssClass' in card
    assert "mconfigDiscoveryActionState(item)" in card
    assert "mconfigAddDiscovered(item)" in card
    assert "Update draft" in js
    assert "Added to draft" in js
    assert "In config" in js

    assert "previewMaintenanceConfig" not in review
    assert "mconfig-discovery-next" not in review


def test_maintenance_discovery_disabled_add_buttons_are_scoped():
    css = _read("admin.css")
    assert ".mconfig-discovery-add-button:disabled" in css
    assert ".mconfig-discovery-add-button.is-in-config:disabled" in css
    assert ".mconfig-discovery-add-button.is-added:disabled" in css
    assert ".mconfig-discovery-add-button.is-configured-missing:disabled" in css


def test_maintenance_discovery_reuses_setup_device_cards_and_badges():
    js = _read("admin.js")
    css = _read("admin.css")
    card = js.split("function renderMaintenanceDiscoveryCard", 1)[1].split(
        "\nfunction renderMaintenanceDiscoveryReview", 1
    )[0]
    review = js.split("function renderMaintenanceDiscoveryReview", 1)[1].split(
        "\nlet mconfigDiscovering", 1
    )[0]

    assert '"device-card mconfig-discovery-device-card"' in card
    assert '"device-role " + discoveryRoleClass(role)' in card
    assert '"device-sources"' in card
    assert "mconfigAppendSourceBadges" in card
    assert '"device-facts"' in card
    assert '"device-card-foot"' in card
    assert '"results-list mconfig-discovery-grid"' in review
    assert "Labels:" not in review
    assert ".mconfig-discovery-device-card[data-state=\"conflict\"]" in css


def _run_maintenance_discovery_node(setup):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for the maintenance discovery behavior test")
    js = _read("admin.js")
    helpers = "\n".join(
        _extract_fn(js, name)
        for name in (
            "deviceKey",
            "isConfigCandidate",
            "mconfigIdentity",
            "mconfigDiscoveryRole",
            "mconfigFindInverterMatch",
            "buildMaintenanceDiscoveryReview",
        )
    )
    result = subprocess.run(
        [node, "-e", helpers + "\n" + setup],
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_maintenance_discovery_behavior_preserves_missing_and_deduplicates_serial():
    result = _run_maintenance_discovery_node(
        """
const pristine = {
  devices: [
    {name: "WR1", ip: "192.168.1.77", sn: "AAA"},
    {name: "WR2", ip: "192.168.1.78", sn: "BBB"},
  ],
  grid_meter: {present: true, type: "shelly", ip: "192.168.1.50"},
};
const mconfigState = {
  pristine: {devices: [], grid_meter: {}},
  draft: JSON.parse(JSON.stringify(pristine)),
};
const before = JSON.stringify(mconfigState.draft);
const discovered = [
  {
    id: "zendure:AAA", api_family: "zendure", role_suggestion: "inverter",
    ip: "192.168.1.81", serial_number: "AAA",
  },
  {
    id: "zendure:CCC", api_family: "zendure", role_suggestion: "inverter",
    ip: "192.168.1.82", serial_number: "CCC",
  },
  {
    id: "shelly:meter", api_family: "shelly", role_suggestion: "grid_meter",
    ip: "192.168.1.50",
  },
];
const review = buildMaintenanceDiscoveryReview(discovered);
console.log(JSON.stringify({
  states: review.map((item) => item.state),
  conflictName: review.find((item) => item.state === "conflict").configured.name,
  conflictIp: review.find((item) => item.state === "conflict").discovered.ip,
  unchanged: before === JSON.stringify(mconfigState.draft),
}));
"""
    )
    assert result["states"] == ["conflict", "missing", "found", "new"]
    assert result["conflictName"] == "WR1"
    assert result["conflictIp"] == "192.168.1.81"
    assert result["unchanged"] is True


def test_maintenance_discovery_grid_meter_without_port_omits_port():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for the maintenance discovery behavior test")
    js = _read("admin.js")
    helpers = "\n".join(
        _extract_fn(js, name)
        for name in (
            "mconfigIdentity",
            "mconfigMarkDraftChanged",
            "mconfigAddDiscovered",
        )
    )
    script = (
        "function gridMeterType() { return 'shelly'; }\n"
        "function renderMaintenanceGridMeter() {}\n"
        "function renderMaintenanceInverters() {}\n"
        "function setMaintenanceFact() {}\n"
        "const mconfigEls = {result: null, applyPanel: null, discoveryStatus: null, summary: null};\n"
        "const mconfigState = {draft: {devices: [], grid_meter: {}}, previewFingerprint: null};\n"
        + helpers
        + """
const item = {
  role: "grid_meter",
  discovered: {ip: "192.168.1.50", api_family: "shelly_gen2"},
};
mconfigAddDiscovered(item);
console.log(JSON.stringify(mconfigState.draft.grid_meter));
"""
    )
    result = subprocess.run(
        [node, "-e", script],
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    meter = json.loads(result.stdout)
    assert meter["ip"] == "192.168.1.50"
    assert "port" not in meter


def test_maintenance_apply_requires_preview_confirmation_and_offers_backup():
    html = _read("index.html")
    assert 'id="maintenance-config-backup" checked' in html
    assert 'id="maintenance-config-apply-btn"' in html
    js = _read("admin.js")
    apply = js.split("async function applyMaintenanceConfig", 1)[1].split(
        "\nif (mconfigEls.applyBtn)", 1
    )[0]
    assert "previewFingerprint" in apply
    assert "window.confirm" in apply
    assert "/api/admin/maintenance/config/apply" in apply
    assert "confirm: true" in apply


def test_shared_discovery_sessions_merge_sources_and_isolate_modes():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for the shared discovery behavior test")
    js = _read("admin.js")
    helpers = "\n".join(
        _extract_fn(js, name)
        for name in (
            "createDiscoverySession",
            "discoveryDeviceType",
            "discoveryDeviceMatch",
            "normalizeDiscoverySource",
            "mergeDiscoveryDevice",
        )
    )
    script = (
        "function sourcesOf(device) { return device.sources || [device.source || 'network_scan']; }\n"
        "function deviceKey(device) { return device.serial_number || device.ip; }\n"
        + helpers
        + """
const setup = createDiscoverySession("setup");
const maintenance = createDiscoverySession("maintenance");
mergeDiscoveryDevice(setup, {
  source: "mdns", serial_number: "ABC", ip: "192.168.1.20",
  device_type: "inverter", display_name: "Device"
}, "mdns");
mergeDiscoveryDevice(setup, {
  source: "network_scan", serial_number: "ABC", ip: "192.168.1.21",
  device_type: "inverter"
}, "manual_scan");
console.log(JSON.stringify({
  setupSize: setup.devices.size,
  maintenanceSize: maintenance.devices.size,
  device: Array.from(setup.devices.values())[0]
}));
"""
    )
    result = subprocess.run(
        [node, "-e", script], text=True, capture_output=True, check=False
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["setupSize"] == 1
    assert payload["maintenanceSize"] == 0
    assert payload["device"]["ip"] == "192.168.1.21"
    assert payload["device"]["sources"] == ["mdns", "manual_scan"]


def test_manual_scan_validation_accepts_host_and_rejects_large_range():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for the manual scan validation test")
    js = _read("admin.js")
    helper = _extract_fn(js, "validateManualScanInput")
    script = helper + """
console.log(JSON.stringify([
  validateManualScanInput("192.168.178.81"),
  validateManualScanInput("192.168.178.81/24"),
  validateManualScanInput("192.168.178.0/23"),
  validateManualScanInput("")
]));
"""
    result = subprocess.run(
        [node, "-e", script], text=True, capture_output=True, check=False
    )
    assert result.returncode == 0, result.stderr
    values = json.loads(result.stdout)
    assert values[0]["cidr"] == "192.168.178.81/32"
    assert values[1]["cidr"] == "192.168.178.0/24"
    assert "/24 or smaller" in values[2]["error"]
    assert "IPv4 address" in values[3]["error"]


def test_setup_and_maintenance_expose_shared_discovery_controls():
    html = _read("index.html")
    js = _read("admin.js")
    for marker in (
        'id="setup-discovery-reset"',
        'id="setup-discovery-progress"',
        'id="maintenance-discovery-manual-form"',
        'id="maintenance-discovery-reset"',
        'id="maintenance-discovery-progress"',
    ):
        assert marker in html
    assert "queueDiscoveryScans(" in js
    assert "discoverySessions.setup" in js
    assert "discoverySessions.maintenance" in js


def test_index_has_container_sync_post_apply_panel():
    html = _read("index.html")
    for marker in (
        'id="maintenance-config-post-apply"',
        'id="maintenance-post-ems-desired"',
        'id="maintenance-post-influx-desired"',
        'id="maintenance-post-action-summary"',
        'id="maintenance-containers-sync"',
        'id="maintenance-containers-recheck"',
        'id="maintenance-post-diagnostics"',
        'id="maintenance-containers-sync-status"',
    ):
        assert marker in html, marker
    assert "Restart / sync containers" in html


def test_index_runtime_containers_has_plan_and_action_hosts():
    html = _read("index.html")
    for marker in (
        'id="maintenance-runtime-container-actions"',
        'id="maintenance-runtime-ems-desired"',
        'id="maintenance-runtime-influx-desired"',
        'id="maintenance-runtime-action-summary"',
        'id="maintenance-runtime-containers-sync"',
        'id="maintenance-runtime-containers-recheck"',
        'id="maintenance-runtime-diagnostics"',
        'id="maintenance-runtime-containers-status"',
    ):
        assert marker in html, marker
    # The Runtime containers section (02) carries its own plan/action host so the
    # sync plan is visible without applying config first.
    containers = html.split('id="maintenance-containers"', 1)[1].split(
        "</section>", 1
    )[0]
    assert 'id="maintenance-runtime-container-actions"' in containers
    assert "Restart / sync containers" in containers


def test_runtime_container_card_has_display_detail_hosts():
    html = _read("index.html")
    containers = html.split('id="maintenance-containers"', 1)[1].split(
        "</section>", 1
    )[0]
    for marker in ("maintenance-ems-detail", "maintenance-influx-detail"):
        assert 'id="' + marker + '"' in containers


def test_js_runtime_service_status_uses_display_label_and_detail():
    js = _read("admin.js")
    fn = js.split("function renderRuntimeServiceStatus", 1)[1].split(
        "\nfunction ", 1
    )[0]
    # Main labels come from the derived display fields, not raw container state.
    assert "ems.display_label" in fn
    assert "ems.display_detail" in fn
    assert "influx.display_label" in fn
    assert "influx.display_detail" in fn
    # The collapsed summary is driven by the plan's status_summary.
    assert "plan.status_summary" in fn
    # Values are written via the textContent setter, never innerHTML.
    assert "setMaintenanceFact" in fn
    assert "innerHTML" not in fn


def test_js_disabled_influx_does_not_render_raw_missing_label():
    js = _read("admin.js")
    # The overview renderer no longer maps InfluxDB to a raw "missing" label; the
    # feature-aware display comes from the plan.
    render = js.split("function renderMaintenance", 1)[1].split(
        "\nfunction renderMaintenanceError", 1
    )[0]
    assert "maintenanceContainerFact" not in render
    assert "maintenanceEls.influx," not in render


def test_js_post_apply_panel_uses_service_desired_state():
    js = _read("admin.js")
    fn = js.split("function renderContainerPlanInto", 1)[1].split(
        "\n// ", 1
    )[0]
    # Both hosts (post-apply + runtime) render desired from the derived service
    # display fields so the two panels stay consistent.
    assert "plan.services" in fn
    assert "desired_state" in fn


def test_js_uses_container_plan_and_sync_endpoints():
    js = _read("admin.js")
    assert "/api/admin/maintenance/containers/plan" in js
    assert "/api/admin/maintenance/containers/sync" in js
    assert "loadMaintenanceContainerPlan(" in js
    assert "syncMaintenanceContainers(" in js


def test_js_container_plan_loads_with_maintenance_overview():
    js = _read("admin.js")
    # The plan is no longer post-apply-only: the overview load flow refreshes it.
    overview = js.split("async function loadMaintenanceOverview", 1)[1].split(
        "\nfunction ", 1
    )[0]
    assert "loadMaintenanceContainerPlan(" in overview


def test_js_runtime_and_post_apply_sync_buttons_are_wired():
    js = _read("admin.js")
    # Both hosts drive the shared sync handler.
    assert 'maintenanceEls.runtimeContainersSync.addEventListener("click"' in js
    assert 'mconfigEls.containersSync.addEventListener("click"' in js
    assert "syncMaintenanceContainers(" in js


def test_js_config_apply_defers_diagnostics_until_after_container_sync():
    js = _read("admin.js")
    apply_start = js.find("async function applyMaintenanceConfig")
    apply_end = js.find("const CONTAINER_SYNC_LABEL")
    assert apply_start != -1 and apply_end != -1 and apply_start < apply_end
    apply_body = js[apply_start:apply_end]
    # The config apply flow must not auto-run diagnostics before the container
    # sync (they would otherwise hit the old container/config).
    assert "runDiagnostics()" not in apply_body
    # After a real write (changed === true) the guided completion block is
    # revealed; a no-op apply (changed === false) hides it instead.
    assert "loadMaintenanceContainerPlan({ showPostApply: true })" in apply_body
    assert "data.changed === false" in apply_body
    assert "No config changes were written. Container restart is not required." in apply_body
    assert "mconfigEls.postApply.hidden = true" in apply_body


def test_js_maintenance_overview_is_deterministic():
    js = _read("admin.js")
    overview = js.split("async function loadMaintenanceOverview", 1)[1].split(
        "\nif (maintenanceEls.refresh)", 1
    )[0]
    # Options gate the follow-up refreshes; defaults preserve the old behavior.
    assert "options.refreshConfig !== false" in overview
    assert "options.refreshContainerPlan !== false" in overview
    assert "options.showPostApply === true" in overview
    # Follow-ups are awaited so a late config reload cannot hide the post-apply
    # panel after it was revealed.
    assert "await loadMaintenanceConfig()" in overview
    assert "await loadMaintenanceContainerPlan({ showPostApply })" in overview
    # No unawaited fire-and-forget reloads remain.
    assert "\n  loadMaintenanceConfig();" not in overview
    assert "loadMaintenanceContainerPlan({ showPostApply: false })" not in overview


def test_js_config_apply_does_not_reset_post_apply_via_overview():
    js = _read("admin.js")
    apply_start = js.find("async function applyMaintenanceConfig")
    apply_end = js.find("const CONTAINER_SYNC_LABEL")
    apply_body = js[apply_start:apply_end]
    # The success path refreshes overview facts only; config + container plan are
    # driven explicitly so the guided post-apply panel is not reset/hidden.
    assert "refreshConfig: false" in apply_body
    assert "refreshContainerPlan: false" in apply_body
    # A bare overview reload (old behavior) would re-run renderMaintenanceConfig
    # and hide the panel.
    assert "await loadMaintenanceOverview();" not in apply_body
    # The changed === true branch keeps the panel visible.
    assert "mconfigEls.postApply.hidden = false" in apply_body


def test_js_container_recheck_and_sync_preserve_config_view():
    js = _read("admin.js")
    # Rechecks and syncs refresh facts only and reload the plan themselves, so a
    # visible post-apply / config-result view is not reset by renderMaintenanceConfig.
    for marker in (
        'mconfigEls.containersRecheck.addEventListener("click"',
        'maintenanceEls.runtimeContainersRecheck.addEventListener("click"',
    ):
        block = js.split(marker, 1)[1].split("});", 1)[0]
        assert "refreshConfig: false" in block
        assert "refreshContainerPlan: false" in block
    sync = _extract_fn(js, "syncMaintenanceContainers")
    assert "refreshConfig: false" in sync
    assert "refreshContainerPlan: false" in sync
    # The post-apply host stays shown across a sync when it was already visible.
    assert "keepPostApply" in sync


def test_js_container_sync_reports_reason_per_host():
    js = _read("admin.js")
    sync = _extract_fn(js, "syncMaintenanceContainers")
    # The shared sync helper forwards the caller's reason to the mutating endpoint.
    assert "confirm: true, reason }" in sync
    # The post-apply host reports config_apply; the runtime host reports manual.
    assert (
        'syncMaintenanceContainers(mconfigEls.containersSyncStatus, "config_apply")' in js
    )
    assert (
        'syncMaintenanceContainers(maintenanceEls.runtimeContainersStatus, "manual")' in js
    )


def test_js_container_sync_avoids_forbidden_commands():
    js = _read("admin.js")
    html = _read("index.html")
    for forbidden in ("down -v", "docker rm -v", "clean install", "Reinstall", "Reset stack"):
        assert forbidden not in js, forbidden
        assert forbidden not in html, forbidden


def test_js_container_sync_renders_influx_schema_step():
    js = _read("admin.js")
    # The sync result surfaces the returned steps, including the InfluxDB schema sync.
    assert '"influxdb:sync": "InfluxDB sync"' in js
    fmt = _extract_fn(js, "formatContainerSyncSteps")
    assert "step.service" in fmt and "step.action" in fmt and "step.status" in fmt
    sync = _extract_fn(js, "syncMaintenanceContainers")
    assert "formatContainerSyncSteps(data.steps)" in sync


def test_js_container_sync_renders_all_expected_step_labels():
    js = _read("admin.js")
    for label in ("InfluxDB init", "InfluxDB start", "InfluxDB sync", "EMS recreate"):
        assert label in js, label


# --- backup / restore ------------------------------------------------------

def test_backup_restore_placeholder_is_replaced():
    html = _read("index.html")
    assert "planned for a follow-up task" not in html
    panel = html.split('id="maintenance-backup-panel"', 1)[1].split(
        'id="view-advanced"', 1
    )[0]
    assert 'id="backup-create"' in panel
    assert 'id="backup-list"' in panel


def test_backup_restore_uses_control_stage_style():
    html = _read("index.html")
    panel = html.split('id="maintenance-backup-panel"', 1)[1].split(
        'id="view-advanced"', 1
    )[0]
    assert "control-pipeline-stage" in panel
    assert "control-stage-title" in panel
    assert "control-stage-step" in panel
    # No new card world is introduced for backups.
    assert "backup-card-world" not in panel


def test_backup_restore_buttons_and_sections_exist():
    html = _read("index.html")
    for element_id in (
        "backup-create",
        "backup-refresh",
        "backup-preview",
        "backup-execute",
        "backup-details-stage",
        "backup-restore-stage",
    ):
        assert 'id="' + element_id + '"' in html, element_id
    # Per-backup Details/Restore/Delete actions are rendered by JS.
    js = _read("admin.js")
    for action in ('data-backup-action="details"', 'data-backup-action="restore"',
                   'data-backup-action="delete"'):
        assert action in js, action


def test_backup_restore_has_no_conflict_policy_selector():
    html = _read("index.html")
    restore_stage = html.split('id="backup-restore-stage"', 1)[1].split(
        'id="advanced-deployment"', 1
    )[0]
    assert 'id="backup-conflict-policy"' not in html
    assert "Existing files" not in restore_stage
    assert "Keep existing files" not in html
    assert "Abort on conflicts" not in html


def test_previewRestore_sends_replace_conflict_policy():
    js = _read("admin.js")
    fn = _extract_fn(js, "previewRestore")
    assert 'conflict_policy: "replace"' in fn
    assert "backupEls.conflictPolicy" not in fn
    assert "/api/admin/maintenance/backups/restore/preview" in fn


def test_executeRestore_requires_confirmation_and_confirm_flag():
    js = _read("admin.js")
    fn = _extract_fn(js, "executeRestore")
    assert "window.confirm(" in fn
    assert "/api/admin/maintenance/backups/restore/execute" in fn
    assert "confirm: true" in fn
    assert "plan.blocked" in fn


def test_backup_restore_frontend_references_expected_endpoints():
    js = _read("admin.js")
    for endpoint in (
        "/api/admin/maintenance/backups",
        "/api/admin/maintenance/backups/create",
        "/api/admin/maintenance/backups/jobs/",
        "/api/admin/maintenance/backups/inspect",
        "/api/admin/maintenance/backups/restore/preview",
        "/api/admin/maintenance/backups/restore/execute",
        "/api/admin/maintenance/backups/delete",
    ):
        assert endpoint in js, endpoint


def test_backup_restore_dynamic_rendering_uses_escape_html():
    js = _read("admin.js")
    # The shared helpers are the escaping choke points for backup markup.
    for fn_name in ("backupValidationItem", "backupFact"):
        assert "escapeHtml(" in _extract_fn(js, fn_name), fn_name
    # Row/detail renderers escape dynamic names/paths directly; job steps route
    # their dynamic text through the escaping helper.
    for fn_name in ("renderBackupRow", "renderBackupDetails"):
        assert "escapeHtml(" in _extract_fn(js, fn_name), fn_name
    assert "backupValidationItem(" in _extract_fn(js, "renderBackupJobSteps")


def test_backup_restore_does_not_downgrade_docker_image():
    js = _read("admin.js")
    backup_block = js.split("// --- backup / restore", 1)[1].split(
        "// --- EMS diagnostics", 1
    )[0]
    for forbidden in ("docker pull", "compose", "image:"):
        assert forbidden not in backup_block, forbidden


def test_backup_influxdb_row_disables_restore_but_keeps_details_and_delete():
    js = _read("admin.js")
    row = _extract_fn(js, "renderBackupRow")
    # InfluxDB rows are never hidden: Details and Delete stay available.
    assert 'data-backup-action="details"' in row
    assert 'data-backup-action="delete"' in row
    # Restore preview is disabled for InfluxDB archives, with a marker so the
    # busy-state toggle cannot silently re-enable it.
    assert 'backup.backup_type === "influxdb"' in row
    assert 'data-backup-restore-disabled="true"' in row
    # The warning tells the user to use the EMS CLI instead.
    assert "InfluxDB restore not supported in Admin yet" in row
    assert "EMS CLI" in row


def test_backup_set_with_influxdb_member_disables_restore_preview():
    js = _read("admin.js")
    row = _extract_fn(js, "renderBackupSetRow")
    assert 'a.type === "influxdb"' in row
    assert 'data-backup-restore-disabled="true"' in row
    assert "Admin restore not supported yet" in row


def test_backup_busy_state_keeps_unsupported_restore_buttons_disabled():
    js = _read("admin.js")
    busy = _extract_fn(js, "setBackupBusy")
    assert "backupRestoreDisabled" in busy


def test_backup_list_uses_compact_rows_not_tiles():
    js = _read("admin.js")
    css = _read("admin.css")
    for fn_name in ("renderBackupRow", "renderBackupSetRow"):
        markup = _extract_fn(js, fn_name)
        assert 'class="backup-row' in markup, fn_name
        assert "backup-item" not in markup, fn_name
    assert 'class="backup-row backup-row-set"' in _extract_fn(js, "renderBackupSetRow")
    for selector in (".backup-row", ".backup-row-set", ".backup-row-meta",
                     ".backup-row-fact", ".backup-row-actions", ".backup-row-flags"):
        assert selector in css, selector
    assert ".backup-item" not in css


def test_backup_list_preserves_list_roles():
    html = _read("index.html")
    js = _read("admin.js")
    # The list container keeps role="list"; rows keep role="listitem".
    assert '<div id="backup-list" class="backup-list" role="list">' in html
    for fn_name in ("renderBackupRow", "renderBackupSetRow"):
        assert 'role="listitem"' in _extract_fn(js, fn_name), fn_name


def test_backup_row_shows_ems_version_and_build():
    js = _read("admin.js")
    row = _extract_fn(js, "renderBackupRow")
    assert 'backupRowFact("EMS", backup.source_version' in row
    assert "backup.source_build || backup.source_commit" in row
    # Dynamic values still flow through the escaping fact helper.
    assert "escapeHtml(" in _extract_fn(js, "backupRowFact")
