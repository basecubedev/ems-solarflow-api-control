# SPDX-License-Identifier: AGPL-3.0-or-later
"""Admin Console self-update: identity, planning, pending state, and API gating.

No real Docker daemon is used anywhere: docker access is faked, and the updater
worker is driven with fake pull/compose callables.
"""

import threading

import pytest

from admin import update_apply
from admin.admin_update import (
    ADMIN_IMAGE_REPO,
    REASON_CURRENT_UNKNOWN,
    REASON_DIGEST_CHANGED,
    REASON_DIGEST_MATCH,
    REASON_DIGEST_UNKNOWN,
    STAGE_FAILED,
    STAGE_STARTED,
    STAGE_SUCCEEDED,
    AdminUpdateService,
    PendingAdminUpdateStore,
    PendingUpdateStateError,
    decide_admin_update,
    detect_current_admin_identity,
    resolve_admin_image_target,
    target_admin_image_for_release,
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


@pytest.mark.parametrize("tag", ["v0.7.0", "latest", "main"])
def test_target_admin_image_accepts_release_tags(tag):
    assert target_admin_image_for_release(tag) == f"{ADMIN_IMAGE_REPO}:{tag}"


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
    ],
)
def test_target_admin_image_rejects_arbitrary_refs(bad):
    with pytest.raises(ValueError):
        target_admin_image_for_release(bad)


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
