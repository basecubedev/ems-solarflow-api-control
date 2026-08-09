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
from appliance.admin_deployment import (
    DeploymentError,
    apply_image,
    resolve_deployment,
    snapshot,
)
from appliance.docker_backend import (
    CONTAINER_RUNNING,
    DAEMON_RUNNING,
    HEALTH_HEALTHY,
    HEALTH_NONE,
    DockerError,
)
from appliance.known_good import HEALTHCHECK_PASSED
from appliance.operations import (
    STATE_FAILED_TERMINAL,
    STATE_ROLLED_BACK,
    STATE_ROLLING_BACK,
    STATE_SUCCEEDED,
    STATE_VERIFYING,
)
from appliance.releases import ReleaseCatalogue, ReleaseResolutionError, resolve_channel
from appliance.validation import (
    ValidationError,
    build_digest_ref,
    build_image_ref,
    normalize_version,
    validate_architecture,
    validate_oci_labels,
)

TYPE_INSTALL = "admin.install"
TYPE_ROLLBACK = "admin.rollback"
TYPE_REPAIR = "admin.repair"
TYPE_LIFECYCLE = "admin.lifecycle"

LABEL_VERSION = "org.opencontainers.image.version"
LABEL_REVISION = "org.opencontainers.image.revision"
LABEL_SOURCE = "org.opencontainers.image.source"


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

    def to_dict(self):
        return {
            "check": self.check,
            "ok": self.ok,
            "detail": self.detail,
            "suggestion": self.suggestion,
            "action": self.action,
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
        catalogue=None,
        time_fn=None,
        sleep=None,
        operation_log=None,
    ):
        self.paths = paths
        self.config = config
        self.docker = docker
        self.known_good = known_good
        self.health = health
        self.operations = operations
        self.runner = runner
        self.catalogue = catalogue or ReleaseCatalogue(config)
        self._time = time_fn or time.time
        self._sleep = sleep or time.sleep
        self._operation_log = operation_log

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
        self._require_deployment(state)

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

        identical = bool(state["digest"]) and state["digest"] == image.digest
        if identical and not reinstall:
            raise AdminLifecycleError(
                "target_identical",
                "the requested version is already installed; choose Reinstall to install it again",
            )

        plan = {
            "type": TYPE_INSTALL,
            "repository": repository,
            "target_tag": target.tag,
            "target_channel": target.channel,
            "target_digest": image.digest,
            "target_revision": str(image.labels.get(LABEL_REVISION) or ""),
            "target_architecture": image.architecture,
            "legacy_labels_accepted": label_result["legacy_exempt"],
            "reinstall": bool(reinstall),
            "current_version": state["version"],
            "current_digest": state["digest"],
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
        self.operations.update_target(
            operation.operation_id,
            {
                "repository": repository,
                "tag": target.tag,
                "digest": image.digest,
                "revision": plan["target_revision"],
                "reinstall": bool(reinstall),
            },
        )
        operation.requested_target.update(
            {
                "repository": repository,
                "tag": target.tag,
                "digest": image.digest,
                "revision": plan["target_revision"],
                "reinstall": bool(reinstall),
            }
        )
        return plan

    def plan_rollback(self, operation):
        state = self.detect()
        self._advance(operation, "preflight")
        self._require_docker(state)
        self._require_deployment(state)

        previous = self.known_good.previous()
        if not previous:
            raise AdminLifecycleError(
                "no_previous_known_good", "no previous known-good Admin has been recorded"
            )

        values = {
            "repository": str(previous.get("admin_image", "")).rpartition(":")[0]
            or self.config.images.admin_repository,
            "tag": previous.get("admin_version", ""),
            "digest": previous.get("admin_digest", ""),
        }
        operation.requested_target.update(values)
        self.operations.update_target(operation.operation_id, values)
        return {
            "type": TYPE_ROLLBACK,
            "target": previous,
            "current_version": state["version"],
            "current_digest": state["digest"],
            "deployment": state["deployment"],
        }

    def plan_lifecycle(self, operation, action):
        state = self.detect()
        self._require_docker(state)
        operation.requested_target.update({"action": action})
        self.operations.update_target(operation.operation_id, {"action": action})
        return {
            "type": TYPE_LIFECYCLE,
            "action": action,
            "container": self.config.admin_container,
            "current_state": state["container"]["state"],
        }

    def plan_repair(self, operation):
        findings = self.inspect_repair()
        actions = [item.action for item in findings if item.action]
        operation.requested_target.update({"actions": actions})
        self.operations.update_target(operation.operation_id, {"actions": actions})
        return {
            "type": TYPE_REPAIR,
            "findings": [item.to_dict() for item in findings],
            "actions": actions,
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
                else "Regenerate the Admin section from the appliance template",
                action="" if deployment.compose_exists else "regenerate_admin_compose",
            )
        )
        findings.append(
            RepairFinding(
                check="admin_service",
                ok=deployment.service_defined,
                detail=f"Service {deployment.service}",
                suggestion=""
                if deployment.service_defined
                else "Regenerate the Admin section from the appliance template",
                action="" if deployment.service_defined else "regenerate_admin_compose",
            )
        )
        findings.append(
            RepairFinding(
                check="admin_environment",
                ok=deployment.env_exists,
                detail=f"Environment file {deployment.env_file}",
                suggestion="" if deployment.env_exists else "Recreate the Admin environment file",
                action="" if deployment.env_exists else "regenerate_admin_environment",
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

        findings.append(self._port_finding())
        return findings

    def _port_finding(self):
        port = str(self.config.admin_port)
        if self.runner is None or not self.runner.available("ss"):
            return RepairFinding(
                check="admin_port",
                ok=True,
                detail=f"Port {port} could not be checked on this host",
            )
        result = self.runner.run("ss", ["-ltnp"], timeout=15)
        conflicting = [
            line.strip()
            for line in (result.stdout or "").splitlines()
            if f":{port} " in line or line.strip().endswith(f":{port}")
        ]
        owned = any(self.config.admin_container in line or "docker" in line for line in conflicting)
        if conflicting and not owned:
            return RepairFinding(
                check="admin_port",
                ok=False,
                detail=f"Port {port} is used by: " + "; ".join(conflicting[:3]),
                suggestion="Stop the conflicting process manually; the appliance never kills it",
                action="",
            )
        return RepairFinding(check="admin_port", ok=True, detail=f"Port {port} is available")

    # --- execution -------------------------------------------------------

    def execute(self, operation):
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

        deployment = self.deployment()
        saved = snapshot(deployment)
        before = self.detect()

        self._advance(operation, "recording_known_good")
        if before["healthy"] and before["digest"]:
            self.known_good.record(
                admin_image=f"{repository}:{before['version'] or 'unknown'}",
                admin_digest=before["digest"],
                admin_version=before["version"] or "unknown",
                revision=before["revision"],
                compose_hash=saved.compose_hash,
            )

        try:
            self._advance(operation, "stopping_admin")
            self._stop_admin(deployment)

            self._advance(operation, "recreating_admin", detail=f"{repository}:{tag}")
            apply_image(deployment, repository, tag)
            if digest:
                self._pin_tag_to_digest(repository, tag, digest)
            self._compose_up(deployment)

            self._advance(operation, "waiting_for_health", state=STATE_VERIFYING)
            verification = self._verify_admin(expected_version=tag)
            if not verification["healthy"]:
                raise AdminLifecycleError(
                    "admin_unhealthy", verification.get("error") or "the new Admin did not become healthy"
                )
        except (AdminLifecycleError, DockerError, DeploymentError) as exc:
            return self._rollback_after_failure(operation, saved, before, exc)

        self._advance(operation, "recording_result")
        entry = self.known_good.record(
            admin_image=f"{repository}:{tag}",
            admin_digest=digest,
            admin_version=tag,
            revision=target.get("revision", ""),
            compose_hash=admin_deployment.compose_hash(
                self._read_text(deployment.compose_file) or ""
            ),
            healthcheck=HEALTHCHECK_PASSED,
        )
        result = {
            "installed_version": tag,
            "digest": digest,
            "known_good": entry,
            "verification": self._verification_summary(),
        }
        self.operations.finish(operation.operation_id, STATE_SUCCEEDED, result=result)
        return result

    def _rollback_after_failure(self, operation, saved, before, exc):
        """Restore the previous deployment bytes and the previous known-good digest."""

        self.operations.advance(
            operation.operation_id,
            "rolling_back",
            state=STATE_ROLLING_BACK,
            detail=getattr(exc, "code", "install_failed"),
        )
        previous = self.known_good.current() or self.known_good.previous()
        try:
            deployment = self.deployment()
            self._stop_admin(deployment)
            saved.restore()
            restored = resolve_deployment(self.paths, self.config)
            if previous and previous.get("admin_digest"):
                repository, _, previous_tag = str(previous.get("admin_image", "")).rpartition(":")
                if repository and previous_tag:
                    self._pin_tag_to_digest(
                        repository, previous_tag, previous["admin_digest"], allow_pull=True
                    )
            self._compose_up(restored)
            verification = self._verify_admin(
                expected_version=(previous or {}).get("admin_version", "")
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
                "restored_version": (previous or {}).get("admin_version", before["version"]),
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

    def _execute_rollback(self, operation):
        target = operation.requested_target
        repository = target.get("repository") or self.config.images.admin_repository
        tag = target.get("tag") or ""
        digest = target.get("digest") or ""
        deployment = self.deployment()

        self._advance(operation, "stopping_admin")
        self._stop_admin(deployment)

        self._advance(operation, "restoring_admin", detail=f"{repository}:{tag}")
        apply_image(deployment, repository, tag)
        if digest:
            self._pin_tag_to_digest(repository, tag, digest, allow_pull=True)
        self._compose_up(deployment)

        self._advance(operation, "waiting_for_health", state=STATE_VERIFYING)
        verification = self._verify_admin(expected_version=tag)
        if not verification["healthy"]:
            self.operations.finish(
                operation.operation_id,
                STATE_FAILED_TERMINAL,
                stage="rollback_unhealthy",
                error={"code": "admin_unhealthy", "message": "the restored Admin is not healthy"},
                result={"verification": verification},
            )
            raise AdminLifecycleError("admin_unhealthy", "the restored Admin is not healthy")

        entry = self.known_good.record(
            admin_image=f"{repository}:{tag}",
            admin_digest=digest,
            admin_version=tag,
            compose_hash=admin_deployment.compose_hash(
                self._read_text(deployment.compose_file) or ""
            ),
        )
        result = {"installed_version": tag, "digest": digest, "known_good": entry}
        self.operations.finish(operation.operation_id, STATE_SUCCEEDED, result=result)
        return result

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

        if not result.ok:
            self.operations.finish(
                operation.operation_id,
                STATE_FAILED_TERMINAL,
                stage=f"{action}_failed",
                error={"code": "admin_lifecycle_failed", "message": f"docker {action} failed"},
            )
            raise AdminLifecycleError("admin_lifecycle_failed", f"docker {action} failed")

        state = self.docker.inspect_container(container)
        payload = {"action": action, "container": state.to_dict()}
        self.operations.finish(operation.operation_id, STATE_SUCCEEDED, result=payload)
        return payload

    def _execute_repair(self, operation):
        actions = list(operation.requested_target.get("actions") or [])
        applied = []
        for action in actions:
            self._advance(operation, f"repair_{action.split(':')[0]}")
            applied.append({"action": action, "result": self._apply_repair(action)})

        self._advance(operation, "verifying_repair", state=STATE_VERIFYING)
        findings = [item.to_dict() for item in self.inspect_repair()]
        payload = {"applied": applied, "findings": findings}
        self.operations.finish(operation.operation_id, STATE_SUCCEEDED, result=payload)
        return payload

    def _apply_repair(self, action):
        if action == "start_docker":
            return "docker_start_requested"
        if action in ("start_admin", "restart_admin", "recreate_admin"):
            container = self.config.admin_container
            if action == "start_admin":
                return "started" if self.docker.start_container(container).ok else "start_failed"
            if action == "restart_admin":
                return "restarted" if self.docker.restart_container(container).ok else "restart_failed"
            deployment = self.deployment()
            return "recreated" if self._compose_up(deployment) else "recreate_failed"
        if action.startswith("create_bind_path:"):
            name = action.split(":", 1)[1]
            target = self.paths.export_paths().get(name)
            if target is None:
                return "unknown_path"
            target.mkdir(parents=True, exist_ok=True)
            return "created"
        if action in ("regenerate_admin_compose", "regenerate_admin_environment"):
            return "manual_action_required"
        return "unsupported"

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

    def _pin_tag_to_digest(self, repository, tag, digest, *, allow_pull=False):
        """Point the mutable tag at the exact digest that was verified."""

        digest_ref = build_digest_ref(repository, digest)
        image = self.docker.inspect_image(digest_ref)
        if not image.exists:
            if not allow_pull:
                return False
            try:
                self.docker.pull_image(digest_ref)
            except DockerError as exc:
                raise AdminLifecycleError(
                    "known_good_image_unavailable",
                    f"the known-good image {digest} is not available locally: {exc.message}",
                )
        return self.docker.tag_image(digest_ref, f"{repository}:{tag}").ok

    def _verify_admin(self, *, expected_version=""):
        container = self.docker.inspect_container(self.config.admin_container)
        deadline = self._time() + self.config.health_timeout_seconds
        while container.state != CONTAINER_RUNNING or container.health == "starting":
            if self._time() >= deadline:
                break
            self._sleep(2)
            container = self.docker.inspect_container(self.config.admin_container)

        container_ok = container.state == CONTAINER_RUNNING and container.health in (
            HEALTH_HEALTHY,
            HEALTH_NONE,
        )
        probe = self.health.wait_until_healthy(
            self.config.admin_health_url, timeout=self.config.health_timeout_seconds
        )

        version_ok = True
        if expected_version and probe.version:
            version_ok = normalize_version(probe.version) == normalize_version(expected_version)

        return {
            "healthy": bool(container_ok and probe.reachable and version_ok),
            "container_state": container.state,
            "container_health": container.health,
            "api_reachable": probe.reachable,
            "api_version": probe.version,
            "version_matches": version_ok,
            "error": ""
            if container_ok and probe.reachable and version_ok
            else "admin_verification_failed",
        }

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
