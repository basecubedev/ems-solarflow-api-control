# Mixed-hardware / multi-transport MQTT test matrix

This note maps the mixed-transport MQTT test suite to the coverage it provides.
The scenario model keeps **transport**, **payload family** and **device role**
strictly separate (see `mqtt_scenarios.py`) so no test can encode an incorrect
assumption such as "all cloud devices share one payload shape" or "all local MQTT
devices are writable".

## Coverage claim (curated critical-pair, not full pairwise)

The catalog is **curated critical-pair** coverage: it guarantees every required
single value (device count, transport, payload family, grid meter, broker
security, write-gate state, telemetry state, failure mode) **plus** the explicit
critical pairs enumerated in `mqtt_catalog.REQUIRED_PAIRS`. It is deliberately
*not* an exhaustive all-factor pairwise product — that would be opaque and mostly
redundant. `test_catalog_covers_every_required_pair` fails if a declared critical
pair is dropped; adding a new critical pair means adding a predicate there.

Every catalog entry also declares a `ScenarioExpectations` (active / control /
telemetry-only device counts, broker-service count, grid meters, rejected
entries) that `test_catalog_entry_meets_declared_expectations` enforces against
the built installation, and a meta-test proves a wrong expectation fails rather
than passing silently.

## Shared infrastructure

| File | Responsibility |
| --- | --- |
| `tests/helpers/fake_mqtt.py` | One reusable fake broker/client harness serving both the Zendure clients and `MqttGridMeterClient`; records every publish tagged with its broker `ref`; opt-in connect/publish failure modes. |
| `tests/helpers/payloads.py` | Canonical, anonymized payloads per family (scalar, cloud scalar, legacy JSON, legacy alt, D0, malformed/partial/foreign). No real secrets. |
| `tests/helpers/mqtt_scenarios.py` | `BrokerSpec` / `DeviceSpec` / `GridMeterSpec` / `Scenario` / `ScenarioExpectations` immutable specs, `build_config`, compatibility rules, `build_installation` (wires the real production builders to the fake network) and `assert_installation_matches`. |
| `tests/helpers/mqtt_catalog.py` | The curated critical-pair scenario catalog + the required-coverage declarations. |
| `tests/helpers/controller.py` | `run_control_cycle` (synthetic-state allocation) and `run_installation_cycle` (true production-fetch path, no `fetch_all_devices` patch); `patch_snapshot_clock` for deterministic staleness. |
| `tests/helpers/fake_mqtt.py` | Fake broker/client harness (above); `FakeClock` for injected-clock staleness. |
| `tests/helpers/mosquitto.py` | Ephemeral Mosquitto container helper for the optional real-broker tests. |

## Test files

| File | Layer |
| --- | --- |
| `test_mqtt_combination_matrix.py` | Layer B — curated critical-pair matrix + coverage guard (fails if a required value/pair is dropped) + per-entry `ScenarioExpectations` enforcement and its meta-test. |
| `test_mqtt_focused_parameterized.py` | Layer A — device counts, per-family parsing, read-only vs writable, broker profiles, duplicate identity, write gates × safety modes. |
| `test_mqtt_mixed_transport_scenarios.py` | Layer C — named scenarios 01–12 over the real runtimes (synthetic-state allocation). |
| `test_mqtt_internal_end_to_end.py` | Layer C-E2E — cases A–F: MQTT payload → snapshot → **production fetch** → DeviceState → controller → transport write, with **no `fetch_all_devices` patch**. |
| `test_mqtt_stale_unseen_states.py` | Fresh / stale / unseen / recovery as distinct runtime states via an injected `FakeClock` (no real sleep); stale/unseen get no unsafe write; stale-vs-unseen D0 grid meter. |
| `test_mqtt_degraded_scenarios.py` | Broker outage, cloud offline, stale/unseen telemetry, grid-meter failure, publish failure, hostile messages. |
| `test_mqtt_admin_runtime_contract.py` | Admin discovery → generated config → EMS Core resolver, plus runtime lifecycle (start once / reuse shared / idempotent stop / grid-meter close). |
| `test_mqtt_real_mosquitto.py` | Optional `docker`-marked real-broker subscription/auth/isolation. |

## Existing coverage retained (not duplicated)

`test_ems_zendure_mqtt_*` (topics, payloads, config entries/mapping, runtime,
control runtime, telemetry, stale, shared services, multi-broker), the existing
`test_mqtt_end_to_end.py`, `test_mqtt_d0_grid_meter_end_to_end.py`,
`test_control_write_gates.py`, `test_admin_*mqtt*` and `test_zendure_d0_topic.py`
remain the authority for their focused units. The new suite adds the *combination*
layer on top and reuses their production entry points rather than re-testing them.

## Unsupported combinations asserted as rejected (never enabled to pass)

- Scalar telemetry family (`zensdk_ha_scalar` / `zendure_cloud_scalar`) + control
  → `write_protocol_unsupported` (never falls back to legacy properties/write).
- Telemetry-only entry requesting `write_output_limit` → `write_output_limit_unsupported`.
- Same physical device across two transports (shared serial) → duplicate identity
  rejection (index-only message, no serial leak).
- D0 / generic MQTT grid meters have no publish path and no write-gate dependency.
- Cloud MQTT grid meter broker_ref → rejected by the Core resolver.

## Dimensions covered

Device counts `0,1,2,3,4,8`; transports `api_http`, two local brokers, cloud MQTT;
payload families `http_zensdk`, `zensdk_ha_scalar`, `legacy_json`, `legacy_json_alt`,
`cloud_scalar`; grid meters `http`, `d0` (broker A and B), `generic_mqtt`; broker
security `anonymous / authenticated / tls_verified / tls_insecure`; write-gate
states `api_only / local_only / cloud_only / all_enabled / all_disabled`; telemetry
states `fresh / stale / unseen / malformed`; failure modes `none / broker_connect /
publish / http_timeout`. Required pairs are enforced by
`test_catalog_covers_every_required_pair`.
