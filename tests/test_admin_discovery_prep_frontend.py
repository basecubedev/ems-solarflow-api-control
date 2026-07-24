# SPDX-License-Identifier: AGPL-3.0-or-later
"""Frontend checks for discovery preparation + unified detected-devices UI."""

import os

import pytest

pytestmark = pytest.mark.simulation

STATIC_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "admin", "static"
)


def _read(name):
    with open(os.path.join(STATIC_DIR, name), encoding="utf-8") as handle:
        return handle.read()


def _devices_section(html):
    return html.split('aria-label="Devices"', 1)[1].split('aria-label="Config"', 1)[0]


def test_devices_step_has_discovery_preparation_section():
    devices = _devices_section(_read("index.html"))
    assert 'id="discovery-preparation"' in devices
    assert "Discovery preparation" in devices
    assert 'id="discovery-priority-list"' in devices
    assert 'id="discovery-run"' in devices
    assert "Run discovery" in devices


def test_devices_step_has_single_primary_discovery_action():
    devices = _devices_section(_read("index.html"))
    # Exactly one primary discovery button: Run discovery.
    assert devices.count('id="discovery-run"') == 1
    # The old redundant header actions are gone.
    assert 'id="networks-scan-all"' not in devices
    assert 'id="networks-refresh"' not in devices
    assert "Scan all" not in devices
    assert "Start discovery" not in devices
    # Enabled-source count is passive status text, not a button.
    assert 'id="discovery-source-count"' in devices
    assert "<button" not in devices.split('id="discovery-source-count"', 1)[0].rsplit(
        "<", 1
    )[-1]
    assert "sources enabled" in devices


def test_js_run_discovery_relabels_after_a_scan_completes():
    js = _read("admin.js")
    # After a completed scan the single button becomes "Run discovery again".
    assert "Run discovery again" in js
    assert "unifiedDiscoveryHasRun" in js
    # The enabled-source count is rendered as plural status text.
    assert 'plural(enabled.length, "source") + " enabled"' in js


def test_devices_step_has_unified_result_area_above_details():
    devices = _devices_section(_read("index.html"))
    assert 'id="unified-devices"' in devices
    assert "Detected devices" in devices
    assert 'id="unified-list"' in devices
    assert 'id="unified-empty"' in devices
    assert 'id="unified-count"' in devices
    # The unified area sits above the source-specific detail panels.
    assert devices.index('id="unified-devices"') < devices.index('id="mqtt-details"')
    assert devices.index('id="unified-devices"') < devices.index(
        'id="zendure-cloud-details"'
    )


def test_devices_step_keeps_all_three_source_detail_panels():
    devices = _devices_section(_read("index.html"))
    # Local API results list, Local MQTT and Zendure MQTT detail panels remain.
    assert 'id="results-list"' in devices
    assert 'id="mqtt-details"' in devices
    assert 'id="zendure-cloud-details"' in devices


def test_js_default_priority_matches_spec_order():
    js = _read("admin.js")
    assert (
        'DEFAULT_DISCOVERY_PRIORITY = ["local_api", "local_mqtt", "zendure_mqtt"]' in js
    )


def test_js_uses_preparation_and_run_endpoints():
    js = _read("admin.js")
    assert '"/api/discovery/preparation"' in js
    assert '"/api/discovery/run"' in js
    assert "loadDiscoveryPreparation" in js
    assert "refreshUnifiedDevices" in js
    # Priority editing persists through the POST endpoint.
    assert "persistDiscoveryPreparation" in js
    assert "moveDiscoverySource" in js
    assert "toggleDiscoverySource" in js


def test_js_unified_card_escapes_dynamic_device_and_source_values():
    js = _read("admin.js")
    escape_at = js.find("function escapeHtml")
    render_at = js.find("function renderUnifiedDeviceCard")
    assert render_at != -1, "renderUnifiedDeviceCard missing"
    assert escape_at != -1 and escape_at < render_at
    card = js.split("function renderUnifiedDeviceCard", 1)[1].split(
        "\nfunction ", 1
    )[0]
    # The display/model fallback is built first, then escaped as a whole.
    assert "escapeHtml(String(model))" in card
    assert "escapeHtml(device.serial_number)" in card
    assert "escapeHtml(discoverySourceLabel(source))" in card
    # The priority rows escape the raw source key/label too.
    row = js.split("function renderPrioritySourceRow", 1)[1].split("\nfunction ", 1)[0]
    assert "escapeHtml(source)" in row
    assert "escapeHtml(discoverySourceLabel(source))" in row


def test_js_priority_rows_offer_up_down_and_enable_controls():
    js = _read("admin.js")
    row = js.split("function renderPrioritySourceRow", 1)[1].split("\nfunction ", 1)[0]
    assert "data-prep-up" in row
    assert "data-prep-down" in row
    assert "data-prep-toggle" in row
    # Disabled sources stay in the list but are visually marked.
    assert "is-disabled" in row
    assert "disabled" in row


def test_js_configure_button_opens_matching_detail_panel():
    js = _read("admin.js")
    assert "function openSourceDetail" in js
    fn = js.split("function openSourceDetail", 1)[1].split("\nfunction ", 1)[0]
    assert 'el.tagName === "DETAILS"' in fn
    assert "el.open = true" in fn
    meta = js.split("DISCOVERY_SOURCE_META = {", 1)[1].split("};", 1)[0]
    assert "local-api-details" in meta
    assert "mqtt-details" in meta
    assert "zendure-cloud-details" in meta


def test_local_api_results_live_in_a_collapsed_details_panel():
    devices = _devices_section(_read("index.html"))
    panel = devices.split('id="local-api-details"', 1)
    assert len(panel) == 2, "Local API details panel missing"
    # Same collapsed pattern as the MQTT / Zendure panels.
    assert panel[0].rstrip().endswith('<details class="advanced-details"')
    assert 'id="local-api-count"' in devices
    # The existing Local API results list now sits inside that panel.
    assert 'id="results-list"' in panel[1]
    # The unified overview stays above the Local API detail panel.
    assert devices.index('id="unified-devices"') < devices.index('id="local-api-details"')


def test_js_configure_opens_inline_config_not_a_page_jump():
    js = _read("admin.js")
    row = js.split("function renderPrioritySourceRow", 1)[1].split("\nfunction ", 1)[0]
    # Configure now toggles an inline panel rendered under the same source item.
    assert "data-prep-configure" in row
    assert "renderInlineConfig(source)" in row
    assert "function renderInlineConfig" in js
    assert "function toggleInlineConfig" in js
    # The click handler wires Configure to the inline toggle, not a scroll jump.
    handler = js.split('els.priorityList.addEventListener("click"', 1)[1].split(
        "});", 1
    )[0]
    assert "toggleInlineConfig(source)" in handler
    assert "rescanSource(source)" in handler
    # A distinct "Open details" action still reaches the detail panel.
    assert "data-prep-open-details" in js
    assert "openSourceDetail(source)" in handler


def test_js_inline_config_escapes_dynamic_status():
    js = _read("admin.js")
    fn = js.split("function inlineConfigStatus", 1)[1].split("\nfunction ", 1)[0]
    assert "escapeHtml(" in fn


def _local_api_panel(devices):
    # Everything from the Local API panel up to the next sibling source panel.
    return devices.split('id="local-api-details"', 1)[1].split('id="mqtt-details"', 1)[0]


def test_detected_networks_render_in_global_area_not_local_api():
    devices = _devices_section(_read("index.html"))
    # A dedicated global section carries the networks/gateways context.
    assert 'id="discovery-networks"' in devices
    assert "Detected networks / gateways" in devices
    assert 'id="networks-list"' in devices
    # It sits in the main discovery area, above the source detail panels.
    assert devices.index('id="discovery-networks"') < devices.index(
        'id="local-api-details"'
    )
    # And it is not nested inside the Local API detail panel.
    assert 'id="networks-list"' not in _local_api_panel(devices)
    assert "Detected networks / gateways" not in _local_api_panel(devices)


def test_discovery_progress_renders_above_detected_devices_globally():
    devices = _devices_section(_read("index.html"))
    assert 'id="discovery-progress-section"' in devices
    assert "Discovery progress" in devices
    assert 'id="setup-discovery-progress"' in devices
    # Progress is above the unified detected-devices list.
    assert devices.index('id="setup-discovery-progress"') < devices.index(
        'id="unified-devices"'
    )
    # Progress is not inside the Local API detail panel.
    assert 'id="setup-discovery-progress"' not in _local_api_panel(devices)


def test_networks_and_progress_sit_before_unified_devices():
    devices = _devices_section(_read("index.html"))
    order = [
        devices.index('id="discovery-networks"'),
        devices.index('id="discovery-progress-section"'),
        devices.index('id="unified-devices"'),
    ]
    assert order == sorted(order)


def test_js_progress_text_reflects_active_scan_type():
    js = _read("admin.js")
    assert "function activeScanLabel" in js
    fn = js.split("function activeScanLabel", 1)[1].split("\nfunction ", 1)[0]
    assert '"Device scan"' in fn
    assert '"Network scan"' in fn
    # The progress text is built from the active scan label.
    progress = js.split("function renderSetupDiscoveryProgress", 1)[1].split(
        "\nfunction ", 1
    )[0]
    assert "activeScanLabel()" in progress


def test_js_networks_summary_pill_is_written_with_textcontent():
    js = _read("admin.js")
    fn = js.split("function updateNetworkSummary", 1)[1].split("\nfunction ", 1)[0]
    # Compact pill built from counts only and written via setSummary (textContent).
    assert "els.networksSummary" in fn
    assert 'plural(gateways, "gateway")' in fn


# --- Inline source configuration lives under the Discovery priority rows -----


def _mqtt_panel(devices):
    return devices.split('id="mqtt-details"', 1)[1].split(
        'id="zendure-cloud-details"', 1
    )[0]


def _zendure_panel(devices):
    return devices.split('id="zendure-cloud-details"', 1)[1].split(
        'id="inline-config-parking"', 1
    )[0]


def _inline_block(devices, source):
    parking = devices.split('id="inline-config-parking"', 1)[1]
    block = parking.split('data-inline-config="' + source + '"', 1)[1]
    # Stop at the next inline-config block so each block stays isolated.
    return block.split("data-inline-config=", 1)[0]


def test_inline_config_parking_has_a_block_for_each_source():
    devices = _devices_section(_read("index.html"))
    assert 'id="inline-config-parking"' in devices
    for source in ("local_api", "local_mqtt", "zendure_mqtt"):
        assert 'data-inline-config="' + source + '"' in devices


def test_js_configure_mounts_parked_config_into_the_open_priority_row():
    js = _read("admin.js")
    # Configure renders a stable slot; the parked config node is moved into it.
    row = js.split("function renderInlineConfig", 1)[1].split("\nfunction ", 1)[0]
    assert "data-inline-slot=" in row
    assert "function parkInlineConfigs" in js
    assert "function mountInlineConfig" in js
    render = js.split("function renderDiscoveryPreparation", 1)[1].split(
        "\nfunction ", 1
    )[0]
    # The list re-render parks live nodes before wiping, then re-mounts the open one.
    assert "parkInlineConfigs()" in render
    assert "mountInlineConfig(openInlineConfigSource)" in render


def test_local_api_config_controls_live_in_inline_block_not_detail_panel():
    devices = _devices_section(_read("index.html"))
    panel = _local_api_panel(devices)
    block = _inline_block(devices, "local_api")
    # mDNS and reset controls moved into the inline config block. The manual scan
    # is a separate global section (serves both API and MQTT), not here.
    for marker in (
        'id="mdns-toggle"',
        'id="mdns-refresh"',
        'id="setup-discovery-reset"',
    ):
        assert marker in block
        assert marker not in panel
    assert 'id="scan-form"' not in block


def test_local_mqtt_config_controls_live_in_inline_block_not_detail_panel():
    devices = _devices_section(_read("index.html"))
    panel = _mqtt_panel(devices)
    block = _inline_block(devices, "local_mqtt")
    assert 'id="mqtt-refresh"' in block
    assert 'id="mqtt-message"' in block
    assert 'id="mqtt-refresh"' not in panel
    assert 'id="mqtt-message"' not in panel


def test_local_mqtt_inline_block_has_credential_pool_not_broker_config():
    devices = _devices_section(_read("index.html"))
    block = _inline_block(devices, "local_mqtt")
    # A reusable credential pool: form + list live in the Discovery inline block.
    assert 'id="mqtt-credential-form"' in block
    assert 'id="mqtt-credential-label"' in block
    assert 'id="mqtt-credential-username"' in block
    assert 'id="mqtt-credential-password"' in block
    assert 'id="mqtt-credential-list"' in block
    # No broker host/port/TLS connection config in Discovery.
    for gone in (
        'id="local-broker-host"',
        'id="local-broker-port"',
        'id="local-broker-tls-mode"',
        "Broker host",
        "Broker port",
    ):
        assert gone not in block
    # Copy makes the scope explicit.
    assert "Optional discovery credentials" in block
    assert "adding them happens in the config step" in block


def test_zendure_token_form_lives_in_inline_block_not_detail_panel():
    devices = _devices_section(_read("index.html"))
    panel = _zendure_panel(devices)
    block = _inline_block(devices, "zendure_mqtt")
    for marker in (
        'id="zendure-cloud-token-form"',
        'id="zendure-cloud-token-input"',
        'id="zendure-cloud-save"',
        'id="zendure-cloud-test"',
        'id="zendure-cloud-refresh"',
        'id="zendure-cloud-forget"',
        'id="zendure-cloud-message"',
    ):
        assert marker in block
        assert marker not in panel


def test_zendure_inline_token_stays_masked_and_unprefilled():
    devices = _devices_section(_read("index.html"))
    block = _inline_block(devices, "zendure_mqtt")
    token_input = block.split('id="zendure-cloud-token-input"', 1)[1].split(">", 1)[0]
    assert 'type="password"' in token_input
    assert "value=" not in token_input


def test_source_detail_panels_are_result_only():
    devices = _devices_section(_read("index.html"))
    for extract in (_local_api_panel, _mqtt_panel, _zendure_panel):
        panel = extract(devices)
        # No configuration surfaces remain: no forms, submit buttons or token/scan
        # inputs — only counts, empty states and result lists.
        assert "<form" not in panel
        assert 'type="submit"' not in panel
        assert 'type="password"' not in panel
    # Each panel still carries its result list + empty state + count.
    assert 'id="results-list"' in _local_api_panel(devices)
    assert 'id="mqtt-list"' in _mqtt_panel(devices)
    assert 'id="mqtt-empty"' in _mqtt_panel(devices)
    assert 'id="zendure-cloud-list"' in _zendure_panel(devices)
    assert 'id="zendure-cloud-empty"' in _zendure_panel(devices)
