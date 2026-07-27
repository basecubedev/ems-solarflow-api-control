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


def test_admin_js_has_valid_syntax_when_node_is_available():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not available")
    result = subprocess.run(
        [node, "--check", os.path.join(STATIC_DIR, "admin.js")],
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


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


def test_index_has_no_top_level_advanced_view():
    html = _read("index.html")
    # The obsolete top-level Advanced placeholder view has been removed; only the
    # nested advanced-details/feature-advanced sections remain valid.
    assert 'id="view-advanced"' not in html
    assert 'data-admin-view-panel="advanced"' not in html
    assert 'id="advanced-deployment"' not in html
    assert 'id="advanced-system"' not in html
    assert 'id="advanced-network"' not in html
    assert "Planned for next phase" not in html


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


def test_add_more_devices_summary_is_a_prominent_menu_row():
    # "Add more devices" is a primary hardware-add entry point in both the setup
    # and maintenance config steps. It must read as a clear, taller menu row
    # rather than a small advanced-details toggle, while staying collapsible.
    css = _read("admin.css")
    rule = css.split(".add-devices-details > summary {", 1)[1].split("}", 1)[0]
    # Larger text and extra vertical padding make the row taller and clearer.
    assert "font-size: 13px" in rule
    assert "padding: 9px" in rule
    assert "var(--text)" in rule
    # Both flows share the same menu-row class so they cannot drift apart.
    html = _read("index.html")
    setup_tag = html.split('id="config-available-details"', 1)[0].rsplit("<details", 1)[1]
    maintenance_tag = html.split('id="maintenance-add-devices"', 1)[0].rsplit(
        "<details", 1
    )[1]
    assert "add-devices-details" in setup_tag
    assert "add-devices-details" in maintenance_tag


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


def test_index_has_single_primary_discovery_action_and_default_keep_results():
    html = _read("index.html")
    # The redundant header scan actions are gone; Run discovery is the only
    # primary discovery button.
    assert 'id="networks-scan-all"' not in html
    assert "Scan all" not in html
    assert 'id="networks-refresh"' not in html
    assert "Start discovery" not in _read("index.html").split(
        'aria-label="Devices"', 1
    )[1].split('aria-label="Config"', 1)[0]
    # Keep previous results is enabled by default.
    assert '<input id="results-accumulate" type="checkbox" checked>' in html


def test_js_run_discovery_scans_every_lan_network():
    js = _read("admin.js")
    assert "function lanCidrs" in js
    # Run discovery scans all detected LAN networks in one run via runInitialScan.
    assert "function runInitialScan" in js
    run_fn = js.split("function runInitialScan", 1)[1].split("\nfunction ", 1)[0]
    assert "runScans(cidrs)" in run_fn
    # Docker networks are excluded from the combined LAN scan.
    fn = js.split("function lanCidrs", 1)[1].split("\nfunction ", 1)[0]
    assert "!net.is_docker_like" in fn


def test_js_auto_scans_all_networks_after_discovery():
    js = _read("admin.js")
    assert "function runInitialScan" in js
    # Running discovery chains into an automatic scan of all LAN networks via the
    # shared network-scan driver.
    run_fn = js.split("async function runUnifiedDiscovery", 1)[1].split(
        "\nasync function ", 1
    )[0]
    assert "detectAndScanNetworks(false)" in run_fn
    detect_fn = js.split("async function detectAndScanNetworks", 1)[1].split(
        "\nasync function ", 1
    )[0]
    assert "loadNetworks()" in detect_fn
    assert "settleNetworkScans()" in detect_fn
    # The settle loop launches a scan for every LAN network not yet scanned.
    settle_fn = js.split("function settleNetworkScans", 1)[1].split(
        "\n// Detect networks", 1
    )[0]
    assert "runInitialScan()" in settle_fn
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
    # First open auto-runs the full discovery (same as the Run discovery button),
    # after the preparation is loaded.
    assert "runUnifiedDiscovery()" in fn
    assert "loadDiscoveryPreparation()" in fn
    # The old unconditional startup scan is gone.
    assert "\nloadNetworks().then(runInitialScan);" not in js


def test_scan_buttons_show_busy_state_during_scan():
    js = _read("admin.js")
    css = _read("admin.css")
    # The manual scan button flags a visible busy state.
    assert 'classList.toggle("is-scanning", scanning)' in js
    # Run discovery and Scan networks show the same spinner while they run.
    assert 'els.discoveryRun.classList.add("is-scanning", "is-cancel")' in js
    assert 'els.networksScan.classList.add("is-scanning", "is-cancel")' in js
    assert '"Scanning…"' in js
    # The busy state is backed by a spinner animation.
    assert ".primary-button.is-scanning" in css
    assert "@keyframes admin-spin" in css


def test_discovery_actions_are_cancelable_toggle_buttons():
    html = _read("index.html")
    js = _read("admin.js")
    css = _read("admin.css")
    # A dedicated network-scan trigger exists alongside Run discovery.
    assert 'id="networks-scan"' in html
    assert "Scan networks" in html
    # Both primary actions toggle to a cancel while a run is active.
    assert '"Cancel discovery"' in js
    assert '"Cancel scan"' in js
    assert "function cancelActiveScans" in js
    # Cancel keeps already-found devices (session generation bump, not a reset).
    cancel_fn = js.split("function cancelActiveScans", 1)[1].split("\nfunction ", 1)[0]
    assert "session.generation += 1" in cancel_fn
    assert "devices.clear()" not in cancel_fn
    # The busy button doubles as a clearly-clickable cancel control.
    assert ".primary-button.is-cancel" in css


def test_js_run_discovery_waits_for_gateway_probe_before_settling():
    js = _read("admin.js")
    # loadNetworks marks detection active for its whole run (direct + gateway).
    load_fn = js.split("async function loadNetworks", 1)[1].split(
        "\nfunction ", 1
    )[0]
    assert "networkDetectionActive = true" in load_fn
    assert "networkDetectionActive = false" in load_fn
    assert "await loadGatewayNetworks()" in load_fn
    # settle only resolves once detection has finished and scans have drained, so
    # the run stays active while the gateway probe is still adding networks.
    settle_fn = js.split("function settleNetworkScans", 1)[1].split(
        "\n// Detect networks", 1
    )[0]
    assert "!networkDetectionActive && !scanning && !pending.length" in settle_fn


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
    assert "Local MQTT discovery" in html
    assert 'id="mqtt-list"' in html
    assert 'id="mqtt-refresh"' in html
    assert 'id="mqtt-probe"' not in html
    assert "checked automatically when you scan a" in html
    # The old "not implemented yet" placeholder must be gone everywhere.
    assert "not implemented yet" not in html
    assert "not implemented yet" not in js
    assert "/api/discovery/mqtt-brokers" in js
    assert "probeMqttNetworks(unique)" in js
    assert "escapeHtml(broker" in js


def test_ui_renders_mqtt_broker_hardware_candidates():
    js = _read("admin.js")
    # Broker groups render their own read-only hardware candidates.
    assert "renderMqttBrokerCard" in js
    assert "renderMqttDeviceCard" in js
    assert "No hardware topics found on this broker." in js
    # Every dynamic broker/device field is escaped before reaching innerHTML.
    assert "escapeHtml(device.display_name" in js
    assert "escapeHtml(device.topic_family" in js
    assert "escapeHtml(String(devices.length))" in js
    # Detail panels stay collapsed by default; the unified overview is the
    # primary result view, so the MQTT panel never auto-opens on results.
    assert "els.mqttDetails.open = true" not in js


def test_ui_has_zendure_cloud_mqtt_discovery_section():
    html = _read("index.html")
    js = _read("admin.js")
    assert "Zendure MQTT discovery" in html
    assert 'id="zendure-cloud-details"' in html
    assert 'id="zendure-cloud-list"' in html
    # The section lives below Local MQTT discovery in the same setup panel.
    assert html.index("Local MQTT discovery") < html.index("Zendure MQTT discovery")
    # Read-only wording; never advertise config apply or publish here.
    assert "read-only and never" in html
    # The token input is masked and not prefilled from settings.
    token_input = html.split('id="zendure-cloud-token-input"', 1)[1].split(">", 1)[0]
    assert 'type="password"' in token_input
    assert "value=" not in token_input
    # UI talks to the new cloud endpoints only.
    assert '"/api/discovery/zendure-cloud-mqtt"' in js
    assert 'ZENDURE_CLOUD_BASE + "/settings"' in js
    assert 'ZENDURE_CLOUD_BASE + "/token"' in js
    assert 'ZENDURE_CLOUD_BASE + "/test"' in js
    assert 'ZENDURE_CLOUD_BASE + "/refresh"' in js


def test_ui_zendure_cloud_auto_detects_api_key_or_ha_token():
    html = _read("index.html")
    js = _read("admin.js")
    devices = _devices_section(html)
    panel = devices.split('id="zendure-cloud-token-form"', 1)[1].split("</form>", 1)[0]
    # Exactly one credential input; no mode selector is needed because the
    # backend safely auto-detects raw appKeys and Zendure HA/deviceList tokens.
    assert "Zendure API key" in html
    assert "HA/deviceList token" in html
    assert "Paste your Zendure API key or HA token" in html
    assert 'id="zendure-cloud-token-input"' in panel
    # No credential type dropdown or manual-MQTT wording in the cloud panel.
    assert "zendure-cloud-credential-mode" not in html
    assert "<select" not in panel
    assert "manual_mqtt_credentials" not in html
    # The shared credential input is enabled (no mode ever disables it).
    assert "disabled" not in panel
    assert "els.zendureCloudTokenInput.disabled" not in js
    # Save/test keep the compatible api_key field; credential shape is detected
    # server-side and no client-supplied mode is required.
    assert "api_key: apiKey" in js
    assert "credential_mode" not in js


def test_ui_zendure_cloud_never_renders_raw_token_and_escapes_values():
    js = _read("admin.js")
    # Settings response is applied via textContent only; the saved token is
    # never echoed back into the input (no prefill from settings).
    assert "applyZendureCloudSettings" in js
    assert "els.zendureCloudTokenInput.value = data" not in js
    # Dynamic cloud device values pass through escapeHtml before innerHTML.
    assert "renderZendureCloudDeviceCard" in js
    assert "escapeHtml(device.display_name" in js
    assert "escapeHtml(device.tls_mode" in js
    # Detail panels stay collapsed by default; the unified overview is the
    # primary result view, so the Zendure panel never auto-opens on results.
    assert "els.zendureCloudDetails.open = true" not in js


def test_discovery_source_panels_stay_collapsed_by_default():
    html = _read("index.html")
    js = _read("admin.js")
    devices = _devices_section(html)
    # None of the three source detail panels carry the `open` attribute, so a
    # populated panel (e.g. "Local API discovery 3 devices") shows a collapsed
    # header rather than expanding just because results exist.
    for panel_id in ("local-api-details", "mqtt-details", "zendure-cloud-details"):
        tag = devices.split('id="' + panel_id + '"', 1)[1].split(">", 1)[0]
        assert " open" not in tag, panel_id + " must be collapsed by default"
    # No result/count-driven auto-expansion of source detail panels remains.
    assert "els.mqttDetails.open = true" not in js
    assert "els.zendureCloudDetails.open = true" not in js
    assert "maybeAutoOpenLocalApi" not in js
    # Header counters/status pills still render for at-a-glance status.
    for count_id in ("local-api-count", "mqtt-count", "zendure-cloud-count"):
        assert 'id="' + count_id + '"' in devices


def test_ui_zendure_cloud_devices_render_separate_from_local_mqtt():
    js = _read("admin.js")
    # Cloud devices render into their own list, not the local broker list.
    assert "els.zendureCloudList.innerHTML" in js
    assert "renderZendureCloudDevices" in js
    # Local and cloud discovery keep separate state.
    assert "zendureCloudDevices" in js
    assert "loadZendureCloudSettings()" in js


def test_ui_has_zendure_mqtt_config_proposals_section():
    html = _read("index.html")
    js = _read("admin.js")
    # A dedicated read-only proposal panel lives below the discovery panels.
    assert 'id="mqtt-proposals-details"' in html
    assert 'id="mqtt-proposals-list"' in html
    assert 'id="mqtt-proposals-empty"' in html
    assert 'id="mqtt-proposals-count"' in html
    assert "Zendure MQTT config proposals" in html
    assert html.index("Zendure MQTT discovery") < html.index(
        "Zendure MQTT config proposals"
    )
    # It consumes the existing backend proposal endpoint only.
    assert "/api/discovery/mqtt-proposals" in js
    assert "loadMqttProposals" in js
    # MQTT is a first-class control transport: the intro must not claim every
    # MQTT device is telemetry-only, and must explain capability-based control.
    assert "same EMS control loop" in html
    assert "output control is enabled where a verified write method exists" in html
    assert "no MQTT control commands are ever sent" not in html


def test_ui_proposal_cards_show_safety_state_and_facts():
    js = _read("admin.js")
    card = js.split("function renderMqttProposalCard", 1)[1].split(
        "\nfunction ", 1
    )[0]
    # The card is capability-aware: a supported inverter advertises output
    # control, an unsupported family explains why control is unavailable.
    assert "Output control available" in card
    assert "Output control not available for this topic family" in card
    assert "output_control_supported" in card
    # The blanket "no MQTT control commands" claim must not apply to every card.
    assert "No MQTT control commands" not in card
    # Core proposal facts are surfaced. The raw topic family never appears as a
    # user-facing label; the friendly hardware generation is shown instead.
    assert 'fact("Role hint"' in card
    assert 'fact("Hardware generation"' in card
    assert 'fact("Topic family"' not in card
    assert 'fact("Capabilities"' in card
    assert 'fact("Metrics seen"' in card
    assert "proposal.confidence" in card
    # The config fragment stays collapsed inside a details/pre block.
    assert "proposal-fragment" in card
    assert "<details" in card


def test_ui_proposal_values_pass_through_escape_helper():
    js = _read("admin.js")
    card = js.split("function renderMqttProposalCard", 1)[1].split(
        "\nfunction ", 1
    )[0]
    # Every dynamic proposal value is escaped before reaching innerHTML.
    assert "escapeHtml(proposal.display_name" in card
    assert "escapeHtml(mqttGenerationLabel(proposal))" in card
    assert "escapeHtml(proposal.role_hint" in card
    assert "escapeHtml(capabilities.join" in card
    assert "escapeHtml(JSON.stringify(fragmentSource" in card
    assert "escapeHtml(String(proposal.confidence" in card


def test_ui_proposal_section_never_offers_config_write_actions():
    html = _read("index.html")
    js = _read("admin.js")
    proposal_html = html.split('id="mqtt-proposals-details"', 1)[1].split(
        "</details>", 1
    )[0]
    card = js.split("function renderMqttProposalCard", 1)[1].split(
        "\nfunction ", 1
    )[0]
    # No apply/restart/write affordance and no credential display in the preview.
    for banned in ("config-apply", "Apply to EMS", "Restart", "restart", "app_key", "token"):
        assert banned not in proposal_html
        assert banned not in card
    # The proposal loader is read-only: only the GET proposals endpoint.
    loader = js.split("async function loadMqttProposals", 1)[1].split(
        "\nasync function ", 1
    )[0]
    assert '"/api/discovery/mqtt-proposals"' in loader
    assert "method:" not in loader


def test_ui_proposal_failure_is_handled_gracefully():
    js = _read("admin.js")
    loader = js.split("async function loadMqttProposals", 1)[1].split(
        "\nasync function ", 1
    )[0]
    # A failed/malformed response shows a compact message and clears the list
    # instead of breaking discovery rendering.
    assert "Config proposals unavailable:" in loader
    assert "renderMqttProposals([])" in loader
    # Failure text is written via textContent, never innerHTML.
    assert "els.mqttProposalsMessage.textContent" in loader


def test_ui_proposal_card_offers_add_to_config_selection_action():
    js = _read("admin.js")
    card = js.split("function renderMqttProposalCard", 1)[1].split(
        "\nfunction ", 1
    )[0]
    # A compact add-to-preview action with the stable proposal id, escaped.
    assert "mqtt-proposal-add" in card
    assert "Add to config preview" in card
    assert "Added to preview" in card
    assert "escapeHtml(proposalId)" in card
    # The selected state states the resolved control role rather than a blanket
    # telemetry-only claim.
    assert "Output control enabled" in card


def test_ui_config_preview_and_export_both_send_proposals():
    js = _read("admin.js")
    preview_fn = js.split("async function requestConfigPreview", 1)[1].split(
        "\nfunction ", 1
    )[0]
    export_fn = js.split("function configExportBody", 1)[1].split(
        "\nfunction ", 1
    )[0]
    # Both the preview request and the export body carry the selected proposals.
    assert "zendure_mqtt_proposals: mqttPreviewPayload()" in preview_fn
    assert "zendure_mqtt_proposals: mqttPreviewPayload()" in export_fn


def test_ui_export_controls_allowed_with_telemetry_only_proposals_selected():
    js = _read("admin.js")
    allowed_fn = js.split("function configExportAllowed", 1)[1].split(
        "\nfunction ", 1
    )[0]
    continue_fn = js.split("async function continueFromConfig", 1)[1].split(
        "\nfunction ", 1
    )[0]
    # Export/continue readiness follows the backend preview alone: selecting a
    # telemetry-only MQTT proposal no longer blocks download/apply/continue.
    assert "hasMqttPreviewProposals()" not in allowed_fn
    assert "hasMqttPreviewProposals()" not in continue_fn


def test_ui_proposal_selection_payload_carries_no_secrets():
    js = _read("admin.js")
    # The one canonical serializer builds every stored/sent selection payload.
    serializer_fn = _extract_fn(js, "serializeMqttProposalSelection")
    # The trusted, secret-free metadata the backend re-validates is preserved.
    for field in ("id:", "target:", "topic_family:", "broker_ref:", "seen_topics:"):
        assert field in serializer_fn
    # No secret is ever placed on the payload.
    assert "physical_identity_token:" in serializer_fn
    for banned in ("app_key:", "password:", "username:"):
        assert banned not in serializer_fn
    assert "\n    token:" not in serializer_fn
    # The preview payload delegates to the one canonical serializer rather than
    # rebuilding a divergent payload shape.
    payload_fn = _extract_fn(js, "mqttPreviewPayload")
    assert "serializeMqttProposalSelection(" in payload_fn


def _setup_panel(html):
    return html.split('id="view-setup"', 1)[1].split('id="view-maintenance"', 1)[0]


def _config_section(html):
    return _setup_panel(html).split('aria-label="Config"', 1)[1]


def _devices_section(html):
    return _setup_panel(html).split('aria-label="Devices"', 1)[1].split(
        'aria-label="Config"', 1
    )[0]


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


def test_start_gate_has_exactly_two_choices():
    html = _read("index.html")
    assert html.count("data-start-path=") == 2
    assert 'data-start-path="setup_new"' in html
    assert 'data-start-path="manage_existing"' in html
    assert "Guided setup" in html
    assert "Maintenance" in html
    # Guided setup is the primary, first option.
    assert html.index('data-start-path="setup_new"') < html.index(
        'data-start-path="manage_existing"'
    )
    assert html.index("Guided setup") < html.index(
        '<span class="start-choice-title">Maintenance</span>'
    )
    # Docker bootstrap / developer setup stay documentation-only paths.
    assert "Docker bootstrap" not in html
    assert "Developer setup" not in html


def test_start_gate_paths_are_clickable_cards_without_next_button():
    html = _read("index.html")
    # The landing page opens each path directly from its card; there is no
    # separate submit/Next action between the cards and the docs hint.
    assert 'id="start-continue"' not in html
    assert ">Next<" not in html.split('id="view-setup"', 1)[0]
    # Both paths are real, keyboard-accessible button controls.
    assert (
        '<button type="button" class="start-choice start-choice-nav is-recommended" '
        'data-start-path="setup_new"' in html
    )
    assert (
        '<button type="button" class="start-choice start-choice-nav" '
        'data-start-path="manage_existing">' in html
    )
    # Each card carries a navigation affordance.
    assert html.count('class="start-choice-arrow"') == 2
    # The docs hint stays visible and secondary below the cards.
    assert "See the setup paths in the documentation." in html


def test_start_gate_cards_open_via_post_start_path_safety_flow():
    js = _read("admin.js")
    fn = js.split("async function startPath(", 1)[1].split("\nfunction ", 1)[0]
    # Card clicks reuse the existing safety flow (unconfirmed post first, then a
    # confirmation retry for an existing installation).
    assert "postStartPath(choice, false)" in fn
    assert "result.requires_confirmation" in fn
    assert "postStartPath(choice, true)" in fn
    assert "migrate_legacy_config" in fn
    # Clicking a card routes through startPath with the card's chosen path.
    assert 'document.querySelectorAll("[data-start-path]")' in js
    assert "startPath(card.dataset.startPath)" in js


def test_setup_wizard_keeps_its_own_next_button():
    html = _read("index.html")
    # The landing Next button is gone, but the setup wizard keeps its own Next
    # control (and its "Continue to deployment" label logic in admin.js).
    setup_html = html.split('id="view-setup"', 1)[1]
    assert 'id="setup-next"' in setup_html
    assert ">Next<" in setup_html
    assert "Continue to deployment" in _read("admin.js")


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
        'id="discovery-run"',
        'id="mdns-refresh"',
        'id="ignored-devices"',
        'id="mqtt-list"',
    ):
        assert marker in devices


def test_manual_scan_is_a_global_section_serving_api_and_mqtt():
    html = _read("index.html")
    devices = _devices_section(html)
    # The manual CIDR scan is a global section (feeds both Local API and MQTT),
    # not nested inside the Local API source config.
    assert 'id="discovery-manual-scan"' in devices
    assert "Manual network scan" in devices
    scan = devices.split('id="discovery-manual-scan"', 1)[1].split("</section>", 1)[0]
    assert 'id="cidr-input"' in scan
    assert 'id="scan-form"' in scan
    # It sits between Discovery priority and the Discovery progress section.
    assert devices.index('id="discovery-preparation"') < devices.index(
        'id="discovery-manual-scan"'
    )
    assert devices.index('id="discovery-manual-scan"') < devices.index(
        'id="discovery-progress-section"'
    )
    # The old per-source collapsed manual-scan block is gone.
    assert 'id="manual-scan-details"' not in devices


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
    assert "Existing deployment container found" in start
    assert 'id="start-conflict-resolve"' in start
    render = js.split("function renderContainerConflict", 1)[1].split(
        "\nfunction ", 1
    )[0]
    assert "safe_fix_available" in render
    assert "replace_available" in render
    assert "Existing InfluxDB container found" in render
    assert '"Replacing running " + serviceName + "…"' in render
    assert '"Replace running " + serviceName + " and continue"' in render
    assert "startConflictResolve.hidden = !safe && !replace" in render
    resolve = js.split("async function resolveContainerConflict", 1)[1].split(
        "\nfunction ", 1
    )[0]
    assert "/api/setup/deployment/resolve-container-conflict" in resolve
    assert "remove_stopped_and_continue" in resolve
    assert "replace_running_and_continue" in resolve
    assert "data.conflict" in resolve
    assert "data.continue" in resolve
    assert "await startDeployment()" in resolve

    success = js.split("function renderStartSuccess", 1)[1].split(
        "\nfunction ", 1
    )[0]
    assert "start.running && !start.conflict" in success


def test_step_05_resolves_every_container_conflict_before_starting():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is required for the container conflict behavior contract")
    js = _read("admin.js")
    resolve = (
        "async function resolveContainerConflict"
        + js.split("async function resolveContainerConflict", 1)[1].split(
            "\nfunction pollStartJob", 1
        )[0]
    )
    script = f"""
const setupState = {{
  start: {{
    status: "idle",
    error: null,
    conflict: {{
      container_name: "ems-solarflow-api-control",
      safe_fix_available: true,
      replace_available: false,
    }},
  }},
}};
let startCalls = 0;
const afterFirst = {{}};
const responses = [
  {{
    ok: true,
    removed: "ems-solarflow-api-control",
    continue: false,
    conflict: {{
      container_name: "ems-influxdb",
      safe_fix_available: true,
      replace_available: false,
    }},
  }},
  {{ok: true, removed: "ems-influxdb", continue: true, conflict: null}},
];
async function fetch() {{
  const payload = responses.shift();
  return {{ok: true, json: async () => payload}};
}}
function renderStart() {{}}
async function startDeployment() {{ startCalls += 1; }}
{resolve}
(async () => {{
  await resolveContainerConflict();
  afterFirst.startCalls = startCalls;
  afterFirst.conflict = setupState.start.conflict && setupState.start.conflict.container_name;
  await resolveContainerConflict();
  console.log(JSON.stringify({{afterFirst, startCalls, conflict: setupState.start.conflict}}));
}})();
"""
    result = subprocess.run(
        [node, "-e", script], text=True, capture_output=True, check=False
    )

    assert result.returncode == 0, result.stderr
    outcome = json.loads(result.stdout)
    assert outcome["afterFirst"] == {
        "startCalls": 0,
        "conflict": "ems-influxdb",
    }
    assert outcome["startCalls"] == 1
    assert outcome["conflict"] is None


def test_step_05_labels_only_known_runtime_services_as_ems():
    js = _read("admin.js")
    labels = js.split("function serviceLabel", 1)[1].split("\nfunction ", 1)[0]

    assert 'service === "ems"' in labels
    assert 'service === "ems-solarflow-admin"' in labels
    assert 'return "Admin"' in labels
    assert "return service" in labels


def test_step_05_conflict_status_and_error_are_not_duplicated():
    js = _read("admin.js")
    status = js.split("function startStatusText", 1)[1].split("\nfunction ", 1)[0]

    assert 'if (start.conflict) return "Resolve the container conflict to continue."' in status


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
    assert "Guided Docker setup for local EMS deployments." in header
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
    cards = js.split("function selectedInverterCards", 1)[1].split(
        "\nfunction ", 1
    )[0]
    # Only inverters render as rows (Local-API draft items plus selected MQTT
    # devices); the grid meter is a separate hardware concept.
    assert "selectedInverterCards()" in fn
    assert "inverterItems()" in cards
    assert "selectedMqttDeviceEntries()" in cards
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
    assert (
        'class="advanced-details add-devices-details" id="config-available-details"'
        in config
    )
    for details_id in (
        "config-preview-details",
        "config-template-details",
    ):
        assert f'class="advanced-details" id="{details_id}"' in config
    for details_id in (
        "config-available-details",
        "config-preview-details",
        "config-template-details",
    ):
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
    assert "nextCompactInverterName" in js
    assert "nextInverterName" in js
    assert 'return "INV_" + number' in js
    # Grid meter default name.
    assert 'config_name: "grid_meter"' in js


def test_js_config_prevents_duplicate_add():
    js = _read("admin.js")
    assert "draftHasSource" in js
    # The add path bails out when the source id is already in the draft.
    assert "if (draftHasSource(sourceId)) return;" in js
    # Added cards disable their button.
    assert ">Added to draft</button>" in js


def test_js_config_available_cards_use_hardware_card_style():
    js = _read("admin.js")
    html = _read("index.html")
    css = _read("admin.css")
    card = js.split("function renderConfigAvailableCard", 1)[1].split(
        "\nfunction ", 1
    )[0]
    # Setup available cards adopt the Maintenance hardware-card layout.
    assert "hardware-card hardware-card-" in card
    assert "hardware-card-head" in card
    assert "hardware-card-summary" in card
    assert "hardware-card-title" in card
    assert "hardware-card-model" in card
    assert "hardware-card-meta" in card
    assert "hardware-card-actions" in card
    assert "hardware-card-status" in card
    assert "hardware-card-toggle" in card
    assert "hardware-card-body" in card
    # Grid meter candidates get the grid-meter variant, inverters the inverter one.
    assert '"grid-meter"' in card
    assert '"inverter"' in card
    # Setup add behavior is preserved, not moved onto mconfig helpers.
    assert "config-add" in card
    assert "data-source-id" in card
    assert "data-add-role" in card
    assert "Added to draft" in card
    assert "draftHasSource(sourceId)" in card
    assert "mconfigAddDiscovered" not in card
    assert "mconfigState" not in card
    # The available list toggles bodies through the shared open-card set.
    handler = js.split("configEls.availableList.addEventListener", 1)[1].split(
        "\n}", 1
    )[0]
    assert "data-available-toggle" in handler
    assert "openHardwareCards" in handler
    assert "renderConfigAvailable()" in handler
    # The available list reads as a vertical hardware list, not a tile grid.
    assert 'id="config-available-list" class="config-available-list-style"' in html
    assert 'id="config-available-list" class="results-list"' not in html
    assert ".config-available-list-style {" in css


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

    assert "✓ System Build resources verified" in release
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


def test_js_admin_views_are_setup_and_maintenance_only():
    js = _read("admin.js")
    registry = js.split("const ADMIN_VIEWS =", 1)[1].split("]", 1)[0]
    assert '"setup"' in registry
    assert '"maintenance"' in registry
    # The obsolete top-level advanced view is no longer a routable admin view.
    assert '"advanced"' not in registry


def test_js_advanced_hash_falls_back_to_setup():
    js = _read("admin.js")
    # #advanced is no longer a known view, so it resolves to the setup fallback
    # like any other unknown hash rather than opening a placeholder page.
    assert '"advanced"' not in js.split("const ADMIN_VIEWS =", 1)[1].split("]", 1)[0]
    resolve = js.split("function adminViewForHash", 1)[1].split("\n}", 1)[0]
    # Only the maintenance family is special-cased; everything else (including
    # #advanced) passes through to the setup fallback in setAdminView.
    assert 'hash === "maintenance"' in resolve
    assert '"advanced"' not in resolve


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
    # Resources are verified automatically after alignment — no manual button.
    assert 'id="release-download"' not in release
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
    # The per-section shape lives in shared factories so the initial page load
    # and Start over cannot drift; the fields are asserted there.
    devices = js.split("function createInitialDevicesState", 1)[1].split("}", 1)[0]
    assert "supported_count" in devices
    config = js.split("function createInitialConfigState", 1)[1].split("}", 1)[0]
    assert "auto_added_count" in config
    for name in ("createInitialDeploymentState", "createInitialStartState"):
        assert "function " + name in js
        section = js.split("function " + name, 1)[1].split("\n}", 1)[0]
        assert "job_id" in section


def test_js_release_gates_next_until_ready():
    js = _read("admin.js")
    assert "function releaseReady" in js
    ready = js.split("function releaseReady", 1)[1].split("\nfunction ", 1)[0]
    assert 'setupState.release.status === "ready"' in ready
    # Devices and Config are locked until the setup operation is confirmed.
    locked = js.split("function stepLocked", 1)[1].split("\nfunction ", 1)[0]
    assert '"devices"' in locked and '"config"' in locked
    assert "!confirmedSetupBuildReady()" in locked
    confirmed = _extract_fn(js, "confirmedSetupBuildReady")
    assert "releaseReady()" in confirmed
    assert "setupOperationContext" in confirmed
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
    # Only the explicit Next confirmation advances the backend pointer and marks
    # the selected System Build resources ready.
    confirm = _async_fn_body(js, "async function confirmSelectedSystemBuild")
    assert "setupState.release.current = confirmedTag" in confirm
    assert 'setReleaseStatus("ready")' in confirm


def test_js_only_active_step_panel_is_shown():
    js = _read("admin.js")
    fn = js.split("function setActiveStep", 1)[1].split("\nfunction ", 1)[0]
    assert "panel.dataset.setupStepPanel !== next" in fn
    assert "panel.hidden" in fn


def test_js_release_preparation_uses_backend_api():
    js = _read("admin.js")
    assert "/api/setup/releases" in js
    assert "/api/setup/system-build/confirm" in js
    assert "/api/setup/releases/prepare" not in js
    assert "function prepareRelease" not in js
    assert "function refreshPrepareButton" not in js
    confirm = _async_fn_body(js, "async function confirmSelectedSystemBuild")
    assert '"/api/setup/system-build/confirm"' in confirm
    for status in ("not_started", "ready", "failed"):
        assert status in js
    setter = js.split("function setReleaseStatus", 1)[1].split("\nfunction ", 1)[0]
    assert "releaseError" in setter
    assert 'setTimeout(() => setReleaseStatus("ready")' not in js


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


def test_load_releases_selects_only_from_supported_rendered_channels():
    js = _read("admin.js")
    load = js.split("async function loadReleases", 1)[1].split(
        "\nasync function ", 1
    )[0]
    # One supported list drives both rendering and selection.
    assert "const supportedReleases = grouped.flatMap(" in load
    assert "groupSetupReleaseOptions(releases)" in load
    # Every candidate is drawn from the supported list, so an unknown/hidden
    # channel can never become the internal selection.
    assert "supportedReleases.find(" in load
    assert "releases.find(" not in load
    # The server hints are still consulted (default + prepared).
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


def test_config_advanced_group_is_open_like_features():
    html = _read("index.html")
    config = _config_section(html)
    # Advanced / System settings renders as an open setup-group card (same style
    # as Features), not a collapsed <details>.
    assert 'data-setup-group="advanced"' in config
    assert 'class="advanced-details setup-group" data-setup-group="advanced"' not in config
    group = config.split('data-setup-group="advanced"', 1)[1].split("</section>", 1)[0]
    assert '<h3 class="config-section-title">Advanced / System settings</h3>' in group
    assert 'id="config-feature-list-advanced"' in group


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


def test_js_non_collapsible_feature_sections_open_by_default():
    js = _read("admin.js")
    # Catalog sections flagged collapsible:false (e.g. "System basics") seed the
    # default open state in both the setup and maintenance config editors.
    helper = js.split("function seedDefaultOpenFeatureSections", 1)[1].split(
        "\nfunction ", 1
    )[0]
    assert "section.collapsible === false" in helper
    # Setup branch seeds after loading its catalog.
    setup = js.split("async function loadSetupCatalog", 1)[1].split(
        "\nasync function ", 1
    )[0]
    assert "seedDefaultOpenFeatureSections(setupCatalog.sections" in setup
    # Maintenance branch re-seeds after clearing its open set on each load.
    assert (
        "mconfigState.openFeatures.clear();" in js
        and "seedDefaultOpenFeatureSections(" in js
        and "mconfigState.catalog.feature_sections" in js
    )


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


def test_js_grid_meter_type_infers_generic_zendure_http():
    js = _read("admin.js")
    choices = js.split("const GRID_METER_TYPE_CHOICES", 1)[1].split("]", 1)[0]
    assert "zendure_grid_meter_http" in choices
    # Both the generic D0/HTTP family and the legacy 3CT family resolve to the
    # single generic local-HTTP type via the family map, not by a "3ct" substring.
    family_map = js.split("const GRID_METER_FAMILY_TYPES", 1)[1].split("};", 1)[0]
    assert "zendure_grid_meter_http: \"zendure_grid_meter_http\"" in family_map
    assert "zendure_smartmeter_3ct_http: \"zendure_grid_meter_http\"" in family_map
    fn = js.split("function gridMeterType", 1)[1].split("\nfunction ", 1)[0]
    assert "GRID_METER_FAMILY_TYPES" in fn
    # The loose "3ct" substring inference is gone.
    assert '"3ct"' not in fn


def test_js_grid_meter_type_choices_include_d0_local_api():
    js = _read("admin.js")
    choices = js.split("const GRID_METER_TYPE_CHOICES", 1)[1].split("]", 1)[0]
    # The D0 local-API meter is a distinct, selectable manual type, separate
    # from the MQTT D0 meter.
    assert "zendure_smartmeter_d0_http" in choices
    assert "zendure_smartmeter_d0" in choices
    # Discovery never silently resolves an unknown Zendure grid meter to a D0 or
    # a 3CT: the family map routes discovered Zendure HTTP meters to the generic
    # local-HTTP type only. The concrete D0/3CT local-API types are manual-only.
    family_map = js.split("const GRID_METER_FAMILY_TYPES", 1)[1].split("};", 1)[0]
    assert "zendure_smartmeter_d0_http" not in family_map


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
    return html.split('id="view-maintenance"', 1)[1].split("</main>", 1)[0]


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
    # Backup / restore is a shipped workflow now; no "Planned" badge remains.
    assert hub.count('class="source-badge">Planned') == 0
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
    # Guided upgrade is a navigation button with the recommended/primary treatment.
    upgrade_tag = hub.split('data-maintenance-path="upgrade"', 1)[0].rsplit("<", 1)[1]
    assert upgrade_tag.startswith("button")
    assert "is-primary" in upgrade_tag
    upgrade = hub.split('data-maintenance-path="upgrade"', 1)[1].split(
        'data-maintenance-path="manual"', 1
    )[0]
    assert "Recommended path" in upgrade


def _maintenance_hub(html):
    return html.split('id="maintenance-hub"', 1)[1].split(
        'id="maintenance-manual-panel"', 1
    )[0]


def test_maintenance_hub_cards_are_navigation_without_step_numbers():
    hub = _maintenance_hub(_read("index.html"))
    # Choosing a maintenance section is navigation, not a numbered workflow step.
    assert "control-stage-step" not in hub
    for badge in ("01", "02", "03"):
        assert ">" + badge + "<" not in hub
    # Each card stays a full-card clickable navigation control with an arrow.
    for path in ("upgrade", "manual", "backup"):
        card = hub.split('data-maintenance-path="' + path + '"', 1)[1].split(
            "</button>", 1
        )[0]
        assert 'data-open-maintenance-path="' + path + '"' in card
        assert "maintenance-path-arrow" in card


def test_maintenance_hub_copy_is_current_and_drops_planned():
    hub = _maintenance_hub(_read("index.html"))
    assert "Plan and validate an EMS update" in hub
    assert "Planned upgrade workflow" not in hub
    assert "Planned" not in hub
    # Backup / restore keeps active wording.
    backup = hub.split('data-maintenance-path="backup"', 1)[1]
    assert "Create, inspect, restore or delete EMS backups" in backup


def test_true_maintenance_workflows_keep_numbered_steps():
    html = _read("index.html")
    # Guided upgrade execution and backup / restore are real workflows and must
    # keep their numbered control stages.
    upgrade = _upgrade_panel(html)
    backup = html.split('id="maintenance-backup-panel"', 1)[1]
    for section in (upgrade, backup):
        assert "control-stage-step" in section
        assert ">01<" in section


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
        "</main>", 1
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
        "</main>", 1
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


def test_guided_upgrade_planning_has_four_numbered_stages():
    html = _read("index.html")
    panel = _upgrade_panel(html)
    for label in ("Target System Build", "Upgrade options", "Admin alignment", "Upgrade validation"):
        assert 'aria-label="' + label + '"' in panel
    for step in ("01", "02", "03", "04"):
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
    # Its stages are plain control-pipeline stages carrying the guided marker
    # (EMS release, options, Admin Console update, and validation).
    assert panel.count("control-pipeline-stage guided-upgrade-stage") == 4
    # Options reuse the shared settings-list rows instead of inline checkboxes.
    assert "feature-fields upgrade-options" in panel
    assert "feature-field-row" in panel


def test_guided_upgrade_options_default_on_with_backup():
    html = _read("index.html")
    panel = _upgrade_panel(html)
    # Only the comfort options remain operator-toggleable; each ships checked.
    for key in (
        "backup",
        "config_check",
        "config_add_keys",
        "config_comments",
        "diagnostics",
    ):
        marker = 'data-upgrade-option="' + key + '"'
        assert marker in panel
        box = panel.split(marker, 1)[1].split(">", 1)[0]
        prefix = panel.split(marker, 1)[0].rsplit("<input", 1)[1]
        assert "checked" in prefix + box


def test_guided_upgrade_deploy_steps_are_mandatory_not_toggleable():
    html = _read("index.html")
    panel = _upgrade_panel(html)
    # Pulling the image and recreating the EMS container are no longer optional
    # checkboxes; deploying the System Build is presented as mandatory.
    assert 'data-upgrade-option="pull_image"' not in panel
    assert 'data-upgrade-option="recreate"' not in panel
    assert 'id="upgrade-mandatory-deploy"' in panel
    assert "mandatory" in panel.lower()
    # The executor still always pulls + recreates regardless of the request.
    js = _read("admin.js")
    fn = js.split("function readUpgradeOptions", 1)[1].split("\nfunction ", 1)[0]
    assert "state.pull_image = true;" in fn
    assert "state.recreate = true;" in fn


def test_guided_upgrade_terminology_uses_system_build_language():
    html = _read("index.html")
    panel = _upgrade_panel(html)
    js = _read("admin.js")
    # Consistent "System Build" / "System" upgrade language, unified with Fresh
    # Install: the explicit action is "Verify System Build" (no legacy wording).
    assert "Verify System Build" in panel
    assert "Validate System Build" not in panel
    assert "Upgrade system" in panel
    assert "Loading System Builds" in panel
    assert "Prepare target" not in panel
    assert "Upgrade EMS" not in panel
    status = js.split("const UPGRADE_RELEASE_STATUS_TEXT", 1)[1].split("};", 1)[0]
    assert "System Build verified." in status
    # The verifying status names the download that is actually happening.
    assert "Downloading and verifying the Admin and EMS images…" in status
    assert "Loading System Builds…" in status
    assert "Loading EMS releases" not in status


def test_guided_upgrade_waiting_states_stay_visible():
    """Long Guided Upgrade waits must read as working, not frozen.

    Verify System Build spins its busy ring and says "Verifying…"; the live
    stepper labels and animates the active step and names the slow install
    stage; both new animations honour reduced motion.
    """
    js = _read("admin.js")
    css = _read("admin.css")
    status_fn = _extract_fn(js, "setUpgradeReleaseStatus")
    assert 'classList.toggle("is-scanning", verifying)' in status_fn
    assert '"Verifying…"' in status_fn
    render_fn = _extract_fn(js, "renderSystemAlignmentStatus")
    assert '"Working…"' in render_fn
    assert "this can take a few minutes" in render_fn
    assert "@keyframes system-alignment-active-pulse" in css
    assert 'li[data-state="active"]' in css
    assert css.count("prefers-reduced-motion") >= 2


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
    assert "loadUpgradePlanning(" in path
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
    # Execution is gated on the verified-fingerprint gate (selected + prepared +
    # matching planned/verified fingerprints) and no in-flight run; it starts
    # disabled in the HTML until this logic enables it.
    assert "upgradeTargetVerified()" in fn
    assert "upgradeState.running" in fn
    assert "executeBtn.disabled = !allowed" in fn


def test_upgrade_alignment_requires_recovery_detects_failed_recoverable():
    """The recovery guard fires only for a failed_recoverable alignment transition,
    which must be recovered through the shared System Alignment controls."""
    js = _read("admin.js")
    fn = _extract_fn(js, "upgradeAlignmentRequiresRecovery")
    script = "let upgradeState;\n" + fn + """
function requires(transition) {
  upgradeState = { alignmentTransition: transition };
  return upgradeAlignmentRequiresRecovery();
}
console.log(JSON.stringify({
  failed: requires({ stage: "failed_recoverable" }),
  none: requires(null),
  running: requires({ stage: "admin_reconnect_pending" }),
  completed: requires({ stage: "completed" }),
}));
"""
    out = _run_node(script)
    assert out["failed"] is True
    assert out["none"] is False
    assert out["running"] is False
    assert out["completed"] is False


def test_guided_upgrade_execute_disabled_when_recovery_required():
    """A failed_recoverable transition must disable Upgrade system so the operator
    is steered to the existing recovery flow instead of a doomed HTTP 409 execute."""
    js = _read("admin.js")
    fn = js.split("function updateExecuteButton", 1)[1].split("\nfunction ", 1)[0]
    assert "!upgradeAlignmentRequiresRecovery()" in fn


def test_failed_recoverable_render_keeps_upgrade_disabled_and_recovery_available():
    """Integration: a verified upgrade plan whose shared transition advances to
    failed_recoverable — rendered through renderSystemAlignmentStatus exactly as the
    poller does, never by assigning upgradeState.alignmentTransition directly — must
    leave Upgrade system disabled while the recovery controls stay available. This
    exercises the central synchronization from the authoritative systemAlignmentState
    into the upgrade action buttons.
    """
    js = _read("admin.js")
    reals = "\n".join(
        [
            _extract_fn(js, "upgradeAlignmentState"),
            _extract_fn(js, "upgradeAdminVerificationCurrent"),
            _extract_fn(js, "upgradeTargetPrepared"),
            _extract_fn(js, "upgradeTargetVerified"),
            _extract_fn(js, "upgradeCanPlan"),
            _extract_fn(js, "upgradeAlignmentRequiresRecovery"),
            _extract_fn(js, "updateExecuteButton"),
            _extract_fn(js, "updateUpgradeActionButtons"),
            _extract_fn(js, "renderUpgradeAdminAlignment"),
            _extract_fn(js, "applyUpgradeAlignmentTransition"),
            _extract_fn(js, "renderSystemAlignmentStatus"),
        ]
    )
    header = """
let systemAlignmentState = null;
const SYSTEM_ALIGNMENT_STAGE_ORDER = [];
const UPGRADE_ALIGNMENT_STATUS_TEXT = {
  failed_recoverable: "Admin alignment failed; recovery is required.",
};
function systemAlignmentStageStates(data) {
  const t = (data && data.transition) || {};
  return { stage: t.stage || (data && data.status) || null, states: [] };
}
function applySystemBuildPresentation() {}
function upgradeDevAckSatisfied() { return true; }
const document = { querySelectorAll: () => [] };
const upgradeAdminEls = { current: {}, target: {}, status: {} };
const upgradeEls = {
  executeBtn: { disabled: false, textContent: "" },
  planBtn: { disabled: false, textContent: "", setAttribute() {}, removeAttribute() {} },
  options: [],
};
const systemAlignmentEls = {
  tag: {}, buildId: {}, revision: {}, adminImage: {}, emsImage: {}, message: {},
  warning: {}, reconnect: {}, partial: {}, partialMessage: {}, resume: {},
  returnToRunning: {}, abandon: {},
};
let authState = { adminInstanceId: "admin-A" };
let upgradeState = {
  selected: "v9.9.10", prepared: true, preparedTag: "v9.9.10",
  preparedFingerprint: "fp:A", planned: true, plannedFingerprint: "fp:A",
  preparedAdminInstanceId: "admin-A", status: "ready", loading: false,
  planning: false, running: false, completed: false, alignmentTransition: null,
  validation: null, releases: [],
};
"""
    driver = """
updateExecuteButton();
const executeDisabledBefore = upgradeEls.executeBtn.disabled;
renderSystemAlignmentStatus({
  active: true,
  status: "failed_recoverable",
  transition: {
    mode: "guided_upgrade", stage: "failed_recoverable", operation_id: "upgrade-op",
    system_tag: "v9.9.10", resume_available: true, return_available: false,
    cancel_available: true, worker_active: false, worker_status_available: true,
  },
});
console.log(JSON.stringify({
  executeDisabledBefore,
  executeDisabledAfter: upgradeEls.executeBtn.disabled,
  resumeAvailable: systemAlignmentEls.resume.disabled === false,
  abandonAvailable: systemAlignmentEls.abandon.disabled === false,
  recoveryPanelShown: systemAlignmentEls.partial.hidden === false,
  syncedStage:
    (upgradeState.alignmentTransition && upgradeState.alignmentTransition.stage) || null,
}));
"""
    out = _run_node(header + reals + driver)
    assert out["executeDisabledBefore"] is False
    assert out["executeDisabledAfter"] is True
    assert out["resumeAvailable"] is True
    assert out["abandonAvailable"] is True
    assert out["recoveryPanelShown"] is True
    assert out["syncedStage"] == "failed_recoverable"


def test_guided_upgrade_validation_is_server_driven():
    js = _read("admin.js")
    fn = js.split("async function prepareUpgradeTarget", 1)[1].split(
        "\nif (upgradeEls.form)", 1
    )[0]
    # The selection is validated by the read-only server endpoint.
    assert "/api/admin/maintenance/upgrade/validate" in fn
    # It is reset to not-prepared before the request and only marked prepared
    # after the server validation is accepted — never locally without a check.
    assert "upgradeState.prepared = false" in fn
    validate_at = fn.index("/api/admin/maintenance/upgrade/validate")
    accepted_at = fn.index("upgradeValidationAccepted")
    prepared_true_at = fn.index("upgradeState.prepared = true")
    assert validate_at < accepted_at < prepared_true_at


# --- Guided Upgrade: side-effect-free selection, explicit verification --------
# Unified with Fresh Install: selecting a target is a local preview only; the one
# explicit "Verify System Build" action downloads/reuses the images and verifies.


def test_guided_upgrade_selection_change_is_side_effect_free():
    js = _read("admin.js")
    change = _extract_fn(js, "onUpgradeReleaseChange")
    # Changing the target never verifies or pulls: no fetch, no validate endpoint.
    assert "fetch(" not in change
    assert "/upgrade/validate" not in change
    assert "prepareUpgradeTarget" not in change
    # It supersedes an in-flight verification and drops the previous plan.
    assert "upgradeState.validationGeneration += 1" in change
    assert "invalidateUpgradePlan({ resetCompleted: true })" in change
    assert "upgradeState.preparedFingerprint = null" in change


def test_guided_upgrade_verify_is_the_only_validation_trigger():
    js = _read("admin.js")
    # The heavy /upgrade/validate call lives only in prepareUpgradeTarget, which is
    # bound only to the explicit form submit (the "Verify System Build" button).
    assert js.count('"/api/admin/maintenance/upgrade/validate"') == 1
    prepare = _async_fn_body(js, "async function prepareUpgradeTarget")
    assert "/api/admin/maintenance/upgrade/validate" in prepare
    bindings = js.split("if (upgradeEls.form)", 1)[1].split("if (upgradeEls.reload)", 1)[0]
    assert "prepareUpgradeTarget()" in bindings


def test_guided_upgrade_verification_discards_stale_responses():
    js = _read("admin.js")
    prepare = _async_fn_body(js, "async function prepareUpgradeTarget")
    # An epoch is captured before the awaits and re-checked after each, so a
    # superseded verification cannot verify or paint a newer target selection.
    assert "const generation = upgradeState.validationGeneration" in prepare
    assert "generation !== upgradeState.validationGeneration" in prepare
    assert "tag !== upgradeState.selected" in prepare
    # The stale guard runs after the fetch, before the verdict is applied.
    before_apply = prepare.split("upgradeState.prepared = true", 1)[0]
    assert before_apply.count("stale()") >= 2


def test_guided_upgrade_plan_is_bound_to_the_verified_fingerprint():
    js = _read("admin.js")
    prepare = _async_fn_body(js, "async function prepareUpgradeTarget")
    # A successful verification binds the plan to the resolved pair's non-empty
    # server fingerprint (never a value synthesized in the browser).
    assert (
        "upgradeState.preparedFingerprint = upgradeResponseFingerprint(data)" in prepare
    )
    # Changing the target clears the bound fingerprint (see the change handler).
    change = _extract_fn(js, "onUpgradeReleaseChange")
    assert "upgradeState.preparedFingerprint = null" in change


# --- Guided Upgrade: the verified fingerprint gates Upgrade System ------------
# The plan is bound to the fingerprint Verify returned; the button is enabled and
# the execute request is sent only when the planned fingerprint matches it.


def _run_node(script):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for the guided-upgrade gate behavior test")
    result = subprocess.run(
        [node, "-e", script], text=True, capture_output=True, check=False
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_upgrade_execute_body_carries_the_verified_selection_fingerprint():
    js = _read("admin.js")
    script = _upgrade_execute_body_fn(js) + """
console.log(JSON.stringify({
  withFp: upgradeExecuteBody("v0.8.0", { backup: true }, false, false, null, "fp:verified"),
  noFp: upgradeExecuteBody("v0.8.0", { backup: true }, false, false, null, null),
}));
"""
    out = _run_node(script)
    assert out["withFp"]["selection_fingerprint"] == "fp:verified"
    assert out["withFp"]["confirm"] is True
    # A missing fingerprint is not synthesized into a truthy value.
    assert not out["noFp"].get("selection_fingerprint")


def test_upgrade_target_verified_requires_matching_prepared_and_planned_fingerprints():
    js = _read("admin.js")
    assert "function upgradeTargetVerified" in js, "verified-gate helper is missing"
    fn = (
        _extract_fn(js, "upgradeTargetPrepared")
        + "\n"
        + _extract_fn(js, "upgradeTargetVerified")
    )
    script = "let upgradeState;\n" + fn + """
function verified(state) {
  upgradeState = state;
  return upgradeTargetVerified();
}
const base = {
  selected: "v9.9.10", prepared: true, preparedTag: "v9.9.10",
  preparedFingerprint: "fp:A", planned: true, plannedFingerprint: "fp:A",
};
console.log(JSON.stringify({
  allMatch: verified({ ...base }),
  noPreparedFingerprint: verified({ ...base, preparedFingerprint: null }),
  noPlanned: verified({ ...base, planned: false }),
  plannedFingerprintMismatch: verified({ ...base, plannedFingerprint: "fp:B" }),
  tagMismatch: verified({ ...base, preparedTag: "v9.9.9" }),
  notPrepared: verified({ ...base, prepared: false }),
}));
"""
    out = _run_node(script)
    assert out["allMatch"] is True
    assert out["noPreparedFingerprint"] is False
    assert out["noPlanned"] is False
    assert out["plannedFingerprintMismatch"] is False
    assert out["tagMismatch"] is False
    assert out["notPrepared"] is False


def test_upgrade_execute_button_gate_uses_the_verified_fingerprint():
    js = _read("admin.js")
    fn = js.split("function updateExecuteButton", 1)[1].split("\nfunction ", 1)[0]
    assert "upgradeTargetVerified()" in fn
    assert "executeBtn.disabled = !allowed" in fn


def test_upgrade_plan_binds_the_planned_fingerprint_to_the_verified_one():
    js = _read("admin.js")
    plan = _async_fn_body(js, "async function planUpgrade")
    assert "upgradeState.planned = true" in plan
    assert "const fingerprint = upgradeState.preparedFingerprint" in plan
    assert "upgradeState.plannedFingerprint = fingerprint" in plan


def test_upgrade_plan_requires_a_ready_verified_target_before_review():
    js = _read("admin.js")
    assert "function upgradeCanPlan" in js
    gate = _extract_fn(js, "upgradeCanPlan")
    for condition in (
        "upgradeState.selected",
        "upgradeTargetPrepared()",
        "upgradeState.preparedFingerprint",
        'upgradeState.status === "ready"',
        "!upgradeState.loading",
        "!upgradeState.planning",
        "!upgradeState.running",
        "!upgradeState.completed",
    ):
        assert condition in gate
    plan = _async_fn_body(js, "async function planUpgrade")
    assert "if (!upgradeCanPlan())" in plan
    assert plan.index("if (!upgradeCanPlan())") < plan.index(
        "loadMqttMigrationReview()"
    )


def test_upgrade_can_plan_only_for_the_current_verified_idle_target():
    js = _read("admin.js")
    helpers = js.split("function upgradeTargetPrepared", 1)[1].split(
        "// Executable only", 1
    )[0]
    script = "function upgradeTargetPrepared" + helpers + """
let upgradeState;
const authState = { adminInstanceId: "admin-A" };
function canPlan(state) {
  upgradeState = state;
  return upgradeCanPlan();
}
const base = {
  selected: "v9.9.10", prepared: true, preparedTag: "v9.9.10",
  preparedFingerprint: "fp:A", preparedAdminInstanceId: "admin-A",
  status: "ready", loading: false, planning: false, planned: false,
  running: false, completed: false,
};
console.log(JSON.stringify({
  verified: canPlan({ ...base }),
  noFingerprint: canPlan({ ...base, preparedFingerprint: null }),
  preparing: canPlan({ ...base, status: "preparing" }),
  loading: canPlan({ ...base, loading: true }),
  planning: canPlan({ ...base, planning: true }),
  planned: canPlan({ ...base, planned: true }),
  running: canPlan({ ...base, running: true }),
  completed: canPlan({ ...base, completed: true }),
  changedAdmin: canPlan({ ...base, preparedAdminInstanceId: "admin-B" }),
  changedTag: canPlan({ ...base, selected: "v9.9.11" }),
}));
"""
    out = _run_node(script)
    assert out == {
        "verified": True,
        "noFingerprint": False,
        "preparing": False,
        "loading": False,
        "planning": False,
        "planned": False,
        "running": False,
        "completed": False,
        "changedAdmin": False,
        "changedTag": False,
    }


def test_upgrade_planning_captures_fingerprint_before_await_and_is_single_flight():
    js = _read("admin.js")
    plan = _async_fn_body(js, "async function planUpgrade")
    capture = "const fingerprint = upgradeState.preparedFingerprint"
    assert capture in plan
    assert plan.index(capture) < plan.index("await loadMqttMigrationReview()")
    assert "upgradeState.planning = true" in plan
    assert "upgradeState.planGeneration" in plan
    assert "upgradePlanStillCurrent(" in plan


def test_upgrade_planning_lifecycle_is_single_flight_and_stale_safe():
    js = _read("admin.js")
    state_helpers = js.split("function upgradeTargetPrepared", 1)[1].split(
        "// Executable only", 1
    )[0]
    plan = js.split("async function planUpgrade", 1)[1].split(
        "// Pick which System Build", 1
    )[0]
    functions = (
        "function upgradeTargetPrepared"
        + state_helpers
        + "async function planUpgrade"
        + plan
    )
    script = """
let reviewCalls = 0;
let releaseReview;
let upgradeState;
const authState = { adminInstanceId: "admin-A" };
const reviewGate = () => new Promise((resolve) => { releaseReview = resolve; });
let activeReviewGate = reviewGate();
function updateUpgradeActionButtons() {}
function renderUpgradeValidation() {}
function renderUpgradePlan() {}
function applyUpgradeMigrationReview(data) {
  upgradeState.migrationReview = data.review;
  upgradeState.migrationRevision = data.revision;
  return true;
}
async function loadMqttMigrationReview() {
  reviewCalls += 1;
  await activeReviewGate;
  return { status: "ok", review: { needs_migration: false }, revision: "rev-1" };
}
function verifiedState() {
  return {
    selected: "v9.9.10", prepared: true, preparedTag: "v9.9.10",
    preparedFingerprint: "fp:A", preparedAdminInstanceId: "admin-A",
    status: "ready", loading: false, planning: false, running: false,
    completed: false, planned: false, plannedFingerprint: null,
    planGeneration: 0,
  };
}
(async () => {
  upgradeState = verifiedState();
  const stalePlan = planUpgrade();
  const duplicate = planUpgrade();
  const busyImmediately = upgradeState.planning;
  upgradeState.preparedFingerprint = "fp:B";
  releaseReview();
  const staleAccepted = await stalePlan;
  const duplicateAccepted = await duplicate;
  const stale = {
    busyImmediately,
    reviewCalls,
    staleAccepted,
    duplicateAccepted,
    planned: upgradeState.planned,
    plannedFingerprint: upgradeState.plannedFingerprint,
  };

  upgradeState = verifiedState();
  activeReviewGate = reviewGate();
  const validPlan = planUpgrade();
  releaseReview();
  const validAccepted = await validPlan;
  console.log(JSON.stringify({
    stale,
    valid: {
      validAccepted,
      planned: upgradeState.planned,
      plannedFingerprint: upgradeState.plannedFingerprint,
      preparedFingerprint: upgradeState.preparedFingerprint,
    },
  }));
})();
"""
    out = _run_node(functions + script)
    assert out["stale"] == {
        "busyImmediately": True,
        "reviewCalls": 1,
        "staleAccepted": False,
        "duplicateAccepted": False,
        "planned": False,
        "plannedFingerprint": None,
    }
    assert out["valid"] == {
        "validAccepted": True,
        "planned": True,
        "plannedFingerprint": "fp:A",
        "preparedFingerprint": "fp:A",
    }


def test_upgrade_action_buttons_render_the_planning_lifecycle():
    html = _read("index.html")
    panel = _upgrade_panel(html)
    plan_tag = panel.split('id="upgrade-plan-btn"', 1)[1].split(">", 1)[0]
    plan_prefix = panel.split('id="upgrade-plan-btn"', 1)[0].rsplit("<button", 1)[1]
    assert "disabled" in plan_prefix + plan_tag

    js = _read("admin.js")
    actions = _extract_fn(js, "updateUpgradeActionButtons")
    for label in ("Plan upgrade", "Planning…", "Plan ready", "Upgrade completed"):
        assert label in actions
    assert "upgradeCanPlan()" in actions
    assert "upgradeState.completed" in actions


def test_upgrade_plan_invalidation_covers_selection_verification_and_options():
    js = _read("admin.js")
    invalidation = _extract_fn(js, "invalidateUpgradePlan")
    assert "upgradeState.planGeneration += 1" in invalidation
    assert "upgradeState.planned = false" in invalidation
    assert "upgradeState.plannedFingerprint = null" in invalidation
    assert "upgradeState.completed = false" in invalidation
    assert "invalidateUpgradePlan({ resetCompleted: true })" in _extract_fn(
        js, "onUpgradeReleaseChange"
    )
    assert "invalidateUpgradePlan()" in _async_fn_body(
        js, "async function prepareUpgradeTarget"
    )
    option_binding = js.split("for (const el of upgradeEls.options)", 2)[2].split(
        "if (upgradeEls.planBtn)", 1
    )[0]
    assert "invalidateUpgradePlan()" in option_binding


def test_upgrade_plan_is_invalidated_by_admin_and_catalogue_identity_changes():
    js = _read("admin.js")
    auth = _extract_fn(js, "applyAuthStatus")
    assert "previousAdminInstanceId !== authState.adminInstanceId" in auth
    assert "clearUpgradeVerification()" in auth

    survives = _extract_fn(js, "upgradeVerificationSurvivesReload")
    assert "preparedReleaseIdentity" in survives
    assert "upgradeReleaseIdentity(release)" in survives
    load = _async_fn_body(js, "async function loadUpgradeReleases")
    assert "clearUpgradeVerification()" in load


def test_upgrade_admin_verification_fails_closed_when_current_identity_missing():
    """When a plan is bound to a prepared Admin instance id but the current Admin
    identity is missing, verification must read as NOT current (fail closed)."""
    js = _read("admin.js")
    fn = _extract_fn(js, "upgradeAdminVerificationCurrent")
    script = "let upgradeState;\nlet authState;\n" + fn + """
function current(prepared, currentId) {
  upgradeState = { preparedAdminInstanceId: prepared };
  authState = { adminInstanceId: currentId };
  return upgradeAdminVerificationCurrent();
}
console.log(JSON.stringify({
  noPrepared: current(null, null),
  preparedMissingCurrent: current("admin-A", null),
  preparedMatches: current("admin-A", "admin-A"),
  preparedDiffers: current("admin-A", "admin-B"),
}));
"""
    out = _run_node(script)
    assert out["noPrepared"] is True
    assert out["preparedMissingCurrent"] is False
    assert out["preparedMatches"] is True
    assert out["preparedDiffers"] is False


def test_show_auth_view_invalidates_prepared_upgrade_plan():
    """Every unauthenticated view (login, create, recovery, logout, session loss)
    passes through showAuthView, which must drop the prepared upgrade plan."""
    js = _read("admin.js")
    fn = _extract_fn(js, "showAuthView")
    assert "clearUpgradeVerification()" in fn


def test_auth_loss_paths_route_through_show_auth_view():
    """Session loss (onAuthLost -> refreshAuthStatus -> applyAuthStatus) and logout
    (submitLogout -> applyAuthStatus, or its catch) both reach showAuthView, so the
    invalidation there covers logout and session expiry alike."""
    js = _read("admin.js")
    on_auth_lost = _extract_fn(js, "onAuthLost")
    assert "refreshAuthStatus()" in on_auth_lost
    refresh = _extract_fn(js, "refreshAuthStatus")
    assert "applyAuthStatus(" in refresh
    assert 'showAuthView("login")' in refresh
    logout = _extract_fn(js, "submitLogout")
    assert "applyAuthStatus(" in logout
    assert 'showAuthView("login")' in logout
    apply_status = _extract_fn(js, "applyAuthStatus")
    assert 'showAuthView("login")' in apply_status


def test_active_planning_is_invalidated_when_auth_is_lost():
    """A plan captured before an auth-loss invalidation is no longer current: the
    generation bump from clearing the verification defeats the mid-flight planning
    guard, so a logout or session loss during planning can never apply a stale plan."""
    js = _read("admin.js")
    deps = "\n".join(
        [
            _extract_fn(js, "upgradeTargetPrepared"),
            _extract_fn(js, "upgradeAdminVerificationCurrent"),
            _extract_fn(js, "invalidateUpgradePlan"),
            _extract_fn(js, "clearUpgradeVerification"),
            _extract_fn(js, "upgradePlanStillCurrent"),
        ]
    )
    script = "let upgradeState;\nlet authState;\n" + deps + """
authState = { adminInstanceId: "admin-A" };
upgradeState = {
  selected: "v9.9.10", planning: true, planGeneration: 3,
  prepared: true, preparedTag: "v9.9.10", preparedFingerprint: "fp:A",
  preparedAdminInstanceId: "admin-A", preparedReleaseIdentity: "id",
  validation: {}, status: "ready", validationGeneration: 0,
  loading: false, running: false, completed: false, planned: false,
  plannedFingerprint: null,
};
const generation = upgradeState.planGeneration;
const beforeLoss = upgradePlanStillCurrent(generation, "v9.9.10", "fp:A");
clearUpgradeVerification();
const afterLoss = upgradePlanStillCurrent(generation, "v9.9.10", "fp:A");
console.log(JSON.stringify({ beforeLoss, afterLoss }));
"""
    out = _run_node(script)
    assert out["beforeLoss"] is True
    assert out["afterLoss"] is False


def test_successful_upgrade_is_terminal_but_retryable_failure_keeps_plan():
    js = _read("admin.js")
    result = _extract_fn(js, "renderUpgradeResult")
    assert "upgradeState.completed = true" in result
    assert "upgradeState.planned = false" not in result
    actions = _extract_fn(js, "updateUpgradeActionButtons")
    assert "upgradeState.completed" in actions


def test_upgrade_selection_change_clears_both_fingerprints():
    js = _read("admin.js")
    change = _extract_fn(js, "onUpgradeReleaseChange")
    assert "upgradeState.preparedFingerprint = null" in change
    assert "invalidateUpgradePlan({ resetCompleted: true })" in change
    assert "upgradeState.plannedFingerprint = null" in _extract_fn(
        js, "invalidateUpgradePlan"
    )


def test_upgrade_stale_verification_error_resets_to_verify_state():
    js = _read("admin.js")
    execute = _async_fn_body(js, "async function executeUpgrade")
    # A missing fingerprint never sends the request.
    assert "if (!upgradeState.preparedFingerprint)" in execute
    # A stale/required rejection clears the verified state and asks to re-verify,
    # without an automatic retry.
    assert "system_build_verification_stale" in execute
    assert "system_build_verification_required" in execute
    assert "clearUpgradeVerification()" in execute
    reset = _extract_fn(js, "clearUpgradeVerification")
    for field in ("upgradeState.prepared = false", "upgradeState.preparedFingerprint = null"):
        assert field in reset
    assert "invalidateUpgradePlan({ resetCompleted: true })" in reset
    invalidation = _extract_fn(js, "invalidateUpgradePlan")
    assert "upgradeState.planned = false" in invalidation
    assert "upgradeState.plannedFingerprint = null" in invalidation


def test_prepared_release_resources_are_not_treated_as_verification():
    js = _read("admin.js")
    load = _async_fn_body(js, "async function loadUpgradeReleases")
    # A catalogue-prepared release only means resources are cached; it must not
    # mark the target verified/ready. Verification requires an explicit Verify.
    assert "Boolean(selected.prepared)" not in load
    assert 'upgradeState.status = upgradeState.prepared ? "ready"' not in load
    # The normal page load leaves the selected target explicitly unverified.
    assert "clearUpgradeVerification()" in load
    reset = _extract_fn(js, "clearUpgradeVerification")
    assert "upgradeState.prepared = false" in reset
    assert "upgradeState.preparedFingerprint = null" in reset


def _upgrade_validation_accepted_fn(js):
    def _fn(name):
        body = js.split("function " + name, 1)[1].split("\n}", 1)[0]
        return "function " + name + body + "\n}"

    # upgradeValidationAccepted calls upgradeResponseFingerprint, so both must be
    # in scope for the standalone node evaluation.
    return _fn("upgradeResponseFingerprint") + "\n" + _fn("upgradeValidationAccepted")


def test_upgrade_validation_accepted_requires_server_confirmation():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for the upgrade-validation behavior test")
    js = _read("admin.js")
    script = _upgrade_validation_accepted_fn(js) + """
const FP = "fp:verified";
console.log(JSON.stringify({
  offline: upgradeValidationAccepted(false, { valid: true, upgrade_allowed: true, selection_fingerprint: FP }),
  invalid: upgradeValidationAccepted(true, { valid: false, upgrade_allowed: true, selection_fingerprint: FP }),
  downgrade: upgradeValidationAccepted(true, { valid: true, upgrade_allowed: false, selection_fingerprint: FP }),
  empty: upgradeValidationAccepted(true, null),
  missingFingerprint: upgradeValidationAccepted(true, { valid: true, upgrade_allowed: true }),
  emptyFingerprint: upgradeValidationAccepted(true, { valid: true, upgrade_allowed: true, selection_fingerprint: "" }),
  ok: upgradeValidationAccepted(true, { valid: true, upgrade_allowed: true, selection_fingerprint: FP }),
}));
"""
    result = subprocess.run(
        [node, "-e", script], text=True, capture_output=True, check=False
    )
    assert result.returncode == 0, result.stderr
    # A validated pair is accepted ONLY with a non-empty server fingerprint; a
    # missing or empty fingerprint fails closed even when valid/upgrade_allowed.
    assert json.loads(result.stdout) == {
        "offline": False,
        "invalid": False,
        "downgrade": False,
        "empty": False,
        "missingFingerprint": False,
        "emptyFingerprint": False,
        "ok": True,
    }


# --- Unified Target System Build selector (Guided Upgrade) ----------------


def test_guided_upgrade_uses_shared_system_build_catalogue():
    js = _read("admin.js")
    load = _async_fn_body(js, "async function loadUpgradeReleases")
    # The upgrade selector reuses the one System Build catalogue and grouping the
    # Guided Setup selector uses — Stable, then Release Candidates, then
    # Development Builds — rather than a flat EMS-only release list.
    assert "groupSetupReleaseOptions(" in load
    assert 'createElement("optgroup")' in load
    assert "function groupSetupReleaseOptions" in js
    # Still the same single shared catalogue endpoint (with the upgrade gate).
    assert "/api/setup/releases?flow=upgrade" in load


def test_guided_upgrade_has_single_target_system_build_selector():
    html = _read("index.html")
    panel = _upgrade_panel(html)
    # Exactly one build selector, labelled "Target System Build". There is no
    # separate Admin target selector and no separate EMS-only version selector.
    assert 'aria-label="Target System Build"' in panel
    assert "Target System Build" in panel
    assert panel.count("<select") == 1
    assert 'id="upgrade-release-select"' in panel
    # The obsolete "EMS release" / "Target release" wording is gone.
    assert "EMS release" not in panel


def test_guided_upgrade_has_no_development_acknowledgement_controls():
    html = _read("index.html")
    panel = _upgrade_panel(html)
    # Selecting a development build is itself the decision, mirroring Guided
    # Setup: the checkbox, its label and the warning banner are all gone.
    for marker in (
        'id="upgrade-system-build-ack"',
        'id="upgrade-system-build-ack-row"',
        'id="upgrade-system-build-dev-warning"',
        "I understand the development-build risks.",
        "Development builds are intended for testing.",
    ):
        assert marker not in panel
    # No hidden acknowledgement checkbox survives anywhere in the document.
    assert 'id="upgrade-system-build-ack"' not in html
    # The acknowledgement state field and checkbox change handler are gone.
    js = _read("admin.js")
    assert "devAcknowledgedTag" not in js
    assert "renderUpgradeDevAcknowledgement" not in js


def test_upgrade_execute_uses_acknowledgement_body_builder():
    js = _read("admin.js")
    fn = js.split("async function executeUpgrade", 1)[1].split(
        "\nasync function", 1
    )[0]
    assert "upgradeExecuteBody(" in fn
    assert "upgradeSelectedIsDevelopment()" in fn
    assert "upgradeDevAckSatisfied()" in fn


def _upgrade_execute_body_fn(js):
    body = js.split("function upgradeExecuteBody", 1)[1].split("\n}", 1)[0]
    return "function upgradeExecuteBody" + body + "\n}"


def test_upgrade_execute_body_carries_development_acknowledgement():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for the upgrade execute-body behavior test")
    js = _read("admin.js")
    script = _upgrade_execute_body_fn(js) + """
console.log(JSON.stringify({
  stable: upgradeExecuteBody("v0.8.0", { backup: true }, false, false),
  devNoAck: upgradeExecuteBody("dev-x-f7265fc-42-1", {}, true, false),
  devAck: upgradeExecuteBody("dev-x-f7265fc-42-1", {}, true, true),
}));
"""
    result = subprocess.run(
        [node, "-e", script], text=True, capture_output=True, check=False
    )
    assert result.returncode == 0, result.stderr
    out = json.loads(result.stdout)
    assert out["stable"]["confirm"] is True
    assert "acknowledge_risk" not in out["stable"]
    assert "acknowledge_risk" not in out["devNoAck"]
    assert out["devAck"]["acknowledge_risk"] is True


# --- Admin alignment as an automatic upgrade stage -----------------------


def test_upgrade_current_admin_never_uses_ems_container_tag():
    js = _read("admin.js")
    fn = js.split("function renderUpgradeAdminAlignment", 1)[1].split(
        "\nfunction ", 1
    )[0]
    # Current Admin comes from the server-provided Admin identity, never from the
    # EMS container tag; the target is the resolved System Build.
    assert "current_admin" in fn
    assert "system_build" in fn
    assert "upgradeState.current" not in fn
    assert "upgradeState.runningAdmin" in fn
    # Dynamic values use textContent (no innerHTML injection surface).
    assert ".textContent" in fn
    assert "innerHTML" not in fn


def test_upgrade_alignment_status_text_covers_every_state():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for the alignment status-text behavior test")
    js = _read("admin.js")
    map_src = (
        "const UPGRADE_ALIGNMENT_STATUS_TEXT"
        + js.split("const UPGRADE_ALIGNMENT_STATUS_TEXT", 1)[1].split("};", 1)[0]
        + "};"
    )
    fn_src = (
        "function upgradeAlignmentState"
        + js.split("function upgradeAlignmentState", 1)[1].split("\n}", 1)[0]
        + "\n}"
    )
    script = map_src + "\n" + fn_src + """
const V = { alignment: "admin_update_required" };
const text = (s) => UPGRADE_ALIGNMENT_STATUS_TEXT[s];
console.log(JSON.stringify({
  update: text(upgradeAlignmentState(V, null)),
  aligned: text(upgradeAlignmentState({ alignment: "aligned" }, null)),
  retag: text(upgradeAlignmentState({ alignment: "retag_required" }, null)),
  recreate: text(upgradeAlignmentState({ alignment: "admin_recreate_required" }, null)),
  liveReconnect: text(
    upgradeAlignmentState(V, { mode: "guided_upgrade", stage: "admin_reconnect_pending" })
  ),
  liveFailure: text(
    upgradeAlignmentState(V, { mode: "guided_upgrade", stage: "failed_recoverable" })
  ),
}));
"""
    result = subprocess.run(
        [node, "-e", script], text=True, capture_output=True, check=False
    )
    assert result.returncode == 0, result.stderr
    out = json.loads(result.stdout)
    assert out["update"] == "The target Admin image will be installed."
    assert out["aligned"] == "Admin already matches the target System Build."
    assert out["retag"] == "The persistent Admin tag will be updated."
    assert out["recreate"] == "The Admin container will be recreated."
    # A live transition stage overrides the static decision.
    assert out["liveReconnect"] == "Waiting for the replacement Admin…"
    assert "failed" in out["liveFailure"].lower()


def test_guided_upgrade_admin_alignment_is_automatic_read_only_stage():
    html = _read("index.html")
    panel = _upgrade_panel(html)
    stage = panel.split('aria-label="Admin alignment"', 1)[1].split("</section>", 1)[0]
    # Read-only facts describe the automatic alignment; there is no button and no
    # separate "Update Admin Server" / "Continue without updating Admin" control.
    assert 'id="upgrade-admin-alignment-status"' in stage
    assert "<button" not in stage
    assert "<input" not in stage
    assert "Update Admin" not in stage
    assert "Continue EMS upgrade" not in stage
    assert "Continue without" not in stage
    # The legacy self-update card ids are gone from the panel entirely.
    assert 'id="admin-update-execute-btn"' not in panel
    assert 'id="admin-update-current"' not in panel


def test_guided_upgrade_does_not_call_legacy_admin_update_endpoints():
    js = _read("admin.js")
    # The whole guided upgrade module no longer touches the standalone Admin
    # self-update endpoints; Admin alignment goes through system-alignment only.
    upgrade = js.split("Guided upgrade planning", 1)[1].split(
        "// --- backup / restore", 1
    )[0]
    assert "/api/admin/maintenance/admin-update/plan" not in upgrade
    assert "/api/admin/maintenance/admin-update/execute" not in upgrade
    assert "/api/admin/maintenance/admin-update/status" not in upgrade
    assert "/api/admin/maintenance/admin-update/resume" not in upgrade
    # The legacy advisory state object is gone.
    assert "adminUpdateState" not in js
    assert "renderAdminUpdate" not in js


def test_guided_upgrade_admin_alignment_uses_system_alignment_status():
    js = _read("admin.js")
    fn = js.split("function loadUpgradePlanning", 1)[1].split(
        "\nfunction onUpgradeReleaseChange", 1
    )[0]
    # Planning reads the shared transition status (recovery of an in-flight Admin
    # alignment is available through it) and renders the read-only stage.
    assert "/api/admin/system-alignment/status" in fn or "loadSystemAlignmentStatus" in fn
    assert "renderUpgradeAdminAlignment" in fn


def test_guided_upgrade_reconnect_overlay_text_exists():
    html = _read("index.html")
    # The reconnect overlay is shared with Guided Setup and stays available so the
    # browser can reconnect after the Admin container is aligned/replaced.
    assert 'id="admin-update-overlay"' in html
    assert "Reconnecting to the Admin Console" in html
    assert "reconnect automatically" in html
    assert 'id="admin-update-overlay-hint"' in html


def test_guided_upgrade_reconnect_resumes_via_upgrade_endpoint():
    js = _read("admin.js")
    fn = js.split("async function resumeGuidedUpgradeFromTransition", 1)[1].split(
        "\n// --- backup / restore", 1
    )[0]
    # Resume is driven by the durable transition (guided_upgrade mode) and
    # continues the whole upgrade from its operation id — never a legacy
    # admin-update resume and never by resending target/options.
    assert 'transition.mode === "guided_upgrade"' in js
    assert "resumeGuidedUpgrade(transition.operation_id)" in fn
    # The operation-id-only upgrade resume hits the dedicated endpoint.
    resume = js.split("async function resumeGuidedUpgrade(", 1)[1].split(
        "\nasync function", 1
    )[0]
    assert "/api/admin/maintenance/upgrade/resume" in resume
    assert "operation_id: operationId" in resume
    # It carries ONLY the operation id — no options/target/plan re-sent.
    assert "options" not in resume
    assert "target_release" not in resume


def test_guided_upgrade_binds_reconnect_to_operation_id():
    js = _read("admin.js")
    fn = js.split("async function executeUpgrade", 1)[1].split(
        "\nasync function", 1
    )[0]
    # The reconnect poller is bound to this operation so a failed/cancelled Admin
    # update on the still-answering old instance is surfaced before the timeout,
    # and a different operation id fails closed.
    assert "data.operation_id" in fn
    assert "waitForAdminReconnect(previousAdminInstanceId, operationId)" in fn


def _select_upgrade_release_tag_fn(js):
    body = js.split("function selectUpgradeReleaseTag", 1)[1].split(
        "\nasync function loadUpgradeReleases", 1
    )[0]
    return "function selectUpgradeReleaseTag" + body


def test_select_upgrade_release_tag_pins_transition_tag():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for the upgrade tag-selection behavior test")
    js = _read("admin.js")
    script = _select_upgrade_release_tag_fn(js) + """
const releases = [
  { tag: "v0.8.0", selectable: true },
  { tag: "v0.7.0", selectable: true },
  { tag: "v0.9.0-RC1", selectable: true },
];
const data = { default_release: "v0.9.0-RC1", prepared_release: "v0.7.0" };
console.log(JSON.stringify({
  pinnedOverDefaultAndPrepared: selectUpgradeReleaseTag(releases, data, "v0.8.0"),
  missingPinnedFailsClosed: selectUpgradeReleaseTag(releases, data, "v9.9.9"),
  noPinUsesDefault: selectUpgradeReleaseTag(releases, data, null),
}));
"""
    result = subprocess.run(
        [node, "-e", script], text=True, capture_output=True, check=False
    )
    assert result.returncode == 0, result.stderr
    out = json.loads(result.stdout)
    # default_release and prepared_release never overwrite the pinned tag.
    assert out["pinnedOverDefaultAndPrepared"] == "v0.8.0"
    # A missing transition tag fails closed (null), never a silent fallback.
    assert out["missingPinnedFailsClosed"] is None
    # Without a pin, the server default is used.
    assert out["noPinUsesDefault"] == "v0.9.0-RC1"


def test_upgrade_resume_selects_transition_tag_deterministically():
    js = _read("admin.js")
    fn = js.split("async function resumeGuidedUpgradeFromTransition", 1)[1].split(
        "\n// --- backup / restore", 1
    )[0]
    # The catalogue is loaded to completion with the transition tag pinned, then
    # the exact tag is confirmed before resuming (fail closed otherwise).
    assert 'await setMaintenancePath("upgrade", transitionTag)' in fn
    assert "upgradeState.selected !== transitionTag" in fn
    assert "await resumeGuidedUpgrade(transition.operation_id)" in fn


def test_upgrade_planning_dedupes_concurrent_loads():
    js = _read("admin.js")
    fn = js.split("function loadUpgradePlanning", 1)[1].split(
        "\nfunction onUpgradeReleaseChange", 1
    )[0]
    # A single shared in-flight promise prevents a second parallel planning run
    # and lets callers await full completion.
    assert "upgradeState.loadingPromise" in fn
    assert "if (upgradeState.loadingPromise) return upgradeState.loadingPromise;" in fn


def test_guided_upgrade_execute_button_not_gated_on_admin_status():
    js = _read("admin.js")
    fn = js.split("function updateExecuteButton", 1)[1].split("\nfunction ", 1)[0]
    # The execute button is no longer gated on a separate Admin update decision.
    assert "adminUpdateBlocksEms" not in fn
    assert "Upgrade system" in fn


def test_guided_upgrade_resume_restores_operation_without_discovery():
    js = _read("admin.js")
    fn = js.split("async function resumeGuidedUpgradeFromTransition", 1)[1].split(
        "\n// --- backup / restore", 1
    )[0]
    # Resume restores the selected build and lands on the upgrade panel; it never
    # starts discovery (that belongs to Guided Setup only).
    assert "transition.system_tag" in fn
    assert 'setMaintenancePath("upgrade"' in fn
    assert "discovery" not in fn.lower()


def test_guided_upgrade_plan_shows_admin_alignment_not_optional_update():
    js = _read("admin.js")
    fn = js.split("function renderUpgradePlan", 1)[1].split("\nfunction ", 1)[0]
    # The plan states Admin alignment is automatic and part of the upgrade; it
    # never offers an optional/standalone Admin update or a skip.
    assert "Align Admin to the target System Build" in fn
    assert "Optional Admin update" not in fn
    assert "Continue without" not in fn
    assert "Update Admin Server" not in fn


def test_maintenance_view_has_three_overview_sections():
    html = _read("index.html")
    maintenance = _maintenance_section(html)
    for label in ("Installation layout", "Runtime containers", "Versions and links"):
        assert 'aria-label="' + label + '"' in maintenance
        assert label in maintenance
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
        'id="maintenance-admin-image"',
        'id="maintenance-ems-image"',
        'id="maintenance-influx-image"',
        'id="maintenance-dashboard"',
        'id="maintenance-warnings"',
    ):
        assert marker in maintenance


def test_maintenance_limits_mutations_to_guarded_workflows():
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
    # MQTT migration is now another sanctioned, revision-bound workflow; remove
    # its dedicated card before checking for arbitrary lifecycle actions.
    migration_start, migration_rest = manual.split(
        '<section class="control-pipeline-stage maintenance-card" id="maintenance-mqtt-migration"',
        1,
    )
    manual_without_migration = migration_start + migration_rest.split("</section>", 1)[1]
    guarded = manual_without_migration.replace("Restart / sync containers", "")
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


MAINTENANCE_OVERVIEW_CARDS = (
    ("maintenance-layout", "maintenance-layout-summary"),
    ("maintenance-containers", "maintenance-containers-summary"),
    ("maintenance-versions", "maintenance-versions-summary"),
    ("maintenance-diagnostics", "maintenance-diagnostics-summary"),
    ("maintenance-config-card", "maintenance-config-summary"),
)


def _maintenance_manual_panel(html):
    return (
        _maintenance_section(html)
        .split('id="maintenance-manual-panel"', 1)[1]
        .split('id="maintenance-upgrade-panel"', 1)[0]
    )


def _overview_card_head(panel, card_id):
    section = panel.split('id="' + card_id + '"', 1)[1]
    return section.split("maintenance-card-body", 1)[0]


def test_maintenance_overview_rows_are_finished_status_accordions():
    html = _read("index.html")
    assert 'id="maintenance-manual-panel"' in _maintenance_section(html)
    panel = _maintenance_manual_panel(html)
    for card_id, summary_id in MAINTENANCE_OVERVIEW_CARDS:
        head = _overview_card_head(panel, card_id)
        # The overview rows are not numbered process steps.
        assert "control-stage-step" not in head
        # One-line accordion row: toggle button, live summary, status, caret.
        assert 'data-maintenance-toggle="' + card_id + '"' in head
        assert 'id="' + summary_id + '"' in head
        assert "maintenance-card-status" in head
        assert "maintenance-caret" in head


def test_maintenance_overview_css_is_one_line_grid_without_step_badge():
    css = _read("admin.css")
    summary = css.split(".maintenance-card-summary {", 1)[1].split("}", 1)[0]
    assert "display: grid" in summary
    assert "grid-template-columns" in summary
    # The overview no longer styles a numbered step badge.
    assert ".maintenance-card .control-stage-step" not in css
    # Tone now drives a compact status badge instead.
    assert ".maintenance-card-status" in css
    for tone in ("ok", "warn", "info", "action"):
        assert (
            '.maintenance-card[data-tone="' + tone + '"] .maintenance-card-status'
            in css
        )


def test_maintenance_cards_are_collapsed_by_default_with_summaries():
    html = _read("index.html")
    maintenance = _maintenance_section(html)
    # Each card starts collapsed: closed state + hidden body + a toggle button
    # carrying a one-line summary in the header. (6 overview cards plus the
    # static Zendure MQTT broker hardware card in the config editor.)
    assert maintenance.count('data-open="false"') == 8
    for card in (
        "maintenance-layout",
        "maintenance-containers",
        "maintenance-versions",
        "maintenance-diagnostics",
        "maintenance-zendure-mqtt",
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
    # The overview row is a status accordion, not a numbered process stage.
    assert 'aria-label="EMS diagnostics"' in maintenance
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
    assert "Add more devices" in card
    assert "Start discovery" in card
    assert "Add inverter" in card
    assert 'id="maintenance-config-features"' in card
    assert 'class="feature-list"' in card
    assert "Advanced / System settings" in card
    # Advanced / System settings renders as an open setup-group card (same style
    # as Features), not a collapsed <details>.
    assert (
        'class="setup-group mconfig-group" id="maintenance-config-advanced-section"'
        in card
    )
    assert 'class="advanced-details setup-group mconfig-group"' not in card
    assert 'id="maintenance-config-advanced"' in card
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
    assert 'id="maintenance-add-devices"' in card
    assert 'id="maintenance-discovery-results"' in card
    # The collapsible "Add more devices" row replaces the Close result button.
    assert "Close result" not in card
    assert "maintenance-discovery-cancel" not in card
    assert ">Cancel</button>" not in card
    assert "Nothing is written until you review and apply the draft." in card
    js = _read("admin.js")
    assert "closeMaintenanceDiscovery" not in js
    start = js.split("async function startMaintenanceDiscovery", 1)[1].split(
        "\nasync function ", 1
    )[0]
    assert "if (!mconfigState.loaded)" in start
    assert "await loadMaintenanceConfig()" in start
    assert 'fetch("/api/discovery/mdns/refresh"' in start
    assert 'fetch("/api/discovery/networks"' in start
    assert 'fetch("/api/discovery/mqtt-brokers/refresh"' in start
    assert 'fetch(ZENDURE_CLOUD_BASE + "/settings")' in start
    assert 'discoveryFetch("/api/discovery/scan"' in js
    assert '"/api/discovery/result/"' in js
    assert "buildMaintenanceDiscoveryReview" in start
    assert "/api/admin/maintenance/config/apply" not in start

    manual = js.split("async function addManualMaintenanceInverter", 1)[1].split(
        "\n// --- ", 1
    )[0]
    assert "if (!mconfigState.loaded)" in manual
    assert "await loadMaintenanceConfig()" in manual


def test_maintenance_discovery_orchestrates_all_three_sources():
    js = _read("admin.js")
    start = js.split("async function startMaintenanceDiscovery", 1)[1].split(
        "\nasync function ", 1
    )[0]
    # Local MQTT parity with setup: re-listen on known brokers (anonymous plus
    # the saved credential pool) before reading the flattened proposals.
    assert 'fetch("/api/discovery/mqtt-brokers/refresh", { method: "POST" })' in start
    # Zendure cloud parity: refresh only when an API key is saved; without a
    # key the source is skipped without failing the run, and the status line
    # points at the Discovery sources settings.
    assert 'fetch(ZENDURE_CLOUD_BASE + "/settings")' in start
    assert 'fetch(ZENDURE_CLOUD_BASE + "/refresh", { method: "POST" })' in start
    assert "token_saved" in start
    assert "Discovery sources" in start
    # Proposals are read only after both refreshes so newly listened local
    # devices and cloud devices land in the same review list.
    proposals_at = start.index('"/api/discovery/mqtt-proposals"')
    assert start.index('"/api/discovery/mqtt-brokers/refresh"') < proposals_at
    assert start.index('ZENDURE_CLOUD_BASE + "/refresh"') < proposals_at
    # Each source is its own work unit in the shared progress bookkeeping:
    # broker re-listen + cloud + proposals + mdns + networks.
    assert "session.progress.total += 5" in start
    assert "session.progress.active += 5" in start
    assert start.count("completeDiscoveryWork(session,") == 6
    # A failed source never touches the draft from the discovery path.
    assert "/api/admin/maintenance/config/apply" not in start


def test_maintenance_add_devices_exposes_shared_source_settings():
    html = _read("index.html")
    add = html.split('id="maintenance-add-devices"', 1)[1].split(
        'id="maintenance-config-features-section"', 1
    )[0]
    # Two collapsed rows expose the parked setup source-config blocks via
    # slots; mDNS needs no row (it refreshes automatically on Start discovery,
    # enable/disable stays a setup decision).
    assert "Discovery sources" in add
    assert 'data-maintenance-source-slot="local_mqtt"' in add
    assert 'data-maintenance-source-slot="zendure_mqtt"' in add
    assert 'data-maintenance-source-slot="local_api"' not in add
    for row in add.split("data-maintenance-source=")[1:]:
        head = row.split(">", 1)[0]
        assert " open" not in head
    # The shared forms stay single nodes that are moved, never copied: their
    # ids exist exactly once in the whole document.
    assert html.count('id="mqtt-credential-form"') == 1
    assert html.count('id="zendure-cloud-token-form"') == 1


def test_maintenance_source_slots_move_parked_nodes_instead_of_copies():
    js = _read("admin.js")
    # Opening a maintenance source row mounts the parked node and refreshes its
    # status; closing it parks the node back.
    assert "function mountMaintenanceSourceConfig" in js
    mount = js.split("function mountMaintenanceSourceConfig", 1)[1].split(
        "\nfunction ", 1
    )[0]
    assert "data-maintenance-source-slot=" in mount
    assert 'data-inline-config="' in mount
    assert "loadMqttCredentials()" in mount
    assert "loadZendureCloudSettings()" in mount
    wiring = js.split('"[data-maintenance-source]"', 2)
    assert len(wiring) >= 2
    # The setup flow's re-render must not steal a node that is currently
    # mounted in a maintenance slot.
    park = js.split("function parkInlineConfigs", 1)[1].split("\nfunction ", 1)[0]
    assert "inlineConfigMountedInMaintenance" in park
    guard = js.split("function inlineConfigMountedInMaintenance", 1)[1].split(
        "\nfunction ", 1
    )[0]
    assert 'closest("#maintenance-add-devices")' in guard
    # Leaving the maintenance view parks maintenance-mounted nodes again.
    view = js.split("function setAdminView", 1)[1].split("\nfunction ", 1)[0]
    assert "parkMaintenanceSourceConfigs()" in view


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
    # Duplicate detection is cross-transport: the physical identity matcher
    # also covers configured MQTT devices (serial_number), not only device.sn.
    assert "physicalInverterIdentity(device) === serial" in add
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

    assert '"mconfig-discovery-add-button " + actionState.cssClass' in card
    assert "mconfigDiscoveryActionState(item)" in card
    assert "mconfigAddDiscovered(item)" in card
    assert "Update draft" in js
    assert "Added to draft" in js
    assert "In config" in js
    # Adding from discovery is fresh-install style: role-specific add actions.
    assert "Add as grid meter" in js
    assert "Add inverter" in js

    assert "previewMaintenanceConfig" not in review
    assert "mconfig-discovery-next" not in review


def test_maintenance_discovery_disabled_add_buttons_are_scoped():
    css = _read("admin.css")
    assert ".mconfig-discovery-add-button:disabled" in css
    assert ".mconfig-discovery-add-button.is-in-config:disabled" in css
    assert ".mconfig-discovery-add-button.is-added:disabled" in css
    assert ".mconfig-discovery-add-button.is-configured-missing:disabled" in css


def test_maintenance_discovery_reuses_setup_hardware_cards_and_badges():
    js = _read("admin.js")
    card = js.split("function renderMaintenanceDiscoveryCard", 1)[1].split(
        "\nfunction renderMaintenanceDiscoveryReview", 1
    )[0]
    review = js.split("function renderMaintenanceDiscoveryReview", 1)[1].split(
        "\nlet mconfigDiscovering", 1
    )[0]

    # Candidates render as the same collapsible hardware cards the setup
    # "Add more devices" row uses, with facts and source badges in the body.
    assert "mconfig-discovery-device-card" in card
    assert "mconfigHardwareCard" in card
    assert "mconfigAppendSourceBadges" in card
    assert '"device-facts"' in card
    assert '"config-available-list-style"' in review
    assert "Labels:" not in review


def test_setup_unified_overview_uses_hardware_card_list_layout():
    js = _read("admin.js")
    html = _read("index.html")
    card = js.split("function renderUnifiedDeviceCard", 1)[1].split(
        "\nfunction ", 1
    )[0]

    # Same collapsible hardware-card list layout as the maintenance/config list.
    assert '"hardware-card hardware-card-' in card
    assert '"hardware-card-head"' in card
    assert '"hardware-card-summary"' in card
    assert '"hardware-card-body"' in card
    assert "data-unified-toggle" in card
    # Facts grid mirrors the config list: IP / Serial / API family / Type.
    for label in ('"IP"', '"Serial"', '"API family"', '"Type"'):
        assert label in card
    # The one setup-specific extra is kept.
    assert "Selected by priority: " in card
    # The overview list uses the vertical list layout, not the tile grid.
    assert '<div id="unified-list" class="config-available-list-style"' in html


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
            "normalizeSerial",
            "usableSerialValue",
            "physicalInverterIdentity",
            "mconfigDiscoveryRole",
            "mconfigFindInverterMatch",
            "maintenanceMqttProposals",
            "mconfigIsMqttDevice",
            "buildMaintenanceDiscoveryReview",
        )
    )
    # Isolated harness has no live discovery session; default to no MQTT proposals.
    stub = "const discoverySessions = {maintenance: {mqttProposals: []}};\n"
    result = subprocess.run(
        [node, "-e", stub + helpers + "\n" + setup],
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
  openHardware: new Set(),
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
            "nextCompactInverterName",
            "mconfigNextInverterName",
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
        "const mconfigState = {draft: {devices: [], grid_meter: {}}, previewFingerprint: null, openHardware: new Set()};\n"
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
    for forbidden in ("down -v", "docker rm -v", "clean install", "Reset stack"):
        assert forbidden not in js, forbidden
        assert forbidden not in html, forbidden
    # Descriptive warnings may discuss whether reinstallation remains possible;
    # the forbidden surface is an actionable reinstall control/command.
    assert ">Reinstall<" not in html
    assert '"Reinstall"' not in js


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
        "</main>", 1
    )[0]
    assert 'id="backup-create"' in panel
    assert 'id="backup-list"' in panel


def test_backup_restore_uses_control_stage_style():
    html = _read("index.html")
    panel = html.split('id="maintenance-backup-panel"', 1)[1].split(
        "</main>", 1
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


def test_restore_stage_has_no_redundant_preview_button():
    html = _read("index.html")
    js = _read("admin.js")
    restore_stage = html.split('id="backup-restore-stage"', 1)[1].split(
        "</main>", 1
    )[0]
    # The archive/set is already chosen from Backup management, so the stage-level
    # "Preview restore" button is redundant and must be gone.
    assert 'id="backup-preview"' not in html
    assert "Preview restore" not in restore_stage
    # The destructive Restore action stays.
    assert 'id="backup-execute"' in restore_stage
    # The frontend no longer wires or references the removed button.
    assert "backupEls.previewBtn" not in js
    assert "backup-preview" not in js
    # The row-level "Restore preview" action still selects/opens stage 05.
    assert 'data-backup-action="restore"' in js
    assert "Restore preview" in js


def test_restore_option_changes_auto_refresh_preview():
    js = _read("admin.js")
    # Toggling rollback / auto-rollback re-runs the preview automatically.
    assert (
        'backupEls.rollback.addEventListener("change", refreshRestorePreviewFromOptions)'
        in js
    )
    assert (
        'backupEls.autoRollback.addEventListener("change", refreshRestorePreviewFromOptions)'
        in js
    )
    refresh = _extract_fn(js, "refreshRestorePreviewFromOptions")
    # Options changed: drop the stale plan, block Restore, then re-preview so no
    # restore can run against an old preview.
    assert "backupState.restorePlan = null" in refresh
    assert "backupEls.executeBtn.disabled = true" in refresh
    assert "previewRestore()" in refresh


def test_previewRestore_ignores_superseded_responses():
    js = _read("admin.js")
    fn = _extract_fn(js, "previewRestore")
    # A per-request token guards against an earlier, slower preview overwriting a
    # newer one after the user changed options.
    assert "++backupState.previewToken" in fn
    assert "token !== backupState.previewToken" in fn


def test_backup_restore_has_no_conflict_policy_selector():
    html = _read("index.html")
    restore_stage = html.split('id="backup-restore-stage"', 1)[1].split(
        "</main>", 1
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


def test_backup_influxdb_row_allows_restore_via_ems_cli_flow():
    js = _read("admin.js")
    row = _extract_fn(js, "renderBackupRow")
    # InfluxDB rows keep Details and Delete, and now also offer Restore preview.
    assert 'data-backup-action="details"' in row
    assert 'data-backup-action="delete"' in row
    assert 'data-backup-action="restore"' in row
    # The info flag explains the EMS CLI restore flow instead of blocking.
    assert 'backup.backup_type === "influxdb"' in row
    assert "EMS CLI restore flow" in row
    assert "InfluxDB restore not supported" not in row
    # Only invalid archives keep the restore button force-disabled by markup.
    assert "const restoreDisabled = !backup.valid;" in row


def test_backup_set_row_renders_delete_action():
    js = _read("admin.js")
    row = _extract_fn(js, "renderBackupSetRow")
    # Set rows expose a Delete action (kind=set) so the second confirm can offer
    # metadata-only vs metadata-and-archives deletion.
    assert 'data-backup-action="delete"' in row
    assert 'data-backup-kind="set"' in row
    # The unrelated Restore preview action is still present on set rows.
    assert 'data-backup-action="restore"' in row


def test_backup_set_with_influxdb_member_allows_restore_preview():
    js = _read("admin.js")
    row = _extract_fn(js, "renderBackupSetRow")
    assert 'a.type === "influxdb"' in row
    # The set is no longer force-disabled just because it contains InfluxDB.
    assert "Admin restore not supported yet" not in row
    assert 'data-backup-restore-disabled="true"' not in row
    assert 'data-backup-action="restore"' in row
    assert "EMS CLI flow" in row


def test_backup_restore_confirm_mentions_influxdb_when_applicable():
    js = _read("admin.js")
    fn = _extract_fn(js, "executeRestore")
    # The confirm dialog warns about bundled InfluxDB data when the plan includes
    # an InfluxDB member.
    assert 'backup_type === "influxdb"' in fn
    assert "Bundled InfluxDB analytics data may be replaced" in fn


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


def test_backup_row_has_single_created_fact():
    js = _read("admin.js")
    row = _extract_fn(js, "renderBackupRow")
    # A duplicated Created pill regressed the row before; keep exactly one.
    assert row.count('backupRowFact("Created"') == 1


def test_restore_preview_summary_uses_spaced_facts():
    js = _read("admin.js")
    html = _read("index.html")
    plan = _extract_fn(js, "renderRestorePlan")
    # Summary counts render as control-pipeline facts so the label and value do
    # not touch (the old "Will restore0" glue).
    for label in (
        'backupFact("Will restore"',
        'backupFact("Will replace"',
        'backupFact("Will skip"',
    ):
        assert label in plan, label
    assert 'class="control-pipeline-fact"' in _extract_fn(js, "backupFact")
    # The summary container is a backup-stage control-pipeline value grid, so the
    # backup-scoped spacing rules apply to it.
    assert 'class="control-pipeline-values" id="backup-restore-summary"' in html


def test_backup_stage_control_pipeline_fact_css():
    css = _read("admin.css")
    for selector in (
        ".backup-stage .control-pipeline-values",
        ".backup-stage .control-pipeline-fact",
        ".backup-stage .control-pipeline-fact .maintenance-fact-label",
        ".backup-stage .control-pipeline-fact .maintenance-fact-value",
    ):
        assert selector in css, selector
    fact_rule = css.split(".backup-stage .control-pipeline-fact {", 1)[1].split("}", 1)[0]
    # space-between is what pushes the value off the label so they never touch.
    assert "justify-content: space-between" in fact_rule


def test_backup_row_metadata_uses_compact_fact_css():
    css = _read("admin.css")
    meta_rule = css.split(".backup-row-meta {", 1)[1].split("}", 1)[0]
    # Metadata stays a compact wrapping row of pills, not full-width stacked cards.
    assert "flex-wrap: wrap" in meta_rule
    fact_rule = css.split(".backup-row-fact {", 1)[1].split("}", 1)[0]
    assert "inline-flex" in fact_rule
    assert "font-size" in fact_rule
    assert ".backup-row-fact strong" in css


def test_backup_row_type_label_sits_above_filename():
    js = _read("admin.js")
    row = _extract_fn(js, "renderBackupRow")
    # The type label carries its own class and stays a source badge, and it is
    # emitted before the filename span so it stacks above it.
    assert 'class="backup-row-type source-badge source-mdns"' in row
    type_idx = row.index('class="backup-row-type source-badge source-mdns"')
    name_idx = row.index('class="backup-row-name"')
    assert type_idx < name_idx
    # The label reuses the existing displayed value and is not special-cased to
    # "config" (that would hide non-config backup types such as databases).
    assert 'backup.backup_type || "backup"' in row
    assert '"config"' not in _backup_row_type_label(row)
    # The filename keeps a title attribute holding the full name for hover.
    assert 'title="' in row
    assert "backup.name || backup.id" in row


def _backup_row_type_label(row):
    # Isolate the type-label span so the assertion cannot be fooled by an
    # unrelated "config" default elsewhere in the row (e.g. the restore button's
    # data-backup-type fallback).
    start = row.index('class="backup-row-type source-badge source-mdns"')
    return row[start:row.index("backup-row-name", start)]


def test_backup_set_row_type_label_sits_above_name():
    js = _read("admin.js")
    row = _extract_fn(js, "renderBackupSetRow")
    assert 'class="backup-row-type source-badge source-scan"' in row
    type_idx = row.index('class="backup-row-type source-badge source-scan"')
    name_idx = row.index('class="backup-row-name"')
    assert type_idx < name_idx
    # The set label is still "set" and the name span carries a hover title.
    assert ">set</span>" in row
    assert 'title="' in row
    assert "set.label || set.id" in row


def test_backup_row_main_stacks_vertically_and_name_never_wraps():
    css = _read("admin.css")
    main_rule = css.split(".backup-row-main {", 1)[1].split("}", 1)[0]
    assert "flex-direction: column" in main_rule
    name_rule = css.split(".backup-row-name {", 1)[1].split("}", 1)[0]
    assert "white-space: nowrap" in name_rule
    # The mobile media query must no longer relax the filename to wrap.
    mobile = css.split("@media (max-width: 860px) {", 1)[1].split("\n}", 1)[0]
    assert "white-space: normal" not in mobile
    assert "overflow-wrap: anywhere" not in mobile


# --- Admin auth gate -------------------------------------------------------


def test_index_has_auth_gate_views_and_logout():
    html = _read("index.html")
    assert 'id="view-auth"' in html
    assert 'id="auth-create"' in html
    assert 'id="auth-login"' in html
    assert 'id="auth-logout"' in html
    # The create-password view frames the password as shared with the Dashboard.
    assert "shared with the EMS Dashboard" in html
    assert "Use your EMS Dashboard password." in html


def test_auth_password_inputs_have_no_length_requirement():
    html = _read("index.html")
    js = _read("admin.js")
    # Short passwords are allowed: no HTML length floor on the password inputs and
    # no user-facing copy demanding a minimum length.
    assert 'minlength="8"' not in html
    assert "minlength" not in html
    for text in ("at least 8 characters", "8 characters", "at least 8"):
        assert text not in html
        assert text not in js
    # The old too-short error message is gone from the auth message map.
    assert "password_too_short" not in js


def test_auth_form_is_compact_and_pins_fields_to_content_height():
    css = _read("admin.css")
    form = css.split(".admin-auth-form {", 1)[1].split("}", 1)[0]
    # Fields stack with a tight 8-10px rhythm instead of the wide default gap.
    assert "gap: 10px" in form
    # The shared .field flex-basis is neutralised inside the auth form so fields
    # size to their content and the submit button sits right below them.
    field = css.split(".admin-auth-form .field {", 1)[1].split("}", 1)[0]
    assert "flex: 0 0 auto" in field


def test_js_checks_auth_status_before_bootstrapping_workflows():
    js = _read("admin.js")
    assert "/api/admin/auth/status" in js
    # The install-state / discovery bootstrap only runs once authenticated.
    show = _extract_fn(js, "showAuthenticatedApp")
    bootstrap = _extract_fn(js, "bootstrapAuthenticatedAppOnce")
    assert "bootstrapAuthenticatedAppOnce()" in show
    assert "resumeAuthenticatedWorkflows()" in show
    assert "loadInstallState()" in bootstrap
    assert "pollMdns()" in bootstrap
    assert "loadMqttBrokers()" in bootstrap
    # There is no unconditional top-level bootstrap anymore.
    assert "\nloadInstallState();" not in js
    # The auth gate is what runs at startup.
    assert "refreshAuthStatus();" in js


def test_authenticated_workflow_resume_has_one_single_flight_orchestrator():
    js = _read("admin.js")
    resume = _async_fn_body(js, "async function resumeAuthenticatedWorkflows")
    perform = _async_fn_body(
        js, "async function performAuthenticatedWorkflowResume"
    )

    assert "authenticatedWorkflowResumeInFlight" in resume
    assert "return await authenticatedWorkflowResumeInFlight" in resume
    assert "performAuthenticatedWorkflowResume()" in resume
    assert "loadSystemAlignmentStatus()" in perform
    assert "resumeGuidedSetupFromTransition(" in perform
    assert "resumeGuidedUpgradeFromTransition(" in perform


def test_js_posts_to_auth_setup_login_logout_endpoints():
    js = _read("admin.js")
    assert "/api/admin/auth/setup" in js
    assert "/api/admin/auth/login" in js
    assert "/api/admin/auth/logout" in js


def test_js_attaches_csrf_token_to_authenticated_posts():
    js = _read("admin.js")
    assert "X-CSRF-Token" in js
    assert "authState.csrfToken" in js
    # Public auth endpoints are exempt from the CSRF attach.
    assert "AUTH_PUBLIC_POST_PATHS" in js


def test_js_returns_to_auth_view_on_401_or_403():
    js = _read("admin.js")
    assert "resp.status === 401 || resp.status === 403" in js
    lost = js.split("function onAuthLost", 1)[1].split("\nasync function ", 1)[0]
    assert "refreshAuthStatus()" in lost


def test_js_logout_button_calls_endpoint_once_authenticated():
    js = _read("admin.js")
    logout = js.split("async function submitLogout", 1)[1].split("\n\n", 1)[0]
    assert "/api/admin/auth/logout" in logout
    assert 'authEls.logout.addEventListener("click", submitLogout)' in js


def test_admin_frontend_defines_is_authenticated_helper():
    js = _read("admin.js")
    assert "function isAuthenticated()" in js
    assert "authState.authenticated" in js


def test_admin_frontend_mdns_poll_checks_auth_before_fetch():
    js = _read("admin.js")
    fn = js.split("async function pollMdns", 1)[1].split("\nasync function ", 1)[0]
    # The recurring interval poller no-ops while unauthenticated, before any fetch.
    assert "isAuthenticated()" in fn
    assert fn.find("isAuthenticated") < fn.find("fetch(")


def test_admin_frontend_mqtt_load_checks_auth_before_fetch():
    js = _read("admin.js")
    fn = js.split("async function loadMqttBrokers", 1)[1].split(
        "\nasync function ", 1
    )[0]
    assert "isAuthenticated()" in fn
    assert fn.find("isAuthenticated") < fn.find("fetch(")


def test_index_has_auth_recovery_panel_without_password_forms():
    html = _read("index.html")
    recovery = html.split('id="auth-recovery"', 1)[1].split("</section>", 1)[0]
    assert "Password file needs repair" in recovery
    assert "config/dashboard-auth.json" in recovery
    assert 'id="auth-recovery-retry"' in recovery
    # The recovery block offers a Retry action, never a password form.
    assert "<form" not in recovery
    assert "type=\"password\"" not in recovery


def test_js_recovery_status_blocks_password_forms():
    js = _read("admin.js")
    fn = js.split("function applyAuthStatus", 1)[1].split("\nfunction ", 1)[0]
    # recovery_required routes to the repair panel, not create/login.
    assert "recovery_required" in fn
    assert 'showAuthView("recovery")' in fn
    assert fn.find('showAuthView("recovery")') < fn.find('showAuthView("create")')
    # The Retry button re-checks the shared auth status.
    assert 'authEls.recoveryRetry.addEventListener("click", refreshAuthStatus)' in js


def test_admin_js_uses_partial_scan_progress_for_progress_bar():
    js = _read("admin.js")
    fn = js.split("function discoveryProgressPercent", 1)[1].split("\nfunction ", 1)[0]
    # A running scan adds a partial host fraction instead of only whole units.
    assert "scanHostFraction" in fn
    frac = js.split("function scanHostFraction", 1)[1].split("\nfunction ", 1)[0]
    assert "checked_hosts" in frac
    assert "total_hosts" in frac


def test_admin_js_progress_text_shows_hosts_checked_when_available():
    js = _read("admin.js")
    detail = js.split("function activeScanHostDetail", 1)[1].split("\nfunction ", 1)[0]
    assert "checked_hosts" in detail
    assert "total_hosts" in detail
    assert "hosts" in detail
    # Both setup and maintenance progress text render the active scan detail.
    assert js.count("activeScanHostDetail(session)") >= 2


def test_admin_js_progress_still_handles_old_results_without_progress():
    js = _read("admin.js")
    # Polling only forwards progress when the result carries it.
    scan_fn = js.split("async function maintenanceScanNetwork", 1)[1].split(
        "\nfunction ", 1
    )[0]
    assert "result.progress" in scan_fn
    # A scan without host progress contributes a zero fraction (no crash/NaN).
    frac = js.split("function scanHostFraction", 1)[1].split("\nfunction ", 1)[0]
    assert "total_hosts > 0" in frac
    assert "? " in frac and ": 0" in frac


def test_index_has_mqtt_credential_pool_form_not_broker_config():
    html = _read("index.html")
    # The Discovery credential pool form exposes only label/username/password.
    assert 'id="mqtt-credential-form"' in html
    assert 'id="mqtt-credential-label"' in html
    assert 'id="mqtt-credential-username"' in html
    assert 'id="mqtt-credential-password"' in html
    assert 'id="mqtt-credential-save"' in html
    assert 'id="mqtt-credential-list"' in html
    assert 'id="mqtt-credential-empty"' in html
    # No broker-specific connection config lives in the Discovery UI anymore.
    for gone in (
        'id="local-broker-form"',
        'id="local-broker-host"',
        'id="local-broker-port"',
        'id="local-broker-tls-mode"',
        'id="local-broker-auth"',
    ):
        assert gone not in html
    # Required copy: devices are only detected; adding happens in the config step.
    assert "Optional discovery credentials" in html
    assert "adding them happens in the config step" in html


def test_js_saves_and_deletes_mqtt_credentials_via_pool_endpoint():
    js = _read("admin.js")
    assert '"/api/discovery/connections/mqtt-credentials"' in js
    assert (
        '"/api/discovery/connections/mqtt-credentials/" + encodeURIComponent' in js
    )
    # The Discovery UI no longer calls broker-specific CRUD to add connections.
    assert '"/api/discovery/connections/mqtt-brokers"' not in js
    save_fn = js.split("async function saveMqttCredential", 1)[1].split(
        "\nasync function ", 1
    )[0]
    assert '"POST"' in save_fn
    # The rendered credential card only shows redacted status, never a password.
    render_fn = js.split("function renderMqttCredentialCard", 1)[1].split(
        "\nfunction ", 1
    )[0]
    assert "username_configured" in render_fn
    assert "password_configured" in render_fn
    assert ".password" not in render_fn.replace("password_configured", "")


def test_js_broker_cards_render_redacted_attempt_statuses():
    js = _read("admin.js")
    assert "function renderMqttAttemptRow" in js
    assert "mqttAttemptStatusLabel" in js
    card = js.split("function renderMqttBrokerCard", 1)[1].split("\nfunction ", 1)[0]
    assert "broker.attempts" in card
    # Attempt labels/statuses are escaped before reaching innerHTML.
    row = js.split("function renderMqttAttemptRow", 1)[1].split("\nfunction ", 1)[0]
    assert "escapeHtml(" in row
    assert "username" not in row
    assert "password" not in row


def _zendure_mqtt_panel(html):
    start = html.index('id="maintenance-zendure-mqtt"')
    return html[start : html.index("</section>", start)]


def test_index_has_zendure_mqtt_runtime_status_card():
    html = _read("index.html")
    assert 'id="maintenance-zendure-mqtt"' in html
    assert 'id="maintenance-zendure-mqtt-list"' in html
    assert 'id="maintenance-zendure-mqtt-empty"' in html
    assert "Zendure MQTT telemetry" in html
    panel = _zendure_mqtt_panel(html)
    # The panel describes only its own read-only behavior and offers no
    # control/apply action.
    assert "read-only" in panel.lower()
    assert "does not send commands" in panel.lower()


def test_maintenance_zendure_mqtt_panel_does_not_claim_runtime_sends_no_commands():
    html = _read("index.html")
    panel = _zendure_mqtt_panel(html)
    # The Maintenance panel is read-only, but the EMS runtime may still be
    # actively controlling configured MQTT devices — the copy must say so and
    # must not imply the whole system sends no MQTT control commands.
    assert "may still be controlled by the EMS runtime" in panel
    for stale in (
        "no MQTT control commands are sent",
        "Output write disabled",
        "Read-only telemetry from EMS",
    ):
        assert stale not in html


def test_js_mqtt_credential_card_reports_legacy_unencrypted_truthfully():
    js = _read("admin.js")
    render = js.split("function renderMqttCredentialCard", 1)[1].split(
        "\nfunction ", 1
    )[0]
    # A legacy unencrypted (base64) record is labeled truthfully — it needs a
    # re-save — rather than being presented as safely stored.
    assert "credentials_encrypted" in render
    assert "not encrypted" in render
    assert "re-save" in render.lower()


def test_js_fetches_zendure_mqtt_runtime_status_endpoint():
    js = _read("admin.js")
    assert '"/api/admin/maintenance/zendure-mqtt/runtime-status"' in js
    assert "async function loadZendureMqttRuntimeStatus" in js
    # It is refreshed as part of the existing maintenance overview reload.
    overview = js.split("async function loadMaintenanceOverview", 1)[1].split(
        "\nif (maintenanceEls.refresh)", 1
    )[0]
    assert "loadZendureMqttRuntimeStatus()" in overview


def test_js_zendure_mqtt_cards_escape_and_show_safety_labels():
    js = _read("admin.js")
    card = js.split("function renderZendureMqttDeviceCard", 1)[1].split(
        "\nfunction zmqttFact", 1
    )[0]
    # Every dynamic value reaches innerHTML through escapeHtml.
    assert "escapeHtml(" in card
    assert "device.name" in card
    assert "device.status" in card
    assert "metric_count" in card or "metricCount" in card
    assert "Capabilities" in card
    # The per-device footer states this is a telemetry view; it must not imply
    # global output control is disabled.
    assert "Telemetry view only" in card
    assert "Output write disabled" not in card
    # No control/publish/restart wording is introduced by the status card.
    for banned in ("properties/write", "function/invoke", "publish", "restart"):
        assert banned not in card


def test_js_zendure_mqtt_unknown_state_degrades_neutrally():
    js = _read("admin.js")
    render = js.split("function renderZendureMqttRuntimeStatus", 1)[1].split(
        "\nasync function ", 1
    )[0]
    # An unknown runtime_state (version skew: an already-open page rendering a
    # newer backend state) must degrade to the muted tone and echo the state
    # name — never light up the card with the red warn tone.
    assert 'ZENDURE_MQTT_STATUS_TONES[state] || "muted"' in render
    assert '|| "warn"' not in render


def test_js_zendure_mqtt_status_has_graceful_failure_state():
    js = _read("admin.js")
    loader = js.split("async function loadZendureMqttRuntimeStatus", 1)[1].split(
        "\nasync function ", 1
    )[0].split("\nfunction ", 1)[0]
    assert 'runtime_state: "unavailable"' in loader
    assert "renderZendureMqttRuntimeStatus" in loader


def test_index_has_zendure_mqtt_source_and_fallback_elements():
    html = _read("index.html")
    assert 'id="maintenance-zendure-mqtt-source"' in html
    assert 'id="maintenance-zendure-mqtt-fallback"' in html


def test_js_renders_zendure_mqtt_live_and_offline_source_labels():
    js = _read("admin.js")
    labels = js.split("ZENDURE_MQTT_SOURCE_LABELS", 1)[1].split("};", 1)[0]
    assert "Live EMS runtime" in labels
    assert "Offline config check" in labels
    render = js.split("function renderZendureMqttRuntimeStatus", 1)[1].split(
        "\nasync function ", 1
    )[0]
    # Honest source line + config-derived fallback note, no overstated freshness.
    assert '"Source: "' in render
    assert "live_available" in render
    assert "config-derived telemetry setup" in render


def test_js_zendure_mqtt_render_has_no_write_or_control_wording():
    js = _read("admin.js")
    render = js.split("function renderZendureMqttRuntimeStatus", 1)[1].split(
        "\nasync function ", 1
    )[0]
    for banned in ("restart", "apply", "publish", "properties/write", "function/invoke"):
        assert banned not in render.lower()


# --- manual Zendure MQTT broker + telemetry-only device setup -------------


def test_index_has_zendure_mqtt_broker_fields():
    html = _read("index.html")
    for element in (
        'id="config-mqtt-broker-host"',
        'id="config-mqtt-broker-port"',
        'id="config-mqtt-broker-security"',
        'id="config-mqtt-broker-username"',
        'id="config-mqtt-broker-password"',
    ):
        assert element in html
    assert "Zendure MQTT broker" in html
    # The broker password never rides in a plain-text input.
    assert 'id="config-mqtt-broker-password" type="password"' in html


def test_index_has_zendure_hardware_generation_select():
    html = _read("index.html")
    assert 'id="config-mqtt-device-generation"' in html
    assert "Zendure hardware generation" in html
    assert 'id="config-mqtt-device-serial"' in html


def test_index_zendure_mqtt_copy_is_user_friendly():
    html = _read("index.html")
    assert "Zendure MQTT device" in html
    # Raw internal topic-family identifiers must never appear in the UI copy.
    for internal in ("zensdk_ha_scalar", "legacy_zendure_json", "topic_family"):
        assert internal not in html


def test_js_does_not_persist_broker_password_in_localstorage():
    js = _read("admin.js")
    saver = js.split("function saveStoredBroker", 1)[1].split("\nfunction ", 1)[0]
    # Only non-secret broker fields are stored; the password stays in memory.
    assert "brokerPassword" not in saver
    assert "password" not in saver.lower()


def test_js_manual_mqtt_device_payload_carries_selected_generation():
    js = _read("admin.js")
    adder = js.split("function addManualMqttDevice", 1)[1].split("\nfunction ", 1)[0]
    assert "generation: generation.id" in adder
    payload = js.split("function manualMqttDevicesPayload", 1)[1].split(
        "\nfunction ", 1
    )[0]
    assert "generation:" in payload
    assert "serial_number:" in payload


def test_index_has_separate_physical_serial_and_mqtt_device_id_fields():
    html = _read("index.html")
    # Fresh Setup exposes the physical serial and the MQTT route id as two
    # distinct manual inputs; the serial is never overloaded as the route id.
    assert "Physical serial number" in html
    assert 'id="config-mqtt-device-serial"' in html
    assert 'id="config-mqtt-device-mqttid"' in html
    assert "Serial number / device ID" not in html


def test_js_manual_mqtt_payload_carries_explicit_route_device_id():
    js = _read("admin.js")
    payload = js.split("function manualMqttDevicesPayload", 1)[1].split(
        "\nfunction ", 1
    )[0]
    assert "mqtt_device_id:" in payload


def test_js_manual_mqtt_control_requires_explicit_route_device_id():
    js = _read("admin.js")
    adder = js.split("function addManualMqttDevice", 1)[1].split("\nfunction ", 1)[0]
    # Output control cannot be enabled from manual entry without an explicit MQTT
    # device ID; the serial is never used as the route id.
    assert "wantsControl && !mqttId" in adder
    assert "MQTT device ID is required" in adder


def test_maintenance_config_has_zendure_mqtt_broker_fields():
    html = _read("index.html")
    editor = html.split('id="maintenance-config-editor"', 1)[1]
    for element in (
        'id="maintenance-mqtt-broker-host"',
        'id="maintenance-mqtt-broker-port"',
        'id="maintenance-mqtt-broker-security"',
        'id="maintenance-mqtt-broker-username"',
        'id="maintenance-mqtt-broker-password"',
        'id="maintenance-mqtt-broker-clear"',
    ):
        assert element in editor
    # The existing password is never rendered into a plain-text field.
    assert 'id="maintenance-mqtt-broker-password" type="password"' in editor


def test_maintenance_and_setup_use_same_generation_label_source():
    js = _read("admin.js")
    # Both flows read the user-facing generation list from the shared catalog,
    # never a hard-coded per-flow label list.
    assert "setupCatalog.zendure_mqtt_generations" in js
    assert "mconfigState.catalog.zendure_mqtt_generations" in js


def test_maintenance_renders_zendure_mqtt_device_without_ip_host():
    js = _read("admin.js")
    render = js.split("function renderMaintenanceZendureMqttDevice", 1)[1].split(
        "\nfunction ", 1
    )[0]
    assert '"IP / host"' not in render
    # serial_number and the MQTT routing id are distinct editable fields (one
    # input must never overwrite the other).
    assert '"Serial number"' in render
    assert '"MQTT device ID"' in render
    assert '"Zendure hardware generation"' in render
    assert '"Exact hardware model"' in render
    assert '"Write protocol"' in render
    assert '"Validation maturity"' in render
    assert '"Supported operations"' in render
    assert '"Current control readiness"' in render
    # The maintenance editor is capability-aware: it offers output control for a
    # supported generation and stays telemetry-only otherwise.
    assert '"Output control"' in render
    assert "mconfigMqttControlSupported" in render
    # Control is gated by capability, never forced on an unsupported family.
    assert "does not send MQTT control commands" not in render
    # Exactly one MQTT device ID field is rendered (no duplicate row).
    assert render.count('"MQTT device ID"') == 1
    # The route field writes only mqtt.device_id; the physical serial and the
    # legacy top-level device_id are never synchronized by any callback here.
    assert "device.mqtt.device_id = trimmed" in render
    assert "device.device_id = trimmed" not in render
    assert "device.device_id = v" not in render


def test_maintenance_mqtt_control_default_is_route_aware():
    js = _read("admin.js")
    sync = js.split("const syncGenerationFields", 1)[1].split("\n  };", 1)[0]
    # Output control can only default on, or stay on, with a complete write route
    # that includes the explicit MQTT route device id.
    assert "routeDeviceId" in sync
    assert "routeComplete" in sync
    assert "mconfigMqttShouldDefaultControl(device, supported, routeComplete)" in sync


def test_maintenance_discovery_review_can_show_mqtt_proposals():
    js = _read("admin.js")
    build = js.split("function buildMaintenanceDiscoveryReview", 1)[1].split(
        "\nfunction ", 1
    )[0]
    assert "maintenanceMqttProposals()" in build
    assert "mqttProposal" in build
    # A dedicated proposal card renders the friendly generation, not the family.
    card = js.split("function renderMaintenanceMqttProposalCard", 1)[1].split(
        "\nfunction ", 1
    )[0]
    assert "mqttGenerationLabel(proposal)" in card
    assert "topic_family" not in card


def test_maintenance_broker_password_not_stored_in_localstorage():
    js = _read("admin.js")
    broker = js.split("function wireMaintenanceBrokerForm", 1)[1].split(
        "\nfunction ", 1
    )[0]
    assert "localStorage" not in broker
    sync = js.split("function syncMaintenanceBrokerForm", 1)[1].split(
        "\nfunction ", 1
    )[0]
    assert "localStorage" not in sync


# --- Zendure SmartMeter D0 guided setup ----------------------------------


def _run_grid_meter_node(setup):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for the grid-meter sync behavior test")
    js = _read("admin.js")
    # The D0 topic consts sit between gridMeterType and the next function, so the
    # gridMeterType extraction already carries them; only pieces declared
    # elsewhere in the file are stubbed here.
    preamble = """
const GRID_METER_TYPE_CHOICES = new Set([
  "shelly", "shelly_3em_gen1", "ecotracker", "zendure_grid_meter_http",
  "zendure_smartmeter_3ct_http", "tasmota_http", "zendure_smartmeter_d0",
  "mqtt", "ha",
]);
const GRID_METER_FAMILY_TYPES = {
  shelly_gen2: "shelly",
  shelly_3em_gen1: "shelly_3em_gen1",
  ecotracker: "ecotracker",
  zendure_grid_meter_http: "zendure_grid_meter_http",
  zendure_smartmeter_3ct_http: "zendure_grid_meter_http",
  tasmota_http: "tasmota_http",
};
let featureValues = {};
function saveFeatureValues() {}
"""
    # Extracting gridMeterType carries the D0 topic consts that sit before
    # zendureD0Topic.
    helpers = "\n".join(
        _extract_fn(js, name)
        for name in (
            "gridMeterType",
            "zendureD0Topic",
            "zendureD0SerialFromTopic",
            "effectiveD0TopicMode",
            "setD0TopicMode",
            "resolveZendureD0Serial",
            "syncZendureD0FeatureValues",
            "syncGridMeterFeatureValues",
        )
    )
    script = preamble + helpers + "\n" + setup
    result = subprocess.run(
        [node, "-e", script], text=True, capture_output=True, check=False
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_js_d0_selection_generates_number_payload_and_topic_from_discovery_serial():
    out = _run_grid_meter_node(
        """
const meter = { grid_meter_type: "zendure_smartmeter_d0", serial_number: "D0SN", ip: "192.168.1.80" };
syncGridMeterFeatureValues(meter);
console.log(JSON.stringify(featureValues));
"""
    )
    assert out["grid_meter.type"] == "zendure_smartmeter_d0"
    assert out["grid_meter.mqtt.payload_format"] == "number"
    assert out["grid_meter.mqtt.topic"] == "Zendure/sensor/D0SN/totalPower"
    # D0 is MQTT-only: the HTTP IP must never be carried into the D0 preview.
    assert "grid_meter.ip" not in out


def test_js_d0_manually_entered_serial_generates_topic():
    out = _run_grid_meter_node(
        """
const meter = { grid_meter_type: "zendure_smartmeter_d0", serial_number: "MANUAL1" };
syncGridMeterFeatureValues(meter);
console.log(JSON.stringify(featureValues));
"""
    )
    assert out["grid_meter.mqtt.topic"] == "Zendure/sensor/MANUAL1/totalPower"


def test_js_d0_serial_change_updates_previously_generated_topic():
    out = _run_grid_meter_node(
        """
const meter = { grid_meter_type: "zendure_smartmeter_d0", serial_number: "OLD" };
syncGridMeterFeatureValues(meter);
meter.serial_number = "NEW";
syncGridMeterFeatureValues(meter);
console.log(JSON.stringify(featureValues));
"""
    )
    assert out["grid_meter.mqtt.topic"] == "Zendure/sensor/NEW/totalPower"


def test_js_d0_manually_customized_topic_is_not_overwritten():
    out = _run_grid_meter_node(
        """
featureValues["grid_meter.mqtt.topic"] = "custom/grid/power";
const meter = { grid_meter_type: "zendure_smartmeter_d0", serial_number: "D0SN" };
syncGridMeterFeatureValues(meter);
console.log(JSON.stringify(featureValues));
"""
    )
    assert out["grid_meter.mqtt.topic"] == "custom/grid/power"


def test_js_switching_away_from_d0_drops_mqtt_ip_expectations():
    # Selecting an HTTP meter type sets grid_meter.ip; D0-specific stale fields
    # are cleared by the backend variant normalization, but the frontend must not
    # keep asserting a D0 IP path.
    out = _run_grid_meter_node(
        """
const shelly = { grid_meter_type: "shelly", ip: "192.168.1.5" };
syncGridMeterFeatureValues(shelly);
console.log(JSON.stringify(featureValues));
"""
    )
    assert out["grid_meter.type"] == "shelly"
    assert out["grid_meter.ip"] == "192.168.1.5"


def test_js_zendure_http_candidate_resolves_to_generic_type():
    # A discovered Zendure HTTP grid meter (D0 or 3CT) is config-ready on numeric
    # total_power alone and resolves to the generic local-HTTP type, never a
    # silent Shelly fallback.
    out = _run_grid_meter_node(
        """
const meter = {
  grid_meter_type: "",
  api_family: "zendure_grid_meter_http",
  device_type: "zendure_grid_meter_http",
  ip: "192.168.1.80",
  serial_number: "D0SN",
};
const resolved = gridMeterType(meter, "shelly");
syncGridMeterFeatureValues(meter);
console.log(JSON.stringify({ resolved: resolved, features: featureValues }));
"""
    )
    assert out["resolved"] == "zendure_grid_meter_http"
    assert out["features"]["grid_meter.type"] == "zendure_grid_meter_http"
    assert out["features"]["grid_meter.ip"] == "192.168.1.80"


def test_js_shelly_candidate_resolves_to_concrete_type():
    # The counterpart: an unambiguous Shelly candidate resolves cleanly and may
    # be auto-applied.
    out = _run_grid_meter_node(
        """
const shelly = { grid_meter_type: "", api_family: "shelly_gen2", ip: "192.168.1.5" };
const resolved = gridMeterType(shelly, "shelly");
syncGridMeterFeatureValues(shelly);
console.log(JSON.stringify({ resolved: resolved, features: featureValues }));
"""
    )
    assert out["resolved"] == "shelly"
    assert out["features"]["grid_meter.type"] == "shelly"
    assert out["features"]["grid_meter.ip"] == "192.168.1.5"


def test_js_d0_auto_topic_mode_regenerates_on_serial_change():
    out = _run_grid_meter_node(
        """
const meter = { grid_meter_type: "zendure_smartmeter_d0", serial_number: "D0-A" };
syncGridMeterFeatureValues(meter);
const first = featureValues["grid_meter.mqtt.topic"];
meter.serial_number = "D0-B";
syncGridMeterFeatureValues(meter);
console.log(JSON.stringify({ first: first, second: featureValues["grid_meter.mqtt.topic"], mode: meter.d0_topic_mode }));
"""
    )
    assert out["first"] == "Zendure/sensor/D0-A/totalPower"
    assert out["second"] == "Zendure/sensor/D0-B/totalPower"
    assert out["mode"] == "auto"


def test_js_d0_manual_mode_preserves_topic_on_serial_change():
    out = _run_grid_meter_node(
        """
const meter = { grid_meter_type: "zendure_smartmeter_d0", serial_number: "D0-A" };
meter.d0_topic_mode = "manual";
featureValues["grid_meter.mqtt.topic"] = "custom/grid/power";
syncGridMeterFeatureValues(meter);
meter.serial_number = "D0-B";
syncGridMeterFeatureValues(meter);
console.log(JSON.stringify(featureValues));
"""
    )
    assert out["grid_meter.mqtt.topic"] == "custom/grid/power"


def test_js_d0_manual_canonical_looking_topic_is_not_overwritten():
    # A manual topic whose SHAPE looks canonical but names a different serial is
    # preserved when the serial changes — ownership is mode, not string shape.
    out = _run_grid_meter_node(
        """
const meter = { grid_meter_type: "zendure_smartmeter_d0", serial_number: "D0-A" };
meter.d0_topic_mode = "manual";
featureValues["grid_meter.mqtt.topic"] = "Zendure/sensor/MANUAL/totalPower";
syncGridMeterFeatureValues(meter);
meter.serial_number = "D0-B";
syncGridMeterFeatureValues(meter);
console.log(JSON.stringify(featureValues));
"""
    )
    assert out["grid_meter.mqtt.topic"] == "Zendure/sensor/MANUAL/totalPower"


def test_js_neutral_candidate_not_config_ready_blocks_auto_selection():
    # Auto-selection only considers config-ready meters. A neutral Zendure
    # candidate is not config-ready, so it can never be auto-applied as Shelly.
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for the auto-selection readiness test")
    js = _read("admin.js")
    helper = _extract_fn(js, "isAutoConfigReady")
    script = helper + """
const neutral = { verified: true, usable_for_config: false };
const shelly = { verified: true, usable_for_config: true };
console.log(JSON.stringify({
  neutral: isAutoConfigReady(neutral),
  shelly: isAutoConfigReady(shelly),
}));
"""
    result = subprocess.run(
        [node, "-e", script], text=True, capture_output=True, check=False
    )
    assert result.returncode == 0, result.stderr
    out = json.loads(result.stdout)
    assert out["neutral"] is False
    assert out["shelly"] is True


def test_js_grid_meter_body_shows_d0_serial_field():
    js = _read("admin.js")
    fn = js.split("function renderGridMeterFields", 1)[1].split("\nfunction ", 1)[0]
    assert "D0 serial number" in fn
    assert 'data-grid-field="serial_number"' in fn or "serial_number" in fn
    # The generated topic label is surfaced to the user.
    assert "Zendure/sensor/<serial>/totalPower" in fn
    # The required D0 serial input is a real required input, not CSS emulation.
    assert "required" in fn


def test_js_grid_type_select_has_placeholder_for_unresolved():
    js = _read("admin.js")
    fn = _extract_fn(js, "renderGridTypeSelect")
    assert "Select grid-meter type" in fn


# --- Guided Setup "Start over" -------------------------------------------


def test_index_has_start_over_button():
    html = _read("index.html")
    nav = html.split('class="setup-nav"', 1)[1].split("</div>", 1)[0]
    assert 'id="setup-start-over"' in nav
    assert "Start over" in nav


def test_js_start_over_resets_draft_and_returns_to_first_step():
    js = _read("admin.js")
    fn = js.split("function startGuidedSetupOver", 1)[1].split("\nfunction ", 1)[0]
    # Confirmation is required before anything is cleared.
    assert "window.confirm(START_OVER_CONFIRM)" in fn
    # Draft, dismissed set, feature values and discovery are all reset.
    assert "configDraftItems = []" in fn
    assert "clearFeatureValues()" in fn
    assert "resetDiscoverySession(discoverySessions.setup)" in fn
    assert "configDismissed.clear()" in fn
    # UI returns to the first Guided Setup step.
    assert 'setActiveStep("release")' in fn


def test_js_start_over_confirmation_states_what_is_and_is_not_deleted():
    js = _read("admin.js")
    const = js.split("const START_OVER_CONFIRM", 1)[1].split(";\n", 1)[0]
    assert "does not delete" in const
    assert "installed EMS system" in const
    assert "backups" in const


def test_js_start_over_does_not_call_destructive_endpoints():
    js = _read("admin.js")
    fn = js.split("function startGuidedSetupOver", 1)[1].split("\nfunction ", 1)[0]
    # Purely a client-side draft reset: no deployment/container/backup/config
    # deletion request may be issued from here.
    assert "fetch(" not in fn
    assert "DELETE" not in fn


def _run_start_over_node(setup):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for the Start over behavior test")
    js = _read("admin.js")
    # createInitialStartState's extraction tail bleeds into unrelated DOM setup,
    # so the four state factories are stubbed here (mirroring the reset-relevant
    # fields); the reset function itself is the real one under test.
    helpers = "\n".join(
        _extract_fn(js, name)
        for name in (
            "clearFeatureValues",
            "resetDiscoverySession",
            "clearGuidedSetupTimers",
            "startGuidedSetupOver",
        )
    )
    preamble = """
let fetchCalls = 0;
global.fetch = () => { fetchCalls += 1; return Promise.resolve({}); };
const removedTimers = [];
const window = {
  confirm: () => true,
  localStorage: { removeItem: () => {} },
  clearTimeout: (h) => { removedTimers.push(h); },
  setTimeout: () => 0,
};
const START_OVER_CONFIRM = "x";
function clearSetupOperationContext() {}
function clearMqttSelection() {}
const CONFIG_DRAFT_STORAGE_KEY = "d";
const CONFIG_DISMISSED_STORAGE_KEY = "x";
const CONFIG_DISMISSED_SERIALS_STORAGE_KEY = "s";
const CONFIG_FEATURES_STORAGE_KEY = "f";
const dismissedSerials = new Set(["eod1aaa"]);
// startGuidedSetupOver's extraction tail registers event listeners guarded by
// setupEls.*; an empty object makes every guard falsy so none of them run.
const setupEls = {};
function createInitialDevicesState() { return { status: "idle" }; }
function createInitialConfigState() { return { status: "empty" }; }
function createInitialDeploymentState() {
  return { generated_ready: false, prepared: false, status: "idle", job_id: null };
}
function createInitialStartState() { return { status: "idle", job_id: null }; }
function makeSession() {
  return {
    generation: 0, active: true, startedAt: 1,
    devices: new Map(), networks: new Map(),
    scanQueue: [1], scans: [1], scanKeys: new Set([1]),
    progress: { total: 3, done: 3, failed: 0, active: 0 },
  };
}
const discoverySessions = { setup: makeSession() };
const keptDevices = discoverySessions.setup.devices;
const mdnsDevices = new Map();
const ignoredMdnsDevices = new Map();
const mqttBrokers = new Map();
const autoScannedCidrs = new Set(["10.0.0.0/24"]);
let lastDiscoverySignature = "sig";
let scanning = true;
let devicesDiscoveryStarted = true;
let configDraftItems = [{ role: "grid_meter" }];
const configDismissed = new Set(["x"]);
let featureValues = { "grid_meter.type": "shelly" };
let latestConfigPreview = { ready: true };
let guidedSetupGeneration = 0;
let configPreviewTimer = 11;
let deploymentJobTimer = 22;
let startJobTimer = 33;
const setupState = {
  activeStep: "start",
  release: {},
  devices: {},
  config: {},
  deployment: { generated_ready: true, prepared: true, status: "succeeded", job_id: "old-deployment-job" },
  start: { status: "succeeded", job_id: "old-start-job" },
};
function setActiveStep(step) { setupState.activeStep = step; }
function showError() {}
function showSetupNavError() {}
function updateBusy() {}
function renderSetupDiscoveryProgress() {}
function renderAggregate() {}
function renderConfigDraft() {}
function renderConfigAvailable() {}
function renderConfigPreview() {}
function renderDeployment() {}
function renderStart() {}
function setStatus() {}
"""
    epilogue = """
const capturedGeneration = guidedSetupGeneration;
startGuidedSetupOver();
console.log(JSON.stringify({
  activeStep: setupState.activeStep,
  devicesDiscoveryStarted: devicesDiscoveryStarted,
  deployment: setupState.deployment,
  start: setupState.start,
  draftLen: configDraftItems.length,
  featureKeys: Object.keys(featureValues),
  removedTimers: removedTimers,
  discoveryGeneration: discoverySessions.setup.generation,
  discoveryActive: discoverySessions.setup.active,
  staleDetected: capturedGeneration !== guidedSetupGeneration,
  fetchCalls: fetchCalls,
  latestConfigPreview: latestConfigPreview,
  dismissedSerialsSize: dismissedSerials.size,
}));
"""
    script = preamble + helpers + "\n" + setup + "\n" + epilogue
    result = subprocess.run(
        [node, "-e", script], text=True, capture_output=True, check=False
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_js_start_over_full_reset_behavior():
    out = _run_start_over_node("")
    # Wizard returns to the first step and a new discovery scan is allowed again.
    assert out["activeStep"] == "release"
    assert out["devicesDiscoveryStarted"] is False
    # Deployment/start draft (generated/prepared flags and old job IDs) are gone.
    assert out["deployment"]["generated_ready"] is False
    assert out["deployment"]["prepared"] is False
    assert out["deployment"]["job_id"] is None
    assert out["start"]["job_id"] is None
    assert out["start"]["status"] == "idle"
    # Draft, feature values and preview are cleared.
    assert out["draftLen"] == 0
    assert out["featureKeys"] == []
    assert out["latestConfigPreview"] is None
    # The cross-transport serial-dismissal set is cleared too, so a re-scan can
    # rediscover devices removed in the previous run.
    assert out["dismissedSerialsSize"] == 0
    # All three wizard timers were cleared.
    assert sorted(out["removedTimers"]) == [11, 22, 33]
    # Discovery session was reset (generation bumped, no longer active).
    assert out["discoveryGeneration"] == 1
    assert out["discoveryActive"] is False
    # A response captured before the reset now detects it is stale.
    assert out["staleDetected"] is True
    # No network request (destructive or otherwise) was issued.
    assert out["fetchCalls"] == 0


# --- D0 MQTT grid-meter proposal UX --------------------------------------


def test_ui_grid_meter_proposal_uses_grid_meter_action_label():
    js = _read("admin.js")
    card = js.split("function renderMqttProposalCard", 1)[1].split(
        "\nfunction ", 1
    )[0]
    # A grid-meter proposal offers "Use as grid meter"; a device proposal keeps
    # "Add to config preview".
    assert '"Use as grid meter"' in card
    assert '"Add to config preview"' in card
    assert "isMqttGridMeterProposal(proposal)" in card


def _run_mqtt_proposal_node(setup):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for the MQTT proposal selection test")
    js = _read("admin.js")
    helpers = "\n".join(
        _extract_fn(js, name)
        for name in (
            "isMqttGridMeterProposal",
            "mqttGridMeterProposalTopic",
            "normalizeInverterAliasTokens",
            "serializeMqttProposalSelection",
            "mqttPreviewPayload",
            "selectedMqttGridMeterId",
            "hasSelectedHttpGridMeter",
            "toggleMqttPreviewProposal",
        )
    )
    preamble = """
const els = {};
globalThis.document = { getElementById: () => null };
const zendureMqttPreviewProposals = new Map();
let latestMqttProposals = [];
let httpGridMeterSelected = false;
let confirmResult = false;
globalThis.window = { confirm: () => confirmResult };
function saveMqttPreviewProposals() {}
function renderMqttProposals() {}
function renderConfigPreview() {}
function loadManualMqttDevices() { return []; }
function gridMeterItem() {
  return httpGridMeterSelected ? { role: "grid_meter", enabled: true } : null;
}
"""
    script = preamble + helpers + "\n" + setup
    result = subprocess.run(
        [node, "-e", script], text=True, capture_output=True, check=False
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_js_grid_meter_proposal_selection_sets_grid_target():
    out = _run_mqtt_proposal_node(
        """
latestMqttProposals = [{
  id: "d0",
  target: "grid_meter",
  grid_meter_fragment: {type: "zendure_smartmeter_d0", mqtt: {broker_ref: "local_mqtt", topic: "Zendure/sensor/D0SN/totalPower"}},
  broker_host: "10.0.0.9", broker_port: 1883, broker_tls: false,
  connection_source: "local_mqtt",
}];
toggleMqttPreviewProposal("d0");
console.log(JSON.stringify({payload: mqttPreviewPayload(), size: zendureMqttPreviewProposals.size}));
"""
    )
    assert out["size"] == 1
    entry = out["payload"][0]
    assert entry["target"] == "grid_meter"
    assert entry["grid_meter_fragment"]["type"] == "zendure_smartmeter_d0"
    # A grid-meter proposal never carries a devices[] config fragment.
    assert entry.get("config_fragment") is None


def test_js_only_one_grid_meter_proposal_stays_selected():
    out = _run_mqtt_proposal_node(
        """
function grid(id) { return {
  id: id, target: "grid_meter",
  grid_meter_fragment: {type: "zendure_smartmeter_d0", mqtt: {broker_ref: "local_mqtt", topic: "Zendure/sensor/" + id + "/totalPower"}},
  connection_source: "local_mqtt",
}; }
latestMqttProposals = [grid("A"), grid("B")];
toggleMqttPreviewProposal("A");
toggleMqttPreviewProposal("B");
console.log(JSON.stringify({selected: selectedMqttGridMeterId(), size: zendureMqttPreviewProposals.size}));
"""
    )
    # Selecting B replaced A: exactly one grid meter remains.
    assert out["size"] == 1
    assert out["selected"] == "B"


def test_js_grid_meter_replaces_http_meter_only_after_confirmation():
    # Without confirmation, an HTTP meter is not replaced (nothing selected).
    denied = _run_mqtt_proposal_node(
        """
httpGridMeterSelected = true;
confirmResult = false;
latestMqttProposals = [{
  id: "d0", target: "grid_meter",
  grid_meter_fragment: {type: "zendure_smartmeter_d0", mqtt: {broker_ref: "local_mqtt", topic: "Zendure/sensor/D0SN/totalPower"}},
  connection_source: "local_mqtt",
}];
toggleMqttPreviewProposal("d0");
console.log(JSON.stringify({size: zendureMqttPreviewProposals.size, payload: mqttPreviewPayload()}));
"""
    )
    assert denied["size"] == 0

    approved = _run_mqtt_proposal_node(
        """
httpGridMeterSelected = true;
confirmResult = true;
latestMqttProposals = [{
  id: "d0", target: "grid_meter",
  grid_meter_fragment: {type: "zendure_smartmeter_d0", mqtt: {broker_ref: "local_mqtt", topic: "Zendure/sensor/D0SN/totalPower"}},
  connection_source: "local_mqtt",
}];
toggleMqttPreviewProposal("d0");
console.log(JSON.stringify({size: zendureMqttPreviewProposals.size, payload: mqttPreviewPayload()}));
"""
    )
    assert approved["size"] == 1
    assert approved["payload"][0]["replace_grid_meter"] is True


def test_js_grid_meter_proposal_payload_carries_no_secrets():
    out = _run_mqtt_proposal_node(
        """
latestMqttProposals = [{
  id: "d0", target: "grid_meter",
  grid_meter_fragment: {type: "zendure_smartmeter_d0", mqtt: {broker_ref: "local_mqtt", topic: "Zendure/sensor/D0SN/totalPower"}},
  broker_host: "10.0.0.9", broker_port: 1883, broker_tls: false,
  connection_source: "local_mqtt",
  broker_password: "secret", app_key: "APPKEY",
}];
toggleMqttPreviewProposal("d0");
console.log(JSON.stringify(mqttPreviewPayload()));
"""
    )
    blob = json.dumps(out).lower()
    for secret in ("secret", "appkey", "password", "app_key", "token"):
        assert secret not in blob


# --- unified hardware editor (Maintenance ↔ Setup parity) -------------------


def test_maintenance_device_cards_are_catalog_driven():
    js = _read("admin.js")
    # The hand-coded device field list is gone; rows come from the shared
    # hardware catalog (mconfigState.catalog.hardware_sections) so Maintenance
    # renders exactly the fields the setup flow does.
    assert "MCONFIG_DEVICE_NUMBERS" not in js
    assert "function mconfigHardwareSection" in js
    assert "function mconfigDeviceCatalogFields" in js
    fn = js.split("function renderMaintenanceInverter", 1)[1].split(
        "\nfunction ", 1
    )[0]
    # Both editors share one catalog-driven renderer pair: connection identity
    # plus the transport-independent common tuning fields.
    assert "renderLocalApiConnectionFields" in fn
    assert "renderCommonInverterFields" in fn
    common = js.split("function renderCommonInverterFields", 1)[1].split(
        "\nfunction ", 1
    )[0]
    assert "mconfigDeviceCatalogFields" in common
    assert "mconfigLevelledFields" in common
    mqtt_editor = js.split("function renderMaintenanceZendureMqttDevice", 1)[1].split(
        "\nfunction mconfigIsMqttDevice", 1
    )[0]
    assert "renderCommonInverterFields" in mqtt_editor


def test_maintenance_levelled_fields_defined_once_and_shared():
    js = _read("admin.js")
    # One level-splitting implementation renders normal fields first and nests
    # advanced/expert in collapsed details — shared by feature and device cards.
    assert js.count("function mconfigLevelledFields") == 1
    fn = js.split("function mconfigLevelledFields", 1)[1].split("\nfunction ", 1)[0]
    assert "feature-advanced" in fn
    assert "feature-expert" in fn
    feature_body = js.split("function mconfigFeatureBody", 1)[1].split(
        "\nfunction ", 1
    )[0]
    assert "mconfigLevelledFields" in feature_body


def test_maintenance_catalog_rows_carry_units_and_descriptions():
    js = _read("admin.js")
    # Catalog-driven rows pass label, description and unit through the shared
    # row builder so Maintenance cards read like the setup cards.
    assert "function mconfigCatalogRow" in js
    fn = js.split("function mconfigCatalogRow", 1)[1].split("\nfunction ", 1)[0]
    assert "field.label" in fn
    assert "field.description" in fn
    assert "field.unit" in fn


def test_maintenance_grid_meter_is_variant_driven():
    js = _read("admin.js")
    # The grid-meter card renders variant-specific fields from the shared
    # catalog (grid_meter_variants + hardware_sections), like the setup flow.
    assert "function mconfigGridMeterVariant" in js
    assert "function mconfigGridMeterCatalogFields" in js
    variant_fn = js.split("function mconfigGridMeterVariant", 1)[1].split(
        "\nfunction ", 1
    )[0]
    assert "grid_meter_variants" in variant_fn
    render_fn = js.split("function renderMaintenanceGridMeter", 1)[1].split(
        "\nfunction ", 1
    )[0]
    assert "mconfigGridMeterCatalogFields" in render_fn
    assert "mconfigLevelledFields" in render_fn
    # The old hard-coded field rows are gone.
    assert "Optional Shelly channels or phases" not in js


def test_maintenance_grid_meter_d0_serial_flow_matches_setup():
    js = _read("admin.js")
    # Maintenance reuses the setup D0 helpers: the serial generates
    # Zendure/sensor/<serial>/totalPower and the raw topic is demoted to
    # Advanced instead of asking users for a raw MQTT topic.
    assert "function mconfigD0SerialRow" in js
    d0_fn = js.split("function mconfigD0SerialRow", 1)[1].split("\nfunction ", 1)[0]
    assert "zendureD0Topic" in d0_fn
    render_fn = js.split("function renderMaintenanceGridMeter", 1)[1].split(
        "\nfunction ", 1
    )[0]
    assert "mconfigD0SerialRow" in render_fn
    assert '"grid_meter.mqtt.topic"' in render_fn
    assert '"advanced"' in render_fn


def test_maintenance_grid_meter_mqtt_password_keeps_secret_semantics():
    js = _read("admin.js")
    fn = js.split("function mconfigGridMqttPasswordRow", 1)[1].split(
        "\nfunction ", 1
    )[0]
    assert "Leave blank to keep the stored password" in fn
    assert "has_password" in fn
    assert "clear_password" in fn
    assert "new-password" in fn


def test_maintenance_add_more_devices_row_matches_setup_structure():
    html = _read("index.html")
    card = html.split('id="maintenance-config-card"', 1)[1].split("</section>", 1)[0]
    row = card.split('id="maintenance-add-devices"', 1)[1].split(
        'id="maintenance-config-features-section"', 1
    )[0]
    # Discovery lives inside the "Add more devices" menu row, like fresh install.
    assert 'id="maintenance-discovery-start"' in row
    assert 'id="maintenance-discovery-manual-form"' in row
    assert 'id="maintenance-discovery-review"' in row
    # Manual add nests as "Add a device manually", the same as the setup flow.
    assert "Add a device manually" in row
    assert 'id="maintenance-config-add-inverter"' in row
    assert 'id="maintenance-config-add-mqtt-device"' in row
    # The old standalone discovery sub-card and its toolbar are gone.
    assert 'id="maintenance-config-discovery"' not in card
    assert "Discover &amp; add hardware" not in card


def test_maintenance_adding_hardware_opens_the_configured_card():
    js = _read("admin.js")
    # Per the unified flow, adding a device immediately opens its configured
    # card so its settings can be completed there (config happens on the card,
    # not in the add flow).
    add = js.split("function mconfigAddDiscovered", 1)[1].split("\nfunction ", 1)[0]
    assert 'mconfigState.openHardware.add("maintenance-grid-meter")' in add
    assert 'mconfigState.openHardware.add("maintenance-inverter-"' in add
    proposal = js.split("function mconfigAddZendureMqttProposal", 1)[1].split(
        "\nfunction ", 1
    )[0]
    assert 'mconfigState.openHardware.add("maintenance-mqtt-device-"' in proposal


def test_maintenance_add_devices_summary_shows_found_count():
    html = _read("index.html")
    assert 'id="maintenance-discovery-count"' in html
    js = _read("admin.js")
    progress = js.split("function renderMaintenanceDiscoveryProgress", 1)[1].split(
        "\nfunction ", 1
    )[0]
    assert "discoveryCount" in progress


def test_maintenance_broker_renders_as_hardware_card():
    html = _read("index.html")
    editor = html.split('id="maintenance-config-editor"', 1)[1]
    card = editor.split('id="maintenance-broker-card"', 1)[1].split(
        "</article>", 1
    )[0]
    # The broker reads like every other configured hardware card: collapsed
    # summary row with model/meta/status, form fields in the card body.
    assert "Zendure MQTT broker" in card
    assert 'class="hardware-card-summary"' in card
    assert 'id="maintenance-broker-model"' in card
    assert 'id="maintenance-broker-meta"' in card
    assert 'id="maintenance-broker-status"' in card
    assert 'id="maintenance-mqtt-broker-form"' in card
    body_tag = card.split('id="maintenance-broker-body"', 1)[1].split(">", 1)[0]
    assert "hidden" in body_tag
    # The old always-open bare form heading is gone from the maintenance editor.
    assert '<h4 class="config-subhead">Zendure MQTT broker</h4>' not in editor


def test_maintenance_broker_card_summary_reflects_state():
    js = _read("admin.js")
    fn = js.split("function syncMaintenanceBrokerForm", 1)[1].split(
        "\nfunction ", 1
    )[0]
    assert "brokerModel" in fn
    assert "brokerMeta" in fn
    assert "brokerStatus" in fn
    assert '"Read-only"' in fn
    assert "Not configured" in fn


# --- maintenance MQTT proposal selection consumes backend capability ---------


def _local_control_observation():
    return {
        "source_type": "local_mqtt",
        "broker_host": "10.0.0.10",
        "broker_port": 1883,
        "topic_family": "legacy_zendure_json",
        "serial_number": "CTL1",
        "device_id": "CTL1",
        "product_key": "PKCTL",
        "model_hint": "SolarFlow Hub 2000",
        "metrics_seen": ["outputLimit", "electricLevel", "outputHomePower"],
    }


def _local_scalar_observation():
    return {
        "source_type": "local_mqtt",
        "broker_host": "10.0.0.10",
        "broker_port": 1883,
        "topic_family": "zensdk_ha_scalar",
        "serial_number": "SF800",
        "device_id": "SF800",
        "model_hint": "SolarFlow 800",
        "metrics_seen": ["outputLimit", "electricLevel"],
    }


def run_mconfig_add_mqtt_proposal(proposal):
    """Run the real Maintenance proposal-selection function on one proposal.

    Returns ``{"added": bool, "device": <draft entry>}`` from the browser code.
    """

    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for the maintenance MQTT proposal test")
    js = _read("admin.js")
    helpers = "\n".join(
        _extract_fn(js, name)
        for name in (
            "nextCompactInverterName",
            "mconfigNextInverterName",
            "mconfigIdentity",
            "normalizeSerial",
            "usableSerialValue",
            "physicalInverterIdentity",
            "inverterVisibleSerial",
            "inverterIdentityTokens",
            "inverterIdentitySet",
            "inverterHasIdentity",
            "inverterIdentityConflict",
            "inverterIdentitiesMatch",
            "mconfigProposalIdentityView",
            "mconfigDeviceCommonDefaults",
            "mconfigApplyCommonDefaults",
            "mconfigIsMqttDevice",
            "mconfigMqttProposalIdentity",
            "mconfigMqttDeviceIdentity",
            "mconfigMqttProposalState",
            "mconfigZendureMqttDraftFromProposal",
            "mconfigAddZendureMqttProposal",
        )
    )
    stub = (
        "function renderMaintenanceInverters() {}\n"
        "function mconfigMarkDraftChanged() {}\n"
        "const mconfigState = {pristine: {devices: []}, draft: {devices: []},"
        " openHardware: new Set(), previewFingerprint: null,"
        " discoveryDraftChanges: 0, catalog: {default_device: {common: {}}}};\n"
    )
    script = (
        stub
        + helpers
        + "\nconst proposal = "
        + json.dumps(proposal)
        + ";\nconst added = mconfigAddZendureMqttProposal(proposal);\n"
        + "console.log(JSON.stringify("
        + "{added: added, device: mconfigState.draft.devices[0] || null}));\n"
    )
    result = subprocess.run(
        [node, "-e", script], text=True, capture_output=True, check=False
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_js_maintenance_selection_uses_backend_capability_for_supported_proposal():
    from admin.zendure_mqtt_config_proposals import build_proposals

    proposal = build_proposals([_local_control_observation()])[0]
    assert proposal["output_control_supported"] is True
    out = run_mconfig_add_mqtt_proposal(proposal)
    assert out["added"] is True
    device = out["device"]
    assert device["output_control"] is True
    assert device["capabilities"]["write_output_limit"] is True
    # The draft carries the concrete registry model that selects the write adapter.
    assert device["hardware_model"]
    assert device["mqtt"]["product_key"] == "PKCTL"


def test_js_maintenance_selection_keeps_unsupported_proposal_telemetry_only():
    from admin.zendure_mqtt_config_proposals import build_proposals

    proposal = build_proposals([_local_scalar_observation()])[0]
    assert proposal["output_control_supported"] is False
    assert proposal["output_control_reason"] in (
        "scalar_write_not_verified",
        "transport_incompatible",
    )
    out = run_mconfig_add_mqtt_proposal(proposal)
    assert out["added"] is True
    device = out["device"]
    assert device["capabilities"]["write_output_limit"] is False
    assert not device.get("output_control")
    assert not device["mqtt"].get("write_protocol")


def test_maintenance_mqtt_card_derives_control_support_from_device_capability():
    js = _read("admin.js")
    card = _extract_fn(js, "renderMaintenanceZendureMqttDevice")
    # Whether control can be offered comes from the device's backend capability
    # (observed topic family), never from the hardware-generation label.
    assert "generation.supports_output_control" not in card
    assert "mconfigMqttControlSupported(device" in card


def test_mqtt_control_support_helper_prefers_device_capability():
    js = _read("admin.js")
    helper = _extract_fn(js, "mconfigMqttControlSupported")
    assert 'typeof device.supports_output_control === "boolean"' in helper


def test_js_maintenance_selection_carries_broker_endpoint_for_backend():
    # The browser passes the trusted proposal's broker endpoint through to the
    # backend so Maintenance can persist the broker profile; it never derives
    # broker rules itself.
    from admin.zendure_mqtt_config_proposals import build_proposals

    proposal = build_proposals([_local_control_observation()])[0]
    device = run_mconfig_add_mqtt_proposal(proposal)["device"]
    assert device["broker"] == {
        "ref": proposal["broker_ref"],
        "host": "10.0.0.10",
        "port": 1883,
        "tls": False,
        "tls_insecure": False,
        "tls_mode": "",
        "credentials_ref": "",
        "source": "local_mqtt",
    }


def test_js_maintenance_selection_seeds_backend_control_support_flag():
    from admin.zendure_mqtt_config_proposals import build_proposals

    supported = build_proposals([_local_control_observation()])[0]
    unsupported = build_proposals([_local_scalar_observation()])[0]
    assert (
        run_mconfig_add_mqtt_proposal(supported)["device"]["supports_output_control"]
        is True
    )
    assert (
        run_mconfig_add_mqtt_proposal(unsupported)["device"]["supports_output_control"]
        is False
    )


# --- MQTT control UI states --------------------------------------------------


def test_mqtt_write_protocol_and_reason_render_with_friendly_labels():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for the MQTT label helper test")
    js = _read("admin.js")
    script = (
        _extract_fn(js, "mqttWriteProtocolLabel")
        + _extract_fn(js, "mqttControlReasonLabel")
        + """
console.log(JSON.stringify({
  legacy: mqttWriteProtocolLabel("legacy_properties_write"),
  scalar: mqttControlReasonLabel("scalar_write_not_verified"),
  missing: mqttControlReasonLabel("write_method_missing"),
  unobserved: mqttControlReasonLabel("output_control_not_observed"),
}));
"""
    )
    result = subprocess.run(
        [node, "-e", script], text=True, capture_output=True, check=False
    )
    assert result.returncode == 0, result.stderr
    labels = json.loads(result.stdout)
    # Raw internal enum values never render; every label is user-facing text.
    assert labels["legacy"] == "Properties write"
    assert labels["scalar"] == "No verified write protocol for this topic family"
    assert labels["missing"] == "No verified write protocol for this topic family"
    assert labels["unobserved"] == "No output control observed in telemetry yet"


def test_maintenance_mqtt_proposal_card_shows_protocol_transport_and_reason():
    js = _read("admin.js")
    card = _extract_fn(js, "renderMaintenanceMqttProposalCard")
    assert '"Transport"' in card
    assert "mqttTransportLabel(proposal)" in card
    assert '"Write protocol"' in card
    assert "mqttWriteProtocolLabel(" in card
    assert '"Reason"' in card
    assert "mqttControlReasonLabel(" in card


def test_setup_mqtt_proposal_card_shows_friendly_write_protocol_and_reason():
    js = _read("admin.js")
    card = _extract_fn(js, "renderMqttProposalCard")
    assert "mqttWriteProtocolLabel(" in card
    assert "mqttControlReasonLabel(" in card


def test_maintenance_existing_mqtt_device_gets_real_control_checkbox():
    js = _read("admin.js")
    card = _extract_fn(js, "renderMaintenanceZendureMqttDevice")
    # The real, accessible checkbox is the Maintenance control for new and
    # existing devices alike — never a read-only status note in its place.
    assert "controlRow.hidden = !supported;" in card
    assert "mconfigCheckboxControl(device.output_control === true" in card
    assert "isNewDevice" not in card


def test_maintenance_output_control_toggle_invalidates_reviewed_preview():
    js = _read("admin.js")
    card = _extract_fn(js, "renderMaintenanceZendureMqttDevice")
    callback = card.split(
        "mconfigCheckboxControl(device.output_control === true", 1
    )[1].split("),", 1)[0]

    assert 'mconfigMarkDraftChanged("manual")' in callback


def test_maintenance_proposal_draft_keeps_opaque_server_resolution_identity():
    from admin.zendure_mqtt_config_proposals import build_proposals

    proposal = build_proposals([_local_control_observation()])[0]
    device = run_mconfig_add_mqtt_proposal(proposal)["device"]

    assert device["proposal_id"] == proposal["id"]
    assert device["proposal_broker_ref"] == proposal["broker_ref"]


# --- credential rollback failure warning (Setup + Maintenance parity) --------


import re  # noqa: E402


def _async_fn_body(js, header):
    body = js.split(header, 1)[1]
    return re.split(r"\n(?:async function |function |const )", body)[0]


def test_credential_rollback_warning_helper_escapes_refs_and_reuses_visual_language():
    js = _read("admin.js")
    assert "function renderCredentialRollbackWarning" in js
    warning = _extract_fn(js, "renderCredentialRollbackWarning")
    # The affected references are always escaped, never raw HTML.
    assert "escapeHtml(" in warning
    assert "credential_rollback" in warning
    assert "failed_refs" in warning
    # Reuses the existing warning/error visual language — no new card/color set.
    assert "config-validation-item" in warning


def test_credential_rollback_warning_uses_valid_semantic_markup():
    js = _read("admin.js")
    warning = _extract_fn(js, "renderCredentialRollbackWarning")
    # A high-severity credential warning is an assertive live region.
    assert 'role="alert"' in warning
    # The affected-references list must live in a block container, never an
    # inline <span> (a <ul> inside a <span> is invalid semantic nesting).
    assert "</ul></div>" in warning
    assert "</ul></span>" not in warning
    # Dynamic content stays escaped and the shared warning classes are reused.
    assert "escapeHtml(" in warning
    assert "config-validation-item" in warning


def test_credential_rollback_warning_rendered_in_both_apply_flows():
    js = _read("admin.js")
    setup = _async_fn_body(js, "async function applyGeneratedConfig")
    maintenance = _async_fn_body(js, "async function applyMaintenanceConfig")
    # Both apply paths drive the one shared renderer.
    assert "CredentialRollbackWarning" in setup
    assert "CredentialRollbackWarning" in maintenance


def test_index_has_credential_rollback_containers_for_both_flows():
    html = _read("index.html")
    assert 'id="config-apply-rollback"' in html
    assert 'id="maintenance-config-apply-rollback"' in html


def test_credential_rollback_warning_behavior_escapes_and_gates_on_rollback():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is required for the rollback warning behavior test")
    js = _read("admin.js")
    helpers = "\n".join(
        _extract_fn(js, name)
        for name in ("escapeHtml", "renderCredentialRollbackWarning")
    )
    script = (
        helpers
        + """
const evil = '<img src=x onerror=alert(1)>';
const withRollback = renderCredentialRollbackWarning({
  ok: false,
  message: "Could not apply the config.",
  credential_rollback: {
    severity: "high",
    failed_refs: [evil, "zendure-cloud"],
    message: "Rolling back staged MQTT credential changes failed.",
  },
});
const noRollback = renderCredentialRollbackWarning({ ok: false, message: "plain error" });
const lowSeverity = renderCredentialRollbackWarning({
  credential_rollback: { severity: "warn", failed_refs: ["home"] },
});
console.log(JSON.stringify({ withRollback, noRollback, lowSeverity }));
"""
    )
    result = subprocess.run(
        [node, "-e", script], text=True, capture_output=True, check=False
    )
    assert result.returncode == 0, result.stderr
    out = json.loads(result.stdout)
    warning = out["withRollback"]
    assert "Credential rollback failed" in warning
    assert "Manual inspection" in warning
    assert "zendure-cloud" in warning
    # The malicious ref is escaped text, never executable HTML.
    assert "<img src=x onerror" not in warning
    assert "&lt;img src=x onerror=alert(1)&gt;" in warning
    # severity high is visually distinguishable via the error tone.
    assert "config-validation-item-error" in warning
    # A low severity still renders, but not as the high-severity error tone.
    assert out["lowSeverity"] != ""
    assert "config-validation-item-error" not in out["lowSeverity"]
    # No rollback block => nothing rendered, so normal errors are unaffected.
    assert out["noRollback"] == ""


# --- unified setup System Build selector (stable / rc / development) --------


def test_setup_release_selector_renders_optgroups_from_one_source():
    js = _read("admin.js")
    load = _async_fn_body(js, "async function loadReleases")
    # One selector, grouped with <optgroup> built from the single catalogue.
    assert "groupSetupReleaseOptions(" in load
    assert 'createElement("optgroup")' in load
    assert "function groupSetupReleaseOptions" in js


def test_setup_release_options_expose_stable_system_build_semantics():
    load = _async_fn_body(_read("admin.js"), "async function loadReleases")

    assert "option.dataset.channel = release.channel" in load
    assert "option.dataset.revision = release.revision" in load
    assert "option.dataset.buildId = release.build_id" in load


def _run_group_setup_release_options(js, releases):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is required for the grouping contract")
    script = (
        _extract_fn(js, "systemBuildGroupLabel")
        + _extract_fn(js, "groupSetupReleaseOptions")
        + "\nconst groups = groupSetupReleaseOptions("
        + json.dumps(releases)
        + ");\n"
        + "console.log(JSON.stringify(groups.map((g) => ({"
        + "label: g.label, tags: g.releases.map((r) => r.tag),"
        + "}))));\n"
    )
    result = subprocess.run(
        [node, "-e", script], text=True, capture_output=True, check=False
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_setup_release_channel_grouping_places_latest_first_then_stable():
    js = _read("admin.js")
    groups = _run_group_setup_release_options(
        js,
        [
            { "tag": "latest", "channel": "latest" },
            { "tag": "v0.8.0", "channel": "stable" },
            { "tag": "v0.8.0-RC1", "channel": "rc" },
            { "tag": "dev-x-95a135f-1-1", "channel": "development" },
        ],
    )
    # The rolling ``latest`` line is its own group, rendered first, ahead of the
    # versioned Stable releases; rc -> Unstable, development -> Experimental.
    assert [g["label"] for g in groups] == [
        "Latest",
        "Stable",
        "Unstable",
        "Experimental",
    ]
    assert groups[0]["tags"] == ["latest"]
    assert groups[1]["tags"] == ["v0.8.0"]
    assert groups[2]["tags"] == ["v0.8.0-RC1"]
    assert groups[3]["tags"] == ["dev-x-95a135f-1-1"]


def test_setup_release_latest_is_never_grouped_under_stable_or_others():
    js = _read("admin.js")
    groups = _run_group_setup_release_options(
        js,
        [
            { "tag": "latest", "channel": "latest" },
            { "tag": "v0.8.0", "channel": "stable" },
            { "tag": "v0.8.0-RC1", "channel": "rc" },
            { "tag": "dev-x-95a135f-1-1", "channel": "development" },
        ],
    )
    by_label = {g["label"]: g["tags"] for g in groups}
    # ``latest`` appears only under its own Latest group, never folded into any
    # versioned or pre-release group.
    assert by_label.get("Latest") == ["latest"]
    for label in ("Stable", "Unstable", "Experimental"):
        assert "latest" not in by_label.get(label, [])


def test_setup_release_development_option_label_is_readable_and_escaped():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is required for the label contract")
    js = _read("admin.js")
    script = (
        _extract_fn(js, "systemBuildTimestampLabel")
        + _extract_fn(js, "releaseOptionLabel")
        + """
console.log(JSON.stringify({
  dev: releaseOptionLabel({
    tag: "dev-x-95a135f-1-1",
    channel: "development",
    display_name: "MQTT device support",
    revision_short: "95a135f",
  }),
}));
"""
    )
    result = subprocess.run(
        [node, "-e", script], text=True, capture_output=True, check=False
    )
    assert result.returncode == 0, result.stderr
    label = json.loads(result.stdout)["dev"]
    assert "MQTT device support" in label
    assert "95a135f" in label
    # Rendered via textContent on the <option>, never as raw HTML.
    assert "escapeHtml(" in _async_fn_body(js, "async function loadReleases") or (
        ".textContent" in _async_fn_body(js, "async function loadReleases")
    )


def test_setup_release_latest_option_label_does_not_call_it_stable():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is required for the label contract")
    js = _read("admin.js")
    script = (
        _extract_fn(js, "systemBuildTimestampLabel")
        + _extract_fn(js, "releaseOptionLabel")
        + """
console.log(JSON.stringify({
  latest: releaseOptionLabel({ tag: "latest", name: "latest", channel: "latest" }),
}));
"""
    )
    result = subprocess.run(
        [node, "-e", script], text=True, capture_output=True, check=False
    )
    assert result.returncode == 0, result.stderr
    label = json.loads(result.stdout)["latest"]
    # The option makes clear this is the rolling main build, and never labels it
    # as any kind of stable release.
    assert "current main build" in label
    assert "stable" not in label.lower()


def test_setup_release_options_append_local_iso_timestamp_when_available():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is required for the label contract")
    js = _read("admin.js")
    script = (
        _extract_fn(js, "systemBuildTimestampLabel")
        + _extract_fn(js, "releaseOptionLabel")
        + """
console.log(JSON.stringify({
  stable: releaseOptionLabel({
    tag: "v0.7.0",
    name: "v0.7.0",
    channel: "stable",
    docker_supported: true,
    published_at: "2026-07-07T17:18:00",
  }),
  development: releaseOptionLabel({
    tag: "dev-x-664f48e-1-1",
    channel: "development",
    display_name: "Feature zendure-mqtt-device-support",
    revision_short: "664f48e",
    created_at: "2026-07-19T01:07:00",
  }),
  noTimestamp: releaseOptionLabel({
    tag: "latest",
    name: "latest",
    channel: "latest",
  }),
  invalidTimestamp: releaseOptionLabel({
    tag: "v0.6.1",
    name: "v0.6.1",
    channel: "stable",
    docker_supported: true,
    published_at: "not-a-timestamp",
  }),
}));
"""
    )
    result = subprocess.run(
        [node, "-e", script], text=True, capture_output=True, check=False
    )
    assert result.returncode == 0, result.stderr
    labels = json.loads(result.stdout)
    assert labels["stable"] == (
        "v0.7.0 — stable · docker · 2026-07-07 · 17:18"
    )
    assert labels["development"] == (
        "Development — Feature zendure-mqtt-device-support · 664f48e "
        "· 2026-07-19 · 01:07"
    )
    assert labels["noTimestamp"] == "Latest · current main build"
    assert labels["invalidTimestamp"] == "v0.6.1 — stable · docker"


# --- Guided Setup Step 1: plain-language System Build channel guidance --------


def test_setup_channel_guidance_copy_sits_directly_above_the_selector():
    html = _read("index.html")
    release = _release_stage(html)
    assert 'class="system-build-channel-help"' in release
    help_block = release.split('class="system-build-channel-help"', 1)[1].split(
        "</dl>", 1
    )[0]
    # Collapse source line-wrapping the way the browser renders the copy.
    text = " ".join(help_block.split())
    # A short lead-in plus a semantic description list of the four channels,
    # led by the rolling ``latest`` line.
    assert "Choose the System Build you want to install." in text
    for term, blurb in (
        (
            "Latest",
            "The current build from the main branch. Updated automatically "
            "and not a versioned release.",
        ),
        ("Stable", "Recommended versioned releases for normal use."),
        (
            "Unstable",
            "Release candidates for early testing. Mostly complete, but they "
            "may still contain issues.",
        ),
        (
            "Experimental",
            "Feature builds with unfinished changes. Intended for testing only.",
        ),
    ):
        assert term in text
        assert blurb in text
    # The Latest guidance must not describe the rolling build as any kind of
    # stable / recommended-stable release.
    latest_dd = text.split("Latest", 1)[1].split("Stable", 1)[0]
    assert "stable" not in latest_dd.lower()
    # Semantic markup: a description list, not colour-only styling.
    assert "<dl>" in help_block
    assert help_block.count("<dt>") == 4
    assert help_block.count("<dd>") == 4
    # Latest guidance is listed ahead of Stable.
    assert help_block.index("Latest") < help_block.index("Stable")
    # The guidance is rendered immediately above the selector.
    assert release.index('class="system-build-channel-help"') < release.index(
        'id="release-select"'
    )


def test_setup_channel_help_conveys_stability_as_text_not_colour():
    release = _release_stage(_read("index.html"))
    help_block = release.split('class="system-build-channel-help"', 1)[1].split(
        "</dl>", 1
    )[0]
    # Each channel name is a real <dt> term, so build stability is conveyed as
    # text available to assistive tech — never through colour or an icon alone.
    for term in ("Latest", "Stable", "Unstable", "Experimental"):
        assert f"<dt>{term}</dt>" in help_block
    # No inline colour styling or colour-only indicator in the guidance block.
    assert "style=" not in help_block
    assert "color:" not in help_block


def test_setup_release_channel_group_labels_map_from_server_channel():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is required for the channel-label contract")
    js = _read("admin.js")
    assert "function systemBuildGroupLabel" in js
    script = (
        _extract_fn(js, "systemBuildGroupLabel")
        + """
console.log(JSON.stringify({
  stable: systemBuildGroupLabel("stable"),
  latest: systemBuildGroupLabel("latest"),
  rc: systemBuildGroupLabel("rc"),
  development: systemBuildGroupLabel("development"),
  unknown: systemBuildGroupLabel("unknown"),
  bogus: systemBuildGroupLabel("something-new"),
}));
"""
    )
    result = subprocess.run(
        [node, "-e", script], text=True, capture_output=True, check=False
    )
    assert result.returncode == 0, result.stderr
    labels = json.loads(result.stdout)
    assert labels["stable"] == "Stable"
    # The rolling main build is its own user-facing group, not Stable.
    assert labels["latest"] == "Latest"
    assert labels["rc"] == "Unstable"
    assert labels["development"] == "Experimental"
    # Anything the frontend does not recognise is never silently shown.
    assert labels["unknown"] is None
    assert labels["bogus"] is None


def test_setup_release_unknown_channel_is_not_grouped_under_stable():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is required for the grouping contract")
    js = _read("admin.js")
    script = (
        _extract_fn(js, "systemBuildGroupLabel")
        + _extract_fn(js, "groupSetupReleaseOptions")
        + """
const groups = groupSetupReleaseOptions([
  { tag: "v0.8.0", channel: "stable" },
  { tag: "mystery", channel: "unknown" },
]);
console.log(JSON.stringify(groups.map((g) => ({
  label: g.label,
  tags: g.releases.map((r) => r.tag),
}))));
"""
    )
    result = subprocess.run(
        [node, "-e", script], text=True, capture_output=True, check=False
    )
    assert result.returncode == 0, result.stderr
    groups = json.loads(result.stdout)
    # Unknown channels never leak into a user-facing group (never under Stable).
    all_tags = [tag for group in groups for tag in group["tags"]]
    assert "mystery" not in all_tags
    assert [g["label"] for g in groups] == ["Stable"]
    assert groups[0]["tags"] == ["v0.8.0"]


# --- Guided Setup Step 1: align Admin before Discovery ----------------------


def test_setup_step1_has_system_build_alignment_controls():
    html = _read("index.html")
    for marker in (
        'id="setup-system-build"',
        'id="setup-system-build-actions"',
        'id="setup-system-build-align"',
        'id="setup-system-build-next"',
        'id="setup-system-build-status"',
    ):
        assert marker in html
    # The two alternative actions carry the agreed English labels; the technical
    # "Align Admin" wording is gone from the Fresh Setup UI.
    section = _setup_system_build_section(html)
    assert "Update Admin Server" in section
    assert "Continue" in section
    assert "Align Admin" not in section
    # The Admin recreate safety notice still promises an automatic reconnect.
    assert "reconnect automatically" in html


def test_setup_update_admin_action_revalidates_and_starts_alignment():
    js = _read("admin.js")
    fn = _async_fn_body(js, "async function updateAdminForSystemBuild")
    # Revalidate the selected build, then start the paired alignment (v2), never
    # the legacy admin-update workflow.
    assert "validateSelectedSystemBuild(" in fn
    assert '"/api/setup/system-build/update-admin"' in fn
    assert "/api/admin/maintenance/admin-update/execute" not in fn
    # Reconnect is handled by the shared overlay/poll loop.
    assert "showReconnectOverlay(" in fn
    assert "waitForAdminReconnect(" in fn
    assert "restoreSelectedSystemBuild()" not in fn


def test_setup_next_is_blocked_until_selected_build_is_aligned():
    js = _read("admin.js")
    # Step 1 owns its own Continue button, so the shared nav Next is hidden on the
    # release step and can never advance past an unaligned build.
    nav = _extract_fn(js, "renderSetupNav")
    assert 'setupState.activeStep === "release"' in nav
    assert "setupEls.next.hidden = isLast || onRelease" in nav
    # The pre-existing lock on the following step is preserved for later steps.
    assert "stepLocked(SETUP_STEPS[index + 1])" in nav
    # The release-local Continue is gated by the explicit state machine.
    actions = _extract_fn(js, "renderSystemBuildActions")
    assert "systemBuildNextAllowed()" in actions
    # Fail-closed: Next is blocked unless the explicit server verdict is green.
    # The client trusts only the normalized backend action contract and never
    # re-derives compatibility from image/resource fields.
    allowed = _extract_fn(js, "systemBuildNextAllowed")
    assert "systemBuildState.status === SYSTEM_BUILD_STATUS.VALID" in allowed
    assert "action.continue_allowed === true" in allowed
    assert "action.admin_update_allowed !== true" in allowed
    assert 'result.alignment === "aligned"' not in allowed
    assert "result.embedded_resources_valid === true" not in allowed


def test_setup_update_disables_controls_and_shows_updating_state():
    js = _read("admin.js")
    fn = _async_fn_body(js, "async function updateAdminForSystemBuild")
    assert "systemBuildState.status = SYSTEM_BUILD_STATUS.UPDATING" in fn
    apply = _extract_fn(js, "applySystemBuildAlignment")
    # While updating, both the Update button and the selector are disabled.
    assert "systemBuildIsUpdating()" in apply
    assert "releaseSelect.disabled" in apply


def test_setup_reconnect_restores_selected_system_build_tag():
    js = _read("admin.js")
    # The single source of truth is restored into the selector after reconnect.
    assert "let selectedSystemBuildTag" in js
    restore = _extract_fn(js, "restoreSelectedSystemBuild")
    assert "selectedSystemBuildTag" in restore
    assert "releaseSelect.value" in restore
    assert "validateSelectedSystemBuild(" in restore


def test_setup_failed_alignment_offers_retry_and_keeps_selection():
    js = _read("admin.js")
    fn = _async_fn_body(js, "async function updateAdminForSystemBuild")
    # On failure the selection stays and the failed state is explicit.
    assert "systemBuildState.status = SYSTEM_BUILD_STATUS.FAILED" in fn
    # Retry is action-specific: the left action re-runs only the validate/align
    # flow it owns and never confirms; confirm retries via the right action.
    retry = _async_fn_body(js, "async function handleAlignAdminClick")
    assert "systemBuildState.status === SYSTEM_BUILD_STATUS.FAILED" in retry
    assert "updateAdminForSystemBuild()" in retry
    assert "validateSelectedSystemBuild()" in retry
    assert "confirmSelectedSystemBuild()" not in retry
    cont = _async_fn_body(js, "async function handleContinueClick")
    assert "confirmSelectedSystemBuild()" in cont
    assert "updateAdminForSystemBuild()" not in cont
    # The obsolete standalone Retry button is gone.
    assert "setup-system-build-retry" not in js


def test_setup_discovery_returns_to_step1_when_alignment_required():
    js = _read("admin.js")
    fn = _async_fn_body(js, "async function refreshUnifiedDevices")
    # A server refusal (system_build_alignment_required) sends the user back to
    # Step 1 rather than silently swallowing the error.
    assert "system_build_alignment_required" in fn
    assert "returnToSystemBuildStep(" in fn
    ret = _extract_fn(js, "returnToSystemBuildStep")
    assert 'setActiveStep("release")' in ret


def test_setup_discovery_mutations_use_confirmed_operation_routes():
    js = _read("admin.js")
    helper = _extract_fn(js, "setupDiscoveryFetch")
    assert '"/api/setup/discovery"' in helper
    assert 'headers.set("X-Setup-Operation-ID", setupOperationId)' in helper
    # Setup-only workflow mutations: these panels exist only inside Guided
    # Setup, so they always speak the operation-gated Setup alias. The shared
    # credential/source controls are covered separately below — they select
    # their route family from the mounted node's owning view.
    for header in (
        "async function persistDiscoveryPreparation",
        "async function refreshUnifiedDevices",
        "async function toggleMdns",
        "async function refreshMdns",
    ):
        body = _async_fn_body(js, header)
        assert "setupDiscoveryFetch(" in body


def test_maintenance_discovery_keeps_separate_general_routes():
    js = _read("admin.js")
    maintenance = _async_fn_body(js, "async function startMaintenanceDiscovery")
    assert 'fetch("/api/discovery/mqtt-brokers/refresh"' in maintenance
    assert 'fetch("/api/discovery/mdns/refresh"' in maintenance
    assert "/api/setup/discovery" not in maintenance


# --- shared discovery controls: workflow-context request routing --------------
# The credential/source-config nodes parked in #inline-config-parking serve both
# Guided Setup (operation-gated /api/setup/discovery aliases) and Maintenance
# "Add more devices" (generic authenticated /api/discovery routes). The node's
# current owning view decides the request contract before the request is sent;
# there is no Setup default and no probe-then-retry fallback, so Maintenance
# keeps working when no Setup transition state exists at all.

_SHARED_DISCOVERY_HANDLERS = (
    "async function saveMqttCredential",
    "async function deleteMqttCredential",
    "async function refreshMqttBrokers",
    "async function saveZendureCloudToken",
    "async function testZendureCloudToken",
    "async function refreshZendureCloudDiscovery",
    "async function forgetZendureCloudToken",
)


def test_discovery_fetch_routes_by_explicit_workflow_context():
    js = _read("admin.js")
    helper = _extract_fn(js, "discoveryFetch")
    assert 'context === "setup"' in helper
    assert "setupDiscoveryFetch(input, init)" in helper
    assert 'context === "maintenance"' in helper
    assert "fetch(input, init)" in helper
    # A missing context is a programming error, never a silent Setup default.
    assert "Discovery request context is required" in helper
    resolver = _extract_fn(js, "discoveryContextFor")
    assert "inlineConfigMountedInMaintenance(" in resolver
    assert '"maintenance"' in resolver
    assert '"setup"' in resolver


def test_shared_credential_handlers_resolve_context_from_owning_node():
    js = _read("admin.js")
    for header in _SHARED_DISCOVERY_HANDLERS:
        body = _async_fn_body(js, header)
        assert "discoveryContextFor(" in body, header
        assert "discoveryFetch(" in body, header
        assert "setupDiscoveryFetch(" not in body, header


def test_setup_discovery_fetch_stays_confined_to_setup_only_callers():
    # Tripwire: a future shared handler must never call setupDiscoveryFetch()
    # directly — shared controls go through discoveryFetch(context). Every call
    # site is attributed to its nearest enclosing function declaration.
    js = _read("admin.js")
    allowed = {
        "discoveryFetch",
        "loadGatewayNetworks",
        "persistDiscoveryPreparation",
        "refreshUnifiedDevices",
        "toggleMdns",
        "refreshMdns",
        "probeMqttNetworks",
    }
    declarations = [
        (match.start(), match.group(1))
        for match in re.finditer(r"(?:async )?function (\w+)", js)
    ]
    callers = set()
    for match in re.finditer(r"setupDiscoveryFetch\(", js):
        enclosing = [name for start, name in declarations if start < match.start()]
        assert enclosing, "setupDiscoveryFetch( call outside any function"
        caller = enclosing[-1]
        if caller == "setupDiscoveryFetch":
            continue
        callers.add(caller)
    assert callers <= allowed, sorted(callers - allowed)


def test_zendure_shared_handlers_gate_setup_followups_on_context():
    js = _read("admin.js")
    # The unified-device rescan and proposal reload mutate the Setup draft and
    # can hijack the Setup wizard on a gate refusal; a Maintenance-mounted
    # refresh/forget must never trigger them.
    for header in (
        "async function refreshZendureCloudDiscovery",
        "async function forgetZendureCloudToken",
    ):
        body = _async_fn_body(js, header)
        assert 'context === "setup"' in body, header
        assert "refreshUnifiedDevices(" in body, header


def test_maintenance_scan_shares_the_context_router():
    js = _read("admin.js")
    scan = _async_fn_body(js, "async function maintenanceScanNetwork")
    # The per-session scan uses the same single routing authority instead of a
    # second hand-rolled setup/maintenance switch.
    assert "discoveryFetch(" in scan
    assert "session.mode" in scan
    assert "setupDiscoveryFetch" not in scan


# --- Guided Setup Step 1: no development-build acknowledgement ---------------
# Selecting an Experimental build is itself the decision, so there is no
# checkbox, warning banner or acknowledgement state anywhere in Fresh Setup.


def _setup_system_build_section(html):
    marker = 'id="setup-system-build"'
    assert marker in html, "missing Step 1 System Build alignment section"
    return html.split(marker, 1)[1].split("</section>", 1)[0]


def test_setup_step1_has_no_development_acknowledgement_controls():
    html = _read("index.html")
    section = _setup_system_build_section(html)
    # The checkbox, its label/help text and the warning container are all gone.
    for marker in (
        'id="setup-system-build-ack"',
        'id="setup-system-build-ack-row"',
        'id="setup-system-build-dev-warning"',
        "I understand the development-build risks.",
        "Development builds are intended for testing.",
    ):
        assert marker not in section
    # No hidden acknowledgement checkbox survives anywhere in the document.
    assert 'id="setup-system-build-ack"' not in html


def test_setup_has_no_development_acknowledgement_state_or_helpers():
    js = _read("admin.js")
    # The whole acknowledgement mechanism (state field, helpers, request flag)
    # is removed from the Fresh Setup flow.
    for symbol in (
        "systemBuildAcknowledgedTag",
        "systemBuildDevAcknowledged",
        "systemBuildDevAckSatisfied",
        "systemBuildStoredDevAckSatisfied",
    ):
        assert symbol not in js


def test_setup_action_gates_do_not_depend_on_acknowledgement():
    js = _read("admin.js")
    # Neither the Continue nor the Update gate references any acknowledgement.
    for name in ("systemBuildNextAllowed", "systemBuildUpdateAllowed"):
        body = _extract_fn(js, name)
        assert "Ack" not in body
        assert "acknowledge" not in body


def test_setup_experimental_selection_does_not_block_the_valid_action():
    # A development (Experimental) build with a full-green aligned verdict enables
    # Continue exactly like any other build — no acknowledgement gate in between.
    out = _actions_for("VALID", _ALIGNED_RESULT)
    assert out["next"]["enabled"] is True
    assert out["next"]["label"] == "Continue"
    assert out["align"]["enabled"] is False


def test_setup_validation_posts_only_the_selected_tag():
    validate = _async_fn_body(
        _read("admin.js"), "async function validateSelectedSystemBuild"
    )
    # The read-only validate call carries only the tag; no acknowledgement flag.
    assert "const body = { tag }" in validate
    assert "acknowledge_risk" not in validate


# --- Guided Setup Step 1: selection behaviour without a checkbox --------------


def test_setup_selection_change_clears_context_and_previews_without_validating():
    change = _async_fn_body(
        _read("admin.js"), "async function onReleaseSelectChange"
    )
    # A new selection revokes the confirmed operation context and shows a local
    # catalogue preview only. Selection is side-effect free: it never runs the
    # full validation endpoint (no pull) — that waits for an explicit Verify.
    assert "clearSetupOperationContext()" in change
    assert "presentSelectedSystemBuild(" in change
    assert "validateSelectedSystemBuild(" not in change


def test_setup_selection_change_cancels_superseded_fresh_install_operation_first():
    js = _read("admin.js")
    change = _async_fn_body(js, "async function onReleaseSelectChange")
    cancel = _async_fn_body(
        js, "async function cancelSupersededFreshInstallTransition"
    )

    # A superseded in-progress fresh-install transition is still cancelled on
    # selection (status/cancel only, no pull, no validation), before the local
    # preview is shown.
    assert "await cancelSupersededFreshInstallTransition(value, previousTag)" in change
    assert change.index("cancelSupersededFreshInstallTransition") < change.index(
        "presentSelectedSystemBuild"
    )
    assert '"/api/admin/system-alignment/status"' in cancel
    assert '"/api/admin/system-alignment/cancel"' in cancel
    assert 'transition.mode !== "fresh_install"' in cancel
    assert "operation_id: transition.operation_id" in cancel
    assert "confirm: true" in cancel
    assert 'postStartPath("setup_new", false)' in cancel
    assert "setupIntentId = result.setup_intent_id" in cancel


def test_setup_revalidation_invalidates_prior_verdict_and_error():
    validate = _async_fn_body(
        _read("admin.js"), "async function validateSelectedSystemBuild"
    )
    # Re-validating drops the previous server verdict and clears the last error
    # so a stale verdict can never keep an action live for a new build.
    assert "systemBuildState.result = null" in validate
    assert "systemBuildState.error = null" in validate


def test_setup_has_no_orphaned_development_detection_helpers():
    js = _read("admin.js")
    # With the acknowledgement gone, the Fresh Setup development-detection helpers
    # have no remaining callers and are removed entirely.
    assert "selectedSystemBuildIsDevelopment" not in js
    assert "selectedSystemBuildRelease" not in js


# --- Guided Setup Step 1: side-effect-free selection, explicit verification ---
# Selecting/browsing a build is a local catalogue preview only; a single explicit
# Verify action is the sole trigger for the full download + identity check.


def test_selection_preview_is_side_effect_free():
    js = _read("admin.js")
    preview = _extract_fn(js, "presentSelectedSystemBuild")
    # Marking a build selected clears any prior verification and enters SELECTED,
    # without any network call: no fetch, no validation, no pull.
    assert "systemBuildState.result = null" in preview
    assert "SYSTEM_BUILD_STATUS.SELECTED" in preview
    assert "applySystemBuildAlignment()" in preview
    assert "fetch(" not in preview
    assert "validateSelectedSystemBuild" not in preview


def test_verify_is_the_single_explicit_verification_trigger():
    js = _read("admin.js")
    # The right footer action becomes "Verify System Build" while unverified, and
    # clicking it is the only path that runs the full validation endpoint.
    actions = _extract_fn(js, "renderSystemBuildActions")
    assert "Verify System Build" in actions
    assert "systemBuildState.status === SYSTEM_BUILD_STATUS.SELECTED" in actions
    assert "systemBuildVerifyAllowed()" in actions
    cont = _async_fn_body(js, "async function handleContinueClick")
    assert "SYSTEM_BUILD_STATUS.SELECTED" in cont
    assert "validateSelectedSystemBuild()" in cont
    # There is no second standalone Verify control; the single primary is reused.
    assert "setup-system-build-verify" not in js


def test_verify_allowed_only_for_a_selected_selectable_build():
    allowed = _extract_fn(_read("admin.js"), "systemBuildVerifyAllowed")
    assert "SYSTEM_BUILD_STATUS.SELECTED" in allowed
    assert "selectedSystemBuildTag" in allowed
    assert "release.selectable !== false" in allowed


def test_selected_state_gates_continue_and_update_off():
    js = _read("admin.js")
    # Continue and Update Admin Server both require the verified VALID state, so a
    # merely-selected (unverified) build can enable neither.
    nxt = _extract_fn(js, "systemBuildNextAllowed")
    upd = _extract_fn(js, "systemBuildUpdateAllowed")
    assert "SYSTEM_BUILD_STATUS.VALID" in nxt
    assert "SYSTEM_BUILD_STATUS.VALID" in upd


def test_rate_limited_verification_keeps_build_unverified_and_retryable():
    fn = _async_fn_body(
        _read("admin.js"), "async function validateSelectedSystemBuild"
    )
    # A GHCR rate-limit is a retryable throttle: it never becomes VALID, keeps the
    # selection, surfaces the actionable message, and leaves a retry available
    # (FAILED + failedAction="validate" → the "Check again" primary).
    assert 'terminal.code === "system_build_registry_rate_limited"' in fn
    assert "rateLimited" in fn
    assert "SYSTEM_BUILD_STATUS.FAILED" in fn
    assert 'systemBuildState.failedAction = "validate"' in fn


def test_stale_validation_response_cannot_verify_a_newer_selection():
    fn = _async_fn_body(
        _read("admin.js"), "async function validateSelectedSystemBuild"
    )
    # After the await, a response is applied only when its epoch and tag still
    # match the current selection; a stale Build A response cannot verify Build B.
    after_fetch = fn.split("await res.json", 1)[1]
    assert "generation !== systemBuildState.validationGeneration" in after_fetch
    assert "tag !== selectedSystemBuildTag" in after_fetch


def test_system_build_progress_stays_task_local_and_reconnect_overlay_separate():
    js = _read("admin.js")
    # The 7-stage progress UI is owned by one task-local presentation authority,
    # while the global reconnect overlay stays a distinct, unchanged element set.
    assert "function applySystemBuildPresentation" in js
    assert "showReconnectOverlay" in js
    assert 'getElementById("admin-update-overlay-title")' in js


# --- Guided Setup Step 1: explicit fail-closed state machine ------------------


def test_system_build_state_machine_declares_all_explicit_statuses():
    js = _read("admin.js")
    assert "const systemBuildState = {" in js
    status = js.split("const SYSTEM_BUILD_STATUS = {", 1)[1].split("};", 1)[0]
    for name in (
        "IDLE",
        "SELECTED",
        "VALIDATING",
        "VALID",
        "INVALID",
        "CONFIRMING",
        "UPDATING",
        "RECONNECTING",
        "FAILED",
    ):
        assert name in status
    for value in (
        "idle",
        "selected",
        "validating",
        "valid",
        "invalid",
        "confirming",
        "updating",
        "reconnecting",
        "failed",
    ):
        assert f'"{value}"' in status


def test_next_allowed_requires_the_full_green_server_verdict():
    allowed = _extract_fn(_read("admin.js"), "systemBuildNextAllowed")
    assert "systemBuildState.status === SYSTEM_BUILD_STATUS.VALID" in allowed
    assert "action.continue_allowed === true" in allowed
    assert "action.admin_update_allowed !== true" in allowed
    # The server verdict is authoritative; compatibility is not re-derived here so
    # a legacy release (embedded_resources_valid === null) is not blocked.
    assert 'result.alignment === "aligned"' not in allowed
    assert "result.embedded_resources_valid === true" not in allowed


def test_update_allowed_requires_the_server_admin_update_flag():
    allowed = _extract_fn(_read("admin.js"), "systemBuildUpdateAllowed")
    assert "systemBuildState.status === SYSTEM_BUILD_STATUS.VALID" in allowed
    assert "action.admin_update_allowed === true" in allowed
    assert "action.continue_allowed !== true" in allowed


def test_validation_enters_validating_then_fails_closed_on_network_error():
    fn = _async_fn_body(_read("admin.js"), "async function validateSelectedSystemBuild")
    # Validating status is entered before the request; a thrown error lands in the
    # explicit failed state so Next stays closed.
    assert "systemBuildState.status = SYSTEM_BUILD_STATUS.VALIDATING" in fn
    assert "catch (err)" in fn
    assert "systemBuildState.status = SYSTEM_BUILD_STATUS.FAILED" in fn
    # A reachable-but-rejected build is an invalid state, not a transient failure.
    assert "SYSTEM_BUILD_STATUS.INVALID" in fn


# --- Guided Setup Step 1: serialized selection / no stale results ------------


def test_stale_validation_responses_are_ignored():
    fn = _async_fn_body(_read("admin.js"), "async function validateSelectedSystemBuild")
    # A monotonic generation guards against out-of-order responses: a newer
    # validation, or a changed selection, invalidates an in-flight one.
    assert "++systemBuildState.validationGeneration" in fn
    assert "generation !== systemBuildState.validationGeneration" in fn
    assert "tag !== selectedSystemBuildTag" in fn


def test_update_reconfirms_the_selected_tag_before_mutating():
    fn = _async_fn_body(_read("admin.js"), "async function updateAdminForSystemBuild")
    # The captured tag must still be the selected one after revalidation, so a
    # selection change during update-start never mutates the wrong build.
    assert "tag !== selectedSystemBuildTag" in fn
    assert "systemBuildUpdateAllowed()" in fn


def test_double_update_click_starts_no_second_transition():
    fn = _async_fn_body(_read("admin.js"), "async function updateAdminForSystemBuild")
    # A second click while already updating/reconnecting is a no-op.
    assert "systemBuildIsUpdating()) return" in fn


def test_next_confirms_the_captured_system_build_before_opening_devices():
    js = _read("admin.js")
    confirm = _async_fn_body(js, "async function confirmSelectedSystemBuild")
    assert 'fetch("/api/setup/system-build/confirm"' in confirm
    assert "systemBuildState.status = SYSTEM_BUILD_STATUS.CONFIRMING" in confirm
    assert "data.resources_verified !== true" in confirm
    assert "bindConfirmedSetupOperation(data.operation_id, confirmedTag)" in confirm
    assert "tag !== selectedSystemBuildTag" in confirm
    assert 'setActiveStep("devices")' in confirm


def test_fresh_setup_intent_is_kept_and_sent_to_setup_mutations():
    js = _read("admin.js")
    start = _async_fn_body(js, "async function startPath")
    headers = _extract_fn(js, "setupIntentHeaders")
    confirm = _async_fn_body(js, "async function confirmSelectedSystemBuild")
    update = _async_fn_body(js, "async function updateAdminForSystemBuild")

    assert "let setupIntentId = null" in js
    assert "result.setup_intent_id" in start
    assert "setupIntentId = result.setup_intent_id" in start
    assert 'headers.set("X-Setup-Intent-ID", setupIntentId)' in headers
    assert "setupIntentHeaders(" in confirm
    assert "setupIntentHeaders(" in update


def test_auth_loss_discards_the_session_bound_setup_intent():
    apply = _extract_fn(_read("admin.js"), "applyAuthStatus")
    assert "setupIntentId = null" in apply


def test_successful_confirm_clears_the_one_shot_setup_intent():
    confirm = _async_fn_body(
        _read("admin.js"), "async function confirmSelectedSystemBuild"
    )
    # A confirm that started the transition consumes the intent server-side; the
    # browser drops its now-useless id so it is never resent.
    request = confirm.index('fetch("/api/setup/system-build/confirm"')
    assert "setupIntentId = null" in confirm[request:]


def test_successful_update_admin_clears_the_one_shot_setup_intent():
    update = _async_fn_body(
        _read("admin.js"), "async function updateAdminForSystemBuild"
    )
    request = update.index('fetch("/api/setup/system-build/update-admin"')
    assert "setupIntentId = null" in update[request:]


def test_setup_intent_rejection_handler_covers_every_stale_reason():
    js = _read("admin.js")
    reasons = js.split("STALE_SETUP_INTENT_REASONS = new Set(", 1)[1].split(")", 1)[0]
    for reason in (
        "setup_intent_consumed",
        "setup_intent_expired",
        "setup_state_changed",
        "setup_intent_required",
    ):
        assert reason in reasons
    handler = _extract_fn(js, "handleSetupIntentRejection")
    assert "STALE_SETUP_INTENT_REASONS.has(reason)" in handler
    # It drops the stale id and demands a fresh Fresh Setup confirmation.
    assert "setupIntentId = null" in handler
    assert "freshSetupConfirmationRequired = true" in handler
    # Reuses the existing Control/Energy-stage error surface — no new card set.
    assert "setSystemBuildError(" in handler
    assert 'setActiveStep("release")' in handler


def test_confirm_and_update_route_rejections_through_the_handler():
    js = _read("admin.js")
    confirm = _async_fn_body(js, "async function confirmSelectedSystemBuild")
    update = _async_fn_body(js, "async function updateAdminForSystemBuild")
    assert "handleSetupIntentRejection(" in confirm
    assert "handleSetupIntentRejection(" in update


def test_fresh_confirmation_requirement_blocks_next_and_update():
    js = _read("admin.js")
    assert "let freshSetupConfirmationRequired = false" in js
    next_allowed = _extract_fn(js, "systemBuildNextAllowed")
    update_allowed = _extract_fn(js, "systemBuildUpdateAllowed")
    assert "!freshSetupConfirmationRequired" in next_allowed
    assert "!freshSetupConfirmationRequired" in update_allowed
    # Selecting a start path re-arms a fresh intent and clears the requirement.
    start = _async_fn_body(js, "async function startPath")
    assert "freshSetupConfirmationRequired = false" in start


def test_next_reuses_a_resource_verified_transition_without_reconfirming():
    confirm = _async_fn_body(
        _read("admin.js"), "async function confirmSelectedSystemBuild"
    )

    prepared = confirm.index("result.resources_verified === true")
    request = confirm.index('fetch("/api/setup/system-build/confirm"')
    assert prepared < request
    assert "bindConfirmedSetupOperation(result.operation_id, tag)" in confirm[:request]


def test_confirm_and_update_share_one_mutation_lock():
    js = _read("admin.js")
    lock = _extract_fn(js, "systemBuildMutationInProgress")
    assert "SYSTEM_BUILD_STATUS.CONFIRMING" in lock
    assert "SYSTEM_BUILD_STATUS.UPDATING" in lock
    assert "SYSTEM_BUILD_STATUS.RECONNECTING" in lock
    confirm = _async_fn_body(js, "async function confirmSelectedSystemBuild")
    update = _async_fn_body(js, "async function updateAdminForSystemBuild")
    assert "systemBuildMutationInProgress()) return" in confirm
    assert "systemBuildMutationInProgress()) return" in update


# --- Guided Setup Step 1: reconnect / login resume from the server transition -


def test_boot_resumes_guided_setup_from_the_server_transition():
    js = _read("admin.js")
    authenticated = _async_fn_body(
        js, "async function performAuthenticatedWorkflowResume"
    )
    assert "loadSystemAlignmentStatus()" in authenticated
    assert "resumeGuidedSetupFromTransition(" in authenticated
    resume = _async_fn_body(js, "async function resumeGuidedSetupFromTransition")
    assert "selectedSystemBuildTag = transition.system_tag" in resume
    assert "setAdminView(" in resume
    assert 'setActiveStep("release")' in resume
    assert "validateSelectedSystemBuild(" in resume


def test_same_page_login_resumes_setup_without_duplicate_timers(tmp_path):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is required for the reauthentication behavior contract")
    js = _read("admin.js")
    def source(header):
        return header + _async_fn_body(js, header)

    sources = "\n".join(
        (
            source("function setupTransitionIsActive"),
            source("async function resumeGuidedSetupFromTransition"),
            source("function bootstrapAuthenticatedAppOnce"),
            source("async function performAuthenticatedWorkflowResume"),
            source("async function resumeAuthenticatedWorkflows"),
            source("function showAuthenticatedApp"),
        )
    )
    script = f"""
let adminBootstrapped = false;
let pendingAuthenticatedWorkflowResume = false;
let authenticatedWorkflowResumeCompleted = false;
let authenticatedWorkflowResumeInFlight = null;
let workspaceRevealed = false;
let selectedSystemBuildTag = null;
let setupOperationId = null;
let setupOperationContext = null;
function bindConfirmedSetupOperation(id, tag) {{
  setupOperationId = id;
  setupOperationContext = {{ operationId: id, systemTag: tag }};
}}
function clearSetupOperationContext() {{
  setupOperationId = null;
  setupOperationContext = null;
}}
let setupInitialized = true;
const calls = [];
const setupEls = {{ releaseSelect: {{ value: "" }} }};
const authEls = {{ view: {{hidden: false}}, logout: {{hidden: true}} }};
const startEls = {{ gate: {{hidden: true}} }};
const window = {{
  location: {{ hash: "" }},
  setInterval: () => calls.push("timer"),
}};
const MDNS_POLL_INTERVAL_MS = 20000;
function loadInstallState() {{ calls.push("install"); }}
function pollMdns() {{ calls.push("poll"); }}
function loadMqttBrokers() {{ calls.push("mqtt"); }}
function loadZendureCloudSettings() {{ calls.push("cloud"); }}
async function loadSystemAlignmentStatus() {{
  calls.push("alignment");
  return {{transition: {{operation_id: "op-7", mode: "fresh_install",
    stage: "resources_verified", system_tag: "v0.8.0"}}}};
}}
function revealWorkspace() {{ calls.push("workspace"); }}
function setAdminView(value) {{ calls.push("view:" + value); }}
function setActiveStep(value) {{ calls.push("step:" + value); }}
async function validateSelectedSystemBuild() {{ calls.push("validate"); }}
async function resumeSystemAlignment() {{ calls.push("alignment-resume"); }}
function initSetupWizard() {{ calls.push("init-setup"); }}
async function loadAdminUpdateResume() {{ calls.push("admin-update-resume"); return false; }}
function navigateToUpgradeForResume() {{ calls.push("upgrade"); }}
{sources}
(async () => {{
  await showAuthenticatedApp();
  await showAuthenticatedApp();
  console.log(JSON.stringify({{calls, selectedSystemBuildTag, setupOperationId,
    selected: setupEls.releaseSelect.value, adminBootstrapped}}));
}})();
"""
    result = subprocess.run([node, "-e", script], text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["adminBootstrapped"] is True
    assert payload["calls"].count("timer") == 1
    assert payload["calls"].count("alignment") == 1
    assert payload["calls"].count("step:release") == 1
    assert "alignment-resume" not in payload["calls"]
    assert "upgrade" not in payload["calls"]
    assert payload["selectedSystemBuildTag"] == "v0.8.0"
    assert payload["setupOperationId"] == "op-7"
    assert payload["selected"] == "v0.8.0"


def test_reconnect_reloads_page_for_replaced_admin():
    js = _read("admin.js")
    reconnect = _async_fn_body(js, "async function waitForAdminReconnect")
    # A replacement Admin (new instance id) forces a full reload so the browser
    # runs its newer assets. The reconnect never resumes in place on the stale
    # page, nor drives any resume helper itself; the fresh load resumes from the
    # durable server-side transition.
    assert "reloadForReplacedAdmin()" in reconnect
    assert "await applyAuthStatus(status)" not in reconnect
    assert "pendingAuthenticatedWorkflowResume = true" not in reconnect
    assert "loadAdminUpdateResume()" not in reconnect
    assert "loadSystemAlignmentStatus()" not in reconnect
    assert "resumeSystemAlignment()" not in reconnect
    helper = _extract_fn(js, "reloadForReplacedAdmin")
    assert "window.location.reload()" in helper


def test_reconnect_ignores_stale_admin_responses_until_instance_changes(tmp_path):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is required for the reconnect identity contract")
    js = _read("admin.js")
    reconnect = (
        "async function waitForAdminReconnect" +
        _async_fn_body(js, "async function waitForAdminReconnect")
    )
    script = f"""
let adminReconnectInFlight = null;
let hidden = 0;
let manual = 0;
let reloaded = 0;
let applied = [];
let calls = 0;
const authState = {{authenticated: true, adminInstanceId: "old"}};
const upgradeState = {{running: true}};
const responses = [
  {{ok: true, json: async () => ({{authenticated: true, admin_instance_id: "old"}})}},
  {{ok: true, json: async () => ({{authenticated: true, admin_instance_id: "old"}})}},
  new Error("offline"),
  {{ok: true, json: async () => ({{authenticated: true, admin_instance_id: "new"}})}},
];
async function rawFetch() {{
  const value = responses[calls++];
  if (value instanceof Error) throw value;
  return value;
}}
function showReconnectOverlay() {{}}
function hideReconnectOverlay() {{ hidden += 1; }}
function showManualReloadHint() {{ manual += 1; }}
function reloadForReplacedAdmin() {{ reloaded += 1; }}
function sleep() {{ return Promise.resolve(); }}
async function applyAuthStatus(payload) {{
  applied.push(payload.admin_instance_id);
}}
{reconnect}
(async () => {{
  await waitForAdminReconnect("old");
  console.log(JSON.stringify({{calls, applied, hidden, manual, reloaded}}));
}})();
"""
    result = subprocess.run(
        [node, "-e", script], text=True, capture_output=True, check=False
    )
    assert result.returncode == 0, result.stderr
    # Stale same-instance responses are ignored (calls 1-3); only the new instance
    # id triggers the reload, and never an in-place resume.
    assert json.loads(result.stdout) == {
        "calls": 4,
        "applied": [],
        "hidden": 0,
        "manual": 0,
        "reloaded": 1,
    }


def test_reconnect_timeout_keeps_overlay_and_shows_manual_reload_hint(tmp_path):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is required for the reconnect timeout contract")
    js = _read("admin.js")
    reconnect = (
        "async function waitForAdminReconnect" +
        _async_fn_body(js, "async function waitForAdminReconnect")
    )
    script = f"""
let adminReconnectInFlight = null;
let pendingAuthenticatedWorkflowResume = false;
let manual = 0;
let hidden = 0;
let calls = 0;
const authState = {{authenticated: true, adminInstanceId: "old"}};
const upgradeState = {{running: true}};
const Date = {{now: (() => {{ let value = 0; return () => (value += 60000); }})()}};
async function rawFetch() {{
  calls += 1;
  return {{ok: true, json: async () => ({{admin_instance_id: "old"}})}};
}}
function showReconnectOverlay() {{}}
function hideReconnectOverlay() {{ hidden += 1; }}
function showManualReloadHint() {{ manual += 1; }}
function sleep() {{ return Promise.resolve(); }}
async function applyAuthStatus() {{ throw new Error("stale Admin was accepted"); }}
{reconnect}
(async () => {{
  await waitForAdminReconnect("old");
  console.log(JSON.stringify({{calls, manual, hidden,
    pendingAuthenticatedWorkflowResume}}));
}})();
"""
    result = subprocess.run(
        [node, "-e", script], text=True, capture_output=True, check=False
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "calls": 1,
        "manual": 1,
        "hidden": 0,
        "pendingAuthenticatedWorkflowResume": False,
    }


def test_reconnect_to_replaced_instance_reloads_not_resume(tmp_path):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is required for the reconnect reload contract")
    js = _read("admin.js")
    reconnect = "async function waitForAdminReconnect" + _async_fn_body(
        js, "async function waitForAdminReconnect"
    )
    script = f"""
let adminReconnectInFlight = null;
let reloaded = 0;
let resumes = 0;
const authState = {{authenticated: true, adminInstanceId: "old"}};
const upgradeState = {{running: true}};
const loggedOut = {{auth_configured: true, authenticated: false,
  requires_initial_password: false, recovery_required: false,
  admin_instance_id: "new"}};
async function rawFetch() {{ return {{ok: true, json: async () => loggedOut}}; }}
function showReconnectOverlay() {{}}
function hideReconnectOverlay() {{}}
function showManualReloadHint() {{ throw new Error("unexpected timeout"); }}
function reloadForReplacedAdmin() {{ reloaded += 1; }}
function sleep() {{ return Promise.resolve(); }}
async function applyAuthStatus() {{ resumes += 1; }}
{reconnect}
(async () => {{
  await waitForAdminReconnect("old");
  console.log(JSON.stringify({{reloaded, resumes}}));
}})();
"""
    result = subprocess.run(
        [node, "-e", script], text=True, capture_output=True, check=False
    )
    assert result.returncode == 0, result.stderr
    # A replacement instance reloads for the new assets even when it comes up
    # logged out; it never resumes in place. The fresh load re-authenticates and
    # resumes from the durable transition.
    assert json.loads(result.stdout) == {"reloaded": 1, "resumes": 0}


def test_reconnect_and_auth_events_start_exactly_one_resume_each(tmp_path):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is required for the reconnect behavior contract")
    js = _read("admin.js")

    def source(header):
        return header + _async_fn_body(js, header)

    sources = "\n".join(
        (
            source("async function waitForAdminReconnect"),
            source("async function performAuthenticatedWorkflowResume"),
            source("async function resumeAuthenticatedWorkflows"),
            source("function showAuthenticatedApp"),
            source("function applyAuthStatus"),
            source("async function refreshAuthStatus"),
        )
    )
    script = f"""
let adminBootstrapped = true;
let workspaceRevealed = false;
let pendingAuthenticatedWorkflowResume = true;
let authenticatedWorkflowResumeCompleted = true;
let authenticatedWorkflowResumeInFlight = null;
let adminReconnectInFlight = null;
let authView = null;
let resumeRequests = 0;
let overlayHidden = 0;
const authState = {{authenticated: true, adminInstanceId: "old"}};
const authEls = {{view: null, logout: null}};
const startEls = {{gate: null}};
const upgradeState = {{running: true}};
const authPayload = {{auth_configured: true, authenticated: true,
  requires_initial_password: false, recovery_required: false, csrf_token: "csrf",
  admin_instance_id: "new"}};
async function rawFetch() {{
  return {{ok: true, json: async () => authPayload}};
}}
function showReconnectOverlay() {{}}
function hideReconnectOverlay() {{ overlayHidden += 1; }}
function reloadForReplacedAdmin() {{}}
function showManualReloadHint() {{ throw new Error("unexpected timeout"); }}
function showAuthView(mode) {{ authView = mode; }}
function bootstrapAuthenticatedAppOnce() {{}}
function sleep() {{ return Promise.resolve(); }}
async function loadSystemAlignmentStatus() {{
  return {{transition: {{operation_id: "op-1", stage: "admin_reconnect_pending"}}}};
}}
async function resumeGuidedSetupFromTransition() {{
  resumeRequests += 1;
  await new Promise((resolve) => setTimeout(resolve, 0));
  return true;
}}
async function resumeGuidedUpgradeFromTransition() {{ return false; }}
{sources}
(async () => {{
  await waitForAdminReconnect("old");
  const afterReconnect = resumeRequests;

  authenticatedWorkflowResumeCompleted = false;
  await Promise.all([applyAuthStatus(authPayload), applyAuthStatus(authPayload)]);
  const afterParallelAuth = resumeRequests;

  await applyAuthStatus({{...authPayload, authenticated: false}});
  const after401 = resumeRequests;
  await applyAuthStatus(authPayload);
  console.log(JSON.stringify({{afterReconnect, afterParallelAuth, after401,
    afterLogin: resumeRequests, authView, overlayHidden}}));
}})();
"""
    result = subprocess.run(
        [node, "-e", script], text=True, capture_output=True, check=False
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    # The reconnect now reloads for the replacement's assets (afterReconnect: 0);
    # resume idempotency still holds for the post-reload auth events: parallel
    # applyAuthStatus dedupes to one resume, a 401 resumes nothing, and a fresh
    # authentication resumes exactly once more.
    assert payload == {
        "afterReconnect": 0,
        "afterParallelAuth": 1,
        "after401": 1,
        "afterLogin": 2,
        "authView": "login",
        "overlayHidden": 0,
    }


def test_setup_resume_only_for_an_active_setup_transition():
    active = _extract_fn(_read("admin.js"), "setupTransitionIsActive")
    assert '"fresh_install"' in active
    assert '"automated_setup"' in active
    assert "transition.system_tag" in active
    # Terminal transitions never resume.
    assert '"completed"' in active
    assert '"cancelled"' in active


def test_setup_resume_does_not_auto_start_discovery():
    resume = _async_fn_body(
        _read("admin.js"), "async function resumeGuidedSetupFromTransition"
    )
    # Resume reopens Step 1 (release) and never jumps to devices / discovery.
    assert 'setActiveStep("release")' in resume
    assert "enterDevicesStep(" not in resume
    assert "runUnifiedDiscovery(" not in resume


def test_reconnect_pending_transition_resumes_alignment():
    resume = _async_fn_body(
        _read("admin.js"), "async function resumeGuidedSetupFromTransition"
    )
    assert 'transition.stage === "admin_reconnect_pending"' in resume
    assert "resumeSystemAlignment(" in resume


def test_load_releases_restores_the_transition_target_tag():
    load = _async_fn_body(_read("admin.js"), "async function loadReleases")
    # An already-restored selection (from the server transition) wins over the
    # catalogue default, so a reload lands on the transition's build.
    assert "selectedSystemBuildTag" in load


# --- Guided Setup Step 1: paired System Build framing before any change ------


def _release_stage(html):
    return _setup_panel(html).split('aria-label="Release"', 1)[1].split(
        'aria-label="Devices"', 1
    )[0]


def test_release_stage_frames_selection_as_paired_system_build():
    release = _release_stage(_read("index.html"))
    # The heading no longer sells this as an EMS-version-only choice.
    assert "Select one paired Admin + EMS System Build" in release
    intro = release.split('id="release-system-build-intro"', 1)[1].split(
        "</div>", 1
    )[0]
    text = re.sub(r"\s+", " ", intro)
    # A compact pre-click explanation of the paired build and its consequences.
    assert "matching Admin Console and EMS images" in text
    assert "updated and recreated first" in text
    assert "reconnect automatically" in text
    # Data safety and the deferred EMS start are stated before any action.
    assert "Config, EMS data and backups are not deleted" in text
    assert "not started until Step 05" in text


def test_guided_setup_uses_system_build_terminology_throughout_step_one():
    release = _release_stage(_read("index.html"))
    assert "Selected System Build" in release
    assert "Select System Build" in release
    assert '<span class="field-label">System Build</span>' in release
    assert "Loading System Builds" in release
    assert ">EMS release<" not in release

    js = _read("admin.js")
    loader = _async_fn_body(js, "async function loadReleases")
    status = _extract_fn(js, "setReleaseStatus")
    assert "Loading System Builds" in status
    assert "No System Builds are available" in loader
    assert "EMS releases" not in loader


def test_system_build_error_matrix_shows_retry_and_hides_update():
    apply = _extract_fn(_read("admin.js"), "applySystemBuildAlignment")
    # The recreate safety notice is still hidden while updating or after a failure.
    assert "recreateNotice.hidden = !updateVisible || failed" in apply
    # Both alternative actions are rendered by the single shared renderer; the old
    # separate per-button hidden/disabled logic is gone from apply.
    assert "renderSystemBuildActions()" in apply
    assert "els.update" not in apply
    assert "els.retry" not in apply


def test_no_separate_prepare_button_in_step_one():
    html = _read("index.html")
    release = _release_stage(html)
    # The separate manual Prepare button is gone from Step 1; resources are
    # verified automatically after alignment.
    assert 'id="release-download"' not in release
    assert "Prepare EMS resources" not in html
    js = _read("admin.js")
    assert "function prepareRelease" not in js
    assert "prepareActionBlockedByAlignment" not in js
    assert "refreshPrepareButton" not in js


# --- Guided Setup Step 1: alternative Update / Continue action footer ---------
# "Update Admin Server" and "Continue" are the two mutually-exclusive next
# actions. They share one split footer so the user reads them as alternatives,
# never as two unrelated controls in different places.


def _system_build_action_footer(html):
    section = _setup_system_build_section(html)
    assert "setup-step-actions-split" in section, "missing shared action footer"
    return section.split("setup-step-actions-split", 1)[1].split("</div>", 1)[0]


def test_fresh_install_actions_share_one_split_footer():
    section = _setup_system_build_section(_read("index.html"))
    # Both alternative actions live inside one shared, split action footer.
    assert 'class="setup-step-actions setup-step-actions-split"' in section
    footer = _system_build_action_footer(_read("index.html"))
    assert 'id="setup-system-build-align"' in footer
    assert 'id="setup-system-build-next"' in footer
    # Align is rendered before Continue so tab order matches the reading order.
    assert footer.index('id="setup-system-build-align"') < footer.index(
        'id="setup-system-build-next"'
    )


def test_fresh_install_actions_use_the_same_button_family():
    footer = _system_build_action_footer(_read("index.html"))
    # Same base component: both are real <button>s in the primary-button family.
    assert footer.count("<button") == 2
    assert footer.count('class="primary-button compact"') == 2
    for marker in (
        'id="setup-system-build-align" type="button" class="primary-button compact"',
        'id="setup-system-build-next" type="button" class="primary-button compact"',
    ):
        assert marker in footer
    # The agreed English labels; no technical "Align Admin" / warning-coloured button.
    assert "Update Admin Server" in footer
    assert "Continue" in footer
    assert "Align Admin" not in footer
    assert "secondary-button" not in footer
    assert "danger" not in footer and "warning" not in footer


def test_fresh_install_status_hint_sits_directly_above_actions():
    section = _setup_system_build_section(_read("index.html"))
    assert 'id="setup-system-build-status"' in section
    status_at = section.index('id="setup-system-build-status"')
    footer_at = section.index("setup-step-actions-split")
    # The short status hint is rendered immediately above the shared footer.
    assert status_at < footer_at
    between = section[section.index("</p>", status_at) : footer_at]
    # Nothing but the status paragraph sits between it and the action footer.
    assert "<button" not in between
    assert "setup-block" not in between


def test_fresh_install_action_footer_replaces_separate_update_and_retry():
    section = _setup_system_build_section(_read("index.html"))
    # The separate mid-panel Update/Retry buttons and updating line are gone; the
    # shared footer is the single action surface for the step.
    assert 'id="setup-system-build-update"' not in section
    assert 'id="setup-system-build-retry"' not in section
    assert 'id="setup-system-build-updating"' not in section
    assert "mconfig-actions" not in section


def test_fresh_install_actions_stack_on_narrow_viewport():
    css = _read("admin.css")
    assert ".setup-step-actions-split" in css
    wide = css.split(".setup-step-actions-split", 1)[1].split("}", 1)[0]
    # Wide viewport: two equal-width columns side by side.
    assert "grid-template-columns: repeat(2, minmax(0, 1fr))" in wide
    # Narrow viewport: the two actions stack into a single column.
    mobile = css.split("@media (max-width: 640px)", 1)
    assert len(mobile) == 2, "missing narrow-viewport media query"
    rule = mobile[1].split(".setup-step-actions-split", 1)[1].split("}", 1)[0]
    assert "grid-template-columns: 1fr" in rule


# --- Guided Setup Step 1: single-primary action state matrix ------------------
# The real renderSystemBuildActions is exercised against a fake DOM under every
# server verdict, proving exactly one action is ever the active primary.


def test_single_renderer_owns_both_setup_actions():
    js = _read("admin.js")
    apply = _extract_fn(js, "applySystemBuildAlignment")
    # apply delegates both buttons to one renderer instead of touching each.
    assert "renderSystemBuildActions()" in apply
    render = _extract_fn(js, "renderSystemBuildActions")
    assert "els.align" in render and "els.next" in render


def _run_system_build_actions_node(setup):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for the system build action-state behaviour test")
    js = _read("admin.js")
    functions = "\n".join(
        _extract_fn(js, name)
        for name in (
            "systemBuildIsUpdating",
            "systemBuildMutationInProgress",
            "foreignTransitionActive",
            "systemBuildActionState",
            "systemBuildUpdateAllowed",
            "systemBuildNextAllowed",
            "renderSystemBuildActions",
        )
    )
    harness = """
const SYSTEM_BUILD_STATUS = {
  IDLE: "idle", VALIDATING: "validating", VALID: "valid", INVALID: "invalid",
  CONFIRMING: "confirming", UPDATING: "updating", RECONNECTING: "reconnecting",
  FAILED: "failed",
};
let selectedSystemBuildTag = "v1";
const systemBuildState = {
  status: SYSTEM_BUILD_STATUS.IDLE, result: null, error: null,
  lastAction: null, failedAction: null, validationGeneration: 0,
};
let systemBuildMutationLocked = false;
let freshSetupConfirmationRequired = false;
const setupSystemBuildEls = {
  align: { disabled: true, textContent: "", setAttribute() {} },
  next: { disabled: true, textContent: "", setAttribute() {} },
};
function __snapshot() {
  return {
    align: {
      enabled: !setupSystemBuildEls.align.disabled,
      label: setupSystemBuildEls.align.textContent,
    },
    next: {
      enabled: !setupSystemBuildEls.next.disabled,
      label: setupSystemBuildEls.next.textContent,
    },
  };
}
"""
    script = harness + functions + "\n" + setup
    result = subprocess.run(
        [node, "-e", script], text=True, capture_output=True, check=False
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


# Full-green "aligned" verdict and the "admin update required" verdict, as the
# server returns them for the validate call.
_ALIGNED_RESULT = """{
  alignment: "aligned", embedded_resources_valid: true, next_allowed: true,
  confirmation_allowed: true, resources_verified: true, operation_id: "op-1",
  admin_update_required: false,
  action_state: { admin_update_required: false, admin_update_allowed: false,
    continue_allowed: true, terminal_error: null, busy: false },
}"""
_UPDATE_RESULT = """{
  alignment: "admin_update_required", embedded_resources_valid: true,
  next_allowed: false, admin_update_required: true,
  action_state: { admin_update_required: true, admin_update_allowed: true,
    continue_allowed: false, terminal_error: null, busy: false },
}"""


def _actions_for(status_const, result_js="null", extra=""):
    return _run_system_build_actions_node(
        f"""
{extra}
systemBuildState.status = SYSTEM_BUILD_STATUS.{status_const};
systemBuildState.result = {result_js};
renderSystemBuildActions();
console.log(JSON.stringify(__snapshot()));
"""
    )


def test_action_matrix_no_build_disables_both():
    out = _actions_for("IDLE", "null")
    assert out["align"]["enabled"] is False
    assert out["next"]["enabled"] is False


def test_action_matrix_validating_disables_both():
    # A stale prior result must never leak an enabled button while validating.
    out = _actions_for("VALIDATING", _ALIGNED_RESULT)
    assert out["align"]["enabled"] is False
    assert out["next"]["enabled"] is False


def test_action_matrix_aligned_enables_only_continue():
    out = _actions_for("VALID", _ALIGNED_RESULT)
    assert out["next"]["enabled"] is True
    assert out["next"]["label"] == "Continue"
    assert out["align"]["enabled"] is False


def test_action_matrix_update_required_enables_only_admin_update():
    out = _actions_for("VALID", _UPDATE_RESULT)
    assert out["align"]["enabled"] is True
    assert out["align"]["label"] == "Update Admin Server"
    assert out["next"]["enabled"] is False


def test_action_matrix_updating_disables_both_and_shows_progress():
    out = _actions_for("UPDATING", _UPDATE_RESULT)
    assert out["align"]["enabled"] is False
    assert out["next"]["enabled"] is False
    # The left action doubles as the progress indicator while the update runs.
    assert out["align"]["label"] == "Updating Admin Server…"


def test_action_matrix_reconnecting_shows_reconnecting_and_disables_both():
    out = _actions_for("RECONNECTING", _UPDATE_RESULT)
    assert out["align"]["enabled"] is False
    assert out["next"]["enabled"] is False
    assert out["align"]["label"] == "Reconnecting…"


def test_action_matrix_failed_enables_only_try_again():
    out = _actions_for("FAILED", _UPDATE_RESULT)
    assert out["align"]["enabled"] is True
    assert out["align"]["label"] == "Try again"
    assert out["next"]["enabled"] is False


def test_action_matrix_confirming_disables_both():
    out = _actions_for("CONFIRMING", _ALIGNED_RESULT)
    assert out["align"]["enabled"] is False
    assert out["next"]["enabled"] is False


def test_action_matrix_never_activates_both_actions():
    # Even a self-contradictory server verdict (update required *and* aligned)
    # must never light up both alternatives at once.
    contradictory = """{
      alignment: "aligned", embedded_resources_valid: true, next_allowed: true,
      confirmation_allowed: true, resources_verified: true, operation_id: "op-1",
      admin_update_required: true,
      action_state: { admin_update_required: true, admin_update_allowed: true,
        continue_allowed: true, terminal_error: null, busy: false },
    }"""
    out = _actions_for("VALID", contradictory)
    assert not (out["align"]["enabled"] and out["next"]["enabled"])


# A legacy release: the release-archive strategy makes embedded resources not
# applicable (null, never false), yet Continue must be enabled.
_LEGACY_RELEASE_RESULT = """{
  alignment: "aligned", compatibility_mode: "legacy_release",
  resource_strategy: "release_archive", embedded_resources_applicable: false,
  embedded_resources_valid: null, next_allowed: true, confirmation_allowed: true,
  resources_verified: false, operation_id: null, admin_update_required: false,
  action_state: { admin_update_required: false, admin_update_allowed: false,
    continue_allowed: true, terminal_error: null, busy: false },
}"""


def test_action_matrix_legacy_release_enables_only_continue():
    out = _actions_for("VALID", _LEGACY_RELEASE_RESULT)
    assert out["next"]["enabled"] is True
    assert out["next"]["label"] == "Continue"
    assert out["align"]["enabled"] is False


def test_action_matrix_legacy_release_never_leaves_both_disabled():
    # The v0.7.0 regression contract at the UI layer: selecting a legacy release
    # never leaves both Update Admin Server and Continue disabled.
    out = _actions_for("VALID", _LEGACY_RELEASE_RESULT)
    assert out["align"]["enabled"] or out["next"]["enabled"]


def test_release_archive_embedded_check_renders_not_applicable():
    checks = _extract_fn(_read("admin.js"), "renderDevelopmentBuildChecks")
    # The embedded-resource check reads the server applicability flag and renders
    # a neutral, informational row instead of a failed (×) check for a legacy
    # release. Not-applicable is never an error class.
    assert "embedded_resources_applicable === false" in checks
    assert "verified release archive" in checks
    assert 'row.classList.remove("config-validation-item-error")' in checks


def test_legacy_status_message_matches_the_continue_action():
    js = _read("admin.js")
    status = _extract_fn(js, "systemBuildStatusMessage")
    # An aligned legacy release shows an install-and-continue message keyed on the
    # server resource strategy, not the generic "ready" line.
    assert 'result.resource_strategy === "release_archive"' in status
    assert "SYSTEM_BUILD_ALIGNMENT_TEXT.legacy_ready" in status
    assert "legacy EMS release" in js


# --- Guided Setup Step 1: accessibility of the alternative actions ------------


def test_setup_actions_are_real_focusable_buttons():
    footer = _system_build_action_footer(_read("index.html"))
    # Real, keyboard-focusable <button>s — never divs/links, never tabindex-pulled
    # out of the natural tab order.
    assert footer.count("<button") == 2
    assert "tabindex" not in footer
    assert "<a " not in footer
    assert 'role="button"' not in footer


def test_setup_action_status_uses_an_aria_live_region():
    section = _setup_system_build_section(_read("index.html"))
    status_tag = section.split('id="setup-system-build-status"', 1)[1].split(">", 1)[0]
    # Status changes are announced through the existing polite live region.
    assert 'role="status"' in status_tag
    assert 'aria-live="polite"' in status_tag


def test_setup_actions_keep_the_recommended_tab_order():
    # Selector, then Update Admin Server, then Continue — no checkbox in between.
    release = _release_stage(_read("index.html"))
    order = [
        'id="release-select"',
        'id="setup-system-build-align"',
        'id="setup-system-build-next"',
    ]
    positions = [release.index(marker) for marker in order]
    assert positions == sorted(positions)


def test_disabled_setup_action_stays_visible_not_hidden():
    render = _extract_fn(_read("admin.js"), "renderSystemBuildActions")
    # The alternative action communicates its state through `disabled`, never by
    # being hidden, so the alternative remains legible (and not colour-only).
    assert "disabled" in render
    assert ".hidden" not in render


def test_focus_ring_is_never_suppressed_on_the_action_buttons():
    css = _read("admin.css")
    # No global focus-ring suppression that would hide keyboard focus.
    assert "outline: none" not in css
    assert "outline:none" not in css


def test_double_click_starts_only_one_mutation():
    js = _read("admin.js")
    # Each mutation entry point is a no-op while one is already in progress...
    for header in (
        "async function updateAdminForSystemBuild",
        "async function confirmSelectedSystemBuild",
    ):
        body = _async_fn_body(js, header)
        assert "systemBuildMutationInProgress()) return" in body
    # ...and both buttons are disabled mid-mutation so a second click can't fire.
    render = _extract_fn(js, "renderSystemBuildActions")
    assert "systemBuildMutationInProgress()" in render


def test_pending_mutation_locks_selector_and_actions_together():
    apply = _extract_fn(_read("admin.js"), "applySystemBuildAlignment")
    # While a mutation runs the target selector is locked so it cannot change.
    assert "systemBuildIsUpdating()" in apply
    assert "systemBuildMutationInProgress()" in apply
    assert "releaseSelect.disabled = updating" in apply
    # And the shared renderer disables both actions for the same mutation window.
    assert "renderSystemBuildActions()" in apply


def test_setup_action_rendering_has_no_duplicate_logic():
    js = _read("admin.js")
    # Exactly one renderer writes both buttons; the obsolete blocks-next helper
    # and its duplicate nav gating are gone.
    assert "function systemBuildBlocksNext" not in js
    assert js.count("function renderSystemBuildActions") == 1
    # The shared nav Next handler no longer duplicates the release confirm; the
    # release-local Continue button is the sole Step-1 confirm trigger.
    handler = js.split("if (setupEls.next) {", 1)[1].split(
        "if (setupEls.releaseSelect)", 1
    )[0]
    assert 'setupState.activeStep === "release"' not in handler
    assert "confirmSelectedSystemBuild" not in handler
    assert "setupSystemBuildEls.next.addEventListener" in js


# --- Guided Setup Step 1: Phase-4 status-hint feedback ------------------------
# The one status line above the footer is the plain-language reason for which
# action is live; the technical error stays in the separate error surface.


def test_status_hint_no_build_prompts_to_continue():
    msg = _extract_fn(_read("admin.js"), "systemBuildStatusMessage")
    assert '"Select a System Build to continue."' in msg


def test_status_hint_validating_says_downloading_and_verifying():
    # While verifying, the status line names the download + identity check that
    # is actually happening — not a vague "checking".
    msg = _extract_fn(_read("admin.js"), "systemBuildStatusMessage")
    assert "Downloading and verifying the Admin and EMS images…" in msg


def test_status_hint_selected_prompts_explicit_verify():
    msg = _extract_fn(_read("admin.js"), "systemBuildStatusMessage")
    assert "Select Verify System Build to download and verify" in msg


def test_status_hint_aligned_says_admin_server_ready():
    assert (
        "The Admin Server is ready for the selected System Build."
        in _read("admin.js")
    )


def test_status_hint_update_required_uses_admin_server_language():
    js = _read("admin.js")
    msg = _extract_fn(js, "systemBuildStatusMessage")
    # The plain-language "must be updated" line drives the update-required state.
    assert "The Admin Server must be updated before you can continue." in js
    assert "SYSTEM_BUILD_ALIGNMENT_TEXT.update_required" in msg
    # No technical "alignment" cause vocabulary leaks into the status line.
    assert "alignment is required" not in msg


def test_status_hint_updating_and_reconnecting_use_admin_server_language():
    msg = _extract_fn(_read("admin.js"), "systemBuildStatusMessage")
    assert "The Admin Server is being updated." in msg
    assert "The browser will reconnect automatically." in msg
    assert "Waiting for the updated Admin Server." in msg


def test_status_hint_failed_points_to_the_error():
    js = _read("admin.js")
    msg = _extract_fn(js, "systemBuildStatusMessage")
    # The failure line now names the failed action; the Admin Server update is
    # the default.
    assert "The Admin Server update failed. Check the details and try again." in msg
    assert "System Build validation failed. Check the details and try again." in msg
    assert "System Build confirmation failed. Check the details and try again." in msg
    # The concrete technical detail still reaches the dedicated error surface.
    apply = _extract_fn(js, "applySystemBuildAlignment")
    assert "setSystemBuildError(systemBuildState.error" in apply


def test_validation_is_read_only_and_resources_are_imported_only_on_next():
    js = _read("admin.js")
    fn = _async_fn_body(js, "async function validateSelectedSystemBuild")
    assert "/api/setup/releases/prepare" not in fn
    assert "prepareSelectedSystemBuildResources(" not in fn
    confirm = _async_fn_body(js, "async function confirmSelectedSystemBuild")
    assert '"/api/setup/system-build/confirm"' in confirm
    assert "loadActiveConfigTemplate(" in confirm


def test_admin_update_action_uses_the_fixed_english_label():
    js = _read("admin.js")
    actions = _extract_fn(js, "renderSystemBuildActions")
    # The left action carries the fixed "Update Admin Server" label (the server
    # owns the tag; the button no longer interpolates it). The technical
    # "Align Admin" wording is gone from the Fresh Setup UI.
    assert '"Update Admin Server"' in actions
    assert "Align Admin" not in actions
    assert "Align Admin" not in _extract_fn(js, "applySystemBuildAlignment")


def test_admin_recreate_is_announced_before_the_update_click():
    section = _setup_system_build_section(_read("index.html"))
    notice = section.split(
        'id="setup-system-build-recreate-notice"', 1
    )[1].split("</div>", 1)[0]
    assert "will be recreated" in notice
    assert "reconnect automatically" in notice
    assert "EMS remains stopped" in notice
    # Shown exactly when an update is required; hidden otherwise so an aligned
    # Admin raises no unnecessary warning.
    apply = _extract_fn(_read("admin.js"), "applySystemBuildAlignment")
    assert "els.recreateNotice.hidden = !updateVisible" in apply


def test_aligned_admin_prepares_ems_without_a_warning():
    js = _read("admin.js")
    # Aligned wording is neutral and mentions no recreate.
    assert "The Admin Server is ready for the selected System Build." in js
    aligned_line = next(
        line
        for line in js.splitlines()
        if "aligned:" in line and "Admin Server is ready" in line
    )
    assert "recreate" not in aligned_line.lower()
    # The neutral recreate notice stays hidden while aligned.
    assert "els.recreateNotice.hidden = !updateVisible" in _extract_fn(
        js, "applySystemBuildAlignment"
    )


def test_release_stage_previews_the_preselected_build_on_load_without_validating():
    # A programmatic default selection fires no change event, so loadReleases
    # shows the local catalogue preview itself. On a fresh load it must NOT
    # validate — merely opening the wizard must never pull or contact the
    # registry. The one exception is a reconnect/reload *resume*, which re-verifies
    # the in-progress build, gated behind the resume flag.
    load = _async_fn_body(_read("admin.js"), "async function loadReleases")
    assert "presentSelectedSystemBuild(" in load
    assert "if (systemBuildResumeValidationTag)" in load
    before_resume, _, resume_branch = load.partition(
        "if (systemBuildResumeValidationTag)"
    )
    # The fresh-load path never validates; the only validation is inside the
    # resume guard, re-verifying an already-in-progress operation.
    assert "validateSelectedSystemBuild(" not in before_resume
    assert "validateSelectedSystemBuild(" in resume_branch


def test_prepare_deployment_and_start_stay_two_distinct_stages():
    html = _read("index.html")
    deployment = _deployment_section(html)
    # Step 04 prepares EMS resources with the Admin already aligned; it does not
    # start EMS.
    assert "download EMS/InfluxDB images" in deployment
    assert (
        "The Admin Console is already aligned and EMS is not started yet." in deployment
    )
    assert "EMS is not started yet" in deployment
    # Step 05 remains the only stage that starts EMS.
    start = _start_section(html)
    assert "Start the prepared Docker deployment" in start


# --- paired System Build selection and recovery -----------------------------


def test_separate_development_build_input_is_removed():
    # Guided Setup no longer carries a parallel development-build input with its
    # own validate button, acknowledgement and duplicate selection state.
    html = _read("index.html")
    for marker in (
        'id="development-build-details"',
        'id="development-build-tag"',
        'id="development-build-validate"',
        'id="development-build-form"',
        'id="development-build-acknowledgement"',
    ):
        assert marker not in html
    js = _read("admin.js")
    for symbol in (
        "function validateDevelopmentBuild",
        "ensureDevelopmentBuildOption",
        "setDevelopmentBuildError",
    ):
        assert symbol not in js


def test_setup_uses_one_system_build_selection_source_of_truth():
    js = _read("admin.js")
    assert "let selectedSystemBuildTag" in js
    # The one selector drives the local preview; there is no second selection
    # variable. Selection is side-effect free — it previews, never validates.
    assert "developmentBuildTag" not in js
    change = _extract_fn(js, "onReleaseSelectChange")
    assert "presentSelectedSystemBuild(" in change
    assert "validateSelectedSystemBuild(" not in change


def test_partial_transition_actions_follow_server_recovery_capabilities():
    render = _extract_fn(_read("admin.js"), "renderSystemAlignmentStatus")
    assert "transition.resume_available !== true" in render
    assert "transition.return_available !== true" in render


def test_reconnect_resume_verifies_resources_through_its_own_stage_route():
    resume = _extract_fn(_read("admin.js"), "resumeSystemAlignment")
    assert "/api/admin/system-alignment/verify-resources" in resume
    assert "admin_aligned" in resume


def test_fresh_install_resume_sends_no_acknowledge_risk():
    # Fresh Install recovery relies on the server's stored transition
    # authorization; the browser never injects an acknowledgement flag.
    resume = _extract_fn(_read("admin.js"), "resumeSystemAlignment")
    assert "acknowledge_risk" not in resume


def test_step1_section_uses_admin_server_compatibility_heading():
    html = _read("index.html")
    section = _setup_system_build_section(html)
    assert "Admin Server compatibility" in section
    assert "System Build alignment" not in section


def test_selected_system_build_placeholder_is_not_selected():
    html = _read("index.html")
    row = html.split('id="release-selected-val"', 1)[1].split("</span>", 1)[0]
    assert "Not selected" in row
    assert "Latest stable" not in html


def test_development_build_tag_check_requires_run_attempt_identity():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is required for the development-tag contract")
    js = _read("admin.js")
    assert "function isImmutableDevelopmentBuildTag" in js
    script = (
        _extract_fn(js, "isImmutableDevelopmentBuildTag")
        + """
console.log(JSON.stringify({
  canonical: isImmutableDevelopmentBuildTag(
    "dev-feature-zendure-mqtt-f7265fc-123456789-1"
  ),
  floating: isImmutableDevelopmentBuildTag("dev-feature-zendure-mqtt"),
  retryMutable: isImmutableDevelopmentBuildTag(
    "dev-feature-zendure-mqtt-f7265fc-123456789"
  ),
  injected: isImmutableDevelopmentBuildTag(
    "dev-feature-f7265fc-123456789-1;docker pull evil"
  ),
}));
"""
    )
    result = subprocess.run(
        [node, "-e", script], text=True, capture_output=True, check=False
    )
    assert result.returncode == 0, result.stderr
    result = json.loads(result.stdout)
    assert result == {
        "canonical": True,
        "floating": False,
        "retryMutable": False,
        "injected": False,
    }


def test_unified_system_build_validation_posts_only_tag():
    js = _read("admin.js")
    assert "async function validateSelectedSystemBuild" in js
    fn = _async_fn_body(js, "async function validateSelectedSystemBuild")

    assert 'fetch("/api/admin/system-alignment/validate"' in fn
    assert "JSON.stringify" in fn
    # The browser only ever sends the selected tag — never an acknowledgement
    # flag and never an image ref. Scope the check to the request-building region
    # (body construction through the fetch), so response handling that legitimately
    # names the registry-rate-limit contract is not mistaken for a leaked field.
    request_region = fn.split("const body")[1].split("await res.json")[0]
    assert "const body = { tag }" in "const body" + request_region
    for forbidden in (
        "acknowledge_risk",
        "admin_image",
        "ems_image",
        "admin_repo",
        "ems_repo",
        "registry",
        "image_url",
    ):
        assert forbidden not in request_region


def test_system_build_validation_lists_every_pair_check():
    section = _setup_system_build_section(_read("index.html"))
    for label in (
        "Admin image available",
        "EMS image available",
        "Revision matches",
        "Build ID matches",
        "Channel matches",
        "Embedded resources match",
    ):
        assert label in section


def test_system_alignment_frontend_has_all_seven_product_stages():
    html = _read("index.html")
    stages = (
        ("select", "Select system build"),
        ("validate", "Validate pair"),
        ("align-admin", "Align Admin"),
        ("reconnect", "Reconnect"),
        ("verify-resources", "Verify resources"),
        ("install-ems", "Install or upgrade EMS"),
        ("verify-system", "Verify system"),
    )
    for key, label in stages:
        assert f'data-system-alignment-stage="{key}"' in html
        assert label in html


def test_system_alignment_dynamic_values_use_text_only_or_escape_html():
    js = _read("admin.js")
    assert "function renderSystemAlignmentStatus" in js
    render = _extract_fn(js, "renderSystemAlignmentStatus")

    # Every server-controlled value must be rendered as text or explicitly
    # escaped before it can reach an HTML template.
    for field in (
        "canonical_tag",
        "build_id",
        "revision",
        "admin_image",
        "ems_image",
        "error_message",
        "warning",
    ):
        assert field in render
    assert ".textContent" in render or "escapeHtml(" in render
    if ".innerHTML" in render:
        assert "escapeHtml(" in render

    node = shutil.which("node")
    if node is None:
        pytest.skip("node is required for the escaping contract")
    escaping = _extract_fn(js, "escapeHtml")
    malicious = '<img src=x onerror=alert(1) data-tag="dev">'
    result = subprocess.run(
        [node, "-e", escaping + f"\nconsole.log(escapeHtml({json.dumps(malicious)}));"],
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "<img" not in result.stdout
    assert "&lt;img" in result.stdout


def test_partial_system_transition_exposes_reconnect_and_recovery_controls():
    html = _read("index.html")
    js = _read("admin.js")

    for marker in (
        'id="system-alignment-reconnect"',
        'id="system-alignment-partial"',
        'id="system-alignment-resume"',
        'id="system-alignment-return"',
        'id="system-alignment-abandon"',
    ):
        assert marker in html
    for endpoint in (
        "/api/admin/system-alignment/status",
        "/api/admin/system-alignment/resume",
        "/api/admin/system-alignment/return-to-running-build",
        "/api/admin/system-alignment/cancel",
    ):
        assert endpoint in js
    assert "failed_recoverable" in js
    assert "admin_alignment_started" in js
    assert "operation_id" in js


def test_partial_transition_offers_an_abandon_escape_hatch():
    # A guided_upgrade whose resume keeps failing (and whose EMS was already
    # recreated, so return_available is false) must still be escapable: the
    # recovery panel exposes an Abandon action that cancels the transition.
    js = _read("admin.js")
    render = _extract_fn(js, "renderSystemAlignmentStatus")
    assert "transition.cancel_available !== true" in render
    abandon = _async_fn_body(js, "async function abandonSystemAlignment")
    assert '"/api/admin/system-alignment/cancel"' in abandon
    assert "operation_id: transition.operation_id" in abandon
    assert "confirm: true" in abandon
    assert 'data.stage !== "cancelled"' in abandon


def test_expired_transition_surfaces_the_recovery_panel():
    """An expired transition must not render as a running reconnect forever.

    The server marks expiry on the transition (``expired``) and offers
    ``cancel_available`` from any non-terminal stage. The renderer must treat
    expiry as a recovery state: show the recovery panel (whose Abandon button
    already follows ``cancel_available``), suppress the reconnecting note, and
    say that the transition expired instead of showing the generic partial
    message.
    """

    js = _read("admin.js")
    render = _extract_fn(js, "renderSystemAlignmentStatus")
    assert 'const expired = transition.expired === true' in render
    assert "const recoveryAvailable = failed || expired" in render
    assert "systemAlignmentEls.partial.hidden = !recoveryAvailable" in render
    assert "!expired &&" in render
    assert (
        "The System Build transition has expired. Abandon it to start a new one."
        in render
    )


def test_expired_transition_abandon_waits_for_the_worker_to_stop():
    # TTL expiry is not proof the mutating worker stopped. While the server
    # reports worker_active, the recovery panel must keep Abandon disabled and
    # explain the wait; only once the worker is gone may Abandon light up.
    js = _read("admin.js")
    render = _extract_fn(js, "renderSystemAlignmentStatus")
    assert "transition.worker_active === true" in render
    assert "still running" in render
    assert "Wait for it to finish before abandoning the transition." in render


def _render_expired_recovery(
    worker_active=False, *, worker_status_available=True, cancel_available=None
):
    if cancel_available is None:
        cancel_available = bool(
            worker_status_available and worker_active is not True
        )

    def _js_bool(value):
        if value is None:
            return "null"
        return "true" if value else "false"

    js = _read("admin.js")
    render = _extract_fn(js, "renderSystemAlignmentStatus")
    driver = f"""
let systemAlignmentState;
const SYSTEM_ALIGNMENT_STAGE_ORDER = [];
function systemAlignmentStageStates(payload) {{
  return {{ stage: (payload.transition || {{}}).stage || null, states: [] }};
}}
function applyUpgradeAlignmentTransition() {{}}
function applySystemBuildPresentation() {{}}
const document = {{ querySelectorAll: () => [] }};
const systemAlignmentEls = {{
  tag: {{}}, buildId: {{}}, revision: {{}}, adminImage: {{}}, emsImage: {{}},
  message: {{}}, warning: {{}}, reconnect: {{}}, partial: {{}}, partialMessage: {{}},
  resume: {{}}, returnToRunning: {{}}, abandon: {{}},
}};
{render}
renderSystemAlignmentStatus({{
  active: true,
  transition: {{
    mode: "guided_upgrade",
    stage: "ems_operation_running",
    operation_id: "expired-op",
    expired: true,
    worker_active: {_js_bool(worker_active)},
    worker_status_available: {_js_bool(worker_status_available)},
    resume_available: false,
    cancel_available: {_js_bool(cancel_available)},
  }},
}});
console.log(JSON.stringify({{
  partialHidden: systemAlignmentEls.partial.hidden,
  reconnectHidden: systemAlignmentEls.reconnect.hidden,
  resumeDisabled: systemAlignmentEls.resume.disabled,
  abandonDisabled: systemAlignmentEls.abandon.disabled,
  message: systemAlignmentEls.partialMessage.textContent,
}}));
"""
    return _run_node(driver)


def test_expired_worker_active_recovery_panel_disables_abandon():
    out = _render_expired_recovery(worker_active=True)
    assert out["partialHidden"] is False
    assert out["reconnectHidden"] is True
    assert out["resumeDisabled"] is True
    assert out["abandonDisabled"] is True
    assert "still running" in out["message"]


def test_expired_worker_inactive_recovery_panel_enables_abandon():
    out = _render_expired_recovery(worker_active=False)
    assert out["partialHidden"] is False
    assert out["reconnectHidden"] is True
    assert out["resumeDisabled"] is True
    assert out["abandonDisabled"] is False
    assert "Abandon it to start a new one." in out["message"]


def test_expired_worker_unknown_recovery_panel_disables_abandon():
    # worker_active === null / worker_status_available === false: liveness could
    # not be verified, so Abandon fails closed and the panel explains the wait.
    out = _render_expired_recovery(
        worker_active=None, worker_status_available=False, cancel_available=False
    )
    assert out["partialHidden"] is False
    assert out["reconnectHidden"] is True
    assert out["resumeDisabled"] is True
    assert out["abandonDisabled"] is True
    assert "could not be verified" in out["message"]


def test_expired_worker_unknown_blocks_abandon_even_if_cancel_available_true():
    # Defence in depth: even if a stale/optimistic payload still says
    # cancel_available true, an unverifiable worker state must keep Abandon off.
    out = _render_expired_recovery(
        worker_active=None, worker_status_available=False, cancel_available=True
    )
    assert out["abandonDisabled"] is True
    assert "could not be verified" in out["message"]


def test_recovery_render_source_fails_closed_on_unknown_worker_state():
    render = _extract_fn(_read("admin.js"), "renderSystemAlignmentStatus")
    assert "transition.worker_status_available === false" in render
    assert "could not be verified" in render


def _render_alignment_payload(payload):
    js = _read("admin.js")
    render = _extract_fn(js, "renderSystemAlignmentStatus")
    driver = f"""
let systemAlignmentState;
const SYSTEM_ALIGNMENT_STAGE_ORDER = [];
function systemAlignmentStageStates(payload) {{
  return {{ stage: (payload.transition || {{}}).stage || null, states: [] }};
}}
function applyUpgradeAlignmentTransition() {{}}
function applySystemBuildPresentation() {{}}
const document = {{ querySelectorAll: () => [] }};
const systemAlignmentEls = {{
  tag: {{}}, buildId: {{}}, revision: {{}}, adminImage: {{}}, emsImage: {{}},
  message: {{}}, warning: {{}}, reconnect: {{}}, partial: {{}}, partialMessage: {{}},
  resume: {{}}, returnToRunning: {{}}, abandon: {{}},
}};
{render}
renderSystemAlignmentStatus({json.dumps(payload)});
console.log(JSON.stringify({{
  partialHidden: systemAlignmentEls.partial.hidden,
  resumeDisabled: systemAlignmentEls.resume.disabled,
  abandonDisabled: systemAlignmentEls.abandon.disabled,
  message: systemAlignmentEls.partialMessage.textContent,
}}));
"""
    return _run_node(driver)


def test_recovery_panel_fails_closed_when_worker_fields_are_absent():
    # A transition without any worker verdict (stale or synthetic payload) is
    # not proof the worker stopped: Abandon must stay disabled until a
    # worker-aware render arrives.
    out = _render_alignment_payload(
        {
            "active": True,
            "transition": {
                "mode": "guided_upgrade",
                "stage": "failed_recoverable",
                "operation_id": "upgrade-op",
                "resume_available": False,
                "cancel_available": True,
            },
        }
    )
    assert out["partialHidden"] is False
    assert out["abandonDisabled"] is True


def test_running_worker_keeps_abandon_disabled_before_expiry():
    # failed_recoverable can race the worker's own terminal commit: while the
    # server still reports the worker active, Abandon stays off and the panel
    # says the operation is running instead of a generic failure message.
    out = _render_alignment_payload(
        {
            "active": True,
            "transition": {
                "mode": "guided_upgrade",
                "stage": "failed_recoverable",
                "operation_id": "upgrade-op",
                "error_message": "EMS deployment failed.",
                "resume_available": False,
                "cancel_available": False,
                "worker_active": True,
                "worker_status_available": True,
            },
        }
    )
    assert out["partialHidden"] is False
    assert out["abandonDisabled"] is True
    assert "still running" in out["message"]


def test_job_poll_shaped_payload_drives_worker_aware_abandon_gating():
    # The poller hands the whole job-poll body to the renderer; the embedded
    # transition's worker verdict must gate Abandon exactly like the dedicated
    # status payload does.
    poll_body = {
        "ok": True,
        "job_id": "job-1",
        "status": "running",
        "steps": [],
        "transition": {
            "mode": "guided_upgrade",
            "stage": "ems_operation_running",
            "operation_id": "upgrade-op",
            "expired": True,
            "resume_available": False,
            "cancel_available": False,
            "worker_active": True,
            "worker_status_available": True,
        },
    }
    out = _render_alignment_payload(poll_body)
    assert out["partialHidden"] is False
    assert out["resumeDisabled"] is True
    assert out["abandonDisabled"] is True
    assert "still running" in out["message"]

    released = json.loads(json.dumps(poll_body))
    released["status"] = "failed"
    released["transition"].update(
        {"worker_active": False, "cancel_available": True}
    )
    out = _render_alignment_payload(released)
    assert out["abandonDisabled"] is False
    assert out["resumeDisabled"] is True


# --- System Build progress is scoped to its owning task ---------------------
# The seven-stage pipeline is a task subworkflow, not an application-global
# card. It must live inside Guided Setup or Guided Upgrade, never above Login or
# Task Selection. These contracts pin the DOM ownership and the JS lifecycle.


def _pre_login_zone(html):
    # Everything under <main class="admin-shell"> before the first task view
    # (the auth/login gate). The System Build pipeline must never render here.
    return html.split('class="admin-shell"', 1)[1].split('id="view-auth"', 1)[0]


def test_system_build_workflow_is_not_positioned_above_login():
    html = _read("index.html")
    # Exactly one workflow node exists: it is moved between task slots, never
    # copied, so element ids stay unique.
    assert html.count('id="system-alignment-workflow"') == 1
    # It must not be a global sibling above the auth/login/task-selection views.
    assert 'id="system-alignment-workflow"' not in _pre_login_zone(html)


def test_reconnect_overlay_stays_global():
    html = _read("index.html")
    # The Admin reconnect overlay is the only progress surface allowed outside
    # the task views; it lives outside <main> so it survives the Admin restart
    # dropping the page back to the login gate.
    assert 'id="admin-update-overlay"' in html
    assert html.index('id="admin-update-overlay"') < html.index('class="admin-shell"')


def test_guided_setup_has_system_build_mount_slot():
    html = _read("index.html")
    assert 'id="setup-system-build-slot"' in _setup_panel(html)


def test_guided_upgrade_has_system_build_mount_slot():
    html = _read("index.html")
    assert 'id="upgrade-system-build-slot"' in _upgrade_panel(html)


def test_system_build_workflow_parks_in_a_hidden_neutral_container():
    html = _read("index.html")
    # The single workflow node lives in a hidden parking container by default so
    # the pipeline is fail-closed before JavaScript initialises and is never a
    # view sibling above login.
    assert 'id="system-build-parking" hidden' in html
    parking = html.split('id="system-build-parking"', 1)[1].split("</main>", 1)[0]
    assert 'id="system-alignment-workflow"' in parking


def test_system_build_owner_resolver_maps_modes_to_tasks():
    js = _read("admin.js")
    assert "function systemBuildOwner" in js
    owner = _extract_fn(js, "systemBuildOwner")
    # Fresh/Automated Setup are owned by Guided Setup; Guided Upgrade owns its
    # own transition; anything else has no owner.
    assert '"fresh_install"' in owner
    assert '"automated_setup"' in owner
    assert '"guided_upgrade"' in owner
    assert '"setup"' in owner
    assert "return null" in owner


def test_system_build_workflow_mounts_into_owning_task_slot():
    js = _read("admin.js")
    assert "function mountSystemBuildWorkflow" in js
    # Both task slots and the neutral parking container the node moves between.
    assert "setup-system-build-slot" in js
    assert "upgrade-system-build-slot" in js
    mount = _extract_fn(js, "mountSystemBuildWorkflow")
    assert "SYSTEM_BUILD_SLOT_IDS" in mount
    assert "system-build-parking" in mount
    # The shared node is moved, never re-created with duplicate innerHTML.
    assert "appendChild" in mount
    assert "innerHTML" not in mount


def test_system_build_presentation_gates_on_auth_owner_and_stage():
    js = _read("admin.js")
    assert "function systemBuildPresentation" in js
    pres = _extract_fn(js, "systemBuildPresentation")
    assert "authenticated" in pres
    assert "owner" in pres
    assert "poll" in pres
    # A cancelled (terminal) transition never keeps the full pipeline visible.
    assert "cancelled" in pres


def test_auth_gate_clears_system_build_progress():
    js = _read("admin.js")
    # Entering any unauthenticated state (login / create / recovery, session
    # expiry, logout, initial load) funnels through showAuthView, which must
    # hide, detach and clear the pipeline.
    show_auth = _extract_fn(js, "showAuthView")
    assert "clearSystemBuildProgress(" in show_auth
    assert "function clearSystemBuildProgress" in js
    clear = _extract_fn(js, "clearSystemBuildProgress")
    assert "stopSystemAlignmentPolling" in clear
    # Detach to the hidden neutral container and drop the cached transition so no
    # build metadata survives into the login gate.
    assert "mountSystemBuildWorkflow(" in clear
    assert "systemAlignmentState = null" in clear


def test_navigation_reevaluates_system_build_presentation():
    js = _read("admin.js")
    # Returning to Task Selection or switching views must re-scope the pipeline
    # (drop any synthetic preview, park/hide it) rather than leaving a stale card
    # from another task.
    for header in ("showLanding", "setAdminView", "setMaintenancePath"):
        body = _extract_fn(js, header)
        assert "rescopeSystemBuildForNavigation(" in body, header
    rescope = _extract_fn(js, "rescopeSystemBuildForNavigation")
    # A synthetic preview (no persisted transition) is dropped so it cannot follow
    # the operator into another task; it still re-applies presentation.
    assert "systemAlignmentState = null" in rescope
    assert "applySystemBuildPresentation(" in rescope


def test_owner_fallback_is_limited_to_transitionless_previews():
    js = _read("admin.js")
    # The activeTask fallback must fire ONLY for a synthetic preview with no
    # persisted transition — never for a real transition whose mode has no owner
    # (e.g. the align-existing rollback), which must park instead of leaking into
    # whatever task is open.
    pres = _extract_fn(js, "systemBuildPresentation")
    assert "hasTransition" in pres
    assert "!hasTransition" in pres


def test_active_task_requires_maintenance_view_visible_for_upgrade():
    js = _read("admin.js")
    # The upgrade sub-panel's own hidden flag can go stale; the owning-task probe
    # must also require the maintenance view ancestor to be visible so the
    # landing gate never counts as the Guided Upgrade task.
    fn = _extract_fn(js, "currentActiveTask")
    assert "view-maintenance" in fn
    assert "maintenance-upgrade-panel" in fn
    assert "view-setup" in fn


# --- reconnect failure awareness (transition bound) -------------------------


def test_classify_reconnect_transition_covers_terminal_and_wrong_operation():
    js = _read("admin.js")
    classify = _extract_fn(js, "classifyReconnectTransition")
    # A failed/cancelled transition on the same operation is terminal, and a
    # different operation id is fail-closed — the old instance's 200 alone is
    # never a reconnect.
    assert '=== "failed_recoverable"' in classify or 'failed_recoverable' in classify
    assert 'cancelled' in classify
    assert 'wrong_operation' in classify
    assert 'operation_id' in classify


def _reconnect_outcome_script(js, *, transition, operation_id):
    reconnect = "async function waitForAdminReconnect" + _async_fn_body(
        js, "async function waitForAdminReconnect"
    )
    # classifyReconnectTransition is the real function under test; the surface and
    # transition-read helpers are stubbed below, declared *after* it so the stubs
    # win over any async helper the coarse extractor pulls in alongside classify.
    classify = _extract_fn(js, "classifyReconnectTransition")
    return f"""
let adminReconnectInFlight = null;
let pendingAuthenticatedWorkflowResume = false;
let events = [];
let calls = 0;
const authState = {{authenticated: true, adminInstanceId: "old"}};
const upgradeState = {{running: true}};
const Date = {{now: (() => {{ let v = 0; return () => (v += 1000); }})()}};
async function rawFetch() {{
  calls += 1;
  return {{ok: true, json: async () => ({{authenticated: true, admin_instance_id: "old"}})}};
}}
function showReconnectOverlay() {{}}
function hideReconnectOverlay() {{ events.push("hide"); }}
function showManualReloadHint() {{ events.push("manual"); }}
function sleep() {{ return Promise.resolve(); }}
async function applyAuthStatus() {{ throw new Error("old admin accepted as reconnect"); }}
{classify}
async function readReconnectTransition() {{ return {transition}; }}
function surfaceReconnectTransitionFailure() {{ events.push("failure"); }}
async function surfaceReconnectTransitionCancelled() {{ events.push("cancelled"); }}
function surfaceReconnectWrongOperation() {{ events.push("wrong_operation"); }}
{reconnect}
(async () => {{
  await waitForAdminReconnect("old", {operation_id});
  console.log(JSON.stringify({{events, calls}}));
}})();
"""


def _run_reconnect_outcome(transition, operation_id):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is required for the reconnect failure contract")
    js = _read("admin.js")
    script = _reconnect_outcome_script(js, transition=transition, operation_id=operation_id)
    result = subprocess.run(
        [node, "-e", script], text=True, capture_output=True, check=False
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_reconnect_surfaces_failed_transition_while_old_admin_survives():
    payload = _run_reconnect_outcome(
        '{stage: "failed_recoverable", operation_id: "op-1"}', '"op-1"'
    )
    # A failed update while the old instance keeps answering stops the overlay at
    # once and shows the failure — it never waits out the 120s timeout.
    assert payload == {"events": ["failure"], "calls": 1}


def test_reconnect_restores_step1_on_cancelled_transition():
    payload = _run_reconnect_outcome(
        '{stage: "cancelled", operation_id: "op-1"}', '"op-1"'
    )
    assert payload == {"events": ["cancelled"], "calls": 1}


def test_reconnect_fails_closed_on_wrong_operation():
    payload = _run_reconnect_outcome(
        '{stage: "failed_recoverable", operation_id: "op-2"}', '"op-1"'
    )
    assert payload == {"events": ["wrong_operation"], "calls": 1}


def test_reconnect_keeps_polling_while_transition_still_runs():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is required for the reconnect running contract")
    js = _read("admin.js")
    reconnect = "async function waitForAdminReconnect" + _async_fn_body(
        js, "async function waitForAdminReconnect"
    )
    classify = _extract_fn(js, "classifyReconnectTransition")
    script = f"""
let adminReconnectInFlight = null;
let pendingAuthenticatedWorkflowResume = false;
let events = [];
let calls = 0;
const authState = {{authenticated: true, adminInstanceId: "old"}};
const upgradeState = {{running: true}};
const Date = {{now: (() => {{ let v = 0; return () => (v += 1000); }})()}};
const responses = [
  {{ok: true, json: async () => ({{authenticated: true, admin_instance_id: "old"}})}},
  {{ok: true, json: async () => ({{authenticated: true, admin_instance_id: "old"}})}},
  {{ok: true, json: async () => ({{authenticated: true, admin_instance_id: "new"}})}},
];
async function rawFetch() {{ return responses[calls++]; }}
function showReconnectOverlay() {{}}
function hideReconnectOverlay() {{ events.push("hide"); }}
function showManualReloadHint() {{ events.push("manual"); }}
function reloadForReplacedAdmin() {{ events.push("reload"); }}
function sleep() {{ return Promise.resolve(); }}
async function applyAuthStatus(status) {{
  events.push("applied:" + status.admin_instance_id);
}}
{classify}
async function readReconnectTransition() {{
  return {{stage: "admin_reconnect_pending", operation_id: "op-1"}};
}}
function surfaceReconnectTransitionFailure() {{ events.push("failure"); }}
async function surfaceReconnectTransitionCancelled() {{ events.push("cancelled"); }}
function surfaceReconnectWrongOperation() {{ events.push("wrong_operation"); }}
{reconnect}
(async () => {{
  await waitForAdminReconnect("old", "op-1");
  console.log(JSON.stringify({{events, calls}}));
}})();
"""
    result = subprocess.run(
        [node, "-e", script], text=True, capture_output=True, check=False
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    # A still-running transition never stops the loop: it keeps polling until the
    # new instance appears and only then reloads for the replacement's assets.
    assert "failure" not in payload["events"]
    assert "cancelled" not in payload["events"]
    assert "manual" not in payload["events"]
    assert payload["events"] == ["reload"]
    assert payload["calls"] == 3


def test_update_admin_binds_reconnect_to_the_started_operation():
    js = _read("admin.js")
    fn = _async_fn_body(js, "async function updateAdminForSystemBuild")
    # The reconnect is bound to the concrete operation so a failure of that
    # transition is detected, not just a new admin instance id.
    assert "waitForAdminReconnect(previousAdminInstanceId," in fn


# --- Phase 1: reconnect bound to the returned operation id -----------------

def _run_update_admin_start(fetch_json):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is required for the update-admin reconnect binding test")
    js = _read("admin.js")
    helper = _extract_fn(js, "reconnectOperationIdFromStart")
    update = "async function updateAdminForSystemBuild" + _async_fn_body(
        js, "async function updateAdminForSystemBuild"
    )
    harness = f"""
const SYSTEM_BUILD_STATUS = {{
  IDLE:"idle", VALIDATING:"validating", VALID:"valid", INVALID:"invalid",
  CONFIRMING:"confirming", UPDATING:"updating", RECONNECTING:"reconnecting",
  FAILED:"failed",
}};
let selectedSystemBuildTag = "v1";
const authState = {{adminInstanceId: "old"}};
let systemBuildMutationLocked = false;
let setupIntentId = "intent-1";
let setupOperationId = null;
let reconnectOp = "__uncalled__";
const systemBuildState = {{
  status: SYSTEM_BUILD_STATUS.VALID,
  result: {{admin_update_required: true, next_allowed: false}},
  error: null, lastAction: null, failedAction: null,
}};
function systemBuildIsUpdating() {{
  return systemBuildState.status === SYSTEM_BUILD_STATUS.UPDATING ||
    systemBuildState.status === SYSTEM_BUILD_STATUS.RECONNECTING;
}}
function systemBuildMutationInProgress() {{
  return systemBuildMutationLocked ||
    systemBuildState.status === SYSTEM_BUILD_STATUS.CONFIRMING;
}}
async function validateSelectedSystemBuild() {{
  systemBuildState.status = SYSTEM_BUILD_STATUS.VALID;
}}
function systemBuildUpdateAllowed() {{ return true; }}
function applySystemBuildAlignment() {{}}
function setupIntentHeaders(h) {{ return h || {{}}; }}
function handleSetupIntentRejection() {{ return false; }}
function renderSystemAlignmentStatus() {{}}
function showReconnectOverlay() {{}}
async function waitForAdminReconnect(prev, op) {{ reconnectOp = op; }}
async function fetch() {{
  return {{ok: true, status: 202, json: async () => ({fetch_json})}};
}}
{helper}
{update}
(async () => {{
  await updateAdminForSystemBuild();
  console.log(JSON.stringify({{
    reconnectOp,
    setupOperationId,
    status: systemBuildState.status,
    failedAction: systemBuildState.failedAction,
  }}));
}})();
"""
    result = subprocess.run(
        [node, "-e", harness], text=True, capture_output=True, check=False
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_reconnect_operation_id_prefers_top_level():
    js = _read("admin.js")
    helper = _extract_fn(js, "reconnectOperationIdFromStart")
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is required")
    script = (
        helper
        + '\nconsole.log(reconnectOperationIdFromStart('
        '{operation_id: "top", transition: {operation_id: "nested"}}));'
    )
    out = subprocess.run([node, "-e", script], text=True, capture_output=True, check=False)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "top"


def test_reconnect_operation_id_falls_back_to_nested_transition():
    js = _read("admin.js")
    helper = _extract_fn(js, "reconnectOperationIdFromStart")
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is required")
    script = (
        helper
        + '\nconsole.log(reconnectOperationIdFromStart('
        '{transition: {operation_id: "nested"}}));'
    )
    out = subprocess.run([node, "-e", script], text=True, capture_output=True, check=False)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "nested"


def test_reconnect_operation_id_missing_returns_null():
    js = _read("admin.js")
    helper = _extract_fn(js, "reconnectOperationIdFromStart")
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is required")
    script = helper + "\nconsole.log(reconnectOperationIdFromStart({reconnect: true}));"
    out = subprocess.run([node, "-e", script], text=True, capture_output=True, check=False)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "null"


def test_update_admin_uses_top_level_operation_id_for_reconnect():
    payload = _run_update_admin_start('{operation_id: "op-123", reconnect: true}')
    assert payload["reconnectOp"] == "op-123"
    # The reconnect polling is bound to the operation before it starts.
    assert payload["setupOperationId"] == "op-123"


def test_update_admin_falls_back_to_nested_operation_id():
    payload = _run_update_admin_start(
        '{transition: {operation_id: "op-9"}, reconnect: true}'
    )
    assert payload["reconnectOp"] == "op-9"
    assert payload["setupOperationId"] == "op-9"


def test_update_admin_fails_closed_without_operation_id():
    # reconnect=true but no operation id: never poll blindly, fail closed.
    payload = _run_update_admin_start('{reconnect: true}')
    assert payload["reconnectOp"] == "__uncalled__"
    assert payload["status"] == "failed"
    assert payload["setupOperationId"] is None


# --- Phase 2: server-led gating of the two setup actions -------------------

_RECONNECT_PENDING = """{
  alignment: "admin_update_required", admin_update_required: true,
  next_allowed: false, embedded_resources_valid: true,
  transition_in_progress: true, transition_stage: "admin_reconnect_pending",
  active_transition_tag: "v1",
  action_state: { admin_update_required: true, admin_update_allowed: false,
    continue_allowed: false, terminal_error: null, busy: true,
    polling_required: true, progress_message: "Waiting for reconnect…" },
}"""
_UPDATE_PENDING = """{
  alignment: "admin_update_required", admin_update_required: true,
  next_allowed: false, embedded_resources_valid: true,
  transition_in_progress: true, transition_stage: "admin_update_pending",
  active_transition_tag: "v1",
  action_state: { admin_update_required: true, admin_update_allowed: false,
    continue_allowed: false, terminal_error: null, busy: true,
    polling_required: true, progress_message: "Preparing update…" },
}"""
_FAILED_RECOVERABLE = """{
  alignment: "admin_update_required", admin_update_required: true,
  next_allowed: false, embedded_resources_valid: true,
  transition_in_progress: true, transition_stage: "failed_recoverable",
  recovery_required: true, active_transition_tag: "v1",
  action_state: { admin_update_required: true, admin_update_allowed: false,
    continue_allowed: false, busy: false,
    terminal_error: {code: "recovery_required", message: "Recover the operation."} },
}"""
_RESOURCES_VERIFIED_MATCH = """{
  alignment: "aligned", admin_update_required: false, next_allowed: true,
  embedded_resources_valid: true, resources_verified: true, operation_id: "op-1",
  confirmation_allowed: false, transition_in_progress: true,
  transition_stage: "resources_verified", active_transition_tag: "v1",
  action_state: { admin_update_required: false, admin_update_allowed: false,
    continue_allowed: true, terminal_error: null, busy: false },
}"""
_FOREIGN_TRANSITION = """{
  alignment: "admin_update_required", admin_update_required: true,
  next_allowed: false, embedded_resources_valid: true,
  transition_in_progress: true, transition_stage: "admin_reconnect_pending",
  active_transition_tag: "v2",
  action_state: { admin_update_required: true, admin_update_allowed: false,
    continue_allowed: false, busy: false,
    terminal_error: {code: "transition_active_for_another_build",
      message: "Finish v2 before selecting v1."} },
}"""


def test_gating_admin_reconnect_pending_disables_both():
    out = _actions_for("VALID", _RECONNECT_PENDING)
    assert out["align"]["enabled"] is False
    assert out["next"]["enabled"] is False


def test_gating_admin_update_pending_disables_both():
    out = _actions_for("VALID", _UPDATE_PENDING)
    assert out["align"]["enabled"] is False
    assert out["next"]["enabled"] is False


def test_gating_failed_recoverable_disables_both():
    out = _actions_for("VALID", _FAILED_RECOVERABLE)
    assert out["align"]["enabled"] is False
    assert out["next"]["enabled"] is False


def test_gating_recovery_required_does_not_enable_align():
    out = _actions_for("VALID", _FAILED_RECOVERABLE)
    assert out["align"]["enabled"] is False


def test_gating_resources_verified_match_enables_only_continue():
    out = _actions_for("VALID", _RESOURCES_VERIFIED_MATCH)
    assert out["next"]["enabled"] is True
    assert out["next"]["label"] == "Continue"
    assert out["align"]["enabled"] is False


def test_gating_foreign_transition_disables_both():
    # A live transition for another build tag blocks both actions for this one.
    out = _actions_for("VALID", _FOREIGN_TRANSITION)
    assert out["align"]["enabled"] is False
    assert out["next"]["enabled"] is False


# --- Phase 3: embedded-resource mismatch surfaces as a recreate ------------

def _run_status_message_node(setup):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is required for the status message behaviour test")
    js = _read("admin.js")
    # The real alignment-text table plus the real message renderer.
    text = js.split("const SYSTEM_BUILD_ALIGNMENT_TEXT = {", 1)[1].split("};", 1)[0]
    message = _extract_fn(js, "systemBuildStatusMessage")
    action = _extract_fn(js, "systemBuildActionState")
    harness = f"""
const SYSTEM_BUILD_STATUS = {{
  IDLE:"idle", VALIDATING:"validating", VALID:"valid", INVALID:"invalid",
  CONFIRMING:"confirming", UPDATING:"updating", RECONNECTING:"reconnecting",
  FAILED:"failed",
}};
const SYSTEM_BUILD_ALIGNMENT_TEXT = {{{text}}};
let selectedSystemBuildTag = "v1";
const systemBuildState = {{
  status: SYSTEM_BUILD_STATUS.VALID, result: null, error: null,
  lastAction: null, failedAction: null,
}};
{action}
{message}
"""
    script = harness + "\n" + setup
    result = subprocess.run(
        [node, "-e", script], text=True, capture_output=True, check=False
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def test_status_message_recreate_required_is_not_already_matches():
    out = _run_status_message_node(
        """
systemBuildState.result = {
  alignment: "admin_recreate_required", admin_update_required: true,
  embedded_resources_valid: false, next_allowed: false,
};
console.log(systemBuildStatusMessage());
"""
    )
    assert "already matches" not in out
    # An embedded-resource mismatch uses the standard Admin update message, not a
    # bespoke resource-mismatch sentence.
    assert out == "The Admin Server must be updated before you can continue."
    assert "does not contain the resources" not in out


def test_setup_update_status_copy_never_uses_alignment_language():
    # Every user-facing status line for the Admin Server update avoids the
    # internal "align/alignment" vocabulary.
    states = (
        "systemBuildState.status = SYSTEM_BUILD_STATUS.UPDATING;",
        "systemBuildState.status = SYSTEM_BUILD_STATUS.RECONNECTING;",
        'systemBuildState.result = { alignment: "aligned" };',
        'systemBuildState.result = { alignment: "admin_update_required",'
        " admin_update_required: true };",
        "systemBuildState.status = SYSTEM_BUILD_STATUS.FAILED;"
        ' systemBuildState.failedAction = "align";',
    )
    for state in states:
        out = _run_status_message_node(
            state + "\nconsole.log(systemBuildStatusMessage());"
        )
        assert "align" not in out.lower(), out


# --- Phase 4: action-specific retry ownership ------------------------------

def test_action_matrix_failed_validate_offers_check_again():
    out = _actions_for("FAILED", "null", 'systemBuildState.failedAction = "validate";')
    assert out["align"] == {"enabled": True, "label": "Check again"}
    assert out["next"]["enabled"] is False


def test_action_matrix_failed_align_offers_try_again_on_the_left():
    out = _actions_for("FAILED", "null", 'systemBuildState.failedAction = "align";')
    assert out["align"] == {"enabled": True, "label": "Try again"}
    assert out["next"]["enabled"] is False


def test_action_matrix_failed_confirm_offers_try_again_on_the_right():
    out = _actions_for("FAILED", "null", 'systemBuildState.failedAction = "confirm";')
    assert out["align"]["enabled"] is False
    assert out["next"] == {"enabled": True, "label": "Try again"}


def test_action_matrix_failed_never_enables_both_retries():
    for value in ('"validate"', '"align"', '"confirm"', "null"):
        out = _actions_for("FAILED", "null", f"systemBuildState.failedAction = {value};")
        assert not (out["align"]["enabled"] and out["next"]["enabled"])


def _run_action_dispatch(dispatcher, failed_action):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is required for the setup action dispatch test")
    js = _read("admin.js")
    align = "async function handleAlignAdminClick" + _async_fn_body(
        js, "async function handleAlignAdminClick"
    )
    cont = "async function handleContinueClick" + _async_fn_body(
        js, "async function handleContinueClick"
    )
    fa = "null" if failed_action is None else f'"{failed_action}"'
    harness = f"""
const SYSTEM_BUILD_STATUS = {{
  IDLE:"idle", VALIDATING:"validating", VALID:"valid", INVALID:"invalid",
  CONFIRMING:"confirming", UPDATING:"updating", RECONNECTING:"reconnecting",
  FAILED:"failed",
}};
const systemBuildState = {{
  status: SYSTEM_BUILD_STATUS.FAILED, failedAction: {fa}, result: {{}},
  error: null, lastAction: null,
}};
let calls = [];
async function updateAdminForSystemBuild() {{ calls.push("update"); }}
async function validateSelectedSystemBuild() {{ calls.push("validate"); }}
async function confirmSelectedSystemBuild() {{ calls.push("confirm"); }}
function applySystemBuildAlignment() {{}}
{align}
{cont}
(async () => {{
  await {dispatcher}();
  console.log(JSON.stringify(calls));
}})();
"""
    result = subprocess.run(
        [node, "-e", harness], text=True, capture_output=True, check=False
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_align_retry_repeats_the_failed_update():
    assert _run_action_dispatch("handleAlignAdminClick", "align") == ["update"]


def test_check_again_repeats_only_validation():
    assert _run_action_dispatch("handleAlignAdminClick", "validate") == ["validate"]


def test_align_button_never_confirms():
    # A confirm failure is owned by the right action; the left never confirms.
    assert _run_action_dispatch("handleAlignAdminClick", "confirm") == []


def test_continue_retry_repeats_the_failed_confirmation():
    assert _run_action_dispatch("handleContinueClick", "confirm") == ["confirm"]


def test_continue_button_never_updates_admin():
    for fa in ("align", "validate", None):
        assert "update" not in _run_action_dispatch("handleContinueClick", fa)


def _run_validate_ownership(internal):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is required for the validate ownership test")
    js = _read("admin.js")
    validate = "async function validateSelectedSystemBuild" + _async_fn_body(
        js, "async function validateSelectedSystemBuild"
    )
    flag = "true" if internal else "false"
    harness = f"""
const SYSTEM_BUILD_STATUS = {{
  IDLE:"idle", VALIDATING:"validating", VALID:"valid", INVALID:"invalid",
  CONFIRMING:"confirming", UPDATING:"updating", RECONNECTING:"reconnecting",
  FAILED:"failed",
}};
let selectedSystemBuildTag = "v1";
const setupEls = {{ releaseSelect: {{ value: "v1" }} }};
const systemBuildState = {{
  status: SYSTEM_BUILD_STATUS.VALID, result: null, error: null,
  lastAction: null, failedAction: "align", validationGeneration: 0,
}};
function renderDevelopmentBuildChecks() {{}}
function renderSystemAlignmentStatus() {{}}
function applySystemBuildAlignment() {{}}
async function fetch() {{ return {{ok:false, status:500, json: async () => ({{message:"boom"}})}}; }}
{validate}
(async () => {{
  await validateSelectedSystemBuild({{ internal: {flag} }});
  console.log(JSON.stringify({{
    status: systemBuildState.status,
    failedAction: systemBuildState.failedAction,
  }}));
}})();
"""
    result = subprocess.run(
        [node, "-e", harness], text=True, capture_output=True, check=False
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_internal_revalidation_does_not_claim_failure_ownership():
    out = _run_validate_ownership(internal=True)
    assert out["status"] == "failed"
    # The outer mutation still owns the failure; the internal check never sets it.
    assert out["failedAction"] == "align"


def test_top_level_validation_owns_its_own_failure():
    out = _run_validate_ownership(internal=False)
    assert out["status"] == "failed"
    assert out["failedAction"] == "validate"


# --- Phase 5: step unlocking bound to a confirmed operation context --------

def _run_confirmed_setup_ready(context_js, *, release_version="v1"):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is required for the confirmed-setup gating test")
    js = _read("admin.js")
    functions = "\n".join(
        _extract_fn(js, name)
        for name in ("releaseReady", "confirmedSetupBuildReady", "stepLocked")
    )
    harness = f"""
const setupState = {{
  release: {{ status: "ready", version: {json.dumps(release_version)} }},
  config: {{ template_loaded: true, template_tag: "v1" }},
  deployment: {{ generated_ready: false }},
}};
let setupOperationContext = {context_js};
function deploymentReady() {{ return false; }}
{functions}
console.log(JSON.stringify({{
  ready: confirmedSetupBuildReady(),
  devices: stepLocked("devices"),
  config: stepLocked("config"),
}}));
"""
    result = subprocess.run(
        [node, "-e", harness], text=True, capture_output=True, check=False
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_prepared_cache_without_operation_keeps_devices_locked():
    out = _run_confirmed_setup_ready("null")
    assert out["ready"] is False
    assert out["devices"] is True
    assert out["config"] is True


def test_prepared_cache_with_wrong_operation_tag_keeps_devices_locked():
    out = _run_confirmed_setup_ready(
        '{ operationId: "op-1", systemTag: "v2" }', release_version="v1"
    )
    assert out["ready"] is False
    assert out["devices"] is True


def test_confirmed_matching_operation_unlocks_devices():
    out = _run_confirmed_setup_ready(
        '{ operationId: "op-1", systemTag: "v1" }', release_version="v1"
    )
    assert out["ready"] is True
    assert out["devices"] is False
    assert out["config"] is False


def test_confirmed_operation_without_id_keeps_devices_locked():
    out = _run_confirmed_setup_ready('{ operationId: null, systemTag: "v1" }')
    assert out["ready"] is False
    assert out["devices"] is True


def test_selection_change_clears_the_operation_context():
    js = _read("admin.js")
    body = _async_fn_body(js, "async function onReleaseSelectChange")
    assert "clearSetupOperationContext()" in body


def test_start_over_clears_the_operation_context():
    js = _read("admin.js")
    body = _extract_fn(js, "startGuidedSetupOver")
    assert "clearSetupOperationContext()" in body


def test_logout_clears_the_operation_context():
    js = _read("admin.js")
    body = _async_fn_body(js, "async function submitLogout")
    assert "clearSetupOperationContext()" in body


def test_stale_intent_clears_the_operation_context():
    js = _read("admin.js")
    body = _extract_fn(js, "handleSetupIntentRejection")
    assert "clearSetupOperationContext()" in body


def test_resume_reconstructs_context_only_from_server_transition():
    js = _read("admin.js")
    body = _async_fn_body(js, "async function resumeGuidedSetupFromTransition")
    # Context is rebuilt from the transition (operation id + system tag), never
    # from a prepared cache or stored front-end state.
    assert "bindConfirmedSetupOperation(" in body or "setupOperationContext" in body
    assert "transition.operation_id" in body
    assert "transition.system_tag" in body


# --- Phase 6: precise, action-specific status messages ---------------------

def _status_for(setup):
    return _run_status_message_node(setup + "\nconsole.log(systemBuildStatusMessage());")


def test_status_failed_validation_names_validation():
    out = _status_for(
        'systemBuildState.status = SYSTEM_BUILD_STATUS.FAILED;'
        ' systemBuildState.failedAction = "validate";'
    )
    assert out == "System Build validation failed. Check the details and try again."


def test_status_failed_admin_update_names_the_admin_server():
    out = _status_for(
        'systemBuildState.status = SYSTEM_BUILD_STATUS.FAILED;'
        ' systemBuildState.failedAction = "align";'
    )
    assert out == "The Admin Server update failed. Check the details and try again."


def test_status_failed_confirmation_names_confirmation():
    out = _status_for(
        'systemBuildState.status = SYSTEM_BUILD_STATUS.FAILED;'
        ' systemBuildState.failedAction = "confirm";'
    )
    assert out == "System Build confirmation failed. Check the details and try again."


def test_status_active_transition_reports_in_progress():
    out = _status_for(
        'systemBuildState.result = { alignment: "admin_update_required",'
        ' transition_in_progress: true, next_allowed: false,'
        ' transition_stage: "admin_reconnect_pending" };'
    )
    assert out == "The Admin Server update is already in progress."


def test_status_recovery_required_reports_recovery():
    out = _status_for(
        'systemBuildState.result = { alignment: "admin_update_required",'
        ' recovery_required: true, transition_in_progress: true,'
        ' next_allowed: false, transition_stage: "failed_recoverable" };'
    )
    assert out == "System Build recovery is required before continuing."


def test_status_resource_mismatch_reports_missing_resources():
    out = _status_for(
        'systemBuildState.result = { alignment: "admin_recreate_required",'
        ' embedded_resources_valid: false, admin_update_required: true,'
        ' next_allowed: false };'
    )
    # An embedded-resource mismatch uses the standard Admin update message; the
    # technical embedded-resource detail is left to diagnostics.
    assert out == "The Admin Server must be updated before you can continue."


# --- authoritative, live seven-stage System Build progress -----------------


def _run_system_alignment_progress(payload):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is required for System Build progress contracts")
    js = _read("admin.js")
    functions = "\n".join(
        _extract_fn(js, name)
        for name in (
            "resolveSystemAlignmentStage",
            "systemAlignmentAdminRequired",
            "systemAlignmentStageStates",
        )
    )
    script = f"""
const SYSTEM_ALIGNMENT_STAGE_ORDER = [
  "select", "validate", "align-admin", "reconnect", "verify-resources",
  "install-ems", "verify-system",
];
const SYSTEM_ALIGNMENT_STAGE_INDEX = {{
  selection_started: 0, validation_running: 1, validation_failed: 1,
  validated: 1, admin_update_pending: 2, admin_alignment_started: 2,
  admin_reconnect_pending: 3, admin_aligned: 4, resources_verified: 5,
  ems_operation_pending: 5, ems_operation_running: 5,
  healthcheck_pending: 6, completed: 7,
}};
{functions}
console.log(JSON.stringify(systemAlignmentStageStates({json.dumps(payload)})));
"""
    result = subprocess.run(
        [node, "-e", script], text=True, capture_output=True, check=False
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_durable_transition_stage_wins_over_generic_validated_status():
    result = _run_system_alignment_progress(
        {
            "status": "validated",
            "transition_stage": "admin_aligned",
            "stage": "resources_verified",
            "transition": {
                "stage": "healthcheck_pending",
                "admin_alignment_required": False,
            },
        }
    )
    assert result["stage"] == "healthcheck_pending"
    assert result["states"] == [
        "done", "done", "skipped", "skipped", "done", "done", "active"
    ]


@pytest.mark.parametrize(
    ("stage", "admin_required", "expected"),
    [
        (
            "selection_started",
            None,
            ["active", "pending", "pending", "pending", "pending", "pending", "pending"],
        ),
        (
            "validation_running",
            None,
            ["done", "active", "pending", "pending", "pending", "pending", "pending"],
        ),
        (
            "validated",
            True,
            ["done", "done", "active", "pending", "pending", "pending", "pending"],
        ),
        (
            "admin_aligned",
            True,
            ["done", "done", "done", "done", "active", "pending", "pending"],
        ),
        (
            "resources_verified",
            False,
            ["done", "done", "skipped", "skipped", "done", "active", "pending"],
        ),
        (
            "admin_update_pending",
            True,
            ["done", "done", "active", "pending", "pending", "pending", "pending"],
        ),
        (
            "admin_reconnect_pending",
            True,
            ["done", "done", "done", "active", "pending", "pending", "pending"],
        ),
        (
            "ems_operation_running",
            True,
            ["done", "done", "done", "done", "done", "active", "pending"],
        ),
        (
            "healthcheck_pending",
            True,
            ["done", "done", "done", "done", "done", "done", "active"],
        ),
        (
            "completed",
            False,
            ["done", "done", "skipped", "skipped", "done", "done", "done"],
        ),
    ],
)
def test_system_alignment_expected_state_mapping(stage, admin_required, expected):
    result = _run_system_alignment_progress(
        {
            "transition": {
                "stage": stage,
                "admin_alignment_required": admin_required,
            }
        }
    )
    assert result["states"] == expected


def test_reload_stage_payloads_preserve_all_advanced_progress_states():
    expected_active = {
        "resources_verified": 5,
        "ems_operation_running": 5,
        "healthcheck_pending": 6,
    }
    for stage, active_index in expected_active.items():
        result = _run_system_alignment_progress(
            {
                "status": "validated",
                "transition": {
                    "operation_id": "op-1",
                    "stage": stage,
                    "admin_alignment_required": False,
                },
            }
        )
        assert result["stage"] == stage
        assert result["states"][active_index] == "active"
        assert result["states"][1] == "done"


def _run_system_alignment_fact_reset(stage):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is required for System Build stale-fact contracts")
    js = _read("admin.js")
    reset = _extract_fn(js, "resetSystemAlignmentPresentation")
    render = _extract_fn(js, "renderSystemAlignmentStatus")
    script = f"""
const systemAlignmentEls = {{
  workflow: {{hidden: true}}, tag: {{textContent: ""}}, buildId: {{textContent: ""}},
  revision: {{textContent: ""}}, adminImage: {{textContent: ""}},
  emsImage: {{textContent: ""}}, message: {{textContent: ""}},
  warning: {{textContent: "", hidden: true}}, reconnect: {{hidden: true}},
  partial: {{hidden: true}}, partialMessage: {{textContent: ""}},
  resume: {{disabled: true}}, returnToRunning: {{disabled: true}},
}};
let systemAlignmentState = null;
const document = {{querySelectorAll: () => []}};
function systemAlignmentStageStates(payload) {{
  return {{stage: payload.status || null, states: Array(7).fill("pending")}};
}}
function systemAlignmentShouldPoll() {{ return false; }}
function scheduleSystemAlignmentPoll() {{}}
function applySystemBuildPresentation() {{}}
function applyUpgradeAlignmentTransition() {{}}
{reset}
{render}
renderSystemAlignmentStatus({{
  status: "validated",
  system_build: {{canonical_tag: "v-old", build_id: "old-build",
    revision: "old-revision", admin_image: "old-admin", ems_image: "old-ems"}},
}});
resetSystemAlignmentPresentation("v-new", {json.dumps(stage)}, "validation failed");
console.log(JSON.stringify({{
  tag: systemAlignmentEls.tag.textContent,
  buildId: systemAlignmentEls.buildId.textContent,
  revision: systemAlignmentEls.revision.textContent,
  adminImage: systemAlignmentEls.adminImage.textContent,
  emsImage: systemAlignmentEls.emsImage.textContent,
  message: systemAlignmentEls.message.textContent,
}}));
"""
    result = subprocess.run(
        [node, "-e", script], text=True, capture_output=True, check=False
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


@pytest.mark.parametrize("stage", ["selection_started", "validation_failed"])
def test_new_selection_or_validation_failure_clears_stale_build_facts(stage):
    result = _run_system_alignment_fact_reset(stage)
    assert result == {
        "tag": "v-new",
        "buildId": "Unknown",
        "revision": "Unknown",
        "adminImage": "Unknown",
        "emsImage": "Unknown",
        "message": "validation failed",
    }


def test_every_system_build_mutation_feeds_the_shared_renderer():
    js = _read("admin.js")
    mutation_functions = (
        "async function validateSelectedSystemBuild",
        "async function confirmSelectedSystemBuild",
        "async function updateAdminForSystemBuild",
        "async function resumeSystemAlignment",
        "async function prepareUpgradeTarget",
        "async function startDeployment",
        "function applyStartJob",
        "async function executeUpgrade",
        "async function resumeGuidedUpgrade",
        "async function pollUpgradeJob",
    )
    for header in mutation_functions:
        body = (
            _async_fn_body(js, header)
            if header.startswith("async ")
            else _extract_fn(js, header.removeprefix("function "))
        )
        assert "renderSystemAlignmentStatus(" in body or "loadSystemAlignmentStatus(" in body

    confirm = _async_fn_body(js, "async function confirmSelectedSystemBuild")
    cursor = 0
    while True:
        navigate = confirm.find('setActiveStep("devices")', cursor)
        if navigate < 0:
            break
        assert confirm.rfind("renderSystemAlignmentStatus(", cursor, navigate) >= cursor
        cursor = navigate + 1


def test_transition_polling_uses_named_interval_and_terminal_stop_rules():
    js = _read("admin.js")
    schedule = _extract_fn(js, "scheduleSystemAlignmentPoll")
    should_poll = _extract_fn(js, "systemAlignmentShouldPoll")
    logout = _async_fn_body(js, "async function submitLogout")
    assert "SYSTEM_ALIGNMENT_POLL_INTERVAL_MS" in schedule
    assert "1800" not in schedule
    for stage in (
        "admin_update_pending",
        "admin_reconnect_pending",
        "admin_aligned",
        "resources_verified",
        "ems_operation_pending",
        "ems_operation_running",
        "healthcheck_pending",
    ):
        assert stage in js
    assert "SYSTEM_ALIGNMENT_TERMINAL_STAGES" in should_poll
    assert "stopSystemAlignmentPolling" in logout


# --- priority-aware auto-add + symmetric MQTT-selection clears ---------------


def _run_autoadd_node(setup):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for the auto-add priority test")
    js = _read("admin.js")
    helpers = "\n".join(
        _extract_fn(js, name)
        for name in (
            "normalizeSerial",
            "zendureMqttPreferredOverLocalApi",
            "serialOfferedOverZendureMqtt",
            "autoAddInverters",
        )
    )
    preamble = """
let availableDevices = [];
let latestMqttProposals = [];
let zendureMqttEnabled = true;
let configDraftItems = [];
let inverterSeq = 0;
const configDismissed = new Set();
const dismissedSerials = new Set();
function inverterDismissed() { return false; }
function serialSelectedOverMqtt() { return false; }
const discoveryPreparation = { discovery_priority: ["local_api", "local_mqtt", "zendure_mqtt"] };
function discoverySourceEnabled(source) {
  return source === "zendure_mqtt" ? zendureMqttEnabled : true;
}
function isMqttGridMeterProposal(p) { return String(p.target || "") === "grid_meter"; }
function availableConfigDevices() { return availableDevices; }
function isAutoConfigReady(device) { return device.ready !== false; }
function deviceKey(device) { return String(device.serial_number || device.id || ""); }
function draftHasSource(id) { return configDraftItems.some((it) => it.source_id === id); }
function draftItemFromDevice(device, role) {
  inverterSeq += 1;
  return {
    role: role,
    source_id: deviceKey(device),
    serial_number: device.serial_number || "",
    config_name: "inverter_" + inverterSeq,
  };
}
"""
    script = preamble + helpers + "\n" + setup
    result = subprocess.run(
        [node, "-e", script], text=True, capture_output=True, check=False
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_js_auto_add_defers_device_offered_over_prioritized_zendure_mqtt():
    out = _run_autoadd_node(
        """
discoveryPreparation.discovery_priority = ["zendure_mqtt", "local_api", "local_mqtt"];
availableDevices = [
  { serial_number: "SN-A", role_suggestion: "inverter" },
  { serial_number: "SN-B", role_suggestion: "inverter" },
];
latestMqttProposals = [{ target: "device", serial_number: "SN-A" }];
autoAddInverters();
console.log(JSON.stringify({ serials: configDraftItems.map((i) => i.serial_number) }));
"""
    )
    # SN-A is offered over the higher-priority MQTT source, so it is left for the
    # user to select over MQTT instead of auto-grabbed as local HTTP.
    assert out["serials"] == ["SN-B"]


def test_js_auto_add_keeps_http_when_local_api_prioritized():
    out = _run_autoadd_node(
        """
availableDevices = [{ serial_number: "SN-A", role_suggestion: "inverter" }];
latestMqttProposals = [{ target: "device", serial_number: "SN-A" }];
autoAddInverters();
console.log(JSON.stringify({ serials: configDraftItems.map((i) => i.serial_number) }));
"""
    )
    # Default priority is local_api-first, so the HTTP device is still auto-added.
    assert out["serials"] == ["SN-A"]


# The old drop-only dropAutoAddedHttpForMqtt was replaced by the unified
# reconcileTransportSelection planner; its drop-and-select behavior is covered
# by tests/test_admin_setup_transport_selection.py.


def _run_clear_mqtt_node(setup):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for the MQTT clear test")
    js = _read("admin.js")
    helpers = "\n".join(
        _extract_fn(js, name)
        for name in ("resetMqttBrokerForm", "clearMqttSelection")
    )
    preamble = """
const removedKeys = [];
const window = { localStorage: { removeItem: (k) => removedKeys.push(k) } };
const CONFIG_MQTT_PREVIEW_STORAGE_KEY = "preview";
const CONFIG_MQTT_MANUAL_DEVICES_STORAGE_KEY = "manual";
const CONFIG_MQTT_BROKER_STORAGE_KEY = "broker";
const zendureMqttPreviewProposals = new Map([["x", { id: "x" }]]);
const transportInverterNames = new Map([["serial", "INV_1"]]);
let manualMqttDevices = [{ name: "d1" }];
let latestMqttProposals = [];
let renderProposalsCalls = 0;
let renderManualCalls = 0;
function field(v) { return { value: v }; }
const mqttManualEls = {
  brokerName: field("n"), brokerHost: field("h"), brokerPort: field("1883"),
  brokerUsername: field("u"), brokerPassword: field("p"), brokerSecurity: field("tls"),
};
function renderMqttProposals() { renderProposalsCalls += 1; }
function renderManualMqttDevices() { renderManualCalls += 1; }
function resetManualMqttDeviceForm() {}
"""
    script = preamble + helpers + "\n" + setup
    result = subprocess.run(
        [node, "-e", script], text=True, capture_output=True, check=False
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_js_clear_mqtt_selection_empties_every_store():
    out = _run_clear_mqtt_node(
        """
clearMqttSelection();
console.log(JSON.stringify({
  proposals: zendureMqttPreviewProposals.size,
  manual: manualMqttDevices.length,
  host: mqttManualEls.brokerHost.value,
  name: mqttManualEls.brokerName.value,
  security: mqttManualEls.brokerSecurity.value,
  removedKeys: removedKeys.slice().sort(),
  rendered: renderProposalsCalls > 0 && renderManualCalls > 0,
}));
"""
    )
    assert out["proposals"] == 0
    assert out["manual"] == 0
    assert out["host"] == ""
    assert out["name"] == ""
    assert out["security"] == "plain"
    assert out["removedKeys"] == ["broker", "manual", "preview"]
    assert out["rendered"] is True


def test_js_clear_draft_and_start_over_clear_mqtt_selection():
    js = _read("admin.js")
    clear_draft = js.split("if (configEls.clearDraft)", 1)[1].split(
        "\nconst ADMIN_VIEWS", 1
    )[0]
    assert "clearMqttSelection()" in clear_draft
    start_over = _extract_fn(js, "startGuidedSetupOver")
    assert "clearMqttSelection()" in start_over
