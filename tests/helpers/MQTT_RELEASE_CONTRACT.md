# MQTT Release Contract

This document is the map for the MQTT release contract: the deterministic,
explicit description of the complete lifecycle from Admin discovery to EMS
runtime. It records the baseline, the boundaries the contract must make
observable, the existing coverage that already guards each boundary, and the
new unified release-contract tests that walk the whole lifecycle end to end.

## Why this exists

The MQTT feature crosses many system boundaries:

```
Admin credential input
  -> secure credential storage
  -> broker discovery (generations, TTL, per-broker validity)
  -> device proposal (server-side trust boundary)
  -> preview (secret-free, side-effect-free)
  -> apply (transactional credential promotion + config write)
  -> config.json (credentials_ref only, never secrets)
  -> Core credential resolution (no admin import)
  -> MQTT connection (per connection profile)
  -> telemetry -> DeviceState -> controller
  -> transport-specific write publish
  -> diagnostics / status
  -> cleanup
```

Historically the defects lived *between* these boundaries, not inside the MQTT
parser. The release contract makes each boundary observable in one place.

## Core architecture invariants (asserted, not assumed)

- EMS Core is the source of truth; Admin is UI and orchestration.
- EMS/Core must never import Admin (`ems.mqtt_credentials` reads the secret
  store directly, no `admin` dependency).
- Secrets stay outside `config.json`; only a non-secret `credentials_ref` is
  ever persisted.

## Baseline (recorded 2026-07-11, branch `feature/zendure-mqtt-device-support`)

- `ruff check .` — clean.
- `python -m compileall -q ems admin tests` — clean.
- `node --check admin/static/admin.js` — clean.
- `git diff --check` — clean.
- Full non-Docker suite: **3645 passed, 3 skipped, 3 failed**.
  - The 3 failures are the known **environmental Docker** cases, not
    regressions: `test_docker_first_e2e.py::test_ems_only_quickstart`,
    `test_docker_first_e2e.py::test_analytics_quickstart` (host port 8080
    already allocated in the sandbox) and
    `test_docker_first_setup.py::test_rendered_compose_validates_with_docker`
    (missing generated `config/influxdb.env`). Both are documented as
    pre-existing sandbox limitations, unrelated to MQTT.
- Curated MQTT-focused sets (192 tests) all green.

Many production-boundary fixes named in the task already landed as earlier
commits on this branch (strict enable-flag parsing, per-broker generation
validity, transactional credential promotion, strict legacy broker input,
connection-identity/error cleanup, Core credential resolution). The release
contract's job now is to encode those boundaries as one explicit, named,
lifecycle-crossing contract and guard them against regression.

### Boundary defects the unified contract newly surfaced (and fixed)

Building the single setup-to-runtime contract exposed two boundary defects that
the previously scattered tests missed — both between the Admin preview stage and
the config it emits:

1. **One broker profile per authenticated endpoint.** Config preview compared a
   proposal endpoint against existing broker profiles using only
   `(source, host, port, tls)`, while the profile it provisions and
   `normalized_broker_identity` also key on `credentials_ref` and `tls_insecure`.
   A D0 grid meter and a control device discovered on the *same authenticated*
   broker each minted a separate collision-suffixed profile. Fixed by including
   `credentials_ref`/`tls_insecure` in the compared endpoint profile
   (`fix(admin): unify one broker profile per authenticated endpoint`). The
   golden path asserts a single shared profile; multi-profile tests confirm two
   *different* credentials still resolve to distinct profiles.

2. **Idempotent repeated setup apply.** Setup apply uses the existing install
   config as the preview base, so re-selecting the same discovered MQTT device
   re-appended it and failed duplicate-identity validation (HTTP 422). Fixed by
   skipping a proposal whose physical device is already declared as a Zendure
   MQTT device in the base
   (`fix(admin): make repeated setup apply of an mqtt proposal idempotent`). A
   genuine conflict still blocks: an existing HTTP device sharing a serial, or
   two distinct proposals in one apply.

## Test tiers

- **fast / in-process** — no real sockets, no Docker, no sleeps. Fake MQTT and
  HTTP transports plus an injected clock. These are the release-contract tests.
- **docker / real Mosquitto** — real broker socket boundary only
  (`eclipse-mosquitto:2`), auto-skipped when Docker is unavailable.

## New unified release-contract deliverables

| File | Tier | Phase |
| --- | --- | --- |
| `tests/helpers/mqtt_release_contract.py` | fast | harness |
| `tests/test_mqtt_release_contract_harness.py` | fast | harness self-tests |
| `tests/test_mqtt_release_contract_golden_path.py` | fast | golden path |
| `tests/test_mqtt_release_contract_failures.py` | fast | failure/rollback |
| `tests/test_mqtt_release_contract_multi_profile.py` | fast | multi-broker/credential |
| `tests/test_mqtt_release_contract_validation.py` | fast | strict input / trust boundary |

The harness composes **real production classes** — `MqttBrokerStore`,
`MqttBrokerDiscovery`, `CredentialStore`, `ConfigPreviewGenerator`,
`ConfigExportService`, `ConfigApplyService`, `build_proposals` /
`resolve_selected_proposals`, `FileMqttCredentialResolver`,
`build_zendure_mqtt_control_runtime`, `build_zendure_mqtt_runtime`,
`create_grid_meter_client`, and the real controller cycle. Only true external
boundaries are faked: the MQTT socket (`tests/helpers/fake_mqtt.py`), the HTTP
socket (recording `Mock` sessions), the clock, and filesystem location (tmp
dirs). Business logic — proposal validation, preview generation, apply
semantics, promotion, config parsing, credential resolution, DeviceState
conversion, allocation, write-gate evaluation, cleanup — is never duplicated in
the helper.

### Boundary coverage matrix

| Boundary | New release-contract test(s) | Existing tests that already guard it |
| --- | --- | --- |
| Discovery credential saved securely, absent from public metadata | golden_path step 1 | `test_admin_mqtt_credential_promotion_transaction.py` |
| Strict broker port/bool/TLS validation at save | validation | `test_mqtt_port_validation.py`, `test_mqtt_tls_and_bool_helpers.py`, `test_mqtt_strict_enable_flags.py` |
| Discovery generation, per-broker validity, TTL | multi_profile | `test_admin_mqtt_discovery_generations.py`, `test_admin_mqtt_multi_broker_refresh.py` |
| Proposal references authenticated connection profile; D0 canonical topic | golden_path steps 3-4 | `test_mqtt_admin_runtime_contract.py`, `test_admin_zendure_mqtt_config_proposals.py` |
| Preview secret-free + no side effects | golden_path step 4, failures 3.5 | `test_admin_config_preview_mqtt_metadata.py`, promotion-transaction preview test |
| Apply transactional (promotion + config write) | golden_path step 5, failures 3.1-3.4 | `test_admin_mqtt_credential_promotion_transaction.py` |
| config.json carries credentials_ref only | golden_path step 5 | promotion-transaction write/apply tests |
| Core resolves credentials_ref without admin import | golden_path step 6, failures 3.6-3.8 | `test_mqtt_credential_resolver.py` |
| Grid-meter + legacy telemetry -> DeviceState -> controller -> publish | golden_path steps 7-9 | `test_mqtt_internal_end_to_end.py`, `test_mqtt_d0_grid_meter_end_to_end.py` |
| Publish uses the device's own connection profile, isolated per broker | golden_path step 9, multi_profile | `test_ems_zendure_mqtt_multi_broker.py`, `test_mqtt_mixed_transport_scenarios.py` |
| Same endpoint, two credential profiles stay isolated | multi_profile 4.8-4.10 | `test_ems_zendure_mqtt_shared_services.py` |
| Forged browser proposal fields rejected | validation 5.5 | `test_admin_zendure_mqtt_config_proposals.py` (`resolve_trusted_proposal`) |
| Secret redaction across all public artifacts | golden_path step 10, validation 5.7 | scattered per-test `assert SECRET not in ...` |
| Cleanup idempotent, one stop per service | golden_path step 11 | `test_mqtt_admin_runtime_contract.py` lifecycle tests |

### Real Mosquitto gates (docker tier)

Verified green against real `eclipse-mosquitto:2` (12 tests, ~44s):

| File | Real boundary proven |
| --- | --- |
| `tests/test_mqtt_real_mosquitto.py` | credentials_ref authenticated connection via the production grid-meter factory; wrong/unknown credential fails without leak; two brokers isolated |
| `tests/test_mqtt_real_mosquitto_acl.py` | two ACL users on one endpoint, read/write isolation per credentials_ref |
| `tests/test_mqtt_real_mosquitto_tls.py` | verified CA succeeds; untrusted cert rejected; insecure_no_verify only when configured |
| `tests/test_mqtt_real_legacy_flow.py` | supported legacy telemetry -> control publish reaches a real subscriber |

Run with `python -m pytest -q -m docker tests/test_mqtt_real_*.py`; auto-skipped
when Docker is unavailable.

## Support level (do not conflate)

- **Fake transport** — every fast release-contract test above. Proves the
  in-process lifecycle and boundary contracts.
- **Real local Mosquitto** — the docker-tier gates. Proves the real network,
  auth, ACL and TLS boundary.
- **Real Zendure hardware** — *not validated in this contract.* The legacy
  telemetry/control protocol is exercised against a real broker but not against
  a physical Zendure device; treat real-hardware control as experimental.
