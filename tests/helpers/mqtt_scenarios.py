# SPDX-License-Identifier: AGPL-3.0-or-later
"""Reusable scenario model for mixed-hardware, multi-transport MQTT tests.

Transport, payload family and device role are kept strictly separate so a
scenario cannot encode an incorrect assumption (all cloud devices share one
payload shape, all local MQTT devices are writable, all scalar topics support
control). :class:`Scenario` is an immutable description; :func:`build_config`
turns it into an EMS ``config.json`` dict, and :func:`build_installation` wires
it to the real production builders with the fake broker network.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import SimpleNamespace
from unittest.mock import Mock

# --- dimension vocabularies -------------------------------------------------
ROLE_INVERTER_CONTROL = "inverter_control"
ROLE_INVERTER_TELEMETRY_ONLY = "inverter_telemetry_only"
ROLE_GRID_METER = "grid_meter"

TRANSPORT_API_HTTP = "api_http"
TRANSPORT_LOCAL_MQTT_A = "local_mqtt_a"
TRANSPORT_LOCAL_MQTT_B = "local_mqtt_b"
TRANSPORT_CLOUD_MQTT = "zendure_cloud_mqtt"
TRANSPORT_GRID_METER_HTTP = "grid_meter_http"
TRANSPORT_GRID_METER_MQTT = "grid_meter_mqtt"

PAYLOAD_HTTP_ZENSDK = "http_zensdk"
PAYLOAD_ZENSDK_HA_SCALAR = "zensdk_ha_scalar"
PAYLOAD_LEGACY_JSON = "legacy_zendure_json"
PAYLOAD_LEGACY_JSON_ALT = "legacy_zendure_json_alt"
PAYLOAD_CLOUD_SCALAR = "zendure_cloud_scalar"
PAYLOAD_MQTT_NUMERIC_GRID = "mqtt_numeric_grid"

GRID_HTTP = "http"
GRID_D0_LOCAL_A = "d0_local_a"
GRID_D0_LOCAL_B = "d0_local_b"
GRID_GENERIC_MQTT = "generic_mqtt"

SECURITY_ANONYMOUS_PLAIN = "anonymous_plain"
SECURITY_AUTHENTICATED_PLAIN = "authenticated_plain"
SECURITY_TLS_VERIFIED = "tls_verified"
SECURITY_TLS_INSECURE = "tls_insecure"

GATE_API_ONLY = "api_only"
GATE_LOCAL_ONLY = "local_only"
GATE_CLOUD_ONLY = "cloud_only"
GATE_ALL_ENABLED = "all_enabled"
GATE_ALL_DISABLED = "all_disabled"

STATE_FRESH = "fresh"
STATE_STALE = "stale"
STATE_UNSEEN = "unseen"
STATE_MALFORMED = "malformed"

FAILURE_NONE = "none"
FAILURE_BROKER_CONNECT = "broker_connect_failure"
FAILURE_PUBLISH = "publish_failure"
FAILURE_HTTP_TIMEOUT = "http_timeout"

# Transport -> the write gate that must be enabled to publish to it.
_TRANSPORT_GATE = {
    TRANSPORT_API_HTTP: "allow_hardware_writes",
    TRANSPORT_LOCAL_MQTT_A: "allow_mqtt_local_control_writes",
    TRANSPORT_LOCAL_MQTT_B: "allow_mqtt_local_control_writes",
    TRANSPORT_CLOUD_MQTT: "allow_mqtt_zendure_control_writes",
}

_MQTT_TRANSPORTS = frozenset(
    {TRANSPORT_LOCAL_MQTT_A, TRANSPORT_LOCAL_MQTT_B, TRANSPORT_CLOUD_MQTT}
)

_GATE_STATE_MAP = {
    GATE_API_ONLY: {
        "allow_hardware_writes": True,
        "allow_mqtt_local_control_writes": False,
        "allow_mqtt_zendure_control_writes": False,
    },
    GATE_LOCAL_ONLY: {
        "allow_hardware_writes": False,
        "allow_mqtt_local_control_writes": True,
        "allow_mqtt_zendure_control_writes": False,
    },
    GATE_CLOUD_ONLY: {
        "allow_hardware_writes": False,
        "allow_mqtt_local_control_writes": False,
        "allow_mqtt_zendure_control_writes": True,
    },
    GATE_ALL_ENABLED: {
        "allow_hardware_writes": True,
        "allow_mqtt_local_control_writes": True,
        "allow_mqtt_zendure_control_writes": True,
    },
    GATE_ALL_DISABLED: {
        "allow_hardware_writes": False,
        "allow_mqtt_local_control_writes": False,
        "allow_mqtt_zendure_control_writes": False,
    },
}


def gate_flags(gate_state: str) -> dict:
    return dict(_GATE_STATE_MAP[gate_state])


# --- immutable scenario specs -----------------------------------------------
@dataclass(frozen=True)
class BrokerSpec:
    ref: str
    source: str = "local_mqtt"
    host: str = "10.0.0.1"
    port: int = 1883
    tls: bool = False
    tls_insecure: bool = False
    username: str | None = None
    # Excluded from repr so a broker password never lands in a scenario repr or a
    # pytest node id built from one.
    password: str | None = field(default=None, repr=False)
    credentials_ref: str | None = None
    enabled: bool = True

    @property
    def security(self) -> str:
        if self.tls and self.tls_insecure:
            return SECURITY_TLS_INSECURE
        if self.tls:
            return SECURITY_TLS_VERIFIED
        if self.username or self.password or self.credentials_ref:
            return SECURITY_AUTHENTICATED_PLAIN
        return SECURITY_ANONYMOUS_PLAIN

    def to_profile(self) -> dict:
        profile = {
            "enabled": self.enabled,
            "source": self.source,
            "host": self.host,
            "port": self.port,
        }
        if self.tls:
            profile["tls"] = True
        if self.tls_insecure:
            profile["tls_insecure"] = True
        if self.username is not None:
            profile["username"] = self.username
        if self.password is not None:
            profile["password"] = self.password
        if self.credentials_ref is not None:
            profile["credentials_ref"] = self.credentials_ref
        return profile


@dataclass(frozen=True)
class DeviceSpec:
    name: str
    role: str
    transport: str
    payload_family: str
    broker_ref: str | None = None
    serial: str | None = None
    device_id: str | None = None
    product_key: str | None = None
    control_enabled: bool = False
    hardware_profile: str | None = None
    max_power: int = 800
    state: str = STATE_FRESH

    @property
    def gate(self) -> str | None:
        return _TRANSPORT_GATE.get(self.transport)

    @property
    def is_mqtt(self) -> bool:
        return self.transport in _MQTT_TRANSPORTS

    def to_config(self) -> dict:
        if self.transport == TRANSPORT_API_HTTP:
            return {
                "name": self.name,
                "ip": "192.0.2.10",
                "sn": self.serial or self.name,
                "max_power": self.max_power,
            }
        mqtt = {
            "broker_ref": self.broker_ref,
            "topic_family": self.payload_family,
            "device_id": self.device_id or self.serial or self.name,
        }
        if self.product_key:
            mqtt["product_key"] = self.product_key
        entry = {
            "type": "zendure_mqtt",
            "name": self.name,
            "mqtt": mqtt,
            "max_power": self.max_power,
        }
        if self.serial:
            entry["serial_number"] = self.serial
        if self.control_enabled:
            entry["capabilities"] = {"write_output_limit": True}
            # A control device must pin a concrete registry hardware profile: a
            # bare topic family or write_protocol never authorizes a write. These
            # transport-routing scenarios default to the ZenSDK profile (its
            # properties/write shape is transport-compatible with the JSON-report
            # families), so the routing assertions stay focused on transport/gate
            # behavior. Per-model write shapes are covered in the power-adapter tests.
            entry["hardware_profile"] = (
                self.hardware_profile or "solarflow_800_pro_2"
            )
        return entry


@dataclass(frozen=True)
class GridMeterSpec:
    meter_type: str
    transport: str
    broker_ref: str | None = None
    serial: str | None = None
    topic: str | None = None
    power_w: float = -400.0
    state: str = STATE_FRESH

    def to_config(self) -> dict:
        if self.transport == TRANSPORT_GRID_METER_HTTP:
            return {"type": self.meter_type, "ip": "192.0.2.3"}
        return {
            "type": self.meter_type,
            "mqtt": {
                "broker_ref": self.broker_ref,
                "topic": self.topic,
                "payload_format": "number",
            },
        }


@dataclass(frozen=True)
class ScenarioExpectations:
    """Declared, enforceable outcome of building a scenario.

    Every field is observed from the built installation, never re-derived from the
    scenario, so an assertion here proves the production builders honored the spec
    rather than restating it. ``active_devices`` is the control-loop device count
    (API + accepted MQTT control); telemetry-only devices are intentionally
    excluded from control and counted separately.
    """

    active_devices: int
    control_devices: int = 0
    broker_services: int = 0
    telemetry_only_devices: int = 0
    grid_meters: int = 1
    rejected_entries: tuple = ()


@dataclass(frozen=True)
class ObservedInstallation:
    active_devices: int
    control_devices: int
    broker_services: int
    telemetry_only_devices: int
    grid_meters: int
    rejected_entries: tuple


@dataclass(frozen=True)
class Scenario:
    name: str
    brokers: tuple = ()
    devices: tuple = ()
    grid_meter: GridMeterSpec = field(
        default_factory=lambda: GridMeterSpec(
            meter_type="shelly", transport=TRANSPORT_GRID_METER_HTTP
        )
    )
    gate_state: str = GATE_ALL_ENABLED
    failure_mode: str = FAILURE_NONE
    expectations: ScenarioExpectations | None = None

    # --- introspection used by the coverage guard ---------------------------
    @property
    def device_count(self) -> int:
        return len(self.devices)

    @property
    def device_transports(self) -> set:
        return {device.transport for device in self.devices}

    @property
    def payload_families(self) -> set:
        return {device.payload_family for device in self.devices}

    @property
    def broker_securities(self) -> set:
        return {broker.security for broker in self.brokers}

    @property
    def telemetry_states(self) -> set:
        states = {device.state for device in self.devices}
        states.add(self.grid_meter.state)
        return states

    @property
    def write_gates(self) -> dict:
        return gate_flags(self.gate_state)


# --- config assembly --------------------------------------------------------
def build_config(scenario: Scenario) -> dict:
    """Turn a :class:`Scenario` into an EMS ``config.json`` dict."""

    brokers = {broker.ref: broker.to_profile() for broker in scenario.brokers}
    config: dict = {
        "system": {"enabled": True, "max_total_power": 6400},
        "dry_run": False,
        "devices": [device.to_config() for device in scenario.devices],
        "grid_meter": scenario.grid_meter.to_config(),
    }
    if brokers:
        config["zendure_mqtt"] = {"enabled": True, "brokers": brokers}
    return config


# --- compatibility rules (which combinations are valid) ---------------------
def scenario_compatibility_issues(scenario: Scenario) -> list[str]:
    """Return human-readable reasons a scenario is structurally invalid.

    These encode the supported-capability matrix: scalar telemetry is never
    control-capable, cloud scalar requires the cloud transport, HTTP zensdk
    requires the API transport, and the D0 grid meter requires local MQTT.
    """

    issues: list[str] = []
    for device in scenario.devices:
        if device.payload_family == PAYLOAD_HTTP_ZENSDK and (
            device.transport != TRANSPORT_API_HTTP
        ):
            issues.append(f"{device.name}: http_zensdk requires api_http transport")
        if device.payload_family == PAYLOAD_CLOUD_SCALAR and (
            device.transport != TRANSPORT_CLOUD_MQTT
        ):
            issues.append(f"{device.name}: cloud_scalar requires cloud transport")
        if (
            device.payload_family
            in (PAYLOAD_ZENSDK_HA_SCALAR, PAYLOAD_CLOUD_SCALAR)
            and device.control_enabled
        ):
            issues.append(f"{device.name}: scalar telemetry is not control-capable")
        if device.control_enabled and device.payload_family not in (
            PAYLOAD_LEGACY_JSON,
            PAYLOAD_LEGACY_JSON_ALT,
        ):
            issues.append(
                f"{device.name}: control requires a legacy JSON payload family"
            )
    gm = scenario.grid_meter
    if gm.meter_type == "zendure_smartmeter_d0" and (
        gm.transport != TRANSPORT_GRID_METER_MQTT
    ):
        issues.append("D0 grid meter requires local MQTT transport")
    return issues


# --- installation builder ---------------------------------------------------
@dataclass
class Installation:
    """A built, ready-to-run mixed-transport installation."""

    scenario: Scenario
    config: dict
    devices: list
    api_sessions: dict
    grid_meter: object
    control_runtime: object
    telemetry_runtime: object
    network: object

    def stop(self) -> None:
        from ems.clients import close_grid_meter_client

        if self.control_runtime is not None:
            self.control_runtime.stop()
        if self.telemetry_runtime is not None:
            self.telemetry_runtime.stop()
        close_grid_meter_client(self.grid_meter)


def observe_installation(installation) -> ObservedInstallation:
    """Read the enforceable outcome of a built installation from the runtimes."""

    control = installation.control_runtime
    return ObservedInstallation(
        active_devices=len(installation.devices),
        control_devices=len(control.devices),
        broker_services=len(control.services),
        telemetry_only_devices=installation.telemetry_runtime.configured_device_count,
        grid_meters=1 if installation.grid_meter is not None else 0,
        rejected_entries=tuple(sorted(entry.name for entry in control.rejected)),
    )


def assert_installation_matches(installation, expectations: ScenarioExpectations) -> None:
    """Assert every declared expectation against the built installation.

    Raises ``AssertionError`` on any mismatch so an intentionally wrong
    expectation cannot pass — the meta-test relies on that.
    """

    observed = observe_installation(installation)
    expected = ObservedInstallation(
        active_devices=expectations.active_devices,
        control_devices=expectations.control_devices,
        broker_services=expectations.broker_services,
        telemetry_only_devices=expectations.telemetry_only_devices,
        grid_meters=expectations.grid_meters,
        rejected_entries=tuple(expectations.rejected_entries),
    )
    assert observed == expected, (
        f"{installation.scenario.name}: observed {observed} != expected {expected}"
    )


# A healthy, discharge-capable HTTP telemetry report (parse_device reads
# ``properties``); shared by API devices so the production fetch path yields a
# usable DeviceState without a synthetic patch.
_API_HEALTHY_PROPERTIES = {
    "electricLevel": 80,
    "minSoc": 150,
    "socSet": 1000,
    "solarInputPower": 800,
    "outputHomePower": 0,
    "outputLimit": 0,
    "packState": 2,
    "smartMode": 1,
    "acMode": 2,
    "acStatus": 1,
    "dcStatus": 1,
    "gridState": 1,
}


def build_api_device(spec: DeviceSpec):
    """Build an HTTP ZendureClient for an API device with a recording session.

    ``session.get`` returns a healthy telemetry report so ``client.fetch()`` (the
    production read path) yields a real DeviceState; ``session.post`` records the
    control write.
    """

    from ems.clients import ZendureClient

    session = Mock()
    session.post.return_value = SimpleNamespace(status_code=200)
    session.get.return_value = SimpleNamespace(
        status_code=200, json=lambda: {"properties": dict(_API_HEALTHY_PROPERTIES)}
    )
    client = ZendureClient(
        spec.name,
        "192.0.2.10",
        spec.serial or spec.name,
        session,
        15,
        100,
        1,
        None,
        spec.max_power,
        1.0,
        1.0,
        1.0,
    )
    return client, session


def build_installation(scenario: Scenario, network) -> Installation:
    """Wire a scenario to the real production builders and the fake broker network.

    Returns an :class:`Installation` holding controller-ready devices, the grid
    meter client, and the control/telemetry runtimes. The caller runs the
    controller and asserts routing; ``Installation.stop`` releases everything.
    """

    from ems.mqtt_credentials import MqttCredentials
    from ems.zendure_mqtt.control_runtime import build_zendure_mqtt_control_runtime
    from ems.zendure_mqtt.runtime import build_zendure_mqtt_runtime

    config = build_config(scenario)

    # Pre-create brokers so broker-level failure modes are honored on connect.
    for broker in scenario.brokers:
        network.broker(
            broker.ref,
            connect_fails=(scenario.failure_mode == FAILURE_BROKER_CONNECT),
            publish_fails=(scenario.failure_mode == FAILURE_PUBLISH),
        )

    api_devices = []
    api_sessions = {}
    for device in scenario.devices:
        if device.transport == TRANSPORT_API_HTTP:
            client, session = build_api_device(device)
            api_devices.append(client)
            api_sessions[device.name] = session

    class ScenarioCredentialResolver:
        def resolve(self, credentials_ref):
            assert any(
                broker.credentials_ref == credentials_ref
                for broker in scenario.brokers
            )
            return MqttCredentials(
                username="scenario-user",
                password="scenario-password",
                client_id="scenario-client",
                app_key="scenario-app-key",
            )

    credential_resolver = ScenarioCredentialResolver()
    control_runtime = build_zendure_mqtt_control_runtime(
        config,
        service_factory=network.control_service_factory(),
        credential_resolver=credential_resolver,
    )
    control_runtime.start()

    telemetry_runtime = build_zendure_mqtt_runtime(
        config,
        service_factory=network.telemetry_service_factory(),
        shared_services=control_runtime.services_by_ref,
        credential_resolver=credential_resolver,
    )
    telemetry_runtime.start()

    _inject_device_telemetry(scenario, network)
    grid_meter = _build_grid_meter(scenario, network, config)

    devices = api_devices + list(control_runtime.devices)
    return Installation(
        scenario=scenario,
        config=config,
        devices=devices,
        api_sessions=api_sessions,
        grid_meter=grid_meter,
        control_runtime=control_runtime,
        telemetry_runtime=telemetry_runtime,
        network=network,
    )


def _build_grid_meter(scenario: Scenario, network, config):
    import ems.config as cfg
    from ems.clients import create_grid_meter_client

    gm = scenario.grid_meter
    if gm.transport == TRANSPORT_GRID_METER_HTTP:
        session = Mock()
        # Shelly Pro 3EM status shape; total grid power via em:0.total_act_power.
        if scenario.failure_mode == FAILURE_HTTP_TIMEOUT:
            session.get.side_effect = TimeoutError("fake http timeout")
        else:
            session.get.return_value = SimpleNamespace(
                status_code=200,
                json=lambda: {"em:0": {"total_act_power": gm.power_w}},
            )
        return create_grid_meter_client(config["grid_meter"], session)

    resolved = cfg.resolve_grid_meter_mqtt_settings(config)
    resolved["_mqtt_client_factory"] = network.grid_meter_client_factory(gm.broker_ref)
    client = create_grid_meter_client(
        {"type": gm.meter_type, "mqtt": resolved}, session=object()
    )
    # A stale meter first receives a valid reading; the focused stale test ages it
    # past max_age. An unseen meter is never sent one.
    if gm.state in (STATE_FRESH, STATE_STALE):
        network.broker(gm.broker_ref).inject(gm.topic, str(gm.power_w).encode())
    return client


def _inject_device_telemetry(scenario: Scenario, network) -> None:
    """Publish telemetry so device state reflects the spec.

    ``fresh`` and ``stale`` devices both receive a valid message; a ``stale``
    device only becomes stale once an injected :class:`FakeClock` is advanced past
    ``stale_after_seconds`` (see the focused stale tests). ``malformed`` devices
    receive junk to prove it is absorbed; ``unseen`` devices are never sent a
    message so they stay distinctly unseen.
    """

    from tests.helpers import payloads

    for device in scenario.devices:
        if not device.is_mqtt:
            continue
        identifier = device.device_id or device.serial or device.name
        broker = network.broker(device.broker_ref)
        if device.state == STATE_MALFORMED:
            if device.payload_family in (PAYLOAD_LEGACY_JSON, PAYLOAD_LEGACY_JSON_ALT):
                topic = _legacy_report_topic(device)
                broker.inject(topic, payloads.MALFORMED_JSON)
            else:
                broker.inject(
                    payloads.scalar_topic("electricLevel", identifier),
                    payloads.MALFORMED_JSON,
                )
            continue
        if device.state not in (STATE_FRESH, STATE_STALE):
            continue
        if device.payload_family == PAYLOAD_ZENSDK_HA_SCALAR:
            inject_scalar_telemetry(
                network, device.broker_ref, identifier, payloads.SCALAR_METRICS
            )
        elif device.payload_family == PAYLOAD_CLOUD_SCALAR:
            for topic, payload in payloads.cloud_scalar_messages(identifier):
                broker.inject(topic, payload)
        elif device.payload_family in (PAYLOAD_LEGACY_JSON, PAYLOAD_LEGACY_JSON_ALT):
            topic = _legacy_report_topic(device)
            broker.inject(
                topic,
                payloads.legacy_json_report(device_id=identifier, serial=identifier),
            )


def _legacy_report_topic(device: DeviceSpec) -> str:
    product_key = device.product_key or "PK"
    device_id = device.device_id or device.serial or device.name
    if device.payload_family == PAYLOAD_LEGACY_JSON_ALT:
        return f"/{product_key}/{device_id}/properties/report"
    return f"iot/{product_key}/{device_id}/properties/report"


def inject_scalar_telemetry(network, broker_ref, serial, metrics):
    broker = network.broker(broker_ref)
    for metric, value in metrics.items():
        broker.inject(f"Zendure/sensor/{serial}/{metric}", str(value).encode())


def is_mapping(value) -> bool:
    return isinstance(value, Mapping)
