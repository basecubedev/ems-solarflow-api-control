# SPDX-FileCopyrightText: 2026 basecubedev
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The Admin side of the Tibber Pulse bridge: password pool, scan, config.

The bridge password is printed on the device and sent in clear text over HTTP
Basic, so the scan may send it only to a host that refused an anonymous request
the way a Tibber bridge does. Every bridge here is a fake session; nothing
reaches the LAN.
"""

import json
import threading
from pathlib import Path

import pytest

from admin import discovery
from admin.config_preview import _build_grid_meter, tibber_grid_meter_issues
from admin.credential_store import CredentialStore, CredentialStoreError, default_config_dir
from admin.discovery_connections import DiscoveryConnectionsStore, normalize_connections
from admin.install_context import detect_install_context
from admin.maintenance_config import (
    load_maintenance_config,
    prepare_maintenance_config_apply,
    preview_maintenance_config,
)
from admin.models import ROLE_GRID_METER
from admin.server import ScanRegistry, create_server
from ems.mqtt_credentials import FileBridgePasswordResolver
from tests.admin_auth_helpers import authenticate, request

pytestmark = [
    pytest.mark.admin,
    pytest.mark.setup,
    pytest.mark.maintenance,
    pytest.mark.integration,
]

TYPE = "tibber_pulse_bridge"
REFUSAL = b"Username/Password Authentication Failed."


@pytest.fixture(autouse=True)
def _isolate(isolated_install_root):
    return isolated_install_root


def test_a_saved_bridge_password_is_what_the_ems_reads(tmp_path):
    store = CredentialStore(tmp_path)
    store.save_bridge_password("basement", "AB12-CD34", label="Basement")

    assert FileBridgePasswordResolver(tmp_path / "secrets").resolve("basement") == "AB12-CD34"
    assert store.bridge_password_status("basement") == {
        "id": "basement", "label": "Basement", "saved": True, "encrypted": True,
    }
    assert b"AB12-CD34" not in (tmp_path / "secrets" / "bridge-basement.json").read_bytes()


def test_an_empty_bridge_password_is_refused(tmp_path):
    from admin.credential_store import CredentialStoreError

    with pytest.raises(CredentialStoreError):
        CredentialStore(tmp_path).save_bridge_password("basement", "")


def test_the_pool_keeps_bridge_passwords_beside_the_mqtt_credentials(tmp_path):
    store = DiscoveryConnectionsStore(tmp_path / "discovery-connections.json")
    store.add_credential_ref("mosquitto")
    store.add_bridge_password_ref("basement")
    store.add_bridge_password_ref("basement")

    loaded = store.load()
    assert loaded["tibber_bridge"]["credential_refs"] == ["basement"]
    assert loaded["local_mqtt"]["credential_refs"] == ["mosquitto"]
    assert store.remove_bridge_password_ref("basement") is True
    assert store.load()["tibber_bridge"]["credential_refs"] == []


def test_only_the_password_routes_change_the_bridge_password_list(tmp_path):
    store = DiscoveryConnectionsStore(tmp_path / "discovery-connections.json")
    store.add_bridge_password_ref("basement")

    store.save({"tibber_bridge": {"credential_refs": []}})

    assert store.load()["tibber_bridge"]["credential_refs"] == ["basement"]
    assert normalize_connections({})["tibber_bridge"] == {"credential_refs": []}


@pytest.fixture()
def server(tmp_path, monkeypatch):
    monkeypatch.setenv("EMS_ADMIN_DATA_DIR", str(tmp_path / "admin-data"))
    srv = create_server("127.0.0.1", 0, registry=ScanRegistry(scan_runner=lambda *a, **k: ([], [])))
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    authenticate(base)
    try:
        yield base
    finally:
        srv.shutdown()
        srv.server_close()


PASSWORDS = "/api/discovery/connections/bridge-passwords"


def test_the_api_saves_a_bridge_password_and_answers_its_name_only(server):
    status, _, payload = request(f"{server}{PASSWORDS}", method="POST", body={"label": "Basement bridge", "password": "AB12-CD34"})

    assert status == 200
    assert payload["tibber_bridge"]["passwords"] == [
        {"id": "basement-bridge", "label": "Basement bridge", "saved": True, "encrypted": True}
    ]
    assert "AB12-CD34" not in json.dumps(payload)
    status, _, listed = request(f"{server}{PASSWORDS}")
    assert status == 200 and listed["passwords"][0]["id"] == "basement-bridge"


def test_a_name_that_collides_with_another_saved_password_is_refused_and_the_same_name_updates(server):
    request(f"{server}{PASSWORDS}", method="POST", body={"label": "Basement", "password": "AB12-CD34"})

    status, _, payload = request(f"{server}{PASSWORDS}", method="POST", body={"label": "basement!", "password": "ZZ99-ZZ99"})
    assert status == 409 and payload["error"] == "bridge_password_name_taken"
    assert CredentialStore().load_bridge_password("basement") == "AB12-CD34"

    status, _, _ = request(f"{server}{PASSWORDS}", method="POST", body={"label": "Basement", "password": "EF56-GH78"})
    assert status == 200
    assert CredentialStore().load_bridge_password("basement") == "EF56-GH78"


@pytest.mark.parametrize("label", ["***", "__", "äöü"])
def test_a_name_without_a_letter_or_digit_is_refused(server, label):
    status, _, payload = request(f"{server}{PASSWORDS}", method="POST", body={"label": label, "password": "AB12-CD34"})

    assert status == 400 and payload["error"] == "bridge_password_name_invalid"
    assert not (default_config_dir() / "secrets" / "bridge-broker.json").exists()


@pytest.mark.parametrize("ref", ["", "%21", "Basement", "a.b"])
def test_removing_a_password_by_a_name_that_is_no_reference_removes_nothing(server, ref):
    request(f"{server}{PASSWORDS}", method="POST", body={"label": "broker", "password": "AB12-CD34"})

    status, _, _ = request(f"{server}{PASSWORDS}/{ref}", method="DELETE")

    assert status == 404
    assert (default_config_dir() / "secrets" / "bridge-broker.json").exists()


def test_the_same_name_in_other_letter_case_replaces_the_password(server):
    request(f"{server}{PASSWORDS}", method="POST", body={"label": "Keller", "password": "AB12-CD34"})

    status, _, _ = request(f"{server}{PASSWORDS}", method="POST", body={"label": "keller", "password": "EF56-GH78"})

    assert status == 200
    assert CredentialStore().load_bridge_password("keller") == "EF56-GH78"


def test_a_password_a_bridge_login_cannot_carry_is_refused(server):
    status, _, payload = request(f"{server}{PASSWORDS}", method="POST", body={"label": "Basement", "password": "AB12-CD3€"})

    assert status == 400 and payload["error"] == "bridge_password_invalid"


def test_a_name_too_long_for_a_reference_is_refused(server):
    status, _, payload = request(f"{server}{PASSWORDS}", method="POST", body={"label": "b" * 200, "password": "AB12-CD34"})

    assert status == 400 and payload["error"] == "bridge_password_name_invalid"


@pytest.mark.parametrize("body", [{"label": "", "password": "x"}, {"label": "Bridge", "password": "  "}])
def test_the_api_refuses_a_password_without_name_or_value(server, body):
    status, _, _ = request(f"{server}{PASSWORDS}", method="POST", body=body)

    assert status == 400


def test_a_password_the_saved_meter_uses_cannot_be_removed(server):
    request(f"{server}{PASSWORDS}", method="POST", body={"label": "Basement", "password": "AB12-CD34"})
    config_path = Path(detect_install_context().config_path)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps({"grid_meter": {"type": TYPE, "ip": "192.0.2.7", "credentials_ref": "basement"}}))

    status, _, payload = request(f"{server}{PASSWORDS}/basement", method="DELETE")
    assert status == 409 and payload["error"] == "bridge_password_in_use"

    config_path.write_text("{ half written")
    status, _, payload = request(f"{server}{PASSWORDS}/basement", method="DELETE")
    assert status == 409 and payload["error"] == "bridge_password_in_use"

    config_path.write_text(json.dumps({"grid_meter": {"type": "shelly", "ip": "192.0.2.8", "credentials_ref": "basement"}}))
    status, _, payload = request(f"{server}{PASSWORDS}/basement", method="DELETE")
    assert status == 200 and payload["removed"] is True
    assert not (default_config_dir() / "secrets" / "bridge-basement.json").exists()


class _Response:
    def __init__(self, status, content=b"", headers=None):
        self.status_code = status
        self.content = content
        self.headers = headers or {}

    @property
    def text(self):
        return self.content.decode("utf-8", "replace")

    def iter_content(self, size):
        yield self.content[:size]

    def close(self):
        pass

    def json(self):
        return json.loads(self.content)


class _Session:
    """Answers per path; records every request and the login it carried.

    ``anonymous`` answers a request without a login; a bridge locks its node
    list, so by default that is the refusal of marq24/ha-tibber-pulse-local#111.
    """

    def __init__(self, answers, accepted_password="AB12-CD34", anonymous=None):
        self.answers = answers
        self.accepted = accepted_password
        self.anonymous = LOCKED if anonymous is None else anonymous
        self.calls = []

    def get(self, url, params=None, timeout=None, headers=None, auth=None, allow_redirects=True, stream=False):
        path = "/" + url.split("/", 3)[3] if url.count("/") >= 3 else "/"
        self.calls.append((path, auth))
        if auth is None and path in self.anonymous:
            return self.anonymous[path]
        if auth is not None and auth[1] != self.accepted:
            return _Response(401, REFUSAL)
        answer = self.answers.get(path)
        return answer if answer is not None else _Response(404)

    def close(self):
        pass


LOCKED = {
    "/nodes.json": _Response(401, REFUSAL, {"WWW-Authenticate": 'Basic realm="admin"'}),
}
HOMEVOLT_LOCKED = {
    "/": _Response(200, b"<html><body>Bridge</body></html>", {"Content-Type": "text/html"}),
    "/nodes.json": _Response(
        401,
        b"No permission -- see authorization schemes",
        {"WWW-Authenticate": 'Basic realm="Access Homevolt default admin:QR-CODE"'},
    ),
}
BRIDGE = {
    "/nodes.json": _Response(200, json.dumps([{"node_id": 2, "eui": "70b3d5aaaa000002"}, {"node_id": 1, "eui": "70b3d5aaaa000001"}]).encode()),
    "/test.json": _Response(200, b'garbage{"esp32_fw_v": "1798-2baebf71"}trailing'),
}


def test_the_refusal_text_makes_a_bridge_candidate():
    device = discovery.probe_tibber_bridge(_Session(BRIDGE), "192.0.2.7", 0.5)

    assert device.api_family == TYPE
    assert device.role_suggestion == ROLE_GRID_METER
    assert device.config_ready is False
    assert device.missing_config_fields == ["grid_meter.credentials_ref"]


def test_a_bridge_with_an_open_start_page_is_found_by_its_homevolt_challenge():
    """Issue #341: its start page answers 200, its node list a challenge naming Homevolt."""

    session = _Session(BRIDGE, anonymous=HOMEVOLT_LOCKED)
    device = discovery.probe_tibber_bridge(session, "192.0.2.7", 0.5)

    assert device is not None and device.api_family == TYPE
    assert session.calls == [("/nodes.json", None)]
    discovery.unlock_tibber_bridge(session, device, [("basement", "AB12-CD34")])
    assert (device.credentials_ref, device.node_id) == ("basement", 1)


@pytest.mark.parametrize(
    "anonymous",
    [
        {"/nodes.json": _Response(401, b"Unauthorized", {"WWW-Authenticate": 'Basic realm="FRITZ!Box"'})},
        {"/nodes.json": _Response(401, b"", {"WWW-Authenticate": 'Digest realm="Homevolt"'})},
        {"/nodes.json": _Response(200, REFUSAL)},
        {"/nodes.json": _Response(403, REFUSAL)},
        {"/": HOMEVOLT_LOCKED["/"]},
    ],
)
def test_another_login_page_is_not_a_bridge(anonymous):
    assert discovery.probe_tibber_bridge(_Session({}, anonymous=anonymous), "192.0.2.8", 0.5) is None


def test_a_challenge_that_names_tibber_is_a_bridge_too():
    answer = _Response(401, b"", {"WWW-Authenticate": 'Basic realm="Tibber Bridge"'})

    assert discovery.probe_tibber_bridge(_Session({}, anonymous={"/nodes.json": answer}), "192.0.2.7", 0.5) is not None


def test_the_saved_passwords_open_the_bridge_and_name_its_first_reading_head():
    session = _Session(BRIDGE)
    device = discovery.probe_tibber_bridge(session, "192.0.2.7", 0.5)

    discovery.unlock_tibber_bridge(session, device, [("wrong", "nope"), ("basement", "AB12-CD34")])

    assert device.credentials_ref == "basement"
    assert device.node_id == 1
    assert device.serial_number is None
    assert device.firmware == "1798-2baebf71"
    assert device.config_ready is True and device.verified is True
    assert device.to_dict()["credentials_ref"] == "basement"


def test_a_node_number_outside_one_to_255_is_not_offered():
    nodes = [{"node_id": 0, "eui": "70b3d5aaaa000000"}, {"node_id": 300}, {"node_id": "x"}, {"node_id": "²"}, {"node_id": 3, "eui": "70b3d5aaaa000003"}]
    session = _Session({**BRIDGE, "/nodes.json": _Response(200, json.dumps(nodes).encode())})
    device = discovery.probe_tibber_bridge(session, "192.0.2.7", 0.5)

    discovery.unlock_tibber_bridge(session, device, [("basement", "AB12-CD34")])

    assert device.node_id == 3


def test_an_open_bridge_without_a_usable_node_still_needs_one():
    session = _Session({**BRIDGE, "/nodes.json": _Response(200, b'[{"node_id": 0}]')})
    device = discovery.probe_tibber_bridge(session, "192.0.2.7", 0.5)

    discovery.unlock_tibber_bridge(session, device, [("basement", "AB12-CD34")])

    assert device.credentials_ref == "basement"
    assert device.config_ready is False and device.missing_config_fields == ["grid_meter.node_id"]


def test_a_bridge_that_took_the_password_keeps_it_when_its_node_list_is_no_json():
    session = _Session({**BRIDGE, "/nodes.json": _Response(200, b"not json")})
    device = discovery.probe_tibber_bridge(session, "192.0.2.7", 0.5)

    discovery.unlock_tibber_bridge(session, device, [("wrong", "nope"), ("basement", "AB12-CD34")])

    assert (device.credentials_ref, device.verified) == ("basement", True)
    assert device.missing_config_fields == ["grid_meter.node_id"]


def test_a_missing_node_list_proves_no_password():
    session = _Session({**BRIDGE, "/nodes.json": _Response(404)})
    device = discovery.probe_tibber_bridge(session, "192.0.2.7", 0.5)

    discovery.unlock_tibber_bridge(session, device, [("basement", "AB12-CD34")])

    assert device.credentials_ref is None and device.config_ready is False


class _ErrorOnWrongPassword(_Session):
    """A bridge that answers a wrong login with 500 instead of 401."""

    def get(self, url, params=None, timeout=None, headers=None, auth=None, allow_redirects=True, stream=False):
        if auth is not None and auth[1] != self.accepted:
            self.calls.append(("/" + url.split("/", 3)[3], auth))
            return _Response(500, b"error")
        return super().get(url, params=params, timeout=timeout, headers=headers, auth=auth,
                           allow_redirects=allow_redirects, stream=stream)


def test_a_server_error_ends_the_attempt_without_a_password():
    session = _ErrorOnWrongPassword(BRIDGE)
    device = discovery.probe_tibber_bridge(session, "192.0.2.7", 0.5)

    discovery.unlock_tibber_bridge(session, device, [("wrong", "nope"), ("basement", "AB12-CD34")])

    assert device.credentials_ref is None and device.config_ready is False
    assert [auth[1] for _, auth in session.calls if auth] == ["nope"]


def test_a_bridge_no_saved_password_opens_stays_a_candidate():
    session = _Session(BRIDGE)
    device = discovery.probe_tibber_bridge(session, "192.0.2.7", 0.5)

    discovery.unlock_tibber_bridge(session, device, [("wrong", "nope")])

    assert device.credentials_ref is None and device.config_ready is False


def test_the_scan_sends_no_password_to_a_host_that_is_not_a_bridge():
    router = {"/nodes.json": _Response(401, b"Unauthorized", {"WWW-Authenticate": 'Basic realm="Router"'})}
    session = _Session({}, anonymous=router)
    asked = []

    devices, _ = discovery.scan_network(
        "127.0.0.1/32", session=session, bridge_passwords=lambda: asked.append(True) or [("basement", "AB12-CD34")]
    )

    assert devices == []
    assert asked == []
    assert all(auth is None for _, auth in session.calls)


@pytest.mark.parametrize("anonymous", [LOCKED, HOMEVOLT_LOCKED])
def test_the_scan_unlocks_a_found_bridge_with_the_saved_password(anonymous):
    session = _Session(BRIDGE, anonymous=anonymous)

    devices, _ = discovery.scan_network(
        "127.0.0.1/32", session=session, bridge_passwords=lambda: [("basement", "AB12-CD34")]
    )

    assert [(d.api_family, d.credentials_ref, d.node_id) for d in devices] == [(TYPE, "basement", 1)]


def test_a_found_bridge_becomes_a_grid_meter_with_its_password_and_node():
    validation = {"errors": [], "warnings": [], "info": []}
    found = {"display_name": "Tibber Pulse bridge", "ip": "192.0.2.7", "port": 80, "api_family": TYPE,
             "device_type": TYPE, "credentials_ref": "basement", "node_id": 2}

    grid = _build_grid_meter(found, {}, validation, {})

    assert grid == {"type": TYPE, "ip": "192.0.2.7", "port": 80, "credentials_ref": "basement", "node_id": 2}
    assert validation["errors"] == []


def test_the_meter_s_own_password_and_node_win_over_the_defaults():
    saved = {"type": TYPE, "ip": "192.0.2.7", "credentials_ref": "old", "node_id": 1}
    found = {"display_name": "Tibber Pulse bridge", "ip": "192.0.2.7", "api_family": TYPE, "device_type": TYPE}

    kept = _build_grid_meter(found, saved, {"errors": [], "warnings": [], "info": []}, {})
    chosen = _build_grid_meter({**found, "credentials_ref": "basement", "node_id": 2}, saved,
                               {"errors": [], "warnings": [], "info": []}, {})

    assert (kept["credentials_ref"], kept["node_id"]) == ("old", 1)
    assert (chosen["credentials_ref"], chosen["node_id"]) == ("basement", 2)


def test_a_meter_naming_a_password_that_is_not_saved_is_refused(tmp_path):
    store = CredentialStore(tmp_path)
    grid = {"type": TYPE, "ip": "192.0.2.7", "credentials_ref": "basement"}

    assert [issue["code"] for issue in tibber_grid_meter_issues(grid, store)] == ["grid_meter_bridge_password_missing"]
    store.save_bridge_password("basement", "AB12-CD34")
    assert tibber_grid_meter_issues(grid, store) == []
    assert [issue["code"] for issue in tibber_grid_meter_issues({"type": TYPE, "ip": "192.0.2.7"}, store)] == [
        "grid_meter_tibber_pulse_invalid"
    ]


def test_a_saved_password_that_no_longer_decrypts_is_refused(tmp_path):
    store = CredentialStore(tmp_path)
    store.save_bridge_password("basement", "AB12-CD34")
    record_path = next(tmp_path.rglob("bridge-basement.json"))
    record = json.loads(record_path.read_text())
    record_path.write_text(json.dumps({**record, "password": "not-a-token"}))

    issues = tibber_grid_meter_issues({"type": TYPE, "ip": "192.0.2.7", "credentials_ref": "basement"}, store)

    assert [issue["code"] for issue in issues] == ["grid_meter_bridge_password_unreadable"]


def _write_config(base_dir, grid_meter):
    config_dir = base_dir / "config"
    config_dir.mkdir(exist_ok=True)
    (config_dir / "config.json").write_text(json.dumps({
        "system": {"max_total_power": 800},
        "devices": [{"name": "WR1", "ip": "192.168.1.100", "sn": "AAA", "max_power": 800}],
        "grid_meter": grid_meter,
    }), encoding="utf-8")


def test_maintenance_edits_the_bridge_node_and_password_reference(tmp_path):
    _write_config(tmp_path, {"type": "shelly", "ip": "192.168.1.3"})
    CredentialStore().save_bridge_password("basement", "AB12-CD34")
    loaded = load_maintenance_config(base_dir=str(tmp_path))
    draft = loaded["draft"]
    draft["grid_meter"] = {**draft["grid_meter"], "type": TYPE, "ip": "192.168.1.70", "node_id": 2, "credentials_ref": "basement"}

    prepared = prepare_maintenance_config_apply(draft, loaded["revision"], base_dir=str(tmp_path))

    assert prepared["status"] == "ok", prepared
    grid = json.loads(prepared["payload"])["grid_meter"]
    assert grid == {"type": TYPE, "ip": "192.168.1.70", "node_id": 2, "credentials_ref": "basement"}
    _write_config(tmp_path, grid)
    reloaded = load_maintenance_config(base_dir=str(tmp_path))["draft"]["grid_meter"]
    assert (reloaded["node_id"], reloaded["credentials_ref"]) == (2, "basement")


def test_maintenance_refuses_a_bridge_password_that_is_not_saved(tmp_path):
    _write_config(tmp_path, {"type": TYPE, "ip": "192.168.1.70", "credentials_ref": "basement"})
    loaded = load_maintenance_config(base_dir=str(tmp_path))

    preview = preview_maintenance_config(loaded["draft"], base_dir=str(tmp_path))

    assert "grid_meter_bridge_password_missing" in {issue["code"] for issue in preview["validation"]["errors"]}


def test_the_unified_search_result_keeps_what_opened_the_bridge_and_nothing_for_others():
    from admin.discovery_unify import build_unified_devices

    bridge = {"ip": "192.0.2.7", "api_family": TYPE, "device_type": TYPE, "role_suggestion": ROLE_GRID_METER,
              "display_name": "Tibber Pulse bridge", "serial_number": "70b3d5aaaa000001", "credentials_ref": "basement", "node_id": 1}
    shelly = {"ip": "192.0.2.8", "api_family": "shelly_gen2", "device_type": "shelly", "role_suggestion": ROLE_GRID_METER,
              "display_name": "Shelly", "serial_number": "SHELLY1", "credentials_ref": "my-broker", "node_id": 3}

    devices = {d["ip"]: d for d in build_unified_devices({"local_api": [bridge, shelly]}, ["local_api"])}

    assert (devices["192.0.2.7"]["credentials_ref"], devices["192.0.2.7"]["node_id"]) == ("basement", 1)
    assert "credentials_ref" not in devices["192.0.2.8"] and "node_id" not in devices["192.0.2.8"]


def test_a_password_whose_file_cannot_be_removed_stays_listed(server, monkeypatch):
    request(f"{server}{PASSWORDS}", method="POST", body={"label": "Basement", "password": "AB12-CD34"})

    def refuse(self, ref):
        raise CredentialStoreError("Could not remove the secret file.")

    monkeypatch.setattr(CredentialStore, "forget_bridge_password", refuse)
    status, _, payload = request(f"{server}{PASSWORDS}/basement", method="DELETE")

    assert status == 500 and payload["error"] == "store_failed"
    _, _, listed = request(f"{server}{PASSWORDS}")
    assert [entry["id"] for entry in listed["passwords"]] == ["basement"]


def test_the_bridge_defaults_agree_where_they_are_kept_twice():
    from ems import tibber_pulse
    from ems.config import OUTPUT_CONTROL_DEFAULTS, TIBBER_PULSE_DEFAULT_PORT
    from ems.config_catalog import GRID_METER_VARIANTS

    assert GRID_METER_VARIANTS[TYPE]["default_port"] == TIBBER_PULSE_DEFAULT_PORT
    assert tibber_pulse.MAX_AGE_SECONDS == OUTPUT_CONTROL_DEFAULTS["telemetry_max_age_seconds"]


def test_the_bridge_keys_have_one_source_that_the_catalog_and_modern_follow():
    import re

    from ems.config import TIBBER_PULSE_BRIDGE_KEYS
    from ems.config_catalog import grid_meter_variant_field_spec

    spec = grid_meter_variant_field_spec(TYPE)["keys"]
    assert set(TIBBER_PULSE_BRIDGE_KEYS) == spec - {"type", "ip", "port"}
    source = (Path(__file__).parents[1] / "frontend/admin-modern/src/lib/setupDraft.js").read_text(encoding="utf-8")
    mirrored = re.search(r"BRIDGE_KEYS = Object\.freeze\(\[(.*?)\]\)", source).group(1)
    assert tuple(re.findall(r'"([a-z_]+)"', mirrored)) == TIBBER_PULSE_BRIDGE_KEYS


class _TimedSession(_Session):
    def __init__(self, answers):
        super().__init__(answers)
        self.timeouts = []

    def get(self, url, params=None, timeout=None, headers=None, auth=None, allow_redirects=True, stream=False):
        if auth is not None:
            self.timeouts.append(timeout)
        return super().get(url, params=params, timeout=timeout, headers=headers, auth=auth,
                           allow_redirects=allow_redirects, stream=stream)


def test_the_unlock_keeps_to_the_scan_timeout():
    session = _TimedSession(BRIDGE)

    discovery.scan_network("127.0.0.1/32", timeout_ms=400, session=session, bridge_passwords=lambda: [("basement", "AB12-CD34")])

    assert session.timeouts and set(session.timeouts) == {(0.4, discovery.TIBBER_BRIDGE_READ_TIMEOUT_S)}
