# SPDX-License-Identifier: AGPL-3.0-or-later
"""Deterministic in-process harness for the MQTT release contract.

One reusable object that walks the *entire* MQTT lifecycle with real production
classes and only the true external boundaries faked (MQTT socket, HTTP socket,
clock, filesystem location):

    save discovery credential
      -> discovery generation (real MqttBrokerStore, generations + TTL)
      -> server-side trusted proposal (real resolve_selected_proposals)
      -> preview / download / write / apply (real Admin HTTP server)
      -> config.json (credentials_ref only)
      -> Core credential resolution (real FileMqttCredentialResolver, no admin dep)
      -> runtime (real build_zendure_mqtt_* + create_grid_meter_client)
      -> telemetry -> DeviceState -> controller -> transport-specific publish
      -> cleanup

The Admin stage is driven through the *real* AdminServer on a loopback socket
(exactly as ``test_admin_mqtt_credential_promotion_transaction`` does) so the
production promotion ordering / rollback / trust boundary is exercised, never
re-implemented here. The runtime stage reads the applied ``config.json`` and the
real on-disk secret store, so ``credentials_ref`` is resolved by the same Core
resolver EMS uses at startup.

Business logic is never duplicated in this helper: proposal validation, preview
generation, apply semantics, credential promotion, config parsing, credential
resolution, DeviceState conversion, allocation, write-gate evaluation and
cleanup all come from production code.
"""

import json
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace

from admin import zendure_mqtt_config_proposals
from admin.mqtt_discovery import MqttBrokerDiscovery, MqttBrokerStore
from admin.server import ScanRegistry, create_server
from tests.admin_auth_helpers import authenticate, auth_headers
from tests.helpers.controller import run_installation_cycle
from tests.helpers.fake_mqtt import FakeMqttNetwork
from tests.helpers.system_alignment import SetupReadySystemAlignment
from tests.test_admin_server import (
    _FakeReleaseManager,
    _fake_gateway_prober,
    _fake_scan,
)

# A release-shaped template so the real preview generator produces a full config.
RELEASE_TEMPLATE = {
    "system": {"max_total_power": 1600, "dry_run": False},
    "devices": [
        {"name": "WR1", "ip": "192.0.2.1", "sn": "YOUR_SN", "max_power": 800}
    ],
    "grid_meter": {"type": "shelly", "ip": "192.0.2.3"},
    "zendure_mqtt": {"enabled": True, "brokers": {}},
}

_ALL_GATES_ENABLED = {
    "allow_hardware_writes": True,
    "allow_mqtt_local_control_writes": True,
    "allow_mqtt_zendure_control_writes": True,
}


class _FreshTemplateReleaseManager(_FakeReleaseManager):
    """Return a fresh template each call, like the real disk-backed manager.

    The production ``ReleaseManager`` reads ``config.template.json`` from disk on
    every ``config_template`` call, so each preview starts from an unmodified
    template. Copying here keeps repeated preview/apply deterministic instead of
    accumulating mutations on one shared dict.
    """

    def config_template(self):
        import copy

        resource = super().config_template()
        resource["template"] = copy.deepcopy(resource["template"])
        return resource


class ManualClock:
    """Injectable float clock for the discovery store (TTL/generation math).

    Matches the ``time.time``-style callable ``MqttBrokerStore`` expects; advance
    it to expire a proposal window with no real sleep.
    """

    def __init__(self, start: float = 100.0):
        self.now = float(start)

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += float(seconds)


def device_observation(
    serial,
    *,
    topic_family="zensdk_ha_scalar",
    device_id=None,
    product_key=None,
    model_hint=None,
    credentials_ref=None,
    metrics=None,
    topics=None,
    source_type="local_mqtt",
):
    """One discovery device observation as the topic discoverer would emit it."""

    observation = {
        "source_type": source_type,
        "topic_family": topic_family,
        "serial_number": serial,
        "device_id": device_id or serial,
        "metrics_seen": list(metrics or ["electricLevel"]),
        "topics_seen": list(topics or [f"Zendure/sensor/{serial}/electricLevel"]),
    }
    if product_key is not None:
        observation["product_key"] = product_key
    if model_hint is not None:
        observation["model_hint"] = model_hint
    if credentials_ref is not None:
        observation["credentials_ref"] = credentials_ref
    return observation


def api_device_selection(serial="SN-RELEASE-API"):
    """One bootable API inverter selection for non-control MQTT fixtures."""

    return {
        "config_name": "WR1",
        "display_name": "Release API inverter",
        "role": "inverter",
        "enabled": True,
        "ip": "192.0.2.10",
        "serial_number": serial,
        "device_type": "zendure_solarflow_800_pro",
        "api_family": "zendure_local_http",
    }


def broker_candidate(
    host,
    *,
    port=1883,
    devices=(),
    reachable=True,
    topic_refresh_success=None,
    tls=False,
    tls_insecure=False,
    tls_mode=None,
):
    """A discovery broker candidate for :meth:`ReleaseContractHarness.run_generation`.

    ``reachable`` / ``topic_refresh_success`` drive per-broker selectability so a
    partial refresh (one broker down) keeps only the reachable broker's devices
    selectable — the store never validates broker B just because broker A worked.
    """

    for observation in devices:
        observation.setdefault("broker_host", host)
        observation.setdefault("broker_port", port)
        if tls:
            observation.setdefault("tls_mode", tls_mode or "system_ca")
    candidate = {
        "id": f"mqtt:{host}:{port}",
        "host": host,
        "port": port,
        "reachable": reachable,
        "topic_refresh_success": (
            reachable if topic_refresh_success is None else topic_refresh_success
        ),
        "tls": tls,
        "tls_insecure": tls_insecure,
        "devices": list(devices) if reachable else [],
    }
    if tls_mode is not None:
        candidate["tls_mode"] = tls_mode
    return candidate


@dataclass
class RuntimeInstallation:
    """A started runtime built from an applied config, ready for a control cycle."""

    devices: list
    grid_meter: object
    control_runtime: object
    telemetry_runtime: object
    network: FakeMqttNetwork
    api_sessions: dict = field(default_factory=dict)
    gates: dict = field(default_factory=lambda: dict(_ALL_GATES_ENABLED))
    _stopped: bool = False

    # run_installation_cycle reads installation.scenario.write_gates when gates
    # is not passed; expose a matching shim so the same helper drives us.
    @property
    def scenario(self):
        return SimpleNamespace(write_gates=self.gates)

    def broker(self, ref):
        return self.network.broker(ref)

    def inject(self, ref, topic, payload, *, retain=False):
        return self.network.broker(ref).inject(topic, payload, retain=retain)

    def run_cycle(self, *, gates=None, clock=None):
        return run_installation_cycle(self, gates=gates or self.gates, clock=clock)

    def stop(self):
        if self._stopped:
            return
        self._stopped = True
        from ems.clients import close_grid_meter_client

        if self.control_runtime is not None:
            self.control_runtime.stop()
        if self.telemetry_runtime is not None:
            self.telemetry_runtime.stop()
        if self.grid_meter is not None:
            close_grid_meter_client(self.grid_meter)


class ReleaseContractHarness:
    """Composes the real MQTT setup-to-runtime lifecycle for one test."""

    def __init__(self, tmp_path, *, clock_start=100.0, proposal_ttl_seconds=900):
        self.tmp_path = Path(tmp_path)
        self.clock = ManualClock(clock_start)
        self.store = MqttBrokerStore(
            clock=self.clock, proposal_ttl_seconds=proposal_ttl_seconds
        )
        self.discovery = MqttBrokerDiscovery(store=self.store, topic_discoverer=None)
        self._srv = None
        self._base = None
        self._thread = None
        self._runtimes = []
        self.last_apply_path = None

    # --- lifecycle ----------------------------------------------------------
    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.close()

    def start(self):
        self._srv = create_server(
            "127.0.0.1",
            0,
            registry=ScanRegistry(scan_runner=_fake_scan),
            gateway_prober=_fake_gateway_prober,
            mqtt_discovery=self.discovery,
            release_manager=_FreshTemplateReleaseManager(
                self.tmp_path, template=RELEASE_TEMPLATE
            ),
            system_alignment=SetupReadySystemAlignment(),
        )
        self._thread = threading.Thread(target=self._srv.serve_forever, daemon=True)
        self._thread.start()
        self._base = f"http://127.0.0.1:{self._srv.server_address[1]}"
        authenticate(self._base)
        return self

    def close(self):
        for runtime in self._runtimes:
            runtime.stop()
        if self._srv is not None:
            self._srv.shutdown()
            self._srv.server_close()
            self._srv = None

    @property
    def server(self):
        return self._srv

    @property
    def credential_store(self):
        return self._srv.credential_store

    @property
    def secrets_dir(self):
        return Path(self.credential_store.secrets_dir)

    # --- credentials --------------------------------------------------------
    def save_discovery_credential(self, ref, username, password, label=None):
        return self.credential_store.save_mqtt_discovery_secret(
            ref, username, password, label
        )

    def runtime_credential_exists(self, ref):
        return self.credential_store.load_mqtt_broker_secret(ref) is not None

    def forget_discovery_credential(self, ref):
        self.credential_store.forget_mqtt_discovery_secret(ref)

    def forget_runtime_credential(self, ref):
        self.credential_store.forget_mqtt_broker_secret(ref)

    def snapshot_secrets_dir(self):
        """Sorted list of secret files, for before/after side-effect assertions."""

        if not self.secrets_dir.exists():
            return []
        return sorted(p.name for p in self.secrets_dir.iterdir() if p.is_file())

    # --- discovery ----------------------------------------------------------
    def run_generation(self, brokers):
        """Run one discovery generation over broker candidate dicts."""

        generation = self.store.begin_refresh()
        reachable = any(candidate.get("reachable", True) for candidate in brokers)
        self.store.complete_refresh(generation, brokers, success=reachable)
        return generation

    def candidates(self):
        return self.discovery.candidates()

    def trusted_proposals(self):
        # Mirror the server: attach identity tokens (and any Cloud/route-primary
        # stable id) so a selection built here resolves against the same set the
        # preview/apply endpoints expose. Local serial-bearing ids are unchanged.
        proposals = zendure_mqtt_config_proposals.proposals_from_brokers(
            self.candidates()
        )
        return zendure_mqtt_config_proposals.annotate_identity_tokens(
            proposals, self._srv.identity_token_key
        )

    def proposal_for(self, serial):
        for proposal in self.trusted_proposals():
            if proposal.get("serial_number") == serial:
                return proposal
        return None

    @staticmethod
    def selection(proposal, *, replace_grid_meter=None):
        entry = {"id": proposal["id"], "broker_ref": proposal["broker_ref"]}
        if replace_grid_meter is not None:
            entry["replace_grid_meter"] = replace_grid_meter
        return entry

    # --- Admin HTTP endpoints (real server) ---------------------------------
    def request(self, path, method="POST", body=None, *, raw=False):
        url = f"{self._base}{path}"
        data = None
        headers = dict(auth_headers(url, method))
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req) as resp:
                payload = resp.read()
                if raw:
                    return resp.status, payload
                return resp.status, json.loads(payload or b"null")
        except urllib.error.HTTPError as exc:
            payload = exc.read()
            if raw:
                return exc.code, payload
            return exc.code, json.loads(payload or b"null")

    @staticmethod
    def _body(devices, selections, supported_grid_meter_count, **extra):
        body = {
            "devices": list(devices or []),
            "supported_grid_meter_count": supported_grid_meter_count,
            "zendure_mqtt_proposals": list(selections or []),
        }
        body.update(extra)
        return body

    def preview(self, *, devices=None, selections=(), supported_grid_meter_count=0):
        return self.request(
            "/api/setup/config-preview",
            body=self._body(devices, selections, supported_grid_meter_count),
        )

    def download(self, *, devices=None, selections=(), supported_grid_meter_count=0):
        return self.request(
            "/api/setup/config/download",
            body=self._body(devices, selections, supported_grid_meter_count),
            raw=True,
        )

    def write(
        self, *, devices=None, selections=(), supported_grid_meter_count=0, overwrite=False
    ):
        return self.request(
            "/api/setup/config/write",
            body=self._body(
                devices, selections, supported_grid_meter_count, overwrite=overwrite
            ),
        )

    def apply(self, *, devices=None, selections=(), supported_grid_meter_count=0):
        status, payload = self.request(
            "/api/setup/config/apply",
            body=self._body(devices, selections, supported_grid_meter_count),
        )
        if status == 200 and isinstance(payload, dict) and payload.get("path"):
            self.last_apply_path = Path(payload["path"])
        return status, payload

    def applied_config(self):
        if self.last_apply_path is None:
            raise AssertionError("no config has been applied yet")
        return json.loads(self.last_apply_path.read_text(encoding="utf-8"))

    # --- runtime ------------------------------------------------------------
    def start_runtime(self, config, *, gates=None, network=None, clock=None):
        """Build and start the runtime from an applied config using the fake broker.

        Uses the same builders EMS uses at startup, wired to the fake MQTT network
        and the real on-disk credential resolver, so ``credentials_ref`` in the
        config is resolved to in-memory username/password by Core, never Admin.
        """

        from ems.mqtt_credentials import FileMqttCredentialResolver
        from ems.zendure_mqtt.control_runtime import build_zendure_mqtt_control_runtime
        from ems.zendure_mqtt.runtime import build_zendure_mqtt_runtime

        resolver = FileMqttCredentialResolver(self.secrets_dir)
        network = network or FakeMqttNetwork(clock=clock)

        control = build_zendure_mqtt_control_runtime(
            config,
            service_factory=network.control_service_factory(),
            credential_resolver=resolver,
        )
        control.start()
        telemetry = build_zendure_mqtt_runtime(
            config,
            service_factory=network.telemetry_service_factory(),
            shared_services=control.services_by_ref,
            credential_resolver=resolver,
        )
        telemetry.start()

        grid_meter = self._build_grid_meter(config, network, resolver)
        api_devices, api_sessions = self._build_api_devices(config)
        devices = api_devices + list(control.devices)

        installation = RuntimeInstallation(
            devices=devices,
            grid_meter=grid_meter,
            control_runtime=control,
            telemetry_runtime=telemetry,
            network=network,
            api_sessions=api_sessions,
            gates=dict(gates or _ALL_GATES_ENABLED),
        )
        self._runtimes.append(installation)
        return installation

    def _build_grid_meter(self, config, network, resolver):
        import ems.config as cfg
        from ems.clients import create_grid_meter_client

        grid = config.get("grid_meter") if isinstance(config, dict) else None
        if not isinstance(grid, dict):
            return None
        mqtt = grid.get("mqtt")
        if not isinstance(mqtt, dict):
            session = SimpleNamespace(
                get=lambda *a, **k: SimpleNamespace(
                    status_code=200, json=lambda: {"em:0": {"total_act_power": -400.0}}
                )
            )
            return create_grid_meter_client(grid, session)
        resolved = cfg.resolve_grid_meter_mqtt_settings(config)
        broker_ref = mqtt.get("broker_ref")
        resolved["_mqtt_client_factory"] = network.grid_meter_client_factory(broker_ref)
        return create_grid_meter_client(
            {"type": grid.get("type"), "mqtt": resolved},
            session=object(),
            mqtt_credential_resolver=resolver,
        )

    @staticmethod
    def _build_api_devices(config):
        from unittest.mock import Mock

        from ems.clients import ZendureClient
        from tests.helpers.mqtt_scenarios import _API_HEALTHY_PROPERTIES

        devices, sessions = [], {}
        for entry in config.get("devices", []) if isinstance(config, dict) else []:
            if entry.get("type") == "zendure_mqtt":
                continue
            ip = entry.get("ip")
            serial = entry.get("sn") or entry.get("serial_number")
            if not ip or not serial:
                continue
            session = Mock()
            session.post.return_value = SimpleNamespace(status_code=200)
            session.get.return_value = SimpleNamespace(
                status_code=200,
                json=lambda: {"properties": dict(_API_HEALTHY_PROPERTIES)},
            )
            client = ZendureClient(
                entry.get("name", serial), ip, serial, session, 15, 100, 1, None,
                entry.get("max_power", 800), 1.0, 1.0, 1.0,
            )
            devices.append(client)
            sessions[entry.get("name", serial)] = session
        return devices, sessions

    # --- secret assertions --------------------------------------------------
    def assert_no_secret(self, *artifacts, tokens):
        """Assert none of ``tokens`` appears in any of ``artifacts`` (any type)."""

        haystacks = []
        for artifact in artifacts:
            if isinstance(artifact, (bytes, bytearray)):
                haystacks.append(artifact.decode("utf-8", "replace"))
            elif isinstance(artifact, str):
                haystacks.append(artifact)
            else:
                haystacks.append(json.dumps(artifact, default=repr))
        blob = "\n".join(haystacks)
        for token in tokens:
            assert token not in blob, f"secret {token!r} leaked into a public artifact"

    def public_secret_surface(self):
        """The redaction-safe public surface for a secret sweep."""

        surface = {
            "candidates": self.candidates(),
            "trusted_proposals": self.trusted_proposals(),
            "config_status": self._srv.config_export.status(),
        }
        if self.last_apply_path is not None:
            surface["applied_config"] = self.applied_config()
        return surface
