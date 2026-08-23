# SPDX-License-Identifier: AGPL-3.0-or-later
"""Admin deployment must be immutable once the target is resolved.

A tag can move between the moment it is validated and the moment the container
is recreated, and it can move again before a rollback. Everything after digest
resolution therefore has to use ``repository@sha256:...`` — and a failure to
resolve or write that reference must stop the replacement while the current
Admin is still healthy and running.
"""

import json
from pathlib import Path

import pytest

from appliance.agent import AgentError, AgentHandlers
from appliance.operations import (
    STATE_FAILED_RECOVERABLE,
    STATE_FAILED_TERMINAL,
    STATE_SUCCEEDED,
)
from tests.helpers.appliance import (
    ADMIN_CONTAINER,
    ADMIN_REPOSITORY,
    StaticCatalogue,
    build_test_services,
)

pytestmark = [pytest.mark.integration, pytest.mark.simulation, pytest.mark.appliance]


def appliance(tmp_path, *, tag="v1.0.0", variable_tag=True):
    services = build_test_services(tmp_path, catalogue=StaticCatalogue(["v1.1.0", "v1.0.0"]))
    host = services.host
    host.write_deployment(tag=tag, variable_tag=variable_tag)
    host.publish_image(tag)
    host.pull_local(f"{ADMIN_REPOSITORY}:{tag}")
    host.run_container(ADMIN_CONTAINER, f"{ADMIN_REPOSITORY}:{tag}")
    return services


def install(services, **fields):
    handlers = AgentHandlers(services, executor=lambda target: target())
    planned = handlers.dispatch({"operation": "admin.plan_install", **fields})
    handlers.dispatch(
        {
            "operation": "operations.execute",
            "operation_id": planned["operation"]["operation_id"],
            "confirmation_token": planned["confirmation_token"],
        }
    )
    return services.operations.get(planned["operation"]["operation_id"]), planned["plan"]


def compose_image(services):
    from appliance.admin_deployment import read_service_image

    compose = (services.paths.install_root / "docker-compose.admin.yml").read_text(encoding="utf-8")
    return read_service_image(compose, services.config.admin_service)


# --- resolution ------------------------------------------------------------


def test_the_plan_resolves_a_tag_to_a_canonical_digest_reference(tmp_path):
    services = appliance(tmp_path)
    services.host.publish_image("v1.1.0")
    handlers = AgentHandlers(services, executor=lambda target: target())

    plan = handlers.dispatch(
        {"operation": "admin.plan_install", "channel": "exact", "tag": "v1.1.0"}
    )["plan"]

    assert plan["target_digest"].startswith("sha256:")
    assert plan["target_reference"] == f"{ADMIN_REPOSITORY}@{plan['target_digest']}"


def test_an_image_without_a_resolvable_digest_is_refused_before_replacement(tmp_path):
    services = appliance(tmp_path)
    services.host.publish_image("v1.1.0", digest=None)
    services.host.registry[f"{ADMIN_REPOSITORY}:v1.1.0"]["RepoDigests"] = []

    handlers = AgentHandlers(services, executor=lambda target: target())
    with pytest.raises(Exception) as excinfo:
        handlers.dispatch({"operation": "admin.plan_install", "channel": "exact", "tag": "v1.1.0"})

    assert getattr(excinfo.value, "code", "") == "digest_unresolved"
    assert services.host.containers[ADMIN_CONTAINER]["State"]["Running"] is True
    assert services.admin.detect()["version"] == "v1.0.0"


# --- deployment ------------------------------------------------------------


def test_the_deployment_uses_the_immutable_digest_reference(tmp_path):
    services = appliance(tmp_path)
    services.host.publish_image("v1.1.0")

    operation, plan = install(services, channel="exact", tag="v1.1.0")

    assert operation.state == STATE_SUCCEEDED
    assert compose_image(services) == plan["target_reference"]
    assert "@sha256:" in compose_image(services)


def test_a_literal_tag_deployment_is_also_pinned_by_digest(tmp_path):
    services = appliance(tmp_path, variable_tag=False)
    services.host.publish_image("v1.1.0")

    operation, plan = install(services, channel="exact", tag="v1.1.0")

    assert operation.state == STATE_SUCCEEDED
    assert compose_image(services) == plan["target_reference"]


def test_a_moved_tag_cannot_change_what_is_running(tmp_path):
    services = appliance(tmp_path)
    services.host.publish_image("v1.1.0")
    operation, plan = install(services, channel="exact", tag="v1.1.0")
    assert operation.state == STATE_SUCCEEDED
    pinned = compose_image(services)

    # The publisher moves v1.1.0 to a different image.
    services.host.publish_image("v1.1.0", digest="sha256:" + "e" * 64, revision="deadbee")

    assert "@sha256:" in pinned, "the deployment must not reference a mutable tag"
    assert compose_image(services) == pinned
    assert plan["target_digest"] in compose_image(services)


def test_writing_the_digest_reference_failure_aborts_before_stopping_admin(tmp_path):
    services = appliance(tmp_path)
    services.host.publish_image("v1.1.0")
    handlers = AgentHandlers(services, executor=lambda target: target())
    planned = handlers.dispatch(
        {"operation": "admin.plan_install", "channel": "exact", "tag": "v1.1.0"}
    )
    # The compose file becomes unwritable between plan and execution.
    compose = services.paths.install_root / "docker-compose.admin.yml"
    compose.unlink()
    compose.mkdir()

    handlers.dispatch(
        {
            "operation": "operations.execute",
            "operation_id": planned["operation"]["operation_id"],
            "confirmation_token": planned["confirmation_token"],
        }
    )
    operation = services.operations.get(planned["operation"]["operation_id"])

    assert operation.state in (STATE_FAILED_RECOVERABLE, STATE_FAILED_TERMINAL)
    assert services.host.containers[ADMIN_CONTAINER]["State"]["Running"] is True


# --- known-good ------------------------------------------------------------


def test_known_good_records_the_immutable_reference_and_its_metadata(tmp_path):
    services = appliance(tmp_path)
    services.host.publish_image("v1.1.0")
    install(services, channel="exact", tag="v1.1.0")

    entry = services.known_good.current()
    assert entry["admin_version"] == "v1.1.0"
    assert entry["admin_digest"].startswith("sha256:")
    assert entry["admin_reference"] == f"{ADMIN_REPOSITORY}@{entry['admin_digest']}"
    assert entry["architecture"] == "arm64"
    assert entry["oci_source"].startswith("https://github.com/")
    assert entry["oci_revision"]
    assert entry["compose_hash"]
    assert entry["verified_at"]
    assert entry["healthcheck"] == "passed"


def test_rollback_deploys_the_stored_reference_without_resolving_the_tag_again(tmp_path):
    services = appliance(tmp_path)
    services.host.publish_image("v1.1.0")
    install(services, channel="exact", tag="v1.1.0")
    services.operations.acknowledge(services.operations.list()[0].operation_id)
    previous = services.known_good.previous()

    # The old tag now points at a different image; rollback must ignore that.
    services.host.publish_image("v1.0.0", digest="sha256:" + "f" * 64, revision="0000000")

    handlers = AgentHandlers(services, executor=lambda target: target())
    planned = handlers.dispatch({"operation": "admin.plan_rollback"})
    assert planned["plan"]["target"]["admin_reference"] == previous["admin_reference"]

    handlers.dispatch(
        {
            "operation": "operations.execute",
            "operation_id": planned["operation"]["operation_id"],
            "confirmation_token": planned["confirmation_token"],
        }
    )
    operation = services.operations.get(planned["operation"]["operation_id"])

    assert operation.state == STATE_SUCCEEDED
    assert compose_image(services) == previous["admin_reference"]
    assert services.admin.detect()["digest"] == previous["admin_digest"]


def test_rollback_fails_terminally_when_the_stored_image_is_gone(tmp_path):
    services = appliance(tmp_path)
    services.host.publish_image("v1.1.0")
    install(services, channel="exact", tag="v1.1.0")
    services.operations.acknowledge(services.operations.list()[0].operation_id)
    previous = services.known_good.previous()

    services.host.images.pop(previous["admin_reference"], None)
    services.host.registry.pop(previous["admin_reference"], None)

    handlers = AgentHandlers(services, executor=lambda target: target())
    planned = handlers.dispatch({"operation": "admin.plan_rollback"})
    handlers.dispatch(
        {
            "operation": "operations.execute",
            "operation_id": planned["operation"]["operation_id"],
            "confirmation_token": planned["confirmation_token"],
        }
    )
    operation = services.operations.get(planned["operation"]["operation_id"])

    assert operation.state == STATE_FAILED_TERMINAL
    assert operation.error["code"] in ("known_good_image_unavailable", "image_pull_failed")


def rollback(services):
    handlers = AgentHandlers(services, executor=lambda target: target())
    planned = handlers.dispatch({"operation": "admin.plan_rollback"})
    handlers.dispatch(
        {
            "operation": "operations.execute",
            "operation_id": planned["operation"]["operation_id"],
            "confirmation_token": planned["confirmation_token"],
        }
    )
    return services.operations.get(planned["operation"]["operation_id"])


def running_admin(services):
    return services.host.containers[ADMIN_CONTAINER]["State"]["Running"]


def prepared_rollback(tmp_path):
    """An appliance with a previous known-good version and a healthy current one."""

    services = appliance(tmp_path)
    services.host.publish_image("v1.1.0")
    install(services, channel="exact", tag="v1.1.0")
    services.operations.acknowledge(services.operations.list()[0].operation_id)
    assert running_admin(services) is True
    return services


# --- rollback preflight ----------------------------------------------------


def test_a_missing_local_digest_does_not_stop_the_running_admin(tmp_path):
    services = prepared_rollback(tmp_path)
    previous = services.known_good.previous()
    services.host.images.pop(previous["admin_reference"], None)
    services.host.registry.pop(previous["admin_reference"], None)
    before = compose_image(services)

    operation = rollback(services)

    assert operation.state == STATE_FAILED_TERMINAL
    assert operation.error["code"] == "known_good_image_unavailable"
    assert "was not stopped" in operation.error["message"]
    assert operation.result["admin_untouched"] is True
    assert running_admin(services) is True
    assert compose_image(services) == before


def test_an_invalid_stored_reference_does_not_stop_the_running_admin(tmp_path):
    services = prepared_rollback(tmp_path)
    history = services.known_good.entries()
    history[1]["admin_reference"] = f"{ADMIN_REPOSITORY}@sha256:" + "e" * 64
    services.known_good.path.write_text(json.dumps(history), encoding="utf-8")
    before = compose_image(services)

    handlers = AgentHandlers(services, executor=lambda target: target())
    with pytest.raises(Exception) as excinfo:
        handlers.dispatch({"operation": "admin.plan_rollback"})

    assert getattr(excinfo.value, "code", "") == "invalid_known_good_record"
    assert running_admin(services) is True
    assert compose_image(services) == before


def test_a_malformed_stored_digest_is_refused_before_any_mutation(tmp_path):
    services = prepared_rollback(tmp_path)
    history = services.known_good.entries()
    history[1]["admin_digest"] = "latest"
    history[1].pop("admin_reference", None)
    services.known_good.path.write_text(json.dumps(history), encoding="utf-8")

    handlers = AgentHandlers(services, executor=lambda target: target())
    with pytest.raises(Exception) as excinfo:
        handlers.dispatch({"operation": "admin.plan_rollback"})

    assert getattr(excinfo.value, "code", "") == "invalid_known_good_record"
    assert running_admin(services) is True


def test_a_compose_write_failure_does_not_stop_the_running_admin(tmp_path, monkeypatch):
    """A directory mode proves nothing when the test runs as root; a failing
    atomic write is the same failure for every privilege level."""

    from appliance import admin_deployment

    services = prepared_rollback(tmp_path)
    compose = services.paths.install_root / "docker-compose.admin.yml"
    before = compose.read_text(encoding="utf-8")

    real_write = admin_deployment.atomic_write

    def refuse(path, *arguments, **keywords):
        if Path(path) == compose:
            raise OSError(30, "Read-only file system", str(path))
        return real_write(path, *arguments, **keywords)

    monkeypatch.setattr(admin_deployment, "atomic_write", refuse)

    operation = rollback(services)

    assert operation.state == STATE_FAILED_RECOVERABLE, operation.result
    assert operation.result["admin_untouched"] is True
    assert running_admin(services) is True
    assert compose.read_text(encoding="utf-8") == before


def test_a_successful_rollback_uses_the_stored_immutable_digest(tmp_path):
    services = prepared_rollback(tmp_path)
    previous = services.known_good.previous()

    operation = rollback(services)

    assert operation.state == STATE_SUCCEEDED, operation.error
    assert operation.result["digest"] == previous["admin_digest"]
    assert compose_image(services) == previous["admin_reference"]
    assert operation.result["verification"]["digest_matches"] is True


def test_a_moved_tag_cannot_change_what_a_rollback_installs(tmp_path):
    services = prepared_rollback(tmp_path)
    previous = services.known_good.previous()
    # The registry now serves something else under the old tag.
    services.host.publish_image("v1.0.0", digest="sha256:" + "b" * 64, revision="9999999")

    operation = rollback(services)

    assert operation.state == STATE_SUCCEEDED, operation.error
    assert operation.result["digest"] == previous["admin_digest"]
    assert compose_image(services) == previous["admin_reference"]


def test_a_rollback_target_that_fails_health_reports_the_recovery_attempt(tmp_path):
    services = prepared_rollback(tmp_path)
    previous = services.known_good.previous()
    # The stored image is present but its Admin never answers.
    services.host.registry[previous["admin_reference"]]["_healthy"] = False
    services.host.images[previous["admin_reference"]]["_healthy"] = False

    operation = rollback(services)

    assert operation.state == STATE_FAILED_TERMINAL
    assert operation.result["admin_untouched"] is False
    assert "recovery" in operation.result
    assert operation.error["message"].endswith(
        "the previously running Admin was restored"
    ) or operation.error["message"].endswith("the previous Admin could not be restored")


def test_automatic_rollback_after_a_failed_install_uses_the_stored_reference(tmp_path):
    services = appliance(tmp_path)
    services.host.publish_image("v1.1.0", healthy=False)

    operation, _ = install(services, channel="exact", tag="v1.1.0")

    assert operation.state == "rolled_back"
    restored = services.known_good.current()
    assert compose_image(services) == restored["admin_reference"]
    assert services.admin.detect()["healthy"] is True


# --- automatic recovery identity -------------------------------------------


def swap_local_digest(services, reference, digest):
    """Serve a *different* image under an existing local reference.

    A registry that was compromised, a locally re-tagged image or an aborted
    pull all produce the same situation: the reference is present, the version
    label still matches, and the bytes behind it are not the ones that were
    recorded as known good.
    """

    entry = dict(services.host.images[reference])
    entry["_digest"] = digest
    entry["Id"] = "sha256:" + digest.split(":")[1]
    entry["RepoDigests"] = [f"{ADMIN_REPOSITORY}@{digest}"]
    services.host.images[reference] = entry
    services.host.registry[reference] = entry
    return entry


def test_automatic_recovery_refuses_a_matching_version_with_a_foreign_digest(tmp_path):
    services = appliance(tmp_path)
    services.host.publish_image("v1.1.0", healthy=False)
    recorded = services.host.registry[f"{ADMIN_REPOSITORY}:v1.0.0"]["_digest"]
    swap_local_digest(services, f"{ADMIN_REPOSITORY}@{recorded}", "sha256:" + "f" * 64)

    operation, _ = install(services, channel="exact", tag="v1.1.0")

    assert operation.state == STATE_FAILED_TERMINAL, operation.result
    verification = operation.result["verification"]
    assert verification["digest_matches"] is False, verification
    assert "image_mismatch" in verification["failures"], verification


def test_successful_automatic_recovery_verifies_both_version_and_digest(tmp_path):
    services = appliance(tmp_path)
    services.host.publish_image("v1.1.0", healthy=False)

    operation, _ = install(services, channel="exact", tag="v1.1.0")

    assert operation.state == "rolled_back", operation.error
    verification = operation.result["verification"]
    assert verification["digest_matches"] is True, verification
    assert verification["version_matches"] is True, verification


# --- planned target staleness ----------------------------------------------


def test_an_install_target_removed_after_the_plan_does_not_stop_the_admin(tmp_path):
    services = appliance(tmp_path)
    services.host.publish_image("v1.1.0")
    handlers = AgentHandlers(services, executor=lambda target: target())
    planned = handlers.dispatch(
        {"operation": "admin.plan_install", "channel": "exact", "tag": "v1.1.0"}
    )
    reference = planned["plan"]["target_reference"]
    before = compose_image(services)
    services.host.images.pop(reference, None)
    services.host.images.pop(f"{ADMIN_REPOSITORY}:v1.1.0", None)
    services.host.registry.pop(reference, None)
    services.host.registry.pop(f"{ADMIN_REPOSITORY}:v1.1.0", None)

    handlers.dispatch(
        {
            "operation": "operations.execute",
            "operation_id": planned["operation"]["operation_id"],
            "confirmation_token": planned["confirmation_token"],
        }
    )
    operation = services.operations.get(planned["operation"]["operation_id"])

    assert operation.state in (STATE_FAILED_TERMINAL, STATE_FAILED_RECOVERABLE), operation.result
    assert operation.result["admin_untouched"] is True, operation.result
    assert running_admin(services) is True
    assert compose_image(services) == before


def test_a_compose_file_changed_after_the_plan_is_refused_before_interruption(tmp_path):
    services = prepared_rollback(tmp_path)
    handlers = AgentHandlers(services, executor=lambda target: target())
    planned = handlers.dispatch({"operation": "admin.plan_rollback"})

    compose = services.paths.install_root / "docker-compose.admin.yml"
    compose.write_text(compose.read_text(encoding="utf-8") + "\n# edited\n", encoding="utf-8")
    edited = compose.read_text(encoding="utf-8")

    handlers.dispatch(
        {
            "operation": "operations.execute",
            "operation_id": planned["operation"]["operation_id"],
            "confirmation_token": planned["confirmation_token"],
        }
    )
    operation = services.operations.get(planned["operation"]["operation_id"])

    assert operation.state in (STATE_FAILED_TERMINAL, STATE_FAILED_RECOVERABLE), operation.result
    assert operation.result["admin_untouched"] is True, operation.result
    assert operation.error["code"] == "deployment_changed_since_plan", operation.error
    assert running_admin(services) is True
    assert compose.read_text(encoding="utf-8") == edited


def test_a_rollback_digest_is_pulled_before_the_running_admin_is_stopped(tmp_path):
    services = prepared_rollback(tmp_path)
    previous = services.known_good.previous()
    # Present in the registry only: the rollback must prepare it by digest.
    services.host.images.pop(previous["admin_reference"], None)
    services.host.images.pop(f"{ADMIN_REPOSITORY}:v1.0.0", None)
    services.host.calls.clear()

    operation = rollback(services)

    assert operation.state == STATE_SUCCEEDED, operation.error
    order = [call for call in services.host.calls if call[0] == "docker"]
    pulled = next(
        index for index, call in enumerate(order) if call[1][:1] == ("pull",)
    )
    stopped = next(index for index, call in enumerate(order) if call[1][:1] == ("stop",))
    assert pulled < stopped, order


# --- the persisted operation target is not trusted ---------------------------


def corrupt_persisted_target(services, operation_id, **fields):
    """Rewrite the stored target the way a partial write or a downgrade would."""

    record = services.operations.get(operation_id)
    target = dict(record.requested_target)
    target.update(fields)
    services.operations.update_target(operation_id, target)
    return target


def confirm(handlers, operation_id, token):
    """Confirm a plan, tolerating a record that is refused at the confirmation.

    A persisted target that no longer holds is refused before it is confirmed,
    so the caller sees the refusal rather than a finished operation.
    """

    try:
        handlers.dispatch(
            {
                "operation": "operations.execute",
                "operation_id": operation_id,
                "confirmation_token": token,
            }
        )
    except AgentError:
        pass
    return operation_id


def test_a_corrupted_persisted_rollback_target_fails_without_touching_admin(tmp_path):
    services = prepared_rollback(tmp_path)
    handlers = AgentHandlers(services, executor=lambda target: target())
    planned = handlers.dispatch({"operation": "admin.plan_rollback"})
    operation_id = planned["operation"]["operation_id"]
    before = compose_image(services)
    corrupt_persisted_target(services, operation_id, digest="not-a-digest")

    confirm(handlers, operation_id, planned["confirmation_token"])
    operation = services.operations.get(operation_id)

    assert operation.state == STATE_FAILED_TERMINAL, operation.result
    assert operation.result["admin_untouched"] is True, operation.result
    assert operation.error["code"] == "operation_plan_requires_replanning", operation.error
    assert "AttributeError" not in str(operation.error), operation.error
    assert running_admin(services) is True
    assert compose_image(services) == before


def test_a_rollback_target_with_a_mismatched_reference_is_refused(tmp_path):
    services = prepared_rollback(tmp_path)
    handlers = AgentHandlers(services, executor=lambda target: target())
    planned = handlers.dispatch({"operation": "admin.plan_rollback"})
    operation_id = planned["operation"]["operation_id"]
    corrupt_persisted_target(
        services, operation_id, reference=f"{ADMIN_REPOSITORY}@sha256:" + "b" * 64
    )

    confirm(handlers, operation_id, planned["confirmation_token"])
    operation = services.operations.get(operation_id)

    assert operation.state == STATE_FAILED_TERMINAL, operation.result
    assert operation.result["admin_untouched"] is True, operation.result
    assert running_admin(services) is True


def test_a_rollback_target_without_a_reference_requires_replanning(tmp_path):
    """The immutable reference is authority, not a convenience field.

    An older record could derive it from repository and digest. A persisted plan
    may not: what it lost cannot be told apart from what was never there.
    """

    services = prepared_rollback(tmp_path)
    handlers = AgentHandlers(services, executor=lambda target: target())
    planned = handlers.dispatch({"operation": "admin.plan_rollback"})
    operation_id = planned["operation"]["operation_id"]
    corrupt_persisted_target(services, operation_id, reference="")

    confirm(handlers, operation_id, planned["confirmation_token"])
    operation = services.operations.get(operation_id)

    assert operation.state == STATE_FAILED_TERMINAL, operation.error
    assert operation.error["code"] == "operation_plan_requires_replanning", operation.error
    assert operation.result["admin_untouched"] is True, operation.result
    assert running_admin(services) is True


def test_an_environment_file_changed_after_the_plan_is_refused(tmp_path):
    services = appliance(tmp_path)
    services.host.publish_image("v1.1.0")
    handlers = AgentHandlers(services, executor=lambda target: target())
    planned = handlers.dispatch(
        {"operation": "admin.plan_install", "channel": "exact", "tag": "v1.1.0"}
    )
    env_file = services.paths.install_root / ".env.admin"
    env_file.write_text(env_file.read_text(encoding="utf-8") + "EMS_EXTRA=1\n", encoding="utf-8")
    before = compose_image(services)

    handlers.dispatch(
        {
            "operation": "operations.execute",
            "operation_id": planned["operation"]["operation_id"],
            "confirmation_token": planned["confirmation_token"],
        }
    )
    operation = services.operations.get(planned["operation"]["operation_id"])

    assert operation.state in (STATE_FAILED_TERMINAL, STATE_FAILED_RECOVERABLE), operation.result
    assert operation.result["admin_untouched"] is True, operation.result
    assert operation.error["code"] == "deployment_changed_since_plan", operation.error
    assert running_admin(services) is True
    assert compose_image(services) == before


def test_an_environment_file_changed_after_a_rollback_plan_is_refused(tmp_path):
    services = prepared_rollback(tmp_path)
    handlers = AgentHandlers(services, executor=lambda target: target())
    planned = handlers.dispatch({"operation": "admin.plan_rollback"})
    env_file = services.paths.install_root / ".env.admin"
    env_file.write_text(env_file.read_text(encoding="utf-8") + "EMS_EXTRA=1\n", encoding="utf-8")

    handlers.dispatch(
        {
            "operation": "operations.execute",
            "operation_id": planned["operation"]["operation_id"],
            "confirmation_token": planned["confirmation_token"],
        }
    )
    operation = services.operations.get(planned["operation"]["operation_id"])

    assert operation.state in (STATE_FAILED_TERMINAL, STATE_FAILED_RECOVERABLE), operation.result
    assert operation.result["admin_untouched"] is True, operation.result
    assert running_admin(services) is True


# --- an immutable recovery identity is captured before every mutation -------


def test_recovery_uses_the_running_admin_when_no_known_good_exists(tmp_path):
    """A healthy Admin that was never recorded must still be recoverable."""

    services = appliance(tmp_path)
    services.host.publish_image("v1.1.0", healthy=False)
    running_digest = services.admin.detect()["digest"]
    services.known_good.path.unlink(missing_ok=True)

    operation, plan = install(services, channel="exact", tag="v1.1.0")

    assert plan["recovery"]["digest"] == running_digest, plan["recovery"]
    assert operation.state == "rolled_back", operation.error
    verification = operation.result["verification"]
    assert verification["expected_digest"] == running_digest, verification
    assert verification["digest_matches"] is True, verification
    assert verification["api_reachable"] is True, verification


def test_an_unidentifiable_running_admin_blocks_a_transactional_install(tmp_path):
    services = appliance(tmp_path)
    services.host.publish_image("v1.1.0")
    services.host.images[f"{ADMIN_REPOSITORY}:v1.0.0"]["RepoDigests"] = []
    services.host.images[f"{ADMIN_REPOSITORY}:v1.0.0"]["_digest"] = ""

    handlers = AgentHandlers(services, executor=lambda target: target())
    with pytest.raises(Exception) as excinfo:
        handlers.dispatch(
            {"operation": "admin.plan_install", "channel": "exact", "tag": "v1.1.0"}
        )

    assert getattr(excinfo.value, "code", "") == "recovery_identity_unavailable"
    assert running_admin(services) is True


def test_a_fresh_install_without_an_admin_needs_no_recovery_identity(tmp_path):
    services = appliance(tmp_path)
    services.host.containers.pop(ADMIN_CONTAINER)
    services.host.publish_image("v1.1.0")

    handlers = AgentHandlers(services, executor=lambda target: target())
    plan = handlers.dispatch(
        {"operation": "admin.plan_install", "channel": "exact", "tag": "v1.1.0"}
    )["plan"]

    assert plan["recovery"]["admin_present"] is False, plan["recovery"]


def test_recovery_identity_is_captured_even_when_the_admin_is_not_healthy(tmp_path):
    """An Admin that was never recorded as known good is still recoverable."""

    services = appliance(tmp_path)
    services.host.publish_image("v1.1.0", healthy=False)
    running_digest = services.admin.detect()["digest"]
    services.host.containers[ADMIN_CONTAINER]["State"]["Health"]["Status"] = "unhealthy"
    services.known_good.path.unlink(missing_ok=True)

    operation, plan = install(services, channel="exact", tag="v1.1.0")

    assert plan["recovery"]["digest"] == running_digest, plan["recovery"]
    assert plan["recovery"]["healthy"] is False, plan["recovery"]
    assert services.known_good.current() is None or services.known_good.entries()
    verification = operation.result["verification"]
    assert verification["expected_digest"] == running_digest, verification


def test_a_replaced_running_admin_invalidates_the_plan(tmp_path):
    services = appliance(tmp_path)
    services.host.publish_image("v1.1.0")
    services.host.publish_image("v1.0.5")
    services.host.pull_local(f"{ADMIN_REPOSITORY}:v1.0.5")
    handlers = AgentHandlers(services, executor=lambda target: target())
    planned = handlers.dispatch(
        {"operation": "admin.plan_install", "channel": "exact", "tag": "v1.1.0"}
    )
    services.host.run_container(ADMIN_CONTAINER, f"{ADMIN_REPOSITORY}:v1.0.5")

    handlers.dispatch(
        {
            "operation": "operations.execute",
            "operation_id": planned["operation"]["operation_id"],
            "confirmation_token": planned["confirmation_token"],
        }
    )
    operation = services.operations.get(planned["operation"]["operation_id"])

    assert operation.state == STATE_FAILED_TERMINAL, operation.result
    assert operation.result["admin_untouched"] is True, operation.result
    assert operation.error["code"] == "current_admin_changed_since_plan", operation.error
