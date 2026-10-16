# SPDX-License-Identifier: AGPL-3.0-or-later
"""The Appliance Manager's known-good history follows the Admin that runs.

The history is the Manager's rollback order: ``current()`` is what a repair
verifies against, ``previous()`` what Rollback and the *Previous known-good*
channel install. The Admin console replaces itself through System Build and
Guided Upgrade and writes nothing back here, so after a console upgrade the
history used to name the Admin the Manager installed last -- a repair that
restarted the console's Admin reported ``image_mismatch``, and a rollback went
two steps back.

Owner decision 2026-10-13 (A-M13b, option A): what runs is the authority. A
status read records the running Admin as known-good once it passes the
Manager's own verification against the digest the deployment asks for, and
only while nothing is changing it: no operation holds the appliance, no
Manager install waits for its verdict, no console transition is live.
"""

import dataclasses
import json
import threading

import pytest

from appliance import admin_deployment, manager_verify
from appliance.admin_deployment import apply_digest
from appliance.admin_lifecycle import (
    TYPE_INSTALL,
    TYPE_LIFECYCLE,
    TYPE_REPAIR,
    TYPE_ROLLBACK,
)
from appliance.agent import AgentHandlers
from appliance.health import HealthResult
from appliance.manager_update import TYPE_MANAGER_REVERT, TYPE_MANAGER_UPDATE
from appliance.operations import STATE_CANCELLED, STATE_ROLLED_BACK, STATE_SUCCEEDED
from tests.helpers.appliance import ADMIN_CONTAINER, ADMIN_REPOSITORY, IMAGE_SOURCE
from tests.test_appliance_admin_lifecycle import healthy_appliance, plan_and_execute
from tests.test_appliance_admin_transition import live_transition, stamp, write_transition

pytestmark = [pytest.mark.integration, pytest.mark.simulation, pytest.mark.appliance]

CONSOLE_TAG = "v1.2.0"


def digest_of(services, tag):
    return services.host.registry[f"{ADMIN_REPOSITORY}:{tag}"]["_digest"]


def console_replaces_admin(services, tag, *, health="healthy", pin=True, **image):
    """What System Build or Guided Upgrade leaves behind on a pinned deployment.

    The console moves the deployment's digest pin and recreates the container
    from it; it never writes to the Manager's history.
    """

    host = services.host
    host.publish_image(tag, **image)
    host.pull_local(f"{ADMIN_REPOSITORY}:{tag}")
    digest = digest_of(services, tag)
    if pin:
        apply_digest(services.admin.deployment(), ADMIN_REPOSITORY, digest, tag=tag)
    host.run_container(ADMIN_CONTAINER, f"{ADMIN_REPOSITORY}@{digest}", health=health)
    return digest


def manager_installed_twice(tmp_path):
    """The Manager installed v1.1.0 over v1.0.0, so its history is [v1.1.0, v1.0.0]."""

    services = healthy_appliance(tmp_path)
    services.host.publish_image("v1.1.0")
    operation, _ = plan_and_execute(services, channel="exact", tag="v1.1.0")
    assert operation.state == STATE_SUCCEEDED, (operation.state, operation.error)
    services.operations.acknowledge(operation.operation_id)
    assert versions(services) == ["v1.1.0", "v1.0.0"]
    return services


def console_upgraded(tmp_path, **options):
    services = manager_installed_twice(tmp_path)
    console_replaces_admin(services, CONSOLE_TAG, **options)
    return services


def handlers(services):
    return AgentHandlers(services, executor=lambda target: target())


def status_read(services):
    return handlers(services).dispatch({"operation": "admin.get"})


def versions(services):
    return [entry["admin_version"] for entry in services.known_good.entries()]


def history_bytes(services):
    path = services.known_good.path
    return path.read_bytes(), path.stat().st_mtime_ns


def run_plan(services, operation, **fields):
    dispatch = handlers(services).dispatch
    planned = dispatch({"operation": operation, **fields})
    dispatch(
        {
            "operation": "operations.execute",
            "operation_id": planned["operation"]["operation_id"],
            "confirmation_token": planned["confirmation_token"],
        }
    )
    return services.operations.get(planned["operation"]["operation_id"]), planned["plan"]


def test_a_status_read_records_the_admin_a_console_upgrade_put_in_place(tmp_path):
    services = console_upgraded(tmp_path)
    assert versions(services) == ["v1.1.0", "v1.0.0"]

    payload = status_read(services)

    assert versions(services) == [CONSOLE_TAG, "v1.1.0", "v1.0.0"]
    assert payload["known_good"]["current"]["admin_version"] == CONSOLE_TAG
    assert payload["known_good"]["previous"]["admin_version"] == "v1.1.0"


def test_the_record_has_the_shape_of_the_managers_own_installs(tmp_path):
    """Digest-pinned and bound to the deployment, so a rollback can install it."""

    services = console_upgraded(tmp_path)
    digest = digest_of(services, CONSOLE_TAG)

    status_read(services)

    entry = services.known_good.current()
    deployment = services.admin.deployment()
    compose = deployment.compose_file.read_text(encoding="utf-8")
    assert entry["admin_digest"] == digest
    assert entry["admin_reference"] == f"{ADMIN_REPOSITORY}@{digest}"
    assert entry["admin_image"] == f"{ADMIN_REPOSITORY}:{CONSOLE_TAG}"
    assert entry["healthcheck"] == "passed"
    assert entry["compose_hash"] == admin_deployment.compose_hash(compose)
    assert entry["environment_hash"] == admin_deployment.environment_hash(deployment)
    assert entry["architecture"] == "arm64"
    assert entry["revision"] == "abc1234"


def test_the_overview_status_read_records_it_too(tmp_path):
    services = console_upgraded(tmp_path)

    overview = handlers(services).dispatch({"operation": "status.get"})

    assert overview["admin"]["known_good"]["current"]["admin_version"] == CONSOLE_TAG
    assert versions(services)[0] == CONSOLE_TAG


def test_a_second_status_read_does_not_rewrite_the_history(tmp_path):
    """The console polls every few seconds; the file must not churn."""

    services = console_upgraded(tmp_path)
    status_read(services)
    before = history_bytes(services)

    status_read(services)
    handlers(services).dispatch({"operation": "status.get"})
    services.admin.releases()

    assert history_bytes(services) == before


def test_an_admin_the_manager_already_records_as_current_is_not_recorded_again(tmp_path):
    services = manager_installed_twice(tmp_path)
    before = history_bytes(services)

    status_read(services)

    assert history_bytes(services) == before


def test_an_admin_without_a_docker_health_check_counts_when_its_endpoint_answers(tmp_path):
    """``none`` is healthy by the Manager's own verification, which then asks the API."""

    services = console_upgraded(tmp_path, health="none")

    status_read(services)

    assert versions(services)[0] == CONSOLE_TAG


def test_a_status_read_asks_the_endpoint_once_and_never_waits(tmp_path, monkeypatch):
    """An install waits for its Admin to come up; a page polling every few seconds must not."""

    services = console_upgraded(tmp_path)
    services.admin.health.forced = HealthResult(reachable=False, error="connection_refused")
    waited = []
    monkeypatch.setattr(
        services.admin.health,
        "wait_until_healthy",
        lambda url, **options: waited.append(url) or services.admin.health.probe(url),
    )
    probes = services.admin.health.probes

    status_read(services)

    assert waited == []
    assert services.admin.health.probes == probes + 1
    assert versions(services) == ["v1.1.0", "v1.0.0"]


@pytest.mark.parametrize(
    ("break_it", "action"),
    [
        ("stop", "start_admin"),
        ("remove", "recreate_admin"),
    ],
)
def test_a_repair_after_a_console_upgrade_verifies_the_admin_the_console_installed(
    tmp_path, break_it, action
):
    services = console_upgraded(tmp_path)
    status_read(services)
    if break_it == "stop":
        container = services.host.containers[ADMIN_CONTAINER]
        container["State"]["Running"] = False
        container["State"]["Status"] = "exited"
    else:
        services.host.containers.pop(ADMIN_CONTAINER)

    operation, plan = run_plan(services, "admin.plan_repair")

    assert action in plan["actions"], plan
    applied = {item["action"]: item for item in operation.result["applied"]}
    assert applied[action]["result"] == "verified", applied[action]
    assert operation.state == STATE_SUCCEEDED, (operation.state, operation.error)


def test_a_rollback_after_a_console_upgrade_targets_the_admin_that_ran_before_it(tmp_path):
    services = console_upgraded(tmp_path)
    status_read(services)

    operation, plan = run_plan(services, "admin.plan_rollback")

    assert plan["target"]["admin_version"] == "v1.1.0"
    assert plan["target"]["admin_digest"] == digest_of(services, "v1.1.0")
    assert operation.state == STATE_SUCCEEDED, (operation.state, operation.error)
    assert services.admin.detect()["version"] == "v1.1.0"
    assert versions(services)[:2] == ["v1.1.0", CONSOLE_TAG]


def test_the_previous_known_good_channel_follows_a_console_upgrade(tmp_path):
    services = console_upgraded(tmp_path)
    status_read(services)

    listing = services.admin.releases()
    planned = handlers(services).dispatch(
        {"operation": "admin.plan_install", "channel": "previous_known_good"}
    )

    assert listing["previous_known_good"]["admin_version"] == "v1.1.0"
    assert planned["plan"]["target_tag"] == "v1.1.0"


@pytest.mark.parametrize("health", ["starting", "unhealthy"])
def test_a_container_docker_does_not_report_healthy_is_not_recorded(tmp_path, health):
    services = console_upgraded(tmp_path, health=health)

    status_read(services)

    assert versions(services) == ["v1.1.0", "v1.0.0"]


@pytest.mark.parametrize("health", ["healthy", "none"])
def test_an_admin_whose_endpoint_does_not_answer_is_not_recorded(tmp_path, health):
    """A container reporting healthy has only proven its own health check ran."""

    services = console_upgraded(tmp_path, health=health)
    services.admin.health.forced = HealthResult(reachable=False, error="connection_refused")

    status_read(services)

    assert versions(services) == ["v1.1.0", "v1.0.0"]


@pytest.mark.parametrize("endpoint_version", ["", CONSOLE_TAG])
def test_an_admin_without_a_version_label_is_not_recorded(tmp_path, endpoint_version):
    """A record without a version cannot be rolled back to by its tag.

    The version comes from the image label, as the status shows it; an
    endpoint that names one does not stand in for the image's own.
    """

    services = console_upgraded(
        tmp_path,
        labels={
            "org.opencontainers.image.source": IMAGE_SOURCE,
            "org.opencontainers.image.revision": "abc1234",
        },
    )
    services.admin.health.forced = HealthResult(
        reachable=True, status_code=200, version=endpoint_version
    )

    status_read(services)

    assert versions(services) == ["v1.1.0", "v1.0.0"]


def test_a_container_the_deployment_does_not_ask_for_is_not_recorded(tmp_path):
    """The console started something without moving the pin, or someone else did."""

    services = manager_installed_twice(tmp_path)
    console_replaces_admin(services, CONSOLE_TAG, pin=False)

    status_read(services)

    assert versions(services) == ["v1.1.0", "v1.0.0"]


def test_a_container_running_neither_the_pin_nor_the_record_is_not_recorded(tmp_path):
    services = console_upgraded(tmp_path)
    console_replaces_admin(services, "v9.9.9", pin=False)

    status_read(services)

    assert versions(services) == ["v1.1.0", "v1.0.0"]


@pytest.mark.parametrize("deployment", ["tag_variable", "image_not_present"])
def test_a_deployment_digest_that_cannot_be_resolved_records_nothing(tmp_path, deployment):
    services = console_upgraded(tmp_path)
    if deployment == "tag_variable":
        services.host.write_deployment(tag=CONSOLE_TAG, variable_tag=True)
    else:
        apply_digest(
            services.admin.deployment(), ADMIN_REPOSITORY, "sha256:" + "e" * 64, tag=CONSOLE_TAG
        )

    status_read(services)

    assert versions(services) == ["v1.1.0", "v1.0.0"]


MANAGER_OPERATIONS = (
    TYPE_INSTALL,
    TYPE_ROLLBACK,
    TYPE_REPAIR,
    TYPE_LIFECYCLE,
    TYPE_MANAGER_UPDATE,
    TYPE_MANAGER_REVERT,
)


def hold_the_appliance(services, operation_type, state):
    store = services.operations
    operation = store.create(operation_type)
    if state in ("awaiting_confirmation", "running"):
        store.await_confirmation(operation.operation_id, {"type": operation_type})
    if state == "running":
        token = store.get(operation.operation_id, include_token=True).confirmation_token
        store.confirm(operation.operation_id, token)
    return operation


@pytest.mark.parametrize("state", ["planned", "awaiting_confirmation", "running"])
@pytest.mark.parametrize("operation_type", MANAGER_OPERATIONS)
def test_nothing_is_recorded_while_an_operation_holds_the_appliance(
    tmp_path, operation_type, state
):
    """An Admin seen running mid-operation is not the rollback target before its verdict."""

    services = console_upgraded(tmp_path)
    operation = hold_the_appliance(services, operation_type, state)

    status_read(services)
    assert versions(services) == ["v1.1.0", "v1.0.0"]

    services.operations.finish(
        operation.operation_id, STATE_SUCCEEDED if state == "running" else STATE_CANCELLED
    )
    status_read(services)
    assert versions(services)[0] == CONSOLE_TAG


def arm_manager_deadline(services, *, armed_at, window=900):
    reverter = services.paths.packages_dir / "packaged-verify-manager.sh"
    reverter.parent.mkdir(parents=True, exist_ok=True)
    reverter.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    previous = services.paths.packages_dir / "previous.deb"
    previous.write_bytes(b"the package that is running")
    manager_verify.arm(
        services.paths,
        services.runner,
        expected_version="0.2.0",
        build_id="b",
        previous=str(previous),
        now=armed_at,
        window_seconds=window,
        reverter=str(reverter),
    )


def test_nothing_is_recorded_while_a_manager_install_waits_for_its_verdict(tmp_path):
    """The Manager running now may be reverted; it writes no rollback order until judged."""

    services = console_upgraded(tmp_path)
    arm_manager_deadline(services, armed_at=int(services.clock()))

    status_read(services)
    assert versions(services) == ["v1.1.0", "v1.0.0"]

    manager_verify.verdict_path(services.paths).write_text(
        json.dumps({"verdict": manager_verify.VERDICT_CONFIRMED}), encoding="utf-8"
    )
    status_read(services)
    assert versions(services)[0] == CONSOLE_TAG


def test_a_manager_deadline_past_its_window_no_longer_holds_the_record_back(tmp_path):
    """The same rule that gives the operator Update and Revert back after the window."""

    services = console_upgraded(tmp_path)
    arm_manager_deadline(services, armed_at=int(services.clock()) - 4000)

    status_read(services)

    assert versions(services)[0] == CONSOLE_TAG


def test_without_a_way_to_ask_about_the_manager_install_nothing_is_recorded(tmp_path):
    services = console_upgraded(tmp_path)
    services.admin.manager_pending = None

    status_read(services)

    assert versions(services) == ["v1.1.0", "v1.0.0"]


@pytest.mark.parametrize("ending", ["completed", "removed"])
def test_nothing_is_recorded_while_a_console_transition_is_live(tmp_path, ending):
    services = console_upgraded(tmp_path)
    path = write_transition(services, live_transition())

    status_read(services)
    assert versions(services) == ["v1.1.0", "v1.0.0"]

    if ending == "completed":
        write_transition(services, live_transition(stage="completed"))
    else:
        path.unlink()
    status_read(services)
    assert versions(services)[0] == CONSOLE_TAG


@pytest.mark.parametrize(
    "transition",
    [
        "expired",
        b"{not json",
        json.dumps({"stage": "admin_update_pending", "expires_at": "not-a-timestamp"}),
    ],
)
def test_an_expired_or_unreadable_transition_does_not_hold_the_record_back(
    tmp_path, transition
):
    """Consistent with every Admin mutation: only a live record makes the Manager yield.

    A record past its own expiry, or one that cannot be read, is the wedged state
    an operator comes to the Manager to fix; it is not a replacement in flight.
    """

    services = console_upgraded(tmp_path)
    if transition == "expired":
        payload = live_transition()
        payload["expires_at"] = stamp(-60)
        write_transition(services, payload)
    else:
        write_transition(services, transition)

    status_read(services)

    assert versions(services)[0] == CONSOLE_TAG


DEVELOPMENT_LABEL = "dev-feat-r24-0a1b2c3d-abc1234-18000000000-1"


def test_a_development_build_the_console_installed_is_not_recorded(tmp_path):
    """Development builds share the release repository; none is a rollback target.

    Recorded, it became ``previous()`` after the console's next upgrade, and both
    Rollback and the *Previous known-good* channel refused it as an unsupported
    tag -- with no way to plan past it.
    """

    services = manager_installed_twice(tmp_path)
    console_replaces_admin(services, DEVELOPMENT_LABEL)
    status_read(services)
    assert versions(services) == ["v1.1.0", "v1.0.0"]

    console_replaces_admin(services, "v1.3.0")
    status_read(services)
    assert versions(services) == ["v1.3.0", "v1.1.0", "v1.0.0"]

    planned = handlers(services).dispatch(
        {"operation": "admin.plan_install", "channel": "previous_known_good"}
    )
    assert planned["plan"]["target_tag"] == "v1.1.0"
    services.operations.cancel(planned["operation"]["operation_id"])

    operation, plan = run_plan(services, "admin.plan_rollback")

    assert plan["target"]["admin_version"] == "v1.1.0"
    assert operation.state == STATE_SUCCEEDED, (operation.state, operation.error)
    assert services.admin.detect()["version"] == "v1.1.0"


def allow_prereleases(services, allowed):
    images = dataclasses.replace(services.config.images, allow_prerelease=allowed)
    object.__setattr__(services.config, "images", images)


@pytest.mark.parametrize("allowed", [False, True])
def test_a_release_candidate_is_recorded_only_where_this_host_installs_candidates(
    tmp_path, allowed
):
    services = manager_installed_twice(tmp_path)
    allow_prereleases(services, allowed)
    console_replaces_admin(services, "v1.3.0-rc1")

    status_read(services)

    expected = ["v1.3.0-rc1", "v1.1.0", "v1.0.0"] if allowed else ["v1.1.0", "v1.0.0"]
    assert versions(services) == expected


@pytest.mark.parametrize(
    "image",
    [
        {"source": "https://example.invalid/someone-else"},
        {
            "labels": {
                "org.opencontainers.image.source": IMAGE_SOURCE,
                "org.opencontainers.image.version": CONSOLE_TAG,
                "org.opencontainers.image.revision": "abc1234",
            }
        },
        {"architecture": "riscv64"},
    ],
    ids=["foreign_source", "label_missing", "foreign_architecture"],
)
def test_an_image_an_install_would_refuse_is_not_recorded(tmp_path, image):
    """The labels and architecture an install validates, validated the same way."""

    services = console_upgraded(tmp_path, **image)

    status_read(services)

    assert versions(services) == ["v1.1.0", "v1.0.0"]


def recording_detail(operation):
    return [
        entry.get("detail", "")
        for entry in operation.progress
        if entry["stage"] == "recording_known_good"
    ]


@pytest.mark.parametrize(
    ("label", "image"),
    [
        (DEVELOPMENT_LABEL, {}),
        ("latest", {}),
        ("v1.2.0-rc.1", {}),
        (CONSOLE_TAG, {"source": "https://example.invalid/someone-else"}),
        (CONSOLE_TAG, {"architecture": "riscv64"}),
    ],
    ids=["development", "latest", "candidate", "foreign_source", "foreign_architecture"],
)
def test_a_manager_install_over_a_build_that_is_no_release_does_not_record_it(
    tmp_path, label, image
):
    """An install records the Admin it replaces by the release rule a status read applies.

    The console ran a Development build, the rolling main build labelled
    ``latest``, a candidate this host does not install, or an image whose
    labels or architecture an install refuses; the status read left it out,
    and the Manager's next install put it into the history as the entry it
    replaced, where Rollback and *Previous known-good* refused it or would
    deploy what this host does not install.
    """

    services = manager_installed_twice(tmp_path)
    console_replaces_admin(services, label, **image)
    status_read(services)
    services.host.publish_image("v1.3.0")

    install, _ = run_plan(services, "admin.plan_install", channel="exact", tag="v1.3.0")

    assert install.state == STATE_SUCCEEDED, (install.state, install.error)
    services.operations.acknowledge(install.operation_id)
    assert versions(services) == ["v1.3.0", "v1.1.0", "v1.0.0"]
    assert any("not recorded" in detail for detail in recording_detail(install))
    planned = handlers(services).dispatch(
        {"operation": "admin.plan_install", "channel": "previous_known_good"}
    )
    assert planned["plan"]["target_tag"] == "v1.1.0"
    services.operations.cancel(planned["operation"]["operation_id"])
    rollback, plan = run_plan(services, "admin.plan_rollback")
    assert plan["target"]["admin_version"] == "v1.1.0"
    assert rollback.state == STATE_SUCCEEDED, (rollback.state, rollback.error)


def test_a_manager_install_still_records_the_release_it_replaces(tmp_path):
    """No status read saw the console's release; the install records it itself."""

    services = console_upgraded(tmp_path)
    services.host.publish_image("v1.3.0")

    install, _ = run_plan(services, "admin.plan_install", channel="exact", tag="v1.3.0")

    assert install.state == STATE_SUCCEEDED, (install.state, install.error)
    assert versions(services) == ["v1.3.0", CONSOLE_TAG, "v1.1.0", "v1.0.0"]
    assert not any(recording_detail(install))


def test_a_failed_install_over_a_development_build_puts_that_build_back(tmp_path):
    """Recovery restores the identity captured at the plan, not a history entry."""

    services = manager_installed_twice(tmp_path)
    build = console_replaces_admin(services, DEVELOPMENT_LABEL)
    services.host.publish_image("v1.3.0", healthy=False)

    install, _ = run_plan(services, "admin.plan_install", channel="exact", tag="v1.3.0")

    assert install.state == STATE_ROLLED_BACK, (install.state, install.error)
    assert install.result["verification"]["active_digest"] == build
    assert services.admin.detect()["digest"] == build
    assert versions(services) == ["v1.1.0", "v1.0.0"]


def test_a_history_that_cannot_be_written_does_not_fail_the_status_read(tmp_path, monkeypatch):
    services = console_upgraded(tmp_path)

    def refuse(**_fields):
        raise OSError("read-only file system")

    monkeypatch.setattr(services.known_good, "record", refuse)

    payload = status_read(services)

    assert payload["installed"] is True
    assert payload["known_good"]["current"]["admin_version"] == "v1.1.0"


def test_an_install_finishing_during_a_status_read_is_not_recorded_twice(tmp_path, monkeypatch):
    """Interleaving: a status read reads the history; a Manager install then
    finishes and records its Admin; the status read goes on to record the same
    Admin under its label.

    The label says ``1.3.0`` and the tag ``v1.3.0``, so the two records are two
    entries for one digest and ``previous()`` -- the rollback target -- would be
    the Admin that runs. An Admin operation that executes and a status read that
    records exclude each other, so the read sees the install either not begun
    or finished.
    """

    services = console_upgraded(tmp_path)
    status_read(services)
    services.host.publish_image("v1.3.0", version="1.3.0")

    verifying = threading.Event()
    release = threading.Event()
    installer = {}
    real_wait = services.admin.health.wait_until_healthy

    def held_verification(url, *, timeout, **options):
        verifying.set()
        assert release.wait(10), "the test never released the install"
        return real_wait(url, timeout=timeout, **options)

    monkeypatch.setattr(services.admin.health, "wait_until_healthy", held_verification)

    def start_in_a_thread(target):
        installer["thread"] = threading.Thread(target=target, daemon=True)
        installer["thread"].start()
        return installer["thread"]

    dispatch = AgentHandlers(services, executor=start_in_a_thread).dispatch
    planned = dispatch({"operation": "admin.plan_install", "channel": "exact", "tag": "v1.3.0"})
    dispatch(
        {
            "operation": "operations.execute",
            "operation_id": planned["operation"]["operation_id"],
            "confirmation_token": planned["confirmation_token"],
        }
    )
    assert verifying.wait(10), "the install never reached its verification"

    reader = threading.current_thread()
    real_current = services.known_good.current
    fired = []

    def current_then_the_install_finishes():
        seen = real_current()
        if threading.current_thread() is reader and not fired:
            fired.append(True)
            release.set()
            installer["thread"].join(10)
            assert not installer["thread"].is_alive(), "the install did not finish"
        return seen

    monkeypatch.setattr(services.known_good, "current", current_then_the_install_finishes)

    status_read(services)
    monkeypatch.setattr(services.known_good, "current", real_current)
    status_read(services)

    operation = services.operations.get(planned["operation"]["operation_id"])
    assert operation.state == STATE_SUCCEEDED, (operation.state, operation.error)
    digests = [entry["admin_digest"] for entry in services.known_good.entries()]
    assert digests[0] == digest_of(services, "v1.3.0")
    assert len(digests) == len(set(digests)), services.known_good.entries()


class SignallingLock:
    """The lock, saying when somebody starts to wait for it."""

    def __init__(self, lock, waiting):
        self._lock = lock
        self._waiting = waiting

    def acquire(self, blocking=True, timeout=-1):
        if blocking:
            self._waiting.set()
        return self._lock.acquire(blocking, timeout)

    def release(self):
        self._lock.release()

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *_exc):
        self.release()


@pytest.mark.parametrize(
    ("plan", "target_of"),
    [
        ({"operation": "admin.plan_rollback"}, lambda plan: plan["target"]["admin_version"]),
        (
            {"operation": "admin.plan_install", "channel": "previous_known_good"},
            lambda plan: plan["target_tag"],
        ),
    ],
    ids=["rollback", "previous_known_good"],
)
def test_a_plan_made_while_a_status_read_records_reads_the_history_that_record_left(
    tmp_path, monkeypatch, plan, target_of
):
    """Interleaving: the first status read after a console upgrade is inside its
    endpoint probe when the operator plans a Rollback.

    The plan read the history the record was about to replace and went two
    steps back, while the record was written under the plan awaiting its
    confirmation. A plan that reads the history now waits for a record in
    flight, so it plans against the history that record left.
    """

    services = console_upgraded(tmp_path)
    probing = threading.Event()
    release_reader = threading.Event()
    plan_waits_or_is_done = threading.Event()
    real_probe = services.admin.health.probe
    threads = {}

    def held_probe(url):
        if threading.current_thread() is threads["reader"]:
            probing.set()
            assert release_reader.wait(10), "the test never released the status read"
        return real_probe(url)

    monkeypatch.setattr(services.admin.health, "probe", held_probe)
    monkeypatch.setattr(
        services.admin,
        "_admin_mutation",
        SignallingLock(services.admin._admin_mutation, plan_waits_or_is_done),
    )

    threads["reader"] = threading.Thread(target=status_read, args=(services,), daemon=True)
    threads["reader"].start()
    assert probing.wait(10), "the status read never reached its endpoint probe"

    answer = {}

    def make_the_plan():
        try:
            answer["planned"] = handlers(services).dispatch(plan)
        finally:
            plan_waits_or_is_done.set()

    threads["planner"] = threading.Thread(target=make_the_plan, daemon=True)
    threads["planner"].start()
    assert plan_waits_or_is_done.wait(10), "the plan neither waited nor finished"
    release_reader.set()
    for thread in threads.values():
        thread.join(10)
        assert not thread.is_alive()

    assert versions(services) == [CONSOLE_TAG, "v1.1.0", "v1.0.0"]
    assert target_of(answer["planned"]["plan"]) == "v1.1.0"
