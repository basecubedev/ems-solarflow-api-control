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


def test_index_renders_two_top_level_tabs():
    html = _read("index.html")
    assert '<nav class="admin-view-tabs"' in html
    assert ">Setup<" in html
    for view in ("setup", "advanced"):
        assert 'data-admin-view="' + view + '"' in html
    # The old five-tab layout is gone: Discovery and Config are not primary tabs.
    assert 'data-admin-view="discovery"' not in html
    assert 'data-admin-view="config"' not in html


def test_setup_tab_is_default_and_active():
    html = _read("index.html")
    # The Setup tab button is the pre-selected one.
    assert '<button type="button" class="active" data-admin-view="setup"' in html
    # Its panel is visible (not hidden); the advanced panel starts hidden.
    assert (
        '<div class="admin-view" id="view-setup" data-admin-view-panel="setup">' in html
    )
    assert 'data-admin-view-panel="advanced" hidden' in html


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


def test_config_draft_cards_have_compact_identity_headers():
    js = _read("admin.js")
    css = _read("admin.css")
    card = js.split("function renderConfigDraftCard", 1)[1].split(
        "\nfunction ", 1
    )[0]

    assert '"Grid meter" : "Inverter " + (inverterIndex + 1)' in card
    assert "config-draft-identity" in card
    assert "config-draft-kind" in card
    assert "config-draft-title" in card
    assert "escapeHtml(title)" in card
    assert ".config-draft-card" in css
    assert "padding: 10px" in css.split(".config-draft-card", 1)[1][:500]


def test_config_device_metadata_is_collapsed_but_preserved():
    js = _read("admin.js")
    css = _read("admin.css")
    card = js.split("function renderConfigDraftCard", 1)[1].split(
        "\nfunction ", 1
    )[0]

    assert '<details class="config-device-details">' in card
    assert "<summary><span>Device details</span>" in card
    assert "config-device-meta-preview" in card
    for label in ("IP", "Port", "Serial", "Type", "API family", "Source"):
        assert f'"{label}"' in card
    assert '<details class="config-device-details" open' not in card
    assert ".config-device-details-grid" in css
    assert "config-draft-readonly" not in card


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
    draft = js.split("renderConfigDraftCard", 1)[1]
    assert "escapeHtml(item.config_name)" in draft
    assert "escapeHtml(item.display_name)" in draft
    assert "escapeHtml(item.serial_number)" in draft


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
    assert "selectGridMeter(button.getAttribute" in js
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
