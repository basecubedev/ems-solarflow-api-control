# SPDX-License-Identifier: AGPL-3.0-or-later
"""Admin Console self-update: identity, planning, pending state, and API gating.

No real Docker daemon is used anywhere: docker access is faked, and the updater
worker is driven with fake pull/compose callables.
"""

import json
import threading
import time
from datetime import datetime, timezone

import pytest

from admin import update_apply
from admin import admin_update as admin_update_module
from admin.admin_update import (
    ADMIN_IMAGE_REPO,
    PENDING_TRANSITION_FILE,
    REASON_CURRENT_UNKNOWN,
    REASON_DIGEST_CHANGED,
    REASON_DIGEST_MATCH,
    REASON_DIGEST_UNKNOWN,
    STAGE_FAILED,
    STAGE_PLANNED,
    STAGE_STARTED,
    STAGE_SUCCEEDED,
    SUPPORTED_TRANSITION_MODES,
    TRANSITION_SCHEMA_VERSION,
    AdminUpdateLauncher,
    AdminUpdateService,
    PendingAdminUpdateStore,
    PendingTransitionStore,
    PendingUpdateStateError,
    TransitionStateError,
    decide_admin_update,
    detect_current_admin_identity,
    make_transition_record,
    resolve_admin_image_target,
    target_admin_image_for_release,
    validate_release_tag,
)
from admin.image_identity import ImageIdentity
from admin.server import ScanRegistry, create_server
from tests.admin_auth_helpers import authenticate, raw_request

pytestmark = pytest.mark.simulation


CURRENT_REF = f"{ADMIN_IMAGE_REPO}:v0.6.2"
TARGET_REF = f"{ADMIN_IMAGE_REPO}:v0.7.0"


class FakeDocker:
    """A DockerCli-shaped double: ready probe, image/container inspect, pull."""

    def __init__(self, images=None, container_image=None, probe_state="ready"):
        self.images = dict(images or {})
        self.container_image = container_image
        self.pulled = []
        self._probe_state = probe_state

    def probe(self):
        return {"state": self._probe_state}

    def inspect_container(self, name):
        if self.container_image:
            return {"image": self.container_image}
        return None

    def inspect_image(self, image_ref):
        return self.images.get(image_ref)

    def pull(self, image, on_progress=None):
        self.pulled.append(image)


def _image(ref, digest):
    return {"image_ref": ref, "digest": digest, "labels": {}}


# --- target image derivation ---------------------------------------------


@pytest.mark.parametrize("tag", ["v0.7.0", "v0.7.0-rc.1", "latest", "main"])
def test_target_admin_image_accepts_release_tags(tag):
    assert target_admin_image_for_release(tag) == f"{ADMIN_IMAGE_REPO}:{tag}"
    assert validate_release_tag(tag) == tag


@pytest.mark.parametrize(
    "bad",
    [
        "ghcr.io/basecubedev/ems-solarflow-admin:v0.7.0",
        "repo/image",
        "v0.7.0; rm -rf /",
        "with space",
        "-leading-dash",
        "",
        "   ",
        # Shell/metacharacter strings without whitespace must also be rejected.
        "v0.7.0;rm",
        "v0.7.0$(id)",
        "v0.7.0|",
        "v0.7.0&&x",
        "../v0.7.0",
    ],
)
def test_target_admin_image_rejects_arbitrary_refs(bad):
    with pytest.raises(ValueError):
        target_admin_image_for_release(bad)
    with pytest.raises(ValueError):
        validate_release_tag(bad)


# --- current identity detection ------------------------------------------


def test_current_identity_from_docker_inspect():
    docker = FakeDocker(
        images={CURRENT_REF: _image(CURRENT_REF, "sha256:aaa")},
        container_image=CURRENT_REF,
    )
    identity, source = detect_current_admin_identity(
        docker=docker, environ={"EMS_ADMIN_CONTAINER_NAME": "ems-solarflow-admin"}
    )
    assert source == "docker_inspect"
    assert identity.image_ref == CURRENT_REF
    assert identity.digest == "sha256:aaa"


def test_current_identity_env_fallback():
    # No container found via Docker, but the compose/installer env is present.
    docker = FakeDocker(images={CURRENT_REF: _image(CURRENT_REF, "sha256:aaa")})
    identity, source = detect_current_admin_identity(
        docker=docker,
        environ={"EMS_ADMIN_IMAGE": ADMIN_IMAGE_REPO, "EMS_ADMIN_TAG": "v0.6.2"},
    )
    assert source == "env"
    assert identity.image_ref == CURRENT_REF
    assert identity.digest == "sha256:aaa"


def test_current_identity_unknown_does_not_crash():
    identity, source = detect_current_admin_identity(docker=None, environ={})
    assert source == "unknown"
    assert identity.image_ref is None
    assert identity.digest is None


# --- update decision -----------------------------------------------------


def test_decide_no_update_when_digests_match():
    current = ImageIdentity(image_ref=CURRENT_REF, digest="sha256:same")
    target = ImageIdentity(image_ref=TARGET_REF, digest="sha256:same")
    decision = decide_admin_update(current, target)
    assert decision.update_required is False
    assert decision.reason == REASON_DIGEST_MATCH
    assert decision.warning is None


def test_decide_update_when_digests_differ():
    current = ImageIdentity(image_ref=CURRENT_REF, digest="sha256:aaa")
    target = ImageIdentity(image_ref=TARGET_REF, digest="sha256:ccc")
    decision = decide_admin_update(current, target)
    assert decision.update_required is True
    assert decision.reason == REASON_DIGEST_CHANGED


def test_decide_uncertain_when_target_digest_unknown():
    current = ImageIdentity(image_ref=CURRENT_REF, digest="sha256:aaa")
    target = ImageIdentity(image_ref=TARGET_REF, digest=None)
    decision = decide_admin_update(current, target)
    assert decision.update_required is True
    assert decision.reason == REASON_DIGEST_UNKNOWN
    assert decision.warning  # requires explicit confirmation


def test_decide_uncertain_when_current_unknown():
    current = ImageIdentity()
    target = ImageIdentity(image_ref=TARGET_REF, digest="sha256:ccc")
    decision = decide_admin_update(current, target)
    assert decision.update_required is True
    assert decision.reason == REASON_CURRENT_UNKNOWN
    assert decision.warning


def test_resolve_target_reads_local_digest():
    docker = FakeDocker(images={TARGET_REF: _image(TARGET_REF, "sha256:ccc")})
    target = resolve_admin_image_target("v0.7.0", docker=docker)
    assert target.image_ref == TARGET_REF
    assert target.digest == "sha256:ccc"


# --- pending state store --------------------------------------------------


def test_pending_state_atomic_write_and_read(tmp_path):
    store = PendingAdminUpdateStore(tmp_path / "state")
    assert store.read() is None  # tolerate a missing file
    state = {"schema_version": 1, "id": "abc", "stage": "planned"}
    store.write(state)
    assert store.path.is_file()
    assert store.read() == state
    store.clear()
    assert store.read() is None


def test_pending_state_corrupt_returns_recovery_error(tmp_path):
    store = PendingAdminUpdateStore(tmp_path / "state")
    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.path.write_text("{not valid json", encoding="utf-8")
    with pytest.raises(PendingUpdateStateError) as excinfo:
        store.read()
    assert excinfo.value.reason == "state_corrupt"
    assert "corrupt" in excinfo.value.message.lower()


def test_pending_state_stores_no_secrets(tmp_path):
    # Sanity guard: the plan record never carries a password field.
    docker = FakeDocker(
        images={
            CURRENT_REF: _image(CURRENT_REF, "sha256:aaa"),
            TARGET_REF: _image(TARGET_REF, "sha256:ccc"),
        },
        container_image=CURRENT_REF,
    )
    store = PendingAdminUpdateStore(tmp_path / "state")
    svc = AdminUpdateService(
        docker=docker,
        store=store,
        environ={"EMS_ADMIN_CONTAINER_NAME": "ems-solarflow-admin"},
        worker_launcher=lambda plan_id: None,
    )
    svc.plan("v0.7.0")
    raw = store.path.read_text(encoding="utf-8").lower()
    assert "password" not in raw
    assert "secret" not in raw


# --- service plan / execute / resume -------------------------------------


def _service(tmp_path, images, container_image=CURRENT_REF, launcher=None, launched=None):
    if launcher is None:
        launched = launched if launched is not None else []
        launcher = launched.append
    docker = FakeDocker(images=images, container_image=container_image)
    store = PendingAdminUpdateStore(tmp_path / "state")
    svc = AdminUpdateService(
        docker=docker,
        store=store,
        environ={"EMS_ADMIN_CONTAINER_NAME": "ems-solarflow-admin"},
        worker_launcher=launcher,
    )
    return svc, store, docker


def test_service_status_reports_unsupported_without_docker(tmp_path):
    store = PendingAdminUpdateStore(tmp_path / "state")
    svc = AdminUpdateService(docker=None, store=store, environ={})
    status = svc.status()
    assert status["ok"] is True
    assert status["supported"] is False
    assert status["reason"] == "docker_unavailable"


def test_service_plan_flags_update_required(tmp_path):
    images = {
        CURRENT_REF: _image(CURRENT_REF, "sha256:aaa"),
        TARGET_REF: _image(TARGET_REF, "sha256:ccc"),
    }
    svc, store, _ = _service(tmp_path, images)
    plan = svc.plan("v0.7.0")
    assert plan["ok"] is True
    assert plan["update_required"] is True
    assert plan["reason"] == REASON_DIGEST_CHANGED
    assert plan["target_admin"]["image_ref"] == TARGET_REF
    # The plan was persisted.
    assert store.read()["id"] == plan["plan_id"]


def test_service_plan_unchanged_when_digest_matches(tmp_path):
    images = {
        CURRENT_REF: _image(CURRENT_REF, "sha256:same"),
        TARGET_REF: _image(TARGET_REF, "sha256:same"),
    }
    svc, _, _ = _service(tmp_path, images)
    plan = svc.plan("v0.7.0")
    assert plan["update_required"] is False
    assert plan["reason"] == REASON_DIGEST_MATCH


def test_service_execute_requires_confirm(tmp_path):
    images = {CURRENT_REF: _image(CURRENT_REF, "sha256:aaa")}
    launched = []
    svc, _, _ = _service(tmp_path, images, launched=launched)
    plan = svc.plan("v0.7.0")
    result = svc.execute(plan["plan_id"], False)
    assert result["ok"] is False
    assert result["error"] == "confirm_required"
    assert launched == []  # no worker started


def test_service_execute_rejects_unknown_plan(tmp_path):
    images = {CURRENT_REF: _image(CURRENT_REF, "sha256:aaa")}
    svc, _, _ = _service(tmp_path, images)
    svc.plan("v0.7.0")
    result = svc.execute("not-the-plan-id", True)
    assert result["ok"] is False
    assert result["error"] == "unknown_plan"


def test_service_execute_starts_worker_and_reconnects(tmp_path):
    images = {
        CURRENT_REF: _image(CURRENT_REF, "sha256:aaa"),
        TARGET_REF: _image(TARGET_REF, "sha256:ccc"),
    }
    launched = []
    svc, store, _ = _service(tmp_path, images, launched=launched)
    plan = svc.plan("v0.7.0")
    result = svc.execute(plan["plan_id"], True)
    assert result["ok"] is True
    assert result["status"] == STAGE_STARTED
    assert result["reconnect"] is True
    assert result["poll_url"] == "/api/admin/auth/status"
    assert launched == [plan["plan_id"]]
    assert store.read()["stage"] == STAGE_STARTED


def test_service_resume_reconciles_started_to_succeeded(tmp_path):
    # After the restart the running Admin now IS the target image; resume must
    # promote the plan to succeeded so the browser can continue the EMS upgrade.
    images = {TARGET_REF: _image(TARGET_REF, "sha256:ccc")}
    svc, store, _ = _service(
        tmp_path, images, container_image=TARGET_REF
    )
    store.write(
        {
            "schema_version": 1,
            "id": "plan-1",
            "stage": STAGE_STARTED,
            "target_release": "v0.7.0",
            "target_admin": {"image_ref": TARGET_REF, "digest": "sha256:ccc"},
            "next_step": "resume_ems_upgrade",
        }
    )
    resume = svc.resume()
    assert resume["ok"] is True
    assert resume["resume_available"] is True
    assert resume["pending"]["stage"] == STAGE_SUCCEEDED
    assert resume["next_step"] == "resume_ems_upgrade"


def test_service_resume_empty_when_no_pending(tmp_path):
    svc, _, _ = _service(tmp_path, {})
    resume = svc.resume()
    assert resume["ok"] is True
    assert resume["pending"] is None
    assert resume["resume_available"] is False


# --- updater worker (admin.update_apply) ---------------------------------


def _seed_started(store, target_ref=TARGET_REF):
    plan_id = "plan-worker"
    store.write(
        {
            "schema_version": 1,
            "id": plan_id,
            "stage": STAGE_STARTED,
            "target_release": "v0.7.0",
            "target_admin": {"image_ref": target_ref, "digest": "sha256:ccc"},
            "next_step": "resume_ems_upgrade",
        }
    )
    return plan_id


def test_worker_success_pulls_updates_compose_and_recreates(tmp_path):
    store = PendingAdminUpdateStore(tmp_path / "state")
    plan_id = _seed_started(store)
    compose = tmp_path / "docker-compose.admin.yml"
    compose.write_text(
        "services:\n  ems-solarflow-admin:\n    image: " + CURRENT_REF + "\n",
        encoding="utf-8",
    )
    docker = FakeDocker()
    recreated = []
    result = update_apply.apply_admin_update(
        plan_id,
        store=store,
        docker=docker,
        environ={
            "EMS_ADMIN_COMPOSE_FILE": str(compose),
            "EMS_ADMIN_CONTAINER_NAME": "ems-solarflow-admin",
        },
        compose_recreate=lambda cf, svc: recreated.append((str(cf), svc)),
        delay_seconds=0,
    )
    assert result["ok"] is True
    assert docker.pulled == [TARGET_REF]
    assert TARGET_REF in compose.read_text(encoding="utf-8")
    assert (tmp_path / "docker-compose.admin.yml.bak").is_file()
    assert recreated == [(str(compose), "ems-solarflow-admin")]
    assert store.read()["stage"] == STAGE_SUCCEEDED


def test_worker_pull_failure_keeps_old_admin(tmp_path):
    store = PendingAdminUpdateStore(tmp_path / "state")
    plan_id = _seed_started(store)
    compose = tmp_path / "docker-compose.admin.yml"
    compose.write_text("image: " + CURRENT_REF, encoding="utf-8")

    class FailingDocker:
        def pull(self, image, on_progress=None):
            raise RuntimeError("registry unreachable")

    def _must_not_recreate(cf, svc):
        raise AssertionError("recreate must not run after a pull failure")

    result = update_apply.apply_admin_update(
        plan_id,
        store=store,
        docker=FailingDocker(),
        environ={"EMS_ADMIN_COMPOSE_FILE": str(compose)},
        compose_recreate=_must_not_recreate,
        delay_seconds=0,
    )
    assert result["ok"] is False
    assert result["error"] == "pull_failed"
    assert store.read()["stage"] == STAGE_FAILED
    # Compose still points at the old image; the old Admin remains valid.
    assert CURRENT_REF in compose.read_text(encoding="utf-8")


def test_worker_updates_env_tag_for_variable_compose(tmp_path):
    store = PendingAdminUpdateStore(tmp_path / "state")
    plan_id = _seed_started(store)
    compose = tmp_path / "docker-compose.admin.yml"
    compose.write_text(
        "image: ghcr.io/basecubedev/ems-solarflow-admin:${EMS_ADMIN_TAG:-latest}\n",
        encoding="utf-8",
    )
    env_file = tmp_path / ".env.admin"
    env_file.write_text("EMS_ADMIN_TAG=latest\n", encoding="utf-8")
    result = update_apply.apply_admin_update(
        plan_id,
        store=store,
        docker=FakeDocker(),
        environ={"EMS_ADMIN_COMPOSE_FILE": str(compose)},
        compose_recreate=lambda cf, svc: None,
        delay_seconds=0,
    )
    assert result["ok"] is True
    assert "EMS_ADMIN_TAG=v0.7.0" in env_file.read_text(encoding="utf-8")
    # The variable-form compose literal is never rewritten.
    assert "${EMS_ADMIN_TAG" in compose.read_text(encoding="utf-8")
    assert not (tmp_path / "docker-compose.admin.yml.bak").exists()


# --- Block 1.3 transactional compose/env updates -------------------------


def _seed_compose(tmp_path, body=None):
    store = PendingAdminUpdateStore(tmp_path / "state")
    plan_id = _seed_started(store)
    compose = tmp_path / "docker-compose.admin.yml"
    compose.write_text(
        body or "services:\n  ems-solarflow-admin:\n    image: " + CURRENT_REF + "\n",
        encoding="utf-8",
    )
    return store, plan_id, compose


def test_env_write_failure_restores_compose_byte_for_byte(tmp_path, monkeypatch):
    # Compose is rewritten first, then the env write fails: the compose file must
    # be rolled back to its exact original bytes so the old Admin stays valid.
    store, plan_id, compose = _seed_compose(tmp_path)
    env_file = tmp_path / ".env.admin"
    env_file.write_text("EMS_ADMIN_TAG=v0.6.2\n", encoding="utf-8")
    original_compose = compose.read_bytes()
    original_env = env_file.read_bytes()

    def _boom_env(*a, **k):
        raise OSError("disk full while writing env")

    monkeypatch.setattr(update_apply, "_write_env_tag", _boom_env)
    result = update_apply.apply_admin_update(
        plan_id,
        store=store,
        docker=FakeDocker(),
        environ={"EMS_ADMIN_COMPOSE_FILE": str(compose)},
        compose_recreate=lambda cf, svc: None,
        delay_seconds=0,
    )
    assert result["ok"] is False
    assert compose.read_bytes() == original_compose
    assert env_file.read_bytes() == original_env
    assert store.read()["stage"] == STAGE_FAILED


def test_recreate_failure_restores_compose_and_env(tmp_path):
    store, plan_id, compose = _seed_compose(tmp_path)
    env_file = tmp_path / ".env.admin"
    env_file.write_text("EMS_ADMIN_TAG=v0.6.2\n", encoding="utf-8")
    original_compose = compose.read_bytes()
    original_env = env_file.read_bytes()

    def _fail_recreate(cf, svc):
        raise update_apply.AdminUpdateApplyError("recreate_failed", "boom")

    result = update_apply.apply_admin_update(
        plan_id,
        store=store,
        docker=FakeDocker(),
        environ={"EMS_ADMIN_COMPOSE_FILE": str(compose)},
        compose_recreate=_fail_recreate,
        delay_seconds=0,
    )
    assert result["ok"] is False
    assert result["error"] == "recreate_failed"
    # Both persistent files are back to the old Admin build.
    assert compose.read_bytes() == original_compose
    assert env_file.read_bytes() == original_env
    assert CURRENT_REF in compose.read_text(encoding="utf-8")


def test_verification_failure_rolls_back(tmp_path):
    store, plan_id, compose = _seed_compose(tmp_path)
    original_compose = compose.read_bytes()
    result = update_apply.apply_admin_update(
        plan_id,
        store=store,
        docker=FakeDocker(),
        environ={"EMS_ADMIN_COMPOSE_FILE": str(compose)},
        compose_recreate=lambda cf, svc: None,
        verify=lambda: False,
        delay_seconds=0,
    )
    assert result["ok"] is False
    assert result["error"] == "verify_failed"
    assert compose.read_bytes() == original_compose
    assert store.read()["stage"] == STAGE_FAILED


def test_unusual_formatting_restored_byte_for_byte(tmp_path):
    # CRLF line endings, tabs, no trailing newline, extra blank lines: rollback
    # must reproduce the raw bytes exactly (never reserialize/normalize).
    weird = (
        "services:\r\n\tems-solarflow-admin:\r\n\t\timage: "
        + CURRENT_REF
        + "\r\n\r\n# trailing comment no newline"
    )
    store, plan_id, compose = _seed_compose(tmp_path, body=weird)
    original = compose.read_bytes()
    assert original.endswith(b"no newline")  # sanity: no trailing newline

    result = update_apply.apply_admin_update(
        plan_id,
        store=store,
        docker=FakeDocker(),
        environ={"EMS_ADMIN_COMPOSE_FILE": str(compose)},
        compose_recreate=lambda cf, svc: (_ for _ in ()).throw(RuntimeError("x")),
        delay_seconds=0,
    )
    assert result["ok"] is False
    assert compose.read_bytes() == original


def test_files_absent_before_update_removed_on_rollback(tmp_path):
    # Variable-driven compose with NO .env.admin present: the update creates the
    # env file; a failed recreate must delete the file that did not exist before.
    body = "image: ghcr.io/basecubedev/ems-solarflow-admin:${EMS_ADMIN_TAG:-latest}\n"
    store, plan_id, compose = _seed_compose(tmp_path, body=body)
    env_file = tmp_path / ".env.admin"
    assert not env_file.exists()

    result = update_apply.apply_admin_update(
        plan_id,
        store=store,
        docker=FakeDocker(),
        environ={"EMS_ADMIN_COMPOSE_FILE": str(compose)},
        compose_recreate=lambda cf, svc: (_ for _ in ()).throw(RuntimeError("x")),
        delay_seconds=0,
    )
    assert result["ok"] is False
    assert not env_file.exists()  # created during update, removed on rollback
    assert not (tmp_path / "docker-compose.admin.yml.bak").exists()


def test_rollback_failure_reports_affected_paths(tmp_path, monkeypatch):
    store, plan_id, compose = _seed_compose(tmp_path)

    def _boom_restore(path, raw):
        raise OSError("cannot restore")

    monkeypatch.setattr(update_apply, "_atomic_write_bytes", _boom_restore)
    result = update_apply.apply_admin_update(
        plan_id,
        store=store,
        docker=FakeDocker(),
        environ={"EMS_ADMIN_COMPOSE_FILE": str(compose)},
        compose_recreate=lambda cf, svc: (_ for _ in ()).throw(RuntimeError("x")),
        delay_seconds=0,
    )
    assert result["ok"] is False
    rollback = result.get("rollback")
    assert isinstance(rollback, list) and rollback
    affected = {entry["path"] for entry in rollback}
    assert str(compose) in affected


def test_rollback_metadata_never_leaks_env_secrets(tmp_path, monkeypatch):
    secret = "SUPER_SECRET_BROKER_PASSWORD_9f3a"
    store, plan_id, compose = _seed_compose(tmp_path)
    env_file = tmp_path / ".env.admin"
    env_file.write_text(f"EMS_ADMIN_TAG=v0.6.2\nBROKER_PASSWORD={secret}\n", encoding="utf-8")
    log_path = tmp_path / "update.log"
    logger = update_apply._FileLogger(log_path)

    result = update_apply.apply_admin_update(
        plan_id,
        store=store,
        docker=FakeDocker(),
        environ={"EMS_ADMIN_COMPOSE_FILE": str(compose)},
        compose_recreate=lambda cf, svc: (_ for _ in ()).throw(RuntimeError("x")),
        log=logger,
        delay_seconds=0,
    )
    assert result["ok"] is False
    import json as _json

    assert secret not in _json.dumps(result)
    assert secret not in log_path.read_text(encoding="utf-8")


# --- HTTP API: auth, CSRF, and gating ------------------------------------


@pytest.fixture()
def admin_update_server(isolated_install_root, tmp_path):
    """A live Admin server with a fake, docker-ready Admin update service.

    The worker launcher is a no-op recorder so /execute never touches real
    Docker; the fake docker reports both current and target images locally so
    planning is deterministic.
    """

    images = {
        CURRENT_REF: _image(CURRENT_REF, "sha256:aaa"),
        TARGET_REF: _image(TARGET_REF, "sha256:ccc"),
    }
    launched = []
    docker = FakeDocker(images=images, container_image=CURRENT_REF)
    store = PendingAdminUpdateStore(tmp_path / "state")
    service = AdminUpdateService(
        docker=docker,
        store=store,
        environ={"EMS_ADMIN_CONTAINER_NAME": "ems-solarflow-admin"},
        worker_launcher=launched.append,
    )
    registry = ScanRegistry(scan_runner=lambda *a, **k: ([], []))
    srv = create_server("127.0.0.1", 0, registry=registry, admin_update=service)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    authenticate(base)
    try:
        yield base, store, launched
    finally:
        srv.shutdown()
        srv.server_close()


def _cookie_only_headers(base):
    from tests.admin_auth_helpers import _CREDENTIALS

    cookie, _csrf = _CREDENTIALS.get(base, ("", None))
    return {"Cookie": cookie} if cookie else {}


def _auth(base, method):
    from tests.admin_auth_helpers import auth_headers

    return auth_headers(base + "/x", method)


def test_admin_update_status_requires_auth(admin_update_server):
    base, _store, _launched = admin_update_server
    status, _headers, _payload = raw_request(
        base + "/api/admin/maintenance/admin-update/status"
    )
    assert status in (401, 403)


def test_admin_update_plan_requires_auth(admin_update_server):
    base, _store, _launched = admin_update_server
    status, _headers, _payload = raw_request(
        base + "/api/admin/maintenance/admin-update/plan",
        method="POST",
        body={"target_release": "v0.7.0"},
    )
    assert status in (401, 403)


def test_admin_update_post_requires_csrf(admin_update_server):
    base, _store, _launched = admin_update_server
    # Valid session cookie but no X-CSRF-Token must be rejected.
    status, _headers, payload = raw_request(
        base + "/api/admin/maintenance/admin-update/plan",
        method="POST",
        body={"target_release": "v0.7.0"},
        headers=_cookie_only_headers(base),
    )
    assert status == 403
    assert payload.get("error") == "csrf_failed"


def test_admin_update_status_ok_when_authenticated(admin_update_server):
    base, _store, _launched = admin_update_server
    status, _headers, payload = raw_request(
        base + "/api/admin/maintenance/admin-update/status",
        headers=_auth(base, "GET"),
    )
    assert status == 200
    assert payload["ok"] is True
    assert payload["supported"] is True
    assert payload["current_admin"]["image_ref"] == CURRENT_REF


def test_admin_update_plan_rejects_arbitrary_image_ref(admin_update_server):
    base, _store, _launched = admin_update_server
    status, _headers, payload = raw_request(
        base + "/api/admin/maintenance/admin-update/plan",
        method="POST",
        body={"target_release": "ghcr.io/evil/image:latest"},
        headers=_auth(base, "POST"),
    )
    assert status == 400
    assert payload["error"] == "invalid_release"


def test_admin_update_execute_requires_confirm(admin_update_server):
    base, _store, _launched = admin_update_server
    _s, _h, plan = raw_request(
        base + "/api/admin/maintenance/admin-update/plan",
        method="POST",
        body={"target_release": "v0.7.0"},
        headers=_auth(base, "POST"),
    )
    status, _headers, payload = raw_request(
        base + "/api/admin/maintenance/admin-update/execute",
        method="POST",
        body={"plan_id": plan["plan_id"], "confirm": False},
        headers=_auth(base, "POST"),
    )
    assert status == 400
    assert payload["error"] == "confirm_required"


def test_admin_update_execute_returns_update_started_before_replacement(
    admin_update_server,
):
    base, _store, launched = admin_update_server
    _s, _h, plan = raw_request(
        base + "/api/admin/maintenance/admin-update/plan",
        method="POST",
        body={"target_release": "v0.7.0"},
        headers=_auth(base, "POST"),
    )
    status, _headers, payload = raw_request(
        base + "/api/admin/maintenance/admin-update/execute",
        method="POST",
        body={"plan_id": plan["plan_id"], "confirm": True},
        headers=_auth(base, "POST"),
    )
    assert status == 202
    assert payload["status"] == STAGE_STARTED
    assert payload["reconnect"] is True
    # The disruptive work is handed to the worker, which ran before replacing us.
    assert launched == [plan["plan_id"]]


def test_admin_update_resume_endpoint(admin_update_server):
    base, store, _launched = admin_update_server
    # No pending update yet.
    status, _headers, payload = raw_request(
        base + "/api/admin/maintenance/admin-update/resume",
        headers=_auth(base, "GET"),
    )
    assert status == 200
    assert payload["resume_available"] is False


def test_admin_update_resume_recovers_from_corrupt_state(admin_update_server):
    base, store, _launched = admin_update_server
    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.path.write_text("{corrupt", encoding="utf-8")
    status, _headers, payload = raw_request(
        base + "/api/admin/maintenance/admin-update/resume",
        headers=_auth(base, "GET"),
    )
    assert status == 200
    assert payload["ok"] is False
    assert payload["error"] == "state_corrupt"


# --- release catalogue validation (Fix 5) --------------------------------


class FakeReleaseCatalogue:
    """A ReleaseManager double exposing only the upgrade release listing."""

    def __init__(self, releases, data_dir=None):
        self._releases = releases
        self.data_dir = data_dir

    def list_releases(self, *, for_upgrade=True):
        return {"releases": list(self._releases)}


def _catalogue_service(tmp_path, releases):
    docker = FakeDocker(
        images={
            CURRENT_REF: _image(CURRENT_REF, "sha256:aaa"),
            TARGET_REF: _image(TARGET_REF, "sha256:ccc"),
        },
        container_image=CURRENT_REF,
    )
    store = PendingAdminUpdateStore(tmp_path / "state")
    return AdminUpdateService(
        docker=docker,
        release_manager=FakeReleaseCatalogue(releases, data_dir=tmp_path),
        store=store,
        environ={"EMS_ADMIN_CONTAINER_NAME": "ems-solarflow-admin"},
        worker_launcher=lambda plan_id: None,
    )


def test_plan_rejects_unknown_release_when_catalogue_present(tmp_path):
    svc = _catalogue_service(tmp_path, [{"tag": "v0.7.0", "selectable": True}])
    with pytest.raises(ValueError):
        svc.plan("v9.9.9")  # syntactically valid, absent from the catalogue


def test_plan_accepts_selectable_known_release(tmp_path):
    svc = _catalogue_service(tmp_path, [{"tag": "v0.7.0", "selectable": True}])
    plan = svc.plan("v0.7.0")
    assert plan["ok"] is True


def test_plan_rejects_non_selectable_release(tmp_path):
    svc = _catalogue_service(tmp_path, [{"tag": "v0.7.0", "selectable": False}])
    with pytest.raises(ValueError):
        svc.plan("v0.7.0")


def test_plan_syntax_only_without_catalogue(tmp_path):
    # No release manager: keep the syntax-only behavior (no catalogue lookup).
    images = {
        CURRENT_REF: _image(CURRENT_REF, "sha256:aaa"),
        TARGET_REF: _image(TARGET_REF, "sha256:ccc"),
    }
    svc, _store, _docker = _service(tmp_path, images)
    assert svc.plan("v0.7.0")["ok"] is True


# --- execute refuses a no-op plan (Fix 6) --------------------------------


def test_execute_refuses_when_update_not_required(tmp_path):
    # Digests match -> update_required False -> execute must not launch the worker.
    images = {
        CURRENT_REF: _image(CURRENT_REF, "sha256:same"),
        TARGET_REF: _image(TARGET_REF, "sha256:same"),
    }
    launched = []
    svc, _store, _docker = _service(tmp_path, images, launched=launched)
    plan = svc.plan("v0.7.0")
    assert plan["update_required"] is False
    result = svc.execute(plan["plan_id"], True)
    assert result["ok"] is False
    assert result["error"] == "admin_update_not_required"
    assert launched == []  # no updater started


# --- EMS-upgrade gate (Fix 1) --------------------------------------------


def test_ems_gate_allows_when_update_not_required(tmp_path):
    images = {
        CURRENT_REF: _image(CURRENT_REF, "sha256:same"),
        TARGET_REF: _image(TARGET_REF, "sha256:same"),
    }
    svc, _store, _docker = _service(tmp_path, images)
    gate = svc.ems_upgrade_allowed("v0.7.0")
    assert gate["allowed"] is True
    assert gate["reason"] == "admin_update_not_required"


def test_ems_gate_allows_when_pending_same_target_update_not_required(tmp_path):
    # A persisted no-op plan (digests match) must not block the EMS upgrade even
    # though its stage is only "planned".
    images = {
        CURRENT_REF: _image(CURRENT_REF, "sha256:same"),
        TARGET_REF: _image(TARGET_REF, "sha256:same"),
    }
    svc, _store, _docker = _service(tmp_path, images)
    plan = svc.plan("v0.7.0")
    assert plan["update_required"] is False

    gate = svc.ems_upgrade_allowed("v0.7.0")

    assert gate["allowed"] is True
    assert gate["reason"] == "admin_update_not_required"


def test_ems_gate_warns_when_update_required(tmp_path):
    # A newer Admin image is recommended, not required: the EMS upgrade may still
    # proceed with a warning.
    images = {
        CURRENT_REF: _image(CURRENT_REF, "sha256:aaa"),
        TARGET_REF: _image(TARGET_REF, "sha256:ccc"),
    }
    svc, _store, _docker = _service(tmp_path, images)
    gate = svc.ems_upgrade_allowed("v0.7.0")
    assert gate["allowed"] is True
    assert gate["severity"] == "warning"
    assert gate["reason"] == "admin_update_recommended"


def test_ems_gate_allows_when_pending_same_target_succeeded(tmp_path):
    images = {TARGET_REF: _image(TARGET_REF, "sha256:ccc")}
    svc, store, _docker = _service(tmp_path, images, container_image=TARGET_REF)
    store.write(
        {
            "schema_version": 1,
            "id": "plan-1",
            "stage": STAGE_SUCCEEDED,
            "target_release": "v0.7.0",
            "target_admin": {"image_ref": TARGET_REF, "digest": "sha256:ccc"},
        }
    )
    gate = svc.ems_upgrade_allowed("v0.7.0")
    assert gate["allowed"] is True
    assert gate["reason"] == "admin_update_completed"


def test_ems_gate_warns_when_pending_target_differs(tmp_path):
    images = {
        CURRENT_REF: _image(CURRENT_REF, "sha256:aaa"),
        TARGET_REF: _image(TARGET_REF, "sha256:ccc"),
    }
    svc, store, _docker = _service(tmp_path, images)
    # A succeeded update for a *different* release does not authorize this one.
    store.write(
        {
            "schema_version": 1,
            "id": "plan-other",
            "stage": STAGE_SUCCEEDED,
            "target_release": "v0.9.9",
            "target_admin": {"image_ref": f"{ADMIN_IMAGE_REPO}:v0.9.9"},
        }
    )
    gate = svc.ems_upgrade_allowed("v0.7.0")
    # The differing release does not match this tag, so the gate falls back to
    # image identity (aaa != ccc): recommend an update, do not block.
    assert gate["allowed"] is True
    assert gate["severity"] == "warning"
    assert gate["reason"] == "admin_update_recommended"


def test_ems_gate_warns_when_pending_same_target_planned(tmp_path):
    # A planned (not yet running) Admin update recommends, but does not block.
    images = {
        CURRENT_REF: _image(CURRENT_REF, "sha256:aaa"),
        TARGET_REF: _image(TARGET_REF, "sha256:ccc"),
    }
    svc, store, _docker = _service(tmp_path, images)
    store.write(
        {
            "schema_version": 1,
            "id": "plan-p",
            "stage": STAGE_PLANNED,
            "target_release": "v0.7.0",
            "target_admin": {"image_ref": TARGET_REF, "digest": "sha256:ccc"},
        }
    )
    gate = svc.ems_upgrade_allowed("v0.7.0")
    assert gate["allowed"] is True
    assert gate["severity"] == "warning"
    assert gate["reason"] == "admin_update_recommended"


def test_ems_gate_blocks_when_pending_same_target_running(tmp_path):
    # An actively running Admin self-update must not overlap the EMS upgrade.
    images = {
        CURRENT_REF: _image(CURRENT_REF, "sha256:aaa"),
        TARGET_REF: _image(TARGET_REF, "sha256:ccc"),
    }
    svc, store, _docker = _service(tmp_path, images)
    store.write(
        {
            "schema_version": 1,
            "id": "plan-r",
            "stage": STAGE_STARTED,
            "target_release": "v0.7.0",
            "target_admin": {"image_ref": TARGET_REF, "digest": "sha256:ccc"},
        }
    )
    gate = svc.ems_upgrade_allowed("v0.7.0")
    assert gate["allowed"] is False
    assert gate["error"] == "admin_update_in_progress"


def test_ems_gate_warns_when_docker_unavailable(tmp_path):
    store = PendingAdminUpdateStore(tmp_path / "state")
    svc = AdminUpdateService(docker=None, store=store, environ={})
    gate = svc.ems_upgrade_allowed("v0.7.0")
    assert gate["allowed"] is True
    assert gate["severity"] == "warning"
    assert gate["reason"] == "admin_update_unavailable"


def test_ems_gate_rejects_invalid_release(tmp_path):
    images = {CURRENT_REF: _image(CURRENT_REF, "sha256:aaa")}
    svc, _store, _docker = _service(tmp_path, images)
    gate = svc.ems_upgrade_allowed("v0.7.0;rm")
    assert gate["allowed"] is False
    assert gate["error"] == "invalid_release"


# --- updater launcher: sidecar by default (Fix 3) ------------------------


class _FakeStore:
    def __init__(self, pending):
        self._pending = pending
        self.state_dir = "/tmp/state"

    def read(self):
        return self._pending


def _launcher_env():
    return {
        "EMS_INSTALL_DIR": "/opt/ems",
        "EMS_ADMIN_DATA_DIR": "/opt/ems/data/admin",
        "EMS_ADMIN_COMPOSE_FILE": "/opt/ems/docker-compose.admin.yml",
        "EMS_ADMIN_COMPOSE_SERVICE": "ems-solarflow-admin",
        "EMS_ADMIN_CONTAINER_NAME": "ems-solarflow-admin",
        # The currently running Admin image (installer-stamped, non-secret). The
        # updater sidecar runs from THIS build, never the target tag.
        "EMS_ADMIN_IMAGE": ADMIN_IMAGE_REPO,
        "EMS_ADMIN_TAG": "v0.6.2",
        # Host permission metadata mirrored from the normal Admin container.
        "PUID": "1000",
        "PGID": "1000",
        "DOCKER_GID": "999",
    }


def test_launcher_builds_docker_run_argv_no_shell():
    pending = {
        "id": "abc123",
        "target_release": "v0.7.0",
        "target_admin": {"image_ref": TARGET_REF},
    }
    launcher = AdminUpdateLauncher(store=_FakeStore(pending), environ=_launcher_env())
    argv = launcher.build_sidecar_argv("abc123")
    assert isinstance(argv, list) and all(isinstance(a, str) for a in argv)
    assert argv[:3] == ["docker", "run", "--rm"]
    assert "--name" in argv and "ems-admin-updater-abc123" in argv
    # The sidecar runs from the CURRENT Admin build, never the target tag, so the
    # updater understands the pending-state format it was handed.
    assert CURRENT_REF in argv
    assert TARGET_REF not in argv
    assert not any(";" in a or "&&" in a or "|" in a for a in argv)
    # Mounts for the install root and Admin data dir (same-path).
    assert "/opt/ems:/opt/ems" in argv
    assert "/opt/ems/data/admin:/opt/ems/data/admin" in argv
    assert "/var/run/docker.sock:/var/run/docker.sock" in argv
    # Compose service passed distinctly from the container name.
    assert "EMS_ADMIN_COMPOSE_SERVICE=ems-solarflow-admin" in argv
    assert "EMS_ADMIN_CONTAINER_NAME=ems-solarflow-admin" in argv
    # Runs the module updater with the plan id and no delay.
    assert argv[-7:] == [
        "python", "-m", "admin.update_apply",
        "--plan-id", "abc123", "--delay-seconds", "0",
    ]


# --- Block 1.1 host-safe sidecar permissions -----------------------------


def _adjacent(argv, flag):
    """Return the value that immediately follows ``flag`` in ``argv``."""

    for index, item in enumerate(argv[:-1]):
        if item == flag:
            return argv[index + 1]
    return None


def test_sidecar_argv_runs_as_host_uid_gid():
    launcher = AdminUpdateLauncher(store=_FakeStore({}), environ=_launcher_env())
    argv = launcher.build_sidecar_argv("abc123")
    assert _adjacent(argv, "--user") == "1000:1000"


def test_sidecar_argv_adds_docker_socket_group():
    launcher = AdminUpdateLauncher(store=_FakeStore({}), environ=_launcher_env())
    argv = launcher.build_sidecar_argv("abc123")
    assert _adjacent(argv, "--group-add") == "999"


def test_sidecar_derives_docker_gid_from_socket_when_env_absent():
    env = dict(_launcher_env())
    env.pop("DOCKER_GID")
    launcher = AdminUpdateLauncher(
        store=_FakeStore({}),
        environ=env,
        socket_stat=lambda path: type("S", (), {"st_gid": 4242})(),
    )
    argv = launcher.build_sidecar_argv("abc123")
    assert _adjacent(argv, "--group-add") == "4242"


def test_sidecar_fails_on_missing_uid():
    env = dict(_launcher_env())
    env.pop("PUID")
    launcher = AdminUpdateLauncher(store=_FakeStore({}), environ=env)
    with pytest.raises(RuntimeError, match="PUID"):
        launcher.build_sidecar_argv("abc123")


@pytest.mark.parametrize("bad", ["root", "-1", "0", "1000; rm -rf /", "10 00", ""])
def test_sidecar_fails_on_invalid_uid(bad):
    env = dict(_launcher_env(), PUID=bad)
    launcher = AdminUpdateLauncher(store=_FakeStore({}), environ=env)
    with pytest.raises(RuntimeError, match="PUID"):
        launcher.build_sidecar_argv("abc123")


def test_sidecar_fails_on_root_uid():
    # Root (0) is rejected for the sidecar file owner, independent of the host env.
    env = dict(_launcher_env(), PUID="0")
    launcher = AdminUpdateLauncher(store=_FakeStore({}), environ=env)
    with pytest.raises(RuntimeError, match="PUID"):
        launcher.build_sidecar_argv("abc123")


def test_sidecar_fails_on_root_gid():
    env = dict(_launcher_env(), PGID="0")
    launcher = AdminUpdateLauncher(store=_FakeStore({}), environ=env)
    with pytest.raises(RuntimeError, match="PGID"):
        launcher.build_sidecar_argv("abc123")


def test_sidecar_fails_when_docker_gid_unresolvable():
    env = dict(_launcher_env())
    env.pop("DOCKER_GID")

    def _boom(path):
        raise OSError("no socket")

    launcher = AdminUpdateLauncher(
        store=_FakeStore({}), environ=env, socket_stat=_boom
    )
    with pytest.raises(RuntimeError, match="[Dd]ocker"):
        launcher.build_sidecar_argv("abc123")


def test_sidecar_docker_socket_path_is_fixed_and_validated():
    launcher = AdminUpdateLauncher(store=_FakeStore({}), environ=_launcher_env())
    argv = launcher.build_sidecar_argv("abc123")
    # Exactly one docker.sock mount, at the fixed canonical host path.
    socket_mounts = [a for a in argv if "docker.sock" in a]
    assert socket_mounts == ["/var/run/docker.sock:/var/run/docker.sock"]


def test_sidecar_host_compose_paths_stay_host_visible():
    launcher = AdminUpdateLauncher(store=_FakeStore({}), environ=_launcher_env())
    argv = launcher.build_sidecar_argv("abc123")
    # The compose file lives under the same-path install root mount, so the
    # daemon sees a real host path (never a container-only path).
    compose = _adjacent_env(argv, "EMS_ADMIN_COMPOSE_FILE")
    assert compose == "/opt/ems/docker-compose.admin.yml"
    assert compose.startswith("/opt/ems/")


def _adjacent_env(argv, key):
    for item in argv:
        if item.startswith(f"{key}="):
            return item.split("=", 1)[1]
    return None


# --- Block 1.2 updater runs from the current Admin build -----------------


def test_launcher_runs_from_current_build_and_validates_target():
    pending = {
        "id": "p1",
        "target_release": "v0.7.0",
        "target_admin": {"image_ref": TARGET_REF},
    }
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        return type("R", (), {"returncode": 0, "stderr": ""})()

    launcher = AdminUpdateLauncher(
        store=_FakeStore(pending), environ=_launcher_env(), run=fake_run
    )
    launcher.launch("p1")
    assert calls and calls[0][:2] == ["docker", "run"]
    # The container image is the current Admin build, not the target tag.
    assert CURRENT_REF in calls[0]
    assert TARGET_REF not in calls[0]


def test_launcher_rejects_missing_target_image():
    pending = {"id": "p1", "target_release": None, "target_admin": {}}
    env = dict(_launcher_env())
    launcher = AdminUpdateLauncher(
        store=_FakeStore(pending), environ=env, run=lambda *a, **k: None
    )
    with pytest.raises(ValueError):
        launcher.launch("p1")


def test_launcher_fails_when_current_build_unknown():
    # Without a detectable current Admin image the sidecar has no safe base.
    env = dict(_launcher_env())
    env.pop("EMS_ADMIN_IMAGE")
    launcher = AdminUpdateLauncher(store=_FakeStore({}), environ=env)
    with pytest.raises(RuntimeError, match="[Cc]urrent Admin"):
        launcher.build_sidecar_argv("abc123")


def test_launcher_rejects_relative_install_dir():
    # A relative install root would produce an unsafe/invalid same-path -v mount.
    env = dict(_launcher_env(), EMS_INSTALL_DIR="relative/ems")
    launcher = AdminUpdateLauncher(store=_FakeStore({}), environ=env)
    with pytest.raises(RuntimeError, match="EMS_INSTALL_DIR"):
        launcher.build_sidecar_argv("abc123")


def test_launcher_rejects_relative_admin_data_dir():
    env = dict(_launcher_env(), EMS_ADMIN_DATA_DIR="relative/data/admin")
    launcher = AdminUpdateLauncher(store=_FakeStore({}), environ=env)
    with pytest.raises(RuntimeError, match="EMS_ADMIN_DATA_DIR"):
        launcher.build_sidecar_argv("abc123")


def test_sidecar_permissions_match_real_owned_files(tmp_path):
    # Filesystem-level check: with the process's own uid/gid stamped as the host
    # permission metadata and a real compose file owned by that uid/gid, the
    # sidecar argv must run as those ids and point at the real host paths, and
    # the updater must be able to write those files.
    import os

    uid, gid = os.getuid(), os.getgid()
    if uid == 0 or gid == 0:
        # The production sidecar rules reject root ids; a root-run test env
        # (Docker/CI) cannot satisfy them. Root rejection is covered separately
        # by test_sidecar_fails_on_root_uid / test_sidecar_fails_on_root_gid.
        pytest.skip("requires a non-root file owner")
    install_root = tmp_path / "ems"
    install_root.mkdir()
    compose = install_root / "docker-compose.admin.yml"
    compose.write_text(
        "services:\n  ems-solarflow-admin:\n    image: " + CURRENT_REF + "\n",
        encoding="utf-8",
    )
    st = compose.stat()
    assert st.st_uid == uid and st.st_gid == gid  # sanity: we own it
    env = {
        "EMS_INSTALL_DIR": str(install_root),
        "EMS_ADMIN_DATA_DIR": str(install_root / "data" / "admin"),
        "EMS_ADMIN_COMPOSE_FILE": str(compose),
        "EMS_ADMIN_COMPOSE_SERVICE": "ems-solarflow-admin",
        "EMS_ADMIN_CONTAINER_NAME": "ems-solarflow-admin",
        "EMS_ADMIN_IMAGE": ADMIN_IMAGE_REPO,
        "EMS_ADMIN_TAG": "v0.6.2",
        "PUID": str(uid),
        "PGID": str(gid),
        "DOCKER_GID": str(gid),
    }
    launcher = AdminUpdateLauncher(store=_FakeStore({}), environ=env)
    argv = launcher.build_sidecar_argv("plan-fs")
    assert _adjacent(argv, "--user") == f"{uid}:{gid}"
    assert f"{install_root}:{install_root}" in argv

    # The resolved paths are genuinely writable by this uid/gid: apply an update.
    store = PendingAdminUpdateStore(install_root / "data" / "admin" / "state")
    plan_id = _seed_started(store)
    result = update_apply.apply_admin_update(
        plan_id,
        store=store,
        docker=FakeDocker(),
        environ={"EMS_ADMIN_COMPOSE_FILE": str(compose)},
        compose_recreate=lambda cf, svc: None,
        delay_seconds=0,
    )
    assert result["ok"] is True
    assert TARGET_REF in compose.read_text(encoding="utf-8")


def test_launcher_local_worker_only_when_configured(monkeypatch):
    pending = {
        "id": "p1",
        "target_release": "v0.7.0",
        "target_admin": {"image_ref": TARGET_REF},
    }
    worker_calls = []
    monkeypatch.setattr(
        update_apply, "apply_admin_update", lambda **kw: worker_calls.append(kw)
    )
    run_calls = []
    env = dict(_launcher_env(), EMS_ADMIN_UPDATE_LOCAL_WORKER="1")
    launcher = AdminUpdateLauncher(
        store=_FakeStore(pending),
        environ=env,
        run=lambda *a, **k: run_calls.append(a),
    )
    launcher.launch("p1")
    # The sidecar docker run is never used when the local worker is enabled.
    assert run_calls == []
    deadline = time.time() + 2.0
    while not worker_calls and time.time() < deadline:
        time.sleep(0.02)
    assert worker_calls  # the local thread worker ran instead


# --- compose/env tag sync + service name (Fix 8, Fix 9) ------------------


def test_update_reference_syncs_literal_image_and_tags(tmp_path):
    compose = tmp_path / "docker-compose.admin.yml"
    compose.write_text(
        "services:\n"
        "  ems-solarflow-admin:\n"
        f"    image: {CURRENT_REF}\n"
        "    environment:\n"
        '      EMS_ADMIN_TAG: "v0.6.2"\n',
        encoding="utf-8",
    )
    env_file = tmp_path / ".env.admin"
    env_file.write_text("EMS_ADMIN_TAG=v0.6.2\n", encoding="utf-8")
    located = update_apply.update_admin_image_reference(
        compose, TARGET_REF, env_file=env_file
    )
    assert located is True
    text = compose.read_text(encoding="utf-8")
    assert TARGET_REF in text
    assert 'EMS_ADMIN_TAG: "v0.7.0"' in text
    assert "EMS_ADMIN_TAG=v0.7.0" in env_file.read_text(encoding="utf-8")


def test_update_reference_updates_default_env_admin(tmp_path):
    compose = tmp_path / "docker-compose.admin.yml"
    compose.write_text(f"    image: {CURRENT_REF}\n", encoding="utf-8")
    env_file = tmp_path / ".env.admin"
    env_file.write_text("EMS_ADMIN_TAG=v0.6.2\n", encoding="utf-8")
    # No env_file argument: the adjacent .env.admin must still be synced.
    located = update_apply.update_admin_image_reference(compose, TARGET_REF)
    assert located is True
    assert "EMS_ADMIN_TAG=v0.7.0" in env_file.read_text(encoding="utf-8")
    assert TARGET_REF in compose.read_text(encoding="utf-8")


def test_variable_driven_compose_without_env_creates_env_tag(tmp_path):
    # A runtime-template compose points at ${EMS_ADMIN_TAG}. With no .env.admin,
    # the tag would stay at its default; the env file must be created so the
    # update actually takes effect.
    compose = tmp_path / "docker-compose.admin.yml"
    compose.write_text(
        "services:\n"
        "  ems-solarflow-admin:\n"
        "    image: ghcr.io/basecubedev/ems-solarflow-admin:${EMS_ADMIN_TAG:-latest}\n",
        encoding="utf-8",
    )

    assert update_apply.update_admin_image_reference(compose, TARGET_REF) is True
    assert (tmp_path / ".env.admin").read_text(encoding="utf-8") == "EMS_ADMIN_TAG=v0.7.0\n"


def test_resolve_compose_service_prefers_service_env():
    assert (
        update_apply._resolve_compose_service(
            {"EMS_ADMIN_COMPOSE_SERVICE": "custom-svc", "EMS_ADMIN_CONTAINER_NAME": "ctr"}
        )
        == "custom-svc"
    )


def test_resolve_compose_service_ignores_container_name():
    # The container name must never be used as the compose service name.
    assert (
        update_apply._resolve_compose_service({"EMS_ADMIN_CONTAINER_NAME": "custom-ctr"})
        == "ems-solarflow-admin"
    )


def test_worker_recreate_uses_compose_service(tmp_path):
    store = PendingAdminUpdateStore(tmp_path / "state")
    plan_id = _seed_started(store)
    compose = tmp_path / "docker-compose.admin.yml"
    compose.write_text(f"    image: {CURRENT_REF}\n", encoding="utf-8")
    recreated = []
    result = update_apply.apply_admin_update(
        plan_id,
        store=store,
        docker=FakeDocker(),
        environ={
            "EMS_ADMIN_COMPOSE_FILE": str(compose),
            "EMS_ADMIN_COMPOSE_SERVICE": "custom-service",
            "EMS_ADMIN_CONTAINER_NAME": "custom-container",
        },
        compose_recreate=lambda cf, svc: recreated.append(svc),
        delay_seconds=0,
    )
    assert result["ok"] is True
    # Recreate targets the compose service, not the container name.
    assert recreated == ["custom-service"]


# --- Phase 5 staged system-build transition lifecycle --------------------


REVISION = "f7265fc747c2223f126f0ee7801e030c6226edf4"
T0 = datetime(2026, 7, 14, 12, 0, 0, tzinfo=timezone.utc)

TRANSITION_STAGE_ADMIN_UPDATE_PENDING = "admin_update_pending"
TRANSITION_STAGE_ADMIN_RECONNECT_PENDING = "admin_reconnect_pending"
TRANSITION_STAGE_ADMIN_ALIGNED = "admin_aligned"
TRANSITION_STAGE_RESOURCES_VERIFIED = "resources_verified"
TRANSITION_STAGE_EMS_OPERATION_PENDING = "ems_operation_pending"
TRANSITION_STAGE_EMS_OPERATION_RUNNING = "ems_operation_running"
TRANSITION_STAGE_HEALTHCHECK_PENDING = "healthcheck_pending"
TRANSITION_STAGE_COMPLETED = "completed"
TRANSITION_STAGE_FAILED_RECOVERABLE = "failed_recoverable"
TRANSITION_STAGE_CANCELLED = "cancelled"

REQUIRED_TRANSITION_STAGES = frozenset(
    {
        TRANSITION_STAGE_ADMIN_UPDATE_PENDING,
        TRANSITION_STAGE_ADMIN_RECONNECT_PENDING,
        TRANSITION_STAGE_ADMIN_ALIGNED,
        TRANSITION_STAGE_RESOURCES_VERIFIED,
        TRANSITION_STAGE_EMS_OPERATION_PENDING,
        TRANSITION_STAGE_EMS_OPERATION_RUNNING,
        TRANSITION_STAGE_HEALTHCHECK_PENDING,
        TRANSITION_STAGE_COMPLETED,
        TRANSITION_STAGE_FAILED_RECOVERABLE,
        TRANSITION_STAGE_CANCELLED,
    }
)


def _txn_kwargs(**over):
    base = dict(
        mode="guided_upgrade",
        system_tag="v0.8.0",
        build_id="v0.8.0-f7265fc",
        revision=REVISION,
        admin_image=f"{ADMIN_IMAGE_REPO}:v0.8.0",
        admin_digest="sha256:admin-aaa",
        ems_image="ghcr.io/basecubedev/ems-solarflow-api-control:v0.8.0",
        ems_digest="sha256:ems-bbb",
    )
    base.update(over)
    return base


def _running(**over):
    base = dict(
        digest="sha256:admin-aaa", revision=REVISION, build_id="v0.8.0-f7265fc"
    )
    base.update(over)
    return base


def _txn_path(tmp_path):
    return tmp_path / "state" / PENDING_TRANSITION_FILE


def _tamper(tmp_path, **changes):
    path = _txn_path(tmp_path)
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw.update(changes)
    path.write_text(json.dumps(raw), encoding="utf-8")


def test_transition_record_has_bounded_ttl_and_fields():
    record = make_transition_record(now=T0, **_txn_kwargs())
    assert record.state_version == TRANSITION_SCHEMA_VERSION
    assert record.operation_id  # unique id assigned
    assert record.mode in SUPPORTED_TRANSITION_MODES
    assert record.expires_at > record.created_at  # bounded TTL


def test_transition_state_machine_exposes_every_required_stage():
    assert admin_update_module.VALID_TRANSITION_STAGES == REQUIRED_TRANSITION_STAGES


def test_transition_rejects_unknown_stage():
    with pytest.raises(TransitionStateError) as exc:
        make_transition_record(
            now=T0,
            stage="admin_update_pending; completed",
            **_txn_kwargs(),
        )
    assert exc.value.reason == "invalid_stage"


def test_transition_rejects_unsupported_mode():
    with pytest.raises(TransitionStateError) as exc:
        make_transition_record(now=T0, **_txn_kwargs(mode="rm_rf_slash"))
    assert exc.value.reason in {"unsupported_mode", "state_malformed"}


def test_admin_reconnect_advances_to_admin_aligned_idempotently(tmp_path):
    store = PendingTransitionStore(tmp_path / "state")
    pending = store.begin(make_transition_record(now=T0, **_txn_kwargs()), now=T0)
    store.advance(
        pending.operation_id,
        expected_stage=TRANSITION_STAGE_ADMIN_UPDATE_PENDING,
        new_stage=TRANSITION_STAGE_ADMIN_RECONNECT_PENDING,
        now=T0,
    )

    resumed = store.resume_after_admin_reconnect(
        pending.operation_id, running_admin=_running(), now=T0
    )
    assert resumed.stage == TRANSITION_STAGE_ADMIN_ALIGNED
    assert store.read().stage == TRANSITION_STAGE_ADMIN_ALIGNED

    # Reconnect polling is safe: observing the same verified Admin again neither
    # rejects the request nor advances the operation past Admin alignment.
    repeated = store.resume_after_admin_reconnect(
        pending.operation_id, running_admin=_running(), now=T0
    )
    assert repeated == resumed
    assert store.read().stage == TRANSITION_STAGE_ADMIN_ALIGNED


def test_store_allows_only_legal_monotonic_transition_edges(tmp_path):
    store = PendingTransitionStore(tmp_path / "state")
    record = store.begin(make_transition_record(now=T0, **_txn_kwargs()), now=T0)
    operation_id = record.operation_id

    store.advance(
        operation_id,
        expected_stage=TRANSITION_STAGE_ADMIN_UPDATE_PENDING,
        new_stage=TRANSITION_STAGE_ADMIN_RECONNECT_PENDING,
        now=T0,
    )
    store.resume_after_admin_reconnect(
        operation_id, running_admin=_running(), now=T0
    )
    legal_path = (
        (TRANSITION_STAGE_ADMIN_ALIGNED, TRANSITION_STAGE_RESOURCES_VERIFIED),
        (TRANSITION_STAGE_RESOURCES_VERIFIED, TRANSITION_STAGE_EMS_OPERATION_PENDING),
        (TRANSITION_STAGE_EMS_OPERATION_PENDING, TRANSITION_STAGE_EMS_OPERATION_RUNNING),
        (TRANSITION_STAGE_EMS_OPERATION_RUNNING, TRANSITION_STAGE_HEALTHCHECK_PENDING),
        (TRANSITION_STAGE_HEALTHCHECK_PENDING, TRANSITION_STAGE_COMPLETED),
    )
    for expected, target in legal_path:
        advanced = store.advance(
            operation_id,
            expected_stage=expected,
            new_stage=target,
            now=T0,
        )
        assert advanced.stage == target

    assert store.read().stage == TRANSITION_STAGE_COMPLETED


def test_store_rejects_illegal_stage_skip(tmp_path):
    store = PendingTransitionStore(tmp_path / "state")
    record = store.begin(make_transition_record(now=T0, **_txn_kwargs()), now=T0)

    with pytest.raises(TransitionStateError) as exc:
        store.advance(
            record.operation_id,
            expected_stage=TRANSITION_STAGE_ADMIN_UPDATE_PENDING,
            new_stage=TRANSITION_STAGE_HEALTHCHECK_PENDING,
            now=T0,
        )
    assert exc.value.reason == "invalid_transition"
    assert store.read().stage == TRANSITION_STAGE_ADMIN_UPDATE_PENDING


def test_store_repeating_same_stage_advance_is_idempotent(tmp_path):
    store = PendingTransitionStore(tmp_path / "state")
    record = store.begin(make_transition_record(now=T0, **_txn_kwargs()), now=T0)
    first = store.advance(
        record.operation_id,
        expected_stage=TRANSITION_STAGE_ADMIN_UPDATE_PENDING,
        new_stage=TRANSITION_STAGE_ADMIN_RECONNECT_PENDING,
        now=T0,
    )
    second = store.advance(
        record.operation_id,
        expected_stage=TRANSITION_STAGE_ADMIN_UPDATE_PENDING,
        new_stage=TRANSITION_STAGE_ADMIN_RECONNECT_PENDING,
        now=T0,
    )
    assert second == first


def test_idempotent_advance_still_requires_the_declared_legal_edge(tmp_path):
    store = PendingTransitionStore(tmp_path / "state")
    record = store.begin(make_transition_record(now=T0, **_txn_kwargs()), now=T0)
    store.advance(
        record.operation_id,
        expected_stage=TRANSITION_STAGE_ADMIN_UPDATE_PENDING,
        new_stage=TRANSITION_STAGE_ADMIN_RECONNECT_PENDING,
        now=T0,
    )
    with pytest.raises(TransitionStateError) as exc:
        store.advance(
            record.operation_id,
            expected_stage=TRANSITION_STAGE_HEALTHCHECK_PENDING,
            new_stage=TRANSITION_STAGE_ADMIN_RECONNECT_PENDING,
            now=T0,
        )
    assert exc.value.reason == "invalid_transition"


def test_claim_is_atomic_across_independent_store_instances(tmp_path):
    state_dir = tmp_path / "state"
    seeded = PendingTransitionStore(state_dir)
    record = seeded.begin(
        make_transition_record(
            now=T0,
            stage=TRANSITION_STAGE_EMS_OPERATION_PENDING,
            **_txn_kwargs(),
        ),
        now=T0,
    )
    start = threading.Barrier(3)
    results = []

    class SlowReadStore(PendingTransitionStore):
        def _record_locked(self, operation_id=None):
            current = super()._record_locked(operation_id)
            time.sleep(0.1)
            return current

    def attempt_claim():
        store = SlowReadStore(state_dir)
        start.wait()
        results.append(
            store.claim(
                record.operation_id,
                expected_stage=TRANSITION_STAGE_EMS_OPERATION_PENDING,
                new_stage=TRANSITION_STAGE_EMS_OPERATION_RUNNING,
                now=T0,
            )
        )

    workers = [threading.Thread(target=attempt_claim) for _ in range(2)]
    for worker in workers:
        worker.start()
    start.wait()
    for worker in workers:
        worker.join(timeout=5)
        assert not worker.is_alive()

    assert sorted(results) == [False, True]


def test_embedded_resource_import_has_one_durable_claim(tmp_path):
    state_dir = tmp_path / "state"
    store = PendingTransitionStore(state_dir)
    record = store.begin(
        make_transition_record(
            now=T0,
            stage=TRANSITION_STAGE_ADMIN_ALIGNED,
            **_txn_kwargs(),
        ),
        now=T0,
    )

    assert store.claim_resource_verification(record.operation_id, now=T0) is True
    assert (
        PendingTransitionStore(state_dir).claim_resource_verification(
            record.operation_id, now=T0
        )
        is False
    )


@pytest.mark.parametrize(
    "stage",
    (TRANSITION_STAGE_ADMIN_RECONNECT_PENDING, TRANSITION_STAGE_EMS_OPERATION_RUNNING),
)
def test_cancel_rejects_transition_while_external_mutation_is_running(tmp_path, stage):
    store = PendingTransitionStore(tmp_path / "state")
    record = store.begin(
        make_transition_record(now=T0, stage=stage, **_txn_kwargs()), now=T0
    )
    with pytest.raises(TransitionStateError) as exc:
        store.cancel(operation_id=record.operation_id, now=T0)
    assert exc.value.reason == "mutation_in_progress"
    assert store.read().stage == stage


@pytest.mark.parametrize(
    "stage",
    (
        TRANSITION_STAGE_ADMIN_RECONNECT_PENDING,
        TRANSITION_STAGE_EMS_OPERATION_RUNNING,
        TRANSITION_STAGE_HEALTHCHECK_PENDING,
    ),
)
def test_expired_transition_is_cancellable_from_any_stage(tmp_path, stage):
    """Expiry supersedes the mutation-in-progress cancel gate.

    The TTL is the store's own bound on external mutations: once a transition
    has expired, every forward path (resume, claim, restart) refuses with
    ``expired``. Refusing cancel as well would wedge the console permanently —
    no resume, no cancel, and ``begin`` refuses to replace a non-terminal
    record. An expired transition must therefore accept an explicit cancel
    from any non-terminal stage.
    """

    store = PendingTransitionStore(tmp_path / "state")
    record = store.begin(
        make_transition_record(now=T0, ttl_seconds=60, stage=stage, **_txn_kwargs()),
        now=T0,
    )
    later = datetime(2026, 7, 14, 14, 0, 0, tzinfo=timezone.utc)
    cancelled = store.cancel(operation_id=record.operation_id, now=later)
    assert cancelled.stage == TRANSITION_STAGE_CANCELLED
    assert store.read().stage == TRANSITION_STAGE_CANCELLED


def test_expired_transition_cannot_claim_new_ems_mutation(tmp_path):
    store = PendingTransitionStore(tmp_path / "state")
    record = store.begin(
        make_transition_record(
            now=T0,
            ttl_seconds=60,
            stage=TRANSITION_STAGE_EMS_OPERATION_PENDING,
            **_txn_kwargs(),
        ),
        now=T0,
    )
    later = datetime(2026, 7, 14, 13, 0, 0, tzinfo=timezone.utc)
    with pytest.raises(TransitionStateError) as exc:
        store.claim(
            record.operation_id,
            expected_stage=TRANSITION_STAGE_EMS_OPERATION_PENDING,
            new_stage=TRANSITION_STAGE_EMS_OPERATION_RUNNING,
            now=later,
        )
    assert exc.value.reason == "expired"


def test_store_failure_records_recovery_stage_and_retry_is_explicit(tmp_path):
    store = PendingTransitionStore(tmp_path / "state")
    record = make_transition_record(
        now=T0,
        stage=TRANSITION_STAGE_EMS_OPERATION_RUNNING,
        **_txn_kwargs(),
    )
    store.begin(record, now=T0)

    failed = store.mark_failed(
        record.operation_id,
        error_code="ems_deployment_failed",
        error_message="compose up failed",
        resume_stage=TRANSITION_STAGE_EMS_OPERATION_PENDING,
        now=T0,
    )
    assert failed.stage == TRANSITION_STAGE_FAILED_RECOVERABLE
    assert failed.failed_stage == TRANSITION_STAGE_EMS_OPERATION_RUNNING
    assert failed.resume_stage == TRANSITION_STAGE_EMS_OPERATION_PENDING
    assert failed.error_code == "ems_deployment_failed"
    assert failed.error_message == "compose up failed"

    # Merely reading failed state never retries it; an explicit retry returns to
    # the recorded safe stage and can happen only for this operation id.
    assert store.read() == failed
    retried = store.retry(record.operation_id, now=T0)
    assert retried.stage == TRANSITION_STAGE_EMS_OPERATION_PENDING


def test_recoverable_transition_rejects_tampered_resume_stage(tmp_path):
    store = PendingTransitionStore(tmp_path / "state")
    record = make_transition_record(
        now=T0,
        stage=TRANSITION_STAGE_EMS_OPERATION_RUNNING,
        **_txn_kwargs(),
    )
    store.begin(record, now=T0)
    store.mark_failed(
        record.operation_id,
        error_code="ems_deployment_failed",
        error_message="compose up failed",
        resume_stage=TRANSITION_STAGE_EMS_OPERATION_PENDING,
        now=T0,
    )
    _tamper(tmp_path, resume_stage=TRANSITION_STAGE_COMPLETED)

    with pytest.raises(TransitionStateError) as exc:
        store.read()

    assert exc.value.reason == "state_tampered"


def test_v2_admin_update_is_claimed_once_and_replay_is_rejected(
    tmp_path, monkeypatch
):
    store = PendingTransitionStore(tmp_path / "state")
    record = store.begin(
        make_transition_record(
            now=T0,
            stage=TRANSITION_STAGE_ADMIN_RECONNECT_PENDING,
            **_txn_kwargs(),
        ),
        now=T0,
    )
    applied = []

    def fake_apply(plan_id, **kwargs):
        applied.append((plan_id, kwargs))
        return {"ok": True, "status": STAGE_SUCCEEDED}

    monkeypatch.setattr(update_apply, "apply_admin_update", fake_apply)
    first = update_apply.apply_system_transition_admin_update(
        record.operation_id, store=store, delay_seconds=0, now=T0
    )
    replay = update_apply.apply_system_transition_admin_update(
        record.operation_id, store=store, delay_seconds=0, now=T0
    )

    assert first["ok"] is True
    assert replay == {"ok": False, "error": "admin_update_already_claimed"}
    assert len(applied) == 1
    assert applied[0][1]["expected_target_digest"] == record.admin_digest


def test_v2_admin_update_rejects_moved_tag_before_compose_mutation(tmp_path):
    store = PendingTransitionStore(tmp_path / "state")
    record = store.begin(
        make_transition_record(
            now=T0,
            stage=TRANSITION_STAGE_ADMIN_RECONNECT_PENDING,
            **_txn_kwargs(),
        ),
        now=T0,
    )
    compose = tmp_path / "docker-compose.admin.yml"
    compose.write_text("image: " + CURRENT_REF + "\n", encoding="utf-8")
    original = compose.read_bytes()
    docker = FakeDocker(
        images={record.admin_image: _image(record.admin_image, "sha256:moved")}
    )

    result = update_apply.apply_system_transition_admin_update(
        record.operation_id,
        store=store,
        docker=docker,
        environ={"EMS_ADMIN_COMPOSE_FILE": str(compose)},
        compose_recreate=lambda *_args: pytest.fail("must not recreate moved tag"),
        delay_seconds=0,
        now=T0,
    )

    assert result["ok"] is False
    assert result["error"] == "target_digest_mismatch"
    assert compose.read_bytes() == original
    assert store.read().stage == TRANSITION_STAGE_FAILED_RECOVERABLE


def test_transition_status_reads_never_mutate_or_consume_state(tmp_path):
    store = PendingTransitionStore(tmp_path / "state")
    record = make_transition_record(
        now=T0,
        stage=TRANSITION_STAGE_EMS_OPERATION_RUNNING,
        **_txn_kwargs(),
    )
    store.begin(record, now=T0)
    before = _txn_path(tmp_path).read_bytes()

    assert store.read() == record
    assert store.read() == record
    assert _txn_path(tmp_path).read_bytes() == before


def test_transition_expired_rejected(tmp_path):
    store = PendingTransitionStore(tmp_path / "state")
    record = store.begin(
        make_transition_record(now=T0, ttl_seconds=60, **_txn_kwargs()), now=T0
    )
    store.advance(
        record.operation_id,
        expected_stage=TRANSITION_STAGE_ADMIN_UPDATE_PENDING,
        new_stage=TRANSITION_STAGE_ADMIN_RECONNECT_PENDING,
        now=T0,
    )
    later = datetime(2026, 7, 14, 13, 0, 0, tzinfo=timezone.utc)  # +1h
    with pytest.raises(TransitionStateError) as exc:
        store.resume_after_admin_reconnect(
            record.operation_id, running_admin=_running(), now=later
        )
    assert exc.value.reason == "expired"


def test_transition_unknown_state_version_rejected(tmp_path):
    store = PendingTransitionStore(tmp_path / "state")
    store.begin(make_transition_record(now=T0, **_txn_kwargs()), now=T0)
    _tamper(tmp_path, state_version=99)
    with pytest.raises(TransitionStateError) as exc:
        store.read()
    assert exc.value.reason == "unsupported_state_version"


def test_transition_malformed_rejected(tmp_path):
    store = PendingTransitionStore(tmp_path / "state")
    store.begin(make_transition_record(now=T0, **_txn_kwargs()), now=T0)
    path = _txn_path(tmp_path)
    raw = json.loads(path.read_text(encoding="utf-8"))
    del raw["revision"]
    path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(TransitionStateError) as exc:
        store.read()
    assert exc.value.reason == "state_malformed"


def test_transition_tampered_digest_rejected(tmp_path):
    store = PendingTransitionStore(tmp_path / "state")
    record = store.begin(make_transition_record(now=T0, **_txn_kwargs()), now=T0)
    store.advance(
        record.operation_id,
        expected_stage=TRANSITION_STAGE_ADMIN_UPDATE_PENDING,
        new_stage=TRANSITION_STAGE_ADMIN_RECONNECT_PENDING,
        now=T0,
    )
    _tamper(tmp_path, admin_digest="sha256:evil")
    with pytest.raises(TransitionStateError) as exc:
        store.resume_after_admin_reconnect(
            record.operation_id, running_admin=_running(), now=T0
        )
    assert exc.value.reason == "admin_identity_mismatch"


def test_transition_tampered_build_id_rejected(tmp_path):
    store = PendingTransitionStore(tmp_path / "state")
    store.begin(make_transition_record(now=T0, **_txn_kwargs()), now=T0)
    # A build_id that no longer embeds the revision is structurally tampered.
    _tamper(tmp_path, build_id="v9.9.9-deadbee")
    with pytest.raises(TransitionStateError) as exc:
        store.read()
    assert exc.value.reason == "state_tampered"


LEGACY_REVISION = "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0"
LEGACY_BUILD_ID = "123456789-1"


def _legacy_txn_kwargs(**over):
    base = _txn_kwargs(
        system_tag="v0.7.0",
        build_id=LEGACY_BUILD_ID,
        revision=LEGACY_REVISION,
        admin_image=f"{ADMIN_IMAGE_REPO}:v0.7.0",
        ems_image="ghcr.io/basecubedev/ems-solarflow-api-control:v0.7.0",
        mode="fresh_install",
    )
    base.update(over)
    return base


def test_transition_accepts_validated_legacy_ci_build_identity(tmp_path):
    # A legacy CI build id (``<run>-<attempt>``) does not embed a git revision.
    # It must persist and round-trip: its format is validated and the release
    # identity is verified upstream, so the modern revision-embedding rule is not
    # applied to it.
    store = PendingTransitionStore(tmp_path / "state")
    record = make_transition_record(now=T0, **_legacy_txn_kwargs())
    assert record.build_id == LEGACY_BUILD_ID
    store.begin(record, now=T0)
    reread = store.read()
    assert reread.build_id == LEGACY_BUILD_ID
    assert reread.revision == LEGACY_REVISION


def test_transition_tampered_legacy_build_id_rejected(tmp_path):
    # Corrupting the legacy build id to an unrecognized shape is still rejected;
    # the legacy exception never disables build-id format validation.
    store = PendingTransitionStore(tmp_path / "state")
    store.begin(make_transition_record(now=T0, **_legacy_txn_kwargs()), now=T0)
    _tamper(tmp_path, build_id="123456789-1-evil")
    with pytest.raises(TransitionStateError) as exc:
        store.read()
    assert exc.value.reason == "state_malformed"


def test_transition_modern_revision_tamper_still_rejected(tmp_path):
    # The modern revision-embedding integrity check is preserved: swapping the
    # revision on a modern build id so it no longer embeds is structural tampering.
    store = PendingTransitionStore(tmp_path / "state")
    store.begin(make_transition_record(now=T0, **_txn_kwargs()), now=T0)
    _tamper(tmp_path, revision="0" * 40)
    with pytest.raises(TransitionStateError) as exc:
        store.read()
    assert exc.value.reason == "state_tampered"


# --- orchestrator Admin vs selected EMS identity (Phase 6) -----------------

MODERN_ADMIN = dict(
    build_id="v0.8.0-f7265fc",
    revision=REVISION,
    image=f"{ADMIN_IMAGE_REPO}:v0.8.0",
    digest="sha256:modern-admin",
)


def _legacy_with_orchestrator(**over):
    base = _legacy_txn_kwargs(
        compatibility_mode="legacy_release",
        resource_strategy="release_archive",
        orchestrator_admin=dict(MODERN_ADMIN),
    )
    base.update(over)
    return base


def test_modern_record_defaults_orchestrator_to_selected_admin(tmp_path):
    # A modern paired record carries no separate orchestrator block: the running
    # Admin *becomes* the selected Admin, so the orchestrator identity is the
    # selected Admin identity. Old persisted records stay readable this way.
    record = make_transition_record(now=T0, **_txn_kwargs())
    orchestrator = record.orchestrator_admin
    assert orchestrator["build_id"] == "v0.8.0-f7265fc"
    assert orchestrator["revision"] == REVISION
    assert orchestrator["digest"] == "sha256:admin-aaa"
    assert record.compatibility_mode is None  # not set for legacy records


def test_legacy_record_separates_orchestrator_admin_from_selected_ems(tmp_path):
    store = PendingTransitionStore(tmp_path / "state")
    record = make_transition_record(
        now=T0, stage=TRANSITION_STAGE_RESOURCES_VERIFIED,
        **_legacy_with_orchestrator(),
    )
    store.begin(record, now=T0)
    reread = store.read()

    # The orchestrator is the modern Admin; the selected build is the historical
    # EMS. Their identities never collapse into one field.
    assert reread.orchestrator_admin["digest"] == "sha256:modern-admin"
    assert reread.orchestrator_admin["build_id"] == "v0.8.0-f7265fc"
    assert reread.ems_digest == _legacy_txn_kwargs()["ems_digest"]
    assert reread.build_id == LEGACY_BUILD_ID
    assert reread.orchestrator_admin["digest"] != reread.ems_digest
    assert reread.compatibility_mode == "legacy_release"
    assert reread.resource_strategy == "release_archive"
    as_dict = reread.as_dict()
    assert as_dict["orchestrator_admin"]["digest"] == "sha256:modern-admin"
    assert as_dict["selected_ems_build"]["digest"] == reread.ems_digest


def test_resume_matches_orchestrator_admin_not_selected_admin(tmp_path):
    # Resume authorization compares the running Admin to the orchestrator, not to
    # the historical selected Admin image (which is never run for a legacy build).
    record = make_transition_record(
        now=T0, stage=TRANSITION_STAGE_EMS_OPERATION_PENDING,
        **_legacy_with_orchestrator(),
    )
    # The running modern Admin matches the orchestrator identity.
    ok = admin_update_module.validate_transition_for_resume(
        record, now=T0, running_admin=dict(MODERN_ADMIN)
    )
    assert ok is record
    # The historical selected Admin identity must NOT authorize resume.
    with pytest.raises(TransitionStateError) as exc:
        admin_update_module.validate_transition_for_resume(
            record,
            now=T0,
            running_admin={
                "build_id": LEGACY_BUILD_ID,
                "revision": LEGACY_REVISION,
                "digest": "sha256:oldems-admin",
            },
        )
    assert exc.value.reason == "admin_identity_mismatch"


def test_tampered_orchestrator_admin_build_id_rejected(tmp_path):
    store = PendingTransitionStore(tmp_path / "state")
    store.begin(
        make_transition_record(
            now=T0, stage=TRANSITION_STAGE_RESOURCES_VERIFIED,
            **_legacy_with_orchestrator(),
        ),
        now=T0,
    )
    # The orchestrator is modern, so it must still embed its revision.
    _tamper(
        tmp_path,
        orchestrator_admin={
            **MODERN_ADMIN,
            "build_id": "v9.9.9-deadbee",
        },
    )
    with pytest.raises(TransitionStateError) as exc:
        store.read()
    assert exc.value.reason == "state_tampered"


def test_old_record_without_orchestrator_block_remains_readable(tmp_path):
    # A modern record persisted before the orchestrator block existed still reads.
    store = PendingTransitionStore(tmp_path / "state")
    store.begin(make_transition_record(now=T0, **_txn_kwargs()), now=T0)
    # Simulate an old on-disk record: no orchestrator/compat/strategy keys.
    reread = store.read()
    assert reread.orchestrator_admin["digest"] == "sha256:admin-aaa"


def test_transition_wrong_running_admin_rejected(tmp_path):
    store = PendingTransitionStore(tmp_path / "state")
    record = store.begin(make_transition_record(now=T0, **_txn_kwargs()), now=T0)
    store.advance(
        record.operation_id,
        expected_stage=TRANSITION_STAGE_ADMIN_UPDATE_PENDING,
        new_stage=TRANSITION_STAGE_ADMIN_RECONNECT_PENDING,
        now=T0,
    )
    wrong = _running(
        digest="sha256:old", revision="0000000old", build_id="v0.7.0-0000000"
    )
    with pytest.raises(TransitionStateError) as exc:
        store.resume_after_admin_reconnect(
            record.operation_id, running_admin=wrong, now=T0
        )
    assert exc.value.reason == "admin_identity_mismatch"


def test_transition_partial_running_admin_identity_is_unverifiable(tmp_path):
    store = PendingTransitionStore(tmp_path / "state")
    record = store.begin(make_transition_record(now=T0, **_txn_kwargs()), now=T0)
    store.advance(
        record.operation_id,
        expected_stage=TRANSITION_STAGE_ADMIN_UPDATE_PENDING,
        new_stage=TRANSITION_STAGE_ADMIN_RECONNECT_PENDING,
        now=T0,
    )

    with pytest.raises(TransitionStateError) as exc:
        store.resume_after_admin_reconnect(
            record.operation_id,
            running_admin={"revision": REVISION},
            now=T0,
        )

    assert exc.value.reason == "admin_unverifiable"


def test_second_transition_cannot_overwrite_active(tmp_path):
    store = PendingTransitionStore(tmp_path / "state")
    store.begin(make_transition_record(now=T0, **_txn_kwargs()), now=T0)
    with pytest.raises(TransitionStateError) as exc:
        store.begin(
            make_transition_record(now=T0, **_txn_kwargs(mode="fresh_install")),
            now=T0,
        )
    assert exc.value.reason == "transition_active"


def test_cancelled_transition_cannot_resume(tmp_path):
    store = PendingTransitionStore(tmp_path / "state")
    record = store.begin(make_transition_record(now=T0, **_txn_kwargs()), now=T0)
    store.cancel(operation_id=record.operation_id, now=T0)
    with pytest.raises(TransitionStateError) as exc:
        store.resume_after_admin_reconnect(
            record.operation_id, running_admin=_running(), now=T0
        )
    assert exc.value.reason == "not_resumable"


@pytest.mark.parametrize(
    "terminal_stage",
    [TRANSITION_STAGE_COMPLETED, TRANSITION_STAGE_CANCELLED],
)
def test_terminal_transition_cannot_restart(tmp_path, terminal_stage):
    store = PendingTransitionStore(tmp_path / "state")
    record = store.begin(
        make_transition_record(now=T0, stage=terminal_stage, **_txn_kwargs()),
        now=T0,
    )
    with pytest.raises(TransitionStateError) as exc:
        store.advance(
            record.operation_id,
            expected_stage=terminal_stage,
            new_stage=TRANSITION_STAGE_ADMIN_UPDATE_PENDING,
            now=T0,
        )
    assert exc.value.reason == "not_resumable"


def test_expired_active_transition_cannot_be_silently_replaced(tmp_path):
    store = PendingTransitionStore(tmp_path / "state")
    store.begin(
        make_transition_record(now=T0, ttl_seconds=60, **_txn_kwargs()), now=T0
    )
    later = datetime(2026, 7, 14, 14, 0, 0, tzinfo=timezone.utc)
    # Expiry cannot silently discard a potentially partial Admin/EMS pairing.
    replacement = make_transition_record(
        now=later, **_txn_kwargs(mode="fresh_install")
    )
    with pytest.raises(TransitionStateError) as exc:
        store.begin(replacement, now=later)
    assert exc.value.reason == "transition_active"
    assert store.read().mode == "guided_upgrade"


def test_expired_transition_escape_is_explicit_cancel_then_begin(tmp_path):
    """The one escape from an expired transition: cancel it, then begin anew.

    Silent replacement stays refused (see above); the operator-visible cancel
    is the only path that frees the store for a fresh operation.
    """

    store = PendingTransitionStore(tmp_path / "state")
    record = store.begin(
        make_transition_record(
            now=T0,
            ttl_seconds=60,
            stage=TRANSITION_STAGE_ADMIN_RECONNECT_PENDING,
            **_txn_kwargs(),
        ),
        now=T0,
    )
    later = datetime(2026, 7, 14, 14, 0, 0, tzinfo=timezone.utc)
    cancelled = store.cancel(operation_id=record.operation_id, now=later)
    assert cancelled.stage == TRANSITION_STAGE_CANCELLED

    replacement = store.begin(
        make_transition_record(now=later, **_txn_kwargs(mode="fresh_install")),
        now=later,
    )
    assert replacement.stage == TRANSITION_STAGE_ADMIN_UPDATE_PENDING
    assert store.read().mode == "fresh_install"
