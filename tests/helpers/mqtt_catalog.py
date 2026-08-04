# SPDX-License-Identifier: AGPL-3.0-or-later
"""Curated critical-pair scenario catalog for the MQTT test matrix.

Hand-authored rather than randomly generated: every entry is stable and named so
a failing matrix case is readable in pytest output. This is *curated critical-pair*
coverage, not exhaustive all-factor pairwise coverage — it guarantees every
required single value plus the explicitly enumerated critical pairs in
:data:`REQUIRED_PAIRS`, which :mod:`tests.test_mqtt_combination_matrix` enforces.
Growing coverage means adding a named scenario (and, for a new critical pair, a
predicate in ``REQUIRED_PAIRS``).
"""

from tests.helpers.mqtt_scenarios import (
    FAILURE_BROKER_CONNECT,
    FAILURE_HTTP_TIMEOUT,
    FAILURE_NONE,
    FAILURE_PUBLISH,
    GATE_ALL_DISABLED,
    GATE_ALL_ENABLED,
    GATE_API_ONLY,
    GATE_CLOUD_ONLY,
    GATE_LOCAL_ONLY,
    PAYLOAD_CLOUD_SCALAR,
    PAYLOAD_HTTP_ZENSDK,
    PAYLOAD_LEGACY_JSON,
    PAYLOAD_LEGACY_JSON_ALT,
    PAYLOAD_ZENSDK_HA_SCALAR,
    ROLE_INVERTER_CONTROL,
    ROLE_INVERTER_TELEMETRY_ONLY,
    SECURITY_ANONYMOUS_PLAIN,
    SECURITY_AUTHENTICATED_PLAIN,
    SECURITY_TLS_INSECURE,
    SECURITY_TLS_VERIFIED,
    STATE_FRESH,
    STATE_MALFORMED,
    STATE_STALE,
    STATE_UNSEEN,
    TRANSPORT_API_HTTP,
    TRANSPORT_CLOUD_MQTT,
    TRANSPORT_GRID_METER_HTTP,
    TRANSPORT_GRID_METER_MQTT,
    TRANSPORT_LOCAL_MQTT_A,
    TRANSPORT_LOCAL_MQTT_B,
    BrokerSpec,
    DeviceSpec,
    GridMeterSpec,
    Scenario,
    ScenarioExpectations,
)

# --- reusable broker profiles -----------------------------------------------
LOCAL_A = BrokerSpec(ref="local_a", source="local_mqtt", host="10.0.0.10", port=1883)
LOCAL_B = BrokerSpec(ref="local_b", source="local_mqtt", host="10.0.0.20", port=1883)
AUTH_LOCAL = BrokerSpec(
    ref="auth_local", source="local_mqtt", host="10.0.0.11", port=1883,
    username="mqttuser", password="mqttpass-SECRET",
)
TLS_LOCAL = BrokerSpec(
    ref="tls_local", source="local_mqtt", host="10.0.0.12", port=8883, tls=True,
)
TLS_INSECURE_LOCAL = BrokerSpec(
    ref="tls_insecure_local", source="local_mqtt", host="10.0.0.13", port=8883,
    tls=True, tls_insecure=True,
)
CLOUD = BrokerSpec(
    ref="cloud", source="zendure_cloud_mqtt", host="mqtt.zendure.example",
    port=8883, tls=True, credentials_ref="cloud-token-ref",
)

HTTP_METER = GridMeterSpec(meter_type="shelly", transport=TRANSPORT_GRID_METER_HTTP)


def _d0(ref, serial):
    return GridMeterSpec(
        meter_type="zendure_smartmeter_d0",
        transport=TRANSPORT_GRID_METER_MQTT,
        broker_ref=ref,
        serial=serial,
        topic=f"Zendure/sensor/{serial}/totalPower",
        power_w=2000.0,
    )


def _generic_mqtt(ref):
    return GridMeterSpec(
        meter_type="mqtt",
        transport=TRANSPORT_GRID_METER_MQTT,
        broker_ref=ref,
        topic="grid/power",
        power_w=1500.0,
    )


def _api(name="API", serial=None):
    return DeviceSpec(
        name, ROLE_INVERTER_CONTROL, TRANSPORT_API_HTTP, PAYLOAD_HTTP_ZENSDK,
        serial=serial or name,
    )


def _legacy(name, ref, transport, *, alt=False, control=True, state=STATE_FRESH):
    family = PAYLOAD_LEGACY_JSON_ALT if alt else PAYLOAD_LEGACY_JSON
    return DeviceSpec(
        name, ROLE_INVERTER_CONTROL if control else ROLE_INVERTER_TELEMETRY_ONLY,
        transport, family, broker_ref=ref,
        device_id=f"DEV{name}", product_key=f"PK{name}",
        control_enabled=control, state=state,
    )


def _scalar(name, ref, transport, family=PAYLOAD_ZENSDK_HA_SCALAR, state=STATE_FRESH):
    return DeviceSpec(
        name, ROLE_INVERTER_TELEMETRY_ONLY, transport, family,
        broker_ref=ref, serial=f"SN{name}", state=state,
    )


def _cloud_control(name="Cloud"):
    return DeviceSpec(
        name, ROLE_INVERTER_CONTROL, TRANSPORT_CLOUD_MQTT, PAYLOAD_LEGACY_JSON,
        broker_ref="cloud", device_id=f"DEV{name}", product_key=f"PK{name}",
        control_enabled=True,
    )


# --- the curated catalog ----------------------------------------------------
CATALOG: tuple[Scenario, ...] = (
    Scenario(
        name="empty_install",
        grid_meter=HTTP_METER,
        gate_state=GATE_ALL_ENABLED,
        expectations=ScenarioExpectations(active_devices=0),
    ),
    Scenario(
        name="single_api",
        devices=(_api(),),
        grid_meter=HTTP_METER,
        gate_state=GATE_API_ONLY,
        expectations=ScenarioExpectations(active_devices=1),
    ),
    Scenario(
        name="api_plus_local_a",
        brokers=(LOCAL_A,),
        devices=(_api(), _legacy("LA", "local_a", TRANSPORT_LOCAL_MQTT_A)),
        grid_meter=HTTP_METER,
        gate_state=GATE_ALL_ENABLED,
        expectations=ScenarioExpectations(active_devices=2, control_devices=1, broker_services=1),
    ),
    Scenario(
        name="api_two_local_brokers",
        brokers=(LOCAL_A, LOCAL_B),
        devices=(
            _api(),
            _legacy("LA", "local_a", TRANSPORT_LOCAL_MQTT_A),
            _legacy("LB", "local_b", TRANSPORT_LOCAL_MQTT_B, alt=True),
        ),
        grid_meter=HTTP_METER,
        gate_state=GATE_ALL_ENABLED,
        expectations=ScenarioExpectations(active_devices=3, control_devices=2, broker_services=2),
    ),
    Scenario(
        name="api_local_cloud_quad",
        brokers=(LOCAL_A, LOCAL_B, CLOUD),
        devices=(
            _api(),
            _legacy("LA", "local_a", TRANSPORT_LOCAL_MQTT_A),
            _legacy("LB", "local_b", TRANSPORT_LOCAL_MQTT_B),
            _cloud_control(),
        ),
        grid_meter=HTTP_METER,
        gate_state=GATE_ALL_ENABLED,
        expectations=ScenarioExpectations(active_devices=4, control_devices=3, broker_services=3),
    ),
    Scenario(
        name="d0_local_a_with_api",
        brokers=(LOCAL_A,),
        devices=(_api(),),
        grid_meter=_d0("local_a", "D0A"),
        gate_state=GATE_API_ONLY,
        expectations=ScenarioExpectations(active_devices=1),
    ),
    Scenario(
        name="d0_broker_b_legacy_broker_a",
        brokers=(LOCAL_A, LOCAL_B),
        devices=(_legacy("LA", "local_a", TRANSPORT_LOCAL_MQTT_A),),
        grid_meter=_d0("local_b", "D0B"),
        gate_state=GATE_LOCAL_ONLY,
        expectations=ScenarioExpectations(active_devices=1, control_devices=1, broker_services=1),
    ),
    Scenario(
        name="generic_mqtt_meter_with_local",
        brokers=(LOCAL_A,),
        devices=(_legacy("LA", "local_a", TRANSPORT_LOCAL_MQTT_A),),
        grid_meter=_generic_mqtt("local_a"),
        gate_state=GATE_LOCAL_ONLY,
        expectations=ScenarioExpectations(active_devices=1, control_devices=1, broker_services=1),
    ),
    Scenario(
        name="scalar_readonly_plus_legacy_control",
        brokers=(LOCAL_A,),
        devices=(
            _scalar("Scal", "local_a", TRANSPORT_LOCAL_MQTT_A),
            _legacy("LA", "local_a", TRANSPORT_LOCAL_MQTT_A),
        ),
        grid_meter=HTTP_METER,
        gate_state=GATE_LOCAL_ONLY,
        expectations=ScenarioExpectations(active_devices=1, control_devices=1, broker_services=1, telemetry_only_devices=1),
    ),
    Scenario(
        name="cloud_scalar_readonly",
        brokers=(CLOUD,),
        devices=(_scalar("CScal", "cloud", TRANSPORT_CLOUD_MQTT, PAYLOAD_CLOUD_SCALAR),),
        grid_meter=HTTP_METER,
        gate_state=GATE_CLOUD_ONLY,
        expectations=ScenarioExpectations(active_devices=0, telemetry_only_devices=1),
    ),
    Scenario(
        name="tls_and_authenticated_brokers",
        brokers=(TLS_LOCAL, AUTH_LOCAL),
        devices=(
            _legacy("Tls", "tls_local", TRANSPORT_LOCAL_MQTT_A),
            _legacy("Auth", "auth_local", TRANSPORT_LOCAL_MQTT_B),
        ),
        grid_meter=HTTP_METER,
        gate_state=GATE_LOCAL_ONLY,
        expectations=ScenarioExpectations(active_devices=2, control_devices=2, broker_services=2),
    ),
    Scenario(
        name="tls_insecure_broker",
        brokers=(TLS_INSECURE_LOCAL,),
        devices=(_legacy("Insec", "tls_insecure_local", TRANSPORT_LOCAL_MQTT_A),),
        grid_meter=HTTP_METER,
        gate_state=GATE_LOCAL_ONLY,
        expectations=ScenarioExpectations(active_devices=1, control_devices=1, broker_services=1),
    ),
    Scenario(
        name="stale_mqtt_with_healthy_http",
        brokers=(LOCAL_A,),
        devices=(
            _api(),
            _legacy("LA", "local_a", TRANSPORT_LOCAL_MQTT_A, state=STATE_STALE),
        ),
        grid_meter=HTTP_METER,
        gate_state=GATE_ALL_ENABLED,
        expectations=ScenarioExpectations(active_devices=2, control_devices=1, broker_services=1),
    ),
    Scenario(
        name="unseen_mqtt_device",
        brokers=(LOCAL_A,),
        devices=(_legacy("LA", "local_a", TRANSPORT_LOCAL_MQTT_A, state=STATE_UNSEEN),),
        grid_meter=HTTP_METER,
        gate_state=GATE_LOCAL_ONLY,
        expectations=ScenarioExpectations(active_devices=1, control_devices=1, broker_services=1),
    ),
    Scenario(
        name="malformed_telemetry_device",
        brokers=(LOCAL_A,),
        devices=(_legacy("LA", "local_a", TRANSPORT_LOCAL_MQTT_A, state=STATE_MALFORMED),),
        grid_meter=HTTP_METER,
        gate_state=GATE_LOCAL_ONLY,
        expectations=ScenarioExpectations(active_devices=1, control_devices=1, broker_services=1),
    ),
    Scenario(
        name="broker_outage_local_a",
        brokers=(LOCAL_A,),
        devices=(_api(), _legacy("LA", "local_a", TRANSPORT_LOCAL_MQTT_A)),
        grid_meter=HTTP_METER,
        gate_state=GATE_ALL_ENABLED,
        failure_mode=FAILURE_BROKER_CONNECT,
        expectations=ScenarioExpectations(active_devices=2, control_devices=1, broker_services=1),
    ),
    Scenario(
        name="publish_failure_local_a",
        brokers=(LOCAL_A,),
        devices=(_legacy("LA", "local_a", TRANSPORT_LOCAL_MQTT_A),),
        grid_meter=HTTP_METER,
        gate_state=GATE_LOCAL_ONLY,
        failure_mode=FAILURE_PUBLISH,
        expectations=ScenarioExpectations(active_devices=1, control_devices=1, broker_services=1),
    ),
    Scenario(
        name="grid_meter_http_timeout",
        brokers=(LOCAL_A,),
        devices=(_legacy("LA", "local_a", TRANSPORT_LOCAL_MQTT_A),),
        grid_meter=HTTP_METER,
        gate_state=GATE_LOCAL_ONLY,
        failure_mode=FAILURE_HTTP_TIMEOUT,
        expectations=ScenarioExpectations(active_devices=1, control_devices=1, broker_services=1),
    ),
    Scenario(
        name="all_gates_disabled",
        brokers=(LOCAL_A, CLOUD),
        devices=(
            _api(),
            _legacy("LA", "local_a", TRANSPORT_LOCAL_MQTT_A),
            _cloud_control(),
        ),
        grid_meter=HTTP_METER,
        gate_state=GATE_ALL_DISABLED,
        expectations=ScenarioExpectations(active_devices=3, control_devices=2, broker_services=2),
    ),
    Scenario(
        name="api_cloud_only_gate",
        brokers=(CLOUD,),
        devices=(_api(), _cloud_control()),
        grid_meter=HTTP_METER,
        gate_state=GATE_CLOUD_ONLY,
        expectations=ScenarioExpectations(active_devices=2, control_devices=1, broker_services=1),
    ),
    Scenario(
        name="eight_device_scaling",
        brokers=(LOCAL_A, LOCAL_B, CLOUD),
        devices=(
            _api("API1", "SNA1"),
            _api("API2", "SNA2"),
            _legacy("LA1", "local_a", TRANSPORT_LOCAL_MQTT_A),
            _legacy("LA2", "local_a", TRANSPORT_LOCAL_MQTT_A),
            _legacy("LB1", "local_b", TRANSPORT_LOCAL_MQTT_B, alt=True),
            _legacy("LB2", "local_b", TRANSPORT_LOCAL_MQTT_B, alt=True),
            _cloud_control("Cloud1"),
            _cloud_control("Cloud2"),
        ),
        grid_meter=HTTP_METER,
        gate_state=GATE_ALL_ENABLED,
        expectations=ScenarioExpectations(active_devices=8, control_devices=6, broker_services=3),
    ),
)

CATALOG_BY_NAME = {scenario.name: scenario for scenario in CATALOG}


# --- required coverage the guard enforces -----------------------------------
REQUIRED_DEVICE_COUNTS = frozenset({0, 1, 2, 3, 4, 8})
REQUIRED_DEVICE_TRANSPORTS = frozenset(
    {TRANSPORT_API_HTTP, TRANSPORT_LOCAL_MQTT_A, TRANSPORT_LOCAL_MQTT_B, TRANSPORT_CLOUD_MQTT}
)
REQUIRED_PAYLOAD_FAMILIES = frozenset(
    {
        PAYLOAD_HTTP_ZENSDK,
        PAYLOAD_ZENSDK_HA_SCALAR,
        PAYLOAD_LEGACY_JSON,
        PAYLOAD_LEGACY_JSON_ALT,
        PAYLOAD_CLOUD_SCALAR,
    }
)
REQUIRED_GRID_METERS = frozenset({"shelly", "zendure_smartmeter_d0", "mqtt"})
REQUIRED_SECURITIES = frozenset(
    {
        SECURITY_ANONYMOUS_PLAIN,
        SECURITY_AUTHENTICATED_PLAIN,
        SECURITY_TLS_VERIFIED,
        SECURITY_TLS_INSECURE,
    }
)
REQUIRED_GATE_STATES = frozenset(
    {GATE_API_ONLY, GATE_LOCAL_ONLY, GATE_CLOUD_ONLY, GATE_ALL_ENABLED, GATE_ALL_DISABLED}
)
REQUIRED_TELEMETRY_STATES = frozenset(
    {STATE_FRESH, STATE_STALE, STATE_UNSEEN, STATE_MALFORMED}
)
REQUIRED_FAILURE_MODES = frozenset(
    {FAILURE_NONE, FAILURE_BROKER_CONNECT, FAILURE_PUBLISH, FAILURE_HTTP_TIMEOUT}
)

# Required pairs, expressed as predicates over a scenario so the labels stay
# readable in a failure message.
REQUIRED_PAIRS = {
    "local_a + local_b": lambda s: {TRANSPORT_LOCAL_MQTT_A, TRANSPORT_LOCAL_MQTT_B}
    <= s.device_transports,
    "local_mqtt + cloud_mqtt": lambda s: bool(
        s.device_transports & {TRANSPORT_LOCAL_MQTT_A, TRANSPORT_LOCAL_MQTT_B}
    )
    and TRANSPORT_CLOUD_MQTT in s.device_transports,
    "api + local_mqtt": lambda s: TRANSPORT_API_HTTP in s.device_transports
    and bool(s.device_transports & {TRANSPORT_LOCAL_MQTT_A, TRANSPORT_LOCAL_MQTT_B}),
    "api + cloud_mqtt": lambda s: TRANSPORT_API_HTTP in s.device_transports
    and TRANSPORT_CLOUD_MQTT in s.device_transports,
    "d0_meter + api_inverter": lambda s: s.grid_meter.meter_type
    == "zendure_smartmeter_d0"
    and TRANSPORT_API_HTTP in s.device_transports,
    "d0_broker_b + legacy_broker_a": lambda s: s.grid_meter.meter_type
    == "zendure_smartmeter_d0"
    and s.grid_meter.broker_ref == "local_b"
    and any(
        d.broker_ref == "local_a" and d.payload_family == PAYLOAD_LEGACY_JSON
        for d in s.devices
    ),
    "scalar_readonly + legacy_control": lambda s: any(
        d.payload_family == PAYLOAD_ZENSDK_HA_SCALAR for d in s.devices
    )
    and any(d.control_enabled and d.payload_family == PAYLOAD_LEGACY_JSON for d in s.devices),
    "tls_broker + authenticated_broker": lambda s: SECURITY_TLS_VERIFIED
    in s.broker_securities
    and SECURITY_AUTHENTICATED_PLAIN in s.broker_securities,
    "stale_mqtt + healthy_http": lambda s: any(
        d.is_mqtt and d.state == STATE_STALE for d in s.devices
    )
    and any(d.transport == TRANSPORT_API_HTTP for d in s.devices),
}
