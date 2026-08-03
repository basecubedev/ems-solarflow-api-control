# SPDX-License-Identifier: AGPL-3.0-or-later
"""Parallel Apply requests cannot corrupt runtime MQTT credential state.

Credential staging, the config write and any rollback must run as one
serialized transaction shared by Fresh Setup and Maintenance: a failed Apply
must never roll its snapshot back over a concurrently successful Apply, and
two Applies creating the same new record must not leave a config referencing
a record that a failed sibling deleted. All tests coordinate the threads with
events (no sleeps): the blocked request A is released only once request B has
either finished (unserialized legacy path) or is provably waiting on the
shared transaction lock.
"""

import json
import pathlib
import threading
from contextlib import contextmanager

import pytest

from ems.mqtt_credentials import FileMqttCredentialResolver
from tests.test_admin_maintenance_mqtt_apply import (
    _CloudFetch,
    _existing_config,
    _local_observation,
    _local_proposal,
    _maintenance_apply_with_proposal,
    _paths,
    _request,
    _serve,
    _write_config,
)
from tests.test_admin_mqtt_credential_promotion_transaction import (
    _authorized,
    _discovery_with_proposal,
    _serve as _serve_setup,
    _write_body,
)

pytestmark = [
    pytest.mark.admin,
    pytest.mark.authority,
    pytest.mark.config,
    pytest.mark.integration,
    pytest.mark.simulation,
]

WAIT_S = 30  # generous failsafe for event waits; never used for sequencing


@pytest.fixture(autouse=True)
def _isolate(isolated_install_root):
    return isolated_install_root


def _observe_transaction(srv, entered_a, waiting_b):
    """Instrument the shared apply transaction to flag a second waiter.

    Returns the original transaction context (or ``None`` when the server does
    not expose one — the unserialized legacy path). ``waiting_b`` fires when a
    request reaches the transaction while request A already sits inside its
    blocked apply, i.e. exactly when releasing A is safe on the serialized
    path.
    """

    transaction = getattr(srv.config_apply, "apply_transaction", None)
    if transaction is None:
        return None

    @contextmanager
    def observed():
        if entered_a.is_set():
            waiting_b.set()
        with transaction():
            yield

    srv.config_apply.apply_transaction = observed
    return transaction


def _release_a_when_b_progressed(transaction, waiting_b, thread_b, release_a):
    if transaction is not None:
        assert waiting_b.wait(WAIT_S), "request B never reached the transaction"
    else:
        thread_b.join(WAIT_S)
        assert not thread_b.is_alive(), "request B did not finish"
    release_a.set()


def test_failed_apply_cannot_roll_back_over_parallel_success(monkeypatch, tmp_path):
    monkeypatch.setenv("EMS_INSTALL_DIR", str(tmp_path))
    _write_config(tmp_path, _existing_config())
    fetch = _CloudFetch()
    srv, base = _serve(
        tmp_path, fetch, local_observation=_local_observation(credentials_ref="home")
    )
    srv.credential_store.save_mqtt_discovery_secret("home", "ems", "old-password")
    try:
        proposal = _local_proposal(base)
        status, payload = _maintenance_apply_with_proposal(base, proposal)
        assert status == 200 and payload.get("ok") is True, payload

        status, loaded = _request(f"{base}/api/admin/maintenance/config")
        assert status == 200 and loaded["status"] == "ok", loaded
        body = {
            "draft": loaded["draft"],
            "revision": loaded["revision"],
            "confirm": True,
        }

        entered_a = threading.Event()
        release_a = threading.Event()
        waiting_b = threading.Event()
        original_apply = srv.config_apply.apply_maintenance
        first_call = []

        def blocking_then_failing_apply(payload_bytes, revision, create_backup=True):
            if not first_call:
                first_call.append(True)
                entered_a.set()
                assert release_a.wait(WAIT_S)
                raise OSError("simulated config write failure")
            return original_apply(payload_bytes, revision, create_backup)

        srv.config_apply.apply_maintenance = blocking_then_failing_apply
        transaction = _observe_transaction(srv, entered_a, waiting_b)

        results = {}

        def run(name):
            results[name] = _request(
                f"{base}/api/admin/maintenance/config/apply", "POST", body
            )

        # Request A stages the rotation to value-a and blocks inside its
        # (failing) config write.
        srv.credential_store.save_mqtt_discovery_secret("home", "ems", "value-a")
        thread_a = threading.Thread(target=run, args=("a",))
        thread_a.start()
        assert entered_a.wait(WAIT_S)

        # Request B rotates the same credential to value-b and succeeds.
        srv.credential_store.save_mqtt_discovery_secret("home", "ems", "value-b")
        thread_b = threading.Thread(target=run, args=("b",))
        thread_b.start()
        _release_a_when_b_progressed(transaction, waiting_b, thread_b, release_a)
        thread_a.join(WAIT_S)
        thread_b.join(WAIT_S)
        assert not thread_a.is_alive() and not thread_b.is_alive()

        status_a, payload_a = results["a"]
        status_b, payload_b = results["b"]
        assert status_a == 500 and payload_a.get("ok") is False, payload_a
        assert status_b == 200 and payload_b.get("ok") is True, payload_b

        # The final state is B's complete successful transaction: A's failed
        # apply must not have restored its own snapshot over B's rotation.
        _, secrets_dir = _paths(tmp_path)
        resolved = FileMqttCredentialResolver(secrets_dir).resolve("home")
        assert resolved.password == "value-b"
    finally:
        srv.shutdown()
        srv.server_close()


def test_parallel_setup_credential_creation_cannot_overlap(tmp_path):
    """Two Setup Applies for one workflow no longer meet inside the transaction.

    Setup mutations hold an exclusive lifecycle claim on their workflow, so the
    second request is refused before it stages anything instead of interleaving
    with the first one's rollback. The rollback itself must still leave the
    credential store consistent, which the sequential retry proves.
    """

    discovery = _discovery_with_proposal()
    srv, base = _serve_setup(discovery, tmp_path)
    srv.credential_store.save_mqtt_discovery_secret("home", "user", "password")
    try:
        # One reviewed preview authorizes the retry too: a failed request must
        # not consume the authority the successful one presents.
        body = _authorized(base, {**_write_body(discovery), "overwrite": True})
        entered_a = threading.Event()
        release_a = threading.Event()
        original_apply = srv.config_apply.apply
        first_call = []

        def blocking_then_failing_apply(*args, **kwargs):
            if not first_call:
                first_call.append(True)
                entered_a.set()
                assert release_a.wait(WAIT_S)
                raise OSError("simulated config write failure")
            return original_apply(*args, **kwargs)

        srv.config_apply.apply = blocking_then_failing_apply

        results = {}

        def run(name):
            results[name] = _request(f"{base}/api/setup/config/apply", "POST", body)

        thread_a = threading.Thread(target=run, args=("a",))
        thread_a.start()
        assert entered_a.wait(WAIT_S), f"apply never entered: {results}"
        status_b, payload_b = _request(f"{base}/api/setup/config/apply", "POST", body)
        release_a.set()
        thread_a.join(WAIT_S)
        assert not thread_a.is_alive()

        status_a, payload_a = results["a"]
        assert status_a == 500 and payload_a.get("ok") is False, payload_a
        assert status_b == 409, payload_b
        assert payload_b["error"] == "setup_operation_in_progress"
        assert payload_b["operation"] == "config_apply"

        # The refused request staged nothing, so the retry is the only writer:
        # it produces a config referencing the record, and A's rollback did not
        # delete the record underneath it.
        status_c, payload_c = _request(f"{base}/api/setup/config/apply", "POST", body)
        assert status_c == 200 and payload_c.get("ok") is True, payload_c
        config = json.loads(
            pathlib.Path(payload_c["path"]).read_text(encoding="utf-8")
        )
        assert "home" in json.dumps(config)
        secret = srv.credential_store.load_mqtt_broker_secret("home")
        assert secret is not None and secret.password == "password"
        FileMqttCredentialResolver(srv.credential_store.secrets_dir).resolve("home")
    finally:
        srv.shutdown()
        srv.server_close()


# --- one exact payload is staged, validated and written ---------------------


def _capture_each_serialization(srv):
    """Count serializations and keep each output so the written call is known.

    A marker injected into the payload would invalidate the reviewed preview's
    prepared-config hash, so the proof is byte capture instead: with exactly
    one recorded serialization, written bytes equal to that output prove the
    write used the same serialization credentials were staged for.
    """

    original = srv.config_export.serialize
    outputs = []

    def wrapper(*args, **kwargs):
        payload, preview = original(*args, **kwargs)
        outputs.append(payload)
        return payload, preview

    srv.config_export.serialize = wrapper
    return outputs


def test_setup_apply_serializes_config_once(tmp_path):
    discovery = _discovery_with_proposal()
    srv, base = _serve_setup(discovery, tmp_path)
    srv.credential_store.save_mqtt_discovery_secret("home", "user", "password")
    try:
        body = _authorized(base, {**_write_body(discovery), "overwrite": True})
        outputs = _capture_each_serialization(srv)
        status, payload = _request(f"{base}/api/setup/config/apply", "POST", body)
        assert status == 200 and payload.get("ok") is True, payload
        assert len(outputs) == 1, "setup apply must serialize the target config once"
        # The written bytes are the first (only) serialization, not a later one.
        assert pathlib.Path(payload["path"]).read_bytes() == outputs[0]
    finally:
        srv.shutdown()
        srv.server_close()


def test_setup_write_serializes_config_once(tmp_path):
    discovery = _discovery_with_proposal()
    srv, base = _serve_setup(discovery, tmp_path)
    srv.credential_store.save_mqtt_discovery_secret("home", "user", "password")
    try:
        body = _authorized(base, {**_write_body(discovery), "overwrite": True})
        outputs = _capture_each_serialization(srv)
        status, payload = _request(f"{base}/api/setup/config/write", "POST", body)
        assert status == 200 and payload.get("ok") is True, payload
        assert len(outputs) == 1, "setup write must serialize the target config once"
        assert pathlib.Path(payload["path"]).read_bytes() == outputs[0]
    finally:
        srv.shutdown()
        srv.server_close()


def test_maintenance_external_change_conflicts_and_rolls_back_credentials(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("EMS_INSTALL_DIR", str(tmp_path))
    _write_config(tmp_path, _existing_config())
    fetch = _CloudFetch()
    srv, base = _serve(
        tmp_path, fetch, local_observation=_local_observation(credentials_ref="home")
    )
    srv.credential_store.save_mqtt_discovery_secret("home", "ems", "old-password")
    try:
        proposal = _local_proposal(base)
        status, payload = _maintenance_apply_with_proposal(base, proposal)
        assert status == 200 and payload.get("ok") is True, payload

        config_path, secrets_dir = _paths(tmp_path)
        record_before = (secrets_dir / "mqtt-home.json").read_bytes()
        # The next apply would rotate the record to this new password.
        srv.credential_store.save_mqtt_discovery_secret("home", "ems", "new-password")

        status, loaded = _request(f"{base}/api/admin/maintenance/config")
        assert status == 200 and loaded["status"] == "ok", loaded

        # config.json changes underneath the apply, after credentials stage but
        # right before the atomic write.
        external = {}
        real_apply = srv.config_apply.apply_maintenance

        def racing_apply(payload_bytes, revision, create_backup=True):
            external["bytes"] = config_path.read_bytes() + b"\n"
            config_path.write_bytes(external["bytes"])
            return real_apply(payload_bytes, revision, create_backup)

        srv.config_apply.apply_maintenance = racing_apply
        status, payload = _request(
            f"{base}/api/admin/maintenance/config/apply",
            "POST",
            {"draft": loaded["draft"], "revision": loaded["revision"], "confirm": True},
        )
        assert status == 409, payload
        assert payload.get("ok") is False
        # The external config is preserved and the staged rotation rolled back.
        assert config_path.read_bytes() == external["bytes"]
        assert (secrets_dir / "mqtt-home.json").read_bytes() == record_before
        resolved = FileMqttCredentialResolver(secrets_dir).resolve("home")
        assert resolved.password == "old-password"
    finally:
        srv.shutdown()
        srv.server_close()


def test_setup_and_maintenance_share_prepared_payload_transaction(monkeypatch, tmp_path):
    monkeypatch.setenv("EMS_INSTALL_DIR", str(tmp_path))
    _write_config(tmp_path, _existing_config())
    fetch = _CloudFetch()
    srv, base = _serve(tmp_path, fetch, local_observation=_local_observation())
    try:
        original = srv.config_apply.apply_prepared
        entries = []

        def observed(change, **kwargs):
            entries.append(change)
            return original(change, **kwargs)

        srv.config_apply.apply_prepared = observed

        proposal = _local_proposal(base)
        status, payload = _maintenance_apply_with_proposal(base, proposal)
        assert status == 200 and payload.get("ok") is True, payload
        assert len(entries) == 1, "maintenance apply must use the prepared payload"

        status, payload = _request(
            f"{base}/api/setup/config/apply",
            "POST",
            _authorized(base, {"devices": [], "supported_grid_meter_count": 0}),
        )
        assert status == 200 and payload.get("ok") is True, payload
        assert len(entries) == 2, "setup apply must use the same prepared payload"
    finally:
        srv.shutdown()
        srv.server_close()


def test_setup_and_maintenance_share_one_apply_transaction(monkeypatch, tmp_path):
    monkeypatch.setenv("EMS_INSTALL_DIR", str(tmp_path))
    _write_config(tmp_path, _existing_config())
    fetch = _CloudFetch()
    srv, base = _serve(tmp_path, fetch, local_observation=_local_observation())
    try:
        transaction = getattr(srv.config_apply, "apply_transaction", None)
        assert transaction is not None, (
            "the config apply service must expose the one shared apply "
            "transaction used by Setup and Maintenance"
        )
        entries = []

        @contextmanager
        def observed():
            entries.append(True)
            with transaction():
                yield

        srv.config_apply.apply_transaction = observed

        proposal = _local_proposal(base)
        status, payload = _maintenance_apply_with_proposal(base, proposal)
        assert status == 200 and payload.get("ok") is True, payload
        assert len(entries) == 1, "maintenance apply must run inside the transaction"

        status, payload = _request(
            f"{base}/api/setup/config/apply",
            "POST",
            _authorized(base, {"devices": [], "supported_grid_meter_count": 0}),
        )
        assert status == 200 and payload.get("ok") is True, payload
        assert len(entries) == 2, "setup apply must run inside the same transaction"
    finally:
        srv.shutdown()
        srv.server_close()
