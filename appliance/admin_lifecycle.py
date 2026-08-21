# SPDX-License-Identifier: AGPL-3.0-or-later
"""Transactional EMS Admin installation, rollback and repair.

The Appliance Manager runs outside Docker, so it stays reachable while the
Admin container is stopped, replaced or broken. Nothing in this module deletes
EMS configuration, EMS data, backups, Docker volumes or containers it does not
manage: a failed replacement is undone by restoring the saved deployment files
and re-pinning the previous known-good digest.
"""

import time
from dataclasses import dataclass

from appliance import admin_deployment
from appliance.admin_bootstrap import (
    BootstrapError,
    DeploymentBootstrap,
    installer_path as bootstrap_installer_path,
)
from appliance import admin_transition
from appliance.admin_deployment import (
    DeploymentError,
    apply_digest,
    environment_hash,
    resolve_deployment,
    snapshot,
)
from appliance.docker_backend import (
    CONTAINER_RUNNING,
    DAEMON_RUNNING,
    HEALTH_HEALTHY,
    HEALTH_NONE,
    HEALTH_STARTING,
    HEALTH_UNHEALTHY,
    DockerError,
)
from appliance.known_good import HEALTHCHECK_PASSED
from appliance.operation_schema import (
    OperationSchemaError,
    is_bootstrap,
    validate_operation,
)
from appliance.operations import (
    STATE_FAILED_RECOVERABLE,
    STATE_FAILED_TERMINAL,
    STATE_MANUAL_ACTION_REQUIRED,
    STATE_ROLLED_BACK,
    STATE_ROLLING_BACK,
    STATE_SUCCEEDED,
    STATE_VERIFYING,
)
from appliance.releases import ReleaseCatalogue, ReleaseResolutionError, resolve_channel
from appliance.systemd import UNIT_DOCKER
from appliance.validation import (
    ValidationError,
    build_digest_ref,
    build_image_ref,
    normalize_version,
    validate_architecture,
    validate_digest,
    validate_image_repository,
    validate_oci_labels,
)

TYPE_INSTALL = "admin.install"
TYPE_ROLLBACK = "admin.rollback"
TYPE_REPAIR = "admin.repair"
TYPE_LIFECYCLE = "admin.lifecycle"

LABEL_VERSION = "org.opencontainers.image.version"
LABEL_REVISION = "org.opencontainers.image.revision"
LABEL_SOURCE = "org.opencontainers.image.source"


VERIFICATION_MESSAGES = {
    "container_missing": "the Admin container does not exist",
    "container_not_running": "the Admin container is not running",
    "container_still_running": "the Admin container is still running",
    "container_unhealthy": "the Admin container reports an unhealthy health check",
    "image_mismatch": "the Admin container runs a different image than expected",
    "api_unreachable": "the Admin HTTP endpoint did not answer",
    "version_unreadable": "the Admin version could not be read",
    "version_mismatch": "the running Admin reports a different version than expected",
}


def lifecycle_failure_message(action, verification):
    reasons = [VERIFICATION_MESSAGES.get(code, code) for code in verification["failures"]]
    return f"docker {action} reported success but " + "; ".join(reasons)


def repair_failure_message(remaining, unverified):
    parts = []
    if unverified:
        parts.append(
            "; ".join(
                f"{item['action']}: {VERIFICATION_MESSAGES.get(item['result'], item['result'])}"
                for item in unverified
            )
        )
    if remaining:
        parts.append(f"{len(remaining)} finding(s) still block a healthy Admin")
    return "the repair ran but " + " — ".join(parts)


class AdminLifecycleError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class RepairFinding:
    check: str
    ok: bool
    detail: str
    suggestion: str = ""
    action: str = ""
    manual: bool = False
    # A check that could not run is neither a pass nor a finding. It must not
    # block a repair, and it must never be displayed as a confirmed result.
    indeterminate: bool = False

    def to_dict(self):
        return {
            "check": self.check,
            "ok": self.ok,
            "detail": self.detail,
            "suggestion": self.suggestion,
            "action": self.action,
            "manual": self.manual,
            "indeterminate": self.indeterminate,
        }


class AdminLifecycleService:
    def __init__(
        self,
        *,
        paths,
        config,
        docker,
        known_good,
        health,
        operations,
        runner=None,
        systemd=None,
        catalogue=None,
        time_fn=None,
        sleep=None,
        operation_log=None,
        bootstrap=None,
    ):
        self.paths = paths
        self.config = config
        self.docker = docker
        self.known_good = known_good
        self.health = health
        self.operations = operations
        self.runner = runner
        self.systemd = systemd
        self.catalogue = catalogue or ReleaseCatalogue(config)
        self.bootstrap = bootstrap or DeploymentBootstrap(paths, config)
        self._time = time_fn or time.time
        self._sleep = sleep or time.sleep
        self._operation_log = operation_log
        self.last_repair_verification = None
        self.last_admin_transition = None

    # --- detection -------------------------------------------------------

    def deployment(self):
        return resolve_deployment(self.paths, self.config)

    def detect(self):
        daemon = self.docker.daemon_state()
        deployment = self.deployment()
        container = self.docker.inspect_container(self.config.admin_container)

        image = None
        if container.exists and container.image:
            image = self.docker.inspect_image(container.image)
        elif deployment.image_reference and "${" not in deployment.image_reference:
            image = self.docker.inspect_image(deployment.image_reference)

        labels = dict(image.labels) if image else {}
        version = str(labels.get(LABEL_VERSION) or "")

        record = {
            "installed": bool(container.exists),
            "docker": daemon,
            "container": container.to_dict(),
            "deployment": deployment.to_dict(),
            "image": image.to_dict() if image else None,
            "version": version,
            "revision": str(labels.get(LABEL_REVISION) or ""),
            "digest": image.digest if image else "",
            "known_good": {
                "current": self.known_good.current(),
                "previous": self.known_good.previous(),
            },
            "health": container.health if container.exists else HEALTH_NONE,
        }
        record["healthy"] = container.state == CONTAINER_RUNNING and container.health in (
            HEALTH_HEALTHY,
            HEALTH_NONE,
        )
        # A flashed appliance has an empty deployment root: no compose file
        # names the Admin service, and no container was ever created from one.
        # That is the only state a first installation may create files in.
        record["bootstrap_required"] = not deployment.service_defined and not container.exists
        record["transition"] = admin_transition.read_transition(
            admin_transition.transition_path(self.paths, deployment)
        )
        return record

    def releases(self):
        current = self.detect()
        try:
            available = [item.to_dict() for item in self.catalogue.available()]
            error = ""
        except ReleaseResolutionError as exc:
            available, error = [], exc.code
        return {
            "available": available,
            "error": error,
            "current_version": current["version"],
            "previous_known_good": self.known_good.previous(),
            "allow_prerelease": self.config.images.allow_prerelease,
            "repository": self.config.images.admin_repository,
        }

    # --- planning --------------------------------------------------------

    def plan_install(self, operation, *, channel, tag=None, reinstall=False):
        """Pull and validate the target, then wait for an explicit confirmation."""

        state = self.detect()
        self._advance(operation, "preflight")
        self._require_docker(state)
        self._require_no_admin_transition()
        bootstrap = self._deployment_state(state)
        if bootstrap:
            # Planning must stay a preview, so the only thing done here is to
            # prove the deployment could be created: the account, the owner and
            # the packaged installer are checked, nothing is written.
            owner = self._bootstrap_owner()

        try:
            target = resolve_channel(
                channel,
                catalogue=self.catalogue,
                current_tag=state["version"],
                previous_known_good=self.known_good.previous(),
                requested_tag=tag,
            )
        except (ReleaseResolutionError, ValidationError) as exc:
            raise AdminLifecycleError(exc.code, exc.message)

        repository = self.config.images.admin_repository
        image_ref = build_image_ref(repository, target.tag)

        self._advance(operation, "pulling_image", detail=image_ref)
        try:
            self.docker.pull_image(image_ref)
        except DockerError as exc:
            raise AdminLifecycleError(exc.code, exc.message)

        self._advance(operation, "inspecting_image")
        image = self.docker.inspect_image(image_ref)
        if not image.exists:
            raise AdminLifecycleError("image_inspect_failed", f"{image_ref} cannot be inspected")

        try:
            validate_architecture(image.architecture, self.config.supported_architectures)
            label_result = validate_oci_labels(
                image.labels,
                requested_tag=target.tag,
                expected_source=self.config.images.expected_source,
                legacy_exempt_tags=self.config.images.legacy_exempt_tags,
            )
        except ValidationError as exc:
            raise AdminLifecycleError(exc.code, exc.message)

        if not image.digest:
            raise AdminLifecycleError(
                "digest_unresolved",
                f"{image_ref} has no canonical repository digest; refusing to deploy a mutable tag",
            )

        identical = bool(state["digest"]) and state["digest"] == image.digest
        if identical and not reinstall:
            raise AdminLifecycleError(
                "target_identical",
                "the requested version is already installed; choose Reinstall to install it again",
            )

        recovery = self._capture_recovery_identity(state)
        plan = {
            "type": TYPE_INSTALL,
            "bootstrap": bootstrap,
            "repository": repository,
            "target_tag": target.tag,
            "target_channel": target.channel,
            "target_digest": image.digest,
            "target_reference": build_digest_ref(repository, image.digest),
            "target_revision": str(image.labels.get(LABEL_REVISION) or ""),
            "target_source": str(image.labels.get(LABEL_SOURCE) or ""),
            "target_architecture": image.architecture,
            "legacy_labels_accepted": label_result["legacy_exempt"],
            "reinstall": bool(reinstall),
            "current_version": state["version"],
            "current_digest": state["digest"],
            "recovery": recovery,
            "rollback_target": self.known_good.current(),
            "deployment": state["deployment"],
            "preserves": [
                "EMS configuration",
                "EMS runtime data",
                "backups",
                "Admin persistent state",
                "unrelated containers and volumes",
            ],
        }
        if bootstrap:
            # Announced, not done: the files appear when the operator confirms.
            plan["creates_deployment"] = {
                "installer": str(bootstrap_installer_path()),
                "compose_file": state["deployment"]["compose_file"],
                "environment_file": state["deployment"]["env_file"],
                "owner_uid": owner[0],
                "owner_gid": owner[1],
            }
        values = {
            "repository": repository,
            "tag": target.tag,
            "digest": image.digest,
            "reference": plan["target_reference"],
            "revision": plan["target_revision"],
            "source": plan["target_source"],
            "architecture": image.architecture,
            "reinstall": bool(reinstall),
            "recovery": recovery,
            **self._deployment_fingerprint(),
        }
        if bootstrap:
            values["bootstrap"] = True
        self.operations.update_target(operation.operation_id, values)
        operation.requested_target.update(values)
        return plan

    def plan_rollback(self, operation):
        state = self.detect()
        self._advance(operation, "preflight")
        self._require_docker(state)
        self._require_no_admin_transition()
        self._require_deployment(state)

        previous = self.known_good.previous()
        if not previous:
            raise AdminLifecycleError(
                "no_previous_known_good", "no previous known-good Admin has been recorded"
            )

        # A record that cannot be turned into an immutable reference is refused
        # here, before anything the operator could confirm exists.
        try:
            repository, digest, reference = self._validated_rollback_target(previous)
        except ValidationError as exc:
            raise AdminLifecycleError("invalid_known_good_record", exc.message)

        recovery = self._capture_recovery_identity(state)
        values = {
            "repository": repository,
            "tag": previous.get("admin_version", ""),
            "digest": digest,
            "reference": reference,
            "recovery": recovery,
            **self._deployment_fingerprint(),
        }
        operation.requested_target.update(values)
        self.operations.update_target(operation.operation_id, values)
        return {
            "type": TYPE_ROLLBACK,
            "target": previous,
            "target_reference": reference,
            "image_available_locally": self.docker.inspect_image(reference).exists,
            "current_version": state["version"],
            "current_digest": state["digest"],
            "recovery": recovery,
            "deployment": state["deployment"],
            "preserves": [
                "the running Admin until every preflight check has passed",
                "EMS configuration, runtime data and backups",
            ],
        }

    def plan_lifecycle(self, operation, action):
        state = self.detect()
        self._require_docker(state)
        self._require_no_admin_transition()
        operation.requested_target.update({"action": action})
        self.operations.update_target(operation.operation_id, {"action": action})
        return {
            "type": TYPE_LIFECYCLE,
            "action": action,
            "container": self.config.admin_container,
            "current_state": state["container"]["state"],
        }

    def plan_repair(self, operation):
        self._require_no_admin_transition()
        findings = self.inspect_repair()
        actions = [item.action for item in findings if item.action]
        manual = [
            item.suggestion or item.detail for item in findings if item.manual and not item.ok
        ]
        values = {"actions": actions, "manual_actions": manual}
        operation.requested_target.update(values)
        self.operations.update_target(operation.operation_id, values)
        return {
            "type": TYPE_REPAIR,
            "findings": [item.to_dict() for item in findings],
            "actions": actions,
            "manual_actions": manual,
            "healthy": all(item.ok for item in findings),
        }

    def inspect_repair(self):
        """Read-only repair inspection; the preview a repair confirmation shows."""

        findings = []
        daemon = self.docker.daemon_state()
        docker_ok = daemon["state"] == DAEMON_RUNNING
        findings.append(
            RepairFinding(
                check="docker_daemon",
                ok=docker_ok,
                detail=f"Docker daemon is {daemon['state']}",
                suggestion="" if docker_ok else "Start the Docker service",
                action="" if docker_ok else "start_docker",
            )
        )

        deployment = self.deployment()
        findings.append(
            RepairFinding(
                check="compose_file",
                ok=deployment.compose_exists,
                detail=f"Compose file {deployment.compose_file}",
                suggestion=""
                if deployment.compose_exists
                else "Recreate the Admin compose file with install-admin-console.sh",
                manual=not deployment.compose_exists,
            )
        )
        findings.append(
            RepairFinding(
                check="admin_service",
                ok=deployment.service_defined,
                detail=f"Service {deployment.service}",
                suggestion=""
                if deployment.service_defined
                else "Add the Admin service with install-admin-console.sh",
                manual=not deployment.service_defined,
            )
        )
        findings.append(
            RepairFinding(
                check="admin_environment",
                ok=deployment.env_exists,
                detail=f"Environment file {deployment.env_file}",
                suggestion=""
                if deployment.env_exists
                else "Recreate the Admin environment file with install-admin-console.sh",
                manual=not deployment.env_exists,
            )
        )

        for name, path in (
            ("config", self.paths.ems_config_dir),
            ("data", self.paths.ems_data_dir),
            ("backups", self.paths.ems_backups_dir),
        ):
            exists = path.is_dir()
            findings.append(
                RepairFinding(
                    check=f"bind_path_{name}",
                    ok=exists,
                    detail=f"{path}",
                    suggestion="" if exists else "Recreate the missing directory after confirmation",
                    action="" if exists else f"create_bind_path:{name}",
                )
            )

        if docker_ok:
            container = self.docker.inspect_container(self.config.admin_container)
            if not container.exists:
                findings.append(
                    RepairFinding(
                        check="admin_container",
                        ok=False,
                        detail="The Admin container does not exist",
                        suggestion="Reinstall the selected Admin version",
                        action="recreate_admin",
                    )
                )
            elif container.state != CONTAINER_RUNNING:
                findings.append(
                    RepairFinding(
                        check="admin_container",
                        ok=False,
                        detail=f"The Admin container is {container.state}",
                        suggestion="Start the Admin container",
                        action="start_admin",
                    )
                )
            elif container.restart_count > 3:
                findings.append(
                    RepairFinding(
                        check="admin_container",
                        ok=False,
                        detail=f"The Admin container restarted {container.restart_count} times",
                        suggestion="Review the Admin logs and reinstall the selected version",
                        action="",
                    )
                )
            else:
                findings.append(
                    RepairFinding(
                        check="admin_container",
                        ok=True,
                        detail=f"The Admin container is {container.state}",
                    )
                )
                findings.append(
                    RepairFinding(
                        check="admin_health",
                        ok=container.health in (HEALTH_HEALTHY, HEALTH_NONE),
                        detail=f"Health check reports {container.health}",
                        suggestion=""
                        if container.health in (HEALTH_HEALTHY, HEALTH_NONE)
                        else "Restart the Admin container",
                        action=""
                        if container.health in (HEALTH_HEALTHY, HEALTH_NONE)
                        else "restart_admin",
                    )
                )
                findings.append(self._api_finding())
                identity = self._identity_finding(container)
                if identity is not None:
                    findings.append(identity)

        findings.append(self._port_finding())
        return findings

    def _identity_finding(self, container):
        """Is the container running the Admin the appliance last verified?"""

        expected = str((self.known_good.current() or {}).get("admin_digest") or "")
        if not expected:
            return None
        active, _ = self._active_digest(container)
        if active == expected:
            return RepairFinding(
                check="admin_identity",
                ok=True,
                detail="The running image matches the recorded known-good digest",
            )
        return RepairFinding(
            check="admin_identity",
            ok=False,
            detail=f"The container runs {active or 'an unidentifiable image'}, "
            f"not the known-good {expected}",
            suggestion="Reinstall the recorded Admin version",
            action="recreate_admin",
        )

    def _api_finding(self):
        """A container with no Docker health check proves nothing on its own."""

        probe = self.health.probe(self.config.admin_health_url)
        if not probe.reachable:
            return RepairFinding(
                check="admin_api",
                ok=False,
                detail=f"The Admin HTTP endpoint {self.config.admin_health_url} did not answer",
                suggestion="Restart the Admin container and review its logs",
                action="restart_admin",
            )
        version = str(probe.version or "")
        return RepairFinding(
            check="admin_api",
            ok=True,
            detail="The Admin HTTP endpoint answered"
            + (f" and reports {version}" if version else " without a version"),
        )

    @staticmethod
    def _listens_on(line, port):
        fields = line.split()
        if len(fields) < 4 or fields[0] != "LISTEN":
            return False
        return fields[3].rsplit(":", 1)[-1] == port

    def _port_owners(self, port):
        """The containers Docker says publish this port, or None if unprovable.

        A daemon that is not running has no containers, so nothing it manages
        can hold the port — that is a proof, not an unknown.
        """

        try:
            if self.docker.daemon_state()["state"] != DAEMON_RUNNING:
                return []
            return self.docker.containers_publishing_port(port)
        except (DockerError, ValueError):
            return None

    def _indeterminate_port(self, port, detail, suggestion=""):
        return RepairFinding(
            check="admin_port",
            ok=True,
            indeterminate=True,
            detail=f"Port {port} could not be checked: {detail}",
            suggestion=suggestion,
        )

    def _port_finding(self):
        """Ownership of the Admin port is proven by Docker, never by a name.

        A port check that could not run has found nothing, not "nothing", and a
        ``docker-proxy`` process says nothing about which container created it.
        """

        port = str(self.config.admin_port)
        if self.runner is None or not self.runner.available("ss"):
            return self._indeterminate_port(
                port,
                "ss is not installed",
                "Install iproute2 to let the appliance inspect listening ports",
            )
        result = self.runner.run("ss", ["-ltnp"], timeout=15)
        if not result.ok:
            reason = (result.stderr or "").strip().splitlines()
            return self._indeterminate_port(
                port,
                "ss failed" + (f" ({reason[0]})" if reason else ""),
                "Review the agent sandbox and the iproute2 installation",
            )

        listeners = [
            line.strip()
            for line in (result.stdout or "").splitlines()
            if self._listens_on(line, port)
        ]
        if not listeners:
            return RepairFinding(check="admin_port", ok=True, detail=f"Port {port} is available")

        owners = self._port_owners(port)
        if owners is None:
            return self._indeterminate_port(
                port,
                "the Docker engine could not say which container publishes it",
                "Start Docker so the appliance can prove who owns the port",
            )
        if len(listeners) == 1 and owners == [self.config.admin_container]:
            return RepairFinding(
                check="admin_port",
                ok=True,
                detail=f"Port {port} is published by the {self.config.admin_container} container",
            )
        return RepairFinding(
            check="admin_port",
            ok=False,
            detail=f"Port {port} is used by: " + "; ".join(listeners[:3]),
            suggestion="Stop the conflicting process manually; the appliance never kills it",
            action="",
        )

    # --- execution -------------------------------------------------------

    def execute(self, operation):
        try:
            validate_operation(
                operation,
                repositories=self.config.images.repositories,
                architectures=self.config.supported_architectures,
            )
        except OperationSchemaError as exc:
            # Nothing has been touched yet: the record is refused before the
            # first Docker call, not repaired by guessing a missing field.
            return self._preflight_failure(
                operation, STATE_FAILED_TERMINAL, exc.code, exc.message
            )
        if operation.type == TYPE_INSTALL:
            return self._execute_install(operation)
        if operation.type == TYPE_ROLLBACK:
            return self._execute_rollback(operation)
        if operation.type == TYPE_LIFECYCLE:
            return self._execute_lifecycle(operation)
        if operation.type == TYPE_REPAIR:
            return self._execute_repair(operation)
        raise AdminLifecycleError("unknown_operation_type", f"{operation.type} is not executable")

    def _execute_install(self, operation):
        target = operation.requested_target
        repository = target["repository"]
        tag = target["tag"]
        digest = target.get("digest") or ""
        reference = target.get("reference") or build_digest_ref(repository, digest)
        bootstrap = is_bootstrap(target)

        # A plan can be confirmed minutes later, by which time the image may be
        # gone and the deployment file may have been edited. Both are checked
        # while the healthy Admin is still running and nothing was touched.
        self._advance(operation, "verifying_target_image", detail=reference)
        try:
            self._require_planned_deployment(target)
            self._require_planned_current_admin(target)
            self._require_planned_image(target, reference, require_labels=True)
        except AdminLifecycleError as exc:
            return self._preflight_failure(
                operation, STATE_FAILED_TERMINAL, exc.code, exc.message
            )

        # The first write of the whole operation. Everything above proved the
        # appliance is still in the state the operator confirmed.
        if bootstrap:
            try:
                self._bootstrap_deployment(operation, tag)
            except AdminLifecycleError as exc:
                return self._bootstrap_failure(operation, exc)

        deployment = self.deployment()
        saved = snapshot(deployment)
        before = self.detect()

        self._advance(operation, "recording_known_good")
        if before["healthy"] and before["digest"]:
            self.known_good.record(
                admin_image=f"{repository}:{before['version'] or 'unknown'}",
                admin_digest=before["digest"],
                admin_version=before["version"] or "unknown",
                admin_reference=build_digest_ref(repository, before["digest"]),
                revision=before["revision"],
                architecture=(before.get("image") or {}).get("architecture", ""),
                compose_hash=saved.compose_hash,
                environment_hash=environment_hash(deployment),
            )

        # The deployment file is pinned before the running Admin is touched, so
        # a write failure leaves the healthy container in place.
        self._advance(operation, "pinning_digest", detail=reference)
        try:
            apply_digest(deployment, repository, digest, tag=tag)
        except (DeploymentError, OSError) as exc:
            saved.restore()
            self.operations.finish(
                operation.operation_id,
                STATE_FAILED_RECOVERABLE,
                stage="digest_pin_failed",
                error={
                    "code": getattr(exc, "code", "digest_pin_failed"),
                    "message": f"the immutable image reference could not be written: {exc}",
                },
            )
            raise AdminLifecycleError(
                "digest_pin_failed", "the immutable image reference could not be written"
            )

        try:
            self._advance(operation, "stopping_admin")
            self._stop_admin(deployment)

            self._advance(operation, "recreating_admin", detail=reference)
            self._compose_up(deployment)

            self._advance(operation, "waiting_for_health", state=STATE_VERIFYING)
            verification = self._verify_admin(expected_version=tag, expected_digest=digest)
            if not verification["verified"]:
                raise AdminLifecycleError(
                    "admin_unhealthy",
                    lifecycle_failure_message("install", verification),
                )
        except (AdminLifecycleError, DockerError, DeploymentError) as exc:
            if bootstrap:
                # There is no previous Admin to restore: this appliance had
                # none. Saying so beats running a rollback that would only
                # start the image that just failed.
                return self._bootstrap_failure(operation, exc)
            return self._rollback_after_failure(operation, saved, before, exc)

        self._advance(operation, "recording_result")
        entry = self.known_good.record(
            admin_image=f"{repository}:{tag}",
            admin_digest=digest,
            admin_version=tag,
            admin_reference=reference,
            revision=target.get("revision", ""),
            oci_source=target.get("source", ""),
            architecture=target.get("architecture", ""),
            compose_hash=admin_deployment.compose_hash(
                self._read_text(deployment.compose_file) or ""
            ),
            environment_hash=environment_hash(deployment),
            healthcheck=HEALTHCHECK_PASSED,
        )
        result = {
            "installed_version": tag,
            "digest": digest,
            "reference": reference,
            "known_good": entry,
            "verification": self._verification_summary(),
            "bootstrapped": bootstrap,
        }
        self.operations.finish(operation.operation_id, STATE_SUCCEEDED, result=result)
        return result

    def _bootstrap_failure(self, operation, exc):
        """A first installation failed, so the appliance still has no Admin.

        Recoverable, not terminal: nothing that existed was replaced, and the
        operator can plan again once the reported cause is fixed.
        """

        # Deliberately not ``admin_untouched``: that says the Admin which was
        # running is still running, and here there never was one.
        payload = {
            "stage": "bootstrap",
            "bootstrap_failed": True,
            # Whether the installer got as far as writing files decides what a
            # retry is: another bootstrap, or a normal install over what is
            # already there. Reporting it is what lets the operator tell.
            "deployment_created": self.deployment().service_defined,
            "current": self._verification_summary(),
        }
        self.operations.finish(
            operation.operation_id,
            STATE_FAILED_RECOVERABLE,
            stage="bootstrap_failed",
            result=payload,
            error={
                "code": getattr(exc, "code", "bootstrap_failed"),
                "message": f"{getattr(exc, 'message', exc)}; this appliance still has no "
                "Admin installation",
            },
        )
        return payload

    def _recovery_target(self, operation):
        """What automatic recovery must put back, and prove it put back.

        The identity captured at preflight wins over the known-good history:
        the Admin that was running is what the operator expects to return, and
        it exists even when nothing was ever recorded as known good.
        """

        recovery = dict(operation.requested_target.get("recovery") or {})
        if recovery.get("digest"):
            repository = recovery.get("repository") or self.config.images.admin_repository
            return {
                "repository": repository,
                "digest": recovery["digest"],
                "version": recovery.get("version", ""),
                "reference": recovery.get("reference")
                or build_digest_ref(repository, recovery["digest"]),
            }
        previous = self.known_good.current() or self.known_good.previous()
        if previous and previous.get("admin_digest"):
            repository = str(previous.get("admin_image", "")).rpartition(":")[0]
            return {
                "repository": repository,
                "digest": previous["admin_digest"],
                "version": previous.get("admin_version", ""),
                "reference": previous.get("admin_reference")
                or build_digest_ref(repository, previous["admin_digest"]),
            }
        return {"repository": "", "digest": "", "version": "", "reference": ""}

    def _rollback_after_failure(self, operation, saved, before, exc):
        """Restore the previous deployment bytes and the captured recovery digest."""

        self.operations.advance(
            operation.operation_id,
            "rolling_back",
            state=STATE_ROLLING_BACK,
            detail=getattr(exc, "code", "install_failed"),
        )
        target = self._recovery_target(operation)
        expected_digest = target["digest"]
        try:
            deployment = self.deployment()
            self._stop_admin(deployment)
            saved.restore()
            restored = resolve_deployment(self.paths, self.config)
            if expected_digest:
                self._require_local_image(target["reference"])
                apply_digest(
                    restored,
                    target["repository"],
                    expected_digest,
                    tag=target["version"],
                )
                restored = resolve_deployment(self.paths, self.config)
            self._compose_up(restored)
            # The version alone does not identify what came back up: an image
            # carrying the same version label but different bytes is not the
            # Admin that was running before the operation started.
            verification = self._verify_admin(
                expected_version=target["version"],
                expected_digest=expected_digest,
            )
        except (AdminLifecycleError, DockerError, DeploymentError, OSError) as rollback_exc:
            self.operations.finish(
                operation.operation_id,
                STATE_FAILED_TERMINAL,
                stage="rollback_failed",
                error={
                    "code": "rollback_failed",
                    "message": f"{getattr(exc, 'message', exc)}; rollback also failed: "
                    f"{getattr(rollback_exc, 'message', rollback_exc)}",
                },
            )
            raise AdminLifecycleError(
                "rollback_failed",
                "the Admin update failed and the previous version could not be restored",
            )

        state = STATE_ROLLED_BACK if verification["healthy"] else STATE_FAILED_TERMINAL
        self.operations.finish(
            operation.operation_id,
            state,
            stage="rolled_back" if verification["healthy"] else "rollback_unhealthy",
            result={
                "restored_version": target["version"] or before["version"],
                "verification": verification,
            },
            error={"code": getattr(exc, "code", "install_failed"), "message": str(exc)},
        )
        if state == STATE_FAILED_TERMINAL:
            raise AdminLifecycleError(
                "rollback_unhealthy",
                "the Admin update failed and the restored version is not healthy",
            )
        raise AdminLifecycleError(
            getattr(exc, "code", "install_failed"),
            f"{exc}; the previous known-good Admin was restored",
        )

    def _validated_rollback_target(self, record):
        """Repository, digest and canonical reference of a stored known-good record.

        Rollback must never resolve a tag again, so the stored digest is the
        authority and a record whose reference does not match it is refused.
        """

        repository = str(record.get("repository") or record.get("admin_image") or "")
        repository = repository.rpartition(":")[0] if ":" in repository else repository
        repository = repository or self.config.images.admin_repository
        repository = validate_image_repository(repository, self.config.images.repositories)
        digest = validate_digest(str(record.get("digest") or record.get("admin_digest") or ""))
        reference = build_digest_ref(repository, digest)
        stored = str(record.get("reference") or record.get("admin_reference") or "")
        if stored and stored != reference:
            raise ValidationError(
                "known_good_reference_mismatch",
                f"the stored reference {stored!r} does not match the stored digest",
            )
        return repository, digest, reference

    def _execute_rollback(self, operation):
        """Prepare everything reversible first; the running Admin is stopped last."""

        target = operation.requested_target
        tag = str(target.get("tag") or "")

        self._advance(operation, "preflight")
        try:
            repository, digest, reference = self._validated_rollback_target(target)
        except ValidationError as exc:
            return self._preflight_failure(
                operation, STATE_FAILED_TERMINAL, "invalid_known_good_record", exc.message
            )

        self._advance(operation, "verifying_target_image", detail=reference)
        try:
            self._require_planned_deployment(target)
            self._require_planned_current_admin(target)
            # A rollback deploys an image this appliance installed and validated
            # once; its labels belong to that older release, so the digest and
            # the architecture are what have to still hold.
            self._require_planned_image(target, reference, require_labels=False)
        except AdminLifecycleError as exc:
            return self._preflight_failure(
                operation, STATE_FAILED_TERMINAL, exc.code, exc.message
            )

        deployment = self.deployment()
        saved = snapshot(deployment)

        # Writing the deployment proves it can be updated. It changes nothing
        # about the container that is running right now.
        self._advance(operation, "pinning_digest", detail=reference)
        try:
            apply_digest(deployment, repository, digest, tag=tag)
        except (DeploymentError, OSError) as exc:
            # The restore must not raise on top of the write failure, or the
            # operation would end terminal and hide that nothing was touched.
            self._safe_restore(saved)
            return self._preflight_failure(
                operation,
                STATE_FAILED_RECOVERABLE,
                getattr(exc, "code", "rollback_pin_failed"),
                "the rollback image reference could not be written",
            )

        # Everything reversible is done; from here the Admin is interrupted.
        self._advance(operation, "stopping_admin")
        self._stop_admin(deployment)

        self._advance(operation, "restoring_admin", detail=reference)
        deployment = self.deployment()
        try:
            self._compose_up(deployment)
        except (AdminLifecycleError, DockerError, DeploymentError) as exc:
            return self._rollback_recovery_failure(operation, saved, exc)

        self._advance(operation, "waiting_for_health", state=STATE_VERIFYING)
        verification = self._verify_admin(expected_version=tag, expected_digest=digest)
        if not verification["verified"]:
            return self._rollback_recovery_failure(
                operation,
                saved,
                AdminLifecycleError(
                    verification["error"], lifecycle_failure_message("rollback", verification)
                ),
                verification=verification,
            )

        entry = self.known_good.record(
            admin_image=f"{repository}:{tag}",
            admin_digest=digest,
            admin_version=tag,
            admin_reference=reference,
            compose_hash=admin_deployment.compose_hash(
                self._read_text(deployment.compose_file) or ""
            ),
            environment_hash=environment_hash(deployment),
        )
        result = {
            "installed_version": tag,
            "digest": digest,
            "reference": reference,
            "known_good": entry,
            "verification": verification,
        }
        self.operations.finish(operation.operation_id, STATE_SUCCEEDED, result=result)
        return result

    @staticmethod
    def _safe_restore(saved):
        try:
            saved.restore()
        except (DeploymentError, OSError):
            return False
        return True

    def _preflight_failure(self, operation, state, code, message):
        """Nothing was mutated, so say so: the healthy Admin is still running."""

        payload = {
            "stage": "preflight",
            "admin_untouched": True,
            "current": self._verification_summary(),
        }
        self.operations.finish(
            operation.operation_id,
            state,
            stage="preflight_failed",
            result=payload,
            error={
                "code": code,
                "message": f"{message}; the running Admin was not stopped",
            },
        )
        return payload

    def _rollback_recovery_failure(self, operation, saved, exc, *, verification=None):
        """Mutation had started: put the exact previous Admin back, or say it is gone.

        The authority is the identity captured before the rollback started, not
        whatever the deployment resolves to now: an image that carries the same
        version label but different bytes is not the Admin that was running.
        """

        self.operations.advance(
            operation.operation_id,
            "restoring_previous_admin",
            state=STATE_ROLLING_BACK,
            detail=getattr(exc, "code", "rollback_failed"),
        )
        target = self._recovery_target(operation)
        recovery = {"restored": False, "error": "", "expected": target}
        try:
            if not target["digest"]:
                raise AdminLifecycleError(
                    "recovery_identity_unavailable",
                    "no immutable identity was captured for the Admin that was running",
                )
            self._stop_admin(self.deployment())
            if not self._safe_restore(saved):
                raise DeploymentError(
                    "deployment_restore_failed", "the previous deployment could not be written back"
                )
            self._require_local_image(target["reference"])
            restored_deployment = resolve_deployment(self.paths, self.config)
            apply_digest(
                restored_deployment,
                target["repository"],
                target["digest"],
                tag=target["version"],
            )
            self._compose_up(resolve_deployment(self.paths, self.config))
            check = self._verify_admin(
                expected_version=target["version"], expected_digest=target["digest"]
            )
            recovery = {
                "restored": bool(check["verified"]),
                "verification": check,
                "expected": target,
                "error": "" if check["verified"] else check["error"],
            }
        except (AdminLifecycleError, DockerError, DeploymentError, OSError) as recovery_exc:
            recovery = {
                "restored": False,
                "expected": target,
                "error": str(getattr(recovery_exc, "message", recovery_exc)),
            }

        payload = {
            "stage": "rollback_failed",
            "admin_untouched": False,
            "verification": verification,
            "recovery": recovery,
        }
        self.operations.finish(
            operation.operation_id,
            STATE_FAILED_TERMINAL,
            stage="rollback_unhealthy",
            result=payload,
            error={
                "code": getattr(exc, "code", "rollback_failed"),
                "message": str(getattr(exc, "message", exc))
                + (
                    "; the previously running Admin was restored"
                    if recovery["restored"]
                    else "; the previous Admin could not be restored"
                ),
            },
        )
        return payload

    def _execute_lifecycle(self, operation):
        action = operation.requested_target.get("action")
        container = self.config.admin_container
        self._advance(operation, f"{action}_admin")
        if action == "start":
            result = self.docker.start_container(container)
        elif action == "stop":
            result = self.docker.stop_container(container)
        else:
            result = self.docker.restart_container(container)

        # Whether the command succeeded or not, the host is asked what it is now:
        # a docker exit code is not evidence that the Admin is usable.
        self._advance(operation, f"verifying_{action}", state=STATE_VERIFYING)
        running = action != "stop"
        expected = self.known_good.current() or {}
        verification = self.verify_admin(
            expect_running=running,
            expected_version=str(expected.get("admin_version") or "") if running else "",
            expected_digest=str(expected.get("admin_digest") or "") if running else "",
        )
        state = self.docker.inspect_container(container)
        payload = {
            "action": action,
            "container": state.to_dict(),
            "verification": verification,
            "command_ok": bool(result.ok),
        }

        if result.ok and verification["verified"]:
            self.operations.finish(operation.operation_id, STATE_SUCCEEDED, result=payload)
            return payload

        if verification["verified"]:
            code = "admin_lifecycle_failed"
            message = f"docker {action} reported an error"
        else:
            code = verification["error"]
            message = lifecycle_failure_message(action, verification)

        self.operations.finish(
            operation.operation_id,
            self._lifecycle_failure_state(verification),
            stage=f"{action}_unverified",
            result=payload,
            error={"code": code, "message": message},
        )
        return payload

    @staticmethod
    def _lifecycle_failure_state(verification):
        """A missing container or a wrong image will not fix itself on retry."""

        if verification["error"] in ("container_missing", "image_mismatch"):
            return STATE_MANUAL_ACTION_REQUIRED
        return STATE_FAILED_RECOVERABLE

    def _execute_repair(self, operation):
        actions = list(operation.requested_target.get("actions") or [])
        manual = list(operation.requested_target.get("manual_actions") or [])
        applied = []
        for action in actions:
            self._advance(operation, f"repair_{action.split(':')[0]}")
            self.last_repair_verification = None
            entry = {"action": action, "result": self._apply_repair(action)}
            if self.last_repair_verification is not None:
                entry["verification"] = self.last_repair_verification
            applied.append(entry)

        self._advance(operation, "verifying_repair", state=STATE_VERIFYING)
        findings = self.inspect_repair()
        remaining = [item.to_dict() for item in findings if not item.ok]
        # An action that ran but could not be verified is a failure even when
        # the re-inspection happens to find nothing else wrong.
        unverified = [item for item in applied if item["result"] != "verified"]
        payload = {
            "applied": applied,
            "manual_actions": manual,
            "findings": [item.to_dict() for item in findings],
            "remaining_findings": remaining,
            "unverified_actions": unverified,
        }

        if not remaining and not unverified:
            self.operations.finish(operation.operation_id, STATE_SUCCEEDED, result=payload)
            return payload

        if remaining and not unverified and all(item["manual"] for item in remaining):
            self.operations.finish(
                operation.operation_id,
                STATE_MANUAL_ACTION_REQUIRED,
                stage="manual_action_required",
                result=payload,
                error={
                    "code": "manual_action_required",
                    "message": "no automatic repair is available for the remaining findings",
                },
            )
            return payload

        needs_operator = {"image_mismatch", "container_missing"}
        state = (
            STATE_MANUAL_ACTION_REQUIRED
            if any(item["result"] in needs_operator for item in unverified)
            else STATE_FAILED_RECOVERABLE
        )
        self.operations.finish(
            operation.operation_id,
            state,
            stage="repair_incomplete",
            result=payload,
            error={
                "code": unverified[0]["result"] if unverified else "repair_incomplete",
                "message": repair_failure_message(remaining, unverified),
            },
        )
        return payload

    def _apply_repair(self, action):
        """Run one allowlisted action and verify the state it promised."""

        if action == "start_docker":
            result = self.systemd.start(UNIT_DOCKER) if self.systemd else None
            if result is not None and not result.ok:
                return "start_failed"
            state = self.docker.daemon_state()
            return "verified" if state["state"] == DAEMON_RUNNING else "api_unreachable"

        container = self.config.admin_container
        if action == "start_admin":
            if not self.docker.start_container(container).ok:
                return "start_failed"
            return self._verified_or_reason()

        if action == "restart_admin":
            if not self.docker.restart_container(container).ok:
                return "restart_failed"
            return self._verified_or_reason()

        if action == "recreate_admin":
            try:
                self._compose_up(self.deployment())
            except AdminLifecycleError:
                return "recreate_failed"
            return self._verified_or_reason()

        if action.startswith("create_bind_path:"):
            name = action.split(":", 1)[1]
            target = self.paths.export_paths().get(name)
            if target is None:
                return "unknown_path"
            try:
                target.mkdir(parents=True, exist_ok=True)
            except OSError:
                return "create_failed"
            return "verified" if target.is_dir() else "create_failed"

        return "unsupported"

    def _verified_or_reason(self):
        """A repair action reports the failing fact, never a bare "unhealthy"."""

        expected = self.known_good.current() or {}
        verification = self.verify_admin(
            expected_version=str(expected.get("admin_version") or ""),
            expected_digest=str(expected.get("admin_digest") or ""),
        )
        self.last_repair_verification = verification
        return "verified" if verification["verified"] else verification["error"]

    def _container_healthy(self, name):
        state = self.docker.inspect_container(name)
        return state.state == CONTAINER_RUNNING and state.health in (HEALTH_HEALTHY, HEALTH_NONE)

    # --- helpers ---------------------------------------------------------

    def _read_text(self, path):
        try:
            return path.read_text(encoding="utf-8")
        except OSError:
            return None

    def _require_docker(self, state):
        if state["docker"]["state"] != DAEMON_RUNNING:
            raise AdminLifecycleError(
                "docker_unavailable", "the Docker daemon is not running; start Docker first"
            )

    def _require_deployment(self, state):
        deployment = state["deployment"]
        if not deployment["compose_exists"]:
            raise AdminLifecycleError(
                "compose_file_missing", "no Admin compose file was found on this appliance"
            )
        if not deployment["service_defined"]:
            raise AdminLifecycleError(
                "admin_service_missing",
                f"the compose file does not define the {deployment['service']} service",
            )

    def _require_no_admin_transition(self):
        """Stand back while the Admin console is replacing itself.

        Two layers write the same deployment files. Admin is the one that can
        be halfway through, with a worker running and a durable record, so this
        side yields -- but only to a record that is still live. An expired or
        unreadable transition is the wedged state an operator came here to fix,
        and refusing to help then would make the recovery tool part of the
        problem.
        """

        record = admin_transition.read_transition(
            admin_transition.transition_path(self.paths, self.deployment())
        )
        self.last_admin_transition = record
        if admin_transition.blocks_admin_mutation(record):
            raise AdminLifecycleError(
                "admin_transition_in_flight", admin_transition.refusal_message(record)
            )
        return record

    def _deployment_state(self, state):
        """Is there a deployment to edit, or one to create? Refuse anything else.

        A missing deployment is not an error for an installation — it is the
        state a flashed appliance is in, and the one case where this appliance
        may write a deployment file. A *container* without a deployment is not
        that case: something else created it, and creating files around it
        would be a second authority over the same Admin.
        """

        if state["deployment"]["service_defined"]:
            return False
        if state["installed"]:
            # Something created this container without a compose file the
            # appliance can find. Writing one around it would put a second
            # authority on the same Admin, so the exact gap is reported and
            # the operator repairs it instead.
            self._require_deployment(state)
        return True

    def _bootstrap_owner(self):
        """The uid/gid a created deployment will belong to."""

        try:
            return self.bootstrap.identity()
        except BootstrapError as exc:
            raise AdminLifecycleError(exc.code, exc.message)

    def _require_absent_deployment(self):
        """A bootstrap was planned against nothing; nothing is what it may find.

        This is the bootstrap's half of the deployment authority: an install
        that edits files proves the bytes did not change, and an install that
        creates them proves there is still nothing to overwrite.
        """

        deployment = self.deployment()
        if deployment.service_defined:
            raise AdminLifecycleError(
                "deployment_appeared_since_plan",
                "an Admin deployment was created after the plan; plan again so the "
                "existing one is updated instead of overwritten",
            )
        return True

    def _bootstrap_deployment(self, operation, tag):
        """Create the deployment with the packaged installer, then prove it exists."""

        self._advance(operation, "creating_deployment", detail=str(bootstrap_installer_path()))
        try:
            owner = self.bootstrap.identity(claim=True)
            record = self.bootstrap.run(tag=tag, uid=owner[0], gid=owner[1])
        except BootstrapError as exc:
            raise AdminLifecycleError(exc.code, exc.message)

        deployment = self.deployment()
        if not deployment.service_defined:
            raise AdminLifecycleError(
                "bootstrap_incomplete",
                f"the Admin installer ran but no compose file defines the "
                f"{deployment.service} service",
            )
        return record

    def _stop_admin(self, deployment):
        container = self.docker.inspect_container(self.config.admin_container)
        if container.exists:
            self.docker.stop_container(self.config.admin_container)
        return True

    def _compose_up(self, deployment):
        try:
            result = self.docker.compose_up_service(deployment.service)
        except DockerError as exc:
            raise AdminLifecycleError(exc.code, exc.message)
        if not result.ok:
            raise AdminLifecycleError("compose_up_failed", "docker compose could not start Admin")
        return True

    def _compose_hash(self):
        return admin_deployment.compose_hash(
            self._read_text(self.deployment().compose_file) or ""
        )

    def _environment_hash(self):
        return environment_hash(self.deployment())

    def _deployment_fingerprint(self):
        """Everything about the deployment a confirmed plan is bound to."""

        deployment = self.deployment()
        return {
            "compose_file": str(deployment.compose_file),
            "compose_hash": self._compose_hash(),
            "environment_file": str(deployment.env_file),
            "environment_hash": environment_hash(deployment),
        }

    def _capture_recovery_identity(self, state):
        """The immutable identity the appliance must be able to restore.

        A known-good record may not exist yet — an Admin installed before this
        appliance, or one that never became healthy, has none. Rollback still
        has to prove what came back, so the identity is captured here, at
        preflight, while the current Admin is untouched.
        """

        from appliance.operation_schema import RECOVERY_SCHEMA_VERSION

        fingerprint = self._deployment_fingerprint()
        if not state["installed"]:
            return {
                "schema_version": RECOVERY_SCHEMA_VERSION,
                "admin_present": False,
                "digest": "",
                "version": "",
                "reference": "",
                "repository": "",
                "healthy": False,
                **fingerprint,
            }

        digest = str(state["digest"] or "")
        if not digest:
            # Health does not weaken this: an Admin that exists may only be
            # replaced when the appliance can prove what it would put back.
            raise AdminLifecycleError(
                "recovery_identity_unavailable",
                "the installed Admin cannot be identified by an image digest, so an "
                "automatic rollback could not be verified; re-pull or reinstall the "
                "current Admin before replacing it",
            )
        repository = self.config.images.admin_repository
        return {
            "schema_version": RECOVERY_SCHEMA_VERSION,
            "admin_present": True,
            "digest": digest,
            "version": str(state["version"] or ""),
            "reference": build_digest_ref(repository, digest),
            "repository": repository,
            "healthy": bool(state["healthy"]),
            **fingerprint,
        }

    def _require_planned_deployment(self, target):
        """The deployment must still be the one the plan was made against."""

        if is_bootstrap(target):
            return self._require_absent_deployment()
        planned = str(target.get("compose_hash") or "")
        if not planned:
            raise AdminLifecycleError(
                "deployment_fingerprint_missing",
                "the plan records no Admin compose hash; plan again",
            )
        deployment = self.deployment()
        if not deployment.compose_exists:
            raise AdminLifecycleError(
                "deployment_changed_since_plan",
                "the Admin compose file disappeared after the plan was created",
            )
        if str(target.get("compose_file") or str(deployment.compose_file)) != str(
            deployment.compose_file
        ):
            raise AdminLifecycleError(
                "deployment_changed_since_plan",
                "a different Admin compose file is in use than when the plan was created",
            )
        if self._compose_hash() != planned:
            raise AdminLifecycleError(
                "deployment_changed_since_plan",
                "the Admin compose file changed after the plan was created; plan again",
            )
        planned_environment = str(target.get("environment_hash") or "")
        if not planned_environment:
            raise AdminLifecycleError(
                "deployment_fingerprint_missing",
                "the plan records no Admin environment hash; plan again",
            )
        if self._environment_hash() != planned_environment:
            raise AdminLifecycleError(
                "deployment_changed_since_plan",
                "the Admin environment file changed after the plan was created; plan again",
            )
        return True

    def _require_planned_current_admin(self, target):
        """The Admin about to be replaced must still be the one that was planned."""

        recovery = target.get("recovery") or {}
        if not recovery.get("admin_present"):
            return True
        expected = str(recovery.get("digest") or "")
        if not expected:
            raise AdminLifecycleError(
                "recovery_identity_unavailable",
                "the plan records an installed Admin without a recovery digest; plan again",
            )
        active = str(self.detect()["digest"] or "")
        if active != expected:
            raise AdminLifecycleError(
                "current_admin_changed_since_plan",
                "the running Admin image changed after the plan was created; plan again",
            )
        return True

    def _require_planned_image(self, target, reference, *, require_labels):
        """Prove the image is still exactly the one the plan described.

        ``architecture``, ``source`` and ``revision`` are strings in a record; a
        partial write can change them and nothing on the host would notice. What
        decides is the image the reference resolves to right now, inspected
        again while the healthy Admin is still running.
        """

        self._require_local_image(reference)
        image = self.docker.inspect_image(reference)
        if not image.exists:
            raise AdminLifecycleError("image_inspect_failed", f"{reference} cannot be inspected")

        planned = str(target.get("digest") or "")
        if image.digest and image.digest != planned:
            raise AdminLifecycleError(
                "target_digest_mismatch",
                f"{reference} resolves to {image.digest}, not the planned {planned}",
            )
        try:
            validate_architecture(image.architecture, self.config.supported_architectures)
        except ValidationError as exc:
            raise AdminLifecycleError(exc.code, exc.message)
        for field, label in (("architecture", ""), ("revision", LABEL_REVISION), (
            "source",
            LABEL_SOURCE,
        )):
            expected = str(target.get(field) or "")
            if not expected:
                continue
            observed = (
                image.architecture if not label else str(image.labels.get(label) or "")
            )
            if observed != expected:
                raise AdminLifecycleError(
                    f"target_{field}_mismatch",
                    f"{reference} reports {field} {observed!r}, not the planned {expected!r}",
                )
        if require_labels:
            try:
                validate_oci_labels(
                    image.labels,
                    requested_tag=str(target.get("tag") or ""),
                    expected_source=self.config.images.expected_source,
                    legacy_exempt_tags=self.config.images.legacy_exempt_tags,
                )
            except ValidationError as exc:
                raise AdminLifecycleError(exc.code, exc.message)
        return True

    def _require_local_image(self, reference):
        """Make sure the immutable reference is present before deploying it."""

        image = self.docker.inspect_image(reference)
        if image.exists:
            return True
        try:
            self.docker.pull_image(reference)
        except DockerError as exc:
            raise AdminLifecycleError(
                "known_good_image_unavailable",
                f"the recorded image {reference} is not available: {exc.message}",
            )
        if not self.docker.inspect_image(reference).exists:
            raise AdminLifecycleError(
                "known_good_image_unavailable",
                f"the recorded image {reference} is not available",
            )
        return True

    def _await_container(self, *, running):
        """Poll until the container reaches the expected lifecycle state."""

        container = self.docker.inspect_container(self.config.admin_container)
        deadline = self._time() + self.config.health_timeout_seconds
        while True:
            if running:
                settled = container.state == CONTAINER_RUNNING and container.health != HEALTH_STARTING
            else:
                settled = container.state != CONTAINER_RUNNING
            if settled or self._time() >= deadline:
                return container
            self._sleep(2)
            container = self.docker.inspect_container(self.config.admin_container)

    def _active_digest(self, container):
        if not container.exists or not container.image:
            return "", {}
        image = self.docker.inspect_image(container.image)
        return (image.digest or ""), dict(image.labels or {})

    def verify_admin(self, *, expected_version="", expected_digest="", expect_running=True):
        """Prove what the Admin actually is, not that a Docker command returned 0.

        A running process is not a verified Admin: a container without a Docker
        health check would otherwise count as healthy while its HTTP endpoint
        never came up, and a container running the wrong image would count as a
        successful install.
        """

        container = self._await_container(running=expect_running)
        record = {
            "expect_running": expect_running,
            "container_exists": container.exists,
            "container_state": container.state,
            "container_health": container.health,
            "image_reference": container.image,
            "expected_digest": expected_digest,
            "active_digest": "",
            "digest_matches": None,
            "api_reachable": False,
            "api_status": 0,
            "version": "",
            "version_source": "",
            "expected_version": expected_version,
            "version_matches": None,
            "failures": [],
        }

        if not container.exists:
            record["failures"].append("container_missing")
            return self._finish_verification(record)

        if not expect_running:
            if container.state == CONTAINER_RUNNING:
                record["failures"].append("container_still_running")
            return self._finish_verification(record)

        if container.state != CONTAINER_RUNNING:
            record["failures"].append("container_not_running")
            return self._finish_verification(record)
        if container.health == HEALTH_UNHEALTHY:
            record["failures"].append("container_unhealthy")

        digest, labels = self._active_digest(container)
        record["active_digest"] = digest
        if expected_digest:
            record["digest_matches"] = bool(digest) and digest == expected_digest
            if not record["digest_matches"]:
                record["failures"].append("image_mismatch")

        probe = self.health.wait_until_healthy(
            self.config.admin_health_url, timeout=self.config.health_timeout_seconds
        )
        record["api_reachable"] = bool(probe.reachable)
        record["api_status"] = int(getattr(probe, "status_code", 0) or 0)
        if not probe.reachable:
            record["failures"].append("api_unreachable")

        version = str(probe.version or "")
        source = "api" if version else ""
        if not version:
            # An Admin whose health payload carries no version is still
            # identifiable through the OCI label of the image it runs.
            version = str(labels.get(LABEL_VERSION) or "")
            source = "image_label" if version else ""
        record["version"] = version
        record["version_source"] = source
        if not version:
            record["failures"].append("version_unreadable")
        elif expected_version:
            record["version_matches"] = normalize_version(version) == normalize_version(
                expected_version
            )
            if not record["version_matches"]:
                record["failures"].append("version_mismatch")

        return self._finish_verification(record)

    @staticmethod
    def _finish_verification(record):
        record["verified"] = not record["failures"]
        # ``healthy`` is the name the operation result and the browser use.
        record["healthy"] = record["verified"]
        record["error"] = record["failures"][0] if record["failures"] else ""
        return record

    def _verify_admin(self, *, expected_version="", expected_digest=""):
        return self.verify_admin(
            expected_version=expected_version, expected_digest=expected_digest
        )

    def _verification_summary(self):
        container = self.docker.inspect_container(self.config.admin_container)
        return {"container_state": container.state, "container_health": container.health}

    def _advance(self, operation, stage, *, state=None, detail=None):
        self.operations.advance(operation.operation_id, stage, state=state, detail=detail)
        if self._operation_log is not None:
            self._operation_log.record(
                operation.operation_id, stage, operation_type=operation.type, detail=detail
            )
        return stage
