# SPDX-License-Identifier: AGPL-3.0-or-later
"""A confirmed Admin operation may only execute on the authority it recorded.

An operation record survives an agent restart, so what it holds is what the
mutation runs on: the target digest, the deployment fingerprints and the
immutable identity of the Admin that has to come back if anything fails. A
record that lost one of those cannot be executed — not with a mutable tag, not
with "the container is running again", and not on the assumption that a missing
hash means no validation was requested.
"""

import json

import pytest

from appliance.agent import AgentError, AgentHandlers
from appliance.operations import STATE_FAILED_TERMINAL
from tests.helpers.appliance import (
    ADMIN_CONTAINER,
    ADMIN_REPOSITORY,
    StaticCatalogue,
    build_test_services,
)

pytestmark = [pytest.mark.integration, pytest.mark.simulation]


def appliance(tmp_path, *, tag="v1.0.0", available=("v1.1.0", "v1.0.0"), catalogue=None):
    services = build_test_services(
        tmp_path, catalogue=catalogue or StaticCatalogue(list(available))
    )
    services.host.write_deployment(tag=tag)
    for release in available:
        services.host.publish_image(release)
    services.host.pull_local(f"{ADMIN_REPOSITORY}:{tag}")
    services.host.run_container(ADMIN_CONTAINER, f"{ADMIN_REPOSITORY}:{tag}")
    return services


def handlers(services):
    return AgentHandlers(services, executor=lambda target: target())


def plan_install(services, **fields):
    return handlers(services).dispatch({"operation": "admin.plan_install", **fields})


def execute(services, planned):
    """Confirm and run a plan, tolerating a refusal at the confirmation itself.

    A record whose authority no longer holds is refused before it is confirmed,
    so the caller sees an error rather than a finished operation. Either way the
    operation record is what says what happened to the Admin.
    """

    try:
        handlers(services).dispatch(
            {
                "operation": "operations.execute",
                "operation_id": planned["operation"]["operation_id"],
                "confirmation_token": planned["confirmation_token"],
            }
        )
    except AgentError:
        pass
    return services.operations.get(planned["operation"]["operation_id"])


def refuse(services, planned):
    """The error a confirmation refuses a corrupted record with."""

    with pytest.raises(AgentError) as excinfo:
        handlers(services).dispatch(
            {
                "operation": "operations.execute",
                "operation_id": planned["operation"]["operation_id"],
                "confirmation_token": planned["confirmation_token"],
            }
        )
    return excinfo.value


def corrupt(services, planned, mutate):
    """Edit the persisted record the way a privileged accident would."""

    path = services.paths.operations_dir / f"{planned['operation']['operation_id']}.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    mutate(payload)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def stopped_admin(services):
    return [
        call
        for call in services.host.calls
        if call[0] == "docker" and ("stop" in call[1] or "down" in call[1])
    ]


# --- an existing Admin always needs an immutable recovery identity ----------


def test_an_unhealthy_admin_without_a_digest_cannot_be_replaced(tmp_path):
    """Health does not weaken the requirement: a replacement must be reversible."""

    services = build_test_services(tmp_path, catalogue=StaticCatalogue(["v1.1.0", "v1.0.0"]))
    services.host.write_deployment(tag="v1.0.0")
    services.host.publish_image("v1.1.0")
    # An Admin whose image is not in the local store any more: it exists, it is
    # unhealthy, and nothing can identify what would have to come back.
    services.host.run_container(
        ADMIN_CONTAINER, f"{ADMIN_REPOSITORY}:v0.9.0", health="unhealthy"
    )

    with pytest.raises(Exception) as excinfo:
        plan_install(services, channel="exact", tag="v1.1.0")

    assert getattr(excinfo.value, "code", "") == "recovery_identity_unavailable", excinfo.value
    assert services.docker.inspect_container(ADMIN_CONTAINER).exists


# --- corrupted persisted plans ----------------------------------------------


@pytest.mark.parametrize(
    "field",
    [
        "compose_hash",
        "environment_hash",
        "compose_file",
        "environment_file",
        "digest",
        "reference",
        "architecture",
    ],
)
def test_a_plan_that_lost_a_required_field_does_not_execute(tmp_path, field):
    services = appliance(tmp_path)
    planned = plan_install(services, channel="exact", tag="v1.1.0")
    before = len(stopped_admin(services))

    corrupt(services, planned, lambda payload: payload["requested_target"].pop(field, None))
    operation = execute(services, planned)

    assert operation.state == STATE_FAILED_TERMINAL, operation.to_dict()
    assert operation.result["admin_untouched"] is True, operation.result
    assert len(stopped_admin(services)) == before, services.host.calls


def test_a_plan_without_a_recovery_identity_does_not_execute(tmp_path):
    services = appliance(tmp_path)
    planned = plan_install(services, channel="exact", tag="v1.1.0")

    corrupt(services, planned, lambda payload: payload["requested_target"].pop("recovery", None))
    operation = execute(services, planned)

    assert operation.state == STATE_FAILED_TERMINAL, operation.to_dict()
    assert operation.result["admin_untouched"] is True, operation.result


def test_a_recovery_identity_without_a_digest_does_not_execute(tmp_path):
    services = appliance(tmp_path)
    planned = plan_install(services, channel="exact", tag="v1.1.0")

    def blank_digest(payload):
        payload["requested_target"]["recovery"]["digest"] = ""

    corrupt(services, planned, blank_digest)
    operation = execute(services, planned)

    assert operation.state == STATE_FAILED_TERMINAL, operation.to_dict()
    assert operation.result["admin_untouched"] is True, operation.result


def test_a_legacy_record_without_a_schema_version_requires_replanning(tmp_path):
    services = appliance(tmp_path)
    planned = plan_install(services, channel="exact", tag="v1.1.0")

    corrupt(services, planned, lambda payload: payload.pop("schema_version", None))
    operation = execute(services, planned)

    assert operation.state == STATE_FAILED_TERMINAL, operation.to_dict()
    assert operation.error["code"] == "operation_plan_requires_replanning", operation.error
    assert operation.result["admin_untouched"] is True, operation.result


def test_a_malformed_digest_does_not_execute(tmp_path):
    services = appliance(tmp_path)
    planned = plan_install(services, channel="exact", tag="v1.1.0")

    def break_digest(payload):
        payload["requested_target"]["digest"] = "not-a-digest"

    corrupt(services, planned, break_digest)
    operation = execute(services, planned)

    assert operation.state == STATE_FAILED_TERMINAL, operation.to_dict()
    assert operation.result["admin_untouched"] is True, operation.result


def test_an_edited_compose_file_does_not_execute(tmp_path):
    services = appliance(tmp_path)
    planned = plan_install(services, channel="exact", tag="v1.1.0")
    compose = services.paths.install_root / "docker-compose.admin.yml"
    compose.write_text(compose.read_text(encoding="utf-8") + "\n# edited\n", encoding="utf-8")

    operation = execute(services, planned)

    assert operation.state == STATE_FAILED_TERMINAL, operation.to_dict()
    assert operation.result["admin_untouched"] is True, operation.result


# --- explicit rollback failure recovery -------------------------------------


def rollback_appliance(tmp_path):
    """A healthy v1.1.0 with a recorded v1.0.0 to roll back to."""

    services = appliance(tmp_path, tag="v1.1.0")
    services.host.publish_image("v1.0.0", healthy=False)
    services.host.pull_local(f"{ADMIN_REPOSITORY}:v1.0.0")
    entry = services.host.registry[f"{ADMIN_REPOSITORY}:v1.0.0"]
    services.known_good.record(
        admin_image=f"{ADMIN_REPOSITORY}:v1.0.0",
        admin_digest=entry["_digest"],
        admin_version="v1.0.0",
        admin_reference=f"{ADMIN_REPOSITORY}@{entry['_digest']}",
    )
    services.known_good.record(
        admin_image=f"{ADMIN_REPOSITORY}:v1.1.0",
        admin_digest=services.host.registry[f"{ADMIN_REPOSITORY}:v1.1.0"]["_digest"],
        admin_version="v1.1.0",
        admin_reference=f"{ADMIN_REPOSITORY}@"
        + services.host.registry[f"{ADMIN_REPOSITORY}:v1.1.0"]["_digest"],
    )
    return services


def run_rollback(services, planned):
    handlers(services).dispatch(
        {
            "operation": "operations.execute",
            "operation_id": planned["operation"]["operation_id"],
            "confirmation_token": planned["confirmation_token"],
        }
    )
    return services.operations.get(planned["operation"]["operation_id"])


def on_stop(services, action):
    """Run ``action`` the moment the running Admin is stopped."""

    original = services.host.handle

    def handle(tool, args, input_text=None):
        if tool == "docker" and "stop" in tuple(args):
            action()
        return original(tool, args, input_text)

    services.host.handle = handle


def test_rollback_recovery_refuses_a_different_image_with_the_same_version(tmp_path):
    """The Admin that comes back has to be the exact one that was running."""

    services = rollback_appliance(tmp_path)
    planned = handlers(services).dispatch({"operation": "admin.plan_rollback"})
    running = services.host.registry[f"{ADMIN_REPOSITORY}:v1.1.0"]["_digest"]
    impostor = services.host.publish_image(
        "v1.1.0", digest="sha256:" + "b" * 64, version="v1.1.0"
    )

    def replace_the_running_build():
        # The exact bytes that were running are gone; only a different build
        # that still calls itself v1.1.0 answers for the tag.
        services.host.images.pop(f"{ADMIN_REPOSITORY}@{running}", None)
        services.host.registry.pop(f"{ADMIN_REPOSITORY}@{running}", None)
        services.host.images[f"{ADMIN_REPOSITORY}:v1.1.0"] = impostor

    on_stop(services, replace_the_running_build)
    operation = run_rollback(services, planned)

    assert operation.state == STATE_FAILED_TERMINAL, operation.to_dict()
    recovery = operation.result["recovery"]
    assert recovery["restored"] is False, recovery
    assert recovery["expected"]["digest"] == running, recovery
    assert "could not be restored" in operation.error["message"], operation.error


def test_rollback_recovery_restores_the_exact_running_admin(tmp_path):
    services = rollback_appliance(tmp_path)
    planned = handlers(services).dispatch({"operation": "admin.plan_rollback"})
    running = services.host.registry[f"{ADMIN_REPOSITORY}:v1.1.0"]["_digest"]

    operation = run_rollback(services, planned)

    recovery = operation.result["recovery"]
    assert recovery["restored"] is True, recovery
    assert recovery["verification"]["active_digest"] == running, recovery
    assert recovery["verification"]["version_matches"] is True, recovery
    assert "the previously running Admin was restored" in operation.error["message"], (
        operation.error
    )


# --- the nested recovery identity is authority too ---------------------------


def corrupt_recovery(services, planned, mutate):
    def edit(payload):
        mutate(payload["requested_target"]["recovery"])

    return corrupt(services, planned, edit)


def refuses(services, planned):
    before = len(stopped_admin(services))
    operation = execute(services, planned)
    assert operation.state == STATE_FAILED_TERMINAL, operation.to_dict()
    assert operation.result["admin_untouched"] is True, operation.result
    assert operation.error["code"] == "operation_plan_requires_replanning", operation.error
    assert len(stopped_admin(services)) == before, services.host.calls
    return operation


@pytest.mark.parametrize(
    "field",
    [
        "digest",
        "reference",
        "repository",
        "version",
        "compose_file",
        "compose_hash",
        "environment_file",
        "environment_hash",
        "healthy",
        "schema_version",
    ],
)
def test_a_recovery_identity_missing_a_field_does_not_execute(tmp_path, field):
    services = appliance(tmp_path)
    planned = plan_install(services, channel="exact", tag="v1.1.0")

    corrupt_recovery(services, planned, lambda recovery: recovery.pop(field, None))

    refuses(services, planned)


@pytest.mark.parametrize(
    "field",
    ["digest", "reference", "repository", "version", "compose_hash", "environment_hash"],
)
def test_a_blank_recovery_field_does_not_execute(tmp_path, field):
    services = appliance(tmp_path)
    planned = plan_install(services, channel="exact", tag="v1.1.0")

    def blank(recovery):
        recovery[field] = ""

    corrupt_recovery(services, planned, blank)

    refuses(services, planned)


def test_a_recovery_reference_that_names_another_digest_does_not_execute(tmp_path):
    services = appliance(tmp_path)
    planned = plan_install(services, channel="exact", tag="v1.1.0")

    def other_digest(recovery):
        recovery["reference"] = f"{recovery['repository']}@sha256:{'b' * 64}"

    corrupt_recovery(services, planned, other_digest)

    refuses(services, planned)


def test_a_recovery_reference_that_names_another_repository_does_not_execute(tmp_path):
    services = appliance(tmp_path)
    planned = plan_install(services, channel="exact", tag="v1.1.0")

    def other_repository(recovery):
        recovery["reference"] = f"ghcr.io/someone/else@{recovery['digest']}"

    corrupt_recovery(services, planned, other_repository)

    refuses(services, planned)


def test_a_malformed_recovery_digest_does_not_execute(tmp_path):
    services = appliance(tmp_path)
    planned = plan_install(services, channel="exact", tag="v1.1.0")

    def malformed(recovery):
        recovery["digest"] = "sha256:not-a-digest"
        recovery["reference"] = f"{recovery['repository']}@sha256:not-a-digest"

    corrupt_recovery(services, planned, malformed)

    refuses(services, planned)


@pytest.mark.parametrize("field", ["compose_file", "environment_file"])
def test_a_relative_recovery_path_does_not_execute(tmp_path, field):
    services = appliance(tmp_path)
    planned = plan_install(services, channel="exact", tag="v1.1.0")

    def relative(recovery):
        recovery[field] = "docker-compose.admin.yml"

    corrupt_recovery(services, planned, relative)

    refuses(services, planned)


@pytest.mark.parametrize(
    "field", ["compose_file", "compose_hash", "environment_file", "environment_hash"]
)
def test_a_recovery_fingerprint_that_left_the_plan_does_not_execute(tmp_path, field):
    """The recovery identity and the planned deployment describe one Admin."""

    services = appliance(tmp_path)
    planned = plan_install(services, channel="exact", tag="v1.1.0")

    def diverge(recovery):
        if field.endswith("_file"):
            recovery[field] = "/opt/somewhere/else.yml"
        else:
            recovery[field] = "sha256:" + "c" * 64

    corrupt_recovery(services, planned, diverge)

    refuses(services, planned)


def test_an_unknown_recovery_schema_does_not_execute(tmp_path):
    services = appliance(tmp_path)
    planned = plan_install(services, channel="exact", tag="v1.1.0")

    def future(recovery):
        recovery["schema_version"] = 99

    corrupt_recovery(services, planned, future)

    refuses(services, planned)


def test_an_unknown_operation_type_does_not_reach_docker(tmp_path):
    services = appliance(tmp_path)
    planned = plan_install(services, channel="exact", tag="v1.1.0")
    before = len(stopped_admin(services))

    corrupt(services, planned, lambda payload: payload.update({"type": "admin.something-new"}))
    operation = execute(services, planned)

    assert operation.state == STATE_FAILED_TERMINAL, operation.to_dict()
    assert operation.result["admin_untouched"] is True, operation.result
    assert operation.error["code"] == "operation_plan_changed", operation.error
    assert len(stopped_admin(services)) == before, services.host.calls


# --- a confirmed plan is bound to the target it was rendered for ------------


def test_a_target_changed_after_the_confirmation_is_refused(tmp_path):
    """Every field is well-formed; it is simply not the plan that was shown."""

    services = appliance(tmp_path)
    planned = plan_install(services, channel="exact", tag="v1.1.0")
    before = len(stopped_admin(services))
    other = "sha256:" + "c" * 64

    corrupt(
        services,
        planned,
        lambda payload: payload["requested_target"].update(
            {"digest": other, "reference": f"{ADMIN_REPOSITORY}@{other}"}
        ),
    )
    error = refuse(services, planned)

    assert error.code == "operation_plan_changed", error.message
    operation = services.operations.get(planned["operation"]["operation_id"])
    assert operation.state == STATE_FAILED_TERMINAL, operation.to_dict()
    assert operation.result["admin_untouched"] is True, operation.result
    assert len(stopped_admin(services)) == before, services.host.calls


def test_a_rendered_plan_carries_the_authority_the_confirmation_checks(tmp_path):
    services = appliance(tmp_path)

    planned = plan_install(services, channel="exact", tag="v1.1.0")

    authority = planned["plan"]["authority"]
    assert len(authority) == 64, authority
    assert planned["operation"]["requested_target"]["authority"] == authority


def test_an_authority_that_was_removed_is_not_the_same_as_no_check(tmp_path):
    services = appliance(tmp_path)
    planned = plan_install(services, channel="exact", tag="v1.1.0")

    corrupt(services, planned, lambda payload: payload["requested_target"].pop("authority", None))
    error = refuse(services, planned)

    assert error.code == "operation_plan_changed", error.message


def test_a_recomputed_plan_hash_does_not_re_authorise_a_changed_target(tmp_path):
    """Editing the plan hash alone cannot make a changed target authoritative."""

    services = appliance(tmp_path)
    planned = plan_install(services, channel="exact", tag="v1.1.0")
    other = "sha256:" + "d" * 64

    def mutate(payload):
        payload["requested_target"].update(
            {
                "digest": other,
                "reference": f"{ADMIN_REPOSITORY}@{other}",
                "authority_plan": "0" * 64,
            }
        )

    corrupt(services, planned, mutate)
    error = refuse(services, planned)

    assert error.code == "operation_plan_changed", error.message


# --- the top-level target is bound to its own reference ---------------------


def test_a_reference_that_names_another_digest_does_not_execute(tmp_path):
    services = appliance(tmp_path)
    planned = plan_install(services, channel="exact", tag="v1.1.0")

    corrupt(
        services,
        planned,
        lambda payload: payload["requested_target"].update(
            {"reference": f"{ADMIN_REPOSITORY}@sha256:" + "b" * 64}
        ),
    )
    operation = execute(services, planned)

    assert operation.state == STATE_FAILED_TERMINAL, operation.to_dict()
    assert operation.result["admin_untouched"] is True, operation.result
    assert operation.error["code"] == "operation_plan_requires_replanning", operation.error


def test_a_reference_that_names_another_repository_does_not_execute(tmp_path):
    services = appliance(tmp_path)
    planned = plan_install(services, channel="exact", tag="v1.1.0")
    digest = planned["operation"]["requested_target"]["digest"]

    corrupt(
        services,
        planned,
        lambda payload: payload["requested_target"].update(
            {"reference": f"ghcr.io/someone-else/admin@{digest}"}
        ),
    )
    operation = execute(services, planned)

    assert operation.state == STATE_FAILED_TERMINAL, operation.to_dict()
    assert operation.result["admin_untouched"] is True, operation.result


def test_a_repository_outside_the_allowlist_does_not_execute(tmp_path):
    services = appliance(tmp_path)
    planned = plan_install(services, channel="exact", tag="v1.1.0")
    digest = planned["operation"]["requested_target"]["digest"]

    corrupt(
        services,
        planned,
        lambda payload: payload["requested_target"].update(
            {
                "repository": "ghcr.io/someone-else/admin",
                "reference": f"ghcr.io/someone-else/admin@{digest}",
            }
        ),
    )
    operation = execute(services, planned)

    assert operation.state == STATE_FAILED_TERMINAL, operation.to_dict()
    assert operation.result["admin_untouched"] is True, operation.result


@pytest.mark.parametrize("tag", ["latest", "not a tag", "v1"])
def test_a_malformed_tag_does_not_execute(tmp_path, tag):
    services = appliance(tmp_path)
    planned = plan_install(services, channel="exact", tag="v1.1.0")

    corrupt(services, planned, lambda payload: payload["requested_target"].update({"tag": tag}))
    operation = execute(services, planned)

    assert operation.state == STATE_FAILED_TERMINAL, operation.to_dict()
    assert operation.result["admin_untouched"] is True, operation.result


def test_an_unknown_architecture_does_not_execute(tmp_path):
    services = appliance(tmp_path)
    planned = plan_install(services, channel="exact", tag="v1.1.0")

    corrupt(
        services,
        planned,
        lambda payload: payload["requested_target"].update({"architecture": "sparc64"}),
    )
    operation = execute(services, planned)

    assert operation.state == STATE_FAILED_TERMINAL, operation.to_dict()
    assert operation.result["admin_untouched"] is True, operation.result


@pytest.mark.parametrize("field", ["compose_hash", "environment_hash"])
def test_a_deployment_hash_that_is_not_a_digest_does_not_execute(tmp_path, field):
    services = appliance(tmp_path)
    planned = plan_install(services, channel="exact", tag="v1.1.0")

    corrupt(services, planned, lambda payload: payload["requested_target"].update({field: "abc"}))
    operation = execute(services, planned)

    assert operation.state == STATE_FAILED_TERMINAL, operation.to_dict()
    assert operation.result["admin_untouched"] is True, operation.result


# --- the nested recovery identity is typed, not merely present -------------


@pytest.mark.parametrize("value", ["yes", 1, None])
def test_a_recovery_healthy_flag_that_is_not_a_boolean_does_not_execute(tmp_path, value):
    services = appliance(tmp_path)
    planned = plan_install(services, channel="exact", tag="v1.1.0")

    corrupt(
        services,
        planned,
        lambda payload: payload["requested_target"]["recovery"].update({"healthy": value}),
    )
    operation = execute(services, planned)

    assert operation.state == STATE_FAILED_TERMINAL, operation.to_dict()
    assert operation.result["admin_untouched"] is True, operation.result


def test_a_recovery_schema_version_that_is_not_a_number_does_not_execute(tmp_path):
    services = appliance(tmp_path)
    planned = plan_install(services, channel="exact", tag="v1.1.0")

    corrupt(
        services,
        planned,
        lambda payload: payload["requested_target"]["recovery"].update({"schema_version": "1"}),
    )
    operation = execute(services, planned)

    assert operation.state == STATE_FAILED_TERMINAL, operation.to_dict()
    assert operation.result["admin_untouched"] is True, operation.result


@pytest.mark.parametrize("field", ["compose_hash", "environment_hash"])
def test_a_recovery_hash_that_is_not_a_digest_does_not_execute(tmp_path, field):
    services = appliance(tmp_path)
    planned = plan_install(services, channel="exact", tag="v1.1.0")

    def mutate(payload):
        payload["requested_target"][field] = "zz"
        payload["requested_target"]["recovery"][field] = "zz"

    corrupt(services, planned, mutate)
    operation = execute(services, planned)

    assert operation.state == STATE_FAILED_TERMINAL, operation.to_dict()
    assert operation.result["admin_untouched"] is True, operation.result
