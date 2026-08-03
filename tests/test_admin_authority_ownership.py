# SPDX-License-Identifier: AGPL-3.0-or-later
"""Structural contracts: each centralized decision keeps exactly one owner.

These pin *ownership and imports*, not formatting. They exist because the
regression they guard against is silent: a second alias table, marker list or
field-index builder added beside the shared one still passes every behavioral
test on the day it is written, and only diverges later.

See ``docs/developer/developer.md`` — "Authority map and test layering" — for
where a new model, transport, broker source, secret field or grid-meter field
must be declared.
"""

import pathlib

import pytest

pytestmark = [
    pytest.mark.admin,
    pytest.mark.authority,
    pytest.mark.contract,
    pytest.mark.simulation,
]

REPO = pathlib.Path(__file__).resolve().parents[1]
ADMIN = REPO / "admin"
ADMIN_JS = REPO / "admin" / "static" / "admin.js"


def _admin_modules(*, exclude=()):
    excluded = set(exclude)
    return [
        path
        for path in sorted(ADMIN.glob("*.py"))
        if path.name not in excluded and path.name != "__init__.py"
    ]


# --- MQTT TLS semantics ------------------------------------------------------
def test_tls_mode_aliases_are_interpreted_in_one_module():
    """Only Core maps a TLS-mode alias onto the canonical vocabulary."""

    from ems import config as cfg

    assert cfg.canonical_mqtt_tls_mode("pinned_ca") == cfg.MQTT_TLS_MODE_INSECURE
    offenders = [
        path.name
        for path in _admin_modules(
            # The discovery client names the CA strategy it selects for its own
            # connection; the accepted set still comes from Core.
            exclude=("zendure_cloud_mqtt.py", "test_support.py")
        )
        if "pinned_ca" in path.read_text() or "encrypted_no_verify" in path.read_text()
    ]
    assert offenders == []


def test_cloud_client_mode_set_is_core_owned():
    from admin import zendure_cloud_mqtt as cloud
    from ems import config as cfg

    assert set(cloud.TLS_MODES) == set(cfg.MQTT_TLS_OBSERVED_MODES)


# --- secret classification ---------------------------------------------------
def test_secret_markers_are_declared_once():
    offenders = [
        path.name
        for path in _admin_modules(exclude=("secret_policy.py",))
        if "passphrase" in path.read_text() or "apikey" in path.read_text()
    ]
    assert offenders == []


def test_catalog_field_classification_is_not_reimplemented_in_admin():
    """Admin re-exports the catalog's answer; it never re-reads the metadata."""

    from admin import secret_policy
    from ems import config_catalog

    assert secret_policy.is_secret_catalog_field is config_catalog.is_secret_catalog_field
    source = pathlib.Path(secret_policy.__file__).read_text()
    # No second body, and no local re-reading of the catalog's metadata keys.
    # The name-marker vocabulary is a different mechanism and stays here.
    assert "def is_secret_catalog_field" not in source
    assert 'field.get("risk")' not in source
    assert 'field.get("type")' not in source


def test_every_admin_classifier_delegates_to_the_policy():
    import admin.guided_setup_workflow as workflow
    import admin.maintenance_config as maintenance
    import admin.setup_config as setup
    import admin.zendure_mqtt_config_draft as draft
    from admin import secret_policy

    for module in (workflow, maintenance, setup, draft):
        source = pathlib.Path(module.__file__).read_text()
        assert "admin.secret_policy" in source, module.__name__

    assert secret_policy.is_secret_key("broker_password", scope=secret_policy.SCOPE_DRAFT)


# --- catalog field indexing --------------------------------------------------
def test_admin_builds_no_field_index_of_its_own():
    """Every index comes from ``config_field_index``, not a local catalog walk."""

    offenders = [
        path.name
        for path in _admin_modules(
            # A pure re-export for external Admin callers; it builds no index.
            exclude=("config_feature_metadata.py",)
        )
        if "get_config_feature_field_index" in path.read_text()
    ]
    assert offenders == []


# --- config mutation ---------------------------------------------------------
def test_catalog_coercion_has_one_owner():
    """No Admin module reimplements how a catalog type reads a raw value."""

    from admin.device_common_fields import coerce_field_value
    from ems.config_mutation import coerce_catalog_value

    assert coerce_field_value is coerce_catalog_value
    offenders = [
        path.name
        for path in _admin_modules()
        if "def coerce_field_value" in path.read_text()
        or "def coerce_catalog_value" in path.read_text()
    ]
    assert offenders == []


def test_grid_meter_variant_cleanup_has_one_authority():
    """The variant cleanup is defined in Core and imported by both flows."""

    offenders = [
        path.name
        for path in _admin_modules()
        if "def strip_incompatible_grid_meter_fields" in path.read_text()
        or "def strip_stale_grid_meter_keys" in path.read_text()
    ]
    assert offenders == []

    import admin.config_preview as preview
    import ems.config_mutation as mutation

    assert (
        preview.strip_incompatible_grid_meter_fields
        is mutation.strip_incompatible_grid_meter_fields
    )


def test_both_workflows_mutate_through_the_shared_core():
    """Setup and Maintenance adapt inputs; neither applies a field itself."""

    import admin.maintenance_config as maintenance
    import admin.setup_config as setup

    for module in (setup, maintenance):
        source = pathlib.Path(module.__file__).read_text()
        assert "from ems.config_mutation import" in source
        assert "apply_grid_meter_changes" in source
        assert "apply_config_changes" in source or "apply_common_values" in source


def test_the_browser_holds_no_config_mutation_rule():
    """Variant cleanup and credential intent stay server-side."""

    source = ADMIN_JS.read_text()
    for marker in (
        "strip_incompatible_grid_meter",
        "GRID_METER_KNOWN_TOP_KEYS",
        "grid_meter_variant_field_spec",
    ):
        assert marker not in source


# --- output-control capability ----------------------------------------------
def test_runtime_and_admin_resolve_control_through_core():
    """One Core module decides write eligibility for both layers."""

    import admin.zendure_mqtt_config_draft as draft
    import ems.zendure_mqtt.config_entries as entries
    import ems.zendure_mqtt.device_client as device_client
    import ems.zendure_mqtt.migration as migration

    for module in (device_client, entries, migration):
        source = pathlib.Path(module.__file__).read_text()
        assert "power_capability import" in source or (
            "from ems.mqtt_control.power_capability import" in source
        ), module.__name__
    assert "ems.zendure_mqtt.capability" in pathlib.Path(draft.__file__).read_text()


def test_frontend_carries_no_hardware_capability_table():
    """The browser projects a verdict; it never owns the model/route matrix."""

    source = ADMIN_JS.read_text()
    for marker in (
        # write profiles
        "zensdk_properties",
        "legacy_hub",
        "legacy_object",
        # publish routes
        "iot_properties_write",
        "iot_function_invoke",
        # TLS vocabulary
        "insecure_no_verify",
        "pinned_ca",
        "encrypted_no_verify",
    ):
        assert marker not in source, marker


def test_frontend_reads_the_backend_control_verdict():
    """The browser's control state is fed by backend fields, not re-derived."""

    source = ADMIN_JS.read_text()
    for field in (
        "output_control_supported",
        "control_broker_sources",
        "control_block_reason",
    ):
        assert field in source, field


# --- physical identity and connection planning -------------------------------
def test_physical_identity_has_one_core_owner():
    """Only Core resolves or compares physical inverter identity."""

    import ems.device_identity as identity

    assert identity.resolve_physical_identity is not None
    offenders = [
        path.name
        for path in _admin_modules()
        # Re-implementing the mask vocabulary or the evidence ranking beside
        # Core is the regression this guards: both were duplicated before.
        if '_MASK_MARKERS = ("•", "…")' in path.read_text()
        or "EVIDENCE_PRECEDENCE = (" in path.read_text()
    ]
    assert offenders == []


def test_replacement_eligibility_has_one_planner():
    """No Admin module decides same-device/replacement beside the planner."""

    offenders = [
        path.name
        for path in _admin_modules(exclude=("connection_planner.py",))
        if "same_physical_inverter_evidence" in path.read_text()
    ]
    assert offenders == []


def test_maintenance_reaches_the_planner_rather_than_its_own_rule():
    import admin.maintenance_config as maintenance

    source = pathlib.Path(maintenance.__file__).read_text()
    assert "from admin.connection_planner import" in source
    assert "plan_connection_change(" in source


def test_setup_reaches_the_planner_rather_than_its_own_rule():
    import admin.setup_planner as setup

    source = pathlib.Path(setup.__file__).read_text()
    assert "from admin.connection_planner import" in source
    assert "plan_connection_change(" in source
    # Even the grouping asks the planner; nothing here compares evidence itself.
    assert "compare_physical_identity" not in source


def test_setup_and_maintenance_adapt_the_same_planner():
    import admin.maintenance_config as maintenance
    import admin.setup_planner as setup

    for module in (maintenance, setup):
        source = pathlib.Path(module.__file__).read_text()
        assert "INTENT_SWITCH_CONNECTION" in source, module.__name__


def test_the_browser_cannot_mint_an_issued_id():
    """admin.js may test and compare issued ids; it may never construct one."""

    source = ADMIN_JS.read_text()
    for prefix in ("obs:v1:", "conn:v1:", "opaque:v1:", "plan:v1:"):
        for minting in (f'"{prefix}" +', f"'{prefix}' +", f'"{prefix}" +'):
            assert minting not in source, f"admin.js concatenates a {prefix} id"


def test_setup_state_is_rehydrated_through_the_backend():
    """Legacy Setup stores are resolved by the planner, not re-matched locally."""

    source = ADMIN_JS.read_text()
    assert '"/api/setup/device-plan"' in source
    for adopted in (
        "function adoptPlannedIdentityState(",
        "function applySetupPlanOperations(",
        "legacyPhysicalDismissals",
    ):
        assert adopted in source, adopted


def test_browser_ids_are_issued_by_one_admin_module():
    """observation/connection/physical ids come from one stamper."""

    import admin.observation_identity as observation_identity

    assert observation_identity.OBSERVATION_ID_FIELD == "observation_id"
    offenders = [
        path.name
        for path in _admin_modules(exclude=("observation_identity.py",))
        if "opaque_observation_id" in path.read_text()
    ]
    assert offenders == []


def test_frontend_carries_no_physical_identity_decision_table():
    """The browser projects server-issued identity; it never derives one.

    Pins ownership, not formatting: the removed helpers are named, and the
    collection key must not be reachable from a displayed hardware field.
    """

    source = ADMIN_JS.read_text()
    for removed in ("function deviceKey(", "function discoveryDeviceMatch("):
        assert removed not in source, removed
    assert "function observationKey(" in source
    assert "function hasObservationIdentity(" in source


def test_browser_collection_key_reads_only_the_issued_observation_id():
    """observationKey must not consult a serial to decide equality."""

    source = ADMIN_JS.read_text()
    start = source.index("function observationKey(")
    body = source[start : source.index("\nfunction ", start)]
    assert "observation_id" in body
    for hardware_field in ("serial_number", "sn", "product_key", "physical_identity_token"):
        assert hardware_field not in body, hardware_field


def test_setup_apply_revalidates_physical_identity_server_side():
    """A plan the browser applied is never the reason a config is accepted."""

    import admin.config_preview as preview

    source = pathlib.Path(preview.__file__).read_text()
    assert "find_duplicate_zendure_device_identities(" in source
    assert "zendure_device_identity_duplicate" in source
