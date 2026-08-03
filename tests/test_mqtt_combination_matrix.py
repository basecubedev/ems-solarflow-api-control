# SPDX-License-Identifier: AGPL-3.0-or-later
"""Curated critical-pair matrix + coverage guard for mixed MQTT installations.

The catalog in :mod:`tests.helpers.mqtt_catalog` is the source of truth for which
hardware/transport/broker combinations the suite exercises. It is *curated
critical-pair* coverage — every required single value plus the explicit critical
pairs in ``REQUIRED_PAIRS`` — not exhaustive all-factor pairwise coverage. This
module proves the catalog still spans every required value and critical pair, that
every entry builds and validates through the real EMS Core resolver, meets its
declared :class:`ScenarioExpectations`, and keeps runtime status credential-free.
Shrinking coverage in a future refactor fails a named guard here rather than
silently passing.
"""

import json

import pytest

import dataclasses

import ems.config as cfg
from tests.helpers import mqtt_catalog as catalog
from tests.helpers.fake_mqtt import FakeMqttNetwork
from tests.helpers.mqtt_scenarios import (
    ScenarioExpectations,
    assert_installation_matches,
    build_config,
    build_installation,
    observe_installation,
    scenario_compatibility_issues,
)

pytestmark = [
    pytest.mark.mqtt,
    pytest.mark.unit,
    pytest.mark.simulation,
    pytest.mark.power_control,
]

_KNOWN_SECRETS = ("mqttpass-SECRET", "cloud-token-ref")


# --- coverage guard: required single values ---------------------------------
def test_catalog_covers_required_device_counts():
    counts = {scenario.device_count for scenario in catalog.CATALOG}
    missing = catalog.REQUIRED_DEVICE_COUNTS - counts
    assert not missing, f"catalog is missing device counts: {sorted(missing)}"


def test_catalog_covers_required_transports():
    transports = set().union(*(s.device_transports for s in catalog.CATALOG))
    missing = catalog.REQUIRED_DEVICE_TRANSPORTS - transports
    assert not missing, f"missing device transports: {sorted(missing)}"


def test_catalog_covers_required_payload_families():
    families = set().union(*(s.payload_families for s in catalog.CATALOG))
    missing = catalog.REQUIRED_PAYLOAD_FAMILIES - families
    assert not missing, f"missing payload families: {sorted(missing)}"


def test_catalog_covers_required_grid_meters():
    meters = {s.grid_meter.meter_type for s in catalog.CATALOG}
    missing = catalog.REQUIRED_GRID_METERS - meters
    assert not missing, f"missing grid meter types: {sorted(missing)}"


def test_catalog_covers_required_broker_securities():
    securities = set().union(
        *(s.broker_securities for s in catalog.CATALOG if s.brokers)
    )
    missing = catalog.REQUIRED_SECURITIES - securities
    assert not missing, f"missing broker securities: {sorted(missing)}"


def test_catalog_covers_required_gate_states():
    states = {s.gate_state for s in catalog.CATALOG}
    missing = catalog.REQUIRED_GATE_STATES - states
    assert not missing, f"missing write-gate states: {sorted(missing)}"


def test_catalog_covers_required_telemetry_states():
    states = set().union(*(s.telemetry_states for s in catalog.CATALOG))
    missing = catalog.REQUIRED_TELEMETRY_STATES - states
    assert not missing, f"missing telemetry states: {sorted(missing)}"


def test_catalog_covers_required_failure_modes():
    modes = {s.failure_mode for s in catalog.CATALOG}
    missing = catalog.REQUIRED_FAILURE_MODES - modes
    assert not missing, f"missing failure modes: {sorted(missing)}"


# --- coverage guard: required pairs -----------------------------------------
def test_catalog_covers_every_required_pair():
    missing = [
        label
        for label, predicate in catalog.REQUIRED_PAIRS.items()
        if not any(predicate(scenario) for scenario in catalog.CATALOG)
    ]
    assert not missing, f"catalog is missing required pairs: {missing}"


def test_required_pairs_report_is_readable():
    # Meta-test: the guard must name each satisfied pair with the scenario that
    # covers it, so a future gap is diagnosable from the output.
    report = {}
    for label, predicate in catalog.REQUIRED_PAIRS.items():
        report[label] = [s.name for s in catalog.CATALOG if predicate(s)]
    assert all(report.values()), report


# --- every catalog entry is a valid, buildable installation -----------------
@pytest.mark.parametrize(
    "scenario", catalog.CATALOG, ids=lambda s: s.name
)
def test_catalog_entry_is_structurally_valid(scenario):
    assert scenario_compatibility_issues(scenario) == []


@pytest.mark.parametrize(
    "scenario", catalog.CATALOG, ids=lambda s: s.name
)
def test_catalog_entry_config_resolves_without_leaking_secrets(scenario):
    config = build_config(scenario)
    # The on-disk grid_meter block never carries a broker secret.
    grid = config.get("grid_meter", {})
    assert "password" not in json.dumps(grid)
    if scenario.grid_meter.meter_type in cfg.MQTT_GRID_METER_TYPES:
        cfg.resolve_grid_meter_mqtt_settings(config)


@pytest.mark.parametrize(
    "scenario", catalog.CATALOG, ids=lambda s: s.name
)
def test_catalog_entry_declares_expectations(scenario):
    # Every catalog entry must declare what it builds so the matrix can enforce
    # it instead of asserting a tautology.
    assert scenario.expectations is not None


@pytest.mark.parametrize(
    "scenario", catalog.CATALOG, ids=lambda s: s.name
)
def test_catalog_entry_meets_declared_expectations(scenario):
    network = FakeMqttNetwork()
    installation = build_installation(scenario, network)
    try:
        assert_installation_matches(installation, scenario.expectations)
    finally:
        installation.stop()


@pytest.mark.parametrize(
    "scenario", catalog.CATALOG, ids=lambda s: s.name
)
def test_catalog_entry_builds_and_status_is_secret_free(scenario):
    network = FakeMqttNetwork()
    installation = build_installation(scenario, network)
    try:
        status = installation.telemetry_runtime.status()
        control_status = installation.control_runtime.status()
        blob = json.dumps({"telemetry": status, "control": control_status})
        for secret in _KNOWN_SECRETS:
            assert secret not in blob, f"secret leaked into status: {secret}"
        # No unexpected control rejections for a valid catalog entry.
        assert installation.control_runtime.rejected == []
    finally:
        installation.stop()


# --- meta-test: wrong expectations must fail --------------------------------
def _build(scenario):
    network = FakeMqttNetwork()
    return network, build_installation(scenario, network)


def test_wrong_active_device_count_is_caught():
    scenario = catalog.CATALOG_BY_NAME["api_plus_local_a"]
    _network, installation = _build(scenario)
    try:
        wrong = dataclasses.replace(scenario.expectations, active_devices=7)
        with pytest.raises(AssertionError):
            assert_installation_matches(installation, wrong)
    finally:
        installation.stop()


def test_wrong_broker_service_count_is_caught():
    scenario = catalog.CATALOG_BY_NAME["api_two_local_brokers"]
    _network, installation = _build(scenario)
    try:
        wrong = dataclasses.replace(scenario.expectations, broker_services=1)
        with pytest.raises(AssertionError):
            assert_installation_matches(installation, wrong)
    finally:
        installation.stop()


def test_wrong_telemetry_only_count_is_caught():
    scenario = catalog.CATALOG_BY_NAME["scalar_readonly_plus_legacy_control"]
    _network, installation = _build(scenario)
    try:
        wrong = dataclasses.replace(scenario.expectations, telemetry_only_devices=0)
        with pytest.raises(AssertionError):
            assert_installation_matches(installation, wrong)
    finally:
        installation.stop()


def test_unexpected_rejection_is_caught():
    # A scenario that silently accepts a device it declared as rejected must fail.
    scenario = catalog.CATALOG_BY_NAME["api_plus_local_a"]
    _network, installation = _build(scenario)
    try:
        wrong = ScenarioExpectations(
            active_devices=2, control_devices=1, broker_services=1,
            rejected_entries=("LA",),
        )
        with pytest.raises(AssertionError):
            assert_installation_matches(installation, wrong)
    finally:
        installation.stop()


def test_correct_expectations_pass_for_every_field():
    scenario = catalog.CATALOG_BY_NAME["scalar_readonly_plus_legacy_control"]
    _network, installation = _build(scenario)
    try:
        observed = observe_installation(installation)
        # Sanity: the observation exposes each enforced field distinctly.
        assert (observed.active_devices, observed.control_devices,
                observed.broker_services, observed.telemetry_only_devices) == (1, 1, 1, 1)
    finally:
        installation.stop()


# --- secrets never surface in refs, reprs, ids, config, diagnostics ---------
_SECRET_PASSWORD = "mqttpass-SECRET"


def test_secret_never_appears_in_scenario_repr():
    for scenario in catalog.CATALOG:
        assert _SECRET_PASSWORD not in repr(scenario)


def test_secret_never_appears_in_pytest_ids():
    # ``ids=lambda s: s.name`` builds every parametrized node id; a secret in one
    # would leak into pytest output.
    for scenario in catalog.CATALOG:
        assert _SECRET_PASSWORD not in scenario.name


def test_secret_never_appears_in_broker_refs():
    # A broker ref is a display/identity token; a secret must never bleed into
    # one (the on-disk broker *profile* legitimately holds inline auth — that is
    # the config, not a leak, and is not what this guards).
    for scenario in catalog.CATALOG:
        config = build_config(scenario)
        if isinstance(config.get("zendure_mqtt"), dict):
            for ref in config["zendure_mqtt"].get("brokers", {}):
                assert _SECRET_PASSWORD not in ref


def test_secret_never_appears_in_status_or_diagnostics():
    scenario = catalog.CATALOG_BY_NAME["tls_and_authenticated_brokers"]
    _network, installation = _build(scenario)
    try:
        blob = json.dumps(
            {
                "telemetry": installation.telemetry_runtime.status(),
                "control": installation.control_runtime.status(),
                "control_devices": [
                    d.describe() for d in installation.control_runtime.devices
                ],
            }
        )
        assert _SECRET_PASSWORD not in blob
    finally:
        installation.stop()
